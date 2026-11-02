"""Native OIDC security boundaries, using real JOSE signatures and protocol clients."""
import logging
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import parse_qs, urlsplit

import httpx2
import pytest
from joserfc import jwt
from joserfc.jwk import RSAKey
from starlette.requests import Request

from src.auth import oidc, routes
from src.config import Settings, settings
from src.limiter import limiter
from src.logging_setup import OIDCCallbackAccessFilter

ISSUER = "https://identity.example.test/realms/home"
CLIENT = "obsidian"
GROUP = "/apps/obsidian"
KEY = RSAKey.generate_key(2048, parameters={"kid": "test-key"})


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    for name, value in dict(oidc_enabled=True, multi_user_mode=True, oidc_issuer=ISSUER,
                            oidc_client_id=CLIENT, oidc_client_secret="test-client-secret",
                            oidc_required_group=GROUP, oidc_subject_user_ids={"immutable-sub": 7},
                            base_url="https://notes.example.test").items():
        monkeypatch.setattr(settings, name, value)
    oidc._transactions.clear()
    oidc._start_times.clear()
    limiter.reset()
    yield
    oidc._transactions.clear()
    oidc._start_times.clear()
    limiter.reset()


def request(path="/admin/auth/oidc/callback", query="", session=None):
    return Request({"type": "http", "method": "GET", "scheme": "https", "path": path,
                    "query_string": query.encode(), "headers": [(b"host", b"notes.example.test")],
                    "client": ("192.0.2.10", 1234), "server": ("notes.example.test", 443),
                    "session": {} if session is None else session})


def claims(tx, **updates):
    result = dict(iss=ISSUER, sub="immutable-sub", aud=CLIENT, exp=int(time.time()) + 300,
                  iat=int(time.time()), nonce=tx.nonce, groups=[GROUP])
    result.update(updates)
    return result


def signed(values, key=KEY):
    return {"id_token": jwt.encode({"alg": "RS256", "kid": "test-key"}, values, key)}


def jwks():
    return {"keys": [KEY.as_dict(private=False)]}


def test_mapping_keeps_existing_numeric_id():
    _, tx = oidc.begin({}, "/authorize?client_id=existing")
    assert oidc.verified_user_id(signed(claims(tx)), jwks(), tx) == 7


@pytest.mark.parametrize("updates", [
    {"iss": "https://other.example.test"}, {"aud": "other"}, {"aud": [CLIENT, "other"]},
    {"azp": "other"}, {"azp": None}, {"nonce": "other"}, {"exp": 0}, {"iat": True},
    {"exp": "9999999999"}, {"sub": "unknown"}, {"sub": ""},
    {"groups": GROUP}, {"groups": [GROUP.upper()]}, {"groups": [GROUP + "-old"]},
    {"groups": [GROUP, 1]}, {"groups": None}, {"groups": [GROUP] * 257},
])
def test_signed_but_unacceptable_claims_refused(updates):
    _, tx = oidc.begin({}, "/admin/")
    with pytest.raises(Exception):
        oidc.verified_user_id(signed(claims(tx, **updates)), jwks(), tx)


@pytest.mark.parametrize("missing", ["iss", "sub", "aud", "exp", "iat", "nonce", "groups"])
def test_missing_claim_refused(missing):
    _, tx = oidc.begin({}, "/admin/")
    value = claims(tx)
    del value[missing]
    with pytest.raises(Exception):
        oidc.verified_user_id(signed(value), jwks(), tx)


def test_untrusted_signature_refused():
    _, tx = oidc.begin({}, "/admin/")
    other = RSAKey.generate_key(2048, parameters={"kid": "test-key"})
    with pytest.raises(Exception):
        oidc.verified_user_id(signed(claims(tx), other), jwks(), tx)


def test_state_is_browser_bound_single_use_even_with_cookie_replay():
    browser = {}
    state, tx = oidc.begin(browser, "/authorize?client_id=existing")
    copied_cookie = dict(browser)
    with pytest.raises(oidc.LoginRefused):
        oidc.consume({}, state)
    assert oidc.consume(browser, state) == tx
    with pytest.raises(oidc.LoginRefused):
        oidc.consume(copied_cookie, state)


def test_expiry_capacity_and_restart(monkeypatch):
    browser = {}
    state, _ = oidc.begin(browser, "/admin/")
    monkeypatch.setattr(oidc, "MAX_TRANSACTIONS", 1)
    with pytest.raises(oidc.LoginRefused, match="capacity"):
        oidc.begin({}, "/admin/")
    assert state in oidc._transactions  # no eviction
    now = time.monotonic()
    monkeypatch.setattr(oidc.time, "monotonic", lambda: now + 301)
    with pytest.raises(oidc.LoginRefused, match="state"):
        oidc.consume(browser, state)
    new_state, _ = oidc.begin(browser, "/admin/")
    oidc._transactions.clear()  # worker restart
    with pytest.raises(oidc.LoginRefused):
        oidc.consume(browser, new_state)


def test_global_start_budget():
    for _ in range(60):
        oidc.begin({}, "/admin/")
    with pytest.raises(oidc.LoginRefused, match="capacity"):
        oidc.begin({}, "/admin/")


@pytest.mark.parametrize("endpoint", ["http://identity.example.test/token", "https://other.test/token",
                                       "https://user@identity.example.test/token", "https://identity.example.test/token#x"])
def test_metadata_endpoint_boundary(endpoint):
    with pytest.raises(oidc.LoginRefused):
        oidc._endpoint(endpoint)


@pytest.mark.asyncio
async def test_real_protocol_client_pkce_and_token_verification(monkeypatch):
    browser = {}
    state, tx = oidc.begin(browser, "/authorize?client_id=existing")
    meta = dict(issuer=ISSUER, authorization_endpoint=ISSUER + "/protocol/openid-connect/auth",
                token_endpoint=ISSUER + "/protocol/openid-connect/token", jwks_uri=ISSUER + "/protocol/openid-connect/certs")
    seen = []

    async def handle(req):
        seen.append(req)
        if req.url.path.endswith("openid-configuration"):
            return httpx2.Response(200, json=meta)
        if req.url.path.endswith("token"):
            form = parse_qs(req.content.decode())
            assert form["code_verifier"] == [tx.verifier]
            assert form["redirect_uri"] == ["https://notes.example.test/admin/auth/oidc/callback"]
            assert req.headers["authorization"].startswith("Basic ")
            return httpx2.Response(200, json={**signed(claims(tx)), "access_token": "transient-token", "token_type": "Bearer"})
        assert "authorization" not in req.headers  # never forward access token to JWKS
        return httpx2.Response(200, json=jwks())

    monkeypatch.setattr(oidc, "BoundedTransport", lambda: httpx2.MockTransport(handle))
    url = await oidc.authorization_url(state, tx)
    params = parse_qs(urlsplit(url).query)
    assert params["code_challenge_method"] == ["S256"]
    assert params["scope"] == ["openid profile"]
    assert params["nonce"] == [tx.nonce]
    assert params["state"] == [state]
    assert params["response_type"] == ["code"]
    assert "code_verifier" not in params
    assert await oidc.exchange("test-code", oidc.consume(browser, state)) == 7


@pytest.mark.asyncio
async def test_callback_uses_existing_session_guard_and_safe_next(monkeypatch):
    browser = {}
    state, _ = oidc.begin(browser, "/authorize?client_id=old&scope=readwrite")
    req = request(query="state=" + state + "&code=test", session=browser)
    monkeypatch.setattr(oidc, "exchange", AsyncMock(return_value=7))
    user = SimpleNamespace(id=7, is_active=True, session_version=12, username="existing")
    db = AsyncMock()
    db.execute.return_value = MagicMock(scalar_one_or_none=lambda: user)
    mint = AsyncMock(return_value="new-app-session")
    monkeypatch.setattr(routes, "start_session", mint)
    monkeypatch.setattr(routes, "warm_user_vault_cache", AsyncMock())
    response = await routes.oidc_callback(req, db)
    assert response.status_code == 302
    assert response.headers["location"] == "/authorize?client_id=old&scope=readwrite"
    mint.assert_awaited_once_with(req, db, 7, expected_session_version=12)
    assert not any(k in browser for k in ("id_token", "access_token", "refresh_token"))


@pytest.mark.asyncio
@pytest.mark.parametrize("active,minted", [(False, "unused"), (True, None)])
async def test_inactive_and_session_race_refuse(monkeypatch, active, minted):
    browser = {}
    state, _ = oidc.begin(browser, "/admin/")
    monkeypatch.setattr(oidc, "exchange", AsyncMock(return_value=7))
    db = AsyncMock()
    db.execute.return_value = MagicMock(scalar_one_or_none=lambda: SimpleNamespace(
        id=7, is_active=active, session_version=3, username="existing"))
    mint = AsyncMock(return_value=minted)
    monkeypatch.setattr(routes, "start_session", mint)
    monkeypatch.setattr(routes, "warm_user_vault_cache", AsyncMock())
    response = await routes.oidc_callback(request(query="state="+state+"&code=test", session=browser), db)
    assert response.status_code == 401
    assert mint.await_count == int(active)


def test_callback_access_log_redacted():
    record = logging.LogRecord("uvicorn.access", 20, "", 0, "%s %s %s %s %s",
                               ("ip", "GET", oidc.CALLBACK_PATH + "?code=secret&state=secret", "1.1", 302), None)
    assert OIDCCallbackAccessFilter().filter(record)
    assert "secret" not in record.getMessage()


def test_settings_fail_closed():
    base = dict(_env_file=None, secret_key="test-only-strong-secret-not-a-placeholder", multi_user_mode=True, oidc_enabled=True,
                oidc_issuer=ISSUER, oidc_client_id=CLIENT, oidc_client_secret="test",
                oidc_required_group=GROUP, oidc_subject_user_ids={"sub": 7},
                base_url="https://notes.example.test")
    assert Settings(**base).oidc_enabled
    for override in ({"multi_user_mode": False}, {"oidc_issuer": "http://bad.test"},
                     {"oidc_subject_user_ids": {"sub": True}}, {"oidc_subject_user_ids": {}},
                     {"oidc_enabled": False, "local_login_enabled": False},
                     {"base_url": "https://notes.example.test/path"}, {"oidc_client_secret": ""}):
        with pytest.raises(ValueError):
            Settings(**(base | override))


@pytest.mark.asyncio
async def test_password_post_and_bootstrap_are_closed(monkeypatch):
    from fastapi import HTTPException
    monkeypatch.setattr(settings, "local_login_enabled", False)
    req = request("/admin/auth/login")
    with pytest.raises(HTTPException) as error:
        await routes.login_submit(req, username="x", password="x", next="/admin/", session=AsyncMock())
    assert error.value.status_code == 403
    for call in (routes.register_form(req, AsyncMock()), routes.register_submit(
            req, username="x", password="x", password_confirm="x", vault_path="/tmp", session=AsyncMock())):
        with pytest.raises(HTTPException) as error:
            await call
        assert error.value.status_code == 404
    page = routes._render_login(req)
    assert b"Sign in with SSO" in page.body
    assert b'action="/admin/auth/login"' not in page.body


@pytest.mark.asyncio
async def test_anonymous_backend_never_receives_admin_sentinel():
    from fastapi import Depends, FastAPI
    from httpx import ASGITransport, AsyncClient
    from starlette.middleware.sessions import SessionMiddleware
    from src.auth.session import require_user
    from src.control_panel.routes import require_user_panel
    from src.database import get_session

    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="test-browser-secret")

    async def db():
        yield AsyncMock()
    app.dependency_overrides[get_session] = db

    @app.get("/api/private")
    async def api(user=Depends(require_user)):
        return {"admin": user.is_admin}

    @app.get("/admin/private")
    async def panel(user=Depends(require_user_panel)):
        return {"admin": user.is_admin}

    async with AsyncClient(transport=ASGITransport(app), base_url="https://notes.example.test") as http:
        assert (await http.get("/api/private")).status_code == 401
        response = await http.get("/admin/private")
        assert response.status_code == 302
        assert response.headers["location"].startswith("/admin/auth/login?")


@pytest.mark.asyncio
async def test_metadata_mismatch_and_redirect_fail(monkeypatch):
    for status, data in [(200, {"issuer": "https://wrong.test"}), (302, {})]:
        async def handle(req):
            return httpx2.Response(status, json=data, headers={"location": "https://wrong.test"})
        monkeypatch.setattr(oidc, "BoundedTransport", lambda: httpx2.MockTransport(handle))
        state, tx = oidc.begin({}, "/admin/")
        with pytest.raises(Exception):
            await oidc.authorization_url(state, tx)


@pytest.mark.asyncio
@pytest.mark.parametrize("encoded,oversized", [(True, False), (False, True)])
async def test_provider_body_limits(encoded, oversized):
    class Body(httpx2.AsyncByteStream):
        async def __aiter__(self):
            yield b"x" * (oidc.MAX_RESPONSE_BYTES + 1 if oversized else 10)

    async def handle(req):
        return httpx2.Response(200, stream=Body(), headers={"Content-Encoding": "gzip" if encoded else "identity"})
    transport = oidc.BoundedTransport()
    await transport.inner.aclose()
    transport.inner = httpx2.MockTransport(handle)
    async with httpx2.AsyncClient(transport=transport) as http:
        with pytest.raises(oidc.LoginRefused):
            await http.get(ISSUER)
