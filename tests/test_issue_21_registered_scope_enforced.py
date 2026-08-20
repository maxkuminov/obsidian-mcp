"""Regression test for GitHub issue #21: the registered client scope is never
enforced at the authorization endpoint.

A client registers via /register with a `scope` (e.g. read-only). At consent
time, `authorize_post` previously took the `scope` form field at face value —
only checking it against the global VALID_SCOPES set — and minted an auth code
with whatever the form claimed. The consent radio buttons are client-side and
trivially bypassed, so a read-only client could POST scope=readwrite and obtain
a readwrite grant, escalating beyond what it was registered for.

The fix clamps the consent-form scope to the intersection of what the user
requested and what the client is registered to hold (`_clamp_scope`), applied
in `authorize_post` before the code is minted. `readwrite` still implies
`read`, so a readwrite-registered client may downgrade to plain read.

Runs fully offline: no DB, no network, no embedding provider. The DB layer is
replaced with an in-memory fake `async_session`; the CSRF cookie/state pair is
produced with the real signed-state serializer so the CSRF guard passes and we
exercise the scope-clamp specifically.
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

    import pytest
    from fastapi.responses import JSONResponse, RedirectResponse

    from src.oauth import routes
finally:
    pydantic_settings.BaseSettings.__init__ = _orig_init


REGISTERED_URI = "https://client.example.com/callback"


class _FakeClient:
    def __init__(self, scope, redirect_uris=(REGISTERED_URI,), user_id=None):
        self.client_id = "client123"
        self.scope = scope
        self.redirect_uris = list(redirect_uris)
        self.user_id = user_id


class _FakeResult:
    def __init__(self, obj):
        self._obj = obj

    def scalar_one_or_none(self):
        return self._obj


class _FakeSession:
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
    def __init__(self, signed_cookie):
        self.cookies = {"oauth_state": signed_cookie}
        self.session = {}


def _signed_state():
    server_state = "csrfstatetoken1234567890"
    signed = routes._state_serializer().dumps(server_state)
    return server_state, signed


def _approve(client, *, requested_scope):
    """Run authorize_post(action=approve) and return the persisted OAuthCode
    (or the JSONResponse on rejection)."""
    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(routes.settings, "multi_user_mode", False, raising=False)
        session = _FakeSession(client)
        monkeypatch.setattr(routes, "async_session", lambda: session)
        server_state, signed = _signed_state()
        req = _FakeRequest(signed)
        resp = asyncio.run(
            routes.authorize_post(
                req,
                action="approve",
                client_id="client123",
                redirect_uri=REGISTERED_URI,
                code_challenge="A" * 43,
                code_challenge_method="S256",
                scope=requested_scope,
                state=server_state,
                client_state="clientecho",
            )
        )
        return resp, session
    finally:
        monkeypatch.undo()


# --- The core regression: read-only client cannot escalate to readwrite ---


def test_readonly_client_cannot_be_granted_readwrite():
    client = _FakeClient(scope="read")
    resp, session = _approve(client, requested_scope="readwrite")

    # The flow still succeeds (it's a valid request), but the issued code's
    # scope is clamped to the registered read-only scope.
    assert isinstance(resp, RedirectResponse)
    assert resp.status_code == 302
    assert len(session.added) == 1
    oauth_code = session.added[0]
    assert oauth_code.scope == "read"


def test_readwrite_client_keeps_readwrite():
    client = _FakeClient(scope="readwrite")
    resp, session = _approve(client, requested_scope="readwrite")

    assert isinstance(resp, RedirectResponse)
    assert len(session.added) == 1
    assert session.added[0].scope == "readwrite"


def test_readwrite_client_may_downgrade_to_read():
    """readwrite implies read — a readwrite-registered client can still pick
    plain read at consent time."""
    client = _FakeClient(scope="readwrite")
    resp, session = _approve(client, requested_scope="read")

    assert isinstance(resp, RedirectResponse)
    assert len(session.added) == 1
    assert session.added[0].scope == "read"


# --- Unit-level coverage of the clamp helper ---


def test_clamp_scope_helper():
    assert routes._clamp_scope("readwrite", "read") == "read"
    assert routes._clamp_scope("read", "read") == "read"
    assert routes._clamp_scope("readwrite", "readwrite") == "readwrite"
    # readwrite registration implies read availability
    assert routes._clamp_scope("read", "readwrite") == "read"
    # No vault scope on either side is a *refusal*, not a fallback to `read`.
    # The old `or "read"` conflated the legitimate readwrite->read downgrade
    # above with "this client is registered for nothing", so a DCR client
    # registered `scope="offline_access"` was handed read access to the whole
    # vault. Every caller now turns an empty result into an error.
    assert routes._clamp_scope("", "read") == ""
    assert routes._clamp_scope("read", "offline_access") == ""
