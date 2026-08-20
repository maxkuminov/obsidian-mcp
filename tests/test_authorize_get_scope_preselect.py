"""The OAuth consent screen preselects "Read only" — always — and tells the
user what the client actually asked for.

History. Originally `authorize.html` hardcoded `checked` on the "Read only"
radio and the request box said only "<client> is requesting access to your
Obsidian vault", so a client asking for `scope=readwrite` got a read-only
grant with no explanation (#62). #62 fixed the silence by binding the
preselect to the requested scope — but **the checked radio is the value the
form submits**, and `/register` is unauthenticated, so that turned one
unchanged Approve click into a vault-wide write grant for any self-registered
client (#63).

What is pinned here is the split #63 settled on:

* the *preselect* is a fail-safe constant — "Read only", regardless of the
  requested scope and regardless of the client's registered scope, so write
  always costs a deliberate click;
* the *disclosure* is driven by the request — the consent screen names the
  access level the client asked for, and says so even when the client is
  registered read-only and cannot hold it.

Server-side enforcement is a separate boundary and is unchanged: `_clamp_scope`
in `authorize_post` (covered by `test_issue_21_registered_scope_enforced.py`)
re-validates whatever the form submits against the client's registered scope.

The requests go through a real `TestClient` rather than calling
`authorize_get` directly, because one of the properties under test is the
`scope: str = Query("read")` default — a direct call would hand the function a
`Query` object, not the string FastAPI resolves it to.
"""
import re

import pydantic_settings
import pytest

_orig_init = pydantic_settings.BaseSettings.__init__


def _no_env_file_init(self, *args, **kwargs):
    kwargs.setdefault("_env_file", None)
    _orig_init(self, *args, **kwargs)


pydantic_settings.BaseSettings.__init__ = _no_env_file_init
try:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from src.limiter import limiter
    from src.oauth import routes
finally:
    pydantic_settings.BaseSettings.__init__ = _orig_init


REGISTERED_URI = "https://client.example.com/callback"
VALID_PKCE_CHALLENGE = "a" * 43
WRITE_CAPABLE = "read readwrite offline_access"
READ_ONLY = "read offline_access"


class _FakeClient:
    def __init__(self, scope, redirect_uris=(REGISTERED_URI,)):
        self.client_id = "client123"
        self.client_name = "Test Client"
        self.scope = scope
        self.redirect_uris = list(redirect_uris)


class _FakeResult:
    def __init__(self, obj):
        self._obj = obj

    def scalar_one_or_none(self):
        return self._obj


class _FakeSession:
    def __init__(self, client):
        self._client = client

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, _stmt):
        return _FakeResult(self._client)


def _get(client, *, requested_scope=None):
    """Render the consent screen for `client`.

    `requested_scope=None` omits the query parameter entirely, so FastAPI
    supplies the `Query("read")` default — the path an ordinary client that
    never sends `scope` actually takes.
    """
    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(routes.settings, "multi_user_mode", False, raising=False)
        session = _FakeSession(client)
        monkeypatch.setattr(routes, "async_session", lambda: session)

        app = FastAPI()
        app.state.limiter = limiter
        app.include_router(routes.router)

        params = {
            "response_type": "code",
            "client_id": "client123",
            "redirect_uri": REGISTERED_URI,
            "code_challenge": VALID_PKCE_CHALLENGE,
            "code_challenge_method": "S256",
        }
        if requested_scope is not None:
            params["scope"] = requested_scope

        response = TestClient(app).get("/authorize", params=params)
        assert response.status_code == 200
        return response.text
    finally:
        monkeypatch.undo()


def _radio_checked(html: str, value: str) -> bool:
    """True if the radio `<input ... value="{value}" ...>` carries `checked`."""
    marker = f'value="{value}"'
    idx = html.index(marker)
    tag_end = html.index(">", idx)
    return "checked" in html[idx:tag_end]


def _checked_radio_values(html: str) -> list[str]:
    """Every scope radio that renders `checked`, by submitted value."""
    checked = []
    for tag in re.findall(r"<input\b[^>]*\btype=\"radio\"[^>]*>", html):
        if "checked" not in tag:
            continue
        value = re.search(r'value="([^"]*)"', tag)
        checked.append(value.group(1) if value else "")
    return checked


def _text(html: str) -> str:
    """Tag-stripped, whitespace-collapsed page text, for prose assertions."""
    return " ".join(re.sub(r"<[^>]+>", " ", html).split())


# --- the fail-safe preselect (#63) -----------------------------------------


def test_readwrite_request_still_preselects_read_radio():
    """The #63 property. A write-capable client asking for `readwrite` must
    still render "Read only" checked: the checked radio is what Approve
    submits, and `/register` is unauthenticated, so anything else lets a
    single unchanged click grant vault-wide write to a client the user never
    vetted."""
    html = _get(_FakeClient(scope=WRITE_CAPABLE), requested_scope="readwrite")

    assert _radio_checked(html, "read") is True
    assert _radio_checked(html, "readwrite") is False


def test_readwrite_request_is_disclosed_even_though_read_is_preselected():
    """The other half of #63: fail-safe must not mean silent. The user whose
    connector asked for write has to see that it asked, or the downgrade is
    the same unexplained surprise #62 was filed about."""
    html = _get(_FakeClient(scope=WRITE_CAPABLE), requested_scope="readwrite")

    assert "Test Client is requesting Read + Write access" in _text(html)
    assert _radio_checked(html, "read") is True


def test_only_the_read_radio_is_ever_checked():
    """Nothing but "Read only" carries `checked`, on either request. Stated as
    a whole-form property so a future third access level cannot be added
    pre-checked without failing here."""
    for requested in ("read", "readwrite"):
        html = _get(_FakeClient(scope=WRITE_CAPABLE), requested_scope=requested)
        assert _checked_radio_values(html) == ["read"], requested


def test_read_request_preselects_read_and_says_read_only():
    html = _get(_FakeClient(scope=WRITE_CAPABLE), requested_scope="read")

    assert "Test Client is requesting Read only access" in _text(html)
    assert _radio_checked(html, "read") is True
    assert _radio_checked(html, "readwrite") is False


def test_default_query_scope_is_read():
    """Exercises the `scope: str = Query("read")` default by omitting the
    parameter — the earlier version of this test passed `scope="read"`
    explicitly and never reached the default at all (#65 gap 3)."""
    html = _get(_FakeClient(scope=WRITE_CAPABLE))

    assert "Test Client is requesting Read only access" in _text(html)
    assert _checked_radio_values(html) == ["read"]


# --- disclosure when the client cannot hold what it asked for --------------


def test_readonly_client_is_told_write_is_not_available():
    """A client not registered for `readwrite` can still send
    `scope=readwrite` at /authorize — it is an attacker-controllable query
    param. The screen must not offer an option the client cannot hold, and
    must say so rather than silently rendering a read-only consent for a
    write request."""
    html = _get(_FakeClient(scope=READ_ONLY), requested_scope="readwrite")
    text = _text(html)

    assert 'value="readwrite"' not in html
    assert _radio_checked(html, "read") is True
    assert "Test Client is requesting Read + Write access" in text
    assert "not available to this client" in text


def test_write_capable_client_is_not_told_write_is_unavailable():
    """The unavailable notice is about the *registered* scope, so it must not
    fire for a client that can hold write."""
    html = _get(_FakeClient(scope=WRITE_CAPABLE), requested_scope="readwrite")

    assert "not available to this client" not in _text(html)


# --- the selected level is visibly marked ----------------------------------


def test_selected_scope_option_is_visibly_highlighted():
    """`.scope-option` had a `:hover` rule but no checked state, so the only
    signal of which level was selected was the native radio dot (#63). The
    `:has()` rule must stand alone: grouping it with a selector that has to
    keep working would take that one down with it in a browser without
    `:has()` support."""
    html = _get(_FakeClient(scope=WRITE_CAPABLE), requested_scope="read")

    css = re.sub(r"/\*.*?\*/", " ", html[: html.index("</style>")], flags=re.S)
    rules = [
        rule for rule in re.findall(r"([^{}]+)\{[^{}]*\}", css) if ":has(" in rule
    ]
    assert rules, "no :has() rule on the consent screen"
    for selector in rules:
        assert ".scope-option" in selector
        assert ":checked" in selector
        # A selector list is dropped wholesale when any part of it is
        # unparseable, so the fallback only degrades gracefully while the
        # `:has()` rules keep to themselves.
        assert "," not in selector
