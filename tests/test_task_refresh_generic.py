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


def test_write_file_creating_a_note_attempts_the_refresh(world):
    # Impl review r1: the decision never depends on a pre-publication sample.
    # A genuine creation finds no index row (`not_indexed`, nothing written).
    world.result = task_index.RefreshResult(task_index.LAGGED, task_index.LAG_NOT_INDEXED)
    out = run(tools.write_file_impl("new.md", PLAIN, encoding="text"))
    assert "MCP-REFUSAL" not in out
    assert [s[0] for s in world.seen] == ["event", "refresh"]
    assert usage_of(world)["task_refresh"] == "lagged:not_indexed"


def test_write_file_refreshes_a_note_moved_onto_the_path_after_the_sample(world, monkeypatch):
    """Impl review r1, MAJOR. `write_file(overwrite=True)` samples the path
    (absent), then a move lands an indexed task note there before the
    publication, which overwrites it. The refresh must still run."""
    real_publish = tools.write_bytes_at
    calls = {"n": 0}

    def move_then_publish(target, data, **kw):
        # Every pre-publication sample has been taken; the move lands now,
        # inside the confirmed step, immediately before the publication.
        calls["n"] += 1
        (world.root / "B.md").rename(world.root / "A.md")
        return real_publish(target, data, **kw)

    put(world, "B.md", TASKS)
    monkeypatch.setattr(tools, "write_bytes_at", move_then_publish)
    out = run(tools.write_file_impl("A.md", "- [ ] different\n", encoding="text", overwrite=True))
    assert "MCP-REFUSAL" not in out, out
    assert calls["n"] == 1
    assert (world.root / "A.md").read_text() == "- [ ] different\n"
    # The event still says `created` (the kind is a best guess, #351); the
    # refresh no longer depends on that guess.
    assert world.events[0][1] == [("created", "A.md")]
    assert len(world.refreshes) == 1 and world.refreshes[0][1] == "A.md"


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


def test_a_cancelled_call_releases_its_concurrency_lease(world, monkeypatch):
    """The cancellation scenario's lease clause, in `queue` mode (where a
    lease is a real slot): after a call cancelled during its refresh, every
    counter of the controller is back to zero."""
    from src.auth.session import current_principal
    from src.config import Settings
    from src.services import concurrency

    controller = concurrency.reset_controller(Settings(
        _env_file=None, secret_key="issue-432-test-secret-only-0123456789abcdef",
        mcp_concurrency_mode="queue"))
    token = current_principal.set(("api_key", 1))
    held: list[int] = []

    async def refresh_holding(**_kw):
        held.append(controller.snapshot()["active"]["tools"])
        raise asyncio.CancelledError()

    try:
        put(world, "a.md", TASKS)
        monkeypatch.setattr(task_index, "refresh_note_tasks", refresh_holding)

        async def go():
            await tools.edit_note_impl("a.md", "- [x] Buy milk", find="- [ ] Buy milk")

        with pytest.raises(asyncio.CancelledError):
            run(go())
        assert held == [1], "a lease was held while the refresh ran"
        snap = controller.snapshot()
        assert snap["active"]["tools"] == 0 and snap["active"]["writers"] == 0
        assert all(v == 0 for v in snap["active"]["classes"].values()), snap
        assert all(v == 0 for v in snap["waiting"].values()), snap
        assert snap["registries"] == {"fingerprints": 0, "tenants": 0, "principals": 0}
    finally:
        current_principal.reset(token)
        concurrency.reset_controller()


# ── the gate and the derivation run off the event loop (D6) ────────────────


def test_the_gate_and_the_derivation_run_in_a_worker_thread(world, monkeypatch):
    import threading

    from src.services import tasks as tasks_service

    seen: dict[str, list[int]] = {"gate": [], "derive": []}
    real_gate = tasks_service.may_hold_tasks
    real_derive = task_index.tasks_from_note_text

    def gate(data):
        seen["gate"].append(threading.get_ident())
        return real_gate(data)

    def derive(data, **kw):
        seen["derive"].append(threading.get_ident())
        return real_derive(data, **kw)

    async def locked(**kw):
        return task_index.RefreshResult(task_index.REFRESHED)

    # The real `refresh_note_tasks` (not the world's stand-in), with only its
    # database half stood in.
    monkeypatch.setattr(task_index, "refresh_note_tasks", REAL_REFRESH)
    monkeypatch.setattr(task_index, "_run_locked", locked)
    monkeypatch.setattr(tools.tasks_service, "may_hold_tasks", gate)
    monkeypatch.setattr(task_index, "tasks_from_note_text", derive)
    put(world, "a.md", TASKS)

    async def go():
        loop_thread = threading.get_ident()
        await tools.edit_note_impl("a.md", "- [x] Buy milk", find="- [ ] Buy milk")
        return loop_thread

    loop_thread = run(go())
    assert seen["gate"] and seen["derive"], seen
    assert loop_thread not in seen["gate"] + seen["derive"]


# ── responses pinned to their pre-#432 text (aafb9f0) ──────────────────────


def _hash(world, rel):
    return content_hash_for_bytes((world.root / rel).read_bytes())


def test_set_frontmatter_text_is_the_pre_change_text(world):
    put(world, "a.md", TASKS)
    out = run(tools.set_frontmatter_impl("a.md", {"k": "v"}))
    assert world.refreshes
    assert out == f"Updated frontmatter in a.md (set: k) — content_hash: {_hash(world, 'a.md')}"


def test_write_file_text_is_the_pre_change_text(world):
    put(world, "a.md", TASKS)
    out = run(tools.write_file_impl("a.md", PLAIN, encoding="text", overwrite=True))
    assert world.refreshes
    n = len(PLAIN.encode())
    assert out == f"Wrote {n:,} bytes to a.md — content_hash: {_hash(world, 'a.md')}"


def test_delete_note_texts_are_the_pre_change_texts(world):
    import re

    put(world, "a.md", TASKS)
    out = run(tools.delete_note_impl("a.md", permanent=True))
    assert out == "Permanently deleted: a.md"
    put(world, "b.md", TASKS)
    out = run(tools.delete_note_impl("b.md"))
    assert re.fullmatch(r"Soft-deleted: b\.md → \.trash/\d{8}-\d{6}-b\.md-[0-9a-f]{8}", out), out
    assert len(world.refreshes) == 2


# ── the only callers (design D1) ────────────────────────────────────────────


def test_the_only_callers_of_the_generic_refresh_are_the_four_writers():
    import ast
    import pathlib

    src = pathlib.Path(tools.__file__).resolve().parents[1]
    callers: set[tuple[str, str]] = set()
    for path in src.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(fn):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                        and node.func.id == "_refresh_after_write"):
                    callers.add((path.name, fn.name))
                elif (isinstance(node, ast.Attribute)
                      and node.attr == "_refresh_after_write"):
                    callers.add((path.name, fn.name))
    assert callers == {
        ("tools.py", "edit_note_impl"),
        ("tools.py", "set_frontmatter_impl"),
        ("tools.py", "write_file_impl"),
        ("tools.py", "delete_note_impl"),
    }, callers


# ── more paths that never refresh ──────────────────────────────────────────


def test_set_frontmatter_no_changes_never_refreshes(world):
    put(world, "a.md", "---\nk: v\n---\n" + TASKS)
    out = run(tools.set_frontmatter_impl("a.md", {"k": "v"}))
    assert out.startswith("No changes for a.md")
    assert world.seen == []


def test_set_frontmatter_refusal_never_refreshes(world):
    put(world, "a.md", "---\nk: [unclosed\n---\n" + TASKS)
    out = run(tools.set_frontmatter_impl("a.md", {"x": 1}))
    assert "MCP-REFUSAL" in out
    assert world.seen == []


def test_delete_note_refusals_never_clear(world):
    out = run(tools.delete_note_impl("missing.md"))
    assert "MCP-REFUSAL" in out
    put(world, "a.md", TASKS)
    out = run(tools.delete_note_impl("a.md", expected_hash="sha256:" + "0" * 64))
    assert "stale_precondition" in out
    assert (world.root / "a.md").exists()
    assert world.seen == []


def test_write_file_refusals_never_refresh(world):
    put(world, "a.md", TASKS)
    out = run(tools.write_file_impl("a.md", PLAIN, encoding="text"))  # exists, no overwrite
    assert "already_exists" in out
    out = run(tools.write_file_impl(
        "a.md", PLAIN, encoding="text", overwrite=True, expected_hash="sha256:" + "0" * 64))
    assert "stale_precondition" in out
    out = run(tools.write_file_impl("a.md", "!!!", encoding="base64", overwrite=True))
    assert "invalid_argument" in out
    assert world.seen == []


REAL_REFRESH = task_index.refresh_note_tasks
