import hashlib
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl, quote, urlencode, urlparse, urlunparse

from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy import select

from src.auth.session import get_active_session_user
from src.config import settings
from src.database import async_session
from src.limiter import limiter
from src.models.db import OAuthClient, OAuthCode, OAuthToken

router = APIRouter(tags=["oauth"])
templates = Jinja2Templates(
    directory=os.path.join(os.path.dirname(__file__), "..", "control_panel", "templates")
)

# Valid OAuth scopes. ChatGPT requests ``offline_access`` when the provider
# advertises refresh-token support. It does not change vault permissions; it
# only makes the already-issued refresh token explicit in the grant.
VALID_SCOPES = {"read", "readwrite", "offline_access"}
DEFAULT_CLIENT_SCOPE = "read readwrite offline_access"
TOKEN_ENDPOINT_AUTH_METHODS = {"none", "client_secret_post"}
_PKCE_RE = re.compile(r"^[A-Za-z0-9._~-]{43,128}$")
_PKCE_CHALLENGE_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")


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


def _append_query(uri: str, **params: str) -> str:
    """Append OAuth response parameters without allowing parameter injection."""
    parsed = urlparse(uri)
    query = parse_qsl(parsed.query, keep_blank_values=True)
    query.extend((key, value) for key, value in params.items() if value != "")
    return urlunparse(parsed._replace(query=urlencode(query)))


def _valid_pkce_challenge(challenge: str, method: str) -> bool:
    return method == "S256" and bool(_PKCE_CHALLENGE_RE.fullmatch(challenge))


def _oauth_json(content: dict, status_code: int = 200) -> JSONResponse:
    """OAuth responses containing credentials must never be cached."""
    return JSONResponse(
        content,
        status_code=status_code,
        headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
    )


def _validate_scope(scope: str) -> str:
    parts = set(scope.split())
    invalid = parts - VALID_SCOPES
    if invalid:
        raise ValueError(f"Invalid scopes: {invalid}")
    return " ".join(parts & VALID_SCOPES) or "read"


def _clamp_scope(requested: str, registered: str) -> str:
    """Restrict a requested scope to what the client registered for.

    Both inputs are already validated scope strings. The user can only ever
    be granted the intersection of what they asked for at consent time and
    what the client is registered to hold. `readwrite` implies `read`, so a
    client registered for `readwrite` may still be granted plain `read`.
    """
    requested_parts = set(requested.split())
    registered_parts = set(registered.split())
    if "readwrite" in registered_parts:
        registered_parts.add("read")
    granted = requested_parts & registered_parts
    return " ".join(sorted(granted)) or "read"


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
        "token_endpoint_auth_methods_supported": ["none", "client_secret_post"],
        "scopes_supported": ["read", "readwrite", "offline_access"],
    })


# RFC 9728 protected-resource metadata. MCP clients probe both the bare path
# and the resource-suffixed variant when discovering auth for /mcp.
def _protected_resource_metadata() -> dict:
    base = settings.base_url
    return {
        "resource": f"{base}/mcp",
        "authorization_servers": [base],
        "scopes_supported": ["read", "readwrite", "offline_access"],
        "bearer_methods_supported": ["header"],
    }


@router.get("/.well-known/oauth-protected-resource")
async def oauth_protected_resource():
    return JSONResponse(_protected_resource_metadata())


@router.get("/.well-known/oauth-protected-resource/mcp")
async def oauth_protected_resource_mcp():
    return JSONResponse(_protected_resource_metadata())


# --- Dynamic Client Registration ---


@router.post("/register")
@limiter.limit("3/minute")
async def register_client(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid_client_metadata"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "invalid_client_metadata"}, status_code=400)
    client_name = body.get("client_name", "Unknown Client")
    redirect_uris = body.get("redirect_uris", [])

    if not isinstance(client_name, str) or not client_name.strip() or len(client_name) > 255:
        return JSONResponse({"error": "invalid_client_metadata"}, status_code=400)
    if (
        not isinstance(redirect_uris, list)
        or not redirect_uris
        or len(redirect_uris) > 10
        or any(not isinstance(uri, str) or len(uri) > 2048 for uri in redirect_uris)
    ):
        return JSONResponse({"error": "redirect_uris required"}, status_code=400)
    if len(set(redirect_uris)) != len(redirect_uris):
        return JSONResponse({"error": "invalid_redirect_uri"}, status_code=400)

    # Validate all redirect URIs
    for uri in redirect_uris:
        if not _valid_redirect_uri(uri):
            return JSONResponse(
                {"error": "invalid_redirect_uri", "error_description": f"Redirect URI must use https and contain no fragment: {uri}"},
                status_code=400,
            )

    # DCR clients commonly omit ``scope`` and ask for the desired subset at
    # /authorize. Register the full supported set in that case so consent can
    # offer both read-only and read/write access. The user still chooses the
    # actual grant on the consent screen.
    raw_scope = body.get("scope", DEFAULT_CLIENT_SCOPE)
    if not isinstance(raw_scope, str):
        return JSONResponse({"error": "invalid_scope"}, status_code=400)
    try:
        scope = _validate_scope(raw_scope)
    except ValueError as exc:
        return JSONResponse({"error": "invalid_scope", "error_description": str(exc)}, status_code=400)

    token_endpoint_auth_method = body.get(
        "token_endpoint_auth_method", "client_secret_post"
    )
    if token_endpoint_auth_method not in TOKEN_ENDPOINT_AUTH_METHODS:
        return JSONResponse(
            {
                "error": "invalid_client_metadata",
                "error_description": "Unsupported token_endpoint_auth_method",
            },
            status_code=400,
        )

    client_id = secrets.token_hex(16)
    client_secret = (
        secrets.token_hex(32)
        if token_endpoint_auth_method == "client_secret_post"
        else None
    )

    async with async_session() as session:
        client = OAuthClient(
            client_id=client_id,
            client_secret_hash=_hash(client_secret) if client_secret else None,
            token_endpoint_auth_method=token_endpoint_auth_method,
            client_name=client_name,
            redirect_uris=redirect_uris,
            scope=scope,
        )
        session.add(client)
        await session.commit()

    registration = {
        "client_id": client_id,
        "client_name": client_name,
        "redirect_uris": redirect_uris,
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": token_endpoint_auth_method,
        "scope": scope,
    }
    if client_secret:
        registration["client_secret"] = client_secret
    return JSONResponse(registration, status_code=201)


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
        async with async_session() as session:
            current_user = await get_active_session_user(request, session)
        if current_user is None:
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

    if not _valid_pkce_challenge(code_challenge, code_challenge_method):
        return JSONResponse({"error": "invalid_request", "error_description": "A valid S256 PKCE challenge is required"}, status_code=400)

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

    # The registered scope caps what the user can grant; surface it so the
    # consent screen only offers access levels the client can actually hold.
    client_can_write = "readwrite" in client.scope.split()

    offline_access_requested = "offline_access" in scope.split()

    response = templates.TemplateResponse(request, "authorize.html", {
        "client_name": client.client_name,
        "scope": scope,
        "client_can_write": client_can_write,
        "read_scope": "read offline_access" if offline_access_requested else "read",
        "readwrite_scope": "readwrite offline_access" if offline_access_requested else "readwrite",
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
        secure=settings.base_url.startswith("https://"),
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

    if not _valid_pkce_challenge(code_challenge, code_challenge_method):
        return JSONResponse({"error": "invalid_request", "error_description": "A valid S256 PKCE challenge is required"}, status_code=400)

    # Validate scope
    try:
        scope = _validate_scope(scope)
    except ValueError as exc:
        return JSONResponse({"error": "invalid_scope", "error_description": str(exc)}, status_code=400)

    async with async_session() as session:
        # GET already validates this identity, but the consent form can remain
        # open across a password reset, logout, or account deactivation.
        session_user_id: int | None = None
        if action == "approve" and settings.multi_user_mode:
            current_user = await get_active_session_user(request, session)
            if current_user is None:
                return JSONResponse(
                    {"error": "login_required", "error_description": "Session required"},
                    status_code=401,
                )
            session_user_id = current_user.id

        # Re-validate redirect_uri against the client's registered list. The
        # GET handler does this, but redirect_uri arrives here as an
        # attacker-controllable form field, so a confused-deputy / open
        # redirect is possible unless we re-check before any redirect or code
        # minting. Load the client once and reuse it for the multi-user
        # first-authorizer binding below.
        result = await session.execute(
            select(OAuthClient).where(OAuthClient.client_id == client_id)
        )
        client_row = result.scalar_one_or_none()

        if client_row is None:
            return JSONResponse({"error": "invalid_client"}, status_code=400)

        if redirect_uri not in client_row.redirect_uris:
            return JSONResponse({"error": "invalid_redirect_uri"}, status_code=400)

        # Clamp the consent-form scope to what the client registered for.
        # `scope` arrives as an attacker-controllable form field (the radio
        # buttons are client-side and trivially bypassed), so a client
        # registered for read-only could otherwise mint a readwrite code.
        scope = _clamp_scope(scope, client_row.scope)

        if action != "approve":
            # Denied — redirect with error (redirect_uri now verified)
            url = _append_query(redirect_uri, error="access_denied", state=client_state)
            return RedirectResponse(url, status_code=302)

        code = secrets.token_hex(32)

        # Bind the OAuth client to its first-authorizing user. RFC 7591
        # dynamic registration is unauthenticated, so we can't bind at
        # registration time — first /authorize wins. Subsequent authorizes
        # for the same client leave `user_id` alone.
        if session_user_id is not None and client_row.user_id is None:
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

    url = _append_query(redirect_uri, code=code, state=client_state)
    return RedirectResponse(url, status_code=302)


# --- Token Endpoint ---


@router.post("/token")
@limiter.limit("10/minute")
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

    if not all([code, code_verifier]):
        return JSONResponse({"error": "invalid_request"}, status_code=400)

    async with async_session() as session:
        # Resolve the authorization code first. Some ChatGPT connector builds
        # omit client_id at the public-client token exchange. PKCE still binds
        # the request to the initiating client, and the code tells us which
        # registered client and auth method must be enforced.
        code_hash = _hash(code)
        code_query = select(OAuthCode).where(
            OAuthCode.code_hash == code_hash,
            OAuthCode.used == False,
        ).with_for_update()
        if client_id:
            code_query = code_query.where(OAuthCode.client_id == client_id)
        result = await session.execute(code_query)
        oauth_code = result.scalar_one_or_none()

        if not oauth_code:
            return JSONResponse({"error": "invalid_grant"}, status_code=400)

        result = await session.execute(
            select(OAuthClient).where(OAuthClient.client_id == oauth_code.client_id)
        )
        client = result.scalar_one_or_none()
        if not client:
            return JSONResponse({"error": "invalid_client"}, status_code=401)

        auth_method = getattr(
            client, "token_endpoint_auth_method", "client_secret_post"
        )
        if auth_method == "client_secret_post":
            if not client_secret or not client.client_secret_hash or not secrets.compare_digest(
                client.client_secret_hash, _hash(client_secret)
            ):
                return JSONResponse({"error": "invalid_client"}, status_code=401)
        elif auth_method != "none":
            return JSONResponse({"error": "invalid_client"}, status_code=401)

        client_id = oauth_code.client_id

        if oauth_code.expires_at < datetime.now(timezone.utc):
            return JSONResponse({"error": "invalid_grant", "error_description": "code expired"}, status_code=400)

        if not redirect_uri or oauth_code.redirect_uri != redirect_uri:
            return JSONResponse({"error": "invalid_grant", "error_description": "redirect_uri mismatch"}, status_code=400)

        # Verify PKCE
        if not isinstance(code_verifier, str) or not _PKCE_RE.fullmatch(code_verifier):
            return JSONResponse({"error": "invalid_grant", "error_description": "Invalid PKCE verifier"}, status_code=400)
        expected_challenge = _base64url_sha256(code_verifier)
        if not secrets.compare_digest(expected_challenge, oauth_code.code_challenge):
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

    return _oauth_json({
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

    if not refresh_token:
        return JSONResponse({"error": "invalid_request"}, status_code=400)

    async with async_session() as session:
        try:
            # Resolve the token before authenticating the client so public
            # clients can refresh without a client secret (or, for ChatGPT
            # compatibility, a repeated client_id).
            token_hash = _hash(refresh_token)
            token_query = select(OAuthToken).where(
                OAuthToken.token_hash == token_hash,
                OAuthToken.token_type == "refresh",
                OAuthToken.revoked == False,
            ).with_for_update()
            if client_id:
                token_query = token_query.where(OAuthToken.client_id == client_id)
            result = await session.execute(token_query)
            old_token = result.scalar_one_or_none()

            if not old_token:
                return JSONResponse({"error": "invalid_grant"}, status_code=400)

            result = await session.execute(
                select(OAuthClient).where(OAuthClient.client_id == old_token.client_id)
            )
            client = result.scalar_one_or_none()
            if not client:
                return JSONResponse({"error": "invalid_client"}, status_code=401)

            auth_method = getattr(
                client, "token_endpoint_auth_method", "client_secret_post"
            )
            if auth_method == "client_secret_post":
                if not client_secret or not client.client_secret_hash or not secrets.compare_digest(
                    client.client_secret_hash, _hash(client_secret)
                ):
                    return JSONResponse({"error": "invalid_client"}, status_code=401)
            elif auth_method != "none":
                return JSONResponse({"error": "invalid_client"}, status_code=401)

            client_id = old_token.client_id

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

    return _oauth_json({
        "access_token": new_access,
        "token_type": "Bearer",
        "expires_in": 3600,
        "refresh_token": new_refresh,
        "scope": old_token.scope,
    })


# --- Revocation Endpoint ---


@router.post("/revoke")
@limiter.limit("20/minute")
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
