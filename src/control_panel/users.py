"""Admin user management router — list, create, edit, delete, reset password.

Mounted at `/admin/users`. Every route depends on `require_admin_panel`, so
regular users hitting any path here get a 403 (and the sidebar hides the
link). In single-user mode the sentinel reports `is_admin=True`, so the
panel works exactly as today — though in practice nobody navigates to
`/admin/users` in single-user mode because the sidebar link is gated on
`multi_user_mode` (see base.html).

Validation rules for `vault_path` (panel-side, before the DB sees it):

- Must be either `settings.vault_path` (legacy `/obsidian` mount on max's
  existing deployment) OR a non-empty subpath of `/vaults/`. Defends
  against an admin pointing a user at `/etc`, the host's home dir, etc.
- Must exist as a directory inside the container fs. Catches the
  docker-compose mount-not-yet-applied case loudly instead of silently
  later when the indexer fails.
- Must not overlap another active user's assigned root. Not merely
  "unique": an equal string is only the degenerate case. `/vaults/team`
  beside `/vaults/team/private` is two different strings and one root
  beneath the other, and a symlink or bind mount makes two different
  strings name one directory — in every one of those shapes each tenant's
  tools reach the other's files and the indexer files both under one
  `user_id`. `_check_vault_root_conflict` runs the shared
  `vault_overlap.relation_between` predicate against every other active
  assigned root, and a peer root that cannot be opened refuses the
  assignment rather than admitting on "we could not look".

When admin mutates a user's `vault_path`, we call
`clear_user_vault_cache(user_id)` so the indexer's next pass and any
authenticated API/MCP request picks up the new value without a process
restart.
"""
import logging
import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select, update as sa_update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth.passwords import hash_password, validate_new_password
from src.auth.session import _SingleUserSentinel, revoke_user_sessions
from src.config import settings
from src.control_panel.flash import ERR, flash
from src.control_panel.routes import (
    _actor,
    _log_panel_forbidden,
    _panel_context,
    _request_route,
    require_admin_panel,
)
from src.csrf import verify_csrf
from src.database import get_session
from src.models.db import APIKey, NoteMetadata, UsageLog, User
from src.oauth.grants import ACCOUNT_GUARD_LOCK_KEY, lock_account_guard
from src.services import security_events, vault_overlap
from src.services.vault import clear_user_vault_cache, validate_vault_root_path

router = APIRouter(prefix="/admin/users", tags=["users"])

templates = Jinja2Templates(
    directory=os.path.join(os.path.dirname(__file__), "templates")
)


# Attach `require_admin_panel` to every route. Non-admin sessions get 403.
router.dependencies.append(Depends(require_admin_panel))
router.dependencies.append(Depends(verify_csrf))


# --- Helpers --------------------------------------------------------------


# Allowed vault_path patterns: absolute paths under /vaults/, OR the
# legacy single-user mount `settings.vault_path` (default /obsidian) for
# max's existing setup post-flag-flip.
_validate_vault_path = validate_vault_root_path


async def _check_vault_root_conflict(
    session: AsyncSession, normalized: str, exclude_user_id: int | None
) -> str | None:
    """Refuse an assignment whose root collides with another active user's.

    **This subsumes the old `_check_vault_path_unique`.** That function compared
    two `vault_path` *strings* and rejected only an exact duplicate, which let
    three shapes through: `/vaults/team` beside `/vaults/team/private`
    (different strings, one root beneath the other, so every tool and the
    indexer reach across both accounts), a symlink or bind-mount alias of one
    directory under two pathnames, and the same pair reached through a
    symlinked component. Two functions answering "do these roots collide" is
    how the two answers drift apart, so there is now one predicate —
    `vault_overlap.relation_between` — and this is its only caller here.

    Exact duplication is not lost: it is the degenerate case of both checks, it
    is reported as `identical`, and `assignment_conflict_message` keeps the
    wording operators already know for it. The other two relations name the
    relation, because "already assigned" would be false for them and would send
    an operator looking for a duplicate string that does not exist.

    **A root that cannot be opened refuses the assignment**, naming that root
    and saying the overlap could not be ruled out — never reporting an overlap
    that was not observed. Identity is precisely the check that catches what
    string equality already misses, so admitting on "we could not look" is the
    expensive direction; `validate_vault_root_path` sets the precedent by
    refusing the *candidate* for the same missing-mount case.

    Only active users **holding an assignment** are peers: an inactive or
    unassigned account is served by nothing and indexed by nothing, so it can
    create no overlap. That is the same population `vault_overlap` and
    `indexer._active_user_ids` scope to.
    """
    q = select(User.id, User.username, User.vault_path).where(
        User.is_active.is_(True),
        User.vault_path.isnot(None),
    )
    if exclude_user_id is not None:
        q = q.where(User.id != exclude_user_id)
    peers = (await session.execute(q.order_by(User.id))).all()
    if not peers:
        return None

    candidate = await vault_overlap.observe_root(normalized)
    for peer in peers:
        peer_root = await vault_overlap.observe_root(
            peer.vault_path, user_id=peer.id, username=peer.username
        )
        if not peer_root.examinable:
            return vault_overlap.peer_unexaminable_message(
                peer_root.assignment, peer_root.cause
            )
        if not candidate.examinable:
            # The candidate passed `validate_vault_root_path`'s `is_dir()` and
            # then could not be opened — a permission wall, a mount that went
            # away between the two calls, or one that stopped answering. With a
            # peer present and nothing observed about the candidate, identity
            # cannot be ruled out, so this refuses for the same reason a
            # peer that cannot be opened does.
            return (
                f"Vault path '{candidate.assignment}' could not be examined: "
                f"{vault_overlap.cause_text(candidate.cause)}. The assignment "
                "is refused because an overlap with another active user's "
                "vault could not be ruled out."
            )
        relation = vault_overlap.relation_between(candidate, peer_root)
        if relation is not None:
            return vault_overlap.assignment_conflict_message(
                normalized, relation, peer.username
            )
    return None


def _quarantine_display(entry) -> dict[str, str]:
    """The users list's rendering of one recorded quarantine.

    Built from the snapshot's **recorded** facts and nothing else — no `users`
    row is re-read to resolve the peer. The operator's first move on reading
    "overlaps bob" is to edit or delete one of the two accounts, and a display
    that resolved the name at render time would show a changed path, or a blank
    where a deleted peer was, beside a condition that is still in force. The
    cell says "as at" so the staleness is legible as a fact about the past.
    """
    reason = entry.reason
    if isinstance(reason, vault_overlap.Overlap):
        detail = (
            f"vault root {vault_overlap.relation_text(reason.relation)} the "
            f"vault root of user '{reason.peer_username}'"
        )
    else:
        detail = (
            "vault root could not be examined — "
            f"{vault_overlap.cause_text(reason.cause)}"
        )
    return {
        "detail": detail,
        "detected_at": entry.detected_at.isoformat(),
    }


def _list_available_vaults() -> list[str]:
    """Scan `/vaults/*` for directories. Used to populate the edit dropdown.

    The legacy `settings.vault_path` is also offered. Result is a sorted
    list of absolute paths; the caller adds a "leave unassigned" option.
    Silent on errors (missing /vaults dir → empty list).
    """
    out: list[str] = []
    legacy = settings.vault_path.rstrip("/")
    if Path(legacy).is_dir():
        out.append(legacy)
    vaults_root = Path("/vaults")
    if vaults_root.is_dir():
        try:
            for item in sorted(vaults_root.iterdir()):
                if item.is_dir() and not item.name.startswith("."):
                    out.append(str(item))
        except OSError:
            pass
    return out


_USERNAME_RE = __import__("re").compile(r"^[a-z0-9_]{1,64}$")


# Every handler that can change a `users.is_admin` / `users.is_active` flag
# takes this one advisory lock before it counts the remaining active admins.
# The count and the write are otherwise two statements in separate
# transactions with nothing between them: two admins demoting each other at
# the same moment both read "1 other active admin remains", both pass the
# guard, and the panel ends up with zero admins and no way back in through
# the UI. The lock is transaction-scoped (`pg_advisory_xact_lock`), so it is
# released by the commit or the rollback — there is no unlock path to forget,
# and a crashed backend cannot strand it.
#
# The value is arbitrary but must never change: it is the *name* of the
# critical section, and two builds using different constants would not
# exclude each other during a rolling restart. It now lives in
# `src/oauth/grants.py` — beside this codebase's other advisory-lock
# primitives, and importable from `src/control_panel/routes.py` and
# `src/auth/session.py`, which need the same key and cannot import this
# module. **The value is unchanged**; only where it is written down moved.
_ADMIN_GUARD_LOCK_KEY = ACCOUNT_GUARD_LOCK_KEY


async def _lock_admin_guard(session: AsyncSession) -> None:
    """Enter the "who may be an admin, and what may happen to an account"
    critical section.

    Must be taken *before* reading the admin count and released only by the
    same transaction that writes the flags — i.e. never commit between this
    and the write. That rule now also covers the session revocations these
    handlers issue: their `UPDATE user_sessions` rides this same transaction,
    which is exactly why `revoke_user_sessions` does not commit.

    **A delegation, not a second implementation.** `lock_account_guard` in
    `src/oauth/grants.py` holds the one definition, and the self-service
    password change (#197) and every session mint (#198) take it too. They
    have to take *the same key as this one*: an administrator deactivating an
    account and that account changing its own password must exclude each
    other, and two keys do not exclude each other. The value is unchanged —
    only where it is written down moved.
    """
    await lock_account_guard(session)


async def _actor_still_privileged(
    session: AsyncSession, user: User | _SingleUserSentinel
) -> bool:
    """Re-read the acting admin's own flags *inside* the critical section.

    `require_admin_panel` authorised this request before the advisory lock
    was even requested, and waiting for that lock is exactly the window in
    which another admin's demotion or deactivation of *this* actor commits.
    Trusting the `User` loaded by the dependency would let a just-demoted
    account perform the privileged mutation it queued for — the guard would
    serialize the writes correctly and still let the wrong one through.

    Single-user mode has no `users` row to re-read (the sentinel's `id` is
    None); there is also no second admin who could have demoted anyone, so
    the sentinel is simply still privileged.
    """
    if not isinstance(user, User):
        return True
    row = (
        await session.execute(
            select(User.is_admin, User.is_active).where(User.id == user.id)
        )
    ).one_or_none()
    # A deleted actor (row is None) is not privileged either.
    return row is not None and row.is_admin is True and row.is_active is True


_ACTOR_REVOKED_MSG = (
    "Your account's admin access changed while that request was in flight — "
    "nothing was saved. Sign in again."
)


def _log_sessions_revoked(
    request: Request,
    user: User | _SingleUserSentinel,
    target_id: int,
    reason: str,
    count: int,
) -> None:
    """The `panel_sessions_revoked` record for an administrative account event.

    **Emitted after the commit that makes it true**, for the same reason
    `panel_password_reset` is: a record asserting that a user's sessions were
    ended is one an operator acts on, and a record written before the commit
    would survive a rollback that ended nothing.

    `user_id` — the *unsuffixed* name — is the account whose sessions went,
    because these callers hold the row they revoked against. (The logout path
    uses `user_id_session`: there the id is copied from the cookie and no row
    was read. The catalogue declares both so the provenance stays legible.)

    `count` is what `revoke_user_sessions` returned, so an operator reading the
    line knows whether anything was live. Nothing else rides along: not the
    identifier, not its stored hash, not the new password.
    """
    actor_user_id, _actor_username = _actor(user)
    security_events.emit(
        "panel_sessions_revoked",
        level=logging.INFO,
        subject=security_events.subject_for(user_id=actor_user_id, request=request),
        reason=reason,
        user_id=target_id,
        count=count,
    )


def _log_actor_revoked(request: Request, user) -> None:
    """The `actor_revoked` record for a lost race inside the admin guard.

    `require_admin_panel` authorised this request before the advisory lock was
    even requested; the wait for that lock is the window in which another
    admin's demotion of *this* actor commits. Nothing else in the server tells
    an operator that happened — the caller sees only a flash message — so the
    refusal is the record.

    `user_id` is deliberately absent: the resource is not what was refused,
    the actor's own standing was. `create_user` takes neither this lock nor
    this re-check and therefore has no such refusal to log (residual R7).
    """
    _log_panel_forbidden(request, "actor_revoked", user, None)


# --- Routes ---------------------------------------------------------------


@router.get("/", response_class=HTMLResponse)
async def list_users(
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: User | _SingleUserSentinel = Depends(require_admin_panel),
):
    # Aggregate per-user counts (api_keys + notes) in one query each.
    #
    # Keys are counted twice: total, and the `is_active` subset. Revocation
    # sets `is_active = False` without deleting the row (routes.py's
    # `revoke_key`), and `src/mcp_server/auth.py` authenticates only active
    # rows — so a bare `count(*)` told an auditing admin "bob — API Keys: 4"
    # when all four were revoked, and the panel had no surface anywhere on
    # which to discover that (#76). Rendering "N active / M total" states
    # both numbers instead of picking one and leaving the ambiguity.
    key_counts = dict(
        (row.user_id, (int(row.active), int(row.total)))
        for row in (
            await session.execute(
                select(
                    APIKey.user_id,
                    func.count(APIKey.id).label("total"),
                    func.count(APIKey.id)
                    .filter(APIKey.is_active.is_(True))
                    .label("active"),
                )
                .group_by(APIKey.user_id)
            )
        ).all()
    )
    note_counts = dict(
        (row.user_id, int(row.cnt))
        for row in (
            await session.execute(
                select(NoteMetadata.user_id, func.count(NoteMetadata.id).label("cnt"))
                .group_by(NoteMetadata.user_id)
            )
        ).all()
    )

    # The quarantine comes from the **published snapshot** and nowhere else:
    # one attribute read, no query, no syscall. An account the snapshot names is
    # refused by the admission gate before any tool body runs, so rendering its
    # note count unqualified beside a healthy-looking assignment is the most
    # misleading of the three states — the row count is real, and nothing will
    # serve it. Nothing is deleted to make the display true; the display was
    # what was wrong (#91, and the same argument again).
    snapshot = vault_overlap.published_snapshot()

    result = await session.execute(select(User).order_by(User.created_at.asc()))
    users = []
    for u in result.scalars().all():
        entry = snapshot.entry_for(u.id) if snapshot is not None else None
        users.append({
            "id": u.id,
            "username": u.username,
            "is_admin": u.is_admin,
            "is_active": u.is_active,
            "vault_path": u.vault_path,
            "last_login_at": u.last_login_at.isoformat() if u.last_login_at else None,
            "created_at": u.created_at.isoformat(),
            "api_keys_active": key_counts.get(u.id, (0, 0))[0],
            "api_keys_total": key_counts.get(u.id, (0, 0))[1],
            "notes": note_counts.get(u.id, 0),
            "quarantine": _quarantine_display(entry) if entry is not None else None,
        })

    # `flash` / `flash_kind` come from the session, through `_panel_context`
    # (#138). Reading them from `request.query_params` let a crafted link
    # choose what an authenticated admin read on the page whose controls
    # delete accounts.
    return templates.TemplateResponse(request, "users.html", _panel_context(request, user, {
        "active": "users",
        "users": users,
    }))


@router.post("/create")
async def create_user(
    request: Request,
    username: str = Form(...),
    initial_password: str = Form(...),
    session: AsyncSession = Depends(get_session),
    user: User | _SingleUserSentinel = Depends(require_admin_panel),
):
    normalized = (username or "").strip().lower()
    if not _USERNAME_RE.match(normalized):
        return _back_to_list_with_error(
            request,
            "Username must be 1–64 chars, lowercase letters / digits / underscores only.",
        )
    # One policy, four setters (#197). `create_user` accepted eight characters
    # and handed its input straight to `hash_password`, which **raises** on an
    # embedded NUL — so a NUL in the initial-password field was an unhandled
    # `ValueError` and a 500 on an admin-only route. The validator turns that
    # into a form error and applies the same minimum the account's owner will
    # be held to when they set it themselves; leaving this one at eight would
    # let an administrator set a password its owner is then forbidden to reuse.
    #
    # No `confirm`: this form has one password field.
    password_error = validate_new_password(initial_password)
    if password_error:
        return _back_to_list_with_error(request, password_error)

    existing = (await session.execute(select(User.id).where(User.username == normalized))).scalar_one_or_none()
    if existing is not None:
        return _back_to_list_with_error(
            request, f"Username '{normalized}' already exists."
        )

    new_user = User(
        username=normalized,
        password_hash=hash_password(initial_password),
        is_admin=False,
        is_active=True,
        vault_path=None,
    )
    session.add(new_user)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        return _back_to_list_with_error(
            request, "Could not create user (DB integrity error)."
        )

    return _back_to_list(
        request, f"User '{normalized}' created. Set their vault path next."
    )


@router.get("/{user_id}/edit", response_class=HTMLResponse)
async def edit_user_form(
    user_id: int,
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: User | _SingleUserSentinel = Depends(require_admin_panel),
):
    result = await session.execute(select(User).where(User.id == user_id))
    target = result.scalar_one_or_none()
    if target is None:
        raise HTTPException(404, "User not found")

    available_vaults = _list_available_vaults()
    # Prepend the current value so it's selectable even if not under /vaults
    # (e.g. legacy /obsidian for max).
    if target.vault_path and target.vault_path not in available_vaults:
        available_vaults.insert(0, target.vault_path)

    # Same as `list_users`: the flash rides the session, not the URL (#138).
    return templates.TemplateResponse(request, "user_edit.html", _panel_context(request, user, {
        "active": "users",
        "target": {
            "id": target.id,
            "username": target.username,
            "is_admin": target.is_admin,
            "is_active": target.is_active,
            "vault_path": target.vault_path or "",
        },
        "available_vaults": available_vaults,
        "is_self": (isinstance(user, User) and user.id == target.id),
    }))


@router.post("/{user_id}/edit")
async def edit_user_submit(
    user_id: int,
    request: Request,
    vault_path: str = Form(""),
    vault_path_custom: str = Form(""),
    # `None` means the field was **absent** from the submission; "" means it
    # was present and empty. A disabled checkbox sends nothing at all, so
    # only `None` may be read as "unchanged" — see the self-edit block below.
    is_admin: str | None = Form(None),
    is_active: str | None = Form(None),
    session: AsyncSession = Depends(get_session),
    user: User | _SingleUserSentinel = Depends(require_admin_panel),
):
    # If the JS toggle didn't run (or the user picked the "Custom path…"
    # option without JS), the form may submit `vault_path=__custom__` plus
    # the actual path in `vault_path_custom`. Reconcile here.
    if vault_path == "__custom__":
        vault_path = vault_path_custom
    # Enter the admin critical section before reading anything the guard
    # decides on — including `target`'s own flags, which a concurrent edit
    # could otherwise change between this read and our write.
    await _lock_admin_guard(session)
    if not await _actor_still_privileged(session, user):
        await session.rollback()
        _log_actor_revoked(request, user)
        return _back_to_list_with_error(request, _ACTOR_REVOKED_MSG)
    result = await session.execute(select(User).where(User.id == user_id))
    target = result.scalar_one_or_none()
    if target is None:
        raise HTTPException(404, "User not found")

    def _checked(raw: str | None) -> bool:
        return raw in ("on", "true", "1")

    new_admin = _checked(is_admin)
    new_active = _checked(is_active)

    is_self = isinstance(user, User) and user.id == target.id

    # A self-edit can never change your own role or active flag (#69).
    # user_edit.html states that as an unconditional promise and renders
    # both checkboxes `disabled`; the handler is what makes the promise
    # true, and it must hold for a hand-crafted POST too — the previous
    # rule ("unless you are the last active admin") let an admin with a
    # colleague demote or deactivate themselves out of the panel in one
    # click, with the alert actively encouraging the belief that the
    # toggle was inert.
    #
    # A `disabled` checkbox is not submitted at all, so an **absent** field
    # (`None`) on a self-edit means "unchanged", never "unchecked" — reading
    # it as unchecked would demote the operator on every save. Anything the
    # caller actually *sent* is an intent, and one that would strip the role
    # or the active flag is refused outright rather than silently ignored:
    # that includes a present-but-empty `is_admin=` (a hand-built POST, or a
    # form that lost its value), which must not slip through the same door as
    # a genuinely absent field.
    if is_self:
        if is_admin is not None and target.is_admin and not new_admin:
            return _back_with_error(
                request,
                user_id,
                "You can't remove your own admin role. Ask another admin to do it.",
            )
        if is_active is not None and target.is_active and not new_active:
            return _back_with_error(
                request,
                user_id,
                "You can't deactivate your own account. Ask another admin to do it.",
            )
        new_admin = target.is_admin
        new_active = target.is_active

    # Defense: never let the last active admin be demoted or deactivated.
    # This covers both "max demotes himself" and "max demotes bob" — the
    # operation succeeds only when another active admin exists. Applies
    # whether `target.id == user.id` or not.
    will_lose_admin = target.is_admin and target.is_active and (not new_admin or not new_active)
    if will_lose_admin:
        remaining_admins = (await session.execute(
            select(func.count(User.id)).where(
                User.is_admin.is_(True),
                User.is_active.is_(True),
                User.id != target.id,
            )
        )).scalar() or 0
        if remaining_admins == 0:
            # Unreachable for a self-edit since the block above pins your own
            # flags; kept as a backstop if that guard is ever relaxed.
            if is_self:
                return _back_with_error(
                    request,
                    user_id,
                    "Refusing to remove the last admin (yourself). Promote another user to admin first.",
                )
            return _back_with_error(
                request,
                user_id,
                "Refusing to demote or deactivate the last active admin.",
            )

    normalized, err = _validate_vault_path(vault_path)
    if err:
        return _back_with_error(request, user_id, err)
    # Only when the edit's **resulting** state is active and assigned. An edit
    # whose result is an inactive account, or one with no assignment, can
    # create no overlap — the peer query and the detector both scope to active
    # users holding an assignment — and refusing it would trap the operator in
    # the one state they most need to leave: deactivating or unassigning is the
    # panel's own remedy for a quarantined account, and a guard that refuses
    # *that* because the account still overlaps is a guard with no exit.
    # Reactivating or reassigning is an edit whose result is active and
    # assigned, and runs the full check.
    #
    # It runs here on purpose: after `_lock_admin_guard` took the *existing*
    # `_ADMIN_GUARD_LOCK_KEY` (no second key — outside a lock this is
    # check-then-act, and two admins assigning `/vaults/team` and
    # `/vaults/team/private` at the same moment would each read the other's
    # previous row and both writes would land) and before the commit below.
    if normalized and new_active:
        conflict = await _check_vault_root_conflict(
            session, normalized, exclude_user_id=target.id
        )
        if conflict:
            return _back_with_error(request, user_id, conflict)

    old_vault = target.vault_path
    # Read *before* the write below, because the transition is what decides the
    # revocation: an account that was active and no longer will be. A vault-path
    # edit, a promotion, a demotion or a save that leaves `is_active` alone all
    # revoke nothing — a live session of an account that is still active is a
    # session nobody ended.
    was_active = target.is_active
    target.vault_path = normalized
    target.is_admin = new_admin
    target.is_active = new_active

    # Deactivation is the moment the account stops being able to sign in, so it
    # is the moment its live sessions stop being credentials (#198). Before the
    # commit and with no commit of its own: the account guard above is
    # transaction-scoped and the flag write and the revocation must land or
    # roll back together, or an `IntegrityError` below would leave a user
    # signed out of an account that is still active.
    revoked = 0
    deactivating = was_active and not new_active
    if deactivating:
        revoked = await revoke_user_sessions(session, target.id)

    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        return _back_with_error(request, user_id, "Database integrity error (vault path may not be unique).")

    if deactivating:
        _log_sessions_revoked(request, user, target.id, "user_deactivated", revoked)

    # Invalidate the in-process vault cache so the next indexer pass and
    # any authenticated request resolves the new path.
    if old_vault != normalized:
        clear_user_vault_cache(target.id)

    return _back_to_list(request, f"Updated user '{target.username}'.")


@router.post("/{user_id}/delete")
async def delete_user(
    user_id: int,
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: User | _SingleUserSentinel = Depends(require_admin_panel),
):
    """Soft-delete (set is_active=false) unless `?permanent=true`.

    Refuses a self-targeted delete outright, and refuses to delete the last
    active admin (defense against locking the panel out entirely). The
    cascade FK on `users.id` handles cleanup of api_keys / oauth_clients /
    oauth_tokens / notes_metadata on permanent delete; usage_logs use SET
    NULL so historical analytics survive.

    **The self-edit promise is about the account, not the form (#90).**
    #69/#80 made it unconditional on the edit form above — the role and
    active checkboxes render `disabled` and `edit_user_submit` refuses a
    hand-built POST — and left this handler, reachable from the two forms
    directly beneath them on the same page, still able to reach the same
    `users.is_active` flag by another route whenever one other active admin
    existed. An operator told the toggle is inert reasonably reads "Soft
    delete: sets `is_active=false`. Data preserved" as a different, safer
    control; it is not. So both delete forms refuse a self-target here, and
    refuse it whether or not other active admins exist. Another admin can
    still remove the account; the actor cannot remove their own.

    The permanent form is the worse of the two and is one click further down
    the same page: the cascade on `users.id` also destroys the actor's own
    `api_keys`, `oauth_clients`, `oauth_tokens` and `notes_metadata`, so the
    actor loses every credential that could undo it along with the account —
    unrecoverable from the panel, because the account that could reverse it
    is the one that was deleted.

    The refusal sits under the *existing* `_lock_admin_guard` advisory lock
    and after the existing `_actor_still_privileged` re-check, so an actor
    demoted while queued for the lock is told *that* rather than told they
    cannot delete themselves. It runs before the last-admin count, which is a
    separate and deliberately weaker check and is left exactly as it is.
    """
    permanent = request.query_params.get("permanent") == "true"

    # Same critical section as `edit_user_submit` — a delete and an edit can
    # each remove the other's "remaining admin", so they must exclude each
    # other, not just their own kind.
    await _lock_admin_guard(session)
    if not await _actor_still_privileged(session, user):
        await session.rollback()
        _log_actor_revoked(request, user)
        return _back_to_list_with_error(request, _ACTOR_REVOKED_MSG)
    result = await session.execute(select(User).where(User.id == user_id))
    target = result.scalar_one_or_none()
    if target is None:
        raise HTTPException(404, "User not found")

    # An admin may not remove their own account by either form (#90), and
    # this holds unconditionally — the presence of other active admins does
    # not make it permissible. See the docstring: #69's promise is about the
    # account, not the form it is made on.
    #
    # This must run *before* the last-admin count below, so that a
    # self-target is answered with this message rather than the last-admin
    # one, and before any row is written. Single-user mode has nothing to
    # refuse: `require_admin_panel` yields a `_SingleUserSentinel`, which is
    # not a `User` and carries no `id`, so no target can be the actor. That
    # is the same `isinstance` test `edit_user_submit` uses.
    if isinstance(user, User) and user.id == target.id:
        return _back_to_list_with_error(
            request,
            "You can't delete or deactivate your own account — neither the "
            "soft delete nor the permanent one. Ask another admin to remove it."
        )

    # Last-admin guard for both soft and hard delete. Refuse exactly when the
    # target is *itself* an active admin and no **other** active admin
    # exists — i.e. when the delete would take that count from one to zero.
    # Deliberately no broader: deleting an account that is not an active
    # admin cannot remove one, so it proceeds even on a table that holds no
    # active admin at all, and one of two active admins deleting the *other*
    # leaves an admin and is permitted — that being the removal the
    # self-delete refusal above tells the operator to ask for.
    #
    # For an acting admin who is a `users` row this is now unreachable: a
    # self-target is refused above, and any other target leaves the actor,
    # re-read as active and admin inside this same lock. Its one remaining
    # path is the single-user sentinel, which holds no `users` row and is
    # therefore never counted.
    if target.is_admin and target.is_active:
        remaining_admins = (await session.execute(
            select(func.count(User.id)).where(
                User.is_admin.is_(True),
                User.is_active.is_(True),
                User.id != target.id,
            )
        )).scalar() or 0
        if remaining_admins == 0:
            return _back_to_list_with_error(
                request,
                "Refusing to delete the last active admin — promote someone else first."
            )

    if permanent:
        # `usage_logs.key_id` has no `ON DELETE` — deliberately, so a log row
        # outlives the credential that wrote it (see CLAUDE.md, #77). The
        # database therefore refuses to cascade-delete an `api_keys` row that a
        # usage log still names, and since `User.api_keys` is
        # `passive_deletes=True` the ORM no longer NULLs those references on
        # its own. Do it here, in the same transaction and before the delete,
        # exactly as `delete_key_form` does for a single key: the rows keep
        # their denormalised `actor_kind`/`actor_label`/`actor_ref`, so the
        # Usage page still attributes them.
        #
        # `usage_logs.user_id` and `.oauth_token_id` need nothing — both are
        # `ON DELETE SET NULL`, and the OAuth token rows cascade from the user.
        await session.execute(
            sa_update(UsageLog)
            .where(UsageLog.key_id.in_(select(APIKey.id).where(APIKey.user_id == target.id)))
            .values(key_id=None)
        )
        await session.delete(target)
        await session.commit()
        clear_user_vault_cache(target.id)
        return _back_to_list(
            request, f"User '{target.username}' permanently deleted."
        )

    # A soft delete is a deactivation by another name and its live sessions go
    # the same way (#198) — inside the guard, before the commit, with no commit
    # of its own. The permanent branch above adds **nothing**: `user_sessions`
    # carries `ON DELETE CASCADE` and `User.sessions` declares
    # `passive_deletes=True`, so the database removes those rows and the ORM
    # does not load and delete them one at a time. Adding a revocation there
    # would hide whether the cascade is the thing that actually fires.
    target.is_active = False
    revoked = await revoke_user_sessions(session, target.id)
    await session.commit()
    clear_user_vault_cache(target.id)
    _log_sessions_revoked(request, user, target.id, "user_deleted", revoked)
    return _back_to_list(request, f"User '{target.username}' deactivated.")


@router.post("/{user_id}/reset-password")
async def reset_password(
    user_id: int,
    request: Request,
    new_password: str = Form(...),
    session: AsyncSession = Depends(get_session),
    user: User | _SingleUserSentinel = Depends(require_admin_panel),
):
    # The shared policy (#197). Same constant, same message, same NUL rule as
    # the account owner's own change and as `create_user` — and the NUL check
    # is what stops `hash_password`'s `ValueError` from reaching the client as
    # a 500. Refused before the lock is taken: a refusal that never writes has
    # no business holding the critical section.
    password_error = validate_new_password(new_password)
    if password_error:
        return _back_with_error(request, user_id, password_error)

    # Same critical section as `edit_user_submit` and `delete_user`. A password
    # reset is a full account takeover of the target — it rewrites the hash and
    # bumps `session_version`, invalidating every live session — so an actor
    # whose own admin access was revoked while this request queued must not get
    # to perform it. `require_admin_panel` authorised the request before the
    # lock was requested, and the wait for the lock is precisely the window in
    # which another admin's demotion of *this* actor commits.
    #
    # The lock is transaction-scoped: nothing may commit between it and the
    # write below.
    await _lock_admin_guard(session)
    if not await _actor_still_privileged(session, user):
        await session.rollback()
        _log_actor_revoked(request, user)
        return _back_to_list_with_error(request, _ACTOR_REVOKED_MSG)
    result = await session.execute(select(User).where(User.id == user_id))
    target = result.scalar_one_or_none()
    if target is None:
        raise HTTPException(404, "User not found")

    target.password_hash = hash_password(new_password)
    target.session_version += 1
    # The rows, not just the account-wide counter (#198). `session_version`
    # was the only invalidator this handler had, and it is a second switch
    # rather than a substitute: the registry is what a *specific* live session
    # is refused by, and it is what survives if a cookie ever escapes the
    # version check.
    #
    # **No commit here and none before it.** `revoke_user_sessions` writes on
    # this session and deliberately does not commit, because the account guard
    # taken above is transaction-scoped and its check-then-act atomicity
    # depends on nothing committing between the lock and the write. The one
    # commit below carries the hash, the version bump and the revocation
    # together — a failure rolls back all three, and nothing is revoked.
    revoked = await revoke_user_sessions(session, target.id)
    await session.commit()

    # **After the commit** (D17). Emitted before the commit, a rollback would
    # leave a log line asserting a password was reset that was not — and this
    # is the one panel action that is a full account takeover of the target,
    # so it is exactly the record an operator must be able to trust.
    #
    # D19's pair, both halves: `actor_user_id` is the administrator who did it,
    # `user_id`/`username` the account it was done to. The new password is not
    # a field and has no field to ride in.
    actor_user_id, _actor_username = _actor(user)
    security_events.emit(
        "panel_password_reset",
        level=logging.INFO,
        subject=security_events.subject_for(user_id=actor_user_id, request=request),
        actor_user_id=actor_user_id,
        user_id=target.id,
        username=target.username,
        client_ip=security_events.client_ip(request),
        route=_request_route(request),
    )
    # Keyed on `target.id` alone, so the acting administrator's own session
    # survives resetting somebody else's password — they stay on the page they
    # did it from.
    _log_sessions_revoked(
        request, user, target.id, "admin_password_reset", revoked
    )
    return _back_to_list(request, f"Password reset for '{target.username}'.")


# --- Internal helpers -----------------------------------------------------


def _back_to_list(request: Request | None, msg: str) -> RedirectResponse:
    """Success redirect to the list, with the message in the session (#138)."""
    flash(request, msg)
    return RedirectResponse("/admin/users/", status_code=303)


def _back_with_error(request: Request | None, user_id: int, msg: str) -> RedirectResponse:
    flash(request, msg, ERR)
    return RedirectResponse(f"/admin/users/{user_id}/edit", status_code=303)


def _back_to_list_with_error(request: Request | None, msg: str) -> RedirectResponse:
    flash(request, msg, ERR)
    return RedirectResponse("/admin/users/", status_code=303)
