"""`note_tasks` is derived state with `note_links`' lifecycle (#350, D1/D4/D5).

Against a real PostgreSQL: the index pass writes task rows beside the link
rebuild, in the pass's transaction; they follow the note through edits,
deletes, both move paths, the unlistable-directory guard and a provenance
discard; and a task-derivation failure is isolated to that note's task rows
(`tasks_failed`) at every entry point — the periodic pass, the startup
full-hash pass and the panel's manual reindex — without quarantining,
pruning or failing anything. Failures are planted with `_poison`'s `task`
site (a real statement error inside the savepoint) or a monkeypatched
extractor.

Skipped unless `PGVECTOR_TEST_ADMIN_URL` is set (see `_harness`).
"""
import asyncio
import contextlib
import datetime
import hashlib
import os

import pytest
import pytest_asyncio
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import src.database
import src.mcp_server.tools as tools
from src.config import settings
from src.mcp_server.auth import current_permission
from src.models.db import NoteEmbedding, NoteLink, NoteMetadata, NoteTask, User
from src.services import embeddings as embeddings_service
from src.services import indexer, indexer_health
from src.services import vault as vault_service
from src.services.search import full_text_search
import _harness
import _poison

pytestmark = [
    _harness.requires_pgvector,
    pytest.mark.asyncio(loop_scope="module"),
]

DIM = 8
VECTOR = [1.0] + [0.0] * (DIM - 1)
running_as_root = hasattr(os, "geteuid") and os.geteuid() == 0


def sha(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


@pytest.fixture(scope="module")
def migrated_url():
    yield from _harness.throwaway_database("note_tasks_350", DIM)


@pytest_asyncio.fixture(loop_scope="module", scope="module")
async def sessionmaker(migrated_url):
    engine = create_async_engine(migrated_url, poolclass=None)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield maker
    await engine.dispose()


#: Bodies whose extraction raises, mutable per test.
EXPLODE: set[str] = set()


@pytest_asyncio.fixture(loop_scope="module")
async def vault(sessionmaker, monkeypatch, tmp_path):
    root = tmp_path / "vault"
    root.mkdir()
    monkeypatch.setattr(settings, "vault_path", str(root), raising=False)
    monkeypatch.setattr(indexer.settings, "vault_path", str(root), raising=False)
    monkeypatch.setattr(tools.settings, "vault_path", str(root), raising=False)
    monkeypatch.setattr(indexer.settings, "multi_user_mode", False, raising=False)
    monkeypatch.setattr(indexer.settings, "embedding_exclude_patterns", [], raising=False)
    monkeypatch.setattr(indexer, "async_session", sessionmaker)
    monkeypatch.setattr(tools, "async_session", sessionmaker)
    monkeypatch.setattr(src.database, "async_session", sessionmaker)
    monkeypatch.setattr(embeddings_service, "async_session", sessionmaker, raising=False)
    monkeypatch.setattr(indexer, "_is_paused", lambda: False)

    async def fake_batch(chunks, **_kw):
        return [list(VECTOR) for _ in chunks]

    async def fake_one(_text, **_kw):
        return list(VECTOR)

    async def noop(*_a, **_k):
        return None

    monkeypatch.setattr(embeddings_service, "get_embeddings_batch", fake_batch)
    monkeypatch.setattr(embeddings_service, "get_embedding", fake_one)
    monkeypatch.setattr(tools, "_log_usage", noop)

    real_extract = indexer.extract_tasks_bounded

    def exploding(body, **kw):
        if any(marker in body for marker in EXPLODE):
            raise RuntimeError("synthetic extractor bug")
        return real_extract(body, **kw)

    monkeypatch.setattr(indexer, "extract_tasks_bounded", exploding)
    EXPLODE.clear()
    indexer._last_full_hash.clear()
    indexer.clear_quarantine()
    indexer_health.reset()
    vault_service.clear_user_vault_cache()
    permission = current_permission.set("readwrite")

    async with sessionmaker() as session:
        for table in ("note_tasks", "note_links", "note_embeddings", "notes_metadata",
                      "indexer_runs", "users"):
            await session.execute(text(f"DELETE FROM {table}"))
        await session.commit()
    await _poison.install(sessionmaker)

    try:
        yield root
    finally:
        current_permission.reset(permission)
        await _poison.uninstall(sessionmaker)
        EXPLODE.clear()
        indexer._last_full_hash.clear()
        indexer.clear_quarantine()
        indexer_health.reset()


# ── helpers ───────────────────────────────────────────────────────────────


def write(root, rel: str, body: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


async def note(sessionmaker, path: str, uid: int | None = None):
    owner = NoteMetadata.user_id.is_(None) if uid is None else NoteMetadata.user_id == uid
    async with sessionmaker() as session:
        return (await session.execute(
            select(NoteMetadata).where(NoteMetadata.file_path == path, owner)
        )).scalar_one_or_none()


async def tasks_of(sessionmaker, path: str, uid: int | None = None) -> list[str]:
    owner = NoteMetadata.user_id.is_(None) if uid is None else NoteMetadata.user_id == uid
    async with sessionmaker() as session:
        return list((await session.execute(
            select(NoteTask.text)
            .join(NoteMetadata, NoteMetadata.id == NoteTask.note_id)
            .where(NoteMetadata.file_path == path, owner)
            .order_by(NoteTask.line)
        )).scalars().all())


async def all_task_note_ids(sessionmaker) -> set[int]:
    async with sessionmaker() as session:
        return set((await session.execute(select(NoteTask.note_id))).scalars().all())


async def links_of(sessionmaker, note_id: int) -> list[str]:
    async with sessionmaker() as session:
        return sorted((await session.execute(
            select(NoteLink.target_path).where(NoteLink.source_note_id == note_id)
        )).scalars().all())


async def embedding_count(sessionmaker, note_id: int) -> int:
    async with sessionmaker() as session:
        return len((await session.execute(
            select(NoteEmbedding.id).where(NoteEmbedding.note_id == note_id)
        )).scalars().all())


async def keyword(sessionmaker, query: str) -> list[str]:
    async with sessionmaker() as session:
        return [r["path"] for r in await full_text_search(session, query)]


async def tenant(sessionmaker, root, name: str) -> int:
    root.mkdir(parents=True, exist_ok=True)
    async with sessionmaker() as session:
        user = User(username=name, password_hash="x", vault_path=str(root))
        session.add(user)
        await session.commit()
        await vault_service.warm_user_vault_cache(session, user_id=user.id)
        return user.id


async def reassign(sessionmaker, uid: int, root) -> None:
    async with sessionmaker() as session:
        await session.execute(update(User).where(User.id == uid).values(vault_path=str(root)))
        await session.commit()
        vault_service.clear_user_vault_cache(user_id=uid)
        await vault_service.warm_user_vault_cache(session, user_id=uid)


@contextlib.contextmanager
def unreadable(path):
    os.chmod(path, 0)
    try:
        yield
    finally:
        os.chmod(path, 0o755)


async def assert_isolated(sessionmaker, bad: str, *, others: dict[str, list[str]]):
    row = await note(sessionmaker, bad)
    assert row is not None, "the note keeps its row"
    assert row.tasks_failed is True
    assert await tasks_of(sessionmaker, bad) == []
    assert indexer.quarantined_paths(None) == []
    assert indexer.quarantine_count() == 0
    for path, expected in others.items():
        assert await tasks_of(sessionmaker, path) == expected, path
        assert (await note(sessionmaker, path)).tasks_failed is False


# ══════════════════════════════════════════════════════════════════════════
# Lifecycle
# ══════════════════════════════════════════════════════════════════════════


async def test_editing_a_note_replaces_its_tasks(sessionmaker, vault):
    write(vault, "n.md", "- [ ] A\n- [ ] B\n")
    await indexer.index_vault()
    assert await tasks_of(sessionmaker, "n.md") == ["A", "B"]
    write(vault, "n.md", "- [ ] B\n- [x] C\n")
    await indexer.index_vault()
    assert await tasks_of(sessionmaker, "n.md") == ["B", "C"]


async def test_frontmatter_lines_count_and_fields_land(sessionmaker, vault):
    write(vault, "f.md", "---\na: 1\nb: 2\n---\nfirst\n- [ ] ship 📅 2026-11-01 ⏫ #work\n")
    await indexer.index_vault()
    async with sessionmaker() as session:
        t = (await session.execute(select(NoteTask))).scalar_one()
    assert (t.line, t.due, t.priority, t.tags, t.state) == (
        6, datetime.date(2026, 11, 1), "high", ["work"], "open",
    )
    row = await note(sessionmaker, "f.md")
    assert row.extraction_version == indexer.CURRENT_EXTRACTION_VERSION == 4


async def test_deleting_a_note_removes_its_tasks(sessionmaker, vault):
    write(vault, "gone.md", "- [ ] doomed\n")
    write(vault, "stay.md", "- [ ] kept\n")
    await indexer.index_vault()
    gone_id = (await note(sessionmaker, "gone.md")).id
    (vault / "gone.md").unlink()
    await indexer.index_vault()
    assert gone_id not in await all_task_note_ids(sessionmaker)
    assert await tasks_of(sessionmaker, "stay.md") == ["kept"]


async def test_an_on_disk_rename_keeps_the_tasks_under_the_new_path(sessionmaker, vault):
    write(vault, "old.md", "- [ ] follow me\n")
    await indexer.index_vault()
    before = await note(sessionmaker, "old.md")
    (vault / "old.md").rename(vault / "new.md")
    await indexer.index_vault()
    after = await note(sessionmaker, "new.md")
    assert after.id == before.id, "the pass's id-preserving move"
    assert await tasks_of(sessionmaker, "new.md") == ["follow me"]
    out = await tools.list_tasks_impl()
    assert "`new.md`:1" in out and "old.md" not in out


async def test_move_note_keeps_the_tasks(sessionmaker, vault):
    write(vault, "m.md", "- [ ] carried\n")
    await indexer.index_vault()
    before = await note(sessionmaker, "m.md")
    result = await tools.move_note_impl("m.md", "Moved/m.md")
    assert "Moved" in str(result)
    after = await note(sessionmaker, "Moved/m.md")
    assert after.id == before.id
    out = await tools.list_tasks_impl()
    assert "`Moved/m.md`:1" in out and "`m.md`" not in out
    await indexer.index_vault()
    assert await tasks_of(sessionmaker, "Moved/m.md") == ["carried"]


@pytest.mark.skipif(running_as_root, reason="root ignores directory modes")
async def test_an_unlistable_directory_keeps_its_tasks(sessionmaker, vault):
    write(vault, "sub/a.md", "- [ ] protected\n")
    write(vault, "top.md", "- [ ] top\n")
    await indexer.index_vault()
    with unreadable(vault / "sub"):
        await indexer._index_pass_once(None)
        assert await tasks_of(sessionmaker, "sub/a.md") == ["protected"]


async def test_the_v3_to_v4_backfill_stamps_4_and_re_embeds_nothing(
    sessionmaker, vault, monkeypatch
):
    write(vault, "old.md", "body\n- [ ] backfilled\n[[x]]\n")
    await indexer.index_vault()
    await indexer.embed_vault()
    before = await note(sessionmaker, "old.md")
    assert before.embedded_content_hash == before.content_hash
    # What a version-3 build left: no task rows, stamped 3.
    async with sessionmaker() as session:
        await session.execute(text("DELETE FROM note_tasks"))
        await session.execute(text("UPDATE notes_metadata SET extraction_version = 3"))
        await session.commit()
    assert "not yet scanned for tasks: 1 notes" in await tools.list_tasks_impl()

    async def unexpected(_chunks, **_kw):
        pytest.fail("the version-4 bump must not call the embedding provider")

    monkeypatch.setattr(embeddings_service, "get_embeddings_batch", unexpected)
    await indexer.index_vault()
    await indexer.embed_vault()
    after = await note(sessionmaker, "old.md")
    assert after.extraction_version == 4
    assert after.embedded_content_hash == before.embedded_content_hash == after.content_hash
    assert await tasks_of(sessionmaker, "old.md") == ["backfilled"]
    assert "not yet scanned for tasks: 0 notes" in await tools.list_tasks_impl()


# ══════════════════════════════════════════════════════════════════════════
# Failure isolation (`index-integrity`)
# ══════════════════════════════════════════════════════════════════════════


async def test_the_periodic_pass_commits_around_a_failing_task_derivation(
    sessionmaker, vault
):
    for i in range(50):
        write(vault, f"n{i:02d}.md", f"word{i} [[n00]]\n- [ ] task {i}\n")
    await _poison.poison(sessionmaker, "task", "n07.md", "22001")
    result = await indexer.index_vault()
    assert result.quarantined == ()
    await indexer.embed_vault()
    await assert_isolated(
        sessionmaker, "n07.md",
        others={f"n{i:02d}.md": [f"task {i}"] for i in range(50) if i != 7},
    )
    bad = await note(sessionmaker, "n07.md")
    assert await links_of(sessionmaker, bad.id) == ["n00"]
    assert await embedding_count(sessionmaker, bad.id) >= 1
    assert "n07.md" in await keyword(sessionmaker, "word7")
    read = await tools.read_note_impl("n07.md")
    assert read.error is None
    out = await tools.list_tasks_impl(limit=500)
    assert "tasks could not be indexed: 1 notes" in out
    assert "Incomplete list:" in out


async def test_the_startup_pass_applies_the_same_isolation(sessionmaker, vault, monkeypatch):
    async def noop(*_a, **_k):
        return None

    monkeypatch.setattr(indexer, "detect_root_overlaps", noop)
    monkeypatch.setattr(indexer, "link_backfill_pass", noop)
    monkeypatch.setattr(indexer, "embed_vault", noop)
    monkeypatch.setattr(indexer.settings, "index_interval_seconds", 3600, raising=False)
    monkeypatch.setattr(indexer, "last_index_run_at", None)
    write(vault, "Good.md", "- [ ] good\n")
    write(vault, "Bad.md", "EXPLODE\n- [ ] bad\n")
    EXPLODE.add("EXPLODE")

    task = asyncio.create_task(indexer.run_indexer_loop())
    try:
        for _ in range(400):
            if indexer.last_index_run_at is not None or task.done():
                break
            await asyncio.sleep(0.05)
        assert indexer.last_index_run_at is not None
        assert indexer.last_index_run_ok is True
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    await assert_isolated(sessionmaker, "Bad.md", others={"Good.md": ["good"]})
    async with sessionmaker() as session:
        run = (await session.execute(text("SELECT trigger, error FROM indexer_runs"))).one()
    assert run.trigger == "startup" and run.error is None


async def test_the_manual_reindex_applies_the_same_isolation(sessionmaker, vault, monkeypatch):
    from src.control_panel import routes

    async def noop(*_a, **_k):
        return None

    monkeypatch.setattr(indexer, "detect_root_overlaps", noop)
    monkeypatch.setattr(indexer, "embed_vault", noop)
    monkeypatch.setattr(routes.settings, "multi_user_mode", False, raising=False)
    write(vault, "Good.md", "- [ ] good\n")
    write(vault, "Bad.md", "- [ ] bad\n")
    await _poison.poison(sessionmaker, "task", "Bad.md", "54000")

    await routes._reindex_background(full_hash=True)

    await assert_isolated(sessionmaker, "Bad.md", others={"Good.md": ["good"]})
    assert indexer_health.snapshot()["quarantined_notes"] == 0
    async with sessionmaker() as session:
        run = (await session.execute(text("SELECT trigger, error FROM indexer_runs"))).one()
    assert run.trigger == "manual" and run.error is None


async def test_a_failure_the_retry_does_not_reproduce_leaves_no_mark(sessionmaker, vault):
    write(vault, "Once.md", "- [ ] once\n")
    await _poison.poison(sessionmaker, "task", "Once.md", "22001", shots=1)
    await indexer.index_vault()
    row = await note(sessionmaker, "Once.md")
    assert row.tasks_failed is False
    assert await tasks_of(sessionmaker, "Once.md") == ["once"]


async def test_a_later_success_clears_the_mark(sessionmaker, vault):
    write(vault, "Fix.md", "- [ ] v1\n")
    await _poison.poison(sessionmaker, "task", "Fix.md", "22001")
    await indexer.index_vault()
    assert (await note(sessionmaker, "Fix.md")).tasks_failed is True
    await _poison.cure(sessionmaker, "Fix.md")
    write(vault, "Fix.md", "- [ ] v2\n")
    await indexer.index_vault()
    assert (await note(sessionmaker, "Fix.md")).tasks_failed is False
    assert await tasks_of(sessionmaker, "Fix.md") == ["v2"]


async def test_a_non_poison_failure_fails_the_pass_with_nothing_committed(
    sessionmaker, vault, monkeypatch
):
    write(vault, "a.md", "- [ ] a\n")
    real = indexer._update_tasks_for_changed

    async def broken(*_a, **_k):
        raise ConnectionError("gone")

    monkeypatch.setattr(indexer, "_update_tasks_for_changed", broken)
    with pytest.raises(ConnectionError):
        await indexer.index_vault()
    assert await note(sessionmaker, "a.md") is None
    monkeypatch.setattr(indexer, "_update_tasks_for_changed", real)


async def test_a_task_cap_is_marked_and_is_not_a_skip(sessionmaker, vault, monkeypatch):
    monkeypatch.setattr(indexer, "MAX_TASKS_PER_NOTE", 3)
    write(vault, "big.md", "".join(f"- [ ] t{i}\n" for i in range(4)))
    await indexer.index_vault()
    row = await note(sessionmaker, "big.md")
    assert row.tasks_truncated is True
    assert await tasks_of(sessionmaker, "big.md") == ["t0", "t1", "t2"]
    assert "over the task cap: 1 notes" in await tools.list_tasks_impl()
    write(vault, "big.md", "- [ ] t0\n")
    await indexer.index_vault()
    assert (await note(sessionmaker, "big.md")).tasks_truncated is False


# ══════════════════════════════════════════════════════════════════════════
# Re-derive (#311) and the provenance discard
# ══════════════════════════════════════════════════════════════════════════


async def run(uid: int):
    result = await indexer.index_vault(user_id=uid)
    indexer.record_index_outcome(uid, True, result)
    return result


async def test_a_persistent_task_failure_does_not_hold_a_re_derive_open(sessionmaker, vault):
    root = vault / "rd"
    write(root, "a.md", "- [ ] a\n")
    write(root, "bad.md", "- [ ] bad\n")
    uid = await tenant(sessionmaker, root, "rd")
    try:
        await _poison.poison(sessionmaker, "task", "bad.md", "22001")
        result = await run(uid)
        assert result.rederive == indexer.REDERIVE_RECORDED
        bad = await note(sessionmaker, "bad.md", uid)
        assert bad.tasks_failed is True and bad.derived_under is not None
        assert indexer.quarantined_paths(uid) == []
    finally:
        vault_service.clear_user_vault_cache(user_id=uid)


async def test_a_missing_buffered_body_withholds_the_re_derive(
    sessionmaker, vault, monkeypatch
):
    root = vault / "skip"
    write(root, "a.md", "- [ ] a\n")
    write(root, "b.md", "- [ ] b\n")
    uid = await tenant(sessionmaker, root, "skip")
    real = indexer._update_tasks_for_changed

    async def drop_a(*args, **kwargs):
        kwargs["bodies"] = {k: v for k, v in kwargs["bodies"].items() if k != "a.md"}
        return await real(*args, **kwargs)

    monkeypatch.setattr(indexer, "_update_tasks_for_changed", drop_a)
    try:
        result = await run(uid)
        assert result.rederive == indexer.REDERIVE_INCOMPLETE
        assert (await note(sessionmaker, "a.md", uid)).derived_under is None
        assert (await note(sessionmaker, "b.md", uid)).derived_under is not None
    finally:
        vault_service.clear_user_vault_cache(user_id=uid)


async def test_a_provenance_discard_removes_the_tasks(sessionmaker, vault):
    a = vault / "A"
    b = vault / "B"
    write(a, "x.md", "- [ ] from A\n")
    write(b, "y.md", "- [ ] from B\n")
    uid = await tenant(sessionmaker, a, "disc")
    try:
        first = await run(uid)
        assert first.rederive == indexer.REDERIVE_RECORDED
        a_id = (await note(sessionmaker, "x.md", uid)).id
        assert await tasks_of(sessionmaker, "x.md", uid) == ["from A"]
        await reassign(sessionmaker, uid, b)
        await run(uid)
        assert await note(sessionmaker, "x.md", uid) is None
        assert a_id not in await all_task_note_ids(sessionmaker)
        assert await tasks_of(sessionmaker, "y.md", uid) == ["from B"]
    finally:
        vault_service.clear_user_vault_cache(user_id=uid)
