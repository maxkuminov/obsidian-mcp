"""#191 — the OAuth authorization server's decisions leave a record.

`/token` is the primary authentication surface for third-party AI clients here,
and before this change nothing tied a refusal or an issuance to a `client_id`,
a user or an address. `/revoke` was worse: RFC 7009 §2.2 requires the *response*
to conceal an unknown, foreign or already-dead token, so a no-op and a real
revocation were the same 200 with an empty body and no record either way.

What is asserted, in the order the design argues it:

* **Exactly one emission attempt per outcome**, counted at `acquire` with the
  suppressor forced open, carrying a `<rfc_code>.<sub_reason>` that says which
  check refused.
* **Provenance.** `client_id` may hold only a value read from a row;
  `client_id_submitted` is the only place the caller's form field appears;
  `user_id` and `grant_id` are simply **absent** on the early refusals that
  resolved neither.
* **After the commit** (D17) — a failed commit leaves no issuance, no rotation
  and no revocation record.
* **The response never moves.** The revocation no-ops keep their 200 with an
  empty body; every refusal keeps its status and error code.
"""
import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone

import pytest
from starlette.requests import Request

from src.models.db import OAuthCode, OAuthToken
from src.oauth import routes as oauth
from src.services import security_events

from _oauth_grant_fakes import (
    FakeClient,
    FakeSession,
    FakeToken,
    SeqSession,
    in_hours,
)

REGISTERED_URI = "https://client.example.com/callback"
VERIFIER = "v" * 64
CODE_SECRET = "the-code"
REFRESH_SECRET = "r" * 64
CLIENT_SECRET = "s" * 64


# --- capture --------------------------------------------------------------


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record):
        self.records.append(record)


class _Events:
    def __init__(self, records, acquires):
        self.records = records
        self.acquires = acquires

    @property
    def attempted(self) -> list[str]:
        return [event for event, _subject in self.acquires]

    def named(self, event: str) -> list[logging.LogRecord]:
        return [r for r in self.records if r.getMessage() == event]

    def one(self, event: str) -> logging.LogRecord:
        (record,) = self.named(event)
        return record


@pytest.fixture
def events(monkeypatch):
    handler = _Capture()
    logger = security_events.logger
    logger.addHandler(handler)
    propagate, level = logger.propagate, logger.level
    logger.propagate = False
    logger.setLevel(logging.DEBUG)

    acquires: list[tuple] = []
    real_acquire = security_events.acquire

    def counting_acquire(event, subject=None, **kwargs):
        acquires.append((event, subject))
        return real_acquire(event, subject, **kwargs)

    monkeypatch.setattr(security_events, "acquire", counting_acquire)
    security_events.reset_state()
    try:
        with security_events.suppression_disabled():
            yield _Events(handler.records, acquires)
    finally:
        logger.removeHandler(handler)
        logger.propagate = propagate
        logger.setLevel(level)
        security_events.reset_state()


def _request(path: str, method: str = "POST", session: dict | None = None) -> Request:
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": method,
            "scheme": "https",
            "path": path,
            "raw_path": path.encode(),
            "root_path": "",
            "query_string": b"",
            "headers": [(b"host", b"testserver")],
            "client": ("203.0.113.7", 54321),
            "server": ("testserver", 443),
            "session": {} if session is None else session,
            "state": {},
        }
    )


def body(response) -> dict:
    return json.loads(response.body)


# --- /token, authorization_code ------------------------------------------


def _code_row(**overrides):
    fields = {
        "code_hash": oauth._hash(CODE_SECRET),
        "client_id": "client123",
        "redirect_uri": REGISTERED_URI,
        "scope": "read",
        "code_challenge": oauth._base64url_sha256(VERIFIER),
        "code_challenge_method": "S256",
        "expires_at": datetime.now(timezone.utc) + timedelta(minutes=5),
        "used": False,
        "user_id": 5,
    }
    fields.update(overrides)
    return OAuthCode(**fields)


def _exchange(monkeypatch, *, rows, form=None, multi_user=True, commit_error=None):
    session = SeqSession(rows)
    if commit_error is not None:
        async def failing_commit():
            raise commit_error

        session.commit = failing_commit
    monkeypatch.setattr(oauth, "async_session", lambda: session)
    monkeypatch.setattr(oauth.settings, "multi_user_mode", multi_user, raising=False)
    payload = {
        "code": CODE_SECRET,
        "code_verifier": VERIFIER,
        "redirect_uri": REGISTERED_URI,
    }
    if form:
        payload.update(form)
    response = asyncio.run(
        oauth._handle_auth_code(payload, _request("/token"))
    )
    return response, session


def test_a_successful_exchange_is_recorded_after_its_commit(monkeypatch, events):
    code = _code_row()
    client = FakeClient(scope="read readwrite offline_access")
    response, session = _exchange(monkeypatch, rows=[code, client])

    assert response.status_code == 200
    assert session.committed is True
    assert events.attempted == ["oauth_token_issued"]
    record = events.one("oauth_token_issued")
    assert record.levelno == logging.INFO
    assert record.reason == "authorization_code"
    assert record.client_id == "client123"
    assert record.user_id == 5
    assert record.scope == "read"
    assert record.grant_id
    assert record.client_ip == "203.0.113.7"


def test_a_failed_mint_commit_leaves_no_issuance_record(monkeypatch, events):
    """D17: a commit that raises must not leave a line claiming a live token."""
    with pytest.raises(RuntimeError):
        _exchange(
            monkeypatch,
            rows=[_code_row(), FakeClient()],
            commit_error=RuntimeError("commit failed"),
        )

    assert events.named("oauth_token_issued") == []


def test_an_unknown_code_carries_only_the_submitted_client_id(monkeypatch, events):
    response, _ = _exchange(
        monkeypatch, rows=[None], form={"client_id": "whatever-they-typed"}
    )

    assert response.status_code == 400
    assert body(response)["error"] == "invalid_grant"
    assert events.attempted == ["oauth_token_refused"]
    record = events.one("oauth_token_refused")
    assert record.reason == "invalid_grant.unknown_code"
    # Nothing resolved, so nothing unsuffixed may appear (D15).
    assert record.client_id_submitted == "whatever-they-typed"
    assert not hasattr(record, "client_id")
    assert not hasattr(record, "user_id")
    assert not hasattr(record, "grant_id")


def test_a_pkce_failure_names_the_client_and_neither_secret(monkeypatch, events):
    code = _code_row(code_challenge=oauth._base64url_sha256("w" * 64))
    response, _ = _exchange(monkeypatch, rows=[code, FakeClient()])

    assert response.status_code == 400
    record = events.one("oauth_token_refused")
    assert record.reason == "invalid_grant.pkce_verification_failed"
    assert record.client_id == "client123"
    assert record.user_id == 5
    rendered = record.getMessage() + " ".join(
        str(value) for value in record.__dict__.values()
    )
    assert VERIFIER not in rendered
    assert CODE_SECRET not in rendered
    assert code.code_challenge not in rendered


def test_a_failed_client_authentication_never_records_the_secret(monkeypatch, events):
    client = FakeClient(
        token_endpoint_auth_method="client_secret_post",
        client_secret_hash=oauth._hash(CLIENT_SECRET),
    )
    response, _ = _exchange(
        monkeypatch,
        rows=[_code_row(), client],
        form={"client_secret": "the-wrong-secret"},
    )

    assert response.status_code == 401
    record = events.one("oauth_token_refused")
    assert record.reason == "invalid_client.authentication_failed"
    assert record.client_id == "client123"
    rendered = " ".join(str(value) for value in record.__dict__.values())
    assert "the-wrong-secret" not in rendered
    assert CLIENT_SECRET not in rendered


def test_an_unsupported_grant_type_is_refused_once(monkeypatch, events):
    request = _request("/token")
    # `Request.form()` returns its cached value, so seeding it drives the real
    # dispatch without a multipart body.
    request._form = {"grant_type": "password", "client_id": "c9"}
    response = asyncio.run(oauth.token_endpoint.__wrapped__(request))

    assert response.status_code == 400
    assert events.attempted == ["oauth_token_refused"]
    record = events.one("oauth_token_refused")
    assert record.reason == "unsupported_grant_type.grant_type_not_supported"
    assert record.client_id_submitted == "c9"


# --- /token, refresh_token ------------------------------------------------


def _live_family(user_id=5, revoked=False):
    return [
        FakeToken(
            grant_id="g1",
            token_type="access",
            scope="read",
            user_id=user_id,
            expires_at=in_hours(1),
        ),
        FakeToken(
            grant_id="g1",
            token_type="refresh",
            scope="read",
            user_id=user_id,
            revoked=revoked,
            expires_at=in_hours(720),
            token_hash=oauth._hash(REFRESH_SECRET),
        ),
    ]


def _refresh(monkeypatch, session, form=None):
    monkeypatch.setattr(oauth, "async_session", lambda: session)
    monkeypatch.setattr(oauth.settings, "multi_user_mode", True, raising=False)
    payload = {"refresh_token": REFRESH_SECRET}
    if form:
        payload.update(form)
    return asyncio.run(oauth._handle_refresh(payload, _request("/token")))


def test_a_rotation_is_recorded_after_its_commit(monkeypatch, events):
    session = FakeSession(clients=[FakeClient()], tokens=_live_family())
    response = _refresh(monkeypatch, session)

    assert response.status_code == 200
    assert events.attempted == ["oauth_token_refreshed"]
    record = events.one("oauth_token_refreshed")
    assert record.levelno == logging.INFO
    assert record.client_id == "client123"
    assert record.user_id == 5
    assert record.grant_id == "g1"
    assert record.scope == "read"
    assert record.client_ip == "203.0.113.7"

    # Neither the presented token nor the pair it rotated into may appear.
    minted = [obj for obj in session.added if isinstance(obj, OAuthToken)]
    rendered = " ".join(str(value) for value in record.__dict__.values())
    assert REFRESH_SECRET not in rendered
    for token in minted:
        assert token.token_hash not in rendered


def test_a_rotation_failure_is_recorded_with_its_traceback(monkeypatch, events):
    """The traceback used to be discarded behind the 500 and nothing replaced it."""
    session = FakeSession(clients=[FakeClient()], tokens=_live_family())

    async def failing_commit():
        raise RuntimeError("rotation exploded")

    session.commit = failing_commit
    response = _refresh(monkeypatch, session)

    assert response.status_code == 500
    assert events.attempted == ["oauth_token_rotation_failed"]
    record = events.one("oauth_token_rotation_failed")
    assert record.levelno == logging.ERROR
    assert record.exc_info is not None
    # Only the identifiers that came off rows.
    assert record.client_id == "client123"
    assert record.grant_id == "g1"
    assert record.client_ip == "203.0.113.7"
    assert events.named("oauth_token_refreshed") == []


def test_an_unknown_refresh_token_resolves_nothing_and_says_so(monkeypatch, events):
    session = FakeSession(clients=[FakeClient()], tokens=[])
    response = _refresh(monkeypatch, session, form={"client_id": "guessed"})

    assert response.status_code == 400
    record = events.one("oauth_token_refused")
    assert record.reason == "invalid_grant.unknown_token"
    assert record.client_id_submitted == "guessed"
    assert not hasattr(record, "client_id")
    assert not hasattr(record, "user_id")
    assert not hasattr(record, "grant_id")
    assert REFRESH_SECRET not in " ".join(
        str(value) for value in record.__dict__.values()
    )


def test_a_client_id_mismatch_reports_the_rows_identity_not_the_claim(
    monkeypatch, events
):
    session = FakeSession(clients=[FakeClient()], tokens=_live_family())
    response = _refresh(monkeypatch, session, form={"client_id": "not-my-client"})

    assert response.status_code == 400
    record = events.one("oauth_token_refused")
    assert record.reason == "invalid_grant.client_id_mismatch"
    assert record.client_id == "client123"
    assert not hasattr(record, "client_id_submitted")
    assert record.grant_id == "g1"


def test_an_expired_refresh_token_names_its_grant(monkeypatch, events):
    family = _live_family()
    family[1].expires_at = in_hours(-1)
    session = FakeSession(clients=[FakeClient()], tokens=family)
    response = _refresh(monkeypatch, session)

    assert response.status_code == 400
    record = events.one("oauth_token_refused")
    assert record.reason == "invalid_grant.refresh_token_expired"
    assert record.grant_id == "g1"
    assert record.user_id == 5


def test_a_replay_against_an_already_dead_family_still_records_one_outcome(
    monkeypatch, events
):
    """Not the reuse alarm — nothing live was killed — but not silence either."""
    session = FakeSession(clients=[FakeClient()], tokens=_live_family(revoked=True))
    # Revoke the access half too, so `revoke_grant_family` flips nothing.
    for token in session.tokens:
        token.revoked = True
    response = _refresh(monkeypatch, session)

    assert response.status_code == 400
    assert events.attempted == ["oauth_token_refused"]
    assert (
        events.one("oauth_token_refused").reason
        == "invalid_grant.refresh_token_revoked"
    )
    assert events.named("oauth_refresh_reuse_detected") == []


# --- /authorize -----------------------------------------------------------


class _AuthorizeSession:
    def __init__(self, client, commit_error=None):
        self._client = client
        self.added: list = []
        self.committed = False
        self._commit_error = commit_error

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def execute(self, stmt, *_a, **_kw):
        from sqlalchemy.sql.dml import Update

        from _oauth_grant_fakes import _Result

        if isinstance(stmt, Update):
            if self._client is not None and self._client.user_id is None:
                self._client.user_id = dict(stmt.compile().params).get("user_id")
                return _Result([self._client.client_id])
            return _Result([])
        return _Result([self._client] if self._client is not None else [])

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        if self._commit_error is not None:
            raise self._commit_error
        self.committed = True

    async def rollback(self):
        pass


def _authorize_post(monkeypatch, *, client, action="approve", session_user=None, **kw):
    session = _AuthorizeSession(client, commit_error=kw.pop("commit_error", None))
    monkeypatch.setattr(oauth, "async_session", lambda: session)
    monkeypatch.setattr(oauth.settings, "multi_user_mode", True, raising=False)
    monkeypatch.setattr(
        oauth, "get_active_session_user", _fake_session_user(session_user)
    )
    server_state = "csrfstatetoken1234567890"
    signed = oauth._state_serializer().dumps(server_state)
    request = _request("/authorize")
    request.scope["headers"] = [(b"cookie", f"oauth_state={signed}".encode())]
    params = {
        "action": action,
        "client_id": kw.pop("client_id", "client123"),
        "redirect_uri": kw.pop("redirect_uri", REGISTERED_URI),
        "code_challenge": oauth._base64url_sha256(VERIFIER),
        "code_challenge_method": "S256",
        "scope": kw.pop("scope", "read"),
        "state": kw.pop("state", server_state),
        "client_state": "",
    }
    params.update(kw)
    response = asyncio.run(oauth.authorize_post(request=request, **params))
    return response, session


def _fake_session_user(user):
    async def _get(_request, _session):
        return user

    return _get


class _User:
    def __init__(self, id):
        self.id = id


def test_consent_granted_is_recorded_after_the_code_row_commits(monkeypatch, events):
    client = FakeClient(redirect_uris=[REGISTERED_URI], user_id=5)
    response, session = _authorize_post(
        monkeypatch, client=client, session_user=_User(5)
    )

    assert response.status_code == 302
    assert session.committed is True
    assert events.attempted == ["oauth_consent_granted"]
    record = events.one("oauth_consent_granted")
    assert record.levelno == logging.INFO
    assert record.client_id == "client123"
    assert record.user_id == 5
    assert record.scope == "read"

    # The code this consent just minted rides in the `Location` header and in
    # no record.
    code = response.headers["location"].split("code=")[1].split("&")[0]
    rendered = " ".join(str(value) for value in record.__dict__.values())
    assert code not in rendered


def test_a_failed_consent_commit_leaves_no_granted_record(monkeypatch, events):
    client = FakeClient(redirect_uris=[REGISTERED_URI], user_id=5)
    with pytest.raises(RuntimeError):
        _authorize_post(
            monkeypatch,
            client=client,
            session_user=_User(5),
            commit_error=RuntimeError("commit failed"),
        )

    assert events.named("oauth_consent_granted") == []


def test_a_deny_now_carries_an_identity(monkeypatch, events):
    """A deny used to carry none at all: no user, no client, no record."""
    client = FakeClient(redirect_uris=[REGISTERED_URI], user_id=5)
    response, _ = _authorize_post(
        monkeypatch, client=client, action="deny", session_user=_User(5)
    )

    assert response.status_code == 302
    assert "error=access_denied" in response.headers["location"]
    record = events.one("oauth_consent_denied")
    assert record.levelno == logging.INFO
    assert record.client_id == "client123"
    assert record.user_id == 5


def test_a_deny_is_still_a_deny_when_the_identity_read_fails(monkeypatch, events):
    """The record may not turn a deny into a 500, so the read is guarded."""
    client = FakeClient(redirect_uris=[REGISTERED_URI], user_id=5)

    async def _explode(_request, _session):
        raise RuntimeError("session read failed")

    monkeypatch.setattr(oauth, "get_active_session_user", _explode)
    session = _AuthorizeSession(client)
    monkeypatch.setattr(oauth, "async_session", lambda: session)
    monkeypatch.setattr(oauth.settings, "multi_user_mode", True, raising=False)
    server_state = "csrfstatetoken1234567890"
    signed = oauth._state_serializer().dumps(server_state)
    request = _request("/authorize")
    request.scope["headers"] = [(b"cookie", f"oauth_state={signed}".encode())]

    response = asyncio.run(
        oauth.authorize_post(
            request=request,
            action="deny",
            client_id="client123",
            redirect_uri=REGISTERED_URI,
            code_challenge=oauth._base64url_sha256(VERIFIER),
            code_challenge_method="S256",
            scope="read",
            state=server_state,
            client_state="",
        )
    )

    assert response.status_code == 302
    record = events.one("oauth_consent_denied")
    assert not hasattr(record, "user_id")


def test_a_state_mismatch_is_refused_with_its_reason(monkeypatch, events):
    client = FakeClient(redirect_uris=[REGISTERED_URI])
    response, _ = _authorize_post(
        monkeypatch, client=client, session_user=_User(5), state="not-the-state"
    )

    assert response.status_code == 400
    assert body(response)["error"] == "invalid_state"
    record = events.one("oauth_authorize_refused")
    assert record.reason == "state_mismatch"
    assert record.client_id_submitted == "client123"
    assert not hasattr(record, "client_id")


def test_an_unregistered_redirect_uri_is_refused_with_the_rows_client_id(
    monkeypatch, events
):
    client = FakeClient(redirect_uris=[REGISTERED_URI], user_id=5)
    response, _ = _authorize_post(
        monkeypatch,
        client=client,
        session_user=_User(5),
        redirect_uri="https://evil.example.com/steal",
    )

    assert response.status_code == 400
    record = events.one("oauth_authorize_refused")
    assert record.reason == "invalid_redirect_uri"
    assert record.client_id == "client123"
    assert record.user_id == 5


def test_a_cross_user_client_names_both_the_actor_and_the_owner(monkeypatch, events):
    """D19: one `user_id` left an operator unable to tell which was which."""
    client = FakeClient(redirect_uris=[REGISTERED_URI], user_id=5)
    response, _ = _authorize_post(
        monkeypatch, client=client, session_user=_User(9)
    )

    assert response.status_code == 403
    record = events.one("oauth_cross_user_client_refused")
    assert record.levelno == logging.WARNING
    assert record.actor_user_id == 9
    assert record.user_id == 5
    assert record.client_id == "client123"
    assert record.route == "/authorize"


# --- /register (DCR) ------------------------------------------------------


class _RegisterRequest:
    """`register_client` reads only `.json()`, plus the record's context."""

    def __init__(self, payload):
        self._payload = payload
        self.client = type("_C", (), {"host": "198.51.100.4"})()
        self.url = type("_U", (), {"path": "/register"})()

    async def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


def _register(monkeypatch, payload, commit_error=None):
    class _Session:
        def __init__(self):
            self.added = []
            self.committed = False

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

        def add(self, obj):
            self.added.append(obj)

        async def commit(self):
            if commit_error is not None:
                raise commit_error
            self.committed = True

    session = _Session()
    monkeypatch.setattr(oauth, "async_session", lambda: session)
    response = asyncio.run(oauth.register_client.__wrapped__(_RegisterRequest(payload)))
    return response, session


def test_a_registration_records_the_public_facts_and_not_the_secret(
    monkeypatch, events
):
    response, session = _register(
        monkeypatch,
        {
            "client_name": "Some Connector",
            "redirect_uris": [REGISTERED_URI, "https://other.example/cb"],
            "token_endpoint_auth_method": "client_secret_post",
        },
    )

    assert response.status_code == 201
    assert session.committed is True
    registration = json.loads(response.body)
    secret = registration["client_secret"]

    record = events.one("oauth_client_registered")
    assert record.levelno == logging.INFO
    assert record.client_id == registration["client_id"]
    # The name is the client's own text, and is marked as such.
    assert record.client_name_submitted == "Some Connector"
    assert record.count == 2
    rendered = " ".join(str(value) for value in record.__dict__.values())
    assert secret not in rendered


@pytest.mark.parametrize(
    "payload, reason",
    [
        ({"client_name": "", "redirect_uris": [REGISTERED_URI]}, "invalid_client_metadata"),
        ({"client_name": "x", "redirect_uris": []}, "invalid_redirect_uri"),
        (
            {"client_name": "x", "redirect_uris": ["http://insecure.example/cb"]},
            "invalid_redirect_uri",
        ),
        (
            {"client_name": "x", "redirect_uris": [REGISTERED_URI], "scope": "bogus"},
            "invalid_scope",
        ),
        (
            {
                "client_name": "x",
                "redirect_uris": [REGISTERED_URI],
                "scope": "offline_access",
            },
            "invalid_scope",
        ),
        (
            {
                "client_name": "x",
                "redirect_uris": [REGISTERED_URI],
                "token_endpoint_auth_method": "private_key_jwt",
            },
            "unsupported_auth_method",
        ),
    ],
)
def test_each_registration_refusal_has_its_own_reason(
    monkeypatch, events, payload, reason
):
    response, _ = _register(monkeypatch, payload)

    assert response.status_code == 400
    assert events.attempted == ["oauth_client_registration_refused"]
    record = events.one("oauth_client_registration_refused")
    assert record.reason == reason
    assert record.client_ip == "198.51.100.4"


def test_an_unparseable_registration_body_is_recorded(monkeypatch, events):
    response, _ = _register(monkeypatch, ValueError("not json"))

    assert response.status_code == 400
    assert events.one("oauth_client_registration_refused").reason == (
        "invalid_client_metadata"
    )


# --- /revoke --------------------------------------------------------------


def _revoke(monkeypatch, session, form):
    monkeypatch.setattr(oauth, "async_session", lambda: session)
    request = _request("/revoke")
    request._form = form
    return asyncio.run(oauth.revoke_token.__wrapped__(request))


def test_a_revocation_is_recorded_after_its_commit_with_no_actor(monkeypatch, events):
    """`/revoke` authenticates as the *client*, so there is no acting human."""
    tokens = _live_family()
    tokens[1].token_hash = oauth._hash(REFRESH_SECRET)
    session = FakeSession(clients=[FakeClient()], tokens=tokens)
    response = _revoke(
        monkeypatch,
        session,
        {"token": REFRESH_SECRET, "client_id": "client123"},
    )

    assert response.status_code == 200
    assert body(response) == {}
    assert events.attempted == ["oauth_grant_revoked"]
    record = events.one("oauth_grant_revoked")
    assert record.levelno == logging.INFO
    assert record.client_id == "client123"
    assert record.user_id == 5
    assert record.grant_id == "g1"
    assert record.count >= 1
    assert record.route == "/revoke"
    assert not hasattr(record, "actor_user_id")
    assert REFRESH_SECRET not in " ".join(
        str(value) for value in record.__dict__.values()
    )


def test_a_failed_revocation_commit_leaves_no_record(monkeypatch, events):
    tokens = _live_family()
    tokens[1].token_hash = oauth._hash(REFRESH_SECRET)
    session = FakeSession(clients=[FakeClient()], tokens=tokens)

    async def failing_commit():
        raise RuntimeError("commit failed")

    session.commit = failing_commit

    with pytest.raises(RuntimeError):
        _revoke(
            monkeypatch,
            session,
            {"token": REFRESH_SECRET, "client_id": "client123"},
        )

    assert events.named("oauth_grant_revoked") == []


@pytest.mark.parametrize(
    "form, reason",
    [
        ({}, "missing_token"),
        ({"token": "z" * 64, "client_id": "c1"}, "unknown_token"),
        ({"token": REFRESH_SECRET, "client_id": "somebody-else"}, "client_mismatch"),
    ],
)
def test_each_revocation_no_op_keeps_its_empty_200_and_records_why(
    monkeypatch, events, form, reason
):
    tokens = _live_family()
    tokens[1].token_hash = oauth._hash(REFRESH_SECRET)
    session = FakeSession(clients=[FakeClient()], tokens=tokens)

    response = _revoke(monkeypatch, session, form)

    assert response.status_code == 200
    assert body(response) == {}
    assert events.attempted == ["oauth_revoke_noop"]
    record = events.one("oauth_revoke_noop")
    assert record.levelno == logging.INFO
    assert record.reason == reason
    # Only the submitted client id, never the token's real owner: §2.2 hides
    # that from the response and the log is not a way to hand it back.
    assert not hasattr(record, "client_id")
    assert not hasattr(record, "user_id")


def test_a_client_that_fails_to_authenticate_is_a_refusal_not_a_no_op(
    monkeypatch, events
):
    tokens = _live_family()
    tokens[1].token_hash = oauth._hash(REFRESH_SECRET)
    client = FakeClient(
        token_endpoint_auth_method="client_secret_post",
        client_secret_hash=oauth._hash(CLIENT_SECRET),
    )
    session = FakeSession(clients=[client], tokens=tokens)

    response = _revoke(
        monkeypatch,
        session,
        {
            "token": REFRESH_SECRET,
            "client_id": "client123",
            "client_secret": "wrong",
        },
    )

    assert response.status_code == 401
    record = events.one("oauth_revoke_refused")
    assert record.levelno == logging.WARNING
    assert record.reason == "client_auth_failed"
    assert record.client_id == "client123"
    rendered = " ".join(str(value) for value in record.__dict__.values())
    assert CLIENT_SECRET not in rendered
    assert "wrong" not in rendered.split()
