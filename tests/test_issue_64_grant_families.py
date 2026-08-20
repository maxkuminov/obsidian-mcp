"""Issue #64: revocation and downgrade must act on the grant, not one row.

Before `oauth_tokens.grant_id` (migration 014) nothing tied an access token to
the refresh token minted beside it, so the panel could only offer per-row
controls -- and both of them were near no-ops:

* **Revoke** flipped one row. `_handle_refresh` resolves purely on
  `token_hash` + `token_type` + `revoked`, so the untouched sibling refresh
  token minted a fresh, identically-scoped pair on the client's next ordinary
  401 retry. Access tokens live one hour, so per-row Revoke on an access token
  bought at most that hour and then restored the identical capability.
* **Downgrade** wrote one row's scope, and `_handle_refresh` copies the
  *refresh* token's scope -- so a downgrade applied to the access row silently
  restored itself on the next rotation. That is worse than the display bug #62
  fixed: it hands back write access the owner believed they had removed.

Every test here is written against the specific claims an adversarial reviewer
was told to look for: a revocation that does not stick after a refresh, a
downgrade that reverts, and a family write that reaches a grant it should not.
"""
import asyncio
import json

import pytest
from fastapi.responses import RedirectResponse

from src.control_panel import routes as panel
from src.models.db import OAuthToken
from src.oauth import routes as oauth
from src.oauth.grants import grant_lock_key, new_grant_id

from _oauth_grant_fakes import (
    SeqSession,
    FakeClient,
    FakeSession,
    FakeToken,
    SingleUserSentinel,
    in_hours,
)


REFRESH_SECRET = "r" * 64
OTHER_REFRESH_SECRET = "s" * 64


def refresh(session, monkeypatch, token=REFRESH_SECRET, client_id=None):
    monkeypatch.setattr(oauth, "async_session", lambda: session)
    form = {"refresh_token": token}
    if client_id:
        form["client_id"] = client_id
    return asyncio.run(oauth._handle_refresh(form))


def body(response) -> dict:
    return json.loads(response.body)


def minted(session) -> list[OAuthToken]:
    return [obj for obj in session.added if isinstance(obj, OAuthToken)]


def live_family(grant_id="g1", scope="offline_access readwrite", user_id=None):
    """The two rows one consent produces."""
    return [
        FakeToken(
            grant_id=grant_id,
            token_type="access",
            scope=scope,
            user_id=user_id,
            expires_at=in_hours(1),
        ),
        FakeToken(
            grant_id=grant_id,
            token_type="refresh",
            scope=scope,
            user_id=user_id,
            expires_at=in_hours(720),
            token_hash=oauth._hash(REFRESH_SECRET),
        ),
    ]


# --- the identifier is stamped at issue and inherited by rotation ---------


def test_auth_code_exchange_puts_both_tokens_in_one_grant(monkeypatch):
    """One consent event, one family. Not two, and not none."""
    from datetime import datetime, timedelta, timezone

    from src.models.db import OAuthClient, OAuthCode

    verifier = "v" * 64
    client = OAuthClient(
        client_id="client123",
        client_secret_hash=None,
        token_endpoint_auth_method="none",
        client_name="Claude",
        redirect_uris=["https://example.test/cb"],
        scope="read readwrite offline_access",
    )
    code = OAuthCode(
        code_hash=oauth._hash("the-code"),
        client_id=client.client_id,
        redirect_uri=client.redirect_uris[0],
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
    pair = minted(session)
    assert len(pair) == 2
    assert {t.token_type for t in pair} == {"access", "refresh"}
    grants = {t.grant_id for t in pair}
    assert len(grants) == 1, "the pair must share one grant_id"
    assert grants != {None} and all(t.grant_id for t in pair)


def test_rotation_stays_inside_the_grant_it_rotated(monkeypatch):
    session = FakeSession(clients=[FakeClient()], tokens=live_family())
    response = refresh(session, monkeypatch)

    assert response.status_code == 200
    pair = minted(session)
    assert len(pair) == 2
    assert {t.grant_id for t in pair} == {"g1"}


def test_grant_ids_are_opaque_and_distinct():
    assert new_grant_id() != new_grant_id()
    assert len(new_grant_id()) >= 22


# --- revocation sticks, including across a refresh -----------------------


def revoke_via_panel(session, token_id):
    return asyncio.run(
        panel.revoke_oauth_token(
            token_id=token_id,
            session=session,
            user=SingleUserSentinel(),
        )
    )


def test_revoking_the_access_row_kills_the_refresh_token_too():
    """The headline defect. One click, both rows, no survivor to rotate with."""
    family = live_family()
    session = FakeSession(clients=[FakeClient()], tokens=family)

    response = revoke_via_panel(session, family[0].id)  # the *access* row

    assert isinstance(response, RedirectResponse)
    assert all(t.revoked for t in family)
    assert session.committed == 1


def test_a_revoked_grant_cannot_be_refreshed(monkeypatch):
    """The property an auditor was told to attack: revocation after refresh.

    Revoking used to leave `_handle_refresh` a live refresh token to work
    with, so the client's next 401 retry silently restored the grant.
    """
    family = live_family()
    session = FakeSession(clients=[FakeClient()], tokens=family)
    revoke_via_panel(session, family[0].id)

    response = refresh(session, monkeypatch)

    assert response.status_code == 400
    assert body(response)["error"] == "invalid_grant"
    assert minted(session) == []


def test_revocation_reaches_a_pair_minted_by_an_earlier_rotation(monkeypatch):
    """Rotation leaves the previous access token live; revocation must not.

    `_handle_refresh` deliberately lets the old access token run to its natural
    expiry -- fine for rotation, wrong for revocation. Every row in the family
    that is still usable has to die.
    """
    family = live_family()
    session = FakeSession(clients=[FakeClient()], tokens=family)
    refresh(session, monkeypatch)

    # Model what the commit would have persisted: the rotated-in pair joins the
    # family, and the old refresh token is revoked.
    rotated = [
        FakeToken(
            grant_id=t.grant_id,
            token_type=t.token_type,
            scope=t.scope,
            user_id=t.user_id,
            expires_at=in_hours(1 if t.token_type == "access" else 720),
        )
        for t in minted(session)
    ]
    session.tokens.extend(rotated)

    revoke_via_panel(session, family[0].id)

    assert all(t.revoked for t in session.tokens), session.tokens


def test_revocation_does_not_touch_a_different_grant():
    """A second concurrent session of the same client is its own family."""
    keep = FakeToken(grant_id="g2", token_type="access", scope="readwrite")
    family = live_family()
    session = FakeSession(clients=[FakeClient()], tokens=family + [keep])

    revoke_via_panel(session, family[1].id)

    assert all(t.revoked for t in family)
    assert keep.revoked is False


def test_revocation_takes_the_family_lock_before_writing():
    """Ordering, not politeness.

    An `UPDATE ... WHERE grant_id = :g` takes its snapshot when the statement
    starts, so rows a concurrent refresh *inserts* afterwards are invisible to
    it. Locking the family first is the only thing that makes the revocation
    cover the pair the client rotated into a moment later.
    """
    family = live_family()
    session = FakeSession(clients=[FakeClient()], tokens=family)

    revoke_via_panel(session, family[0].id)

    assert session.locked_grants == [grant_lock_key("g1")]


def test_refresh_takes_the_same_lock_before_reading_the_family(monkeypatch):
    session = FakeSession(clients=[FakeClient()], tokens=live_family())
    refresh(session, monkeypatch)

    assert session.locked_grants == [grant_lock_key("g1")]


def test_grant_lock_key_is_stable_and_grant_specific():
    assert grant_lock_key("g1") == grant_lock_key("g1")
    assert grant_lock_key("g1") != grant_lock_key("g2")
    assert -(2 ** 63) <= grant_lock_key("g1") < 2 ** 63


# --- the RFC 7009 endpoint is grant-scoped too ---------------------------


class _FormRequest:
    def __init__(self, form):
        self._form = form

    async def form(self):
        return self._form


def test_rfc_revocation_endpoint_kills_the_whole_family(monkeypatch):
    """Otherwise `/revoke` on an access token is the same near no-op.

    RFC 7009 §2.1 explicitly permits revoking the associated refresh token,
    and anything narrower means a client told "revoked" keeps rotating.
    """
    family = live_family()
    access = family[0]
    access.token_hash = oauth._hash("access-secret")
    session = FakeSession(clients=[FakeClient()], tokens=family)
    monkeypatch.setattr(oauth, "async_session", lambda: session)

    response = asyncio.run(
        oauth.revoke_token.__wrapped__(_FormRequest({"token": "access-secret"}))
    )

    assert response.status_code == 200
    assert all(t.revoked for t in family)


# --- a downgrade applies to the family and survives rotation -------------


def downgrade_via_panel(session, token_id, scope="read"):
    return asyncio.run(
        panel.update_oauth_token_scope(
            token_id=token_id,
            scope=scope,
            session=session,
            user=SingleUserSentinel(),
        )
    )


def test_downgrading_the_access_row_also_downgrades_the_refresh_row():
    family = live_family(scope="offline_access readwrite")
    session = FakeSession(clients=[FakeClient()], tokens=family)

    downgrade_via_panel(session, family[0].id)  # the *access* row

    for token in family:
        assert "readwrite" not in token.scope.split(), token
        assert "read" in token.scope.split()


def test_a_downgrade_does_not_revert_on_the_next_refresh(monkeypatch):
    """The exact scenario in the issue, end to end.

    Owner flips the access row to `read`; the connector refreshes within the
    hour; the new pair must not come back `readwrite`.
    """
    family = live_family(scope="offline_access readwrite")
    session = FakeSession(clients=[FakeClient()], tokens=family)

    downgrade_via_panel(session, family[0].id)
    response = refresh(session, monkeypatch)

    assert response.status_code == 200
    assert "readwrite" not in body(response)["scope"].split()
    for token in minted(session):
        assert "readwrite" not in token.scope.split(), token


def test_downgrading_the_refresh_row_also_downgrades_the_live_access_token():
    """The issue's converse: the existing access token stayed write-capable."""
    family = live_family(scope="offline_access readwrite")
    session = FakeSession(clients=[FakeClient()], tokens=family)

    downgrade_via_panel(session, family[1].id)  # the *refresh* row

    assert "readwrite" not in family[0].scope.split()


def test_a_downgrade_does_not_reach_another_grant():
    other = FakeToken(grant_id="g2", token_type="access", scope="readwrite")
    family = live_family(scope="offline_access readwrite")
    session = FakeSession(clients=[FakeClient()], tokens=family + [other])

    downgrade_via_panel(session, family[0].id)

    assert other.scope == "readwrite"


def test_a_revoked_row_keeps_the_scope_it_died_with():
    """History must record what the token carried, not what replaced it."""
    dead = FakeToken(grant_id="g1", token_type="access", scope="readwrite", revoked=True)
    family = live_family(scope="offline_access readwrite")
    session = FakeSession(clients=[FakeClient()], tokens=[dead] + family)

    downgrade_via_panel(session, family[0].id)

    assert dead.scope == "readwrite"


def test_upgrading_is_also_family_wide():
    """Not only downgrades -- the control writes one scope for the grant."""
    family = live_family(scope="offline_access read")
    session = FakeSession(clients=[FakeClient()], tokens=family)

    downgrade_via_panel(session, family[0].id, scope="readwrite")

    for token in family:
        assert "readwrite" in token.scope.split()


def test_an_unknown_scope_value_writes_nothing():
    family = live_family(scope="offline_access readwrite")
    session = FakeSession(clients=[FakeClient()], tokens=family)

    downgrade_via_panel(session, family[0].id, scope="admin")

    assert session.committed == 0
    for token in family:
        assert "readwrite" in token.scope.split()


@pytest.mark.parametrize("handler", ["revoke", "scope"])
def test_family_writes_refuse_a_token_owned_by_another_user(handler):
    """`_assert_oauth_token_owner` still guards the handle, not just the row."""
    from fastapi import HTTPException

    family = live_family(user_id=1)
    session = FakeSession(clients=[FakeClient(user_id=1)], tokens=family)

    class _OtherUser:
        id = 2
        is_admin = False
        username = "mallory"

    with pytest.raises(HTTPException) as exc:
        if handler == "revoke":
            asyncio.run(
                panel.revoke_oauth_token(
                    token_id=family[0].id, session=session, user=_OtherUser()
                )
            )
        else:
            asyncio.run(
                panel.update_oauth_token_scope(
                    token_id=family[0].id,
                    scope="readwrite",
                    session=session,
                    user=_OtherUser(),
                )
            )

    assert exc.value.status_code == 403
    assert not any(t.revoked for t in family)
