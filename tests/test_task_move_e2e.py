"""`move_task`'s wiring, offline (#430, design D10–D18; tasks.md 2.4).

Everything runs the REAL `_tracked` wrapper, the real planners and the real
anchored publication over a temporary single-user vault; only the database
touch points are stood in for (the settings read, the write-time task
refresh, the change-feed append), each recording what it was handed, in
order. Failures are injected at each publication by wrapping
`tools.write_file_at`, `tools.confirmed_publication` or `tools.read_bytes_at`
— hooks between the steps, never sleeps.

Pinned here: every row of the D10 outcome table (6a and 14a included), the
D11 partial outcome (prose lines, sentinel fields, `complete with:` calls that
then complete the move), the D12 precondition matrix, the event and refresh
order, the usage row, the dry run, each note's permissions across a move
(#447), and a move into an excluded sync-conflict copy (#443). The same flows
against PostgreSQL are in `tests/integration/test_task_move_pg.py`.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import errno
import json
import os
import stat
import tempfile

os.environ.setdefault("SECRET_KEY", "test")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("VAULT_PATH", "/tmp/test-vault")
os.chdir(tempfile.gettempdir())

import pytest  # noqa: E402

import src.mcp_server.tools as tools  # noqa: E402
from src.mcp_server.auth import current_permission  # noqa: E402
from src.services import task_index, task_line, task_rule, vault_fs  # noqa: E402
from src.services import vault as vault_service  # noqa: E402
from src.services.note_hash import canonical_note_hash  # noqa: E402
from src.services.task_settings import EffectiveTaskSettings  # noqa: E402
from src.services.vault import (  # noqa: E402
    VaultAnchorUnavailable,
    VaultAssignmentChanged,
    VaultConfirmationUnavailable,
    content_hash_for_bytes,
)

NOW = dt.datetime(2026, 11, 16, 12, 0, tzinfo=dt.timezone.utc)
REQUIRE_ACL_ENV = "OMCP_TEST_REQUIRE_POSIX_ACL"


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


def line_of(out: str, prefix: str) -> str:
    for ln in str(out).splitlines():
        if ln.startswith(prefix):
            return ln
    raise AssertionError(f"no line starting {prefix!r} in:\n{out}")


class World:
    def __init__(self):
        self.events: list = []
        self.usage: list[dict] = []
        self.settings = settings_with()
        self.settings_reads = 0
        self.refresh_results: dict[str, task_index.RefreshResult] = {}


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
        return w.refresh_results.get(rel, task_index.RefreshResult(task_index.REFRESHED))

    async def record(uid, events):
        w.events.append(("event", [(e.kind, e.path, e.content_hash) for e in events]))

    monkeypatch.setattr(vault_service.settings, "vault_path", str(tmp_path))
    monkeypatch.setattr(tools, "_log_usage", log_usage)
    monkeypatch.setattr(tools, "_effective_task_settings", effective)
    monkeypatch.setattr(tools, "_task_now", lambda: NOW)
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


def write(world, rel, text):
    p = world.root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(text.encode("utf-8"))
    return p


def read(world, rel) -> str:
    return (world.root / rel).read_bytes().decode("utf-8")


def h(text: str) -> str:
    return content_hash_for_bytes(text.encode("utf-8"))


def order(world) -> list[tuple[str, str]]:
    """The refresh/event sequence as (kind, path)."""
    out = []
    for e in world.events:
        if e[0] == "refresh":
            out.append(("refresh", e[1]))
        else:
            out.append(("event", e[1][0][1]))
    return out


INBOX = "# Inbox\n- [ ] Call the bank 📅 2026-11-20\n    - ask about the fee\n    - [ ] find the account number\n- [ ] Other\n"
TASK = "- [ ] Call the bank 📅 2026-11-20"
MONEY = "# Money\n- [ ] Pay rent\n"
MOVED_MONEY = (
    "# Money\n- [ ] Pay rent\n- [ ] Call the bank 📅 2026-11-20\n    - ask about the fee\n"
    "    - [ ] find the account number\n"
)
LEFT_INBOX = "# Inbox\n- [ ] Other\n"


def setup_pair(world):
    write(world, "Inbox.md", INBOX)
    write(world, "Projects/Money.md", MONEY)
    return key_of(TASK)


def write_hook(monkeypatch, on_call):
    """Wrap `tools.write_file_at`: `on_call(n, target)` runs before the n-th
    publication (1-based), then the real write."""
    original = tools.write_file_at
    calls = []

    def publish(target, content, **kw):
        calls.append(target.rel)
        on_call(len(calls), target)
        return original(target, content, **kw)

    monkeypatch.setattr(tools, "write_file_at", publish)
    return calls


# ── row 1: arguments, before any read ────────────────────────────────────────


@pytest.fixture
def no_read(monkeypatch):
    def boom(*a, **kw):
        raise AssertionError("no note may be opened for an invalid argument")

    monkeypatch.setattr(tools, "open_mutable", boom)


def test_a_suffixed_id_without_a_hash_is_refused_before_any_read(world, no_read):
    """Scenario 17 (first half): a suffixed id without `expected_hash`."""
    out = run(tools.move_task_impl("Inbox.md", "0123456789ab.1of2", "B.md"))
    assert code_of(out) == "precondition_required"
    assert payload_of(out)["nothing_written"] is True
    assert world.settings_reads == 0


@pytest.mark.parametrize("kw,needle", [
    (dict(resume=True, allow_duplicate=True), "allow_duplicate"),
    (dict(resume=True, to_section="Later"), "to_section"),
])
def test_resume_combinations_are_invalid_before_any_read(world, no_read, kw, needle):
    """Scenario: Resume without exactly one copy is refused (the forbidden
    combinations half, D1)."""
    out = run(tools.move_task_impl("A.md", "0123456789ab", "B.md", **kw))
    assert code_of(out) == "invalid_argument"
    assert needle in out


def test_a_malformed_destination_hash_names_to_expected_hash(world, no_read):
    out = run(tools.move_task_impl("A.md", "0123456789ab", "B.md", to_expected_hash="abc"))
    assert code_of(out) == "malformed_precondition"
    assert "to_expected_hash for B.md" in out
    assert payload_of(out)["path"] == "B.md"


def test_a_read_only_credential_is_refused(world):
    key = setup_pair(world)
    token = current_permission.set("read")
    try:
        out = run(tools.move_task_impl("Inbox.md", key, "Projects/Money.md"))
    finally:
        current_permission.reset(token)
    assert code_of(out) == "permission_denied"
    assert read(world, "Inbox.md") == INBOX and world.events == []


# ── row 18: success, and the order of everything after a publication ────────


def test_a_task_moves_with_its_context_and_sub_task(world):
    """Scenario: A task moves with its context and sub-task; Scenario: Two
    events, attributed (order half); Scenario: The id survives the move."""
    key = setup_pair(world)
    out = run(tools.move_task_impl("Inbox.md", key, "Projects/Money.md"))
    assert code_of(out) is None, out
    assert read(world, "Projects/Money.md") == MOVED_MONEY
    assert read(world, "Inbox.md") == LEFT_INBOX
    lines = out.splitlines()
    assert lines[0] == "Moved task from `Inbox.md` to `Projects/Money.md`"
    assert lines[1] == f"line 3: {TASK}"
    assert lines[2] == f"id: {key}"
    assert "moved: 3 lines, 2 tasks (1 sub-task)" in lines
    assert f"sub-task line 5: id {key_of('- [ ] find the account number')}" in lines
    assert "source: removed lines 2–4" in lines
    assert f"destination content_hash: {h(MOVED_MONEY)}" in lines
    assert f"source content_hash: {h(LEFT_INBOX)}" in lines
    assert lines[-2:] == ["index (destination): refreshed", "index (source): refreshed"]
    # D10 steps 3–8: destination publish → refresh → event, then the source.
    assert order(world) == [("refresh", "Projects/Money.md"), ("event", "Projects/Money.md"),
                            ("refresh", "Inbox.md"), ("event", "Inbox.md")]
    assert world.events[1][1][0] == ("modified", "Projects/Money.md",
                                     canonical_note_hash(MOVED_MONEY.encode()))
    (row,) = world.usage
    assert row["move_outcome"] == "moved"
    assert row["task_refresh"] == "refreshed" and row["task_refresh_to"] == "refreshed"
    # Scenario: The id survives the move — the printed id resolves at the destination.
    printed = lines[2].split("id: ", 1)[1]
    out = run(tools.complete_task_impl("Projects/Money.md", printed))
    assert code_of(out) is None, out
    assert "- [x] Call the bank 📅 2026-11-20 ✅" in read(world, "Projects/Money.md")


def test_the_usage_row_holds_the_arguments_and_no_task_text(world):
    """Scenario 33: params and result fields; no task text, for a success, a
    partial and a refusal."""
    key = setup_pair(world)
    run(tools.move_task_impl("Inbox.md", key, "Projects/Money.md", to_section="Money",
                             expected_hash=h(INBOX)))
    row = world.usage[-1]
    for name in ("path", "id", "to_path", "to_section", "dry_run", "allow_duplicate", "resume"):
        assert name in row, name
    assert row["expected_hash"] is True and row["to_expected_hash"] is False
    run(tools.move_task_impl("Inbox.md", key_of("- [ ] Pay rent"), "Projects/Money.md"))  # refused
    write(world, "Inbox.md", INBOX)
    write(world, "Projects/Money.md", MONEY)
    pna = []

    def fail(n, t):
        if t.rel == "Inbox.md":
            pna.append(1)
            raise vault_fs.PermissionsNotApplied("fchmod the staged copy", errno.EIO)

    mp = pytest.MonkeyPatch()
    write_hook(mp, fail)
    try:
        out = run(tools.move_task_impl("Inbox.md", key, "Projects/Money.md"))  # partial
    finally:
        mp.undo()
    assert payload_of(out)["code"] == "task_move_incomplete" and pna
    assert world.usage[-1]["move_outcome"] == "incomplete:permissions_not_applied"
    assert len(world.usage) == 3
    for row in world.usage:
        flat = json.dumps(row, ensure_ascii=False)
        assert "Call the bank" not in flat and "account number" not in flat and "Pay rent" not in flat


# ── row 2: refusals in step 1, either note ──────────────────────────────────


def test_a_missing_destination_is_never_created(world):
    """Scenario: A missing destination is never created."""
    key = setup_pair(world)
    out = run(tools.move_task_impl("Inbox.md", key, "Nowhere.md"))
    assert code_of(out) == "not_found"
    assert not (world.root / "Nowhere.md").exists()
    assert read(world, "Inbox.md") == INBOX and world.events == []


def test_a_stale_destination_hash_writes_nothing(world):
    """Scenario: A stale destination hash writes nothing; scenario 29."""
    key = setup_pair(world)
    out = run(tools.move_task_impl("Inbox.md", key, "Projects/Money.md",
                                   to_expected_hash="sha256:" + "0" * 64))
    assert code_of(out) == "stale_precondition"
    assert payload_of(out)["path"] == "Projects/Money.md"
    assert payload_of(out)["current_hash"] == h(MONEY)
    assert read(world, "Inbox.md") == INBOX and read(world, "Projects/Money.md") == MONEY


def test_a_stale_source_hash_names_the_source(world):
    key = setup_pair(world)
    out = run(tools.move_task_impl("Inbox.md", key, "Projects/Money.md",
                                   expected_hash="sha256:" + "1" * 64))
    assert code_of(out) == "stale_precondition"
    assert payload_of(out)["path"] == "Inbox.md"


def test_required_mode_with_a_bare_id_proceeds(world, monkeypatch):
    """Scenario: Required mode with a bare id; scenario 29."""
    monkeypatch.setattr(tools.settings, "write_precondition_required", True)
    key = setup_pair(world)
    out = run(tools.move_task_impl("Inbox.md", key, "Projects/Money.md"))
    assert code_of(out) is None, out
    assert read(world, "Projects/Money.md") == MOVED_MONEY


@pytest.mark.parametrize("source,line,needle", [
    ("- [ ] Project\n    - [ ] step one\n    - [ ] step two\n", "- [ ] step one", "nested"),
    ("> - [ ] Renew passport\n", "- [ ] Renew passport", "blockquote or callout"),
    ("- [ ] t\n    > quoted context\n", "- [ ] t", "blockquote line"),
    ("- [ ] t\n    ```\n    x\n    ```\n", "- [ ] t", "fenced code block"),
    ("- [ ] t\n    ```\n    x\n", "- [ ] t", "fenced code block"),
    ("- [ ] t\n  # Notes\n", "- [ ] t", "heading"),
])
def test_scope_refusals_write_nothing_in_either_note(world, source, line, needle):
    """Scenarios: A nested task is refused; A callout task is refused; A
    blockquote line in the block is refused; A fence line in the block is
    refused; A heading under the task is refused (scenarios 10–14)."""
    write(world, "A.md", source)
    write(world, "B.md", "- [ ] b\n")
    out = run(tools.move_task_impl("A.md", key_of(line), "B.md"))
    assert code_of(out) == "task_malformed", out
    assert needle in out and "edit_note" in out
    assert payload_of(out)["path"] == "A.md" and payload_of(out)["nothing_written"] is True
    assert read(world, "A.md") == source and read(world, "B.md") == "- [ ] b\n"
    assert world.events == []


def test_a_quoted_destination_anchor_is_refused(world):
    """Scenario: A quoted destination anchor is refused (scenario 11)."""
    write(world, "A.md", "- [ ] t\n")
    write(world, "B.md", "> - [ ] a\n")
    out = run(tools.move_task_impl("A.md", key_of("- [ ] t"), "B.md"))
    assert code_of(out) == "task_malformed"
    assert payload_of(out)["path"] == "B.md"
    assert read(world, "A.md") == "- [ ] t\n" and read(world, "B.md") == "> - [ ] a\n"


def test_a_block_id_collision_is_refused(world):
    """Scenario: A block id that already exists at the destination is
    refused (scenario 40)."""
    write(world, "Inbox.md", "- [ ] Follow up ^followup\n")
    write(world, "Project.md", "Some text ^FollowUp\n")
    out = run(tools.move_task_impl("Inbox.md", key_of("- [ ] Follow up ^followup"), "Project.md"))
    assert code_of(out) == "block_id_conflict"
    assert "followup" in out and "line 1" in out
    assert payload_of(out)["nothing_written"] is True
    assert read(world, "Inbox.md") == "- [ ] Follow up ^followup\n"


def test_a_moved_block_id_is_named(world):
    write(world, "Inbox.md", "- [ ] Follow up ^followup\n")
    write(world, "Project.md", "")
    out = run(tools.move_task_impl("Inbox.md", key_of("- [ ] Follow up ^followup"), "Project.md"))
    assert code_of(out) is None, out
    assert "block ids moved: ^followup (links to `Inbox.md`#^followup no longer resolve)" in out


def test_an_independent_identical_task_is_not_deleted(world):
    """Scenario: An independent identical task at the destination is not
    deleted (scenario 37)."""
    note = "- [ ] Call Alex\n    - about the lease\n"
    write(world, "Inbox.md", note)
    write(world, "Project.md", note)
    k = key_of("- [ ] Call Alex")
    out = run(tools.move_task_impl("Inbox.md", k, "Project.md"))
    assert code_of(out) == "duplicate_task"
    assert "byte-identical" in out and "resume=true" in out and "allow_duplicate=true" in out
    assert f"- `Project.md`:1 id {k}" in out
    assert read(world, "Inbox.md") == note and read(world, "Project.md") == note
    out = run(tools.move_task_impl("Inbox.md", k, "Project.md", allow_duplicate=True))
    assert code_of(out) is None, out
    assert read(world, "Project.md") == note + note and read(world, "Inbox.md") == ""
    assert f"id: {k}.2of2" in out  # scenario 18: the moved copy's suffixed id


def test_an_open_duplicate_at_the_destination_is_refused(world):
    """Scenario: An open duplicate at the destination is refused."""
    write(world, "Inbox.md", "- [ ] Call the bank\n")
    write(world, "Projects/Money.md", "- [ ] call the  Bank\n")
    k = key_of("- [ ] Call the bank")
    out = run(tools.move_task_impl("Inbox.md", k, "Projects/Money.md"))
    assert code_of(out) == "duplicate_task"
    assert "`Projects/Money.md`:1" in out
    out = run(tools.move_task_impl("Inbox.md", k, "Projects/Money.md", allow_duplicate=True))
    assert code_of(out) is None
    assert read(world, "Projects/Money.md") == "- [ ] call the  Bank\n- [ ] Call the bank\n"


def test_a_done_task_into_an_archive_is_not_refused_for_its_description(world):
    """Scenario: A done task into an archive is not refused for its
    description; Scenario: A done task is moved as it is."""
    write(world, "Inbox.md", "- [x] Water plants ✅ 2026-11-01\n- [x] Paid rent 🔁 every month 📅 2026-11-01 ✅ 2026-11-02\n")
    write(world, "Archive.md", "- [x] Water plants ✅ 2026-10-01\n")
    out = run(tools.move_task_impl("Inbox.md", key_of("- [x] Water plants ✅ 2026-11-01"), "Archive.md"))
    assert code_of(out) is None, out
    paid = "- [x] Paid rent 🔁 every month 📅 2026-11-01 ✅ 2026-11-02"
    out = run(tools.move_task_impl("Inbox.md", key_of(paid), "Archive.md"))
    assert code_of(out) is None, out
    assert read(world, "Archive.md") == (
        "- [x] Water plants ✅ 2026-10-01\n- [x] Water plants ✅ 2026-11-01\n" + paid + "\n")
    assert read(world, "Inbox.md") == ""


# ── row 3: the dry run ──────────────────────────────────────────────────────


def test_a_dry_run_shows_both_diffs_and_writes_nothing(world):
    key = setup_pair(world)
    out = run(tools.move_task_impl("Inbox.md", key, "Projects/Money.md", dry_run=True))
    assert code_of(out) is None, out
    lines = out.splitlines()
    assert lines[0] == "Dry run — nothing was written."
    assert lines[1] == "Would move task from `Inbox.md` to `Projects/Money.md`"
    assert f"line 3: {TASK}" in lines and "destination diff:" in lines and "source diff:" in lines
    assert "+- [ ] Call the bank 📅 2026-11-20" in out and "-- [ ] Call the bank 📅 2026-11-20" in out
    assert "content_hash" not in out
    assert read(world, "Inbox.md") == INBOX and read(world, "Projects/Money.md") == MONEY
    assert world.events == []
    assert world.usage[0]["task_refresh"] == "skipped"


# ── row 4: the destination's in-call conflict ───────────────────────────────


def _poke(world, rel, n):
    p = world.root / rel
    p.write_bytes(p.read_bytes() + f"other writer {n}\n".encode())


@pytest.mark.parametrize("conflicts", [1, 2])
def test_a_destination_conflict_is_retried_from_both_reads(world, monkeypatch, conflicts):
    key = setup_pair(world)
    write_hook(monkeypatch, lambda n, t: _poke(world, "Projects/Money.md", n)
               if t.rel == "Projects/Money.md" and n <= conflicts else None)
    out = run(tools.move_task_impl("Inbox.md", key, "Projects/Money.md"))
    assert code_of(out) is None, out
    assert read(world, "Inbox.md") == LEFT_INBOX
    assert "other writer 1" in read(world, "Projects/Money.md")
    assert world.usage[0]["task_write_attempts"] == f"{conflicts + 1}/1"


def test_three_destination_conflicts_write_nothing(world, monkeypatch):
    key = setup_pair(world)
    write_hook(monkeypatch, lambda n, t: _poke(world, "Projects/Money.md", n))
    out = run(tools.move_task_impl("Inbox.md", key, "Projects/Money.md"))
    assert code_of(out) == "concurrent_write"
    assert payload_of(out)["nothing_written"] is True
    assert payload_of(out)["path"] == "Projects/Money.md"
    assert read(world, "Inbox.md") == INBOX and world.events == []


def test_no_destination_retry_with_to_expected_hash(world, monkeypatch):
    key = setup_pair(world)
    calls = write_hook(monkeypatch, lambda n, t: _poke(world, "Projects/Money.md", n))
    out = run(tools.move_task_impl("Inbox.md", key, "Projects/Money.md", to_expected_hash=h(MONEY)))
    assert code_of(out) == "concurrent_write" and len(calls) == 1
    assert read(world, "Inbox.md") == INBOX


def test_a_destination_that_vanishes_is_never_created(world, monkeypatch):
    """Scenario 45: the destination deleted after step 1 fails as the in-call
    conflict; nothing is created and the source is untouched."""
    key = setup_pair(world)

    def vanish(n, t):
        if t.rel == "Projects/Money.md":
            (world.root / "Projects/Money.md").unlink(missing_ok=True)

    write_hook(monkeypatch, vanish)
    out = run(tools.move_task_impl("Inbox.md", key, "Projects/Money.md"))
    assert code_of(out) in {"concurrent_write", "io_failure", "not_found"}, out
    assert not (world.root / "Projects/Money.md").exists()
    assert read(world, "Inbox.md") == INBOX and world.events == []


# ── rows 5, 6, 6a: the destination publication fails ────────────────────────


def confirm_hook(monkeypatch, on_call):
    """Wrap `tools.confirmed_publication`: `on_call(n)` runs before the n-th
    confirmation and may raise in its place."""
    original = tools.confirmed_publication
    calls = []

    async def confirmed(uid, publish):
        calls.append(1)
        on_call(len(calls))
        return await original(uid, publish)

    monkeypatch.setattr(tools, "confirmed_publication", confirmed)
    return calls


def test_a_reassignment_before_the_destination_write_writes_nothing(world, monkeypatch):
    """Row 5; Scenario: Nothing is removed when the destination did not
    publish (refused)."""
    key = setup_pair(world)

    def refuse(n):
        raise VaultAssignmentChanged("The vault assignment changed during this call.")

    confirm_hook(monkeypatch, refuse)
    out = run(tools.move_task_impl("Inbox.md", key, "Projects/Money.md"))
    assert code_of(out) == "vault_assignment_changed"
    assert read(world, "Inbox.md") == INBOX and read(world, "Projects/Money.md") == MONEY
    assert world.events == []


def test_an_os_error_at_the_destination_is_uncertain_and_names_both_calls(world, monkeypatch):
    """Row 6; scenario 5; Scenario: Nothing is removed when the destination did
    not publish (OS error)."""
    key = setup_pair(world)

    def fail(n, t):
        raise OSError(errno.EIO, "I/O error")

    write_hook(monkeypatch, fail)
    out = run(tools.move_task_impl("Inbox.md", key, "Projects/Money.md"))
    assert code_of(out) == "publication_uncertain"
    assert "nothing_written" not in payload_of(out)
    assert out.disposition == "partial"
    complete = line_of(out, "complete with:")
    assert "resume=true" in complete and 'read_note(path="Projects/Money.md")' in complete
    assert read(world, "Inbox.md") == INBOX and world.events == []
    assert world.usage[0]["move_outcome"] == "uncertain"


def test_permissions_not_applied_at_the_destination_is_io_failure(world, monkeypatch):
    """Row 6a; scenario 42; Scenario: Nothing is removed when the destination
    did not publish (permission rule)."""
    key = setup_pair(world)
    before = {r: os.lstat(world.root / r).st_mode for r in ("Inbox.md", "Projects/Money.md")}

    def fail(n, t):
        raise vault_fs.PermissionsNotApplied("write the access ACL", errno.EIO)

    write_hook(monkeypatch, fail)
    out = run(tools.move_task_impl("Inbox.md", key, "Projects/Money.md"))
    assert code_of(out) == "io_failure"
    assert "nothing was written" in out and "permissions" in out
    assert out.disposition == "refused"
    assert read(world, "Inbox.md") == INBOX and read(world, "Projects/Money.md") == MONEY
    assert {r: os.lstat(world.root / r).st_mode for r in before} == before
    assert world.events == []


# ── rows 7 and 15: a lagging refresh ────────────────────────────────────────


def test_a_lagging_destination_refresh_does_not_stop_the_move(world):
    """Row 7; Scenario: A lagging destination refresh is reported, not hidden
    (offline half); scenarios 30 and 39."""
    key = setup_pair(world)
    world.refresh_results["Projects/Money.md"] = task_index.RefreshResult(task_index.LAGGED, "lock_timeout")
    out = run(tools.move_task_impl("Inbox.md", key, "Projects/Money.md"))
    assert code_of(out) is None, out
    assert "index (destination): may lag (lock_timeout)" in out
    assert out.rstrip().endswith("index (source): refreshed")
    assert read(world, "Inbox.md") == LEFT_INBOX
    assert world.usage[0]["task_refresh_to"] == "lagged:lock_timeout"


def test_a_lagging_source_refresh_keeps_the_move(world):
    key = setup_pair(world)
    world.refresh_results["Inbox.md"] = task_index.RefreshResult(task_index.LAGGED, "superseded")
    out = run(tools.move_task_impl("Inbox.md", key, "Projects/Money.md"))
    assert code_of(out) is None
    assert out.rstrip().endswith("index (source): may lag (superseded)")


def test_a_move_into_an_excluded_conflict_copy(world, monkeypatch):
    """Scenario: A move into an excluded conflict copy (scenario 46)."""
    monkeypatch.setattr(tools.settings, "index_exclude_conflict_files", True, raising=False)
    copy = "N (conflict 2026-05-29-19-45-39).md"
    write(world, "Inbox.md", "- [ ] x\n")
    write(world, copy, "")
    world.refresh_results[copy] = task_index.RefreshResult(task_index.LAGGED, task_index.LAG_NOT_INDEXED)
    out = run(tools.move_task_impl("Inbox.md", key_of("- [ ] x"), copy))
    assert code_of(out) is None, out
    assert "index (destination): may lag (not_indexed)" in out
    assert "excluded" not in out.lower() and "conflict" not in out.split("\n", 1)[1].lower()
    assert read(world, copy) == "- [ ] x"
    # And out of such a copy into an indexed note.
    write(world, "B.md", "")
    out = run(tools.move_task_impl(copy, key_of("- [ ] x"), "B.md"))
    assert code_of(out) is None, out
    assert read(world, "B.md") == "- [ ] x" and read(world, copy) == ""


# ── rows 8 and 8a: the destination presence check ───────────────────────────


def test_an_older_destination_saved_between_the_steps_stops_the_move(world, monkeypatch):
    """Row 8; Scenario: An older destination saved between the steps stops the
    move (scenario 34). The hook runs after the destination publication and
    before the source's confirmation, as an outside editor's save would."""
    key = setup_pair(world)
    world.refresh_results["Projects/Money.md"] = task_index.RefreshResult(task_index.LAGGED, "superseded")

    def older(n):
        if n == 2:
            write(world, "Projects/Money.md", MONEY)

    confirm_hook(monkeypatch, older)
    out = run(tools.move_task_impl("Inbox.md", key, "Projects/Money.md"))
    assert code_of(out) == "task_move_incomplete", out
    p = payload_of(out)
    assert p["reason"] == "destination_changed" and "destination_id" not in p
    assert p["path"] == "Inbox.md" and p["destination_path"] == "Projects/Money.md"
    assert p["source_id"] == key and "nothing_written" not in p
    assert out.disposition == "partial"
    assert line_of(out, "destination:").startswith(
        "destination: did not hold the moved lines at line 3 when re-read before the source write")
    assert line_of(out, "source:") == "source: this call did not change it"
    assert read(world, "Inbox.md") == INBOX
    # The destination event was recorded, the source's was not.
    assert order(world) == [("refresh", "Projects/Money.md"), ("event", "Projects/Money.md")]
    # The completing call re-inserts the task.
    out = run(tools.move_task_impl("Inbox.md", key, "Projects/Money.md"))
    assert code_of(out) is None, out
    assert read(world, "Projects/Money.md") == MOVED_MONEY and read(world, "Inbox.md") == LEFT_INBOX


def test_a_line_added_above_the_inserted_lines_stops_the_move(world, monkeypatch):
    """Row 8 (L18); Scenario: A line added above the inserted lines stops the
    move (scenario 36); then the named completing calls finish it with no
    third copy."""
    key = setup_pair(world)

    def above(n):
        if n == 2:
            p = world.root / "Projects/Money.md"
            p.write_bytes(b"Top line\n" + p.read_bytes())

    confirm_hook(monkeypatch, above)
    out = run(tools.move_task_impl("Inbox.md", key, "Projects/Money.md"))
    assert payload_of(out)["reason"] == "destination_changed"
    assert read(world, "Inbox.md") == INBOX
    complete = line_of(out, "complete with:")
    assert "resume=true" in complete
    monkeypatch.setattr(tools, "confirmed_publication", vault_service.confirmed_publication)
    out = run(tools.move_task_impl("Inbox.md", key, "Projects/Money.md"))
    assert code_of(out) == "duplicate_task"
    out = run(tools.move_task_impl("Inbox.md", key, "Projects/Money.md", resume=True))
    assert code_of(out) is None, out
    assert read(world, "Projects/Money.md") == "Top line\n" + MOVED_MONEY
    assert read(world, "Inbox.md") == LEFT_INBOX


def test_the_presence_check_runs_inside_the_publication_callback(world, monkeypatch):
    """Test plan: a hook that mutates the destination AFTER the confirming
    read and immediately before the callback must stop the move — the check
    is inside the synchronous step, not before it."""
    key = setup_pair(world)
    original = vault_service.confirmed_publication
    calls = []

    async def confirmed(uid, publish):
        calls.append(1)

        def wrapped(c):
            if len(calls) == 2:
                write(world, "Projects/Money.md", MONEY)
            return publish(c)

        return await original(uid, wrapped)

    monkeypatch.setattr(tools, "confirmed_publication", confirmed)
    out = run(tools.move_task_impl("Inbox.md", key, "Projects/Money.md"))
    assert payload_of(out)["reason"] == "destination_changed"
    assert read(world, "Inbox.md") == INBOX


def test_an_unreadable_destination_is_not_reported_as_changed(world, monkeypatch):
    """Row 8a; Scenario: An unreadable destination is not reported as changed
    (scenario 47); once readable, the named branch completes the move."""
    key = setup_pair(world)
    original = tools.read_bytes_at
    state = {"published": False}

    def guarded(target, *a, **kw):
        if state["published"] and target.rel == "Projects/Money.md":
            raise PermissionError(errno.EACCES, "Permission denied")
        return original(target, *a, **kw)

    def mark(n, t):
        if t.rel == "Projects/Money.md":
            state["published"] = True

    monkeypatch.setattr(tools, "read_bytes_at", guarded)
    original_write = tools.write_file_at

    def publish(target, content, **kw):
        result = original_write(target, content, **kw)
        mark(0, target)
        return result

    monkeypatch.setattr(tools, "write_file_at", publish)
    out = run(tools.move_task_impl("Inbox.md", key, "Projects/Money.md"))
    p = payload_of(out)
    assert p["reason"] == "destination_unverified" and "destination_id" not in p
    dest = line_of(out, "destination:")
    assert "could not be read" in dest and "unverified" in dest and "EACCES" in dest
    assert not dest.startswith("destination: holds") and "no longer holds" not in dest
    assert read(world, "Inbox.md") == INBOX
    state["published"] = False
    monkeypatch.setattr(tools, "write_file_at", original_write)
    out = run(tools.move_task_impl("Inbox.md", key, "Projects/Money.md", resume=True))
    assert code_of(out) is None, out
    assert read(world, "Projects/Money.md") == MOVED_MONEY and read(world, "Inbox.md") == LEFT_INBOX


# ── rows 9–12: the source's in-call conflict ────────────────────────────────


def source_hook(monkeypatch, world, edit, times=1):
    """Before the first `times` source publications, `edit(path)` changes the
    source as another writer would."""
    seen = []

    def on_call(n, t):
        if t.rel == "Inbox.md" and len(seen) < times:
            seen.append(1)
            edit(world.root / "Inbox.md")

    return write_hook(monkeypatch, on_call)


def test_the_source_removal_conflicts_with_a_hash(world, monkeypatch):
    """Scenario: The source removal conflicts (row 9, scenario 1); Scenario: A
    plain retry after a partial outcome writes nothing; Scenario: Resume
    completes a partial move; Scenario: A partial task move records the
    destination only."""
    key = setup_pair(world)
    source_hook(monkeypatch, world, lambda p: p.write_bytes(p.read_bytes() + b"unrelated\n"))
    out = run(tools.move_task_impl("Inbox.md", key, "Projects/Money.md", expected_hash=h(INBOX)))
    assert code_of(out) == "task_move_incomplete", out
    p = payload_of(out)
    assert p["reason"] == "concurrent_write"
    assert p["source_id"] == key and p["destination_id"] == key
    assert "nothing_written" not in p
    assert line_of(out, "destination:") == (
        f"destination: holds the task at line 3, id {key} (re-checked before the source write)")
    complete = line_of(out, "complete with:")
    assert "read_note" in complete and "resume=true" in complete and "allow_duplicate=true" in complete
    other = INBOX + "unrelated\n"
    assert read(world, "Inbox.md") == other  # the other writer's bytes, unchanged
    assert read(world, "Projects/Money.md") == MOVED_MONEY
    assert order(world) == [("refresh", "Projects/Money.md"), ("event", "Projects/Money.md")]
    monkeypatch.setattr(tools, "write_file_at", vault_service.write_file_at)
    out = run(tools.move_task_impl("Inbox.md", key, "Projects/Money.md", expected_hash=h(other)))
    assert code_of(out) == "duplicate_task"
    assert read(world, "Inbox.md") == other
    out = run(tools.move_task_impl("Inbox.md", key, "Projects/Money.md", resume=True,
                                   expected_hash=h(other)))
    assert code_of(out) is None, out
    assert out.splitlines()[0].startswith("Resumed move: the destination already held this task at line 3")
    assert "index (destination): unchanged" in out
    assert read(world, "Inbox.md") == LEFT_INBOX + "unrelated\n"
    assert read(world, "Projects/Money.md") == MOVED_MONEY
    assert world.usage[-1]["move_outcome"] == "resumed"


def test_a_conflicting_change_to_the_block_is_not_completed_by_resume(world, monkeypatch):
    """Scenario: A conflicting change to the task's block is not completed by
    resume (row 9 branch, scenario 1 second half)."""
    key = setup_pair(world)

    def add_context(p):
        p.write_bytes(p.read_bytes().replace(b"- [ ] Other", b"    - new context\n- [ ] Other"))

    source_hook(monkeypatch, world, add_context)
    out = run(tools.move_task_impl("Inbox.md", key, "Projects/Money.md", expected_hash=h(INBOX)))
    assert payload_of(out)["reason"] == "concurrent_write"
    changed = read(world, "Inbox.md")
    assert "    - new context" in changed
    monkeypatch.setattr(tools, "write_file_at", vault_service.write_file_at)
    out = run(tools.move_task_impl("Inbox.md", key, "Projects/Money.md", resume=True,
                                   expected_hash=h(changed)))
    assert code_of(out) == "resume_unmatched"
    assert read(world, "Inbox.md") == changed
    out = run(tools.move_task_impl("Inbox.md", key, "Projects/Money.md", allow_duplicate=True))
    assert code_of(out) is None, out
    dest = read(world, "Projects/Money.md")
    assert dest.count(TASK) == 2 and "    - new context" in dest
    assert read(world, "Inbox.md") == LEFT_INBOX


def test_an_unrelated_source_edit_without_a_hash_is_retried(world, monkeypatch):
    """Rows 9–12, the retry: the block re-locates identical, the move completes."""
    key = setup_pair(world)
    source_hook(monkeypatch, world, lambda p: p.write_bytes(p.read_bytes() + b"unrelated\n"))
    out = run(tools.move_task_impl("Inbox.md", key, "Projects/Money.md"))
    assert code_of(out) is None, out
    assert read(world, "Inbox.md") == LEFT_INBOX + "unrelated\n"
    assert world.usage[0]["task_write_attempts"] == "1/2"
    assert [k for k, _ in order(world)] == ["refresh", "event", "refresh", "event"]


def test_a_context_line_added_in_the_source_is_kept(world, monkeypatch):
    """Scenario: A context line added in the source is kept (row 11,
    scenario 3)."""
    key = setup_pair(world)

    def add_context(p):
        p.write_bytes(p.read_bytes().replace(b"- [ ] Other", b"    - new context\n- [ ] Other"))

    source_hook(monkeypatch, world, add_context)
    out = run(tools.move_task_impl("Inbox.md", key, "Projects/Money.md"))
    p = payload_of(out)
    assert p["reason"] == "source_changed"
    assert f"id {key}" in line_of(out, "source:")
    assert line_of(out, "complete with:") == (
        f'complete with: move_task(path="Inbox.md", id="{key}", to_path="Projects/Money.md", '
        "allow_duplicate=true)")
    assert "    - new context" in read(world, "Inbox.md")


def test_a_task_completed_in_the_source_is_reported_missing(world, monkeypatch):
    """Scenario: A task completed in the source between the steps is reported
    missing, not present (row 10, scenario 2)."""
    key = setup_pair(world)
    done = TASK.replace("- [ ]", "- [x]") + " ✅ 2026-11-16"
    source_hook(monkeypatch, world, lambda p: p.write_bytes(p.read_bytes().replace(TASK.encode(), done.encode())))
    out = run(tools.move_task_impl("Inbox.md", key, "Projects/Money.md"))
    p = payload_of(out)
    assert p["reason"] == "source_missing"
    src = line_of(out, "source:")
    assert src.startswith("source: a re-read during this call found no task with id")
    assert "holds the task" not in src
    assert done in read(world, "Inbox.md")
    # No call writes a third copy without allow_duplicate.
    monkeypatch.setattr(tools, "write_file_at", vault_service.write_file_at)
    out = run(tools.move_task_impl("Inbox.md", key, "Projects/Money.md"))
    assert code_of(out) == "task_not_found"


def test_an_identical_line_added_in_the_source_is_ambiguous(world, monkeypatch):
    """Row 12, scenario 4: no guess."""
    key = setup_pair(world)
    source_hook(monkeypatch, world, lambda p: p.write_bytes(p.read_bytes() + (TASK + "\n").encode()))
    out = run(tools.move_task_impl("Inbox.md", key, "Projects/Money.md"))
    assert payload_of(out)["reason"] == "source_ambiguous"
    assert read(world, "Inbox.md").count(TASK) == 2


def test_two_concurrent_moves_of_one_task_to_different_notes(world, monkeypatch):
    """Scenario: Two concurrent moves of one task to different notes (scenario
    8), interleaved deterministically: the second call's source write happens
    after the first move completed."""
    key = setup_pair(world)
    write(world, "C.md", "")
    inner = {}

    def on_call(n, t):
        if t.rel == "Inbox.md" and not inner:
            inner["out"] = run_in_thread(tools.move_task_impl("Inbox.md", key, "C.md"))

    write_hook(monkeypatch, on_call)
    out = run(tools.move_task_impl("Inbox.md", key, "Projects/Money.md"))
    assert code_of(inner["out"]) is None, inner["out"]
    p = payload_of(out)
    assert p["reason"] == "source_missing"
    assert "holds the task" not in line_of(out, "source:")
    assert TASK in read(world, "C.md") and TASK in read(world, "Projects/Money.md")
    assert TASK not in read(world, "Inbox.md")


def run_in_thread(coro):
    import concurrent.futures
    import contextvars

    ctx = contextvars.copy_context()
    with concurrent.futures.ThreadPoolExecutor(1) as ex:
        return ex.submit(ctx.run, asyncio.run, coro).result()


# ── rows 13, 14, 14a: the source publication fails ──────────────────────────


@pytest.mark.parametrize("exc,reason", [
    (VaultAssignmentChanged("changed"), "vault_assignment_changed"),
    (VaultAnchorUnavailable("anchor gone"), "vault_anchor_lost_at_publish"),
    (VaultConfirmationUnavailable("db down"), "vault_confirmation_unavailable"),
])
def test_a_reassignment_between_the_steps_is_partial(world, monkeypatch, exc, reason):
    """Scenario: A reassignment between the steps is partial (row 13,
    scenario 6)."""
    key = setup_pair(world)

    def refuse(n):
        if n == 2:
            raise exc

    confirm_hook(monkeypatch, refuse)
    out = run(tools.move_task_impl("Inbox.md", key, "Projects/Money.md"))
    p = payload_of(out)
    assert p["code"] == "task_move_incomplete" and p["reason"] == reason
    assert "nothing_written" not in p and out.disposition == "partial"
    assert "resume=true" in line_of(out, "complete with:")
    assert read(world, "Inbox.md") == INBOX


def test_an_os_error_at_the_source_is_partial_io_failure(world, monkeypatch):
    """Row 14."""
    key = setup_pair(world)

    def fail(n, t):
        if t.rel == "Inbox.md":
            raise OSError(errno.EIO, "I/O error")

    write_hook(monkeypatch, fail)
    out = run(tools.move_task_impl("Inbox.md", key, "Projects/Money.md"))
    p = payload_of(out)
    assert p["reason"] == "io_failure"
    assert line_of(out, "source:").startswith("source: unknown")
    assert len([e for e in world.events if e[0] == "event"]) == 1


def test_permissions_not_applied_at_the_source_is_partial(world, monkeypatch):
    """Scenario: A permission failure at the source write is partial (row 14a,
    scenario 43): no retry, the destination event only; resume completes."""
    key = setup_pair(world)
    calls = []

    def fail(n, t):
        if t.rel == "Inbox.md":
            calls.append(1)
            raise vault_fs.PermissionsNotApplied("write the access ACL", errno.EIO)

    write_hook(monkeypatch, fail)
    out = run(tools.move_task_impl("Inbox.md", key, "Projects/Money.md"))
    p = payload_of(out)
    assert p["reason"] == "permissions_not_applied" and len(calls) == 1
    assert line_of(out, "source:").startswith("source: this call did not change it: its file permissions")
    assert "EIO" in line_of(out, "source:")
    assert line_of(out, "complete with:").startswith(
        f'complete with: move_task(path="Inbox.md", id="{key}", to_path="Projects/Money.md", resume=true)')
    assert read(world, "Inbox.md") == INBOX
    assert order(world) == [("refresh", "Projects/Money.md"), ("event", "Projects/Money.md")]
    monkeypatch.setattr(tools, "write_file_at", vault_service.write_file_at)
    out = run(tools.move_task_impl("Inbox.md", key, "Projects/Money.md", resume=True))
    assert code_of(out) is None, out
    assert read(world, "Inbox.md") == LEFT_INBOX and read(world, "Projects/Money.md") == MOVED_MONEY


def test_a_to_expected_hash_makes_the_completing_call_say_it_is_stale(world, monkeypatch):
    """D10 / spec reasons list: every completing call omits to_expected_hash or
    passes a fresh one, and the line says so when one was supplied."""
    key = setup_pair(world)

    def fail(n, t):
        if t.rel == "Inbox.md":
            raise vault_fs.PermissionsNotApplied("fchmod the staged copy", errno.EIO)

    write_hook(monkeypatch, fail)
    out = run(tools.move_task_impl("Inbox.md", key, "Projects/Money.md", to_expected_hash=h(MONEY)))
    assert "omit to_expected_hash" in line_of(out, "complete with:")


# ── row 16 and row 17 ────────────────────────────────────────────────────────


def test_a_move_killed_between_the_publications_is_completed_by_resume(world):
    """Row 16, scenario 31: both notes hold the task (what a killed move
    leaves); a plain retry writes nothing; resume completes."""
    key = setup_pair(world)
    write(world, "Projects/Money.md", MOVED_MONEY)
    out = run(tools.move_task_impl("Inbox.md", key, "Projects/Money.md"))
    assert code_of(out) == "duplicate_task"
    assert read(world, "Inbox.md") == INBOX
    out = run(tools.move_task_impl("Inbox.md", key, "Projects/Money.md", resume=True))
    assert code_of(out) is None
    assert read(world, "Inbox.md") == LEFT_INBOX and read(world, "Projects/Money.md") == MOVED_MONEY


def test_resume_without_exactly_one_copy_is_refused(world):
    """Scenario: Resume without exactly one copy is refused (scenario 38)."""
    key = setup_pair(world)
    out = run(tools.move_task_impl("Inbox.md", key, "Projects/Money.md", resume=True))
    assert code_of(out) == "resume_unmatched" and "no such copies" in out
    block = TASK + "\n    - ask about the fee\n    - [ ] find the account number\n"
    write(world, "Projects/Money.md", MONEY + block + block)
    out = run(tools.move_task_impl("Inbox.md", key, "Projects/Money.md", resume=True))
    assert code_of(out) == "resume_unmatched" and "2 such copies" in out
    assert read(world, "Inbox.md") == INBOX


def test_the_l17_window_is_reproduced_not_claimed_safe(world, monkeypatch):
    """Row 17, scenario 35 (L17): an older destination saved after the 6(b)
    check and before the source rename loses the task while the move reports
    success. Documented, not claimed safe."""
    key = setup_pair(world)

    def stale_save(n, t):
        if t.rel == "Inbox.md":  # inside the callback, after the presence check
            write(world, "Projects/Money.md", MONEY)

    write_hook(monkeypatch, stale_save)
    out = run(tools.move_task_impl("Inbox.md", key, "Projects/Money.md"))
    assert code_of(out) is None
    assert TASK not in read(world, "Projects/Money.md") and TASK not in read(world, "Inbox.md")
    assert TASK in out  # recoverable from this call's response (L17)


# ── #447: each note keeps its own permissions ───────────────────────────────


def _acl_or_skip(path):
    probe = vault_fs.pack_acl(((vault_fs.ACL_USER_OBJ, 6, vault_fs.ACL_UNDEFINED_ID),
                               (vault_fs.ACL_GROUP_OBJ, 6, vault_fs.ACL_UNDEFINED_ID),
                               (vault_fs.ACL_GROUP, 6, 4242),
                               (vault_fs.ACL_MASK, 6, vault_fs.ACL_UNDEFINED_ID),
                               (vault_fs.ACL_OTHER, 4, vault_fs.ACL_UNDEFINED_ID)))
    try:
        os.setxattr(str(path), vault_fs.ACL_ACCESS_XATTR, probe)
    except OSError as exc:
        if exc.errno not in (errno.EOPNOTSUPP, errno.ENOTSUP):
            raise
        if os.environ.get(REQUIRE_ACL_ENV) == "1":
            pytest.fail(f"no POSIX ACLs on the test filesystem, and {REQUIRE_ACL_ENV}=1")
        return None
    return probe


def test_each_note_keeps_its_own_permissions(world):
    """Scenario: Each note keeps its own permissions (#447 edit rule; the mode
    everywhere, the access ACL where the filesystem has them)."""
    key = setup_pair(world)
    inbox, money = world.root / "Inbox.md", world.root / "Projects/Money.md"
    os.chmod(inbox, 0o600)
    os.chmod(money, 0o664)
    acl = _acl_or_skip(money)
    before = (os.lstat(inbox).st_gid, os.lstat(money).st_gid)
    out = run(tools.move_task_impl("Inbox.md", key, "Projects/Money.md"))
    assert code_of(out) is None, out
    assert stat.S_IMODE(os.lstat(inbox).st_mode) == 0o600
    assert stat.S_IMODE(os.lstat(money).st_mode) == 0o664
    assert (os.lstat(inbox).st_gid, os.lstat(money).st_gid) == before
    if acl is not None:
        assert os.getxattr(str(money), vault_fs.ACL_ACCESS_XATTR) == acl
    assert read(world, "Projects/Money.md") == MOVED_MONEY


def _after_the_nth_staged_flush(monkeypatch, n, action):
    """Run `action` right after the n-th regular-file `fsync`: past that
    publication's P1, before its P2 (the #447 test hook)."""
    real = os.fsync
    seen = []

    def hooked(fd):
        result = real(fd)
        if stat.S_ISREG(os.fstat(fd).st_mode):
            seen.append(1)
            if len(seen) == n:
                action()
        return result

    monkeypatch.setattr(os, "fsync", hooked)
    return seen


def test_a_permission_change_during_the_move_is_a_conflict_not_a_loss(world, monkeypatch):
    """Scenario: A permission change during the move is a conflict, not a loss
    (scenario 44, no hash): the source write publishes nothing, is retried,
    and the move completes with the new mode kept."""
    key = setup_pair(world)
    inbox = world.root / "Inbox.md"
    os.chmod(inbox, 0o644)
    # fsync 1: the destination's staged copy; fsync 2: the source's first try.
    _after_the_nth_staged_flush(monkeypatch, 2, lambda: os.chmod(inbox, 0o600))
    out = run(tools.move_task_impl("Inbox.md", key, "Projects/Money.md"))
    assert code_of(out) is None, out
    assert world.usage[0]["task_write_attempts"] == "1/2"
    assert read(world, "Inbox.md") == LEFT_INBOX and read(world, "Projects/Money.md") == MOVED_MONEY
    assert stat.S_IMODE(os.lstat(inbox).st_mode) == 0o600


def test_a_permission_change_with_a_hash_is_partial_and_resume_completes(world, monkeypatch):
    """Scenario 44 with `expected_hash`: `task_move_incomplete`
    (`concurrent_write`); resume with the same hash completes."""
    key = setup_pair(world)
    inbox = world.root / "Inbox.md"
    os.chmod(inbox, 0o644)
    _after_the_nth_staged_flush(monkeypatch, 2, lambda: os.chmod(inbox, 0o600))
    out = run(tools.move_task_impl("Inbox.md", key, "Projects/Money.md", expected_hash=h(INBOX)))
    assert payload_of(out)["reason"] == "concurrent_write"
    # The hook fired on the second flush only; the resume below runs unhooked.
    out = run(tools.move_task_impl("Inbox.md", key, "Projects/Money.md", resume=True,
                                   expected_hash=h(INBOX)))
    assert code_of(out) is None, out
    assert stat.S_IMODE(os.lstat(inbox).st_mode) == 0o600
    assert read(world, "Inbox.md") == LEFT_INBOX


def test_a_permission_change_at_the_destination_is_retried(world, monkeypatch):
    """A P1/P2 difference at the destination write is row 4: retried."""
    key = setup_pair(world)
    money = world.root / "Projects/Money.md"
    os.chmod(money, 0o664)
    _after_the_nth_staged_flush(monkeypatch, 1, lambda: os.chmod(money, 0o640))
    out = run(tools.move_task_impl("Inbox.md", key, "Projects/Money.md"))
    assert code_of(out) is None, out
    assert world.usage[0]["task_write_attempts"] == "2/1"
    assert stat.S_IMODE(os.lstat(money).st_mode) == 0o640


# ── D12, D13: the same file ─────────────────────────────────────────────────


def test_two_paths_to_one_file_are_refused(world):
    """Scenario: Two paths to one file are refused (scenario 19)."""
    write(world, "A.md", "- [ ] t\n")
    os.link(world.root / "A.md", world.root / "B.md")
    out = run(tools.move_task_impl("A.md", key_of("- [ ] t"), "B.md"))
    assert code_of(out) == "invalid_argument" and "same file" in out
    assert read(world, "A.md") == "- [ ] t\n" and world.events == []


TWO_SECTIONS = "# Today\n- [ ] a\n    - ctx\n- [ ] b\n# Later\n- [ ] c\n"


def test_moving_a_task_to_another_section_of_its_note(world):
    """Scenario: Moving a task to another section of its note (scenario 20,
    below)."""
    write(world, "Inbox.md", TWO_SECTIONS)
    calls = []
    original = tools.write_file_at

    def publish(target, content, **kw):
        calls.append(target.rel)
        return original(target, content, **kw)

    tools_mp = pytest.MonkeyPatch()
    tools_mp.setattr(tools, "write_file_at", publish)
    try:
        out = run(tools.move_task_impl("Inbox.md", key_of("- [ ] a"), "Inbox.md", to_section="Later"))
    finally:
        tools_mp.undo()
    assert code_of(out) is None, out
    assert read(world, "Inbox.md") == "# Today\n- [ ] b\n# Later\n- [ ] c\n- [ ] a\n    - ctx\n"
    assert calls == ["Inbox.md"]
    assert out.splitlines()[0] == "Moved task within `Inbox.md`"
    assert "line 5: - [ ] a" in out and out.rstrip().endswith("index: refreshed")
    assert order(world) == [("refresh", "Inbox.md"), ("event", "Inbox.md")]
    assert world.usage[0]["move_outcome"] == "moved_within"


def test_moving_a_task_to_a_section_above(world):
    """Scenario 20, above."""
    write(world, "Inbox.md", TWO_SECTIONS)
    out = run(tools.move_task_impl("Inbox.md", key_of("- [ ] c"), "Inbox.md", to_section="Today"))
    assert code_of(out) is None, out
    assert read(world, "Inbox.md") == "# Today\n- [ ] a\n    - ctx\n- [ ] b\n- [ ] c\n# Later\n"


def test_a_task_already_in_place(world):
    """Scenario: A task already in place (scenario 20, no change)."""
    write(world, "Inbox.md", TWO_SECTIONS)
    out = run(tools.move_task_impl("Inbox.md", key_of("- [ ] c"), "Inbox.md", to_section="Later"))
    assert out.startswith("No changes for `Inbox.md`")
    assert read(world, "Inbox.md") == TWO_SECTIONS and world.events == []
    assert world.usage[0]["move_outcome"] == "no_change"


def test_a_heading_in_the_block_refuses_a_same_note_move(world):
    """Scenario 21: `to_section`'s heading inside the moved block."""
    note = "- [ ] t\n  # Inner\n# Later\n"
    write(world, "A.md", note)
    out = run(tools.move_task_impl("A.md", key_of("- [ ] t"), "A.md", to_section="Later"))
    assert code_of(out) == "task_malformed" and read(world, "A.md") == note


def test_same_note_hashes_and_resume(world):
    """D12 same-note rules: `to_expected_hash` absent or equal; no resume."""
    write(world, "A.md", TWO_SECTIONS)
    k = key_of("- [ ] a")
    out = run(tools.move_task_impl("A.md", k, "A.md", resume=True))
    assert code_of(out) == "invalid_argument"
    out = run(tools.move_task_impl("A.md", k, "A.md", to_section="Later", to_expected_hash=h(TWO_SECTIONS)))
    assert code_of(out) == "invalid_argument"
    out = run(tools.move_task_impl("A.md", k, "A.md", to_section="Later", expected_hash=h(TWO_SECTIONS),
                                   to_expected_hash=h(TWO_SECTIONS)))
    assert code_of(out) is None, out


# ── D1, D18: ids ────────────────────────────────────────────────────────────


def test_a_remaining_sibling_loses_its_suffix(world):
    """Scenario: A remaining sibling loses its suffix (scenario 17)."""
    note = "- [ ] Call\n    - first\n- [ ] Call\n"
    write(world, "Inbox.md", note)
    write(world, "B.md", "")
    k = key_of("- [ ] Call")
    out = run(tools.move_task_impl("Inbox.md", f"{k}.1of2", "B.md", expected_hash=h(note)))
    assert code_of(out) is None, out
    assert read(world, "Inbox.md") == "- [ ] Call\n"
    assert read(world, "B.md") == "- [ ] Call\n    - first"
    assert "ids renumbered: tasks sharing a moved key now carry new .<i>of<n> suffixes in `Inbox.md`" in out
    assert f"id: {k}" in out


def test_a_path_with_a_backtick_or_newline_is_escaped(world):
    """Scenario 27: every printed path escaped; no forged line."""
    write(world, "we`ird.md", "- [ ] t\n")
    write(world, "B.md", "")
    out = run(tools.move_task_impl("we`ird.md", key_of("- [ ] t"), "B.md"))
    assert code_of(out) is None, out
    assert "we\\u0060ird.md" in out and "we`ird" not in out.replace("we\\u0060ird", "")


# ── D16: the task rule and the default scope ────────────────────────────────


@pytest.mark.parametrize("dry_run", [False, True])
def test_a_checkbox_that_is_not_a_task_under_the_rule_is_moved_and_said_so(world, dry_run):
    """Scenario: A checkbox that is not a task under the rule is moved and
    said so (scenario 48)."""
    world.settings = settings_with(task_rule="fields")
    write(world, "Inbox.md", "- [ ] buy stamps\n")
    write(world, "Errands.md", "")
    out = run(tools.move_task_impl("Inbox.md", key_of("- [ ] buy stamps"), "Errands.md", dry_run=dry_run))
    assert code_of(out) is None, out
    assert task_rule.not_task_note(world.settings) in out
    assert (read(world, "Errands.md") == "- [ ] buy stamps") is (not dry_run)


def test_no_rule_note_under_all(world):
    write(world, "Inbox.md", "- [ ] buy stamps\n")
    write(world, "Errands.md", "")
    out = run(tools.move_task_impl("Inbox.md", key_of("- [ ] buy stamps"), "Errands.md"))
    assert "not a task under your task rule" not in out


def test_the_filter_rule_note_on_a_move(world):
    world.settings = settings_with(task_rule="filter", global_filter="#task")
    write(world, "Inbox.md", "- [ ] buy stamps\n")
    write(world, "Errands.md", "")
    out = run(tools.move_task_impl("Inbox.md", key_of("- [ ] buy stamps"), "Errands.md"))
    assert task_rule.not_task_note(world.settings) in out
    assert read(world, "Errands.md") == "- [ ] buy stamps"  # no filter text added


def test_a_move_into_an_excluded_folder_says_so(world):
    """Scenario 32: the scope note; the write is unchanged."""
    world.settings = settings_with(excluded_folders=("Archive/",))
    write(world, "Inbox.md", "- [ ] t\n")
    write(world, "Archive/Old.md", "")
    out = run(tools.move_task_impl("Inbox.md", key_of("- [ ] t"), "Archive/Old.md"))
    assert code_of(out) is None
    assert "folder your default task scope excludes" in out
    assert read(world, "Archive/Old.md") == "- [ ] t"


# ── terminators through the real publication ───────────────────────────────


def test_end_of_file_without_a_newline_at_both_ends(world):
    """Scenario: End of file without a newline, at both ends (scenario 15)."""
    write(world, "S.md", "Intro\n- [ ] x")
    write(world, "D.md", "Notes")
    out = run(tools.move_task_impl("S.md", key_of("- [ ] x"), "D.md"))
    assert code_of(out) is None, out
    assert read(world, "S.md") == "Intro" and read(world, "D.md") == "Notes\n- [ ] x"


def test_terminators_follow_the_destination(world):
    """Scenario: Terminators follow the destination (scenario 16)."""
    write(world, "S.md", "Intro\r\n- [ ] x\r\n    - ctx\r\nEnd\r\n")
    write(world, "D.md", "Notes\n")
    out = run(tools.move_task_impl("S.md", key_of("- [ ] x"), "D.md"))
    assert code_of(out) is None, out
    assert read(world, "S.md") == "Intro\r\nEnd\r\n" and read(world, "D.md") == "Notes\n- [ ] x\n    - ctx\n"


# ── rate limits (Q7): one write token per move ──────────────────────────────


@pytest.fixture
def write_burst(monkeypatch):
    from src.auth.session import current_principal
    from src.services import rate_limits

    def configure(burst: int):
        monkeypatch.setattr(tools.settings, "mcp_write_rate_limit_per_minute", 1)
        monkeypatch.setattr(tools.settings, "mcp_write_rate_limit_burst", burst)
        rate_limits.reset_state_for_tests()

    token = current_principal.set(("api_key", 4301))
    yield configure
    current_principal.reset(token)
    rate_limits.reset_state_for_tests()


def test_one_write_token_per_move(world, write_burst):
    """Scenario: One write token per move (a burst of 2 admits two two-note
    moves); mcp-rate-limits Scenario: Write tools pass both buckets (the
    third is refused under the write scope while a read tool still runs)."""
    from src.services import refusals

    write_burst(2)
    write(world, "A.md", "- [ ] one\n- [ ] two\n- [ ] three\n")
    write(world, "B.md", "")
    for line in ("- [ ] one", "- [ ] two"):
        out = run(tools.move_task_impl("A.md", key_of(line), "B.md"))
        assert code_of(out) is None, out
    out = run(tools.move_task_impl("A.md", key_of("- [ ] three"), "B.md"))
    assert code_of(out) == "rate_limited"
    assert payload_of(out)["scope"] == refusals.SCOPE_PRINCIPAL_WRITE
    assert read(world, "A.md") == "- [ ] three\n"
    note = run(tools.read_note_impl("B.md"))  # a read tool is still admitted
    assert "- [ ] two" in note.content


def test_a_two_note_move_takes_exactly_one_write_token(world, write_burst):
    """mcp-rate-limits Scenario: A two-note move takes one write token."""
    from src.auth.session import current_principal
    from src.services import rate_limits, refusals

    write_burst(1)
    write(world, "A.md", "- [ ] one\n")
    write(world, "B.md", "")
    out = run(tools.move_task_impl("A.md", key_of("- [ ] one"), "B.md"))
    assert code_of(out) is None, out
    assert read(world, "A.md") == "" and read(world, "B.md") == "- [ ] one"
    admitted, _retry = rate_limits.take(current_principal.get(), refusals.SCOPE_PRINCIPAL_WRITE)
    assert not admitted, "the bucket should be empty after one move"


# ── the published surface (D17, D18) ────────────────────────────────────────


def test_move_task_is_registered_last_destructive_with_its_contract_first():
    from src.mcp_server import server

    listed = {t.name: t for t in server.mcp._tool_manager.list_tools()}
    names = list(listed)
    assert names[-2:] == ["add_task", "move_task"]
    tool = listed["move_task"]
    assert tool.title == "Move task" and tool.annotations == server._DESTRUCTIVE
    text = " ".join(tool.description.split())
    assert len(tool.description) <= 4000
    for needle in ("readwrite", "`to_expected_hash`", "top-level, unquoted",
                   "destination is written first", "`task_move_incomplete`", "`complete with:`",
                   "`resume=true`", "One window remains", "`dry_run=true`", "`resume_unmatched`",
                   "`block_id_conflict`"):
        assert needle in text, needle


def test_the_instructions_name_move_task_within_their_caps():
    """vault-guide Scenario: The instructions name the task tools within their
    caps (D17: the lead byte-identical, the total ≤ 2,000)."""
    from src.mcp_server import server

    text = server.SERVER_INSTRUCTIONS
    assert len(text) <= 2000
    assert "`move_task` moves one, with the lines indented under it, to another note or section." in text
    assert text.index("`move_task`") > 512


def test_the_primer_names_move_task_neutrally(world):
    """vault-guide Scenario: The primer names the task tools."""
    result = run(tools.get_vault_guide_impl())
    tasks = result.split("## Tasks", 1)[1].split("\n## ", 1)[0]
    flat = " ".join(tasks.split())
    for name in ("list_tasks", "task_agenda", "complete_task", "update_task", "add_task", "move_task"):
        assert f"`{name}`" in tasks, name
    assert "`move_task` moves one task, with the lines indented under it, to another note or section" in flat
    for word in ("MUST", "IMPORTANT", "always use", "primary"):
        assert word not in tasks


def test_the_refusal_shape_checks():
    """D11: the four move fields only on `task_move_incomplete`, which needs a
    declared reason and never carries `nothing_written`; absent-not-null."""
    from src.services import refusals

    with pytest.raises(refusals.RefusalShapeError):
        refusals.Refusal("io_failure", reason="io_failure")
    with pytest.raises(refusals.RefusalShapeError):
        refusals.Refusal("task_move_incomplete")
    with pytest.raises(refusals.RefusalShapeError):
        refusals.Refusal("task_move_incomplete", reason="made_up")
    with pytest.raises(refusals.RefusalShapeError):
        refusals.Refusal("task_move_incomplete", reason="io_failure", nothing_written=True)
    r = refusals.Refusal("task_move_incomplete", path="A.md", destination_path="B.md",
                         source_id="0123456789ab", reason="destination_changed")
    assert refusals.sentinel_line(r) == (
        'MCP-REFUSAL {"code":"task_move_incomplete","path":"A.md","destination_path":"B.md",'
        '"source_id":"0123456789ab","reason":"destination_changed"}')
    assert refusals.Refusal("not_found").as_payload() == {"code": "not_found"}


def test_two_concurrent_moves_of_one_task_to_the_same_note(world, monkeypatch):
    """Scenario 9: the second move, landing between the first's destination
    publication and its source write, is refused `duplicate_task`; the
    destination never holds three copies (two never either: the first
    completes)."""
    key = setup_pair(world)
    inner = {}

    def on_call(n, t):
        if t.rel == "Inbox.md" and not inner:
            inner["out"] = run_in_thread(tools.move_task_impl("Inbox.md", key, "Projects/Money.md"))

    write_hook(monkeypatch, on_call)
    out = run(tools.move_task_impl("Inbox.md", key, "Projects/Money.md"))
    assert code_of(inner["out"]) == "duplicate_task", inner["out"]
    assert code_of(out) is None, out
    assert read(world, "Projects/Money.md") == MOVED_MONEY and read(world, "Inbox.md") == LEFT_INBOX


def test_a_destination_over_the_note_size_cap_is_refused_before_publishing(world, monkeypatch):
    """Scenario 25: a result over `MAX_NOTE_BYTES` is `size_limit` before the
    destination publication; nothing written in either note."""
    key = setup_pair(world)
    monkeypatch.setattr(tools, "MAX_NOTE_BYTES", len(MONEY.encode()) + 10)
    out = run(tools.move_task_impl("Inbox.md", key, "Projects/Money.md"))
    assert code_of(out) == "size_limit"
    assert read(world, "Inbox.md") == INBOX and read(world, "Projects/Money.md") == MONEY
    assert world.events == []


# ── implementation review round 1 ───────────────────────────────────────────


def test_a_destination_that_is_not_utf8_is_unverified_not_changed(world, monkeypatch):
    """Review r1 MAJOR: a destination that reads but fails UTF-8 decoding,
    with the moved lines still in place, is `destination_unverified` — the
    response never claims the lines are gone."""
    key = setup_pair(world)

    def corrupt(n):
        if n == 2:
            p = world.root / "Projects/Money.md"
            p.write_bytes(p.read_bytes() + b"\xff\xfe stray\n")

    confirm_hook(monkeypatch, corrupt)
    out = run(tools.move_task_impl("Inbox.md", key, "Projects/Money.md"))
    p = payload_of(out)
    assert p["reason"] == "destination_unverified" and "destination_id" not in p
    dest = line_of(out, "destination:")
    assert "UTF-8" in dest and "no longer holds" not in dest
    assert read(world, "Inbox.md") == INBOX


def test_a_completing_call_never_repeats_a_stale_source_hash(world, monkeypatch):
    """Review r1 MAJOR: with a bare id the `complete with:` calls omit the
    original `expected_hash`; after a concurrent source edit the named call,
    made exactly as printed, completes the move."""
    key = setup_pair(world)

    def both(n):
        if n == 2:  # an unrelated source edit and an older destination save
            src = world.root / "Inbox.md"
            src.write_bytes(src.read_bytes() + b"unrelated\n")
            write(world, "Projects/Money.md", MONEY)

    confirm_hook(monkeypatch, both)
    out = run(tools.move_task_impl("Inbox.md", key, "Projects/Money.md", expected_hash=h(INBOX)))
    assert payload_of(out)["reason"] == "destination_changed"
    complete = line_of(out, "complete with:")
    assert "expected_hash" not in complete and h(INBOX) not in out
    monkeypatch.setattr(tools, "confirmed_publication", vault_service.confirmed_publication)
    out = run(tools.move_task_impl("Inbox.md", key, "Projects/Money.md"))  # as printed
    assert code_of(out) is None, out
    assert read(world, "Inbox.md") == LEFT_INBOX + "unrelated\n"


@pytest.mark.parametrize("fail_at", ["destination_changed", "permissions_not_applied", "reassigned"])
def test_a_suffixed_id_completing_call_reads_the_source_first(world, monkeypatch, fail_at):
    """Review r1 MAJOR, every row that names a call with the task id: a
    suffixed id is told to read the source and pass its fresh
    `content_hash`, never the original call's hash."""
    note = "- [ ] Call\n    - first\n- [ ] Call\n"
    write(world, "Inbox.md", note)
    write(world, "B.md", "")
    k = key_of("- [ ] Call")
    if fail_at == "destination_changed":
        confirm_hook(monkeypatch, lambda n: write(world, "B.md", "") if n == 2 else None)
    elif fail_at == "reassigned":
        def refuse(n):
            if n == 2:
                raise VaultAssignmentChanged("changed")
        confirm_hook(monkeypatch, refuse)
    else:
        def fail(n, t):
            if t.rel == "Inbox.md":
                raise vault_fs.PermissionsNotApplied("fchmod the staged copy", errno.EIO)
        write_hook(monkeypatch, fail)
    out = run(tools.move_task_impl("Inbox.md", f"{k}.1of2", "B.md", expected_hash=h(note)))
    assert code_of(out) == "task_move_incomplete", out
    complete = line_of(out, "complete with:")
    assert 'expected_hash=<the content_hash that read_note(path="Inbox.md") returns>' in complete
    assert f'id="{k}.1of2"' in complete
    assert h(note) not in complete


class _Killed(BaseException):
    """Stands in for the process dying: nothing after it runs."""


def test_a_real_move_interrupted_after_the_destination_publish_is_resumable(world, monkeypatch):
    """Review r1 MINOR (row 16, L2): a real move stopped at the step boundary
    right after the destination publication leaves the task in BOTH notes on
    disk (destination first), and the prescribed `resume=true` completes."""
    key = setup_pair(world)
    original = task_index.refresh_note_tasks

    async def die(**kw):
        raise _Killed()

    monkeypatch.setattr(task_index, "refresh_note_tasks", die)
    with pytest.raises(_Killed):
        run(tools.move_task_impl("Inbox.md", key, "Projects/Money.md"))
    assert read(world, "Projects/Money.md") == MOVED_MONEY
    assert read(world, "Inbox.md") == INBOX
    monkeypatch.setattr(task_index, "refresh_note_tasks", original)
    out = run(tools.move_task_impl("Inbox.md", key, "Projects/Money.md"))
    assert code_of(out) == "duplicate_task"
    out = run(tools.move_task_impl("Inbox.md", key, "Projects/Money.md", resume=True))
    assert code_of(out) is None, out
    assert read(world, "Inbox.md") == LEFT_INBOX and read(world, "Projects/Money.md") == MOVED_MONEY


# ── implementation review round 2 ───────────────────────────────────────────


def test_an_unclosed_fence_above_the_moved_lines_stops_the_move(world, monkeypatch):
    """Review r2/r3: another writer turns the line above the moved lines into
    an unclosed fence; the moved bytes are still at their position but no
    longer parse as a task, so the presence check stops with
    `destination_not_a_task` (not `destination_changed`), the source is kept,
    and the printed resume works once the fence is closed."""
    write(world, "Inbox.md", "- [ ] x\n")
    write(world, "D.md", "Notes\n")

    def fence(n):
        if n == 2:
            write(world, "D.md", "```\n- [ ] x\n")
            # A concurrent source edit too: the response must not claim the
            # source still holds the task (review r4).
            write(world, "Inbox.md", "- [ ] x\nunrelated\n")

    confirm_hook(monkeypatch, fence)
    out = run(tools.move_task_impl("Inbox.md", key_of("- [ ] x"), "D.md"))
    p = payload_of(out)
    assert p["reason"] == "destination_not_a_task", out
    assert "destination_id" not in p
    dest = line_of(out, "destination:")
    assert "held the moved lines at line 2" in dest and "no longer read as a task" in dest
    assert line_of(out, "source:") == "source: this call did not change it"
    assert "still holds the task" not in out.split("complete with:")[0]
    complete = line_of(out, "complete with:")
    assert "close the fence" in complete and "edit_note" in complete
    assert 'read_note(path="Inbox.md") confirms the source still holds the task' in complete
    resume_call = f'move_task(path="Inbox.md", id="{key_of("- [ ] x")}", to_path="D.md", resume=true)'
    assert resume_call in complete
    assert read(world, "Inbox.md") == "- [ ] x\nunrelated\n"  # the other writer's bytes
    # Repair the destination (close the fence), then the printed resume completes.
    monkeypatch.setattr(tools, "confirmed_publication", vault_service.confirmed_publication)
    write(world, "D.md", "```\n```\n- [ ] x\n")
    out = run(tools.move_task_impl("Inbox.md", key_of("- [ ] x"), "D.md", resume=True))
    assert code_of(out) is None, out
    assert read(world, "Inbox.md") == "unrelated\n" and read(world, "D.md") == "```\n```\n- [ ] x\n"


def test_every_completing_call_is_rendered_by_one_helper():
    """Review r2, at the root: `_move_call` is reached only through
    `_completing_call`, which decides the hash from the id it names."""
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(tools))
    callers = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for sub in ast.walk(node):
                if isinstance(sub, ast.Call) and getattr(sub.func, "id", None) == "_move_call":
                    callers.add(node.name)
    assert callers == {"_completing_call"}
    fresh = '<the content_hash that read_note(path="S.md") returns>'
    bare = tools._completing_call("S.md", "0123456789ab", "D.md", resume=True)
    assert "expected_hash" not in bare
    assert f"expected_hash={fresh}" in tools._completing_call("S.md", "0123456789ab.2of2", "D.md")
    assert f"expected_hash={fresh}" in tools._completing_call("S.md", "<key>.<i>of<n>", "D.md")
    listed = tools._completing_call("S.md", "<its new id>", "D.md", allow_duplicate=True)
    assert listed.endswith(f"(adding expected_hash={fresh} if that id carries a .<i>of<n> suffix)")


def test_a_source_missing_call_with_a_newly_suffixed_id_asks_for_a_fresh_hash(world, monkeypatch):
    """Review r2 MAJOR (the `source_missing` and `concurrent_write` families):
    the source task A is edited into a second B; the completing call names a
    listed id, so it says to add the source's fresh `content_hash` when that
    id carries a suffix."""
    write(world, "Inbox.md", "- [ ] A\n- [ ] B\n")
    write(world, "D.md", "")
    source_hook(monkeypatch, world, lambda p: p.write_bytes(b"- [ ] B\n- [ ] B\n"))
    out = run(tools.move_task_impl("Inbox.md", key_of("- [ ] A"), "D.md"))
    assert payload_of(out)["reason"] == "source_missing"
    complete = line_of(out, "complete with:")
    assert ('(adding expected_hash=<the content_hash that read_note(path="Inbox.md") returns> '
            "if that id carries a .<i>of<n> suffix)") in complete


def test_the_ambiguous_and_conflict_families_name_fresh_hashes(world, monkeypatch):
    """Review r2 (the `source_ambiguous` family): the suffixed placeholder
    always carries the fresh-hash instruction; the `concurrent_write`
    family's listed-id calls carry the conditional one."""
    key = setup_pair(world)
    source_hook(monkeypatch, world, lambda p: p.write_bytes(p.read_bytes() + (TASK + "\n").encode()))
    out = run(tools.move_task_impl("Inbox.md", key, "Projects/Money.md"))
    assert payload_of(out)["reason"] == "source_ambiguous"
    assert ('id=<key>.<i>of<n>, to_path="Projects/Money.md", resume=true, expected_hash=<the '
            'content_hash that read_note(path="Inbox.md") returns>') in line_of(out, "complete with:")
    text = tools._move_incomplete(
        reason="concurrent_write", src_rel="S.md", dst_rel="D.md", task_id="0123456789ab",
        dest_line=1, dest_id="0123456789ab", expected_hash="sha256:" + "0" * 64,
        to_hash_supplied=False, dest_published=True, dest_hash=None, dest_refresh=None)
    complete = line_of(text, "complete with:")
    assert "sha256:" not in complete
    assert complete.count("if that id carries a .<i>of<n> suffix") == 2
