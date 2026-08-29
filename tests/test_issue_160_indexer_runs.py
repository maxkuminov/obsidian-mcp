"""The indexer's persistent pass record (#160, migration 019).

Hermetic: no database. The recorder's *statements* are captured through a fake
session, which is enough to pin the contract that matters here — one row per
pass, written in a `finally` so a raising pass records its error, the trigger
threaded from the caller, and the prune riding in the same transaction as the
insert. The behaviour of those statements against a real PostgreSQL (the prune
actually leaving 500 rows, the trigger CHECK, the FK) is
`tests/integration/test_issue_160_performance_pg.py`.
"""
import asyncio

import pytest

import src.services.indexer as indexer


# --------------------------------------------------------------------------
# A session that records what was executed on it.
# --------------------------------------------------------------------------


class _FakeSession:
    def __init__(self, log):
        self.log = log

    async def execute(self, statement, params=None):
        self.log.append(("execute", statement, params))
        return None

    async def commit(self):
        self.log.append(("commit", None, None))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _capture(monkeypatch):
    """Replace the indexer's session factory; return the statement log."""
    log: list = []
    monkeypatch.setattr(indexer, "async_session", lambda: _FakeSession(log))
    return log


def _inserts(log):
    """The parameter dicts of every INSERT executed, in order."""
    out = []
    for kind, statement, _params in log:
        if kind != "execute":
            continue
        compiled = statement.compile()
        if compiled.statement.is_insert:
            out.append(dict(compiled.params))
    return out


def _deletes(log):
    out = []
    for kind, statement, params in log:
        if kind == "execute" and "DELETE FROM indexer_runs" in str(statement):
            out.append((str(statement), params))
    return out


# --------------------------------------------------------------------------
# 1. A pass that succeeds records a row.
# --------------------------------------------------------------------------


def test_a_completed_pass_records_one_row(monkeypatch):
    log = _capture(monkeypatch)

    async def _run():
        async with indexer.record_indexer_run("scheduled", 7) as stats:
            stats.record_index((120, 4))
            stats.record_embedded(3)

    asyncio.run(_run())

    rows = _inserts(log)
    assert len(rows) == 1, "a pass must record exactly one row"
    row = rows[0]
    assert row["trigger"] == "scheduled"
    assert row["user_id"] == 7
    assert row["notes_scanned"] == 120
    assert row["notes_indexed"] == 4
    assert row["notes_embedded"] == 3
    assert row["error"] is None
    assert row["started_at"] is not None and row["finished_at"] is not None
    assert row["finished_at"] >= row["started_at"]


def test_the_recorder_tolerates_a_stage_that_returns_nothing(monkeypatch):
    """`index_vault`/`embed_vault` are replaced by bare no-op coroutines all
    over the suite. Instrumentation that insisted on a tuple would turn every
    one of those into a failure about recording rather than about indexing."""
    log = _capture(monkeypatch)

    async def _run():
        async with indexer.record_indexer_run("manual") as stats:
            stats.record_index(None)
            stats.record_embedded(None)

    asyncio.run(_run())

    row = _inserts(log)[0]
    assert (row["notes_scanned"], row["notes_indexed"], row["notes_embedded"]) == (0, 0, 0)
    assert row["user_id"] is None


# --------------------------------------------------------------------------
# 2. A pass that raises still records — that is the whole point.
# --------------------------------------------------------------------------


def test_a_raising_pass_records_its_error(monkeypatch):
    log = _capture(monkeypatch)

    async def _run():
        async with indexer.record_indexer_run("scheduled", None) as stats:
            stats.record_index((9, 1))
            raise RuntimeError("the vault went away")

    with pytest.raises(RuntimeError):
        asyncio.run(_run())

    rows = _inserts(log)
    assert len(rows) == 1, "a failed pass must still be recorded"
    row = rows[0]
    assert "the vault went away" in row["error"]
    assert "RuntimeError" in row["error"]
    assert row["finished_at"] is not None
    # The work it managed before dying is still reported.
    assert row["notes_scanned"] == 9


def test_a_swallowed_stage_failure_reaches_the_row(monkeypatch):
    """`_index_pass_once` deliberately swallows per-stage exceptions so one
    user's broken vault cannot stop the others. A row that came out clean
    anyway would reproduce the "reports fine, is not" defect one layer down."""
    log = _capture(monkeypatch)

    async def _ok(*_a, **_k):
        return (5, 2)

    async def _boom(*_a, **_k):
        raise RuntimeError("ollama is down")

    monkeypatch.setattr(indexer, "index_vault", _ok)
    monkeypatch.setattr(indexer, "embed_vault", _boom)

    assert asyncio.run(indexer._index_pass_once(3)) is False

    row = _inserts(log)[0]
    assert row["trigger"] == "scheduled"
    assert row["user_id"] == 3
    assert "ollama is down" in row["error"]
    assert row["notes_scanned"] == 5


def test_shutdown_is_not_recorded_as_a_pass(monkeypatch):
    """`CancelledError` is lifespan shutdown, not a failed pass — and awaiting
    a database write inside a cancelled task's `finally` is not something to do
    on the way out. Same treatment `_record_index_run` gives it."""
    log = _capture(monkeypatch)

    async def _run():
        async with indexer.record_indexer_run("scheduled", None):
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(_run())

    assert _inserts(log) == []


def test_a_skipped_pass_records_nothing(monkeypatch):
    """The link backfill's "this scope already has links" probe fires on every
    startup after the first. A row per startup for a pass that did no work is
    noise, and noise in a 500-row history evicts what an operator came for."""
    log = _capture(monkeypatch)

    async def _run():
        async with indexer.record_indexer_run("backfill", 1) as stats:
            stats.skipped = True

    asyncio.run(_run())
    assert _inserts(log) == []


# --------------------------------------------------------------------------
# 3. Pruning rides in the same transaction as the insert.
# --------------------------------------------------------------------------


def test_the_insert_and_the_prune_share_one_transaction(monkeypatch):
    log = _capture(monkeypatch)

    async def _run():
        async with indexer.record_indexer_run("startup", None):
            pass

    asyncio.run(_run())

    kinds = [entry[0] for entry in log]
    assert kinds == ["execute", "execute", "commit"], (
        "the insert and the prune must be the only statements, and must be "
        f"followed by exactly one commit; got {kinds}"
    )

    deletes = _deletes(log)
    assert len(deletes) == 1
    sql, params = deletes[0]
    assert params == {"keep": indexer.MAX_INDEXER_RUNS}
    assert indexer.MAX_INDEXER_RUNS == 500
    # Newest-first by start time: passes are inserted at their *finish*, so two
    # that started minutes apart can land in the other order.
    assert "ORDER BY started_at DESC" in sql
    assert "LIMIT :keep" in sql


def test_a_failing_write_never_fails_the_pass(monkeypatch):
    """Recording is instrumentation. A pass that did its work must not be
    reported as failed because the recorder could not reach the database —
    and in a `finally`, a raise would also replace the exception the operator
    actually needs to see."""

    class _BrokenSession(_FakeSession):
        async def execute(self, statement, params=None):
            raise OSError("connection refused")

    monkeypatch.setattr(indexer, "async_session", lambda: _BrokenSession([]))

    async def _run():
        async with indexer.record_indexer_run("scheduled", None):
            return "done"

    asyncio.run(_run())  # must not raise


# --------------------------------------------------------------------------
# 4. The startup pass records under the `startup` trigger.
# --------------------------------------------------------------------------


def test_the_startup_pass_records_a_startup_row(monkeypatch):
    log = _capture(monkeypatch)
    monkeypatch.setattr(indexer.settings, "multi_user_mode", False)
    monkeypatch.setattr(indexer, "_is_paused", lambda: False)

    async def _noop(*_a, **_k):
        return None

    async def _index(*_a, **_k):
        return (2577, 12)

    async def _embed(*_a, **_k):
        return 12

    async def _stop(*_a, **_k):
        # Cancel while the loop is asleep, i.e. before any tick can record.
        raise asyncio.CancelledError

    monkeypatch.setattr(indexer, "index_vault", _index)
    monkeypatch.setattr(indexer, "embed_vault", _embed)
    monkeypatch.setattr(indexer, "link_backfill_pass", _noop)
    monkeypatch.setattr(indexer.asyncio, "sleep", _stop)

    try:
        asyncio.run(indexer.run_indexer_loop())
    except asyncio.CancelledError:
        pass

    rows = _inserts(log)
    assert [r["trigger"] for r in rows] == ["startup"]
    assert rows[0]["notes_scanned"] == 2577
    assert rows[0]["notes_embedded"] == 12
    assert rows[0]["user_id"] is None


def test_a_periodic_tick_records_a_scheduled_row(monkeypatch):
    log = _capture(monkeypatch)
    monkeypatch.setattr(indexer.settings, "multi_user_mode", False)
    monkeypatch.setattr(indexer.settings, "index_interval_seconds", 0)
    monkeypatch.setattr(indexer, "_is_paused", lambda: False)

    async def _noop(*_a, **_k):
        return None

    async def _index(*_a, **_k):
        return (10, 0)

    ticks = {"n": 0}

    async def _tick_counter(*_a, **_k):
        ticks["n"] += 1
        if ticks["n"] >= 1:
            raise asyncio.CancelledError

    monkeypatch.setattr(indexer, "index_vault", _index)
    monkeypatch.setattr(indexer, "embed_vault", _noop)
    monkeypatch.setattr(indexer, "link_backfill_pass", _noop)
    monkeypatch.setattr(indexer, "prewarm_search_caches", _noop)
    monkeypatch.setattr(indexer, "cleanup_expired_tokens", _tick_counter)

    try:
        asyncio.run(indexer.run_indexer_loop())
    except asyncio.CancelledError:
        pass

    assert [r["trigger"] for r in _inserts(log)] == ["startup", "scheduled"]


def test_the_panel_reindex_records_a_manual_row(monkeypatch):
    """A pass in the history that took ten times as long as its neighbours is
    the first thing anyone looks at; "the operator pressed Reindex Now" is the
    first explanation, and only the trigger can say so."""
    import src.control_panel.routes as routes

    log = _capture(monkeypatch)
    monkeypatch.setattr(routes.settings, "multi_user_mode", False, raising=False)
    monkeypatch.setattr(indexer, "index_pass_lock", asyncio.Lock())

    async def _index(*_a, **_k):
        return (4, 1)

    async def _embed(*_a, **_k):
        return 1

    monkeypatch.setattr(indexer, "index_vault", _index)
    monkeypatch.setattr(indexer, "embed_vault", _embed)

    asyncio.run(routes._reindex_background())

    rows = _inserts(log)
    assert [r["trigger"] for r in rows] == ["manual"]
    assert rows[0]["notes_indexed"] == 1
