"""OIDC-off regression goldens, database half (oidc-account-linking D16, task 0.3).

Captured on the base commit (bd9b386) before any linking slice, through
`src.main.app` against a throwaway, migrated Postgres, in the D16 matrix: OIDC
off single-user, OIDC off multi-user, OIDC on multi-user. The browser session
is minted the way each mode mints it in production — none in single-user mode,
the password form with OIDC off, #415's callback against a fake provider
(real RS256 ID token, real protocol client) with OIDC on — and never by a
dependency override.

* D16 item 2 — the authenticated consent render, DCR and CIMD;
* D16 item 3 — the approval: its redirect (golden), the code's user, and the
  `grant_id` lineage from the code through the exchanged token pair and a
  refresh, every token owned by the same user;
* D16 item 4 — the empty-table bootstrap POST (the database half; the closed
  and OIDC-on halves are offline in `tests/test_oidc_off_regression.py`, and
  the populated GET/POST here must equal those files);
* D16 item 5 — the users list, user edit and account pages.

In the OIDC-on column, items 2 and 3 must equal the OIDC-off multi-user
column (asserted), and item 5's goldens are its own files: slice C may add
expectations for item 5's OIDC blocks in a separate file, never edit these.

Skipped unless `PGVECTOR_TEST_ADMIN_URL` names a throwaway Postgres server
(`make test-integration` sets it).
"""
from __future__ import annotations

import datetime as dt
import hashlib
import sys
from urllib.parse import parse_qs, urlsplit

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete as sa_delete
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import _harness
from src.auth.passwords import hash_password
from src.database import get_session
from src.limiter import limiter
from src.models.db import (
    APIKey,
    OAuthClient,
    OAuthCode,
    OAuthToken,
    UsageLog,
    User,
    UserSession,
)
from src.oauth import cimd
from src.oauth import routes as oauth
from src.services import vault_overlap
from tests.golden.oidc_off import _support as g

pytestmark = [pytest.mark.asyncio(loop_scope="module"), _harness.requires_pgvector]

MODES = list(g.MODES)
PASSWORD = "golden-admin-password"
VERIFIER = "v" * 64
DCR_CLIENT_ID = "b7e2c41d9a3f4e60b7e2c41d9a3f4e60"
DCR_REDIRECT = "https://client.example/callback"
CIMD_URL = "https://claude.ai/oauth/claude-code-client-metadata"
CIMD_DOC = {
    "client_id": CIMD_URL,
    "client_name": "Claude Code",
    "redirect_uris": ["http://localhost/callback", "http://127.0.0.1/callback"],
    "grant_types": ["authorization_code", "refresh_token"],
    "response_types": ["code"],
    "token_endpoint_auth_method": "none",
}
CIMD_REDIRECT = "http://localhost:3118/callback"
FIXED_CREATED = dt.datetime(2026, 1, 2, 3, 4, 5, tzinfo=dt.timezone.utc)


@pytest.fixture(scope="module")
def migrated_url():
    yield from _harness.throwaway_database("oidc_goldens", 64)


@pytest_asyncio.fixture(loop_scope="module", scope="module")
async def sessionmaker(migrated_url):
    engine = create_async_engine(migrated_url)
    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await engine.dispose()


async def _wipe(sessionmaker):
    async with sessionmaker() as session:
        for model in (UsageLog, OAuthToken, OAuthCode, OAuthClient, APIKey, UserSession, User):
            await session.execute(sa_delete(model))
        await session.commit()


@pytest_asyncio.fixture(loop_scope="module")
async def env(sessionmaker, monkeypatch, tmp_path):
    """A factory: `await env(mode, seed=True)` → (AsyncClient, ctx dict)."""
    from src.config import settings
    from src.database import async_session as real_async_session

    await _wipe(sessionmaker)
    vault = tmp_path / "vault"
    vault.mkdir()
    for module in list(sys.modules.values()):
        if (getattr(module, "__name__", "").startswith("src.")
                and getattr(module, "async_session", None) is real_async_session):
            monkeypatch.setattr(module, "async_session", sessionmaker)

    async def _get_session():
        async with sessionmaker() as session:
            yield session

    provider = g.FakeProvider()
    provider.install(monkeypatch)
    stack, clients = [], []

    async def factory(mode: str, *, seed: bool = True):
        ctx = {"mode": mode, "vault": str(vault), "user_ids": {}, "admin_id": None}
        if seed:
            async with sessionmaker() as session:
                admin = User(username="admin", password_hash=hash_password(PASSWORD),
                             is_admin=True, is_active=True, vault_path=str(vault),
                             created_at=FIXED_CREATED)
                bob = User(username="bob", password_hash=hash_password(PASSWORD + "-bob"),
                           is_admin=False, is_active=True, vault_path=None,
                           created_at=FIXED_CREATED + dt.timedelta(hours=1))
                # Distinct creation instants: the users list orders by
                # `created_at`, and a tie would make the row order arbitrary.
                session.add_all([admin, bob])
                await session.flush()
                session.add(OAuthClient(
                    client_id=DCR_CLIENT_ID, client_secret_hash=None,
                    token_endpoint_auth_method="none", client_name="Golden Client",
                    redirect_uris=[DCR_REDIRECT], scope="read readwrite offline_access",
                    created_at=FIXED_CREATED,
                    user_id=admin.id if mode != "off_single" else None,
                ))
                await session.commit()
                ctx["admin_id"], ctx["bob_id"] = admin.id, bob.id
                ctx["user_ids"] = {admin.id: "admin", bob.id: "bob"}
        g.apply_mode(monkeypatch, mode, subject_user_id=ctx["admin_id"])
        monkeypatch.setattr(settings, "vault_path", str(vault))
        mounted = g.mounted_app(mode)
        app = mounted.__enter__()
        stack.append(mounted)
        app.dependency_overrides[get_session] = _get_session
        http = AsyncClient(transport=ASGITransport(app), base_url=g.BASE)
        clients.append(http)
        ctx["provider"] = provider
        return http, ctx

    limiter.reset()
    cimd.reset_state_for_tests()
    vault_overlap.reset_snapshot_state()
    try:
        yield factory
    finally:
        for http in clients:
            await http.aclose()
        for mounted in reversed(stack):
            mounted.__exit__(None, None, None)
        cimd.reset_state_for_tests()
        vault_overlap.reset_snapshot_state()
        limiter.reset()
        await _wipe(sessionmaker)


def render(response, ctx) -> str:
    return g.render(response, user_ids=ctx["user_ids"], paths={ctx["vault"]: "<VAULT>"})


async def sign_in(http, ctx):
    """Mint the browser session the way the mode does in production."""
    mode = ctx["mode"]
    if mode == "off_single":
        return
    if mode == "off_multi":
        page = await http.get("/admin/auth/login")
        response = await http.post("/admin/auth/login", data={
            "csrf_token": g.csrf_from(page.text), "username": "admin",
            "password": PASSWORD, "next": "/admin/",
        })
        assert response.status_code == 302, response.text[:300]
        return
    # #416 slice C: the subject is bound through the database (the link
    # service's direct bind), replacing #415's retired environment map.
    from src.services import oidc_links

    async with oauth.async_session() as session:
        bound = await oidc_links.bind_subject(session, ctx["admin_id"], g.ISSUER, g.OIDC_SUBJECT)
        assert bound.ok, bound
        await session.commit()
    await ctx["provider"].sign_in(http)


def _prime_cimd():
    cimd._cache_put(CIMD_URL, cimd.validate_document(CIMD_URL, CIMD_DOC), None)


def authorize_params(kind: str) -> dict:
    client_id, redirect = (DCR_CLIENT_ID, DCR_REDIRECT) if kind == "dcr" else (CIMD_URL, CIMD_REDIRECT)
    return {
        "response_type": "code", "client_id": client_id, "redirect_uri": redirect,
        "code_challenge": oauth._base64url_sha256(VERIFIER), "code_challenge_method": "S256",
        "scope": "read offline_access", "state": "client-state-golden",
    }


# ── D16 item 2: authenticated consent render ────────────────────────────────


async def _consent(env, mode, kind):
    http, ctx = await env(mode)
    await sign_in(http, ctx)
    if kind == "cimd":
        _prime_cimd()
    response = await http.get("/authorize", params=authorize_params(kind))
    assert response.status_code == 200, response.text[:300]
    return http, ctx, response


@pytest.mark.parametrize("kind", ["dcr", "cimd"])
@pytest.mark.parametrize("mode", MODES)
async def test_authenticated_consent_render(env, mode, kind):
    _, ctx, response = await _consent(env, mode, kind)
    g.check(f"consent_{kind}_{mode}", render(response, ctx))


@pytest.mark.parametrize("kind", ["dcr", "cimd"])
async def test_consent_render_equal_across_oidc(env, kind):
    rendered = {}
    for mode in g.MULTI_MODES:
        _, ctx, response = await _consent(env, mode, kind)
        rendered[mode] = render(response, ctx)
        await _wipe_between(env)
    assert rendered["off_multi"] == rendered["on_multi"]


async def _wipe_between(env):
    """Clear the previous factory call's rows (usernames are unique) without
    tearing down the app overrides; the factory seeds fresh rows."""
    await _wipe(oauth.async_session)


# ── D16 item 3: consent approval, code user, grant lineage ──────────────────


async def _approve(env, mode, kind):
    http, ctx, page = await _consent(env, mode, kind)
    params = authorize_params(kind)
    response = await http.post("/authorize", data={
        "action": "approve", "client_id": params["client_id"],
        "redirect_uri": params["redirect_uri"], "code_challenge": params["code_challenge"],
        "code_challenge_method": "S256", "scope": "read offline_access",
        "state": g.hidden(page.text, "state"), "client_state": "client-state-golden",
    })
    assert response.status_code == 302, response.text[:300]
    return http, ctx, response


@pytest.mark.parametrize("kind", ["dcr", "cimd"])
@pytest.mark.parametrize("mode", MODES)
async def test_consent_approval_lineage(env, mode, kind):
    http, ctx, response = await _approve(env, mode, kind)
    g.check(f"approve_{kind}_{mode}", render(response, ctx))

    params = authorize_params(kind)
    query = parse_qs(urlsplit(response.headers["location"]).query)
    assert query["iss"] == [g.BASE] and query["state"] == ["client-state-golden"]
    code = query["code"][0]
    expected_user = None if mode == "off_single" else ctx["admin_id"]

    sm = oauth.async_session
    code_hash = hashlib.sha256(code.encode()).hexdigest()
    async with sm() as session:
        row = (await session.execute(select(OAuthCode).where(OAuthCode.code_hash == code_hash))).scalar_one()
    assert row.user_id == expected_user
    assert row.grant_id is None and not row.used

    exchanged = await http.post("/token", data={
        "grant_type": "authorization_code", "code": code, "code_verifier": VERIFIER,
        "redirect_uri": params["redirect_uri"], "client_id": params["client_id"],
    })
    assert exchanged.status_code == 200, exchanged.text
    pair = exchanged.json()
    assert pair["token_type"].lower() == "bearer" and pair["scope"] == "offline_access read"
    assert pair["access_token"] and pair["refresh_token"]

    async with sm() as session:
        row = (await session.execute(select(OAuthCode).where(OAuthCode.code_hash == code_hash))).scalar_one()
        tokens = (await session.execute(select(OAuthToken))).scalars().all()
    assert row.used and row.grant_id
    assert tokens and {t.grant_id for t in tokens} == {row.grant_id}
    assert {t.user_id for t in tokens} == {expected_user}

    refreshed = await http.post("/token", data={
        "grant_type": "refresh_token", "refresh_token": pair["refresh_token"],
        "client_id": params["client_id"],
    })
    assert refreshed.status_code == 200, refreshed.text
    async with sm() as session:
        tokens = (await session.execute(select(OAuthToken))).scalars().all()
    assert len(tokens) > 2
    assert {t.grant_id for t in tokens} == {row.grant_id}
    assert {t.user_id for t in tokens} == {expected_user}
    assert len({t.grant_issued_at for t in tokens}) == 1


@pytest.mark.parametrize("kind", ["dcr", "cimd"])
async def test_approval_redirect_equal_across_oidc(env, kind):
    rendered = {}
    for mode in g.MULTI_MODES:
        _, ctx, response = await _approve(env, mode, kind)
        rendered[mode] = render(response, ctx)
        await _wipe_between(env)
    assert rendered["off_multi"] == rendered["on_multi"]


# ── D16 item 4: /admin/register against the real table ──────────────────────


async def test_register_bootstrap_on_empty_table(env):
    http, ctx = await env("off_multi", seed=False)
    page = await http.get("/admin/register")
    assert page.status_code == 200
    g.check("register_get_empty_off_multi", render(page, ctx))  # == the offline golden
    response = await http.post("/admin/register", data={
        "csrf_token": g.csrf_from(page.text), "username": "first",
        "password": PASSWORD, "password_confirm": PASSWORD, "vault_path": ctx["vault"],
    })
    assert response.status_code == 302
    g.check("register_post_empty_off_multi", render(response, ctx))
    async with oauth.async_session() as session:
        users = (await session.execute(select(User))).scalars().all()
    assert [(u.username, u.is_admin, u.vault_path) for u in users] == [("first", True, ctx["vault"])]
    closed = await http.get("/admin/register")
    assert closed.status_code == 302 and closed.headers["location"] == "/admin/auth/login"


async def test_register_closed_on_populated_table(env):
    http, ctx = await env("off_multi")
    page = await http.get("/admin/register")
    g.check("register_get_populated_off_multi", render(page, ctx))  # == the offline golden
    token = g.csrf_from((await http.get("/admin/auth/login")).text)
    response = await http.post("/admin/register", data={
        "csrf_token": token, "username": "second", "password": PASSWORD,
        "password_confirm": PASSWORD, "vault_path": ctx["vault"],
    })
    g.check("register_post_populated_off_multi", render(response, ctx))
    async with oauth.async_session() as session:
        names = sorted((await session.execute(select(User.username))).scalars().all())
    assert names == ["admin", "bob"]


# ── D16 item 5: users list, user edit, account ──────────────────────────────


@pytest.mark.parametrize("page", ["users", "user_edit_self", "user_edit_other", "account"])
@pytest.mark.parametrize("mode", MODES)
async def test_panel_pages(env, mode, page):
    http, ctx = await env(mode)
    await sign_in(http, ctx)
    path = {
        "users": "/admin/users/",
        "user_edit_self": f"/admin/users/{ctx['admin_id']}/edit",
        "user_edit_other": f"/admin/users/{ctx['bob_id']}/edit",
        "account": "/admin/account",
    }[page]
    response = await http.get(path)
    if page == "account" and mode == "off_single":
        assert response.status_code == 404
    else:
        assert response.status_code == 200, response.text[:300]
    g.check(f"page_{page}_{mode}", render(response, ctx))
