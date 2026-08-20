"""RFC 7009 `/revoke` must authenticate the client, not just accept a token.

The endpoint did neither client authentication nor an ownership check: it
looked the token value up by hash and revoked. Widening revocation to the whole
grant family (issue #64) made that materially worse — a single leaked access
token now ends a 30-day refresh grant, not just its own remaining hour.

RFC 7009 §2.1 requires the client to authenticate. §2.2 is equally load-bearing
in the other direction: a token that is not valid *for the requesting client*
is answered HTTP 200 and nothing happens, because an unknown token, a foreign
token and an already-revoked one must be indistinguishable. The only case that
is a real error is naming the right client and failing to authenticate as it.

A missing `client_id` is tolerated exactly the way `/token` tolerates it — some
ChatGPT connector builds omit it — so the client is then identified by the
token itself, and a confidential client still has to present its secret.
"""
import asyncio
import json

import pytest

from src.oauth import routes as oauth

from _oauth_grant_fakes import FakeClient, FakeSession, FakeToken, in_hours

SECRET = "s" * 64
ACCESS_VALUE = "access-token-value"


class FormRequest:
    def __init__(self, form):
        self._form = form

    async def form(self):
        return self._form


def revoke(session, monkeypatch, **form):
    monkeypatch.setattr(oauth, "async_session", lambda: session)
    return asyncio.run(oauth.revoke_token.__wrapped__(FormRequest(form)))


def family(client_id="client123"):
    return [
        FakeToken(
            grant_id="g1",
            token_type="access",
            client_id=client_id,
            scope="readwrite",
            expires_at=in_hours(1),
            token_hash=oauth._hash(ACCESS_VALUE),
        ),
        FakeToken(
            grant_id="g1",
            token_type="refresh",
            client_id=client_id,
            scope="readwrite",
            expires_at=in_hours(720),
        ),
    ]


def public_client(client_id="client123"):
    return FakeClient(
        client_id=client_id,
        token_endpoint_auth_method="none",
        client_secret_hash=None,
    )


def confidential_client(client_id="client123"):
    return FakeClient(
        client_id=client_id,
        token_endpoint_auth_method="client_secret_post",
        client_secret_hash=oauth._hash(SECRET),
    )


# --- confidential clients must present their secret ----------------------


def test_confidential_client_without_a_secret_cannot_revoke(monkeypatch):
    """The headline hole: token possession alone ended the whole grant."""
    tokens = family()
    session = FakeSession(clients=[confidential_client()], tokens=tokens)

    response = revoke(session, monkeypatch, token=ACCESS_VALUE, client_id="client123")

    assert response.status_code == 401
    assert json.loads(response.body)["error"] == "invalid_client"
    assert not any(t.revoked for t in tokens)
    assert session.committed == 0


def test_confidential_client_with_a_wrong_secret_cannot_revoke(monkeypatch):
    tokens = family()
    session = FakeSession(clients=[confidential_client()], tokens=tokens)

    response = revoke(
        session,
        monkeypatch,
        token=ACCESS_VALUE,
        client_id="client123",
        client_secret="w" * 64,
    )

    assert response.status_code == 401
    assert not any(t.revoked for t in tokens)


def test_confidential_client_with_its_secret_revokes_the_family(monkeypatch):
    tokens = family()
    session = FakeSession(clients=[confidential_client()], tokens=tokens)

    response = revoke(
        session,
        monkeypatch,
        token=ACCESS_VALUE,
        client_id="client123",
        client_secret=SECRET,
    )

    assert response.status_code == 200
    assert all(t.revoked for t in tokens)


# --- public clients authenticate by possession, as the RFC intends -------


def test_public_client_may_still_revoke(monkeypatch):
    """A public PKCE client has no secret; the token *is* the credential."""
    tokens = family()
    session = FakeSession(clients=[public_client()], tokens=tokens)

    response = revoke(session, monkeypatch, token=ACCESS_VALUE, client_id="client123")

    assert response.status_code == 200
    assert all(t.revoked for t in tokens)


def test_public_client_may_omit_client_id(monkeypatch):
    """Same tolerance `/token` already grants some ChatGPT connector builds."""
    tokens = family()
    session = FakeSession(clients=[public_client()], tokens=tokens)

    response = revoke(session, monkeypatch, token=ACCESS_VALUE)

    assert response.status_code == 200
    assert all(t.revoked for t in tokens)


# --- naming a different client is a no-op with a 200 ---------------------


def test_a_foreign_client_id_revokes_nothing_and_still_returns_200(monkeypatch):
    """§2.2: a token not valid for the requesting client is not an error.

    Answering 401 or 404 here would turn the endpoint into an oracle for
    which client a token value belongs to.
    """
    tokens = family()
    session = FakeSession(clients=[public_client()], tokens=tokens)

    response = revoke(session, monkeypatch, token=ACCESS_VALUE, client_id="someone-else")

    assert response.status_code == 200
    assert response.body == b"{}"
    assert not any(t.revoked for t in tokens)
    assert session.committed == 0


def test_an_unknown_token_returns_200_without_a_lookup_result(monkeypatch):
    tokens = family()
    session = FakeSession(clients=[public_client()], tokens=tokens)

    response = revoke(session, monkeypatch, token="never-issued", client_id="client123")

    assert response.status_code == 200
    assert not any(t.revoked for t in tokens)


def test_a_missing_token_parameter_returns_200(monkeypatch):
    session = FakeSession(clients=[public_client()], tokens=family())

    response = revoke(session, monkeypatch, client_id="client123")

    assert response.status_code == 200


@pytest.mark.parametrize(
    "form, expected_status",
    [
        # The only real error: right client named, authentication failed.
        ({"token": ACCESS_VALUE, "client_id": "client123"}, 401),
        # Everything else the RFC calls indistinguishable.
        ({"token": ACCESS_VALUE, "client_id": "other"}, 200),
        ({"token": "unknown"}, 200),
    ],
)
def test_only_a_failed_authentication_is_an_error(monkeypatch, form, expected_status):
    session = FakeSession(clients=[confidential_client()], tokens=family())

    response = revoke(session, monkeypatch, **form)

    assert response.status_code == expected_status


# --- the shared client-auth predicate ------------------------------------


def test_client_authenticated_helper():
    assert oauth._client_authenticated(confidential_client(), SECRET) is True
    assert oauth._client_authenticated(confidential_client(), "w" * 64) is False
    assert oauth._client_authenticated(confidential_client(), None) is False
    assert oauth._client_authenticated(public_client(), None) is True
    # An unregistered method is a refusal, never a fallback.
    unknown = FakeClient(token_endpoint_auth_method="private_key_jwt")
    assert oauth._client_authenticated(unknown, SECRET) is False
