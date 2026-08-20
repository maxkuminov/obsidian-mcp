"""Issue #67: no path may raise a grant above the client's registration.

`OAuthClient.scope` is what the OAuth path treats as authoritative: the consent
screen gates the Read+Write radio on it, and `authorize_post` re-clamps the
posted form value precisely so a read-only-registered client cannot mint a
readwrite code. The panel bypassed both -- `update_oauth_token_scope` validated
the submitted value against the literal tuple `('read', 'readwrite')` and wrote
it straight onto the token.

And it was **permanent**: `_handle_refresh` re-minted with the token's own
scope and never re-clamped, so a panel-granted `readwrite` on the refresh row
survived every rotation indefinitely, not just the access token's hour.

The registration is a statement about that software. These tests pin the three
places that statement now holds: the panel handler, the rendered options, and
rotation.
"""
import asyncio
import json

import pytest

from src.control_panel import routes as panel
from src.mcp_server import auth as mcp_auth
from src.models.db import OAuthToken
from src.oauth import routes as oauth
from src.oauth.scope import clamp_scope, client_can_write, token_has_write

from _oauth_grant_fakes import (
    SeqSession,
    FakeClient,
    FakeRequest,
    FakeSession,
    FakeToken,
    SingleUserSentinel,
    in_hours,
)

READ_ONLY = "read"
FULL = "read readwrite offline_access"
REFRESH_SECRET = "r" * 64


def family(scope="read", grant_id="g1"):
    return [
        FakeToken(grant_id=grant_id, token_type="access", scope=scope, expires_at=in_hours(1)),
        FakeToken(
            grant_id=grant_id,
            token_type="refresh",
            scope=scope,
            expires_at=in_hours(720),
            token_hash=oauth._hash(REFRESH_SECRET),
        ),
    ]


def set_scope(session, token_id, scope):
    return asyncio.run(
        panel.update_oauth_token_scope(
            token_id=token_id, scope=scope, session=session, user=SingleUserSentinel()
        )
    )


# --- the panel handler refuses to exceed the registration ----------------


def test_read_only_client_cannot_be_raised_to_readwrite_from_the_panel():
    """The failing scenario in the issue, at the handler.

    A DCR client registered `"scope": "read"` gets `oauth_clients.scope =
    'read'`. Selecting `readwrite` in the panel used to commit it with no
    clamp, and `src/mcp_server/auth.py` then granted write.
    """
    tokens = family(scope="read")
    session = FakeSession(clients=[FakeClient(scope=READ_ONLY)], tokens=tokens)

    set_scope(session, tokens[0].id, "readwrite")

    for token in tokens:
        assert token.scope == "read"
        assert token_has_write(token.scope) is False
    assert session.committed == 0, "nothing should have been written"


def test_readwrite_registered_client_may_still_be_raised():
    tokens = family(scope="read")
    session = FakeSession(clients=[FakeClient(scope=FULL)], tokens=tokens)

    set_scope(session, tokens[0].id, "readwrite")

    for token in tokens:
        assert token_has_write(token.scope) is True


def test_downgrade_is_always_allowed_whatever_the_registration():
    """`readwrite` implies `read`; narrowing must never be refused."""
    tokens = family(scope="readwrite")
    session = FakeSession(clients=[FakeClient(scope=FULL)], tokens=tokens)

    set_scope(session, tokens[0].id, "read")

    for token in tokens:
        assert token_has_write(token.scope) is False


def test_offline_access_is_not_kept_past_a_registration_that_lost_it():
    """The written value is clamped, not just the read/readwrite choice."""
    tokens = family(scope="offline_access read")
    session = FakeSession(clients=[FakeClient(scope=READ_ONLY)], tokens=tokens)

    set_scope(session, tokens[0].id, "read")

    for token in tokens:
        assert set(token.scope.split()) == {"read"}


def test_missing_client_row_is_a_404_not_an_unclamped_write():
    """Without a registration there is nothing to clamp against, so refuse."""
    from fastapi import HTTPException

    tokens = family(scope="read")
    session = FakeSession(clients=[], tokens=tokens)

    with pytest.raises(HTTPException) as exc:
        set_scope(session, tokens[0].id, "readwrite")

    assert exc.value.status_code == 404
    assert all(t.scope == "read" for t in tokens)


# --- the template never offers an option the client cannot hold ----------


def render(clients, tokens):
    session = FakeSession(clients=clients, tokens=tokens)
    response = asyncio.run(
        panel.oauth_page(
            request=FakeRequest(), session=session, user=SingleUserSentinel()
        )
    )
    return response.body.decode()


def test_readwrite_option_is_absent_for_a_read_only_client():
    html = render([FakeClient(scope=READ_ONLY)], family(scope="read"))
    assert '<option value="read"' in html
    assert '<option value="readwrite"' not in html


def test_readwrite_option_is_present_for_a_write_registered_client():
    html = render([FakeClient(scope=FULL)], family(scope="read"))
    assert '<option value="readwrite"' in html


# --- rotation re-clamps, so nothing above the registration is permanent --


def test_refresh_reclamps_a_scope_above_the_registration(monkeypatch):
    """The "permanent" half of the issue.

    Even if a token somehow carries `readwrite` under a read-only client --
    a pre-fix panel write, or a registration narrowed after the grant -- the
    next rotation must not carry it forward.
    """
    tokens = family(scope="offline_access readwrite")
    session = FakeSession(clients=[FakeClient(scope=READ_ONLY)], tokens=tokens)
    monkeypatch.setattr(oauth, "async_session", lambda: session)

    response = asyncio.run(oauth._handle_refresh({"refresh_token": REFRESH_SECRET}))

    assert response.status_code == 200
    assert "readwrite" not in json.loads(response.body)["scope"].split()
    minted = [o for o in session.added if isinstance(o, OAuthToken)]
    assert len(minted) == 2
    for token in minted:
        assert token_has_write(token.scope) is False


def test_refresh_keeps_a_legitimate_readwrite_grant(monkeypatch):
    """The clamp must not quietly downgrade a grant the client is entitled to."""
    tokens = family(scope="offline_access readwrite")
    session = FakeSession(clients=[FakeClient(scope=FULL)], tokens=tokens)
    monkeypatch.setattr(oauth, "async_session", lambda: session)

    response = asyncio.run(oauth._handle_refresh({"refresh_token": REFRESH_SECRET}))

    assert "readwrite" in json.loads(response.body)["scope"].split()
    for token in [o for o in session.added if isinstance(o, OAuthToken)]:
        assert token_has_write(token.scope) is True


# --- the authorization-code exchange clamps too --------------------------


def test_auth_code_exchange_clamps_against_the_registration(monkeypatch):
    """The last write path, closed for the same reason as the other two.

    `authorize_post` already clamps what it writes onto the code, so this is
    normally a no-op -- but it is the only thing between a code minted under
    one registration and a token minted under a narrower one, and "normally a
    no-op" is not a guarantee.
    """
    from datetime import datetime, timedelta, timezone

    from src.models.db import OAuthClient, OAuthCode

    verifier = "v" * 64
    client = OAuthClient(
        client_id="client123",
        client_secret_hash=None,
        token_endpoint_auth_method="none",
        client_name="Claude",
        redirect_uris=["https://example.test/cb"],
        scope=READ_ONLY,
    )
    code = OAuthCode(
        code_hash=oauth._hash("the-code"),
        client_id=client.client_id,
        redirect_uri=client.redirect_uris[0],
        # A code carrying more than the registration allows — however it got
        # there, it must not become a token.
        scope="offline_access readwrite",
        code_challenge=oauth._base64url_sha256(verifier),
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
                "code_verifier": verifier,
                "redirect_uri": client.redirect_uris[0],
            }
        )
    )

    assert response.status_code == 200
    assert "readwrite" not in json.loads(response.body)["scope"].split()
    minted = [o for o in session.added if isinstance(o, OAuthToken)]
    assert len(minted) == 2
    for token in minted:
        assert token_has_write(token.scope) is False


# --- the helpers, and the fact that every surface uses them --------------


def test_client_can_write_is_membership_not_equality():
    assert client_can_write("read readwrite offline_access") is True
    assert client_can_write("readwrite") is True
    assert client_can_write("read") is False
    assert client_can_write("read offline_access") is False
    assert client_can_write("") is False
    assert client_can_write(None) is False


def test_clamp_scope_never_widens():
    assert clamp_scope("readwrite", "read") == "read"
    assert clamp_scope("read", "read") == "read"
    assert clamp_scope("readwrite", "readwrite") == "readwrite"
    # readwrite registration implies read availability
    assert clamp_scope("read", "readwrite") == "read"
    # A client registered read-only that asks for readwrite is downgraded,
    # not refused — that is what a clamp means (issue #21).
    assert clamp_scope("readwrite offline_access", "read") == "read"
    # But "no vault scope on either side" is a refusal, never a fallback.
    assert clamp_scope("", "read") == ""
    assert clamp_scope("read", "offline_access") == ""
    assert clamp_scope("offline_access", "offline_access") == ""


def test_no_surface_keeps_a_private_copy_of_the_membership_test():
    """The layers that used to each carry their own `"readwrite" in …`.

    They agreed by coincidence, which is not a property anything could test --
    and it stopped holding the moment the panel grew a fourth path with no
    clamp at all. `authorize_get` is deliberately not checked here: its
    `"readwrite" in scope_parts` reads the *requested* scope off the query
    string, which is a different question from what a client or a token holds.
    """
    for module in (panel, mcp_auth):
        with open(module.__file__) as fh:
            body = fh.read()
        assert '"readwrite" in ' not in body, module.__file__
    # And the OAuth routes' historical private name is the shared helper
    # itself, not a second implementation kept in step by hand.
    assert oauth._clamp_scope is clamp_scope
