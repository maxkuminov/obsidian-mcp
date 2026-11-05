"""OIDC-off regression goldens, offline half (oidc-account-linking D16, task 0.3).

Captured on the base commit (bd9b386) before any linking slice. Each case runs
`src.main.app` — the real middleware, the real routers — over a fake database
that answers only what the case needs, in the D16 matrix: OIDC off in
single-user mode, OIDC off in multi-user mode, OIDC on in multi-user mode
(#415's settings). The rendered response is normalized and compared byte for
byte to a file in `tests/golden/oidc_off/`.

Here, the cases whose output depends on no stored row beyond a count or a
single lookup:

* D16 item 1 — the unauthenticated `GET /authorize` for a DCR client and a
  CIMD client (the 302 to login in multi-user mode; the consent page itself in
  single-user mode, which has no login);
* D16 item 4 — `/admin/register` GET with an empty and a populated users
  table, POST with a populated one (the closed-once-users-exist redirect), and
  OIDC on → 404 for both;
* D16 item 5 — the login page.

The database half — authenticated consent, the approval's lineage, the
empty-table bootstrap POST, the users list, user edit and account pages — is
`tests/integration/test_oidc_off_consent_pg.py`.

**Later slices may only run this file.** A diff here, or in a golden, is the
regression D16 exists to catch.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from src.database import get_session
from src.limiter import limiter
from src.models.db import OAuthClient
from src.oauth import cimd
from tests.golden.oidc_off import _support as g

MODES = list(g.MODES)

DCR_CLIENT_ID = "a3f19c7e5b2d4081a3f19c7e5b2d4081"
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
CHALLENGE = "a" * 43


# ── fake database ───────────────────────────────────────────────────────────


class _Result:
    rowcount = 0

    def __init__(self, entity=None, count=0):
        self._entity = entity
        self._count = count

    def scalar(self):
        return self._count

    def scalar_one_or_none(self):
        return self._entity

    def scalars(self):
        return self

    def all(self):
        return []

    def first(self):
        return None

    def one_or_none(self):
        return None


class _Session:
    """Answers a whole-entity `select(<class>)` from `lookups`, a count from
    `count`, and everything else empty. A column select (`select(X.col)`)
    resolves nothing, so the CIMD page's prior-approval lookup is "never"."""

    def __init__(self, lookups=None, count=0):
        self.lookups = lookups or {}
        self.count = count

    async def execute(self, stmt, *args, **kwargs):
        entity = None
        try:
            descriptions = stmt.column_descriptions
        except Exception:
            descriptions = []
        if len(descriptions) == 1 and descriptions[0].get("expr") is descriptions[0].get("entity"):
            entity = self.lookups.get(descriptions[0].get("entity"))
        return _Result(entity, self.count)

    async def scalar(self, *args, **kwargs):
        return self.count

    def add(self, *args, **kwargs):
        pass

    async def flush(self):
        pass

    async def commit(self):
        pass

    async def rollback(self):
        pass

    async def close(self):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _dcr_client():
    return SimpleNamespace(
        client_id=DCR_CLIENT_ID,
        client_name="Golden Client",
        scope="read readwrite offline_access",
        redirect_uris=[DCR_REDIRECT],
        user_id=None,
        created_at=None,
    )


@pytest.fixture
def vault(tmp_path):
    path = tmp_path / "vault"
    path.mkdir()
    return str(path)


def r(response, vault):
    return g.render(response, paths={vault: "<VAULT>"})


@pytest.fixture
def harness(monkeypatch, vault):
    """A factory: `harness(mode, count=…)` → an AsyncClient on the real app."""
    import sys

    from src.database import async_session as real_async_session

    stack = []
    state = {}

    def _factory(mode: str, *, count: int = 0):
        g.apply_mode(monkeypatch, mode)
        from src.config import settings

        monkeypatch.setattr(settings, "vault_path", vault)
        state["db"] = _Session({OAuthClient: _dcr_client()}, count=count)

        def _fake_async_session():
            return state["db"]

        async def _get_session():
            yield state["db"]

        for module in list(sys.modules.values()):
            if (getattr(module, "__name__", "").startswith("src.")
                    and getattr(module, "async_session", None) is real_async_session):
                monkeypatch.setattr(module, "async_session", _fake_async_session)
        ctx = g.mounted_app(mode)
        app = ctx.__enter__()
        stack.append(ctx)
        app.dependency_overrides[get_session] = _get_session
        return AsyncClient(transport=ASGITransport(app), base_url=g.BASE)

    limiter.reset()
    cimd.reset_state_for_tests()
    yield _factory
    for ctx in reversed(stack):
        ctx.__exit__(None, None, None)
    cimd.reset_state_for_tests()
    limiter.reset()


def _prime_cimd():
    projection = cimd.validate_document(CIMD_URL, CIMD_DOC)
    cimd._cache_put(CIMD_URL, projection, None)


def authorize_params(kind: str) -> dict:
    if kind == "dcr":
        client_id, redirect = DCR_CLIENT_ID, DCR_REDIRECT
    else:
        client_id, redirect = CIMD_URL, CIMD_REDIRECT
    return {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect,
        "code_challenge": CHALLENGE,
        "code_challenge_method": "S256",
        "scope": "read offline_access",
        "state": "client-state-golden",
    }


# ── D16 item 1: the unauthenticated /authorize ──────────────────────────────


@pytest.mark.parametrize("kind", ["dcr", "cimd"])
@pytest.mark.parametrize("mode", MODES)
async def test_unauthenticated_authorize(harness, vault, mode, kind):
    async with harness(mode) as http:
        if kind == "cimd":
            _prime_cimd()
        response = await http.get("/authorize", params=authorize_params(kind))
    if mode == "off_single":
        assert response.status_code == 200
    else:
        assert response.status_code == 302
        assert response.headers["location"].startswith("/admin/auth/login?next=%2Fauthorize%3F")
    g.check(f"authorize_unauth_{kind}_{mode}", r(response, vault))


@pytest.mark.parametrize("kind", ["dcr", "cimd"])
async def test_unauthenticated_authorize_redirect_is_mode_independent(harness, vault, kind):
    """D16: in the OIDC-on column item 1 equals the OIDC-off multi-user column."""
    rendered = {}
    for mode in g.MULTI_MODES:
        async with harness(mode) as http:
            if kind == "cimd":
                _prime_cimd()
            rendered[mode] = g.render(await http.get("/authorize", params=authorize_params(kind)))
    assert rendered["off_multi"] == rendered["on_multi"]


# ── D16 item 4: /admin/register (closed and OIDC-on halves) ─────────────────


@pytest.mark.parametrize("populated", [False, True], ids=["empty", "populated"])
@pytest.mark.parametrize("mode", MODES)
async def test_register_get(harness, vault, mode, populated):
    async with harness(mode, count=1 if populated else 0) as http:
        response = await http.get("/admin/register")
    expected = {"off_single": 404, "on_multi": 404}.get(mode, 302 if populated else 200)
    assert response.status_code == expected
    g.check(f"register_get_{'populated' if populated else 'empty'}_{mode}", r(response, vault))


@pytest.mark.parametrize("mode", MODES)
async def test_register_post_populated(harness, vault, mode):
    """The closed-once-users-exist redirect (OIDC off), 404 (OIDC on, single-user)."""
    async with harness(mode, count=1) as http:
        token = ""
        if mode != "off_single":
            login = await http.get("/admin/auth/login")
            token = g.csrf_from(login.text)
        response = await http.post("/admin/register", data={
            "csrf_token": token,
            "username": "second",
            "password": "a-long-enough-password",
            "password_confirm": "a-long-enough-password",
            "vault_path": vault,
        })
    expected = {"off_single": 404, "on_multi": 404}.get(mode, 302)
    assert response.status_code == expected
    g.check(f"register_post_populated_{mode}", r(response, vault))


async def test_register_post_oidc_on_empty_table_is_404(harness, vault):
    async with harness("on_multi", count=0) as http:
        token = g.csrf_from((await http.get("/admin/auth/login")).text)
        response = await http.post("/admin/register", data={
            "csrf_token": token,
            "username": "first",
            "password": "a-long-enough-password",
            "password_confirm": "a-long-enough-password",
            "vault_path": vault,
        })
    assert response.status_code == 404
    g.check("register_post_empty_on_multi", r(response, vault))


# ── D16 item 5: the login page ──────────────────────────────────────────────


@pytest.mark.parametrize("mode", MODES)
async def test_login_page(harness, vault, mode):
    async with harness(mode, count=1) as http:
        response = await http.get("/admin/auth/login")
        with_next = await http.get(
            "/admin/auth/login", params={"next": "/authorize?client_id=x&scope=read"}
        )
    assert response.status_code == (404 if mode == "off_single" else 200)
    g.check(f"login_{mode}", r(response, vault))
    g.check(f"login_next_{mode}", r(with_next, vault))


def test_goldens_carry_no_unnormalized_secret():
    """No golden may hold a raw code, the provider secret or a password."""
    import re

    for path in g.GOLDEN_DIR.glob("*.golden"):
        text = path.read_text("utf-8")
        assert not re.search(r"\bcode=[0-9a-f]{64}\b", text), path.name
        assert "test-client-secret" not in text, path.name
        assert "golden-admin-password" not in text, path.name
        assert "a-long-enough-password" not in text, path.name
