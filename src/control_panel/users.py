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
- Must be unique among active users. Two users sharing a vault would
  produce silently overlapping `notes_metadata` rows and confused links.

When admin mutates a user's `vault_path`, we call
`clear_user_vault_cache(user_id)` so the indexer's next pass and any
authenticated API/MCP request picks up the new value without a process
restart.
"""
import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth.passwords import hash_password
from src.auth.session import _SingleUserSentinel
from src.config import settings
from src.control_panel.routes import _panel_context, require_admin_panel
from src.csrf import verify_csrf
from src.database import get_session
from src.models.db import APIKey, NoteMetadata, User
from src.services.vault import clear_user_vault_cache

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
def _validate_vault_path(p: str) -> tuple[str | None, str | None]:
    """Return `(normalized_path, error)` — at most one of the two is set.

    Empty or None → returns `(None, None)`: it's valid to clear a user's
    vault_path (their vault tools just error until reassigned).
    """
    raw = (p or "").strip()
    if not raw:
        return None, None
    # Canonicalize: forbid `..` traversal and trailing slashes.
    if ".." in Path(raw).parts:
        return None, "Vault path may not contain '..' traversal."
    normalized = os.path.normpath(raw)
    # `os.path.normpath` collapses double slashes and trailing slashes.
    # We accept either the legacy mount or a strict /vaults/ subpath.
    legacy = settings.vault_path.rstrip("/")
    if normalized != legacy and not normalized.startswith("/vaults/"):
        return None, (
            f"Vault path must be either '{legacy}' (legacy mount) or a "
            "subpath of '/vaults/'."
        )
    if not Path(normalized).is_dir():
        return None, (
            f"Vault path '{normalized}' does not exist as a directory "
            "inside the container. Check the docker-compose volume mount."
        )
    return normalized, None


async def _check_vault_path_unique(
    session: AsyncSession, normalized: str, exclude_user_id: int | None
) -> str | None:
    """Reject reuse of the same vault_path among active users."""
    q = select(User.username).where(
        User.vault_path == normalized,
        User.is_active.is_(True),
    )
    if exclude_user_id is not None:
        q = q.where(User.id != exclude_user_id)
    other = (await session.execute(q)).scalar_one_or_none()
    if other is not None:
        return f"Vault path '{normalized}' is already assigned to user '{other}'."
    return None


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


# --- Routes ---------------------------------------------------------------


@router.get("/", response_class=HTMLResponse)
async def list_users(
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: User | _SingleUserSentinel = Depends(require_admin_panel),
):
    # Aggregate per-user counts (api_keys + notes) in one query each.
    key_counts = dict(
        (row.user_id, int(row.cnt))
        for row in (
            await session.execute(
                select(APIKey.user_id, func.count(APIKey.id).label("cnt"))
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

    result = await session.execute(select(User).order_by(User.created_at.asc()))
    users = []
    for u in result.scalars().all():
        users.append({
            "id": u.id,
            "username": u.username,
            "is_admin": u.is_admin,
            "is_active": u.is_active,
            "vault_path": u.vault_path,
            "last_login_at": u.last_login_at.isoformat() if u.last_login_at else None,
            "created_at": u.created_at.isoformat(),
            "api_keys": key_counts.get(u.id, 0),
            "notes": note_counts.get(u.id, 0),
        })

    flash = request.query_params.get("flash")
    flash_kind = request.query_params.get("flash_kind", "ok")
    error = request.query_params.get("error")

    return templates.TemplateResponse(request, "users.html", _panel_context(request, user, {
        "active": "users",
        "users": users,
        "flash": flash,
        "flash_kind": flash_kind,
        "error": error,
    }))


@router.post("/create")
async def create_user(
    username: str = Form(...),
    initial_password: str = Form(...),
    session: AsyncSession = Depends(get_session),
    user: User | _SingleUserSentinel = Depends(require_admin_panel),
):
    normalized = (username or "").strip().lower()
    if not _USERNAME_RE.match(normalized):
        return RedirectResponse(
            "/admin/users/?error=" + _q("Username must be 1–64 chars, lowercase letters / digits / underscores only."),
            status_code=303,
        )
    if len(initial_password) < 8:
        return RedirectResponse(
            "/admin/users/?error=" + _q("Initial password must be at least 8 characters."),
            status_code=303,
        )

    existing = (await session.execute(select(User.id).where(User.username == normalized))).scalar_one_or_none()
    if existing is not None:
        return RedirectResponse(
            "/admin/users/?error=" + _q(f"Username '{normalized}' already exists."),
            status_code=303,
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
        return RedirectResponse(
            "/admin/users/?error=" + _q("Could not create user (DB integrity error)."),
            status_code=303,
        )

    return RedirectResponse(
        f"/admin/users/?flash=" + _q(f"User '{normalized}' created. Set their vault path next."),
        status_code=303,
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

    error = request.query_params.get("error")
    flash = request.query_params.get("flash")
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
        "error": error,
        "flash": flash,
    }))


@router.post("/{user_id}/edit")
async def edit_user_submit(
    user_id: int,
    vault_path: str = Form(""),
    vault_path_custom: str = Form(""),
    is_admin: str = Form(""),
    is_active: str = Form(""),
    session: AsyncSession = Depends(get_session),
    user: User | _SingleUserSentinel = Depends(require_admin_panel),
):
    # If the JS toggle didn't run (or the user picked the "Custom path…"
    # option without JS), the form may submit `vault_path=__custom__` plus
    # the actual path in `vault_path_custom`. Reconcile here.
    if vault_path == "__custom__":
        vault_path = vault_path_custom
    result = await session.execute(select(User).where(User.id == user_id))
    target = result.scalar_one_or_none()
    if target is None:
        raise HTTPException(404, "User not found")

    new_admin = is_admin == "on" or is_admin == "true" or is_admin == "1"
    new_active = is_active == "on" or is_active == "true" or is_active == "1"

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
            if isinstance(user, User) and target.id == user.id:
                return _back_with_error(
                    user_id,
                    "Refusing to remove the last admin (yourself). Promote another user to admin first.",
                )
            return _back_with_error(
                user_id, "Refusing to demote or deactivate the last active admin."
            )

    normalized, err = _validate_vault_path(vault_path)
    if err:
        return _back_with_error(user_id, err)
    if normalized:
        uniq_err = await _check_vault_path_unique(
            session, normalized, exclude_user_id=target.id
        )
        if uniq_err:
            return _back_with_error(user_id, uniq_err)

    old_vault = target.vault_path
    target.vault_path = normalized
    target.is_admin = new_admin
    target.is_active = new_active

    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        return _back_with_error(user_id, "Database integrity error (vault path may not be unique).")

    # Invalidate the in-process vault cache so the next indexer pass and
    # any authenticated request resolves the new path.
    if old_vault != normalized:
        clear_user_vault_cache(target.id)

    return RedirectResponse(
        f"/admin/users/?flash=" + _q(f"Updated user '{target.username}'."),
        status_code=303,
    )


@router.post("/{user_id}/delete")
async def delete_user(
    user_id: int,
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: User | _SingleUserSentinel = Depends(require_admin_panel),
):
    """Soft-delete (set is_active=false) unless `?permanent=true`.

    Refuses to delete the last active admin (defense against locking the
    panel out entirely). The cascade FK on `users.id` handles cleanup of
    api_keys / oauth_clients / oauth_tokens / notes_metadata on permanent
    delete; usage_logs use SET NULL so historical analytics survive.
    """
    permanent = request.query_params.get("permanent") == "true"

    result = await session.execute(select(User).where(User.id == user_id))
    target = result.scalar_one_or_none()
    if target is None:
        raise HTTPException(404, "User not found")

    # Last-admin guard for both soft and hard delete. If `target` is the
    # *only* active admin (including the case where target is yourself),
    # refuse — admin must promote someone else first. This is the only
    # defense against locking the panel out entirely; self-deletion is
    # otherwise allowed once another admin exists.
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
                "Refusing to delete the last active admin — promote someone else first."
            )

    if permanent:
        await session.delete(target)
        await session.commit()
        clear_user_vault_cache(target.id)
        return RedirectResponse(
            f"/admin/users/?flash=" + _q(f"User '{target.username}' permanently deleted."),
            status_code=303,
        )

    target.is_active = False
    await session.commit()
    clear_user_vault_cache(target.id)
    return RedirectResponse(
        f"/admin/users/?flash=" + _q(f"User '{target.username}' deactivated."),
        status_code=303,
    )


@router.post("/{user_id}/reset-password")
async def reset_password(
    user_id: int,
    new_password: str = Form(...),
    session: AsyncSession = Depends(get_session),
    user: User | _SingleUserSentinel = Depends(require_admin_panel),
):
    if len(new_password) < 8:
        return _back_with_error(user_id, "New password must be at least 8 characters.")

    result = await session.execute(select(User).where(User.id == user_id))
    target = result.scalar_one_or_none()
    if target is None:
        raise HTTPException(404, "User not found")

    target.password_hash = hash_password(new_password)
    await session.commit()

    return RedirectResponse(
        f"/admin/users/?flash=" + _q(f"Password reset for '{target.username}'."),
        status_code=303,
    )


# --- Internal helpers -----------------------------------------------------


def _q(s: str) -> str:
    """Minimal URL-encode for the flash/error querystrings."""
    from urllib.parse import quote
    return quote(s)


def _back_with_error(user_id: int, msg: str) -> RedirectResponse:
    return RedirectResponse(
        f"/admin/users/{user_id}/edit?error={_q(msg)}",
        status_code=303,
    )


def _back_to_list_with_error(msg: str) -> RedirectResponse:
    return RedirectResponse(
        f"/admin/users/?error={_q(msg)}&flash_kind=err",
        status_code=303,
    )
