"""The task tools' wiring, offline (#428, design D2, D3, D15–D17; tasks.md 7.1).

Everything here runs the REAL `_tracked` wrappers and the real planners over a
temporary single-user vault; only the database touch points are stood in for
(the settings read, the write-time task refresh, the change-feed append and
the duplicate scan), each recording what it was handed. The same scenarios
against PostgreSQL, with the real extractor and index, are in
`tests/integration/test_task_tools_pg.py`.

Pinned here: argument validation ahead of any read (D2 step 2), the refusal
mapping (every planner code one to one, `nothing_written`), the D2 bounded
retry with a publisher that conflicts N times (and none with `expected_hash`),
the publish → refresh → event order, usage params free of task text (D17),
and the description and instruction bounds (D15).
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import re
import tempfile

os.environ.setdefault("SECRET_KEY", "test")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("VAULT_PATH", "/tmp/test-vault")
os.chdir(tempfile.gettempdir())

import pytest  # noqa: E402

import src.mcp_server.tools as tools  # noqa: E402
from src.mcp_server import server  # noqa: E402
from src.mcp_server.auth import current_permission  # noqa: E402
from src.services import refusals, task_index, task_line, task_write  # noqa: E402
from src.services import vault as vault_service  # noqa: E402
from src.services import vault_fs  # noqa: E402
from src.services.task_settings import EffectiveTaskSettings  # noqa: E402
from src.services.vault import content_hash_for_bytes  # noqa: E402

NOW = dt.datetime(2026, 11, 10, 12, 0, tzinfo=dt.timezone.utc)
TODAY = "2026-11-10"


def settings_with(**kw) -> EffectiveTaskSettings:
    base = dict(
        timezone="UTC", timezone_fallback=False, configured_timezone=None,
        excluded_folders=(), excluded_note_tags=(), set_done_date=True,
        set_cancelled_date=True, set_created_date=False, next_recurrence_below=False,
    )
    base.update(kw)
    return EffectiveTaskSettings(**base)


def key_of(line: str) -> str:
    parsed = task_line.parse(line)
    assert parsed is not None, line
    return task_line.task_key(parsed.full_text)


def code_of(out: str) -> str | None:
    last = str(out).rstrip().splitlines()[-1]
    if not last.startswith("MCP-REFUSAL "):
        return None
    return json.loads(last[len("MCP-REFUSAL "):])["code"]


def payload_of(out: str) -> dict:
    return json.loads(str(out).rstrip().splitlines()[-1][len("MCP-REFUSAL "):])


class World:
    """What the stood-in database touch points saw, in order."""

    def __init__(self):
        self.events: list = []  # ("refresh"|"event", detail)
        self.usage: list[dict] = []
        self.settings = settings_with()
        self.settings_reads = 0
        self.refresh_result = task_index.RefreshResult(task_index.REFRESHED)
        self.scan = ([], [], False)
        self.scan_calls: list = []


@pytest.fixture
def world(monkeypatch, tmp_path):
    w = World()

    async def log_usage(tool, params, duration_ms, size):
        w.usage.append({"tool": tool, **params})

    async def effective(uid):
        w.settings_reads += 1
        return w.settings

    async def refresh(*, uid, root, target, rel, published):
        w.events.append(("refresh", rel, published))
        return w.refresh_result

    async def record(uid, events):
        w.events.append(("event", [(e.kind, e.path, e.content_hash) for e in events]))

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    async def scan(session, uid, settings, norm, terms, *, exclude_path=None):
        w.scan_calls.append((norm, tuple(terms), exclude_path))
        return w.scan

    monkeypatch.setattr(vault_service.settings, "vault_path", str(tmp_path))
    monkeypatch.setattr(tools, "_log_usage", log_usage)
    monkeypatch.setattr(tools, "_effective_task_settings", effective)
    monkeypatch.setattr(tools, "_task_now", lambda: NOW)
    monkeypatch.setattr(task_index, "refresh_note_tasks", refresh)
    monkeypatch.setattr(tools, "_record_note_events", record)
    monkeypatch.setattr(tools, "async_session", lambda: _Session())
    monkeypatch.setattr(task_write, "duplicate_scan", scan)
    vault_fs.reset_filesystem_probe_cache()
    token = current_permission.set("readwrite")
    w.root = tmp_path
    yield w
    current_permission.reset(token)
    vault_fs.reset_filesystem_probe_cache()


def run(coro):
    return asyncio.run(coro)


def write(world, rel, text):
    p = world.root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(text.encode("utf-8"))
    return p


def read(world, rel) -> str:
    return (world.root / rel).read_bytes().decode("utf-8")


# ── D2 step 2: argument validation ahead of any read ─────────────────────────


@pytest.fixture
def no_read(monkeypatch):
    def boom(*a, **kw):
        raise AssertionError("no note may be opened for an invalid argument")

    monkeypatch.setattr(tools, "open_mutable", boom)


def test_a_suffixed_id_without_a_hash_is_refused_before_any_read(world, no_read):
    out = run(tools.complete_task_impl("Projects/Alpha.md", "0123456789ab.2of2"))
    assert code_of(out) == "precondition_required"
    assert "read_note" in out and "content_hash" in out
    assert payload_of(out)["nothing_written"] is True
    assert world.settings_reads == 0


@pytest.mark.parametrize("bad", ["", "xyz", "0123456789AB", "0123456789ab.3of2", "0123456789ab.0of2"])
def test_a_malformed_id_is_invalid_argument(world, no_read, bad):
    for impl in (tools.complete_task_impl, tools.update_task_impl):
        kw = {} if impl is tools.complete_task_impl else {"due": "today"}
        out = run(impl("Projects/Alpha.md", bad, **kw))
        assert code_of(out) == "invalid_argument", (impl, bad)


def test_update_task_status_done_names_complete_task(world, no_read):
    out = run(tools.update_task_impl("A.md", "0123456789ab", status="done"))
    assert code_of(out) == "invalid_argument" and "complete_task" in out


@pytest.mark.parametrize("kw,needle", [
    (dict(status="paused"), "status"),
    (dict(clear=["colour"]), "clear"),
    (dict(due="today", clear=["due"]), "both set and cleared"),
    (dict(priority="urgent"), "priority"),
    (dict(add_tags=["9lives"]), "inline tag"),
    (dict(add_tags=["t"] * 33), "at most 32"),
    (dict(text="two\nlines"), "text"),
    (dict(append_context="📅 2026-01-01"), "append_context"),
    (dict(), "at least one change"),
])
def test_update_task_arguments(world, no_read, kw, needle):
    out = run(tools.update_task_impl("A.md", "0123456789ab", **kw))
    assert code_of(out) == "invalid_argument", out
    assert needle in out


def test_an_unsupported_rule_is_refused_at_entry(world, no_read):
    out = run(tools.update_task_impl("A.md", "0123456789ab", recurrence="every weekend"))
    assert code_of(out) == "recurrence_unsupported"
    out = run(tools.add_task_impl("A.md", "x", recurrence="every 2 weeks on Monday"))
    assert code_of(out) == "recurrence_unsupported"


def test_a_bad_date_is_invalid_argument(world, no_read):
    out = run(tools.complete_task_impl("A.md", "0123456789ab", done_on="next week"))
    assert code_of(out) == "invalid_argument" and "done_on" in out
    out = run(tools.add_task_impl("A.md", "x", due="+3661d"))
    assert code_of(out) == "invalid_argument" and "due" in out


def test_a_malformed_hash_outranks_not_found(world):
    out = run(tools.complete_task_impl("Missing.md", "0123456789ab", expected_hash="abc"))
    assert code_of(out) == "malformed_precondition"


def test_a_read_only_credential_is_refused_and_nothing_is_written(world):
    write(world, "A.md", "- [ ] a\n")
    token = current_permission.set("read")
    try:
        for out in (
            run(tools.add_task_impl("A.md", "b")),
            run(tools.complete_task_impl("A.md", key_of("- [ ] a"))),
            run(tools.update_task_impl("A.md", key_of("- [ ] a"), due="today")),
        ):
            assert code_of(out) == "permission_denied"
    finally:
        current_permission.reset(token)
    assert read(world, "A.md") == "- [ ] a\n"
    assert world.events == []


# ── the write path, end to end over a real file ─────────────────────────────


def test_complete_task_writes_refreshes_then_records_the_event(world):
    note = "# Inbox\n- [ ] Send invoice 📅 2026-11-10\n- [ ] Other\n"
    write(world, "Projects/Alpha.md", note)
    out = run(tools.complete_task_impl("Projects/Alpha.md", key_of("- [ ] Send invoice 📅 2026-11-10")))
    assert code_of(out) is None, out
    expected = "# Inbox\n- [x] Send invoice 📅 2026-11-10 ✅ 2026-11-10\n- [ ] Other\n"
    assert read(world, "Projects/Alpha.md") == expected
    new_id = key_of("- [x] Send invoice 📅 2026-11-10 ✅ 2026-11-10")
    assert out.splitlines()[0] == "Completed task in `Projects/Alpha.md`"
    assert "line 2: - [x] Send invoice 📅 2026-11-10 ✅ 2026-11-10" in out
    assert f"id: {new_id}" in out
    assert f"content_hash: {content_hash_for_bytes(expected.encode())}" in out
    assert out.rstrip().endswith("index: refreshed")
    assert "timezone: UTC" in out and f"done_on: today={TODAY}" in out
    # D10: publish → refresh → change-feed event, each exactly once.
    assert [e[0] for e in world.events] == ["refresh", "event"]
    assert world.events[0][2] == expected.encode()
    assert world.events[1][1][0][0] == "modified"
    (row,) = world.usage
    assert row["task_refresh"] == "refreshed" and row["expected_hash"] is False


def test_a_lagging_refresh_keeps_the_write_and_says_so(world):
    write(world, "A.md", "- [ ] a\n")
    world.refresh_result = task_index.RefreshResult(task_index.LAGGED, "lock_timeout")
    out = run(tools.complete_task_impl("A.md", key_of("- [ ] a")))
    assert code_of(out) is None
    assert out.rstrip().endswith("index: may lag (lock_timeout)")
    assert read(world, "A.md").startswith("- [x] a")
    assert world.usage[0]["task_refresh"] == "lagged:lock_timeout"
    assert [e[0] for e in world.events] == ["refresh", "event"]


def test_a_recurring_completion_dry_run_writes_nothing(world):
    note = "- [ ] Rent 🔁 every month 📅 2026-11-01\n"
    write(world, "A.md", note)
    out = run(tools.complete_task_impl("A.md", key_of(note.rstrip("\n")), dry_run=True))
    assert code_of(out) is None
    assert out.startswith("Dry run — nothing was written.")
    # No line of a dry run asserts a write.
    assert out.splitlines()[1] == "Would complete task in `A.md`"
    assert "Completed task" not in out
    assert "next occurrence, line 1 (above): - [ ] Rent 🔁 every month 📅 2026-12-01" in out
    assert "+- [ ] Rent 🔁 every month 📅 2026-12-01" in out  # the diff
    assert read(world, "A.md") == note
    assert world.events == []
    assert world.usage[0]["task_refresh"] == "skipped"


def test_a_custom_marker_is_named(world):
    write(world, "A.md", "- [>] Forwarded item\n")
    out = run(tools.complete_task_impl("A.md", key_of("- [>] Forwarded item")))
    assert "replaced marker: `>`" in out
    assert read(world, "A.md") == f"- [x] Forwarded item ✅ {TODAY}\n"


@pytest.mark.parametrize("note,line,code", [
    ("- [x] done\n", "- [x] done", "already_completed"),
    ("- [-] gone\n", "- [-] gone", "task_cancelled"),
    ("- [ ] x 🏁 delete 🔁 every day 📅 2026-11-01\n", "- [ ] x 🏁 delete 🔁 every day 📅 2026-11-01",
     "on_completion_unsupported"),
    ("- [ ] Review 🔁 every month on the last Friday 📅 2026-11-27\n",
     "- [ ] Review 🔁 every month on the last Friday 📅 2026-11-27", "recurrence_unsupported"),
    ("- [ ] w\n- [ ] w\n", "- [ ] w", "task_ambiguous"),
])
def test_planner_refusals_map_one_to_one_and_write_nothing(world, note, line, code):
    write(world, "A.md", note)
    out = run(tools.complete_task_impl("A.md", key_of(line)))
    assert code_of(out) == code, out
    assert payload_of(out)["nothing_written"] is True
    assert payload_of(out)["path"] == "A.md"
    assert out.prose.endswith("Nothing was written.") or code == "task_ambiguous"
    assert read(world, "A.md") == note
    assert world.events == []


def test_task_ambiguous_lists_the_suffixed_ids(world):
    write(world, "A.md", "- [ ] Water plants\n- [ ] Water plants\n")
    k = key_of("- [ ] Water plants")
    out = run(tools.complete_task_impl("A.md", k))
    assert code_of(out) == "task_ambiguous"
    assert f"line 1 id {k}.1of2" in out and f"line 2 id {k}.2of2" in out


def test_a_suffixed_id_with_the_current_hash_resolves(world):
    note = "- [ ] Call\n    - first\n- [ ] Call\n    - second\n"
    write(world, "A.md", note)
    k = key_of("- [ ] Call")
    h = content_hash_for_bytes(note.encode())
    out = run(tools.complete_task_impl("A.md", f"{k}.2of2", expected_hash=h))
    assert code_of(out) is None, out
    assert read(world, "A.md") == f"- [ ] Call\n    - first\n- [x] Call ✅ {TODAY}\n    - second\n"
    assert world.usage[0]["expected_hash"] is True


def test_task_not_found_after_the_line_changed(world):
    write(world, "A.md", "- [ ] Pay rent today\n")
    out = run(tools.update_task_impl("A.md", key_of("- [ ] Pay rent"), due="tomorrow"))
    assert code_of(out) == "task_not_found"


def test_update_task_sets_a_relative_due_date_and_echoes_it(world):
    write(world, "A.md", "- [ ] Pay 🔁 every month 📅 2026-11-05\n")
    out = run(tools.update_task_impl("A.md", key_of("- [ ] Pay 🔁 every month 📅 2026-11-05"),
                                     scheduled="+3d"))
    assert code_of(out) is None, out
    assert read(world, "A.md") == "- [ ] Pay 🔁 every month ⏳ 2026-11-13 📅 2026-11-05\n"
    assert "fields changed: scheduled" in out.splitlines()[0]
    assert "scheduled: +3d=2026-11-13" in out
    (row,) = world.usage
    assert row["fields"] == ["scheduled"]


def test_update_task_no_change_writes_nothing(world):
    write(world, "A.md", "- [ ] Chase #waiting-for/bob\n")
    out = run(tools.update_task_impl("A.md", key_of("- [ ] Chase #waiting-for/bob"),
                                     remove_tags=["waiting-for"]))
    assert code_of(out) is None
    assert out.startswith("No changes for `A.md`")
    assert world.events == []
    assert world.usage[0]["task_refresh"] == "skipped"


def test_add_task_refuses_a_missing_note_and_creates_nothing(world):
    out = run(tools.add_task_impl("Nowhere.md", "x"))
    assert code_of(out) == "not_found"
    assert not (world.root / "Nowhere.md").exists()


def test_add_task_inserts_after_the_task_list_and_lists_similar_tasks(world):
    note = "# Today\nIntro\n- [ ] a\n- [ ] b\n    - ctx\n\nMore prose\n# Later\n"
    write(world, "A.md", note)
    world.scan = ([], [task_write.TaskRef(line=4, id="aaaaaaaaaaaa", text="Renew passport for the trip",
                                          path="Other.md", score=0.4)], False)
    out = run(tools.add_task_impl("A.md", "Renew passport", section="Today", due="2026-11-12",
                                  context="ref 1234"))
    assert code_of(out) is None, out
    assert read(world, "A.md") == (
        "# Today\nIntro\n- [ ] a\n- [ ] b\n    - ctx\n- [ ] Renew passport 📅 2026-11-12\n"
        "    - ref 1234\n\nMore prose\n# Later\n"
    )
    assert "similar open tasks:" in out
    assert "- `Other.md`:4 id aaaaaaaaaaaa — Renew passport for the trip" in out
    assert f"id: {key_of('- [ ] Renew passport 📅 2026-11-12')}" in out
    assert world.scan_calls[0][2] == "A.md"  # the target note is excluded
    (row,) = world.usage
    assert row["similar_count"] == 1
    # D17: no task text or context in the usage params.
    flat = json.dumps(row, ensure_ascii=False)
    assert "Renew passport" not in flat and "ref 1234" not in flat


def test_add_task_index_duplicate_refused_then_allowed(world):
    write(world, "Inbox.md", "")
    dup = task_write.TaskRef(line=3, id="bbbbbbbbbbbb", text="Renew passport #admin 📅 2026-12-01",
                             path="Projects/Alpha.md")
    world.scan = ([dup], [], False)
    out = run(tools.add_task_impl("Inbox.md", "renew  Passport", tags=["admin"]))
    assert code_of(out) == "duplicate_task"
    assert "`Projects/Alpha.md`:3 id bbbbbbbbbbbb" in out
    assert read(world, "Inbox.md") == ""
    out = run(tools.add_task_impl("Inbox.md", "renew  Passport", tags=["admin"], allow_duplicate=True))
    assert code_of(out) is None, out
    assert read(world, "Inbox.md") == "- [ ] renew  Passport #admin"


def test_add_task_section_defects_reuse_the_edit_note_messages(world):
    write(world, "A.md", "---\nkey: [unclosed\n---\n# S\n")
    out = run(tools.add_task_impl("A.md", "x", section="S"))
    assert code_of(out) == "content_unsafe"
    assert out.startswith("add_task: A.md has a malformed frontmatter block")
    write(world, "B.md", "# S\n  ```\ncode\n")
    out = run(tools.add_task_impl("B.md", "x", section="S"))
    assert code_of(out) == "content_unsafe"
    assert out.startswith("add_task: B.md contains an indented fence opener")


def test_add_task_dry_run_lists_similar_and_writes_nothing(world):
    write(world, "A.md", "- [ ] a\n")
    out = run(tools.add_task_impl("A.md", "b", dry_run=True))
    assert out.startswith("Dry run — nothing was written.")
    # No line of a dry run asserts a write: it says what would be added.
    assert out.splitlines()[1] == "Would add task to `A.md`"
    assert "Added task" not in out
    assert "similar open tasks: none" in out and "+- [ ] b" in out
    assert read(world, "A.md") == "- [ ] a\n"
    assert world.events == []


def test_required_mode_is_satisfied_by_the_id(world, monkeypatch):
    """D3: a bare id needs no hash under WRITE_PRECONDITION_REQUIRED; a
    supplied hash that does not match is still `stale_precondition`;
    `add_task` is exempt."""
    monkeypatch.setattr(tools.settings, "write_precondition_required", True)
    write(world, "A.md", "- [ ] a\n- [ ] b\n")
    out = run(tools.complete_task_impl("A.md", key_of("- [ ] a")))
    assert code_of(out) is None, out
    out = run(tools.complete_task_impl("A.md", key_of("- [ ] b"), expected_hash="sha256:" + "0" * 64))
    assert code_of(out) == "stale_precondition"
    out = run(tools.add_task_impl("A.md", "c"))
    assert code_of(out) is None, out
    # A real write keeps its heading, byte for byte.
    assert out.splitlines()[0] == "Added task to `A.md`"


# ── D2: the bounded retry ────────────────────────────────────────────────────


def conflicting_publisher(monkeypatch, world, conflicts: int, rel="A.md"):
    """`write_file_at` that, `conflicts` times, lets another writer change an
    unrelated line first — a real in-call conflict, detected by the real
    `expected=` comparison — and then publishes normally."""
    original = tools.write_file_at
    calls = []

    def publish(target, content, **kw):
        calls.append(content)
        if len(calls) <= conflicts:
            path = world.root / rel
            path.write_bytes(path.read_bytes() + f"other writer {len(calls)}\n".encode())
        return original(target, content, **kw)

    monkeypatch.setattr(tools, "write_file_at", publish)
    return calls


@pytest.mark.parametrize("conflicts", [1, 2])
def test_a_conflict_without_a_hash_is_retried_from_a_fresh_read(world, monkeypatch, conflicts):
    write(world, "A.md", "- [ ] a\n")
    calls = conflicting_publisher(monkeypatch, world, conflicts)
    out = run(tools.complete_task_impl("A.md", key_of("- [ ] a")))
    assert code_of(out) is None, out
    assert len(calls) == conflicts + 1
    # Each attempt re-read: the published text holds every other writer's line.
    tail = "".join(f"other writer {i}\n" for i in range(1, conflicts + 1))
    assert read(world, "A.md") == f"- [x] a ✅ {TODAY}\n" + tail
    assert [e[0] for e in world.events] == ["refresh", "event"], "one refresh, one event"
    assert len(world.usage) == 1 and world.usage[0]["task_write_attempts"] == conflicts + 1


def test_three_conflicts_give_concurrent_write_and_write_nothing_of_ours(world, monkeypatch):
    write(world, "A.md", "- [ ] a\n")
    calls = conflicting_publisher(monkeypatch, world, 3)
    out = run(tools.complete_task_impl("A.md", key_of("- [ ] a")))
    assert code_of(out) == "concurrent_write"
    assert len(calls) == tools.TASK_WRITE_MAX_ATTEMPTS == 3
    assert read(world, "A.md") == "- [ ] a\nother writer 1\nother writer 2\nother writer 3\n"
    assert world.events == [] and len(world.usage) == 1


def test_no_retry_with_an_expected_hash(world, monkeypatch):
    note = "- [ ] a\n"
    write(world, "A.md", note)
    calls = conflicting_publisher(monkeypatch, world, 1)
    out = run(tools.complete_task_impl("A.md", key_of("- [ ] a"),
                                       expected_hash=content_hash_for_bytes(note.encode())))
    assert code_of(out) == "concurrent_write"
    assert len(calls) == 1
    assert read(world, "A.md") == note + "other writer 1\n"


def test_a_retry_re_resolves_the_id_against_the_new_bytes(world, monkeypatch):
    """The other writer completes the same task: the retry must not complete
    it twice, it must answer from the new bytes (D2: same task in parallel)."""
    write(world, "A.md", "- [ ] a\n")
    original = tools.write_file_at
    calls = []

    def publish(target, content, **kw):
        calls.append(1)
        if len(calls) == 1:
            (world.root / "A.md").write_bytes(f"- [x] a ✅ {TODAY}\n".encode())
        return original(target, content, **kw)

    monkeypatch.setattr(tools, "write_file_at", publish)
    out = run(tools.complete_task_impl("A.md", key_of("- [ ] a")))
    assert code_of(out) in {"task_not_found", "already_completed"}
    assert read(world, "A.md") == f"- [x] a ✅ {TODAY}\n"


# ── the read tools ───────────────────────────────────────────────────────────


def test_task_agenda_refuses_a_small_response_cap_before_any_statement(world, monkeypatch):
    monkeypatch.setattr(tools.settings, "max_read_response_chars", 2799)
    out = run(tools.task_agenda_impl())
    assert code_of(out) == "response_cap_too_small"
    assert world.settings_reads == 0


def test_task_agenda_renders_through_the_query(world, monkeypatch):
    from src.services import task_query

    seen = {}
    real = task_query.task_agenda_query

    async def agenda(uid, args, settings, *, now=None, session_factory=None):
        seen.update(uid=uid, args=args, now=now)
        return await _empty_agenda(real, args, settings, now)

    monkeypatch.setattr(task_query, "task_agenda_query", agenda)
    out = run(tools.task_agenda_impl(date="tomorrow", days=3))
    assert code_of(out) is None, out
    assert out.startswith("Agenda")
    assert seen["args"].date == "tomorrow" and seen["args"].days == 3 and seen["now"] == NOW
    assert world.usage[0]["date"] == "tomorrow" and world.usage[0]["days"] == 3


async def _empty_agenda(real, args, settings, now):
    """The real `task_agenda_query` over a session that returns nothing."""
    class R:
        def scalar_one(self):
            return 0

        def one(self):
            return (0, 0, 0, 0, 0)

        def all(self):
            return []

    class S:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def execute(self, *a, **k):
            return R()

        async def rollback(self):
            return None

    class NoteCounts(R):
        def one(self):
            return (0, 0)

    class S2(S):
        n = 0

        async def execute(self, stmt, *a, **k):
            self.n += 1
            # the second statement is `_note_counts`'s failed/capped pair
            return NoteCounts() if self.n == 3 else R()

    return await real(None, args, settings, now=now, session_factory=S2)


# ── D15: the published surface ───────────────────────────────────────────────


def test_descriptions_and_instructions_are_within_their_bounds():
    listed = {t.name: t for t in server.mcp._tool_manager.list_tools()}
    for name in ("list_tasks", "task_agenda", "complete_task", "update_task", "add_task"):
        assert len(listed[name].description) <= 4000, name
    assert len(server.SERVER_INSTRUCTIONS) <= 2000
    lead = server.SERVER_INSTRUCTIONS[:512]
    assert "task" not in lead
    assert re.search(r"`list_tasks` and\s+`task_agenda`", server.SERVER_INSTRUCTIONS)


def test_registration_order_titles_and_annotations():
    names = [t.name for t in server.mcp._tool_manager.list_tools()]
    assert names[-7:] == ["list_tasks", "get_changes", "hybrid_search", "task_agenda",
                          "complete_task", "update_task", "add_task"]
    listed = {t.name: t for t in server.mcp._tool_manager.list_tools()}
    assert listed["task_agenda"].title == "Task agenda"
    assert listed["task_agenda"].annotations == server._READ_ONLY
    assert listed["complete_task"].annotations == server._DESTRUCTIVE
    assert listed["update_task"].annotations == server._DESTRUCTIVE
    assert listed["add_task"].annotations == server._ADDITIVE


def test_every_new_refusal_code_is_produced_by_the_wiring():
    """Each `refusals.TASK_CODES` code reaches a caller through these tools:
    the planner codes via `_task_write_refusal`, `response_cap_too_small` via
    the read tools. Pinned so a code nobody can produce is noticed."""
    for code in refusals.TASK_CODES - {"response_cap_too_small"}:
        out = tools._task_write_refusal(
            "complete_task", "A.md", task_write.TaskWriteRefusal(code, "because"))
        assert code_of(out) == code
        assert payload_of(out)["nothing_written"] is True


def test_the_primer_names_the_task_tools_neutrally(world):
    """`vault-guide` delta (#428): the Tasks section names the five tools,
    says the writes take a path plus a task id, that plain checkboxes work
    and the plugin fields are optional, and that query blocks are not
    evaluated — ranking no tool above another."""
    result = run(tools.get_vault_guide_impl())
    tasks = result.split("## Tasks", 1)[1].split("\n## ", 1)[0]
    flat = " ".join(tasks.split())
    for name in ("list_tasks", "task_agenda", "complete_task", "update_task", "add_task"):
        assert f"`{name}`" in tasks, name
    assert "note path plus the task id a listing prints" in flat
    assert "Plain checkboxes work without the Tasks plugin" in flat and "optional" in flat
    assert "not evaluate" in flat
    for word in ("MUST", "IMPORTANT", "always use", "primary"):
        assert word not in tasks


# ── a backtick in a note path can never forge a row (#428 Codex round 2) ────

#: File names carrying every delimiter a row uses.
FORGING_PATHS = [
    "a.md`:7 — id 01b4dc203544 — `x.md",
    "Inbox/`evil`.md",
    "id 0123456789ab — :7.md",
    "back\\`tick.md",
    "tab\tand\nnewline`.md",
]


def unescape_path(shown: str) -> str:
    """The documented rule: a doubled backslash, \\n \\r \\t, \\uXXXX (no other)."""
    out, i = [], 0
    simple = {"\\": "\\", "n": "\n", "r": "\r", "t": "\t"}
    while i < len(shown):
        ch = shown[i]
        if ch != "\\":
            out.append(ch)
            i += 1
            continue
        nxt = shown[i + 1]
        if nxt == "u":
            out.append(chr(int(shown[i + 2:i + 6], 16)))
            i += 6
        else:
            out.append(simple[nxt])
            i += 2
    return "".join(out)


def bare_backticks(line: str) -> int:
    """Backticks not preceded by an escaping backslash."""
    count, i = 0, 0
    while i < len(line):
        if line[i] == "\\":
            i += 2
            continue
        count += line[i] == "`"
        i += 1
    return count


def first_path_field(line: str) -> str:
    """The text between the first two bare backticks."""
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


def _row(path):
    return {
        "status_char": " ", "file_path": path, "line": 1, "due": dt.date(2026, 11, 10),
        "scheduled": None, "start": None, "done_on": None, "priority": None,
        "tags": None, "tags_cut": False, "heading_path": None, "text": "t",
        "text_cut": False, "state": "open", "task_key": "0123456789ab",
        "key_ordinal": 1, "key_count": 1, "context": None, "context_cut": False,
    }


@pytest.mark.parametrize("path", FORGING_PATHS)
def test_every_row_shape_shows_one_path_field_that_unescapes_to_the_path(path):
    from src.mcp_server import task_render

    for line in (task_render.render_full_row(_row(path)),
                 task_render.render_brief_row(_row(path)),
                 tools.render_task_row(_row(path))):
        assert "\n" not in line
        assert bare_backticks(line) == 2, line
        assert unescape_path(first_path_field(line)) == path, line
    abbreviated = task_render.render_abbreviated_row(_row(path), 400)
    assert bare_backticks(abbreviated) == 2, abbreviated


def test_a_cut_path_never_splits_an_escape():
    path = "`" * 200
    for room in range(3, 60):
        shown = tools._cut_escaped_path(path, room)
        assert bare_backticks(shown) == 0, (room, shown)
        assert "\\…" not in shown
    echo = tools._echo_path(path, 32)
    assert len(echo) <= 32 and bare_backticks(echo) == 0


def test_the_header_echo_escapes_the_folder():
    header = tools.render_task_header(
        status="open", folder="F`older/", due_by=None, scheduled_by=None, tags=None,
        shown=0, more=False, not_scanned=0, failed=0, capped=0, cut_tags=None)
    assert "folder: F\\u0060older/" in header and "`" not in header


@pytest.mark.parametrize("path", FORGING_PATHS[:3])
def test_write_responses_refusals_and_similar_lists_escape_the_path(world, path):
    write(world, path, "- [ ] Water plants\n- [ ] Water plants\n")
    k = key_of("- [ ] Water plants")
    # The task_ambiguous refusal prose and its listing.
    out = run(tools.complete_task_impl(path, k))
    assert code_of(out) == "task_ambiguous"
    for line in out.prose.splitlines():
        assert bare_backticks(line) in (0, 2), line
    assert unescape_path(first_path_field(out.prose)) == path
    # A write response.
    body = read(world, path).encode()
    out = run(tools.complete_task_impl(path, f"{k}.1of2", expected_hash=content_hash_for_bytes(body)))
    assert code_of(out) is None, out
    head = out.splitlines()[0]
    assert bare_backticks(head) == 2 and unescape_path(first_path_field(head)) == path
    # add_task's similar list and the dry-run diff header (the diff BODY is
    # note content, prefixed, and exempt).
    ref = task_write.TaskRef(line=1, id="aaaaaaaaaaaa", text="Water plants daily", path=path)
    world.scan = ([], [ref], False)
    out = run(tools.add_task_impl(path, "Water the plants", dry_run=True))
    seen_similar = False
    for line in out.splitlines():
        if line.startswith("- `"):
            seen_similar = True
            assert bare_backticks(line) == 2 and unescape_path(first_path_field(line)) == path
        if line.startswith(("--- ", "+++ ")):
            assert line[4:] == tools._escape_task_path(path)
    assert seen_similar
    # The duplicate refusal's listing.
    world.scan = ([ref], [], False)
    out = run(tools.add_task_impl(path, "Water the plants"))
    assert code_of(out) == "duplicate_task"
    for line in out.prose.splitlines():
        assert bare_backticks(line) in (0, 2), line
        if bare_backticks(line):
            assert unescape_path(first_path_field(line)) == path


def test_a_backslash_before_a_backtick_stays_distinguishable():
    assert tools._escape_task_path("a\\`b") == "a\\\\\\u0060b"
    assert tools._escape_task_path("a`b") == "a\\u0060b"
    assert tools._escape_task_path("a\\u0060b") == "a\\\\u0060b"
    assert unescape_path(tools._escape_task_path("a\\`b")) == "a\\`b"
    assert unescape_path(tools._escape_task_path("a\\u0060b")) == "a\\u0060b"


#: Characters a hostile path is built from: every row delimiter and escape.
_HOSTILE = ["`", "\\", " — ", "id ", ":7", "\n", "\r", "\t", "\u2028", "\u0085",
            "u0060", "a", "é", "/", ".md", " ", "0123456789ab", "`:7 — id 0123456789ab — `"]


def test_no_backtick_ever_reaches_the_output_and_the_rule_round_trips():
    """Property: for random and hostile paths, the escaped rendering holds NO
    backtick character, unescapes to the original by the documented rule, and
    no echo or cut splits an escape (#428 Codex round 2)."""
    import random

    from src.mcp_server import task_render

    rng = random.Random(428)
    paths = ["a.md`:7", "a.md`:7 — id 01b4dc203544 — `x.md", "\\`", "``", "\\u0060"]
    paths += ["".join(rng.choice(_HOSTILE) for _ in range(rng.randint(1, 12)))
              for _ in range(400)]
    for path in paths:
        shown = tools._escape_task_path(path)
        assert "`" not in shown, (path, shown)
        assert unescape_path(shown) == path, (path, shown)
        for line in (task_render.render_full_row(_row(path)),
                     task_render.render_brief_row(_row(path))):
            assert line.count("`") == 2, line
            assert unescape_path(first_path_field(line)) == path
        for room in (3, 7, 12, 33, 64):
            cut = tools._cut_escaped_path(path, room)
            assert "`" not in cut and len(cut) <= max(room, 3)
            # Every escape in the cut is whole: it unescapes without error.
            for piece in cut.split("…"):
                unescape_path(piece)
        echo = tools._echo_path(path, 32)
        assert "`" not in echo and len(echo) <= 32


# ── one clock, the caller's zone (#428 verifier) ─────────────────────────────


def test_list_tasks_validates_dates_in_the_callers_zone(monkeypatch):
    """Server default UTC, caller `Pacific/Kiritimati` (UTC+14), clock
    2026-11-10 12:00 UTC: in the caller's zone today is 2026-11-11, so
    `due_from="2026-11-11", due_by="today"` is a valid, one-day window."""
    from tests.test_list_tasks_render import _FakeSession

    reads = []

    async def kiritimati(uid):
        reads.append(uid)
        return settings_with(timezone="Pacific/Kiritimati")

    monkeypatch.setattr(tools, "_effective_task_settings", kiritimati)
    monkeypatch.setattr(tools, "async_session", lambda: _FakeSession())
    monkeypatch.setattr(tools, "_task_now", lambda: NOW)
    out = run(tools.list_tasks_impl.__wrapped__(due_from="2026-11-11", due_by="today"))
    assert code_of(out) is None, out
    assert "today: 2026-11-11" in out and "timezone: Pacific/Kiritimati" in out
    assert reads == [None], "one settings read per call"


@pytest.mark.parametrize("call", [
    lambda: tools.list_tasks_impl.__wrapped__(due_by="today", due_from="-3d"),
    lambda: tools.complete_task_impl("A.md", key_of("- [ ] a")),
    lambda: tools.update_task_impl("A.md", key_of("- [ ] a"), due="tomorrow", start="today"),
    lambda: tools.add_task_impl("A.md", "b", due="+2d", scheduled="today", start="yesterday"),
], ids=["list_tasks", "complete_task", "update_task", "add_task"])
def test_each_call_reads_the_clock_once(world, monkeypatch, call):
    from tests.test_list_tasks_render import _FakeSession

    write(world, "A.md", "- [ ] a\n")
    reads = []

    def clock():
        reads.append(1)
        return NOW

    monkeypatch.setattr(tools, "_task_now", clock)
    monkeypatch.setattr(tools, "async_session", lambda: _FakeSession())
    out = run(call())
    assert code_of(out) is None, out
    assert len(reads) == 1, reads


def test_task_agenda_reads_the_clock_once(world, monkeypatch):
    from src.services import task_query

    seen = []

    async def agenda(uid, args, settings, *, now=None, session_factory=None):
        seen.append(now)
        return task_query.QueryRefusal("invalid_argument", "stop here")

    reads = []
    monkeypatch.setattr(tools, "_task_now", lambda: reads.append(1) or NOW)
    monkeypatch.setattr(task_query, "task_agenda_query", agenda)
    run(tools.task_agenda_impl())
    assert len(reads) == 1 and seen == [NOW]


# ── the refusal codes an agent must act on are listed (#428 verifier) ────────


@pytest.mark.parametrize("name", ["complete_task", "update_task"])
def test_the_write_descriptions_list_content_unsafe_and_malformed_precondition(name):
    listed = {t.name: t for t in server.mcp._tool_manager.list_tools()}
    text = listed[name].description
    assert "`content_unsafe`" in text and "`malformed_precondition`" in text
    assert len(text) <= 4000


def test_the_list_tasks_description_states_the_path_escapes():
    listed = {t.name: t for t in server.mcp._tool_manager.list_tools()}
    text = " ".join(listed["list_tasks"].description.split())
    assert "a backtick is `\\u0060`" in text
    assert "the write tools take the unescaped path" in text


# ── one owner predicate (#428 verifier) ──────────────────────────────────────


def test_tenancy_has_one_owner_predicate(monkeypatch):
    import inspect

    sentinel = object()
    monkeypatch.setattr(tools, "_note_owner_predicate", lambda uid: (sentinel, uid))
    assert task_index._owner(7) == (sentinel, 7)
    for module in (task_index, task_write):
        assert "user_id.is_(None)" not in inspect.getsource(module), module.__name__
    assert "_note_owner_predicate(uid)" in inspect.getsource(task_write.duplicate_scan)
