"""A client registered for `offline_access` alone must be granted nothing.

`clamp_scope` used to end with ``or "read"``. That fallback was doing two
unrelated jobs at once:

* the legitimate downgrade — a client registered `read` that asks for
  `readwrite` gets `read` (issue #21), because the requested and registered
  sets do not intersect at all under the old implementation; and
* a silent default for a client registered for **no vault scope at all**.

RFC 7591 registration takes `scope` verbatim, so `{"scope": "offline_access"}`
is a client that may hold a refresh token and read nothing. Under the old
fallback it was handed `read` over the entire vault — a permission its
registration never named, arrived at by a code path that exists to *restrict*
scopes.

The downgrade is now expressed directly (the weaker of the two vault levels),
so the fallback could become what it should always have been: nothing. Every
path that writes or enforces a scope turns that into a refusal, and each of
those paths is pinned below.
"""
import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.responses import RedirectResponse

from src.control_panel import routes as panel
from src.models.db import OAuthClient, OAuthCode, OAuthToken
from src.oauth import routes as oauth
from src.oauth.scope import clamp_scope, has_vault_scope, vault_level

from _oauth_grant_fakes import (
    SeqSession,
    FakeClient,
    FakeSession,
    FakeToken,
    SingleUserSentinel,
    in_hours,
)

OFFLINE_ONLY = "offline_access"
REGISTERED_URI = "https://client.example.com/callback"
REFRESH_SECRET = "r" * 64
VERIFIER = "v" * 64


def body(response) -> dict:
    return json.loads(response.body)


# --- the helpers ----------------------------------------------------------


def test_vault_level_does_not_count_offline_access():
    assert vault_level("readwrite") == "readwrite"
    assert vault_level("offline_access readwrite") == "readwrite"
    assert vault_level("read") == "read"
    assert vault_level("offline_access read") == "read"
    assert vault_level(OFFLINE_ONLY) is None
    assert vault_level("") is None
    assert vault_level(None) is None


def test_has_vault_scope_is_the_same_question():
    assert has_vault_scope("offline_access readwrite") is True
    assert has_vault_scope("read") is True
    assert has_vault_scope(OFFLINE_ONLY) is False
    assert has_vault_scope("") is False


@pytest.mark.parametrize("requested", ["read", "readwrite", "offline_access", "offline_access read", ""])
def test_clamp_against_an_offline_only_registration_grants_nothing(requested):
    assert clamp_scope(requested, OFFLINE_ONLY) == ""


def test_clamp_still_downgrades_rather_than_refusing():
    """The behaviour the old fallback was accidentally providing (issue #21)."""
    assert clamp_scope("readwrite", "read") == "read"
    assert clamp_scope("readwrite offline_access", "read") == "read"


# --- /register refuses the registration in the first place ----------------


class _RegistrationRequest:
    def __init__(self, body):
        self._body = body

    async def json(self):
        return self._body


def _register(scope, monkeypatch):
    session = SeqSession()
    monkeypatch.setattr(oauth, "async_session", lambda: session)
    response = asyncio.run(
        oauth.register_client.__wrapped__(
            _RegistrationRequest(
                {
                    "client_name": "Scopeless",
                    "redirect_uris": [REGISTERED_URI],
                    "token_endpoint_auth_method": "none",
                    "scope": scope,
                }
            )
        )
    )
    return response, session


def test_registering_with_offline_access_alone_is_refused(monkeypatch):
    response, session = _register(OFFLINE_ONLY, monkeypatch)

    assert response.status_code == 400
    assert body(response)["error"] == "invalid_scope"
    assert session.added == [], "no client row may be created"


def test_registering_with_a_vault_scope_still_works(monkeypatch):
    response, session = _register("read offline_access", monkeypatch)

    assert response.status_code == 201
    assert len(session.added) == 1


# --- /authorize (POST) refuses to mint a code -----------------------------


class _FakeAuthzSession:
    def __init__(self, client):
        self._client = client
        self.added = []
        self.committed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def execute(self, _stmt, *_a, **_kw):
        client = self._client

        class _R:
            def scalar_one_or_none(self_inner):
                return client

        return _R()

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.committed = True


class _FakeRequest:
    def __init__(self, signed_cookie):
        self.cookies = {"oauth_state": signed_cookie}
        self.session = {}


def _approve(client_scope, requested="read", monkeypatch=None):
    mp = monkeypatch or pytest.MonkeyPatch()
    owns = monkeypatch is None
    try:
        mp.setattr(oauth.settings, "multi_user_mode", False, raising=False)
        client = OAuthClient(
            client_id="client123",
            client_secret_hash=None,
            token_endpoint_auth_method="none",
            client_name="Scopeless",
            redirect_uris=[REGISTERED_URI],
            scope=client_scope,
        )
        session = _FakeAuthzSession(client)
        mp.setattr(oauth, "async_session", lambda: session)
        server_state = "csrfstatetoken1234567890"
        signed = oauth._state_serializer().dumps(server_state)
        response = asyncio.run(
            oauth.authorize_post(
                _FakeRequest(signed),
                action="approve",
                client_id="client123",
                redirect_uri=REGISTERED_URI,
                code_challenge="A" * 43,
                code_challenge_method="S256",
                scope=requested,
                state=server_state,
                client_state="echo",
            )
        )
        return response, session
    finally:
        if owns:
            mp.undo()


def test_consent_approval_mints_no_code_for_an_offline_only_client():
    response, session = _approve(OFFLINE_ONLY)

    assert response.status_code == 400
    assert body(response)["error"] == "invalid_scope"
    assert session.added == []
    assert session.committed is False


def test_consent_approval_still_works_for_a_read_client():
    response, session = _approve("read offline_access")

    assert isinstance(response, RedirectResponse)
    assert len(session.added) == 1
    assert session.added[0].scope == "read"


# --- the token endpoint refuses the exchange ------------------------------


def _exchange(client_scope, code_scope, monkeypatch):
    client = OAuthClient(
        client_id="client123",
        client_secret_hash=None,
        token_endpoint_auth_method="none",
        client_name="Scopeless",
        redirect_uris=[REGISTERED_URI],
        scope=client_scope,
    )
    code = OAuthCode(
        code_hash=oauth._hash("the-code"),
        client_id=client.client_id,
        redirect_uri=REGISTERED_URI,
        scope=code_scope,
        code_challenge=oauth._base64url_sha256(VERIFIER),
        code_challenge_method="S256",
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        used=False,
    )
    session = SeqSession([code, client])
    monkeypatch.setattr(oauth, "async_session", lambda: session)
    response = asyncio.run(
        oauth._handle_auth_code(
            {
                "code": "the-code",
                "code_verifier": VERIFIER,
                "redirect_uri": REGISTERED_URI,
            }
        )
    )
    return response, session, code


def test_code_exchange_mints_no_token_for_an_offline_only_client(monkeypatch):
    """The path that would have handed out the vault-wide read token."""
    response, session, code = _exchange(OFFLINE_ONLY, "offline_access", monkeypatch)

    assert response.status_code == 400
    assert body(response)["error"] == "invalid_scope"
    assert session.added == []
    assert code.used is False, "a refusal must not burn the code"


def test_code_exchange_refuses_even_when_the_code_claims_read(monkeypatch):
    """A code minted before the registration was narrowed cannot resurrect it."""
    response, session, code = _exchange(OFFLINE_ONLY, "read", monkeypatch)

    assert response.status_code == 400
    assert body(response)["error"] == "invalid_scope"
    assert session.added == []


# --- rotation refuses too -------------------------------------------------


def _refresh(client_scope, token_scope, monkeypatch):
    tokens = [
        FakeToken(
            grant_id="g1",
            token_type="refresh",
            scope=token_scope,
            expires_at=in_hours(720),
            token_hash=oauth._hash(REFRESH_SECRET),
        )
    ]
    session = FakeSession(clients=[FakeClient(scope=client_scope)], tokens=tokens)
    monkeypatch.setattr(oauth, "async_session", lambda: session)
    response = asyncio.run(oauth._handle_refresh({"refresh_token": REFRESH_SECRET}))
    return response, session, tokens[0]


def test_rotation_refuses_when_the_registration_grants_nothing(monkeypatch):
    """A registration narrowed to `offline_access` ends the grant at the next
    rotation instead of re-minting read access forever."""
    response, session, old = _refresh(OFFLINE_ONLY, "offline_access read", monkeypatch)

    assert response.status_code == 400
    assert body(response)["error"] == "invalid_scope"
    assert [o for o in session.added if isinstance(o, OAuthToken)] == []
    assert old.revoked is False, "nothing is committed, so the old token is untouched"


# --- the panel refuses to write an empty scope ----------------------------


def test_panel_scope_change_is_refused_for_an_offline_only_client():
    tokens = [
        FakeToken(grant_id="g1", token_type="access", scope="offline_access"),
        FakeToken(grant_id="g1", token_type="refresh", scope="offline_access"),
    ]
    session = FakeSession(clients=[FakeClient(scope=OFFLINE_ONLY)], tokens=tokens)

    asyncio.run(
        panel.update_oauth_token_scope(
            token_id=tokens[0].id,
            request=None,
            scope="read",
            session=session,
            user=SingleUserSentinel(),
        )
    )

    assert session.committed == 0
    for token in tokens:
        assert token.scope == "offline_access", (
            "writing an empty scope would be worse than refusing — the "
            "middleware maps anything without `readwrite` to `read`"
        )


# --- the panel does not badge such a token "Active" -----------------------


def _render_panel(clients, tokens):
    from _oauth_grant_fakes import FakeRequest

    from src.control_panel import routes as panel_routes

    session = FakeSession(clients=clients, tokens=tokens)
    response = asyncio.run(
        panel_routes.oauth_page(
            request=FakeRequest(), session=session, user=SingleUserSentinel()
        )
    )
    return response.body.decode()


def test_panel_shows_no_vault_scope_instead_of_active():
    """The panel must not over-report liveness (the #76 direction).

    `src/mcp_server/auth.py` 401s this token, so rendering it green — with a
    working scope dropdown next to it — told the operator a dead credential
    was live and offered a control that could only ever be refused.
    """
    tokens = [
        FakeToken(grant_id="g1", token_type="access", scope=OFFLINE_ONLY),
        FakeToken(grant_id="g1", token_type="refresh", scope=OFFLINE_ONLY),
    ]

    html = _render_panel([FakeClient(scope=OFFLINE_ONLY)], tokens)

    assert "No vault scope" in html
    assert 'class="badge badge-green"' not in html
    # No scope control and no Revoke: the credential authenticates nowhere,
    # and the select could only write a scope the client is not registered for.
    assert "<option" not in html
    assert "/revoke" not in html


def test_the_grant_status_itself_is_no_vault_scope():
    """Asserted on the route's own view, not just the rendered string."""
    import pytest as _pytest

    from _oauth_grant_fakes import FakeRequest
    from src.control_panel import routes as panel_routes

    tokens = [FakeToken(grant_id="g1", token_type="access", scope=OFFLINE_ONLY)]
    session = FakeSession(clients=[FakeClient(scope=OFFLINE_ONLY)], tokens=tokens)
    captured = {}
    real = panel_routes.templates.TemplateResponse

    def _capture(request, name, context, *a, **kw):
        captured.update(context)
        return real(request, name, context, *a, **kw)

    mp = _pytest.MonkeyPatch()
    try:
        mp.setattr(panel_routes.templates, "TemplateResponse", _capture)
        asyncio.run(
            panel_routes.oauth_page(
                request=FakeRequest(), session=session, user=SingleUserSentinel()
            )
        )
    finally:
        mp.undo()

    grant = captured["clients"][0]["grants"][0]
    assert grant["status"] == "no_vault_scope"
    assert grant["token_id"] is None, "nothing to act on"
    assert grant["has_write"] is False


def test_a_normal_grant_is_still_active():
    """The guard must not swallow the ordinary case."""
    tokens = [FakeToken(grant_id="g1", token_type="access", scope="read")]

    html = _render_panel([FakeClient(scope="read")], tokens)

    assert "No vault scope" not in html
    assert 'class="badge badge-green"' in html


# --- and the enforcement boundary rejects a legacy token ------------------


def test_middleware_source_rejects_a_token_with_no_vault_scope():
    """No path can mint one now, but a pre-fix client could already hold one.

    `src/mcp_server/auth.py` mapped every non-`readwrite` scope to `read`, so
    an `offline_access`-only token authenticated with read access to the whole
    vault. The boundary has to reject it, not just the mint sites.
    """
    from src.mcp_server import auth as mcp_auth

    with open(mcp_auth.__file__) as fh:
        source = fh.read()
    assert "has_vault_scope(oauth_token.scope)" in source
    assert "no_vault_scope" in source
