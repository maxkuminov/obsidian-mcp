"""Usage attribution survives deleting the credential (#77).

`/admin/usage` resolved every log line's actor by LEFT JOIN — through
`api_keys`, or through `oauth_tokens` -> `oauth_clients`. Both joins are
allowed to go NULL while the log row stays, and both do so on the operator's
own most urgent path:

- deleting an OAuth client cascades its tokens, and
  `usage_logs.oauth_token_id` is `ON DELETE SET NULL`;
- `usage_logs.key_id` has no `ON DELETE` at all, so the panel explicitly
  `UPDATE usage_logs SET key_id = NULL` before deleting an API key.

Either way, an operator who stops a suspect credential and then opens the Usage
page to review what it did is shown "unknown" for every row it produced — the
evidence destroyed by the button pressed to stop it.

The fix denormalises the actor at call time. These tests pin the whole chain:
the middleware binds the label from the credential row it already loaded,
`_log_usage` writes it onto `usage_logs`, and the panel prefers it over the
join — so NULLing `key_id` or cascading a token changes nothing about what the
page renders.

Fully offline: no database, no network.
"""

import asyncio
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("SECRET_KEY", "test")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("VAULT_PATH", "/tmp/test-vault")
_TEMPLATES = Path(__file__).resolve().parent.parent / "src" / "control_panel" / "templates"
os.chdir(tempfile.gettempdir())

import pytest  # noqa: E402

import src.mcp_server.auth as mcp_auth  # noqa: E402
import src.mcp_server.tools as tools  # noqa: E402
from src.auth.session import current_actor  # noqa: E402
from src.control_panel.routes import _usage_actor  # noqa: E402
from src.models.db import APIKey, OAuthToken, UsageLog  # noqa: E402


# --------------------------------------------------------------------------
# 1. `_log_usage` writes the denormalised columns
# --------------------------------------------------------------------------


class _CapturingSession:
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
        self.committed = True


def _log_with_actor(actor, monkeypatch) -> UsageLog:
    session = _CapturingSession()
    monkeypatch.setattr(tools, "async_session", lambda: session)

    async def run():
        token = current_actor.set(actor)
        try:
            await tools._log_usage("read_note", {"path": "a.md"}, 12, 34)
        finally:
            current_actor.reset(token)

    asyncio.run(run())
    assert session.committed, "the usage row was never committed"
    assert len(session.added) == 1
    return session.added[0]


def test_log_usage_denormalises_an_api_key_actor(monkeypatch):
    row = _log_with_actor(("api_key", "nightly sync", "omcp_a1b2c3d4"), monkeypatch)
    assert row.actor_kind == "api_key"
    assert row.actor_label == "nightly sync"
    assert row.actor_ref == "omcp_a1b2c3d4"


def test_log_usage_denormalises_an_oauth_actor(monkeypatch):
    row = _log_with_actor(("oauth", "Claude Desktop", "client-abc"), monkeypatch)
    assert row.actor_kind == "oauth"
    assert row.actor_label == "Claude Desktop"
    assert row.actor_ref == "client-abc"


def test_log_usage_without_an_actor_leaves_the_columns_unset(monkeypatch):
    """A caller outside a request — the indexer, a test, sandbox mode — has no
    credential to name, and must still record that the call happened."""
    row = _log_with_actor(None, monkeypatch)
    assert row.actor_kind is None
    assert row.actor_label is None
    assert row.actor_ref is None


def test_an_over_long_label_is_truncated_not_dropped(monkeypatch):
    """`_log_usage` swallows exceptions, so an over-wide value would silently
    lose the *whole* usage row — the opposite of what these columns are for.

    `api_keys.name` and `actor_label` are both varchar(255) today, so this
    guards the pairing rather than a live overflow: widen one without the
    other and the truncation is what keeps the row.
    """
    row = _log_with_actor(("api_key", "k" * 400, "r" * 200), monkeypatch)
    assert len(row.actor_label) == tools._ACTOR_LABEL_MAX
    assert len(row.actor_ref) == tools._ACTOR_REF_MAX


# --------------------------------------------------------------------------
# 2. the middleware binds the label from the credential it authenticated
# --------------------------------------------------------------------------


class _EmptyResult:
    rowcount = 0


class _RowsResult:
    def __init__(self, rows):
        self._rows = list(rows)

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)

    def first(self):
        return self._rows[0] if self._rows else None

    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else None


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


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
            # `(token, client_owner, client_name)` — one statement, joined.
            if self.oauth_token is None:
                return _RowsResult([])
            owner, name = self.client_row if self.client_row else (None, None)
            return _RowsResult([(self.oauth_token, owner, name)])
        if "vault_path" in sql:
            return _RowsResult([])
        return _ScalarResult(True)


def _drive(bearer: str, session: _MiddlewareSession, multi_user: bool = False) -> dict:
    """Run the real middleware once; report what it bound inside the request."""
    captured = {}

    async def downstream(scope, receive, send):
        captured["actor"] = current_actor.get()
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
            mp.setattr(mcp_auth.settings, "multi_user_mode", multi_user, raising=False)
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
            # Read back inside the same context the middleware ran in.
            captured["after"] = current_actor.get()
        finally:
            mp.undo()

    asyncio.run(run())
    captured["status"] = sent[0]["status"] if sent else None
    return captured


def test_middleware_binds_the_api_key_name_and_prefix():
    api_key = APIKey(
        id=7,
        name="nightly sync",
        key_hash="x",
        key_prefix="omcp_a1b2c3",
        permission="read",
        user_id=None,
        expires_at=None,
        is_active=True,
    )
    captured = _drive("omcp_testkey", _MiddlewareSession(api_key=api_key))

    assert captured["status"] == 200, "the request was not authenticated"
    assert captured["actor"] == ("api_key", "nightly sync", "omcp_a1b2c3")
    # And it does not outlive the request.
    assert captured["after"] is None


def test_middleware_binds_the_oauth_client_name_and_id():
    from datetime import datetime, timedelta, timezone

    token = OAuthToken(
        id=11,
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

    assert captured["status"] == 200, "the request was not authenticated"
    assert captured["actor"] == ("oauth", "Claude Desktop", "client-abc")
    assert captured["after"] is None


# --------------------------------------------------------------------------
# 3. the panel prefers the denormalised label over the join
# --------------------------------------------------------------------------


def _row(**kwargs):
    """One `/admin/usage` result row. Defaults are "nothing resolves"."""
    fields = {
        "actor_kind": None,
        "actor_label": None,
        "actor_ref": None,
        "api_key_name": None,
        "api_key_prefix": None,
        "oauth_client_name": None,
    }
    fields.update(kwargs)
    return SimpleNamespace(**fields)


def test_the_denormalised_api_key_label_survives_key_id_being_nulled():
    """The exact state the panel leaves behind before deleting an API key.

    `delete_key_form` / `delete_all_revoked` run
    `UPDATE usage_logs SET key_id = NULL` first, because `key_id` has no
    `ON DELETE` and the delete would otherwise raise. So the join columns are
    NULL while the row is intact — and the label must still name the key.
    """
    row = _row(actor_kind="api_key", actor_label="nightly sync", actor_ref="omcp_a1b2c3")
    assert _usage_actor(row) == ("nightly sync", "omcp_a1b2c3")


def test_the_denormalised_oauth_label_survives_the_client_delete_cascade():
    """Deleting an OAuth client cascades its tokens and SET NULLs
    `usage_logs.oauth_token_id`, so both join columns are NULL here."""
    row = _row(actor_kind="oauth", actor_label="Claude Desktop", actor_ref="client-abc")
    assert _usage_actor(row) == ("Claude Desktop", "OAuth · client-abc")


def test_the_join_is_the_fallback_for_rows_written_before_015():
    """Pre-015 rows carry no label; the join still answers while it can."""
    assert _usage_actor(
        _row(api_key_name="legacy key", api_key_prefix="omcp_zzz")
    ) == ("legacy key", "omcp_zzz")
    assert _usage_actor(_row(oauth_client_name="Legacy App")) == ("Legacy App", "OAuth")


def test_the_denormalised_label_wins_over_a_stale_join():
    """A key renamed after the call must not relabel history.

    The label is a snapshot of the credential at call time; the join reports
    what the credential is called *now*. Preferring the join would silently
    rewrite the audit trail on every rename.
    """
    row = _row(
        actor_kind="api_key",
        actor_label="nightly sync",
        actor_ref="omcp_a1b2c3",
        api_key_name="renamed later",
        api_key_prefix="omcp_a1b2c3",
    )
    assert _usage_actor(row) == ("nightly sync", "omcp_a1b2c3")


def test_a_row_with_neither_is_reported_as_unknown():
    assert _usage_actor(_row()) == (None, None)


def test_a_kind_without_a_label_does_not_suppress_a_live_join():
    """The gate is `actor_label`, not `actor_kind`.

    A kind with no label names nothing an operator can read, so treating it as
    "recorded" would suppress a join that could still have answered. The writer
    cannot produce this row (`api_keys.name` and `oauth_clients.client_name`
    are both NOT NULL), which is exactly why preferring the join is free.
    """
    row = _row(
        actor_kind="api_key",
        actor_ref="omcp_a1b2c3",
        api_key_name="still here",
        api_key_prefix="omcp_a1b2c3",
    )
    assert _usage_actor(row) == ("still here", "omcp_a1b2c3")


def test_a_kind_without_a_label_and_no_join_is_unknown():
    assert _usage_actor(_row(actor_kind="oauth", actor_ref="client-abc")) == (None, None)


def test_an_unrecognised_actor_kind_is_not_rendered_as_oauth():
    """A row written by a newer build, or by hand. Show what it says; do not
    assert a credential type — the previous shape fell through to the OAuth
    branch, which would have labelled an API key "OAuth"."""
    row = _row(actor_kind="webhook", actor_label="Some Integration", actor_ref="wh-1")
    name, detail = _usage_actor(row)
    assert name == "Some Integration"
    assert detail == "wh-1"
    assert "OAuth" not in (detail or "")


# --------------------------------------------------------------------------
# 4. rendered copy
# --------------------------------------------------------------------------


def _render(template: str, **context) -> str:
    from jinja2 import ChainableUndefined, ChoiceLoader, DictLoader, Environment, FileSystemLoader

    env = Environment(
        loader=ChoiceLoader([
            DictLoader({
                "base.html": "{% block title %}{% endblock %}{% block content %}{% endblock %}"
            }),
            FileSystemLoader(str(_TEMPLATES)),
        ]),
        undefined=ChainableUndefined,
        autoescape=True,
    )
    return env.get_template(template).render(**context)


def test_usage_page_says_why_an_actor_is_unknown():
    """A bare "unknown" reads as "the server failed to record who called".
    The true statement is that the credential was deleted before the label
    column existed, and the page has to say so."""
    rendered = _render(
        "usage.html",
        logs=[{
            "tool": "read_note",
            "duration_ms": 5,
            "created_at": "2026-01-01T00:00:00Z",
            "actor_name": None,
            "actor_detail": None,
        }],
        chart_data={"labels": [], "values": []},
    )
    assert "unknown (credential deleted)" in rendered


def test_usage_page_renders_a_denormalised_oauth_label():
    rendered = _render(
        "usage.html",
        logs=[{
            "tool": "read_note",
            "duration_ms": 5,
            "created_at": "2026-01-01T00:00:00Z",
            "actor_name": "Claude Desktop",
            "actor_detail": "OAuth · client-abc",
        }],
        chart_data={"labels": [], "values": []},
    )
    assert "Claude Desktop" in rendered
    assert "client-abc" in rendered
    assert "unknown" not in rendered


def test_the_delete_confirm_states_the_real_blast_radius():
    """The old text promised a revocation ("Delete this client and revoke all
    its tokens?"). Assert the *rendered* button, so a template refactor cannot
    quietly restore the promise.

    The three facts the operator needs before clicking: the tokens go, the
    transfer capabilities minted under them go, and the usage history stays
    attributed. The last one is the one #77 is about.
    """
    rendered = _render(
        "oauth.html",
        clients=[SimpleNamespace(
            client_id="client-abc",
            client_name="Claude Desktop",
            scope="read",
            created_at="2026-01-01T00:00:00Z",
            grants=[],
            can_write=False,
        )],
        csrf_token="t",
        flash_error=None,
    )
    confirms = [line for line in rendered.splitlines() if "/delete" in line or "confirm(" in line]
    blob = "\n".join(confirms)
    assert "revoke all its tokens?" not in blob
    assert "tokens are deleted" in blob
    assert "upload or download links" in blob
    assert "usage history stays" in blob


def test_a_hostile_label_is_escaped_in_the_rendered_page():
    """The label is attacker-influenced text.

    An OAuth `client_name` arrives in an unauthenticated `/register` body, so a
    self-registered client chooses what the operator's Usage page renders — and
    #77's whole point is that the label now *outlives* the client, so deleting
    the client no longer removes it from the page. Autoescaping is what stands
    between that and script execution in an admin session. Assert the rendered
    output, not the template source.
    """
    rendered = _render(
        "usage.html",
        logs=[{
            "tool": "read_note",
            "duration_ms": 5,
            "created_at": "2026-01-01T00:00:00Z",
            "actor_name": "<script>alert(1)</script>",
            "actor_detail": "OAuth · <img src=x onerror=alert(2)>",
        }],
        chart_data={"labels": [], "values": []},
    )
    # The payload must survive only as text. Assert on the *markup* — the
    # escaped form still contains the substring `onerror=alert(2)`, harmlessly,
    # inside a text node, so testing for that substring would be testing the
    # wrong thing.
    assert "<script>alert(1)" not in rendered
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in rendered
    assert "<img" not in rendered
    assert "&lt;img src=x onerror=alert(2)&gt;" in rendered


# --------------------------------------------------------------------------
# 5. the log row survives a credential deleted mid-call
# --------------------------------------------------------------------------


class _ForeignKeyViolation(Exception):
    """Stands in for asyncpg's `ForeignKeyViolationError`."""

    def __init__(self, constraint_name=None):
        super().__init__("insert violates foreign key constraint")
        self.sqlstate = "23503"
        self.constraint_name = constraint_name


class _DialectError(Exception):
    """The asyncpg dialect's DBAPI-shaped error: SQLSTATE, no constraint name.

    This is the layer SQLAlchemy actually puts in `.orig`. It is separate from
    the asyncpg error, which hangs off it as `__cause__` and is the only place
    the constraint name appears — pinned against a real database in
    `tests/integration/test_usage_log_fk_recovery.py`.
    """

    def __init__(self, cause):
        super().__init__(str(cause))
        self.sqlstate = getattr(cause, "sqlstate", None)
        self.__cause__ = cause


class _WrappedIntegrityError(Exception):
    """Stands in for SQLAlchemy's `IntegrityError`, wrapping both layers."""

    def __init__(self, orig, *, wrap_in_dialect_layer=True):
        super().__init__("integrity error")
        self.orig = _DialectError(orig) if wrap_in_dialect_layer else orig


class _FailingSession:
    """Raises `raises[n]` on the n-th commit; records what was added."""

    def __init__(self, raises):
        self.raises = list(raises)
        self.added = []
        self.rolled_back = 0
        self.commits = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    def add(self, obj):
        self.added.append(obj)

    async def rollback(self):
        self.rolled_back += 1

    async def commit(self):
        exc = self.raises[self.commits] if self.commits < len(self.raises) else None
        self.commits += 1
        if exc is not None:
            raise exc


class _FakeVar:
    def __init__(self, value):
        self._value = value

    def get(self):
        return self._value


def _run_log_usage(sessions, monkeypatch, actor=("api_key", "nightly sync", "omcp_a1")):
    made = []

    def _factory():
        session = sessions[len(made)] if len(made) < len(sessions) else sessions[-1]
        made.append(session)
        return session

    monkeypatch.setattr(tools, "async_session", _factory)
    monkeypatch.setattr(tools, "current_api_key_id", _FakeVar(7))
    monkeypatch.setattr(tools, "current_oauth_token_id", _FakeVar(None))
    monkeypatch.setattr(tools, "current_user_id", _FakeVar(3))

    async def run():
        token = current_actor.set(actor)
        try:
            await tools._log_usage("read_note", {"path": "a.md"}, 12, 34)
        finally:
            current_actor.reset(token)

    asyncio.run(run())
    return made


def test_a_deleted_key_mid_call_does_not_discard_the_log_row(monkeypatch):
    """The row an operator investigating that key most wants to see.

    A revoke-then-delete can commit while a slow call is still running. The
    insert then names an `api_keys` row that is gone, and a blanket `except`
    drops the whole audit line — losing exactly the history the `actor_*`
    columns exist to preserve. Denormalising the label is pointless if the row
    it rides on is the thing discarded.
    """
    first = _FailingSession([
        _WrappedIntegrityError(_ForeignKeyViolation("usage_logs_key_id_fkey"))
    ])
    second = _FailingSession([])
    made = _run_log_usage([first, second], monkeypatch)

    assert len(made) == 2, "the insert was not retried"
    assert first.rolled_back == 1, "the failed transaction was not rolled back"
    row = second.added[0]
    assert row.key_id is None and row.oauth_token_id is None
    # A key FK failure says nothing about the user, and the panel scopes a
    # non-admin's page by `user_id` — dropping it would hide the row from the
    # one person entitled to see it.
    assert row.user_id == 3
    assert (row.actor_kind, row.actor_label, row.actor_ref) == (
        "api_key", "nightly sync", "omcp_a1",
    )


def test_a_deleted_oauth_client_mid_call_does_not_discard_the_log_row(monkeypatch):
    first = _FailingSession([
        _WrappedIntegrityError(_ForeignKeyViolation("fk_usage_logs_oauth_token_id"))
    ])
    second = _FailingSession([])
    made = _run_log_usage(
        [first, second], monkeypatch, actor=("oauth", "Claude Desktop", "client-abc")
    )

    row = made[1].added[0]
    assert row.oauth_token_id is None and row.key_id is None
    assert row.user_id == 3
    assert row.actor_label == "Claude Desktop"


def test_a_violated_user_fk_clears_user_id_too(monkeypatch):
    first = _FailingSession([
        _WrappedIntegrityError(_ForeignKeyViolation("fk_usage_logs_user_id"))
    ])
    second = _FailingSession([])
    made = _run_log_usage([first, second], monkeypatch)

    row = made[1].added[0]
    assert row.user_id is None
    assert row.actor_label == "nightly sync"


def test_an_unnamed_fk_violation_clears_every_credential_column(monkeypatch):
    """Unresolvable is treated as "it might have been user_id".

    Clearing a column that did not have to be cleared costs the row its
    per-user scoping. Not clearing the one that did costs the row entirely.
    """
    first = _FailingSession([_WrappedIntegrityError(_ForeignKeyViolation(None))])
    second = _FailingSession([])
    made = _run_log_usage([first, second], monkeypatch)

    row = made[1].added[0]
    assert (row.key_id, row.oauth_token_id, row.user_id) == (None, None, None)
    assert row.actor_label == "nightly sync"


def test_a_non_fk_failure_is_not_retried(monkeypatch):
    """The retry is for one recoverable shape, not a general second attempt."""
    first = _FailingSession([RuntimeError("connection reset")])
    made = _run_log_usage([first, _FailingSession([])], monkeypatch)

    assert len(made) == 1, "a non-FK failure must not be retried"


def test_a_failing_retry_never_raises(monkeypatch):
    """Usage logging must not fail a tool call that has already done its work."""
    first = _FailingSession([
        _WrappedIntegrityError(_ForeignKeyViolation("usage_logs_key_id_fkey"))
    ])
    second = _FailingSession([RuntimeError("still broken")])
    made = _run_log_usage([first, second], monkeypatch)

    assert len(made) == 2


def test_the_constraint_name_is_read_through_the_whole_exception_chain():
    """The constraint name lives two layers down, and only there.

    SQLAlchemy's `.orig` is the dialect's DBAPI error, which carries the
    SQLSTATE; asyncpg's own error hangs off it as `__cause__` and is the only
    layer with `constraint_name`. Reading `orig.constraint_name` alone (the
    first draft) found nothing and so degraded *every* recovery to "assume it
    was user_id", silently dropping the row's owner.
    """
    exc = _WrappedIntegrityError(_ForeignKeyViolation("fk_usage_logs_oauth_token_id"))
    assert getattr(exc.orig, "constraint_name", None) is None, (
        "the fake no longer models the real wrapping"
    )
    assert tools._is_fk_violation(exc)
    assert tools._fk_constraint_name(exc) == "fk_usage_logs_oauth_token_id"
    assert not tools._violated_user_fk(exc)


def test_the_constraint_name_falls_back_to_the_message_text():
    """Belt and braces: if no layer exposes the attribute, the message still
    names the constraint, and every layer carries the message."""

    class _Nameless(Exception):
        sqlstate = "23503"

    exc = _WrappedIntegrityError(
        _Nameless('insert violates foreign key constraint "fk_usage_logs_user_id"')
    )
    assert tools._fk_constraint_name(exc) == "fk_usage_logs_user_id"
    assert tools._violated_user_fk(exc)


# --------------------------------------------------------------------------
# 6. the actor reaches every logged path, and leaves none of them behind
# --------------------------------------------------------------------------


def test_a_refused_call_still_records_the_actor(monkeypatch):
    """A call the admission gate refuses is the *most* worth attributing.

    `_tracked` logs the refusal with `params["error"] = "no_vault_assigned"`
    (issue #66). Which credential kept trying is the entire content of that
    line, so the actor columns have to come with it — and this path never
    reaches the tool body, so it is the one most easily left behind.
    """
    session = _CapturingSession()
    monkeypatch.setattr(tools, "async_session", lambda: session)
    monkeypatch.setattr(tools, "_vault_admission_error", lambda: tools._NO_VAULT_MESSAGE)

    async def run():
        token = current_actor.set(("oauth", "Claude Desktop", "client-abc"))
        try:
            return await tools.read_note_impl("a.md")
        finally:
            current_actor.reset(token)

    result = asyncio.run(run())

    # `read_note` declares an output schema, so its refusal is typed (#149).
    assert result.error == tools._NO_VAULT_MESSAGE
    assert result.content is None
    row = session.added[0]
    assert row.params["error"] == tools._NO_VAULT_MARKER
    assert (row.actor_kind, row.actor_label, row.actor_ref) == (
        "oauth", "Claude Desktop", "client-abc",
    )


def _drive_failing(bearer: str, session: _MiddlewareSession, error: BaseException):
    """Drive the middleware over a downstream that raises `error`."""
    seen = {}

    async def downstream(scope, receive, send):
        seen["during"] = current_actor.get()
        raise error

    async def receive():  # pragma: no cover - never awaited
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(_message):  # pragma: no cover - never reached
        return None

    async def run():
        mp = pytest.MonkeyPatch()
        try:
            mp.setattr(mcp_auth, "async_session", lambda: session)
            mp.setattr(mcp_auth.settings, "multi_user_mode", False, raising=False)
            app = mcp_auth.APIKeyMiddleware(downstream)
            try:
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
            except BaseException as raised:  # noqa: BLE001 - that is the point
                seen["raised"] = type(raised)
            # Read back inside the same context the middleware ran in.
            seen["after"] = current_actor.get()
        finally:
            mp.undo()

    asyncio.run(run())
    return seen


def _api_key_row():
    return APIKey(
        id=7, name="nightly sync", key_hash="x", key_prefix="omcp_a1b2c3",
        permission="read", user_id=None, expires_at=None, is_active=True,
    )


def _oauth_token_row(user_id=None):
    from datetime import datetime, timedelta, timezone

    return OAuthToken(
        id=11, token_hash="y", token_type="access", client_id="client-abc",
        scope="read", user_id=user_id, grant_id="g1",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1), revoked=False,
    )


def test_the_actor_is_reset_after_an_exception_on_the_key_branch():
    seen = _drive_failing(
        "omcp_testkey",
        _MiddlewareSession(api_key=_api_key_row()),
        RuntimeError("downstream exploded"),
    )
    assert seen["during"] == ("api_key", "nightly sync", "omcp_a1b2c3")
    assert seen["raised"] is RuntimeError
    assert seen["after"] is None


def test_the_actor_is_reset_after_an_exception_on_the_oauth_branch():
    seen = _drive_failing(
        "oauth-token",
        _MiddlewareSession(
            oauth_token=_oauth_token_row(), client_row=(None, "Claude Desktop")
        ),
        RuntimeError("downstream exploded"),
    )
    assert seen["during"] == ("oauth", "Claude Desktop", "client-abc")
    assert seen["raised"] is RuntimeError
    assert seen["after"] is None


def test_the_actor_is_reset_after_a_cancellation():
    """A disconnected client cancels the task. `CancelledError` is a
    `BaseException`, so an `except Exception` would not unwind it — the reset
    lives in `finally` for exactly that reason, and a leaked actor would label
    the next call in this worker with the wrong credential."""
    seen = _drive_failing(
        "omcp_testkey", _MiddlewareSession(api_key=_api_key_row()), asyncio.CancelledError()
    )
    assert seen["during"] == ("api_key", "nightly sync", "omcp_a1b2c3")
    assert seen["raised"] is asyncio.CancelledError
    assert seen["after"] is None


# --------------------------------------------------------------------------
# 7. the label costs no extra query
# --------------------------------------------------------------------------


class _CountingSession(_MiddlewareSession):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.statements = []

    async def execute(self, stmt, *a, **kw):
        self.statements.append(str(stmt))
        return await super().execute(stmt, *a, **kw)


def test_an_ownerless_oauth_request_issues_exactly_one_statement():
    """The label must not cost a round trip on the path that had none.

    A single-user (ownerless) token skips the `User.is_active` check, the
    cross-user check and the vault warm, so the token lookup is the only
    statement — and it now carries `client_name` with it. A second
    `oauth_clients` query here would be a new per-request round trip on the
    hottest path in the server.
    """
    session = _CountingSession(
        oauth_token=_oauth_token_row(None), client_row=(None, "Claude Desktop")
    )
    captured = _drive("oauth-token", session)

    assert captured["status"] == 200
    assert captured["actor"] == ("oauth", "Claude Desktop", "client-abc")
    assert len(session.statements) == 1, session.statements


def test_an_owned_oauth_request_issues_no_second_client_query():
    """The owned path does more work, but none of it is a client lookup."""
    session = _CountingSession(
        oauth_token=_oauth_token_row(42), client_row=(42, "Claude Desktop")
    )
    captured = _drive("oauth-token", session, multi_user=True)

    assert captured["status"] == 200
    assert captured["actor"] == ("oauth", "Claude Desktop", "client-abc")
    client_only = [
        s for s in session.statements
        if "FROM oauth_clients" in s and "FROM oauth_tokens" not in s
    ]
    assert client_only == [], client_only


# --------------------------------------------------------------------------
# 8. the usage page stays scoped to its viewer
# --------------------------------------------------------------------------


class _EmptyRows:
    def fetchall(self):
        return []


class _ScopeProbeSession:
    """Records every statement `usage_page` issues, with its bound params."""

    def __init__(self):
        self.calls = []

    async def execute(self, stmt, params=None):
        self.calls.append((str(stmt), params))
        return _EmptyRows()


def _usage_page_statements(user):
    import src.control_panel.routes as panel

    mp = pytest.MonkeyPatch()
    session = _ScopeProbeSession()
    try:
        mp.setattr(
            panel.templates, "TemplateResponse", lambda *a, **kw: SimpleNamespace()
        )
        asyncio.run(
            panel.usage_page(
                request=SimpleNamespace(session={}), session=session, user=user
            )
        )
    finally:
        mp.undo()
    return session.calls


def test_a_non_admin_usage_page_is_filtered_to_their_own_rows():
    """Attribution that survives a delete must not become attribution another
    user can read. The denormalised columns changed what the SELECT list
    carries; the WHERE clause has to be exactly as scoped as it was — and #162
    added three more statements to the page, every one of which has to be
    scoped the same way."""
    calls = _usage_page_statements(
        SimpleNamespace(id=42, is_admin=False, username="bob")
    )

    assert calls, "usage_page issued no statements"
    log_reads = [(sql, params) for sql, params in calls if "usage_logs" in sql]
    assert log_reads, "usage_page read no usage_logs"
    for sql, params in log_reads:
        assert "user_id = :scope_uid" in sql or "user_id = :uid" in sql, sql
        assert 42 in (params or {}).values(), (sql, params)

    # The key selector is scoped too: a filter list naming another user's keys
    # would leak the credential names this page exists to attribute.
    key_reads = [(sql, params) for sql, params in calls if "FROM api_keys" in sql]
    assert key_reads
    for sql, params in key_reads:
        assert "user_id = :uid" in sql, sql
        assert params == {"uid": 42}

    # And the user selector is not offered at all, so there is nothing to
    # widen the scope *with*.
    assert not [sql for sql, _ in calls if "FROM users" in sql]


def test_an_admin_usage_page_is_unfiltered():
    calls = _usage_page_statements(
        SimpleNamespace(id=1, is_admin=True, username="admin")
    )

    assert calls
    for sql, params in calls:
        assert ":scope_uid" not in sql, sql
        assert "scope_uid" not in (params or {}), (sql, params)


def test_a_non_admin_cannot_introduce_a_user_filter_from_the_query_string():
    """The owner scope and the `user=` filter are different things: the scope
    is the viewer's tenancy and is applied whatever the URL says, and the
    filter is only an admin's choice of whose rows to read. A hand-edited
    `?user=1` from a regular user must change nothing."""
    import src.control_panel.routes as panel

    mp = pytest.MonkeyPatch()
    session = _ScopeProbeSession()
    try:
        mp.setattr(
            panel.templates, "TemplateResponse", lambda *a, **kw: SimpleNamespace()
        )
        asyncio.run(
            panel.usage_page(
                request=SimpleNamespace(session={}),
                session=session,
                user=SimpleNamespace(id=42, is_admin=False, username="bob"),
                user_filter="1",
            )
        )
    finally:
        mp.undo()

    for sql, params in session.calls:
        assert "filter_uid" not in sql, sql
        assert "filter_uid" not in (params or {}), (sql, params)
        if "usage_logs" in sql:
            assert 42 in (params or {}).values(), (sql, params)
