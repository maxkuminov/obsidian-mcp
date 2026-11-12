"""The task tools end to end against PostgreSQL (#428, tasks.md 7.6).

Every call goes through the REGISTERED tool (`mcp.call_tool`: the SDK's
argument validation, the server wrapper, `_tracked`'s gates and the impl),
over a real vault directory, the real extractor and the real index pass, the
real write-time refresh, the real change feed and the real usage log. Only
the embedding provider is a stand-in (the pass embeds; nothing here searches).

Covers the `task-write` scenarios: read-your-writes (list → complete → list
with no pass), each write tool and its dry run, a recurring completion,
wrong-line protection (identical lines, suffixed ids, swapped lines, a count
change, an edited line), CRLF / blockquote / fence / frontmatter look-alike
notes, required mode, a read-only grant, the attributed change-feed event
after the refresh, usage params free of task text, two-user tenancy with
same-path notes, parallel completions, a refresh that lags behind a held row
lock, `add_task`'s duplicate refusal and similar listing, and `task_agenda`'s
disjoint sections whose continuations `list_tasks` accepts.

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
from src.services import change_feed, indexer, indexer_health, rate_limits, task_line, vault_overlap
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
NOW = dt.datetime(2026, 11, 10, 12, 0, tzinfo=dt.timezone.utc)
TODAY = "2026-11-10"
WRITE_TOOLS = {"complete_task", "update_task", "add_task"}


@pytest.fixture(scope="module")
def migrated_url():
    yield from _harness.throwaway_database("task_tools_428", DIM)


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


def disk(root, rel: str) -> bytes:
    return (root / rel).read_bytes()


async def index(uid: int | None = None):
    await indexer.index_vault(user_id=uid)


async def call(name: str, **args) -> str:
    """One call through the registered tool, as a client makes it."""
    content = await mcp.call_tool(name, args)
    blocks = content[0] if isinstance(content, tuple) else content
    return "".join(getattr(b, "text", "") for b in blocks)


def code(out: str) -> str | None:
    last = out.rstrip().splitlines()[-1]
    return json.loads(last[len("MCP-REFUSAL "):])["code"] if last.startswith("MCP-REFUSAL ") else None


ROW_ID = re.compile(r"`:(\d+) — id ([0-9a-f]{12}(?:\.\d+of\d+)?)")


def ids_by_text(listing: str) -> dict[str, str]:
    """`text -> id` from a full-detail `list_tasks` page."""
    out = {}
    for line in listing.splitlines():
        if not line.startswith("- ["):
            continue
        m = ROW_ID.search(line)
        assert m, line
        out[line.rsplit("text: ", 1)[1]] = m.group(2)
    return out


def rows(listing: str) -> list[str]:
    return [ln for ln in listing.splitlines() if ln.startswith("- [")]


def new_id(out: str, label: str = "id") -> str:
    m = re.search(rf"^{label}: (\S+)$", out, re.M)
    assert m, out
    return m.group(1)


def key(line: str) -> str:
    return task_line.task_key(task_line.parse(line).full_text)


async def scalar(sessionmaker, sql: str, **params):
    async with sessionmaker() as session:
        return (await session.execute(text(sql), params)).scalar()


# ══════════════════════════════════════════════════════════════════════════
# Read-your-writes and each write tool
# ══════════════════════════════════════════════════════════════════════════


async def test_list_complete_list_needs_no_pass(sessionmaker, vault):
    put(vault, "Projects/Alpha.md", "# Inbox\n- [ ] Send invoice 📅 2026-11-10\n- [ ] Call Bob\n")
    await index()
    first = await call("list_tasks")
    ids = ids_by_text(first)
    assert set(ids) == {"Send invoice 📅 2026-11-10", "Call Bob"}

    out = await call("complete_task", path="Projects/Alpha.md", id=ids["Send invoice 📅 2026-11-10"])
    assert code(out) is None, out
    assert "index: refreshed" in out
    done_id = new_id(out)
    assert disk(vault, "Projects/Alpha.md") == (
        "# Inbox\n- [x] Send invoice 📅 2026-11-10 ✅ 2026-11-10\n- [ ] Call Bob\n".encode())

    # No pass in between: the listing already reflects the write.
    assert set(ids_by_text(await call("list_tasks"))) == {"Call Bob"}
    every = ids_by_text(await call("list_tasks", status="all"))
    assert every["Send invoice 📅 2026-11-10 ✅ 2026-11-10"] == done_id
    row = [r for r in rows(await call("list_tasks", status="done"))]
    assert len(row) == 1 and row[0].startswith("- [x] `Projects/Alpha.md`:2 — id " + done_id)

    # And the next pass converges onto the same rows.
    await index()
    assert ids_by_text(await call("list_tasks", status="all")) == every
    assert await scalar(
        sessionmaker,
        "SELECT tasks_source_hash = content_hash FROM notes_metadata WHERE file_path = :p",
        p="Projects/Alpha.md")


async def test_a_recurring_completion_lists_both_lines(sessionmaker, vault):
    put(vault, "r.md", "- [ ] Pay rent 🔁 every month 📅 2026-11-01\n")
    await index()
    (tid,) = ids_by_text(await call("list_tasks")).values()
    out = await call("complete_task", path="r.md", id=tid)
    assert code(out) is None, out
    assert "next occurrence, line 1 (above): - [ ] Pay rent 🔁 every month 📅 2026-12-01" in out
    assert disk(vault, "r.md") == (
        "- [ ] Pay rent 🔁 every month 📅 2026-12-01\n"
        "- [x] Pay rent 🔁 every month 📅 2026-11-01 ✅ 2026-11-10\n").encode()
    listing = await call("list_tasks", status="all", order_by="path")
    assert rows(listing)[0].startswith("- [ ] `r.md`:1 — id " + new_id(out, "next id"))
    assert rows(listing)[1].startswith("- [x] `r.md`:2 — id " + new_id(out))


async def test_every_write_tool_and_its_dry_run(sessionmaker, vault):
    body = "# Today\n- [ ] Pay 🔁 every month 📅 2026-11-05\n\n# Later\n"
    put(vault, "w.md", body)
    await index()
    (tid,) = ids_by_text(await call("list_tasks")).values()

    for name, args in (
        ("complete_task", dict(id=tid)),
        ("update_task", dict(id=tid, scheduled="2026-11-03")),
        ("add_task", dict(text="New thing", section="Today")),
    ):
        out = await call(name, path="w.md", dry_run=True, **args)
        assert out.startswith("Dry run — nothing was written."), (name, out)
        # The heading says what would happen; no dry-run line asserts a write.
        heading = out.splitlines()[1]
        assert heading.startswith({"complete_task": "Would complete task in `w.md`",
                                   "update_task": "Would update task in `w.md`",
                                   "add_task": "Would add task to `w.md`"}[name]), (name, out)
        assert not any(w in out for w in ("Completed task", "Updated task", "Added task"))
        assert "diff:" in out
        assert disk(vault, "w.md") == body.encode(), name
    assert await scalar(sessionmaker, "SELECT count(*) FROM note_events WHERE source = 'mcp'") == 0

    out = await call("update_task", path="w.md", id=tid, scheduled="2026-11-03")
    assert code(out) is None, out
    assert disk(vault, "w.md").decode().splitlines()[1] == "- [ ] Pay 🔁 every month ⏳ 2026-11-03 📅 2026-11-05"
    tid = new_id(out)
    out = await call("add_task", path="w.md", text="New thing", section="Today", due="tomorrow")
    assert code(out) is None, out
    assert "due: tomorrow=2026-11-11" in out and "timezone: UTC" in out
    assert disk(vault, "w.md").decode() == (
        "# Today\n- [ ] Pay 🔁 every month ⏳ 2026-11-03 📅 2026-11-05\n"
        "- [ ] New thing 📅 2026-11-11\n\n# Later\n")
    listed = ids_by_text(await call("list_tasks", path="w.md"))
    assert listed["New thing 📅 2026-11-11"] == new_id(out)
    assert listed["Pay 🔁 every month ⏳ 2026-11-03 📅 2026-11-05"] == tid


# ══════════════════════════════════════════════════════════════════════════
# Wrong-line protection
# ══════════════════════════════════════════════════════════════════════════


async def test_identical_lines_suffixed_ids_and_swaps(sessionmaker, vault):
    body = "- [ ] Call\n    - about the lease\n- [ ] Call\n    - about the car\n"
    put(vault, "dup.md", body)
    await index()
    listing = await call("list_tasks", path="dup.md")
    k = key("- [ ] Call")
    assert [ROW_ID.search(r).group(2) for r in rows(listing)] == [f"{k}.1of2", f"{k}.2of2"]

    out = await call("complete_task", path="dup.md", id=k)
    assert code(out) == "task_ambiguous" and f"{k}.1of2" in out and f"{k}.2of2" in out
    out = await call("complete_task", path="dup.md", id=f"{k}.2of2")
    assert code(out) == "precondition_required" and "read_note" in out
    assert disk(vault, "dup.md") == body.encode()

    old_hash = content_hash_for_bytes(body.encode())
    swapped = "- [ ] Call\n    - about the car\n- [ ] Call\n    - about the lease\n"
    put(vault, "dup.md", swapped)
    out = await call("complete_task", path="dup.md", id=f"{k}.1of2", expected_hash=old_hash)
    assert code(out) == "stale_precondition"
    assert disk(vault, "dup.md") == swapped.encode()

    out = await call("complete_task", path="dup.md", id=f"{k}.1of2",
                     expected_hash=content_hash_for_bytes(swapped.encode()))
    assert code(out) is None, out
    assert disk(vault, "dup.md").decode() == (
        f"- [x] Call ✅ {TODAY}\n    - about the car\n- [ ] Call\n    - about the lease\n")


async def test_a_count_change_and_an_edited_line(sessionmaker, vault):
    put(vault, "c.md", "- [ ] Ping\n- [ ] Ping\n- [ ] Ping\n- [ ] Pay rent\n")
    await index()
    listing = await call("list_tasks", path="c.md")
    k = key("- [ ] Ping")
    pay = ids_by_text(listing)["Pay rent"]
    # One identical line removed after the listing: `.1of3` no longer counts.
    now = put(vault, "c.md", "- [ ] Ping\n- [ ] Ping\n- [ ] Pay rent today\n")
    out = await call("complete_task", path="c.md", id=f"{k}.1of3",
                     expected_hash=content_hash_for_bytes(now))
    assert code(out) == "task_changed"
    # The other line was edited after the listing: its old id finds nothing.
    out = await call("update_task", path="c.md", id=pay, due="2026-12-01")
    assert code(out) == "task_not_found"
    assert disk(vault, "c.md") == now


async def test_a_short_text_never_matches_inside_a_longer_one(sessionmaker, vault):
    put(vault, "s.md", "- [ ] Call Bob\n- [ ] Call Bob about the lease\n")
    await index()
    ids = ids_by_text(await call("list_tasks", path="s.md"))
    out = await call("complete_task", path="s.md", id=ids["Call Bob"])
    assert code(out) is None, out
    assert disk(vault, "s.md").decode() == f"- [x] Call Bob ✅ {TODAY}\n- [ ] Call Bob about the lease\n"


async def test_crlf_blockquote_fence_and_frontmatter_lookalikes(sessionmaker, vault):
    put(vault, "crlf.md", b"- [ ] one\r\n> \t1. [ ] Ship it  \r\n- [ ] three\n")
    put(vault, "shapes.md", "---\ntodo: |\n  - [ ] yaml\n---\n```\n- [ ] fenced\n```\n- [ ] real\n")
    await index()
    ids = ids_by_text(await call("list_tasks", path="crlf.md"))
    out = await call("complete_task", path="crlf.md", id=ids["Ship it"])
    assert code(out) is None, out
    assert disk(vault, "crlf.md") == f"- [ ] one\r\n> \t1. [x] Ship it ✅ {TODAY}  \r\n- [ ] three\n".encode()

    assert list(ids_by_text(await call("list_tasks", path="shapes.md"))) == ["real"]
    before = disk(vault, "shapes.md")
    for ghost in ("- [ ] yaml", "- [ ] fenced"):
        out = await call("complete_task", path="shapes.md", id=key(ghost))
        assert code(out) == "task_not_found", ghost
    assert disk(vault, "shapes.md") == before


async def test_required_mode_is_satisfied_by_a_bare_id(sessionmaker, vault, monkeypatch):
    monkeypatch.setattr(settings, "write_precondition_required", True)
    put(vault, "req.md", "- [ ] a\n- [ ] b\n")
    await index()
    ids = ids_by_text(await call("list_tasks", path="req.md"))
    out = await call("complete_task", path="req.md", id=ids["a"])
    assert code(out) is None, out
    out = await call("update_task", path="req.md", id=ids["b"], priority="high",
                     expected_hash="sha256:" + "0" * 64)
    assert code(out) == "stale_precondition"
    out = await call("add_task", path="req.md", text="c")
    assert code(out) is None, out
    # The generic writer still refuses an unguarded edit in this mode.
    out = await call("edit_note", path="req.md", content="x", append=True)
    assert code(out) == "precondition_required"


# ══════════════════════════════════════════════════════════════════════════
# Permission, attribution, usage
# ══════════════════════════════════════════════════════════════════════════


async def test_a_read_only_grant_neither_lists_nor_runs_the_write_tools(sessionmaker, vault):
    put(vault, "ro.md", "- [ ] a\n")
    await index()
    tid = key("- [ ] a")
    p = current_principal.set(("oauth", "grant-ro"))
    q = current_permission.set("read")
    try:
        listed = [t.name for t in await mcp.list_tools()]
        assert len(listed) == 20 and "task_agenda" in listed
        assert not WRITE_TOOLS & set(listed)
        for name, args in (("add_task", dict(text="b")), ("complete_task", dict(id=tid)),
                           ("update_task", dict(id=tid, due="today"))):
            assert code(await call(name, path="ro.md", **args)) == "permission_denied", name
        assert code(await call("list_tasks")) is None
    finally:
        current_permission.reset(q)
        current_principal.reset(p)
    assert disk(vault, "ro.md") == b"- [ ] a\n"


async def test_the_event_is_attributed_and_recorded_after_the_refresh(sessionmaker, vault):
    put(vault, "ev.md", "- [ ] a\n")
    await index()
    (tid,) = ids_by_text(await call("list_tasks", path="ev.md")).values()
    out = await call("complete_task", path="ev.md", id=tid)
    assert code(out) is None and "index: refreshed" in out
    async with sessionmaker() as session:
        events = (await session.execute(text(
            "SELECT * FROM note_events WHERE source = 'mcp' AND path = 'ev.md'"))).fetchall()
        refreshed_at = (await session.execute(text(
            "SELECT tasks_refreshed_at FROM notes_metadata WHERE file_path = 'ev.md'"))).scalar()
    (event,) = events
    assert (event.kind, event.tool, event.actor_kind, event.actor_label,
            event.principal_kind, event.principal_ref) == (
        "modified", "complete_task", "api_key", "laptop", "api_key", "12")
    assert event.content_hash == canonical_note_hash(disk(vault, "ev.md"))
    assert refreshed_at is not None and event.occurred_at >= refreshed_at


async def test_usage_params_hold_no_task_text(sessionmaker, vault):
    put(vault, "u.md", "- [ ] a\n")
    await index()
    out = await call("add_task", path="u.md", text="Call the bank about the mortgage",
                     context="ref 1234")
    assert code(out) is None, out
    (tid,) = [v for k, v in ids_by_text(await call("list_tasks", path="u.md")).items()
              if k.startswith("Call the bank")]
    out = await call("update_task", path="u.md", id=tid, text="Phone the bank",
                     append_context="left a message")
    assert code(out) is None, out
    async with sessionmaker() as session:
        params = (await session.execute(text(
            "SELECT tool, params FROM usage_logs WHERE tool IN ('add_task', 'update_task') "
            "ORDER BY id"))).fetchall()
    assert [p.tool for p in params] == ["add_task", "update_task"]
    flat = json.dumps([p.params for p in params], ensure_ascii=False)
    for secret in ("Call the bank", "mortgage", "ref 1234", "Phone the bank", "left a message"):
        assert secret not in flat, secret
    add, update = (p.params for p in params)
    assert add["path"] == "u.md" and add["task_refresh"] == "refreshed" and "similar_count" in add
    assert update["fields"] == ["append_context", "text"] and update["id"] == tid


# ══════════════════════════════════════════════════════════════════════════
# Tenancy
# ══════════════════════════════════════════════════════════════════════════


async def _two_users(sessionmaker, vault, monkeypatch):
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
    return roots


class As:
    def __init__(self, uid, root):
        self.uid, self.root = uid, root

    def __enter__(self):
        self.t = [(current_user_id, current_user_id.set(self.uid)),
                  (current_vault_root, current_vault_root.set((self.uid, self.root)))]

    def __exit__(self, *a):
        for var, token in reversed(self.t):
            var.reset(token)


async def test_two_users_with_the_same_path_never_cross(sessionmaker, vault, monkeypatch):
    roots = await _two_users(sessionmaker, vault, monkeypatch)
    body = "- [ ] Renew passport #admin\n- [ ] Water plants\n"
    for uid in (7, 8):
        put(roots[uid], "Projects/Alpha.md", body)
        await index(uid)
    with As(8, roots[8]):
        eight_before = await call("list_tasks", status="all")
    with As(7, roots[7]):
        ids = ids_by_text(await call("list_tasks"))
        out = await call("complete_task", path="Projects/Alpha.md", id=ids["Water plants"])
        assert code(out) is None and "index: refreshed" in out
        # User 8 holds an identical open task: not a duplicate for user 7,
        # and never listed as similar.
        put(roots[7], "Inbox.md", "")
        await index(7)
        out = await call("add_task", path="Inbox.md", text="Renew passport", tags=["admin"])
        assert code(out) == "duplicate_task"  # user 7's OWN Projects/Alpha.md
        assert "u8" not in out
        out = await call("add_task", path="Inbox.md", text="Renew passport", tags=["admin"],
                         allow_duplicate=True)
        assert code(out) is None, out
    with As(8, roots[8]):
        assert await call("list_tasks", status="all") == eight_before
        out = await call("add_task", path="Projects/Alpha.md", text="Water plants")
        assert code(out) == "duplicate_task"  # its own still-open line, in the note's bytes
    assert disk(roots[8], "Projects/Alpha.md") == body.encode()
    vault_service.clear_user_vault_cache()


async def test_a_duplicate_held_only_by_another_user_is_not_one(sessionmaker, vault, monkeypatch):
    roots = await _two_users(sessionmaker, vault, monkeypatch)
    put(roots[8], "Other.md", "- [ ] Renew passport for the trip\n- [ ] Book dentist\n")
    put(roots[7], "Mine.md", "- [ ] something\n")
    await index(7)
    await index(8)
    with As(7, roots[7]):
        out = await call("add_task", path="Mine.md", text="Book dentist")
        assert code(out) is None, out
        assert "similar open tasks: none" in out
        out = await call("add_task", path="Mine.md", text="Renew passport")
        assert "similar open tasks: none" in out
    vault_service.clear_user_vault_cache()


# ══════════════════════════════════════════════════════════════════════════
# Parallel writes and the lagging refresh
# ══════════════════════════════════════════════════════════════════════════


async def test_three_parallel_completions_of_different_tasks_all_succeed(sessionmaker, vault):
    body = "# Today\n- [ ] one\n- [ ] two\nprose between\n- [ ] three\n- [ ] four\n"
    put(vault, "par.md", body)
    await index()
    ids = ids_by_text(await call("list_tasks", path="par.md"))
    outs = await asyncio.gather(*(
        call("complete_task", path="par.md", id=ids[name]) for name in ("one", "two", "three")
    ))
    assert [code(o) for o in outs] == [None, None, None], outs
    assert disk(vault, "par.md").decode() == (
        f"# Today\n- [x] one ✅ {TODAY}\n- [x] two ✅ {TODAY}\nprose between\n"
        f"- [x] three ✅ {TODAY}\n- [ ] four\n")
    assert await scalar(sessionmaker,
                        "SELECT count(*) FROM note_events WHERE path = 'par.md' AND source = 'mcp'") == 3
    assert set(ids_by_text(await call("list_tasks", path="par.md"))) == {"four"}


async def test_two_parallel_completions_of_one_task_succeed_once(sessionmaker, vault):
    put(vault, "once.md", "- [ ] only\n- [ ] other\n")
    await index()
    tid = ids_by_text(await call("list_tasks", path="once.md"))["only"]
    outs = await asyncio.gather(*(call("complete_task", path="once.md", id=tid) for _ in range(2)))
    codes = sorted(str(code(o)) for o in outs)
    assert codes[0] == "None" and codes[1] in {"already_completed", "task_not_found"}, outs
    assert disk(vault, "once.md").decode() == f"- [x] only ✅ {TODAY}\n- [ ] other\n"


async def test_a_held_row_lock_lags_the_listing_not_the_write(sessionmaker, vault):
    put(vault, "lock.md", "- [ ] a\n")
    await index()
    tid = ids_by_text(await call("list_tasks", path="lock.md"))["a"]
    async with sessionmaker() as holder:
        await holder.execute(text(
            "SELECT id FROM notes_metadata WHERE file_path = 'lock.md' FOR UPDATE"))
        out = await call("update_task", path="lock.md", id=tid, priority="high")
        await holder.rollback()
    assert code(out) is None, out
    assert "index: may lag (lock_timeout)" in out
    assert disk(vault, "lock.md") == "- [ ] a ⏫\n".encode()
    assert "a" in ids_by_text(await call("list_tasks", path="lock.md"))  # stale until a pass
    await index()
    assert set(ids_by_text(await call("list_tasks", path="lock.md"))) == {"a ⏫"}


# ══════════════════════════════════════════════════════════════════════════
# add_task duplicates and similar tasks
# ══════════════════════════════════════════════════════════════════════════


async def test_add_task_duplicate_then_allowed_then_similar(sessionmaker, vault):
    put(vault, "Projects/Alpha.md", "- [ ] Renew passport #admin 📅 2026-12-01\n- [ ] Renew passport for the trip\n")
    put(vault, "Inbox.md", "# Inbox\n")
    await index()
    alpha = ids_by_text(await call("list_tasks", path="Projects/Alpha.md"))
    out = await call("add_task", path="Inbox.md", text="renew  Passport", tags=["admin"])
    assert code(out) == "duplicate_task"
    assert f"`Projects/Alpha.md`:1 id {alpha['Renew passport #admin 📅 2026-12-01']}" in out
    assert disk(vault, "Inbox.md") == b"# Inbox\n"
    out = await call("add_task", path="Inbox.md", text="renew  Passport", tags=["admin"],
                     allow_duplicate=True)
    assert code(out) is None, out
    assert disk(vault, "Inbox.md") == b"# Inbox\n- [ ] renew  Passport #admin\n"
    out = await call("add_task", path="Inbox.md", text="Renew passport")
    assert code(out) is None, out
    assert f"`Projects/Alpha.md`:2 id {alpha['Renew passport for the trip']}" in out


# ══════════════════════════════════════════════════════════════════════════
# task_agenda
# ══════════════════════════════════════════════════════════════════════════


CONTINUE = re.compile(r"^continue: list_tasks\((.*)\) with this call's", re.M)
ARG = re.compile(r'(\w+)=("(?:[^"\\]|\\.)*"|true|false|-?\d+)')


def continuations(out: str) -> list[dict]:
    return [{k: json.loads(v) for k, v in ARG.findall(m)} for m in CONTINUE.findall(out)]


async def test_agenda_sections_are_disjoint_and_continue_in_list_tasks(sessionmaker, vault):
    lines = [f"- [ ] overdue {i:02d} 📅 2026-10-{i + 1:02d}" for i in range(30)]
    lines += ["- [ ] today a 📅 2026-11-10", "- [ ] sched undated ⏳ 2026-11-09",
              "- [ ] sched later ⏳ 2026-11-01 📅 2026-12-30", "- [ ] upcoming 📅 2026-11-12",
              "- [ ] beyond 📅 2026-12-01", "- [x] done 📅 2026-11-01"]
    put(vault, "ag.md", "\n".join(lines) + "\n")
    await index()
    out = await call("task_agenda")
    assert code(out) is None, out
    assert "## Overdue (due on or before 2026-11-09) — total: 30 tasks — showing 25" in out
    shown = [ln for ln in out.splitlines() if ln.startswith("- [")]
    texts = [ln.rsplit(" — ", 1)[1] for ln in shown]
    assert len(texts) == len(set(texts)), "a task appeared in two sections"
    assert "beyond" not in " ".join(texts) and "done" not in texts
    for wanted in ("today a", "sched undated", "sched later", "upcoming"):
        assert sum(t.startswith(wanted) for t in texts) == 1, wanted

    (cont,) = continuations(out)
    assert cont["due_by"] == "2026-11-09" and cont["order_by"] == "due" and "cursor" in cont
    page = await call("list_tasks", **cont)
    assert code(page) is None, page
    rest = [r.rsplit("text: ", 1)[1] for r in rows(page)]
    assert rest == [f"overdue {i:02d} 📅 2026-10-{i + 1:02d}" for i in range(25, 30)]


# ══════════════════════════════════════════════════════════════════════════
# A backtick in a note path (#428 Codex round 2)
# ══════════════════════════════════════════════════════════════════════════


def _bare_backticks(line: str) -> int:
    count, i = 0, 0
    while i < len(line):
        if line[i] == "\\":
            i += 2
            continue
        count += line[i] == "`"
        i += 1
    return count


def _path_field(line: str) -> str:
    i, start = 0, None
    while i < len(line):
        if line[i] == "\\":
            i += 2
            continue
        if line[i] == "`":
            if start is None:
                start = i + 1
            else:
                return line[start:i]
        i += 1
    raise AssertionError(line)


def _unescape(shown: str) -> str:
    """The rule the `list_tasks` description states."""
    out, i = [], 0
    simple = {"\\": "\\", "n": "\n", "r": "\r", "t": "\t"}
    while i < len(shown):
        if shown[i] != "\\":
            out.append(shown[i])
            i += 1
        elif shown[i + 1] == "u":
            out.append(chr(int(shown[i + 2:i + 6], 16)))
            i += 6
        else:
            out.append(simple[shown[i + 1]])
            i += 2
    return "".join(out)


async def test_a_backtick_path_cannot_forge_a_row_and_round_trips(sessionmaker, vault):
    forged = "a.md`:7 — id 01b4dc203544 — `x.md"
    put(vault, forged, "- [ ] real task 📅 2026-11-10\n")
    put(vault, "a.md", "- [ ] decoy 📅 2026-11-10\n")
    await index()
    listing = await call("list_tasks", detail="full")
    (row,) = [r for r in rows(listing) if "real task" in r]
    assert _bare_backticks(row) == 2 and row.count("`") == 2, row
    shown = _path_field(row)
    assert _unescape(shown) == forged
    tid = re.search(r"`:1 — id ([0-9a-f]{12})", row).group(1)

    brief = [r for r in rows(await call("list_tasks", detail="brief")) if "real task" in r]
    assert len(brief) == 1 and _bare_backticks(brief[0]) == 2
    agenda = [ln for ln in (await call("task_agenda")).splitlines() if "real task" in ln]
    assert len(agenda) == 1 and _bare_backticks(agenda[0]) == 2
    assert _unescape(_path_field(agenda[0])) == forged

    # The unescaped path is the one the write tools take; the decoy is untouched.
    out = await call("complete_task", path=_unescape(shown), id=tid)
    assert code(out) is None, out
    assert _bare_backticks(out.splitlines()[0]) == 2
    assert disk(vault, forged).decode().startswith("- [x] real task")
    assert disk(vault, "a.md") == "- [ ] decoy 📅 2026-11-10\n".encode()
    # The escaped form names no note.
    out = await call("complete_task", path=shown, id=tid)
    assert code(out) in {"not_found", "validation_failed"}, out


# ══════════════════════════════════════════════════════════════════════════
# The task rule (#434, D7): writes follow the rule, never blocked by it
# ══════════════════════════════════════════════════════════════════════════

FIELDS_NOTE = ('note: not a task under your task rule "fields" (no task field and no task '
               'tag), so default listings will not show it; list_tasks(scope="all") will.')


async def set_rule(sessionmaker, rule, *, tags=(), filt=None, created=None):
    async with sessionmaker() as session:
        await session.execute(text("DELETE FROM task_settings"))
        await session.execute(text(
            "INSERT INTO task_settings (user_id, task_rule, task_tags, global_filter, "
            " set_created_date) VALUES (NULL, :r, :t, :f, :c)"),
            {"r": rule, "t": list(tags), "f": filt, "c": created})
        await session.commit()


async def test_add_task_under_filter_prepends_and_lists(sessionmaker, vault):
    put(vault, "Inbox.md", "# Inbox\n")
    await index()
    await set_rule(sessionmaker, "filter", filt="#task")
    dry = await call("add_task", path="Inbox.md", text="Book flights", due="2026-12-01",
                     dry_run=True)
    assert code(dry) is None, dry
    assert '\nglobal filter added: "#task"\n' in dry
    assert "line 2: - [ ] #task Book flights 📅 2026-12-01" in dry
    assert disk(vault, "Inbox.md") == b"# Inbox\n"
    out = await call("add_task", path="Inbox.md", text="Book flights", due="2026-12-01")
    assert code(out) is None, out
    lines = out.splitlines()
    i = lines.index("line 2: - [ ] #task Book flights 📅 2026-12-01")
    assert lines[i + 1] == 'global filter added: "#task"'
    assert disk(vault, "Inbox.md") == "# Inbox\n- [ ] #task Book flights 📅 2026-12-01\n".encode()
    listing = await call("list_tasks")
    assert "#task Book flights 📅 2026-12-01" in ids_by_text(listing)
    assert listing.splitlines()[2].startswith('Rule — task rule: filter "#task"')

    for args in ({"text": "Hotel", "tags": ["task"]}, {"text": "#task Hotel two"}):
        out = await call("add_task", path="Inbox.md", **args)
        assert code(out) is None and "global filter added" not in out, out
    body = disk(vault, "Inbox.md").decode()
    assert "- [ ] Hotel #task\n" in body and "- [ ] #task Hotel two\n" in body

    out = await call("add_task", path="Inbox.md", text="Car #Task")
    assert "line 5: - [ ] #task Car #Task" in out and 'global filter added: "#task"' in out

    before = disk(vault, "Inbox.md")
    out = await call("add_task", path="Inbox.md", text="x" * 1000)
    assert code(out) == "invalid_argument" and "global filter text" in out, out
    assert disk(vault, "Inbox.md") == before


async def test_a_stored_unsafe_filter_text_is_treated_as_all(sessionmaker, vault):
    put(vault, "Inbox.md", "# Inbox\n")
    await index()
    for bad in ("%%", "<!--"):
        await set_rule(sessionmaker, "filter", filt=bad)
        out = await call("add_task", path="Inbox.md", text=f"Call bank {len(bad)}")
        assert code(out) is None, out
        assert "global filter added" not in out and "note:" not in out
    body = disk(vault, "Inbox.md").decode()
    assert "%%" not in body and "<!--" not in body
    listing = await call("list_tasks")
    assert "the configured rule cannot be applied: filter text not allowed" in listing


async def test_add_task_under_fields_states_a_non_task(sessionmaker, vault):
    put(vault, "Inbox.md", "# Inbox\n")
    await index()
    await set_rule(sessionmaker, "fields")
    for dry_run in (True, False):
        out = await call("add_task", path="Inbox.md", text="Read the report", dry_run=dry_run)
        assert code(out) is None, out
        lines = out.splitlines()
        i = next(k for k, ln in enumerate(lines) if ln.startswith("line 2: - [ ] Read the report"))
        assert lines[i + 1] == FIELDS_NOTE
    assert disk(vault, "Inbox.md") == b"# Inbox\n- [ ] Read the report\n"
    out = await call("add_task", path="Inbox.md", text="Dated", due="2026-12-01")
    assert code(out) is None and "note:" not in out, out
    await set_rule(sessionmaker, "fields", created=True)
    out = await call("add_task", path="Inbox.md", text="Created")
    assert code(out) is None and "note:" not in out and "➕ 2026-11-10" in out, out
    # The non-task is not listed by default, is listed by scope="all", and
    # an id from that listing completes it: the rule never blocks a write.
    assert "Read the report" not in ids_by_text(await call("list_tasks"))
    ids = ids_by_text(await call("list_tasks", scope="all"))
    out = await call("complete_task", path="Inbox.md", id=ids["Read the report"])
    assert code(out) is None and "note:" not in out, out
    assert "- [x] Read the report ✅ 2026-11-10\n" in disk(vault, "Inbox.md").decode()


async def test_update_task_states_a_non_task_and_never_re_adds_the_filter(sessionmaker, vault):
    put(vault, "Inbox.md", "- [ ] Renew passport 📅 2026-12-01\n- [ ] TODO Call the bank\n")
    await index()
    await set_rule(sessionmaker, "fields")
    ids = ids_by_text(await call("list_tasks", scope="all"))
    out = await call("update_task", path="Inbox.md", id=ids["Renew passport 📅 2026-12-01"],
                     clear=["due"])
    assert code(out) is None, out
    lines = out.splitlines()
    assert lines[lines.index("line 1: - [ ] Renew passport") + 1] == FIELDS_NOTE

    await set_rule(sessionmaker, "filter", filt="TODO")
    ids = ids_by_text(await call("list_tasks"))
    for dry_run in (True, False):
        out = await call("update_task", path="Inbox.md", id=ids["TODO Call the bank"],
                         text="Call the bank today", dry_run=dry_run)
        assert code(out) is None, out
        assert ('note: not a task under your task rule "filter" (the line does not contain '
                '"TODO"), so default listings will not show it; list_tasks(scope="all") will.'
                ) in out
    assert disk(vault, "Inbox.md").decode().endswith("- [ ] Call the bank today\n")


async def test_a_checklist_elsewhere_no_longer_blocks_but_the_target_note_still_does(
        sessionmaker, vault):
    put(vault, "Projects/Alpha.md", "- [ ] Renew passport\n")
    put(vault, "Inbox.md", "# Inbox\n")
    await index()
    await set_rule(sessionmaker, "all")
    out = await call("add_task", path="Inbox.md", text="Renew passport", due="2026-12-01",
                     dry_run=True)
    assert code(out) == "duplicate_task", out
    await set_rule(sessionmaker, "fields")
    out = await call("add_task", path="Inbox.md", text="Renew passport", due="2026-12-01")
    assert code(out) is None, out
    assert "Projects/Alpha.md" not in out and "similar open tasks: none" in out

    put(vault, "Inbox2.md", "- [ ] Renew passport\n")
    await index()
    out = await call("add_task", path="Inbox2.md", text="Renew passport", due="2026-12-01")
    assert code(out) == "duplicate_task", out
    out = await call("add_task", path="Inbox2.md", text="Renew passport", due="2026-12-01",
                     allow_duplicate=True)
    assert code(out) is None, out


async def test_under_filter_a_duplicate_in_front_or_as_a_trailing_tag_is_refused(
        sessionmaker, vault):
    """#434 D7: the existing task carries the filter as a trailing tag (in
    another note: the index-side scan) or in front (added by the server
    earlier); both refuse `duplicate_task`, and Inbox.md is unchanged."""
    put(vault, "Projects/Alpha.md", "- [ ] Renew passport #task\n")
    put(vault, "Projects/Beta.md", "- [ ] #task Book flights\n")
    put(vault, "Inbox.md", "# Inbox\n")
    await index()
    await set_rule(sessionmaker, "filter", filt="#task")
    for text_, where in (("Renew passport", "Projects/Alpha.md"),
                         ("Book flights", "Projects/Beta.md")):
        out = await call("add_task", path="Inbox.md", text=text_)
        assert code(out) == "duplicate_task", out
        assert f"`{where}`:1" in out
    assert disk(vault, "Inbox.md") == b"# Inbox\n"
    # In the target note itself: trailing tag, and the bare description.
    put(vault, "Inbox.md", "# Inbox\n- [ ] Call bank #task\n- [ ] Water plants\n")
    await index()
    for text_ in ("Call bank", "Water plants"):
        out = await call("add_task", path="Inbox.md", text=text_, dry_run=True)
        assert code(out) == "duplicate_task", out
    out = await call("add_task", path="Inbox.md", text="Renew passport", allow_duplicate=True)
    assert code(out) is None and "line 4: - [ ] #task Renew passport" in out, out
    # The filter text is not a similar-task term: "#task Book hotel" would be
    # similar to "#task Book flights" (Jaccard 2/4) only through `task`.
    out = await call("add_task", path="Inbox.md", text="Book hotel", dry_run=True)
    assert "similar open tasks: none" in out, out
    # Rule `all`: a filter-tagged line elsewhere is a different description.
    await set_rule(sessionmaker, "all")
    out = await call("add_task", path="Inbox.md", text="Book flights", dry_run=True)
    assert code(out) is None, out
