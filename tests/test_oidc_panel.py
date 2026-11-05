"""Panel side of OIDC account linking, offline half (#416 slice C, task 3.10).

What needs no database: the link-status helpers, the rendered card / column /
status line under the nonce CSP, the login button label, the handler's order
and refusals with a stubbed session and stubbed link operations, and the 404
with OIDC off. The real-database half — each action's success, conflict and
re-point refusal, CSRF and non-admin refusals, session revocation, the
deterministic actor re-check per route and the D15 rule — is
`tests/integration/test_oidc_reachable_admin_pg.py`.
"""
from __future__ import annotations

import datetime as dt
import logging
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from starlette.requests import Request

import src.main  # noqa: F401  (configures logging at import; before any caplog)
from src.config import settings
from src.control_panel import routes as panel_routes
from src.control_panel import users
from src.models.db import User
from src.services import oidc_links

ISSUER = "https://identity.example.test/realms/home"
NOW = dt.datetime.now(dt.timezone.utc)


@pytest.fixture(autouse=True)
def oidc_on(monkeypatch):
    for name, value in dict(oidc_enabled=True, multi_user_mode=True, oidc_issuer=ISSUER,
                            local_login_enabled=True, oidc_display_name="SSO").items():
        monkeypatch.setattr(settings, name, value)


def make_user(**columns) -> User:
    base = dict(id=5, username="bob", is_admin=False, is_active=True, password_hash="x",
                session_version=1, oidc_issuer=None, oidc_subject=None, oidc_linked_at=None,
                oidc_expected_issuer=None, oidc_expected_email=None, oidc_expected_set_at=None)
    base.update(columns)
    return User(**base)


def request(path="/admin/users/5/oidc/expect"):
    return Request({"type": "http", "method": "POST", "scheme": "https", "path": path,
                    "query_string": b"", "headers": [(b"host", b"notes.example.test")],
                    "client": ("192.0.2.10", 1234), "server": ("notes.example.test", 443),
                    "session": {}})


# ── status helpers ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("columns,list_state,card_state", [
    ({}, None, "none"),
    (dict(oidc_issuer=ISSUER, oidc_subject="0123456789abcdef", oidc_linked_at=NOW), "linked", "linked"),
    (dict(oidc_issuer="https://old.example.test", oidc_subject="s", oidc_linked_at=NOW), None, "foreign"),
    (dict(oidc_expected_issuer=ISSUER, oidc_expected_email="bob@example.com",
          oidc_expected_set_at=NOW - dt.timedelta(days=1)), "pending", "pending"),
    (dict(oidc_expected_issuer=ISSUER, oidc_expected_email="bob@example.com",
          oidc_expected_set_at=NOW - dt.timedelta(days=8)), None, "expired"),
    (dict(oidc_expected_issuer="https://old.example.test", oidc_expected_email="bob@example.com",
          oidc_expected_set_at=NOW), None, "expired"),
])
def test_link_status(columns, list_state, card_state):
    user = make_user(**columns)
    assert users._oidc_list_state(user) == list_state
    card = users._oidc_card_state(user)
    assert card["state"] == card_state
    if card_state == "linked":
        assert card["subject_prefix"] == "01234567" and card["subject_truncated"]
    assert ISSUER not in repr(card) and "old.example.test" not in repr(card)


# ── rendering under the nonce CSP ───────────────────────────────────────────


_INLINE_HANDLER = re.compile(r"\son[a-z]+\s*=", re.I)


def csp_clean(html: str) -> None:
    """No inline handler, no `style=` attribute, no `javascript:` URL in any
    tag (script and style *bodies* are code, not attributes), and every
    `<script>` / `<style>` carries the nonce."""
    for tag in re.findall(r"<(?:script|style)\b[^>]*>", html):
        assert 'nonce="' in tag, tag
    markup = re.sub(r"<(script|style)\b[^>]*>.*?</\1>", "", html, flags=re.S | re.I)
    for tag in re.findall(r"<[a-zA-Z][^>]*>", markup):
        assert not _INLINE_HANDLER.search(tag), tag
        assert "style=" not in tag, tag
        assert "javascript:" not in tag.lower(), tag


def render(name, **ctx):
    base = dict(csp_nonce="n0nce", csrf_token="tok", multi_user_mode=True, username="admin",
                is_admin=True, active="users", min_password_length=12, flash=None,
                flash_kind="ok")
    base.update(ctx)
    return users.templates.get_template(name).render(base)


def edit_page(target, **ctx):
    target_ctx = {"id": target.id, "username": target.username, "is_admin": target.is_admin,
                  "is_active": target.is_active, "vault_path": ""}
    return render("user_edit.html", target=target_ctx, available_vaults=[], is_self=False,
                  oidc_enabled=True, oidc=users._oidc_card_state(target),
                  local_login_enabled=True, **ctx)


@pytest.mark.parametrize("columns,expect_forms,expect_unlink,text", [
    ({}, True, False, "Not linked"),
    (dict(oidc_issuer=ISSUER, oidc_subject="0123456789abcdef", oidc_linked_at=NOW),
     False, True, "Linked (since"),
    (dict(oidc_issuer="https://old.example.test", oidc_subject="s", oidc_linked_at=NOW),
     False, True, "Linked under a previous issuer"),
    (dict(oidc_expected_issuer=ISSUER, oidc_expected_email="bob@example.com",
          oidc_expected_set_at=NOW), True, True, "Waiting for first sign-in from"),
    (dict(oidc_expected_issuer=ISSUER, oidc_expected_email="bob@example.com",
          oidc_expected_set_at=NOW - dt.timedelta(days=9)), True, True, "Expired"),
])
def test_user_edit_card(columns, expect_forms, expect_unlink, text):
    html = edit_page(make_user(**columns))
    csp_clean(html)
    assert "Single sign-on" in html and text in html
    assert ("/oidc/expect" in html) is expect_forms
    assert ("/oidc/bind" in html) is expect_forms
    assert ("/oidc/unlink" in html) is expect_unlink
    for action in re.findall(r'<form method="POST" action="(/admin/users/5/oidc/[a-z]+)">', html):
        form = html[html.index(action):]
        form = form[:form.index("</form>")]
        assert 'name="csrf_token" value="tok"' in form, action
    if expect_unlink:
        button = re.search(r'<form method="POST" action="/admin/users/5/oidc/unlink">.*?</form>',
                           html, re.S).group(0)
        assert 'type="button"' in button and "data-confirm=" in button
        assert 'type="submit"' not in button
    assert "0123456789abcdef" not in html  # the full subject never renders
    assert ISSUER not in html


def test_self_unlink_disabled_when_password_login_is_off():
    target = make_user(oidc_issuer=ISSUER, oidc_subject="sub", oidc_linked_at=NOW)
    html = render("user_edit.html", target={"id": 5, "username": "bob", "is_admin": True,
                                            "is_active": True, "vault_path": ""},
                  available_vaults=[], is_self=True, oidc_enabled=True,
                  oidc=users._oidc_card_state(target), local_login_enabled=False)
    button = re.search(r'action="/admin/users/5/oidc/unlink">.*?</form>', html, re.S).group(0)
    assert "disabled" in button


def test_card_and_column_absent_with_oidc_off():
    target = make_user()
    html = render("user_edit.html", target={"id": 5, "username": "bob", "is_admin": False,
                                            "is_active": True, "vault_path": ""},
                  available_vaults=[], is_self=False, oidc_enabled=False, oidc=None)
    assert "Single sign-on" not in html and "/oidc/" not in html
    listing = render("users.html", users=[], oidc_enabled=False)
    assert "<th>SSO</th>" not in listing and 'colspan="9"' in listing
    del target


def test_users_list_column():
    rows = [dict(id=i, username=f"u{i}", is_admin=False, is_active=True, vault_path="/v",
                 last_login_at=None, created_at=NOW.isoformat(), api_keys_active=0,
                 api_keys_total=0, notes=0, quarantine=None, oidc_state=state)
            for i, state in enumerate(["linked", "pending", None])]
    html = render("users.html", users=rows, oidc_enabled=True)
    csp_clean(html)
    assert "<th>SSO</th>" in html
    assert ">linked</span>" in html and ">pending</span>" in html
    assert render("users.html", users=[], oidc_enabled=True).count('colspan="10"') == 1


@pytest.mark.parametrize("linked", [True, False])
def test_account_status_line_is_read_only(linked):
    html = panel_routes.templates.get_template("account.html").render(dict(
        csp_nonce="n", csrf_token="t", multi_user_mode=True, username="bob", is_admin=False,
        active="account", min_password_length=12, flash=None, flash_kind="ok",
        oidc_enabled=True, oidc_linked=linked))
    csp_clean(html)
    assert "Single sign-on" in html
    assert ("Linked" if linked else "Not linked") in html
    assert "/oidc/" not in html


def test_login_button_label_is_the_display_name_autoescaped(monkeypatch):
    from src.auth import routes as auth_routes

    monkeypatch.setattr(settings, "oidc_display_name", "Keycloak <Home>")
    page = auth_routes._render_login(Request({
        "type": "http", "method": "GET", "path": "/admin/auth/login", "headers": [],
        "query_string": b"", "session": {}}))
    body = page.body.decode()
    assert "Sign in with Keycloak &lt;Home&gt;" in body
    csp_clean(body)


# ── handler order and refusals (stubbed session) ────────────────────────────


def stub_session(*, privileged=True, exists=True, username="bob", target=None):
    """`target` is the row `_oidc_link_action` reads under the guard; by
    default a reachable admin (active, admin, bound under the issuer)."""
    session = AsyncMock()
    row = target or SimpleNamespace(id=5, is_admin=True, is_active=True,
                                    oidc_issuer=ISSUER, oidc_subject="sub-bob")

    async def execute(stmt, *args, **kwargs):
        return MagicMock(scalar_one_or_none=lambda: 5 if exists else None,
                         one_or_none=lambda: row if exists else None,
                         scalar_one=lambda: username)

    session.execute.side_effect = execute
    return session


@pytest.fixture
def guard(monkeypatch):
    calls = []
    monkeypatch.setattr(users, "_lock_admin_guard", AsyncMock(side_effect=lambda s: calls.append("lock")))
    state = SimpleNamespace(privileged=True, calls=calls)

    async def still(session, user):
        calls.append("actor")
        return state.privileged

    monkeypatch.setattr(users, "_actor_still_privileged", still)
    return state


ACTOR = SimpleNamespace(id=1, username="admin", is_admin=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["expect", "bind", "unlink"])
async def test_oidc_off_is_404_and_touches_nothing(monkeypatch, mode):
    monkeypatch.setattr(settings, "oidc_enabled", False)
    session = AsyncMock()
    with pytest.raises(HTTPException) as error:
        await users._oidc_link_action(request(), session, ACTOR, 5, mode, "x")
    assert error.value.status_code == 404
    assert not session.method_calls


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["expect", "bind", "unlink"])
async def test_actor_revoked_refusal_writes_nothing(monkeypatch, guard, mode, caplog):
    guard.privileged = False
    op = AsyncMock()
    for name in ("set_expectation", "bind_subject", "unlink"):
        monkeypatch.setattr(oidc_links, name, op)
    session = stub_session()
    with caplog.at_level(logging.WARNING):
        response = await users._oidc_link_action(request(), session, ACTOR, 5, mode, "x")
    assert guard.calls == ["lock", "actor"]
    session.execute.assert_not_awaited()  # the target was never read
    session.rollback.assert_awaited()
    session.commit.assert_not_awaited()
    op.assert_not_awaited()
    assert response.status_code == 303 and response.headers["location"] == "/admin/users/"
    assert any(getattr(r, "reason", None) == "actor_revoked" for r in caplog.records)


@pytest.mark.asyncio
async def test_missing_target_is_404_before_the_operation(monkeypatch, guard):
    op = AsyncMock()
    monkeypatch.setattr(oidc_links, "set_expectation", op)
    with pytest.raises(HTTPException) as error:
        await users._oidc_link_action(request(), stub_session(exists=False), ACTOR, 5,
                                      "expect", "a@b.c")
    assert error.value.status_code == 404
    op.assert_not_awaited()


@pytest.mark.asyncio
async def test_self_unlink_refused_with_password_login_off(monkeypatch, guard):
    monkeypatch.setattr(settings, "local_login_enabled", False)
    op = AsyncMock()
    monkeypatch.setattr(oidc_links, "unlink", op)
    me = User(id=5, username="bob", is_admin=True, is_active=True, password_hash="x")
    session = stub_session()
    response = await users._oidc_link_action(request(), session, me, 5, "unlink")
    assert response.headers["location"] == "/admin/users/5/edit"
    op.assert_not_awaited()
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome,message", [
    ("bound_already", "already linked"),
    ("conflict", "already used by another account"),
    ("invalid", "valid email"),
])
async def test_operation_refusals_flash_and_roll_back(monkeypatch, guard, outcome, message):
    monkeypatch.setattr(oidc_links, "set_expectation",
                        AsyncMock(return_value=oidc_links.AdminResult(outcome)))
    req = request()
    session = stub_session()
    response = await users._oidc_link_action(req, session, ACTOR, 5, "expect", "x@y.z")
    assert response.headers["location"] == "/admin/users/5/edit"
    session.rollback.assert_awaited()
    session.commit.assert_not_awaited()
    flashed = next(v for v in req.session.values() if isinstance(v, dict))
    assert message in flashed["message"] and flashed["kind"] == "err"


@pytest.mark.asyncio
async def test_unlink_runs_d15_before_the_operation(monkeypatch, guard):
    order = []
    monkeypatch.setattr(settings, "local_login_enabled", False)

    async def unlink(session, user_id):
        order.append("unlink")
        return oidc_links.AdminResult("ok", revoked_sessions=2)

    async def reachable(session, issuer, *, target_id, target_after):
        order.append(("reachable", issuer, target_id, target_after))
        return 0

    monkeypatch.setattr(oidc_links, "unlink", unlink)
    monkeypatch.setattr(oidc_links, "reachable_admins_after", reachable)
    session = stub_session()
    response = await users._oidc_link_action(request(), session, ACTOR, 5, "unlink")
    assert order == [("reachable", ISSUER, 5, "unlinked")]
    session.commit.assert_not_awaited()
    session.rollback.assert_awaited()
    assert response.headers["location"] == "/admin/users/5/edit"


@pytest.mark.asyncio
@pytest.mark.parametrize("target", [
    SimpleNamespace(id=5, is_admin=False, is_active=True, oidc_issuer=ISSUER, oidc_subject="s"),
    SimpleNamespace(id=5, is_admin=True, is_active=False, oidc_issuer=ISSUER, oidc_subject="s"),
    SimpleNamespace(id=5, is_admin=True, is_active=True, oidc_issuer="https://old.example",
                    oidc_subject="s"),
    SimpleNamespace(id=5, is_admin=True, is_active=True, oidc_issuer=None, oidc_subject=None),
], ids=["not_admin", "inactive", "previous_issuer", "pending_only"])
async def test_unlink_of_a_target_that_is_not_reachable_skips_d15(monkeypatch, guard, target):
    """D15 refuses only when the target is a reachable admin now (#416)."""
    monkeypatch.setattr(settings, "local_login_enabled", False)
    reachable = AsyncMock(return_value=0)
    monkeypatch.setattr(oidc_links, "reachable_admins_after", reachable)
    monkeypatch.setattr(oidc_links, "unlink",
                        AsyncMock(return_value=oidc_links.AdminResult("ok", revoked_sessions=0)))
    session = stub_session(target=target)
    await users._oidc_link_action(request(), session, ACTOR, 5, "unlink")
    reachable.assert_not_awaited()
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,event,extra", [
    ("expect", "panel_oidc_link_set", {"mode": "expect"}),
    ("bind", "panel_oidc_link_set", {"mode": "bind"}),
    ("unlink", "panel_oidc_link_removed", {"count": 2}),
])
async def test_success_commits_then_emits_without_identity(monkeypatch, guard, caplog, mode,
                                                           event, extra):
    result = oidc_links.AdminResult("ok", revoked_sessions=2 if mode == "unlink" else 0)
    for name in ("set_expectation", "bind_subject", "unlink"):
        monkeypatch.setattr(oidc_links, name, AsyncMock(return_value=result))
    session = stub_session()
    value = {"expect": "Secret.Person@Example.com", "bind": "secret-subject-xyz"}.get(mode)
    with caplog.at_level(logging.INFO):
        response = await users._oidc_link_action(request(), session, ACTOR, 5, mode, value)
    session.commit.assert_awaited_once()
    assert response.headers["location"] == "/admin/users/5/edit"
    records = [r for r in caplog.records if r.getMessage() == event]
    assert len(records) == 1
    record = records[0]
    assert (record.actor_user_id, record.user_id, record.username) == (1, 5, "bob")
    for key, value_ in extra.items():
        assert getattr(record, key) == value_
    text = " ".join(f"{r.getMessage()} {r.__dict__}" for r in caplog.records).lower()
    assert "secret" not in text and ISSUER not in text


# ── the lifespan lockout warning (D13) ──────────────────────────────────────


class _CountSession:
    def __init__(self, outcome):
        self.outcome = outcome
        self.statements = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, stmt):
        self.statements.append(str(stmt))
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return MagicMock(scalar_one=lambda: self.outcome)


@pytest.mark.asyncio
@pytest.mark.parametrize("local,outcome,critical,warning", [
    (False, 0, 1, 0),
    (False, 2, 0, 0),
    (False, RuntimeError("db said sub-xyz max@example.com"), 0, 1),
    (True, 0, 0, 0),
])
async def test_lockout_warning(monkeypatch, caplog, local, outcome, critical, warning):
    from src import main

    monkeypatch.setattr(settings, "local_login_enabled", local)
    fake = _CountSession(outcome)
    monkeypatch.setattr(main, "async_session", lambda: fake)
    with caplog.at_level(logging.DEBUG, logger="src.main"):
        await main._warn_oidc_lockout()  # never raises
    records = [r for r in caplog.records if r.name == "src.main"]
    assert sum(r.levelno == logging.CRITICAL for r in records) == critical
    assert sum(r.levelno == logging.WARNING for r in records) == warning
    if critical:
        assert "LOCAL_LOGIN_ENABLED=true" in records[0].getMessage()
    if warning:
        assert "RuntimeError" in records[0].getMessage()
        assert "sub-xyz" not in records[0].getMessage()
    assert bool(fake.statements) is (not local)
    if fake.statements:
        assert "oidc_issuer" in fake.statements[0] and "is_admin" in fake.statements[0]


def test_lockout_warning_runs_in_the_lifespan():
    import inspect

    from src import main

    source = inspect.getsource(main.lifespan)
    assert "await _warn_oidc_lockout()" in source
    sandbox = source.index("if settings.mcp_sandbox_mode")
    assert source.index("await _warn_oidc_lockout()") > source.index("return", sandbox)
