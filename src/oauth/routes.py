import hashlib
import os
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urlparse

from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy import select

from src.config import settings
from src.database import async_session
from src.limiter import limiter
from src.models.db import OAuthClient, OAuthCode, OAuthToken

router = APIRouter(tags=["oauth"])
templates = Jinja2Templates(
    directory=os.path.join(os.path.dirname(__file__), "..", "control_panel", "templates")
)

# Valid OAuth scopes
VALID_SCOPES = {"read", "readwrite"}


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _base64url_sha256(verifier: str) -> str:
    """Compute S256 PKCE challenge from verifier."""
    import base64

    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _valid_redirect_uri(uri: str) -> bool:
    try:
        p = urlparse(uri)
        return p.scheme == "https" and bool(p.netloc) and not p.fragment
    except Exception:
        return False


def _validate_scope(scope: str) -> str:
    parts = set(scope.split())
    invalid = parts - VALID_SCOPES
    if invalid:
        raise ValueError(f"Invalid scopes: {invalid}")
    return " ".join(parts & VALID_SCOPES) or "read"


def _state_serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(settings.secret_key, salt="oauth-state")


# --- OAuth Metadata ---


@router.get("/.well-known/oauth-authorization-server")
async def oauth_metadata():
    base = settings.base_url
    return JSONResponse({
        "issuer": base,
        "authorization_endpoint": f"{base}/authorize",
        "token_endpoint": f"{base}/token",
        "registration_endpoint": f"{base}/register",
        "revocation_endpoint": f"{base}/revoke",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["client_secret_post"],
        "scopes_supported": ["read", "readwrite"],
    })


# RFC 9728 protected-resource metadata. MCP clients probe both the bare path
# and the resource-suffixed variant when discovering auth for /mcp.
def _protected_resource_metadata() -> dict:
    base = settings.base_url
    return {
        "resource": f"{base}/mcp",
        "authorization_servers": [base],
        "scopes_supported": ["read", "readwrite"],
        "bearer_methods_supported": ["header"],
    }


@router.get("/.well-known/oauth-protected-resource")
async def oauth_protected_resource():
    return JSONResponse(_protected_resource_metadata())


@router.get("/.well-known/oauth-protected-resource/mcp")
async def oauth_protected_resource_mcp():
    return JSONResponse(_protected_resource_metadata())


# --- Dynamic Client Registration ---


@limiter.limit("3/minute")
@router.post("/register")
async def register_client(request: Request):
    body = await request.json()
    client_name = body.get("client_name", "Unknown Client")
    redirect_uris = body.get("redirect_uris", [])

    if not redirect_uris:
        return JSONResponse({"error": "redirect_uris required"}, status_code=400)

    # Validate all redirect URIs
    for uri in redirect_uris:
        if not _valid_redirect_uri(uri):
            return JSONResponse(
                {"error": "invalid_redirect_uri", "error_description": f"Redirect URI must use https and contain no fragment: {uri}"},
                status_code=400,
            )

    # Validate requested scope
    raw_scope = body.get("scope", "read")
    try:
        scope = _validate_scope(raw_scope)
    except ValueError as exc:
        return JSONResponse({"error": "invalid_scope", "error_description": str(exc)}, status_code=400)

    client_id = secrets.token_hex(16)
    client_secret = secrets.token_hex(32)

    async with async_session() as session:
        client = OAuthClient(
            client_id=client_id,
            client_secret_hash=_hash(client_secret),
            client_name=client_name,
            redirect_uris=redirect_uris,
            scope=scope,
        )
        session.add(client)
        await session.commit()

    return JSONResponse({
        "client_id": client_id,
        "client_secret": client_secret,
        "client_name": client_name,
        "redirect_uris": redirect_uris,
    }, status_code=201)


# --- Authorization Endpoint ---


@router.get("/authorize", response_class=HTMLResponse)
async def authorize_get(
    request: Request,
    response_type: str = Query(...),
    client_id: str = Query(...),
    redirect_uri: str = Query(...),
    code_challenge: str = Query(...),
    code_challenge_method: str = Query("S256"),
    scope: str = Query("read"),
    state: str = Query(""),
):
    # Multi-user mode: an end-user identity must exist on the request
    # session before we can stamp it onto the issued code/token. If there's
    # no session, bounce through login first and come back here. Single-
    # user mode is unchanged — the consent screen renders without any
    # session requirement.
    if settings.multi_user_mode:
        try:
            current_uid = request.session.get("user_id")
        except (AssertionError, AttributeError):
            current_uid = None
        if current_uid is None:
            # Preserve the entire /authorize URL (path + query) so the
            # client doesn't have to re-issue the request after login.
            target = request.url.path
            if request.url.query:
                target = f"{target}?{request.url.query}"
            return RedirectResponse(
                f"/admin/auth/login?next={quote(target, safe='')}",
                status_code=302,
            )

    if response_type != "code":
        return JSONResponse({"error": "unsupported_response_type"}, status_code=400)

    if code_challenge_method != "S256":
        return JSONResponse({"error": "invalid_request", "error_description": "Only S256 supported"}, status_code=400)

    # Validate scope
    try:
        scope = _validate_scope(scope)
    except ValueError as exc:
        return JSONResponse({"error": "invalid_scope", "error_description": str(exc)}, status_code=400)

    async with async_session() as session:
        result = await session.execute(
            select(OAuthClient).where(OAuthClient.client_id == client_id)
        )
        client = result.scalar_one_or_none()

    if client is None:
        return JSONResponse({"error": "invalid_client"}, status_code=400)

    if redirect_uri not in client.redirect_uris:
        return JSONResponse({"error": "invalid_redirect_uri"}, status_code=400)

    # Generate server-side CSRF state and bind it to a signed cookie
    server_state = secrets.token_urlsafe(16)
    signed_state = _state_serializer().dumps(server_state)

    response = templates.TemplateResponse(request, "authorize.html", {
        "client_name": client.client_name,
        "scope": scope,
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "code_challenge": code_challenge,
        "code_challenge_method": code_challenge_method,
        # server_state is for CSRF verification; client_state is echoed back to the client
        "state": server_state,
        "client_state": state,
    })
    response.set_cookie(
        "oauth_state",
        signed_state,
        httponly=True,
        secure=True,
        samesite="lax",
        max_age=600,  # 10 minutes, matching auth code lifetime
    )
    return response


@router.post("/authorize")
async def authorize_post(
    request: Request,
    action: str = Form(...),
    client_id: str = Form(...),
    redirect_uri: str = Form(...),
    code_challenge: str = Form(...),
    code_challenge_method: str = Form("S256"),
    scope: str = Form("read"),
    state: str = Form(""),
    client_state: str = Form(""),
):
    # Verify CSRF state against the signed cookie
    signed_cookie = request.cookies.get("oauth_state", "")
    state_valid = False
    if signed_cookie and state:
        try:
            expected_state = _state_serializer().loads(signed_cookie, max_age=600)
            state_valid = secrets.compare_digest(expected_state, state)
        except (BadSignature, SignatureExpired):
            state_valid = False

    if not state_valid:
        return JSONResponse({"error": "invalid_state", "error_description": "CSRF state mismatch or missing"}, status_code=400)

    # Validate scope
    try:
        scope = _validate_scope(scope)
    except ValueError as exc:
        return JSONResponse({"error": "invalid_scope", "error_description": str(exc)}, status_code=400)

    if action != "approve":
        # Denied — redirect with error
        sep = "&" if "?" in redirect_uri else "?"
        url = f"{redirect_uri}{sep}error=access_denied"
        if client_state:
            url += f"&state={client_state}"
        return RedirectResponse(url, status_code=302)

    # Multi-user mode: refuse to mint a code without a session. The GET
    # handler already redirects to login, but defend against a stale POST
    # arriving after the session expired.
    session_user_id: int | None = None
    if settings.multi_user_mode:
        try:
            session_user_id = request.session.get("user_id")
        except (AssertionError, AttributeError):
            session_user_id = None
        if session_user_id is None:
            return JSONResponse(
                {"error": "login_required", "error_description": "Session required"},
                status_code=401,
            )

    code = secrets.token_hex(32)

    async with async_session() as session:
        # Bind the OAuth client to its first-authorizing user. RFC 7591
        # dynamic registration is unauthenticated, so we can't bind at
        # registration time — first /authorize wins. Subsequent authorizes
        # for the same client leave `user_id` alone.
        if session_user_id is not None:
            result = await session.execute(
                select(OAuthClient).where(OAuthClient.client_id == client_id)
            )
            client_row = result.scalar_one_or_none()
            if client_row is not None and client_row.user_id is None:
                client_row.user_id = session_user_id

        oauth_code = OAuthCode(
            code_hash=_hash(code),
            client_id=client_id,
            redirect_uri=redirect_uri,
            scope=scope,
            code_challenge=code_challenge,
            code_challenge_method=code_challenge_method,
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
            user_id=session_user_id,
        )
        session.add(oauth_code)
        await session.commit()

    sep = "&" if "?" in redirect_uri else "?"
    url = f"{redirect_uri}{sep}code={code}"
    if client_state:
        url += f"&state={client_state}"
    return RedirectResponse(url, status_code=302)


# --- Token Endpoint ---


@limiter.limit("10/minute")
@router.post("/token")
async def token_endpoint(request: Request):
    form = await request.form()
    grant_type = form.get("grant_type")

    if grant_type == "authorization_code":
        return await _handle_auth_code(form)
    elif grant_type == "refresh_token":
        return await _handle_refresh(form)
    else:
        return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)


async def _handle_auth_code(form):
    code = form.get("code")
    client_id = form.get("client_id")
    client_secret = form.get("client_secret")
    code_verifier = form.get("code_verifier")
    redirect_uri = form.get("redirect_uri")

    if not all([code, client_id, client_secret, code_verifier]):
        return JSONResponse({"error": "invalid_request"}, status_code=400)

    async with async_session() as session:
        # Verify client
        result = await session.execute(
            select(OAuthClient).where(OAuthClient.client_id == client_id)
        )
        client = result.scalar_one_or_none()
        if not client or not secrets.compare_digest(client.client_secret_hash, _hash(client_secret)):
            return JSONResponse({"error": "invalid_client"}, status_code=401)

        # Verify code
        code_hash = _hash(code)
        result = await session.execute(
            select(OAuthCode).where(
                OAuthCode.code_hash == code_hash,
                OAuthCode.client_id == client_id,
                OAuthCode.used == False,
            )
        )
        oauth_code = result.scalar_one_or_none()

        if not oauth_code:
            return JSONResponse({"error": "invalid_grant"}, status_code=400)

        if oauth_code.expires_at < datetime.now(timezone.utc):
            return JSONResponse({"error": "invalid_grant", "error_description": "code expired"}, status_code=400)

        if redirect_uri and oauth_code.redirect_uri != redirect_uri:
            return JSONResponse({"error": "invalid_grant", "error_description": "redirect_uri mismatch"}, status_code=400)

        # Verify PKCE
        expected_challenge = _base64url_sha256(code_verifier)
        if expected_challenge != oauth_code.code_challenge:
            return JSONResponse({"error": "invalid_grant", "error_description": "PKCE verification failed"}, status_code=400)

        # Mark code as used
        oauth_code.used = True

        # Mint tokens. In multi-user mode the issued tokens inherit the
        # `user_id` stamped on the auth code at /authorize time; in single-
        # user mode that value is NULL and tokens stay NULL too.
        access_token = secrets.token_hex(32)
        refresh_token = secrets.token_hex(32)

        session.add(OAuthToken(
            token_hash=_hash(access_token),
            token_type="access",
            client_id=client_id,
            scope=oauth_code.scope,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            user_id=oauth_code.user_id,
        ))
        session.add(OAuthToken(
            token_hash=_hash(refresh_token),
            token_type="refresh",
            client_id=client_id,
            scope=oauth_code.scope,
            expires_at=datetime.now(timezone.utc) + timedelta(days=30),
            user_id=oauth_code.user_id,
        ))
        await session.commit()

    return JSONResponse({
        "access_token": access_token,
        "token_type": "Bearer",
        "expires_in": 3600,
        "refresh_token": refresh_token,
        "scope": oauth_code.scope,
    })


async def _handle_refresh(form):
    refresh_token = form.get("refresh_token")
    client_id = form.get("client_id")
    client_secret = form.get("client_secret")

    if not all([refresh_token, client_id, client_secret]):
        return JSONResponse({"error": "invalid_request"}, status_code=400)

    async with async_session() as session:
        try:
            # Verify client
            result = await session.execute(
                select(OAuthClient).where(OAuthClient.client_id == client_id)
            )
            client = result.scalar_one_or_none()
            if not client or not secrets.compare_digest(client.client_secret_hash, _hash(client_secret)):
                return JSONResponse({"error": "invalid_client"}, status_code=401)

            # Verify refresh token
            token_hash = _hash(refresh_token)
            result = await session.execute(
                select(OAuthToken).where(
                    OAuthToken.token_hash == token_hash,
                    OAuthToken.token_type == "refresh",
                    OAuthToken.client_id == client_id,
                    OAuthToken.revoked == False,
                )
            )
            old_token = result.scalar_one_or_none()

            if not old_token:
                return JSONResponse({"error": "invalid_grant"}, status_code=400)

            if old_token.expires_at < datetime.now(timezone.utc):
                return JSONResponse({"error": "invalid_grant", "error_description": "refresh token expired"}, status_code=400)

            # Mint new token pair FIRST, then revoke old token — all in one transaction
            new_access = secrets.token_hex(32)
            new_refresh = secrets.token_hex(32)

            session.add(OAuthToken(
                token_hash=_hash(new_access),
                token_type="access",
                client_id=client_id,
                scope=old_token.scope,
                expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
                user_id=old_token.user_id,
            ))
            session.add(OAuthToken(
                token_hash=_hash(new_refresh),
                token_type="refresh",
                client_id=client_id,
                scope=old_token.scope,
                expires_at=datetime.now(timezone.utc) + timedelta(days=30),
                user_id=old_token.user_id,
            ))

            # Revoke old refresh token in the same commit
            old_token.revoked = True

            await session.commit()
        except Exception:
            await session.rollback()
            return JSONResponse({"error": "server_error", "error_description": "Token rotation failed"}, status_code=500)

    return JSONResponse({
        "access_token": new_access,
        "token_type": "Bearer",
        "expires_in": 3600,
        "refresh_token": new_refresh,
        "scope": old_token.scope,
    })


# --- Revocation Endpoint ---


@router.post("/revoke")
async def revoke_token(request: Request):
    form = await request.form()
    token = form.get("token")

    if token:
        token_hash = _hash(token)
        async with async_session() as session:
            result = await session.execute(
                select(OAuthToken).where(OAuthToken.token_hash == token_hash)
            )
            oauth_token = result.scalar_one_or_none()
            if oauth_token:
                oauth_token.revoked = True
                await session.commit()

    # RFC 7009: always return 200
    return JSONResponse({})
