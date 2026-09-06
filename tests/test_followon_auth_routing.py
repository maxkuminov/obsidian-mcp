from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from src.auth.session import SESSION_ID_KEY, get_active_session_user
from src.main import RootMCPProxyMiddleware
from src.oauth import routes as oauth_routes

import session_helpers as sh


class _Request:
    def __init__(self, session=None, cookies=None):
        self.session = session or {}
        self.cookies = cookies or {}
        self.url = SimpleNamespace(path="/authorize", query="client_id=test")


@pytest.mark.asyncio
async def test_active_session_resolver_clears_deactivated_user():
    """A deactivated account's *live* session is refused and its cookie cleared.

    The cookie is minted through `session_helpers.sign_in`, not hand-built.
    Written by hand it carried no `sid`, and once #198 landed the resolver
    refused it at the `no_session_id` branch before ever reading the `users`
    row — so the assertion below passed without the deactivation being what
    caused it. The registry row and the `sid` are what make this test reach the
    branch it is named for.
    """
    user = sh.fake_user(user_id=7, session_version=3)
    sid, request, registry = await sh.sign_in(user)
    assert request.session[SESSION_ID_KEY] == sid

    user.is_active = False

    assert await get_active_session_user(request, registry) is None
    assert request.session == {}
    # The proof that the refusal came from the account and not from the cookie
    # shape: the resolver got past the `sid` check and looked the row up.
    assert any("user_sessions" in statement for statement in registry.statements)


@pytest.mark.asyncio
async def test_authorize_get_revalidates_database_backed_session(monkeypatch):
    db = AsyncMock()
    context = AsyncMock()
    context.__aenter__.return_value = db
    monkeypatch.setattr(oauth_routes, "async_session", lambda: context)
    resolver = AsyncMock(return_value=None)
    monkeypatch.setattr(oauth_routes, "get_active_session_user", resolver)
    monkeypatch.setattr(oauth_routes.settings, "multi_user_mode", True)
    request = _Request({"user_id": 7, "session_version": 2})

    response = await oauth_routes.authorize_get(
        request=request,
        response_type="code",
        client_id="client",
        redirect_uri="https://client.example/callback",
        code_challenge="a" * 43,
        code_challenge_method="S256",
        scope="read",
        state="state",
    )

    assert response.status_code == 302
    assert response.headers["location"].startswith("/admin/auth/login?")
    resolver.assert_awaited_once_with(request, db)


@pytest.mark.asyncio
async def test_authorize_post_stale_session_cannot_mint_code(monkeypatch):
    server_state = "server-state"
    signed_state = oauth_routes._state_serializer().dumps(server_state)
    request = _Request(
        {"user_id": 7, "session_version": 2},
        {"oauth_state": signed_state},
    )
    db = AsyncMock()
    context = AsyncMock()
    context.__aenter__.return_value = db
    monkeypatch.setattr(oauth_routes, "async_session", lambda: context)
    resolver = AsyncMock(return_value=None)
    monkeypatch.setattr(oauth_routes, "get_active_session_user", resolver)
    monkeypatch.setattr(oauth_routes.settings, "multi_user_mode", True)

    response = await oauth_routes.authorize_post(
        request=request,
        action="approve",
        client_id="client",
        redirect_uri="https://client.example/callback",
        code_challenge="a" * 43,
        code_challenge_method="S256",
        scope="read",
        state=server_state,
        client_state="client-state",
    )

    assert response.status_code == 401
    assert db.add.call_count == 0
    db.commit.assert_not_awaited()
    resolver.assert_awaited_once_with(request, db)


async def _asgi_response(app, scope):
    messages = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        messages.append(message)

    await app(scope, receive, send)
    start = next(message for message in messages if message["type"] == "http.response.start")
    return start["status"], dict(start["headers"])


def _scope(host=b"allowed.example", origin=None):
    headers = [(b"host", host), (b"authorization", b"Bearer token")]
    if origin:
        headers.append((b"origin", origin))
    return {"type": "http", "method": "POST", "path": "/", "raw_path": b"/", "headers": headers}


@pytest.mark.asyncio
async def test_root_mcp_fallback_preserves_trusted_host_middleware():
    async def endpoint(_scope, _receive, send):
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    app = RootMCPProxyMiddleware(
        TrustedHostMiddleware(endpoint, allowed_hosts=["allowed.example"])
    )
    status, _headers = await _asgi_response(app, _scope(host=b"forged.example"))

    assert status == 400


@pytest.mark.asyncio
async def test_root_mcp_fallback_rewrites_through_cors_stack():
    seen = {}

    async def endpoint(scope, _receive, send):
        seen["path"] = scope["path"]
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    app = RootMCPProxyMiddleware(
        CORSMiddleware(endpoint, allow_origins=["https://client.example"])
    )
    status, headers = await _asgi_response(
        app, _scope(origin=b"https://client.example")
    )

    assert status == 204
    assert seen["path"] == "/mcp/"
    assert headers[b"access-control-allow-origin"] == b"https://client.example"
