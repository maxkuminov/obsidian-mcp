"""The panel Content-Security-Policy header, through the real app (#195).

Every HTML response rendered by the panel, auth and consent template instances
must carry the nonce policy; nothing else may gain it; an enforcing policy a
route already set (the transfer pages) must survive byte-for-byte. All of it is
asserted through `src.main.app` — the real middleware stack, the real
`add_security_headers` — with the database replaced by a permissive fake and
the panel's user dependency overridden.

**Route metadata is a starting list, not an exhaustive HTML inventory.** It
misses HTML rendered by a route declared without `response_class`, and it
misses the error renders — the login page's 401 and the bootstrap register
page's 400 are HTML from `POST` routes that declare nothing. So the cases here
come from three sources: (1) every `APIRoute` declared with an HTML response
class, with an inventory assertion that fails if one is not exercised; (2)
`GET /authorize` for a registered client; (3) explicit method/status cases.
Coverage of a future panel route that renders HTML without declaring it rests
on the design, not on this list: the policy is keyed on a marker the templates'
context processor sets, so such a route is covered without appearing here.

**The body-nonce equality check depends on the template slice.** Until the
templates write `nonce="{{ csp_nonce }}"` (Slice A of `panel-csp`), a rendered
body carries no nonce to compare. `test_body_nonces_equal_the_header_nonce`
therefore *skips* — with a reason naming Slice A — while `base.html` on disk
does not reference `csp_nonce`, and is a real assertion on every page the
moment it does. The skip keys on the template source, not on the rendered
body, so a page that loses its nonces after the merge fails rather than skips.
"""
from __future__ import annotations

import ast
import logging
import pathlib
import re
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from src import main
from src.auth import routes as auth_routes
from src.config import settings
from src.control_panel import routes as panel_routes
from src.control_panel import users as users_routes
from src.database import get_session
from src.limiter import limiter
from src.models.db import OAuthClient, User
from src.oauth import routes as oauth_routes
from src.services import panel_csp

ROOT = pathlib.Path(__file__).resolve().parent.parent
TEMPLATES = ROOT / "src" / "control_panel" / "templates"

NONCE_RE = re.compile(r"^[A-Za-z0-9_-]{22,}$")
BODY_NONCE_RE = re.compile(r'\bnonce="([^"]*)"')

#: Whether the templates have been converted to carry the nonce (Slice A).
SLICE_A_LANDED = "csp_nonce" in (TEMPLATES / "base.html").read_text(encoding="utf-8")

VALID_PKCE_CHALLENGE = "a" * 43
CLIENT_ID = "a3f19c7e5b2d4081a3f19c7e5b2d4081"
REDIRECT_URI = "https://client.example/callback"

# The D4 directive set, in order, with `N` for the nonce.
EXPECTED_DIRECTIVES = [
    ("default-src", ["'self'"]),
    ("script-src", ["'nonce-N'"]),
    ("style-src", ["https://fonts.googleapis.com", "'unsafe-inline'"]),
    ("style-src-elem", ["'nonce-N'", "https://fonts.googleapis.com"]),
    ("style-src-attr", ["'unsafe-inline'"]),
    ("img-src", ["'self'", "data:"]),
    ("font-src", ["https://fonts.gstatic.com"]),
    ("connect-src", ["'self'"]),
    ("object-src", ["'none'"]),
    ("base-uri", ["'none'"]),
    ("frame-ancestors", ["'none'"]),
    ("form-action", None),  # checked per page
]


# ── fakes ───────────────────────────────────────────────────────────────────


def _admin() -> User:
    user = User(
        username="admin",
        password_hash="x",
        is_admin=True,
        is_active=True,
        vault_path="/tmp/test-vault",
    )
    user.id = 1
    user.session_version = 1
    return user


def _client() -> SimpleNamespace:
    return SimpleNamespace(
        client_id=CLIENT_ID,
        client_name="Test Client",
        scope="read readwrite offline_access",
        redirect_uris=[REDIRECT_URI],
        created_at=None,
    )


class _Result:
    """Empty for everything, except a single-entity lookup the test primed."""

    rowcount = 0

    def __init__(self, entity=None):
        self._entity = entity

    def scalar(self):
        return None

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

    def mappings(self):
        return self

    def fetchall(self):
        return []

    def __iter__(self):
        return iter([])


class _Session:
    """A permissive `AsyncSession` stand-in: every read comes back empty.

    `lookups` maps an ORM class to what a `select(<that class>)` resolves to,
    which is how the user-edit page finds its target and `/authorize` its
    client. Nothing here is under test — the pages only have to render.
    """

    def __init__(self, lookups=None):
        self._lookups = lookups or {}

    async def execute(self, stmt, *args, **kwargs):
        entity = None
        try:
            descriptions = stmt.column_descriptions
        except Exception:
            descriptions = []
        if len(descriptions) == 1:
            entity = self._lookups.get(descriptions[0].get("entity"))
        return _Result(entity)

    async def scalar(self, *args, **kwargs):
        return None

    async def get(self, *args, **kwargs):
        return None

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


# ── harness ─────────────────────────────────────────────────────────────────


def _auth_router_included() -> bool:
    return any(
        getattr(route, "original_router", None) is auth_routes.router
        for route in main.app.router.routes
    )


@pytest.fixture
def client(monkeypatch):
    """`src.main.app`, with a fake database and a signed-in administrator.

    The auth routes are mounted by `src/main.py` only in multi-user mode, and
    the suite imports the app in single-user mode, so they are included here
    for the duration of the test and removed afterwards — the same router
    object production includes, behind the same middleware.
    """
    lookups = {User: _admin(), OAuthClient: _client()}

    def _fake_session():
        return _Session(lookups)

    async def _get_session():
        yield _fake_session()

    async def _panel_user():
        return lookups[User]

    for module in (panel_routes, users_routes, oauth_routes, auth_routes):
        if hasattr(module, "async_session"):
            monkeypatch.setattr(module, "async_session", _fake_session)
    monkeypatch.setattr(settings, "panel_csp", "enforce")

    overrides = dict(main.app.dependency_overrides)
    main.app.dependency_overrides[get_session] = _get_session
    main.app.dependency_overrides[panel_routes.require_user_panel] = _panel_user

    before = list(main.app.router.routes)
    if not _auth_router_included():
        main.app.include_router(auth_routes.router)
    limiter.reset()
    try:
        yield TestClient(main.app, base_url="https://localhost")
    finally:
        main.app.router.routes[:] = before
        main.app.dependency_overrides.clear()
        main.app.dependency_overrides.update(overrides)
        limiter.reset()


def _walk(routes):
    for route in routes:
        original = getattr(route, "original_router", None)
        if original is not None:
            yield from _walk(original.routes)
        elif isinstance(route, APIRoute):
            yield route


def _declares_html(route: APIRoute) -> bool:
    response_class = getattr(route.response_class, "value", route.response_class)
    return isinstance(response_class, type) and issubclass(response_class, HTMLResponse)


def _html_route_inventory() -> set[tuple[str, str]]:
    routes = list(_walk(main.app.router.routes))
    # Mounted by `src/main.py` only in multi-user mode; part of the surface.
    routes += [r for r in auth_routes.router.routes if isinstance(r, APIRoute)]
    inventory = set()
    for route in routes:
        if _declares_html(route):
            for method in route.methods:
                inventory.add((method, route.path))
    return inventory


# ── the cases ───────────────────────────────────────────────────────────────


def _csrf_from(body: str) -> str:
    match = re.search(r'name="csrf_token"\s+value="([^"]+)"', body)
    assert match, "the page rendered no CSRF token"
    return match.group(1)


def _get(path, *, multi_user=True, params=None):
    def run(client, monkeypatch):
        monkeypatch.setattr(settings, "multi_user_mode", multi_user)
        return client.get(path, params=params, follow_redirects=False)
    return run


def _authorize_get(client, monkeypatch):
    # Single-user mode: the consent page renders without a panel session.
    monkeypatch.setattr(settings, "multi_user_mode", False)
    return client.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": CLIENT_ID,
            "redirect_uri": REDIRECT_URI,
            "code_challenge": VALID_PKCE_CHALLENGE,
            "code_challenge_method": "S256",
        },
        follow_redirects=False,
    )


def _login_401(client, monkeypatch):
    monkeypatch.setattr(settings, "multi_user_mode", True)
    page = client.get("/admin/auth/login")
    assert page.status_code == 200
    # No row resolves, so the failure is `unknown_user`: no bcrypt, no
    # per-account budget, the plain 401 re-render of `login.html`.
    main.app.dependency_overrides[get_session] = _empty_session
    return client.post(
        "/admin/auth/login",
        data={
            "csrf_token": _csrf_from(page.text),
            "username": "nobody",
            "password": "wrong-password",
            "next": "/admin/",
        },
        follow_redirects=False,
    )


def _register_400(client, monkeypatch):
    monkeypatch.setattr(settings, "multi_user_mode", True)
    main.app.dependency_overrides[get_session] = _empty_session
    page = client.get("/admin/register")
    assert page.status_code == 200
    return client.post(
        "/admin/register",
        data={
            "csrf_token": _csrf_from(page.text),
            "username": "Not A Valid Name!",
            "password": "irrelevant",
            "password_confirm": "irrelevant",
            "vault_path": "/tmp/test-vault",
        },
        follow_redirects=False,
    )


async def _empty_session():
    """A database with no rows at all: bootstrap is open, no user resolves."""
    yield _Session()


def _register_get(client, monkeypatch):
    monkeypatch.setattr(settings, "multi_user_mode", True)
    main.app.dependency_overrides[get_session] = _empty_session
    return client.get("/admin/register", follow_redirects=False)


# (case id, route key or None, runner, expected status, consent?)
CASES = [
    ("dashboard", ("GET", "/admin/"), _get("/admin/"), 200, False),
    ("account", ("GET", "/admin/account"), _get("/admin/account"), 200, False),
    ("keys", ("GET", "/admin/keys"), _get("/admin/keys"), 200, False),
    ("oauth", ("GET", "/admin/oauth"), _get("/admin/oauth"), 200, False),
    ("usage", ("GET", "/admin/usage"), _get("/admin/usage"), 200, False),
    ("performance", ("GET", "/admin/performance"), _get("/admin/performance"), 200, False),
    ("health", ("GET", "/admin/health"), _get("/admin/health"), 200, False),
    (
        "search-analytics",
        ("GET", "/admin/search-analytics"),
        _get("/admin/search-analytics"),
        200,
        False,
    ),
    ("vault", ("GET", "/admin/vault"), _get("/admin/vault"), 200, False),
    ("settings", ("GET", "/admin/settings"), _get("/admin/settings"), 200, False),
    (
        "reembed-confirm",
        ("GET", "/admin/settings/reembed"),
        _get("/admin/settings/reembed"),
        200,
        False,
    ),
    ("users", ("GET", "/admin/users/"), _get("/admin/users/"), 200, False),
    (
        "user-edit",
        ("GET", "/admin/users/{user_id}/edit"),
        _get("/admin/users/1/edit"),
        200,
        False,
    ),
    ("login", ("GET", "/admin/auth/login"), _get("/admin/auth/login"), 200, False),
    ("register", ("GET", "/admin/register"), _register_get, 200, False),
    ("authorize", ("GET", "/authorize"), _authorize_get, 200, True),
    ("login-401", None, _login_401, 401, False),
    ("register-400", None, _register_400, 400, False),
]

CASE_IDS = [case[0] for case in CASES]


def _run(case, client, monkeypatch):
    _, _, runner, status, _ = case
    response = runner(client, monkeypatch)
    assert response.status_code == status, (response.status_code, response.text[:300])
    assert response.headers["content-type"].startswith("text/html")
    return response


def _parse(policy: str) -> list[tuple[str, list[str]]]:
    directives = []
    for part in policy.split(";"):
        tokens = part.split()
        if tokens:
            directives.append((tokens[0], tokens[1:]))
    return directives


def _nonce_of(policy: str) -> str:
    sources = dict(_parse(policy))["script-src"]
    assert len(sources) == 1, sources
    match = re.fullmatch(r"'nonce-([^']+)'", sources[0])
    assert match, sources
    return match.group(1)


# ── inventory ───────────────────────────────────────────────────────────────


def test_every_html_declared_route_is_exercised():
    """A route declared with an HTML response class and not in `CASES` fails.

    Route metadata is where the list starts, not where it ends — see the
    module docstring for the renders it cannot see.
    """
    covered = {case[1] for case in CASES if case[1] is not None}
    missing = _html_route_inventory() - covered
    assert not missing, f"HTML routes with no header case: {sorted(missing)}"


# ── the header on every panel-surface response ──────────────────────────────


@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
def test_panel_surface_carries_the_enforced_policy(case, client, monkeypatch):
    response = _run(case, client, monkeypatch)
    consent = case[4]

    policy = response.headers.get("content-security-policy")
    assert policy, "no Content-Security-Policy on a panel-surface response"
    assert "content-security-policy-report-only" not in response.headers

    nonce = _nonce_of(policy)
    assert NONCE_RE.match(nonce), nonce

    # Exactly the D4 set, in order, nothing more.
    parsed = _parse(policy)
    assert [name for name, _ in parsed] == [name for name, _ in EXPECTED_DIRECTIVES]
    for (name, sources), (_, expected) in zip(parsed, EXPECTED_DIRECTIVES):
        if expected is None:
            continue
        assert sources == [s.replace("N", nonce) if s == "'nonce-N'" else s for s in expected], name
    directives = dict(parsed)
    assert directives["script-src"] == [f"'nonce-{nonce}'"]
    for forbidden in ("'unsafe-inline'", "'unsafe-eval'", "'unsafe-hashes'"):
        assert forbidden not in directives["script-src"]
    assert "'unsafe-inline'" not in directives["style-src-elem"]
    assert f"'nonce-{nonce}'" not in directives["style-src"]

    if consent:
        assert directives["form-action"] == ["'self'", "https:"]
    else:
        assert directives["form-action"] == ["'self'"]

    assert policy == panel_csp.build_policy(nonce, consent=consent)

    # The four headers that were there before are still there.
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert "strict-transport-security" in response.headers


@pytest.mark.skipif(
    not SLICE_A_LANDED,
    reason=(
        "panel-csp Slice A (templates writing nonce=\"{{ csp_nonce }}\") has not "
        "landed on this tree; this becomes a real assertion once base.html "
        "references csp_nonce"
    ),
)
@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
def test_body_nonces_equal_the_header_nonce(case, client, monkeypatch):
    response = _run(case, client, monkeypatch)
    nonce = _nonce_of(response.headers["content-security-policy"])
    body = response.text

    assert re.search(r"<script\b[^>]*\bnonce=\"", body), "no nonced <script>"
    assert re.search(r"<style\b[^>]*\bnonce=\"", body), "no nonced <style>"
    found = BODY_NONCE_RE.findall(body)
    assert found and set(found) == {nonce}, (nonce, sorted(set(found)))


@pytest.mark.parametrize(
    "path", ["/admin/keys", "/admin/auth/login"], ids=["panel", "login"]
)
def test_the_nonce_is_fresh_per_response(path, client, monkeypatch):
    monkeypatch.setattr(settings, "multi_user_mode", True)
    first = client.get(path, follow_redirects=False)
    second = client.get(path, follow_redirects=False)
    assert first.status_code == second.status_code == 200
    a = _nonce_of(first.headers["content-security-policy"])
    b = _nonce_of(second.headers["content-security-policy"])
    assert a != b


# ── the mode switch ─────────────────────────────────────────────────────────


def test_report_only_moves_the_value_to_the_report_only_header(client, monkeypatch):
    monkeypatch.setattr(settings, "panel_csp", "report-only")
    response = _authorize_get(client, monkeypatch)
    assert response.status_code == 200
    assert "content-security-policy" not in response.headers
    policy = response.headers["content-security-policy-report-only"]
    assert policy == panel_csp.build_policy(_nonce_of(policy), consent=True)


def test_report_only_on_a_panel_page(client, monkeypatch):
    monkeypatch.setattr(settings, "panel_csp", "report-only")
    monkeypatch.setattr(settings, "multi_user_mode", True)
    response = client.get("/admin/keys")
    assert response.status_code == 200
    assert "content-security-policy" not in response.headers
    policy = response.headers["content-security-policy-report-only"]
    assert policy == panel_csp.build_policy(_nonce_of(policy), consent=False)


def test_off_sends_neither_header(client, monkeypatch):
    monkeypatch.setattr(settings, "panel_csp", "off")
    monkeypatch.setattr(settings, "multi_user_mode", True)
    for path in ("/admin/keys", "/admin/auth/login"):
        response = client.get(path)
        assert response.status_code == 200
        assert "content-security-policy" not in response.headers
        assert "content-security-policy-report-only" not in response.headers


# ── outside the surface ─────────────────────────────────────────────────────


def _transfer_policy(nonce: str) -> str:
    """The policy `src/transfer/routes.py::_page` sets, spelled independently."""
    return (
        "default-src 'none'; "
        f"script-src 'nonce-{nonce}'; "
        f"style-src 'nonce-{nonce}'; "
        "connect-src 'self'; "
        "form-action 'none'; "
        "base-uri 'none'; "
        "frame-ancestors 'none'"
    )


@pytest.mark.parametrize("mode", ["enforce", "report-only", "off"])
@pytest.mark.parametrize("method", ["GET", "HEAD"])
@pytest.mark.parametrize("path", ["/transfer/upload", "/transfer/download"])
def test_transfer_pages_keep_their_own_policy(path, method, mode, client, monkeypatch):
    monkeypatch.setattr(settings, "panel_csp", mode)
    limiter.reset()
    response = client.request(method, path)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    policy = response.headers["content-security-policy"]
    nonce = _nonce_of(policy)
    assert policy == _transfer_policy(nonce)
    assert "content-security-policy-report-only" not in response.headers


def test_docs_get_no_panel_policy(client):
    response = client.get("/docs")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "content-security-policy" not in response.headers
    assert "content-security-policy-report-only" not in response.headers


def test_a_json_response_gains_no_policy(client, monkeypatch):
    """An OAuth validation error from `GET /authorize` is JSON: no policy."""
    monkeypatch.setattr(settings, "multi_user_mode", False)
    response = client.get(
        "/authorize",
        params={
            "response_type": "token",
            "client_id": CLIENT_ID,
            "redirect_uri": REDIRECT_URI,
            "code_challenge": VALID_PKCE_CHALLENGE,
        },
    )
    assert response.status_code == 400
    assert response.headers["content-type"].startswith("application/json")
    assert "content-security-policy" not in response.headers
    assert "content-security-policy-report-only" not in response.headers


# ── pre-existing headers on a marked response ───────────────────────────────


def _probe_app(extra_headers: dict[str, str]) -> TestClient:
    """A route that renders through a panel template instance, then sets a
    header of its own — behind the real `add_security_headers`."""
    app = FastAPI()
    app.middleware("http")(main.add_security_headers)

    @app.get("/probe")
    async def probe(request: Request):
        # `template_context` is exactly what the panel instances run while
        # rendering; calling it marks the request the same way.
        panel_csp.template_context(request)
        response = HTMLResponse("<!doctype html><title>probe</title>")
        for name, value in extra_headers.items():
            response.headers[name] = value
        return response

    return TestClient(app)


def test_a_report_only_header_does_not_suppress_enforcement(monkeypatch):
    monkeypatch.setattr(settings, "panel_csp", "enforce")
    route_value = "default-src 'none'; report-uri /elsewhere"
    response = _probe_app({"Content-Security-Policy-Report-Only": route_value}).get("/probe")
    policy = response.headers["content-security-policy"]
    assert policy == panel_csp.build_policy(_nonce_of(policy), consent=False)
    # The route's own report-only value is left where it was.
    assert response.headers["content-security-policy-report-only"] == route_value


def test_an_existing_enforcing_policy_is_left_byte_for_byte(monkeypatch):
    route_value = "default-src 'none';  script-src 'nonce-abc'"
    for mode in ("enforce", "report-only"):
        monkeypatch.setattr(settings, "panel_csp", mode)
        response = _probe_app({"Content-Security-Policy": route_value}).get("/probe")
        assert response.headers.get_list("content-security-policy") == [route_value]
        assert "content-security-policy-report-only" not in response.headers


def test_an_unmarked_html_response_gets_nothing(monkeypatch):
    app = FastAPI()
    app.middleware("http")(main.add_security_headers)

    @app.get("/plain")
    async def plain():
        return HTMLResponse("<!doctype html><title>plain</title>")

    response = TestClient(app).get("/plain")
    assert "content-security-policy" not in response.headers


# ── the policy builder ──────────────────────────────────────────────────────


def test_build_policy_refuses_a_nonce_it_did_not_generate():
    for bad in ("", "short", "a" * 22 + ";script-src *", "a" * 22 + "'", "a" * 22 + "\r\nX: y"):
        with pytest.raises(ValueError):
            panel_csp.build_policy(bad, consent=False)


def test_build_policy_interpolates_nothing_but_the_nonce():
    nonce = "A" * 22
    other = panel_csp.build_policy(nonce, consent=False)
    consent = panel_csp.build_policy(nonce, consent=True)
    assert other.endswith("form-action 'self'")
    assert consent.endswith("form-action 'self' https:")
    assert other.replace(nonce, "N") == panel_csp.build_policy("B" * 22, consent=False).replace("B" * 22, "N")


# ── every panel template instance registers the processor ──────────────────


def _template_constructions():
    for path in sorted((ROOT / "src").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            if name == "Jinja2Templates":
                yield path.relative_to(ROOT).as_posix(), node


def _registers_processor(call: ast.Call) -> bool:
    for keyword in call.keywords:
        if keyword.arg != "context_processors" or not isinstance(keyword.value, ast.List):
            continue
        for element in keyword.value.elts:
            if (
                isinstance(element, ast.Attribute)
                and element.attr == "template_context"
                and isinstance(element.value, ast.Name)
                and element.value.id == "panel_csp"
            ):
                return True
    return False


def test_every_panel_template_instance_registers_the_context_processor():
    constructions = list(_template_constructions())
    files = {path for path, _ in constructions}
    assert {
        "src/control_panel/routes.py",
        "src/control_panel/users.py",
        "src/auth/routes.py",
        "src/oauth/routes.py",
        "src/transfer/routes.py",
    } <= files, files
    for path, call in constructions:
        if path == "src/transfer/routes.py":
            # The transfer pages set their own, stricter policy; the panel
            # marker must never reach them.
            assert not _registers_processor(call), path
        else:
            assert _registers_processor(call), f"{path} does not register panel_csp.template_context"


def test_only_the_consent_route_marks_consent():
    hits = []
    for path in sorted((ROOT / "src").rglob("*.py")):
        if "mark_consent(" in path.read_text(encoding="utf-8"):
            hits.append(path.relative_to(ROOT).as_posix())
    assert hits == ["src/oauth/routes.py", "src/services/panel_csp.py"], hits
