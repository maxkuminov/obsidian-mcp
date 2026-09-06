"""The dashboard's currency counts and the Danger zone's generation lock
(#201, #206).

Two things ship here and they are not the same feature; what joins them is
that both are ways the panel can tell an operator something untrue about the
index.

**Coverage is not currency (#201).** `stats.embedding_pct` counts notes holding
*at least one vector row* — "is this note represented at all". A vault where
every note was embedded last week and every note has been edited since reads
100% on that bar, correctly, and is entirely superseded. So the page gains a
second number answering the second question, and the bar is deliberately *not*
redefined: every coverage figure an operator has ever read on this page meant
the looser question, and quietly changing it would rewrite that history. A
provider outage and a tenant repeatedly stopped at its per-pass embedding
budget both surface here and nowhere else on this page.

The predicate behind the new count is written **once**
(`routes._vectors_not_current`) and called from the dashboard and from
`/settings/reset-embeddings/progress`, because a second copy is exactly how the
page and the poller come to disagree about what "pending" means. The two
callers differ in *scope* on purpose and in nothing else: the dashboard scopes
by `_scope_user_id(user)` like the coverage numbers beside it, the poller is
admin-only and whole-database. Copying the poller's unscoped query onto the
dashboard would show a regular user another tenant's backlog as their own.

**The reset paths take the generation lock first, and record the fingerprint in
the same transaction (#206).** `index_pass_lock` is process-local — it stops
*this* container's pass. The reset workflow is deliberately a one-off container
that runs while the service is up, so the process that must be excluded is a
different one, and only a database-level advisory lock can do it. And the
fingerprint write is not instrumentation: it is the claim a later startup
refuses on, so a failed record rolls the wipe back and reaches the operator as
a flash error instead of leaving the stored value naming the *previous*
configuration over rows about to be built under the new one.

Hermetic: no PostgreSQL, no container, no provider. The predicate's *semantics*
are exercised against a real SQL engine (sqlite in memory, where SQLAlchemy
renders `IS DISTINCT FROM` as the NULL-safe `IS NOT`), which is what makes the
`!=`-regression case a real failure rather than a string comparison.
"""
import asyncio
import os
import sqlite3
import tempfile

import pytest

os.environ.setdefault("SECRET_KEY", "test")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("VAULT_PATH", "/tmp/test-vault")
os.chdir(tempfile.gettempdir())

from fastapi.responses import JSONResponse  # noqa: E402
from sqlalchemy import func, select  # noqa: E402
from sqlalchemy.dialects import postgresql  # noqa: E402
from sqlalchemy.dialects import sqlite as sqlite_dialect  # noqa: E402
from starlette.templating import Jinja2Templates  # noqa: E402

from src.control_panel import routes  # noqa: E402
from src.control_panel.flash import FLASH_SESSION_KEY  # noqa: E402
from src.models.db import NoteMetadata  # noqa: E402
from src.services.index_state import (  # noqa: E402
    KEY_EMBEDDING_FINGERPRINT,
    embedding_fingerprint,
)

TEMPLATES_DIR = os.path.join(os.path.dirname(routes.__file__), "templates")


# ==========================================================================
# 1. The predicate, against a real SQL engine
# ==========================================================================

#: (user_id, content_hash, embedded_content_hash, chunks_truncated)
#:
#: The states a row can be in, under two owners. `NULL` is the state a
#: never-embedded note is in and also the state a *move* or a grammar bump
#: leaves behind, which is why it must count as not-current rather than as
#: unknown.
_ROWS = [
    (1, "aaa", "aaa", 0),   # user 1: current
    (1, "bbb", "old", 0),   # user 1: edited since it was embedded
    (1, "ccc", None, 0),    # user 1: never embedded (or invalidated)
    (1, "ddd", "ddd", 1),   # user 1: current, but truncated at the chunk cap
    (2, "eee", "old", 0),   # user 2: another tenant's backlog
    (2, "fff", None, 0),    # user 2: another tenant's backlog
    (2, "ggg", "ggg", 1),   # user 2: another tenant's truncation
]


def _conn(rows=_ROWS):
    """An in-memory `notes_metadata` holding `rows`."""
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE notes_metadata ("
        " id INTEGER PRIMARY KEY,"
        " user_id INTEGER,"
        " content_hash TEXT NOT NULL,"
        " embedded_content_hash TEXT,"
        " chunks_truncated INTEGER NOT NULL DEFAULT 0)"
    )
    conn.executemany(
        "INSERT INTO notes_metadata"
        " (user_id, content_hash, embedded_content_hash, chunks_truncated)"
        " VALUES (?, ?, ?, ?)",
        rows,
    )
    return conn


def _run(conn, stmt) -> int:
    sql = str(
        stmt.compile(
            dialect=sqlite_dialect.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    return conn.execute(sql).fetchone()[0]


def _pending_stmt(uid=None):
    """Exactly the query `dashboard()` builds."""
    q = select(func.count(NoteMetadata.id)).where(routes._vectors_not_current())
    if uid is not None:
        q = q.where(NoteMetadata.user_id == uid)
    return q


def test_the_predicate_is_null_safe_and_says_so_in_sql():
    """`IS DISTINCT FROM`, not `!=`, and the explicit `IS NULL` arm beside it."""
    rendered = str(_pending_stmt().compile(dialect=postgresql.dialect()))
    assert (
        "notes_metadata.embedded_content_hash IS NULL "
        "OR notes_metadata.embedded_content_hash IS DISTINCT FROM "
        "notes_metadata.content_hash" in rendered
    ), rendered


def test_a_naive_inequality_would_lose_every_never_embedded_note():
    """The regression this predicate exists to prevent, demonstrated.

    Under `!=`, a NULL `embedded_content_hash` yields NULL and a `WHERE` reads
    NULL as false — so every never-embedded note would count as *current*,
    which is the exact inversion of what the number is for.
    """
    conn = _conn()
    correct = _run(conn, _pending_stmt())
    naive = conn.execute(
        "SELECT count(id) FROM notes_metadata "
        "WHERE embedded_content_hash != content_hash"
    ).fetchone()[0]
    assert correct == 4, "2 edited + 2 never embedded, across both tenants"
    assert naive == 2, "the naive form silently drops the NULL rows"


def test_a_fully_covered_fully_stale_vault_is_wholly_pending():
    """Scenario: every note has vectors and every note has been edited since.

    Coverage — which counts vector rows, not hashes — reads 100%. The pending
    count equals the note count. Both are true and they are different facts.
    """
    rows = [(1, f"new{i}", "old", 0) for i in range(9)]
    conn = _conn(rows)
    total = conn.execute("SELECT count(id) FROM notes_metadata").fetchone()[0]
    assert _run(conn, _pending_stmt(uid=1)) == total == 9


def test_a_healthy_vault_is_zero_pending():
    rows = [(1, f"h{i}", f"h{i}", 0) for i in range(5)]
    assert _run(_conn(rows), _pending_stmt(uid=1)) == 0


def test_the_scoped_count_excludes_another_tenants_backlog():
    conn = _conn()
    assert _run(conn, _pending_stmt(uid=1)) == 2, "user 1's own two"
    assert _run(conn, _pending_stmt(uid=2)) == 2, "user 2's own two"
    assert _run(conn, _pending_stmt()) == 4, "the admin/poller view is the sum"


def test_the_truncated_count_is_scoped_the_same_way():
    conn = _conn()

    def truncated(uid=None):
        q = select(func.count(NoteMetadata.id)).where(
            NoteMetadata.chunks_truncated.is_(True)
        )
        if uid is not None:
            q = q.where(NoteMetadata.user_id == uid)
        return _run(conn, q)

    assert truncated(uid=1) == 1, "a capped note is counted"
    assert truncated(uid=2) == 1
    assert truncated() == 2


def test_pending_and_the_pollers_old_embedded_query_are_exact_complements():
    """What makes the progress endpoint's numbers identical after the switch to
    the shared predicate: `pending` and the query that used to produce
    `embedded` partition the table with no overlap and no gap."""
    conn = _conn()
    total = conn.execute("SELECT count(id) FROM notes_metadata").fetchone()[0]
    old_embedded = conn.execute(
        "SELECT count(*) FROM notes_metadata "
        "WHERE embedded_content_hash IS NOT NULL "
        "AND embedded_content_hash = content_hash"
    ).fetchone()[0]
    assert _run(conn, _pending_stmt()) + old_embedded == total


# ==========================================================================
# 2. The dashboard route wires both counts in, scoped
# ==========================================================================


class _Result:
    def __init__(self, value=None, rows=None):
        self._value = value
        self._rows = rows if rows is not None else []

    def scalar(self):
        return self._value

    def scalar_one_or_none(self):
        return self._value

    def fetchall(self):
        return self._rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _DashboardSession:
    """Answers `dashboard()`'s aggregates by looking at the SQL it is handed.

    Dispatching on the statement rather than on call order is deliberate: a
    later refactor that reorders the queries must not silently start feeding
    the pending count the note count.
    """

    def __init__(self, *, notes, embedded, pending, truncated):
        self.notes = notes
        self.embedded = embedded
        self.pending = pending
        self.truncated = truncated
        self.statements: list[str] = []

    async def execute(self, clause, params=None, *_a, **_k):
        sql = str(clause)
        self.statements.append(sql)
        if "IS DISTINCT FROM" in sql:
            return _Result(self.pending)
        if "chunks_truncated" in sql:
            return _Result(self.truncated)
        if "note_embeddings.note_id" in sql:
            return _Result(self.embedded)
        if "api_keys" in sql:
            return _Result(1)
        if "max(notes_metadata.indexed_at)" in sql:
            return _Result(None)
        if "notes_metadata.indexed_at >=" in sql:
            return _Result(0)
        if "count(notes_metadata.id)" in sql:
            return _Result(self.notes)
        return _Result(0)

    async def rollback(self):
        pass


class _User:
    def __init__(self, uid, is_admin):
        self.id = uid
        self.is_admin = is_admin
        self.username = "u"


def _dashboard_stats(monkeypatch, *, user, **counts) -> tuple[dict, list[str]]:
    captured: dict = {}

    def _template_response(request, name, context):
        captured.update(context)
        return context

    monkeypatch.setattr(
        routes, "_panel_context", lambda request, user, extra=None: dict(extra or {})
    )
    monkeypatch.setattr(routes.templates, "TemplateResponse", _template_response)

    async def _graph(*_a, **_k):
        return {}

    async def _health(*_a, **_k):
        return {}

    monkeypatch.setattr(routes, "_graph_stats", _graph)
    monkeypatch.setattr(routes, "_health_strip_or_degraded", _health)

    session = _DashboardSession(**counts)
    asyncio.run(routes.dashboard(request=object(), session=session, user=user))
    return captured["stats"], session.statements


def test_dashboard_reports_pending_and_truncated_beside_coverage(monkeypatch):
    stats, _ = _dashboard_stats(
        monkeypatch,
        user=_User(1, True),
        notes=12,
        embedded=12,
        pending=12,
        truncated=3,
    )
    assert stats["notes_pending"] == 12
    assert stats["notes_chunks_truncated"] == 3
    # The bar is untouched: it still means "has at least one vector row".
    assert stats["notes_with_embeddings"] == 12
    assert stats["embedding_pct"] == 100, (
        "a fully covered, wholly superseded vault still reads 100% coverage"
    )


def test_a_zero_pending_count_is_still_put_in_the_context(monkeypatch):
    stats, _ = _dashboard_stats(
        monkeypatch, user=_User(1, True), notes=5, embedded=5, pending=0, truncated=0
    )
    assert stats["notes_pending"] == 0
    assert stats["notes_chunks_truncated"] == 0


def test_the_new_counts_are_scoped_for_a_non_admin(monkeypatch):
    _, statements = _dashboard_stats(
        monkeypatch, user=_User(7, False), notes=4, embedded=4, pending=2, truncated=1
    )
    pending_sql = [s for s in statements if "IS DISTINCT FROM" in s]
    truncated_sql = [s for s in statements if "chunks_truncated" in s]
    assert len(pending_sql) == 1 and len(truncated_sql) == 1
    for sql in pending_sql + truncated_sql:
        assert "notes_metadata.user_id" in sql, (
            "an unscoped copy of the poller's query would show this user the "
            "whole database's backlog as their own: " + sql
        )


def test_an_admins_counts_are_unscoped(monkeypatch):
    _, statements = _dashboard_stats(
        monkeypatch, user=_User(1, True), notes=4, embedded=4, pending=2, truncated=1
    )
    pending_sql = next(s for s in statements if "IS DISTINCT FROM" in s)
    assert "notes_metadata.user_id" not in pending_sql


# ==========================================================================
# 3. The template renders both, zero included
# ==========================================================================


def _render(stats_overrides=None) -> str:
    stats = {
        "notes_indexed": 12,
        "notes_with_embeddings": 12,
        "embedding_pct": 100,
        "notes_pending": 12,
        "notes_chunks_truncated": 0,
        "active_keys": 1,
        "requests_today": 3,
    }
    stats.update(stats_overrides or {})
    context = {
        "active": "dashboard",
        "is_admin": True,
        "multi_user_mode": False,
        "username": "max",
        "csrf_token": "csrf",
        "stats": stats,
        "recent_usage": [],
        "reindexed_24h": 0,
        "last_indexed_iso": None,
        "last_indexed_rel": None,
        "last_run_iso": None,
        "last_run_rel": None,
        "last_run_ok": True,
        "index_interval": 300,
        "graph": {},
        "graph_backfill_running": False,
        "health": {"show_ops": True, "last_run": None, "stale_after_days": 8},
    }
    templates = Jinja2Templates(directory=TEMPLATES_DIR)
    return templates.TemplateResponse(
        request=None, name="dashboard.html", context=context
    ).body.decode()


def test_the_page_shows_full_coverage_and_a_full_backlog_at_once():
    html = _render()
    assert "100%" in html
    assert "12 not current" in html, (
        "the label has to distinguish the figure from the coverage number "
        "immediately above it"
    )


def test_zero_is_rendered_rather_than_omitted():
    """An absent count is not evidence of absence: an operator cannot tell
    "no backlog" from "this build does not report one"."""
    html = _render({"notes_pending": 0, "notes_chunks_truncated": 0})
    assert "0 not current" in html
    assert "0 truncated" in html


def test_a_capped_note_is_reported():
    html = _render({"notes_chunks_truncated": 4})
    assert "4 truncated" in html
    assert "var(--warning)" in html


def test_the_new_row_carries_no_colour_literal():
    """`checks/token_coverage.py` and the literal sweep stay green because the
    row reuses `--text-3` and `--warning` and defines nothing of its own."""
    html = _render({"notes_chunks_truncated": 4})
    start = html.index("not current")
    row = html[max(0, start - 500) : start + 500]
    assert "#" not in row, f"a colour literal beside the new counts: {row!r}"


# ==========================================================================
# 4. The progress endpoint: same predicate, same JSON
# ==========================================================================


class _ProgressSession:
    def __init__(self, total, pending):
        self.total = total
        self.pending = pending
        self.statements: list[str] = []

    async def execute(self, clause, params=None, *_a, **_k):
        sql = str(clause)
        self.statements.append(sql)
        if "IS DISTINCT FROM" in sql:
            return _Result(self.pending)
        return _Result(self.total)


def test_the_progress_endpoint_uses_the_shared_predicate():
    session = _ProgressSession(total=100, pending=40)
    asyncio.run(routes.reset_progress(session=session, user=object()))
    assert any("IS DISTINCT FROM" in s for s in session.statements), (
        "the poller and the page must not hold two copies of 'pending'"
    )


def test_the_progress_endpoint_stays_unscoped():
    session = _ProgressSession(total=100, pending=40)
    asyncio.run(routes.reset_progress(session=session, user=object()))
    pending_sql = next(s for s in session.statements if "IS DISTINCT FROM" in s)
    assert "user_id" not in pending_sql, (
        "it is admin-only and answers about the whole database, by design"
    )


def test_the_progress_response_is_byte_compatible():
    """Same four keys, same order, same types — the dashboard's poller reads
    this and nothing about it may have moved."""
    session = _ProgressSession(total=100, pending=40)
    routes.indexer_paused = False
    resp = asyncio.run(routes.reset_progress(session=session, user=object()))
    expected = JSONResponse(
        {"paused": False, "total": 100, "embedded": 60, "pending": 40}
    ).body
    assert resp.body == expected


def test_the_progress_response_cannot_report_more_pending_than_total():
    """The old `max(0, total - embedded)` floor is kept in both directions: the
    two counts come from two statements, and a concurrent write between them
    must not produce a negative `embedded`."""
    session = _ProgressSession(total=3, pending=9)
    resp = asyncio.run(routes.reset_progress(session=session, user=object()))
    body = resp.body.replace(b" ", b"")
    assert b'"embedded":0' in body
    assert b'"pending":3' in body


# ==========================================================================
# 5. The Danger zone: the lock first, the fingerprint in the same transaction
# ==========================================================================


class _FreshSession:
    def __init__(self):
        self.calls: list[tuple[str, dict | None]] = []
        self.committed = False
        self.rolled_back = False
        self.closed = False

    async def execute(self, clause, params=None, *_a, **_k):
        self.calls.append((str(clause), params))
        return _Result(None)

    @property
    def executed(self) -> list[str]:
        return [sql for sql, _ in self.calls]

    async def commit(self):
        self.committed = True

    async def rollback(self):
        self.rolled_back = True

    async def close(self):
        self.closed = True

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return None


class _Req:
    def __init__(self, accept=""):
        self.headers = {"accept": accept}
        self.session: dict = {}


@pytest.fixture
def danger(monkeypatch):
    """A reset that touches no database, spawns no reindex, and holds a real
    (but uncontended) pass lock."""
    from src.services import indexer

    monkeypatch.setattr(indexer, "index_pass_lock", asyncio.Lock())
    monkeypatch.setattr(routes, "_spawn", lambda coro: coro.close())
    monkeypatch.setattr(routes.settings, "embedding_dimensions", 8, raising=False)
    fresh = _FreshSession()
    monkeypatch.setattr(routes, "async_session", lambda: fresh)
    return fresh


def _index(haystack: list[str], needle: str) -> int:
    for i, sql in enumerate(haystack):
        if needle in sql:
            return i
    raise AssertionError(f"{needle!r} never executed; got {haystack}")


def test_reset_takes_the_generation_lock_as_its_first_statement(danger):
    asyncio.run(
        routes.reset_embeddings(
            request=_Req("application/json"), session=_FreshSession(), user=object()
        )
    )
    lock_at = _index(danger.executed, "pg_advisory_xact_lock")
    # The ordering rule is "advisory before any row or **table** lock", and the
    # only statements ahead of it are the `SET LOCAL`s that lift the engine's
    # 60 s `statement_timeout` off the wait. A `SET LOCAL` takes neither kind
    # of lock — it is a session-variable assignment the lock graph cannot see —
    # so it does not violate the rule, and without it the wait for an in-flight
    # pass is cancelled after a minute instead of served.
    assert all(
        "statement_timeout" in sql for sql in danger.executed[:lock_at]
    ), (
        "something other than the timeout raise ran before the generation "
        f"lock: {danger.executed[:lock_at]!r}"
    )
    assert danger.executed[lock_at - 1].endswith("statement_timeout = 0"), (
        "the timeout raise must be the statement immediately before the "
        f"acquisition; got {danger.executed[lock_at - 1]!r}"
    )
    assert lock_at < _index(danger.executed, "statement_timeout = '5min'"), (
        "a 5-minute statement timeout must not abort a legitimate lock wait"
    )
    assert lock_at < _index(danger.executed, "DROP INDEX")


def test_reset_records_the_fingerprint_in_the_same_transaction_as_the_wipe(danger):
    asyncio.run(
        routes.reset_embeddings(
            request=_Req("application/json"), session=_FreshSession(), user=object()
        )
    )
    at = _index(danger.executed, "INSERT INTO indexer_state")
    assert at > _index(danger.executed, "embedded_content_hash = NULL"), (
        "the fingerprint describes the rows the wipe is about to license"
    )
    assert danger.committed, "one transaction, one commit"
    params = danger.calls[at][1]
    assert params["key"] == KEY_EMBEDDING_FINGERPRINT
    assert params["value"] == embedding_fingerprint()


def test_reset_rolls_back_and_flashes_when_the_fingerprint_write_fails(
    danger, monkeypatch
):
    """D7d: this write is not instrumentation. Swallowing it would leave the
    stored value naming the previous configuration over rows about to be built
    under the new one, and every later startup silent about it."""

    async def _boom(*_a, **_k):
        raise RuntimeError("indexer_state is unwritable")

    monkeypatch.setattr(routes, "set_state", _boom)
    spawned: list[int] = []
    monkeypatch.setattr(
        routes, "_spawn", lambda coro: (spawned.append(1), coro.close())
    )

    req = _Req("application/json")
    resp = asyncio.run(
        routes.reset_embeddings(request=req, session=_FreshSession(), user=object())
    )

    assert danger.rolled_back and not danger.committed, (
        "the wipe must not survive a fingerprint it could not record"
    )
    assert not spawned, "no reindex is spawned for a reset that did not happen"
    assert resp.status_code == 500
    assert b'"status":"error"' in resp.body.replace(b" ", b"")
    assert req.session[FLASH_SESSION_KEY]["kind"] == "err"
    assert "nothing was changed" in req.session[FLASH_SESSION_KEY]["message"]


def test_reset_flashes_and_redirects_for_a_browser(danger, monkeypatch):
    async def _boom(*_a, **_k):
        raise RuntimeError("nope")

    monkeypatch.setattr(routes, "set_state", _boom)
    req = _Req("text/html")
    resp = asyncio.run(
        routes.reset_embeddings(request=req, session=_FreshSession(), user=object())
    )
    assert resp.status_code == 303
    assert req.session[FLASH_SESSION_KEY]["kind"] == "err"


def test_legacy_reembed_takes_the_lock_and_records_the_fingerprint(danger):
    token = routes._reembed_serializer().dumps("x")
    asyncio.run(
        routes.trigger_reembed(
            token=token, request=_Req(), session=_FreshSession(), user=object()
        )
    )
    lock_at = _index(danger.executed, "pg_advisory_xact_lock")
    # Only the `statement_timeout` raise precedes it; see the reset test above.
    assert all(
        "statement_timeout" in sql for sql in danger.executed[:lock_at]
    ), danger.executed[:lock_at]
    assert lock_at < _index(danger.executed, "DELETE FROM note_embeddings")
    at = _index(danger.executed, "INSERT INTO indexer_state")
    assert danger.calls[at][1]["value"] == embedding_fingerprint()
    assert danger.committed


def test_legacy_reembed_rolls_back_on_a_failed_fingerprint(danger, monkeypatch):
    async def _boom(*_a, **_k):
        raise RuntimeError("nope")

    monkeypatch.setattr(routes, "set_state", _boom)
    spawned: list[int] = []
    monkeypatch.setattr(
        routes, "_spawn", lambda coro: (spawned.append(1), coro.close())
    )

    token = routes._reembed_serializer().dumps("x")
    req = _Req()
    resp = asyncio.run(
        routes.trigger_reembed(
            token=token, request=req, session=_FreshSession(), user=object()
        )
    )
    assert danger.rolled_back and not danger.committed
    assert not spawned
    assert resp.status_code == 303
    assert req.session[FLASH_SESSION_KEY]["kind"] == "err"


def test_the_reset_path_still_gates_hnsw_creation_on_the_dimension(danger, monkeypatch):
    """Unchanged, and confirmed rather than assumed: pgvector refuses the index
    above 2000 dims, so an unconditional CREATE aborts the whole reset and
    leaves a wiped column behind (#6)."""
    monkeypatch.setattr(routes.settings, "embedding_dimensions", 3072, raising=False)
    asyncio.run(
        routes.reset_embeddings(
            request=_Req("application/json"), session=_FreshSession(), user=object()
        )
    )
    joined = " | ".join(danger.executed)
    assert "USING hnsw" not in joined
    assert "ALTER COLUMN embedding TYPE vector(3072)" in joined
    assert "CREATE INDEX" not in joined
