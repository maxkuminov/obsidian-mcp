"""Every vault mutation confirms the assignment before each publication (#88).

`APIKeyMiddleware` binds `current_vault_root` once, at admission, and that
snapshot is deliberately immutable — it is what makes #66's admission gate fail
closed under a concurrent bulk cache warm. The price is that it is stale by
design for a whole request: an administrator commits a reassignment, the panel
reports it complete, and a write already in flight still publishes into the
*former* vault. The bound was one request's lifetime, which for a write tool is
the whole tool body — a read, a diff, a section resolve, a payload up to the
note size cap.

The fix is a fresh `SELECT users.vault_path, users.is_active` immediately
before **each** publishing operation, compared against the root the request was
admitted for, stamped onto the `MutableTarget` that operation publishes
through, and *consumed* by it. Two properties follow, and both are asserted
here rather than described:

* **structural inheritance** — the publish helpers (`_atomic_write_at`,
  `move_file_no_clobber`, `soft_delete_target` and the permanent-unlink helper
  `unlink_at`) refuse a target carrying no confirmation, so a mutating tool
  added later cannot publish without one. The permanent unlink is in that list
  because it was the one bare `os.unlink(..., dir_fd=...)` left on the seam:
  while it stood, "the publish helpers refuse an unconfirmed target" was a
  false description of the whole rather than an incomplete one.
* **one confirmation per publication, never per call** — five tools publish
  once, and `move_note(rewrite_links=True)` publishes once for the move and
  once per link rewrite with a metadata transaction of unbounded duration in
  between. A single confirmation reused across all of that would be the same
  staleness this change exists to narrow, merely relocated inside one call.
* **the confirmation is not something a caller can hold** — the only entry
  point is `vault.confirmed_publication`, which awaits the read and calls a
  *synchronous* publish before returning control, and the confirmation carries
  its own spent flag and is checked against the target's user and root.

The refusal is optimistic and says so: it narrows the window to staging, the
durability flush and one publishing call. A reassignment committing inside that
window still lands in the former root. Nothing here claims otherwise.

The confirming read is faked at `src.database.async_session` — the *function
level* import `_confirm_vault_assignment` performs — while the note tools' own
metadata session (`tools.async_session`, bound at import) is faked separately.
Keeping the two apart is what lets a test count assignment re-reads exactly,
and what lets one flip the assignment *between* two of them to drive the
interleaving a real `COMMIT` would produce.
"""
from __future__ import annotations

import ast
import asyncio
import contextlib
from pathlib import Path

import pytest
from sqlalchemy.exc import OperationalError

import src.database
import src.mcp_server.server as server
import src.mcp_server.tools as tools
import src.services.vault as vault_service
from src.auth.session import current_user_id, current_vault_root
from src.mcp_server.auth import current_permission
from src.mcp_server.server import mcp
from src.services import vault_fs
from src.services.vault import UnconfirmedPublication, VaultAssignmentChanged


UID = 8801


# ── the two fake sessions ───────────────────────────────────────────────────


class _UserRow:
    """What `SELECT users.vault_path, users.is_active` hands back."""

    def __init__(self, vault_path: str | None, is_active: bool = True) -> None:
        self.vault_path = vault_path
        self.is_active = is_active


class _Result:
    def __init__(self, rows: list) -> None:
        self._rows = rows

    def first(self):
        return self._rows[0] if self._rows else None

    def all(self):
        return list(self._rows)

    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else None


class _AssignmentSession:
    """Answers the one confirming SELECT, and records that it happened.

    `flip_after` is the interleaving lever: after that many confirming reads
    the assignment changes, exactly as an administrator's `COMMIT` landing
    part way through a multi-publication tool would. It is the only way to
    drive `move_note`'s "refused between the move and the rewrites" case
    deterministically — a real reassignment has to land inside an `await` that
    the test cannot otherwise get between.
    """

    def __init__(self, state: dict) -> None:
        self.state = state

    async def __aenter__(self) -> "_AssignmentSession":
        return self

    async def __aexit__(self, *exc_info) -> bool:
        return False

    async def execute(self, statement):
        self.state["reads"] += 1
        self.state["statements"].append(str(statement))
        # The confirmation *outage* lever: the read fails outright rather than
        # returning a different assignment. Nobody reassigned anything; the
        # database is simply unreachable.
        fail = self.state.get("fail_after")
        if fail is not None and self.state["reads"] >= fail:
            raise OperationalError("SELECT users.vault_path", {}, Exception("boom"))
        row = self.state["row"]
        flip = self.state.get("flip_after")
        if flip is not None and self.state["reads"] >= flip:
            self.state["row"] = self.state["flip_to"]
            self.state["flip_after"] = None
        return _Result([] if row is None else [row])


class _MetadataSession:
    """The note tools' own `notes_metadata` / `note_links` session.

    Scripted by call order: the vault index, then the backlink sources, then
    empty. `move_note`'s phase-3 update opens a *fresh* session, so its two
    statements start the script again and land on the (harmless) index shape.
    """

    def __init__(self, state: dict) -> None:
        self.state = state
        self.calls = 0

    async def __aenter__(self) -> "_MetadataSession":
        return self

    async def __aexit__(self, *exc_info) -> bool:
        return False

    async def execute(self, statement):
        self.calls += 1
        self.state["statements"] += 1
        if self.calls == 1:
            return _Result(list(self.state["index_rows"]))
        if self.calls == 2:
            return _Result(list(self.state["backlink_rows"]))
        return _Result([])

    async def commit(self):
        self.state["commits"] += 1

    async def close(self):
        return None


class _IndexRow:
    def __init__(self, file_path: str, row_id: int) -> None:
        self.file_path = file_path
        self.id = row_id


# ── fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def multi_user_vault(monkeypatch, tmp_path):
    """A multi-user request admitted for `<tmp>/current`, with both DBs faked.

    Yields a namespace carrying the vault root, the root an administrator
    reassigns *to*, the assignment state the confirming read answers from, and
    the captured usage-log params.
    """
    current = tmp_path / "current"
    other = tmp_path / "elsewhere"
    current.mkdir()
    other.mkdir()

    monkeypatch.setattr(vault_service.settings, "multi_user_mode", True)
    monkeypatch.setattr(tools.settings, "multi_user_mode", True, raising=False)

    assignment = {
        "row": _UserRow(str(current), True),
        "reads": 0,
        "statements": [],
        "flip_after": None,
        "flip_to": None,
        "fail_after": None,
    }
    metadata = {
        "index_rows": [],
        "backlink_rows": [],
        "statements": 0,
        "commits": 0,
    }
    monkeypatch.setattr(
        src.database, "async_session", lambda: _AssignmentSession(assignment)
    )
    monkeypatch.setattr(
        tools, "async_session", lambda: _MetadataSession(metadata)
    )

    logged: dict = {}

    async def capture(tool, params, duration_ms, response_size):
        logged["tool"] = tool
        logged["params"] = params

    monkeypatch.setattr(tools, "_log_usage", capture)

    vault_service._user_vault_cache[UID] = current
    perm = current_permission.set("readwrite")
    uid_token = current_user_id.set(UID)
    root_token = current_vault_root.set((UID, current))
    vault_fs.reset_filesystem_probe_cache()

    class _Ctx:
        pass

    ctx = _Ctx()
    ctx.vault = current
    ctx.other = other
    ctx.assignment = assignment
    ctx.metadata = metadata
    ctx.logged = logged

    try:
        yield ctx
    finally:
        current_vault_root.reset(root_token)
        current_user_id.reset(uid_token)
        current_permission.reset(perm)
        vault_service._user_vault_cache.pop(UID, None)
        vault_fs.reset_filesystem_probe_cache()


def reassign(ctx, condition: str) -> None:
    """Apply one of the four conditions that revoke the bound assignment."""
    if condition == "reassigned":
        ctx.assignment["row"] = _UserRow(str(ctx.other), True)
    elif condition == "unassigned":
        ctx.assignment["row"] = _UserRow(None, True)
    elif condition == "deactivated":
        ctx.assignment["row"] = _UserRow(str(ctx.vault), False)
    elif condition == "deleted":
        ctx.assignment["row"] = None
    else:  # pragma: no cover - guard against a typo in a parametrisation
        raise AssertionError(condition)


CONDITIONS = ["reassigned", "unassigned", "deactivated", "deleted"]


@contextlib.contextmanager
def a_confirmation(ctx, *, user_id=UID, root=None):
    """A **leased** confirmation of the bound root, for the tests that drive a
    publish helper directly.

    `_single_shot_confirmation` deliberately refuses under `multi_user_mode`,
    which is the mode these fixtures run in — a synchronous helper cannot read
    a user's assignment, and quietly publishing unconfirmed is the hole the
    confirmation exists to close. So the direct-helper tests build the object
    the async wrapper would have produced, and lease it the way
    `confirmed_publication` does: a confirmation nobody leased authorises
    nothing, which is the round-2 fix.
    """
    confirmation = vault_service.RootConfirmation(
        user_id, str(ctx.vault) if root is None else root, queried=True
    )
    with vault_service._leased(confirmation):
        yield confirmation


def seed(ctx) -> None:
    """The note and the file every refusal case must leave byte-identical."""
    (ctx.vault / "note.md").write_text("original body\n", encoding="utf-8")
    (ctx.vault / "picture.png").write_bytes(b"\x89PNG\r\n\x1a\npixels")


def untouched(ctx) -> None:
    """The former root is intact and nothing was created in the new one."""
    assert (ctx.vault / "note.md").read_text(encoding="utf-8") == "original body\n"
    assert (ctx.vault / "picture.png").read_bytes() == b"\x89PNG\r\n\x1a\npixels"
    assert not (ctx.vault / ".trash").exists()
    assert sorted(p.name for p in ctx.other.iterdir()) == []


async def _call(ctx, name: str):
    """Invoke one mutating tool, in a shape that would publish if allowed."""
    if name == "create_note":
        return await tools.create_note_impl("fresh.md", "new body\n")
    if name == "edit_note":
        return await tools.edit_note_impl("note.md", "rewritten\n")
    if name == "set_frontmatter":
        return await tools.set_frontmatter_impl("note.md", {"tags": ["x"]})
    if name == "write_file":
        return await tools.write_file_impl(
            "blob.bin", "aGVsbG8=", encoding="base64", overwrite=False
        )
    if name == "move_note":
        return await tools.move_note_impl("note.md", "moved.md")
    if name == "delete_note":
        return await tools.delete_note_impl("note.md")
    if name == "delete_note_permanent":
        return await tools.delete_note_impl("note.md", permanent=True)
    if name == "delete_file":
        return await tools.delete_file_impl("picture.png")
    raise AssertionError(name)  # pragma: no cover


MUTATING_TOOLS = [
    "create_note",
    "edit_note",
    "set_frontmatter",
    "write_file",
    "move_note",
    "delete_note",
    "delete_note_permanent",
    "delete_file",
]


# ── (a) every mutating tool refuses, for every condition ────────────────────


@pytest.mark.parametrize("tool_name", MUTATING_TOOLS)
@pytest.mark.parametrize("condition", CONDITIONS)
async def test_a_changed_assignment_refuses_every_mutating_tool(
    multi_user_vault, tool_name, condition
):
    ctx = multi_user_vault
    seed(ctx)
    reassign(ctx, condition)

    result = await _call(ctx, tool_name)

    assert "Vault assignment changed" in result, (tool_name, condition, result)
    untouched(ctx)
    # Nothing was created in the former root either — a refused create must not
    # leave the note it was about to write.
    assert not (ctx.vault / "fresh.md").exists()
    assert not (ctx.vault / "moved.md").exists()
    assert not (ctx.vault / "blob.bin").exists()
    assert ctx.assignment["reads"] >= 1, "the confirmation issued no query"


async def test_a_refused_create_leaves_no_directory_behind(multi_user_vault):
    """"No target directory SHALL be created" — the confirmation sits ahead of
    the first use of `dir_fd`, which is what creates a missing parent."""
    ctx = multi_user_vault
    reassign(ctx, "reassigned")

    result = await tools.create_note_impl("Brand/New/Folder/fresh.md", "body\n")

    assert "Vault assignment changed" in result
    assert not (ctx.vault / "Brand").exists()
    assert not (ctx.other / "Brand").exists()


async def test_a_refused_write_file_leaves_no_directory_behind(multi_user_vault):
    ctx = multi_user_vault
    reassign(ctx, "unassigned")

    result = await tools.write_file_impl(
        "Brand/New/blob.bin", "aGVsbG8=", encoding="base64"
    )

    assert "Vault assignment changed" in result
    assert not (ctx.vault / "Brand").exists()


async def test_the_refusal_names_the_residual_rather_than_implying_it(
    multi_user_vault,
):
    """C.7: the error text says what the check does and does not buy."""
    ctx = multi_user_vault
    seed(ctx)
    reassign(ctx, "reassigned")

    result = await tools.edit_note_impl("note.md", "rewritten\n")

    assert "immediately before publication" in result
    assert "does not close it" in result
    assert "edit_note(expected=…)" in result


async def test_the_refusal_does_not_name_the_new_root(multi_user_vault):
    """The caller learns that the assignment moved, not where it moved to."""
    ctx = multi_user_vault
    seed(ctx)
    reassign(ctx, "reassigned")

    result = await tools.create_note_impl("fresh.md", "body\n")

    assert str(ctx.other) not in result


# ── (b) the confirmation is a fresh read, not a cache hit ───────────────────


async def test_a_stale_cache_and_a_stale_snapshot_still_refuse(multi_user_vault):
    """The two values being checked are exactly the two that must not answer.

    The snapshot is bound once at admission and is immutable by design; the
    process cache is add-only from the indexer's side. Consulting either would
    compare a value with itself, so the database's disagreement must win even
    while both still hold the previous root.
    """
    ctx = multi_user_vault
    seed(ctx)
    reassign(ctx, "reassigned")

    assert vault_service._user_vault_cache[UID] == ctx.vault
    assert current_vault_root.get() == (UID, ctx.vault)

    result = await tools.edit_note_impl("note.md", "rewritten\n")

    assert "Vault assignment changed" in result
    assert (ctx.vault / "note.md").read_text(encoding="utf-8") == "original body\n"


async def test_an_ownerless_credential_is_refused_without_a_query(
    multi_user_vault,
):
    """`user_id is None` in multi-user mode is nobody, not the global vault.

    Refused the way `_vault_root` refuses it — `VaultAnchorUnavailable`, a
    `RuntimeError` carrying the admission wording — so the usage row does not
    claim an administrator changed an assignment that never existed.
    """
    ctx = multi_user_vault
    with pytest.raises(vault_service.VaultAnchorUnavailable) as excinfo:
        await vault_service.confirmed_publication(None, lambda c: None)

    assert isinstance(excinfo.value, RuntimeError)
    assert not isinstance(excinfo.value, VaultAssignmentChanged)
    assert "not bound to a user" in str(excinfo.value)
    assert ctx.assignment["reads"] == 0


async def test_the_confirmation_holds_no_row_lock_across_the_publish(
    multi_user_vault,
):
    """A plain `SELECT`, in its own short-lived session, closed before the
    publish. The locked gate is what the transfer routes hold and what this
    path deliberately does not: putting a note read, a link-rewrite plan and an
    unbounded number of file writes inside a lock every authenticated request
    contends for is the cost that was rejected, not overlooked."""
    ctx = multi_user_vault
    seed(ctx)

    await tools.create_note_impl("fresh.md", "body\n")

    assert len(ctx.assignment["statements"]) == 1
    statement = ctx.assignment["statements"][0].upper()
    assert "FOR UPDATE" not in statement
    assert "FOR NO KEY UPDATE" not in statement
    assert "FOR SHARE" not in statement
    assert "PG_ADVISORY" not in statement


def test_the_admission_gate_still_performs_no_database_work(multi_user_vault):
    """#66's rule survives: `_vault_root` is a pure cache lookup."""
    ctx = multi_user_vault
    before = ctx.assignment["reads"]
    assert vault_service._vault_root(UID) == ctx.vault
    assert ctx.assignment["reads"] == before


# ── (c) an unchanged assignment publishes, once per publication ─────────────


async def test_an_unchanged_assignment_publishes_with_exactly_one_re_read(
    multi_user_vault,
):
    ctx = multi_user_vault
    seed(ctx)

    result = await tools.create_note_impl("fresh.md", "new body\n")

    assert result == "Created note: fresh.md"
    assert (ctx.vault / "fresh.md").read_text(encoding="utf-8") == "new body\n"
    assert ctx.assignment["reads"] == 1


@pytest.mark.parametrize("tool_name", MUTATING_TOOLS)
async def test_every_single_publication_tool_re_reads_exactly_once(
    multi_user_vault, tool_name
):
    if tool_name == "move_note":
        pytest.skip("move_note publishes more than once; covered below")
    ctx = multi_user_vault
    seed(ctx)

    result = await _call(ctx, tool_name)

    assert "Vault assignment changed" not in result, result
    assert ctx.assignment["reads"] == 1, result


async def test_a_read_tool_issues_no_assignment_re_read(multi_user_vault):
    ctx = multi_user_vault
    seed(ctx)

    result = await tools.read_note_impl("note.md")

    assert "original body" in result.content
    assert ctx.assignment["reads"] == 0


async def test_a_dry_run_edit_publishes_nothing_and_confirms_nothing(
    multi_user_vault,
):
    """A dry run needs no confirmation — and must never be the reason a later
    mode skipped one, which is why the check sits after the `dry_run` return
    rather than before the read."""
    ctx = multi_user_vault
    seed(ctx)

    result = await tools.edit_note_impl("note.md", "rewritten\n", dry_run=True)

    assert "---" in result or "@@" in result, result
    assert ctx.assignment["reads"] == 0
    assert (ctx.vault / "note.md").read_text(encoding="utf-8") == "original body\n"


async def test_single_user_mode_issues_no_re_read(monkeypatch, tmp_path):
    """No user row exists to disagree, so the specification says no query."""
    monkeypatch.setattr(vault_service.settings, "multi_user_mode", False)
    monkeypatch.setattr(vault_service.settings, "vault_path", str(tmp_path))

    reads = {"n": 0}

    class _Counting(_AssignmentSession):
        async def execute(self, statement):  # pragma: no cover - must not run
            reads["n"] += 1
            raise AssertionError("single-user mode must issue no query")

    monkeypatch.setattr(
        src.database,
        "async_session",
        lambda: _Counting({"row": None, "reads": 0, "statements": []}),
    )

    async def noop(*args, **kwargs):
        return None

    monkeypatch.setattr(tools, "_log_usage", noop)
    perm = current_permission.set("readwrite")
    try:
        result = await tools.create_note_impl("solo.md", "body\n")
    finally:
        current_permission.reset(perm)

    assert result == "Created note: solo.md"
    assert (tmp_path / "solo.md").read_text(encoding="utf-8") == "body\n"
    assert reads["n"] == 0


# ── (d) the refusal is auditable, with its own marker ───────────────────────


async def test_the_refusal_writes_the_distinct_error_marker(multi_user_vault):
    ctx = multi_user_vault
    seed(ctx)
    reassign(ctx, "reassigned")

    await tools.edit_note_impl("note.md", "rewritten\n")

    params = ctx.logged["params"]
    assert ctx.logged["tool"] == "edit_note"
    assert params["error"] == tools._VAULT_REASSIGNED_MARKER
    assert params["error"] != tools._NO_VAULT_MARKER
    # The usual allow-listed params, plus `error`, and nothing else: a refusal
    # must not become a new disclosure channel.
    assert set(params) == {
        "path",
        "append",
        "operation",
        "find",
        "section",
        "replace_all",
        "dry_run",
        # The destructive-intent flag joined the allow-list with #128; an
        # operator reading this row after a frontmatter block went missing
        # needs to see whether wholesale replacement was asked for.
        "replace_frontmatter",
        "error",
    }
    assert params["path"] == "note.md"


def test_the_two_markers_are_distinct():
    assert tools._VAULT_REASSIGNED_MARKER != tools._NO_VAULT_MARKER


async def test_a_successful_publication_writes_no_error_marker(multi_user_vault):
    ctx = multi_user_vault
    seed(ctx)

    await tools.create_note_impl("fresh.md", "body\n")

    assert "error" not in ctx.logged["params"]


# ── (e) both delete forms, independently ────────────────────────────────────


async def test_the_soft_delete_is_refused_before_the_trash_is_touched(
    multi_user_vault,
):
    ctx = multi_user_vault
    seed(ctx)
    reassign(ctx, "reassigned")

    result = await tools.delete_note_impl("note.md")

    assert "Vault assignment changed" in result
    assert (ctx.vault / "note.md").read_text(encoding="utf-8") == "original body\n"
    assert not (ctx.vault / ".trash").exists()


async def test_the_permanent_delete_is_refused_and_the_note_stays(
    multi_user_vault,
):
    ctx = multi_user_vault
    seed(ctx)
    reassign(ctx, "reassigned")

    result = await tools.delete_note_impl("note.md", permanent=True)

    assert "Vault assignment changed" in result
    assert (ctx.vault / "note.md").read_text(encoding="utf-8") == "original body\n"
    assert not (ctx.vault / ".trash").exists()


def test_the_permanent_delete_refusal_comes_from_the_helper(multi_user_vault):
    """C.2a: not from a check written into the tool body.

    Calling the helper directly with an unstamped target is the only way to
    tell the two apart, and the distinction is the whole point — a tool-body
    check protects one tool, a helper protects every caller there will ever be.
    """
    ctx = multi_user_vault
    seed(ctx)

    with vault_service.open_mutable("note.md", user_id=UID) as target:
        with pytest.raises(UnconfirmedPublication) as excinfo:
            vault_service.unlink_at(target)

    assert "permanently delete" in str(excinfo.value)
    assert (ctx.vault / "note.md").read_text(encoding="utf-8") == "original body\n"


def test_the_soft_delete_helper_refuses_an_unstamped_target(multi_user_vault):
    ctx = multi_user_vault
    seed(ctx)

    with vault_service.open_mutable("note.md", user_id=UID) as target:
        with pytest.raises(UnconfirmedPublication):
            vault_service.soft_delete_target(target, stamp="20260101-000000")

    assert (ctx.vault / "note.md").read_text(encoding="utf-8") == "original body\n"
    assert not (ctx.vault / ".trash").exists()


# ── (f) the unstamped path, for every publish helper ────────────────────────


def test_every_publish_helper_refuses_an_unconfirmed_target(multi_user_vault):
    ctx = multi_user_vault
    seed(ctx)
    (ctx.vault / "dest.md").write_text("x\n", encoding="utf-8")

    with vault_service.open_mutable("note.md", user_id=UID) as source:
        with pytest.raises(UnconfirmedPublication):
            vault_service.write_file_at(source, "body\n")
        with pytest.raises(UnconfirmedPublication):
            vault_service.write_bytes_at(source, b"body\n", overwrite=True)
        with pytest.raises(UnconfirmedPublication):
            vault_service.unlink_at(source)
        with pytest.raises(UnconfirmedPublication):
            vault_service.soft_delete_target(source)
        with vault_service.open_mutable("elsewhere.md", user_id=UID) as dest:
            with pytest.raises(UnconfirmedPublication):
                vault_service.move_file_no_clobber(source, dest)

    assert (ctx.vault / "note.md").read_text(encoding="utf-8") == "original body\n"
    assert not (ctx.vault / "elsewhere.md").exists()


def test_the_unstamped_refusal_is_distinguishable_from_the_operational_one():
    """A programming error and an administrator's action must not share a type.

    `UnconfirmedPublication` is deliberately *not* a `RuntimeError`, because the
    tool bodies catch `ValueError`/`RuntimeError`/`OSError` around their
    publishes and turn them into strings — a missing confirmation rendered as a
    failed write is a bug reported as weather.
    """
    assert not issubclass(UnconfirmedPublication, VaultAssignmentChanged)
    assert not issubclass(VaultAssignmentChanged, UnconfirmedPublication)
    assert issubclass(VaultAssignmentChanged, RuntimeError)
    assert not issubclass(UnconfirmedPublication, RuntimeError)


def test_a_confirmation_is_consumed_by_the_publication_it_covers(multi_user_vault):
    """The confirmation is intrinsically single-consumption.

    The spent flag lives on the confirmation object itself, not on the target it
    was handed to, so the same object cannot be spent twice — by a second
    publication through the same target, or by attaching it to a different one.
    """
    ctx = multi_user_vault
    seed(ctx)

    with vault_service.open_mutable("note.md", user_id=UID) as target:
        with a_confirmation(ctx) as confirmation:
            assert not confirmation.spent
            vault_service.write_file_at(target, "first\n", confirmation=confirmation)
            assert confirmation.spent
            with pytest.raises(UnconfirmedPublication) as excinfo:
                vault_service.write_file_at(
                    target, "second\n", confirmation=confirmation
                )
            assert "already been spent" in str(excinfo.value)

    assert (ctx.vault / "note.md").read_text(encoding="utf-8") == "first\n"


def test_one_confirmation_cannot_be_spent_on_two_targets(multi_user_vault):
    """Stamp C onto T1 and T2, publish T1, let the assignment change, publish
    T2 — both from one database read.

    Consumption used to clear a slot on the *target*, so the second target's
    slot was still full. It is on the confirmation now, so the second
    publication is refused whatever it is attached to.
    """
    ctx = multi_user_vault
    seed(ctx)

    with vault_service.open_mutable("note.md", user_id=UID) as first, (
        vault_service.open_mutable("second.md", user_id=UID)
    ) as second:
        with a_confirmation(ctx) as confirmation:
            vault_service.write_file_at(first, "one\n", confirmation=confirmation)
            with pytest.raises(UnconfirmedPublication):
                vault_service.write_file_at(
                    second, "two\n", confirmation=confirmation
                )

    assert not (ctx.vault / "second.md").exists()


def test_an_unleased_confirmation_authorises_nothing(multi_user_vault):
    """Adversarial round 2, MAJOR 1 — the property single-consumption lacked.

    A confirmation bounds *how many times* it may be used; the lease bounds
    *when*. An object that no confirmed publication is currently holding —
    never leased, or leased by a call that has already returned — is inert, so
    a callback that saved one and published with it after the `await` is
    refused rather than obeyed.
    """
    ctx = multi_user_vault
    seed(ctx)

    never_leased = vault_service.RootConfirmation(UID, str(ctx.vault), queried=True)
    assert not never_leased.active
    with vault_service.open_mutable("note.md", user_id=UID) as target:
        with pytest.raises(UnconfirmedPublication) as excinfo:
            vault_service.write_file_at(target, "x\n", confirmation=never_leased)
        assert "not leased" in str(excinfo.value)

        with a_confirmation(ctx) as expired:
            pass
        assert not expired.active
        with pytest.raises(UnconfirmedPublication):
            vault_service.write_file_at(target, "x\n", confirmation=expired)

    assert (ctx.vault / "note.md").read_text(encoding="utf-8") == "original body\n"


def test_a_confirmation_is_bound_to_the_user_and_the_root_it_was_taken_for(
    multi_user_vault,
):
    """`confirm` used to validate neither field, so a confirmation for one
    user or one root could authorise a publication into another's target."""
    ctx = multi_user_vault
    seed(ctx)

    with vault_service.open_mutable("note.md", user_id=UID) as target:
        with a_confirmation(ctx, user_id=UID + 1) as wrong_user:
            with pytest.raises(UnconfirmedPublication) as excinfo:
                vault_service.write_file_at(target, "x\n", confirmation=wrong_user)
            assert "taken for user_id" in str(excinfo.value)

        with a_confirmation(ctx, root=str(ctx.other)) as wrong_root:
            with pytest.raises(UnconfirmedPublication) as excinfo:
                vault_service.write_file_at(target, "x\n", confirmation=wrong_root)
            assert str(ctx.other) in str(excinfo.value)

    assert (ctx.vault / "note.md").read_text(encoding="utf-8") == "original body\n"


async def test_a_confirmation_cannot_be_held_across_an_await(multi_user_vault):
    """The property is now structural rather than tested by convention.

    The old test *demonstrated the hole*: it stamped a target, awaited, and
    then published — which is exactly the interleaving an administrator's
    commit lands in. There is no public way to write that any more. The only
    entry point is `confirmed_publication`, which awaits the read and calls a
    **synchronous** publish before returning control, so a caller never holds
    an unspent confirmation across a scheduling point.
    """
    ctx = multi_user_vault
    seed(ctx)

    # There is no public producer of a confirmation to retain.
    assert not hasattr(vault_service, "confirm_vault_assignment")
    assert not hasattr(vault_service.MutableTarget, "confirm")
    assert not hasattr(vault_service.MutableTarget, "take_confirmation")

    with vault_service.open_mutable("note.md", user_id=UID) as target:
        assert (
            await tools._confirmed_publication(
                UID, lambda c: vault_service.write_file_at(
                    target, "first\n", confirmation=c
                )
            )
        )[0] is None
        await asyncio.sleep(0)
        # A second publication costs a second confirmation, and therefore a
        # second read.
        assert (
            await tools._confirmed_publication(
                UID, lambda c: vault_service.write_file_at(
                    target, "second\n", confirmation=c
                )
            )
        )[0] is None

    assert ctx.assignment["reads"] == 2
    assert (ctx.vault / "note.md").read_text(encoding="utf-8") == "second\n"


async def test_a_deferred_publish_callback_is_refused_rather_than_driven(
    multi_user_vault,
):
    """Adversarial round 2: `iscoroutinefunction` saw neither generator shape.

    A coroutine function, a generator function and an async-generator function
    all publish on somebody else's schedule — a generator's body does not run
    at all when it is called — which is exactly the window the wrapper closes.
    All three are refused as callables, and the *result* is checked too,
    because a callable object whose `__call__` is a generator is none of them.
    """
    ctx = multi_user_vault
    seed(ctx)

    async def a_coroutine(confirmation):  # pragma: no cover - never run
        return None

    async def an_async_generator(confirmation):  # pragma: no cover - never run
        yield None

    def a_generator(confirmation):  # pragma: no cover - never run
        yield None

    class _CallableGenerator:
        def __call__(self, confirmation):  # pragma: no cover - never run
            yield None

    for callback, fragment in (
        (a_coroutine, "synchronous function"),
        (an_async_generator, "async generator function"),
        (a_generator, "generator function"),
    ):
        with pytest.raises(UnconfirmedPublication) as excinfo:
            await vault_service.confirmed_publication(UID, callback)
        assert fragment in str(excinfo.value)

    # Not a coroutine/generator *function*, so only the result check sees it.
    with pytest.raises(UnconfirmedPublication) as excinfo:
        await vault_service.confirmed_publication(UID, _CallableGenerator())
    assert "returned a generator" in str(excinfo.value)

    pending = []
    with pytest.raises(UnconfirmedPublication) as excinfo:
        await vault_service.confirmed_publication(
            UID, lambda c: pending.append(a_coroutine(c)) or pending[-1]
        )
    assert "returned an awaitable" in str(excinfo.value)
    # The wrapper deliberately does not close an unknown awaitable — that is
    # arbitrary code of a stranger's choosing — so the *test* closes the one it
    # created, rather than leaving a "never awaited" warning behind.
    pending[-1].close()

    untouched(ctx)


async def test_a_callback_cannot_retain_its_confirmation(multi_user_vault):
    """Adversarial round 2, MAJOR 1 — the exact failing input.

    `saved=[]; await confirmed_publication(7, lambda c: saved.append(c))` then
    a reassignment, then `write_file_at(target, …, confirmation=saved[0])`.
    The callback returns None, so under single-consumption alone the
    confirmation was still unspent and the later write succeeded against the
    old assignment.

    Two things stop it now, and the test asserts both: the wrapper refuses a
    callback that returned without consuming its confirmation, and the object
    it retained is inert anyway because the lease was revoked in a `finally`.
    """
    ctx = multi_user_vault
    seed(ctx)

    saved: list = []
    with pytest.raises(UnconfirmedPublication) as excinfo:
        await vault_service.confirmed_publication(UID, lambda c: saved.append(c))
    assert "without consuming" in str(excinfo.value)

    assert len(saved) == 1
    retained = saved[0]
    assert not retained.active and not retained.spent

    reassign(ctx, "reassigned")
    with vault_service.open_mutable("note.md", user_id=UID) as target:
        with pytest.raises(UnconfirmedPublication) as excinfo:
            vault_service.write_file_at(
                target, "stale write\n", confirmation=retained
            )
        assert "not leased" in str(excinfo.value)

    assert (ctx.vault / "note.md").read_text(encoding="utf-8") == "original body\n"


async def test_the_lease_is_revoked_even_when_the_callback_raises(
    multi_user_vault,
):
    """The `finally` is the mechanism, so an exception must revoke too — and
    must not be turned into the "did not consume" error on the way out."""
    ctx = multi_user_vault
    seed(ctx)

    saved: list = []

    def explode(confirmation):
        saved.append(confirmation)
        raise KeyError("boom")

    with pytest.raises(KeyError):
        await vault_service.confirmed_publication(UID, explode)

    assert not saved[0].active


# ── (g) move_note: the interleavings ────────────────────────────────────────


def _with_backlinks(ctx) -> None:
    """A move whose preflight plans two link rewrites."""
    (ctx.vault / "Old.md").write_text("the moved note\n", encoding="utf-8")
    (ctx.vault / "A.md").write_text("see [[Old]] here\n", encoding="utf-8")
    (ctx.vault / "B.md").write_text("also [[Old]] here\n", encoding="utf-8")
    ctx.metadata["index_rows"] = [
        _IndexRow("Old.md", 1),
        _IndexRow("A.md", 2),
        _IndexRow("B.md", 3),
    ]
    ctx.metadata["backlink_rows"] = [_IndexRow("A.md", 2), _IndexRow("B.md", 3)]


async def test_a_move_with_rewrites_confirms_once_per_publication(
    multi_user_vault,
):
    ctx = multi_user_vault
    _with_backlinks(ctx)

    result = await tools.move_note_impl("Old.md", "New.md", rewrite_links=True)

    assert "Moved Old.md → New.md" in result
    assert "warning" not in result, result
    # One for the `renameat2` that commits the move, one for each rewrite.
    assert ctx.assignment["reads"] == 3
    assert (ctx.vault / "New.md").exists()
    assert "[[New]]" in (ctx.vault / "A.md").read_text(encoding="utf-8")
    assert "[[New]]" in (ctx.vault / "B.md").read_text(encoding="utf-8")


async def test_the_moved_note_rewrites_its_own_links_under_a_fresh_stamp(
    multi_user_vault,
):
    """The destination target's stamp was consumed by the `renameat2`, so the
    self-link rewrite that publishes through the *same* target has to take its
    own. If the move's stamp were reused, this would be the first place it
    showed up as still-working code."""
    ctx = multi_user_vault
    (ctx.vault / "Old.md").write_text("I link to [[Old]] myself\n", encoding="utf-8")
    ctx.metadata["index_rows"] = [_IndexRow("Old.md", 1)]
    ctx.metadata["backlink_rows"] = []

    result = await tools.move_note_impl("Old.md", "New.md", rewrite_links=True)

    assert "rewrote 1 link(s) across 1 note(s)" in result
    assert (ctx.vault / "New.md").read_text(encoding="utf-8") == (
        "I link to [[New]] myself\n"
    )
    # One for the move, one for the self-rewrite.
    assert ctx.assignment["reads"] == 2


async def test_a_move_refused_before_the_commit_leaves_everything_alone(
    multi_user_vault,
):
    """The confirmation sits after the whole preflight, so a refusal is still
    free: nothing has been mutated and the metadata rows are untouched."""
    ctx = multi_user_vault
    _with_backlinks(ctx)
    reassign(ctx, "reassigned")

    result = await tools.move_note_impl("Old.md", "New.md", rewrite_links=True)

    assert "Vault assignment changed" in result
    assert (ctx.vault / "Old.md").read_text(encoding="utf-8") == "the moved note\n"
    assert not (ctx.vault / "New.md").exists()
    assert (ctx.vault / "A.md").read_text(encoding="utf-8") == "see [[Old]] here\n"
    assert (ctx.vault / "B.md").read_text(encoding="utf-8") == "also [[Old]] here\n"
    # No `notes_metadata` / `note_links` update transaction was ever opened.
    assert ctx.metadata["commits"] == 0


async def test_a_confirmation_outage_before_the_move_fails_the_call(
    multi_user_vault,
):
    """Adversarial round 1, MAJOR, first timing.

    Before the first publication a database failure propagates as a tool
    failure. Nothing has been mutated, so there is no partial outcome to
    report, and the two alternatives are worse: swallowing it would report a
    write that did not happen as an ordinary refusal, and recording it under
    the reassignment marker would put a claim in the audit trail that no
    administrator made.
    """
    ctx = multi_user_vault
    _with_backlinks(ctx)
    ctx.assignment["fail_after"] = 1

    with pytest.raises(vault_service.VaultConfirmationUnavailable):
        await tools.move_note_impl("Old.md", "New.md", rewrite_links=True)

    assert (ctx.vault / "Old.md").read_text(encoding="utf-8") == "the moved note\n"
    assert not (ctx.vault / "New.md").exists()
    assert ctx.metadata["commits"] == 0
    # `_tracked` builds no *result* for a raising body, but since #193 it does
    # write the audit row: a write tool that fails halfway used to leave no row
    # and no log line at all. The row says the body raised and names the class,
    # and it is deliberately not one of the pre-body refusal markers — the body
    # ran, read the note and computed the write before the confirmation failed.
    assert ctx.logged["tool"] == "move_note"
    assert ctx.logged["params"]["error"] == tools._TOOL_EXCEPTION_MARKER
    assert ctx.logged["params"]["error_type"] == "VaultConfirmationUnavailable"


async def test_a_confirmation_outage_after_the_move_reports_the_partial_outcome(
    multi_user_vault,
):
    """Adversarial round 1, MAJOR, second timing — the defect itself.

    The move confirms and commits, the metadata transaction commits, and then
    the first backlink rewrite's confirming SELECT raises. The exception used
    to propagate: `_tracked` never built or logged a result, and the agent got
    a traceback while `Old.md` was gone, `New.md` existed and every planned
    source was still unrewritten.

    It is now the same partial-outcome idiom a per-source failure uses — and it
    is named as an **outage**, not as a reassignment: nobody changed the
    assignment, and saying so would be a statement about something that did not
    happen.
    """
    ctx = multi_user_vault
    _with_backlinks(ctx)
    # 1: the move. 2: the first rewrite's confirmation — which fails.
    ctx.assignment["fail_after"] = 2

    result = await tools.move_note_impl("Old.md", "New.md", rewrite_links=True)

    assert "Moved Old.md → New.md" in result
    assert "partial success" in result
    assert "confirmation outage, not a reassignment" in result
    assert "could not be re-read" in result
    assert str(ctx.vault) in result
    assert "A.md" in result and "B.md" in result
    # The move stands and the metadata transaction is not undone.
    assert not (ctx.vault / "Old.md").exists()
    assert (ctx.vault / "New.md").read_text(encoding="utf-8") == "the moved note\n"
    assert ctx.metadata["commits"] == 1
    # Neither source was rewritten, and the loop stopped at the first one.
    assert (ctx.vault / "A.md").read_text(encoding="utf-8") == "see [[Old]] here\n"
    assert (ctx.vault / "B.md").read_text(encoding="utf-8") == "also [[Old]] here\n"
    # Logged, with a marker distinct from the reassignment one.
    assert ctx.logged["params"]["error"] == "vault_confirmation_unavailable"
    assert ctx.logged["params"]["error"] != tools._VAULT_REASSIGNED_MARKER


def test_the_three_error_markers_are_distinct():
    """An operator reading `/admin/usage` after an incident has to be able to
    tell "this credential had no vault", "an administrator moved it" and "the
    server could not tell" apart."""
    markers = {
        tools._NO_VAULT_MARKER,
        tools._VAULT_REASSIGNED_MARKER,
        tools._CONFIRMATION_UNAVAILABLE_MARKER,
    }
    assert len(markers) == 3


async def test_a_confirmation_outage_between_two_rewrites_stops_at_that_one(
    multi_user_vault,
):
    """The outage can land part way through the rewrites too: the ones already
    published stand, the rest are reported unrewritten."""
    ctx = multi_user_vault
    _with_backlinks(ctx)
    # 1: the move. 2: A.md's rewrite. 3: B.md's confirmation — which fails.
    ctx.assignment["fail_after"] = 3

    result = await tools.move_note_impl("Old.md", "New.md", rewrite_links=True)

    assert "rewrote 1 link(s) across 1 note(s)" in result
    assert "confirmation outage" in result
    assert "[[New]]" in (ctx.vault / "A.md").read_text(encoding="utf-8")
    assert (ctx.vault / "B.md").read_text(encoding="utf-8") == "also [[Old]] here\n"
    assert "B.md" in result


async def test_a_reassignment_during_the_metadata_transaction_stops_the_rewrites(
    multi_user_vault,
):
    """The interleaving the per-publication rule exists for.

    The move's own confirmation is read 1 and passes; the fake session then
    changes the assignment, which is exactly what an administrator's `COMMIT`
    landing inside the metadata transaction does. Read 2 — the first rewrite's
    own confirmation — therefore fails. A single stamp taken before the move
    would have carried straight through and published every rewrite into a
    vault the caller no longer holds.
    """
    ctx = multi_user_vault
    _with_backlinks(ctx)
    ctx.assignment["flip_after"] = 1
    ctx.assignment["flip_to"] = _UserRow(str(ctx.other), True)

    result = await tools.move_note_impl("Old.md", "New.md", rewrite_links=True)

    # The move stands and is reported, and it is *not* reported as clean.
    assert "Moved Old.md → New.md" in result
    assert "rewrote 0 link(s) across 0 note(s)" in result
    assert "warning" in result
    assert "vault assignment changed while the call was in flight" in result
    assert str(ctx.vault) in result, "the previous root is not named"
    assert "A.md" in result and "B.md" in result
    # The move is not rolled back, and the metadata update is not undone.
    assert (ctx.vault / "New.md").read_text(encoding="utf-8") == "the moved note\n"
    assert not (ctx.vault / "Old.md").exists()
    assert ctx.metadata["commits"] == 1
    # Not one further source was rewritten.
    assert (ctx.vault / "A.md").read_text(encoding="utf-8") == "see [[Old]] here\n"
    assert (ctx.vault / "B.md").read_text(encoding="utf-8") == "also [[Old]] here\n"
    # Read 1 committed the move, read 2 refused the first rewrite, and nothing
    # asked a third time.
    assert ctx.assignment["reads"] == 2
    assert ctx.logged["params"]["error"] == tools._VAULT_REASSIGNED_MARKER


async def test_a_reassignment_between_two_rewrites_stops_at_the_next_one(
    multi_user_vault,
):
    """Refused between rewrite *k* and *k+1*: the first *k* stand."""
    ctx = multi_user_vault
    _with_backlinks(ctx)
    ctx.assignment["flip_after"] = 2
    ctx.assignment["flip_to"] = _UserRow(str(ctx.other), True)

    result = await tools.move_note_impl("Old.md", "New.md", rewrite_links=True)

    assert "rewrote 1 link(s) across 1 note(s)" in result
    assert "vault assignment changed while the call was in flight" in result
    assert "B.md" in result
    assert "[[New]]" in (ctx.vault / "A.md").read_text(encoding="utf-8")
    assert (ctx.vault / "B.md").read_text(encoding="utf-8") == "also [[Old]] here\n"
    assert ctx.assignment["reads"] == 3


async def test_a_plain_move_confirms_once(multi_user_vault):
    ctx = multi_user_vault
    seed(ctx)

    result = await tools.move_note_impl("note.md", "moved.md")

    assert "Moved note.md → moved.md" in result
    assert ctx.assignment["reads"] == 1
    assert (ctx.vault / "moved.md").read_text(encoding="utf-8") == "original body\n"


async def test_the_rollback_is_authorised_by_a_permit_issued_only_by_the_move(
    multi_user_vault,
):
    """`_verify_the_moved_inode` rolls back by calling the same helper with the
    endpoints swapped. The permit is what authorises that — and round 2 found
    it was **forgeable**, so a hand-built one renamed with no confirmation at
    all.

    Issuance is internal to a successful confirmed forward move now; the permit
    authorises exactly one reverse move between those same two targets, and
    only while the confirmed publication that issued it is still on the stack.
    """
    ctx = multi_user_vault
    seed(ctx)

    # Forging one is refused outright.
    with vault_service.open_mutable("note.md", user_id=UID) as a, (
        vault_service.open_mutable("moved.md", user_id=UID)
    ) as b:
        with pytest.raises(UnconfirmedPublication) as excinfo:
            vault_service.MovePermit(source=b, destination=a)
        assert "issued only by a confirmed forward move" in str(excinfo.value)
        with pytest.raises(UnconfirmedPublication):
            vault_service.MovePermit(object(), source=b, destination=a)

    captured: dict = {}

    def _move_and_roll_back(confirmation):
        source = captured["source"]
        dest = captured["dest"]
        permit = vault_service.move_file_no_clobber(
            source, dest, confirmation=confirmation
        )
        assert confirmation.spent
        assert not isinstance(permit, vault_service.RootConfirmation)
        # Only the reverse of the move that produced it.
        with pytest.raises(UnconfirmedPublication) as excinfo:
            vault_service.move_file_no_clobber(source, dest, permit=permit)
        assert "only the reverse" in str(excinfo.value)

        vault_service.move_file_no_clobber(dest, source, permit=permit)
        # And exactly once.
        with pytest.raises(UnconfirmedPublication) as excinfo:
            vault_service.move_file_no_clobber(dest, source, permit=permit)
        assert "already been used" in str(excinfo.value)
        return permit

    with vault_service.open_mutable("note.md", user_id=UID) as source, (
        vault_service.open_mutable("moved.md", user_id=UID)
    ) as dest:
        captured["source"] = source
        captured["dest"] = dest
        permit = await vault_service.confirmed_publication(
            UID, _move_and_roll_back
        )

        # Inert once the confirmed publication has returned.
        with pytest.raises(UnconfirmedPublication) as excinfo:
            vault_service.move_file_no_clobber(dest, source, permit=permit)
        assert "already been used" in str(excinfo.value)

    assert (ctx.vault / "note.md").read_text(encoding="utf-8") == "original body\n"
    assert not (ctx.vault / "moved.md").exists()


async def test_a_permit_is_inert_once_its_publication_has_returned(
    multi_user_vault,
):
    """A rollback is part of the publication it reverses or it is nothing."""
    ctx = multi_user_vault
    seed(ctx)

    captured: dict = {}

    def _move(confirmation):
        return vault_service.move_file_no_clobber(
            captured["source"], captured["dest"], confirmation=confirmation
        )

    with vault_service.open_mutable("note.md", user_id=UID) as source, (
        vault_service.open_mutable("moved.md", user_id=UID)
    ) as dest:
        captured["source"] = source
        captured["dest"] = dest
        permit = await vault_service.confirmed_publication(UID, _move)
        assert not permit._confirmation.active

        with pytest.raises(UnconfirmedPublication) as excinfo:
            vault_service.move_file_no_clobber(dest, source, permit=permit)
        assert "already returned" in str(excinfo.value)

    # The move stands; the rollback was refused rather than performed.
    assert (ctx.vault / "moved.md").read_text(encoding="utf-8") == "original body\n"
    assert not (ctx.vault / "note.md").exists()


def test_a_move_takes_exactly_one_of_a_confirmation_and_a_permit(multi_user_vault):
    ctx = multi_user_vault
    seed(ctx)

    with vault_service.open_mutable("note.md", user_id=UID) as source, (
        vault_service.open_mutable("moved.md", user_id=UID)
    ) as dest:
        with pytest.raises(UnconfirmedPublication):
            vault_service.move_file_no_clobber(source, dest)

    assert not (ctx.vault / "moved.md").exists()


async def test_a_move_refuses_endpoints_belonging_to_different_callers(
    multi_user_vault, monkeypatch, tmp_path
):
    """Adversarial round 2, MAJOR 3.

    `rename_noreplace` removes the source entry as surely as it creates the
    destination one, but only the destination's confirmation was consumed. A
    source opened for one user under one assignment could therefore be removed
    under another user's confirmation. Unreachable from `move_note`, which
    opens both ends with one `uid` — checked at the primitive because the next
    caller may not.
    """
    ctx = multi_user_vault
    seed(ctx)

    other_uid = UID + 1
    other_root = tmp_path / "bob"
    other_root.mkdir()
    (other_root / "theirs.md").write_text("bob's note\n", encoding="utf-8")
    vault_service._user_vault_cache[other_uid] = other_root
    try:
        with vault_service.open_mutable("note.md", user_id=UID) as mine, (
            vault_service.open_mutable("theirs.md", user_id=other_uid)
        ) as theirs:
            # Different user ids.
            with a_confirmation(ctx) as confirmation:
                with pytest.raises(UnconfirmedPublication) as excinfo:
                    vault_service.move_file_no_clobber(
                        theirs, mine, confirmation=confirmation
                    )
                assert "different callers" in str(excinfo.value)
                assert not confirmation.spent
            # And in the other direction, so the refusal is not about which end
            # the confirmation happens to match.
            with a_confirmation(ctx) as confirmation:
                with pytest.raises(UnconfirmedPublication):
                    vault_service.move_file_no_clobber(
                        mine, theirs, confirmation=confirmation
                    )
    finally:
        vault_service._user_vault_cache.pop(other_uid, None)

    assert (ctx.vault / "note.md").read_text(encoding="utf-8") == "original body\n"
    assert (other_root / "theirs.md").read_text(encoding="utf-8") == "bob's note\n"


def test_a_move_refuses_endpoints_under_different_assignments(multi_user_vault):
    """The same rule on the assignment string alone, with one user id."""
    ctx = multi_user_vault
    seed(ctx)

    with vault_service.open_mutable("note.md", user_id=UID) as source, (
        vault_service.open_mutable("moved.md", user_id=UID)
    ) as dest:
        object.__setattr__(dest, "assignment", str(ctx.other))
        with a_confirmation(ctx, root=str(ctx.other)) as confirmation:
            with pytest.raises(UnconfirmedPublication) as excinfo:
                vault_service.move_file_no_clobber(
                    source, dest, confirmation=confirmation
                )
            assert "different vault assignments" in str(excinfo.value)

    assert not (ctx.vault / "moved.md").exists()


# ── (h) the registry, and the structural scan ───────────────────────────────


TOOLS_SOURCE = Path(tools.__file__).read_text(encoding="utf-8")
TOOLS_TREE = ast.parse(TOOLS_SOURCE)
TOOLS_FUNCTIONS = {
    node.name: node
    for node in ast.walk(TOOLS_TREE)
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
}

# The two tools that publish under the *stronger* locked gate — `before_publish`
# / `lock_identity_for_publish` hold the credential and user rows `FOR UPDATE`
# across the filesystem publish and re-check the root captured at mint. They are
# not weakened to the optimistic confirmation, and the specification says so.
LOCKED_GATE_TOOLS = {"import_from_url", "request_upload"}


def _called_names(node: ast.AST) -> set[str]:
    return {
        child.func.id
        for child in ast.walk(node)
        if isinstance(child, ast.Call) and isinstance(child.func, ast.Name)
    }


def _reachable(name: str, seen: set[str] | None = None) -> set[str]:
    """Every function in `tools.py` reachable from `name` by direct call."""
    seen = set() if seen is None else seen
    if name in seen or name not in TOOLS_FUNCTIONS:
        return seen
    seen.add(name)
    for callee in _called_names(TOOLS_FUNCTIONS[name]):
        _reachable(callee, seen)
    return seen


# `_mint_preflight` calls `_require_write()` behind its own `need_write`
# parameter, so its presence in a reachable set says nothing: `request_download`
# reaches it too and publishes nothing. The write gating for that helper is read
# at the *call site*, from the keyword.
_CONDITIONAL_WRITE_GATE = "_mint_preflight"


def _needs_write(reachable: set[str]) -> bool:
    """Whether the tool gates on `_require_write`, directly or through the
    shared mint preflight — and, for the preflight, only when the call site
    actually asks for write."""
    for name in reachable:
        node = TOOLS_FUNCTIONS[name]
        if name != _CONDITIONAL_WRITE_GATE and "_require_write" in _called_names(node):
            return True
        for call in ast.walk(node):
            if (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Name)
                and call.func.id == "_mint_preflight"
            ):
                for kw in call.keywords:
                    if (
                        kw.arg == "need_write"
                        and isinstance(kw.value, ast.Constant)
                        and kw.value.value is True
                    ):
                        return True
    return False


def _impl_for(tool) -> object | None:
    """The `_tracked` impl a registered tool delegates to."""
    for name in tool.fn.__code__.co_names:
        candidate = getattr(server, name, None)
        if hasattr(candidate, "__tracked_tool__"):
            return candidate
    return None


def test_the_registry_is_not_empty():
    assert len(mcp._tool_manager.list_tools()) >= 25


def test_every_write_tool_publishes_through_a_confirmed_target():
    """A tool added later must fail this test rather than publish unconfirmed.

    Driven off the server's own registry, in the idiom of
    `tests/test_issue_66_vault_unassignment_revokes_tools.py`: a hand-kept list
    of write tools is the shape of the bug itself, because the tool nobody
    remembers to add to the list is the one that leaks.
    """
    unconfirmed = []
    for tool in mcp._tool_manager.list_tools():
        impl = _impl_for(tool)
        assert impl is not None, f"{tool.name} delegates to no _tracked impl"
        reachable = _reachable(impl.__name__)
        if not _needs_write(reachable):
            continue
        if tool.name in LOCKED_GATE_TOOLS:
            continue
        confirms = any(
            callee.startswith("_confirmed_publication")
            for name in reachable
            for callee in _called_names(TOOLS_FUNCTIONS[name])
        )
        if not confirms:
            unconfirmed.append(tool.name)
    assert unconfirmed == [], (
        "these write tools publish without confirming the vault assignment: "
        f"{unconfirmed}"
    )


def test_no_read_tool_confirms_anything():
    """The other half of "the read path gains no query", structurally.

    `_vault_root` staying a pure cache lookup is only worth anything while the
    query it excludes is not reintroduced somewhere else on the read path. A
    search, a read, a listing and every graph tool dominate the call mix; a
    confirmation reachable from one of them would be the per-call query #66
    forbade, wearing a different name.
    """
    confirming_reads = []
    for tool in mcp._tool_manager.list_tools():
        impl = _impl_for(tool)
        if impl is None:
            continue
        reachable = _reachable(impl.__name__)
        if _needs_write(reachable):
            continue
        if any(
            callee.startswith("_confirmed_publication")
            for name in reachable
            for callee in _called_names(TOOLS_FUNCTIONS[name])
        ):
            confirming_reads.append(tool.name)
    assert confirming_reads == [], confirming_reads


def test_the_locked_gate_allow_list_is_exactly_the_two_stronger_paths():
    """Guard the exemption: it may only ever hold tools that take the *stronger*
    gate, never one that takes none."""
    import src.services.transfer as transfer

    assert LOCKED_GATE_TOOLS == {"import_from_url", "request_upload"}
    # `import_from_url` locks its own credential and user rows across the
    # publish; `request_upload` mints the capability the upload route redeems
    # under `before_publish()`'s held locks (`transfer.PrePublishGate`).
    assert hasattr(transfer, "lock_identity_for_publish")
    assert hasattr(transfer, "PrePublishGate")
    assert "lock_identity_for_publish" in _called_names(
        TOOLS_FUNCTIONS["import_from_url_impl"]
    ) or "lock_identity_for_publish" in TOOLS_SOURCE
    registered = {tool.name for tool in mcp._tool_manager.list_tools()}
    assert LOCKED_GATE_TOOLS <= registered


def test_the_write_tool_detector_finds_the_tools_we_know_about():
    """Guard the guard: a detector that classified nothing as a write tool
    would make the assertion above vacuous."""
    found = set()
    for tool in mcp._tool_manager.list_tools():
        impl = _impl_for(tool)
        if impl is not None and _needs_write(_reachable(impl.__name__)):
            found.add(tool.name)
    assert {
        "create_note",
        "edit_note",
        "move_note",
        "delete_note",
        "set_frontmatter",
        "write_file",
        "delete_file",
        "import_from_url",
        "request_upload",
    } <= found, found
    # A read-only mint shares `_mint_preflight` and must not be swept in.
    assert "request_download" not in found


# ── the structural scan: no bare mutating syscall on a target's dir_fd ──────


# Every module under `src/mcp_server/`, plus `src/services/vault.py` — globbed
# rather than listed, so a module added later is scanned on the day it appears.
SCANNED_SOURCES = sorted(Path(tools.__file__).parent.rglob("*.py")) + [
    Path(vault_service.__file__)
]

MUTATING_SYSCALLS = {"unlink", "rename", "replace", "link", "mkdir", "remove", "rmdir"}
DIR_FD_KEYWORDS = {"dir_fd", "src_dir_fd", "dst_dir_fd"}

# The only functions permitted to reach one, and why each is a publish helper
# rather than a call site that slipped the net:
#   `_atomic_write_at`   — the overwrite publication (`os.replace`).
#   `_link_staged_name`  — the named-staging fallback's no-clobber publication
#                          (#103/#104, `os.link` — creates the name or fails
#                          `EEXIST`); called only from `_atomic_write_at`,
#                          after `_require_confirmation` has run, with the same
#                          standing as its `os.replace`.
#   `unlink_at`          — the permanent-unlink helper #88 added; the call
#                          site C.2a moved out of `delete_note`.
# `_discard_temp` left this list when #104 delegated it to
# `vault_fs.discard_staged_name` (the D27 consolidation): the unlink now lives
# in the primitive module the scan deliberately does not police.
PUBLISH_HELPERS = {"_atomic_write_at", "_link_staged_name", "unlink_at"}


def _enclosing_function(tree: ast.AST, target: ast.AST) -> str | None:
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for child in ast.walk(node):
                if child is target:
                    return node.name
    return None


def _dir_fd_mutations(path: Path) -> list[tuple[str, str | None, int]]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id == "os"
            and func.attr in MUTATING_SYSCALLS
        ):
            continue
        if not any(kw.arg in DIR_FD_KEYWORDS for kw in node.keywords):
            continue
        found.append(
            (func.attr, _enclosing_function(tree, node), node.lineno)
        )
    return found


def test_no_mutating_syscall_reaches_a_targets_dir_fd_outside_a_publish_helper():
    """What would have caught the bare permanent unlink (C.2a).

    `delete_note(permanent=True)` reached `os.unlink(target.name,
    dir_fd=target.dir_fd)` directly. Every other destructive operation on a
    `MutableTarget` went through a helper, so the claim "the publish helpers
    refuse an unconfirmed target" read as true while being false for the one
    operation that destroys a note outright. A prose rule would not have found
    it; this does, and it fails for the next one too.
    """
    offenders = []
    for source in SCANNED_SOURCES:
        for syscall, function, lineno in _dir_fd_mutations(source):
            if function in PUBLISH_HELPERS:
                continue
            offenders.append(f"{source.name}:{lineno} os.{syscall} in {function}")
    assert offenders == [], offenders


def test_the_structural_scan_actually_sees_the_permitted_call_sites():
    """Guard the guard: a scan that matched nothing would pass vacuously and
    keep passing after somebody added the very call site it exists to find."""
    seen = {
        (source.name, syscall, function)
        for source in SCANNED_SOURCES
        for syscall, function, _ in _dir_fd_mutations(source)
    }
    assert ("vault.py", "replace", "_atomic_write_at") in seen
    assert ("vault.py", "link", "_link_staged_name") in seen
    assert ("vault.py", "unlink", "unlink_at") in seen
    # And nothing under `src/mcp_server/` reaches one at all any more.
    assert not [entry for entry in seen if entry[0] != "vault.py"], seen


def test_the_structural_scan_would_fail_on_a_reintroduced_bare_unlink(tmp_path):
    """The scan is only worth having if it fails on the pattern it names."""
    offending = tmp_path / "regressed.py"
    offending.write_text(
        "import os\n"
        "def delete_note_impl(target):\n"
        "    os.unlink(target.name, dir_fd=target.dir_fd)\n",
        encoding="utf-8",
    )
    found = _dir_fd_mutations(offending)
    assert found == [("unlink", "delete_note_impl", 3)]
    assert found[0][1] not in PUBLISH_HELPERS
