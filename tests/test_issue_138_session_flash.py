"""Regression test (#138): panel flash messages live in the session, not the URL.

The panel's post-redirect-get messages used to travel as `?flash=` / `?error=`
(with `&flash_kind=err` for a refusal) and the templates rendered whatever the
query string carried. Jinja escapes it, so there was never an XSS — the defect
is that the *text* an authenticated admin reads was chosen by whoever composed
the link, on the one page whose controls delete accounts. A crafted
`/admin/users/?flash=…` is indistinguishable from a message the server wrote.

What must hold now:

- a handler parks the message in `request.session` and redirects to a bare
  path — nothing about the message is in the URL;
- `_panel_context` pops it, so it renders exactly once and is gone on reload;
- a `?flash=` / `?error=` / `?flash_kind=` a link carries is rendered nowhere.

The requests here are real `starlette.requests.Request` objects over a
hand-built scope, with the `session` dict `SessionMiddleware` would install —
so `request.query_params` parses the query string exactly as it does in
production, which is the half a plain fake object cannot exercise.
"""
import ast
import asyncio
import os
import re
from datetime import datetime, timezone

import pydantic_settings

_orig_init = pydantic_settings.BaseSettings.__init__


def _no_env_file_init(self, *args, **kwargs):
    kwargs.setdefault("_env_file", None)
    _orig_init(self, *args, **kwargs)


pydantic_settings.BaseSettings.__init__ = _no_env_file_init
try:
    from starlette.requests import Request

    from src.control_panel import routes as panel_routes
    from src.control_panel import users as users_mod
    from src.control_panel.flash import (
        ERR,
        FLASH_SESSION_KEY,
        OK,
        flash,
        pop_flash,
    )
    from src.models.db import User
finally:
    pydantic_settings.BaseSettings.__init__ = _orig_init


PANEL_DIR = os.path.dirname(users_mod.__file__)


# --- Fakes ----------------------------------------------------------------


def _request(path: str = "/admin/users/", query: str = "", session=None) -> Request:
    """A real `Request` carrying the session dict SessionMiddleware installs."""
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "server": ("testserver", 80),
        "path": path,
        "root_path": "",
        "query_string": query.encode(),
        "headers": [],
        "session": {} if session is None else session,
    }
    return Request(scope)


class _NoSessionRequest:
    """An app built without SessionMiddleware — `request.session` raises."""

    @property
    def session(self):
        raise AssertionError("SessionMiddleware must be installed")


class _Rows:
    """Serves both `.all()` and `.scalars().all()`."""

    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows

    def scalars(self):
        return self

    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else None


class _FakeSession:
    """Answers `list_users`' three queries: key counts, note counts, users."""

    def __init__(self, users):
        self._users = users
        self._n = 0

    async def execute(self, *_a, **_k):
        self._n += 1
        if self._n <= 2:
            return _Rows([])
        return _Rows(self._users)


class _EditSession:
    async def execute(self, *_a, **_k):
        return _Rows([_a_user()])


def _a_user() -> User:
    u = User(
        username="bob",
        password_hash="x",
        is_admin=False,
        is_active=True,
        vault_path=None,
    )
    u.id = 2
    u.created_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    u.last_login_at = None
    return u


def _an_admin() -> User:
    u = User(
        username="max",
        password_hash="x",
        is_admin=True,
        is_active=True,
        vault_path=None,
    )
    u.id = 1
    return u


def _render_users(request: Request) -> str:
    response = asyncio.run(
        users_mod.list_users(
            request=request, session=_FakeSession([_a_user()]), user=_an_admin()
        )
    )
    return response.body.decode()


def _render_user_edit(request: Request) -> str:
    response = asyncio.run(
        users_mod.edit_user_form(
            user_id=2, request=request, session=_EditSession(), user=_an_admin()
        )
    )
    return response.body.decode()


# --- The message is set on the redirect, and the redirect carries nothing --


def test_a_refusal_parks_the_message_and_redirects_to_a_bare_path():
    request = _request()

    response = asyncio.run(
        users_mod.create_user(
            request=request,
            username="Not A Username",
            initial_password="long-enough",
            session=_FakeSession([]),
            user=_an_admin(),
        )
    )

    assert response.status_code == 303
    # Nothing about the message is in the URL — that is the whole change.
    assert response.headers["location"] == "/admin/users/"
    assert request.session[FLASH_SESSION_KEY] == {
        "message": (
            "Username must be 1–64 chars, lowercase letters / digits / "
            "underscores only."
        ),
        "kind": "err",
    }


def test_the_success_message_is_a_session_flash_too():
    request = _request()

    response = users_mod._back_to_list(request, "User 'bob' created.")

    assert response.headers["location"] == "/admin/users/"
    assert "flash" not in response.headers["location"]
    assert request.session[FLASH_SESSION_KEY] == {
        "message": "User 'bob' created.",
        "kind": "ok",
    }


# --- Rendered exactly once ------------------------------------------------


def test_the_flash_renders_once_and_is_gone_on_reload():
    """A browser keeps one session across both requests; the message must
    survive the redirect and not the reload after it."""
    session: dict = {}
    flash(_request(session=session), "User bob deactivated.")

    first = _render_users(_request(session=session))
    assert "User bob deactivated." in first
    assert FLASH_SESSION_KEY not in session, "the flash was not popped"

    second = _render_users(_request(session=session))
    assert "deactivated." not in second


# The rendered alert, not the stylesheet: `base.html` defines both classes in
# its CSS, so a bare `"alert-warning" in body` is true of every page.
_WARNING_ALERT = '<div class="alert alert-warning anim-1">'
_SUCCESS_ALERT = '<div class="alert alert-success anim-1">'


def test_an_err_flash_renders_as_a_warning_and_ok_as_a_success():
    err_session: dict = {}
    flash(_request(session=err_session), "Refusing to demote", ERR)
    err_body = _render_users(_request(session=err_session))
    assert _WARNING_ALERT in err_body
    assert "Refusing to demote" in err_body

    ok_session: dict = {}
    flash(_request(session=ok_session), "Updated user", OK)
    ok_body = _render_users(_request(session=ok_session))
    assert _SUCCESS_ALERT in ok_body
    assert "Updated user" in ok_body


def test_the_edit_page_pops_the_same_one_flash():
    session: dict = {}
    flash(_request(session=session), "You can't remove your own admin role.", ERR)

    first = _render_user_edit(_request(session=session))
    assert "remove your own admin role" in first
    assert _WARNING_ALERT in first

    assert "remove your own admin role" not in _render_user_edit(
        _request(session=session)
    )


# --- A crafted link renders nothing ---------------------------------------


def test_a_crafted_flash_query_parameter_is_not_rendered():
    """The #138 vector: a link an admin is sent, planting server-looking text
    on the page whose controls delete accounts."""
    query = (
        "flash=Vault%20reassigned%20-%20click%20Permanent%20delete%20to%20finish"
        "&error=EVIL_ERROR&flash_kind=err"
    )
    for render in (_render_users, _render_user_edit):
        body = render(_request(query=query))
        assert "Vault reassigned" not in body
        assert "EVIL_ERROR" not in body
        assert _WARNING_ALERT not in body
        assert _SUCCESS_ALERT not in body


def test_a_crafted_query_parameter_cannot_override_a_real_flash():
    session: dict = {}
    flash(_request(session=session), "User bob deactivated.")

    body = _render_users(_request(query="flash=EVIL&flash_kind=ok", session=session))

    assert "EVIL" not in body
    assert "deactivated." in body


def _code_string_literals(source: str) -> list[str]:
    """Every string literal in `source` except the docstrings.

    Prose is allowed to *describe* the old `?flash=` transport — this file and
    `src/control_panel/flash.py` both do — so the sweep below looks at what
    the code can actually build, not at what it says.
    """
    tree = ast.parse(source)
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(
            node,
            (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef),
        ):
            if ast.get_docstring(node, clean=False) is not None:
                docstrings.add(id(node.body[0].value))
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
    ]


def test_no_panel_module_builds_a_flash_or_error_query_string():
    """The sweep half: one converted handler is not the property — a redirect
    anywhere in the panel that still carries the message in its URL puts the
    vector straight back."""
    offenders = []
    for root in ("src/control_panel", "src/api", "src/auth"):
        for dirpath, _dirs, files in os.walk(root):
            for name in files:
                if not name.endswith(".py"):
                    continue
                path = os.path.join(dirpath, name)
                with open(path, encoding="utf-8") as fh:
                    source = fh.read()
                for literal in _code_string_literals(source):
                    if re.search(r"[?&](flash|error|flash_kind)=", literal):
                        offenders.append(f"{path}: {literal!r}")
    assert offenders == [], f"flash/error still travels in a URL: {offenders}"


def test_no_panel_handler_reads_a_flash_out_of_the_query_string():
    offenders = []
    for dirpath, _dirs, files in os.walk("src/control_panel"):
        for name in files:
            if not name.endswith(".py"):
                continue
            path = os.path.join(dirpath, name)
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    if "query_params" in line and re.search(
                        r"query_params.*(flash|error)", line
                    ):
                        offenders.append(f"{path}: {line.strip()}")
    assert offenders == [], offenders


def test_no_panel_template_renders_a_query_parameter():
    offenders = []
    for name in sorted(os.listdir(os.path.join(PANEL_DIR, "templates"))):
        if not name.endswith(".html"):
            continue
        path = os.path.join(PANEL_DIR, "templates", name)
        with open(path, encoding="utf-8") as fh:
            if "query_params" in fh.read():
                offenders.append(path)
    assert offenders == [], offenders


# --- Through the real middleware stack ------------------------------------
#
# The tests above call the handlers directly, which cannot show the one thing
# the transport change actually depends on: `SessionMiddleware` carrying the
# message across the redirect in a signed cookie, and rewriting that cookie
# without it once the render has popped it. This exercises the round trip —
# no database, the two dependencies stubbed.


def _panel_app():
    from fastapi import FastAPI
    from starlette.middleware.sessions import SessionMiddleware

    from src.control_panel.routes import require_admin_panel
    from src.control_panel.users import router as users_router
    from src.csrf import verify_csrf
    from src.database import get_session

    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="k" * 32)
    app.include_router(users_router)
    app.dependency_overrides[get_session] = lambda: _FakeSession([])
    app.dependency_overrides[require_admin_panel] = _an_admin
    # CSRF is unrelated to the message transport and has its own tests (#3).
    app.dependency_overrides[verify_csrf] = lambda: None
    return app


def test_the_message_survives_the_redirect_and_dies_on_the_reload():
    from fastapi.testclient import TestClient

    client = TestClient(_panel_app())

    posted = client.post(
        "/admin/users/create",
        data={"username": "Bad Name", "initial_password": "longenough"},
        follow_redirects=False,
    )
    assert posted.status_code == 303
    assert posted.headers["location"] == "/admin/users/"

    # …and a crafted parameter on the very request that renders it changes
    # nothing about what the admin reads.
    first = client.get("/admin/users/?flash=EVIL&flash_kind=ok")
    assert "EVIL" not in first.text
    assert _WARNING_ALERT in first.text
    assert "lowercase letters / digits / underscores" in first.text

    second = client.get("/admin/users/")
    assert '<div class="alert' not in second.text, "the flash survived a reload"


# --- The helper's own contract --------------------------------------------


def test_pop_returns_nothing_when_no_flash_was_set():
    assert pop_flash(_request()) == (None, OK)


def test_an_unknown_kind_is_stored_and_read_as_ok():
    request = _request()
    flash(request, "hello", "catastrophe")
    assert request.session[FLASH_SESSION_KEY]["kind"] == OK
    assert pop_flash(request) == ("hello", OK)


def test_an_empty_message_is_not_stored():
    request = _request()
    flash(request, "")
    assert FLASH_SESSION_KEY not in request.session


def test_a_malformed_entry_is_ignored_rather_than_rendered():
    """A shape written by an older deploy must not 500 the page."""
    for junk in ("a bare string", {"kind": "err"}, {"message": 7}, None):
        request = _request(session={FLASH_SESSION_KEY: junk})
        assert pop_flash(request) == (None, OK)


def test_the_helpers_survive_an_app_without_session_middleware():
    """Single-user harnesses build apps with no session; a message going
    unshown must never turn a completed action into a 500."""
    request = _NoSessionRequest()
    flash(request, "nowhere to put this", ERR)  # must not raise
    assert pop_flash(request) == (None, OK)
    assert flash(None, "nor here") is None
    assert pop_flash(None) == (None, OK)


def test_panel_context_supplies_the_flash_to_every_template():
    session = {FLASH_SESSION_KEY: {"message": "hi", "kind": ERR}}
    request = _request(session=session)

    ctx = panel_routes._panel_context(request, _an_admin(), {"active": "users"})

    assert ctx["flash"] == "hi"
    assert ctx["flash_kind"] == ERR
    assert FLASH_SESSION_KEY not in session
