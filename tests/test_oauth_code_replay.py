"""Authorization-code replay (#325) — branch logic and response constancy.

The properties that *are* the database (the lineage write, the family
revocation under the grant lock, the concurrent double exchange, retention)
run on real Postgres in `tests/integration/test_oauth_grant_lifetime_pg.py`.
What is here needs a database that can be told to fail, or needs to observe
that a statement was *not* issued: every failure on the replay branch still
answers the constant 400, the record carries the class name only, a refused
replay never reaches the revoking UPDATE, and the reordered checks.
"""
import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.sql.dml import Update

from src.models.db import OAuthClient, OAuthCode
from src.oauth import routes as oauth
from src.services import security_events

REDIRECT_URI = "https://example.test/cb"
VERIFIER = "v" * 64
CODE = "the-code"
UNKNOWN_BODY = {"error": "invalid_grant"}


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.records = []

    def emit(self, record):
        self.records.append(record)


@pytest.fixture
def events():
    handler = _Capture()
    logger = security_events.logger
    logger.addHandler(handler)
    propagate, level = logger.propagate, logger.level
    logger.propagate = False
    logger.setLevel(logging.DEBUG)
    security_events.reset_state()
    try:
        with security_events.suppression_disabled():
            yield handler.records
    finally:
        logger.removeHandler(handler)
        logger.propagate = propagate
        logger.setLevel(level)
        security_events.reset_state()


def named(records, event):
    return [r for r in records if r.getMessage() == event]


class _Code:
    def __init__(self, *, used=False, grant_id=None, expires_at=None):
        self.code_hash = oauth._hash(CODE)
        self.client_id = "client123"
        self.redirect_uri = REDIRECT_URI
        self.code_challenge = oauth._base64url_sha256(VERIFIER)
        self.code_challenge_method = "S256"
        self.scope = "readwrite"
        self.user_id = 7
        self.used = used
        self.grant_id = grant_id
        self.expires_at = expires_at or datetime.now(timezone.utc) + timedelta(minutes=10)


class _Client:
    client_id = "client123"
    client_name = "Claude"
    scope = "read readwrite offline_access"
    user_id = 7
    token_endpoint_auth_method = "none"
    client_secret_hash = None


class _ConfidentialClient(_Client):
    token_endpoint_auth_method = "client_secret_post"
    client_secret_hash = oauth._hash("s" * 64)


class _Result:
    def __init__(self, obj=None, rowcount=0):
        self._obj = obj
        self.rowcount = rowcount

    def scalar_one_or_none(self):
        return self._obj


class _Session:
    """Answers the code and client lookups, counts the revoking UPDATE, and
    can be told to fail at the UPDATE, the commit or the rollback."""

    def __init__(self, code, *, rowcount=2, fail_update=None, fail_commit=None,
                 fail_rollback=None, client=_Client):
        self.code = code
        self.client = client
        self.rowcount = rowcount
        self.fail_update = fail_update
        self.fail_commit = fail_commit
        self.fail_rollback = fail_rollback
        self.updates = 0
        self.commits = 0
        self.rollbacks = 0
        self.added = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, stmt, params=None):
        if isinstance(stmt, Update):
            self.updates += 1
            if self.fail_update is not None:
                raise self.fail_update
            return _Result(rowcount=self.rowcount)
        descs = getattr(stmt, "column_descriptions", None) or []
        entity = descs[0].get("entity") if descs else None
        if entity is OAuthCode:
            return _Result(self.code)
        if entity is OAuthClient:
            return _Result(self.client() if self.client else None)
        return _Result()  # advisory locks

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.commits += 1
        if self.fail_commit is not None:
            raise self.fail_commit

    async def rollback(self):
        self.rollbacks += 1
        if self.fail_rollback is not None:
            raise self.fail_rollback


def exchange(session, monkeypatch, *, verifier=VERIFIER, client_id=None,
             redirect_uri=REDIRECT_URI, client_secret=None):
    monkeypatch.setattr(oauth, "async_session", lambda: session)
    monkeypatch.setattr(oauth.settings, "multi_user_mode", False, raising=False)
    form = {"code": CODE, "code_verifier": verifier}
    if redirect_uri is not None:
        form["redirect_uri"] = redirect_uri
    if client_id:
        form["client_id"] = client_id
    if client_secret:
        form["client_secret"] = client_secret
    return asyncio.run(oauth._handle_auth_code(form))


def assert_constant(response):
    assert response.status_code == 400
    assert json.loads(response.body) == UNKNOWN_BODY
    unknown = oauth.JSONResponse({"error": "invalid_grant"}, status_code=400)
    assert response.body == unknown.body
    assert dict(response.headers) == dict(unknown.headers)


def test_a_valid_replay_revokes_and_records_one_warning(monkeypatch, events):
    session = _Session(_Code(used=True, grant_id="g1"))
    response = exchange(session, monkeypatch)
    assert_constant(response)
    assert session.updates == 1 and session.commits == 1
    (record,) = named(events, "oauth_code_replay_detected")
    assert record.levelno == logging.WARNING
    assert record.grant_id == "g1" and record.revoked_tokens == 2


@pytest.mark.parametrize(
    "fail,expected",
    [
        (dict(fail_update=RuntimeError("UPDATE … code_hash=abc")), "RuntimeError"),
        (dict(fail_commit=ValueError("boom")), "ValueError"),
        (
            dict(fail_commit=ValueError("boom"), fail_rollback=OSError("gone")),
            "ValueError+OSError",
        ),
    ],
)
def test_every_failure_on_the_replay_path_still_answers_the_constant_400(
    monkeypatch, events, fail, expected
):
    session = _Session(_Code(used=True, grant_id="g1"), **fail)
    response = exchange(session, monkeypatch)
    assert_constant(response)
    (record,) = named(events, "oauth_code_replay_revocation_failed")
    assert record.levelno == logging.ERROR
    assert record.error_type == expected
    assert record.exc_info is None
    assert "code_hash" not in record.getMessage()
    assert named(events, "oauth_code_replay_detected") == []


def test_a_replay_against_a_dead_family_records_no_alarm(monkeypatch, events):
    session = _Session(_Code(used=True, grant_id="g1"), rowcount=0)
    response = exchange(session, monkeypatch)
    assert_constant(response)
    assert session.commits == 0 and session.rollbacks == 1
    assert named(events, "oauth_code_replay_detected") == []
    (refused,) = named(events, "oauth_token_refused")
    assert refused.reason == "invalid_grant.code_reused"


def test_a_spent_code_without_lineage_revokes_and_commits_nothing(monkeypatch, events):
    session = _Session(_Code(used=True, grant_id=None))
    response = exchange(session, monkeypatch)
    assert_constant(response)
    assert session.updates == 0 and session.commits == 0
    (refused,) = named(events, "oauth_token_refused")
    assert refused.reason == "invalid_grant.code_reused"


SPENT_FAILURES = [
    (dict(verifier="w" * 64), _Client, "pkce_verification_failed"),
    (dict(verifier="short"), _Client, "pkce_verifier_invalid"),
    (dict(redirect_uri="https://elsewhere.test/cb"), _Client, "redirect_uri_mismatch"),
    (dict(redirect_uri=None), _Client, "redirect_uri_mismatch"),
    (dict(client_id="someone-else"), _Client, "client_id_mismatch"),
    (dict(client_secret="wrong"), _ConfidentialClient, "authentication_failed"),
    (dict(), _ConfidentialClient, "authentication_failed"),
    (dict(), None, "unknown_client"),
]


@pytest.mark.parametrize("kwargs,client,check", SPENT_FAILURES)
def test_a_replay_failing_revalidation_never_reaches_the_update(
    monkeypatch, events, kwargs, client, check
):
    """And answers exactly as an unknown code does (Codex review of #325): a
    spent code is retained and found by hash alone, so a specific refusal
    would confirm to a holder of the bare code that it exists and was spent.
    The specific check survives only in the bounded record."""
    session = _Session(_Code(used=True, grant_id="g1"), client=client)
    response = exchange(session, monkeypatch, **kwargs)
    assert_constant(response)
    assert session.updates == 0 and session.commits == 0
    assert named(events, "oauth_code_replay_detected") == []
    (refused,) = named(events, "oauth_token_refused")
    assert refused.reason == f"invalid_grant.spent_code_{check}"
    assert refused.client_id == "client123"
    assert getattr(refused, "client_id_submitted", None) is None


@pytest.mark.parametrize(
    "kwargs,client,status,body,reason",
    [
        (dict(verifier="w" * 64), _Client, 400,
         {"error": "invalid_grant", "error_description": "PKCE verification failed"},
         "invalid_grant.pkce_verification_failed"),
        (dict(verifier="short"), _Client, 400,
         {"error": "invalid_grant", "error_description": "Invalid PKCE verifier"},
         "invalid_grant.pkce_verifier_invalid"),
        (dict(redirect_uri=None), _Client, 400,
         {"error": "invalid_grant", "error_description": "redirect_uri mismatch"},
         "invalid_grant.redirect_uri_mismatch"),
        (dict(client_secret="wrong"), _ConfidentialClient, 401,
         {"error": "invalid_client"}, "invalid_client.authentication_failed"),
        (dict(), None, 401, {"error": "invalid_client"}, "invalid_client.unknown_client"),
        (dict(client_id="someone-else"), _Client, 400, UNKNOWN_BODY,
         "invalid_grant.client_id_mismatch"),
    ],
)
def test_a_live_code_keeps_its_specific_refusals(
    monkeypatch, events, kwargs, client, status, body, reason
):
    session = _Session(_Code(), client=client)
    response = exchange(session, monkeypatch, **kwargs)
    assert response.status_code == status
    assert json.loads(response.body) == body
    (refused,) = named(events, "oauth_token_refused")
    assert refused.reason == reason


def test_a_client_id_mismatch_answers_the_unknown_code_body(monkeypatch, events):
    """It used to *be* the unknown-code refusal (the lookup filtered on the
    caller's `client_id`); the body stays the same now that it is checked
    against the row."""
    session = _Session(_Code())
    assert_constant(exchange(session, monkeypatch, client_id="someone-else"))


def test_a_replay_after_the_code_expired_still_revokes(monkeypatch, events):
    expired = datetime.now(timezone.utc) - timedelta(days=2)
    session = _Session(_Code(used=True, grant_id="g1", expires_at=expired))
    assert_constant(exchange(session, monkeypatch))
    assert session.updates == 1
    assert len(named(events, "oauth_code_replay_detected")) == 1


@pytest.mark.parametrize(
    "kwargs",
    [dict(verifier="w" * 64), dict(verifier="short"), dict(redirect_uri=None)],
)
def test_an_expired_unspent_code_reports_expiry_first_as_before(
    monkeypatch, events, kwargs
):
    """A live code keeps the pre-#325 order (D7): expiry before the redirect
    URI and PKCE, so its answer is what it always was."""
    expired = datetime.now(timezone.utc) - timedelta(minutes=1)
    session = _Session(_Code(expires_at=expired))
    response = exchange(session, monkeypatch, **kwargs)
    assert response.status_code == 400
    assert json.loads(response.body)["error_description"] == "code expired"


def test_an_expired_unspent_code_is_still_refused_as_expired(monkeypatch, events):
    expired = datetime.now(timezone.utc) - timedelta(minutes=1)
    session = _Session(_Code(expires_at=expired))
    response = exchange(session, monkeypatch)
    assert json.loads(response.body)["error_description"] == "code expired"
    assert session.added == []


def test_the_first_exchange_writes_lineage_and_issuance(monkeypatch, events):
    code = _Code()
    session = _Session(code)
    monkeypatch.setattr(oauth, "_stamp_client_use", _async_true)
    response = exchange(session, monkeypatch)
    assert response.status_code == 200, response.body
    payload = json.loads(response.body)
    assert payload["expires_in"] == 3600
    access, refresh = session.added
    assert code.used is True
    assert code.grant_id == access.grant_id == refresh.grant_id
    assert access.grant_issued_at == refresh.grant_issued_at
    assert refresh.expires_at - refresh.grant_issued_at == timedelta(days=30)
    assert access.expires_at - access.grant_issued_at == timedelta(hours=1)


async def _async_true(*_args, **_kwargs):
    return True
