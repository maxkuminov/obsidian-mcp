"""Transfer service: token lifecycle, streaming writer, SSRF-guarded fetch.

Three independent pieces sit here because they are the three things the
`/transfer/*` routes and the transfer MCP tools need and nothing else does.

**Token lifecycle** (design D1, D2, D4). A transfer token is a capability: it
is the *only* thing authorising a public, session-less request, so everything
it may do is pinned at mint time and re-checked from the database at use time.
Single-use is a linearizable claim — one conditional `UPDATE … RETURNING`
committed before a single body byte is read — not an `if` around a `SELECT`.
The pre-publication check takes `SELECT … FOR UPDATE` on the token, the
credential and the user and *holds those locks across the filesystem publish*,
so a revocation racing a publish either waits or wins; it never interleaves.

**Streaming writer** (D6, D7). Bytes go to a descriptor-anchored temp file with
the cap enforced during the stream, then through `vault_fs.publish`. Nothing is
buffered in memory and nothing lands at the target path until the gate says the
identity is still good.

**SSRF guard** (D11). This container sits next to Postgres, Ollama and a
registry, so `import_from_url` is a request to make outbound connections from
inside the trust boundary. The guard canonicalises, applies an explicit deny
policy (Python's `is_global` admits multicast and the NAT64 prefix, so it is
the last check rather than the only one), and pins the connection to the
address that was validated — re-resolving between check and connect is the
classic DNS-rebinding hole.
"""
from __future__ import annotations

import asyncio
import contextlib
import datetime
import hashlib
import ipaddress
import logging
import os
import re
import secrets
import socket
import time
import unicodedata
import weakref
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import AsyncContextManager, AsyncIterator, Awaitable, Callable, Iterable
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx
import idna
from sqlalchemy import and_, delete, literal, select, update
from sqlalchemy.orm import aliased

from src.auth.session import actor_columns
from src.config import settings
from src.models.db import APIKey, OAuthToken, TransferToken, User
from src.oauth.scope import token_has_write
from src.services import vault_fs
from src.services.vault import classify_bytes

logger = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════════════════════════
# Errors
# ════════════════════════════════════════════════════════════════════════════


class TransferError(Exception):
    """Base class for transfer-service failures."""


class TooLarge(TransferError):
    """The body exceeded the byte cap (declared or observed)."""


class Timeout(TransferError):
    """The stream stalled past the idle timeout or ran past its deadline."""


class QueueTimeout(TransferError):
    """No upload slot became free within `slot_timeout`, deadline still open.

    Deliberately **not** a `Timeout`. The two are different verdicts about the
    same request and the state machine treats them differently: a deadline
    overrun means the capability's window is gone, so the token is *consumed*
    and a retry must mint afresh; a full queue means the server was busy while
    the capability's window was still open and nothing was staged, so the claim
    is *released* and the very same link may be retried. The route maps this to
    503 + `Retry-After`, which is the honest status for "come back shortly" —
    408 would tell the caller their link had expired when it had not.
    """


class PrePublishAborted(TransferError):
    """The locked pre-publication re-validation refused; nothing was published."""


class PostPublishFailure(TransferError):
    """The bytes landed but the transaction recording that did not commit.

    Deliberately a distinct type: it is the one failure after which a caller
    must **not** release the claim. Releasing would make a token replayable
    over a path that already holds the uploaded file — and from here we cannot
    prove which of the two states the database is in, so `claimed` (terminal
    until the TTL passes) is the only safe answer.
    """


class CredentialNotUsable(TransferError):
    """The credential asking for a capability cannot back one.

    Raised at mint, before any row exists, so a refusal leaves nothing behind.
    The tools turn it into a tool-level error telling the agent to
    re-authenticate — which is actionable, unlike the link it would otherwise
    have been handed, whose only possible future is the uniform 404.
    """


class CredentialTooShortLived(CredentialNotUsable):
    """The minting credential dies too soon to back a usable capability.

    Redemption re-checks the *credential*, not just the token
    (`_credential_ok`), so a link can never outlive the key or access token
    that minted it. When that leaves less runway than `MIN_MINT_TTL_SECONDS`
    the honest answer is to mint nothing: a link that is already dead — or
    dead before a human can plausibly open it — is worse than an error,
    because the error tells the agent to re-authenticate and the link does
    not.
    """


class SSRFError(TransferError):
    """A URL, hop, or resolved address violated the outbound-fetch policy.

    The message names the violated rule: the caller is an agent that has to
    decide whether to retry with a different URL, and "blocked" alone is not
    actionable.
    """


# ════════════════════════════════════════════════════════════════════════════
# 1. Token lifecycle
# ════════════════════════════════════════════════════════════════════════════

TOKEN_BYTES = 32  # 256 bits, matching the api_keys convention
PUBLIC_ID_BYTES = 16  # 128 bits — an unguessable handle, not a capability
MIN_EXPIRES_IN = 60
MAX_EXPIRES_IN = 3600
# The least runway a mint will hand out. Below this the link would expire
# before the human it is meant for could open it, so `mint_token` refuses
# instead of advertising a deadline nobody can meet.
MIN_MINT_TTL_SECONDS = 30
PRUNE_AFTER = datetime.timedelta(days=1)

DIRECTIONS = ("upload", "download")
STATE_PENDING = "pending"
STATE_CLAIMED = "claimed"
STATE_COMPLETED = "completed"
STATE_CONSUMED = "consumed"


@dataclass(frozen=True)
class Identity:
    """The minting identity, exactly as `APIKeyMiddleware` resolved it.

    **At most one credential.** `APIKeyMiddleware` sets both ContextVars to
    None at the head of every request and then fills in exactly one branch, so
    a two-credential identity is unreachable today — and that is precisely why
    it is asserted here rather than assumed. The identity is written straight
    onto `transfer_tokens.key_id` / `.oauth_token_id`, whose CHECK constraint
    (migration 017) forbids the pair, and the attribution copied from that row
    onto `usage_logs` at redemption is shown to an operator as an audit trail:
    a row naming two actors records which of them minted it nowhere, so any
    label chosen for it would be an invention. Both None stays legal — that is
    the single-user and sandbox shape.
    """

    key_id: int | None = None
    oauth_token_id: int | None = None
    user_id: int | None = None

    def __post_init__(self) -> None:
        if self.key_id is not None and self.oauth_token_id is not None:
            raise ValueError(
                "A minting identity names at most one credential, and this one "
                f"names two (key_id={self.key_id}, "
                f"oauth_token_id={self.oauth_token_id}). Nothing records which "
                "of them minted a capability, so nothing may attribute one to "
                "either."
            )


@dataclass(frozen=True)
class LockedRows:
    """The rows `lock_for_publish` holds `FOR UPDATE` across the publish."""

    token: TransferToken
    credential: APIKey | OAuthToken
    user: User | None


def hash_token(token: str) -> str:
    """SHA-256 hex of a token — the only form that is ever stored or logged."""
    return hashlib.sha256(token.encode()).hexdigest()


def new_token() -> str:
    return secrets.token_urlsafe(TOKEN_BYTES)


def new_public_id() -> str:
    """The non-secret handle `request_upload` returns and `check_upload` takes.

    Opaque rather than the row id: `transfer_tokens.id` is sequential, so an
    `upload_id` built on it tells any holder how many transfers the server has
    ever minted and lets them guess neighbours. It is *not* a capability —
    `check_upload` still scopes every lookup to the calling identity — so 128
    bits is ample.
    """
    return secrets.token_urlsafe(PUBLIC_ID_BYTES)


# `secrets.token_urlsafe(16)` is always 22 characters of the URL-safe base64
# alphabet — no padding, no other punctuation.
PUBLIC_ID_RE = re.compile(r"[A-Za-z0-9_-]{22}")


def is_public_id(value: object) -> bool:
    """Does `value` have the exact shape `new_public_id()` produces?

    Checked *before* the value reaches a log line, not just before it reaches
    the database. `check_upload`'s argument is agent-supplied, and the two
    mistakes an agent actually makes are pasting the whole
    `…/transfer/upload#<token>` URL or the bare token in place of the handle —
    both of which would otherwise write a capability into `usage_logs`, which
    is precisely the place a capability must never appear.
    """
    return isinstance(value, str) and PUBLIC_ID_RE.fullmatch(value) is not None


def clamp_expires_in(expires_in: int | None) -> int:
    """Clamp a caller-supplied TTL into `[60, 3600]`; `None` → the configured default.

    Clamping rather than rejecting: an agent asking for a week gets an hour and
    a working link, which is the useful failure mode for a tool whose result is
    read by a model.
    """
    if expires_in is None:
        expires_in = settings.transfer_token_ttl_seconds
    return max(MIN_EXPIRES_IN, min(MAX_EXPIRES_IN, int(expires_in)))


def canonical_vault_root(path: str | Path) -> str:
    """Normalise a vault root exactly as `vault._vault_root` yields it.

    `_vault_root` returns `Path(settings.vault_path)` or `Path(user.vault_path)`
    with no `resolve()`, so this is `str(Path(...))` and nothing more.
    Deliberately does *not* consult `vault._user_vault_cache`: the whole point
    of the root check is to read what the database says now, not what this
    worker last cached (D4).
    """
    return str(Path(path))


def now_utc() -> datetime.datetime:
    """Wall-clock UTC — **the single clock domain for every transfer deadline**.

    Token expiry, the stream deadline the upload route enforces, and the
    classification `check_upload` reports all read this one function. They used
    to disagree: the route converted the absolute deadline to `time.monotonic()`
    once at claim time while the status tool compared wall clocks, so a
    realtime step made the two surfaces describe different instants — the tool
    calling a stream live that the route had already abandoned, or the reverse.
    A clock step now moves both together.

    Patch this (not `datetime.datetime.now`) to simulate a clock step.
    """
    return datetime.datetime.now(datetime.timezone.utc)


def _now() -> datetime.datetime:
    return now_utc()


def _deadline_remaining(deadline: float | datetime.datetime) -> float:
    """Seconds left on a deadline, in the clock domain it was expressed in.

    A `datetime` is an absolute UTC instant derived from the token row —
    `upload_stream_deadline` — and is measured against `now_utc()`, the same
    clock `check_upload` classifies with. A `float` is a `time.monotonic()`
    instant, which is what `import_from_url` passes: its fetch deadline is a
    private duration nothing else ever compares against, so it keeps the
    monotonic guarantee rather than inheriting the clock-step exposure.

    The trade-off of the wall-clock form is deliberate: a backward realtime
    step extends the stream. Two surfaces that agree about *which instant* the
    deadline is beats two that disagree but are individually monotonic, because
    the disagreement is the thing an agent relays to a human.
    """
    if isinstance(deadline, datetime.datetime):
        return (_as_aware(deadline) - now_utc()).total_seconds()
    return deadline - time.monotonic()


def _as_aware(value: datetime.datetime) -> datetime.datetime:
    """A UTC-aware copy of a database timestamp.

    Postgres `timestamptz` comes back aware through asyncpg, but a row built
    in a test — or by a future backend — may not be, and a naive/aware compare
    raises rather than returning a wrong answer. Normalising here keeps the
    expiry arithmetic total.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=datetime.timezone.utc)
    return value


def credential_expires_at(cred) -> datetime.datetime | None:
    """When the credential itself dies, or `None` if it never does.

    Mirrors the expiry half of `_credential_ok`: an `APIKey` may have a null
    `expires_at` and live forever, an `OAuthToken` may not — a null there is
    already unusable, so it reads as "expired at the epoch" rather than
    "immortal". Getting that backwards would mint links against dead tokens.
    """
    if isinstance(cred, APIKey):
        return _as_aware(cred.expires_at) if cred.expires_at is not None else None
    if isinstance(cred, OAuthToken):
        if cred.expires_at is None:
            return datetime.datetime.min.replace(tzinfo=datetime.timezone.utc)
        return _as_aware(cred.expires_at)
    raise CredentialTooShortLived(  # pragma: no cover - defensive
        "The credential backing this request cannot be re-validated"
    )


@dataclass(frozen=True)
class MintWindow:
    """The deadline a mint may actually promise, and what shortened it."""

    expires_at: datetime.datetime
    requested_expires_at: datetime.datetime
    credential_expires_at: datetime.datetime | None

    @property
    def clamped(self) -> bool:
        """Did the credential cut the requested TTL short?"""
        return self.expires_at < self.requested_expires_at


async def plan_mint_window(
    session, identity: Identity, expires_in: int | None, *, need_write: bool
) -> MintWindow:
    """`min(requested TTL, credential expiry)` — the only deadline worth showing.

    Redemption requires the *minting credential* to still be unexpired, so
    `transfer_tokens.expires_at` alone was never the effective lifetime. On the
    OAuth path — how the Claude.ai connector authenticates — an access token
    lives one hour, so any `expires_in` above its remaining life advertised a
    deadline the routes would refuse to honour, and the human met the uniform
    404 well inside the window the agent had quoted them.

    Clamping here (rather than only reporting) is what makes every downstream
    surface honest for free: the tool result, `/transfer/*/info` and both pages
    all read `row.expires_at`.

    **Called by `mint_token` itself, immediately before the INSERT and in the
    same transaction.** It is not a parameter and there is no way to hand
    `mint_token` a window computed elsewhere: an expiry decided by the caller
    is an expiry the caller can get wrong or stale, and the clamp is a security
    boundary, not a display detail.

    The credential is also re-validated with `_credential_ok` — the exact
    predicate redemption uses — so a key revoked, downgraded, deactivated or
    reassigned between the tool's permission check and this INSERT mints
    nothing rather than a row that can only 404.

    Raises `CredentialNotUsable` (`CredentialTooShortLived` for the runway
    case).
    """
    cred = await _load_credential(session, identity)
    if cred is None:
        # No credential to re-validate means no redemption can ever succeed
        # (`resolve_identity_ok` returns False for exactly this), so minting
        # would only produce a link that 404s.
        raise CredentialNotUsable(
            "This request carries no credential the transfer routes could "
            "re-validate, so any link minted for it would be refused. "
            "Re-authenticate and try again."
        )
    # `Identity` carries the `user_id` `_credential_ok` compares against, which
    # is the only field of a token row it reads — so the mint check and the
    # redemption check are literally the same function, and cannot drift.
    if not _credential_ok(cred, need_write=need_write, row=identity):
        raise CredentialNotUsable(
            "The credential you are authenticated with is not valid for this "
            "operation any more (revoked, downgraded, deactivated, expired, or "
            "reassigned to another user). Re-authenticate and try again."
        )

    now = _now()
    requested = now + datetime.timedelta(seconds=clamp_expires_in(expires_in))
    cred_expiry = credential_expires_at(cred)
    effective = requested if cred_expiry is None else min(requested, cred_expiry)
    if (effective - now).total_seconds() < MIN_MINT_TTL_SECONDS:
        raise CredentialTooShortLived(
            "The credential you are authenticated with expires in under "
            f"{MIN_MINT_TTL_SECONDS} seconds, so no usable link can be minted "
            "from it. Re-authenticate (or use a longer-lived API key) and try "
            "again."
        )
    return MintWindow(
        expires_at=effective,
        requested_expires_at=requested,
        credential_expires_at=cred_expiry,
    )


async def mint_token(
    session,
    direction: str,
    path: str,
    *,
    overwrite: bool,
    identity: Identity,
    vault_root: str,
    expected_fingerprint: dict | None,
    expires_in: int | None = None,
) -> tuple[str, TransferToken, MintWindow]:
    """Create one transfer capability; return `(token, row, window)`.

    The plaintext token is returned exactly once, to the minting tool. Only its
    hash is persisted. Every constraint the redemption route will enforce —
    direction, path, root, identity, overwrite, fingerprint, expiry — is fixed
    here, because the route has no session to derive any of it from.

    The expiry is `min(requested TTL, minting credential's own expiry)`, and it
    is computed **here**, by `plan_mint_window`, immediately before the INSERT
    and inside this transaction. There is deliberately no parameter for it: a
    caller-supplied window is a caller-supplied security boundary, and a stale
    one (computed before a revocation, or by a code path that forgot) would
    reinstate exactly the divergence this exists to remove. The window is
    *returned* instead, so the mint tools can say when the credential shortened
    the link.

    The minting request's denormalised actor is recorded on the row for the
    same reason and by the same discipline (issue #92): the redemption request
    is session-less and carries a capability, so it has no credential of its
    own to name in the usage log, and the joins it would otherwise rely on go
    NULL the moment the operator deletes the credential they are investigating.
    It is read from `current_actor` here rather than passed in, and it changes
    no decision — display and audit only.

    Raises `CredentialNotUsable` before writing anything when the credential
    cannot back a capability at all.
    """
    if direction not in DIRECTIONS:
        raise ValueError(f"Unknown transfer direction: {direction!r}")

    # Read the credential and decide the deadline here, in this transaction,
    # immediately before the INSERT: a token that outlives its credential is a
    # link that 404s at a time we advertised as valid, and a window computed
    # anywhere else could be stale by the time the row lands.
    window = await plan_mint_window(
        session, identity, expires_in, need_write=direction == "upload"
    )
    token = new_token()
    row = TransferToken(
        public_id=new_public_id(),
        token_hash=hash_token(token),
        direction=direction,
        state=STATE_PENDING,
        path=path,
        vault_root=canonical_vault_root(vault_root),
        overwrite=bool(overwrite),
        expected_fingerprint=expected_fingerprint,
        key_id=identity.key_id,
        oauth_token_id=identity.oauth_token_id,
        user_id=identity.user_id,
        expires_at=window.expires_at,
        # Who minted this, denormalised (issue #92). Read here, from the
        # ContextVar `APIKeyMiddleware` bound out of the credential row it had
        # already loaded, for the same reason the window is computed here: a
        # caller-supplied value is one the caller can get stale or wrong, and
        # this one has to be the credential *this* transaction is minting
        # against. It costs no query — the middleware did the reading — and it
        # goes through the same reader `_log_usage` uses, so a mint and a tool
        # call in one request cannot disagree about the caller or truncate
        # differently.
        #
        # Redemption is where it is needed and cannot be taken: that request
        # carries a capability, not a credential, so `_log_row` could only
        # attribute by join — and both joins go NULL exactly when an operator
        # deletes the credential they are investigating. Unset ContextVar (a
        # mint outside a request) leaves all three NULL and the row keeps its
        # pre-017 shape; nothing is inferred from `user_id`.
        **actor_columns(),
    )
    session.add(row)

    # Opportunistic prune: a day past expiry a row can no longer be redeemed by
    # any code path, and minting is the only moment we are already writing.
    await session.execute(
        delete(TransferToken).where(TransferToken.expires_at < _now() - PRUNE_AFTER)
    )
    await session.commit()
    await session.refresh(row)
    return token, row, window


def upload_stream_deadline(row) -> datetime.datetime:
    """`min(expires_at, claimed_at + TRANSFER_MAX_UPLOAD_SECONDS)`, absolute UTC.

    Two bounds for two different things: the capability's own TTL, and how long
    one claimed stream may hold a slot. The stricter one wins.

    It lives here, rather than in the route that enforces it, because
    `check_upload` has to answer the same question from the other side — is
    this claimed token still plausibly in flight, or is its stream already
    over? — and a second copy of the arithmetic would eventually disagree with
    the one the route runs, which is how a status tool starts lying.

    A claimed row always carries `claimed_at`; the `None` fallback is for a row
    that has not been claimed at all, where "the stream started now" is the
    most generous reading.
    """
    now = _now()
    claimed = _as_aware(row.claimed_at) if row.claimed_at else now
    return min(
        _as_aware(row.expires_at),
        claimed + datetime.timedelta(seconds=settings.transfer_max_upload_seconds),
    )


async def claim_upload(session, token: str) -> TransferToken | None:
    """Atomically move an upload token `pending → claimed`; `None` if it cannot.

    One conditional `UPDATE … RETURNING`, committed. `None` covers *every*
    unusable case — unknown, expired, wrong direction, already claimed,
    completed, consumed, or lost the race to a concurrent PUT — and the route
    maps all of them to the same 404. This must run to completion **before the
    handler reads any body byte**: reading first would let an unauthenticated
    caller stream gigabytes to disk on a token that was never valid.
    """
    result = await session.execute(
        update(TransferToken)
        .where(
            TransferToken.token_hash == hash_token(token),
            TransferToken.direction == "upload",
            TransferToken.state == STATE_PENDING,
            TransferToken.expires_at > _now(),
        )
        .values(state=STATE_CLAIMED, claimed_at=_now())
        .returning(TransferToken)
        # `populate_existing` because the minting session may still hold this
        # row in its identity map with `expire_on_commit=False`; without it the
        # RETURNING values are discarded and the caller reads back `pending`.
        .execution_options(synchronize_session=False, populate_existing=True)
    )
    row = result.scalars().first()
    await session.commit()
    return row


async def release_claim(session, row: TransferToken) -> bool:
    """Return a claimed token to `pending` after a handled pre-publication failure.

    Safe precisely because nothing was published: a 413, a 409, or a client
    disconnect leaves the vault untouched, so letting the human retry the same
    link costs nothing. Deadline and idle expiry use `consume` instead — there
    the request was terminated mid-stream and a retry should mint afresh.
    """
    result = await session.execute(
        update(TransferToken)
        .where(TransferToken.id == row.id, TransferToken.state == STATE_CLAIMED)
        .values(state=STATE_PENDING, claimed_at=None)
    )
    await session.commit()
    return result.rowcount == 1


async def consume(session, row: TransferToken) -> bool:
    """Burn a claimed token without completing it (deadline / idle timeout)."""
    result = await session.execute(
        update(TransferToken)
        .where(TransferToken.id == row.id, TransferToken.state == STATE_CLAIMED)
        .values(state=STATE_CONSUMED)
    )
    await session.commit()
    return result.rowcount == 1


async def complete_upload(
    session, row: TransferToken, size: int, sha256: str, mime: str, *, commit: bool = True
) -> bool:
    """Record a published upload as `completed`.

    `commit=False` is how the route folds this into the transaction that still
    holds the publish locks, so completion and the usage-log row land together
    or not at all.
    """
    result = await session.execute(
        update(TransferToken)
        .where(TransferToken.id == row.id, TransferToken.state == STATE_CLAIMED)
        .values(
            state=STATE_COMPLETED,
            completed_at=_now(),
            size=size,
            sha256=sha256,
            mime=mime,
        )
    )
    if commit:
        await session.commit()
    return result.rowcount == 1


@dataclass(frozen=True)
class TransferRefusal:
    """Why a bearer-protected transfer endpoint said no, plus the row if any.

    The *response* never carries this: `_not_found()` is byte-identical for
    every cause and that is the anti-oracle the whole surface rests on. The
    reason exists only so the server-side record can say what the response
    deliberately withholds.
    """

    reason: str
    row: TransferToken | None = None


async def classify_token_refusal(
    session, token: str, *, direction: str
) -> TransferRefusal:
    """Diagnose an already-refused token. **Read-only, and never an admission.**

    `lookup_token` and `claim_upload` are each *one filtered query* — hash,
    direction, `state = pending`, `expires_at > now` — and that is the
    linearizability argument for single-use redemption. Splitting either of
    them into a sequence of probes so the route could name the reason would
    rewrite that argument, so nothing here touches them: the decision has
    already been taken by the time this runs, this helper takes no decision of
    its own, and its answer changes no outcome.

    Because the four collapsed conditions overlap — a row can be expired *and*
    consumed, expired *and* of the wrong direction — the precedence is
    **total**, so a row matching several always yields the same reason:

    1. `unknown_token` — no row for the hash at all;
    2. `wrong_direction` — the row exists but belongs to the other endpoint;
    3. the row's state — `already_claimed` / `already_completed` /
       `already_consumed`;
    4. `expired` — still `pending`, but past `expires_at`;
    5. `claim_lost` — pending, unexpired, right direction: the conditional
       `UPDATE` lost a race to a concurrent redemption.

    State precedes expiry deliberately: a consumed token that has since aged
    out was *used*, and reporting it as "expired" would tell an operator the
    least interesting of the two true facts.

    It may raise — it issues a database read on a path whose whole contract is
    a fixed 404 — so the caller wraps it and answers `diagnosis_failed` rather
    than letting a dead connection turn a refusal into a 500.
    """
    result = await session.execute(
        select(TransferToken).where(TransferToken.token_hash == hash_token(token))
    )
    row = result.scalars().first()
    if row is None:
        return TransferRefusal("unknown_token")
    if row.direction != direction:
        return TransferRefusal("wrong_direction", row)
    if row.state == STATE_CLAIMED:
        return TransferRefusal("already_claimed", row)
    if row.state == STATE_COMPLETED:
        return TransferRefusal("already_completed", row)
    if row.state == STATE_CONSUMED:
        return TransferRefusal("already_consumed", row)
    if _as_aware(row.expires_at) <= _now():
        return TransferRefusal("expired", row)
    return TransferRefusal("claim_lost", row)


async def lookup_token(session, token: str, *, direction: str) -> TransferToken | None:
    """A usable (`pending`, unexpired) token of the given direction, or `None`.

    Used by the read-only endpoints (`…/info`, `download/file`). Download
    tokens are multi-use within their TTL and so never leave `pending`; an
    upload token that has been claimed or completed is deliberately invisible
    here, which is what makes the uniform 404 cover replay.
    """
    result = await session.execute(
        select(TransferToken)
        .where(
            TransferToken.token_hash == hash_token(token),
            TransferToken.direction == direction,
            TransferToken.state == STATE_PENDING,
            TransferToken.expires_at > _now(),
        )
        .execution_options(populate_existing=True)
    )
    return result.scalar_one_or_none()


def _same(column, value):
    """`column = value`, or `column IS NULL` when `value` is `None`.

    `column == None` renders as `= NULL`, which is never true — and would
    silently turn "single-user API key" into "matches nothing".
    """
    return column.is_(None) if value is None else column == value


def _minted_by_principal(identity: Identity):
    """WHERE fragment: was this row minted by the principal now asking?

    Two different questions, because the two credential kinds have different
    lifetimes:

    - An **API key** *is* the principal. It does not rotate, so the row must
      name this exact key and carry no OAuth credential.
    - An **OAuth access token** is one hour of one principal. The principal is
      the grant family behind it (`oauth_tokens.grant_id`, migration 014):
      every row minted from one `/authorize` approval shares it and every
      rotation inherits it. So the row's *minting* token and the *presenting*
      token merely have to be siblings in that family. Pinning the row id
      instead is issue #74 — after the hourly refresh the agent's own upload
      handle came back "not minted by this identity".

    The family predicate is a correlated `EXISTS` rather than a join on the
    outer select, so the lookup stays a single statement returning at most the
    one `public_id` row. `grant_id` is NOT NULL on `oauth_tokens`, so there is
    no null-equality trap here; if the presenting token's row has since been
    deleted the `EXISTS` is simply false and the answer is "not found", which
    is the fail-closed direction.

    **`client_id` is compared as well as `grant_id`.** A grant belongs to
    exactly one `(client_id, user_id)` — the invariant `src/oauth/grants.py`
    establishes at every write site — so post-014 the extra equality can never
    change the answer. It is here because the invariant is *asserted* rather
    than enforced by a constraint, and because this predicate is the access
    control: if a family ever did span two clients, the failure would be one
    client reading another's handles, which is the one thing this must not do.

    What it does **not** fix, because no predicate here can: 014's backfill
    groups pre-014 rows by `(client_id, user_id)`, which #64 accepted as
    approximate. Two consents by the same user *for the same client* made
    before 014 therefore share one family, and this predicate holds for both.
    See the "same-client, pre-014" limitation in CLAUDE.md.
    """
    if identity.oauth_token_id is None:
        return and_(
            _same(TransferToken.key_id, identity.key_id),
            TransferToken.oauth_token_id.is_(None),
        )
    minting = aliased(OAuthToken, name="minting_token")
    presenting = aliased(OAuthToken, name="presenting_token")
    return and_(
        TransferToken.key_id.is_(None),
        select(literal(1))
        .select_from(minting)
        .join(
            presenting,
            and_(
                presenting.grant_id == minting.grant_id,
                presenting.client_id == minting.client_id,
            ),
        )
        .where(
            minting.id == TransferToken.oauth_token_id,
            presenting.id == identity.oauth_token_id,
        )
        .exists(),
    )


async def lookup_by_public_id(
    session, public_id: str, *, identity: Identity, direction: str
) -> TransferToken | None:
    """A transfer row by its public handle, **scoped to the calling principal**.

    `check_upload` is the only read that is not gated by a capability, so this
    scoping is the access control. What it scopes *to* is the stable principal
    behind the request, not the credential row the request happened to arrive
    on:

    - **API key** — the key row itself, unchanged. A key is the principal: it
      does not rotate, and nothing else stands behind it.
    - **OAuth** — the *grant family* (`oauth_tokens.grant_id`, migration 014).
      An access token rotates roughly hourly, and rotation mints a brand-new
      `oauth_tokens` row for the same user, the same client and the same
      consent. Matching on the row id therefore made the agent's own handle
      stop being its own after a routine refresh: `check_upload` answered "no
      upload link with id … was minted by this identity" about a *completed*
      upload it had minted an hour earlier, which is the one message reserved
      for someone else's handle (issue #74). One consent is one principal, so
      the family is what the handle belongs to.

    The `user_id` comparison is kept on top of both. A grant family already
    belongs to exactly one `(client_id, user_id)` — the invariant
    `src/oauth/grants.py` establishes and 014's backfill preserves — so this is
    defence in depth for the OAuth path; on the API-key path it is the check
    that stops a key reassigned to another user from carrying its old handles
    across.

    What is deliberately *not* widened: a token from a **different** grant is
    still not found, even for the same user and the same client software. A
    second `/authorize` approval is a second consent, and the operator may
    revoke either one independently. Nor does this touch redemption — the
    `/transfer/*` routes still re-validate the exact credential row that
    minted the capability (see `resolve_identity_ok`). That asymmetry is
    intentional and costs nothing, because `plan_mint_window` clamps every
    capability's expiry to the minting credential's own: the minting access
    token outlives the link it minted unless it is revoked, and a revocation
    *should* kill the link.

    No state filter: reporting `expired` and `completed` is the whole job.
    """
    stmt = select(TransferToken).where(
        TransferToken.public_id == public_id,
        TransferToken.direction == direction,
        _same(TransferToken.user_id, identity.user_id),
        _minted_by_principal(identity),
    )
    result = await session.execute(stmt.execution_options(populate_existing=True))
    return result.scalar_one_or_none()


async def lookup_download(session, token: str) -> TransferToken | None:
    return await lookup_token(session, token, direction="download")


async def lookup_upload(session, token: str) -> TransferToken | None:
    return await lookup_token(session, token, direction="upload")


def _ownerless_in_multi_user(*user_ids) -> bool:
    """Is a `user_id IS NULL` being treated as an identity while multi-user is on?

    In single-user mode a null owner is normal — there are no users, and the
    vault root is the globally configured one. In multi-user mode it is a
    *stale* row: a key or token minted before the operator flipped the switch,
    which now belongs to nobody. `None == None` quietly passed the ownership
    comparison and `resolve_root_ok` / `locked_rows_ok` then authorised
    `settings.vault_path` outright, so an ownerless capability minted before
    the switch could still replace a file in whichever vault that setting names
    — after the MCP middleware had already started rejecting the same key.

    Fail closed: in multi-user mode an ownerless identity is nobody, and the
    routes answer with the uniform 404.
    """
    return settings.multi_user_mode and any(uid is None for uid in user_ids)


def _credential_ok(cred, *, need_write: bool, row: TransferToken) -> bool:
    """Exact predicates from D4 — kept in one place so the pre-publication
    re-check and the entry check cannot drift apart."""
    now = _now()
    # Before anything else: an ownerless row is not an identity here.
    if _ownerless_in_multi_user(getattr(row, "user_id", None), cred.user_id):
        return False
    if isinstance(cred, APIKey):
        if not cred.is_active:
            return False
        if cred.expires_at is not None and cred.expires_at <= now:
            return False
        if need_write and cred.permission != "readwrite":
            return False
    elif isinstance(cred, OAuthToken):
        if cred.revoked:
            return False
        if cred.expires_at is None or cred.expires_at <= now:
            return False
        if need_write and not token_has_write(cred.scope):
            return False
    else:  # pragma: no cover - defensive
        return False
    # The credential must still belong to the user the token was minted for.
    # Reassigning a key to another user must not carry its capabilities over.
    return cred.user_id == row.user_id


async def _load_credential(session, ref: TransferToken | Identity, *, lock: bool = False):
    """The credential row behind a token — or behind a bare `Identity`.

    Both carry `key_id` / `oauth_token_id`, and both need the same lookup: the
    redemption re-check reads it off the token, the mint clamp off the identity
    that is asking. One function so the two cannot disagree about which
    credential backs a transfer.
    """
    if ref.key_id is not None:
        stmt = select(APIKey).where(APIKey.id == ref.key_id)
    elif ref.oauth_token_id is not None:
        stmt = select(OAuthToken).where(OAuthToken.id == ref.oauth_token_id)
    else:
        # A token with no credential cannot be re-validated and is therefore
        # never usable. Mint always sets one.
        return None
    if lock:
        stmt = stmt.with_for_update()
    stmt = stmt.execution_options(populate_existing=True)
    return (await session.execute(stmt)).scalar_one_or_none()


async def resolve_identity_ok(session, row: TransferToken, *, need_write: bool) -> bool:
    """Is the minting identity still valid, right now, per D4's predicates?"""
    cred = await _load_credential(session, row)
    if cred is None:
        return False
    if not _credential_ok(cred, need_write=need_write, row=row):
        return False
    if row.user_id is not None:
        user = (
            await session.execute(
                select(User)
                .where(User.id == row.user_id)
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if user is None or not user.is_active:
            return False
    return True


async def resolve_root_ok(session, row: TransferToken) -> bool:
    """Does the token's stored vault root still equal the user's current root?

    Read from the database, never from `vault._user_vault_cache`: the panel
    clears that cache on reassignment in *its* worker, and another worker may
    still hold the old value. A reassigned user's token is dead (uniform 404),
    which is why this returns a bool rather than raising — the route must not
    turn a reassignment into a 500.
    """
    if row.user_id is None:
        # Defence in depth — `_credential_ok` already refuses this row in
        # multi-user mode, and this is the other half of the pair, so neither
        # check alone is the only thing standing between a stale ownerless
        # capability and the globally configured vault.
        if _ownerless_in_multi_user(row.user_id):
            return False
        return canonical_vault_root(settings.vault_path) == row.vault_root
    result = await session.execute(
        select(User.vault_path, User.is_active).where(User.id == row.user_id)
    )
    found = result.first()
    if found is None or not found.is_active or not found.vault_path:
        return False
    return canonical_vault_root(found.vault_path) == row.vault_root


async def lock_for_publish(session, token_id: int) -> LockedRows | None:
    """`SELECT … FOR UPDATE` the token, its credential and its user.

    Locks are taken in a fixed order (token → credential → user) so two
    publishers can never deadlock against each other. The caller holds the
    transaction open across the filesystem publish: that is what makes
    "revoked after the check, published anyway" impossible — a revocation has
    to take the same row locks, so it either waits for us or beats us.

    Returns `None` when the token row is gone (cascade-deleted with its
    credential) or is no longer claimed.
    """
    row = (
        await session.execute(
            select(TransferToken)
            .where(TransferToken.id == token_id, TransferToken.state == STATE_CLAIMED)
            .with_for_update()
            # The locked read must be the *database's* view; an identity-mapped
            # copy from earlier in this session would make the lock pointless.
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if row is None:
        return None

    cred = await _load_credential(session, row, lock=True)
    if cred is None:
        return None

    user = None
    if row.user_id is not None:
        user = (
            await session.execute(
                select(User)
                .where(User.id == row.user_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if user is None:
            return None
    return LockedRows(token=row, credential=cred, user=user)


def locked_rows_ok(locked: LockedRows, *, need_write: bool) -> bool:
    """Re-run D4's predicates against rows that are held `FOR UPDATE`."""
    if not _credential_ok(locked.credential, need_write=need_write, row=locked.token):
        return False
    if locked.token.user_id is not None:
        if locked.user is None or not locked.user.is_active:
            return False
        if not locked.user.vault_path:
            return False
        if canonical_vault_root(locked.user.vault_path) != locked.token.vault_root:
            return False
    elif _ownerless_in_multi_user(locked.token.user_id):
        # Same defensive pair as `resolve_root_ok`, on the locked rows.
        return False
    elif canonical_vault_root(settings.vault_path) != locked.token.vault_root:
        return False
    return True


# ════════════════════════════════════════════════════════════════════════════
# 2. Streaming writer
# ════════════════════════════════════════════════════════════════════════════

# One semaphore per event loop rather than one per process. The bound is what
# the design asks for; keying by loop keeps a test's loop from inheriting a
# semaphore another test's loop is waiting on, which would turn a concurrency
# bound into cross-test flakiness.
_upload_semaphores: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()

MIME_SNIFF_BYTES = 8192


def upload_semaphore() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    sem = _upload_semaphores.get(loop)
    if sem is None:
        sem = asyncio.Semaphore(settings.transfer_max_concurrent_uploads)
        _upload_semaphores[loop] = sem
    return sem


# How long one wait for the slot may block before the deadline is re-read. Small
# enough that a realtime step is noticed within a second, large enough that a
# 30 s queue wait is ~30 wake-ups rather than a spin.
SLOT_WAIT_SLICE = 1.0

#: Default bound on how long a request may queue for an upload slot.
DEFAULT_SLOT_TIMEOUT = 30.0


async def _acquire_upload_slot(
    sem: asyncio.Semaphore,
    *,
    deadline: float | datetime.datetime,
    slot_timeout: float,
) -> None:
    """Wait for an upload slot, bounded twice, in the deadline's own clock.

    Two bounds, because the queue and the capability answer different
    questions. `slot_timeout` bounds *this request's* patience — it is a
    duration nothing else reports on, so it is measured with `time.monotonic()`
    exactly as `idle_timeout` is. The stream `deadline` is an instant
    `check_upload` also reads, so it is re-derived through
    `_deadline_remaining` on **every slice** rather than converted once into a
    monotonic budget: a single `asyncio.wait_for(sem.acquire(), remaining)`
    would freeze the wall-clock deadline into the monotonic domain at the top of
    the wait, which is precisely the clock split `now_utc` exists to prevent.
    Sliced at `SLOT_WAIT_SLICE`, a realtime step moves this wait and the status
    tool together.

    Before #208 there was no bound at all here and the deadline was consulted
    only *after* the slot was won, so a queued upload's wait was unbounded — and
    it held a pooled database connection while it waited.

    **Precedence when the wait ends without a slot: deadline first.** An
    overrun raises the existing `Timeout`, which the route maps to `consume` /
    408 — the same verdict as an overrun during the body, which is what the
    state machine says about a request that outlived its capability. Only a wait
    cut short by `slot_timeout` *with deadline remaining* is a `QueueTimeout`
    (503, claim released): there the capability is still good and the server was
    merely busy. Getting this the wrong way round would tell a caller whose link
    had genuinely expired to retry it, and tell a caller whose link was fine
    that it had expired.
    """
    started = time.monotonic()
    while True:
        # Deadline first, on both the entry and the retry path.
        remaining_deadline = _deadline_remaining(deadline)
        if remaining_deadline <= 0:
            raise Timeout("Upload exceeded its deadline while waiting for an upload slot")
        remaining_slot = slot_timeout - (time.monotonic() - started)
        if remaining_slot <= 0:
            raise QueueTimeout(
                f"No upload slot became free within {slot_timeout:g}s; retry shortly"
            )
        try:
            await asyncio.wait_for(
                sem.acquire(),
                timeout=min(SLOT_WAIT_SLICE, remaining_slot, remaining_deadline),
            )
        except (asyncio.TimeoutError, TimeoutError):
            continue
        return


@dataclass
class GateHandle:
    """What `before_publish()` yields: the verdict, plus a way to record.

    `ok` answers "is the minting identity still valid" from rows the gate holds
    `FOR UPDATE`. `complete` is called by `stream_to_vault` **immediately after
    the publish and still inside the context**, so the completion row and the
    usage-log row are written by the same transaction that holds those locks —
    which is the only way "revoked after the check, published anyway" can be
    impossible. `session` is the gate's own session, exposed so a caller that
    needs to add more rows can use the same transaction.

    tasks.md types the gate as `Callable[[], Awaitable[bool]]` and then asks
    for a context that stays open across `publish`; a bool-returning awaitable
    cannot do that, and a context yielding a bare bool cannot record the
    result. This shape delivers both properties the task actually asks for.
    """

    ok: bool
    session: object | None = None
    on_complete: "Callable[[dict, bool], Awaitable[None]] | None" = None

    async def complete(self, result: dict, *, published: bool) -> None:
        if self.on_complete is not None:
            await self.on_complete(result, published)


PrePublishGate = Callable[[], AsyncContextManager[GateHandle]]


@dataclass(frozen=True)
class _IdentityRow:
    """The one field `_credential_ok` reads off a token row.

    `import_from_url` has no token — it is authorised by the caller's own MCP
    session — but the predicate that decides whether a credential may still
    write is exactly the same one, and it must not be reimplemented.
    """

    user_id: int | None


async def _load_identity_credential(session, identity: Identity, *, lock: bool = False):
    if identity.key_id is not None:
        stmt = select(APIKey).where(APIKey.id == identity.key_id)
    elif identity.oauth_token_id is not None:
        stmt = select(OAuthToken).where(OAuthToken.id == identity.oauth_token_id)
    else:
        # No credential to re-validate means nothing can be proven about the
        # caller at publish time, so the gate refuses. Every authenticated MCP
        # path sets one of the two.
        return None
    if lock:
        stmt = stmt.with_for_update()
    return (
        await session.execute(stmt.execution_options(populate_existing=True))
    ).scalar_one_or_none()


async def _identity_publish_ok(
    session, identity: Identity, *, vault_root: str, need_write: bool
) -> bool:
    """D4's predicates, run against rows this transaction holds `FOR UPDATE`."""
    cred = await _load_identity_credential(session, identity, lock=True)
    if cred is None:
        return False
    if not _credential_ok(
        cred, need_write=need_write, row=_IdentityRow(user_id=identity.user_id)
    ):
        return False
    expected = canonical_vault_root(vault_root)
    if identity.user_id is None:
        return canonical_vault_root(settings.vault_path) == expected
    user = (
        await session.execute(
            select(User)
            .where(User.id == identity.user_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if user is None or not user.is_active or not user.vault_path:
        return False
    return canonical_vault_root(user.vault_path) == expected


@asynccontextmanager
async def lock_identity_for_publish(
    session,
    identity: Identity,
    *,
    vault_root: str,
    need_write: bool = True,
    on_complete: "Callable[[dict, bool], Awaitable[None]] | None" = None,
) -> AsyncIterator[GateHandle]:
    """The publish gate for a write authorised by a *session*, not a token.

    `lock_for_publish` protects a capability: it locks the transfer row plus the
    credential that minted it. This is the same guarantee for `import_from_url`,
    which has no capability but does hold a network stream open for up to 30 s —
    long enough for the key to be revoked, downgraded, deleted, or pointed at a
    different vault while the bytes are still arriving. Without a gate the
    publish would land under whatever the identity looked like when the tool
    *started*.

    So: `SELECT … FOR UPDATE` on the caller's credential row and (multi-user)
    their user row, re-run D4's predicates against those locked rows, and check
    the vault root the database reports still equals the root captured when the
    tool began resolving paths. The locks are held for as long as the caller
    keeps the context open — which `stream_to_vault` keeps open across the
    filesystem publish — so a revocation either waits for us or wins outright.

    Lock order is credential → user, the same relative order `lock_for_publish`
    uses, so an import and an upload cannot deadlock against each other.

    `complete()` is a no-op unless the caller supplies `on_complete`: an import
    has no token row to move to `completed`, and its `usage_logs` row is written
    by `_tracked` after the tool returns.
    """
    async with session.begin():
        ok = await _identity_publish_ok(
            session, identity, vault_root=vault_root, need_write=need_write
        )
        yield GateHandle(
            ok=ok, session=session, on_complete=on_complete if ok else None
        )


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        view = view[written:]


def _fsync_payload(fd: int) -> None:
    """The blocking half of the staged-payload flush. Its own function so a
    test can make it slow, make it fail, or ask which thread ran it."""
    os.fsync(fd)


async def _flush_staged_payload(fd: int) -> None:
    """Make the staged bytes durable — **off the event loop, before the gate**.

    Two placements matter and both are load-bearing (#97).

    *Before the gate*, because `before_publish()` holds `SELECT … FOR UPDATE`
    on the token, the credential and the user across the publish. A flush of up
    to `MAX_FILE_WRITE_BYTES` is not something to do while every revocation,
    downgrade and reassignment of that credential queues behind it.

    *Off the loop*, because unlike `_drain`'s per-chunk `_write_all` — which
    lands in the page cache and returns — a single `fsync` waits for the whole
    body to reach the device. `TRANSFER_MAX_CONCURRENT_UPLOADS` of those inline
    would stall every other request in the process, search and panel included.

    A failure here is unambiguously **pre-publication**: nothing has been
    linked into place, the caller's `except` discards the staged bytes, and the
    upload route releases the claim so the human may retry the same link. It
    must never be dressed up as `PostPublishFailure`.
    """
    await asyncio.to_thread(_fsync_payload, fd)


async def _drain(
    chunks: AsyncIterator[bytes],
    fd: int,
    *,
    max_bytes: int,
    deadline: float | datetime.datetime,
    idle_timeout: float,
) -> tuple[int, str, bytes]:
    """Copy `chunks` to `fd`, counting, hashing and bounding as they arrive.

    Every deadline test goes through `_deadline_remaining`, so an upload's
    deadline is checked against the same wall clock `check_upload` classifies
    with. The *idle* timeout stays a duration measured by the event loop's
    monotonic clock — it bounds a gap between chunks, which nothing else
    reports on, so it has no surface to disagree with.
    """
    digest = hashlib.sha256()
    total = 0
    head = bytearray()
    iterator = chunks.__aiter__()

    while True:
        remaining = _deadline_remaining(deadline)
        if remaining <= 0:
            raise Timeout("Upload exceeded its deadline")
        try:
            chunk = await asyncio.wait_for(
                iterator.__anext__(), timeout=min(idle_timeout, remaining)
            )
        except StopAsyncIteration:
            break
        except (asyncio.TimeoutError, TimeoutError):
            if _deadline_remaining(deadline) <= 0:
                raise Timeout("Upload exceeded its deadline") from None
            raise Timeout(
                f"Upload stalled for more than {idle_timeout:g}s"
            ) from None
        if not chunk:
            continue
        total += len(chunk)
        if total > max_bytes:
            # Abort at cap+1: the point is never to have the whole oversize
            # body on disk, so this cannot wait for the stream to end.
            raise TooLarge(
                f"Upload exceeds the {max_bytes:,}-byte limit"
            )
        if len(head) < MIME_SNIFF_BYTES:
            head.extend(chunk[: MIME_SNIFF_BYTES - len(head)])
        digest.update(chunk)
        _write_all(fd, chunk)

    if _deadline_remaining(deadline) < 0:
        raise Timeout("Upload exceeded its deadline")
    return total, digest.hexdigest(), bytes(head)


async def stream_to_vault(
    row,
    chunks: AsyncIterator[bytes],
    *,
    max_bytes: int,
    content_length: int | None = None,
    deadline: float | datetime.datetime,
    idle_timeout: float = 30.0,
    slot_timeout: float = DEFAULT_SLOT_TIMEOUT,
    before_publish: PrePublishGate | None = None,
) -> dict:
    """Stream a body into the vault at `row.path`, capped, anchored, atomic.

    `row` only needs `vault_root`, `path`, `overwrite` and
    `expected_fingerprint` — the same four fields whether it is a
    `TransferToken` (upload route) or the ad-hoc descriptor `import_from_url`
    builds, which is why this takes a duck-typed row rather than the ORM class.

    `deadline` is an instant, not a duration. The upload route passes the
    absolute UTC instant `upload_stream_deadline(row)` returns, so the stream is
    enforced against the very instant `check_upload` reports; `import_from_url`
    passes a `time.monotonic()` float, because its fetch budget is private and
    keeps the monotonic guarantee. See `_deadline_remaining`.

    `slot_timeout` bounds the wait for one of the
    `TRANSFER_MAX_CONCURRENT_UPLOADS` streaming slots. See
    `_acquire_upload_slot` for the two-bound rule and the 408-before-503
    precedence; the caller must hold **no database connection** across this
    call, because the wait can last the whole `slot_timeout` (#208).

    Returns `{"size", "sha256", "mime"}`. Raises `TooLarge`, `Timeout`,
    `QueueTimeout`, `PrePublishAborted`, or `vault_fs.Conflict` /
    `vault_fs.UnsafePath` — and in every case leaves no staged file and nothing
    at the target path.

    **Staging holds no directory entry** (#92 item 1). The body streams into an
    `O_TMPFILE` inode in `.transfer-tmp`, and a no-clobber publish links that
    inode into place by descriptor through `/proc/self/fd/<fd>` — so for the
    whole streaming window there is nothing in the staging directory to observe,
    replace or race, and an abandoned upload leaves nothing for the sweep. An
    *overwrite* publish cannot consume an unnamed inode (`renameat` has no
    by-descriptor form), so it materialises a transient name in the staging
    directory inside the publish gate, immediately before the fingerprint check
    and the rename (D20). Where the publication probe finds the filesystem
    cannot allocate an unnamed inode, the transfer is refused unless
    `VAULT_ALLOW_NAMED_STAGING_FALLBACK` is set, in which case the pre-change
    named staging is used instead — with the identity check and the guarded
    discard the transient name introduced. The probe decides which, once per
    root; nothing here re-decides it.

    **The contract callers depend on: `PostPublishFailure` is the only error
    raised after the bytes are in place.** Every other exception — including an
    unexpected `OSError` from the stream or a database error opening the gate —
    means nothing was published and no temp file survives, which is what lets
    the upload route release the claim rather than stranding the token. If you
    add a step between `publish` and the return, it must raise
    `PostPublishFailure` too.

    **Durability (#97), and where its two flushes sit relative to that
    contract.** The staged payload is flushed after the body is fully received
    and *before* the gate opens, off the event loop
    (`_flush_staged_payload`) — pre-publication, so a failure releases the
    claim. The destination directory, and the parent of every directory this
    call created, are flushed *after* the publication has been recorded
    (`_publish_into_current_parent`) — post-publication, so a failure becomes
    `PostPublishFailure` and the token strands. Without the pair, a crash can
    leave a transfer recorded `completed` whose file is absent or truncated at
    the path an agent was told to read back by `sha256`.
    """
    if content_length is not None and content_length > max_bytes:
        # Refuse before opening anything: a declared oversize body should cost
        # us a header parse, not a directory walk and a temp file.
        raise TooLarge(
            f"Declared Content-Length {content_length:,} exceeds the "
            f"{max_bytes:,}-byte limit"
        )

    # The acquire stays *here*, inside `stream_to_vault`, so `import_from_url`
    # and every direct caller inherit the same bound. Do not lift it into the
    # upload route: at `TRANSFER_MAX_CONCURRENT_UPLOADS=1` a route-side acquire
    # plus this one is a self-deadlock, and the parameter is the wrong place to
    # express a process-wide concurrency ceiling.
    sem = upload_semaphore()
    await _acquire_upload_slot(sem, deadline=deadline, slot_timeout=slot_timeout)
    try:
        return await _stream_locked(
            row,
            chunks,
            max_bytes=max_bytes,
            deadline=deadline,
            idle_timeout=idle_timeout,
            before_publish=before_publish,
        )
    finally:
        sem.release()


def _refuse_if_past_deadline(deadline: float | datetime.datetime) -> None:
    """Raise `Timeout` when the stream deadline has already passed.

    Deliberately the *existing* `Timeout` and not a new type: the upload route
    maps it to `consume`, and consuming is what the state machine says about a
    request that ran past its deadline — a retry must mint afresh rather than
    replay a link whose window is gone. It is also unambiguously
    pre-publication, so raising it here preserves the contract that
    `PostPublishFailure` is the only exception `stream_to_vault` raises once
    the bytes are in place: `state["published"]` is still false, so the
    surrounding handler re-raises this untouched.

    **Honoured to within the publish latency, not to the syscall.** This runs
    before `vault_fs.publish`'s own authoritative `open_parent` walk and, on an
    overwrite, the incumbent's fingerprint re-hash (bounded by
    `MAX_FILE_WRITE_BYTES`), so the bytes can land a few milliseconds past the
    instant checked here. Accepted: closing that would mean a pre-mutation
    callback threaded inside `publish`, and the write that lands late is the
    consented, fingerprint-verified one — not a destructive write on anything
    unintended.

    For an upload `deadline` is `upload_stream_deadline(row)` measured against
    `now_utc()`; taking it from the parameter rather than re-deriving it from
    `row` keeps this working for `import_from_url`, whose row has no expiry and
    whose monotonic fetch budget deserves the same treatment.
    """
    if _deadline_remaining(deadline) <= 0:
        raise Timeout("Upload exceeded its deadline before it could be published")


def _close_quietly(fd: int, what: str) -> None:
    """Close a descriptor; a failing close never fails the operation.

    Closes on the publish path are pure bookkeeping — the bytes are already
    where they are — so a `close` that returns `EIO` must not be able to
    reverse the verdict on an upload. It is logged and dropped.

    Note this is *not* used for the staged temp file's own descriptor, which is
    closed before publication and where a failure genuinely means the data may
    not have reached the disk.
    """
    try:
        os.close(fd)
    except OSError as exc:
        logger.warning("Could not close the %s descriptor: %s", what, exc)


def _parent_rel(rel_path: str) -> str:
    """The vault-relative parent directory of `rel_path` (`""` for the root)."""
    parent = str(PurePosixPath(str(rel_path)).parent)
    return "" if parent == "." else parent


def _publish_into_current_parent(
    root_fd: int,
    staging_fd: int,
    tmp_name: str | None,
    row,
    on_published: Callable[[object], None] | None = None,
    created: list[str] | None = None,
    *,
    staged_fd: int | None = None,
    staged_st: os.stat_result | None = None,
):
    """Resolve the destination parent *now* and link the staged file into it.

    The fresh `open_dir_beneath` lookup is the point. A descriptor opened before
    a minutes-long stream keeps pointing at the same directory even after that
    directory is renamed or moved — including into `.trash` or out of the vault
    entirely — so publishing through it would follow the move and write
    somewhere the token never named. Re-resolving from the root descriptor under
    the caller's lock means the bytes land at the path the token committed to,
    as that path resolves at publication time, or not at all.

    That lookup is one `openat2(RESOLVE_BENEATH | …)` since #87, so no rename
    *during* it can hand back a descriptor outside the root. It proves
    containment at the instant it resolves and not afterwards (D26): the
    interval between this lookup and the `link`/`rename` below is the retained
    residual of anchoring to a descriptor at all, and it is the same interval
    the per-component walk had underneath the larger window it did not close.
    This is also the second of an upload's two creating descents — the cheap
    up-front one in `_stream_locked` is the first — which is why D22's bound is
    stated per descent rather than per call.

    **The mount-identity re-check runs here**, after that lookup and before
    `publish`, so a destination that has come to sit on a different mount is
    refused rather than published into. See the comment at the call.

    `on_published` is called with the outcome the *instant* `publish` returns
    having placed the bytes, before this function does anything else — closing
    the destination descriptor included. Nothing after a successful publish may
    be able to make the caller believe nothing was published; see
    `_stream_locked`.

    **The directory flush runs after that callback and before this returns**
    (#97), and the ordering is the whole of its failure classification. A
    `fsync` that fails once `on_published` has recorded the publication is seen
    by `_stream_locked` with `state["published"]` already true, so it becomes
    `PostPublishFailure` — the token strands, the human is told to look at the
    path. Flushing *before* the callback would let the same failure escape as a
    bare `OSError`, which the upload route reads as "nothing was published" and
    answers by handing back a replayable token over a path that already holds
    the file.

    `created` carries the directories this call made on the way here — from the
    up-front walk in `_stream_locked` as well as from this one — because
    flushing the destination alone leaves the entry that names *it* unflushed.

    `tmp_name` is `None` in the unnamed staging mode, where `staged_fd` is the
    `O_TMPFILE` descriptor and the no-clobber publish is a `linkat` through
    `/proc/self/fd/<fd>` — the staged bytes never carry a directory entry, so
    there is nothing here for a peer to observe or substitute. In the
    named-staging fallback `tmp_name` is the `.tmp-*` in `.transfer-tmp` and
    `staged_st` is the `fstat` this call took of it when it was created, which
    is what `publish`'s identity check compares against. Everything else on this
    path — the fresh lookup, the gate, the flushes, the callback ordering — is
    the same code on both branches.
    """
    dst_fd, name = vault_fs.open_parent(
        root_fd, row.path, create=True, created=created
    )
    try:
        # **Before `publish`, against the authoritative destination descriptor.**
        # The mint-time check (`vault_fs.require_destination_mount`) refuses a
        # boundary that was already there before a byte moves; this one catches
        # a mount established between the mint and now. Raising here keeps the
        # refusal unambiguously pre-publication — `_stream_locked` sees
        # `state["published"]` still false, so the claim is released and the
        # human may retry the same link.
        #
        # What it does **not** save: by the time the gate runs the body has
        # streamed in full. This half is pre-*publication*, not pre-*body*, and
        # the pair must not be described as "refused before any body is
        # streamed" (D23).
        vault_fs.require_same_mount(staging_fd, dst_fd, row.path)
        if row.overwrite:
            # A bind mount on the destination *file* leaves the parent check
            # above satisfied and still fails the rename with `EBUSY`.
            vault_fs.require_leaf_on_same_mount(staging_fd, dst_fd, name, row.path)
        outcome = vault_fs.publish(
            staging_fd,
            tmp_name,
            name,
            overwrite=bool(row.overwrite),
            expected_fingerprint=row.expected_fingerprint,
            dst_dir_fd=dst_fd,
            staged_fd=staged_fd,
            staged_st=staged_st,
        )
        if on_published is not None and outcome.published:
            on_published(outcome)
        if outcome.published:
            vault_fs.flush_dir_fd(dst_fd)
            # And every directory *above* the destination parent, up to the
            # vault root — not just the ones this call created. A previous
            # attempt that created `New/Folder` and then aborted (a 413, a
            # disconnect, a refused deadline) flushed nothing, correctly, since
            # it published nothing; this call finds both directories there,
            # records no creations, and would otherwise leave the entry naming
            # `New` durable nowhere while reporting the upload `completed`.
            # Per-call provenance cannot cover an obligation that outlives the
            # call, or the process. See `publication_flush_dirs`.
            vault_fs.flush_publication_ancestors(
                root_fd, _parent_rel(row.path), created or ()
            )
        return outcome
    finally:
        # Never `os.close` bare here: a close that fails *after* publication
        # would discard the return value and surface as a generic `OSError`,
        # which the upload route reads as "nothing was published" and answers
        # by releasing the claim — over a path that already holds the file.
        _close_quietly(dst_fd, f"publish destination for {row.path}")


async def _stream_locked(
    row,
    chunks: AsyncIterator[bytes],
    *,
    max_bytes: int,
    deadline: float | datetime.datetime,
    idle_timeout: float,
    before_publish: PrePublishGate | None,
) -> dict:
    root_fd = vault_fs.open_root(row.vault_root)
    staging_fd: int | None = None
    tmp_name: str | None = None
    staged_fd: int | None = None
    staged_st: os.stat_result | None = None
    # Every directory *this call* creates, from either descent. The up-front
    # walk below usually makes them, so the publish-time walk finds them there
    # and records nothing — collecting from both is what keeps the flush from
    # silently covering nothing (#97).
    created_dirs: list[str] = []
    # **Before the staging block, so it exists from before `tmp_name` can.**
    # The outer cleanup reads it to tell a name its publish consumed from one
    # that disappeared while the write was still in flight (#115), and that
    # cleanup is reached by every failure after staging — the drain, the
    # identity `fstat`, the `fchmod`, the payload flush. Created down beside
    # the gate, as it was, those failures would find it unbound and the
    # cleanup would raise `UnboundLocalError`: the real failure masked, and the
    # guarded discard skipped entirely.
    state = {"published": False}

    def _record(_outcome) -> None:
        state["published"] = True

    try:
        # Walk the destination once up front so a `..`, a symlinked ancestor or
        # a non-directory costs a syscall rather than a whole upload. The
        # descriptor is closed immediately — the authoritative walk is the one
        # inside the gate.
        probe_fd, name = vault_fs.open_parent(
            root_fd, row.path, create=True, created=created_dirs
        )
        os.close(probe_fd)

        # How this root stages was decided once, by the publication probe, and
        # is read back here — never re-decided per call (D27). The probe has
        # already run on every path that reaches this function (the mint tools
        # and the upload route all call it before a token is handed out), so
        # this is a cache read; passing our own anchored root descriptor keeps
        # it from re-walking the root's pathname if it is not.
        mode = vault_fs.check_publication_support(row.vault_root, root_fd=root_fd)

        staging_fd = vault_fs.open_staging_dir(root_fd)
        if mode == vault_fs.STAGING_MODE_UNNAMED:
            # No directory entry at any point: nothing in `.transfer-tmp` for a
            # peer to observe, replace or race for the whole streaming window,
            # and nothing for a sweep to collect if this upload is abandoned.
            staged_fd = vault_fs.create_nameless_temp(staging_fd)
        else:
            # The `VAULT_ALLOW_NAMED_STAGING_FALLBACK` branch: exactly the
            # pre-change staging, through the staging descriptor the
            # beneath-root lookup returned. Only the staging differs — the
            # payload flush, the gate and its lock order, the size caps, the
            # deadline and the token state machine below are the same code.
            staged_fd, tmp_name = vault_fs.create_temp(staging_fd)
            # First *exercise*, which is the moment the warning means something
            # — after the name exists, never before.
            vault_fs.note_named_staging_exercised(
                vault_fs.NAMED_STAGING_TRANSFER_PATH
            )
        try:
            # The identity the publish's check compares against. Taken from the
            # descriptor, so it names the inode this call staged whatever
            # happens to the name afterwards.
            staged_st = os.fstat(staged_fd)
            size, digest, head = await _drain(
                chunks,
                staged_fd,
                max_bytes=max_bytes,
                deadline=deadline,
                idle_timeout=idle_timeout,
            )
            # Publication links this very inode into place, so the 0600 it was
            # created with would become the published mode. Relax it to what a
            # plain write would have produced, exactly as
            # `vault._atomic_write_at` does — an upload must not land less
            # readable than the note beside it (#95).
            #
            # Relaxing it here rather than at publish time means the bytes are
            # group/world-readable for as long as the gate takes, which can be
            # minutes; `open_staging_dir` holds `.transfer-tmp` at 0700 so that
            # window is not reachable — and in the unnamed mode there is no name
            # through which to reach them at all.
            os.fchmod(staged_fd, vault_fs.default_file_mode())
            # Durability, before the gate and off the loop — see
            # `_flush_staged_payload`. The body is complete at this point and
            # nothing has been published, so a failure here is a plain
            # pre-publication error: the `except` below discards the staged
            # bytes and the route releases the claim.
            await _flush_staged_payload(staged_fd)
        finally:
            if tmp_name is not None and staged_fd is not None:
                # Named mode releases the descriptor before the gate, as it
                # always has: holding one across an unbounded wait on
                # `SELECT … FOR UPDATE`, per upload, is what this avoids. A
                # close that fails here is genuinely pre-publication and must
                # not be swallowed.
                os.close(staged_fd)
                staged_fd = None
        # In the unnamed mode the descriptor stays open until after publication
        # — it is the only handle on the bytes, and publishing *is* linking it
        # into place. It is closed quietly in the outer `finally`, once the
        # publication verdict has been decided.

        _kind, mime = classify_bytes(head, name)
        result = {"size": size, "sha256": digest, "mime": mime}

        # `publish` succeeding is the point of no return, and it is recorded
        # through `_record` above rather than taken from the return value: the
        # return value only becomes visible once `_publish_into_current_parent`
        # has finished unwinding, and anything that raises on the way out — a
        # failing directory flush, a failing descriptor close — would otherwise
        # leave `published` false for a file that is already on disk.
        try:
            if before_publish is None:
                _refuse_if_past_deadline(deadline)
                _publish_into_current_parent(
                    root_fd,
                    staging_fd,
                    tmp_name,
                    row,
                    _record,
                    created_dirs,
                    staged_fd=staged_fd,
                    staged_st=staged_st,
                )
                tmp_name = None  # publish owns cleanup from here
            else:
                async with before_publish() as gate:
                    if not gate.ok:
                        raise PrePublishAborted(
                            "The minting identity or vault root is no longer valid"
                        )
                    # **Last thing before the bytes move, inside the locks.**
                    # `_drain` checks the deadline while reading the body, but
                    # the gate can take arbitrarily long after that: it waits
                    # on `SELECT … FOR UPDATE` behind another writer or a
                    # migration. A body that finished a second inside the
                    # deadline could therefore publish — an overwrite
                    # included — minutes after the capability's advertised
                    # expiry, while `check_upload` was already reporting
                    # `unknown` for it. Re-checking here, holding the locks,
                    # is the only place where "still in time" and "about to
                    # write" are the same instant.
                    _refuse_if_past_deadline(deadline)
                    _publish_into_current_parent(
                        root_fd,
                        staging_fd,
                        tmp_name,
                        row,
                        _record,
                        created_dirs,
                        staged_fd=staged_fd,
                        staged_st=staged_st,
                    )
                    tmp_name = None
                    # Inside the context, so completion commits with the locks.
                    await gate.complete(result, published=state["published"])
        except PrePublishAborted:
            # Always pre-publication: the gate refuses before `publish` runs.
            raise
        except BaseException as exc:
            if state["published"]:
                # The file is in place; whatever failed did so afterwards. A
                # distinct type, because the caller must not treat this like
                # the pre-publication failures and release the claim.
                raise PostPublishFailure(
                    f"Published {row.path} but could not record the completion: {exc}"
                ) from exc
            raise
        return result
    except BaseException:
        # Named mode only. The unnamed mode has no name to unlink: closing the
        # descriptor in the `finally` below frees the inode, which is what makes
        # an abandoned upload leave nothing for the 24-hour sweep to collect.
        # The unlink is inode-guarded, so a substitute is left in place and
        # logged rather than deleted.
        #
        # **`published=` is the real outcome, not a hardcoded `False`** (#115).
        # `tmp_name` is cleared on the happy path, so this is reached with a
        # name *and* a publication only in one place: a failure after `publish`
        # returned — the post-publication directory flush — which is correctly
        # a `PostPublishFailure` with the claim stranded. Reaching
        # `discard_staged_name` through `discard_temp`, whose contract is the
        # abandon path and whose `published=False` is baked in, made that
        # doubly-degraded corner log "staging name disappeared before its write
        # was published" about a name the overwrite rename had legitimately
        # consumed. A false disappearance warning trains an operator to ignore
        # the true one, which is the substitution signal. `discard_temp` keeps
        # its contract; this caller simply stops borrowing it.
        if tmp_name is not None and staging_fd is not None:
            vault_fs.discard_staged_name(
                staging_fd, tmp_name, staged_st, published=state["published"]
            )
        raise
    finally:
        # Quietly, and in a `finally` that runs after the publication verdict
        # has already been decided: a descriptor we are done with cannot be
        # allowed to turn a published upload into a generic `OSError` on the
        # way out, which the route would answer by releasing the claim. The
        # staged payload's descriptor is here too in the unnamed mode — it is
        # held across the publish by construction, and by the time we reach this
        # line the publication has either happened or provably has not.
        if staged_fd is not None:
            _close_quietly(staged_fd, "staged upload payload")
        if staging_fd is not None:
            _close_quietly(staging_fd, "upload staging directory")
        _close_quietly(root_fd, "vault root")


# ════════════════════════════════════════════════════════════════════════════
# 3. SSRF-guarded fetch
# ════════════════════════════════════════════════════════════════════════════

# Scheme-paired ports. A URL may not name 443 over http or 8080 over https:
# port and scheme travelling together is what stops a redirect from smuggling
# a plaintext hop past an https-only policy.
SCHEME_PORTS: dict[str, frozenset[int]] = {
    "https": frozenset({443, 8443}),
    "http": frozenset({80, 8080}),
}
DEFAULT_PORTS = {"https": 443, "http": 80}

FORBIDDEN_HOSTS = frozenset({"localhost"})
FORBIDDEN_SUFFIXES = (".localhost", ".local", ".internal", ".home.arpa")

# What a canonical ASCII host may look like once IDNA has run: LDH labels of
# 1–63 characters. Anything else is either an encoding trick or a name no
# resolver would accept anyway.
_ASCII_HOST_RE = re.compile(r"(?=.{1,253}$)[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?"
                            r"(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*")

REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
MAX_REDIRECTS = 5
DEFAULT_FETCH_DEADLINE = 30.0

_FORBIDDEN_V4 = tuple(
    ipaddress.ip_network(cidr)
    for cidr in (
        "0.0.0.0/8",  # "this network"
        "10.0.0.0/8",
        "100.64.0.0/10",  # CGNAT
        "127.0.0.0/8",
        "169.254.0.0/16",  # link-local, incl. the cloud metadata address
        "172.16.0.0/12",
        "192.0.0.0/24",  # IETF protocol assignments
        "192.0.2.0/24",  # TEST-NET-1
        "192.88.99.0/24",  # 6to4 relay anycast
        "192.168.0.0/16",
        "198.18.0.0/15",  # benchmarking
        "198.51.100.0/24",  # TEST-NET-2
        "203.0.113.0/24",  # TEST-NET-3
        "224.0.0.0/4",  # multicast
        "240.0.0.0/4",  # reserved, incl. 255.255.255.255
    )
)

_FORBIDDEN_V6 = tuple(
    ipaddress.ip_network(cidr)
    for cidr in (
        "::/128",  # unspecified
        "::1/128",  # loopback
        "100::/64",  # discard-only
        "2001:db8::/32",  # documentation
        "fc00::/7",  # ULA
        "fe80::/10",  # link-local
        "ff00::/8",  # multicast — `is_global` does NOT exclude this
    )
)

_NAT64_96 = ipaddress.ip_network("64:ff9b::/96")
_NAT64_48 = ipaddress.ip_network("64:ff9b:1::/48")
_SIXTOFOUR = ipaddress.ip_network("2002::/16")
_TEREDO = ipaddress.ip_network("2001::/32")

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address


@dataclass(frozen=True)
class UrlParts:
    """A URL that passed every pre-connection check, in canonical form."""

    scheme: str
    host: str  # IDNA-encoded name, or canonical IP literal text
    port: int
    url: str  # what to request — original host, not the pinned address
    literal_ip: IPAddress | None


def _embedded_v4(addr: ipaddress.IPv6Address) -> list[ipaddress.IPv4Address]:
    """IPv4 addresses tunnelled inside an IPv6 address.

    An IPv6 literal is not automatically "somewhere else": 6to4, Teredo, NAT64
    and IPv4-mapped forms all carry a v4 destination that a naive `is_global`
    check never looks at. `::ffff:10.0.0.5` and `2002:0a00:0005::1` both reach
    10.0.0.5.
    """
    out: list[ipaddress.IPv4Address] = []
    mapped = addr.ipv4_mapped
    if mapped is not None:
        out.append(mapped)
    sixtofour = addr.sixtofour
    if sixtofour is not None:
        out.append(sixtofour)
    teredo = addr.teredo
    if teredo is not None:
        out.extend(teredo)
    packed = addr.packed
    if addr in _NAT64_96:
        out.append(ipaddress.IPv4Address(packed[12:16]))
    if addr in _NAT64_48:
        # RFC 6052 §2.2: for a /48 prefix the IPv4 address is split around the
        # reserved `u` octet — bits 48-63 and 72-87.
        out.append(ipaddress.IPv4Address(bytes([packed[6], packed[7], packed[9], packed[10]])))
    value = int(addr)
    if 1 < value < 2**32:
        # IPv4-compatible `::a.b.c.d` (deprecated, still routed by some stacks).
        out.append(ipaddress.IPv4Address(value))
    return out


def is_global_address(ip: IPAddress) -> bool:
    """`ip.is_global` with IPv4-mapped forms unwrapped first."""
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return ip.ipv4_mapped.is_global
    return ip.is_global


def is_forbidden_address(ip: IPAddress) -> bool:
    """The explicit deny policy of D11, applied to an address and anything it embeds.

    `is_global` alone is not enough — in Python 3.12 it admits IPv6 multicast
    and the NAT64 well-known prefix — so the range list runs first and
    `is_global` is the backstop, not the test.
    """
    pending: list[IPAddress] = [ip]
    seen: set[IPAddress] = set()
    while pending:
        addr = pending.pop()
        if addr in seen:
            continue
        seen.add(addr)
        if isinstance(addr, ipaddress.IPv6Address):
            pending.extend(_embedded_v4(addr))
            if any(addr in net for net in _FORBIDDEN_V6):
                return True
            if addr in _TEREDO or addr in _SIXTOFOUR:
                # The embedded v4 queued above is the real destination; the
                # wrapper itself carries no independent legitimacy.
                continue
            if addr in _NAT64_96 or addr in _NAT64_48:
                continue
        else:
            if any(addr in net for net in _FORBIDDEN_V4):
                return True
        if not addr.is_global:
            return True
    return False


def default_address_policy(ip: IPAddress) -> bool:
    """True when an address may be connected to. Injectable for tests."""
    return not is_forbidden_address(ip)


def _parse_ip(text: str) -> IPAddress | None:
    try:
        return ipaddress.ip_address(text)
    except ValueError:
        return None


# The three code points a resolver treats as a label separator besides U+002E.
# UTS-46 maps all of them to "."; IDNA 2003 (the stdlib codec) does not, which
# is exactly how `svc.prod。internal` used to sail past the suffix check and
# then resolve as `svc.prod.internal`.
_LABEL_SEPARATORS = "。．｡"
_SEPARATOR_MAP = str.maketrans({c: "." for c in _LABEL_SEPARATORS})


def _canonical_host(host: str) -> str:
    """Fold a URL host to canonical ASCII **before** any policy check runs.

    Every check downstream — forbidden suffix, single label, ambiguous numeric,
    IP literal — is a string comparison, and a string comparison is only as
    good as the normalisation in front of it. Fullwidth letters, fullwidth
    digits and the alternative full stops all survive `str.lower()` untouched
    and are folded by the resolver later, so the checks have to run on the form
    the resolver will see, not the form the caller typed.
    """
    host = unicodedata.normalize("NFKC", host.strip()).translate(_SEPARATOR_MAP)
    host = host.rstrip(".").lower()
    if not host:
        raise SSRFError("URL has no host")
    if host.isascii():
        return host
    try:
        return idna.encode(host, uts46=True, transitional=False).decode("ascii").lower()
    except (idna.IDNAError, UnicodeError, ValueError) as exc:
        raise SSRFError(f"Host {host!r} is not IDNA-encodable: {exc}") from None


def _looks_numeric(host: str) -> bool:
    """Would a resolver read this name as a packed/short IPv4 spelling?

    `getaddrinfo` happily turns `2130706433`, `0x7f000001` and `0177.0.0.1`
    into 127.0.0.1, while `ipaddress` rejects all three — so a host that
    `ipaddress` refuses but whose last label is numeric must never be handed to
    the resolver. No real TLD is all digits or hex-prefixed.
    """
    last = host.rsplit(".", 1)[-1]
    if not last:
        return False
    if last.isdigit():
        return True
    return last[:2].lower() == "0x" and len(last) > 2


def canonicalise(url: str, *, allow_http: bool) -> UrlParts:
    """Parse and vet a URL before any DNS or TCP happens.

    Every rejection here is pre-connection by construction, which is what makes
    "no connection was attempted" a testable property rather than a hope.
    """
    try:
        parts = urlsplit(url)
    except ValueError as exc:
        raise SSRFError(f"Malformed URL: {exc}") from None

    scheme = (parts.scheme or "").lower()
    if scheme not in ("http", "https"):
        raise SSRFError(f"Only https URLs are allowed (got scheme {scheme!r})")
    if scheme == "http" and not allow_http:
        raise SSRFError(
            "Plain http is disabled; use https or set IMPORT_ALLOW_HTTP=true"
        )

    if "@" in parts.netloc:
        raise SSRFError("URL credentials (user:password@) are not allowed")

    try:
        host = parts.hostname
        port = parts.port
    except ValueError as exc:
        raise SSRFError(f"Malformed host or port: {exc}") from None
    if not host:
        raise SSRFError("URL has no host")
    if "%" in parts.netloc:
        raise SSRFError("IPv6 zone identifiers are not allowed")

    # Normalise *first*, then check. Every rule below is a comparison against
    # the canonical ASCII form the resolver would use.
    host = _canonical_host(host)

    port = DEFAULT_PORTS[scheme] if port is None else port
    if port not in SCHEME_PORTS[scheme]:
        allowed = ", ".join(str(p) for p in sorted(SCHEME_PORTS[scheme]))
        raise SSRFError(f"Port {port} is not allowed for {scheme} (allowed: {allowed})")

    literal = _parse_ip(host)
    if literal is None:
        if _looks_numeric(host):
            raise SSRFError(
                f"Ambiguous numeric host {host!r}; use a name or a canonical IP"
            )
        if host in FORBIDDEN_HOSTS or host.endswith(FORBIDDEN_SUFFIXES):
            raise SSRFError(f"Host {host!r} names a local/internal scope")
        if "." not in host:
            raise SSRFError(
                f"Single-label host {host!r} is not allowed (it can only resolve "
                "inside the container network)"
            )
        if not _ASCII_HOST_RE.fullmatch(host):
            raise SSRFError(f"Host {host!r} contains characters a host name may not")
    else:
        host = literal.compressed

    netloc = f"[{host}]" if literal is not None and literal.version == 6 else host
    if port != DEFAULT_PORTS[scheme]:
        netloc = f"{netloc}:{port}"
    canonical = urlunsplit((scheme, netloc, parts.path or "/", parts.query, ""))
    return UrlParts(scheme=scheme, host=host, port=port, url=canonical, literal_ip=literal)


async def default_resolver(host: str, port: int) -> list[str]:
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [info[4][0].split("%", 1)[0] for info in infos]


async def resolve_and_check(
    parts: UrlParts,
    *,
    resolver: Callable[[str, int], Awaitable[Iterable[str]]] | None = None,
    policy: Callable[[IPAddress], bool] | None = None,
) -> str:
    """Resolve a vetted host and return one address that may be connected to.

    **Every** answer must pass, not just the one we pick: a name that resolves
    to one public and one private address is a rebinding attack with extra
    steps, and picking the good one only means we lose the race later.
    """
    policy = policy or default_address_policy
    if parts.literal_ip is not None:
        if not policy(parts.literal_ip):
            raise SSRFError(
                f"Address {parts.literal_ip.compressed} is not a globally "
                "routable public address"
            )
        return parts.literal_ip.compressed

    resolver = resolver or default_resolver
    try:
        answers = list(await resolver(parts.host, parts.port))
    except OSError as exc:
        raise SSRFError(f"Could not resolve {parts.host!r}: {exc}") from None
    if not answers:
        raise SSRFError(f"Host {parts.host!r} did not resolve")

    chosen: str | None = None
    for answer in answers:
        addr = _parse_ip(answer)
        if addr is None:
            raise SSRFError(f"Resolver returned an unparsable address: {answer!r}")
        if not policy(addr):
            raise SSRFError(
                f"Host {parts.host!r} resolves to {addr.compressed}, which is not a "
                "globally routable public address"
            )
        if chosen is None:
            chosen = addr.compressed
    return chosen  # type: ignore[return-value]


class PinnedTransport(httpx.AsyncHTTPTransport):
    """Connect to a pre-validated address while keeping the original identity.

    The URL's host is rewritten to the validated IP so no second resolution can
    happen between the check and the connect, but `Host` and TLS SNI keep the
    original name — so virtual hosting still works and the certificate is
    verified against the name the caller asked for, not against an IP.
    """

    def __init__(self, *, address: str, host_header: str, **kwargs):
        super().__init__(**kwargs)
        self.address = address
        self.host_header = host_header

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        # `netloc` still carries the original authority, port included. Sending
        # a bare hostname when the URL named a non-default port would produce a
        # `Host` the origin server does not recognise — virtual hosts and
        # redirect generation both key off it.
        authority = request.url.netloc.decode("ascii")
        request.url = request.url.copy_with(host=self.address)
        request.headers["Host"] = authority
        request.extensions = {**request.extensions, "sni_hostname": self.host_header}
        return await super().handle_async_request(request)


@dataclass
class FetchResult:
    chunks: AsyncIterator[bytes]
    final_url: str
    content_type: str | None


@asynccontextmanager
async def fetch_url_guarded(
    url: str,
    *,
    allow_http: bool | None = None,
    max_bytes: int,
    deadline: float = DEFAULT_FETCH_DEADLINE,
    resolver: Callable[[str, int], Awaitable[Iterable[str]]] | None = None,
    policy: Callable[[IPAddress], bool] | None = None,
    max_redirects: int = MAX_REDIRECTS,
) -> AsyncIterator[FetchResult]:
    """Fetch `url` under the full outbound policy; yields a streaming result.

    An async context manager rather than tasks.md's plain tuple return: the
    body is a stream over a live connection, and a tuple gives the caller no
    way to guarantee the client is closed if it never consumes it. The yielded
    `chunks` are valid only inside the `with` block.

    A **new client per hop** — connection reuse across hosts would let hop 2
    ride a socket opened for hop 1's address. `trust_env=False` so a proxy
    environment variable cannot become the connection target; `http2=False`
    because its multiplexing reintroduces the same reuse question.
    """
    allow_http = settings.import_allow_http if allow_http is None else allow_http
    current = url

    try:
        async with asyncio.timeout(deadline):
            async for item in _fetch_hops(
                current,
                allow_http=allow_http,
                max_bytes=max_bytes,
                deadline=deadline,
                resolver=resolver,
                policy=policy,
                max_redirects=max_redirects,
            ):
                yield item
    except TimeoutError:
        # `asyncio.timeout` speaks in builtins; every other failure in this
        # module is a `TransferError`, and a caller should not have to know
        # which layer raised. Cleanup has already run — the client is closed by
        # `_fetch_hops`'s own `finally` before this is reached.
        raise Timeout(
            f"Fetch of {url!r} exceeded its {deadline:g}s deadline"
        ) from None


async def _fetch_hops(
    current: str,
    *,
    allow_http: bool,
    max_bytes: int,
    deadline: float,
    resolver,
    policy,
    max_redirects: int,
) -> AsyncIterator[FetchResult]:
    """The redirect loop. Split out so the deadline wrapper stays legible."""
    for hop in range(max_redirects + 1):
        parts = canonicalise(current, allow_http=allow_http)
        address = await resolve_and_check(parts, resolver=resolver, policy=policy)

        transport = PinnedTransport(
            address=address, host_header=parts.host, retries=0
        )
        client = httpx.AsyncClient(
            transport=transport,
            trust_env=False,
            http2=False,
            follow_redirects=False,
            timeout=httpx.Timeout(deadline),
        )
        response: httpx.Response | None = None
        redirect_to: str | None = None
        try:
            request = client.build_request(
                "GET", parts.url, headers={"Accept-Encoding": "identity"}
            )
            try:
                response = await client.send(request, stream=True)
            except httpx.HTTPError as exc:
                raise SSRFError(f"Fetch failed: {exc}") from None

            if response.status_code in REDIRECT_STATUSES:
                location = response.headers.get("location")
                if not location:
                    raise SSRFError(
                        f"Redirect {response.status_code} without a Location header"
                    )
                if hop >= max_redirects:
                    raise SSRFError(
                        f"More than {max_redirects} redirects"
                    )
                redirect_to = urljoin(parts.url, location)
                continue

            if response.status_code != 200:
                raise SSRFError(
                    f"Expected HTTP 200, got {response.status_code}"
                )
            if response.headers.get("content-encoding"):
                raise SSRFError(
                    "Response is content-encoded; only identity encoding is "
                    "accepted so the byte cap counts real bytes"
                )
            declared = response.headers.get("content-length")
            if declared is not None:
                try:
                    if int(declared) > max_bytes:
                        raise TooLarge(
                            f"Remote file declares {int(declared):,} bytes, over "
                            f"the {max_bytes:,}-byte limit"
                        )
                except ValueError:
                    raise SSRFError("Malformed Content-Length in response") from None

            yield FetchResult(
                chunks=response.aiter_bytes(),
                final_url=parts.url,
                content_type=response.headers.get("content-type"),
            )
            return
        finally:
            if response is not None:
                with contextlib.suppress(Exception):
                    await response.aclose()
            await client.aclose()
            if redirect_to is not None:
                current = redirect_to

    raise SSRFError(f"More than {max_redirects} redirects")  # pragma: no cover
