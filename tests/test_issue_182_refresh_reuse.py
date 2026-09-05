"""Issue #182 (ASVS V10.4.5): a replayed refresh token kills its grant family.

Rotation was already correct -- `_handle_refresh` mints a new pair and revokes
the refresh token it rotated. The missing half is what happens when the
*already rotated* token comes back: the handler resolved the family, held the
family lock, found no live row, and answered `invalid_grant` without touching
anything else.

That answer discards the one signal OAuth 2.1 / RFC 6819 §5.2.1.1 give for
detecting a stolen refresh token. Only one party can hold the current token
after a rotation, so a second presentation of a rotated one means two parties
hold the same credential. Production clients (claude.ai, ChatGPT) register
`token_endpoint_auth_method = "none"`, so possession is the whole credential:
whoever redeems first keeps an identically-scoped, silently-renewing pair --
`readwrite` over the vault, for a 30-day sliding window -- while the legitimate
client sees `invalid_grant` and quietly re-authorizes.

Everything below runs the production handler against the in-memory session
double from `_oauth_grant_fakes`, so the real `revoke_grant_family` UPDATE is
what the assertions observe.
"""
import asyncio
import json
import logging

import pytest

from src.models.db import OAuthToken
from src.oauth import routes as oauth
from src.oauth.grants import grant_lock_key

from _oauth_grant_fakes import (
    FakeClient,
    FakeSession,
    FakeToken,
    in_hours,
)


REFRESH_SECRET = "r" * 64
UNKNOWN_SECRET = "z" * 64
LOGGER_NAME = "src.oauth.routes"


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


def live_family(
    grant_id="g1",
    scope="offline_access readwrite",
    user_id=7,
    refresh_expires=None,
):
    """The two rows one consent produces, with a redeemable refresh token."""
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
            expires_at=refresh_expires or in_hours(720),
            token_hash=oauth._hash(REFRESH_SECRET),
        ),
    ]


def persist_rotation(session) -> list[FakeToken]:
    """Mirror into the fake table what the rotation's COMMIT would have stored.

    `_handle_refresh` calls `session.add(...)`, which the fake records but does
    not make queryable -- and a replay has to see the pair the thief rotated
    into, since revoking those rows is the entire point.
    """
    rows = [
        FakeToken(
            grant_id=token.grant_id,
            token_type=token.token_type,
            scope=token.scope,
            client_id=token.client_id,
            user_id=token.user_id,
            expires_at=in_hours(1 if token.token_type == "access" else 720),
            token_hash=token.token_hash,
        )
        for token in minted(session)
    ]
    session.tokens.extend(rows)
    del session.added[:]
    return rows


def rotate_once(session, monkeypatch):
    """One legitimate refresh; returns the new refresh token's raw value."""
    response = refresh(session, monkeypatch)
    assert response.status_code == 200, body(response)
    new_refresh = body(response)["refresh_token"]
    persist_rotation(session)
    return new_refresh


def reuse_records(caplog) -> list[logging.LogRecord]:
    return [
        record
        for record in caplog.records
        if record.getMessage() == "oauth.refresh_reuse_detected"
    ]


# --- the replay kills the family ----------------------------------------


def test_replaying_a_rotated_refresh_token_revokes_the_whole_family(
    monkeypatch, caplog
):
    """The headline defect: the rotated-away token comes back, nothing dies."""
    family = live_family()
    session = FakeSession(clients=[FakeClient()], tokens=family)
    rotate_once(session, monkeypatch)

    # Rotation leaves the old access token deliberately live and the old
    # refresh token revoked -- the state a replay arrives into.
    assert family[0].revoked is False
    assert family[1].revoked is True

    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        replay = refresh(session, monkeypatch)  # the ORIGINAL secret, again

    assert replay.status_code == 400
    assert body(replay) == {"error": "invalid_grant"}
    assert minted(session) == [], "a replay must not mint anything"
    assert all(t.revoked for t in session.tokens), session.tokens
    assert len(reuse_records(caplog)) == 1


def test_the_current_refresh_token_is_refused_after_the_replay(monkeypatch):
    """The thief's rotated pair is what the revocation exists to reach.

    Asserting only that the replay is rejected would pass on today's code. The
    property is that the family the replay identified has no usable token left
    -- including the pair the *first* redemption rotated into.
    """
    family = live_family()
    session = FakeSession(clients=[FakeClient()], tokens=family)
    current_refresh = rotate_once(session, monkeypatch)

    refresh(session, monkeypatch)  # the replay

    response = refresh(session, monkeypatch, token=current_refresh)

    assert response.status_code == 400
    assert body(response) == {"error": "invalid_grant"}
    assert minted(session) == [], "the rotated-in token must not rotate again"
    assert [t for t in session.tokens if not t.revoked] == []


def test_the_replay_response_is_indistinguishable_from_an_unknown_token(
    monkeypatch,
):
    """Constant response: the caller learns nothing about what it hit."""
    family = live_family()
    session = FakeSession(clients=[FakeClient()], tokens=family)
    rotate_once(session, monkeypatch)

    replay = refresh(session, monkeypatch)
    unknown = refresh(session, monkeypatch, token=UNKNOWN_SECRET)

    assert replay.status_code == unknown.status_code == 400
    assert body(replay) == body(unknown) == {"error": "invalid_grant"}
    assert bytes(replay.body) == bytes(unknown.body)


def test_the_revoking_replay_takes_the_family_lock(monkeypatch):
    """The write happens under the same lock rotation and the panel take.

    Without it, a concurrent refresh could insert a pair after this UPDATE's
    snapshot began and survive the revocation -- exactly the #64 failure.
    """
    family = live_family()
    session = FakeSession(clients=[FakeClient()], tokens=family)
    rotate_once(session, monkeypatch)
    del session.advisory_locks[:]

    refresh(session, monkeypatch)

    assert session.locked_grants
    assert set(session.locked_grants) == {grant_lock_key("g1")}


# --- what must NOT trigger it -------------------------------------------


def test_an_unknown_refresh_token_revokes_nothing(monkeypatch, caplog):
    """No row, no family, no write -- and no alarm to drown the real one in."""
    family = live_family()
    session = FakeSession(clients=[FakeClient()], tokens=family)

    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        response = refresh(session, monkeypatch, token=UNKNOWN_SECRET)

    assert response.status_code == 400
    assert body(response) == {"error": "invalid_grant"}
    assert not any(t.revoked for t in family)
    assert session.committed == 0
    assert reuse_records(caplog) == []


def test_an_expired_but_never_rotated_refresh_token_is_not_reuse(
    monkeypatch, caplog
):
    """Expiry is the token dying of old age, not evidence of a second holder.

    The row is still live (`revoked == False`), so the handler reaches its
    expiry check -- and revoking the family there would let any client that
    left a tab open for 30 days kill its own still-valid access token.
    """
    family = live_family(refresh_expires=in_hours(-1))
    session = FakeSession(clients=[FakeClient()], tokens=family)

    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        response = refresh(session, monkeypatch)

    assert response.status_code == 400
    assert body(response)["error"] == "invalid_grant"
    assert not any(t.revoked for t in family)
    assert session.committed == 0
    assert reuse_records(caplog) == []


def test_an_already_revoked_family_stays_revoked_with_the_same_answer(
    monkeypatch, caplog
):
    """A no-op, deliberately: nothing left to kill and nothing new to report."""
    family = live_family()
    for token in family:
        token.revoked = True
    session = FakeSession(clients=[FakeClient()], tokens=family)

    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        response = refresh(session, monkeypatch)

    assert response.status_code == 400
    assert body(response) == {"error": "invalid_grant"}
    assert all(t.revoked for t in family)
    assert session.committed == 0, "nothing changed, so nothing is committed"
    assert reuse_records(caplog) == []


def test_a_legitimate_rotation_still_succeeds(monkeypatch, caplog):
    """The ordinary path must not have acquired a new way to fail."""
    family = live_family()
    session = FakeSession(clients=[FakeClient()], tokens=family)

    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        response = refresh(session, monkeypatch)

    assert response.status_code == 200
    assert len(minted(session)) == 2
    assert family[0].revoked is False, "rotation still lets the access token run"
    assert reuse_records(caplog) == []


# --- what the record says (and what it must never say) -------------------


def test_the_reuse_record_names_the_grant_and_carries_no_token_material(
    monkeypatch, caplog
):
    family = live_family(user_id=7)
    session = FakeSession(clients=[FakeClient()], tokens=family)
    current_refresh = rotate_once(session, monkeypatch)

    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        refresh(session, monkeypatch)

    (record,) = reuse_records(caplog)
    assert record.levelno == logging.WARNING
    assert record.name == LOGGER_NAME
    assert record.event == "oauth.refresh_reuse_detected"
    assert record.client_id == "client123"
    assert record.grant_id == "g1"
    assert record.user_id == 7
    assert record.revoked_tokens >= 1

    rendered = " ".join(
        str(value) for value in record.__dict__.values()
    ) + record.getMessage()
    for secret in (REFRESH_SECRET, current_refresh, oauth._hash(REFRESH_SECRET)):
        assert secret not in rendered


@pytest.mark.parametrize("attempt", (1, 2))
def test_repeated_replays_report_the_family_once(monkeypatch, caplog, attempt):
    """The second replay finds a dead family: still refused, no new alarm."""
    family = live_family()
    session = FakeSession(clients=[FakeClient()], tokens=family)
    rotate_once(session, monkeypatch)

    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        for _ in range(attempt):
            response = refresh(session, monkeypatch)

    assert response.status_code == 400
    assert len(reuse_records(caplog)) == 1
