"""The OIDC callback's linking phase, offline half (#416 slice C, task 3.9).

The real protocol client against a fake provider (real RS256 signatures), with
the database phase stubbed at `oidc_links.resolve` / `prepare_login` where a
case needs no rows. What it pins: the D4 order (state, discovery, `iss`,
token + JWKS, and only then the database), the RFC 9207 `iss` rules, one
response builder for every refusal, the password budget left untouched, the
`/authorize` round trip, the event sequence, and that no subject or email
reaches a log record. The real-database cases are
`tests/integration/test_oidc_callback_pg.py`.
"""
from __future__ import annotations

import ast
import logging
import re
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import parse_qs, urlsplit

import httpx2
import pytest
from joserfc import jwt
from joserfc.jwk import RSAKey
from starlette.requests import Request

from src.auth import oidc, routes
from src.config import settings
from src.limiter import limiter
from src.services import oidc_links, rate_limits, security_events

ROOT = Path(__file__).resolve().parent.parent
ISSUER = "https://identity.example.test/realms/home"
CLIENT = "obsidian"
SUBJECT = "f3c1-secret-subject-9a7e"
EMAIL = "Secret.Person@Example.com"
KEY = RSAKey.generate_key(2048, parameters={"kid": "k1"})


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    for name, value in dict(oidc_enabled=True, multi_user_mode=True, oidc_issuer=ISSUER,
                            oidc_client_id=CLIENT, oidc_client_secret="s3cret",
                            oidc_required_group="", oidc_token_auth_method="client_secret_basic",
                            oidc_extra_scopes="", base_url="https://notes.example.test",
                            local_login_enabled=True).items():
        monkeypatch.setattr(settings, name, value)
    monkeypatch.setattr(settings, "_oidc_ssl_context", None)
    oidc._transactions.clear()
    oidc._start_times.clear()
    limiter.reset()
    rate_limits.reset_state_for_tests()
    with security_events.suppression_disabled():
        yield
    oidc._transactions.clear()
    oidc._start_times.clear()
    limiter.reset()


def request(path=oidc.CALLBACK_PATH, query="", session=None):
    return Request({"type": "http", "method": "GET", "scheme": "https", "path": path,
                    "query_string": query.encode(), "headers": [(b"host", b"notes.example.test")],
                    "client": ("192.0.2.10", 1234), "server": ("notes.example.test", 443),
                    "session": {} if session is None else session})


class Provider:
    def __init__(self, monkeypatch, *, iss_supported=None, **claims):
        self.claims, self.iss_supported, self.seen = claims, iss_supported, []
        self.nonce = None
        monkeypatch.setattr(oidc, "BoundedTransport", lambda: httpx2.MockTransport(self.handle))

    def token_requests(self):
        return [r for r in self.seen if r.url.path.endswith("/token")]

    async def handle(self, req):
        self.seen.append(req)
        if req.url.path.endswith("openid-configuration"):
            meta = dict(issuer=ISSUER, authorization_endpoint=ISSUER + "/auth",
                        token_endpoint=ISSUER + "/token", jwks_uri=ISSUER + "/certs")
            if self.iss_supported is not None:
                meta["authorization_response_iss_parameter_supported"] = self.iss_supported
            return httpx2.Response(200, json=meta)
        if req.url.path.endswith("/token"):
            now = int(time.time())
            claims = dict(iss=ISSUER, sub=SUBJECT, aud=CLIENT, exp=now + 300, iat=now,
                          nonce=self.nonce, email=EMAIL, email_verified=True)
            claims.update(self.claims)
            token = jwt.encode({"alg": "RS256", "kid": "k1"}, claims, KEY)
            return httpx2.Response(200, json={"id_token": token, "access_token": "transient",
                                              "token_type": "Bearer"})
        return httpx2.Response(200, json={"keys": [KEY.as_dict(private=False)]})


async def start(provider, next_url="/admin/"):
    browser: dict = {}
    response = await routes.oidc_start(request("/admin/auth/oidc/start", session=browser),
                                       next=next_url)
    assert response.status_code == 302
    state = parse_qs(urlsplit(response.headers["location"]).query)["state"][0]
    provider.nonce = oidc._transactions[state].nonce
    return browser, state


def stub_db(monkeypatch, *, kind="bound", reason=None, linked=False, user_id=7, version=4):
    resolve = AsyncMock(return_value=oidc_links.Resolution(
        kind, ISSUER, SUBJECT, user_id=None if kind == "none" else user_id))
    prepare = AsyncMock(return_value=oidc_links.PrepareResult(reason) if reason else
                        oidc_links.PrepareResult(None, user_id=user_id, session_version=version,
                                                 linked=linked))
    monkeypatch.setattr(oidc_links, "resolve", resolve)
    monkeypatch.setattr(oidc_links, "prepare_login", prepare)
    mint = AsyncMock(return_value="sid")
    monkeypatch.setattr(routes, "start_session", mint)
    monkeypatch.setattr(routes, "warm_user_vault_cache", AsyncMock())
    db = AsyncMock()
    db.execute.return_value = MagicMock(scalar_one_or_none=lambda: "bob")
    return db, resolve, prepare, mint


async def callback(browser, state, db, **extra):
    query = "&".join([f"state={state}", "code=abc", *(f"{k}={v}" for k, v in extra.items())])
    return await routes.oidc_callback(request(query=query, session=browser), db)


# ── D4 order ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_database_phase_follows_validation_and_passes_claims(monkeypatch):
    provider = Provider(monkeypatch)
    browser, state = await start(provider)
    db, resolve, prepare, mint = stub_db(monkeypatch, version=9)
    order = []
    real_exchange = oidc.exchange

    async def exchange(*args, **kwargs):
        result = await real_exchange(*args, **kwargs)
        order.append("exchange")
        return result

    monkeypatch.setattr(oidc, "exchange", exchange)
    resolve.side_effect = lambda *a, **k: order.append("resolve") or oidc_links.Resolution(
        "bound", ISSUER, SUBJECT, user_id=7)
    response = await callback(browser, state, db)
    assert response.status_code == 302 and response.headers["location"] == "/admin/"
    assert order == ["exchange", "resolve"]
    args = resolve.await_args.args
    assert args[1:] == (ISSUER, SUBJECT, EMAIL, True)
    mint.assert_awaited_once()
    assert mint.await_args.kwargs == {"expected_session_version": 9}


@pytest.mark.asyncio
@pytest.mark.parametrize("claims", [{"aud": "someone-else"}, {"nonce": "forged"}])
async def test_an_invalid_token_never_reaches_the_database(monkeypatch, claims):
    provider = Provider(monkeypatch, **claims)
    browser, state = await start(provider)
    db, resolve, prepare, mint = stub_db(monkeypatch)
    response = await callback(browser, state, db)
    assert response.status_code == 401
    resolve.assert_not_awaited()
    db.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_provider_error_parameter_refuses_before_any_fetch(monkeypatch):
    provider = Provider(monkeypatch)
    browser, state = await start(provider)
    provider.seen.clear()
    db, resolve, *_ = stub_db(monkeypatch)
    response = await callback(browser, state, db, error="access_denied")
    assert response.status_code == 401
    assert provider.seen == [] and not resolve.await_count


# ── RFC 9207 iss ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("advertised,iss,ok", [
    (True, None, False),            # required and missing
    (True, ISSUER, True),
    (None, None, True),             # not advertised: optional
    (False, None, True),
    (None, "https://evil.example.test", False),   # mismatched, whatever discovery says
    (True, "https://evil.example.test", False),
])
async def test_iss_parameter(monkeypatch, advertised, iss, ok):
    provider = Provider(monkeypatch, iss_supported=advertised)
    browser, state = await start(provider)
    db, *_ = stub_db(monkeypatch)
    extra = {} if iss is None else {"iss": iss}
    response = await callback(browser, state, db, **extra)
    assert response.status_code == (302 if ok else 401)
    if not ok:
        # Discovery was fetched (the iss rule needs it); the token request never.
        assert provider.token_requests() == []


# ── one response builder ────────────────────────────────────────────────────


def _normalized(response) -> str:
    body = response.body.decode()
    body = re.sub(r'name="csrf_token" value="[^"]*"', "CSRF", body)
    return re.sub(r'nonce="[^"]*"', "NONCE", body)


@pytest.mark.asyncio
async def test_refusal_bodies_are_identical_across_reasons(monkeypatch):
    rendered = {}
    for reason in sorted(security_events.OIDC_REFUSAL_REASONS - {"capacity"}):
        rendered[reason] = (routes._oidc_refused(request(), reason))
    bodies = {_normalized(r) for r in rendered.values()}
    statuses = {r.status_code for r in rendered.values()}
    assert len(bodies) == 1 and statuses == {401}
    capacity = routes._oidc_refused(request(), "capacity")
    assert capacity.status_code == 503 and capacity.headers["Retry-After"] == "60"
    assert _normalized(capacity) in bodies


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["unlinked", "account", "email_unverified", "link_conflict"])
async def test_resolution_refusals_through_the_callback(monkeypatch, reason, caplog):
    provider = Provider(monkeypatch)
    bodies = set()
    for r in (reason, "unlinked"):
        browser, state = await start(provider)
        db, resolve, prepare, mint = stub_db(monkeypatch, reason=r)
        with caplog.at_level(logging.WARNING):
            response = await callback(browser, state, db)
        assert response.status_code == 401
        mint.assert_not_awaited()
        db.commit.assert_not_awaited()
        bodies.add(_normalized(response))
    assert len(bodies) == 1
    refused = [rec for rec in caplog.records if rec.getMessage() == "panel_oidc_refused"]
    assert {rec.reason for rec in refused} == {reason, "unlinked"}
    assert not any(hasattr(rec, "user_id") or hasattr(rec, "username") for rec in refused)


@pytest.mark.asyncio
async def test_refused_mint_rolls_back_and_refuses(monkeypatch):
    provider = Provider(monkeypatch)
    browser, state = await start(provider)
    db, resolve, prepare, mint = stub_db(monkeypatch, linked=True)
    mint.return_value = None
    response = await callback(browser, state, db)
    assert response.status_code == 401
    db.rollback.assert_awaited()


# ── events, budget, /authorize, logs ────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("linked", [True, False])
async def test_events_after_the_mint(monkeypatch, caplog, linked):
    provider = Provider(monkeypatch)
    browser, state = await start(provider)
    db, *_ = stub_db(monkeypatch, linked=linked)
    with caplog.at_level(logging.INFO):
        response = await callback(browser, state, db)
    assert response.status_code == 302
    names = [r.getMessage() for r in caplog.records
             if r.getMessage().startswith(("panel_oidc_linked", "panel_login_succeeded"))]
    assert names == (["panel_oidc_linked"] if linked else []) + ["panel_login_succeeded"]
    for record in caplog.records:
        if record.getMessage() in names:
            assert (record.user_id, record.username) == (7, "bob")


@pytest.mark.asyncio
async def test_exhausted_password_budget_neither_blocks_nor_is_charged(monkeypatch):
    for _ in range(50):
        rate_limits.record_login_failure(7)
    assert rate_limits.check_login_failures(7) is not None
    real_check = rate_limits.check_login_failures
    charge = MagicMock(side_effect=AssertionError("charged"))
    check = MagicMock(side_effect=AssertionError("consulted"))
    monkeypatch.setattr(rate_limits, "record_login_failure", charge)
    monkeypatch.setattr(rate_limits, "check_login_failures", check)
    for reason in (None, "unlinked"):
        provider = Provider(monkeypatch)
        browser, state = await start(provider)
        db, *_ = stub_db(monkeypatch, reason=reason)
        response = await callback(browser, state, db)
        assert response.status_code == (302 if reason is None else 401)
    assert real_check(7) is not None  # still exhausted, not reset


@pytest.mark.asyncio
async def test_authorize_round_trip_returns_to_the_exact_encoded_url(monkeypatch):
    target = ("/authorize?response_type=code&client_id=https%3A%2F%2Fclaude.ai%2Foauth%2Fm"
              "&redirect_uri=http%3A%2F%2Flocalhost%3A3118%2Fcallback&scope=read%20offline_access"
              "&state=s%2Fx&code_challenge=" + "a" * 43 + "&code_challenge_method=S256")
    provider = Provider(monkeypatch)
    browser, state = await start(provider, next_url=target)
    db, *_ = stub_db(monkeypatch)
    response = await callback(browser, state, db)
    assert response.status_code == 302 and response.headers["location"] == target


@pytest.mark.asyncio
async def test_no_subject_or_email_in_any_log_record(monkeypatch, caplog):
    with caplog.at_level(logging.DEBUG):
        for kind, reason, linked in (("pending", None, True), ("bound", None, False),
                                     ("unverified", "email_unverified", False),
                                     ("none", "unlinked", False)):
            provider = Provider(monkeypatch)
            browser, state = await start(provider)
            db, *_ = stub_db(monkeypatch, kind=kind, reason=reason, linked=linked)
            await callback(browser, state, db)
    assert caplog.records
    text = "\n".join(f"{r.getMessage()} {r.__dict__}" for r in caplog.records).lower()
    for secret in (SUBJECT, EMAIL.lower(), "secret.person", "abc&", "transient"):
        assert secret.lower() not in text, secret


# ── the closed reason set ───────────────────────────────────────────────────


def _literal_reasons() -> set[str]:
    found = set()
    for path in (ROOT / "src/auth/routes.py", ROOT / "src/auth/oidc.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
            if name in ("LoginRefused", "_oidc_refused"):
                for arg in node.args:
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                        found.add(arg.value)
    return found


def test_emitted_reasons_are_the_closed_set_and_mapping_is_gone():
    from typing import get_args

    emitted = _literal_reasons() | set(get_args(oidc_links.RefusalReason))
    assert emitted == security_events.OIDC_REFUSAL_REASONS
    assert "mapping" not in emitted
    doc = (ROOT / "docs/architecture/security-event-logging.md").read_text()
    row = next(line for line in doc.splitlines() if line.startswith("| `panel_oidc_refused`"))
    documented = set(re.findall(r"`([a-z_]+)`", row.split("|")[4].split(";")[0]))
    assert documented == security_events.OIDC_REFUSAL_REASONS
    assert "mapping" not in (ROOT / "src/auth/routes.py").read_text()
