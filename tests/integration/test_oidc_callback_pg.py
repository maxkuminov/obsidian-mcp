"""The OIDC callback against a real database (#416 slice C, task 3.9).

The shipped app (`src/main.py`, OIDC on, multi-user), the real protocol client
against a fake provider signing real RS256 ID tokens, and a throwaway migrated
Postgres: resolution by `(iss, sub)`, the first-login bind, every refusal's
"nothing written", the bind/mint atomicity and the `last_login_at` stamp.

Skipped unless `PGVECTOR_TEST_ADMIN_URL` names a throwaway Postgres server
(`make test-integration` sets it).
"""
from __future__ import annotations

import logging
import sys
import time
from urllib.parse import parse_qs, urlsplit

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete as sa_delete
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import _harness
from src.auth import oidc
from src.auth import routes as auth_routes
from src.database import get_session
from src.limiter import limiter
from src.models.db import User, UserSession
from src.services import oidc_links, security_events
from tests.golden.oidc_off import _support as g

pytestmark = [pytest.mark.asyncio(loop_scope="module"), _harness.requires_pgvector]

ISS = g.ISSUER
DROP = object()


class Provider(g.FakeProvider):
    """The golden fake provider, with per-login claims."""

    def __init__(self):
        super().__init__()
        self.claims: dict = {}

    async def handle(self, req):
        import httpx2
        from joserfc import jwt

        if req.url.path.endswith("/token"):
            now = int(time.time())
            claims = dict(iss=ISS, sub=self.subject, aud=g.OIDC_CLIENT, exp=now + 300,
                          iat=now, nonce=self.nonce, groups=[g.OIDC_GROUP])
            for key, value in self.claims.items():
                if value is DROP:
                    claims.pop(key, None)
                else:
                    claims[key] = value
            token = jwt.encode({"alg": "RS256", "kid": "golden-key"}, claims, self.key)
            return httpx2.Response(200, json={"id_token": token, "access_token": "transient",
                                              "token_type": "Bearer"})
        return await super().handle(req)


@pytest.fixture(scope="module")
def migrated_url():
    yield from _harness.throwaway_database("oidc_callback", 64)


@pytest_asyncio.fixture(loop_scope="module", scope="module")
async def maker(migrated_url):
    engine = create_async_engine(migrated_url, pool_size=10, max_overflow=5)
    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await engine.dispose()


async def _wipe(maker):
    async with maker() as session:
        await session.execute(sa_delete(UserSession))
        await session.execute(sa_delete(User))
        await session.commit()


@pytest_asyncio.fixture(loop_scope="module")
async def env(maker, monkeypatch):
    from src.database import async_session as real_async_session

    await _wipe(maker)
    for module in list(sys.modules.values()):
        if (getattr(module, "__name__", "").startswith("src.")
                and getattr(module, "async_session", None) is real_async_session):
            monkeypatch.setattr(module, "async_session", maker)

    async def _get_session():
        async with maker() as session:
            yield session

    g.apply_mode(monkeypatch, "on_multi")
    provider = Provider()
    provider.install(monkeypatch)
    mounted = g.mounted_app("on_multi")
    app = mounted.__enter__()
    app.dependency_overrides[get_session] = _get_session
    http = AsyncClient(transport=ASGITransport(app), base_url=g.BASE)
    limiter.reset()
    with security_events.suppression_disabled():
        try:
            yield http, provider
        finally:
            await http.aclose()
            mounted.__exit__(None, None, None)
            limiter.reset()
            await _wipe(maker)


async def make_user(maker, username, *, is_active=True, bound=None, pending=None, age_days=0):
    async with maker() as session:
        user = User(username=username, password_hash="x", is_admin=False,
                    is_active=is_active, session_version=1)
        session.add(user)
        await session.flush()
        values = {}
        if bound:
            values.update(oidc_issuer=ISS, oidc_subject=bound, oidc_linked_at=text("now()"))
        if pending:
            values.update(oidc_expected_issuer=ISS, oidc_expected_email=pending,
                          oidc_expected_set_at=text(f"now() - interval '{age_days} days'"))
        if values:
            await session.execute(update(User).where(User.id == user.id).values(**values))
        await session.commit()
        return user.id


async def state_of(maker, user_id):
    async with maker() as session:
        user = (await session.execute(select(User).where(User.id == user_id))).scalar_one()
        sessions = (await session.execute(
            select(UserSession).where(UserSession.user_id == user_id)
        )).scalars().all()
    return {
        "bound": (user.oidc_issuer, user.oidc_subject),
        "pending": (user.oidc_expected_issuer, user.oidc_expected_email),
        "last_login": user.last_login_at,
        "sessions": len(sessions),
    }


async def login(http, provider, sub, **claims):
    limiter.reset()
    oidc._start_times.clear()
    provider.subject = sub
    provider.claims = claims
    start = await http.get("/admin/auth/oidc/start", params={"next": "/admin/"})
    assert start.status_code == 302, start.text[:300]
    state = parse_qs(urlsplit(start.headers["location"]).query)["state"][0]
    provider.nonce = oidc._transactions[state].nonce
    response = await http.get(oidc.CALLBACK_PATH, params={"state": state, "code": "c0de"})
    http.cookies.clear()
    return response


def refused_reasons(caplog):
    return [r.reason for r in caplog.records if r.getMessage() == "panel_oidc_refused"]


# ── bound: (iss, sub) alone ─────────────────────────────────────────────────


@pytest.mark.parametrize("email_claims", [
    {"email": DROP, "email_verified": DROP},
    {"email": 42, "email_verified": "true"},
    {"email": ["a@b.c"], "email_verified": "true"},
    {"email": "x" * 290 + "@example.com", "email_verified": "true"},
    {"email": "two@at@example.com", "email_verified": "true"},
])
async def test_bound_login_ignores_the_email_claim(env, maker, email_claims):
    http, provider = env
    uid = await make_user(maker, "max", bound="sub-max")
    response = await login(http, provider, "sub-max", **email_claims)
    assert response.status_code == 302, response.text[:300]
    after = await state_of(maker, uid)
    assert after["sessions"] == 1 and after["last_login"] is not None
    assert after["bound"] == (ISS, "sub-max")


async def test_renamed_provider_account_still_signs_in(env, maker):
    http, provider = env
    uid = await make_user(maker, "max", bound="sub-max")
    response = await login(http, provider, "sub-max", email="new-name@example.com",
                           email_verified=True, preferred_username="renamed")
    assert response.status_code == 302
    assert (await state_of(maker, uid))["sessions"] == 1


async def test_reassigned_address_with_a_new_subject_is_refused(env, maker, caplog):
    http, provider = env
    uid = await make_user(maker, "max", bound="sub-max")
    with caplog.at_level(logging.WARNING):
        response = await login(http, provider, "sub-impostor", email="max@example.com",
                               email_verified=True)
    assert response.status_code == 401
    assert refused_reasons(caplog) == ["unlinked"]
    assert (await state_of(maker, uid))["sessions"] == 0


async def test_bound_identity_wins_over_another_users_pending_email(env, maker):
    http, provider = env
    a = await make_user(maker, "alice", bound="sub-alice")
    b = await make_user(maker, "bob", pending="bob@example.com", age_days=1)
    response = await login(http, provider, "sub-alice", email="bob@example.com",
                           email_verified=True)
    assert response.status_code == 302
    assert (await state_of(maker, a))["sessions"] == 1
    after_b = await state_of(maker, b)
    assert after_b["pending"] == (ISS, "bob@example.com") and after_b["bound"] == (None, None)
    assert after_b["sessions"] == 0


# ── pending: verified email, once ───────────────────────────────────────────


async def test_pending_bind_with_a_verified_email(env, maker, caplog):
    http, provider = env
    uid = await make_user(maker, "max", pending="max@example.com", age_days=1)
    with caplog.at_level(logging.INFO):
        response = await login(http, provider, "sub-new", email="Max@Example.com",
                               email_verified=True)
    assert response.status_code == 302, response.text[:300]
    after = await state_of(maker, uid)
    assert after["bound"] == (ISS, "sub-new") and after["pending"] == (None, None)
    assert after["sessions"] == 1 and after["last_login"] is not None
    names = [r.getMessage() for r in caplog.records
             if r.getMessage() in ("panel_oidc_linked", "panel_login_succeeded")]
    assert names == ["panel_oidc_linked", "panel_login_succeeded"]
    text_ = "\n".join(f"{r.getMessage()} {r.__dict__}" for r in caplog.records).lower()
    assert "sub-new" not in text_ and "max@example.com" not in text_
    # Once: the same address under another subject no longer matches.
    second = await login(http, provider, "sub-other", email="max@example.com",
                         email_verified=True)
    assert second.status_code == 401


@pytest.mark.parametrize("verified", [False, DROP, "true"])
async def test_unverified_email_does_not_bind(env, maker, caplog, verified):
    http, provider = env
    uid = await make_user(maker, "max", pending="max@example.com", age_days=1)
    before = await state_of(maker, uid)
    with caplog.at_level(logging.WARNING):
        response = await login(http, provider, "sub-new", email="max@example.com",
                               email_verified=verified)
    assert response.status_code == 401
    assert refused_reasons(caplog) == ["email_unverified"]
    assert await state_of(maker, uid) == before


@pytest.mark.parametrize("email", [DROP, 42, "max@@example.com", "x" * 300])
async def test_absent_or_malformed_email_on_the_pending_branch(env, maker, caplog, email):
    http, provider = env
    uid = await make_user(maker, "max", pending="max@example.com", age_days=1)
    before = await state_of(maker, uid)
    with caplog.at_level(logging.WARNING):
        response = await login(http, provider, "sub-new", email=email, email_verified=True)
    assert response.status_code == 401
    assert refused_reasons(caplog) == ["unlinked"]
    assert await state_of(maker, uid) == before


async def test_expired_expectation_does_not_bind(env, maker, caplog):
    http, provider = env
    uid = await make_user(maker, "max", pending="max@example.com", age_days=8)
    before = await state_of(maker, uid)
    with caplog.at_level(logging.WARNING):
        response = await login(http, provider, "sub-new", email="max@example.com",
                               email_verified=True)
    assert response.status_code == 401
    assert refused_reasons(caplog) == ["unlinked"]
    assert await state_of(maker, uid) == before


async def test_refusal_bodies_are_identical(env, maker):
    http, provider = env
    await make_user(maker, "inactive", is_active=False, bound="sub-inactive")
    unlinked = await login(http, provider, "sub-nobody")
    account = await login(http, provider, "sub-inactive")
    assert unlinked.status_code == account.status_code == 401
    norm = lambda r: g.normalize(r.text)  # noqa: E731
    assert norm(unlinked) == norm(account)


# ── nothing written on refusal; bind and mint are one fact ─────────────────


async def test_inactive_pending_user_is_refused_before_any_write(env, maker, caplog):
    http, provider = env
    uid = await make_user(maker, "max", is_active=False, pending="max@example.com", age_days=1)
    before = await state_of(maker, uid)
    with caplog.at_level(logging.WARNING):
        response = await login(http, provider, "sub-new", email="max@example.com",
                               email_verified=True)
    assert response.status_code == 401
    assert refused_reasons(caplog) == ["account"]
    assert await state_of(maker, uid) == before


async def test_refused_mint_leaves_no_binding_and_no_last_login(env, maker, monkeypatch, caplog):
    http, provider = env
    uid = await make_user(maker, "max", pending="max@example.com", age_days=1)
    real = auth_routes.start_session

    async def stale_version(request, session, user_id, *, expected_session_version):
        # The account's session version moved between the re-read and the mint.
        return await real(request, session, user_id,
                          expected_session_version=expected_session_version + 1)

    monkeypatch.setattr(auth_routes, "start_session", stale_version)
    with caplog.at_level(logging.WARNING):
        response = await login(http, provider, "sub-new", email="max@example.com",
                               email_verified=True)
    assert response.status_code == 401
    assert refused_reasons(caplog) == ["session_mint"]
    after = await state_of(maker, uid)
    assert after["bound"] == (None, None) and after["pending"] == (ISS, "max@example.com")
    assert after["last_login"] is None and after["sessions"] == 0


async def test_unlink_committed_between_resolve_and_mint_refuses(env, maker, monkeypatch, caplog):
    http, provider = env
    uid = await make_user(maker, "max", bound="sub-max")
    real = oidc_links.prepare_login

    async def unlink_first(session, resolution):
        async with maker() as other:
            await other.execute(update(User).where(User.id == uid).values(
                oidc_issuer=None, oidc_subject=None, oidc_linked_at=None))
            await other.commit()
        return await real(session, resolution)

    monkeypatch.setattr(oidc_links, "prepare_login", unlink_first)
    with caplog.at_level(logging.WARNING):
        response = await login(http, provider, "sub-max")
    assert response.status_code == 401
    assert refused_reasons(caplog) == ["unlinked"]
    after = await state_of(maker, uid)
    assert after["sessions"] == 0 and after["last_login"] is None


async def test_last_login_is_stamped_on_a_bound_login(env, maker):
    http, provider = env
    uid = await make_user(maker, "max", bound="sub-max")
    assert (await state_of(maker, uid))["last_login"] is None
    assert (await login(http, provider, "sub-max")).status_code == 302
    assert (await state_of(maker, uid))["last_login"] is not None
