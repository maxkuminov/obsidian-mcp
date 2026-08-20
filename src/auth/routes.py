"""Auth routes — login, logout, bootstrap admin registration.

The router is mounted at the FastAPI app level in `src/main.py` ONLY when
`settings.multi_user_mode` is true. In single-user mode the router is not
mounted at all, so these paths 404.

`/admin/auth/*` and `/admin/register` live under the `/admin` prefix so that
Traefik's `chain-oauth@file` middleware (which gates `/admin/*` on the
production deploy) still fronts them. That gating is what makes the bootstrap
race-free in practice — only an already-SSO'd admin can reach
`/admin/register`. The application also enforces a strict empty-users-table
guard with a PostgreSQL transaction-scoped advisory lock for defense in
depth.
"""
import os
import re
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Form, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth.passwords import hash_password, verify_password
from src.config import settings
from src.csrf import generate_csrf_token, verify_csrf
from src.database import get_session
from src.limiter import limiter
from src.models.db import APIKey, NoteMetadata, OAuthClient, OAuthCode, OAuthToken, UsageLog, User
from src.oauth.grants import USER_BOOTSTRAP_LOCK_KEY
from src.services.vault import validate_vault_root_path, warm_user_vault_cache

router = APIRouter(tags=["auth"], dependencies=[Depends(verify_csrf)])

# Templates resolved from the panel directory so all auth templates can
# extend `auth_base.html` co-located with the existing panel templates.
templates = Jinja2Templates(
    directory=os.path.join(os.path.dirname(__file__), "..", "control_panel", "templates")
)

# Advisory-lock key for the bootstrap-registration critical section. Any
# distinct 32-bit int works; this is just a constant the lock function
# expects. Two concurrent /admin/register POSTs will serialize on this key.
# Shared with the OAuth token-minting handlers, which take the same key so a
# mint cannot insert a new ownerless token in the window between this
# transaction's `WHERE user_id IS NULL` claim and its COMMIT. The value is
# unchanged; only its definition moved, so a rolling deploy still serializes.
_BOOTSTRAP_LOCK_KEY = USER_BOOTSTRAP_LOCK_KEY


# --- Helpers --------------------------------------------------------------


_USERNAME_RE = re.compile(r"^[a-z0-9_]{1,64}$")


def _safe_next(next_url: str | None) -> str:
    """Return `next_url` if it's a safe in-app redirect, else `/admin/`.

    Prevents an open-redirect via `?next=https://evil.example/...`. Only
    same-origin paths (must start with `/` and not `//`) are allowed.
    """
    if not next_url:
        return "/admin/"
    if not next_url.startswith("/") or next_url.startswith("//"):
        return "/admin/"
    return next_url


async def _users_table_empty(session: AsyncSession) -> bool:
    count = (await session.execute(select(func.count(User.id)))).scalar() or 0
    return count == 0


def _render_login(
    request: Request,
    *,
    error: str | None = None,
    next_url: str = "/admin/",
    username: str = "",
    status_code: int = 200,
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "login.html",
        {"error": error, "next": next_url, "username": username, "csrf_token": generate_csrf_token(request)},
        status_code=status_code,
    )


def _render_register(
    request: Request,
    *,
    error: str | None = None,
    username: str | None = None,
    vault_path: str | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    default_username = os.environ.get("BOOTSTRAP_ADMIN_USERNAME", "max")
    return templates.TemplateResponse(
        request,
        "register.html",
        {
            "error": error,
            "username": username if username is not None else default_username,
            "vault_path": vault_path if vault_path is not None else settings.vault_path,
            "csrf_token": generate_csrf_token(request),
        },
        status_code=status_code,
    )


# --- Login / logout -------------------------------------------------------


@router.get("/admin/auth/login", response_class=HTMLResponse)
async def login_form(request: Request, next: str = "/admin/"):
    # If already logged in, redirect straight through.
    if request.session.get("user_id") is not None:
        return RedirectResponse(_safe_next(next), status_code=status.HTTP_302_FOUND)
    return _render_login(request, next_url=_safe_next(next))


@router.post("/admin/auth/login")
@limiter.limit("5/minute")
async def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    next: str = Form("/admin/"),
    session: AsyncSession = Depends(get_session),
):
    target = _safe_next(next)
    normalized = (username or "").strip().lower()

    # Constant error message: don't leak whether the username exists.
    invalid_msg = "Invalid credentials"

    result = await session.execute(select(User).where(User.username == normalized))
    user = result.scalar_one_or_none()

    if user is None or not user.is_active or not verify_password(password, user.password_hash):
        return _render_login(
            request,
            error=invalid_msg,
            next_url=target,
            username=normalized,
            status_code=status.HTTP_401_UNAUTHORIZED,
        )

    # Update last_login_at in the same session.
    await session.execute(
        update(User).where(User.id == user.id).values(last_login_at=datetime.now(timezone.utc))
    )
    await session.commit()

    # Warm the per-user vault-path cache so any subsequent panel route /
    # vault tool call in this process can resolve `_vault_root(user.id)`
    # without a sync DB miss. Skips users with no vault_path assigned
    # (warm_user_vault_cache filters them out).
    await warm_user_vault_cache(session, user.id)

    request.session.clear()
    request.session["user_id"] = user.id
    request.session["session_version"] = user.session_version
    request.session["is_admin"] = bool(user.is_admin)
    request.session["username"] = user.username

    return RedirectResponse(target, status_code=status.HTTP_302_FOUND)


@router.post("/admin/auth/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/admin/auth/login", status_code=status.HTTP_302_FOUND)


# --- Bootstrap registration ----------------------------------------------


@router.get("/admin/register", response_class=HTMLResponse)
async def register_form(
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    # Bootstrap is closed once any user exists.
    if not await _users_table_empty(session):
        return RedirectResponse("/admin/auth/login", status_code=status.HTTP_302_FOUND)
    return _render_register(request)


@router.post("/admin/register")
async def register_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    password_confirm: str = Form(...),
    vault_path: str = Form(...),
    session: AsyncSession = Depends(get_session),
):
    # Early UX-friendly validation (no DB roundtrip).
    normalized = (username or "").strip().lower()
    if not _USERNAME_RE.match(normalized):
        return _render_register(
            request,
            error="Username must be 1–64 chars, lowercase letters / digits / underscores only.",
            username=normalized,
            vault_path=vault_path,
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    if len(password) < 8:
        return _render_register(
            request,
            error="Password must be at least 8 characters.",
            username=normalized,
            vault_path=vault_path,
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    if password != password_confirm:
        return _render_register(
            request,
            error="Passwords do not match.",
            username=normalized,
            vault_path=vault_path,
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    vault_path = (vault_path or "").strip()
    if not vault_path:
        return _render_register(
            request,
            error="Vault path is required.",
            username=normalized,
            vault_path=vault_path,
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    normalized_vp, vp_err = validate_vault_root_path(vault_path)
    if vp_err:
        return _render_register(
            request,
            error=vp_err,
            username=normalized,
            vault_path=vault_path,
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    vault_path = normalized_vp or vault_path

    # Critical section: take a transaction-scoped advisory lock so two
    # concurrent first-visits serialize. Inside the lock we re-check that
    # `users` is empty before inserting. The lock auto-releases on commit
    # or rollback.
    try:
        await session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _BOOTSTRAP_LOCK_KEY})

        if not await _users_table_empty(session):
            # Someone else won the race. Don't reveal that to the form
            # (just send them to login).
            await session.rollback()
            return RedirectResponse(
                "/admin/auth/login", status_code=status.HTTP_302_FOUND
            )

        new_user = User(
            username=normalized,
            password_hash=hash_password(password),
            is_admin=True,
            is_active=True,
            vault_path=vault_path,
        )
        session.add(new_user)
        await session.flush()  # populate new_user.id

        # Backfill — bind every pre-flag-flip orphaned row to the new admin.
        # All inside the same transaction so a failure rolls everything back.
        uid = new_user.id
        await session.execute(
            update(APIKey).where(APIKey.user_id.is_(None)).values(user_id=uid)
        )
        await session.execute(
            update(OAuthClient).where(OAuthClient.user_id.is_(None)).values(user_id=uid)
        )
        await session.execute(
            update(OAuthToken).where(OAuthToken.user_id.is_(None)).values(user_id=uid)
        )
        await session.execute(
            update(OAuthCode).where(OAuthCode.user_id.is_(None)).values(user_id=uid)
        )
        await session.execute(
            update(NoteMetadata).where(NoteMetadata.user_id.is_(None)).values(user_id=uid)
        )
        await session.execute(
            update(UsageLog).where(UsageLog.user_id.is_(None)).values(user_id=uid)
        )

        # Stamp last_login_at since we're logging the new admin in immediately.
        new_user.last_login_at = datetime.now(timezone.utc)

        await session.commit()
    except Exception:
        await session.rollback()
        raise

    # Warm the freshly-created admin's vault-path cache before any vault
    # tool call. The bootstrap flow flips us straight into /admin/ which
    # in phase 4 will load the dashboard for `uid`.
    await warm_user_vault_cache(session, uid)

    request.session.clear()
    request.session["user_id"] = uid
    request.session["session_version"] = new_user.session_version
    request.session["is_admin"] = True
    request.session["username"] = normalized

    return RedirectResponse("/admin/", status_code=status.HTTP_302_FOUND)
