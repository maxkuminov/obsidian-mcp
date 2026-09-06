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
import logging
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
from src.services import security_events
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
    same-origin paths are allowed: one leading `/`, no scheme, no authority.

    Two refusals beyond "starts with a single slash", both about what a
    *browser* does with the value rather than what a parser says about it:

    * a leading `/` followed by a backslash, where the second slash of `//`
      would be. Browsers normalise that to `//`, so it navigates off-site
      exactly as `//evil.example` would.
    * control characters, including the tab, CR and LF that some clients strip
      *before* resolving the URL, which lets a value reassemble into an
      authority after this check has looked at it.

    The producing side (`require_user_panel` in the panel router) now
    percent-encodes what it puts in `?next=`, so a legitimate path with its own
    query string arrives here intact instead of half of it landing in the login
    URL's own query.
    """
    if not next_url:
        return "/admin/"
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in next_url):
        return "/admin/"
    if not next_url.startswith("/"):
        return "/admin/"
    if next_url[1:2] in ("/", "\\"):
        return "/admin/"
    return next_url


def _bootstrap_refused(request: Request, reason: str) -> None:
    """One `panel_bootstrap_refused` record. The rendered form is unchanged.

    Bootstrap is reachable by anyone Traefik's OAuth chain lets through, and
    every branch below is a form field away, so the record is bounded on the
    client address like every other unauthenticated refusal.
    """
    security_events.emit(
        "panel_bootstrap_refused",
        subject=security_events.subject_for(request=request),
        reason=reason,
        client_ip=security_events.client_ip(request),
    )


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
    # A raw API key left in the session by an unfollowed `/admin/keys` redirect
    # must not outlive the hop it was minted for. The panel's own dependency
    # (`_forget_new_key_flash`) covers every `/admin` and `/api` route; this
    # router is mounted at the app level and shares none of them, so the login
    # form — reachable while a stale session cookie is still being replayed —
    # clears it here. Logout and a successful login already do, through
    # `request.session.clear()`.
    try:
        request.session.pop("flash_new_key", None)
    except (AssertionError, AttributeError):
        pass
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

    # The merged condition is split **for the reason code only**. All three
    # branches fall into one `_render_login(..., status_code=401)`, so the
    # response is byte-identical across them and the log is the only place the
    # cause exists (#191). The order also preserves the original
    # short-circuit: `verify_password` still runs only for an active row.
    reason: str | None = None
    if user is None:
        reason = "unknown_user"
    elif not user.is_active:
        reason = "inactive_user"
    elif not verify_password(password, user.password_hash, user_id=user.id):
        reason = "bad_password"

    if reason is not None:
        # The suppression subject is the client address, never the resolved
        # row: a failed login resolved no credential, and keying on the user
        # would hand an attacker one fresh allowance per valid username they
        # guess.
        security_events.emit(
            "panel_login_failed",
            subject=security_events.subject_for(request=request),
            reason=reason,
            username_submitted=normalized,
            # Present only where a row actually resolved; `unknown_user` has
            # none, and the unsuffixed name may hold nothing else (D15).
            user_id=None if user is None else user.id,
            client_ip=security_events.client_ip(request),
            route=request.url.path,
        )
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

    # After the commit, never before it (D17): a commit that then fails would
    # otherwise leave a record asserting a sign-in that did not happen.
    security_events.emit(
        "panel_login_succeeded",
        level=logging.INFO,
        subject=security_events.subject_for(user_id=user.id, request=request),
        user_id=user.id,
        username=user.username,
        client_ip=security_events.client_ip(request),
        route=request.url.path,
    )

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
    # Read the session **before** it is cleared, and read nothing else: the
    # `_session` suffix is the provenance (D15). Both values are copied from
    # the session cookie without a database lookup, so they may name an account
    # that has since been renamed or deleted — which is exactly what a logout
    # can honestly say, and a logout must not pay for a query to say it.
    try:
        user_id_session = request.session.get("user_id")
        username_session = request.session.get("username")
    except (AssertionError, AttributeError):
        user_id_session = None
        username_session = None

    security_events.emit(
        "panel_logout",
        level=logging.INFO,
        subject=security_events.subject_for(user_id=user_id_session, request=request),
        user_id_session=user_id_session,
        username_session=username_session,
        client_ip=security_events.client_ip(request),
    )

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
        _bootstrap_refused(request, "invalid_username")
        return _render_register(
            request,
            error="Username must be 1–64 chars, lowercase letters / digits / underscores only.",
            username=normalized,
            vault_path=vault_path,
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    if len(password) < 8:
        _bootstrap_refused(request, "weak_password")
        return _render_register(
            request,
            error="Password must be at least 8 characters.",
            username=normalized,
            vault_path=vault_path,
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    if password != password_confirm:
        _bootstrap_refused(request, "password_mismatch")
        return _render_register(
            request,
            error="Passwords do not match.",
            username=normalized,
            vault_path=vault_path,
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    vault_path = (vault_path or "").strip()
    if not vault_path:
        _bootstrap_refused(request, "vault_path_missing")
        return _render_register(
            request,
            error="Vault path is required.",
            username=normalized,
            vault_path=vault_path,
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    normalized_vp, vp_err = validate_vault_root_path(vault_path)
    if vp_err:
        _bootstrap_refused(request, "vault_path_invalid")
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
            _bootstrap_refused(request, "already_bootstrapped")
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

    # After the commit, never after the insert (D17): a commit that then raises
    # would otherwise leave a record asserting an administrator exists who does
    # not — which is the one claim an operator reading this line must be able
    # to trust.
    security_events.emit(
        "panel_bootstrap_admin_created",
        level=logging.INFO,
        subject=security_events.subject_for(user_id=uid, request=request),
        user_id=uid,
        username=normalized,
        client_ip=security_events.client_ip(request),
    )

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
