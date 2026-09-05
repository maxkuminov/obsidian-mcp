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

from sqlalchemy.sql.dml import Update

from _oauth_grant_fakes import (
    ADVISORY_LOCK_SQL,
    FakeClient,
    FakeSession,
    FakeToken,
    in_hours,
)


class TracingSession(FakeSession):
    """A `FakeSession` that records *when* things happened, and can fail.

    Two properties need more than the base fake. Ordering: "the revoking
    UPDATE runs while the family lock is held" is not shown by "a lock was
    taken at some point" -- the handler took that lock before the fix too, so
    the sequence has to be observed. Failure: the reuse path's promise is that
    no database failure after detection can change the response, which cannot
    be tested without one.
    """

    def __init__(
        self,
        *args,
        raise_on_update: Exception | None = None,
        raise_on_commit: Exception | None = None,
        raise_on_rollback: Exception | None = None,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.trace: list[str] = []
        self._raise_on_update = raise_on_update
        self._raise_on_commit = raise_on_commit
        self._raise_on_rollback = raise_on_rollback

    async def execute(self, stmt, params=None):
        if " ".join(str(stmt).split()) == ADVISORY_LOCK_SQL:
            self.trace.append(f"lock:{params['key']}")
        elif isinstance(stmt, Update):
            self.trace.append("update")
            if self._raise_on_update is not None:
                raise self._raise_on_update
        return await super().execute(stmt, params)

    async def commit(self):
        self.trace.append("commit")
        if self._raise_on_commit is not None:
            raise self._raise_on_commit
        return await super().commit()

    async def rollback(self):
        self.trace.append("rollback")
        if self._raise_on_rollback is not None:
            raise self._raise_on_rollback
        return await super().rollback()

    def arm(self, *, update=None, commit=None, rollback=None):
        """Start failing from here on, after the set-up rotation has run."""
        self._raise_on_update = update
        self._raise_on_commit = commit
        self._raise_on_rollback = rollback


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


def records_named(caplog, event: str) -> list[logging.LogRecord]:
    """Records for one event, matched on the message the operator sees.

    The process formatter is `%(message)s` (`src/main.py`), so an identifier
    that lives only in `extra` never reaches an operator. Matching on the
    rendered message is therefore also an assertion that the message carries
    the event name at all.
    """
    return [
        record
        for record in caplog.records
        if record.getMessage().startswith(event)
    ]


def reuse_records(caplog) -> list[logging.LogRecord]:
    return records_named(caplog, "oauth.refresh_reuse_detected")


def failure_records(caplog) -> list[logging.LogRecord]:
    return records_named(caplog, "oauth.refresh_reuse_revocation_failed")


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


def test_the_replay_answers_the_same_status_headers_and_body_as_an_unknown_token(
    monkeypatch,
):
    """Constant response: same status, same headers, same bytes.

    Timing is deliberately *not* claimed -- the detection path takes a lock,
    reads twice and writes, so it is measurably slower than the not-found
    refusal. That residual is accepted and recorded in
    `docs/architecture/oauth-and-grants.md`; what must not differ is anything
    the response itself says.
    """
    family = live_family()
    session = FakeSession(clients=[FakeClient()], tokens=family)
    rotate_once(session, monkeypatch)

    replay = refresh(session, monkeypatch)
    unknown = refresh(session, monkeypatch, token=UNKNOWN_SECRET)

    assert replay.status_code == unknown.status_code == 400
    assert body(replay) == body(unknown) == {"error": "invalid_grant"}
    assert bytes(replay.body) == bytes(unknown.body)
    assert dict(replay.headers) == dict(unknown.headers)


def test_the_revoking_write_happens_while_the_family_lock_is_held(monkeypatch):
    """Ordering, observed as a sequence -- not "a lock was taken at some point".

    The handler took this lock before the fix too, so a test that only counts
    acquisitions passes on the broken code. What has to hold is that the
    revoking UPDATE runs *between* the acquisition and the commit that
    releases it: a transaction-scoped advisory lock is released by COMMIT or
    ROLLBACK, so an UPDATE after either would be an unlocked family write --
    and a concurrent refresh could insert a pair its snapshot cannot see,
    exactly the #64 failure.
    """
    family = live_family()
    session = TracingSession(clients=[FakeClient()], tokens=family)
    rotate_once(session, monkeypatch)
    del session.trace[:]

    refresh(session, monkeypatch)

    lock = f"lock:{grant_lock_key('g1')}"
    assert lock in session.trace, session.trace
    assert "update" in session.trace, session.trace
    held = session.trace[session.trace.index(lock) : session.trace.index("update")]
    assert "commit" not in held and "rollback" not in held, session.trace
    assert session.trace[session.trace.index("update") + 1] == "commit"


def test_a_replay_with_a_wrong_client_id_still_revokes_the_family(
    monkeypatch, caplog
):
    """`client_id` is the caller's claim, not a filter on the evidence.

    Both lookups used to carry the caller-supplied `client_id`, so a thief who
    presented the stolen token with any other (or a garbage) `client_id` made
    the row look *unknown*: the family survived the very replay that proved
    the token had leaked, and nothing was logged. The token hash is what
    identifies the family; identity is checked against the row afterwards.
    """
    family = live_family()
    session = FakeSession(clients=[FakeClient()], tokens=family)
    rotate_once(session, monkeypatch)

    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        replay = refresh(session, monkeypatch, client_id="some-other-client")

    assert replay.status_code == 400
    assert body(replay) == {"error": "invalid_grant"}
    assert all(t.revoked for t in session.tokens), session.tokens
    assert len(reuse_records(caplog)) == 1


def test_an_expired_and_rotated_refresh_token_is_still_reuse(monkeypatch, caplog):
    """Revocation is checked before expiry, and it has to be.

    A replay of a token that has since also passed its expiry is the same
    evidence of a leak; letting the expiry check answer first would give a
    patient thief a free window -- wait out the 30 days, then replay.
    """
    family = live_family(refresh_expires=in_hours(-1))
    family[1].revoked = True  # rotated away before it expired
    session = FakeSession(clients=[FakeClient()], tokens=family)

    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        response = refresh(session, monkeypatch)

    assert response.status_code == 400
    assert body(response) == {"error": "invalid_grant"}
    assert all(t.revoked for t in family), "the live access token must die too"
    assert len(reuse_records(caplog)) == 1


# --- no failure after detection may change the answer --------------------


def test_a_failed_revocation_keeps_the_response_and_logs_no_token_material(
    monkeypatch, caplog
):
    """The write can fail; the answer and the secrecy of the hash cannot.

    A SQLAlchemy error renders the failing statement *and its bound
    parameters*, and the engine does not set `hide_parameters` -- so one of
    those parameters is the token hash. Neither the exception text nor
    `exc_info` may reach the log.
    """
    family = live_family()
    session = TracingSession(clients=[FakeClient()], tokens=family)
    rotate_once(session, monkeypatch)
    session.arm(
        update=RuntimeError(
            "UPDATE oauth_tokens ... [parameters: "
            f"{{'token_hash_1': '{oauth._hash(REFRESH_SECRET)}'}}]"
        )
    )

    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        response = refresh(session, monkeypatch)

    assert response.status_code == 400
    assert body(response) == {"error": "invalid_grant"}

    (record,) = failure_records(caplog)
    assert record.exc_info is None, "a traceback would carry the statement"
    assert record.error == "RuntimeError"
    assert reuse_records(caplog) == [], "nothing was revoked, so nothing fired"
    rendered = record.getMessage() + " ".join(
        str(value) for value in record.__dict__.values()
    )
    assert oauth._hash(REFRESH_SECRET) not in rendered
    assert REFRESH_SECRET not in rendered


def test_a_failing_rollback_after_a_failed_commit_still_answers_invalid_grant(
    monkeypatch,
):
    """Even the recovery is allowed to fail. The response is decided here."""
    family = live_family()
    session = TracingSession(clients=[FakeClient()], tokens=family)
    rotate_once(session, monkeypatch)
    session.arm(
        commit=RuntimeError("commit failed"),
        rollback=RuntimeError("rollback failed"),
    )

    response = refresh(session, monkeypatch)

    assert response.status_code == 400
    assert body(response) == {"error": "invalid_grant"}
    assert "commit" in session.trace and session.trace[-1] == "rollback"


# --- what must NOT trigger it -------------------------------------------


def test_a_live_token_with_a_mismatched_client_id_is_refused_without_revoking(
    monkeypatch, caplog
):
    """The other half of the `client_id` rule, pinned so it cannot drift.

    A *live* refresh token presented with the wrong `client_id` is a confused
    or misconfigured client, not evidence of a leak. It stays the refusal it
    has always been, and it must not cost the user their grant -- otherwise
    any third party who learns a client_id can end a session by guessing.
    """
    family = live_family()
    session = FakeSession(clients=[FakeClient()], tokens=family)

    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        response = refresh(session, monkeypatch, client_id="some-other-client")

    assert response.status_code == 400
    assert body(response) == {"error": "invalid_grant"}
    assert not any(t.revoked for t in family)
    assert minted(session) == []
    assert session.committed == 0
    assert reuse_records(caplog) == []


def test_the_matching_client_id_still_rotates(monkeypatch):
    """The identity check moved; it did not become stricter."""
    family = live_family()
    session = FakeSession(clients=[FakeClient()], tokens=family)

    response = refresh(session, monkeypatch, client_id="client123")

    assert response.status_code == 200
    assert len(minted(session)) == 2


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

    # The identifiers must survive the process formatter, which is
    # `%(message)s` (`src/main.py`): anything left in `extra` alone is invisible
    # to the operator who has to act on the alarm.
    message = record.getMessage()
    assert "client_id=client123" in message
    assert "grant_id=g1" in message
    assert "user_id=7" in message
    assert f"revoked_tokens={record.revoked_tokens}" in message

    rendered = " ".join(
        str(value) for value in record.__dict__.values()
    ) + message
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
