"""Regression test for GitHub issue #4: open redirect / confused-deputy in
the OAuth authorization POST handler.

`authorize_post` accepts `redirect_uri` as a form field but previously never
re-validated it against the client's registered list (the GET handler does).
A CSRF cookie+state pair is obtainable by an attacker who runs their own
GET /authorize, so they could POST action=approve (or action=deny) with an
arbitrary `redirect_uri` and have the issued auth code (or an error redirect)
delivered to an attacker-controlled URL.

The fix loads the client in the POST path and rejects any `redirect_uri` not
in `client.redirect_uris` BEFORE minting a code or emitting any redirect, on
both the approve and deny paths.

Runs fully offline: no DB, no network, no embedding provider. The DB layer is
replaced with an in-memory fake `async_session`; the CSRF cookie/state pair is
produced with the real signed-state serializer so the CSRF guard passes and we
exercise the redirect-uri check specifically.
"""
# Point pydantic-settings at a non-existent env file BEFORE importing
# `src.oauth.routes` (which imports `src.config`) so config loads purely from
# process env + conftest defaults, independent of the dev host's `.env`.
import pydantic_settings  # noqa: E402

_orig_init = pydantic_settings.BaseSettings.__init__


def _no_env_file_init(self, *args, **kwargs):
    kwargs.setdefault("_env_file", None)
    _orig_init(self, *args, **kwargs)


pydantic_settings.BaseSettings.__init__ = _no_env_file_init
try:
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    import pytest
    from fastapi.responses import JSONResponse, RedirectResponse

    from src.oauth import routes
finally:
    pydantic_settings.BaseSettings.__init__ = _orig_init


REGISTERED_URI = "https://client.example.com/callback"
ATTACKER_URI = "https://evil.example.com/steal"


class _FakeClient:
    def __init__(self, redirect_uris, user_id=None, scope="read"):
        self.client_id = "client123"
        self.redirect_uris = list(redirect_uris)
        self.user_id = user_id
        self.scope = scope


class _FakeResult:
    def __init__(self, obj):
        self._obj = obj

    def scalar_one_or_none(self):
        return self._obj


class _FakeSession:
    """Minimal async-session stand-in. Returns a preset client for the single
    SELECT the handler issues; records any added rows; no-ops commit."""

    def __init__(self, client):
        self._client = client
        self.added = []
        self.committed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, _stmt):
        return _FakeResult(self._client)

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.committed = True


class _FakeRequest:
    """Carries only what authorize_post touches: cookies and session."""

    def __init__(self, signed_cookie):
        self.cookies = {"oauth_state": signed_cookie}
        self.session = {}


def _signed_state():
    """Produce a (server_state, signed_cookie) pair via the real serializer,
    mirroring what the GET handler sets so the CSRF guard passes."""
    server_state = "csrfstatetoken1234567890"
    signed = routes._state_serializer().dumps(server_state)
    return server_state, signed


def _install_fake_session(monkeypatch, client):
    session = _FakeSession(client)
    monkeypatch.setattr(routes, "async_session", lambda: session)
    return session


def _call(client, *, action, redirect_uri, multi_user=False):
    monkeypatch = pytest.MonkeyPatch()
    try:
        if multi_user:
            monkeypatch.setattr(routes.settings, "multi_user_mode", True, raising=False)
        else:
            monkeypatch.setattr(routes.settings, "multi_user_mode", False, raising=False)
        session = _install_fake_session(monkeypatch, client)
        server_state, signed = _signed_state()
        req = _FakeRequest(signed)
        if multi_user:
            req.session["user_id"] = 7
            req.session["session_version"] = 1
            monkeypatch.setattr(
                routes,
                "get_active_session_user",
                AsyncMock(return_value=SimpleNamespace(id=7)),
            )
        resp = asyncio.run(
            routes.authorize_post(
                req,
                action=action,
                client_id="client123",
                redirect_uri=redirect_uri,
                code_challenge="A" * 43,
                code_challenge_method="S256",
                scope="read",
                state=server_state,
                client_state="clientecho",
            )
        )
        return resp, session
    finally:
        monkeypatch.undo()


def test_approve_with_unregistered_redirect_uri_rejected():
    """The core vuln: action=approve with an attacker redirect_uri must be
    rejected with invalid_redirect_uri and must NOT mint/redirect a code."""
    client = _FakeClient([REGISTERED_URI])
    resp, session = _call(client, action="approve", redirect_uri=ATTACKER_URI)

    assert isinstance(resp, JSONResponse)
    assert resp.status_code == 400
    assert b"invalid_redirect_uri" in resp.body
    # No auth code was persisted and nothing was committed.
    assert session.added == []
    assert session.committed is False


def test_deny_with_unregistered_redirect_uri_rejected():
    """The deny path must not be abusable as an open redirect either."""
    client = _FakeClient([REGISTERED_URI])
    resp, _session = _call(client, action="deny", redirect_uri=ATTACKER_URI)

    assert isinstance(resp, JSONResponse)
    assert resp.status_code == 400
    assert b"invalid_redirect_uri" in resp.body


def test_unknown_client_rejected():
    resp, _session = _call(None, action="approve", redirect_uri=REGISTERED_URI)
    assert isinstance(resp, JSONResponse)
    assert resp.status_code == 400
    assert b"invalid_client" in resp.body


def test_approve_with_registered_redirect_uri_still_works():
    """Legitimate flow: a registered redirect_uri mints a code and 302s to it."""
    client = _FakeClient([REGISTERED_URI])
    resp, session = _call(client, action="approve", redirect_uri=REGISTERED_URI)

    assert isinstance(resp, RedirectResponse)
    assert resp.status_code == 302
    location = resp.headers["location"]
    assert location.startswith(REGISTERED_URI)
    assert "code=" in location
    assert ATTACKER_URI not in location
    # A code row was persisted.
    assert len(session.added) == 1
    assert session.committed is True


def test_deny_with_registered_redirect_uri_still_redirects():
    """Legitimate deny: registered redirect_uri gets an access_denied 302."""
    client = _FakeClient([REGISTERED_URI])
    resp, _session = _call(client, action="deny", redirect_uri=REGISTERED_URI)

    assert isinstance(resp, RedirectResponse)
    assert resp.status_code == 302
    location = resp.headers["location"]
    assert location.startswith(REGISTERED_URI)
    assert "error=access_denied" in location


def test_multi_user_binds_first_authorizer_on_registered_uri():
    """Multi-user first-authorizer binding still works and reuses the loaded
    client row (user_id stamped) on the legitimate approve path."""
    client = _FakeClient([REGISTERED_URI], user_id=None)
    resp, session = _call(
        client, action="approve", redirect_uri=REGISTERED_URI, multi_user=True
    )

    assert isinstance(resp, RedirectResponse)
    assert resp.status_code == 302
    assert client.user_id == 7
    assert session.committed is True
