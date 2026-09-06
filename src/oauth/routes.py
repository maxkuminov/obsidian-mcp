import hashlib
import logging
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
from sqlalchemy import update as sa_update

from src.auth.session import get_active_session_user
from src.config import settings
from src.database import async_session
from src.limiter import limiter
from src.models.db import OAuthClient, OAuthCode, OAuthToken
from src.oauth.grants import (
    lock_grant,
    lock_user_bootstrap,
    new_grant_id,
    revoke_grant_family,
)
from src.oauth.scope import VALID_SCOPES, clamp_scope, has_vault_scope
# Aliased on import so `authorize_get` can keep its long-standing local name
# `client_can_write` (the consent template reads that key) without shadowing
# the helper.
from src.oauth.scope import client_can_write as _client_can_write
from src.services import security_events

# No module logger, deliberately. Everything this module records is an
# authentication outcome a caller can drive on demand, so it goes through
# `security_events.emit` and its allowance check; a bare logger sitting here
# would be an unbounded flood channel beside the bounded one, and the next
# person to add a refusal would reach for it. `logging` stays imported for the
# level constants the emitter takes.

router = APIRouter(tags=["oauth"])
templates = Jinja2Templates(
    directory=os.path.join(os.path.dirname(__file__), "..", "control_panel", "templates")
)

# Valid OAuth scopes live in `src/oauth/scope.py` alongside the helpers that
# interpret them, so the panel and the ASGI auth middleware can import them
# without reaching into this module (issue #67). ChatGPT requests
# ``offline_access`` when the provider advertises refresh-token support; it does
# not change vault permissions, it only makes the already-issued refresh token
# explicit in the grant.
DEFAULT_CLIENT_SCOPE = "read readwrite offline_access"
TOKEN_ENDPOINT_AUTH_METHODS = {"none", "client_secret_post"}
_PKCE_RE = re.compile(r"^[A-Za-z0-9._~-]{43,128}$")
_PKCE_CHALLENGE_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


# --- Security-event helpers ------------------------------------------------
#
# This module is the primary authentication surface for third-party AI clients,
# and until #191 it contained no logger at all: an issuance, a refusal and a
# revocation left the same trace, which is none. Every outcome below now emits
# exactly one record through `src/services/security_events.py` — one allowance
# check, an allow-listed field set, and never a secret.
#
# Three rules the helpers exist to keep in one place rather than at forty call
# sites:
#
# * **Provenance is in the name.** `client_id` may hold only a value read from
#   an `oauth_clients` row; the caller's form field goes in
#   `client_id_submitted` and nowhere else (D15). Each helper takes both and
#   drops the submitted one the moment a row has resolved.
# * **The subject is the trusted client address** for every refusal, because a
#   refusal resolved no credential. Only the records about an authenticated
#   principal (an issuance, a consent, a revocation) key on `user_id`.
# * **Nothing secret has a field to ride in.** No `code`, no `code_verifier`,
#   no `client_secret`, no `access_token`, no `refresh_token`, no token hash —
#   not in a field, not in a message. `client_id` is a public identifier and is
#   logged deliberately.
#
# `request` is optional throughout: `_handle_auth_code` and `_handle_refresh`
# are called directly by a large body of tests with the form alone, and a
# missing request means an absent `client_ip`, never a raise.


def _route(request) -> str | None:
    """`request.url.path`, or `None` when there is no request to ask.

    Defensive for the same reason the helpers take an optional request: these
    handlers are called directly, with a form and a stand-in, by a large body of
    tests, and a logging helper may never be the thing that raises.
    """
    try:
        return request.url.path
    except Exception:  # noqa: BLE001 - a logging helper may not raise
        return None


def _token_refused(
    request,
    reason: str,
    *,
    client_id: str | None = None,
    submitted_client_id=None,
    user_id: int | None = None,
    grant_id: str | None = None,
) -> None:
    """One `oauth_token_refused`, reason `<rfc_code>.<sub_reason>`.

    `user_id` and `grant_id` are absent on the early failures that have
    resolved neither — a missing field means the path did not have it, which is
    the honest answer.
    """
    security_events.emit(
        "oauth_token_refused",
        subject=security_events.subject_for(request=request),
        reason=reason,
        client_id=client_id,
        client_id_submitted=None if client_id else submitted_client_id,
        user_id=user_id,
        grant_id=grant_id,
        client_ip=security_events.client_ip(request),
    )


def _authorize_refused(
    request,
    reason: str,
    *,
    client_id: str | None = None,
    submitted_client_id=None,
    user_id: int | None = None,
) -> None:
    """One `oauth_authorize_refused`. The rendered refusal is unchanged."""
    security_events.emit(
        "oauth_authorize_refused",
        subject=security_events.subject_for(request=request),
        reason=reason,
        client_id=client_id,
        client_id_submitted=None if client_id else submitted_client_id,
        user_id=user_id,
        client_ip=security_events.client_ip(request),
    )


def _registration_refused(request, reason: str) -> None:
    """One `oauth_client_registration_refused`.

    `/register` is unauthenticated (RFC 7591), so the record carries no
    identity at all and is bounded on the address.
    """
    security_events.emit(
        "oauth_client_registration_refused",
        subject=security_events.subject_for(request=request),
        reason=reason,
        client_ip=security_events.client_ip(request),
    )


def _revoke_noop(request, reason: str, submitted_client_id=None) -> None:
    """One `oauth_revoke_noop`. RFC 7009 §2.2 forbids the *response* saying it.

    The endpoint answers 200 with an empty body whether the token was unknown,
    foreign or already dead, so the log is the only place the distinction
    exists.
    """
    security_events.emit(
        "oauth_revoke_noop",
        level=logging.INFO,
        subject=security_events.subject_for(request=request),
        reason=reason,
        client_id_submitted=submitted_client_id,
        client_ip=security_events.client_ip(request),
    )


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


# The clamp now lives in `src/oauth/scope.py` so the control panel can apply
# the same cap without importing this module. Kept under its historical private
# name because it is referenced throughout this file and by the #21 regression
# tests; there is exactly one implementation behind both names.
_clamp_scope = clamp_scope


def _client_authenticated(client, client_secret) -> bool:
    """Does `client_secret` satisfy this client's registered auth method?

    One definition for all three endpoints that authenticate a client
    (`/token` for both grant types, and `/revoke`). A public PKCE client
    registers `token_endpoint_auth_method = "none"` and carries no secret, so
    it authenticates trivially — for those, possession of the token *is* the
    credential, which is the model RFC 6749 §2.1 describes and the reason the
    revocation endpoint can still act on a public client's request.

    Any method other than the two we register is a refusal, not a fallback.
    """
    auth_method = getattr(client, "token_endpoint_auth_method", "client_secret_post")
    if auth_method == "client_secret_post":
        return bool(
            client_secret
            and client.client_secret_hash
            and secrets.compare_digest(client.client_secret_hash, _hash(client_secret))
        )
    return auth_method == "none"


def _cross_user_client_error(
    request=None,
    *,
    client_id: str | None = None,
    actor_user_id: int | None = None,
    owner_user_id: int | None = None,
) -> JSONResponse:
    """The refusal both consent paths give for someone else's client.

    The record is emitted **here**, at the one helper both `:approve` sites
    return, so neither can grow a refusal that logs nothing. It is the clearest
    case for D19's pair: `actor_user_id` is the session user who asked, and
    `user_id` is the account that owns the client they asked about — one
    `user_id` would have left an operator unable to tell which was which.
    """
    security_events.emit(
        "oauth_cross_user_client_refused",
        subject=security_events.subject_for(user_id=actor_user_id, request=request),
        client_id=client_id,
        actor_user_id=actor_user_id,
        user_id=owner_user_id,
        route=_route(request),
        client_ip=security_events.client_ip(request),
    )
    return JSONResponse(
        {
            "error": "access_denied",
            "error_description": (
                "This OAuth client is registered to a different user. "
                "Register the connector again from your own account "
                "instead of reusing an existing client_id."
            ),
        },
        status_code=403,
    )


def _client_belongs_to_another_user(client_row, session_user_id: int | None) -> bool:
    """Is this client owned by a *different* user than the one consenting?

    Both `None` cases mean "no conflict", and they mean it for different
    reasons: an unbound client (`user_id IS NULL`) is about to be claimed by
    its first authorizer, and a `None` session identity only happens in
    single-user mode, where there are no other users to conflict with.
    """
    return (
        session_user_id is not None
        and client_row.user_id is not None
        and client_row.user_id != session_user_id
    )


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
        _registration_refused(request, "invalid_client_metadata")
        return JSONResponse({"error": "invalid_client_metadata"}, status_code=400)
    if not isinstance(body, dict):
        _registration_refused(request, "invalid_client_metadata")
        return JSONResponse({"error": "invalid_client_metadata"}, status_code=400)
    client_name = body.get("client_name", "Unknown Client")
    redirect_uris = body.get("redirect_uris", [])

    if not isinstance(client_name, str) or not client_name.strip() or len(client_name) > 255:
        _registration_refused(request, "invalid_client_metadata")
        return JSONResponse({"error": "invalid_client_metadata"}, status_code=400)
    if (
        not isinstance(redirect_uris, list)
        or not redirect_uris
        or len(redirect_uris) > 10
        or any(not isinstance(uri, str) or len(uri) > 2048 for uri in redirect_uris)
    ):
        _registration_refused(request, "invalid_redirect_uri")
        return JSONResponse({"error": "redirect_uris required"}, status_code=400)
    if len(set(redirect_uris)) != len(redirect_uris):
        _registration_refused(request, "invalid_redirect_uri")
        return JSONResponse({"error": "invalid_redirect_uri"}, status_code=400)

    # Validate all redirect URIs
    for uri in redirect_uris:
        if not _valid_redirect_uri(uri):
            _registration_refused(request, "invalid_redirect_uri")
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
        _registration_refused(request, "invalid_scope")
        return JSONResponse({"error": "invalid_scope"}, status_code=400)
    try:
        scope = _validate_scope(raw_scope)
    except ValueError as exc:
        _registration_refused(request, "invalid_scope")
        return JSONResponse({"error": "invalid_scope", "error_description": str(exc)}, status_code=400)

    # A registration naming neither `read` nor `readwrite` grants nothing --
    # `offline_access` says the grant may carry a refresh token, not that it
    # may read a note. Such a client could be registered and could reach the
    # consent screen, and every downstream clamp now (correctly) resolves it
    # to an empty grant, so the whole flow would dead-end at the token
    # endpoint. Refusing here says so at the only point where the developer
    # registering the client is still in the loop.
    if not has_vault_scope(scope):
        _registration_refused(request, "invalid_scope")
        return JSONResponse(
            {
                "error": "invalid_scope",
                "error_description": (
                    "scope must include 'read' or 'readwrite'; "
                    "'offline_access' alone grants no access"
                ),
            },
            status_code=400,
        )

    token_endpoint_auth_method = body.get(
        "token_endpoint_auth_method", "client_secret_post"
    )
    if token_endpoint_auth_method not in TOKEN_ENDPOINT_AUTH_METHODS:
        _registration_refused(request, "unsupported_auth_method")
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

    # After the commit (D17). `client_id` is server-generated and public; the
    # name is the client's own text and is marked as such. The `client_secret`
    # this route just minted appears in **no** field and in no message — the
    # canary test captures the value the response carries and asserts its
    # absence from every record.
    security_events.emit(
        "oauth_client_registered",
        level=logging.INFO,
        subject=security_events.subject_for(request=request),
        client_id=client_id,
        client_name_submitted=client_name,
        scope=scope,
        count=len(redirect_uris),
        client_ip=security_events.client_ip(request),
    )

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
        _authorize_refused(
            request, "unsupported_response_type", submitted_client_id=client_id
        )
        return JSONResponse({"error": "unsupported_response_type"}, status_code=400)

    if not _valid_pkce_challenge(code_challenge, code_challenge_method):
        # The challenge is a public value, but the record carries neither it nor
        # the verifier that will answer it — only that the check refused.
        _authorize_refused(request, "pkce_invalid", submitted_client_id=client_id)
        return JSONResponse({"error": "invalid_request", "error_description": "A valid S256 PKCE challenge is required"}, status_code=400)

    # Validate scope
    try:
        scope = _validate_scope(scope)
    except ValueError as exc:
        _authorize_refused(request, "invalid_scope", submitted_client_id=client_id)
        return JSONResponse({"error": "invalid_scope", "error_description": str(exc)}, status_code=400)

    async with async_session() as session:
        result = await session.execute(
            select(OAuthClient).where(OAuthClient.client_id == client_id)
        )
        client = result.scalar_one_or_none()

    if client is None:
        # No row resolved, so the caller's value may appear only under the
        # `_submitted` name (D15).
        _authorize_refused(request, "unknown_client", submitted_client_id=client_id)
        return JSONResponse({"error": "invalid_client"}, status_code=400)

    if redirect_uri not in client.redirect_uris:
        _authorize_refused(
            request, "invalid_redirect_uri", client_id=client.client_id
        )
        return JSONResponse({"error": "invalid_redirect_uri"}, status_code=400)

    # Generate server-side CSRF state and bind it to a signed cookie
    server_state = secrets.token_urlsafe(16)
    signed_state = _state_serializer().dumps(server_state)

    # The registered scope caps what the user can grant; surface it so the
    # consent screen only offers access levels the client can actually hold.
    client_can_write = _client_can_write(client.scope)

    scope_parts = scope.split()
    offline_access_requested = "offline_access" in scope_parts
    # What the client asked for, straight from the (validated) query param —
    # *display only*. It is deliberately NOT gated on `client_can_write`: the
    # consent screen tells the user what was requested, and a client asking
    # for more than it is registered for is exactly the mismatch worth
    # showing. `write_unavailable` is that case.
    #
    # It must never drive the preselected radio. The checked radio is the
    # value the form submits, so binding it to a client-controlled query
    # param would let one unchanged Approve click grant readwrite to any
    # self-registered client (`/register` is unauthenticated) — see #63.
    # "Read only" stays preselected unconditionally in the template; write
    # requires a deliberate click.
    requested_write = "readwrite" in scope_parts
    write_unavailable = requested_write and not client_can_write

    response = templates.TemplateResponse(request, "authorize.html", {
        "client_name": client.client_name,
        "scope": scope,
        "client_can_write": client_can_write,
        "requested_write": requested_write,
        "write_unavailable": write_unavailable,
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
        _authorize_refused(request, "state_mismatch", submitted_client_id=client_id)
        return JSONResponse({"error": "invalid_state", "error_description": "CSRF state mismatch or missing"}, status_code=400)

    if not _valid_pkce_challenge(code_challenge, code_challenge_method):
        _authorize_refused(request, "pkce_invalid", submitted_client_id=client_id)
        return JSONResponse({"error": "invalid_request", "error_description": "A valid S256 PKCE challenge is required"}, status_code=400)

    # Validate scope
    try:
        scope = _validate_scope(scope)
    except ValueError as exc:
        _authorize_refused(request, "invalid_scope", submitted_client_id=client_id)
        return JSONResponse({"error": "invalid_scope", "error_description": str(exc)}, status_code=400)

    async with async_session() as session:
        # GET already validates this identity, but the consent form can remain
        # open across a password reset, logout, or account deactivation.
        session_user_id: int | None = None
        if action == "approve" and settings.multi_user_mode:
            current_user = await get_active_session_user(request, session)
            if current_user is None:
                _authorize_refused(
                    request, "session_required", submitted_client_id=client_id
                )
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
            _authorize_refused(
                request,
                "unknown_client",
                submitted_client_id=client_id,
                user_id=session_user_id,
            )
            return JSONResponse({"error": "invalid_client"}, status_code=400)

        if redirect_uri not in client_row.redirect_uris:
            _authorize_refused(
                request,
                "invalid_redirect_uri",
                client_id=client_row.client_id,
                user_id=session_user_id,
            )
            return JSONResponse({"error": "invalid_redirect_uri"}, status_code=400)

        # The lookup above filtered on `client_id`, so this value is now a
        # value read from a row rather than the caller's form field — the
        # unsuffixed `client_id` field may hold nothing else (D15).
        resolved_client_id = client_row.client_id

        # Clamp the consent-form scope to what the client registered for.
        # `scope` arrives as an attacker-controllable form field (the radio
        # buttons are client-side and trivially bypassed), so a client
        # registered for read-only could otherwise mint a readwrite code.
        scope = _clamp_scope(scope, client_row.scope)

        if action != "approve":
            # A deny carried no identity at all before #191, so the session user
            # is resolved *for the record* — best-effort and guarded, because a
            # logging read must never turn a deny into a 500 and must never
            # change where the browser is sent.
            denied_user_id: int | None = None
            if settings.multi_user_mode:
                try:
                    denying_user = await get_active_session_user(request, session)
                    denied_user_id = None if denying_user is None else denying_user.id
                except Exception:  # noqa: BLE001 - the deny is decided already
                    denied_user_id = None
            security_events.emit(
                "oauth_consent_denied",
                level=logging.INFO,
                subject=security_events.subject_for(
                    user_id=denied_user_id, request=request
                ),
                client_id=resolved_client_id,
                user_id=denied_user_id,
                client_ip=security_events.client_ip(request),
            )
            # Denied — redirect with error (redirect_uri now verified)
            url = _append_query(redirect_uri, error="access_denied", state=client_state)
            return RedirectResponse(url, status_code=302)

        # Refuse cross-user reuse of a client somebody else already owns
        # (issue #68). A client binds to its *first* authorizing user below and
        # never rebinds, so without this a second user's approval mints live
        # tokens under a client they do not own: `oauth_page` filters clients by
        # `OAuthClient.user_id`, so their own grant is invisible and unrevokable
        # in their panel, while the owner's "Delete this client and revoke all
        # its tokens" cascades through it and silently kills their session.
        #
        # Fail closed at the source rather than unioning the panel listing: an
        # unlistable live grant and a delete button that reaches into another
        # user's grants are both consequences of letting the reuse happen.
        # Single-user mode never reaches this — `session_user_id` stays None and
        # `client_row.user_id` stays NULL.
        # An empty clamp means the registration grants no vault access at all
        # (e.g. `scope="offline_access"`), so there is nothing to consent to.
        # `clamp_scope` used to answer `read` here, handing such a client the
        # whole vault read-only — a permission its registration never named.
        # Refuse instead of minting a code for a grant that means nothing.
        if not scope:
            _authorize_refused(
                request,
                "scope_clamped_empty",
                client_id=client_row.client_id,
                user_id=session_user_id,
            )
            return JSONResponse(
                {
                    "error": "invalid_scope",
                    "error_description": (
                        "This client is not registered for any vault access. "
                        "Register it with 'read' or 'readwrite'."
                    ),
                },
                status_code=400,
            )

        if _client_belongs_to_another_user(client_row, session_user_id):
            return _cross_user_client_error(
                request,
                client_id=client_row.client_id,
                actor_user_id=session_user_id,
                owner_user_id=client_row.user_id,
            )

        code = secrets.token_hex(32)

        # Bind the OAuth client to its first-authorizing user. RFC 7591
        # dynamic registration is unauthenticated, so we can't bind at
        # registration time — first /authorize wins. Subsequent authorizes
        # for the same client leave `user_id` alone.
        #
        # Conditional, because `client_row.user_id is None` was read from this
        # transaction's snapshot: two users consenting to the same unbound
        # client at once both saw NULL, and an unconditional ORM assignment let
        # the second one overwrite the first one's claim. `WHERE user_id IS
        # NULL ... RETURNING` makes the database arbitrate — under READ
        # COMMITTED the loser blocks on the row lock, re-evaluates the
        # predicate against the winner's committed row, and matches nothing.
        if session_user_id is not None and client_row.user_id is None:
            claimed = (
                await session.execute(
                    sa_update(OAuthClient)
                    .where(
                        OAuthClient.client_id == client_id,
                        OAuthClient.user_id.is_(None),
                    )
                    .values(user_id=session_user_id)
                    .returning(OAuthClient.client_id)
                )
            ).scalar_one_or_none()
            if claimed is None:
                # Somebody else got there first. Re-read in a fresh statement
                # (a new snapshot, so the winner's commit is visible) and
                # refuse unless the winner happens to be this same user, which
                # is the ordinary case of one person opening two tabs.
                owner = (
                    await session.execute(
                        select(OAuthClient.user_id).where(
                            OAuthClient.client_id == client_id
                        )
                    )
                ).scalar_one_or_none()
                if owner != session_user_id:
                    return _cross_user_client_error(
                        request,
                        client_id=client_row.client_id,
                        actor_user_id=session_user_id,
                        owner_user_id=owner,
                    )

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

    # After the code row commits (D17), and before the redirect is built. The
    # `code` this consent just minted is a bearer credential and appears in no
    # field and no message; the canary test captures it out of the `Location`
    # header and asserts its absence.
    security_events.emit(
        "oauth_consent_granted",
        level=logging.INFO,
        subject=security_events.subject_for(user_id=session_user_id, request=request),
        client_id=resolved_client_id,
        user_id=session_user_id,
        scope=scope,
        client_ip=security_events.client_ip(request),
    )

    url = _append_query(redirect_uri, code=code, state=client_state)
    return RedirectResponse(url, status_code=302)


# --- Token Endpoint ---


@router.post("/token")
@limiter.limit("10/minute")
async def token_endpoint(request: Request):
    form = await request.form()
    grant_type = form.get("grant_type")

    if grant_type == "authorization_code":
        return await _handle_auth_code(form, request)
    elif grant_type == "refresh_token":
        return await _handle_refresh(form, request)
    else:
        _token_refused(
            request,
            "unsupported_grant_type.grant_type_not_supported",
            submitted_client_id=form.get("client_id"),
        )
        return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)


async def _handle_auth_code(form, request=None):
    code = form.get("code")
    client_id = form.get("client_id")
    client_secret = form.get("client_secret")
    code_verifier = form.get("code_verifier")
    redirect_uri = form.get("redirect_uri")

    # The caller's `client_id` until a row resolves. Every refusal below that
    # has not yet read `oauth_clients` reports it as `client_id_submitted`.
    submitted_client_id = client_id

    if not all([code, code_verifier]):
        _token_refused(
            request,
            "invalid_request.missing_parameter",
            submitted_client_id=submitted_client_id,
        )
        return JSONResponse({"error": "invalid_request"}, status_code=400)

    async with async_session() as session:
        # Serialize against the single-user -> multi-user bootstrap before
        # anything is read or written. Its claim is
        # `UPDATE ... WHERE user_id IS NULL`, whose snapshot is taken when the
        # statement starts, so a mint committing afterwards would insert a pair
        # the claim can no longer see and those tokens would belong to nobody.
        # Taken before any per-grant lock, which is the fixed order both token
        # handlers use — see `src/oauth/grants.py`.
        await lock_user_bootstrap(session)

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
            # No code row, so no client and no owner resolved: only the
            # submitted identifier may appear, and `user_id`/`grant_id` are
            # absent because this path has neither.
            _token_refused(
                request,
                "invalid_grant.unknown_code",
                submitted_client_id=submitted_client_id,
            )
            return JSONResponse({"error": "invalid_grant"}, status_code=400)

        result = await session.execute(
            select(OAuthClient).where(OAuthClient.client_id == oauth_code.client_id)
        )
        client = result.scalar_one_or_none()
        if not client:
            _token_refused(
                request,
                "invalid_client.unknown_client",
                submitted_client_id=submitted_client_id,
                user_id=oauth_code.user_id,
            )
            return JSONResponse({"error": "invalid_client"}, status_code=401)

        if not _client_authenticated(client, client_secret):
            # The presented `client_secret` is never recorded, in any form.
            _token_refused(
                request,
                "invalid_client.authentication_failed",
                client_id=client.client_id,
                user_id=oauth_code.user_id,
            )
            return JSONResponse({"error": "invalid_client"}, status_code=401)

        client_id = oauth_code.client_id

        if oauth_code.expires_at < datetime.now(timezone.utc):
            _token_refused(
                request,
                "invalid_grant.code_expired",
                client_id=client_id,
                user_id=oauth_code.user_id,
            )
            return JSONResponse({"error": "invalid_grant", "error_description": "code expired"}, status_code=400)

        if not redirect_uri or oauth_code.redirect_uri != redirect_uri:
            # The URI itself is not recorded: the reason says which check
            # refused, and the allow-list has no field a URL could ride in.
            _token_refused(
                request,
                "invalid_grant.redirect_uri_mismatch",
                client_id=client_id,
                user_id=oauth_code.user_id,
            )
            return JSONResponse({"error": "invalid_grant", "error_description": "redirect_uri mismatch"}, status_code=400)

        # Verify PKCE
        if not isinstance(code_verifier, str) or not _PKCE_RE.fullmatch(code_verifier):
            _token_refused(
                request,
                "invalid_grant.pkce_verifier_invalid",
                client_id=client_id,
                user_id=oauth_code.user_id,
            )
            return JSONResponse({"error": "invalid_grant", "error_description": "Invalid PKCE verifier"}, status_code=400)
        expected_challenge = _base64url_sha256(code_verifier)
        if not secrets.compare_digest(expected_challenge, oauth_code.code_challenge):
            # Neither the verifier nor the challenge is recorded — the verifier
            # is a bearer secret, and a failed exchange is exactly when one
            # would be most tempting to log.
            _token_refused(
                request,
                "invalid_grant.pkce_verification_failed",
                client_id=client_id,
                user_id=oauth_code.user_id,
            )
            return JSONResponse({"error": "invalid_grant", "error_description": "PKCE verification failed"}, status_code=400)

        # In multi-user mode every token must have an owner. A code stamped
        # with a NULL `user_id` predates the flag flip (or escaped the
        # bootstrap's claim), and minting from it produces a credential the
        # ownership checks cannot reason about — `_assert_oauth_token_owner`,
        # the panel's per-user filters and the vault-root lookup all key off
        # `user_id`. Refuse rather than create one.
        if settings.multi_user_mode and oauth_code.user_id is None:
            _token_refused(
                request, "invalid_grant.code_ownerless", client_id=client_id
            )
            return JSONResponse(
                {
                    "error": "invalid_grant",
                    "error_description": "Authorization code has no owner; re-authorize.",
                },
                status_code=400,
            )

        # The client must still belong to the user this code was minted for
        # (issue #68). `authorize_post` refuses a client another user owns, but
        # it claims an *unbound* client in the same transaction as the code —
        # so two users consenting to the same unbound client at the same moment
        # both get a code and only one claim wins. Re-checking here means the
        # loser's code cannot be exchanged for tokens under a client they do not
        # own. A code minted in single-user mode carries a NULL `user_id` and is
        # unaffected, including on a database whose clients were later claimed
        # by the multi-user bootstrap.
        if _client_belongs_to_another_user(client, oauth_code.user_id):
            _token_refused(
                request,
                "invalid_grant.cross_user_client",
                client_id=client_id,
                user_id=oauth_code.user_id,
            )
            return JSONResponse({"error": "invalid_grant"}, status_code=400)

        # Last clamp before anything is persisted (issue #67). `authorize_post`
        # already clamped what it wrote onto the code, so this is normally a
        # no-op — but it is the only thing standing between a code minted under
        # one registration and a token minted under a narrower one, and the
        # cost is nothing next to a write grant nobody registered for.
        #
        # It runs *before* the code is marked used: an empty clamp means the
        # registration grants no vault access, which is not something a retry
        # can fix, and burning the code would only make the failure harder to
        # read. `clamp_scope` answering `read` here is exactly the hole this
        # closes — a client registered `offline_access` alone would have been
        # handed a read token over the entire vault.
        granted_scope = _clamp_scope(oauth_code.scope, client.scope)
        if not granted_scope:
            _token_refused(
                request,
                "invalid_scope.no_vault_scope",
                client_id=client_id,
                user_id=oauth_code.user_id,
            )
            return JSONResponse(
                {
                    "error": "invalid_scope",
                    "error_description": (
                        "This client is not registered for any vault access."
                    ),
                },
                status_code=400,
            )

        # Mark code as used
        oauth_code.used = True

        # Mint tokens. In multi-user mode the issued tokens inherit the
        # `user_id` stamped on the auth code at /authorize time; in single-
        # user mode that value is NULL and tokens stay NULL too.
        access_token = secrets.token_hex(32)
        refresh_token = secrets.token_hex(32)

        # One consent event, one grant family (issue #64). The authorization
        # code is consumed into exactly one grant — it is single-use and this
        # transaction just marked it used — so minting the id here, rather than
        # at /authorize, keeps `oauth_codes` unchanged and still gives both
        # tokens the same family. Every later rotation inherits it, which is
        # what lets the panel revoke or downgrade the pair as one unit instead
        # of a row whose sibling immediately undoes the change.
        grant_id = new_grant_id()

        session.add(OAuthToken(
            token_hash=_hash(access_token),
            token_type="access",
            client_id=client_id,
            scope=granted_scope,
            grant_id=grant_id,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            user_id=oauth_code.user_id,
        ))
        session.add(OAuthToken(
            token_hash=_hash(refresh_token),
            token_type="refresh",
            client_id=client_id,
            scope=granted_scope,
            grant_id=grant_id,
            expires_at=datetime.now(timezone.utc) + timedelta(days=30),
            user_id=oauth_code.user_id,
        ))
        await session.commit()
        issued_user_id = oauth_code.user_id

    # After the mint's commit (D17), so a commit that fails leaves no record
    # claiming a live token exists. Neither token value appears — only the
    # public `client_id`, the owner, the grant family and the granted scope.
    security_events.emit(
        "oauth_token_issued",
        level=logging.INFO,
        subject=security_events.subject_for(user_id=issued_user_id, request=request),
        reason="authorization_code",
        client_id=client_id,
        user_id=issued_user_id,
        grant_id=grant_id,
        scope=granted_scope,
        client_ip=security_events.client_ip(request),
    )

    return _oauth_json({
        "access_token": access_token,
        "token_type": "Bearer",
        "expires_in": 3600,
        "refresh_token": refresh_token,
        "scope": granted_scope,
    })


async def _handle_refresh(form, request=None):
    refresh_token = form.get("refresh_token")
    client_id = form.get("client_id")
    client_secret = form.get("client_secret")

    # Two provenances, kept apart deliberately (D15). `submitted_client_id` is
    # whatever the caller typed and may appear only under the `_submitted`
    # name; `resolved_client_id` and `resolved_grant_id` are set from rows and
    # are the only values the unsuffixed fields may carry — which matters most
    # in the rotation-failure handler below, where `client_id` may still hold
    # the caller's form value.
    submitted_client_id = client_id
    resolved_client_id: str | None = None
    resolved_grant_id: str | None = None
    resolved_user_id: int | None = None

    if not refresh_token:
        _token_refused(
            request,
            "invalid_request.missing_parameter",
            submitted_client_id=submitted_client_id,
        )
        return JSONResponse({"error": "invalid_request"}, status_code=400)

    async with async_session() as session:
        try:
            # Resolve the token before authenticating the client so public
            # clients can refresh without a client secret (or, for ChatGPT
            # compatibility, a repeated client_id).
            token_hash = _hash(refresh_token)

            # Bootstrap lock before the grant lock — the fixed order both token
            # handlers use, and the reason there is no cycle with the panel
            # (which takes only the grant lock). See `src/oauth/grants.py`.
            await lock_user_bootstrap(session)

            # Take the grant-family lock *before* any row in the family
            # is read or written. Rotation inserts two brand-new rows, and a
            # concurrent panel revocation cannot see rows that did not exist
            # when its UPDATE took its snapshot — so without this the operator
            # revokes every row there is and the client keeps the pair it
            # rotated into a moment later. Locking the family, not the row,
            # is what closes that: see `src/oauth/grants.py`.
            #
            # The lookup that finds the family filters on the token hash and
            # type **alone**. Both of the predicates it does not carry are
            # load-bearing:
            #
            # * no `revoked` filter — a revoked row still names its family,
            #   and the locked re-read below is what decides whether this
            #   refresh is allowed;
            # * no caller-supplied `client_id` filter — folding the caller's
            #   claimed identity into the lookup makes a *replayed* token
            #   presented with somebody else's (or a garbage) `client_id` look
            #   unknown, so the live family would survive the very replay that
            #   proves the token leaked. Identity is checked against the row,
            #   on the rotation path below, where a mismatch is a refusal.
            #
            # Ordering matters more than precision here: both sides take this
            # one lock before any row lock, so the acquisition order is total
            # and cannot deadlock.
            grant_query = select(OAuthToken.grant_id).where(
                OAuthToken.token_hash == token_hash,
                OAuthToken.token_type == "refresh",
            )
            grant_ids = (await session.execute(grant_query)).scalars().all()
            if not grant_ids:
                # Nothing resolved at all: no client row, no grant, no owner.
                # The record says so by carrying none of them (D15) — the
                # presented token never appears in any form.
                _token_refused(
                    request,
                    "invalid_grant.unknown_token",
                    submitted_client_id=submitted_client_id,
                )
                return JSONResponse({"error": "invalid_grant"}, status_code=400)
            grant_id = grant_ids[0]
            resolved_grant_id = grant_id
            await lock_grant(session, grant_id)

            # Re-read under the lock. The statement takes a fresh snapshot, so
            # a rotation or revocation that committed while this transaction
            # waited for the lock is visible — this row, not anything read
            # before the lock, is authoritative for both decisions below.
            #
            # `populate_existing` is not decoration. A `SELECT ... FOR UPDATE`
            # whose row is already in the session's identity map returns the
            # *loaded* object with its old attribute values; SQLAlchemy will
            # not overwrite them without being told to. The reuse decision
            # reads `revoked` off this object in Python, so a stale attribute
            # would mean deciding on a pre-lock snapshot — precisely what the
            # lock exists to prevent. (The family lookup above deliberately
            # selects the `grant_id` column rather than the entity, so it does
            # not populate the identity map in the first place.)
            token_query = (
                select(OAuthToken)
                .where(
                    OAuthToken.token_hash == token_hash,
                    OAuthToken.token_type == "refresh",
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            locked = (await session.execute(token_query)).scalars().all()
            old_token = locked[0] if locked else None

            if old_token is None:
                # The row was deleted while we waited (`cleanup_expired_tokens`
                # retires long-dead rows). Nothing left says it was ever
                # rotated, so this is a not-found refusal, not reuse.
                _token_refused(
                    request,
                    "invalid_grant.token_vanished",
                    submitted_client_id=submitted_client_id,
                    grant_id=grant_id,
                )
                return JSONResponse({"error": "invalid_grant"}, status_code=400)

            if old_token.revoked:
                # Refresh-token reuse (issue #182, ASVS V10.4.5). The row
                # exists and is revoked: the caller is replaying a refresh
                # token that was already rotated away (or revoked outright).
                # The flag is read off the locked row itself rather than
                # inferred from an empty result, so no other predicate can
                # ever be mistaken for it.
                #
                # Rotation alone is only half the requirement. A replayed
                # refresh token is evidence the token leaked — the thief and
                # the legitimate client cannot both hold the current one — and
                # RFC 6819 §5.2.1.1 / OAuth 2.1 answer that by killing the
                # whole family. Without this, the thief who redeems first keeps
                # a live, identically-scoped pair (up to `readwrite` over the
                # vault) for the 30-day sliding window while the legitimate
                # client sees `invalid_grant` and quietly re-authorizes.
                #
                # Race-safe because the grant lock is already held: nothing can
                # rotate a new pair into this family between the select above
                # and this UPDATE, so "every live token" cannot be stale.
                # `revoke_grant_family` re-takes the same transaction-scoped
                # advisory lock, which is re-entrant.
                #
                # The refusal carries the same error code, status, headers and
                # body as the not-found refusal above, deliberately: the caller
                # must not learn whether it named a live family, whether
                # anything was revoked, or that reuse detection exists at all.
                # (Timing is not covered — this path does strictly more work
                # and is measurably slower. Accepted residual, recorded in
                # `docs/architecture/oauth-and-grants.md`.)
                #
                # Every database call below is therefore guarded, including the
                # rollbacks: the response is decided *here*, by this branch,
                # and must not depend on whether the session survived. An
                # unguarded failure would escape to the outer handler and
                # answer 500 on the reuse path only — which is the disclosure
                # the constant response exists to prevent.
                #
                # The identifiers are read before the write so the record does
                # not depend on the row surviving the commit.
                reused_client_id = old_token.client_id
                reused_user_id = old_token.user_id
                revoked_count = 0
                failure: str | None = None
                try:
                    revoked_count = await revoke_grant_family(session, grant_id)
                    if revoked_count:
                        await session.commit()
                    else:
                        # Already fully revoked: nothing was flipped, so there
                        # is nothing to commit.
                        await session.rollback()
                except Exception as exc:
                    # Only the exception's class name is ever recorded. A
                    # SQLAlchemy error renders the failing statement *and its
                    # bound parameters* in `str(exc)`, and one of those
                    # parameters is the token hash — so neither the text nor
                    # `exc_info` may reach the log.
                    failure = type(exc).__name__
                    revoked_count = 0
                    try:
                        await session.rollback()
                    except Exception as rollback_exc:
                        failure = f"{failure}+{type(rollback_exc).__name__}"

                # Through `security_events.emit` since #191, not a bare
                # `logger.warning`: a replayed refresh token is caller-driven
                # and repeatable, so the alarm has to pass the same allowance
                # check as every other caller-triggerable record. The
                # identifiers are structured fields now rather than message
                # text — #190 made `extra` actually reach the sink, which is
                # exactly what the old "put them in the message" workaround
                # existed to route around. None of them is a secret; no token
                # value or hash is ever among them.
                if failure is not None:
                    security_events.emit(
                        "oauth_refresh_reuse_revocation_failed",
                        level=logging.ERROR,
                        subject=security_events.subject_for(
                            user_id=reused_user_id, request=request
                        ),
                        client_id=reused_client_id,
                        grant_id=grant_id,
                        user_id=reused_user_id,
                        client_ip=security_events.client_ip(request),
                        # The class name only, never `str(exc)`: a SQLAlchemy
                        # error renders the failing statement *and its bound
                        # parameters*, one of which is the token hash. For the
                        # same reason there is no `exc_info` here.
                        error_type=failure,
                    )
                elif revoked_count:
                    # One record, on the path that actually killed live tokens.
                    # A family that was already fully revoked (an operator
                    # revocation the client has not noticed yet, or a second
                    # replay after the first one closed it) is a no-op with
                    # nothing new to report, and the not-found refusals are not
                    # reuse at all.
                    #
                    # The record is written after the commit, so a crash in
                    # between loses the alarm while keeping the revocation.
                    # Accepted: the safe half is the one that persists, and an
                    # outbox for one WARNING is not worth its own failure mode.
                    security_events.emit(
                        "oauth_refresh_reuse_detected",
                        subject=security_events.subject_for(
                            user_id=reused_user_id, request=request
                        ),
                        client_id=reused_client_id,
                        grant_id=grant_id,
                        user_id=reused_user_id,
                        revoked_tokens=revoked_count,
                        client_ip=security_events.client_ip(request),
                    )
                else:
                    # Revoked, but nothing live was left to kill: an operator
                    # revocation the client has not noticed, or a second replay
                    # after the first one closed the family. Not the reuse
                    # alarm — that one means *this* request killed something —
                    # but still exactly one record for exactly one outcome.
                    _token_refused(
                        request,
                        "invalid_grant.refresh_token_revoked",
                        client_id=reused_client_id,
                        user_id=reused_user_id,
                        grant_id=grant_id,
                    )
                return JSONResponse({"error": "invalid_grant"}, status_code=400)

            # The caller's claimed identity is checked against the row it
            # named, now that the row is known to be live. A mismatch is the
            # same refusal it has always been, and revokes nothing: a *live*
            # token presented with the wrong `client_id` is a misconfigured or
            # confused client, not evidence that the token leaked.
            if client_id and old_token.client_id != client_id:
                # The row's own identity resolved, so it is the one that may
                # appear; what the caller claimed does not.
                _token_refused(
                    request,
                    "invalid_grant.client_id_mismatch",
                    client_id=old_token.client_id,
                    user_id=old_token.user_id,
                    grant_id=grant_id,
                )
                return JSONResponse({"error": "invalid_grant"}, status_code=400)

            result = await session.execute(
                select(OAuthClient).where(OAuthClient.client_id == old_token.client_id)
            )
            client = result.scalar_one_or_none()
            if not client:
                _token_refused(
                    request,
                    "invalid_client.unknown_client",
                    submitted_client_id=submitted_client_id,
                    user_id=old_token.user_id,
                    grant_id=grant_id,
                )
                return JSONResponse({"error": "invalid_client"}, status_code=401)

            if not _client_authenticated(client, client_secret):
                _token_refused(
                    request,
                    "invalid_client.authentication_failed",
                    client_id=client.client_id,
                    user_id=old_token.user_id,
                    grant_id=grant_id,
                )
                return JSONResponse({"error": "invalid_client"}, status_code=401)

            client_id = old_token.client_id
            resolved_client_id = old_token.client_id
            resolved_user_id = old_token.user_id

            if old_token.expires_at < datetime.now(timezone.utc):
                _token_refused(
                    request,
                    "invalid_grant.refresh_token_expired",
                    client_id=client_id,
                    user_id=old_token.user_id,
                    grant_id=grant_id,
                )
                return JSONResponse({"error": "invalid_grant", "error_description": "refresh token expired"}, status_code=400)

            # In multi-user mode a token with no owner cannot be rotated: the
            # replacement would inherit the NULL and stay outside every
            # ownership check. Such a row can only be a pre-flag-flip leftover
            # the bootstrap did not claim.
            if settings.multi_user_mode and old_token.user_id is None:
                _token_refused(
                    request,
                    "invalid_grant.token_ownerless",
                    client_id=client_id,
                    grant_id=grant_id,
                )
                return JSONResponse(
                    {
                        "error": "invalid_grant",
                        "error_description": "Token has no owner; re-authorize.",
                    },
                    status_code=400,
                )

            # The grant's owner must still be the client's owner (issue #68).
            # `authorize_post` and `_handle_auth_code` both refuse to create
            # such a pairing now, but a legacy row — or one created by the
            # first-claim race before it was closed — would otherwise rotate
            # forever, keeping a live cross-user grant alive indefinitely and
            # invisible in either user's panel.
            if _client_belongs_to_another_user(client, old_token.user_id):
                _token_refused(
                    request,
                    "invalid_grant.cross_user_client",
                    client_id=client_id,
                    user_id=old_token.user_id,
                    grant_id=grant_id,
                )
                return JSONResponse({"error": "invalid_grant"}, status_code=400)

            # Re-clamp against the client's *current* registration (issue #67).
            # Rotation used to copy `old_token.scope` verbatim, so any scope a
            # token had acquired above what its client was registered for
            # survived every rotation indefinitely — the panel could grant
            # `readwrite` to a client registered `read` and nothing ever took it
            # back. Clamping here also means narrowing a client's registration
            # takes effect on the next refresh instead of never.
            granted_scope = _clamp_scope(old_token.scope, client.scope)
            if not granted_scope:
                _token_refused(
                    request,
                    "invalid_scope.no_vault_scope",
                    client_id=client_id,
                    user_id=old_token.user_id,
                    grant_id=grant_id,
                )
                # The registration grants no vault access any more, so there is
                # nothing to rotate into. Nothing is committed, so the old
                # refresh token is left exactly as it was — the thing to fix is
                # the client's registration, not this token.
                return JSONResponse(
                    {
                        "error": "invalid_scope",
                        "error_description": (
                            "This client is not registered for any vault access."
                        ),
                    },
                    status_code=400,
                )

            # Mint new token pair FIRST, then revoke old token — all in one transaction
            new_access = secrets.token_hex(32)
            new_refresh = secrets.token_hex(32)

            session.add(OAuthToken(
                token_hash=_hash(new_access),
                token_type="access",
                client_id=client_id,
                scope=granted_scope,
                # Rotation stays inside the family it rotated (issue #64), so a
                # revocation or downgrade applied to the grant still covers the
                # pair the client is about to start using.
                grant_id=old_token.grant_id,
                expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
                user_id=old_token.user_id,
            ))
            session.add(OAuthToken(
                token_hash=_hash(new_refresh),
                token_type="refresh",
                client_id=client_id,
                scope=granted_scope,
                grant_id=old_token.grant_id,
                expires_at=datetime.now(timezone.utc) + timedelta(days=30),
                user_id=old_token.user_id,
            ))

            # Revoke old refresh token in the same commit
            old_token.revoked = True

            await session.commit()
            rotated_user_id = old_token.user_id
            rotated_grant_id = old_token.grant_id
        except Exception as exc:
            # The traceback used to be discarded behind the 500, so a rotation
            # failing in production left an operator with a status code and
            # nothing else. Emitted **before** the rollback, which is the
            # cheapest step and the one that may not be skipped if the rollback
            # itself fails; only the identifiers that resolved from rows appear.
            security_events.emit(
                "oauth_token_rotation_failed",
                level=logging.ERROR,
                exc_info=exc,
                subject=security_events.subject_for(
                    user_id=resolved_user_id, request=request
                ),
                client_id=resolved_client_id,
                grant_id=resolved_grant_id,
                client_ip=security_events.client_ip(request),
            )
            await session.rollback()
            return JSONResponse({"error": "server_error", "error_description": "Token rotation failed"}, status_code=500)

    # After the rotation's commit (D17) and outside the guarded block, so a
    # record can never claim a rotation the transaction did not keep. The new
    # pair is in hand here and appears nowhere in the record.
    security_events.emit(
        "oauth_token_refreshed",
        level=logging.INFO,
        subject=security_events.subject_for(user_id=rotated_user_id, request=request),
        client_id=client_id,
        user_id=rotated_user_id,
        grant_id=rotated_grant_id,
        scope=granted_scope,
        client_ip=security_events.client_ip(request),
    )

    return _oauth_json({
        "access_token": new_access,
        "token_type": "Bearer",
        "expires_in": 3600,
        "refresh_token": new_refresh,
        # The clamped scope, not the old token's — RFC 6749 §5.1 requires the
        # response to state the granted scope whenever it differs from what was
        # asked for, and a client told `readwrite` while holding `read` would
        # keep retrying writes that 403.
        "scope": granted_scope,
    })


# --- Revocation Endpoint ---


@router.post("/revoke")
@limiter.limit("20/minute")
async def revoke_token(request: Request):
    form = await request.form()
    token = form.get("token")
    client_id = form.get("client_id")
    client_secret = form.get("client_secret")

    if not token:
        _revoke_noop(request, "missing_token", client_id)

    if token:
        token_hash = _hash(token)
        async with async_session() as session:
            result = await session.execute(
                select(OAuthToken).where(OAuthToken.token_hash == token_hash)
            )
            oauth_token = result.scalar_one_or_none()
            if oauth_token is None:
                _revoke_noop(request, "unknown_token", client_id)
            if oauth_token:
                # RFC 7009 §2.1: the client authenticates. This endpoint used
                # to do neither authentication nor an ownership check, so any
                # holder of any token value — a leaked one, one belonging to a
                # different client — could revoke a whole grant. Widening
                # revocation to the family (below) made that worse, not better.
                result = await session.execute(
                    select(OAuthClient).where(
                        OAuthClient.client_id == oauth_token.client_id
                    )
                )
                client = result.scalar_one_or_none()

                # §2.2: "If the server responds with HTTP 200 for a token that
                # is not valid *for the requesting client*, that is not an
                # error" — an unknown, foreign or unauthenticated token is
                # indistinguishable from an already-revoked one. So a caller
                # who does not identify himself as this token's client is
                # answered 200 and nothing happens, rather than being told
                # whose token it is.
                #
                # `client_id` must be **present and exactly equal**. Treating
                # its absence as a match was a hole, not a tolerance: a public
                # client authenticates trivially (no secret to check), so
                # "omit client_id" was a universal bypass — anyone holding A's
                # token could end A's whole grant without naming a client at
                # all, and for a public client without proving anything. Unlike
                # `/token`, where PKCE still binds the request to the
                # initiating client and the code identifies it, nothing else
                # here identifies the caller. There is no ChatGPT-compatibility
                # cost either: revocation is optional for a client, and RFC
                # 7009 §2.1 requires it to authenticate when it does revoke.
                if client is None:
                    _revoke_noop(request, "unknown_client", client_id)
                    return JSONResponse({})
                if not client_id or client_id != oauth_token.client_id:
                    # Deliberately does **not** name the token's real owner or
                    # client: §2.2 hides that from the response, and a log an
                    # operator shares is not a reason to hand it back.
                    _revoke_noop(request, "client_mismatch", client_id)
                    return JSONResponse({})
                if not _client_authenticated(client, client_secret):
                    # The one case that is a real error rather than a no-op:
                    # the right client was named and failed to authenticate.
                    security_events.emit(
                        "oauth_revoke_refused",
                        subject=security_events.subject_for(request=request),
                        reason="client_auth_failed",
                        client_id=oauth_token.client_id,
                        client_ip=security_events.client_ip(request),
                    )
                    return JSONResponse({"error": "invalid_client"}, status_code=401)

                # Revoke the whole grant family, not just the row presented.
                #
                # RFC 7009 §2.1 explicitly permits this ("the authorization
                # server ... MAY revoke the respective refresh token as well"),
                # and it is the only behaviour that means anything here: a
                # client presenting its access token and being told "revoked"
                # while its refresh token quietly mints a replacement pair is
                # the same near-no-op the panel had (issue #64). Presenting the
                # refresh token was already the working case; now both are.
                #
                # The family cannot span clients or users (see
                # `src/oauth/grants.py`), so an authenticated client revoking
                # its own token reaches nothing that is not its own.
                revoked_count = await revoke_grant_family(
                    session, oauth_token.grant_id
                )
                await session.commit()

                # Emitted **here**, by the HTTP caller, after its own commit —
                # never inside `revoke_grant_family`, which has no request, no
                # client address and no session user, and does not commit, so a
                # record written there would be a claim about a transaction
                # that may still roll back (D10).
                #
                # No `actor_user_id`: `/revoke` authenticates as the *client*,
                # not as a person, so there is no acting human to name. The
                # panel's revoke handler emits the same event *with* one.
                security_events.emit(
                    "oauth_grant_revoked",
                    level=logging.INFO,
                    subject=security_events.subject_for(
                        user_id=oauth_token.user_id, request=request
                    ),
                    client_id=oauth_token.client_id,
                    user_id=oauth_token.user_id,
                    grant_id=oauth_token.grant_id,
                    count=revoked_count,
                    client_ip=security_events.client_ip(request),
                    route=_route(request),
                )

    # RFC 7009: always return 200
    return JSONResponse({})
