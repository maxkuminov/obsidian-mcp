"""Native OIDC security boundaries, using real JOSE signatures and protocol clients.

#416 slice B: the provider profile (RS256/PS256/ES256 ∩ discovery, basic or
post client auth, optional group, RFC 9207 `iss`, the back-channel trust
anchor) and `exchange()` returning a `VerifiedIdentity` that never judges
email. Settings validation lives in `tests/test_oidc_settings.py`.
"""
import base64
import json
import logging
import ssl
import time
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import parse_qs, urlsplit

import httpx2
import pytest
from joserfc import jwt
from joserfc.jwk import ECKey, OctKey, RSAKey
from starlette.requests import Request

from src.auth import oidc, routes
from src.config import settings
from src.limiter import limiter
from src.logging_setup import OIDCCallbackAccessFilter

ISSUER = "https://identity.example.test/realms/home"
CLIENT = "obsidian"
SECRET = "test-client-secret"
GROUP = "/apps/obsidian"
KEY = RSAKey.generate_key(2048, parameters={"kid": "test-key"})
EC_KEY = ECKey.generate_key("P-256", parameters={"kid": "ec-key"})
HMAC_KEY = OctKey.generate_key(256, parameters={"kid": "oct-key"})
KEYS = {"RS256": KEY, "PS256": KEY, "ES256": EC_KEY}


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    for name, value in dict(oidc_enabled=True, multi_user_mode=True, oidc_issuer=ISSUER,
                            oidc_client_id=CLIENT, oidc_client_secret=SECRET,
                            oidc_required_group=GROUP, oidc_token_auth_method="client_secret_basic",
                            oidc_extra_scopes="",
                            base_url="https://notes.example.test").items():
        monkeypatch.setattr(settings, name, value)
    monkeypatch.setattr(settings, "_oidc_ssl_context", None)
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


def signed(values, key=KEY, alg="RS256"):
    return {"id_token": jwt.encode({"alg": alg, "kid": key.kid}, values, key, algorithms=[alg])}


def unsigned(values):
    def part(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()
    return {"id_token": part({"alg": "none", "kid": "test-key"}) + "." + part(values) + "."}


def jwks(*keys):
    return {"keys": [k.as_dict(private=False) for k in (keys or (KEY,))]}


def identity(tx, algorithms=("RS256",), **updates):
    return oidc.verified_identity(signed(claims(tx, **updates)), jwks(), tx, algorithms)


def discovery(**extra):
    return dict(issuer=ISSUER, authorization_endpoint=ISSUER + "/protocol/openid-connect/auth",
                token_endpoint=ISSUER + "/protocol/openid-connect/token",
                jwks_uri=ISSUER + "/protocol/openid-connect/certs", **extra)


class Provider:
    """A fake provider for the real Authlib client: discovery, token, JWKS."""

    def __init__(self, monkeypatch, tx=None, meta=None, token=None, keys=None):
        self.tx, self.meta, self.token, self.keys = tx, meta or discovery(), token, keys or jwks()
        self.seen: list = []
        monkeypatch.setattr(oidc, "BoundedTransport", lambda: httpx2.MockTransport(self.handle))

    def token_requests(self):
        return [r for r in self.seen if r.url.path.endswith("/token")]

    async def handle(self, req):
        self.seen.append(req)
        if req.url.path.endswith("openid-configuration"):
            return httpx2.Response(200, json=self.meta)
        if req.url.path.endswith("/token"):
            body = self.token if self.token is not None else signed(claims(self.tx))
            return httpx2.Response(200, json={**body, "access_token": "transient-token",
                                              "token_type": "Bearer"})
        assert "authorization" not in req.headers  # never forward access token to JWKS
        return httpx2.Response(200, json=self.keys)


# ── claims → VerifiedIdentity ───────────────────────────────────────────────


def test_identity_carries_claims_not_a_user_id():
    _, tx = oidc.begin({}, "/authorize?client_id=existing")
    result = identity(tx, email="max@example.com", email_verified=True)
    assert result == oidc.VerifiedIdentity(ISSUER, "immutable-sub", "max@example.com", True)
    assert "immutable-sub" not in repr(result) and "max@" not in repr(result)


def test_subject_lookup_is_gone():
    """An unmapped subject is a valid identity here; resolution is the callback's job."""
    _, tx = oidc.begin({}, "/admin/")
    assert identity(tx, sub="never-mapped").sub == "never-mapped"
    assert not hasattr(oidc, "verified_user_id")


@pytest.mark.parametrize("updates,email,verified", [
    ({}, None, False),
    ({"email": 42}, None, False),
    ({"email": ["max@example.com"]}, None, False),
    ({"email": "a" * 290 + "@example.com"}, None, False),
    ({"email": "a@b@example.com", "email_verified": "true"}, "a@b@example.com", False),
    ({"email": "Max@Example.com", "email_verified": "true"}, "Max@Example.com", False),
    ({"email": "max@example.com", "email_verified": 1}, "max@example.com", False),
    ({"email": "max@example.com", "email_verified": False}, "max@example.com", False),
    ({"email": "max@example.com", "email_verified": True}, "max@example.com", True),
    ({"email": "x" * 242 + "@example.com"}, "x" * 242 + "@example.com", False),  # 254 chars
    ({"email": "x" * 243 + "@example.com"}, None, False),  # 255 chars
])
def test_email_never_refuses(updates, email, verified):
    _, tx = oidc.begin({}, "/admin/")
    result = identity(tx, **updates)
    assert (result.email, result.email_verified) == (email, verified)


@pytest.mark.parametrize("updates", [
    {"iss": "https://other.example.test"}, {"aud": "other"}, {"aud": [CLIENT, "other"]},
    {"azp": "other"}, {"azp": None}, {"nonce": "other"}, {"exp": 0}, {"iat": True},
    {"exp": "9999999999"}, {"sub": ""}, {"sub": 5}, {"sub": "s" * 256},
])
def test_signed_but_unacceptable_claims_refused_as_token(updates):
    _, tx = oidc.begin({}, "/admin/")
    with pytest.raises(oidc.LoginRefused) as error:
        identity(tx, **updates)
    assert error.value.reason == "token"


def test_subject_at_the_bound_is_accepted():
    _, tx = oidc.begin({}, "/admin/")
    assert identity(tx, sub="s" * 255).sub == "s" * 255


@pytest.mark.parametrize("updates", [
    {"groups": GROUP}, {"groups": [GROUP.upper()]}, {"groups": [GROUP + "-old"]},
    {"groups": [GROUP, 1]}, {"groups": None}, {"groups": [GROUP] * 257}, {"groups": ["x" * 513, GROUP]},
])
def test_required_group_is_exact(updates):
    _, tx = oidc.begin({}, "/admin/")
    with pytest.raises(oidc.LoginRefused) as error:
        identity(tx, **updates)
    assert error.value.reason == "entitlement"


@pytest.mark.parametrize("groups", ["<absent>", None, "scalar", [], [1, 2]])
def test_group_is_optional(monkeypatch, groups):
    monkeypatch.setattr(settings, "oidc_required_group", "")
    _, tx = oidc.begin({}, "/admin/")
    value = claims(tx, groups=groups)
    if groups == "<absent>":
        del value["groups"]
    assert oidc.verified_identity(signed(value), jwks(), tx).sub == "immutable-sub"


@pytest.mark.parametrize("missing", ["iss", "sub", "aud", "exp", "iat", "nonce"])
def test_missing_claim_refused(missing):
    _, tx = oidc.begin({}, "/admin/")
    value = claims(tx)
    del value[missing]
    with pytest.raises(oidc.LoginRefused, match="token"):
        oidc.verified_identity(signed(value), jwks(), tx)


def test_missing_groups_refused_when_required():
    _, tx = oidc.begin({}, "/admin/")
    value = claims(tx)
    del value["groups"]
    with pytest.raises(oidc.LoginRefused, match="entitlement"):
        oidc.verified_identity(signed(value), jwks(), tx)


def test_untrusted_signature_refused():
    _, tx = oidc.begin({}, "/admin/")
    other = RSAKey.generate_key(2048, parameters={"kid": "test-key"})
    with pytest.raises(oidc.LoginRefused, match="token"):
        oidc.verified_identity(signed(claims(tx), other), jwks(), tx)


# ── algorithms (D5) ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("alg", ["RS256", "PS256", "ES256"])
def test_each_algorithm_accepted_when_advertised(alg):
    _, tx = oidc.begin({}, "/admin/")
    token = signed(claims(tx), KEYS[alg], alg)
    assert oidc.verified_identity(token, jwks(KEYS[alg]), tx, (alg,)).sub == "immutable-sub"


@pytest.mark.parametrize("alg,advertised", [
    ("PS256", ("RS256",)), ("ES256", ("RS256",)), ("RS256", ("ES256",)), ("ES256", ("RS256", "PS256")),
])
def test_algorithm_refused_when_not_advertised(alg, advertised):
    _, tx = oidc.begin({}, "/admin/")
    token = signed(claims(tx), KEYS[alg], alg)
    with pytest.raises(oidc.LoginRefused, match="token"):
        oidc.verified_identity(token, jwks(KEYS[alg]), tx, advertised)


def test_symmetric_and_unsigned_tokens_refused_whatever_is_advertised():
    _, tx = oidc.begin({}, "/admin/")
    hs = {"id_token": jwt.encode({"alg": "HS256", "kid": "oct-key"}, claims(tx), HMAC_KEY)}
    symmetric_jwks = {"keys": [HMAC_KEY.as_dict(private=True)]}
    for token, keys in ((hs, symmetric_jwks), (unsigned(claims(tx)), jwks())):
        for advertised in (("RS256", "PS256", "ES256"), ("HS256", "none")):
            with pytest.raises(oidc.LoginRefused, match="token"):
                oidc.verified_identity(token, keys, tx, advertised)


@pytest.mark.asyncio
@pytest.mark.parametrize("alg,advertised,ok", [
    ("RS256", None, True),             # absent → RS256 (OIDC Discovery default)
    ("PS256", None, False),
    ("ES256", ["ES256"], True),
    ("PS256", ["RS256", "PS256"], True),
    ("PS256", ["RS256"], False),
    ("RS256", ["HS256", "none"], False),  # nothing allowed advertised
])
async def test_exchange_intersects_discovery_algorithms(monkeypatch, alg, advertised, ok):
    browser = {}
    state, tx = oidc.begin(browser, "/admin/")
    meta = discovery() if advertised is None else discovery(id_token_signing_alg_values_supported=advertised)
    Provider(monkeypatch, tx, meta, signed(claims(tx), KEYS[alg], alg), jwks(KEYS[alg]))
    tx = oidc.consume(browser, state)
    if ok:
        assert (await oidc.exchange("code", tx)).sub == "immutable-sub"
    else:
        with pytest.raises(oidc.LoginRefused, match="token"):
            await oidc.exchange("code", tx)


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["RS256", [1], {"x": 1}])
async def test_malformed_discovery_algorithm_list_refused(monkeypatch, bad):
    state, tx = oidc.begin({}, "/admin/")
    Provider(monkeypatch, tx, discovery(id_token_signing_alg_values_supported=bad))
    with pytest.raises(oidc.LoginRefused, match="provider"):
        await oidc.authorization_url(state, tx)


# ── client authentication (D5) ──────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["client_secret_basic", "client_secret_post"])
async def test_token_auth_method_header_or_body_never_both(monkeypatch, method):
    monkeypatch.setattr(settings, "oidc_token_auth_method", method)
    browser = {}
    state, tx = oidc.begin(browser, "/admin/")
    provider = Provider(monkeypatch, tx, discovery(
        token_endpoint_auth_methods_supported=["client_secret_basic", "client_secret_post"]))
    await oidc.exchange("code", oidc.consume(browser, state))
    [req] = provider.token_requests()
    form = parse_qs(req.content.decode())
    if method == "client_secret_basic":
        expected = "Basic " + base64.b64encode(f"{CLIENT}:{SECRET}".encode()).decode()
        assert req.headers["authorization"] == expected
        assert "client_secret" not in form
    else:
        assert "authorization" not in req.headers
        assert form["client_id"] == [CLIENT] and form["client_secret"] == [SECRET]


@pytest.mark.asyncio
@pytest.mark.parametrize("method,advertised,ok", [
    ("client_secret_post", ["client_secret_basic"], False),
    ("client_secret_basic", ["client_secret_post"], False),
    ("client_secret_basic", ["private_key_jwt", "client_secret_basic"], True),
    ("client_secret_post", None, True),   # not listed → not checked
    ("client_secret_basic", "client_secret_basic", False),  # malformed
])
async def test_auth_method_refused_at_start_when_not_offered(monkeypatch, method, advertised, ok):
    monkeypatch.setattr(settings, "oidc_token_auth_method", method)
    state, tx = oidc.begin({}, "/admin/")
    meta = discovery() if advertised is None else discovery(token_endpoint_auth_methods_supported=advertised)
    Provider(monkeypatch, tx, meta)
    if ok:
        assert (await oidc.authorization_url(state, tx)).startswith(ISSUER)
    else:
        with pytest.raises(oidc.LoginRefused, match="provider"):
            await oidc.authorization_url(state, tx)


# ── RFC 9207 iss (D5) and metadata() ────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("advertised,iss,ok", [
    (True, None, False), (True, ISSUER, True), (True, ISSUER + "/", False), (True, "", False),
    (None, None, True), (False, None, True), ("true", None, True),
    (None, "https://evil.test", False), (False, ISSUER, True),
])
async def test_iss_parameter_required_only_when_advertised(monkeypatch, advertised, iss, ok):
    browser = {}
    state, tx = oidc.begin(browser, "/admin/")
    meta = discovery() if advertised is None else discovery(
        authorization_response_iss_parameter_supported=advertised)
    provider = Provider(monkeypatch, tx, meta)
    tx = oidc.consume(browser, state)
    if ok:
        assert (await oidc.exchange("code", tx, iss=iss)).iss == ISSUER
    else:
        with pytest.raises(oidc.LoginRefused, match="provider"):
            await oidc.exchange("code", tx, iss=iss)
        assert provider.token_requests() == []  # refused before the token request


@pytest.mark.asyncio
async def test_metadata_exposes_profile_fields(monkeypatch):
    Provider(monkeypatch, meta=discovery(authorization_response_iss_parameter_supported=True,
                                         id_token_signing_alg_values_supported=["ES256", "HS256", "RS256"]))
    async with oidc.client() as http:
        meta = await oidc.metadata(http)
    assert meta.iss_parameter_required is True
    assert meta.signing_algs == ("RS256", "ES256")
    assert meta.token_endpoint == ISSUER + "/protocol/openid-connect/token"


# ── transactions, endpoints, transport ──────────────────────────────────────


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


def test_transport_verifies_with_certifi_by_default():
    from src.services.transport_security import certifi_ssl_context
    transport = oidc.BoundedTransport()
    assert transport.inner._pool._ssl_context is certifi_ssl_context()


def test_transport_verifies_with_the_oidc_ca_context(monkeypatch):
    ctx = ssl.create_default_context()
    monkeypatch.setattr(settings, "_oidc_ssl_context", ctx)
    transport = oidc.BoundedTransport()
    assert transport.inner._pool._ssl_context is ctx
    assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname


@pytest.mark.parametrize("extra,expected", [
    ("", "openid email profile"), ("groups", "openid email profile groups"),
    ("groups roles", "openid email profile groups roles"),
])
def test_scopes(monkeypatch, extra, expected):
    monkeypatch.setattr(settings, "oidc_extra_scopes", extra)
    assert oidc.scope() == expected


@pytest.mark.asyncio
async def test_real_protocol_client_pkce_and_token_verification(monkeypatch):
    browser = {}
    state, tx = oidc.begin(browser, "/authorize?client_id=existing")
    provider = Provider(monkeypatch, tx)
    url = await oidc.authorization_url(state, tx)
    params = parse_qs(urlsplit(url).query)
    assert params["code_challenge_method"] == ["S256"]
    assert params["scope"] == ["openid email profile"]
    assert params["nonce"] == [tx.nonce]
    assert params["state"] == [state]
    assert params["response_type"] == ["code"]
    assert "code_verifier" not in params
    result = await oidc.exchange("test-code", oidc.consume(browser, state))
    assert result == oidc.VerifiedIdentity(ISSUER, "immutable-sub", None, False)
    [req] = provider.token_requests()
    form = parse_qs(req.content.decode())
    assert form["code_verifier"] == [tx.verifier]
    assert form["redirect_uri"] == ["https://notes.example.test/admin/auth/oidc/callback"]


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


# ── the callback (slice C: resolution through src/services/oidc_links.py) ──
#
# The database phase is stubbed at `oidc_links.resolve` / `prepare_login`; the
# real-database behaviour is `tests/test_oidc_callback_linking.py` and
# `tests/integration/test_oidc_callback_pg.py`.


def verified(sub="immutable-sub"):
    return oidc.VerifiedIdentity(ISSUER, sub, None, False)


def stub_links(monkeypatch, *, kind="bound", user_id=7, version=12, reason=None):
    from src.services import oidc_links
    resolution = oidc_links.Resolution(kind, ISSUER, "immutable-sub",
                                       user_id=None if kind == "none" else user_id)
    monkeypatch.setattr(oidc_links, "resolve", AsyncMock(return_value=resolution))
    if reason is not None or kind == "bound":
        monkeypatch.setattr(oidc_links, "prepare_login", AsyncMock(return_value=(
            oidc_links.PrepareResult(reason) if reason else
            oidc_links.PrepareResult(None, user_id=user_id, session_version=version))))


@pytest.mark.asyncio
async def test_callback_uses_existing_session_guard_and_safe_next(monkeypatch):
    browser = {}
    state, _ = oidc.begin(browser, "/authorize?client_id=old&scope=readwrite")
    req = request(query="state=" + state + "&code=test", session=browser)
    exchange = AsyncMock(return_value=verified())
    monkeypatch.setattr(oidc, "exchange", exchange)
    stub_links(monkeypatch, user_id=7, version=12)
    db = AsyncMock()
    db.execute.return_value = MagicMock(scalar_one_or_none=lambda: "existing")
    mint = AsyncMock(return_value="new-app-session")
    monkeypatch.setattr(routes, "start_session", mint)
    monkeypatch.setattr(routes, "warm_user_vault_cache", AsyncMock())
    response = await routes.oidc_callback(req, db)
    assert response.status_code == 302
    assert response.headers["location"] == "/authorize?client_id=old&scope=readwrite"
    mint.assert_awaited_once_with(req, db, 7, expected_session_version=12)
    assert exchange.await_args.kwargs == {"iss": None}
    assert not any(k in browser for k in ("id_token", "access_token", "refresh_token"))


@pytest.mark.asyncio
@pytest.mark.parametrize("active,minted", [(False, "unused"), (True, None)])
async def test_inactive_and_session_race_refuse(monkeypatch, active, minted):
    browser = {}
    state, _ = oidc.begin(browser, "/admin/")
    monkeypatch.setattr(oidc, "exchange", AsyncMock(return_value=verified()))
    stub_links(monkeypatch, version=3, reason=None if active else "account")
    db = AsyncMock()
    mint = AsyncMock(return_value=minted)
    monkeypatch.setattr(routes, "start_session", mint)
    monkeypatch.setattr(routes, "warm_user_vault_cache", AsyncMock())
    response = await routes.oidc_callback(request(query="state="+state+"&code=test", session=browser), db)
    assert response.status_code == 401
    assert mint.await_count == int(active)


@pytest.mark.asyncio
async def test_callback_refuses_unresolved_identity_without_db_writes(monkeypatch):
    browser = {}
    state, _ = oidc.begin(browser, "/admin/")
    monkeypatch.setattr(oidc, "exchange", AsyncMock(return_value=verified("someone-else")))
    stub_links(monkeypatch, kind="none")  # the real prepare_login refuses `unlinked`
    db = AsyncMock()
    response = await routes.oidc_callback(request(query="state="+state+"&code=test", session=browser), db)
    assert response.status_code == 401
    db.execute.assert_not_awaited()
    db.commit.assert_not_awaited()


@pytest.mark.parametrize("path", [oidc.CALLBACK_PATH, "/admin/auth/oidc/%63allback", "/admin/auth%2Foidc/callback"])
def test_callback_access_log_redacted(path):
    record = logging.LogRecord("uvicorn.access", 20, "", 0, "%s %s %s %s %s",
                               ("ip", "GET", path + "?code=secret&state=secret", "1.1", 302), None)
    assert OIDCCallbackAccessFilter().filter(record)
    assert "secret" not in record.getMessage()


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
async def test_encoded_callback_still_routes_and_refuses_invalid_state():
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from starlette.middleware.sessions import SessionMiddleware
    from src.database import get_session
    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="test-browser-secret")
    app.include_router(routes.router)

    async def db():
        yield AsyncMock()
    app.dependency_overrides[get_session] = db
    async with AsyncClient(transport=ASGITransport(app), base_url="https://notes.example.test") as http:
        response = await http.get("/admin/auth/oidc/%63allback?state=invalid&code=test")
        assert response.status_code == 401
        assert "Single sign-on could not be completed" in response.text
