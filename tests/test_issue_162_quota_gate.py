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
import datetime as _dt
import os
import tempfile

os.environ.setdefault("SECRET_KEY", "test")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("VAULT_PATH", "/tmp/test-vault")
os.chdir(tempfile.gettempdir())

import pytest  # noqa: E402

import src.mcp_server.auth as mcp_auth  # noqa: E402

TODAY = _dt.datetime.now(_dt.timezone.utc).date()
import src.mcp_server.tools as tools  # noqa: E402
import src.services.quotas as quotas  # noqa: E402
from src.models.db import APIKey  # noqa: E402
from src.services import refusals, usage_stats  # noqa: E402


def _sentinel_payload(message: str) -> dict:
    """The JSON object on a refusal's machine-readable final line."""
    import json

    line = message.splitlines()[-1]
    assert line.startswith(f"{refusals.SENTINEL} "), message
    return json.loads(line.split(" ", 1)[1])


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


@tools._tracked("quota_probe", ["value"], resource_class="other")
async def _probe(value: str = "x") -> str:
    """A minimal tracked tool. The decorator is what is under test, so a real
    tool would only add a vault and a database to the surface."""
    return f"ran:{value}"


@tools._tracked("quota_probe_raises", [], resource_class="other")
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
    # Bracket the call with two clock reads: a suite running across UTC
    # midnight (it happened — CI started 23:56Z, this test ran 00:05Z) must
    # accept the reset for whichever day the admission actually used.
    day_before = _dt.datetime.now(_dt.timezone.utc).date()
    result, params, spy = _run_tracked(
        _probe, limit=5, spy=_QuotaSpySession(admitted=None)
    )
    day_after = _dt.datetime.now(_dt.timezone.utc).date()

    # The prose is unchanged and the sentinel line is **appended** (#194), so
    # the refusal still *starts* with exactly what #162 wrote — which is the
    # additive property the rate-limit change promised for all three
    # pre-existing pre-body refusals.
    expected = {
        quotas.quota_refusal_message(5, quotas.reset_instant(d))
        for d in {day_before, day_after}
    }
    assert any(result.startswith(prose) for prose in expected)
    assert result.splitlines()[-1].startswith("MCP-REFUSAL ")
    assert "ran:" not in result, "the tool body ran anyway"
    assert len(_admissions(spy)) == 1


def test_the_refusal_message_names_the_limit_and_the_utc_reset():
    """The reader is an agent. "Quota exceeded" gives it nothing to act on; a
    number and a timestamp let it wait or tell its operator what to raise."""
    reset = quotas.reset_instant(TODAY)
    message = quotas.quota_refusal_message(250, reset)
    assert "250" in message
    assert "UTC" in message
    assert reset.strftime("%Y-%m-%dT%H:%M:%SZ") in message
    assert reset.hour == 0 and reset.minute == 0 and reset.second == 0
    assert reset.date() == TODAY + _dt.timedelta(days=1)


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
        assert asyncio.run(quotas.admit(7, 100)).count == 1
    finally:
        mp.undo()
    assert [s for s, _ in fresh.statements if "DELETE FROM quota_counters" in s]

    later = _QuotaSpySession(admitted=42)
    mp = pytest.MonkeyPatch()
    mp.setattr(quotas, "async_session", later)
    try:
        assert asyncio.run(quotas.admit(7, 100)).count == 42
    finally:
        mp.undo()
    assert not [s for s, _ in later.statements if "DELETE FROM quota_counters" in s]


def test_a_refusal_issues_no_second_statement():
    refused = _QuotaSpySession(admitted=None)
    mp = pytest.MonkeyPatch()
    mp.setattr(quotas, "async_session", refused)
    try:
        assert not asyncio.run(quotas.admit(7, 100)).admitted
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


# --------------------------------------------------------------------------
# 8. the panel pages render with the context their routes actually build
# --------------------------------------------------------------------------
#
# These exist because of a bug this file caught: the usage route passed the
# selector options as one dict and the template iterated `filter_options.keys`,
# which Jinja resolves to the dict's **method** — attribute lookup is tried
# before item lookup — so the key selector iterated a builtin and 500'd the
# page. Nothing else would have found it: the route returns a context object in
# every unit test, `TemplateResponse` is stubbed, and the template is only
# compiled when a browser asks for it.
#
# So the context comes from the **real route**, and the template is the real
# one. A renamed context key fails here rather than in production.


def _render(route_coro, template_name, session):
    """Run a panel route for real, then render its real template."""
    import os as _os

    from jinja2 import (
        ChainableUndefined,
        ChoiceLoader,
        DictLoader,
        Environment,
        FileSystemLoader,
    )

    import src.control_panel.routes as panel

    captured = {}

    def _capture(request, name, context):
        captured["name"] = name
        captured["context"] = context
        return None

    mp = pytest.MonkeyPatch()
    mp.setattr(panel.templates, "TemplateResponse", _capture)
    mp.setattr(panel, "generate_csrf_token", lambda _r: "csrf-token")
    try:
        asyncio.run(route_coro(panel, session))
    finally:
        mp.undo()

    assert captured["name"] == template_name

    here = _os.path.dirname(_os.path.abspath(__file__))
    templates = _os.path.join(here, "..", "src", "control_panel", "templates")
    env = Environment(
        loader=ChoiceLoader([
            DictLoader({
                "base.html":
                    "{% block title %}{% endblock %}{% block content %}{% endblock %}"
            }),
            FileSystemLoader(templates),
        ]),
        undefined=ChainableUndefined,
        autoescape=True,
    )
    context = dict(captured["context"])
    context.pop("request", None)
    return env.get_template(template_name).render(**context)


class _Row(dict):
    """A row that answers to attribute access, like a SQLAlchemy `Row`."""

    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError as exc:  # pragma: no cover - a missing column is a bug
            raise AttributeError(name) from exc


class _PageResult:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows

    def all(self):
        return self._rows


class _UsagePageSession:
    """Answers each statement `usage_page` issues with one plausible row."""

    async def execute(self, stmt, params=None):
        sql = str(stmt)
        if "FROM users" in sql:
            return _PageResult([_Row(id=1, username="max")])
        if "FROM api_keys" in sql:
            return _PageResult([_Row(id=4, name="nightly", key_prefix="omcp_a1b2c3")])
        if "DISTINCT ul.tool" in sql:
            return _PageResult([_Row(tool="read_note")])
        if "count(*)" in sql and "GROUP BY" in sql and "actor_kind" in sql:
            return _PageResult([
                _Row(actor_kind="api_key", actor_label="nightly",
                     actor_ref="omcp_a1b2c3", api_key_name=None,
                     api_key_prefix=None, oauth_client_name=None,
                     requests=12, last_seen=_Stamp()),
                # The row an operator opens this page to read: no label and no
                # join, i.e. a credential deleted before migration 015.
                _Row(actor_kind=None, actor_label=None, actor_ref=None,
                     api_key_name=None, api_key_prefix=None,
                     oauth_client_name=None, requests=1, last_seen=None),
            ])
        if "date_trunc" in sql:
            return _PageResult([_Row(bucket=_Stamp(), cnt=12)])
        return _PageResult([
            _Row(id=1, tool="read_note", duration_ms=12, created_at=_Stamp(),
                 actor_kind="api_key", actor_label="nightly",
                 actor_ref="omcp_a1b2c3", api_key_name=None,
                 api_key_prefix=None, oauth_client_name=None)
        ])


class _Stamp:
    def isoformat(self):
        return "2026-08-29T10:00:00+00:00"

    def strftime(self, _fmt):
        return "10:00"


class _KeysPageSession:
    """The key/owner join, then the quota counters for today."""

    async def execute(self, stmt, params=None):
        if params is not None:
            return _PageResult([_Row(key_id=4, count=7)])
        limited = APIKey(
            id=4, name="nightly", key_hash="x", key_prefix="omcp_a1b2c3",
            permission="read", is_active=True, user_id=1, expires_at=None,
            daily_request_limit=100,
        )
        limited.created_at = _Stamp()
        limited.last_used_at = None
        unlimited = APIKey(
            id=5, name="ad hoc", key_hash="y", key_prefix="omcp_b2c3d4",
            permission="readwrite", is_active=True, user_id=1, expires_at=None,
            daily_request_limit=None,
        )
        unlimited.created_at = _Stamp()
        unlimited.last_used_at = None
        return _PageResult([(limited, "max", True), (unlimited, "max", True)])


def test_the_usage_page_renders_the_context_its_route_builds():
    from types import SimpleNamespace

    async def route(panel, session):
        return await panel.usage_page(
            request=SimpleNamespace(session={}, scope={}),
            session=session,
            user=SimpleNamespace(id=1, is_admin=True, username="max"),
        )

    html = _render(route, "usage.html", _UsagePageSession())

    # Every selector rendered its options — the regression this exists for is
    # one of these loops iterating a dict method instead of a list.
    assert 'name="user"' in html and ">max<" in html
    assert 'name="key"' in html and "omcp_a1b2c3" in html
    assert 'name="tool"' in html and ">read_note<" in html
    # The per-actor totals table, including the unattributable row.
    assert "By actor" in html
    assert "unknown (credential deleted)" in html
    assert ">12<" in html


def test_the_keys_page_renders_both_a_limited_and_an_unlimited_key():
    from types import SimpleNamespace

    async def route(panel, session):
        return await panel.keys_page(
            request=SimpleNamespace(session={}, scope={}),
            session=session,
            user=SimpleNamespace(id=1, is_admin=True, username="max"),
        )

    html = _render(route, "keys.html", _KeysPageSession())

    assert "7 / 100" in html, "the limited key's consumption is not rendered"
    assert "Unlimited" in html, "the unlimited key is not labelled"
    # The edit control carries the key's id and its current value, and both are
    # numeric — nothing quotable is interpolated into the `onclick`, which is
    # the trap documented for the OAuth delete's confirm().
    assert "omcpEditLimit(4, '100')" in html
    assert "omcpEditLimit(5, '')" in html
    # The create form offers the field, with the domain the CHECK enforces.
    assert 'name="daily_request_limit"' in html
    assert 'max="1000000"' in html
    assert 'min="1"' in html
    # And the copy states the basis, because "43 / 100" is otherwise read as
    # requests since midnight.
    assert "since the limit was set" in html


# --------------------------------------------------------------------------
# 9. the refusal's reset instant comes from the decision, not a second clock
# --------------------------------------------------------------------------
#
# The failure this closes: `admit()` picks the accounting day from one clock
# read, and the refusal message used to pick its reset from another. A call
# whose two reads straddle UTC midnight then decides against day D (D's counter
# is full) and reports D+2's midnight as the reset — telling an obedient agent
# to back off for nearly forty-eight hours when its quota was milliseconds from
# resetting. Self-inflicted, and invisible to every test that runs mid-day.


def test_reset_instant_takes_a_day_not_a_clock_reading():
    """The signature is the fix. A function taking `now` can always be handed a
    second, later `now`; one taking the accounting date cannot."""
    import inspect

    params = list(inspect.signature(quotas.reset_instant).parameters)
    assert params == ["day"]
    assert quotas.reset_instant(_dt.date(2026, 8, 29)) == _dt.datetime(
        2026, 8, 30, 0, 0, tzinfo=_dt.timezone.utc
    )


def test_an_admission_carries_the_day_it_decided_for():
    decided_at = _dt.datetime(2026, 8, 29, 12, 0, tzinfo=_dt.timezone.utc)
    refused = quotas.Admission(
        day=_dt.date(2026, 8, 29), count=None, decided_at=decided_at
    )
    assert refused.admitted is False
    assert refused.reset_at == _dt.datetime(
        2026, 8, 30, 0, 0, tzinfo=_dt.timezone.utc
    )
    admitted = quotas.Admission(
        day=_dt.date(2026, 8, 29), count=7, decided_at=decided_at
    )
    assert admitted.admitted is True


def test_an_admission_carries_the_instant_it_decided_at(monkeypatch):
    """`decided_at` is **required**, and it is the reading `day` came from.

    A default would let an `Admission` exist without the instant the whole
    retry interval is measured from, and the fallback such a decision took
    would be a second clock read — the exact defect (#194, D10). So the
    dataclass refuses to be built without it.
    """
    import inspect

    field = inspect.signature(quotas.Admission).parameters["decided_at"]
    assert field.default is inspect.Parameter.empty, (
        "decided_at acquired a default, so an Admission can exist without the "
        "instant its retry interval is measured from"
    )

    frozen = _dt.datetime(2026, 8, 29, 12, 0, tzinfo=_dt.timezone.utc)
    spy = _QuotaSpySession(admitted=None)
    monkeypatch.setattr(quotas, "async_session", spy)

    decision = asyncio.run(quotas.admit(7, 5, now=frozen))

    assert decision.decided_at == frozen
    assert decision.day == frozen.date(), "the day did not come from that reading"


def test_the_retry_interval_is_arithmetic_on_the_decision_alone():
    """No clock, no `now`: the interval is a pure function of the pair the
    decision recorded, so reading it twice cannot give two answers."""
    day = _dt.date(2026, 8, 29)
    decision = quotas.Admission(
        day=day,
        count=None,
        decided_at=_dt.datetime(2026, 8, 29, 23, 0, tzinfo=_dt.timezone.utc),
    )
    assert decision.retry_after_seconds == 3600
    assert decision.retry_after_seconds == decision.retry_after_seconds

    # Sub-second, and already-past, both floor at one second: zero invites a
    # retry that arrives before the counter has rolled, and a negative number
    # is not a refusal shape `Refusal` will accept at all.
    late = quotas.Admission(
        day=day,
        count=None,
        decided_at=_dt.datetime(
            2026, 8, 29, 23, 59, 59, 900000, tzinfo=_dt.timezone.utc
        ),
    )
    assert late.retry_after_seconds == 1
    overdue = quotas.Admission(
        day=day,
        count=None,
        decided_at=_dt.datetime(2026, 8, 31, 12, 0, tzinfo=_dt.timezone.utc),
    )
    assert overdue.retry_after_seconds == 1


def test_a_refusal_straddling_utc_midnight_names_the_right_midnight(monkeypatch):
    """The normative case, with the two clock reads frozen either side of
    midnight: the statement decides against 2026-08-29 at 23:59:59.9, and the
    message is built after the clock has ticked over to 2026-08-30.

    The reset must be **2026-08-30T00:00:00Z** — the boundary the decision was
    actually made against — and never 2026-08-31's.
    """
    before = _dt.datetime(2026, 8, 29, 23, 59, 59, 900000, tzinfo=_dt.timezone.utc)
    after = _dt.datetime(2026, 8, 30, 0, 0, 0, 100000, tzinfo=_dt.timezone.utc)
    readings = iter([before, after, after, after])

    class _FrozenClock(_dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return next(readings)

    # Patch the clock *inside* `quotas`, so `admit` picks its day from `before`
    # and anything that later re-read the clock would get `after`.
    monkeypatch.setattr(quotas._dt, "datetime", _FrozenClock)

    spy = _QuotaSpySession(admitted=None)
    monkeypatch.setattr(quotas, "async_session", spy)

    decision = asyncio.run(quotas.admit(7, 5))

    # The statement was bound to the day the first reading fell in.
    assert decision.day == _dt.date(2026, 8, 29)
    assert spy.statements[0][1]["day"] == _dt.date(2026, 8, 29)
    assert decision.admitted is False

    message = quotas.quota_refusal_message(5, decision.reset_at)
    assert "2026-08-30T00:00:00Z" in message, message
    assert "2026-08-31" not in message, (
        "the refusal named the day-after-next's midnight — an obedient agent "
        "would back off for ~24h of quota it was entitled to spend"
    )


def test_the_gate_hands_the_decisions_reset_to_the_message(monkeypatch):
    """End to end through `_tracked`: whatever day the admission decided for is
    the day the caller is told about, with no clock read in between.

    The decided day is deliberately **not** today. A gate that re-read the
    clock would produce today+1 and pass this test on any day that happened to
    match — which is how the original defect survived: mid-day, the two reads
    agree, and only the run that straddles midnight disagrees.
    """
    fixed_day = _dt.date(2019, 3, 7)
    assert fixed_day != TODAY, "the fixture must not coincide with today"

    async def fake_admit(key_id, limit, now=None):
        return quotas.Admission(
            day=fixed_day,
            count=None,
            decided_at=_dt.datetime(2019, 3, 7, 9, 0, tzinfo=_dt.timezone.utc),
        )

    monkeypatch.setattr(tools, "_admit_quota", fake_admit)
    result, params, _ = _run_tracked(_probe, limit=5)

    assert params["over_quota"] is True
    assert "2019-03-08T00:00:00Z" in result, result
    today_reset = quotas.reset_instant(
        _dt.datetime.now(_dt.timezone.utc).date()
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert today_reset not in result, (
        "the gate re-read the clock instead of using the admission's day"
    )


def test_the_retry_interval_survives_a_decision_taken_at_utc_midnight(monkeypatch):
    """D10, end to end: the decision lands 100 ms before midnight and the
    refusal is built after the clock has crossed it.

    Two failures are pinned at once, and the second is the one that is easy to
    reintroduce because it looks like ordinary code:

    * the reset must be **2026-08-30T00:00:00Z** — the boundary the statement
      was actually bound to — and never 2026-08-31's;
    * the interval must be the **small** one. A second clock read after
      midnight, subtracted from a reset recomputed for the new day, quotes
      ~86,400 seconds — nearly forty-eight hours from the decision — and an
      obedient agent then sleeps through quota it was entitled to spend. That
      is a self-inflicted outage produced entirely by reading the clock twice.

    The clock inside `quotas` counts its readings, and the decorator's own
    clock **raises** if it is read at all: between the admission statement and
    the rendered refusal there must be no reading, not merely a harmless one.
    """
    before = _dt.datetime(2026, 8, 29, 23, 59, 59, 900000, tzinfo=_dt.timezone.utc)
    after = _dt.datetime(2026, 8, 30, 0, 0, 0, 100000, tzinfo=_dt.timezone.utc)
    reads = []

    class _CountingClock(_dt.datetime):
        @classmethod
        def now(cls, tz=None):
            reads.append(len(reads))
            # Only the first reading falls before midnight: anything that read
            # the clock again would be told it is already the next day.
            return before if len(reads) == 1 else after

    class _ForbiddenClock(_dt.datetime):
        @classmethod
        def now(cls, tz=None):  # pragma: no cover - the assertion is the test
            raise AssertionError(
                "the quota gate read the clock after the admission decided"
            )

    monkeypatch.setattr(quotas._dt, "datetime", _CountingClock)
    monkeypatch.setattr(tools, "datetime", _ForbiddenClock)

    result, params, spy = _run_tracked(
        _probe, limit=5, spy=_QuotaSpySession(admitted=None)
    )

    assert params["over_quota"] is True
    assert spy.statements[0][1]["day"] == _dt.date(2026, 8, 29)
    assert len(reads) == 1, (
        f"the clock was read {len(reads)} times; the decision is one reading "
        "and everything downstream is arithmetic on it"
    )

    assert "2026-08-30T00:00:00Z" in result, result
    assert "2026-08-31" not in result

    payload = _sentinel_payload(result)
    assert payload["code"] == refusals.OVER_QUOTA
    assert payload["limit"] == 5
    assert payload["limit_unit"] == refusals.CALLS_PER_DAY
    assert payload["retry_after_seconds"] == 1, (
        "the refusal quoted an interval measured from a second clock read"
    )


def test_the_message_carries_the_sentinel_when_given_the_decisions_interval():
    """`quota_refusal_message` renders the machine-readable line itself when it
    is handed the interval the decision derived — and the prose in front of it
    is byte-for-byte what #162 wrote, because the line is *appended*."""
    decision = quotas.Admission(
        day=_dt.date(2026, 8, 29),
        count=None,
        decided_at=_dt.datetime(2026, 8, 29, 23, 0, tzinfo=_dt.timezone.utc),
    )

    prose = quotas.quota_refusal_message(5, decision.reset_at)
    message = quotas.quota_refusal_message(
        5, decision.reset_at, decision.retry_after_seconds
    )

    assert refusals.SENTINEL not in prose, (
        "the bare two-argument call must stay prose-only: it holds a reset "
        "instant but no interval, and an over-quota refusal that omitted "
        "retry_after_seconds would tell an agent that waiting cannot help"
    )
    assert message.startswith(prose + "\n")
    assert _sentinel_payload(message) == {
        "code": refusals.OVER_QUOTA,
        "scope": quotas.QUOTA_REFUSAL_SCOPE,
        "limit": 5,
        "limit_unit": refusals.CALLS_PER_DAY,
        "retry_after_seconds": 3600,
    }


def test_rendering_the_message_again_does_not_stack_a_second_line():
    """The two altitudes — this module composing the refusal, the decorator
    rendering it — must not be able to put two sentinel lines on one message
    by both doing their job. `refusals.render` is idempotent."""
    decision = quotas.Admission(
        day=_dt.date(2026, 8, 29),
        count=None,
        decided_at=_dt.datetime(2026, 8, 29, 23, 0, tzinfo=_dt.timezone.utc),
    )
    message = quotas.quota_refusal_message(
        5, decision.reset_at, decision.retry_after_seconds
    )

    again = refusals.render(
        message,
        refusals.Refusal(
            code=refusals.OVER_QUOTA,
            scope=quotas.QUOTA_REFUSAL_SCOPE,
            limit=5,
            limit_unit=refusals.CALLS_PER_DAY,
            retry_after_seconds=1,
        ),
    )

    assert again == message
    assert message.count(refusals.SENTINEL) == 1

