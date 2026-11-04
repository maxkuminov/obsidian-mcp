"""OIDC account links: resolution, first-login binding, admin operations (#416).

The link state lives on `users` (migration 037): a bound pair
`(oidc_issuer, oidc_subject, oidc_linked_at)` and a pending expectation
`(oidc_expected_issuer, oidc_expected_email, oidc_expected_set_at)`. This
module is the one place that reads and writes them. It takes plain values —
`iss`, `sub`, `email`, `email_verified` from a token the caller has already
fully validated — and never talks to the provider.

**Resolution order (design D2).** `(iss, sub)` first: a bound user is that
user, and the email claim is not even looked at. Only when no row is bound to
`(iss, sub)` is the email examined, normalized, and matched against a live
pending expectation under the same issuer. A match with `email_verified` the
boolean `True` may bind; a match without it is `unverified`; anything else is
`none`.

**Binding (D4).** `resolve` is unlocked. `prepare_login` takes the account
guard, re-reads the row `FOR UPDATE`, re-checks every condition, writes the
bind (pending branch only) and `last_login_at`, and **does not commit**: the
caller's `start_session` commits the bind and the session row together, so a
refused mint leaves no binding. On any refusal `prepare_login` rolls back.

**Admin operations** (`set_expectation`, `bind_subject`, `unlink`) also never
commit. They are called by panel handlers that already hold the admin guard
and have re-checked the actor; they re-read the target `FOR UPDATE` and write
inside a SAVEPOINT, so a constraint violation is reported as `conflict`
without aborting the caller's guarded transaction.

Nothing here logs a subject, an issuer or an email.
"""
from __future__ import annotations

import datetime
import unicodedata
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.db import User

#: An expectation matches for this many days after the admin set it (D2). A
#: module constant on purpose, not a setting.
PENDING_DAYS = 7

EMAIL_MIN_LENGTH = 3
EMAIL_MAX_LENGTH = 254
SUBJECT_MAX_LENGTH = 255
ISSUER_MAX_LENGTH = 2048

_ASCII_WHITESPACE = " \t\n\r\x0b\x0c"
_ASCII_UPPER_TO_LOWER = str.maketrans(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"
)

# The constraint whose violation means "this identity is bound to another
# user"; and the one meaning "this address is pending for another user".
_IDENTITY_CONSTRAINT = "uq_users_oidc_identity"
_EXPECTED_CONSTRAINT = "uq_users_oidc_expected"


# --------------------------------------------------------------------------
# normalization
# --------------------------------------------------------------------------


def normalize_email(value: object) -> str | None:
    """The stored and compared form of an email, or None when it has none.

    Strip surrounding ASCII whitespace; then require 3–254 characters, exactly
    one `@` with a non-empty local part and domain, and no whitespace or
    control characters anywhere (nor a lone surrogate, which the database
    could not encode). Lower-case **ASCII letters only**; every non-ASCII
    character stays as entered. No IDN or Unicode folding: that would add a
    homograph surface to an exact match.

    Pure, and never raises: a non-string returns None.
    """
    if not isinstance(value, str):
        return None
    try:
        text = value.strip(_ASCII_WHITESPACE)
        if not EMAIL_MIN_LENGTH <= len(text) <= EMAIL_MAX_LENGTH:
            return None
        if text.count("@") != 1:
            return None
        local, _, domain = text.partition("@")
        if not local or not domain:
            return None
        for ch in text:
            if ch.isspace() or unicodedata.category(ch) in ("Cc", "Cs"):
                return None
        return text.translate(_ASCII_UPPER_TO_LOWER)
    except Exception:  # pragma: no cover - defensive: the contract is "never raises"
        return None


def valid_subject(value: object) -> bool:
    """1–255 printable ASCII characters, no whitespace (D11's bind field)."""
    return (
        isinstance(value, str)
        and 1 <= len(value) <= SUBJECT_MAX_LENGTH
        and all("\x21" <= ch <= "\x7e" for ch in value)
    )


def _valid_issuer(value: object) -> bool:
    return isinstance(value, str) and 1 <= len(value) <= ISSUER_MAX_LENGTH


def pending_expires_at(set_at: datetime.datetime) -> datetime.datetime:
    """When an expectation set at `set_at` stops matching."""
    return set_at + datetime.timedelta(days=PENDING_DAYS)


def _live_cutoff():
    """SQL: expectations set after this still match (database clock)."""
    return func.now() - datetime.timedelta(days=PENDING_DAYS)


# --------------------------------------------------------------------------
# resolution
# --------------------------------------------------------------------------


ResolutionKind = Literal["bound", "pending", "unverified", "none"]


@dataclass(frozen=True)
class Resolution:
    """What a validated `(iss, sub, email, email_verified)` resolves to.

    `kind`:
      * `bound` — `user_id` is bound to `(iss, sub)`; email was not examined.
      * `pending` — `user_id` has a live expectation for `email` under `iss`,
        and `email_verified` was `True`; `prepare_login` may bind it.
      * `unverified` — the same match, but `email_verified` was not `True`.
      * `none` — nothing matched (including an absent, malformed or expired
        email); `user_id` is None.

    `email` is the **normalized** email on the `pending` / `unverified`
    branches, else None.
    """

    kind: ResolutionKind
    iss: str
    sub: str
    user_id: int | None = None
    email: str | None = None


async def resolve(
    session: AsyncSession,
    iss: str,
    sub: str,
    email: str | None,
    email_verified: bool,
) -> Resolution:
    """Resolve a validated identity. Unlocked SELECTs; writes nothing."""
    bound = (
        await session.execute(
            select(User.id).where(User.oidc_issuer == iss, User.oidc_subject == sub)
        )
    ).scalar_one_or_none()
    if bound is not None:
        return Resolution("bound", iss, sub, user_id=bound)

    normalized = normalize_email(email)
    if normalized is None:
        return Resolution("none", iss, sub)

    pending = (
        await session.execute(
            select(User.id).where(
                User.oidc_expected_issuer == iss,
                User.oidc_expected_email == normalized,
                User.oidc_expected_set_at > _live_cutoff(),
            )
        )
    ).scalar_one_or_none()
    if pending is None:
        return Resolution("none", iss, sub)
    kind: ResolutionKind = "pending" if email_verified is True else "unverified"
    return Resolution(kind, iss, sub, user_id=pending, email=normalized)


# --------------------------------------------------------------------------
# first-login bind / login preparation
# --------------------------------------------------------------------------


RefusalReason = Literal["unlinked", "email_unverified", "link_conflict", "account"]


@dataclass(frozen=True)
class PrepareResult:
    """The outcome of `prepare_login`.

    On success (`reason is None`): `user_id` and `session_version` (from the
    locked re-read, to pass as `start_session(expected_session_version=…)`),
    and `linked` True when this call wrote a first-login bind (the caller
    emits `panel_oidc_linked` after its commit). Nothing is committed.

    On refusal: `reason` is one of the closed `RefusalReason` values, the
    other fields are None / False, and the session has been rolled back.
    """

    reason: RefusalReason | None
    user_id: int | None = None
    session_version: int | None = None
    linked: bool = False

    @property
    def ok(self) -> bool:
        return self.reason is None


async def _refuse(session: AsyncSession, reason: RefusalReason) -> PrepareResult:
    await session.rollback()
    return PrepareResult(reason)


def _constraint_of(exc: IntegrityError) -> str | None:
    """The violated constraint's name, walked down SQLAlchemy's wrapping
    (`.orig`, then `__cause__` — asyncpg's own error carries the name)."""
    node = getattr(exc, "orig", None)
    seen = []
    while node is not None and node not in seen:
        seen.append(node)
        name = getattr(node, "constraint_name", None)
        if name:
            return name
        node = getattr(node, "__cause__", None)
    for node in (*seen, exc):
        for candidate in (_IDENTITY_CONSTRAINT, _EXPECTED_CONSTRAINT):
            if candidate in str(node):
                return candidate
    return None


async def _identity_held_elsewhere(
    session: AsyncSession, iss: str, sub: str, user_id: int
) -> bool:
    other = (
        await session.execute(
            select(User.id).where(
                User.oidc_issuer == iss,
                User.oidc_subject == sub,
                User.id != user_id,
            )
        )
    ).first()
    return other is not None


async def prepare_login(session: AsyncSession, resolution: Resolution) -> PrepareResult:
    """Bind (pending branch) and stamp `last_login_at`, under the account guard.

    Steps 2–3 of D4: take `lock_account_guard`, re-read the user `FOR UPDATE`
    with `populate_existing=True`, re-check every condition, write, flush —
    **no commit**. `start_session` re-takes the same advisory lock in the same
    transaction, which is re-entrant, and commits everything together.

    Refusals, each after a rollback:
      * `unverified` resolution → `email_unverified`; `none` → `unlinked`
        (nothing is locked or read);
      * the row is gone or inactive → `account`, before any write;
      * bound branch: the row is no longer bound to `(iss, sub)` → `unlinked`;
      * pending branch: another row now holds `(iss, sub)`, or a unique
        violation on `uq_users_oidc_identity` → `link_conflict`; the row is no
        longer pending with the same `(issuer, email)`, is expired, or is
        bound → `unlinked`.
    """
    if resolution.kind == "unverified":
        return await _refuse(session, "email_unverified")
    if resolution.kind not in ("bound", "pending") or resolution.user_id is None:
        return await _refuse(session, "unlinked")

    from src.oauth.grants import lock_account_guard

    await lock_account_guard(session)
    row = (
        await session.execute(
            select(User, (User.oidc_expected_set_at > _live_cutoff()).label("live"))
            .where(User.id == resolution.user_id)
            .with_for_update(of=User)
            .execution_options(populate_existing=True)
        )
    ).one_or_none()
    if row is None:
        return await _refuse(session, "account")
    user, live = row
    if user.is_active is not True:
        return await _refuse(session, "account")

    values: dict = {"last_login_at": func.now()}
    linked = False
    if resolution.kind == "bound":
        if user.oidc_issuer != resolution.iss or user.oidc_subject != resolution.sub:
            # An unlink (or unlink-and-rebind) committed since `resolve`.
            return await _refuse(session, "unlinked")
    else:
        if await _identity_held_elsewhere(
            session, resolution.iss, resolution.sub, user.id
        ):
            return await _refuse(session, "link_conflict")
        still_pending = (
            user.oidc_subject is None
            and user.oidc_issuer is None
            and user.oidc_expected_issuer == resolution.iss
            and user.oidc_expected_email == resolution.email
            and live is True
        )
        if not still_pending:
            return await _refuse(session, "unlinked")
        values.update(
            oidc_issuer=resolution.iss,
            oidc_subject=resolution.sub,
            oidc_linked_at=func.now(),
            oidc_expected_issuer=None,
            oidc_expected_email=None,
            oidc_expected_set_at=None,
        )
        linked = True

    try:
        await session.execute(
            update(User)
            .where(User.id == user.id)
            .values(**values)
            .execution_options(synchronize_session=False)
        )
        await session.flush()
    except IntegrityError as exc:
        if _constraint_of(exc) == _IDENTITY_CONSTRAINT:
            return await _refuse(session, "link_conflict")
        await session.rollback()
        raise
    return PrepareResult(
        None, user_id=user.id, session_version=user.session_version, linked=linked
    )


# --------------------------------------------------------------------------
# the last reachable administrator (D15)
# --------------------------------------------------------------------------


TargetAfter = Literal["unlinked", "demoted", "deactivated", "deleted"]
_TARGET_AFTER = frozenset({"unlinked", "demoted", "deactivated", "deleted"})


async def reachable_admins_after(
    session: AsyncSession,
    issuer: str,
    *,
    target_id: int,
    target_after: TargetAfter,
) -> int:
    """How many reachable administrators remain once `target_id` takes the
    post-mutation state `target_after`.

    A reachable admin is a row with `is_admin AND is_active AND oidc_issuer =
    issuer AND oidc_subject IS NOT NULL`. Each of the four states removes the
    target from that set (unbound, not admin, not active, gone), so the count
    is over every other row. Called inside the admin guard, after the actor
    re-check, in the transaction that writes; it reads committed state plus
    this transaction's own writes, so calling it before or after the
    target's write gives the same answer.
    """
    if target_after not in _TARGET_AFTER:
        raise ValueError(f"unknown target_after {target_after!r}")
    return (
        await session.execute(
            select(func.count(User.id)).where(
                User.is_admin.is_(True),
                User.is_active.is_(True),
                User.oidc_issuer == issuer,
                User.oidc_subject.is_not(None),
                User.id != target_id,
            )
        )
    ).scalar_one()


# --------------------------------------------------------------------------
# admin operations (no commit)
# --------------------------------------------------------------------------


AdminOutcome = Literal["ok", "bound_already", "conflict", "invalid"]


@dataclass(frozen=True)
class AdminResult:
    """The outcome of an admin link operation. Nothing is committed.

    * `ok` — written; the caller commits.
    * `bound_already` — the target is bound; expect/bind never re-point a
      bound user (unlink first). Nothing written.
    * `conflict` — the email is pending for, or the subject bound to,
      another user. Nothing written.
    * `invalid` — the input failed validation, the target does not exist, or
      (unlink) the target has neither a binding nor an expectation. Nothing
      written.

    `revoked_sessions` is the `revoke_user_sessions` count (unlink only).
    """

    outcome: AdminOutcome
    revoked_sessions: int = 0

    @property
    def ok(self) -> bool:
        return self.outcome == "ok"


async def _lock_target(session: AsyncSession, user_id: int) -> User | None:
    return (
        await session.execute(
            select(User)
            .where(User.id == user_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()


async def _write_guarded(session: AsyncSession, user_id: int, values: dict) -> bool:
    """UPDATE inside a SAVEPOINT. False on a link-uniqueness violation (the
    savepoint is rolled back; the caller's transaction and guard survive)."""
    try:
        async with session.begin_nested():
            await session.execute(
                update(User)
                .where(User.id == user_id)
                .values(**values)
                .execution_options(synchronize_session=False)
            )
    except IntegrityError as exc:
        if _constraint_of(exc) in (_IDENTITY_CONSTRAINT, _EXPECTED_CONSTRAINT):
            return False
        raise
    return True


async def set_expectation(
    session: AsyncSession, user_id: int, issuer: str, email: object
) -> AdminResult:
    """Record that the first verified login with `email` under `issuer` binds
    `user_id`. Replaces an existing expectation; refused on a bound row."""
    normalized = normalize_email(email)
    if normalized is None or not _valid_issuer(issuer):
        return AdminResult("invalid")
    user = await _lock_target(session, user_id)
    if user is None:
        return AdminResult("invalid")
    if user.oidc_subject is not None or user.oidc_issuer is not None:
        return AdminResult("bound_already")
    other = (
        await session.execute(
            select(User.id).where(
                User.oidc_expected_issuer == issuer,
                User.oidc_expected_email == normalized,
                User.id != user_id,
            )
        )
    ).first()
    if other is not None:
        return AdminResult("conflict")
    written = await _write_guarded(
        session,
        user_id,
        {
            "oidc_expected_issuer": issuer,
            "oidc_expected_email": normalized,
            "oidc_expected_set_at": func.now(),
        },
    )
    return AdminResult("ok" if written else "conflict")


async def bind_subject(
    session: AsyncSession, user_id: int, issuer: str, subject: object
) -> AdminResult:
    """Bind `user_id` to `(issuer, subject)` immediately, clearing any
    expectation. Refused on a bound row."""
    if not valid_subject(subject) or not _valid_issuer(issuer):
        return AdminResult("invalid")
    user = await _lock_target(session, user_id)
    if user is None:
        return AdminResult("invalid")
    if user.oidc_subject is not None or user.oidc_issuer is not None:
        return AdminResult("bound_already")
    if await _identity_held_elsewhere(session, issuer, subject, user_id):
        return AdminResult("conflict")
    written = await _write_guarded(
        session,
        user_id,
        {
            "oidc_issuer": issuer,
            "oidc_subject": subject,
            "oidc_linked_at": func.now(),
            "oidc_expected_issuer": None,
            "oidc_expected_email": None,
            "oidc_expected_set_at": None,
        },
    )
    return AdminResult("ok" if written else "conflict")


async def unlink(session: AsyncSession, user_id: int) -> AdminResult:
    """Clear all six link columns and revoke the user's live sessions, in the
    caller's transaction. `invalid` (nothing written) when the row is gone or
    has neither a binding nor an expectation."""
    from src.auth.session import revoke_user_sessions

    user = await _lock_target(session, user_id)
    if user is None:
        return AdminResult("invalid")
    if (
        user.oidc_subject is None
        and user.oidc_issuer is None
        and user.oidc_expected_email is None
        and user.oidc_expected_issuer is None
    ):
        return AdminResult("invalid")
    await session.execute(
        update(User)
        .where(User.id == user_id)
        .values(
            oidc_issuer=None,
            oidc_subject=None,
            oidc_linked_at=None,
            oidc_expected_issuer=None,
            oidc_expected_email=None,
            oidc_expected_set_at=None,
        )
        .execution_options(synchronize_session=False)
    )
    count = await revoke_user_sessions(session, user_id)
    return AdminResult("ok", revoked_sessions=count)
