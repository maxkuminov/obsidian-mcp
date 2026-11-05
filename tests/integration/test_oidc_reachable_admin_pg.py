"""Panel link actions and the last reachable administrator (#416 slice C).

Tasks 3.10 (the database half) and 3.11, through the shipped app (OIDC on,
multi-user) against a throwaway migrated Postgres:

* the three link actions — success, conflict, re-point refusal, CSRF and
  non-admin refusals, OIDC off → 404, session revocation on unlink, the
  self-unlink refusal;
* the actor re-check, deterministic: A's request is held *before* the admin
  guard while B's demotion of A commits, then released (once per route);
* D15 at every reducing site — unlink, demote, deactivate, soft delete,
  permanent delete — refusing a zero-reachable result while password sign-in
  is disabled, proceeding when another reachable admin exists or password
  sign-in is enabled, and an unbound active admin not counting;
* D15 narrowed to its target: with zero reachable admins and password sign-in
  off, a mutation of an ordinary user, an unbound admin or an admin bound
  under a previous issuer proceeds, because it cannot reduce the count;
* the D15 races, deterministic: the first request holds the guard on a
  barrier while the second is issued; exactly one succeeds and one reachable
  admin always remains.

Sessions are minted the way production mints them: password sign-in (while it
is enabled) for unbound accounts, the OIDC callback against a fake provider
for bound ones. Password sign-in is switched off afterwards where a case needs
it; existing sessions stay valid, as they do in production.

Skipped unless `PGVECTOR_TEST_ADMIN_URL` names a throwaway Postgres server.
"""
from __future__ import annotations

import asyncio
import logging
import sys
from urllib.parse import parse_qs, urlsplit

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete as sa_delete
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import _harness
from src.auth import oidc
from src.auth.passwords import hash_password
from src.config import settings
from src.control_panel import users as users_module
from src.database import get_session
from src.limiter import limiter
from src.models.db import User, UserSession
from src.services import security_events
from tests.golden.oidc_off import _support as g

pytestmark = [pytest.mark.asyncio(loop_scope="module"), _harness.requires_pgvector]

ISS = g.ISSUER
PASSWORD = "reachable-admin-password"


@pytest.fixture(scope="module")
def migrated_url():
    yield from _harness.throwaway_database("oidc_reachable", 64)


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


class Env:
    def __init__(self, maker, app, provider, monkeypatch):
        self.maker, self.app, self.provider, self.monkeypatch = maker, app, provider, monkeypatch
        self.clients: list[AsyncClient] = []

    def client(self) -> AsyncClient:
        http = AsyncClient(transport=ASGITransport(self.app), base_url=g.BASE)
        self.clients.append(http)
        return http

    async def user(self, username, *, admin=False, active=True, bound=None, pending=None):
        async with self.maker() as session:
            user = User(username=username, password_hash=hash_password(PASSWORD),
                        is_admin=admin, is_active=active, session_version=1)
            session.add(user)
            await session.flush()
            values = {}
            if bound:
                values.update(oidc_issuer=ISS, oidc_subject=bound, oidc_linked_at=text("now()"))
            if pending:
                values.update(oidc_expected_issuer=ISS, oidc_expected_email=pending,
                              oidc_expected_set_at=text("now()"))
            if values:
                await session.execute(update(User).where(User.id == user.id).values(**values))
            await session.commit()
            return user.id

    async def password_login(self, username) -> AsyncClient:
        limiter.reset()
        http = self.client()
        page = await http.get("/admin/auth/login")
        response = await http.post("/admin/auth/login", data={
            "csrf_token": g.csrf_from(page.text), "username": username,
            "password": PASSWORD, "next": "/admin/"})
        assert response.status_code == 302, response.text[:300]
        return http

    async def sso_login(self, subject) -> AsyncClient:
        limiter.reset()
        oidc._start_times.clear()
        http = self.client()
        self.provider.subject = subject
        start = await http.get("/admin/auth/oidc/start", params={"next": "/admin/"})
        state = parse_qs(urlsplit(start.headers["location"]).query)["state"][0]
        self.provider.nonce = oidc._transactions[state].nonce
        response = await http.get(oidc.CALLBACK_PATH, params={"state": state, "code": "c"})
        assert response.status_code == 302, response.text[:300]
        return http

    async def row(self, user_id):
        async with self.maker() as session:
            return (await session.execute(select(User).where(User.id == user_id))).scalar_one_or_none()

    async def live_sessions(self, user_id):
        async with self.maker() as session:
            return (await session.execute(select(func.count(UserSession.id)).where(
                UserSession.user_id == user_id, UserSession.revoked_at.is_(None)))).scalar_one()

    async def reachable(self):
        async with self.maker() as session:
            return (await session.execute(select(func.count(User.id)).where(
                User.is_admin.is_(True), User.is_active.is_(True),
                User.oidc_issuer == ISS, User.oidc_subject.is_not(None)))).scalar_one()


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
    provider = g.FakeProvider()
    provider.install(monkeypatch)
    mounted = g.mounted_app("on_multi")
    app = mounted.__enter__()
    app.dependency_overrides[get_session] = _get_session
    e = Env(maker, app, provider, monkeypatch)
    with security_events.suppression_disabled():
        try:
            yield e
        finally:
            for http in e.clients:
                await http.aclose()
            mounted.__exit__(None, None, None)
            limiter.reset()
            await _wipe(maker)


def password_login_off(env):
    env.monkeypatch.setattr(settings, "local_login_enabled", False)


async def post(http, path, data=None, *, edit_page=None):
    """POST a panel form with a CSRF token taken from a page the actor can see."""
    page = await http.get(edit_page or "/admin/account")
    assert page.status_code == 200, page.status_code
    payload = {"csrf_token": g.csrf_from(page.text), **(data or {})}
    return await http.post(path, data=payload)


async def edit(http, target_id, *, admin, active):
    data = {"vault_path": ""}
    if admin:
        data["is_admin"] = "on"
    if active:
        data["is_active"] = "on"
    return await post(http, f"/admin/users/{target_id}/edit", data)


async def delete(http, target_id, *, permanent=False):
    suffix = "?permanent=true" if permanent else ""
    return await post(http, f"/admin/users/{target_id}/delete{suffix}")


async def unlink(http, target_id):
    return await post(http, f"/admin/users/{target_id}/oidc/unlink")


# ── the link actions ────────────────────────────────────────────────────────


async def test_expect_then_bind_then_refuse_repoint(env, caplog):
    admin = await env.user("admin", admin=True)
    bob = await env.user("bob")
    http = await env.password_login("admin")
    with caplog.at_level(logging.INFO):
        response = await post(http, f"/admin/users/{bob}/oidc/expect", {"email": " Bob@Example.COM "})
    assert response.status_code == 303 and response.headers["location"] == f"/admin/users/{bob}/edit"
    row = await env.row(bob)
    assert (row.oidc_expected_issuer, row.oidc_expected_email) == (ISS, "bob@example.com")
    assert row.oidc_expected_set_at is not None and row.oidc_subject is None
    page = await http.get(f"/admin/users/{bob}/edit")
    assert "Waiting for first sign-in from" in page.text and "bob@example.com" in page.text
    set_events = [r for r in caplog.records if r.getMessage() == "panel_oidc_link_set"]
    assert [(r.mode, r.actor_user_id, r.user_id) for r in set_events] == [("expect", admin, bob)]
    assert "bob@example.com" not in str(set_events[0].__dict__).lower()

    response = await post(http, f"/admin/users/{bob}/oidc/bind", {"subject": "sub-bob"})
    row = await env.row(bob)
    assert (row.oidc_issuer, row.oidc_subject) == (ISS, "sub-bob")
    assert row.oidc_expected_email is None and row.oidc_linked_at is not None

    for path, data in ((f"/admin/users/{bob}/oidc/expect", {"email": "x@example.com"}),
                       (f"/admin/users/{bob}/oidc/bind", {"subject": "sub-other"})):
        response = await post(http, path, data)
        assert response.status_code == 303
        row = await env.row(bob)
        assert (row.oidc_issuer, row.oidc_subject, row.oidc_expected_email) == (ISS, "sub-bob", None)


async def test_conflicts_write_nothing(env):
    await env.user("admin", admin=True)
    await env.user("carol", bound="sub-carol", pending=None)
    await env.user("dave", pending="dave@example.com")
    bob = await env.user("bob")
    http = await env.password_login("admin")
    await post(http, f"/admin/users/{bob}/oidc/bind", {"subject": "sub-carol"})
    await post(http, f"/admin/users/{bob}/oidc/expect", {"email": "DAVE@example.com"})
    row = await env.row(bob)
    assert (row.oidc_subject, row.oidc_expected_email) == (None, None)


async def test_invalid_values_write_nothing(env):
    await env.user("admin", admin=True)
    bob = await env.user("bob")
    http = await env.password_login("admin")
    await post(http, f"/admin/users/{bob}/oidc/expect", {"email": "not-an-email"})
    await post(http, f"/admin/users/{bob}/oidc/bind", {"subject": "has space"})
    row = await env.row(bob)
    assert (row.oidc_subject, row.oidc_expected_email) == (None, None)


async def test_csrf_missing_non_admin_and_oidc_off(env, monkeypatch):
    await env.user("admin", admin=True)
    bob = await env.user("bob")
    admin_http = await env.password_login("admin")
    bob_http = await env.password_login("bob")
    for mode, data in (("expect", {"email": "b@example.com"}), ("bind", {"subject": "s"}),
                       ("unlink", {})):
        path = f"/admin/users/{bob}/oidc/{mode}"
        assert (await admin_http.post(path, data=data)).status_code == 403  # no CSRF token
        assert (await post(bob_http, path, data)).status_code == 403        # not an admin
    monkeypatch.setattr(settings, "oidc_enabled", False)
    for mode in ("expect", "bind", "unlink"):
        response = await post(admin_http, f"/admin/users/{bob}/oidc/{mode}",
                              {"email": "b@example.com", "subject": "s"})
        assert response.status_code == 404
    row = await env.row(bob)
    assert (row.oidc_subject, row.oidc_expected_email) == (None, None)


async def test_unlink_revokes_sessions(env, caplog):
    await env.user("admin", admin=True)
    bob = await env.user("bob", bound="sub-bob")
    http = await env.password_login("admin")
    await env.sso_login("sub-bob")
    await env.sso_login("sub-bob")
    assert await env.live_sessions(bob) == 2
    with caplog.at_level(logging.INFO):
        response = await unlink(http, bob)
    assert response.status_code == 303
    row = await env.row(bob)
    assert (row.oidc_issuer, row.oidc_subject, row.oidc_linked_at) == (None, None, None)
    assert await env.live_sessions(bob) == 0
    removed = [r for r in caplog.records if r.getMessage() == "panel_oidc_link_removed"]
    assert len(removed) == 1 and removed[0].count == 2


async def test_self_unlink_refused_with_password_login_off(env):
    me = await env.user("admin", admin=True, bound="sub-admin")
    await env.user("other", admin=True, bound="sub-other")
    http = await env.sso_login("sub-admin")
    password_login_off(env)
    await assert_flashed(http, await unlink(http, me), "unlink your own account while password sign-in is")
    assert (await env.row(me)).oidc_subject == "sub-admin"


# ── the actor re-check, deterministic, once per route ───────────────────────


class Gate:
    """Hold the first call of `_lock_admin_guard` before or after the lock."""

    def __init__(self, env, when):
        self.real = users_module._lock_admin_guard
        self.when, self.armed = when, True
        self.arrived, self.release = asyncio.Event(), asyncio.Event()
        env.monkeypatch.setattr(users_module, "_lock_admin_guard", self)

    async def __call__(self, session):
        if not self.armed:
            return await self.real(session)
        self.armed = False
        if self.when == "before":
            self.arrived.set()
            await self.release.wait()
            return await self.real(session)
        await self.real(session)
        self.arrived.set()
        await self.release.wait()


@pytest.mark.parametrize("mode", ["expect", "bind", "unlink"])
async def test_actor_demoted_while_waiting_for_the_guard(env, caplog, mode):
    a = await env.user("alice", admin=True)
    await env.user("brian", admin=True)
    target = await env.user("bob", pending="bob@example.com" if mode == "unlink" else None)
    a_http = await env.password_login("alice")
    b_http = await env.password_login("brian")
    page = await a_http.get("/admin/account")
    token = g.csrf_from(page.text)
    gate = Gate(env, "before")
    data = {"csrf_token": token, "email": "new@example.com", "subject": "sub-new"}
    held = asyncio.create_task(a_http.post(f"/admin/users/{target}/oidc/{mode}", data=data))
    await asyncio.wait_for(gate.arrived.wait(), 10)
    demoted = await edit(b_http, a, admin=False, active=True)
    assert demoted.status_code == 303
    assert (await env.row(a)).is_admin is False
    gate.release.set()
    with caplog.at_level(logging.WARNING):
        response = await asyncio.wait_for(held, 10)
    assert response.status_code == 303 and response.headers["location"] == "/admin/users/"
    row = await env.row(target)
    assert row.oidc_subject is None
    assert row.oidc_expected_email == ("bob@example.com" if mode == "unlink" else None)
    assert any(r.getMessage() == "panel_forbidden" and getattr(r, "reason", None) == "actor_revoked"
               for r in caplog.records)


# ── D15 at each site ────────────────────────────────────────────────────────


async def _only_reachable(env):
    """A: an active admin who is *not* bound (signed in by password). B: the
    only bound admin. Password sign-in then disabled."""
    a = await env.user("alice", admin=True)
    b = await env.user("brian", admin=True, bound="sub-brian")
    http = await env.password_login("alice")
    password_login_off(env)
    return a, b, http


D15_TEXT = "would leave no active admin who can sign in with single sign-on"


async def assert_flashed(http, response, text_):
    page = await http.get(response.headers["location"])
    assert text_ in page.text


async def test_unlink_of_the_only_reachable_admin_is_refused(env):
    a, b, http = await _only_reachable(env)
    await assert_flashed(http, await unlink(http, b), D15_TEXT)
    assert (await env.row(b)).oidc_subject == "sub-brian"


@pytest.mark.parametrize("admin,active", [(False, True), (True, False)])
async def test_demote_or_deactivate_the_only_reachable_admin_is_refused(env, admin, active):
    a, b, http = await _only_reachable(env)
    await assert_flashed(http, await edit(http, b, admin=admin, active=active), D15_TEXT)
    row = await env.row(b)
    assert (row.is_admin, row.is_active) == (True, True)


@pytest.mark.parametrize("permanent", [False, True])
async def test_delete_of_the_only_reachable_admin_is_refused(env, permanent):
    a, b, http = await _only_reachable(env)
    await assert_flashed(http, await delete(http, b, permanent=permanent), D15_TEXT)
    row = await env.row(b)
    assert row is not None and row.is_active is True


@pytest.mark.parametrize("site", ["unlink", "demote", "deactivate", "soft", "permanent"])
@pytest.mark.parametrize("why", ["another_reachable", "password_enabled"])
async def test_each_site_proceeds_when_safe(env, site, why):
    a = await env.user("alice", admin=True)
    b = await env.user("brian", admin=True, bound="sub-brian")
    if why == "another_reachable":
        await env.user("carol", admin=True, bound="sub-carol")
    http = await env.password_login("alice")
    if why == "another_reachable":
        password_login_off(env)
    if site == "unlink":
        await unlink(http, b)
        assert (await env.row(b)).oidc_subject is None
    elif site in ("demote", "deactivate"):
        await edit(http, b, admin=site != "demote", active=site != "deactivate")
        row = await env.row(b)
        assert (row.is_admin and row.is_active) is False
    else:
        await delete(http, b, permanent=site == "permanent")
        row = await env.row(b)
        assert row is None if site == "permanent" else row.is_active is False


async def test_an_unbound_admin_does_not_count(env):
    a, b, http = await _only_reachable(env)
    # Alice is an active admin, but not bound: she cannot sign in with password
    # sign-in off, so she does not make Brian's demotion safe.
    assert await env.reachable() == 1
    await edit(http, b, admin=False, active=True)
    assert (await env.row(b)).is_admin is True


async def _none_reachable(env):
    """Zero reachable admins, password sign-in off (the boot CRITICAL state):
    Alice, an unbound admin signed in by password before it was disabled;
    Dave, a second unbound admin; Erin, an admin bound under a previous
    issuer; Olive, an ordinary active user with a pending expectation."""
    a = await env.user("alice", admin=True)
    d = await env.user("dave", admin=True)
    e = await env.user("erin", admin=True)
    async with env.maker() as session:
        await session.execute(update(User).where(User.id == e).values(
            oidc_issuer="https://old-issuer.example", oidc_subject="sub-erin",
            oidc_linked_at=text("now()")))
        await session.commit()
    o = await env.user("olive", pending="olive@example.com")
    http = await env.password_login("alice")
    password_login_off(env)
    assert await env.reachable() == 0
    return a, d, e, o, http


@pytest.mark.parametrize("site", ["deactivate", "soft", "permanent", "unlink"])
async def test_an_ordinary_user_is_not_held_by_d15_at_zero_reachable(env, site):
    a, d, e, o, http = await _none_reachable(env)
    if site == "deactivate":
        response = await edit(http, o, admin=False, active=False)
    elif site == "unlink":
        response = await unlink(http, o)
    else:
        response = await delete(http, o, permanent=site == "permanent")
    page = await http.get(response.headers["location"])
    assert D15_TEXT not in page.text
    row = await env.row(o)
    if site == "permanent":
        assert row is None
    elif site == "unlink":
        assert row.oidc_expected_email is None
    else:
        assert row.is_active is False


@pytest.mark.parametrize("site", ["demote", "deactivate", "soft", "permanent"])
async def test_an_unbound_admin_is_not_held_by_d15_at_zero_reachable(env, site):
    a, d, e, o, http = await _none_reachable(env)
    if site in ("demote", "deactivate"):
        response = await edit(http, d, admin=site != "demote", active=site != "deactivate")
    else:
        response = await delete(http, d, permanent=site == "permanent")
    page = await http.get(response.headers["location"])
    assert D15_TEXT not in page.text
    row = await env.row(d)
    if site == "permanent":
        assert row is None
    else:
        assert (row.is_admin and row.is_active) is False


@pytest.mark.parametrize("site", ["demote", "unlink"])
async def test_an_admin_under_a_previous_issuer_is_not_held_by_d15(env, site):
    a, d, e, o, http = await _none_reachable(env)
    response = await (unlink(http, e) if site == "unlink"
                      else edit(http, e, admin=False, active=True))
    page = await http.get(response.headers["location"])
    assert D15_TEXT not in page.text
    row = await env.row(e)
    if site == "unlink":
        assert row.oidc_subject is None
    else:
        assert row.is_admin is False


# ── D15 races, deterministic ────────────────────────────────────────────────


async def _two_bound(env):
    a = await env.user("alice", admin=True, bound="sub-alice")
    b = await env.user("brian", admin=True, bound="sub-brian")
    a_http = await env.sso_login("sub-alice")
    b_http = await env.sso_login("sub-brian")
    password_login_off(env)
    return a, b, a_http, b_http


async def _race(env, first, second):
    gate = Gate(env, "after")
    held = asyncio.create_task(first())
    await asyncio.wait_for(gate.arrived.wait(), 10)
    queued = asyncio.create_task(second())
    await asyncio.sleep(0.3)  # let it reach the guard; the lock orders it regardless
    gate.release.set()
    return await asyncio.wait_for(held, 10), await asyncio.wait_for(queued, 10)


async def test_race_a_unlinks_b_vs_b_deactivates_a(env):
    a, b, a_http, b_http = await _two_bound(env)
    await _race(env, lambda: unlink(a_http, b), lambda: edit(b_http, a, admin=True, active=False))
    ra, rb = await env.row(a), await env.row(b)
    unlinked_b = rb.oidc_subject is None
    deactivated_a = ra.is_active is False
    assert unlinked_b != deactivated_a  # exactly one succeeded
    assert unlinked_b  # the first held the guard
    assert await env.reachable() == 1


async def test_race_a_demotes_b_vs_b_deletes_a(env):
    a, b, a_http, b_http = await _two_bound(env)
    await _race(env, lambda: edit(a_http, b, admin=False, active=True),
                lambda: delete(b_http, a, permanent=True))
    ra, rb = await env.row(a), await env.row(b)
    demoted_b = rb.is_admin is False
    deleted_a = ra is None
    assert demoted_b != deleted_a and demoted_b
    assert await env.reachable() == 1


async def test_race_two_unlinks_of_each_other(env):
    a, b, a_http, b_http = await _two_bound(env)
    await _race(env, lambda: unlink(a_http, b), lambda: unlink(b_http, a))
    ra, rb = await env.row(a), await env.row(b)
    assert (ra.oidc_subject is None) != (rb.oidc_subject is None)
    assert rb.oidc_subject is None
    assert await env.reachable() == 1
