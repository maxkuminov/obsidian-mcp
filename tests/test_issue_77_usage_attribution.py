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
            return _RowsResult([self.oauth_token] if self.oauth_token else [])
        if "FROM oauth_clients" in sql:
            return _RowsResult([self.client_row] if self.client_row else [])
        if "vault_path" in sql:
            return _RowsResult([])
        return _ScalarResult(True)


def _drive(bearer: str, session: _MiddlewareSession) -> dict:
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
            mp.setattr(mcp_auth.settings, "multi_user_mode", False, raising=False)
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


def test_a_labelled_row_with_no_name_still_names_its_kind():
    """`actor_kind` set with an empty label must not fall through to the join
    and must not render as an unattributed row."""
    name, detail = _usage_actor(_row(actor_kind="oauth", actor_ref="client-abc"))
    assert name and "client" in name.lower()
    assert detail == "OAuth · client-abc"


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
