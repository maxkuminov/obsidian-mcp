"""The generic writers' task refresh end to end (#432, task-refresh-on-write).

Every call goes through the REGISTERED tool (`mcp.call_tool`), over a real
vault directory, the real extractor, the real index pass, the real write-time
refresh, the real change feed and the real usage log. Only the embedding
provider is a stand-in.

Covers:
- read-your-writes when the refresh runs, for `edit_note`, `set_frontmatter`
  and a `write_file` overwrite, and the clear after `delete_note` (soft and
  permanent);
- the last checkbox removed;
- the review's delete interleaving (finding 1) and a recreated path;
- a lagging refresh that leaves the response and the event unchanged;
- refreshes of different writers finishing out of order;
- a pass whose scan precedes a generic write or a delete (D7 1 and 9);
- two-user tenancy.

Skipped unless `PGVECTOR_TEST_ADMIN_URL` is set (see `_harness`).
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import re

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import src.database
import src.mcp_server.tools as tools
from src.auth.session import current_actor, current_principal, current_user_id, current_vault_root
from src.config import settings
from src.mcp_server.auth import current_permission
from src.mcp_server.server import mcp
from src.services import change_feed, indexer, indexer_health, rate_limits, task_index, vault_overlap
from src.services import embeddings as embeddings_service
from src.services import vault as vault_service
from src.services.note_hash import canonical_note_hash
from src.services.vault import content_hash_for_bytes
import _harness

pytestmark = [
    _harness.requires_pgvector,
    pytest.mark.asyncio(loop_scope="module"),
]

DIM = 8
VECTOR = [1.0] + [0.0] * (DIM - 1)
NOW = dt.datetime(2026, 11, 12, 12, 0, tzinfo=dt.timezone.utc)


@pytest.fixture(scope="module")
def migrated_url():
    yield from _harness.throwaway_database("task_refresh_generic_432", DIM)


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
    for target in (settings, tools.settings, indexer.settings, change_feed.settings):
        monkeypatch.setattr(target, "vault_path", str(root), raising=False)
    monkeypatch.setattr(settings, "multi_user_mode", False, raising=False)
    monkeypatch.setattr(settings, "embedding_exclude_patterns", [], raising=False)
    monkeypatch.setattr(settings, "write_precondition_required", False, raising=False)
    monkeypatch.setattr(indexer, "async_session", sessionmaker)
    monkeypatch.setattr(tools, "async_session", sessionmaker)
    monkeypatch.setattr(src.database, "async_session", sessionmaker)
    monkeypatch.setattr(embeddings_service, "async_session", sessionmaker, raising=False)
    monkeypatch.setattr(indexer, "_is_paused", lambda: False)
    monkeypatch.setattr(tools, "_task_now", lambda: NOW)

    async def fake_batch(chunks, **_kw):
        return [list(VECTOR) for _ in chunks]

    async def fake_one(_text, **_kw):
        return list(VECTOR)

    monkeypatch.setattr(embeddings_service, "get_embeddings_batch", fake_batch)
    monkeypatch.setattr(embeddings_service, "get_embedding", fake_one)
    tokens = [
        (current_permission, current_permission.set("readwrite")),
        (current_actor, current_actor.set(("api_key", "laptop", "omcp_lap"))),
        (current_principal, current_principal.set(("api_key", 12))),
        (current_user_id, current_user_id.set(None)),
    ]
    rate_limits.reset_state_for_tests()
    indexer._last_full_hash.clear()
    indexer.clear_quarantine()
    indexer_health.reset()
    vault_service.clear_user_vault_cache()
    async with sessionmaker() as session:
        for table in ("note_events", "note_event_scopes", "note_event_gaps", "usage_logs",
                      "note_tasks", "note_links", "note_embeddings", "notes_metadata",
                      "task_settings", "indexer_runs", "users"):
            await session.execute(text(f"DELETE FROM {table}"))
        await session.commit()
    try:
        yield root
    finally:
        for var, token in reversed(tokens):
            var.reset(token)
        rate_limits.reset_state_for_tests()
        indexer._last_full_hash.clear()
        indexer.clear_quarantine()
        indexer_health.reset()
        vault_service.clear_user_vault_cache()


# ── helpers ───────────────────────────────────────────────────────────────


def put(root, rel: str, body: str | bytes) -> bytes:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    data = body.encode("utf-8") if isinstance(body, str) else body
    path.write_bytes(data)
    return data


async def index(uid: int | None = None, *, between=None):
    """One pass. `between` runs after the scan, before the attempt's lock."""
    if between is None:
        return await indexer.index_vault(user_id=uid)
    real = indexer._index_vault_attempt

    async def attempt(*a, **kw):
        await between()
        return await real(*a, **kw)

    indexer._index_vault_attempt = attempt
    try:
        return await indexer.index_vault(user_id=uid)
    finally:
        indexer._index_vault_attempt = real


async def call(name: str, **args) -> str:
    content = await mcp.call_tool(name, args)
    blocks = content[0] if isinstance(content, tuple) else content
    return "".join(getattr(b, "text", "") for b in blocks)


def code(out: str) -> str | None:
    last = out.rstrip().splitlines()[-1]
    return json.loads(last[len("MCP-REFUSAL "):])["code"] if last.startswith("MCP-REFUSAL ") else None


def texts(listing: str) -> list[str]:
    return [ln.rsplit("text: ", 1)[1] for ln in listing.splitlines() if ln.startswith("- [")]


def lines_of(listing: str) -> list[int]:
    return [int(m.group(1)) for m in re.finditer(r"`:(\d+) — id ", listing)]


async def scalar(sessionmaker, sql: str, **params):
    async with sessionmaker() as session:
        return (await session.execute(text(sql), params)).scalar()


async def usage(sessionmaker, tool: str) -> dict:
    async with sessionmaker() as session:
        rows = (await session.execute(text(
            "SELECT params FROM usage_logs WHERE tool = :t ORDER BY id"), {"t": tool})).all()
    assert rows, tool
    return rows[-1].params


async def events(sessionmaker, tool: str) -> int:
    return await scalar(sessionmaker, "SELECT count(*) FROM note_events WHERE tool = :t", t=tool)


async def converged(sessionmaker, root, rel: str) -> bool:
    return await scalar(
        sessionmaker,
        "SELECT tasks_source_hash = content_hash AND content_hash = :h "
        "FROM notes_metadata WHERE file_path = :p",
        p=rel, h=canonical_note_hash((root / rel).read_bytes()),
    )


TASKS = "# List\n\n- [ ] Buy milk\n- [ ] Call bank\n"


# ══════════════════════════════════════════════════════════════════════════
# Read-your-writes when the refresh runs
# ══════════════════════════════════════════════════════════════════════════


async def test_list_edit_list_needs_no_pass(sessionmaker, vault):
    put(vault, "Home.md", TASKS)
    await index()
    assert texts(await call("list_tasks")) == ["Buy milk", "Call bank"]
    out = await call("edit_note", path="Home.md", find="- [ ] Buy milk", content="- [x] Buy milk")
    assert code(out) is None, out
    data = (vault / "Home.md").read_bytes()
    # Silent: exactly the response edit_note gave before #432.
    assert out == f"Updated note: Home.md — content_hash: {content_hash_for_bytes(data)}"
    assert texts(await call("list_tasks")) == ["Call bank"]
    assert texts(await call("list_tasks", status="done")) == ["Buy milk"]
    assert (await usage(sessionmaker, "edit_note"))["task_refresh"] == "refreshed"
    assert await events(sessionmaker, "edit_note") == 1
    await index()
    assert await converged(sessionmaker, vault, "Home.md")
    assert texts(await call("list_tasks", status="done")) == ["Buy milk"]


async def test_removing_the_last_checkbox_removes_the_rows(sessionmaker, vault):
    put(vault, "r.md", TASKS)
    await index()
    out = await call("edit_note", path="r.md", content="# Nothing left\n", replace_frontmatter=True)
    assert code(out) is None, out
    assert texts(await call("list_tasks", status="all")) == []
    assert await scalar(sessionmaker, "SELECT count(*) FROM note_tasks") == 0


async def test_set_frontmatter_keeps_line_numbers_true(sessionmaker, vault):
    put(vault, "f.md", "---\na: 1\nb: 2\nc: 3\n---\n- [ ] the task\n")
    await index()
    assert lines_of(await call("list_tasks")) == [6]
    out = await call("set_frontmatter", path="f.md", updates={"d": 4, "e": 5})
    assert code(out) is None, out
    assert lines_of(await call("list_tasks")) == [8]
    assert (await usage(sessionmaker, "set_frontmatter"))["task_refresh"] == "refreshed"


async def test_write_file_overwrite_replaces_the_tasks(sessionmaker, vault):
    put(vault, "w.md", TASKS)
    await index()
    out = await call("write_file", path="w.md", content="- [ ] Only this\n",
                     encoding="text", overwrite=True)
    assert code(out) is None, out
    assert texts(await call("list_tasks")) == ["Only this"]


async def test_a_maximal_note_is_stored_truncated(sessionmaker, vault):
    from src.config import MAX_NOTE_BYTES, MAX_TASKS_PER_NOTE

    put(vault, "big.md", "- [ ] seed\n")
    await index()
    lines = "".join(f"- [ ] t{i}\n" for i in range(MAX_TASKS_PER_NOTE + 1))
    body = lines + "x" * (MAX_NOTE_BYTES - len(lines.encode()) - 1) + "\n"
    assert len(body.encode()) == MAX_NOTE_BYTES
    out = await call("edit_note", path="big.md", content=body, replace_frontmatter=True)
    assert code(out) is None, out[:300]
    assert await scalar(sessionmaker, "SELECT count(*) FROM note_tasks") == MAX_TASKS_PER_NOTE
    assert await scalar(
        sessionmaker, "SELECT tasks_truncated FROM notes_metadata WHERE file_path = 'big.md'")


async def test_a_note_without_checkboxes_logs_no_refresh(sessionmaker, vault):
    put(vault, "p.md", "plain\n")
    await index()
    await call("edit_note", path="p.md", content="still plain\n")
    assert "task_refresh" not in await usage(sessionmaker, "edit_note")


# ══════════════════════════════════════════════════════════════════════════
# The delete clear
# ══════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("permanent", [False, True])
async def test_a_deleted_note_leaves_the_listing(sessionmaker, vault, permanent):
    put(vault, "Done.md", TASKS)
    put(vault, "Keep.md", "- [ ] stays\n")
    await index()
    out = await call("delete_note", path="Done.md", permanent=permanent)
    assert code(out) is None, out
    assert texts(await call("list_tasks")) == ["stays"]
    assert (await usage(sessionmaker, "delete_note"))["task_refresh"] == "refreshed"
    assert await events(sessionmaker, "delete_note") == 1
    assert await scalar(
        sessionmaker, "SELECT tasks_source_hash FROM notes_metadata WHERE file_path = 'Done.md'"
    ) == task_index.EMPTY_NOTE_HASH
    await index()
    assert await scalar(
        sessionmaker, "SELECT count(*) FROM notes_metadata WHERE file_path = 'Done.md'") == 0


async def test_a_task_added_between_the_deletes_read_and_the_delete_is_cleared(
    sessionmaker, vault, monkeypatch,
):
    """Review finding 1. The delete reads plain bytes P (and is guarded by
    their hash); a second writer then replaces them with T, holding a task,
    and its refresh lists the task; the delete then removes T. The clear is
    not gated on P, so the task leaves the listing."""
    plain = put(vault, "x.md", "plain\n")
    await index()
    newer = put(vault, "x.md", "- [ ] added in between\n")
    target = vault_service.open_mutable("x.md", None)
    try:
        assert (await task_index.refresh_note_tasks(
            uid=None, root=vault, target=target, rel="x.md", published=newer)).refreshed
    finally:
        target.close()
    assert texts(await call("list_tasks")) == ["added in between"]
    real = tools._read_incumbent

    def read_before_the_second_writer(target, path, cap):
        real(target, path, cap)
        return plain, False  # what the delete saw, before T landed

    monkeypatch.setattr(tools, "_read_incumbent", read_before_the_second_writer)
    out = await call("delete_note", path="x.md", expected_hash=content_hash_for_bytes(plain))
    assert code(out) is None, out
    assert not (vault / "x.md").exists()
    assert texts(await call("list_tasks")) == []


async def test_a_path_recreated_before_the_clear_is_not_cleared(sessionmaker, vault, monkeypatch):
    put(vault, "re.md", TASKS)
    await index()
    real = tools._record_note_events

    async def record_then_recreate(uid, evs):
        await real(uid, evs)
        put(vault, "re.md", "- [ ] recreated\n")  # lands after the event, before the clear

    monkeypatch.setattr(tools, "_record_note_events", record_then_recreate)
    out = await call("delete_note", path="re.md", permanent=True)
    assert code(out) is None, out
    assert (await usage(sessionmaker, "delete_note"))["task_refresh"] == "lagged:superseded"
    monkeypatch.setattr(tools, "_record_note_events", real)
    await index()
    assert texts(await call("list_tasks")) == ["recreated"]
    assert await converged(sessionmaker, vault, "re.md")


# ══════════════════════════════════════════════════════════════════════════
# A lagging refresh, and refreshes out of order
# ══════════════════════════════════════════════════════════════════════════


async def test_a_held_row_lock_leaves_response_and_event_unchanged(sessionmaker, vault):
    put(vault, "l.md", TASKS)
    await index()
    row_id = await scalar(sessionmaker, "SELECT id FROM notes_metadata WHERE file_path = 'l.md'")
    async with sessionmaker() as holder:
        await holder.execute(
            text("SELECT id FROM notes_metadata WHERE id = :i FOR UPDATE"), {"i": row_id})
        out = await call("edit_note", path="l.md", find="- [ ] Buy milk", content="- [x] Buy milk")
        await holder.rollback()
    data = (vault / "l.md").read_bytes()
    assert out == f"Updated note: l.md — content_hash: {content_hash_for_bytes(data)}", out
    assert b"- [x] Buy milk" in data
    assert (await usage(sessionmaker, "edit_note"))["task_refresh"] == "lagged:lock_timeout"
    assert await events(sessionmaker, "edit_note") == 1
    assert "Buy milk" in texts(await call("list_tasks"))  # stale until the pass
    await index()
    assert "Buy milk" not in texts(await call("list_tasks"))
    assert await converged(sessionmaker, vault, "l.md")


async def test_a_call_cancelled_while_its_refresh_waits_for_the_row_lock(sessionmaker, vault):
    """The cancellation scenario against real locks: the file stands, the
    attributed event exists, the task rows (and the row's task columns) are
    unchanged, and nothing partial was written."""
    put(vault, "k.md", TASKS)
    await index()
    row_sql = ("SELECT id, tasks_source_hash, tasks_refreshed_at, tasks_failed, tasks_truncated "
               "FROM notes_metadata WHERE file_path = 'k.md'")
    tasks_sql = ("SELECT t.id, t.line, t.text, t.state, t.task_key FROM note_tasks t JOIN notes_metadata m "
                 "ON m.id = t.note_id WHERE m.file_path = 'k.md' ORDER BY t.line")
    async with sessionmaker() as s:
        row_before = (await s.execute(text(row_sql))).one()
        tasks_before = (await s.execute(text(tasks_sql))).all()
    async with sessionmaker() as holder:
        await holder.execute(
            text("SELECT id FROM notes_metadata WHERE id = :i FOR UPDATE"), {"i": row_before.id})
        edit = asyncio.ensure_future(
            call("edit_note", path="k.md", find="- [ ] Buy milk", content="- [x] Buy milk"))
        # Wait (bounded) until the refresh's statement is blocked on the lock.
        for _ in range(400):
            waiting = await scalar(
                sessionmaker,
                "SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock' "
                "AND query ILIKE '%notes_metadata%FOR UPDATE%'")
            if waiting:
                break
            await asyncio.sleep(0.01)
        assert waiting, "the refresh never reached the row lock"
        edit.cancel()
        with pytest.raises(asyncio.CancelledError):
            await edit
        await holder.rollback()
    assert b"- [x] Buy milk" in (vault / "k.md").read_bytes()
    assert await events(sessionmaker, "edit_note") == 1
    async with sessionmaker() as s:
        assert (await s.execute(text(row_sql))).one() == row_before
        assert (await s.execute(text(tasks_sql))).all() == tasks_before
    assert await scalar(sessionmaker, "SELECT count(*) FROM usage_logs WHERE tool = 'edit_note'") == 0
    await index()
    assert await converged(sessionmaker, vault, "k.md")


async def test_write_file_creating_a_note_is_a_no_op_refresh(sessionmaker, vault):
    out = await call("write_file", path="fresh.md", content="- [ ] new\n", encoding="text")
    assert code(out) is None, out
    assert (await usage(sessionmaker, "write_file"))["task_refresh"] == "lagged:not_indexed"
    assert await scalar(sessionmaker, "SELECT count(*) FROM notes_metadata") == 0
    await index()
    assert texts(await call("list_tasks")) == ["new"]


async def test_an_edit_refresh_finishing_after_a_task_write_is_superseded(
    sessionmaker, vault, monkeypatch,
):
    put(vault, "o.md", TASKS)
    await index()
    ids = {t: i for t, i in re.findall(r"— id (\S+).*text: (.+)$",
                                       await call("list_tasks"), re.M)}
    ids = {v: k for k, v in ids.items()}
    real = task_index.refresh_note_tasks
    paused = asyncio.Event()
    release = asyncio.Event()

    async def refresh(**kw):
        # Only the edit's own refresh pauses (the first call): the task
        # write's published bytes contain the edit too.
        if not paused.is_set():
            paused.set()
            await release.wait()
        return await real(**kw)

    monkeypatch.setattr(task_index, "refresh_note_tasks", refresh)
    edit = asyncio.create_task(call(
        "edit_note", path="o.md", find="# List", content="# List edited"))
    await asyncio.wait_for(paused.wait(), timeout=20)
    out = await asyncio.wait_for(
        call("complete_task", path="o.md", id=ids["Call bank"]), timeout=20)
    assert code(out) is None and "index: refreshed" in out, out
    release.set()
    assert code(await edit) is None
    assert (await usage(sessionmaker, "edit_note"))["task_refresh"] == "lagged:superseded"
    assert texts(await call("list_tasks", status="done"))[0].startswith("Call bank")


# ══════════════════════════════════════════════════════════════════════════
# Convergence with a pass (design D7)
# ══════════════════════════════════════════════════════════════════════════


async def test_a_pass_that_scanned_before_the_edit_keeps_the_refresh(sessionmaker, vault):
    put(vault, "c.md", TASKS)
    await index()

    async def edit():
        out = await call("edit_note", path="c.md", find="- [ ] Buy milk", content="- [x] Buy milk")
        assert code(out) is None, out

    # D7.1: the pass buffered the old bytes; the edit lands after its scan.
    await index(between=edit)
    assert texts(await call("list_tasks")) == ["Call bank"]
    await index()
    assert await converged(sessionmaker, vault, "c.md")
    assert texts(await call("list_tasks")) == ["Call bank"]


async def test_a_pass_that_scanned_before_the_delete_keeps_the_clear(sessionmaker, vault):
    put(vault, "d.md", TASKS)
    put(vault, "keep.md", "kept\n")  # an emptied root is refused by the pass (#309)
    await index()

    async def delete():
        assert code(await call("delete_note", path="d.md")) is None

    # D7.9.
    await index(between=delete)
    assert texts(await call("list_tasks")) == []
    await index()
    assert await scalar(
        sessionmaker, "SELECT count(*) FROM notes_metadata WHERE file_path = 'd.md'") == 0


# ══════════════════════════════════════════════════════════════════════════
# Tenancy
# ══════════════════════════════════════════════════════════════════════════


async def test_another_users_note_at_the_same_path_is_untouched(sessionmaker, vault, monkeypatch):
    roots = {}
    async with sessionmaker() as session:
        for uid in (7, 8):
            roots[uid] = vault / f"u{uid}"
            roots[uid].mkdir()
            await session.execute(text(
                "INSERT INTO users (id, username, password_hash, vault_path, is_active) "
                "VALUES (:i, :u, 'x', :v, true)"),
                {"i": uid, "u": f"user{uid}", "v": str(roots[uid])})
        await session.commit()
        for uid in (7, 8):
            await vault_service.warm_user_vault_cache(session, user_id=uid)
    monkeypatch.setattr(settings, "multi_user_mode", True, raising=False)
    vault_overlap.publish_synthetic_snapshot()
    for uid in (7, 8):
        put(roots[uid], "Notes/a.md", TASKS)
        await index(uid)

    snapshot_sql = (
        "SELECT m.tasks_source_hash, m.tasks_refreshed_at, m.tasks_failed, m.tasks_truncated, "
        "array_agg(t.text ORDER BY t.line) FROM notes_metadata m "
        "LEFT JOIN note_tasks t ON t.note_id = m.id WHERE m.user_id = 8 "
        "GROUP BY m.id")

    async def eight():
        async with sessionmaker() as session:
            return (await session.execute(text(snapshot_sql))).all()

    before = await eight()
    tokens = [(current_user_id, current_user_id.set(7)),
              (current_vault_root, current_vault_root.set((7, roots[7])))]
    try:
        out = await call("edit_note", path="Notes/a.md", find="- [ ] Buy milk",
                         content="- [x] Buy milk")
        assert code(out) is None, out
        assert texts(await call("list_tasks")) == ["Call bank"]
        assert code(await call("delete_note", path="Notes/a.md")) is None
        assert texts(await call("list_tasks", status="all")) == []
    finally:
        for var, token in reversed(tokens):
            var.reset(token)
    assert await eight() == before
    vault_service.clear_user_vault_cache()
