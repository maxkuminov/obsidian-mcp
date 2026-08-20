"""Regression test: the OAuth consent screen must preselect the radio that
matches the scope the client actually requested at /authorize.

Before this fix, `authorize.html` hardcoded `checked` on the "Read only"
radio regardless of the `scope` query param. A client requesting
`scope=readwrite` (e.g. because the end user picked read-write access in
the client's own connector settings) still rendered "Read only"
preselected, so approving without manually re-clicking the other radio
silently granted a read-only token despite read-write having been
requested.

This does not touch the security boundary: `_clamp_scope` in
authorize_post (covered by test_issue_21_registered_scope_enforced.py)
still re-validates whatever the submitted form claims against the
client's registered scope, independent of what's preselected here.
"""
import asyncio

import pydantic_settings

_orig_init = pydantic_settings.BaseSettings.__init__


def _no_env_file_init(self, *args, **kwargs):
    kwargs.setdefault("_env_file", None)
    _orig_init(self, *args, **kwargs)


pydantic_settings.BaseSettings.__init__ = _no_env_file_init
try:
    import pytest

    from src.oauth import routes
finally:
    pydantic_settings.BaseSettings.__init__ = _orig_init


REGISTERED_URI = "https://client.example.com/callback"
VALID_PKCE_CHALLENGE = "a" * 43


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


class _FakeRequest:
    """Minimal stand-in: authorize_get only stores this on the Jinja2
    context (`context.setdefault("request", request)`); the template
    doesn't call `request.url_for` or read any attribute off it."""


def _get(client, *, requested_scope):
    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(routes.settings, "multi_user_mode", False, raising=False)
        session = _FakeSession(client)
        monkeypatch.setattr(routes, "async_session", lambda: session)
        response = asyncio.run(
            routes.authorize_get(
                _FakeRequest(),
                response_type="code",
                client_id="client123",
                redirect_uri=REGISTERED_URI,
                code_challenge=VALID_PKCE_CHALLENGE,
                code_challenge_method="S256",
                scope=requested_scope,
                state="",
            )
        )
        return response
    finally:
        monkeypatch.undo()


def _radio_checked(html: str, value: str) -> bool:
    """True if the radio `<input ... value="{value}" ...>` carries `checked`."""
    marker = f'value="{value}"'
    idx = html.index(marker)
    tag_end = html.index(">", idx)
    return "checked" in html[idx:tag_end]


def test_readwrite_request_preselects_readwrite_radio():
    client = _FakeClient(scope="read readwrite offline_access")
    response = _get(client, requested_scope="readwrite")
    html = response.body.decode()

    assert _radio_checked(html, "readwrite") is True
    assert _radio_checked(html, "read") is False


def test_read_request_preselects_read_radio():
    client = _FakeClient(scope="read readwrite offline_access")
    response = _get(client, requested_scope="read")
    html = response.body.decode()

    assert _radio_checked(html, "read") is True
    assert _radio_checked(html, "readwrite") is False


def test_readonly_client_ignores_readwrite_request_and_omits_the_option():
    """A client not registered for readwrite can still send scope=readwrite
    at /authorize (it's an attacker-controllable query param) - the consent
    screen must neither preselect nor even offer an option it can't hold.
    Enforcement still lives in `_clamp_scope`; this just keeps the UI from
    implying an option the client was never registered for."""
    client = _FakeClient(scope="read offline_access")
    response = _get(client, requested_scope="readwrite")
    html = response.body.decode()

    assert 'value="readwrite"' not in html
    assert _radio_checked(html, "read") is True


def test_default_query_scope_preselects_read_radio():
    client = _FakeClient(scope="read readwrite offline_access")
    response = _get(client, requested_scope="read")  # matches the Query(...) default

    html = response.body.decode()
    assert _radio_checked(html, "read") is True
    assert _radio_checked(html, "readwrite") is False
