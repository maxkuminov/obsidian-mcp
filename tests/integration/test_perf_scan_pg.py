"""#278 / #282 — the walk ahead of the lock, and the sweep gate, on a real server.

The walk now runs before the generation lock (D9), off the loop and outside
any transaction. What keeps that safe is that every row decision is still
taken under the lock, against rows re-read under it:

* **C4** — a walked path whose locked row differs from the snapshot is
  re-read and re-decided under the lock;
* **C5** — a locked row the walk did not see is pruned or paired as a move
  only if it is exactly the row the snapshot saw; otherwise it is deferred.

Those are properties of real interleavings with other transactions, so they
are driven here against PostgreSQL, with the "other process" committing in the
window between the pass's snapshot and its lock. Also here: that no
transaction is open while the walk runs (read off `pg_stat_activity`), that a
reset racing a walk neither deadlocks nor lets the walk's verdicts land, and
the exclusion sweep's gate (D13) — skipped after a clean sweep, re-run after a
provider failure or on a backstop pass, and not fooled by an A→B→A edit.

Skipped unless `PGVECTOR_TEST_ADMIN_URL` is set — see `_harness.py`.
"""
import asyncio
import hashlib
import threading
import time

import pytest
import pytest_asyncio
from sqlalchemy import event, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import src.database
import src.mcp_server.tools as tools
from src.config import settings
from src.mcp_server.auth import current_permission
from src.models.db import NoteEmbedding, NoteMetadata
from src.services import embeddings as embeddings_service
from src.services import index_state, indexer
import _harness

pytestmark = [
    _harness.requires_pgvector,
    pytest.mark.asyncio(loop_scope="module"),
]

DIM = int(settings.embedding_dimensions)
BODY = "a body with enough words in it to produce exactly one chunk\n"


def content_hash(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


@pytest.fixture(scope="module")
def migrated_url():
    yield from _harness.throwaway_database("perf_scan_s3", DIM)


@pytest_asyncio.fixture(loop_scope="module", scope="module")
async def engine(migrated_url):
    engine = create_async_engine(migrated_url)
    yield engine
    await engine.dispose()


class _Patterns:
    def __init__(self, monkeypatch):
        self._monkeypatch = monkeypatch
        self.set([])

    def set(self, patterns):
        for target in (settings, indexer.settings):
            self._monkeypatch.setattr(
                target, "embedding_exclude_patterns", patterns, raising=False
            )


@pytest_asyncio.fixture(loop_scope="module")
async def world(engine, monkeypatch, tmp_path):
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    root = tmp_path / "vault"
    (root / "Private").mkdir(parents=True)
    (root / "Public").mkdir(parents=True)

    for target in (settings, tools.settings, indexer.settings):
        monkeypatch.setattr(target, "vault_path", str(root), raising=False)
    monkeypatch.setattr(indexer.settings, "multi_user_mode", False, raising=False)
    monkeypatch.setattr(indexer.settings, "index_stat_shortcut", True, raising=False)
    monkeypatch.setattr(indexer, "async_session", maker)
    monkeypatch.setattr(tools, "async_session", maker)
    monkeypatch.setattr(src.database, "async_session", maker)
    monkeypatch.setattr(indexer, "_is_paused", lambda: False)
    monkeypatch.setattr(indexer, "_refuse_quarantined_pass", lambda *_a, **_k: None)
    # Files written by the test are seconds old; measure racy recency as if the
    # pass ran ten seconds from now, so their stats are recorded.
    monkeypatch.setattr(
        indexer, "_wall_clock_ns", lambda: time.time_ns() + 10_000_000_000
    )

    provider = {"n": 0, "fail": False}

    async def fake_batch(chunks):
        provider["n"] += 1
        if provider["fail"]:
            raise RuntimeError("provider down")
        return [[1.0] + [0.0] * (DIM - 1) for _ in chunks]

    monkeypatch.setattr(embeddings_service, "get_embeddings_batch", fake_batch)

    async def noop(*_a, **_k):
        return None

    monkeypatch.setattr(tools, "_log_usage", noop)
    permission = current_permission.set("readwrite")

    async with maker() as session:
        await session.execute(text("DELETE FROM note_embeddings"))
        await session.execute(text("DELETE FROM notes_metadata"))
        await session.execute(text("DELETE FROM indexer_state"))
        await session.commit()
    try:
        yield {
            "root": root,
            "maker": maker,
            "engine": engine,
            "patterns": _Patterns(monkeypatch),
            "provider": provider,
        }
    finally:
        current_permission.reset(permission)


async def rows(maker):
    async with maker() as session:
        result = await session.execute(
            select(
                NoteMetadata.id,
                NoteMetadata.file_path,
                NoteMetadata.content_hash,
                NoteMetadata.embedded_content_hash,
                NoteMetadata.stat_size,
            ).order_by(NoteMetadata.file_path)
        )
        return {r.file_path: r for r in result}


async def vectors(maker, note_id):
    async with maker() as session:
        return len((await session.execute(
            select(NoteEmbedding.id).where(NoteEmbedding.note_id == note_id)
        )).all())


def after_the_walk(monkeypatch, hook):
    """Run `hook` after the walk and before the locked transaction opens —
    the window in which another process can commit under the pass."""
    real = indexer._run_scan

    async def wrapped(*args, **kwargs):
        result = await real(*args, **kwargs)
        await hook()
        return result

    monkeypatch.setattr(indexer, "_run_scan", wrapped)


def block_the_walk(monkeypatch):
    """Hold the scan thread inside its first read until released."""
    reading = threading.Event()
    release = threading.Event()
    real = indexer.read_note_at

    def blocked(parent_fd, name):
        reading.set()
        release.wait(30)
        return real(parent_fd, name)

    monkeypatch.setattr(indexer, "read_note_at", blocked)
    return reading, release


async def wait_for(flag: threading.Event):
    for _ in range(3000):
        if flag.is_set():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("the walk never started")


# ══════════════════════════════════════════════════════════════════════════
# C5 and C4
# ══════════════════════════════════════════════════════════════════════════


async def test_a_move_committed_mid_walk_is_not_pruned_and_settles_next_pass(
    world, monkeypatch
):
    root, maker = world["root"], world["maker"]
    (root / "Public" / "A.md").write_text(BODY, encoding="utf-8")
    await indexer.index_vault()
    before = await rows(maker)
    note_id = before["Public/A.md"].id
    assert before["Public/A.md"].stat_size is not None

    async def move():
        result = await tools.move_note_impl("Public/A.md", "Public/B.md")
        assert "Moved" in result, result
        # `move_note` NULLs the stat (D10): the next pass reads the new path.
        moved = await rows(maker)
        assert moved["Public/B.md"].stat_size is None

    real_scan = indexer._run_scan
    after_the_walk(monkeypatch, move)
    await indexer.index_vault()
    monkeypatch.setattr(indexer, "_run_scan", real_scan)

    after = await rows(maker)
    assert list(after) == ["Public/B.md"], after
    assert after["Public/B.md"].id == note_id, (
        "the moved row was pruned or re-inserted on the strength of an old walk"
    )

    # The next pass sees the file at its new path and settles the row: same
    # id, hash unchanged, stat recorded again by the unchanged-hash refresh.
    await indexer.index_vault()
    settled = await rows(maker)
    assert list(settled) == ["Public/B.md"]
    assert settled["Public/B.md"].id == note_id
    assert settled["Public/B.md"].content_hash == content_hash(BODY)
    assert settled["Public/B.md"].stat_size is not None


async def test_a_move_committed_during_the_walk_that_it_sees_neither_side_of_is_deferred(
    world, monkeypatch, caplog
):
    """C5, the literal scenario (verifier, wave 1). The snapshot is taken; a
    `move_note` commits **while the walk is running**, at the one point where
    the walk sees neither path: `Private/` has already been listed (without
    the new path) and `Public/` has not been yet (it will list without the
    old one). The walk is held inside its read of `Private/Sentinel.md` to make
    that interleaving deterministic. The moved row is absent from the snapshot
    and unseen by the walk, so it must be neither pruned nor paired, only
    deferred; the next pass settles it."""
    import logging

    root, maker = world["root"], world["maker"]
    (root / "Private" / "Sentinel.md").write_text("sentinel\n", encoding="utf-8")
    (root / "Public" / "A.md").write_text(BODY, encoding="utf-8")
    await indexer.index_vault()
    before = await rows(maker)
    note_id = before["Public/A.md"].id
    sentinel_id = before["Private/Sentinel.md"].id

    in_private = threading.Event()
    release = threading.Event()
    walked: list[str] = []
    real = indexer.read_note_at

    def held(parent_fd, name):
        walked.append(name)
        if name == "Sentinel.md" and not in_private.is_set():
            in_private.set()
            release.wait(30)
        return real(parent_fd, name)

    monkeypatch.setattr(indexer, "read_note_at", held)

    # A full-hash pass, so the walk reads the sentinel (and blocks there).
    pass_task = asyncio.create_task(indexer.index_vault(full_hash=True))
    try:
        await wait_for(in_private)
        result = await tools.move_note_impl("Public/A.md", "Private/A.md")
        assert "Moved" in result, result
    finally:
        release.set()
    with caplog.at_level(logging.INFO, logger=indexer.logger.name):
        await asyncio.wait_for(pass_task, timeout=60)

    assert walked == ["Sentinel.md"], (
        f"the walk saw a side of the move, so the interleaving did not happen: {walked}"
    )
    after = await rows(maker)
    assert sorted(after) == ["Private/A.md", "Private/Sentinel.md"], after
    assert after["Private/A.md"].id == note_id, (
        "the moved row was pruned, re-inserted or paired on the strength of a "
        "walk older than it"
    )
    assert after["Private/A.md"].content_hash == content_hash(BODY)
    assert after["Private/Sentinel.md"].id == sentinel_id
    assert any(
        "Deferring Private/A.md to the next pass" in r.getMessage()
        for r in caplog.records
    ), "the unseen, changed row was not deferred"

    monkeypatch.setattr(indexer, "read_note_at", real)
    await indexer.index_vault()
    settled = await rows(maker)
    assert sorted(settled) == ["Private/A.md", "Private/Sentinel.md"]
    assert settled["Private/A.md"].id == note_id
    assert settled["Private/A.md"].stat_size is not None


async def test_a_row_another_process_changed_is_re_decided_under_the_lock(
    world, monkeypatch
):
    root, maker = world["root"], world["maker"]
    (root / "Public" / "A.md").write_text(BODY, encoding="utf-8")
    await indexer.index_vault()  # backstop: records the stat

    reads: list[str] = []
    real = indexer.read_note_at

    def counting(parent_fd, name):
        reads.append(name)
        return real(parent_fd, name)

    monkeypatch.setattr(indexer, "read_note_at", counting)

    async def other_process_upsert():
        # Another container's pass commits a different hash (and no stat)
        # for the very path this walk just shortcut-skipped.
        async with maker() as session:
            await session.execute(text(
                "UPDATE notes_metadata SET content_hash = 'deadbeef', "
                "stat_size = NULL, stat_mtime_ns = NULL, stat_ctime_ns = NULL, "
                "stat_ino = NULL WHERE file_path = 'Public/A.md'"
            ))
            await session.commit()

    after_the_walk(monkeypatch, other_process_upsert)
    await indexer.index_vault()  # not a backstop: the walk shortcut-skips A

    assert reads == ["A.md"], (
        "the path was not re-read under the lock after its row changed: "
        f"{reads}"
    )
    row = (await rows(maker))["Public/A.md"]
    assert row.content_hash == content_hash(BODY), (
        "the decision was taken against the snapshot, not the locked row"
    )
    assert row.stat_size is not None


# ══════════════════════════════════════════════════════════════════════════
# C2: no transaction spans the walk
# ══════════════════════════════════════════════════════════════════════════


async def test_no_transaction_is_open_while_the_walk_runs(world, monkeypatch):
    root, engine = world["root"], world["engine"]
    (root / "Public" / "A.md").write_text(BODY, encoding="utf-8")
    reading, release = block_the_walk(monkeypatch)

    pass_task = asyncio.create_task(indexer.index_vault())
    try:
        await wait_for(reading)
        probe = create_async_engine(engine.url, poolclass=None)
        try:
            async with probe.connect() as conn:
                open_xacts = (await conn.execute(text(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE datname = current_database() "
                    "  AND pid <> pg_backend_pid() "
                    "  AND xact_start IS NOT NULL"
                ))).scalar_one()
        finally:
            await probe.dispose()
        assert open_xacts == 0, (
            f"{open_xacts} transaction(s) open while the walk ran — the "
            "snapshot must commit before the walk (C2)"
        )
    finally:
        release.set()
        await asyncio.wait_for(pass_task, timeout=60)


# ══════════════════════════════════════════════════════════════════════════
# A reset racing the walk
# ══════════════════════════════════════════════════════════════════════════


async def test_a_reset_during_the_walk_neither_deadlocks_nor_lets_old_decisions_land(
    world, monkeypatch
):
    """The reset commits while the walk is still reading, then holds the lock
    as the pass reaches it. The pass waits, then decides against the rows the
    reset left — none — so every walked file is re-read and re-inserted, and
    nothing the walk concluded about the old rows is written."""
    root, maker = world["root"], world["maker"]
    for name in ("A", "B"):
        (root / "Public" / f"{name}.md").write_text(f"{name} {BODY}", encoding="utf-8")
    await indexer.index_vault()
    old_ids = {r.id for r in (await rows(maker)).values()}

    reading, release = block_the_walk(monkeypatch)
    reset_holding = asyncio.Event()

    async def reset():
        async with maker() as session:
            await index_state.acquire_generation_lock_unbounded(session)
            await session.execute(text("DELETE FROM notes_metadata"))
            reset_holding.set()
            await asyncio.sleep(0.5)
            await session.commit()

    # A full-hash pass, so the walk reads (and blocks) rather than trusting
    # the stats the first pass recorded.
    pass_task = asyncio.create_task(indexer.index_vault(full_hash=True))
    await wait_for(reading)
    reset_task = asyncio.create_task(reset())
    await reset_holding.wait()
    release.set()
    await asyncio.wait_for(asyncio.gather(pass_task, reset_task), timeout=60)

    after = await rows(maker)
    assert sorted(after) == ["Public/A.md", "Public/B.md"]
    assert not ({r.id for r in after.values()} & old_ids)
    for name in ("A", "B"):
        assert after[f"Public/{name}.md"].content_hash == content_hash(f"{name} {BODY}")


# ══════════════════════════════════════════════════════════════════════════
# The exclusion-sweep gate (D13)
# ══════════════════════════════════════════════════════════════════════════


def count_sweeps(engine):
    """Count the sweep's discovery query on the wire."""
    counter = {"n": 0}

    def _record(conn, cursor, statement, parameters, context, executemany):
        if "exists (" in statement.lower() and "note_embeddings ne" in statement.lower():
            counter["n"] += 1

    event.listen(engine.sync_engine, "before_cursor_execute", _record)
    return counter, lambda: event.remove(
        engine.sync_engine, "before_cursor_execute", _record
    )


async def test_the_sweep_is_skipped_after_a_clean_one_and_repeats_when_it_must(
    world,
):
    root, maker, provider = world["root"], world["maker"], world["provider"]
    (root / "Public" / "A.md").write_text(BODY, encoding="utf-8")
    await indexer.index_vault()
    sweeps, stop = count_sweeps(world["engine"])
    try:
        await indexer.embed_vault()
        assert sweeps["n"] == 1, "the first embed pass after start must sweep"

        await indexer.embed_vault()
        assert sweeps["n"] == 1, "a clean sweep under unchanged patterns ran again"

        # A row the sweep must repair — certification current, no vectors
        # (a stale exclusion stamp) — and a provider that fails on it.
        note_id = (await rows(maker))["Public/A.md"].id
        async with maker() as session:
            await session.execute(text(
                "DELETE FROM note_embeddings WHERE note_id = :i"
            ), {"i": note_id})
            await session.commit()
        indexer.clear_sweep_state()
        provider["fail"] = True
        await indexer.embed_vault()
        assert sweeps["n"] == 2
        assert await vectors(maker, note_id) == 0

        provider["fail"] = False
        await indexer.embed_vault()
        assert sweeps["n"] == 3, "a sweep with a provider failure was recorded clean"
        assert await vectors(maker, note_id) > 0

        await indexer.embed_vault()
        assert sweeps["n"] == 3

        # A backstop pass forces the sweep whatever the record says.
        await indexer.index_vault(full_hash=True)
        await indexer.embed_vault()
        assert sweeps["n"] == 4
    finally:
        stop()


async def test_an_edit_undone_between_scan_and_sweep_does_not_hide_a_note(
    world, monkeypatch
):
    """A→B→A. The excluded note (content A, certified with zero vectors) has
    its pattern removed, then the process restarts. The scan reads A. Before
    the sweep reaches the note it is saved as B, so the sweep skips it on a
    hash mismatch; it is restored to A before the next scan. The row is then
    hash-equal and certification-current, so the backlog never selects it —
    only a sweep that did not record itself clean can still repair it."""
    root, maker, patterns = world["root"], world["maker"], world["patterns"]
    path = root / "Private" / "A.md"
    body_a = BODY
    body_b = "B" * (len(BODY) - 1) + "\n"
    path.write_text(body_a, encoding="utf-8")

    patterns.set(["Private/*"])
    await indexer.index_vault()
    await indexer.embed_vault()
    note = (await rows(maker))["Private/A.md"]
    assert note.embedded_content_hash == content_hash(body_a)
    assert await vectors(maker, note.id) == 0

    # The pattern is removed; a restart forgets both in-memory records.
    patterns.set([])
    indexer._last_full_hash.clear()
    indexer.clear_sweep_state()
    await indexer.index_vault()  # reads A: the row is unchanged

    real = indexer.read_note_beneath
    edits = {"done": False}

    def save_b_first(root_fd, rel):
        if rel == "Private/A.md" and not edits["done"]:
            edits["done"] = True
            path.write_text(body_b, encoding="utf-8")
        return real(root_fd, rel)

    monkeypatch.setattr(indexer, "read_note_beneath", save_b_first)
    await indexer.embed_vault()
    monkeypatch.setattr(indexer, "read_note_beneath", real)
    assert edits["done"], "the sweep never reached the note; the race did not happen"
    assert await vectors(maker, note.id) == 0
    assert None not in indexer._swept, (
        "a sweep with a hash-mismatch skip recorded itself clean"
    )

    path.write_text(body_a, encoding="utf-8")  # undo, before the next scan
    await indexer.index_vault()
    after = (await rows(maker))["Private/A.md"]
    assert after.content_hash == content_hash(body_a)
    assert after.embedded_content_hash == content_hash(body_a)

    await indexer.embed_vault()
    assert await vectors(maker, note.id) > 0, (
        "the now-included note stayed absent from semantic search"
    )
