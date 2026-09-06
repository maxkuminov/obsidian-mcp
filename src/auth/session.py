"""Panel identity: the session registry and the one validation every entry uses.

Before the registry (#198), a browser session *was* the signed cookie: logout
called `request.session.clear()`, Starlette answered with an expiring
`Set-Cookie`, and the copy already taken stayed a correctly-signed credential
until its itsdangerous timestamp aged out — seven days of panel access, on a
panel that mints `readwrite` API keys and approves OAuth grants. A signed
cookie cannot be un-signed. Only a server-side row can be revoked, so the row
is what revocation acts on and what validation consults.

Four functions, and the asymmetry between two of them is the contract:

* `start_session` — the **single** mint. It takes the account guard, re-reads
  the account under it, refuses unless the row is active **and still on the
  `expected_session_version` its caller authorized against**, and **commits**,
  because `get_session` neither commits nor rolls back: an insert left to a
  caller's discretion is an insert that may never happen, and the cookie handed
  out beside it would authenticate nothing.
* `revoke_session` / `revoke_user_sessions` — **never** commit. They ride the
  caller's transaction, because every caller holds the account guard and
  nothing may commit between taking that lock and writing the flags it
  protects, or the last-admin check-then-act stops being atomic.
* `touch_session` — telemetry, on the request's **own** session, on safe
  methods only. A second `AsyncSession` would hold two connection-pool leases
  for the life of the request against a pool that tops out at fifteen.
* `get_active_session_user` — the one validation, reached from
  `require_user_panel`, `login_form`, `authorize_get` and `authorize_post`. A
  fifth entry point reading `request.session["user_id"]` raw is the defect this
  change removed from the login page.
"""
import contextlib
import hashlib
import logging
import secrets
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import settings
from src.database import get_session
from src.models.db import User, UserSession
from src.services import security_events

logger = logging.getLogger(__name__)

current_user_id: ContextVar[int | None] = ContextVar("current_user_id", default=None)


class _UnsetVaultRoot:
    """Sentinel: this context carries no authenticated vault snapshot."""

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "<unset vault root>"


UNSET_VAULT_ROOT = _UnsetVaultRoot()

# The authenticated request's *own* answer to "where is this user's vault, right
# now" — `(user_id, Path)` when assigned, `(user_id, None)` when the user has no
# usable assignment, and `UNSET_VAULT_ROOT` outside a request (indexer, panel,
# tests).
#
# It exists because the process-level cache in `src/services/vault.py` is shared
# with the indexer's bulk warm, and a bulk `SELECT` that started *before* an
# admin cleared `vault_path` can land *after* the per-request warm evicted the
# entry — re-admitting a user whose assignment was already revoked, mid-call.
# A snapshot bound to this request cannot be overwritten by another task, so
# `_vault_root` prefers it whenever it is set. See "The vault assignment is the
# admission gate for every tool" in CLAUDE.md.
current_vault_root: ContextVar[tuple[int, Path | None] | _UnsetVaultRoot] = ContextVar(
    "current_vault_root", default=UNSET_VAULT_ROOT
)

# The actor label denormalised onto every `usage_logs` row this request writes:
# `(kind, label, ref)` — `("api_key", <key name>, <omcp_ prefix>)` or
# `("oauth", <client_name>, <client_id>)`. `APIKeyMiddleware` binds it from the
# credential row it has already loaded, so the label costs no extra query and
# nothing later has to go looking for it.
#
# It exists because both credential tables are allowed to disappear while their
# history stays. `usage_logs.oauth_token_id` is `ON DELETE SET NULL`, so
# deleting an OAuth client cascades its tokens and unattributes every line that
# client produced; `usage_logs.key_id` has no `ON DELETE` at all, so the panel
# explicitly NULLs it before deleting an API key. Resolving the actor by LEFT
# JOIN at read time therefore turned all of that history into "unknown" — the
# evidence an operator opens `/admin/usage` to read, destroyed by the button
# they pressed to stop the client (issue #77). A label written at call time
# cannot be taken away by a later delete.
current_actor: ContextVar[tuple[str, str | None, str | None] | None] = ContextVar(
    "current_actor", default=None
)


# The identity every per-principal rate control is keyed on:
# `("api_key", api_keys.id)` or `("oauth", oauth_tokens.grant_id)`, bound by
# `APIKeyMiddleware` from the credential row it has already loaded — so it
# costs no query, exactly like `current_actor` beside it — and `reset()` in the
# same `finally` as the rest, so an allowance can never leak into another
# request's calls.
#
# **The OAuth key is the grant, never the access token and never
# `(client_id, user_id)`.** `oauth_tokens.grant_id` is NOT NULL and indexed
# (migration 014, issue #64), and every rotation of one `/authorize` approval
# inherits it, so a refreshing agent continues from its existing allowance
# instead of being handed a fresh one every hour. `(client_id, user_id)` would
# merge two grants that #64 deliberately made independently revocable: revoking
# one would not free the other's allowance, and the operator's stop would look
# like it had not worked.
#
# `None` — sandbox mode, or a direct in-process caller that never passed the
# middleware — means **exempt from every per-principal control**, not refused:
# the same shape as the quota gate's "a limit with no key is exempt rather than
# a crash". Nothing untrusted reaches that path. The failed-authentication
# budget is not keyed on this and applies regardless.
current_principal: ContextVar[tuple[str, object] | None] = ContextVar(
    "current_principal", default=None
)


# Widths of the denormalised actor columns, on `usage_logs` (migration 015) and
# on `transfer_tokens` (migration 017). The values are truncated to them rather
# than left to overflow: an over-long label raises inside the writer, and on the
# `usage_logs` path that writer swallows the error, so the failure mode would be
# the silent loss of the whole row — the opposite of what these columns exist
# for.
ACTOR_LABEL_MAX = 255
ACTOR_REF_MAX = 64


def actor_columns() -> dict:
    """The denormalised actor for this request, or `{}` when there is none.

    `APIKeyMiddleware` binds `current_actor` from the credential row it already
    loaded, so this is a ContextVar read, not a join. Writing the label *with*
    the row is the whole point of issue #77: `usage_logs.oauth_token_id` is
    ON DELETE SET NULL and `usage_logs.key_id` is NULLed by the panel before an
    API key is deleted, so an actor resolved by join at read time disappears
    exactly when an operator most wants it — after they revoked the credential
    they are investigating.

    `{}` (rather than three explicit NULLs) so a caller with no request context
    — the transfer redemption routes, the tests, sandbox mode — leaves the
    columns unset and the row keeps the shape it had before the scheme existed.

    **One reader, deliberately.** Two writers record this triple now: the tool
    call log (`_log_usage`) and the transfer mint (`mint_token`, issue #92).
    The columns are identically typed on both tables, so a second copy of this
    mapping is how the two start truncating differently — and a mint and the
    tool call in the same request would then disagree about who the caller was.
    It lives here, beside the ContextVar it reads, so neither writer owns it.
    """
    actor = current_actor.get()
    if actor is None:
        return {}
    kind, label, ref = actor
    return {
        "actor_kind": kind,
        "actor_label": label[:ACTOR_LABEL_MAX] if label else None,
        "actor_ref": ref[:ACTOR_REF_MAX] if ref else None,
    }


@dataclass
class _SingleUserSentinel:
    id: int | None = None
    is_admin: bool = True
    username: str = "admin"
    vault_path: str | None = None
    is_active: bool = True


_SINGLE_USER_SENTINEL = _SingleUserSentinel()


async def get_current_user(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> User | _SingleUserSentinel | None:
    if not settings.multi_user_mode:
        return _SINGLE_USER_SENTINEL
    return await get_active_session_user(request, session)


# ── The session registry ─────────────────────────────────────────────

#: Bytes of CSPRNG entropy behind the cookie's identifier. 32 bytes is 256
#: bits, which is what makes the unkeyed digest below safe: there is nothing to
#: brute-force, so the table can store the digest and never the identifier.
SESSION_ID_BYTES = 32

#: The cookie key the identifier travels under, beside `user_id`,
#: `session_version`, `is_admin` and `username`. A cookie carrying `user_id`
#: and **no** `sid` is a pre-registry cookie and is refused, not grandfathered:
#: accepting it would keep #198's replay window open for another seven days.
SESSION_ID_KEY = "sid"

#: The refusal reasons `panel_session_replay_refused` may carry — a closed
#: vocabulary, mirrored in the event catalogue.
REPLAY_REFUSAL_REASONS = frozenset(
    {
        "no_session_id",
        "unknown_session",
        "revoked_session",
        "expired_session",
        "user_mismatch",
        # The three account-level refusals. They are here rather than silent
        # because **every** validation refusal clears the cookie, and a cleared
        # cookie is a user signed out mid-session: an operator asking "why was
        # this browser logged out?" must be able to answer it from the log for
        # all eight branches, not five of them. `version_mismatch` in
        # particular is the account-wide invalidator doing its job after a
        # password reset, and it was the one refusal with no record at all.
        "user_missing",
        "user_inactive",
        "version_mismatch",
    }
)


def hash_session_id(sid: str) -> str:
    """`user_sessions.id` for a cookie identifier: its SHA-256 hex digest.

    The one definition. The identifier itself is a bearer credential for seven
    days, and a `pg_dump` of this database is taken before every migration and
    kept for thirty days; storing the identifier verbatim would turn every
    retained dump into a file full of live panel sessions. Unkeyed on purpose
    — 256 bits of CSPRNG output has nothing to brute-force, and keying it with
    `SECRET_KEY` would make the whole table unreadable after a rotation an
    operator may need to perform.
    """
    return hashlib.sha256(sid.encode()).hexdigest()


def _user_agent_hash(request: Request) -> str | None:
    """Forensic only, and never an authorization input (D5).

    Whoever stole the cookie also has the header, so as a *binding* it stops
    nobody; enforcing it would sign real users out on every browser
    auto-update, training them to re-authenticate after an unexplained logout
    — the habit phishing depends on.
    """
    try:
        agent = request.headers.get("user-agent") or ""
    except (AttributeError, KeyError):  # pragma: no cover - defensive
        return None
    return hashlib.sha256(agent.encode("utf-8", "replace")).hexdigest()


def _session_cookie(request: Request):
    """`request.session`, or `None` when no `SessionMiddleware` is mounted."""
    try:
        return request.session
    except (AssertionError, AttributeError):
        return None


def _aware(moment: datetime | None) -> datetime | None:
    """A naive timestamp read back from a column is UTC; say so explicitly."""
    if moment is None:
        return None
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment


async def start_session(
    request: Request,
    session: AsyncSession,
    user_id: int,
    *,
    expected_session_version: int,
) -> str | None:
    """Mint one browser session, inside the account guard, and commit it.

    Returns the identifier written into the cookie, or `None` when the account
    is gone, inactive, or **no longer on the credential generation the caller
    authorized against** — a refusal the caller does not recover from: the user
    is simply not signed in.

    **`expected_session_version` is the credential generation that authorized
    this mint, and it is not optional.** Checking `is_active` alone is not
    enough: an administrator's password reset bumps `session_version` and
    revokes every row, and it can commit in the window between the caller
    verifying a credential and this function taking the guard. Adopting
    whatever version the locked re-read happens to see would then hand the
    *old* password a brand-new, fully valid session — the account-wide
    invalidator defeated by the very race the guard exists to close, and the
    reset's own revocation sweep already behind it. The three callers each pass
    the generation they hold: `login_submit` the version it verified the
    password against, `register_submit` the freshly inserted row's, and the
    self-service change the bumped version it has just committed.

    **One guarded critical section.** Take the account guard, re-read the user
    `FOR UPDATE` with `populate_existing=True`, require the row to exist and be
    active, insert, commit — and only then write the identifier into the
    cookie.

    *Why it commits, when the revoke helpers deliberately do not.*
    `get_session` neither commits nor rolls back, so an insert left to a
    caller's discretion is an insert that may never happen; the cookie handed
    to the browser beside it would authenticate nothing, which is a hard logout
    loop on the very next request. Making the helper own the transaction makes
    "the row exists before the cookie leaves" a property of one function rather
    than of three call sites' discipline.

    *Why the guard and the re-read are not belt-and-braces.* A mint that runs
    after its caller's guard has been released can be overtaken by an
    administrator's deactivation, and will then insert a **live row for a
    just-disabled account**. Validation refuses that row while `is_active` is
    false, which hides it — and the day the account is reactivated it becomes a
    working credential nobody granted and nobody saw. Serializing the mint
    against the handlers that deactivate is what removes the window: the insert
    either precedes the deactivation, and is revoked by it, or never happens.

    `populate_existing` is load-bearing for the same reason it is on the OAuth
    rotation re-read: a `SELECT … FOR UPDATE` whose row is already in the
    session's identity map hands back the *loaded* object with its pre-lock
    attribute values, and `is_active` is read here in Python. Every caller
    arrives with that row already loaded.

    The cookie is rewritten from scratch (`clear()` first), so a mint is also
    session-fixation hygiene: nothing a previous session left behind survives
    it. A caller that wants to flash a message must therefore flash **after**
    the mint, never before.
    """
    # Imported here rather than at module scope because `src/oauth/__init__.py`
    # imports `src/oauth/routes.py`, which imports this module: a top-level
    # `from src.oauth.grants import …` makes that cycle a hard ImportError at
    # startup. The lock primitive lives in `grants.py` because the panel's two
    # routers cannot import each other, and `grants.py` is dependency-light on
    # purpose — the cycle is the package `__init__`, not the module.
    from src.oauth.grants import lock_account_guard

    await lock_account_guard(session)
    locked = await session.execute(
        select(User)
        .where(User.id == user_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    user = locked.scalar_one_or_none()
    if user is None or user.is_active is not True:
        # Nothing was written, but this transaction holds the guard and the
        # caller goes on to render a page: release it rather than leave an
        # advisory lock held for the rest of the request.
        await session.rollback()
        return None
    if user.session_version != expected_session_version:
        # A reset (or another change that bumps the account-wide version)
        # committed between the caller's credential check and this lock. The
        # credential that authorized this mint is the *previous* generation, so
        # minting here would resurrect it: the reset revoked every row it could
        # see, and this insert lands after that sweep. Refuse, for the same
        # reason and on the same terms as the deactivation branch above.
        await session.rollback()
        return None

    sid = secrets.token_urlsafe(SESSION_ID_BYTES)
    now = datetime.now(timezone.utc)
    session.add(
        UserSession(
            id=hash_session_id(sid),
            user_id=user.id,
            created_at=now,
            last_seen_at=now,
            # Absolute, never extended (D3). Starlette re-signs the cookie on
            # any response that modifies the session, so the cookie's own age
            # slides; the row must be the tighter bound, or a session used
            # daily never expires at all — which is precisely the durable
            # access #198 is about.
            expires_at=now + timedelta(seconds=settings.session_max_age),
            user_agent_hash=_user_agent_hash(request),
        )
    )
    await session.commit()

    cookie = _session_cookie(request)
    if cookie is not None:
        cookie.clear()
        cookie["user_id"] = user.id
        cookie["session_version"] = user.session_version
        cookie["is_admin"] = bool(user.is_admin)
        cookie["username"] = user.username
        cookie[SESSION_ID_KEY] = sid
    return sid


async def revoke_session(session: AsyncSession, sid_hash: str) -> int:
    """Revoke one session row. **Does not commit** — the caller does.

    `AND revoked_at IS NULL`, so a second revocation does not rewrite a
    historical revocation time. Returns the number of rows actually flipped.
    """
    result = await session.execute(
        update(UserSession)
        .where(UserSession.id == sid_hash, UserSession.revoked_at.is_(None))
        .values(revoked_at=func.now())
    )
    return result.rowcount or 0


async def revoke_user_sessions(session: AsyncSession, user_id: int) -> int:
    """Revoke every live session of one user. **Does not commit** — the caller does.

    The asymmetry with `start_session` is the contract. Every caller here —
    the self-service password change, the administrative reset, the
    deactivation, the soft delete — holds the account guard, and the
    documented rule for that critical section is that **nothing may commit
    between taking the lock and writing the flags it protects**. A helper that
    committed would silently break the last-admin guard from inside.

    Returns the number of rows flipped, so a caller can record a count without
    a second query.
    """
    result = await session.execute(
        update(UserSession)
        .where(UserSession.user_id == user_id, UserSession.revoked_at.is_(None))
        .values(revoked_at=func.now())
    )
    return result.rowcount or 0


async def touch_session(
    request: Request,
    session: AsyncSession,
    row: UserSession,
    *,
    protect: "tuple[object, ...]" = (),
) -> bool:
    """Refresh `last_seen_at` on the request's **own** session. Never raises.

    Returns whether the update was issued and committed.

    Three restrictions, each answering a specific failure:

    * **The request's own `AsyncSession`, never a second one.** A request
      holding two connection-pool leases halves a pool that tops out at
      `pool_size + max_overflow` = fifteen; the sixteenth caller anywhere in
      the process — an MCP tool call, `/token`, the indexer — waits
      `pool_timeout` and then 500s. A telemetry field must not be able to take
      the server down.
    * **`GET`/`HEAD` only, committed here and now.** Committing inside the
      dependency is safe precisely because no handler work is pending on a safe
      request. It also releases the row lock this `UPDATE` takes *before* any
      handler can ask for the account-guard advisory lock — otherwise a
      mutating panel request would hold this row lock while waiting on a lock
      an administrator holds while revoking that same user's sessions, which is
      a deadlock between the two.
    * **Throttled to `session_touch_interval_seconds`.** Nothing authorizes on
      `last_seen_at`; a write per request buys nothing and costs a commit.

    Any failure is swallowed: a `panel_session_touch_failed` record is emitted
    and the request is served. **The `UPDATE` runs inside a savepoint**, so the
    ordinary failure — the write refused while the reads succeed — rolls back
    that savepoint alone and leaves the enclosing transaction, and every object
    loaded in it, **attached and writable**. That last word is the point: a
    plain rollback here detached the authenticated user, and a write through a
    detached instance commits nothing and says nothing. `protect` names the
    instances whose loaded state must survive the rarer case where the *commit*
    fails and a real rollback is unavoidable.
    The exception's **class name only** reaches the log — SQLAlchemy renders
    bound parameters into an error's text, and one of them here is the stored
    session hash (the engine also sets `hide_parameters=True`; this is the
    second layer).

    **Through the permit, not through the bare logger.** A stale browser
    hammering `GET` against a database that is refusing this `UPDATE` drives
    one record per request, throttled by nothing — the touch interval gates the
    *write*, and a failing write never records a new `last_seen_at` to throttle
    against, so the interval check passes every time. That is the unbounded
    flood channel D18 exists to close, and the subject is the resolved user,
    who is the party actually generating it.
    """
    method = (getattr(request, "method", "") or "").upper()
    if method not in ("GET", "HEAD"):
        return False
    last_seen = _aware(getattr(row, "last_seen_at", None))
    now = datetime.now(timezone.utc)
    if last_seen is not None and now - last_seen < timedelta(
        seconds=settings.session_touch_interval_seconds
    ):
        return False
    # **The `UPDATE` runs inside a SAVEPOINT, and only the savepoint is rolled
    # back when it fails.** `Session.rollback()` restores the identity map to
    # the transaction's start, which **expunges every object that became
    # persistent inside it** — and the object loaded inside *this* one is the
    # authenticated `User` this function's caller is about to return. Rolling
    # the request's own transaction back to recover from a failed *telemetry*
    # write therefore handed the panel a **detached** user.
    #
    # Detached is the dangerous kind of broken, because it is silent. Every
    # column already loaded still reads, so nothing raises and the page renders
    # exactly as expected. What breaks is writing: a mutation through a
    # detached instance is not in the session's unit of work, so a later
    # `commit()` reports success and persists **nothing**. A refused
    # `last_seen_at` update turned any subsequent write through the request's
    # own user object into a lost update that announced itself nowhere. (An
    # *expired* instance would instead raise `MissingGreenlet` on the next read
    # — loud, and not what actually happens here. Measured, not assumed:
    # `tests/integration/test_issue_198_touch_failure_isolation.py`.)
    #
    # The reachable trigger is narrow and entirely plausible: the `UPDATE` is
    # refused while the `SELECT`s succeed — a revoked `UPDATE` grant on
    # `user_sessions`, a trigger rejecting the write, a check constraint added
    # by hand. A savepoint rollback restores the connection and leaves the
    # enclosing transaction, and everything loaded in it, exactly as it was.
    try:
        async with session.begin_nested():
            await session.execute(
                update(UserSession)
                .where(UserSession.id == row.id, UserSession.revoked_at.is_(None))
                .values(last_seen_at=now)
            )
    except Exception as exc:  # noqa: BLE001 - telemetry may not fail a request
        # The savepoint is gone; the outer transaction is untouched and there
        # is deliberately nothing else to undo.
        _touch_failed(request, row, "touch", exc)
        return False

    try:
        await session.commit()
        return True
    except Exception as exc:  # noqa: BLE001 - nor may the commit
        _touch_failed(request, row, "touch", exc)
        # A failed commit leaves the session unusable, and its rollback will
        # evict what is loaded whether or not a savepoint was used. `protect`
        # names the instances whose already-loaded state the caller still
        # needs; expunging them deliberately, before the rollback does it by
        # accident, is what makes the outcome a *stated* one — a detached
        # object whose columns read — rather than a side effect nobody chose.
        # It cannot make the write survive: nothing can, once the transaction
        # carrying it is gone.
        for instance in protect:
            with contextlib.suppress(Exception):
                session.expunge(instance)
        try:
            await session.rollback()
        except Exception as rollback_exc:  # noqa: BLE001 - nor may the recovery
            _touch_failed(request, row, "rollback", rollback_exc)
        return False


def _touch_failed(request, row, stage: str, exc: BaseException) -> None:
    """One `panel_session_touch_failed`, bounded and class-only.

    `stage` is `touch` (the `UPDATE`/commit) or `rollback` (the recovery that
    follows it), and it rides in `reason` because that is the allow-list's one
    closed-vocabulary field. The pair matters: a failing update with a working
    rollback is a database refusing one statement, while a failing rollback is
    a connection that is gone — the same request, two different pages.

    Never the exception's text and never a traceback: this failure is a
    statement binding `user_sessions.session_hash`, and rendering it is how the
    registry's own key would reach a shared sink.
    """
    security_events.emit(
        "panel_session_touch_failed",
        subject=security_events.subject_for(
            user_id=getattr(row, "user_id", None), request=request
        ),
        reason=stage,
        user_id=getattr(row, "user_id", None),
        error_type=type(exc).__name__,
        route=getattr(getattr(request, "url", None), "path", None),
    )


def _replay_refused(
    request: Request,
    reason: str,
    *,
    user_id: int | None,
    sid: str | None,
) -> None:
    """One `panel_session_replay_refused`. The caller clears the cookie.

    **No credential material, ever.** Not the identifier, and not its stored
    SHA-256 — that digest is the registry's key, so a record carrying it names
    a specific live session. Where a session must be identified it is by
    `token_tag`, the catalogue's `sha:` plus eight hex characters, which is
    shorter than the twelve-character fragment the canary test forbids.
    """
    security_events.emit(
        "panel_session_replay_refused",
        subject=security_events.subject_for(user_id=user_id, request=request),
        reason=reason,
        user_id=user_id if isinstance(user_id, int) and not isinstance(user_id, bool) else None,
        token_tag=security_events.redacted_token_tag(sid),
        route=getattr(getattr(request, "url", None), "path", None),
        client_ip=security_events.client_ip(request),
    )


async def get_active_session_user(
    request: Request,
    session: AsyncSession,
) -> User | None:
    """Resolve and validate a multi-user browser session. One implementation.

    Every browser entry point — `require_user_panel`, the login page's
    already-signed-in short-circuit, `authorize_get` and `authorize_post` —
    comes through here, so the checks cannot diverge. A fifth entry point
    reading `request.session["user_id"]` directly is the shape of this
    function's regression, and was the defect removed from `login_form`.

    The cookie must carry **both** `user_id` and `sid`; the row is resolved by
    `sha256(sid)` and refused when it is absent, revoked, expired, or owned by
    a different user than the cookie names (D14 — the two are written together
    at mint and can only disagree through tampering or a bug, so a
    disagreement is an error rather than a preference for one of them). Then
    the existing user-exists / `is_active` / `session_version` checks run as
    before: the registry is the per-session control and `session_version` the
    account-wide one, and both have to pass.

    **Every refusal clears the cookie**, so a rejected cookie cannot be
    replayed against a different route — notably OAuth consent — after a
    password reset, a deactivation or a logout. **And every refusal is
    recorded**, under one of `REPLAY_REFUSAL_REASONS`: clearing the cookie
    signs a browser out mid-session, and an operator asked why must be able to
    answer it from the log for all eight branches. The lone anonymous
    request — no `user_id` at all — is not a refusal and records nothing.
    """
    cookie = _session_cookie(request)
    if cookie is None:
        return None
    user_id = cookie.get("user_id")
    sid = cookie.get(SESSION_ID_KEY)
    if user_id is None:
        # An anonymous request, not a refusal: nothing to clear, nothing to
        # record.
        return None
    if not sid:
        # A correctly-signed cookie from before the registry existed. Refused
        # rather than grandfathered: accepting it would keep the #198 replay
        # window open for a further seven days after the fix shipped.
        _replay_refused(request, "no_session_id", user_id=user_id, sid=None)
        cookie.clear()
        return None

    row = (
        await session.execute(
            select(UserSession).where(UserSession.id == hash_session_id(sid))
        )
    ).scalar_one_or_none()

    now = datetime.now(timezone.utc)
    reason: str | None = None
    if row is None:
        reason = "unknown_session"
    elif row.revoked_at is not None:
        reason = "revoked_session"
    elif (_aware(row.expires_at) or now) <= now:
        reason = "expired_session"
    elif row.user_id != user_id:
        reason = "user_mismatch"

    if reason is not None:
        _replay_refused(request, reason, user_id=user_id, sid=sid)
        cookie.clear()
        return None

    result = await session.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None or not user.is_active:
        _replay_refused(
            request,
            "user_missing" if user is None else "user_inactive",
            user_id=user_id,
            sid=sid,
        )
        cookie.clear()
        return None
    # Starlette sessions are signed client-side cookies. Binding the cookie to
    # a database-backed version lets password resets invalidate every
    # previously issued session, and it is the one invalidator that still works
    # if a registry write is lost — which is why it stays beside the registry
    # rather than being replaced by it.
    if cookie.get("session_version") != user.session_version:
        _replay_refused(request, "version_mismatch", user_id=user_id, sid=sid)
        cookie.clear()
        return None

    # `protect=(user,)`: the touch is optional, and its failure must not cost
    # this request the identity it just resolved.
    await touch_session(request, session, row, protect=(user,))
    return user


async def require_user(
    user: User | _SingleUserSentinel | None = Depends(get_current_user),
) -> User | _SingleUserSentinel:
    if user is None or (isinstance(user, User) and not user.is_active):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    return user


async def require_admin(
    user: User | _SingleUserSentinel = Depends(require_user),
) -> User | _SingleUserSentinel:
    if not user.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin required")
    return user
