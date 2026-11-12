"""The generic writers' best-effort task refresh, offline (#432, task-refresh-on-write).

Runs the REAL `_tracked` wrappers and the real gate over a temporary
single-user vault. Only the database touch points are stood in for: the
write-time refresh (`task_index.refresh_note_tasks`) and the change-feed
append, each recording what it saw, in order. The PostgreSQL side (real rows,
real locks, the pass) is `tests/integration/test_task_refresh_pg.py`.

Pinned here:
- which writers refresh and which never do (design D1);
- the gate, including unknown incumbents and the equal-bytes rule (D2);
- the absence form after every delete (D3);
- the order publication → event → refresh (D4);
- byte-identical responses whatever the refresh reports (D5);
- the usage key, present only when attempted (D5);
- a cancelled refresh (D4).
"""
from __future__ import annotations

import asyncio
import os
import tempfile

os.environ.setdefault("SECRET_KEY", "test")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("VAULT_PATH", "/tmp/test-vault")
os.chdir(tempfile.gettempdir())

import pytest  # noqa: E402

import src.mcp_server.tools as tools  # noqa: E402
from src.mcp_server.auth import current_permission  # noqa: E402
from src.services import task_index  # noqa: E402
from src.services import vault as vault_service  # noqa: E402
from src.services import vault_fs  # noqa: E402
from src.services.vault import content_hash_for_bytes  # noqa: E402

TASKS = "# List\n\n- [ ] Buy milk\n- [ ] Call bank\n"
PLAIN = "# Notes\n\nNothing to do here.\n"


class World:
    def __init__(self):
        self.seen: list = []  # ("event", kinds) | ("refresh", rel, published)
        self.usage: list[dict] = []
        self.result = task_index.RefreshResult(task_index.REFRESHED)
        self.raise_in_refresh: BaseException | None = None

    @property
    def refreshes(self):
        return [s for s in self.seen if s[0] == "refresh"]

    @property
    def events(self):
        return [s for s in self.seen if s[0] == "event"]


@pytest.fixture
def world(monkeypatch, tmp_path):
    w = World()

    async def log_usage(tool, params, duration_ms, size):
        w.usage.append({"tool": tool, **params})

    async def refresh(*, uid, root, target, rel, published):
        w.seen.append(("refresh", rel, published))
        if w.raise_in_refresh is not None:
            raise w.raise_in_refresh
        return w.result

    async def record(uid, events):
        w.seen.append(("event", [(e.kind, e.path) for e in events]))

    monkeypatch.setattr(vault_service.settings, "vault_path", str(tmp_path))
    monkeypatch.setattr(tools, "_log_usage", log_usage)
    monkeypatch.setattr(task_index, "refresh_note_tasks", refresh)
    monkeypatch.setattr(tools, "_record_note_events", record)
    vault_fs.reset_filesystem_probe_cache()
    token = current_permission.set("readwrite")
    w.root = tmp_path
    yield w
    current_permission.reset(token)
    vault_fs.reset_filesystem_probe_cache()


def run(coro):
    return asyncio.run(coro)


def put(world, rel, text):
    p = world.root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(text.encode("utf-8"))
    return p


def usage_of(world) -> dict:
    assert len(world.usage) == 1, world.usage
    return world.usage[0]


# ── D1: which writers refresh ───────────────────────────────────────────────


def test_edit_note_refreshes_after_its_event(world):
    put(world, "a.md", TASKS)
    out = run(tools.edit_note_impl("a.md", "- [x] Buy milk", find="- [ ] Buy milk"))
    assert "MCP-REFUSAL" not in out
    kinds = [s[0] for s in world.seen]
    assert kinds == ["event", "refresh"], world.seen  # D4: event first
    assert world.refreshes[0][1] == "a.md"
    assert world.refreshes[0][2] == (world.root / "a.md").read_bytes()
    assert usage_of(world)["task_refresh"] == "refreshed"


def test_set_frontmatter_refreshes(world):
    put(world, "a.md", "---\na: 1\n---\n" + TASKS)
    out = run(tools.set_frontmatter_impl("a.md", {"b": 2, "c": 3}))
    assert "MCP-REFUSAL" not in out
    assert [s[0] for s in world.seen] == ["event", "refresh"]
    assert usage_of(world)["task_refresh"] == "refreshed"


def test_write_file_overwrite_refreshes(world):
    put(world, "a.md", TASKS)
    out = run(tools.write_file_impl("a.md", PLAIN, encoding="text", overwrite=True))
    assert "MCP-REFUSAL" not in out
    assert [s[0] for s in world.seen] == ["event", "refresh"]


def test_write_file_creating_a_note_never_refreshes(world):
    out = run(tools.write_file_impl("new.md", TASKS, encoding="text"))
    assert "MCP-REFUSAL" not in out
    assert world.refreshes == []
    assert "task_refresh" not in usage_of(world)


def test_write_file_non_note_never_refreshes(world):
    put(world, "a.txt", TASKS)
    run(tools.write_file_impl("a.txt", TASKS, encoding="text", overwrite=True))
    assert world.refreshes == []


def test_create_note_never_refreshes(world):
    out = run(tools.create_note_impl("new.md", TASKS))
    assert "MCP-REFUSAL" not in out
    assert world.refreshes == []
    assert "task_refresh" not in usage_of(world)


def test_plain_move_never_refreshes(world):
    put(world, "Old.md", TASKS)
    run(tools.move_note_impl("Old.md", "New.md"))
    assert world.refreshes == []


@pytest.mark.parametrize("fn", [
    "_move_note_locked", "move_note_impl", "create_note_impl", "import_from_url_impl",
    "delete_file_impl",
])
def test_excluded_writers_hold_no_refresh_call(fn):
    # A link-rewrite move needs the index to run at all, so its exclusion
    # (design D1, L4) is pinned on the source: no excluded writer calls either
    # refresh entry point.
    import inspect

    source = inspect.getsource(getattr(tools, fn))
    assert "_refresh_after_write" not in source and "refresh_note_tasks" not in source


@pytest.mark.parametrize("permanent", [False, True])
def test_delete_note_always_clears_after_its_event(world, permanent):
    put(world, "a.md", PLAIN)  # no checkbox: the clear is never gated
    out = run(tools.delete_note_impl("a.md", permanent=permanent))
    assert "MCP-REFUSAL" not in out
    assert [s[0] for s in world.seen] == ["event", "refresh"]
    assert world.refreshes[0][2] is task_index.ABSENT
    assert usage_of(world)["task_refresh"] == "refreshed"


def test_guarded_delete_still_clears(world):
    data = put(world, "a.md", PLAIN).read_bytes()
    run(tools.delete_note_impl("a.md", expected_hash=content_hash_for_bytes(data)))
    assert world.refreshes and world.refreshes[0][2] is task_index.ABSENT


# ── D2: the gate ────────────────────────────────────────────────────────────


def test_no_checkbox_on_either_side_means_no_refresh_and_no_usage_key(world):
    put(world, "a.md", PLAIN)
    run(tools.edit_note_impl("a.md", "# Notes\n\nStill nothing.\n"))
    assert world.refreshes == []
    assert [s[0] for s in world.seen] == ["event"]
    assert "task_refresh" not in usage_of(world)


def test_removing_the_last_checkbox_refreshes(world):
    put(world, "a.md", TASKS)
    run(tools.edit_note_impl("a.md", PLAIN, replace_frontmatter=True))
    assert len(world.refreshes) == 1


def test_adding_the_first_checkbox_refreshes(world):
    put(world, "a.md", PLAIN)
    run(tools.edit_note_impl("a.md", "\n- [ ] new", append=True))
    assert len(world.refreshes) == 1


def test_unguarded_overwrite_assumes_the_incumbent_held_tasks_even_with_equal_bytes(world):
    put(world, "a.md", PLAIN)
    run(tools.write_file_impl("a.md", PLAIN, encoding="text", overwrite=True))
    assert len(world.refreshes) == 1


def test_guarded_overwrite_with_equal_bytes_does_not_refresh(world):
    data = put(world, "a.md", TASKS).read_bytes()
    run(tools.write_file_impl(
        "a.md", TASKS, encoding="text", overwrite=True,
        expected_hash=content_hash_for_bytes(data),
    ))
    assert world.refreshes == []


def test_guarded_overwrite_with_known_plain_incumbent_and_plain_bytes_does_not_refresh(world):
    data = put(world, "a.md", PLAIN).read_bytes()
    run(tools.write_file_impl(
        "a.md", "plain again\n", encoding="text", overwrite=True,
        expected_hash=content_hash_for_bytes(data),
    ))
    assert world.refreshes == []


def test_a_gate_fault_costs_a_refresh_never_the_write(world, monkeypatch):
    def boom(_data):
        raise RuntimeError("gate")

    monkeypatch.setattr(tools.tasks_service, "may_hold_tasks", boom)
    put(world, "a.md", PLAIN)
    out = run(tools.edit_note_impl("a.md", "other\n"))
    assert "MCP-REFUSAL" not in out
    assert len(world.refreshes) == 1


# ── what never refreshes ────────────────────────────────────────────────────


def test_dry_run_never_refreshes(world):
    put(world, "a.md", TASKS)
    run(tools.edit_note_impl("a.md", "- [x] Buy milk", find="- [ ] Buy milk", dry_run=True))
    assert world.seen == []


def test_identical_edit_never_refreshes(world):
    put(world, "a.md", TASKS)
    run(tools.edit_note_impl("a.md", TASKS, replace_frontmatter=True))
    assert world.refreshes == []


def test_a_refused_edit_never_refreshes(world):
    put(world, "a.md", TASKS)
    out = run(tools.edit_note_impl("a.md", "x", find="not there"))
    assert "match_not_found" in out
    assert world.seen == []


def test_a_conflicting_publication_never_refreshes(world, monkeypatch):
    put(world, "a.md", TASKS)

    def conflicting(*a, **k):
        raise RuntimeError("File changed while editing: a.md")

    monkeypatch.setattr(tools, "write_file_at", conflicting)
    out = run(tools.edit_note_impl("a.md", "- [x] Buy milk", find="- [ ] Buy milk"))
    assert "concurrent_write" in out
    assert world.seen == []


# ── D5: the response is identical whatever the refresh reports ─────────────


CODES = [task_index.RefreshResult(task_index.REFRESHED)] + [
    task_index.RefreshResult(task_index.LAGGED, c) for c in task_index.LAG_CODES
]


@pytest.mark.parametrize("result", CODES, ids=lambda r: r.usage)
@pytest.mark.parametrize("writer", ["edit", "frontmatter", "write_file", "delete", "delete_permanent"])
def test_every_refresh_code_leaves_the_response_and_event_unchanged(tmp_path, monkeypatch, writer, result):
    calls = {
        "edit": lambda: tools.edit_note_impl("a.md", "- [x] Buy milk", find="- [ ] Buy milk"),
        "frontmatter": lambda: tools.set_frontmatter_impl("a.md", {"k": "v"}),
        "write_file": lambda: tools.write_file_impl("a.md", PLAIN, encoding="text", overwrite=True),
        "delete": lambda: tools.delete_note_impl("a.md"),
        "delete_permanent": lambda: tools.delete_note_impl("a.md", permanent=True),
    }
    outs = []
    for mode in ("baseline", "refresh"):
        mp = pytest.MonkeyPatch()
        w = World()
        w.result = result
        root = tmp_path / mode
        root.mkdir()
        (root / "a.md").write_text(TASKS)

        async def log_usage(tool, params, duration_ms, size, w=w):
            w.usage.append(params)

        async def refresh(*, uid, root, target, rel, published, w=w):
            w.seen.append(("refresh", rel, published))
            return w.result

        async def record(uid, events, w=w):
            w.seen.append(("event", [(e.kind, e.path) for e in events]))

        mp.setattr(vault_service.settings, "vault_path", str(root))
        mp.setattr(tools, "_log_usage", log_usage)
        mp.setattr(task_index, "refresh_note_tasks", refresh)
        mp.setattr(tools, "_record_note_events", record)
        if mode == "baseline":
            async def nothing(*a, **k):
                return None
            mp.setattr(tools, "_refresh_after_write", nothing)
        vault_fs.reset_filesystem_probe_cache()
        token = current_permission.set("readwrite")
        try:
            out = run(calls[writer]())
        finally:
            current_permission.reset(token)
            mp.undo()
        # The soft delete names a timestamped trash path; compare up to it.
        outs.append(str(out).split(" → ")[0])
        assert w.events, writer
        if mode == "refresh":
            assert w.refreshes, writer
            assert w.usage[0]["task_refresh"] == result.usage
    assert outs[0] == outs[1]


# ── D4: a cancelled refresh ─────────────────────────────────────────────────


def test_a_cancelled_refresh_keeps_the_write_and_the_event_and_writes_no_usage_row(world):
    put(world, "a.md", TASKS)
    world.raise_in_refresh = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        run(tools.edit_note_impl("a.md", "- [x] Buy milk", find="- [ ] Buy milk"))
    assert "- [x] Buy milk" in (world.root / "a.md").read_text()
    assert len(world.events) == 1
    assert world.usage == []


def test_an_unexpected_refresh_exception_is_swallowed(world):
    put(world, "a.md", TASKS)
    world.raise_in_refresh = RuntimeError("boom")
    out = run(tools.edit_note_impl("a.md", "- [x] Buy milk", find="- [ ] Buy milk"))
    assert "MCP-REFUSAL" not in out
    assert len(world.events) == 1
    assert "task_refresh" not in usage_of(world)
