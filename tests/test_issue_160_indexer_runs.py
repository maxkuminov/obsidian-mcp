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
from src.services.embeddings import (
    EmbedNoteFailure,
    EmbedNoteResult,
    NoteEmbedOutcome,
)


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


# --------------------------------------------------------------------------
# 5. A guard phase that raises is a pass, not a skip.
# --------------------------------------------------------------------------


def test_a_guard_phase_that_raises_still_records(monkeypatch):
    """`skipped` is set **up-front** by the link backfill and cleared only once
    every guard has passed — so a guard that raised left the flag standing and
    the write was suppressed. The pass an operator would go looking for was the
    only one that recorded nothing.

    This is the shape of that bug in one place: the flag is set, then the body
    raises. An exception is evidence the pass ran.
    """
    log = _capture(monkeypatch)

    async def _run():
        async with indexer.record_indexer_run("backfill", 4) as stats:
            stats.skipped = True  # the guard phase's up-front default
            raise RuntimeError("the provenance probe blew up")

    with pytest.raises(RuntimeError):
        asyncio.run(_run())

    rows = _inserts(log)
    assert len(rows) == 1, "a raising guard phase recorded nothing"
    assert rows[0]["trigger"] == "backfill"
    assert rows[0]["user_id"] == 4
    assert "the provenance probe blew up" in rows[0]["error"]


def test_a_raising_link_backfill_records_its_error(monkeypatch, tmp_path):
    """The same property through the real caller: `_link_backfill_pinned` sets
    `skipped` before its first guard, and the guard is what raises here."""
    log = _capture(monkeypatch)
    vault = tmp_path / "vault"
    vault.mkdir()
    monkeypatch.setattr(indexer, "_vault_root", lambda _uid: vault)

    async def _explode(*_a, **_k):
        raise RuntimeError("provenance query failed")

    monkeypatch.setattr(indexer, "_ancillary_pass_is_permitted", _explode)

    with pytest.raises(RuntimeError):
        asyncio.run(indexer.link_backfill_pass(user_id=9))

    rows = _inserts(log)
    assert [r["trigger"] for r in rows] == ["backfill"]
    assert rows[0]["user_id"] == 9
    assert "provenance query failed" in rows[0]["error"]


def test_a_genuinely_skipped_backfill_still_records_nothing(monkeypatch, tmp_path):
    """The other side of the same line: the guards pass, decide there is nothing
    to do, and return. That is not a pass and must not spend a row — a row per
    startup for work that did not happen evicts what an operator came for."""
    log = _capture(monkeypatch)
    vault = tmp_path / "vault"
    vault.mkdir()
    monkeypatch.setattr(indexer, "_vault_root", lambda _uid: vault)

    async def _permitted(*_a, **_k):
        return False

    monkeypatch.setattr(indexer, "_ancillary_pass_is_permitted", _permitted)
    monkeypatch.setattr(indexer, "async_session", lambda: _FakeSession(log))

    asyncio.run(indexer.link_backfill_pass(user_id=9))
    assert _inserts(log) == []


# --------------------------------------------------------------------------
# 6. A user deleted mid-pass costs the owner label, not the row.
# --------------------------------------------------------------------------


class _FKViolation(Exception):
    """Stands in for asyncpg's `ForeignKeyViolationError`, which carries the
    SQLSTATE the retry keys on."""

    sqlstate = "23503"


def _integrity_error():
    from sqlalchemy.exc import IntegrityError

    return IntegrityError("INSERT INTO indexer_runs", {}, _FKViolation("no such user"))


def test_a_user_deleted_mid_pass_is_recorded_with_no_owner(monkeypatch):
    """`user_id` is captured at pass start and inserted at pass end; a pass over
    a large vault runs for minutes. An administrator deleting that user in
    between makes the FK reject the INSERT — `ON DELETE SET NULL` cannot help,
    because it fires on rows that already exist and this one never got in.

    Swallowed, the whole pass vanished from the history: the longest passes are
    the likeliest to lose the race, and "the operator just deleted a user" is
    exactly when they open the page.
    """
    log: list = []
    sessions: list = []

    class _FKSession(_FakeSession):
        async def execute(self, statement, params=None):
            compiled = getattr(statement, "compile", lambda: None)()
            is_insert = compiled is not None and compiled.statement.is_insert
            # Only the *first* session's insert fails, and only when it names a
            # user: the retry must go through.
            if is_insert and len(sessions) == 1 and compiled.params["user_id"] is not None:
                raise _integrity_error()
            return await super().execute(statement, params)

    def _factory():
        session = _FKSession(log)
        sessions.append(session)
        return session

    monkeypatch.setattr(indexer, "async_session", _factory)

    async def _run():
        async with indexer.record_indexer_run("scheduled", 42) as stats:
            stats.record_index((900, 30))

    asyncio.run(_run())

    assert len(sessions) == 2, "the retry must run in a fresh session — the "\
        "first one's transaction is aborted and every statement on it fails"
    rows = _inserts(log)
    assert len(rows) == 1, "the pass must still be recorded"
    assert rows[0]["user_id"] is None, (
        "the owner is gone; NULL is what the column would hold a moment later"
    )
    assert rows[0]["notes_scanned"] == 900, "the pass's own numbers survive"
    assert rows[0]["trigger"] == "scheduled"
    # And the retry prunes exactly like the first attempt would have.
    assert len(_deletes(log)) == 1


def test_a_non_fk_failure_is_not_retried(monkeypatch):
    """The retry is narrow on purpose: a connection failure retried with a NULL
    owner is one more failed write and a lost owner label for nothing."""
    log: list = []
    sessions: list = []

    class _BrokenSession(_FakeSession):
        async def execute(self, statement, params=None):
            raise OSError("connection refused")

    def _factory():
        session = _BrokenSession(log)
        sessions.append(session)
        return session

    monkeypatch.setattr(indexer, "async_session", _factory)

    async def _run():
        async with indexer.record_indexer_run("scheduled", 42):
            pass

    asyncio.run(_run())  # must not raise
    assert len(sessions) == 1


def test_an_ownerless_pass_is_not_retried(monkeypatch):
    """Nothing to fall back to: `user_id` is already NULL."""
    log: list = []
    sessions: list = []

    class _AlwaysFK(_FakeSession):
        async def execute(self, statement, params=None):
            raise _integrity_error()

    def _factory():
        session = _AlwaysFK(log)
        sessions.append(session)
        return session

    monkeypatch.setattr(indexer, "async_session", _factory)

    async def _run():
        async with indexer.record_indexer_run("startup", None):
            pass

    asyncio.run(_run())
    assert len(sessions) == 1


def test_the_fk_predicate_reads_the_drivers_sqlstate():
    """Matched on the SQLSTATE, never on the message, which is localised."""
    assert indexer._is_fk_violation(_integrity_error())
    assert not indexer._is_fk_violation(OSError("connection refused"))

    class _OtherPg(Exception):
        sqlstate = "23505"  # unique_violation

    from sqlalchemy.exc import IntegrityError

    assert not indexer._is_fk_violation(
        IntegrityError("stmt", {}, _OtherPg("duplicate key"))
    )


# --------------------------------------------------------------------------
# 7. A provider outage is not a clean pass.
# --------------------------------------------------------------------------


class _EmbedSession:
    """Just enough session for `_embed_vault_pinned`.

    It answers the backlog query with `rows` and the ORM re-read with a note
    object, and records nothing else. The failure under test is raised by the
    *provider* — `embed_note` — which is where a real Ollama or OpenAI outage
    surfaces; a test that made `embed_vault` itself raise would exercise the
    `record_indexer_run` handler instead of the per-note swallow that is the
    actual defect.
    """

    def __init__(self, rows, log):
        self.rows = list(rows)
        self.log = log

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def commit(self):
        self.log.append(("commit", None, None))

    async def rollback(self):
        pass

    async def execute(self, statement, params=None):
        from sqlalchemy.sql import Select
        from sqlalchemy.sql.elements import TextClause

        if isinstance(statement, TextClause):
            if "embedded_content_hash IS NULL" in statement.text:
                return _EmbedResult(self.rows)
            return _EmbedResult()
        if isinstance(statement, Select):
            note_id = statement.compile().params.get("id_1")
            row = next(r for r in self.rows if r.id == note_id)
            result = _EmbedResult([row])
            result.scalar_one = lambda: row
            return result
        self.log.append(("execute", statement, params))
        return _EmbedResult()


class _EmbedResult:
    def __init__(self, rows=()):
        self.rows = list(rows)

    def fetchall(self):
        return self.rows

    def scalar(self):
        return self.rows[0] if self.rows else None

    def scalar_one_or_none(self):
        # `get_state`: no stored embedding fingerprint, which is **absent** —
        # the state startup adopts — so the stage-head early exit proceeds and
        # this section stays about the outage it is named for.
        return self.rows[0] if self.rows else None


def _provider_failed(message: str, *, chunks: int = 1) -> EmbedNoteResult:
    """What `embed_note` returns for a provider raise now that it is typed.

    The exception no longer escapes `embed_note` — it is swallowed there and
    described by an `EmbedNoteFailure`, because `_reconcile_exclusions` calls
    the same function and its declared convergence exception requires the row
    to be left unstamped and retried. So the pass's record is built from the
    structured failure, and `first_error` reads the provider's message rather
    than `None`.
    """
    return EmbedNoteResult(
        outcome=NoteEmbedOutcome.PROVIDER_FAILED,
        chunks_submitted=chunks,
        chunks_embedded=0,
        truncated=False,
        failure=EmbedNoteFailure(
            exc_type="RuntimeError", message=message, requested=chunks
        ),
    )


def _embedded(chunks: int = 1) -> EmbedNoteResult:
    return EmbedNoteResult(
        outcome=NoteEmbedOutcome.EMBEDDED,
        chunks_submitted=chunks,
        chunks_embedded=chunks,
        truncated=False,
    )


def _embed_fixture(monkeypatch, tmp_path, contents: dict[str, str]):
    """A real vault (the pass pins a root and reads through it) plus rows whose
    `content_hash` matches the bytes, so nothing is skipped by verification."""
    import types

    vault = tmp_path / "vault"
    vault.mkdir()
    rows = []
    for i, (name, body) in enumerate(contents.items(), start=1):
        (vault / name).write_text(body, encoding="utf-8")
        rows.append(types.SimpleNamespace(
            id=i,
            file_path=name,
            content_hash=indexer._content_hash(body),
            embedded_content_hash=None,
        ))

    log: list = []
    monkeypatch.setattr(indexer, "_vault_root", lambda _uid: vault)
    monkeypatch.setattr(indexer, "_is_paused", lambda: False)
    monkeypatch.setattr(
        indexer.settings, "embedding_exclude_patterns", [], raising=False
    )

    async def _no_reconcile(*_a, **_k):
        return None

    monkeypatch.setattr(indexer, "_reconcile_exclusions", _no_reconcile)
    monkeypatch.setattr(indexer, "async_session", lambda: _EmbedSession(rows, log))
    return vault, rows


def test_a_total_provider_outage_is_not_a_clean_run_row(monkeypatch, tmp_path):
    """The falsely-clean row this exists to remove.

    `embed_vault` catches every per-note provider exception, logs a warning and
    carries on — right behaviour, and it used to be the pass's only record. A
    total outage therefore embedded nothing, raised nothing, and wrote
    `notes_embedded = 0, error = NULL`: byte for byte the row a pass with
    nothing to embed writes. An operator watching the history through an
    afternoon of Ollama being down would have seen a wall of healthy passes.
    """
    _embed_fixture(monkeypatch, tmp_path, {
        "A.md": "alpha\n", "B.md": "beta\n", "C.md": "gamma\n",
    })

    async def _provider_is_down(*_a, **_k):
        return _provider_failed("Ollama: connection refused")

    monkeypatch.setattr(indexer, "embed_note", _provider_is_down)

    result = asyncio.run(indexer.embed_vault(user_id=7))
    assert result.embedded == 0
    assert result.failures == 3
    assert result.attempted == 3
    assert "connection refused" in result.first_error

    # And the summary reaches the row, which is the point.
    log = _capture(monkeypatch)

    async def _run():
        async with indexer.record_indexer_run("scheduled", 7) as stats:
            stats.record_embedded(result)

    asyncio.run(_run())
    row = _inserts(log)[0]
    assert row["notes_embedded"] == 0
    assert row["error"] is not None, "a total outage recorded a clean pass"
    assert "embed failures: 3 of 3" in row["error"]
    assert "Ollama: connection refused" in row["error"]


def test_a_partial_outage_keeps_the_successful_count(monkeypatch, tmp_path):
    """The count stays truthful — a pass that embedded two of three reports two
    — and the failure rides alongside it rather than replacing it."""
    _embed_fixture(monkeypatch, tmp_path, {
        "A.md": "alpha\n", "B.md": "beta\n", "C.md": "gamma\n",
    })

    async def _flaky(_session, note, _content, **_kwargs):
        if note.file_path == "B.md":
            return _provider_failed("read timeout", chunks=2)
        return _embedded(2)

    monkeypatch.setattr(indexer, "embed_note", _flaky)

    result = asyncio.run(indexer.embed_vault(user_id=7))
    assert (result.embedded, result.failures, result.attempted) == (2, 1, 3)

    log = _capture(monkeypatch)

    async def _run():
        async with indexer.record_indexer_run("scheduled", 7) as stats:
            stats.record_embedded(result)

    asyncio.run(_run())
    row = _inserts(log)[0]
    assert row["notes_embedded"] == 2
    assert "embed failures: 1 of 3" in row["error"]
    assert "read timeout" in row["error"]


def test_a_pass_with_nothing_wrong_still_records_no_error(monkeypatch, tmp_path):
    """The control. Without it "the error column is never NULL" would pass this
    section, and the two rows an operator has to tell apart would merge from the
    other direction."""
    _embed_fixture(monkeypatch, tmp_path, {"A.md": "alpha\n"})

    async def _ok(*_a, **_k):
        return _embedded()

    monkeypatch.setattr(indexer, "embed_note", _ok)

    result = asyncio.run(indexer.embed_vault(user_id=7))
    assert (result.embedded, result.failures) == (1, 0)
    assert result.failure_summary is None

    log = _capture(monkeypatch)

    async def _run():
        async with indexer.record_indexer_run("scheduled", 7) as stats:
            stats.record_embedded(result)

    asyncio.run(_run())
    row = _inserts(log)[0]
    assert row["notes_embedded"] == 1
    assert row["error"] is None


def test_the_recorder_still_takes_a_bare_count(monkeypatch):
    """`embed_vault` is monkeypatched with plain no-op coroutines all over the
    suite, and the panel's reindex path hands the result straight through."""
    log = _capture(monkeypatch)

    async def _run():
        async with indexer.record_indexer_run("manual") as stats:
            stats.record_embedded(11)

    asyncio.run(_run())
    row = _inserts(log)[0]
    assert row["notes_embedded"] == 11 and row["error"] is None


# --------------------------------------------------------------------------
# 8. The multi-user startup: one pass per user, index → backfill → embed.
# --------------------------------------------------------------------------


def _startup_two_users(monkeypatch, *, backfill_fails_for=None):
    """Drive `run_indexer_loop`'s startup half with two users and record the
    order of every phase. Returns `(insert rows, phase log)`."""
    log = _capture(monkeypatch)
    monkeypatch.setattr(indexer.settings, "multi_user_mode", True)
    monkeypatch.setattr(indexer, "_is_paused", lambda: False)
    monkeypatch.setattr(indexer, "index_pass_lock", asyncio.Lock())

    phases: list[tuple[str, int | None]] = []

    async def _users(*_a, **_k):
        return [1, 2]

    async def _index(user_id=None, **_k):
        phases.append(("index", user_id))
        return (100 + user_id, user_id)

    async def _backfill(user_id=None, **_k):
        phases.append(("backfill", user_id))
        if user_id == backfill_fails_for:
            raise RuntimeError(f"user {user_id}'s links are unreadable")
        return None

    async def _embed(user_id=None, **_k):
        phases.append(("embed", user_id))
        return user_id

    async def _stop(*_a, **_k):
        # Cancel while the loop is asleep: the startup half is done, no tick
        # has begun.
        raise asyncio.CancelledError

    monkeypatch.setattr(indexer, "_active_user_ids", _users)
    monkeypatch.setattr(indexer, "index_vault", _index)
    monkeypatch.setattr(indexer, "link_backfill_pass", _backfill)
    monkeypatch.setattr(indexer, "embed_vault", _embed)
    monkeypatch.setattr(indexer.asyncio, "sleep", _stop)

    try:
        asyncio.run(indexer.run_indexer_loop())
    except asyncio.CancelledError:
        pass

    return _inserts(log), phases


def test_multi_user_startup_records_one_row_per_user(monkeypatch):
    """The restructure the run record forced: three loops over every user
    became one per-user index → backfill → embed sequence, because a row
    spanning two loops would have to be held open across them and its start and
    finish would describe the whole startup rather than that user's pass."""
    rows, phases = _startup_two_users(monkeypatch)

    assert [r["trigger"] for r in rows] == ["startup", "startup"]
    assert [r["user_id"] for r in rows] == [1, 2]
    # Each row carries that user's own numbers, not the startup's total.
    assert [(r["notes_scanned"], r["notes_indexed"], r["notes_embedded"]) for r in rows] == [
        (101, 1, 1), (102, 2, 2),
    ]
    assert all(r["error"] is None for r in rows)


def test_multi_user_startup_orders_the_phases_within_each_user(monkeypatch):
    """The ordering that matters is per user and unchanged: the backfill reads
    `notes_metadata`, and the embed pass reads the hashes the scan wrote."""
    _rows, phases = _startup_two_users(monkeypatch)

    assert phases == [
        ("index", 1), ("backfill", 1), ("embed", 1),
        ("index", 2), ("backfill", 2), ("embed", 2),
    ]


def test_one_users_failed_backfill_does_not_abort_the_next_user(monkeypatch):
    """What the three-loop shape got wrong. The index and embed stages were
    already isolated per user; the backfill was not, so the first user whose
    link rebuild raised took every later user's backfill down with it — on
    startup, silently, for as long as that vault stayed broken."""
    rows, phases = _startup_two_users(monkeypatch, backfill_fails_for=1)

    assert phases == [
        ("index", 1), ("backfill", 1), ("embed", 1),
        ("index", 2), ("backfill", 2), ("embed", 2),
    ], "user 1's failed backfill aborted the rest of the startup"

    assert [r["user_id"] for r in rows] == [1, 2]
    # User 1's row names the failure, and user 1's *other* two stages still ran.
    assert "link backfill" in rows[0]["error"]
    assert "unreadable" in rows[0]["error"]
    assert rows[0]["notes_embedded"] == 1
    # User 2's pass is untouched by it.
    assert rows[1]["error"] is None
    assert (rows[1]["notes_scanned"], rows[1]["notes_embedded"]) == (102, 2)
