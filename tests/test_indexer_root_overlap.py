"""Detection at every pass entry point, and the skip in the shared helpers (#199).

The rule this file pins is not "the call appears in five places" but the
property behind it: **no code path may begin a pass over a vault root without a
snapshot published in this process**, and the per-user skip lives in the shared
pass helpers so that a caller added later inherits it rather than having to
remember it.

Entry points, per `design.md`: E1 the lifespan, E2 the indexer's startup block,
E3 each periodic tick, E4 the panel's `_reindex_background` (whose own detect
call is slice 6 — asserted here only against the shared helper it calls), and E5
the standalone `scripts/rebuild_tsvectors.py` process.

Hermetic: no database, no network, no embedding provider.
"""

import asyncio
import datetime
import errno
import importlib.util
import inspect
import os
from pathlib import Path

import pytest

import src.main as main
import src.services.indexer as indexer
from src.services import vault, vault_overlap
from src.services.vault_overlap import (
    RELATION_CONTAINS,
    Overlap,
    QuarantineEntry,
    RootUnexaminable,
)

ROOT = Path(__file__).resolve().parent.parent


# ── Harness ─────────────────────────────────────────────────────────────────


def _entry(user_id, username="alice", assignment="/vaults/team", reason=None):
    return QuarantineEntry(
        user_id=user_id,
        username=username,
        assignment=assignment,
        reason=reason
        or Overlap(99, "bob", "/vaults/team/private", RELATION_CONTAINS),
        detected_at=datetime.datetime.now(datetime.timezone.utc),
    )


def _quarantine(*entries) -> None:
    vault_overlap.publish_synthetic_snapshot(entries)


class _FakeSession:
    """Records the statements executed on it. Enough for the run-row contract."""

    def __init__(self, log):
        self.log = log

    async def execute(self, statement, params=None):
        self.log.append((statement, params))
        return None

    async def commit(self):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _capture_sessions(monkeypatch) -> list:
    log: list = []
    monkeypatch.setattr(indexer, "async_session", lambda: _FakeSession(log))
    return log


def _run_row_errors(log) -> list[str]:
    """The `error` value of every `indexer_runs` INSERT, in order."""
    out = []
    for statement, _params in log:
        try:
            compiled = statement.compile()
        except Exception:
            continue
        if not compiled.statement.is_insert:
            continue
        error = dict(compiled.params).get("error")
        if error is not None:
            out.append(error)
    return out


class _StubSessionManager:
    def run(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _StubMcp:
    def __init__(self):
        self.session_manager = _StubSessionManager()


async def _forever():
    while True:
        await asyncio.sleep(3600)


def _install_lifespan_fakes(monkeypatch):
    """Everything the lifespan touches before the detection, stubbed out."""
    monkeypatch.setattr(main.settings, "mcp_sandbox_mode", False, raising=False)

    async def _noop_check():
        return None

    for name in (
        "_check_embedding_dim",
        "_check_pgvector_version",
        "_validate_fts_configs",
        "_check_embedding_fingerprint",
        "_check_fts_fingerprint",
    ):
        monkeypatch.setattr(main, name, _noop_check)
    monkeypatch.setattr(main, "_check_openat2_support", lambda: None)
    monkeypatch.setattr(main, "_check_mount_identity_support", lambda: None)
    monkeypatch.setattr(main, "run_indexer_loop", _forever)
    monkeypatch.setattr(main, "mcp", _StubMcp())


# ── E1: the lifespan ────────────────────────────────────────────────────────


async def test_e1_publishes_synchronously_before_serving_and_before_the_indexer(
    monkeypatch, unpublished_vault_root_snapshot
):
    """The first snapshot exists before `yield` and before the indexer task.

    An asynchronously-published snapshot is not startup enforcement: a call
    between the first accepted connection and the first published snapshot is
    served against roots nothing has checked.
    """
    _install_lifespan_fakes(monkeypatch)
    order: list[str] = []

    async def _detect(session_factory=None):
        order.append("detect")
        return vault_overlap.publish_synthetic_snapshot()

    monkeypatch.setattr(vault_overlap, "detect_and_publish", _detect)

    real_create_task = asyncio.create_task

    def _spy(coro, *a, **k):
        order.append("create_task")
        return real_create_task(coro, *a, **k)

    monkeypatch.setattr(main.asyncio, "create_task", _spy)

    cm = main.lifespan(object())
    await cm.__aenter__()
    order.append("serving")
    try:
        assert order[0] == "detect"
        assert order.index("detect") < order.index("create_task")
        assert order.index("detect") < order.index("serving")
        assert vault_overlap.is_published()
    finally:
        await cm.__aexit__(None, None, None)
        await asyncio.sleep(0)


async def test_e1_sandbox_publishes_an_empty_snapshot_without_touching_anything(
    monkeypatch, unpublished_vault_root_snapshot
):
    monkeypatch.setattr(main.settings, "mcp_sandbox_mode", True, raising=False)
    monkeypatch.setattr(vault_overlap.settings, "mcp_sandbox_mode", True, raising=False)
    monkeypatch.setattr(main, "mcp", _StubMcp())

    def _never(*a, **k):  # pragma: no cover - asserted by not being called
        raise AssertionError("sandbox mode must open no root")

    monkeypatch.setattr(vault_overlap, "observe_root_blocking", _never)

    cm = main.lifespan(object())
    await cm.__aenter__()
    try:
        snapshot = vault_overlap.published_snapshot()
        assert snapshot is not None
        assert snapshot.entries == {}
    finally:
        await cm.__aexit__(None, None, None)


async def test_e1_a_failed_detection_neither_exits_nor_opens_the_gate(
    monkeypatch, unpublished_vault_root_snapshot, caplog
):
    _install_lifespan_fakes(monkeypatch)

    async def _boom(session_factory=None):
        raise RuntimeError("database is away")

    monkeypatch.setattr(vault_overlap, "detect_and_publish", _boom)
    monkeypatch.setattr(vault.settings, "multi_user_mode", True)

    with caplog.at_level("ERROR"):
        cm = main.lifespan(object())
        await cm.__aenter__()
    try:
        assert not vault_overlap.is_published()
        with pytest.raises(vault.VaultRootNotReady):
            vault._vault_root(5)
        assert any(r.levelname == "ERROR" for r in caplog.records)
    finally:
        await cm.__aexit__(None, None, None)
        await asyncio.sleep(0)


# ── E2 and E3: the indexer loop ─────────────────────────────────────────────


def _install_loop_fakes(monkeypatch, *, multi_user=True, users=(1,)):
    monkeypatch.setattr(indexer, "index_pass_lock", asyncio.Lock())
    monkeypatch.setattr(indexer.settings, "multi_user_mode", multi_user, raising=False)
    monkeypatch.setattr(indexer.settings, "index_interval_seconds", 0, raising=False)

    async def _users():
        return list(users)

    monkeypatch.setattr(indexer, "_active_user_ids", _users)

    async def _noop(*_a, **_k):
        return None

    monkeypatch.setattr(indexer, "cleanup_expired_tokens", _noop)
    monkeypatch.setattr(indexer, "prewarm_search_caches", _noop)
    monkeypatch.setattr(indexer, "_record_index_run", lambda *_a, **_k: None)


async def test_e2_the_startup_pass_publishes_before_it_takes_the_pass_lock(
    monkeypatch, unpublished_vault_root_snapshot
):
    _install_loop_fakes(monkeypatch)
    _capture_sessions(monkeypatch)
    order: list[str] = []

    async def _detect(where):
        order.append(f"detect:{where}")
        assert not indexer.index_pass_lock.locked(), (
            "detection must run before the pass lock, never queued behind the "
            "pass it exists to gate"
        )
        vault_overlap.publish_synthetic_snapshot()

    async def _index(user_id=None):
        order.append("index")
        raise asyncio.CancelledError

    monkeypatch.setattr(indexer, "detect_root_overlaps", _detect)
    monkeypatch.setattr(indexer, "index_vault", _index)

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(indexer.run_indexer_loop(), timeout=5)

    assert order[0] == "detect:startup"
    assert order.index("detect:startup") < order.index("index")


async def test_e3_each_tick_publishes_before_the_pause_check(monkeypatch):
    """Before the pause check, deliberately: a pause suppresses work, not the check."""
    _install_loop_fakes(monkeypatch)
    _capture_sessions(monkeypatch)
    order: list[str] = []

    async def _detect(where):
        order.append(f"detect:{where}")
        if order.count("detect:periodic") >= 2:
            raise asyncio.CancelledError

    def _paused():
        order.append("paused-check")
        return True

    monkeypatch.setattr(indexer, "detect_root_overlaps", _detect)
    monkeypatch.setattr(indexer, "_is_paused", _paused)

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(indexer.run_indexer_loop(), timeout=5)

    assert order[:3] == ["detect:startup", "detect:periodic", "paused-check"]


async def test_e3_a_paused_iteration_still_records_the_quarantine(
    monkeypatch, caplog
):
    """A pause suppresses index and embed work. It must not suppress the record."""
    _install_loop_fakes(monkeypatch)
    log = _capture_sessions(monkeypatch)
    ticks = {"n": 0}

    async def _detect(where):
        ticks["n"] += 1
        _quarantine(_entry(1))
        if ticks["n"] >= 2:
            # One startup detection and one tick; stop after the tick's record.
            pass

    async def _no_stage_while_paused(*_a, **_k):
        return None

    monkeypatch.setattr(indexer, "detect_root_overlaps", _detect)
    monkeypatch.setattr(indexer, "_is_paused", lambda: True)
    monkeypatch.setattr(indexer, "index_vault", _no_stage_while_paused)
    monkeypatch.setattr(indexer, "embed_vault", _no_stage_while_paused)
    monkeypatch.setattr(indexer, "link_backfill_pass", _no_stage_while_paused)

    real_record = indexer.record_quarantined_runs
    calls = {"n": 0}

    async def _record(trigger):
        calls["n"] += 1
        await real_record(trigger)
        if calls["n"] >= 1:
            raise asyncio.CancelledError

    monkeypatch.setattr(indexer, "record_quarantined_runs", _record)

    with caplog.at_level("ERROR"):
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(indexer.run_indexer_loop(), timeout=5)

    errors = _run_row_errors(log)
    assert errors, "a paused iteration must still write the per-user run row"
    # The startup pass runs before the first tick and writes its own row; the
    # one that matters here is the quarantine row the *paused* tick wrote.
    quarantine_rows = [e for e in errors if "alice" in e and "bob" in e]
    assert quarantine_rows, errors
    assert any(r.levelname == "ERROR" for r in caplog.records)


async def test_the_quarantine_row_names_the_pair_from_the_recorded_facts(
    monkeypatch,
):
    log = _capture_sessions(monkeypatch)
    _quarantine(
        _entry(1),
        _entry(
            2,
            username="carol",
            assignment="/vaults/carol",
            reason=RootUnexaminable(errno.ENOENT),
        ),
    )
    await indexer.record_quarantined_runs("scheduled")

    errors = _run_row_errors(log)
    assert len(errors) == 2
    joined = "\n".join(errors)
    assert "bob" in joined  # the peer, from the snapshot's own facts
    assert "ENOENT" in joined
    assert "no peer was observed" in joined.lower()


async def test_no_rows_are_written_when_nothing_is_quarantined(monkeypatch):
    log = _capture_sessions(monkeypatch)
    vault_overlap.publish_synthetic_snapshot()
    await indexer.record_quarantined_runs("scheduled")
    assert _run_row_errors(log) == []


# ── The skip in the shared pass helpers ─────────────────────────────────────


@pytest.mark.parametrize(
    "stage",
    ["index_vault", "link_backfill_pass", "embed_vault"],
)
async def test_every_pass_helper_refuses_a_quarantined_user(monkeypatch, stage):
    _quarantine(_entry(1))

    def _never(*a, **k):  # pragma: no cover - asserted by not being called
        raise AssertionError("the skip must precede the root resolution")

    monkeypatch.setattr(indexer, "_vault_root", _never)
    monkeypatch.setattr(indexer, "pinned_root", _never)

    with pytest.raises(indexer.VaultRootQuarantined) as excinfo:
        await getattr(indexer, stage)(user_id=1)
    message = str(excinfo.value)
    assert "alice" in message and "bob" in message


async def test_the_tsvector_rebuild_helper_refuses_a_quarantined_user(monkeypatch):
    _quarantine(_entry(1))

    def _never(*a, **k):  # pragma: no cover - asserted by not being called
        raise AssertionError("the skip must precede the root resolution")

    monkeypatch.setattr(indexer, "_vault_root", _never)
    with pytest.raises(indexer.VaultRootQuarantined):
        await indexer.rebuild_tsvectors(object(), user_id=1)


@pytest.mark.parametrize(
    "stage",
    ["index_vault", "link_backfill_pass", "embed_vault"],
)
async def test_every_pass_helper_refuses_before_the_first_snapshot(
    monkeypatch, stage, unpublished_vault_root_snapshot
):
    """No pass begins over a root that nothing has checked."""

    def _never(*a, **k):  # pragma: no cover - asserted by not being called
        raise AssertionError("a pass began without a published snapshot")

    monkeypatch.setattr(indexer, "_vault_root", _never)
    with pytest.raises(indexer.VaultRootQuarantined) as excinfo:
        await getattr(indexer, stage)(user_id=1)
    assert "no vault-root overlap snapshot" in str(excinfo.value)


def test_the_skip_never_applies_to_single_user_mode(unpublished_vault_root_snapshot):
    """`user_id is None` has one root and no second assignment."""
    indexer._refuse_quarantined_pass(None, "index")


def test_an_unrelated_tenant_is_not_skipped():
    _quarantine(_entry(1))
    indexer._refuse_quarantined_pass(2, "index")


async def test_an_unexaminable_root_skips_only_its_own_user(monkeypatch):
    _quarantine(
        _entry(
            1,
            username="hung",
            assignment="/vaults/hung",
            reason=RootUnexaminable(errno.ENOENT),
        )
    )
    monkeypatch.setattr(indexer, "_vault_root", lambda uid=None: Path("/tmp"))

    with pytest.raises(indexer.VaultRootQuarantined):
        indexer._refuse_quarantined_pass(1, "index")
    indexer._refuse_quarantined_pass(2, "index")
    indexer._refuse_quarantined_pass(3, "index")


async def test_a_corrected_quarantine_resumes_indexing(monkeypatch, tmp_path):
    _quarantine(_entry(1))
    with pytest.raises(indexer.VaultRootQuarantined):
        indexer._refuse_quarantined_pass(1, "index")

    vault_overlap.publish_synthetic_snapshot()  # a later, clean detection
    indexer._refuse_quarantined_pass(1, "index")


async def test_the_skip_reaches_the_run_row_and_the_error_log(monkeypatch, caplog):
    """`_index_pass_once` records the refusal and does not report a clean run."""
    log = _capture_sessions(monkeypatch)
    _quarantine(_entry(1))

    with caplog.at_level("ERROR"):
        ok = await indexer._index_pass_once(1)

    assert ok is False, "a skipped user's pass is not a clean run"
    errors = _run_row_errors(log)
    assert len(errors) == 1
    assert "alice" in errors[0] and "bob" in errors[0]
    assert "VaultRootQuarantined" in errors[0]
    assert any(r.levelname == "ERROR" for r in caplog.records)


async def test_an_unrelated_tenant_still_indexes_in_the_same_pass(monkeypatch):
    _capture_sessions(monkeypatch)
    _quarantine(_entry(1))
    ran: list[int] = []

    async def _index(user_id=None):
        ran.append(user_id)
        return (0, 0)

    async def _embed(user_id=None):
        return 0

    real_index = indexer.index_vault
    real_embed = indexer.embed_vault

    async def _index_guarded(user_id=None):
        indexer._refuse_quarantined_pass(user_id, "index")
        return await _index(user_id=user_id)

    async def _embed_guarded(user_id=None):
        indexer._refuse_quarantined_pass(user_id, "embed")
        return await _embed(user_id=user_id)

    monkeypatch.setattr(indexer, "index_vault", _index_guarded)
    monkeypatch.setattr(indexer, "embed_vault", _embed_guarded)
    try:
        assert await indexer._index_pass_once(1) is False
        assert await indexer._index_pass_once(2) is True
    finally:
        monkeypatch.setattr(indexer, "index_vault", real_index)
        monkeypatch.setattr(indexer, "embed_vault", real_embed)

    assert ran == [2]


# ── E4: the panel's on-demand reindex inherits the skip ─────────────────────


def test_e4_the_panel_reindex_calls_the_guarded_helpers():
    """`_reindex_background` reaches a pass through the shared helpers.

    Its own `detect_and_publish` call is slice 6's; the *skip* is inherited
    here, which is the point of putting it in the helpers rather than in each
    loop — a caller cannot begin a stage for a quarantined user however it was
    started.
    """
    from src.control_panel import routes

    source = inspect.getsource(routes._reindex_background)
    assert "index_vault" in source
    assert "embed_vault" in source
    for name in ("index_vault", "link_backfill_pass", "embed_vault", "rebuild_tsvectors"):
        assert "_refuse_quarantined_pass" in inspect.getsource(
            getattr(indexer, name)
        ), f"{name} must carry the shared skip"


# ── E5: the standalone tsvector rebuild process ─────────────────────────────


def _load_rebuild_script():
    spec = importlib.util.spec_from_file_location(
        "_rebuild_tsvectors_script", ROOT / "scripts" / "rebuild_tsvectors.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_e5_the_script_publishes_before_it_reads_a_root(
    monkeypatch, unpublished_vault_root_snapshot
):
    """Detection first, and a quarantined scope stops the whole rebuild.

    The publish-before-any-read half is E5's and unchanged. The *disposition*
    of a quarantined scope is not a per-user skip any more (#206): this command
    records a keyword fingerprint asserting that **every retained row** was
    rebuilt under the current `FTS_CONFIGS`, and that is not a claim that can
    be made one tenant at a time. A quarantined owner's rows keep their
    previous-configuration vectors, so continuing past them and recording the
    fingerprint would certify exactly the rows that were not rebuilt — and the
    startup guard, which now fails closed on that fingerprint, would pass while
    keyword search stayed as wrong as before. So the scope aborts the operation,
    names itself, and nothing is committed.
    """
    script = _load_rebuild_script()
    order: list[str] = []

    async def _detect(session_factory=None):
        order.append("detect")
        _quarantine(_entry(1))

    async def _noop_validate(_session):
        order.append("validate")

    async def _driver(session):
        order.append("driver")
        # The driver's own per-scope guard, reached before any row is read for
        # that scope; the real one turns it into a non-completed outcome.
        indexer._refuse_quarantined_pass(1, "tsvector rebuild")
        raise AssertionError("the driver read a quarantined scope")

    async def _dispose():
        return None

    monkeypatch.setattr(script, "detect_and_publish", _detect)
    monkeypatch.setattr(script, "validate_fts_configs", _noop_validate)
    monkeypatch.setattr(script, "rebuild_tsvectors_all_scopes", _driver)
    monkeypatch.setattr(script, "async_session", lambda: _FakeSession([]))
    monkeypatch.setattr(script, "engine", type("E", (), {"dispose": staticmethod(_dispose)}))
    monkeypatch.setattr(script.settings, "multi_user_mode", True, raising=False)

    with pytest.raises(indexer.VaultRootQuarantined):
        await script.main()

    assert order[0] == "detect", "the script publishes before it reads any root"


async def test_e5_a_quarantined_scope_blocks_the_fingerprint(
    monkeypatch, unpublished_vault_root_snapshot
):
    """The driver's own disposition, at the unit the coverage proof is made in.

    A quarantined scope is a **skip**, so it is not a completed rebuild, so the
    driver aborts and records nothing — the same shape as an unsettled
    provenance or an unpinnable root.
    """
    _quarantine(_entry(4))
    empty = indexer.RebuildRootSurvey(
        observations={}, assignments={}, failures={}, descriptors={}
    )
    outcome = await indexer._rebuild_scope(object(), 4, empty)
    assert not outcome.completed
    assert outcome.skip is indexer.RebuildSkip.ROOT_QUARANTINED
    assert "4" in outcome.describe()


async def test_e5_a_failed_detection_aborts_the_script(
    monkeypatch, unpublished_vault_root_snapshot
):
    """An operator at a terminal can read the error and re-run; a rebuild of
    every keyword vector against unchecked roots is what this stops."""
    script = _load_rebuild_script()

    async def _boom(session_factory=None):
        raise RuntimeError("database is away")

    monkeypatch.setattr(script, "detect_and_publish", _boom)

    def _never(*a, **k):  # pragma: no cover - asserted by not being called
        raise AssertionError("the script opened a session before detecting")

    monkeypatch.setattr(script, "async_session", _never)
    with pytest.raises(RuntimeError):
        await script.main()


# ── Detection failures at an entry point ────────────────────────────────────


async def test_a_detection_failure_at_an_entry_point_retains_the_snapshot(
    monkeypatch, caplog
):
    _quarantine(_entry(1))
    standing = vault_overlap.published_snapshot()

    async def _boom(session_factory=None):
        raise RuntimeError("database is away")

    monkeypatch.setattr(vault_overlap, "detect_and_publish", _boom)
    with caplog.at_level("ERROR"):
        await indexer.detect_root_overlaps("periodic")

    assert vault_overlap.published_snapshot() is standing
    assert any(r.levelname == "ERROR" for r in caplog.records)


async def test_a_detection_failure_does_not_abort_the_caller(monkeypatch, caplog):
    """The loop keeps running; every multi-user stage refuses until one publishes."""

    async def _boom(session_factory=None):
        raise RuntimeError("database is away")

    monkeypatch.setattr(vault_overlap, "detect_and_publish", _boom)
    with caplog.at_level("ERROR"):
        await indexer.detect_root_overlaps("startup")  # must not raise
    assert any("detection failed" in r.message.lower() for r in caplog.records)


# ── The detection an entry point actually runs, end to end ──────────────────


class _RowSessionFactory:
    """`async_session`-shaped factory over fixed `(id, username, vault_path)`."""

    def __init__(self, rows):
        self.rows = rows

    def __call__(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, _statement):
        rows = self.rows

        class _Result:
            def all(self):
                return [
                    type("Row", (), {"id": r[0], "username": r[1], "vault_path": r[2]})()
                    for r in rows
                ]

        return _Result()


async def test_an_alias_created_after_assignment_is_caught_at_the_next_entry_point(
    monkeypatch, tmp_path
):
    a = tmp_path / "alice"
    a.mkdir()
    b = tmp_path / "bob"
    b.mkdir()
    factory = _RowSessionFactory([(1, "alice", str(a)), (2, "bob", str(b))])
    monkeypatch.setattr("src.database.async_session", factory, raising=False)
    monkeypatch.setattr(vault_overlap.settings, "mcp_sandbox_mode", False)
    monkeypatch.setattr(vault_overlap.settings, "multi_user_mode", True)

    await indexer.detect_root_overlaps("periodic")
    assert vault_overlap.published_snapshot().entries == {}
    indexer._refuse_quarantined_pass(1, "index")

    b.rmdir()
    b.symlink_to(a)

    await indexer.detect_root_overlaps("periodic")
    assert set(vault_overlap.published_snapshot().entries) == {1, 2}
    with pytest.raises(indexer.VaultRootQuarantined):
        indexer._refuse_quarantined_pass(1, "index")
    with pytest.raises(indexer.VaultRootQuarantined):
        indexer._refuse_quarantined_pass(2, "index")


async def test_a_nested_symlink_is_found_through_the_canonical_paths(
    monkeypatch, tmp_path
):
    """Both assignment strings are unchanged siblings; the real paths nest."""
    outer = tmp_path / "team"
    outer.mkdir()
    inner = outer / "private"
    inner.mkdir()
    alias = tmp_path / "solo"
    alias.symlink_to(inner)

    factory = _RowSessionFactory([(1, "alice", str(outer)), (2, "bob", str(alias))])
    monkeypatch.setattr("src.database.async_session", factory, raising=False)
    monkeypatch.setattr(vault_overlap.settings, "mcp_sandbox_mode", False)
    monkeypatch.setattr(vault_overlap.settings, "multi_user_mode", True)

    await indexer.detect_root_overlaps("periodic")
    entries = vault_overlap.published_snapshot().entries
    assert set(entries) == {1, 2}
    assert entries[1].reason.relation == RELATION_CONTAINS


async def test_single_user_mode_publishes_an_empty_snapshot(monkeypatch):
    factory = _RowSessionFactory([])
    monkeypatch.setattr("src.database.async_session", factory, raising=False)
    monkeypatch.setattr(vault_overlap.settings, "mcp_sandbox_mode", False)

    await indexer.detect_root_overlaps("startup")
    assert vault_overlap.published_snapshot().entries == {}
    indexer._refuse_quarantined_pass(None, "index")


# ── E5's own root survey: the population the serving snapshot does not cover ──
#
# `detect_and_publish` observes **active** users holding an assignment, because
# that is exactly whom the server serves and indexes. The all-scopes keyword
# rebuild opens a different set: every scope that holds `notes_metadata` rows,
# which since #206 deliberately includes an **inactive** owner's retained root.
# So an inactive user retaining `/vaults/team` beside an active tenant at
# `/vaults/team/private` is named by nothing the serving snapshot publishes,
# and the rebuild would read that tenant's notes under the inactive owner's
# scope — under a fingerprint certifying the result.
#
# The survey is that command's own check over its own read set. It publishes
# nothing and quarantines nobody: the serving population is unchanged.


def _scope(owner, assignment, label=None):
    return indexer._RootParticipant(
        owner=owner,
        label=label or f"retained scope user_id={owner}",
        assignment=str(assignment),
        is_scope=True,
    )


def _peer(owner, assignment, username="alice"):
    return indexer._RootParticipant(
        owner=owner,
        label=f"active user '{username}' (user_id={owner})",
        assignment=str(assignment),
        is_scope=False,
    )


async def test_the_survey_catches_an_inactive_scope_containing_an_active_root(
    tmp_path,
):
    """The blocker, at the unit. Nothing about this pair is in the snapshot."""
    outer = tmp_path / "team"
    inner = outer / "private"
    inner.mkdir(parents=True)

    survey = await indexer.survey_rebuild_roots(
        [_scope(2, outer), _scope(1, inner)]
    )

    assert set(survey.failures) == {1, 2}
    assert survey.failures[2].skip is indexer.RebuildSkip.ROOT_OVERLAPS
    detail = survey.failures[2].describe()
    assert str(outer) in detail and str(inner) in detail
    assert "contains" in detail
    assert "is inside" in survey.failures[1].describe()


async def test_the_survey_relates_a_scope_to_an_active_peer_holding_no_rows(
    tmp_path,
):
    """The active tenant need not be a scope: a fresh tenant with no rows yet
    is still a tenant whose notes the rebuild would read through the ancestor.
    """
    outer = tmp_path / "team"
    inner = outer / "private"
    inner.mkdir(parents=True)

    survey = await indexer.survey_rebuild_roots(
        [_scope(2, outer), _peer(1, inner, "alice")]
    )

    assert set(survey.failures) == {2}, "only a scope this driver reads aborts"
    assert "alice" in survey.failures[2].describe()


async def test_two_peers_alone_are_not_this_commands_business(tmp_path):
    """Two active tenants overlapping is the serving snapshot's finding and it
    quarantines them there. This command aborts on what *it* would open."""
    outer = tmp_path / "team"
    inner = outer / "private"
    inner.mkdir(parents=True)

    survey = await indexer.survey_rebuild_roots(
        [_peer(1, outer, "alice"), _peer(2, inner, "bob")]
    )
    assert survey.failures == {}


async def test_the_survey_accepts_siblings_and_a_string_prefix_sibling(tmp_path):
    """The false-positive direction. `/vaults/team` is not an ancestor of
    `/vaults/team-2`, and aborting maintenance on a healthy layout is the
    expensive error here."""
    for name in ("team", "team-2", "other"):
        (tmp_path / name).mkdir()

    survey = await indexer.survey_rebuild_roots([
        _scope(1, tmp_path / "team"),
        _scope(2, tmp_path / "team-2"),
        _scope(3, tmp_path / "other"),
    ])
    assert survey.failures == {}
    assert set(survey.observations) == {1, 2, 3}
    assert all(o.examinable for o in survey.observations.values())


async def test_a_scope_whose_root_cannot_be_examined_is_a_non_completed_outcome(
    tmp_path,
):
    """"We could not look" is not a completed rebuild, and it aborts like the
    others rather than being stepped over."""
    survey = await indexer.survey_rebuild_roots(
        [_scope(1, tmp_path / "gone")]
    )
    assert survey.failures[1].skip is indexer.RebuildSkip.ROOT_UNEXAMINABLE
    assert "could not be examined" in survey.failures[1].describe()


async def test_an_unexaminable_peer_does_not_abort_the_command(tmp_path):
    """L2's class, and the decision that goes with it: one unrelated tenant's
    broken mount must not fail a maintenance command that would not read it."""
    root = tmp_path / "team"
    root.mkdir()

    survey = await indexer.survey_rebuild_roots(
        [_scope(1, root), _peer(2, tmp_path / "gone", "bob")]
    )
    assert survey.failures == {}


async def test_the_survey_sees_through_a_symlink_alias(tmp_path):
    """Two spellings, one inode — which is exactly what a string comparison of
    two `vault_path` values cannot see."""
    real = tmp_path / "team"
    real.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(real)

    survey = await indexer.survey_rebuild_roots(
        [_scope(1, real), _scope(2, alias)]
    )
    assert set(survey.failures) == {1, 2}
    assert "is the same directory as" in survey.failures[1].describe()


async def test_the_survey_publishes_nothing(tmp_path, unpublished_vault_root_snapshot):
    """Maintenance-only. It is a verdict about one command's read set, computed
    and discarded inside it — quarantining an inactive account would refuse
    nothing (nothing serves it) while making an active peer look implicated."""
    outer = tmp_path / "team"
    inner = outer / "private"
    inner.mkdir(parents=True)

    await indexer.survey_rebuild_roots([_scope(2, outer), _scope(1, inner)])

    assert vault_overlap.published_snapshot() is None


async def test_the_survey_observes_off_the_loop_under_the_deadline(
    monkeypatch, tmp_path
):
    """It runs before `acquire_generation_lock`, and it is bounded — a root
    opened synchronously *after* the lock lets one hung mount hold the index
    generation lock for as long as the kernel likes."""
    import threading

    release = threading.Event()

    def _blocking(assignment, **kwargs):
        release.set()
        threading.Event().wait(30)
        raise AssertionError("the deadline did not abandon the wait")

    monkeypatch.setattr(
        vault_overlap, "observe_root_blocking_retaining", _blocking
    )
    monkeypatch.setattr(
        vault_overlap.settings, "vault_root_observe_timeout_seconds", 0.05
    )

    survey = await asyncio.wait_for(
        indexer.survey_rebuild_roots([_scope(1, tmp_path / "team")]), 5
    )
    assert survey.failures[1].skip is indexer.RebuildSkip.ROOT_UNEXAMINABLE
    assert "not answering" in survey.failures[1].describe()


async def test_the_scope_survey_runs_before_the_generation_lock(monkeypatch, tmp_path):
    """Ordering, asserted directly: the observation is complete before the lock
    is taken, and the lock is never held across an unexamined open."""
    outer = tmp_path / "team"
    inner = outer / "private"
    inner.mkdir(parents=True)

    order: list[str] = []

    async def _participants(_session):
        order.append("participants")
        return [_scope(2, outer), _scope(1, inner)]

    real_survey = indexer.survey_rebuild_roots

    async def _survey(participants):
        order.append("survey")
        return await real_survey(participants)

    async def _lock(_session):
        order.append("lock")

    def _never(*_a, **_k):
        raise AssertionError("a root was opened despite the survey's verdict")

    async def _guard(_session):
        order.append("account guard")

    monkeypatch.setattr(indexer, "lock_account_guard", _guard)
    monkeypatch.setattr(indexer, "_rebuild_root_participants", _participants)
    monkeypatch.setattr(indexer, "survey_rebuild_roots", _survey)
    monkeypatch.setattr(indexer, "acquire_generation_lock", _lock)
    monkeypatch.setattr(indexer, "pinned_root", _never)

    with pytest.raises(indexer.RebuildCoverageAborted) as excinfo:
        await indexer.rebuild_tsvectors_all_scopes(object())

    # The account guard is first of all: the survey it protects has not run yet.
    assert order == ["account guard", "participants", "survey"], order
    assert "before the generation lock" in str(excinfo.value)
    assert str(outer) in str(excinfo.value) and str(inner) in str(excinfo.value)


# ── The verdict carried into the locked section ──────────────────────────────
#
# The survey happens before `acquire_generation_lock`; the read happens after.
# In between, a pathname can be retargeted and a scope can be reassigned — and
# a verdict that no longer describes what is about to be opened is not a
# verdict. So `_rebuild_scope` opens nothing the survey did not examine: the
# assignment must still be the one that was observed, and the directory the pin
# lands on must still be the inode that was observed.


def _survey_of(owner, assignment, observation, fd=None):
    """A survey as the driver would hand it over, descriptor included.

    `fd` defaults to a real descriptor for `assignment`, because that is what
    the locked rebuild reads through — a survey without one refuses before it
    reaches any of the checks below, which would make those checks untested.
    The caller closes it via `survey.close()`.
    """
    if fd is None:
        fd = os.open(str(assignment), os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    return indexer.RebuildRootSurvey(
        observations={owner: observation},
        assignments={owner: str(assignment)},
        failures={},
        descriptors={str(assignment): fd},
    )


async def test_a_scope_missing_from_the_survey_is_never_opened(monkeypatch, tmp_path):
    """A scope that appeared between the survey and the lock is a root nobody
    examined. Re-running is cheap; opening it on a survey that did not include
    it is the hole the survey exists to close."""
    root = tmp_path / "team"
    root.mkdir()

    async def _path(_session, _owner):
        return root

    def _never(*_a, **_k):
        raise AssertionError("an unexamined root was opened under the lock")

    monkeypatch.setattr(indexer, "_scope_vault_path", _path)
    monkeypatch.setattr(indexer, "pinned_root", _never)

    empty = indexer.RebuildRootSurvey(observations={}, assignments={}, failures={})
    outcome = await indexer._rebuild_scope(object(), 7, empty)

    assert outcome.skip is indexer.RebuildSkip.ROOT_UNEXAMINABLE
    assert "not in the pre-lock root survey" in outcome.describe()


async def test_a_reassignment_between_the_survey_and_the_lock_refuses(
    monkeypatch, tmp_path
):
    """The scope's `users.vault_path` changed after it was observed, so the
    verdict describes a directory this rebuild is no longer going to read."""
    surveyed = tmp_path / "team"
    surveyed.mkdir()
    moved = tmp_path / "elsewhere"
    moved.mkdir()

    async def _path(_session, _owner):
        return moved

    def _never(*_a, **_k):
        raise AssertionError("an unexamined root was opened under the lock")

    monkeypatch.setattr(indexer, "_scope_vault_path", _path)
    monkeypatch.setattr(indexer, "pinned_root", _never)

    survey = _survey_of(7, surveyed, vault_overlap.observe_root_blocking(str(surveyed)))
    try:
        outcome = await indexer._rebuild_scope(object(), 7, survey)
    finally:
        survey.close()

    assert outcome.skip is indexer.RebuildSkip.ROOT_UNEXAMINABLE
    assert str(surveyed) in outcome.describe()
    assert str(moved) in outcome.describe()


async def test_a_pathname_retargeted_after_the_survey_cannot_reach_the_rebuild(
    monkeypatch, tmp_path
):
    """Retargeting the pathname is now a no-op against this command.

    The rebuild reads through the **descriptor the survey opened**, not through
    the name: a symlink repointed between the survey and the lock renames
    nothing the driver is holding. This asserts the mechanism directly — the
    scope root is replaced by a symlink to somewhere else entirely, and the
    rebuild still receives the original directory's descriptor.
    """
    root = tmp_path / "team"
    root.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    async def _path(_session, _owner):
        return root

    monkeypatch.setattr(indexer, "_scope_vault_path", _path)

    observed = vault_overlap.observe_root_blocking(str(root))
    survey = _survey_of(7, root, observed)

    handed = {}

    async def _rebuilt(session, user_id, vault, root_fd, log_suffix):
        handed["st"] = os.fstat(root_fd)
        return indexer.RebuildOutcome(rows=1)

    monkeypatch.setattr(indexer, "_rebuild_tsvectors_pinned", _rebuilt)

    # The name now points somewhere else. A reopen would land on `elsewhere`.
    root.rmdir()
    root.symlink_to(elsewhere)

    def _no_open(*_a, **_k):
        raise AssertionError("the rebuild resolved a pathname after the survey")

    monkeypatch.setattr(indexer, "pinned_root", _no_open)

    try:
        outcome = await indexer._rebuild_scope(object(), 7, survey)
    finally:
        survey.close()

    assert outcome.completed
    assert (handed["st"].st_dev, handed["st"].st_ino) == (
        observed.st_dev, observed.st_ino
    ), "the rebuild read the retargeted directory rather than the examined one"


async def test_a_descriptor_that_does_not_match_its_recorded_facts_refuses(
    monkeypatch, tmp_path
):
    """The bookkeeping check on the handover.

    The descriptor cannot drift from the directory it names, so this cannot be
    a filesystem race — it is the assertion that the fd handed to the rebuild
    is the fd whose facts the survey recorded, which is what makes the
    survey's verdict a statement about what is read.
    """
    root = tmp_path / "team"
    root.mkdir()

    async def _path(_session, _owner):
        return root

    monkeypatch.setattr(indexer, "_scope_vault_path", _path)

    observed = vault_overlap.observe_root_blocking(str(root))
    stale = vault_overlap.RootObservation(
        assignment=observed.assignment,
        st_dev=observed.st_dev,
        st_ino=observed.st_ino + 1,
        realpath=observed.realpath,
    )

    async def _never_rebuilt(*_a, **_k):
        raise AssertionError("the scope was rebuilt through an unexamined inode")

    monkeypatch.setattr(indexer, "_rebuild_tsvectors_pinned", _never_rebuilt)

    survey = _survey_of(7, root, stale)
    try:
        outcome = await indexer._rebuild_scope(object(), 7, survey)
    finally:
        survey.close()

    assert outcome.skip is indexer.RebuildSkip.ROOT_UNEXAMINABLE
    assert "does not report the facts the survey recorded" in outcome.describe()


async def test_a_scope_with_no_retained_descriptor_is_never_reopened(
    monkeypatch, tmp_path
):
    """No descriptor, no rebuild — and emphatically no second `open`."""
    root = tmp_path / "team"
    root.mkdir()

    async def _path(_session, _owner):
        return root

    def _no_open(*_a, **_k):
        raise AssertionError("the rebuild reopened a pathname under the lock")

    monkeypatch.setattr(indexer, "_scope_vault_path", _path)
    monkeypatch.setattr(indexer, "pinned_root", _no_open)

    survey = indexer.RebuildRootSurvey(
        observations={7: vault_overlap.observe_root_blocking(str(root))},
        assignments={7: str(root)},
        failures={},
        descriptors={},
    )
    outcome = await indexer._rebuild_scope(object(), 7, survey)

    assert outcome.skip is indexer.RebuildSkip.ROOT_UNEXAMINABLE
    assert "no retained descriptor" in outcome.describe()


async def test_a_matching_verdict_proceeds_to_the_rebuild(monkeypatch, tmp_path):
    """The negative control: same assignment, same inode, and the scope is
    rebuilt exactly as it was before the survey existed."""
    root = tmp_path / "team"
    root.mkdir()

    async def _path(_session, _owner):
        return root

    seen = {}

    async def _rebuilt(session, user_id, vault, root_fd, log_suffix):
        seen["user_id"] = user_id
        return indexer.RebuildOutcome(rows=3)

    monkeypatch.setattr(indexer, "_scope_vault_path", _path)
    monkeypatch.setattr(indexer, "_rebuild_tsvectors_pinned", _rebuilt)

    survey = _survey_of(7, root, vault_overlap.observe_root_blocking(str(root)))
    try:
        outcome = await indexer._rebuild_scope(object(), 7, survey)
    finally:
        survey.close()

    assert outcome.completed and outcome.rows == 3
    assert seen["user_id"] == 7


# ── Nothing resolves a pathname after the generation lock ────────────────────


async def test_no_root_is_opened_by_name_after_the_generation_lock(
    monkeypatch, tmp_path
):
    """The MAJOR, asserted where it bites: `os.open` on a vault root is banned
    from the locked section entirely.

    `pinned_root` opened the scope's pathname *after* `acquire_generation_lock`.
    Two things follow and both are bad. The `open` is unbounded, so a hung NFS
    or FUSE mount holds the index generation lock for as long as the kernel
    takes — every pass in the process, in every container, queues behind one
    stalled mount. And it is a second lookup, so the inode it lands on need not
    be the inode the survey checked.

    The survey therefore retains the descriptor and the rebuild reads through
    it. The stub below fails **only** for the scope roots and only after the
    lock, so pytest's own machinery (which opens source files to format a
    failure) is untouched.
    """
    roots = {}
    for owner in (1, 2):
        root = tmp_path / f"vault{owner}"
        root.mkdir()
        roots[owner] = root

    participants = [_scope(owner, root) for owner, root in roots.items()]
    survey = await indexer.survey_rebuild_roots(participants)
    assert survey.failures == {}
    assert len(survey.descriptors) == 2, "the survey retained no descriptors"

    locked = {"yes": False}
    banned = {str(root) for root in roots.values()}
    real_open = os.open

    def _guarded_open(path, *args, **kwargs):
        if locked["yes"] and str(path) in banned:
            raise AssertionError(
                f"a vault root pathname was opened after the generation lock: {path}"
            )
        return real_open(path, *args, **kwargs)

    async def _path(_session, owner):
        return roots[owner]

    async def _rebuilt(session, user_id, vault, root_fd, log_suffix):
        # It really did get a usable directory, not merely "not an open".
        os.fstat(root_fd)
        return indexer.RebuildOutcome(rows=1)

    monkeypatch.setattr(indexer, "_scope_vault_path", _path)
    monkeypatch.setattr(indexer, "_rebuild_tsvectors_pinned", _rebuilt)
    monkeypatch.setattr(os, "open", _guarded_open)

    try:
        locked["yes"] = True
        outcomes = [
            await indexer._rebuild_scope(object(), owner, survey)
            for owner in roots
        ]
    finally:
        locked["yes"] = False
        monkeypatch.undo()
        survey.close()

    assert all(o.completed for o in outcomes)


async def test_the_survey_closes_a_peer_descriptor_it_will_never_read(tmp_path):
    """A peer is present so a scope can be found to overlap it. Nothing reads
    its directory, so holding it open would be a plain leak."""
    scope_root = tmp_path / "scope"
    scope_root.mkdir()
    peer_root = tmp_path / "peer"
    peer_root.mkdir()

    survey = await indexer.survey_rebuild_roots(
        [_scope(1, scope_root), _peer(2, peer_root, "bob")]
    )
    try:
        assert set(survey.descriptors) == {str(scope_root)}
        assert survey.descriptor_for(2) is None
    finally:
        survey.close()


async def test_close_is_idempotent_and_releases_every_descriptor(tmp_path):
    """It runs from a `finally` that also runs on the abort paths, so a second
    call must be a no-op rather than an `EBADF` replacing the real failure."""
    roots = []
    for name in ("a", "b"):
        root = tmp_path / name
        root.mkdir()
        roots.append(root)

    survey = await indexer.survey_rebuild_roots(
        [_scope(i, root) for i, root in enumerate(roots)]
    )
    held = list(survey.descriptors.values())
    assert len(held) == 2

    survey.close()
    assert survey.descriptors == {}
    survey.close()  # must not raise

    for fd in held:
        with pytest.raises(OSError):
            os.fstat(fd)


async def test_an_abort_after_the_survey_still_closes_every_descriptor(
    monkeypatch, tmp_path
):
    """The driver's `finally`, through the real entry point."""
    root = tmp_path / "team"
    root.mkdir()

    surveys = []
    real_survey = indexer.survey_rebuild_roots

    async def _survey(participants):
        result = await real_survey(participants)
        surveys.append(result)
        return result

    async def _participants(_session):
        return [_scope(1, root)]

    async def _guard(_session):
        return None

    async def _lock(_session):
        raise RuntimeError("the lock wait was interrupted")

    monkeypatch.setattr(indexer, "lock_account_guard", _guard)
    monkeypatch.setattr(indexer, "_rebuild_root_participants", _participants)
    monkeypatch.setattr(indexer, "survey_rebuild_roots", _survey)
    monkeypatch.setattr(indexer, "acquire_generation_lock", _lock)

    with pytest.raises(RuntimeError, match="lock wait"):
        await indexer.rebuild_tsvectors_all_scopes(object())

    assert len(surveys) == 1
    assert surveys[0].descriptors == {}, (
        "an abort after the survey leaked its retained descriptors"
    )


async def test_an_abandoned_observation_closes_the_descriptor_it_opened_late(
    monkeypatch, tmp_path
):
    """L4's descriptor half.

    The deadline abandons the wait, not the syscall: the thread stays parked in
    `open(2)` and comes back holding an open directory nobody is waiting for.
    Without the late-completion callback that descriptor lives as long as the
    process — one per stalled root per run, on exactly the pathological mount
    the deadline exists to survive.
    """
    import threading

    root = tmp_path / "team"
    root.mkdir()
    release = threading.Event()
    opened = {}

    real = vault_overlap.observe_root_blocking_retaining

    def _slow(assignment, **kwargs):
        release.wait(10)
        observation, fd = real(assignment, **kwargs)
        opened["fd"] = fd
        return observation, fd

    monkeypatch.setattr(vault_overlap, "observe_root_blocking_retaining", _slow)
    monkeypatch.setattr(
        vault_overlap.settings, "vault_root_observe_timeout_seconds", 0.05
    )

    observation, fd = await indexer._observe_root_retaining(str(root))
    assert observation.cause == vault_overlap.CAUSE_TIMEOUT
    assert fd is None

    # Let the thread finish; the done-callback closes what it opened.
    release.set()
    for _ in range(200):
        if "fd" in opened:
            break
        await asyncio.sleep(0.01)
    assert "fd" in opened, "the abandoned observation never completed"
    for _ in range(200):
        try:
            os.fstat(opened["fd"])
        except OSError:
            break
        await asyncio.sleep(0.01)
    else:  # pragma: no cover - the callback did not run
        raise AssertionError("the late descriptor was never closed")
