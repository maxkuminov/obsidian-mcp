"""The quota gate's shape, ordering, and cost (#162). Hermetic.

What is pinned here is everything about the gate that does *not* need a real
database: where it sits relative to the other two pre-body gates, what it
writes into `usage_logs.params`, that its marker is the reader's own constant
rather than a second copy, and — the one an operator would never notice going
wrong — that a key with no limit issues **no statement at all**.

That the counter actually admits exactly `limit` calls under concurrency is a
fact about PostgreSQL and is proved in
`tests/integration/test_issue_162_quotas_pg.py`. A hermetic test cannot see it
and must not pretend to.

The preamble matches `tests/test_issue_66_vault_unassignment_revokes_tools.py`:
`src.mcp_server.tools` imports `src.config`, whose module-level `Settings()`
reads `./.env`, so the environment is set and the cwd moved before the import.
"""
import asyncio
import os
import tempfile

os.environ.setdefault("SECRET_KEY", "test")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("VAULT_PATH", "/tmp/test-vault")
os.chdir(tempfile.gettempdir())

import pytest  # noqa: E402

import src.mcp_server.auth as mcp_auth  # noqa: E402
import src.mcp_server.tools as tools  # noqa: E402
import src.services.quotas as quotas  # noqa: E402
from src.models.db import APIKey  # noqa: E402
from src.services import usage_stats  # noqa: E402


# --------------------------------------------------------------------------
# plumbing
# --------------------------------------------------------------------------


class _AdmissionResult:
    def __init__(self, value):
        self._value = value

    def scalar(self):
        return self._value

    def fetchall(self):
        return []


class _QuotaSpySession:
    """Stands in for the admission's own `async_session()`.

    Counts statements, because "a key with no limit issues zero quota SQL" is
    the property that makes this feature free for everybody who has not turned
    it on, and it is invisible from any surface an operator can see.
    """

    def __init__(self, admitted=1):
        self.statements = []
        self.commits = 0
        self.admitted = admitted

    def __call__(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def execute(self, stmt, params=None):
        self.statements.append((str(stmt), params))
        if "INSERT INTO quota_counters" in str(stmt):
            return _AdmissionResult(self.admitted)
        return _AdmissionResult(None)

    async def commit(self):
        self.commits += 1

    async def rollback(self):  # pragma: no cover - the prune's failure path
        pass


def _run_tracked(fn, *args, limit=None, key_id=7, spy=None, **kwargs):
    """Call a `_tracked` function with a bound quota context.

    Returns `(result, logged_params, spy)`. `_log_usage` is stubbed so nothing
    touches a database by *logging*; if a gate regressed, the admission itself
    would try to connect and the test would fail loudly rather than pass
    quietly.
    """
    spy = spy or _QuotaSpySession()
    captured = {}

    async def fake_log_usage(tool, params, duration_ms, response_size):
        captured["tool"] = tool
        captured["params"] = params

    mp = pytest.MonkeyPatch()
    mp.setattr(tools, "_log_usage", fake_log_usage)
    mp.setattr(quotas, "async_session", spy)

    async def run():
        limit_token = mcp_auth.current_daily_request_limit.set(limit)
        key_token = mcp_auth.current_api_key_id.set(key_id)
        try:
            return await fn(*args, **kwargs)
        finally:
            mcp_auth.current_api_key_id.reset(key_token)
            mcp_auth.current_daily_request_limit.reset(limit_token)

    try:
        result = asyncio.run(run())
    finally:
        mp.undo()
    return result, captured.get("params"), spy


@tools._tracked("quota_probe", ["value"])
async def _probe(value: str = "x") -> str:
    """A minimal tracked tool. The decorator is what is under test, so a real
    tool would only add a vault and a database to the surface."""
    return f"ran:{value}"


@tools._tracked("quota_probe_raises", [])
async def _probe_raises() -> str:
    raise RuntimeError("the body failed after being admitted")


def _admissions(spy):
    return [s for s, _ in spy.statements if "INSERT INTO quota_counters" in s]


# --------------------------------------------------------------------------
# 1. one marker, imported and not mirrored
# --------------------------------------------------------------------------


def test_the_gate_uses_the_readers_own_constant():
    """`usage_stats` enumerated `over_quota` ahead of this gate shipping so the
    two would land as one contract. The import runs writer → reader, which is
    the direction that does not close a cycle: `usage_stats` mirrors *its* two
    string markers from `tools.py` for exactly the same reason."""
    assert tools._OVER_QUOTA_MARKER is usage_stats.OVER_QUOTA_PARAM
    assert tools._OVER_QUOTA_MARKER == "over_quota"
    assert quotas.OVER_QUOTA_PARAM is usage_stats.OVER_QUOTA_PARAM


def test_the_marker_is_read_by_the_pre_body_refusal_predicate():
    fragment = usage_stats.pre_body_refusal_sql()
    assert "over_quota" in fragment
    # NULL-safely, because the overwhelming majority of rows carry no such key
    # and a NULL there would make the executed filter drop every ordinary row.
    assert "COALESCE((ul.params->>'over_quota')::boolean, false)" in fragment


# --------------------------------------------------------------------------
# 2. the refusal's shape
# --------------------------------------------------------------------------


def test_an_over_quota_call_is_refused_before_the_body():
    result, params, spy = _run_tracked(
        _probe, limit=5, spy=_QuotaSpySession(admitted=None)
    )

    assert result == quotas.quota_refusal_message(5)
    assert "ran:" not in result, "the tool body ran anyway"
    assert len(_admissions(spy)) == 1


def test_the_refusal_message_names_the_limit_and_the_utc_reset():
    """The reader is an agent. "Quota exceeded" gives it nothing to act on; a
    number and a timestamp let it wait or tell its operator what to raise."""
    message = quotas.quota_refusal_message(250)
    assert "250" in message
    assert "UTC" in message
    reset = quotas.reset_instant()
    assert reset.strftime("%Y-%m-%dT%H:%M:%SZ") in message
    assert reset.hour == 0 and reset.minute == 0 and reset.second == 0


def test_the_logged_marker_is_a_json_boolean_and_nothing_else():
    """`/admin/performance` evaluates `(params->>'over_quota')::boolean` with no
    guard, so a single row carrying the *string* `"true"` takes the page down
    with a 500 for every user until it ages out."""
    _, params, _ = _run_tracked(_probe, limit=5, spy=_QuotaSpySession(admitted=None))

    assert params["over_quota"] is True
    assert not isinstance(params["over_quota"], str)
    # And it does not also claim to be one of the `error` markers: the two
    # halves of the predicate are separate, and a refusal that set both would
    # be counted once but read as two different facts.
    assert "error" not in params


def test_an_admitted_call_logs_no_quota_marker():
    result, params, spy = _run_tracked(_probe, "hello", limit=5)

    assert result == "ran:hello"
    assert "over_quota" not in params
    assert params["value"] == "hello"
    assert len(_admissions(spy)) == 1


# --------------------------------------------------------------------------
# 3. the null-limit path costs nothing
# --------------------------------------------------------------------------


def test_a_key_with_no_limit_issues_no_quota_statement():
    """The property that makes this feature free for every key that has not
    opted in — and the one nothing on any page would reveal if it regressed."""
    result, params, spy = _run_tracked(_probe, limit=None)

    assert result == "ran:x"
    assert spy.statements == []
    assert spy.commits == 0
    assert "over_quota" not in params


def test_a_limit_with_no_api_key_is_exempt_rather_than_a_crash():
    """Not a state the middleware produces — it binds both together — but the
    one a direct in-process caller or a half-set fixture reaches. Counting
    against a key id of None would violate the counter's own NOT NULL."""
    result, _, spy = _run_tracked(_probe, limit=5, key_id=None)

    assert result == "ran:x"
    assert spy.statements == []


# --------------------------------------------------------------------------
# 4. ordering: the quota gate is last, so earlier refusals consume nothing
# --------------------------------------------------------------------------


def test_a_no_vault_refusal_consumes_no_quota(monkeypatch):
    monkeypatch.setattr(
        tools, "_vault_admission_error", lambda: tools._NO_VAULT_MESSAGE
    )
    result, params, spy = _run_tracked(_probe, limit=5)

    assert result == tools._NO_VAULT_MESSAGE
    assert params["error"] == tools._NO_VAULT_MARKER
    assert spy.statements == [], "a call that was never going to run took a slot"


def test_an_unencodable_argument_refusal_consumes_no_quota():
    result, params, spy = _run_tracked(_probe, "\ud800", limit=5)

    assert params["error"] == tools._UNENCODABLE_ARG_MARKER
    assert "not valid UTF-8" in result
    assert spy.statements == []


def test_an_admitted_body_that_raises_has_already_consumed():
    """The increment commits before the body starts, so nothing gives the slot
    back. The other direction — increment on completion — would admit
    unboundedly many concurrent calls, and refunding a failure makes a tool
    that always fails free."""
    spy = _QuotaSpySession()
    with pytest.raises(RuntimeError):
        _run_tracked(_probe_raises, limit=5, spy=spy)

    assert len(_admissions(spy)) == 1
    assert spy.commits >= 1


# --------------------------------------------------------------------------
# 5. the admission statement itself
# --------------------------------------------------------------------------


def test_the_admission_is_one_guarded_upsert_returning_the_count():
    sql = str(quotas.ADMISSION_SQL)
    assert "INSERT INTO quota_counters" in sql
    assert "ON CONFLICT (key_id, day)" in sql
    assert "DO UPDATE SET count = quota_counters.count + 1" in sql
    # The guard is the whole design: without it the UPDATE always fires and the
    # limit is decoration.
    assert "WHERE quota_counters.count < :limit" in sql
    assert "RETURNING count" in sql
    # Every value is a bind parameter; nothing is interpolated.
    assert ":key_id" in sql and ":day" in sql and ":limit" in sql


def test_the_day_is_utc_and_bound_not_derived_by_the_server():
    """`now()::date` is the server's timezone, which is not a property anyone
    administering a limit can see."""
    assert "now()" not in str(quotas.ADMISSION_SQL)
    assert "CURRENT_DATE" not in str(quotas.ADMISSION_SQL)

    import datetime as dt

    # 23:30 in a +02:00 zone is already the next UTC day.
    moment = dt.datetime(
        2026, 8, 29, 23, 30, tzinfo=dt.timezone(dt.timedelta(hours=2))
    )
    assert quotas.utc_day(moment) == dt.date(2026, 8, 29)
    assert quotas.utc_day(
        dt.datetime(2026, 8, 30, 1, 30, tzinfo=dt.timezone(dt.timedelta(hours=2)))
    ) == dt.date(2026, 8, 29)


def test_the_prune_fires_on_a_fresh_row_and_not_on_a_subsequent_one():
    """A `RETURNING count` of 1 is the INSERT branch: the `DO UPDATE` adds to a
    count that is already at least 1. So the prune happens once per key per
    day, never on the contended path."""
    mp = pytest.MonkeyPatch()

    fresh = _QuotaSpySession(admitted=1)
    mp.setattr(quotas, "async_session", fresh)
    try:
        assert asyncio.run(quotas.admit(7, 100)) == 1
    finally:
        mp.undo()
    assert [s for s, _ in fresh.statements if "DELETE FROM quota_counters" in s]

    later = _QuotaSpySession(admitted=42)
    mp = pytest.MonkeyPatch()
    mp.setattr(quotas, "async_session", later)
    try:
        assert asyncio.run(quotas.admit(7, 100)) == 42
    finally:
        mp.undo()
    assert not [s for s, _ in later.statements if "DELETE FROM quota_counters" in s]


def test_a_refusal_issues_no_second_statement():
    refused = _QuotaSpySession(admitted=None)
    mp = pytest.MonkeyPatch()
    mp.setattr(quotas, "async_session", refused)
    try:
        assert asyncio.run(quotas.admit(7, 100)) is None
    finally:
        mp.undo()
    assert len(refused.statements) == 1


# --------------------------------------------------------------------------
# 6. the limit domain, server side
# --------------------------------------------------------------------------


@pytest.mark.parametrize("value", [0, -1, -5, 1000001, 99999999])
def test_out_of_domain_limits_are_rejected(value):
    assert quotas.limit_value_error(value) is not None
    assert quotas.parse_limit_form_value(str(value))[0] is None
    assert quotas.parse_limit_form_value(str(value))[1] is not None


@pytest.mark.parametrize("value", [1, 2, 100, 1000000])
def test_in_domain_limits_are_accepted(value):
    assert quotas.limit_value_error(value) is None
    assert quotas.parse_limit_form_value(str(value)) == (value, None)


@pytest.mark.parametrize("raw", ["", "   ", None])
def test_an_empty_field_means_unlimited_not_zero(raw):
    assert quotas.parse_limit_form_value(raw) == (None, None)


def test_a_non_numeric_limit_is_an_error_not_a_silent_clear():
    """Silently discarding "1oo" would look exactly like success, and the key
    would go on being unlimited while the operator believed otherwise."""
    value, error = quotas.parse_limit_form_value("1oo")
    assert value is None
    assert error is not None and "whole number" in error


def test_the_zero_message_points_at_revoke():
    """Disabling a key is what revoke is for; a key silently refusing every
    call reads to its operator as an outage."""
    assert "revoke" in quotas.limit_value_error(0).lower()


# --------------------------------------------------------------------------
# 7. OAuth is exempt by construction, not by a list
# --------------------------------------------------------------------------


class _EmptyResult:
    def fetchall(self):
        return []

    def scalar_one_or_none(self):
        return None

    def first(self):
        return None


class _RowsResult:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows

    def first(self):
        return self._rows[0] if self._rows else None

    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else None


class _MiddlewareSession:
    """Answers the statements `APIKeyMiddleware` issues, on either branch."""

    def __init__(self, *, api_key=None, oauth_token=None, client_row=None):
        self.api_key = api_key
        self.oauth_token = oauth_token
        self.client_row = client_row

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def commit(self):
        pass

    async def execute(self, stmt, *_a, **_kw):
        sql = str(stmt)
        if sql.startswith("UPDATE"):
            return _EmptyResult()
        if "FROM api_keys" in sql:
            return _RowsResult([self.api_key] if self.api_key else [])
        if "FROM oauth_tokens" in sql:
            if self.oauth_token is None:
                return _RowsResult([])
            owner, name = self.client_row if self.client_row else (None, None)
            return _RowsResult([(self.oauth_token, owner, name)])
        if "vault_path" in sql:
            return _RowsResult([])
        return _EmptyResult()


def _drive(bearer: str, session: _MiddlewareSession) -> dict:
    """Run the real middleware once; report the limit it bound."""
    captured = {}

    async def downstream(scope, receive, send):
        captured["limit"] = mcp_auth.current_daily_request_limit.get()
        captured["key_id"] = mcp_auth.current_api_key_id.get()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def receive():  # pragma: no cover - never awaited
        return {"type": "http.request", "body": b"", "more_body": False}

    sent = []

    async def send(message):
        sent.append(message)

    async def run():
        mp = pytest.MonkeyPatch()
        try:
            mp.setattr(mcp_auth, "async_session", lambda: session)
            app = mcp_auth.APIKeyMiddleware(downstream)
            await app(
                {
                    "type": "http",
                    "method": "POST",
                    "path": "/mcp/",
                    "headers": [(b"authorization", f"Bearer {bearer}".encode())],
                },
                receive,
                send,
            )
            captured["after"] = mcp_auth.current_daily_request_limit.get()
        finally:
            mp.undo()

    asyncio.run(run())
    captured["status"] = sent[0]["status"] if sent else None
    return captured


def test_the_middleware_binds_an_api_keys_limit_from_the_row_it_already_loaded():
    api_key = APIKey(
        id=7,
        name="nightly sync",
        key_hash="x",
        key_prefix="omcp_a1b2c3",
        permission="read",
        user_id=None,
        expires_at=None,
        is_active=True,
        daily_request_limit=500,
    )
    captured = _drive("omcp_testkey", _MiddlewareSession(api_key=api_key))

    assert captured["status"] == 200
    assert captured["limit"] == 500
    assert captured["key_id"] == 7
    # And it does not outlive the request: a limit that leaked would apply to
    # whatever ran next in the same context.
    assert captured["after"] is None


def test_a_null_limit_key_binds_none():
    api_key = APIKey(
        id=8,
        name="unlimited",
        key_hash="x",
        key_prefix="omcp_b2c3d4",
        permission="read",
        user_id=None,
        expires_at=None,
        is_active=True,
        daily_request_limit=None,
    )
    captured = _drive("omcp_testkey", _MiddlewareSession(api_key=api_key))

    assert captured["status"] == 200
    assert captured["limit"] is None


def test_oauth_traffic_is_exempt_because_no_branch_binds_a_limit_for_it():
    """v1 exempts OAuth deliberately: panel OAuth is the operator, and an
    operator locked out by a ceiling they set on themselves cannot raise it.

    It is exempt *by construction* — the OAuth branch never calls
    `current_daily_request_limit.set`, so the default None stands — rather than
    by a list of exempt credential kinds somebody has to remember to update.
    """
    from datetime import datetime, timedelta, timezone

    from src.models.db import OAuthToken

    token = OAuthToken(
        id=42,
        token_hash="y",
        token_type="access",
        client_id="client-abc",
        scope="read",
        user_id=None,
        grant_id="g1",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        revoked=False,
    )
    captured = _drive(
        "oauth-token",
        _MiddlewareSession(oauth_token=token, client_row=(None, "Claude Desktop")),
    )

    assert captured["status"] == 200
    assert captured["limit"] is None
    assert captured["key_id"] is None
