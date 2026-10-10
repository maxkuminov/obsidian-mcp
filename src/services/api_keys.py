"""Who may create a key, with what limit, and whether there is room for it (#323).

Two surfaces create keys — `POST /api/keys` (JSON) and `POST /admin/keys/create`
(the panel form) — and two edit a key's limit — `PUT /api/keys/{id}/limit` and
`POST /admin/keys/{id}/limit`. Every rule that decides the outcome lives here,
once, so the surfaces cannot drift: a rule that holds on the form and not on the
JSON twin (or the reverse) is exactly how #162 and #323 happened.

The rules, owner decision #323:

* **A blank or omitted limit gets `DEFAULT_DAILY_REQUEST_LIMIT`.** It used to
  mean unlimited on the form, which made a scripted blank form a quota-bypass
  primitive: every new key is a fresh `/mcp` principal, and an unlimited one has
  no daily ceiling at all. With the default null, a create that names no limit
  is refused as missing one instead of silently becoming unlimited.
* **Unlimited is an administrator's explicit request** — the form's `unlimited`
  box (rendered for admins only) or an explicit JSON `null` — on create and on
  edit. A non-admin's request is refused, never downgraded: substituting a
  different outcome for what was explicitly asked is the #194 D9 surprise in
  reverse, and an API client would believe it held an unlimited key.
* **A blank edit is an error.** The edit path never applies the default to an
  existing key; it applies what was typed, or NULL on an admin's explicit
  request, and nothing else.
* **A non-admin holds at most `KEY_MAX_ACTIVE_PER_ACCOUNT` active keys.** The
  creation budget bounds the flow of new principals; this bounds the stock.

The resolvers are pure (no DB, no settings writes) so they can be checked
before anything is charged: a refused or invalid request spends no
key-creation allowance (`src/services/rate_limits.py`).
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select

from src.config import settings
from src.models.db import APIKey, User
from src.services.rate_limits import AccountKey, KeyCreationRefusal, try_charge_key_creation

#: Refusal codes. `forbidden_unlimited` is a 403 + `panel_forbidden`
#: (`unlimited_requires_admin`); the others are ordinary validation errors.
REFUSAL_REQUIRED = "required"
REFUSAL_FORBIDDEN_UNLIMITED = "forbidden_unlimited"
REFUSAL_BLANK_EDIT = "blank_edit"

#: The `panel_forbidden` reason a non-admin unlimited request is recorded with.
FORBIDDEN_UNLIMITED_REASON = "unlimited_requires_admin"

_FORBIDDEN_UNLIMITED_MESSAGE = "Only an administrator can make a key unlimited."


@dataclass(frozen=True)
class LimitRefusal:
    """A limit request that cannot be honoured. `message` is user-facing."""

    code: str
    message: str


@dataclass(frozen=True)
class CapRefusal:
    """The account already holds the maximum number of active keys."""

    active: int
    cap: int

    @property
    def message(self) -> str:
        return (
            f"You already have {self.active} active keys, and the limit is "
            f"{self.cap}. Revoke a key you no longer use to create another."
        )


def _is_admin(user) -> bool:
    return bool(getattr(user, "is_admin", False))


def resolve_create_limit(
    user, *, provided: bool, value: int | None, unlimited: bool
) -> tuple[int | None, LimitRefusal | None]:
    """The `daily_request_limit` a new key receives, or why it cannot be created.

    * `unlimited` — the explicit request (form box `"1"`, JSON explicit
      `null`): NULL for an admin, refused for anyone else. It wins over any
      number sent alongside it.
    * `provided` with a `value` — that value, for anyone.
    * neither (blank form field, omitted JSON field) — the configured default,
      or refused as required when the default is null.

    The default is read per call, so this module never holds a stale copy, and
    is applied here in application code, never as a column default: existing
    keys keep whatever they carry (#194 D9).
    """
    if unlimited:
        if _is_admin(user):
            return None, None
        return None, LimitRefusal(REFUSAL_FORBIDDEN_UNLIMITED, _FORBIDDEN_UNLIMITED_MESSAGE)
    if provided and value is not None:
        return value, None
    default = settings.default_daily_request_limit
    if default is None:
        return None, LimitRefusal(
            REFUSAL_REQUIRED, "A daily request limit is required for a new key."
        )
    return default, None


def resolve_edit_limit(
    user, *, value: int | None, unlimited: bool
) -> tuple[int | None, LimitRefusal | None]:
    """The limit an edit writes to an existing key, or why it cannot.

    Never the default: an edit applies what was asked, and a blank edit is a
    validation error rather than a way to clear a limit. Ownership is the
    caller's (`_assert_key_owner`) and must have run first.
    """
    if unlimited:
        if _is_admin(user):
            return None, None
        return None, LimitRefusal(REFUSAL_FORBIDDEN_UNLIMITED, _FORBIDDEN_UNLIMITED_MESSAGE)
    if value is not None:
        return value, None
    message = (
        "Enter a daily request limit, or tick Unlimited."
        if _is_admin(user)
        else "Enter a daily request limit."
    )
    return None, LimitRefusal(REFUSAL_BLANK_EDIT, message)


def account_key(user) -> AccountKey:
    """The budget's exact account identity: `("user", id)` or the sentinel's.

    The single-user sentinel has `id=None`; it gets one fixed key rather than
    falling back to anything request-derived, so rotating the client address
    cannot reach a fresh account allowance.
    """
    user_id = getattr(user, "id", None)
    if isinstance(user_id, int) and not isinstance(user_id, bool):
        return ("user", user_id)
    return ("single-user",)


def account_subject(user) -> str:
    """The security-event suppression subject for this account.

    Derived from `account_key`, **not** `security_events.subject_for`: that
    helper falls back to the client address when there is no user id, which
    for the single-user sentinel would give each rotated address its own log
    allowance. `user:<id>` matches what `subject_for` yields for a real
    account, so one account's events share one allowance across event types.
    """
    key = account_key(user)
    if key[0] == "user":
        return f"user:{key[1]}"
    return "account:single-user"


async def assert_active_key_capacity(session, user) -> CapRefusal | None:
    """Refuse a non-admin creation that would exceed the active-key cap.

    Takes `SELECT … FOR UPDATE` on the owning `users` row first, so concurrent
    creates for one account serialize and the count is exact. The lock is held
    by the caller's open transaction until it commits the insert or rolls back.

    Admins and the single-user operator are exempt (no row to lock, and an
    admin can already create unlimited keys). "Active" is `is_active`; revoked
    keys do not count, expired-but-unrevoked keys do. An account already over
    the cap is grandfathered — nothing is revoked — and simply cannot add more.
    """
    cap = settings.key_max_active_per_account
    if cap is None or _is_admin(user):
        return None
    user_id = getattr(user, "id", None)
    if not isinstance(user_id, int):
        return None
    await session.execute(select(User.id).where(User.id == user_id).with_for_update())
    active = (
        await session.execute(
            select(func.count(APIKey.id)).where(
                APIKey.user_id == user_id, APIKey.is_active.is_(True)
            )
        )
    ).scalar() or 0
    if active >= cap:
        return CapRefusal(active=int(active), cap=cap)
    return None


async def admit_key_creation(
    session, user, address: str | None
) -> CapRefusal | KeyCreationRefusal | None:
    """Steps 3 and 4 of the create order, shared by both routes.

    The cap is checked before the budget so a cap refusal spends no velocity
    allowance. On any refusal the caller must roll back, which releases the
    `users` row lock. A charged creation whose commit then fails is not refunded
    (L2): a refund path is a second piece of state to get wrong, and an
    over-charge is the safe direction.
    """
    cap_refusal = await assert_active_key_capacity(session, user)
    if cap_refusal is not None:
        return cap_refusal
    return try_charge_key_creation(account_key(user), address)
