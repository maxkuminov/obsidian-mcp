"""Issue #68: a client bound to one user must not mint grants for another.

An OAuth client binds to its **first** authorizing user and never rebinds
(`src/oauth/routes.py`: "Subsequent authorizes for the same client leave
`user_id` alone"). The enforcement layer authenticates on the `OAuthToken` row
alone and never consults the client's owner, so a second user who authorized
the same `client_id` got live tokens under a client they do not own:

* `oauth_page` filters clients by `OAuthClient.user_id`, so their own live
  readwrite grant rendered as "No OAuth clients registered" -- invisible and
  unrevokable in the only UI they have;
* the owner's "Delete this client and revoke all its tokens?" cascades through
  `oauth_tokens.client_id ondelete=CASCADE` and silently kills it.

The issue offers two fixes and prefers (b): refuse the reuse at the source,
which also closes the converse hazard. Unioning the client list instead (a)
would have to gate every client-level action, and the first draft of that idea
renders the owner's Delete button to the other user.

Preconditions are narrow -- `multi_user_mode` defaults to False -- so the
single-user path is pinned here too: it must be completely unaffected.
"""
import asyncio
import json

import pytest
from fastapi.responses import RedirectResponse

from src.oauth import routes as oauth

from _oauth_grant_fakes import SeqSession

REGISTERED_URI = "https://client.example.com/callback"


class _FakeClient:
    def __init__(self, user_id=None, scope="read readwrite offline_access"):
        self.client_id = "client123"
        self.scope = scope
        self.redirect_uris = [REGISTERED_URI]
        self.user_id = user_id


class _Result:
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

    async def __aexit__(self, *_exc):
        return False

    async def execute(self, _stmt):
        return _Result(self._client)

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.committed = True


class _FakeRequest:
    def __init__(self, signed_cookie):
        self.cookies = {"oauth_state": signed_cookie}
        self.session = {}


class _SessionUser:
    def __init__(self, uid):
        self.id = uid


def approve(client, *, multi_user, session_user_id=None, action="approve"):
    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(oauth.settings, "multi_user_mode", multi_user, raising=False)
        session = _FakeSession(client)
        monkeypatch.setattr(oauth, "async_session", lambda: session)

        async def _resolve(_request, _session):
            return _SessionUser(session_user_id) if session_user_id else None

        monkeypatch.setattr(oauth, "get_active_session_user", _resolve)

        server_state = "csrfstatetoken1234567890"
        signed = oauth._state_serializer().dumps(server_state)
        response = asyncio.run(
            oauth.authorize_post(
                _FakeRequest(signed),
                action=action,
                client_id="client123",
                redirect_uri=REGISTERED_URI,
                code_challenge="A" * 43,
                code_challenge_method="S256",
                scope="readwrite",
                state=server_state,
                client_state="clientecho",
            )
        )
        return response, session
    finally:
        monkeypatch.undo()


def error(response) -> dict:
    return json.loads(response.body)


# --- the refusal ----------------------------------------------------------


def test_second_user_cannot_authorize_a_client_owned_by_someone_else():
    """User B approving user A's client mints nothing at all."""
    response, session = approve(
        _FakeClient(user_id=1), multi_user=True, session_user_id=2
    )

    assert response.status_code == 403
    assert error(response)["error"] == "access_denied"
    assert session.added == [], "no authorization code may be minted"
    assert session.committed is False


def test_the_refusal_does_not_rebind_the_client():
    """The owner's binding is left exactly as it was."""
    client = _FakeClient(user_id=1)
    approve(client, multi_user=True, session_user_id=2)

    assert client.user_id == 1


def test_the_owner_can_still_authorize_their_own_client():
    response, session = approve(
        _FakeClient(user_id=7), multi_user=True, session_user_id=7
    )

    assert isinstance(response, RedirectResponse)
    assert len(session.added) == 1
    assert session.added[0].user_id == 7


def test_an_unbound_client_is_claimed_by_its_first_authorizer():
    """First /authorize wins is unchanged -- RFC 7591 registration is
    unauthenticated, so there is nothing to bind at registration time."""
    client = _FakeClient(user_id=None)
    response, session = approve(client, multi_user=True, session_user_id=5)

    assert isinstance(response, RedirectResponse)
    assert client.user_id == 5
    assert session.added[0].user_id == 5


def test_single_user_mode_is_unaffected():
    """`multi_user_mode` defaults to False and there is no second identity.

    A legacy client carrying a stale `user_id` must not lock the single-user
    deployment out of its own connector.
    """
    for owner in (None, 1):
        response, session = approve(_FakeClient(user_id=owner), multi_user=False)

        assert isinstance(response, RedirectResponse), owner
        assert len(session.added) == 1
        assert session.added[0].user_id is None


def test_denial_still_redirects_rather_than_403ing():
    """A user who clicks Deny gets the protocol's access_denied redirect.

    The cross-user check sits after the deny branch on purpose: the client is
    entitled to a redirect it can parse, and nothing is minted either way.
    """
    response, session = approve(
        _FakeClient(user_id=1), multi_user=True, session_user_id=2, action="deny"
    )

    assert isinstance(response, RedirectResponse)
    assert "error=access_denied" in response.headers["location"]
    assert session.added == []


# --- the exchange re-checks, closing the unbound-client race --------------


def _exchange(client_user_id, code_user_id):
    from datetime import datetime, timedelta, timezone

    from src.models.db import OAuthClient, OAuthCode

    verifier = "v" * 64
    client = OAuthClient(
        client_id="client123",
        client_secret_hash=None,
        token_endpoint_auth_method="none",
        client_name="Claude",
        redirect_uris=[REGISTERED_URI],
        scope="read readwrite offline_access",
        user_id=client_user_id,
    )
    code = OAuthCode(
        code_hash=oauth._hash("the-code"),
        client_id=client.client_id,
        redirect_uri=REGISTERED_URI,
        scope="read",
        code_challenge=oauth._base64url_sha256(verifier),
        code_challenge_method="S256",
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        used=False,
        user_id=code_user_id,
    )
    session = SeqSession([code, client])
    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(oauth, "async_session", lambda: session)
        response = asyncio.run(
            oauth._handle_auth_code(
                {
                    "code": "the-code",
                    "code_verifier": verifier,
                    "redirect_uri": REGISTERED_URI,
                }
            )
        )
        return response, session, code
    finally:
        monkeypatch.undo()


def test_a_code_cannot_be_exchanged_under_a_client_another_user_claimed():
    """`authorize_post` claims an unbound client in the same transaction as
    the code, so two users consenting to the same unbound client at once both
    get a code and only one claim wins. The loser's code must not become
    tokens under a client they do not own."""
    response, session, code = _exchange(client_user_id=1, code_user_id=2)

    assert response.status_code == 400
    assert error(response)["error"] == "invalid_grant"
    assert session.added == []
    assert code.used is False, "a refused exchange must not burn the code"


def test_the_matching_owner_exchanges_normally():
    response, session, _ = _exchange(client_user_id=7, code_user_id=7)

    assert response.status_code == 200
    assert len(session.added) == 2


def test_a_single_user_code_is_unaffected_by_a_claimed_client():
    """A code minted before multi-user mode carries a NULL `user_id`; the
    bootstrap may since have claimed the client. That must still exchange."""
    response, session, _ = _exchange(client_user_id=1, code_user_id=None)

    assert response.status_code == 200
    assert len(session.added) == 2


# --- the helper -----------------------------------------------------------


def test_client_belongs_to_another_user_helper():
    assert oauth._client_belongs_to_another_user(_FakeClient(user_id=1), 2) is True
    assert oauth._client_belongs_to_another_user(_FakeClient(user_id=1), 1) is False
    # Unbound client: about to be claimed by its first authorizer.
    assert oauth._client_belongs_to_another_user(_FakeClient(user_id=None), 2) is False
    # No session identity: single-user mode, no other users to conflict with.
    assert oauth._client_belongs_to_another_user(_FakeClient(user_id=1), None) is False
