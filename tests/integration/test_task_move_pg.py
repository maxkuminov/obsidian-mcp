"""`move_task` end to end against PostgreSQL (#430, tasks.md 2.5).

Every call goes through the REGISTERED tool (`mcp.call_tool`: the SDK's
argument validation, the server wrapper, `_tracked`'s gates and the impl),
over a real vault directory, the real extractor and index pass, the real
write-time refresh (twice per move), the real change feed and the real usage
log. Only the embedding provider is a stand-in.

Covers: list → move → list with no pass (read-your-writes for both notes),
the lagging destination refresh (Q10: the move completes and names the
lagging note, the task is in neither listing until the next pass), two
attributed `modified` events with the destination's first, the opposite-moves
race (no deadlock, no loss), tenancy (another user's same-path notes
untouched), and convergence of `tasks_source_hash` after the next pass.

Skipped unless `PGVECTOR_TEST_ADMIN_URL` is set (see `_harness`); uses its
own throwaway database.
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
from src.services import change_feed, indexer, indexer_health, rate_limits, vault_overlap
from src.services import embeddings as embeddings_service
from src.services import vault as vault_service
from src.services.note_hash import canonical_note_hash
import _harness

pytestmark = [
    _harness.requires_pgvector,
    pytest.mark.asyncio(loop_scope="module"),
]

DIM = 8
VECTOR = [1.0] + [0.0] * (DIM - 1)
NOW = dt.datetime(2026, 11, 16, 12, 0, tzinfo=dt.timezone.utc)


@pytest.fixture(scope="module")
def migrated_url():
    yield from _harness.throwaway_database("task_move_430", DIM)


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


def put(root, rel: str, body: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body.encode("utf-8"))


def disk(root, rel: str) -> str:
    return (root / rel).read_bytes().decode("utf-8")


async def index(uid: int | None = None):
    await indexer.index_vault(user_id=uid)


async def call(name: str, **args) -> str:
    content = await mcp.call_tool(name, args)
    blocks = content[0] if isinstance(content, tuple) else content
    return "".join(getattr(b, "text", "") for b in blocks)


def payload(out: str) -> dict | None:
    last = out.rstrip().splitlines()[-1]
    return json.loads(last[len("MCP-REFUSAL "):]) if last.startswith("MCP-REFUSAL ") else None


def code(out: str) -> str | None:
    p = payload(out)
    return p["code"] if p else None


ROW = re.compile(r"^- \[.\] `([^`]+)`:(\d+) — id ([0-9a-f]{12}(?:\.\d+of\d+)?)")


def listed(listing: str) -> dict[str, tuple[str, str]]:
    """`text -> (path, id)` from a full-detail `list_tasks` page."""
    out = {}
    for line in listing.splitlines():
        m = ROW.match(line)
        if m:
            out[line.rsplit("text: ", 1)[1]] = (m.group(1), m.group(3))
    return out


async def scalar(sessionmaker, sql: str, **params):
    async with sessionmaker() as session:
        return (await session.execute(text(sql), params)).scalar()


INBOX = "# Inbox\n- [ ] Call the bank 📅 2026-11-20\n    - ask about the fee\n    - [ ] find the account number\n- [ ] Other\n"
MONEY = "# Money\n- [ ] Pay rent\n"


# ══════════════════════════════════════════════════════════════════════════


async def test_list_move_list(sessionmaker, vault):
    """Scenario: List, move, list — with no index pass, the task is listed in
    the destination with its id and not in the source; then the next pass
    converges both notes' `tasks_source_hash`."""
    put(vault, "Inbox.md", INBOX)
    put(vault, "Projects/Money.md", MONEY)
    await index()
    before = listed(await call("list_tasks"))
    path, tid = before["Call the bank 📅 2026-11-20"]
    assert path == "Inbox.md"
    out = await call("move_task", path="Inbox.md", id=tid, to_path="Projects/Money.md")
    assert code(out) is None, out
    assert "index (destination): refreshed" in out and "index (source): refreshed" in out
    after = listed(await call("list_tasks"))
    assert after["Call the bank 📅 2026-11-20"] == ("Projects/Money.md", tid)
    assert after["find the account number"][0] == "Projects/Money.md"
    assert after["Other"][0] == "Inbox.md"
    await index()
    assert listed(await call("list_tasks")) == after
    for rel in ("Inbox.md", "Projects/Money.md"):
        assert await scalar(
            sessionmaker,
            "SELECT tasks_source_hash = content_hash FROM notes_metadata WHERE file_path = :p",
            p=rel), rel


async def test_two_events_attributed_destination_first(sessionmaker, vault):
    """Scenario: Two events, attributed; change-feed Scenario: A task move
    records both notes."""
    put(vault, "Inbox.md", "- [ ] a\n")
    put(vault, "B.md", "")
    await index()
    (_, tid) = listed(await call("list_tasks"))["a"]
    out = await call("move_task", path="Inbox.md", id=tid, to_path="B.md")
    assert code(out) is None, out
    async with sessionmaker() as session:
        events = (await session.execute(text(
            "SELECT * FROM note_events WHERE source = 'mcp' ORDER BY seq"))).fetchall()
    assert [(e.kind, e.path, e.tool) for e in events] == [
        ("modified", "B.md", "move_task"), ("modified", "Inbox.md", "move_task")]
    for e in events:
        assert (e.actor_kind, e.actor_label, e.principal_kind, e.principal_ref) == (
            "api_key", "laptop", "api_key", "12")
        assert e.content_hash == canonical_note_hash((vault / e.path).read_bytes())


async def test_a_lagging_destination_refresh_is_reported_not_hidden(sessionmaker, vault):
    """Scenario: A lagging destination refresh is reported, not hidden (Q10,
    L13): the move completes, names the lagging note, the task is in neither
    listing until the next pass, then in the destination."""
    put(vault, "Inbox.md", "- [ ] lagging\n- [ ] stays\n")
    put(vault, "Dest.md", "- [ ] there\n")
    await index()
    (_, tid) = listed(await call("list_tasks"))["lagging"]
    async with sessionmaker() as holder:
        await holder.execute(text(
            "SELECT id FROM notes_metadata WHERE file_path = 'Dest.md' FOR UPDATE"))
        out = await call("move_task", path="Inbox.md", id=tid, to_path="Dest.md")
        await holder.rollback()
    assert code(out) is None, out
    assert "index (destination): may lag (lock_timeout)" in out
    assert "index (source): refreshed" in out
    assert disk(vault, "Dest.md") == "- [ ] there\n- [ ] lagging\n"
    assert "lagging" not in listed(await call("list_tasks"))
    await index()
    assert listed(await call("list_tasks"))["lagging"] == ("Dest.md", tid)


async def test_opposite_moves_do_not_deadlock(sessionmaker, vault):
    """Scenario: Opposite moves do not deadlock (scenario 7): released together,
    repeatedly; every task ends in exactly one note, or in two with a
    `task_move_incomplete` answer."""
    for n in range(10):
        put(vault, "A.md", f"- [ ] from a {n}\n")
        put(vault, "B.md", f"- [ ] from b {n}\n")
        await index()
        ids = listed(await call("list_tasks"))
        a_out, b_out = await asyncio.gather(
            call("move_task", path="A.md", id=ids[f"from a {n}"][1], to_path="B.md"),
            call("move_task", path="B.md", id=ids[f"from b {n}"][1], to_path="A.md"),
        )
        texts = disk(vault, "A.md") + disk(vault, "B.md")
        for line, out in ((f"- [ ] from a {n}", a_out), (f"- [ ] from b {n}", b_out)):
            copies = texts.count(line)
            assert copies >= 1, (n, out)  # never a loss
            if copies == 2:
                assert code(out) == "task_move_incomplete", (n, out)
            else:
                assert code(out) in (None, "concurrent_write"), (n, out)


async def test_tenancy_another_users_same_path_notes_are_untouched(sessionmaker, vault, monkeypatch):
    """Tenancy: user 8's identically named notes are untouched by user 7's
    move, and neither listing crosses."""
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
        put(roots[uid], "Inbox.md", "- [ ] shared text\n")
        put(roots[uid], "Done.md", "")
        await index(uid)

    def as_user(uid):
        return [(current_user_id, current_user_id.set(uid)),
                (current_vault_root, current_vault_root.set((uid, roots[uid])))]

    tokens = as_user(8)
    try:
        eight_before = await call("list_tasks", status="all")
    finally:
        for var, token in reversed(tokens):
            var.reset(token)
    tokens = as_user(7)
    try:
        (_, tid) = listed(await call("list_tasks"))["shared text"]
        out = await call("move_task", path="Inbox.md", id=tid, to_path="Done.md")
        assert code(out) is None, out
        assert listed(await call("list_tasks"))["shared text"] == ("Done.md", tid)
    finally:
        for var, token in reversed(tokens):
            var.reset(token)
    tokens = as_user(8)
    try:
        assert await call("list_tasks", status="all") == eight_before
    finally:
        for var, token in reversed(tokens):
            var.reset(token)
    assert disk(roots[8], "Inbox.md") == "- [ ] shared text\n" and disk(roots[8], "Done.md") == ""
    assert disk(roots[7], "Done.md") == "- [ ] shared text" and disk(roots[7], "Inbox.md") == ""
    vault_service.clear_user_vault_cache()


async def test_a_move_records_one_usage_row_without_task_text(sessionmaker, vault):
    put(vault, "Inbox.md", "- [ ] Secret errand\n")
    put(vault, "B.md", "")
    await index()
    (_, tid) = listed(await call("list_tasks"))["Secret errand"]
    out = await call("move_task", path="Inbox.md", id=tid, to_path="B.md")
    assert code(out) is None, out
    async with sessionmaker() as session:
        rows = (await session.execute(text(
            "SELECT params FROM usage_logs WHERE tool = 'move_task'"))).fetchall()
    (row,) = rows
    params = row.params if isinstance(row.params, dict) else json.loads(row.params)
    assert params["move_outcome"] == "moved"
    assert params["task_refresh"] == "refreshed" and params["task_refresh_to"] == "refreshed"
    assert "Secret errand" not in json.dumps(params, ensure_ascii=False)
