"""#278 — whole-vault filesystem work and large-note cleaning run off the loop.

The walk, the stat, the read and the SHA-256 used to run on the event loop
inside the index pass: a cold pass over the production vault froze every
request — `/health` included — for 235 s. The acceptance criterion is binary
and is asserted as such here: while a pass is reading, the application keeps
serving `/health` and a concurrent coroutine keeps making progress. No timing
bound is asserted; a timing assertion against a concurrent request is a flake
generator on a shared runner.

Also here: the three embed paths (the backlog, the exclusion sweep's probe and
`embed_note`) execute `parse_frontmatter`, `clean_for_embedding` and the
bounded chunker on a worker thread; and cancelling the pass stops the scan
thread before its next file.
"""
from __future__ import annotations

import asyncio
import threading
import time
from types import SimpleNamespace

import pytest
from sqlalchemy.sql.elements import TextClause

from src.services import embeddings, indexer
from src.services.embeddings import EmbedNoteResult, NoteEmbedOutcome
from tests.test_perf_stat_shortcut import FakeIndexDB, install


def _off_main(record: list, label: str):
    record.append((label, threading.current_thread() is threading.main_thread()))


def _wrap(monkeypatch, module, name, record, label=None):
    real = getattr(module, name)

    def wrapped(*args, **kwargs):
        _off_main(record, label or name)
        return real(*args, **kwargs)

    monkeypatch.setattr(module, name, wrapped)


@pytest.fixture
def vault(tmp_path):
    root = tmp_path / "vault"
    root.mkdir()
    return root


# ══════════════════════════════════════════════════════════════════════════
# The scan
# ══════════════════════════════════════════════════════════════════════════


async def test_the_scan_reads_and_hashes_on_a_worker_thread(monkeypatch, vault):
    (vault / "A.md").write_text("alpha\n", encoding="utf-8")
    (vault / "Sub").mkdir()
    (vault / "Sub" / "B.md").write_text("beta\n", encoding="utf-8")
    db = FakeIndexDB()
    install(monkeypatch, db, vault)

    record: list = []
    _wrap(monkeypatch, indexer, "read_note_at", record, "read")
    _wrap(monkeypatch, indexer, "_content_hash", record, "hash")
    _wrap(monkeypatch, indexer, "parse_frontmatter", record, "parse")

    await indexer.index_vault()

    assert {label for label, _ in record} == {"read", "hash", "parse"}
    on_loop = [label for label, on_main in record if on_main]
    assert on_loop == [], f"ran on the event loop's thread: {on_loop}"
    assert sorted(db.rows) == ["A.md", "Sub/B.md"]


async def test_health_answers_and_the_loop_progresses_during_a_pass(
    monkeypatch, vault
):
    """The acceptance criterion: with every read blocking for 2 s, `/health`
    is answered and a concurrent coroutine iterates **while the scan is still
    in progress**. Binary progress assertions, no timing bound."""
    import httpx

    from src.main import app

    for i in range(2):
        (vault / f"N{i}.md").write_text(f"note {i}\n", encoding="utf-8")
    db = FakeIndexDB()
    install(monkeypatch, db, vault)

    reading = threading.Event()
    real = indexer.read_note_at

    def blocking_read(parent_fd, name):
        reading.set()
        time.sleep(2)
        return real(parent_fd, name)

    monkeypatch.setattr(indexer, "read_note_at", blocking_read)

    ticks = 0
    stop_ticking = False

    async def ticker():
        nonlocal ticks
        while not stop_ticking:
            ticks += 1
            await asyncio.sleep(0.01)

    pass_task = asyncio.create_task(indexer.index_vault())
    tick_task = asyncio.create_task(ticker())
    try:
        while not reading.is_set():
            await asyncio.sleep(0.01)
        ticks_at_read = ticks
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=("203.0.113.7", 4242)),
            base_url="http://localhost:8000",
        ) as client:
            response = await client.get("/health")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"
        await asyncio.sleep(0.05)
        assert not pass_task.done(), "the pass finished before the probe; no evidence"
        assert ticks > ticks_at_read, "the event loop made no progress during the scan"
    finally:
        stop_ticking = True
        await tick_task
        await pass_task
    assert sorted(db.rows) == ["N0.md", "N1.md"]


async def test_cancellation_stops_the_scan_before_its_next_file(monkeypatch, vault):
    for i in range(6):
        (vault / f"N{i}.md").write_text(f"note {i}\n", encoding="utf-8")
    db = FakeIndexDB()
    install(monkeypatch, db, vault)

    started: list[str] = []
    first_read = threading.Event()
    real = indexer.read_note_at

    def slow_read(parent_fd, name):
        started.append(name)
        first_read.set()
        time.sleep(0.5)
        return real(parent_fd, name)

    monkeypatch.setattr(indexer, "read_note_at", slow_read)

    stops: list[threading.Event] = []
    finished = threading.Event()
    real_scan = indexer._scan_vault

    def recording_scan(*args, stop, **kwargs):
        stops.append(stop)
        try:
            return real_scan(*args, stop=stop, **kwargs)
        finally:
            finished.set()

    monkeypatch.setattr(indexer, "_scan_vault", recording_scan)

    task = asyncio.create_task(indexer.index_vault())
    while not first_read.is_set():
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert stops and stops[0].is_set(), "cancellation did not set the stop event"
    # The thread was awaited before the cancellation propagated, so the walk is
    # over — not still running against a root descriptor about to be closed.
    assert finished.is_set()
    assert started == ["N0.md"], f"the scan kept reading after cancel: {started}"
    assert db.upserts == [], "a cancelled pass wrote rows"
    assert indexer._full_hash_due(None)


# ══════════════════════════════════════════════════════════════════════════
# The three embed paths
# ══════════════════════════════════════════════════════════════════════════


class _EmbedResult:
    def __init__(self, rows=()):
        self.rows = list(rows)

    def fetchall(self):
        return self.rows

    def scalar_one(self):
        return SimpleNamespace(id=1, chunks_truncated=False)

    def scalar(self):
        return None

    def scalar_one_or_none(self):
        return None


class _EmbedSession:
    """Serves the backlog query and the sweep query, inert otherwise."""

    def __init__(self, backlog, sweep):
        self.backlog = backlog
        self.sweep = sweep

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def commit(self):
        pass

    async def rollback(self):
        pass

    async def execute(self, stmt, params=None):
        if isinstance(stmt, TextClause):
            if "EXISTS" in stmt.text:
                return _EmbedResult(self.sweep)
            if "embedded_content_hash IS NULL" in stmt.text:
                return _EmbedResult(self.backlog)
        return _EmbedResult()


def _embed_world(monkeypatch, vault, *, backlog, sweep):
    session = _EmbedSession(backlog, sweep)
    monkeypatch.setattr(indexer, "async_session", lambda: session)
    monkeypatch.setattr(indexer, "_vault_root", lambda _uid: vault)
    monkeypatch.setattr(indexer, "_refuse_quarantined_pass", lambda *_a, **_k: None)
    monkeypatch.setattr(indexer, "_is_paused", lambda: False)
    monkeypatch.setattr(indexer.settings, "embedding_exclude_patterns", [], raising=False)

    async def current(*_a, **_k):
        return True

    async def budget(*_a, **_k):
        return indexer.EmbedBudget()

    monkeypatch.setattr(indexer, "_embedding_generation_current", current)
    monkeypatch.setattr(indexer, "_budget_for_pass", budget)

    embedded: list[str] = []

    async def fake_embed_note(_session, _note, content, **_kwargs):
        embedded.append(content)
        return EmbedNoteResult(
            outcome=NoteEmbedOutcome.EMBEDDED,
            chunks_submitted=1,
            chunks_embedded=1,
            truncated=False,
        )

    monkeypatch.setattr(indexer, "embed_note", fake_embed_note)
    return embedded


async def test_the_backlog_reads_hashes_and_parses_off_the_loop(monkeypatch, vault):
    body = "---\ntitle: T\n---\nsome body text\n"
    (vault / "A.md").write_text(body, encoding="utf-8")
    row = SimpleNamespace(
        id=1, file_path="A.md", content_hash=indexer._content_hash(body),
        chunks_truncated=False,
    )
    embedded = _embed_world(monkeypatch, vault, backlog=[row], sweep=[])
    record: list = []
    _wrap(monkeypatch, indexer, "read_note_beneath", record, "read")
    _wrap(monkeypatch, indexer, "_content_hash", record, "hash")
    _wrap(monkeypatch, indexer, "parse_frontmatter", record, "parse")

    await indexer.embed_vault()

    assert embedded == ["some body text\n"]
    assert {label for label, _ in record} == {"read", "hash", "parse"}
    assert [label for label, on_main in record if on_main] == []


async def test_the_sweep_probe_cleans_and_chunks_off_the_loop(monkeypatch, vault):
    body = "a body long enough to chunk\n"
    (vault / "A.md").write_text(body, encoding="utf-8")
    row = SimpleNamespace(
        id=1, file_path="A.md", content_hash=indexer._content_hash(body),
        chunks_truncated=False, has_vectors=False,
    )
    embedded = _embed_world(monkeypatch, vault, backlog=[], sweep=[row])
    record: list = []
    for name in ("read_note_beneath", "_content_hash", "parse_frontmatter",
                 "clean_for_embedding", "chunk_text_bounded"):
        _wrap(monkeypatch, indexer, name, record)

    await indexer.embed_vault()

    assert embedded == [body], "the included note with no vectors was not repaired"
    assert {label for label, _ in record} == {
        "read_note_beneath", "_content_hash", "parse_frontmatter",
        "clean_for_embedding", "chunk_text_bounded",
    }
    assert [label for label, on_main in record if on_main] == []


async def test_embed_note_cleans_and_chunks_off_the_loop(monkeypatch):
    record: list = []
    _wrap(monkeypatch, embeddings, "clean_for_embedding", record)

    def no_chunks(*_a, **_k):
        _off_main(record, "chunk_text_bounded")
        return [], False

    monkeypatch.setattr(embeddings, "chunk_text_bounded", no_chunks)

    class _Session:
        async def execute(self, *_a, **_k):
            return None

        async def flush(self):
            pass

    note = SimpleNamespace(id=1, content_hash="h", embedded_content_hash=None)
    result = await embeddings.embed_note(_Session(), note, "body")

    assert result.outcome is NoteEmbedOutcome.CERTIFIED_EMPTY
    assert {label for label, _ in record} == {"clean_for_embedding", "chunk_text_bounded"}
    assert [label for label, on_main in record if on_main] == []


# ══════════════════════════════════════════════════════════════════════════
# The other two whole-vault reads: the link backfill and the keyword rebuild
# ══════════════════════════════════════════════════════════════════════════


def _blocking_beneath(monkeypatch, record: list):
    """`read_note_beneath` that blocks for 0.5 s and records its thread."""
    reading = threading.Event()
    real = indexer.read_note_beneath

    def blocking(root_fd, rel):
        _off_main(record, "read")
        reading.set()
        time.sleep(0.5)
        return real(root_fd, rel)

    monkeypatch.setattr(indexer, "read_note_beneath", blocking)
    return reading


async def _loop_progresses_while(reading: threading.Event, coro):
    """Run `coro`; once its read has started, a concurrent coroutine must
    iterate while it is still running. Binary, no timing bound."""
    ticks = 0
    stop_ticking = False

    async def ticker():
        nonlocal ticks
        while not stop_ticking:
            ticks += 1
            await asyncio.sleep(0.01)

    task = asyncio.create_task(coro)
    tick_task = asyncio.create_task(ticker())
    try:
        while not reading.is_set():
            await asyncio.sleep(0.01)
        ticks_at_read = ticks
        await asyncio.sleep(0.1)
        assert not task.done(), "the work finished before the probe; no evidence"
        assert ticks > ticks_at_read, "the event loop made no progress during the read"
    finally:
        stop_ticking = True
        await tick_task
    return await task


class _BackfillResult:
    def __init__(self, rows=()):
        self._rows = list(rows)

    def scalar(self):
        return 0

    def all(self):
        return self._rows


class _BackfillSession:
    def __init__(self, rows):
        self.rows = rows
        self.inserts = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def execute(self, stmt, *_a, **_k):
        from sqlalchemy.sql.dml import Insert

        if isinstance(stmt, Insert):
            self.inserts += 1
        return _BackfillResult(self.rows)

    async def commit(self):
        pass


async def test_the_link_backfill_reads_and_parses_off_the_loop(monkeypatch, vault):
    import os

    (vault / "A.md").write_text("---\ntitle: A\n---\nsee [[B]]\n", encoding="utf-8")
    session = _BackfillSession([SimpleNamespace(id=1, file_path="A.md")])
    monkeypatch.setattr(indexer, "async_session", lambda: session)

    async def permitted(*_a, **_k):
        return True

    monkeypatch.setattr(indexer, "_ancillary_pass_is_permitted", permitted)
    record: list = []
    reading = _blocking_beneath(monkeypatch, record)
    _wrap(monkeypatch, indexer, "parse_frontmatter", record, "parse")

    root_fd = os.open(vault, os.O_RDONLY | os.O_DIRECTORY)
    try:
        stats = indexer.PassStats()
        await _loop_progresses_while(
            reading, indexer._link_backfill_pinned(None, vault, root_fd, stats)
        )
    finally:
        os.close(root_fd)

    assert {label for label, _ in record} == {"read", "parse"}
    assert [label for label, on_main in record if on_main] == []
    assert stats.notes_indexed == 1 and session.inserts == 1


async def test_the_keyword_rebuild_reads_parses_and_hashes_off_the_loop(
    monkeypatch, vault
):
    body = "---\ntitle: A\n---\nalpha body\n"
    (vault / "A.md").write_text(body, encoding="utf-8")
    monkeypatch.setattr(indexer.settings, "vault_path", str(vault), raising=False)
    monkeypatch.setattr(indexer.settings, "fts_configs", ["simple"], raising=False)
    rows = [SimpleNamespace(
        id=1, user_id=None, file_path="A.md", content_hash=indexer._content_hash(body)
    )]

    class _Result:
        rowcount = 1

        def all(self):
            return rows

    class _Savepoint:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

    class _Session:
        def __init__(self):
            self.updates = 0

        def begin_nested(self):
            return _Savepoint()

        async def execute(self, stmt, params=None):
            if isinstance(stmt, TextClause) and "content_tsvector" in stmt.text:
                self.updates += 1
            return _Result()

        async def commit(self):
            pass

    record: list = []
    reading = _blocking_beneath(monkeypatch, record)
    _wrap(monkeypatch, indexer, "parse_frontmatter", record, "parse")
    _wrap(monkeypatch, indexer, "_content_hash", record, "hash")

    session = _Session()
    n = await _loop_progresses_while(
        reading,
        indexer._rebuild_tsvectors_single_scope_for_tests(session, user_id=None),
    )

    assert n == 1 and session.updates == 1
    assert {label for label, _ in record} == {"read", "parse", "hash"}
    assert [label for label, on_main in record if on_main] == []
