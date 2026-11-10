"""The write-time task refresh composes with the index pass (#428, design D10).

Against a real PostgreSQL: `task_index.refresh_note_tasks` (R1, including the
3a "still current?" re-read), the pass's R2 scan start, R3 (a task-dirty row
is never stat-shortcut and joins the task stage) and R4 (keep a refresh newer
than the scan). The property proven is **convergence**: whatever the order of
a pass's scan, a tool's publication and refresh, and a later generic edit,
the task rows equal a fresh derivation of the file within two passes, with
`tasks_source_hash = content_hash`.

A "tool publication" here is what the task tools do around the refresh: the
bytes are published at the path, the call's anchored target is opened with
`open_mutable`, and the refresh is handed both. The tools themselves are
wired by another slice.

Skipped unless `PGVECTOR_TEST_ADMIN_URL` is set (see `_harness`).
"""
import asyncio
import itertools
import os
import threading
import time

import pytest
import pytest_asyncio
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import src.database
from src.config import settings
from src.models.db import NoteMetadata, NoteTask, User
from src.services import embeddings as embeddings_service
from src.services import indexer, indexer_health, task_index
from src.services import vault as vault_service
from src.services.note_hash import canonical_note_hash
from src.services.tasks import note_task_row, tasks_from_note_text
import _harness

pytestmark = [
    _harness.requires_pgvector,
    pytest.mark.asyncio(loop_scope="module"),
]

DIM = 8
VECTOR = [1.0] + [0.0] * (DIM - 1)
PAST = time.time() - 3600  # an mtime well outside the racy window


@pytest.fixture(scope="module")
def migrated_url():
    yield from _harness.throwaway_database("task_refresh_428", DIM)


@pytest_asyncio.fixture(loop_scope="module", scope="module")
async def sessionmaker(migrated_url):
    engine = create_async_engine(migrated_url, poolclass=None)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield maker
    await engine.dispose()


@pytest_asyncio.fixture(loop_scope="module")
async def vault(sessionmaker, monkeypatch, tmp_path):
    root = tmp_path / "vault"
    root.mkdir()
    monkeypatch.setattr(settings, "vault_path", str(root), raising=False)
    monkeypatch.setattr(indexer.settings, "vault_path", str(root), raising=False)
    monkeypatch.setattr(indexer.settings, "multi_user_mode", False, raising=False)
    monkeypatch.setattr(indexer.settings, "embedding_exclude_patterns", [], raising=False)
    monkeypatch.setattr(indexer, "async_session", sessionmaker)
    monkeypatch.setattr(src.database, "async_session", sessionmaker)
    monkeypatch.setattr(embeddings_service, "async_session", sessionmaker, raising=False)
    monkeypatch.setattr(indexer, "_is_paused", lambda: False)

    async def fake_batch(chunks, **_kw):
        return [list(VECTOR) for _ in chunks]

    async def fake_one(_text, **_kw):
        return list(VECTOR)

    monkeypatch.setattr(embeddings_service, "get_embeddings_batch", fake_batch)
    monkeypatch.setattr(embeddings_service, "get_embedding", fake_one)
    indexer._last_full_hash.clear()
    indexer.clear_quarantine()
    indexer_health.reset()
    vault_service.clear_user_vault_cache()

    async with sessionmaker() as session:
        for table in ("note_tasks", "note_links", "note_embeddings", "notes_metadata",
                      "indexer_runs", "users"):
            await session.execute(text(f"DELETE FROM {table}"))
        await session.commit()
    try:
        yield root
    finally:
        indexer._last_full_hash.clear()
        indexer.clear_quarantine()
        indexer_health.reset()
        vault_service.clear_user_vault_cache()


# ── helpers ───────────────────────────────────────────────────────────────


def put(root, rel: str, body: str, *, old: bool = False) -> bytes:
    """A generic write (not a task tool): the bytes at `rel`."""
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    data = body.encode("utf-8")
    path.write_bytes(data)
    if old:
        os.utime(path, (PAST, PAST))
    return data


class Publication:
    """A task tool's publication: bytes at the path and its anchored target."""

    def __init__(self, root, rel: str, body: str, uid: int | None = None):
        self.root, self.rel, self.uid = root, rel, uid
        self.published = put(root, rel, body)
        self.target = vault_service.open_mutable(rel, uid)

    async def refresh(self) -> task_index.RefreshResult:
        try:
            return await task_index.refresh_note_tasks(
                uid=self.uid, root=self.root, target=self.target, rel=self.rel,
                published=self.published,
            )
        finally:
            self.target.close()


def owner(uid):
    return NoteMetadata.user_id.is_(None) if uid is None else NoteMetadata.user_id == uid


async def note(sessionmaker, path: str, uid: int | None = None):
    async with sessionmaker() as session:
        return (await session.execute(
            select(NoteMetadata).where(NoteMetadata.file_path == path, owner(uid))
        )).scalar_one_or_none()


_COLUMNS = [c for c in NoteTask.__table__.columns if c.name not in ("id", "note_id")]


async def stored(sessionmaker, path: str, uid: int | None = None) -> list[dict]:
    """Every column of the note's task rows but the serial id and note id."""
    async with sessionmaker() as session:
        rows = (await session.execute(
            select(*_COLUMNS)
            .join(NoteMetadata, NoteMetadata.id == NoteTask.note_id)
            .where(NoteMetadata.file_path == path, owner(uid))
            .order_by(NoteTask.line)
        )).mappings().all()
    return [dict(r) for r in rows]


def fresh(root, path: str) -> list[dict]:
    """A fresh derivation of the file as it is on disk now."""
    tasks, _ = tasks_from_note_text((root / path).read_bytes())
    out = []
    for t in tasks:
        row = note_task_row(0, t)
        row.pop("note_id")
        out.append(row)
    return out


async def assert_converged(sessionmaker, root, path: str, uid: int | None = None):
    row = await note(sessionmaker, path, uid)
    assert row is not None
    assert row.tasks_source_hash == row.content_hash == canonical_note_hash(
        (root / path).read_bytes()
    )
    # Deliberately not `tasks_refreshed_at IS NULL`: a refresh that lands
    # after a pass already derived the same bytes leaves a clean row with a
    # stamp (the auditor's p4). That is harmless — R4 keeps rows only when
    # the source hash differs from the pass's buffered hash, which a clean
    # row's never does — and the design requires only the two facts here.
    assert await stored(sessionmaker, path, uid) == fresh(root, path)


async def run_pass(uid: int | None = None, *, between=None, stage_hook=None):
    """One index pass. `between` runs after the scan, before the attempt's
    lock (a write landing mid-pass); `stage_hook` runs just before the task
    stage, inside the attempt (the pass holds the generation lock and the
    row locks of the notes it upserted)."""
    real_attempt = indexer._index_vault_attempt
    real_stage = indexer._update_tasks_for_changed
    patched = []
    if between is not None:
        async def attempt(*a, **kw):
            await between()
            return await real_attempt(*a, **kw)
        indexer._index_vault_attempt = attempt
        patched.append(("_index_vault_attempt", real_attempt))
    if stage_hook is not None:
        async def stage(*a, **kw):
            await stage_hook()
            return await real_stage(*a, **kw)
        indexer._update_tasks_for_changed = stage
        patched.append(("_update_tasks_for_changed", real_stage))
    try:
        return await indexer.index_vault(user_id=uid)
    finally:
        for name, fn in patched:
            setattr(indexer, name, fn)


async def tenant(sessionmaker, root, name: str) -> int:
    root.mkdir(parents=True, exist_ok=True)
    async with sessionmaker() as session:
        user = User(username=name, password_hash="x", vault_path=str(root))
        session.add(user)
        await session.commit()
        await vault_service.warm_user_vault_cache(session, user_id=user.id)
        return user.id


A = "- [ ] alpha 📅 2026-11-01\n- [ ] beta #work\n"
B = "- [x] alpha 📅 2026-11-01 ✅ 2026-11-10\n- [ ] beta #work\n"
C = "- [x] alpha 📅 2026-11-01 ✅ 2026-11-10\n- [-] beta #work ❌ 2026-11-10\n"
D = "# Edited by hand\n- [ ] gamma 🔁 every week\n    with context\n"


# ══════════════════════════════════════════════════════════════════════════
# R1: the refresh
# ══════════════════════════════════════════════════════════════════════════


async def test_a_refresh_rewrites_only_the_task_rows_and_its_two_columns(sessionmaker, vault):
    put(vault, "n.md", A)
    await run_pass()
    before = await note(sessionmaker, "n.md")
    assert before.tasks_source_hash == before.content_hash

    result = await Publication(vault, "n.md", B).refresh()
    assert result == task_index.RefreshResult("refreshed")
    after = await note(sessionmaker, "n.md")
    assert after.tasks_source_hash == canonical_note_hash(B.encode())
    assert after.tasks_source_hash != after.content_hash, "task-dirty"
    assert after.tasks_refreshed_at is not None
    for column in ("content_hash", "stat_size", "stat_mtime_ns", "stat_ctime_ns",
                   "stat_ino", "derived_under", "extraction_version", "title", "tags",
                   "embedded_content_hash", "modified_at", "content_changed_at"):
        assert getattr(after, column) == getattr(before, column), column
    assert await stored(sessionmaker, "n.md") == fresh(vault, "n.md")
    assert [r["state"] for r in await stored(sessionmaker, "n.md")] == ["done", "open"]

    await run_pass()
    await assert_converged(sessionmaker, vault, "n.md")


async def test_pass_and_refresh_derivations_agree_column_for_column(sessionmaker, vault):
    corpus = {
        "c1.md": "---\ntitle: T\ntags: [x]\n---\n# H\n- [ ] a 🔁 every month 📅 2026-01-31 #a/b\n"
                 "    - ref 1\n    more\n- [ ] a 🔁 every month 📅 2026-01-31 #a/b\n",
        "c2.md": "> - [/] quoted ⏫ ➕ 2026-01-01 ❌ 2026-02-02\n>   ctx\n\n```\n- [ ] fenced\n```\n",
        "c3.md": "1. [x] done ✅ 2026-11-10 ^blk\n\t- [ ] tab child #Waiting-For//Chase/\n",
        "c4.md": "- [ ] crlf one\r\n- [ ] crlf two 🛫 2026-03-03 ⏳ 2026-03-04\r\n",
        "c5.md": "- [ ] " + "long " * 300 + "\n- [ ] Позвонить в БАНК\n",
        "c6.md": "no tasks at all\n",
    }
    for rel, body in corpus.items():
        put(vault, rel, body)
    await run_pass()
    by_pass = {rel: await stored(sessionmaker, rel) for rel in corpus}
    for rel, body in corpus.items():
        assert by_pass[rel] == fresh(vault, rel), rel
        # The same bytes, published by a tool: identical rows.
        result = await Publication(vault, rel, body).refresh()
        assert result.refreshed, (rel, result)
        assert await stored(sessionmaker, rel) == by_pass[rel], rel
        row = await note(sessionmaker, rel)
        assert row.tasks_source_hash == row.content_hash
    assert by_pass["c1.md"][0]["key_count"] == 2
    assert by_pass["c1.md"][0]["context"] == "- ref 1\nmore"


async def test_a_note_never_indexed_lags_and_the_write_stands(sessionmaker, vault):
    pub = Publication(vault, "new.md", A)
    result = await pub.refresh()
    assert result == task_index.RefreshResult("lagged", "not_indexed")
    assert result.line == "index: may lag (not_indexed)"
    assert (vault / "new.md").read_bytes() == A.encode()
    await run_pass()
    await assert_converged(sessionmaker, vault, "new.md")


async def test_two_refreshes_out_of_order_the_first_is_superseded(sessionmaker, vault):
    put(vault, "o.md", A)
    await run_pass()
    first = Publication(vault, "o.md", B)
    second = Publication(vault, "o.md", C)
    assert (await second.refresh()).refreshed
    late = await first.refresh()
    assert late == task_index.RefreshResult("lagged", "superseded")
    assert await stored(sessionmaker, "o.md") == fresh(vault, "o.md")
    assert (await note(sessionmaker, "o.md")).tasks_source_hash == canonical_note_hash(
        C.encode()
    )


async def test_a_superseded_refresh_writes_nothing(sessionmaker, vault):
    put(vault, "s.md", A)
    await run_pass()
    pub = Publication(vault, "s.md", B)
    put(vault, "s.md", D)  # a generic writer, after the tool, before its refresh
    before = await stored(sessionmaker, "s.md")
    assert (await pub.refresh()).code == "superseded"
    assert await stored(sessionmaker, "s.md") == before
    row = await note(sessionmaker, "s.md")
    assert row.tasks_refreshed_at is None and row.tasks_source_hash == row.content_hash
    await run_pass()
    await assert_converged(sessionmaker, vault, "s.md")


async def test_a_held_row_lock_lags_within_the_wait_and_the_write_stands(sessionmaker, vault):
    put(vault, "l.md", A)
    await run_pass()
    row_id = (await note(sessionmaker, "l.md")).id
    async with sessionmaker() as holder:
        await holder.execute(
            text("SELECT id FROM notes_metadata WHERE id = :i FOR UPDATE"), {"i": row_id}
        )
        pub = Publication(vault, "l.md", B)
        started = time.monotonic()
        result = await pub.refresh()
        waited = time.monotonic() - started
        await holder.rollback()
    assert result == task_index.RefreshResult("lagged", "lock_timeout")
    assert result.usage == "lagged:lock_timeout"
    assert 1.5 <= waited < 6, waited
    assert (vault / "l.md").read_bytes() == B.encode()
    await run_pass()
    await assert_converged(sessionmaker, vault, "l.md")


# ══════════════════════════════════════════════════════════════════════════
# R3: a task-dirty row is never stat-shortcut
# ══════════════════════════════════════════════════════════════════════════


async def test_a_task_dirty_row_with_an_unchanged_stat_is_re_read(
    sessionmaker, vault, monkeypatch
):
    put(vault, "dirty.md", A, old=True)
    put(vault, "clean.md", A, old=True)
    # The ctime cannot be backdated, so the racy rule's clock is moved
    # forward instead: both stats are then recordable.
    monkeypatch.setattr(indexer, "_wall_clock_ns", lambda: time.time_ns() + 10**10)
    await run_pass()  # the start-of-process full-hash pass records both stats
    monkeypatch.setattr(indexer, "_full_hash_due", lambda _scope: False)
    for rel in ("dirty.md", "clean.md"):
        assert (await note(sessionmaker, rel)).stat_size is not None, rel
    # A refresh from bytes that are not the row's (as after a tool write the
    # pass then saw reverted): the row is task-dirty, the stat unchanged.
    async with sessionmaker() as session:
        await session.execute(
            update(NoteMetadata).where(NoteMetadata.file_path == "dirty.md")
            .values(tasks_source_hash="0" * 64)
        )
        await session.execute(text(
            "DELETE FROM note_tasks WHERE note_id = "
            "(SELECT id FROM notes_metadata WHERE file_path = 'dirty.md')"
        ))
        await session.commit()

    reads: list[str] = []
    real = indexer._read_and_hash

    def counting(parent_fd, name, rel):
        reads.append(rel)
        return real(parent_fd, name, rel)

    monkeypatch.setattr(indexer, "_read_and_hash", counting)
    await run_pass()
    assert reads == ["dirty.md"], "the clean row is shortcut, the dirty one read"
    await assert_converged(sessionmaker, vault, "dirty.md")
    reads.clear()
    await run_pass()
    assert reads == [], "converged: both shortcut again"


# ══════════════════════════════════════════════════════════════════════════
# R4 and convergence
# ══════════════════════════════════════════════════════════════════════════


async def test_a_pass_that_read_older_bytes_keeps_the_tools_newer_rows(sessionmaker, vault):
    put(vault, "k.md", A)
    await run_pass()
    assert (await Publication(vault, "k.md", B).refresh()).refreshed  # W1
    # The pass reads W1; W2 is published and refreshed before its attempt.
    newer = {}

    async def w2():
        newer["result"] = await Publication(vault, "k.md", C).refresh()

    await run_pass(between=w2)
    assert newer["result"].refreshed
    row = await note(sessionmaker, "k.md")
    assert row.content_hash == canonical_note_hash(B.encode()), "the pass upserted W1"
    assert row.tasks_source_hash == canonical_note_hash(C.encode()), "kept W2's rows"
    assert row.tasks_refreshed_at is not None
    assert await stored(sessionmaker, "k.md") == fresh(vault, "k.md")
    await run_pass()
    await assert_converged(sessionmaker, vault, "k.md")


async def test_a_task_only_note_keeps_a_refresh_landing_inside_the_attempt(
    sessionmaker, vault
):
    put(vault, "t.md", A)
    await run_pass()
    assert (await Publication(vault, "t.md", B).refresh()).refreshed
    put(vault, "t.md", A)  # reverted by hand: bytes == content_hash, rows dirty
    landed = {}

    async def inside():
        # The pass holds the generation lock; this note's row is not locked
        # (it is task-only), so the refresh takes it and commits.
        landed["result"] = await Publication(vault, "t.md", C).refresh()

    await run_pass(stage_hook=inside)
    assert landed["result"].refreshed
    row = await note(sessionmaker, "t.md")
    assert row.content_hash == canonical_note_hash(A.encode())
    assert row.tasks_source_hash == canonical_note_hash(C.encode())
    assert await stored(sessionmaker, "t.md") == fresh(vault, "t.md")
    await run_pass()
    await assert_converged(sessionmaker, vault, "t.md")


async def test_no_lock_cycle_with_a_pass_holding_the_generation_lock(sessionmaker, vault):
    put(vault, "g.md", A)
    await run_pass()
    put(vault, "g.md", D)  # the pass will upsert g.md and hold its row lock
    started = {}

    async def concurrently():
        pub = Publication(vault, "g.md", B)
        started["task"] = asyncio.create_task(pub.refresh())
        await asyncio.sleep(0.5)  # the refresh is now waiting on the row lock

    await asyncio.wait_for(run_pass(stage_hook=concurrently), timeout=30)
    result = await asyncio.wait_for(started["task"], timeout=10)
    assert result.refreshed or result.code == "lock_timeout", result
    await run_pass()
    await run_pass()
    await assert_converged(sessionmaker, vault, "g.md")


EVENTS = ("P", "R", "E", "S", "C")


def _orders():
    for order in itertools.permutations(EVENTS):
        i = order.index
        if i("P") < i("R") and i("P") < i("E") and i("S") < i("C"):
            yield "".join(order)


ORDERS = list(_orders())


async def test_every_interleaving_is_enumerated():
    assert len(ORDERS) == 20


@pytest.mark.parametrize("order", ORDERS)
async def test_interleavings_converge_within_two_passes(sessionmaker, vault, order):
    """P = a task tool publishes, R = its refresh, E = a later generic edit,
    S…C = one pass (S: snapshot, scan start and walk; C: its attempt and
    commit). Events between S and C land after the walk, before the lock."""
    rel = "i.md"
    put(vault, rel, A)
    await run_pass()
    pub: dict = {}
    results: list = []

    async def do(event):
        if event == "P":
            pub["p"] = Publication(vault, rel, B)
        elif event == "R":
            results.append(await pub["p"].refresh())
        elif event == "E":
            put(vault, rel, D)

    s, c = order.index("S"), order.index("C")
    for event in order[:s]:
        await do(event)

    async def mid():
        for event in order[s + 1:c]:
            await do(event)

    await run_pass(between=mid)
    for event in order[c + 1:]:
        await do(event)
    assert results and (results[0].refreshed or results[0].code == "superseded"), results
    if order.index("E") < order.index("R"):
        assert results[0].code == "superseded"

    await run_pass()
    await run_pass()
    await assert_converged(sessionmaker, vault, rel)


# ══════════════════════════════════════════════════════════════════════════
# Tenancy and the root re-check
# ══════════════════════════════════════════════════════════════════════════


async def test_a_refresh_never_touches_another_owners_row_at_the_same_path(
    sessionmaker, vault
):
    root7, root8 = vault / "u7", vault / "u8"
    put(root7, "Projects/Alpha.md", A)
    put(root8, "Projects/Alpha.md", A)
    put(vault, "Projects/Alpha.md", A)  # the NULL-owned scope's own note
    u7 = await tenant(sessionmaker, root7, "seven")
    u8 = await tenant(sessionmaker, root8, "eight")
    await run_pass(u7)
    await run_pass(u8)
    await run_pass(None)
    rel = "Projects/Alpha.md"
    seven_before = (await note(sessionmaker, rel, u7), await stored(sessionmaker, rel, u7))
    null_before = (await note(sessionmaker, rel, None), await stored(sessionmaker, rel, None))

    result = await Publication(root8, rel, C, uid=u8).refresh()
    assert result.refreshed
    assert await stored(sessionmaker, rel, u8) == fresh(root8, rel)
    for uid, (row, rows) in ((u7, seven_before), (None, null_before)):
        after = await note(sessionmaker, rel, uid)
        assert after.tasks_source_hash == row.tasks_source_hash
        assert after.tasks_refreshed_at is None
        assert await stored(sessionmaker, rel, uid) == rows

    # A user's refresh when no row of theirs is at the path: nothing, even
    # though another owner has one there.
    (root7 / "Other.md").write_text(A)
    put(vault, "Other.md", A)
    await run_pass(None)
    assert (await Publication(root7, "Other.md", B, uid=u7).refresh()).code == "not_indexed"
    assert (await note(sessionmaker, "Other.md", None)).tasks_refreshed_at is None


async def test_a_reassigned_account_refreshes_nothing(sessionmaker, vault):
    root, other = vault / "ra", vault / "rb"
    put(root, "n.md", A)
    other.mkdir()
    uid = await tenant(sessionmaker, root, "reassigned")
    await run_pass(uid)
    pub = Publication(root, "n.md", B, uid=uid)
    async with sessionmaker() as session:
        await session.execute(update(User).where(User.id == uid).values(vault_path=str(other)))
        await session.commit()
    result = await pub.refresh()
    assert result == task_index.RefreshResult("lagged", "root_changed")
    row = await note(sessionmaker, "n.md", uid)
    assert row.tasks_refreshed_at is None and row.tasks_source_hash == row.content_hash


# ══════════════════════════════════════════════════════════════════════════
# The version 4 -> 5 backfill
# ══════════════════════════════════════════════════════════════════════════


async def test_the_v4_to_v5_backfill_derives_every_note_and_re_embeds_nothing(
    sessionmaker, vault, monkeypatch
):
    put(vault, "old.md", "body\n- [ ] backfilled #a/b\n    ctx\n[[x]]\n")
    put(vault, "two.md", "- [ ] dup\n- [ ] dup\n")
    await run_pass()
    await indexer.embed_vault()
    before = {rel: await note(sessionmaker, rel) for rel in ("old.md", "two.md")}
    assert before["old.md"].embedded_content_hash == before["old.md"].content_hash
    # What a version-4 build left: rows without the version-5 columns, no
    # task source, stamped 4.
    async with sessionmaker() as session:
        await session.execute(text(
            "UPDATE note_tasks SET task_key = NULL, key_ordinal = NULL, key_count = NULL, "
            "description_key = NULL, text_fold = NULL, tag_prefixes = NULL, "
            "recurrence = NULL, created = NULL, cancelled = NULL, context = NULL"
        ))
        await session.execute(text(
            "UPDATE notes_metadata SET extraction_version = 4, tasks_source_hash = NULL"
        ))
        await session.commit()

    async def unexpected(_chunks, **_kw):
        pytest.fail("the version-5 bump must not call the embedding provider")

    monkeypatch.setattr(embeddings_service, "get_embeddings_batch", unexpected)
    await run_pass()
    await indexer.embed_vault()
    for rel in ("old.md", "two.md"):
        after = await note(sessionmaker, rel)
        assert after.extraction_version == 5
        assert after.embedded_content_hash == before[rel].embedded_content_hash
        await assert_converged(sessionmaker, vault, rel)
    rows = await stored(sessionmaker, "two.md")
    assert [(r["key_ordinal"], r["key_count"]) for r in rows] == [(1, 2), (2, 2)]
    assert (await stored(sessionmaker, "old.md"))[0]["tag_prefixes"] == ["a", "a/b"]


async def test_a_refresh_of_a_version_4_note_writes_its_ids(sessionmaker, vault):
    put(vault, "v4.md", A)
    await run_pass()
    async with sessionmaker() as session:
        await session.execute(text("UPDATE note_tasks SET task_key = NULL, key_ordinal = NULL, "
                                   "key_count = NULL"))
        await session.execute(text("UPDATE notes_metadata SET extraction_version = 4"))
        await session.commit()
    assert (await Publication(vault, "v4.md", B).refresh()).refreshed
    row = await note(sessionmaker, "v4.md")
    assert row.extraction_version == 4, "the refresh never writes the version"
    assert all(r["task_key"] is not None for r in await stored(sessionmaker, "v4.md"))


# ══════════════════════════════════════════════════════════════════════════
# Audit follow-ups (#428): the derivation holds no lock; more interleavings
# ══════════════════════════════════════════════════════════════════════════

A2 = "- [ ] alpha EDITED\n- [ ] beta #work\n"


async def test_the_derivation_runs_before_the_row_lock(sessionmaker, vault, monkeypatch):
    put(vault, "nolock.md", A)
    await run_pass()
    row_id = (await note(sessionmaker, "nolock.md")).id
    entered, release = threading.Event(), threading.Event()
    real = task_index.tasks_from_note_text

    def blocking(raw):
        entered.set()
        assert release.wait(20)
        return real(raw)

    monkeypatch.setattr(task_index, "tasks_from_note_text", blocking)
    pending = asyncio.create_task(Publication(vault, "nolock.md", B).refresh())
    try:
        assert await asyncio.to_thread(entered.wait, 10)
        # While the refresh is deriving, nobody holds the row: NOWAIT succeeds.
        async with sessionmaker() as other:
            got = (await other.execute(
                text("SELECT id FROM notes_metadata WHERE id = :i FOR UPDATE NOWAIT"),
                {"i": row_id},
            )).scalar_one()
            assert got == row_id
            await other.rollback()
    finally:
        release.set()
    assert (await asyncio.wait_for(pending, 20)).refreshed
    assert await stored(sessionmaker, "nolock.md") == fresh(vault, "nolock.md")


async def test_a_derivation_failure_records_the_pass_style_mark(
    sessionmaker, vault, monkeypatch
):
    put(vault, "fail.md", A)
    await run_pass()

    def boom(_raw):
        raise RuntimeError("synthetic extractor bug")

    monkeypatch.setattr(task_index, "tasks_from_note_text", boom)
    result = await Publication(vault, "fail.md", B).refresh()
    assert result == task_index.RefreshResult("lagged", "derivation_failed")
    row = await note(sessionmaker, "fail.md")
    assert row.tasks_failed is True
    assert row.tasks_source_hash == canonical_note_hash(B.encode())
    assert row.tasks_refreshed_at is not None
    assert await stored(sessionmaker, "fail.md") == []
    # The pass's own extractor is not patched: the dirty row is re-derived.
    await run_pass()
    await assert_converged(sessionmaker, vault, "fail.md")
    assert (await note(sessionmaker, "fail.md")).tasks_failed is False


async def test_a_refresh_between_the_locked_read_and_the_upsert_is_kept(
    sessionmaker, vault, monkeypatch
):
    """The pass read A2 (a generic edit); a tool publishes and refreshes B
    after the pass's locked read but before its upsert takes the row lock."""
    put(vault, "p1.md", A)
    await run_pass()
    put(vault, "p1.md", A2)
    loop = asyncio.get_running_loop()
    real = indexer.parse_frontmatter
    state: dict = {"armed": False, "result": None}

    async def between():
        state["armed"] = True
        state["pub"] = Publication(vault, "p1.md", B)

    def hooked(raw):
        # Runs on a worker thread inside the change loop, before the upsert.
        if state["armed"] and state["result"] is None:
            state["result"] = asyncio.run_coroutine_threadsafe(
                state["pub"].refresh(), loop
            ).result(20)
        return real(raw)

    monkeypatch.setattr(indexer, "parse_frontmatter", hooked)
    await run_pass(between=between)
    monkeypatch.setattr(indexer, "parse_frontmatter", real)
    assert state["result"].refreshed
    row = await note(sessionmaker, "p1.md")
    assert row.content_hash == canonical_note_hash(A2.encode()), "the pass upserted A2"
    assert row.tasks_source_hash == canonical_note_hash(B.encode()), "kept B's rows"
    assert await stored(sessionmaker, "p1.md") == fresh(vault, "p1.md")
    await run_pass()
    await assert_converged(sessionmaker, vault, "p1.md")


async def test_a_dirty_row_reverted_and_renamed_on_disk_converges(sessionmaker, vault):
    put(vault, "m.md", A)
    await run_pass()
    assert (await Publication(vault, "m.md", B).refresh()).refreshed
    put(vault, "m.md", A)  # reverted: bytes == content_hash, rows describe B
    os.rename(vault / "m.md", vault / "moved.md")
    await run_pass()  # the id-preserving move pairs and re-derives it
    await assert_converged(sessionmaker, vault, "moved.md")
    assert await note(sessionmaker, "m.md") is None


async def test_a_file_deleted_before_its_refresh_is_superseded_and_pruned(
    sessionmaker, vault
):
    put(vault, "d.md", A)
    put(vault, "keep.md", A)
    await run_pass()
    pub = Publication(vault, "d.md", B)
    os.unlink(vault / "d.md")
    assert (await pub.refresh()).code == "superseded"
    await run_pass()
    assert await note(sessionmaker, "d.md") is None
    await assert_converged(sessionmaker, vault, "keep.md")


async def test_a_refresh_after_a_pass_that_read_the_same_bytes(sessionmaker, vault):
    put(vault, "same.md", A)
    await run_pass()
    pub = Publication(vault, "same.md", B)
    await run_pass()  # the pass reads B first
    assert (await pub.refresh()).refreshed  # then the refresh of B lands
    row = await note(sessionmaker, "same.md")
    assert row.tasks_refreshed_at is not None, "a harmless leftover stamp"
    await assert_converged(sessionmaker, vault, "same.md")
    await run_pass()
    await assert_converged(sessionmaker, vault, "same.md")


@pytest.mark.parametrize("first,second", [
    ("before", "before"), ("before", "between"), ("before", "after"),
    ("between", "between"), ("between", "after"), ("after", "after"),
])
async def test_two_tool_writes_inside_one_interleaving_converge(
    sessionmaker, vault, first, second
):
    rel = "two.md"
    put(vault, rel, A)
    await run_pass()
    results: list = []

    async def write(body):
        results.append(await Publication(vault, rel, body).refresh())

    plan: dict = {"before": [], "between": [], "after": []}
    plan[first].append(B)
    plan[second].append(C)
    for body in plan["before"]:
        await write(body)

    async def between():
        for body in plan["between"]:
            await write(body)

    await run_pass(between=between)
    for body in plan["after"]:
        await write(body)
    assert all(r.refreshed for r in results), results
    assert await stored(sessionmaker, rel) == fresh(vault, rel), "read-your-writes"
    await run_pass()
    await run_pass()
    await assert_converged(sessionmaker, vault, rel)


async def test_a_move_note_between_publish_and_refresh(sessionmaker, vault, monkeypatch):
    import src.mcp_server.tools as tools
    from src.mcp_server.auth import current_permission

    async def noop(*_a, **_k):
        return None

    monkeypatch.setattr(tools.settings, "vault_path", str(vault), raising=False)
    monkeypatch.setattr(tools, "async_session", sessionmaker)
    monkeypatch.setattr(tools, "_log_usage", noop)
    permission = current_permission.set("readwrite")
    try:
        put(vault, "mv.md", A)
        await run_pass()
        pub = Publication(vault, "mv.md", B)
        assert "Moved" in str(await tools.move_note_impl("mv.md", "Moved/mv.md"))
        result = await pub.refresh()
        assert result.code in ("not_indexed", "superseded"), result
    finally:
        current_permission.reset(permission)
    await run_pass()
    await run_pass()
    await assert_converged(sessionmaker, vault, "Moved/mv.md")
    assert await note(sessionmaker, "mv.md") is None


async def test_two_users_inside_one_interleaving(sessionmaker, vault):
    root7, root8 = vault / "i7", vault / "i8"
    rel = "Projects/Alpha.md"
    put(root7, rel, A)
    put(root8, rel, A)
    u7 = await tenant(sessionmaker, root7, "inter-seven")
    u8 = await tenant(sessionmaker, root8, "inter-eight")
    await run_pass(u7)
    await run_pass(u8)
    results: dict = {}

    async def between():
        # During user 7's pass: user 8's tool write, then user 7's.
        results[u8] = await Publication(root8, rel, C, uid=u8).refresh()
        results[u7] = await Publication(root7, rel, B, uid=u7).refresh()

    await run_pass(u7, between=between)
    assert results[u7].refreshed and results[u8].refreshed
    assert await stored(sessionmaker, rel, u8) == fresh(root8, rel)
    assert await stored(sessionmaker, rel, u7) == fresh(root7, rel)
    for _ in range(2):
        await run_pass(u7)
        await run_pass(u8)
    await assert_converged(sessionmaker, root7, rel, u7)
    await assert_converged(sessionmaker, root8, rel, u8)
    assert await stored(sessionmaker, rel, u7) != await stored(sessionmaker, rel, u8)
