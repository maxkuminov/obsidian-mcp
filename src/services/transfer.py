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
from pathlib import Path
from typing import AsyncContextManager, AsyncIterator, Awaitable, Callable, Iterable
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx
import idna
from sqlalchemy import delete, select, update

from src.config import settings
from src.models.db import APIKey, OAuthToken, TransferToken, User
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


class CredentialTooShortLived(TransferError):
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
    """The minting identity, exactly as `APIKeyMiddleware` resolved it."""

    key_id: int | None = None
    oauth_token_id: int | None = None
    user_id: int | None = None


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


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


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


async def plan_mint_window(session, identity: Identity, expires_in: int | None) -> MintWindow:
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

    Raises `CredentialTooShortLived` when less than `MIN_MINT_TTL_SECONDS`
    would remain.
    """
    cred = await _load_credential(session, identity)
    if cred is None:
        # No credential to re-validate means no redemption can ever succeed
        # (`resolve_identity_ok` returns False for exactly this), so minting
        # would only produce a link that 404s.
        raise CredentialTooShortLived(
            "This request carries no credential the transfer routes could "
            "re-validate, so any link minted for it would be refused"
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
    window: MintWindow | None = None,
) -> tuple[str, TransferToken]:
    """Create one transfer capability; return `(token, row)`.

    The plaintext token is returned exactly once, to the minting tool. Only its
    hash is persisted. Every constraint the redemption route will enforce —
    direction, path, root, identity, overwrite, fingerprint, expiry — is fixed
    here, because the route has no session to derive any of it from.

    The expiry is `min(requested TTL, minting credential's own expiry)` — see
    `plan_mint_window`. Pass `window` when the caller needs to know whether the
    credential shortened it; omit it and this computes the same window itself.
    """
    if direction not in DIRECTIONS:
        raise ValueError(f"Unknown transfer direction: {direction!r}")

    # Computed here when the caller did not, so no mint path can forget the
    # credential clamp: a token that outlives its credential is a link that
    # 404s at a time we advertised as valid.
    if window is None:
        window = await plan_mint_window(session, identity, expires_in)
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
    )
    session.add(row)

    # Opportunistic prune: a day past expiry a row can no longer be redeemed by
    # any code path, and minting is the only moment we are already writing.
    await session.execute(
        delete(TransferToken).where(TransferToken.expires_at < _now() - PRUNE_AFTER)
    )
    await session.commit()
    await session.refresh(row)
    return token, row


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


async def lookup_by_public_id(
    session, public_id: str, *, identity: Identity, direction: str
) -> TransferToken | None:
    """A transfer row by its public handle, **scoped to the calling identity**.

    `check_upload` is the only read that is not gated by a capability, so the
    scoping is the access control: the credential must be the exact one that
    minted the row, and the user must match too. A handle minted by another key
    — or by the same key after it was reassigned to another user — is simply
    not found. No state filter: reporting `expired` and `completed` is the
    whole job.
    """
    def same(column, value):
        # `column == None` renders as `= NULL`, which is never true — and would
        # silently turn "single-user API key" into "matches nothing".
        return column.is_(None) if value is None else column == value

    stmt = select(TransferToken).where(
        TransferToken.public_id == public_id,
        TransferToken.direction == direction,
        same(TransferToken.key_id, identity.key_id),
        same(TransferToken.oauth_token_id, identity.oauth_token_id),
        same(TransferToken.user_id, identity.user_id),
    )
    result = await session.execute(stmt.execution_options(populate_existing=True))
    return result.scalar_one_or_none()


async def lookup_download(session, token: str) -> TransferToken | None:
    return await lookup_token(session, token, direction="download")


async def lookup_upload(session, token: str) -> TransferToken | None:
    return await lookup_token(session, token, direction="upload")


def _credential_ok(cred, *, need_write: bool, row: TransferToken) -> bool:
    """Exact predicates from D4 — kept in one place so the pre-publication
    re-check and the entry check cannot drift apart."""
    now = _now()
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
        if need_write and "readwrite" not in (cred.scope or "").split():
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


async def _drain(
    chunks: AsyncIterator[bytes],
    fd: int,
    *,
    max_bytes: int,
    deadline: float,
    idle_timeout: float,
) -> tuple[int, str, bytes]:
    """Copy `chunks` to `fd`, counting, hashing and bounding as they arrive."""
    digest = hashlib.sha256()
    total = 0
    head = bytearray()
    iterator = chunks.__aiter__()

    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise Timeout("Upload exceeded its deadline")
        try:
            chunk = await asyncio.wait_for(
                iterator.__anext__(), timeout=min(idle_timeout, remaining)
            )
        except StopAsyncIteration:
            break
        except (asyncio.TimeoutError, TimeoutError):
            if time.monotonic() >= deadline:
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

    if time.monotonic() > deadline:
        raise Timeout("Upload exceeded its deadline")
    return total, digest.hexdigest(), bytes(head)


async def stream_to_vault(
    row,
    chunks: AsyncIterator[bytes],
    *,
    max_bytes: int,
    content_length: int | None = None,
    deadline: float,
    idle_timeout: float = 30.0,
    before_publish: PrePublishGate | None = None,
) -> dict:
    """Stream a body into the vault at `row.path`, capped, anchored, atomic.

    `row` only needs `vault_root`, `path`, `overwrite` and
    `expected_fingerprint` — the same four fields whether it is a
    `TransferToken` (upload route) or the ad-hoc descriptor `import_from_url`
    builds, which is why this takes a duck-typed row rather than the ORM class.

    `deadline` is a `time.monotonic()` value, not a duration: the caller
    computes `min(expires_at, claimed_at + TRANSFER_MAX_UPLOAD_SECONDS)` and
    this enforces it across the whole stream.

    Returns `{"size", "sha256", "mime"}`. Raises `TooLarge`, `Timeout`,
    `PrePublishAborted`, or `vault_fs.Conflict` / `vault_fs.UnsafePath` — and
    in every case leaves no temp file and nothing at the target path.

    **The contract callers depend on: `PostPublishFailure` is the only error
    raised after the bytes are in place.** Every other exception — including an
    unexpected `OSError` from the stream or a database error opening the gate —
    means nothing was published and no temp file survives, which is what lets
    the upload route release the claim rather than stranding the token. If you
    add a step between `publish` and the return, it must raise
    `PostPublishFailure` too.
    """
    if content_length is not None and content_length > max_bytes:
        # Refuse before opening anything: a declared oversize body should cost
        # us a header parse, not a directory walk and a temp file.
        raise TooLarge(
            f"Declared Content-Length {content_length:,} exceeds the "
            f"{max_bytes:,}-byte limit"
        )

    async with upload_semaphore():
        return await _stream_locked(
            row,
            chunks,
            max_bytes=max_bytes,
            deadline=deadline,
            idle_timeout=idle_timeout,
            before_publish=before_publish,
        )


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


def _publish_into_current_parent(
    root_fd: int,
    staging_fd: int,
    tmp_name: str,
    row,
    on_published: Callable[[object], None] | None = None,
):
    """Resolve the destination parent *now* and link the staged file into it.

    The fresh `open_dir_beneath` walk is the point. A descriptor opened before
    a minutes-long stream keeps pointing at the same directory even after that
    directory is renamed or moved — including into `.trash` or out of the vault
    entirely — so publishing through it would follow the move and write
    somewhere the token never named. Re-walking from the root descriptor under
    the caller's lock means the bytes land at the path the token committed to,
    as that path resolves at publication time, or not at all.

    `on_published` is called with the outcome the *instant* `publish` returns
    having placed the bytes, before this function does anything else — closing
    the destination descriptor included. Nothing after a successful publish may
    be able to make the caller believe nothing was published; see
    `_stream_locked`.
    """
    dst_fd, name = vault_fs.open_parent(root_fd, row.path, create=True)
    try:
        outcome = vault_fs.publish(
            staging_fd,
            tmp_name,
            name,
            overwrite=bool(row.overwrite),
            expected_fingerprint=row.expected_fingerprint,
            dst_dir_fd=dst_fd,
        )
        if on_published is not None and outcome.published:
            on_published(outcome)
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
    deadline: float,
    idle_timeout: float,
    before_publish: PrePublishGate | None,
) -> dict:
    root_fd = vault_fs.open_root(row.vault_root)
    staging_fd: int | None = None
    tmp_name: str | None = None
    try:
        # Walk the destination once up front so a `..`, a symlinked ancestor or
        # a non-directory costs a syscall rather than a whole upload. The
        # descriptor is closed immediately — the authoritative walk is the one
        # inside the gate.
        probe_fd, name = vault_fs.open_parent(root_fd, row.path, create=True)
        os.close(probe_fd)

        staging_fd = vault_fs.open_dir_beneath(
            root_fd, vault_fs.STAGING_DIR, create=True
        )
        fd, tmp_name = vault_fs.create_temp(staging_fd)
        try:
            size, digest, head = await _drain(
                chunks,
                fd,
                max_bytes=max_bytes,
                deadline=deadline,
                idle_timeout=idle_timeout,
            )
        finally:
            os.close(fd)

        _kind, mime = classify_bytes(head, name)
        result = {"size": size, "sha256": digest, "mime": mime}

        # `publish` succeeding is the point of no return, and it is recorded
        # through this callback rather than from the return value: the return
        # value only becomes visible once `_publish_into_current_parent` has
        # finished unwinding, and anything that raises on the way out — a
        # failing descriptor close, most plausibly — would otherwise leave
        # `published` false for a file that is already on disk.
        state = {"published": False}

        def _record(_outcome) -> None:
            state["published"] = True

        try:
            if before_publish is None:
                _publish_into_current_parent(
                    root_fd, staging_fd, tmp_name, row, _record
                )
                tmp_name = None  # publish owns cleanup from here
            else:
                async with before_publish() as gate:
                    if not gate.ok:
                        raise PrePublishAborted(
                            "The minting identity or vault root is no longer valid"
                        )
                    _publish_into_current_parent(
                        root_fd, staging_fd, tmp_name, row, _record
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
        if tmp_name is not None and staging_fd is not None:
            vault_fs.discard_temp(staging_fd, tmp_name)
        raise
    finally:
        # Quietly, and in a `finally` that runs after the publication verdict
        # has already been decided: a descriptor we are done with cannot be
        # allowed to turn a published upload into a generic `OSError` on the
        # way out, which the route would answer by releasing the claim.
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
