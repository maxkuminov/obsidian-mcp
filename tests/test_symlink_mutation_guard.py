"""Mutating tools act on the path as named — never on a symlink's target.

`validate_path` resolves the whole path, so an in-vault alias
`alias.md -> important.md` made every write tool operate on `important.md`
while reporting success for `alias.md`: a destructive write on a path nobody
named. `validate_mutable_path` resolves only the *parent* (keeping symlinked
folders inside the vault usable, which is a common Obsidian setup) and refuses
a symlinked final component, naming the target so the agent can act on the real
note instead.

Reads are deliberately unchanged — an alias reading as its target is what a
user expects from an alias.
"""
import base64
from pathlib import Path

import pytest

import src.mcp_server.tools as tools
from src.auth.session import current_user_id
from src.config import MAX_NOTE_BYTES, settings
from src.mcp_server.auth import current_permission
from src.services import vault as vault_service
from src.services.indexer import discover_markdown_files
from src.services.vault import validate_mutable_path


@pytest.fixture
def vault(monkeypatch, tmp_path):
    monkeypatch.setattr(vault_service.settings, "vault_path", str(tmp_path))
    return tmp_path


# ── the alias case, and what the error says ─────────────────────────────────


def test_alias_at_the_vault_root_is_refused_and_names_its_target(vault):
    (vault / "important.md").write_text("real", encoding="utf-8")
    (vault / "alias.md").symlink_to(vault / "important.md")

    with pytest.raises(ValueError) as excinfo:
        validate_mutable_path("alias.md")

    message = str(excinfo.value)
    assert "alias.md is a symbolic link to important.md" in message
    assert "operate on the target instead" in message


def test_nested_alias_names_the_canonical_vault_relative_target(vault):
    (vault / "real.md").write_text("real", encoding="utf-8")
    (vault / "Folder").mkdir()
    # A relative link with a `..` hop: the message must show where it lands in
    # the vault, not the link's literal (unusable) text.
    (vault / "Folder" / "alias.md").symlink_to(Path("..") / "real.md")

    with pytest.raises(ValueError) as excinfo:
        validate_mutable_path("Folder/alias.md")

    assert "is a symbolic link to real.md" in str(excinfo.value)


def test_dangling_link_is_refused(vault):
    (vault / "alias.md").symlink_to(vault / "gone.md")

    with pytest.raises(ValueError) as excinfo:
        validate_mutable_path("alias.md")

    # Still names the intended target: the caller learns what the alias meant.
    assert "is a symbolic link to gone.md" in str(excinfo.value)


def test_link_pointing_outside_the_vault_is_refused_without_leaking_the_path(
    vault, tmp_path_factory
):
    secret = tmp_path_factory.mktemp("outside") / "secret.md"
    secret.write_text("secret", encoding="utf-8")
    (vault / "alias.md").symlink_to(secret)

    with pytest.raises(ValueError) as excinfo:
        validate_mutable_path("alias.md")

    message = str(excinfo.value)
    assert "outside the vault" in message
    assert str(secret) not in message


# ── symlinked directories: allowed inside, rejected escaping ────────────────


def test_symlinked_ancestor_inside_the_vault_resolves_to_the_real_directory(vault):
    (vault / "Real").mkdir()
    (vault / "Shared").symlink_to(vault / "Real")

    resolved = validate_mutable_path("Shared/new.md")

    assert resolved == (vault / "Real" / "new.md").resolve()


def test_symlinked_ancestor_leaving_the_vault_is_refused(vault, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside")
    (vault / "Escape").symlink_to(outside)

    with pytest.raises(ValueError, match="Path traversal denied"):
        validate_mutable_path("Escape/new.md")


def test_a_plain_path_returns_the_named_entry(vault):
    (vault / "Folder").mkdir()
    assert validate_mutable_path("Folder/note.md") == (
        vault / "Folder" / "note.md"
    ).resolve()


# ── the guards inherited from the existing validators ───────────────────────


@pytest.mark.parametrize(
    "bad_path",
    ["../escape.md", "Folder/../../escape.md", "/etc/passwd", "note\x00.md"],
)
def test_traversal_shapes_are_denied(vault, bad_path):
    with pytest.raises(ValueError, match="Path traversal denied"):
        validate_mutable_path(bad_path)


@pytest.mark.parametrize("bad_path", ["", ".", "Folder/"])
def test_non_file_shapes_are_denied(vault, bad_path):
    with pytest.raises(ValueError, match="Not a file path"):
        validate_mutable_path(bad_path)


def test_hidden_paths_are_denied(vault):
    (vault / ".obsidian").mkdir()
    with pytest.raises(ValueError, match="Hidden path denied"):
        validate_mutable_path(".obsidian/config.md")


def test_a_hidden_directory_reached_through_a_link_is_denied(vault):
    """The hidden check runs on the *resolved* relative path, so a symlinked
    folder cannot be used to smuggle a write into `.obsidian`."""
    (vault / ".obsidian").mkdir()
    (vault / "Config").symlink_to(vault / ".obsidian")

    with pytest.raises(ValueError, match="Hidden path denied"):
        validate_mutable_path("Config/app.json")


# ── multi-user vault roots ──────────────────────────────────────────────────


def test_per_user_root_is_used_for_both_the_check_and_the_target_name(
    vault, tmp_path_factory
):
    user_vault = tmp_path_factory.mktemp("user-7")
    (user_vault / "important.md").write_text("real", encoding="utf-8")
    (user_vault / "alias.md").symlink_to(user_vault / "important.md")
    vault_service._user_vault_cache[7] = Path(user_vault)
    try:
        # The single-user root has no such link — resolution must use user 7's.
        with pytest.raises(ValueError) as excinfo:
            validate_mutable_path("alias.md", user_id=7)
        assert "is a symbolic link to important.md" in str(excinfo.value)

        (user_vault / "Real").mkdir()
        (user_vault / "Shared").symlink_to(user_vault / "Real")
        assert validate_mutable_path("Shared/n.md", user_id=7) == (
            user_vault / "Real" / "n.md"
        ).resolve()
    finally:
        vault_service.clear_user_vault_cache()


# ────────────────────────────────────────────────────────────────────────────
# The tools themselves
# ────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def writable(vault, monkeypatch):
    """A vault the write tools can use, with usage logging stubbed out."""

    async def noop(*args, **kwargs):
        return None

    monkeypatch.setattr(tools, "_log_usage", noop)
    token = current_permission.set("readwrite")
    yield vault
    current_permission.reset(token)


@pytest.fixture
def alias(writable):
    """`alias.md` → `important.md`, plus the target's exact pre-state."""
    target = writable / "important.md"
    target.write_text("---\nstatus: real\n---\nimportant body\n", encoding="utf-8")
    link = writable / "alias.md"
    link.symlink_to(target)
    return target, link, target.read_bytes(), target.stat().st_mtime_ns


def _mutations(path: str) -> dict:
    """Every mutating tool, keyed by label, ready to be invoked on `path`.

    `move_note` appears twice — a symlink is refused whether it is the source
    or the destination — and `edit_note` once per mode, `dry_run` included:
    a mode that does not write must still refuse, or the caller gets a diff
    against a note it did not name.
    """
    return {
        "create_note": lambda: tools.create_note_impl(path, "clobber"),
        "edit_note": lambda: tools.edit_note_impl(path, "clobber"),
        "edit_note_append": lambda: tools.edit_note_impl(
            path, "clobber", append=True
        ),
        "edit_note_find": lambda: tools.edit_note_impl(
            path, "clobber", find="important"
        ),
        "edit_note_section": lambda: tools.edit_note_impl(
            path, "clobber", section="#1"
        ),
        "edit_note_dry_run": lambda: tools.edit_note_impl(
            path, "clobber", dry_run=True
        ),
        "set_frontmatter": lambda: tools.set_frontmatter_impl(
            path, updates={"status": "clobbered"}
        ),
        "move_note_source": lambda: tools.move_note_impl(path, "elsewhere.md"),
        "move_note_destination": lambda: tools.move_note_impl("mover.md", path),
        "delete_note": lambda: tools.delete_note_impl(path),
        "delete_note_permanent": lambda: tools.delete_note_impl(
            path, permanent=True
        ),
        "write_file": lambda: tools.write_file_impl(
            path,
            base64.b64encode(b"clobber").decode(),
            overwrite=True,
        ),
    }


MUTATIONS = tuple(_mutations("unused"))


@pytest.mark.parametrize("tool", MUTATIONS)
async def test_every_mutating_tool_refuses_an_alias(alias, writable, tool):
    target, link, before_bytes, before_mtime = alias
    (writable / "mover.md").write_text("mover", encoding="utf-8")

    result = await _mutations("alias.md")[tool]()

    assert "symbolic link" in result, result
    assert "important.md" in result, result
    # The target is untouched — bytes and mtime — and the link is still a link.
    assert target.read_bytes() == before_bytes
    assert target.stat().st_mtime_ns == before_mtime
    assert link.is_symlink()


@pytest.mark.parametrize(
    "tool", ["create_note", "edit_note_dry_run", "write_file", "delete_note"]
)
async def test_a_dangling_link_is_refused_and_nothing_is_written(writable, tool):
    """A link whose target does not exist is still a link: creating or writing
    'through' it would put the bytes at the target path, not the named one."""
    (writable / "mover.md").write_text("mover", encoding="utf-8")
    (writable / "alias.md").symlink_to(writable / "gone.md")

    result = await _mutations("alias.md")[tool]()

    assert "symbolic link" in result, result
    assert "gone.md" in result, result
    assert not (writable / "gone.md").exists()
    assert (writable / "alias.md").is_symlink()


@pytest.mark.parametrize("tool", ["create_note", "write_file", "move_note_destination"])
async def test_a_dangling_link_at_a_destination_is_refused(writable, tool):
    (writable / "mover.md").write_text("mover", encoding="utf-8")
    (writable / "dest.md").symlink_to(writable / "nowhere.md")

    result = await _mutations("dest.md")[tool]()

    assert "symbolic link" in result, result
    assert not (writable / "nowhere.md").exists()
    assert (writable / "mover.md").read_text(encoding="utf-8") == "mover"


@pytest.mark.parametrize("tool", MUTATIONS)
async def test_an_escaping_link_still_gets_the_traversal_error(
    writable, tmp_path_factory, tool
):
    outside = tmp_path_factory.mktemp("outside")
    (outside / "victim.md").write_text("victim", encoding="utf-8")
    (writable / "mover.md").write_text("mover", encoding="utf-8")
    (writable / "Escape").symlink_to(outside)

    result = await _mutations("Escape/victim.md")[tool]()

    assert "Path traversal denied" in result, result
    assert (outside / "victim.md").read_text(encoding="utf-8") == "victim"


# ── symlinked folders inside the vault keep working ─────────────────────────


async def test_create_note_through_a_symlinked_folder_lands_in_the_real_one(
    writable,
):
    (writable / "Real").mkdir()
    (writable / "Shared").symlink_to(writable / "Real")

    result = await tools.create_note_impl("Shared/new.md", "body\n")

    assert "Created note" in result, result
    assert (writable / "Real" / "new.md").read_text(encoding="utf-8") == "body\n"
    assert not (writable / "Real" / "Shared").exists()


async def test_write_file_through_a_symlinked_folder_lands_in_the_real_one(
    writable,
):
    (writable / "Real").mkdir()
    (writable / "Shared").symlink_to(writable / "Real")

    result = await tools.write_file_impl(
        "Shared/a.bin", base64.b64encode(b"bytes").decode()
    )

    assert "Wrote" in result, result
    assert (writable / "Real" / "a.bin").read_bytes() == b"bytes"


async def test_edit_and_delete_work_through_a_symlinked_folder(writable):
    (writable / "Real").mkdir()
    (writable / "Real" / "note.md").write_text("before\n", encoding="utf-8")
    (writable / "Shared").symlink_to(writable / "Real")

    assert "Updated note" in await tools.edit_note_impl("Shared/note.md", "after\n")
    assert (writable / "Real" / "note.md").read_text(encoding="utf-8") == "after\n"

    assert "Soft-deleted" in await tools.delete_note_impl("Shared/note.md")
    assert not (writable / "Real" / "note.md").exists()


# ── move through a symlinked folder: the DB rows follow the real path ───────


class _Row:
    def __init__(self, **fields):
        self.__dict__.update(fields)


def _fake_session_recording(*result_rows):
    """`async_session` stand-in replaying canned rows and recording statements."""
    calls = {"n": 0}
    statements: list = []

    class Result:
        def __init__(self, rows):
            self._rows = rows

        def all(self):
            return self._rows

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def execute(self, statement):
            statements.append(statement)
            i = calls["n"]
            calls["n"] += 1
            return Result(result_rows[i] if i < len(result_rows) else [])

        async def commit(self):
            return None

    return FakeSession, statements


def test_discovery_sees_a_note_under_a_symlinked_folder_only_at_its_real_path(
    vault,
):
    """The premise the move test rests on, checked against the real discovery.

    `Path.rglob` does not descend directory symlinks, so the indexer never
    walks `Shared/`. Whatever this function returns is what
    `notes_metadata.file_path` ends up holding.
    """
    (vault / "Real").mkdir()
    (vault / "Real" / "A.md").write_text("moved note\n", encoding="utf-8")
    (vault / "Shared").symlink_to(vault / "Real")
    (vault / "src.md").write_text("See [[A]]\n", encoding="utf-8")

    assert sorted(discover_markdown_files(vault)) == ["Real/A.md", "src.md"]


async def test_move_through_a_symlinked_folder_keeps_the_index_consistent(
    writable, monkeypatch
):
    """The indexer stores `Real/A.md` for a note reachable as `Shared/A.md`.
    The move has to update *those* rows — keying the UPDATEs on the path the
    caller typed would leave `notes_metadata` pointing at a file that no longer
    exists.

    The indexed rows here come from the indexer's own discovery rather than a
    canned string, so the test breaks if discovery ever starts following
    directory links (which would make `Shared/A.md` a second, competing row).
    """
    (writable / "Real").mkdir()
    (writable / "Real" / "A.md").write_text("moved note\n", encoding="utf-8")
    (writable / "Shared").symlink_to(writable / "Real")
    (writable / "src.md").write_text("See [[A]]\n", encoding="utf-8")

    indexed = {
        rel: i + 1 for i, rel in enumerate(sorted(discover_markdown_files(writable)))
    }
    assert indexed == {"Real/A.md": 1, "src.md": 2}

    factory, statements = _fake_session_recording(
        [_Row(file_path=rel, id=note_id) for rel, note_id in indexed.items()],
        [_Row(file_path="src.md")],
    )
    monkeypatch.setattr(tools, "async_session", factory)

    result = await tools.move_note_impl(
        "Shared/A.md", "Shared/B.md", rewrite_links=True
    )

    # The move reports — and performs — the real path behind the link.
    assert "Moved Real/A.md → Real/B.md" in result, result
    assert (writable / "Real" / "B.md").read_text(encoding="utf-8") == "moved note\n"
    assert not (writable / "Real" / "A.md").exists()
    assert (writable / "Shared" / "B.md").exists()

    # Backlink discovery looked up `Real/A.md` in the index, so it found the
    # real note id rather than the synthetic id a miss would have invented.
    backlink_params = statements[1].compile().params
    assert indexed["Real/A.md"] in backlink_params.values()
    assert -1 not in backlink_params.values()

    # And the backlink itself was rewritten.
    assert (writable / "src.md").read_text(encoding="utf-8") == "See [[B]]\n"

    # Both UPDATEs are keyed on the resolved paths.
    for statement in statements[2:]:
        values = set(statement.compile().params.values())
        assert "Real/A.md" in values
        assert "Real/B.md" in values
        assert "Shared/A.md" not in values
        assert "Shared/B.md" not in values


# ── one resolution per mutation ─────────────────────────────────────────────
#
# The guard resolves the parent once. That is only worth anything if the tool
# then acts on the returned Path: a tool that keeps the Path for the check and
# re-passes its own string to `read_bytes`/`write_file` resolves it again, and
# an ancestor repointed in between redirects the write to a note nobody named.
# `expected=` does not save it — the decoy can hold byte-identical content.


@pytest.fixture
def repoint_after_read(monkeypatch):
    """Repoint `link` at `new_target` in the window between the read and the
    write — the moment the tool has the old bytes in hand and has not yet
    published the new ones.

    Hooked on `_read_fd_bytes`, the one read primitive every *mutation* path
    shares, so the test does not encode *how* a tool reads. It fires once: the
    compare in `_atomic_write_at` reads again, and swapping twice would prove
    nothing.
    """

    def install(link: Path, new_target: Path):
        original = vault_service._read_fd_bytes
        fired: list[bool] = []

        def swapping(dir_fd, name, max_bytes=None):
            data = original(dir_fd, name, max_bytes=max_bytes)
            if not fired:
                fired.append(True)
                link.unlink()
                link.symlink_to(new_target)
            return data

        monkeypatch.setattr(vault_service, "_read_fd_bytes", swapping)

    return install


def _two_directories_with_identical_notes(root: Path) -> tuple[Path, Path]:
    """`RealA/note.md` and `RealB/note.md`, byte-identical on purpose."""
    (root / "RealA").mkdir()
    (root / "RealB").mkdir()
    (root / "RealA" / "note.md").write_text("before\n", encoding="utf-8")
    (root / "RealB" / "note.md").write_text("before\n", encoding="utf-8")
    return root / "RealA" / "note.md", root / "RealB" / "note.md"


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(
            lambda: tools.edit_note_impl("Shared/note.md", "after\n"),
            id="edit_note",
        ),
        pytest.param(
            lambda: tools.set_frontmatter_impl(
                "Shared/note.md", updates={"status": "done"}
            ),
            id="set_frontmatter",
        ),
    ],
)
async def test_repointing_an_ancestor_mid_write_cannot_redirect_it(
    writable, repoint_after_read, mutate
):
    note, decoy = _two_directories_with_identical_notes(writable)
    link = writable / "Shared"
    link.symlink_to(writable / "RealA")
    repoint_after_read(link, writable / "RealB")

    result = await mutate()

    assert "note.md" in result, result
    # The write landed in the directory that was validated…
    assert note.read_text(encoding="utf-8") != "before\n"
    # …and the note the link now points at was never touched.
    assert decoy.read_text(encoding="utf-8") == "before\n"


async def test_repointing_a_symlinked_vault_root_mid_write_cannot_redirect_it(
    writable, monkeypatch, tmp_path_factory, repoint_after_read
):
    """Same hazard one level up: the vault root itself is a link."""
    real_root = tmp_path_factory.mktemp("real-root")
    decoy_root = tmp_path_factory.mktemp("decoy-root")
    (real_root / "note.md").write_text("before\n", encoding="utf-8")
    (decoy_root / "note.md").write_text("before\n", encoding="utf-8")
    root_link = tmp_path_factory.mktemp("roots") / "vault"
    root_link.symlink_to(real_root)
    monkeypatch.setattr(vault_service.settings, "vault_path", str(root_link))

    repoint_after_read(root_link, decoy_root)

    result = await tools.edit_note_impl("note.md", "after\n")

    assert "Updated note" in result, result
    assert (real_root / "note.md").read_text(encoding="utf-8") == "after\n"
    assert (decoy_root / "note.md").read_text(encoding="utf-8") == "before\n"


# `existing` says whether `note.md` is there when the tool validates: the
# read-modify-write tools need it to be, the creating ones need it not to be
# (a link present at validation is refused outright, which is the #54 case).
_SWAPPED_LEAF_CASES = {
    "edit_note": (lambda: tools.edit_note_impl("note.md", "after\n"), True),
    "edit_note_append": (
        lambda: tools.edit_note_impl("note.md", "tail", append=True),
        True,
    ),
    "edit_note_find": (
        lambda: tools.edit_note_impl("note.md", "after", find="before"),
        True,
    ),
    "set_frontmatter": (
        lambda: tools.set_frontmatter_impl("note.md", updates={"a": 1}),
        True,
    ),
    "delete_note": (lambda: tools.delete_note_impl("note.md"), True),
    "delete_note_permanent": (
        lambda: tools.delete_note_impl("note.md", permanent=True),
        True,
    ),
    "move_note_source": (
        lambda: tools.move_note_impl("note.md", "moved.md"),
        True,
    ),
    "create_note": (lambda: tools.create_note_impl("note.md", "body\n"), False),
    "write_file_overwrite": (
        lambda: tools.write_file_impl(
            "note.md", base64.b64encode(b"clobber").decode(), overwrite=True
        ),
        False,
    ),
    "write_file_no_clobber": (
        lambda: tools.write_file_impl(
            "note.md", base64.b64encode(b"clobber").decode()
        ),
        False,
    ),
    "move_note_destination": (
        lambda: tools.move_note_impl("mover.md", "note.md"),
        False,
    ),
}


@pytest.mark.parametrize("tool", sorted(_SWAPPED_LEAF_CASES))
async def test_a_leaf_swapped_for_a_link_after_validation_is_reported(
    writable, monkeypatch, tool
):
    """The residual TOCTOU the design accepts: the leaf becomes a symlink
    between validation and the act.

    Every mutating tool must *name* it. Two wrong answers this pins against:
    reporting "not found" (an agent's next move is to create it, over the
    link), and reporting success — `write_file(overwrite=True)` replaces the
    link rather than following it, which is safe for the target but silently
    consumes an alias the caller still believes in.

    Nothing is mutated on any path, and the link's target is never touched.
    """
    mutate, existing = _SWAPPED_LEAF_CASES[tool]
    note = writable / "note.md"
    if existing:
        note.write_text("before\n", encoding="utf-8")
    (writable / "mover.md").write_text("mover\n", encoding="utf-8")
    (writable / "elsewhere.md").write_text("elsewhere\n", encoding="utf-8")

    original = vault_service.open_mutable
    swapped: list[bool] = []

    def swap_after_validating(relative_path, user_id=None):
        target = original(relative_path, user_id=user_id)
        # Only for the path this case is about: `move_note` validates two, and
        # the destination case must be sabotaged on the destination.
        if relative_path == "note.md" and not swapped:
            swapped.append(True)
            if existing:
                note.unlink()
            note.symlink_to(writable / "elsewhere.md")
        return target

    monkeypatch.setattr(tools, "open_mutable", swap_after_validating)

    result = await mutate()

    assert swapped, "the race never ran"
    assert "symbolic link" in result, result
    assert "not found" not in result.lower(), result
    assert "wrote" not in result.lower(), result
    # The link is still a link, and what it points at is untouched.
    assert (writable / "note.md").is_symlink()
    assert (writable / "elsewhere.md").read_text(encoding="utf-8") == "elsewhere\n"
    assert (writable / "mover.md").read_text(encoding="utf-8") == "mover\n"
    assert not (writable / "moved.md").exists()
    assert not (writable / ".trash").exists()


# ── an in-vault `..` is refused, but says why ───────────────────────────────


def test_an_in_vault_dotdot_names_the_normalised_path(vault):
    (vault / "Folder").mkdir()
    (vault / "note.md").write_text("real", encoding="utf-8")

    with pytest.raises(ValueError) as excinfo:
        validate_mutable_path("Folder/../note.md")

    message = str(excinfo.value)
    # Still refused — a mutating tool never resolves a component away — but
    # "traversal denied" would be a lie the caller cannot act on.
    assert "Path traversal denied" not in message
    assert "note.md" in message

    # Reads are unchanged: `..` inside the vault still resolves.
    assert vault_service.validate_visible_path("Folder/../note.md") == (
        vault / "note.md"
    ).resolve()


async def test_edit_note_passes_the_dotdot_message_through(writable):
    (writable / "Folder").mkdir()
    (writable / "note.md").write_text("real\n", encoding="utf-8")

    result = await tools.edit_note_impl("Folder/../note.md", "clobber")

    assert "Path traversal denied" not in result, result
    assert "note.md" in result, result
    assert (writable / "note.md").read_text(encoding="utf-8") == "real\n"


# ── the symlink error outranks the size check ───────────────────────────────


async def test_create_note_reports_the_alias_rather_than_the_size_limit(alias):
    """An oversize body through an alias is a *path* problem. Reporting the
    size limit would send the caller off to trim content that was never why
    the write is refused."""
    target, link, before_bytes, _ = alias

    result = await tools.create_note_impl("alias.md", "x" * (MAX_NOTE_BYTES + 1))

    assert "symbolic link" in result, result
    assert "important.md" in result, result
    assert target.read_bytes() == before_bytes


async def test_write_file_reports_the_alias_rather_than_the_size_limit(alias):
    target, link, before_bytes, _ = alias
    oversize = base64.b64encode(b"x" * (settings.max_file_write_bytes + 1)).decode()

    result = await tools.write_file_impl("alias.md", oversize, overwrite=True)

    assert "symbolic link" in result, result
    assert target.read_bytes() == before_bytes


# ── reads are deliberately unchanged ────────────────────────────────────────


async def test_reads_still_follow_an_alias(alias, writable):
    """An alias reading as its target is the whole point of an alias, and a
    read cannot destroy anything — so `read_note`/`read_file` are untouched."""
    note = await tools.read_note_impl("alias.md")
    assert "important body" in note

    text = await tools.read_file_impl("alias.md", encoding="text")
    assert "important body" in text

    (writable / "picture.png").write_bytes(b"\x89PNG\r\n\x1a\npixels")
    (writable / "alias.png").symlink_to(writable / "picture.png")
    binary = await tools.read_file_impl("alias.png", encoding="base64")
    assert base64.b64encode(b"\x89PNG\r\n\x1a\npixels").decode() in binary


# ── multi-user vault roots ──────────────────────────────────────────────────


async def test_the_tools_refuse_an_alias_under_a_per_user_root(
    writable, tmp_path_factory
):
    user_vault = tmp_path_factory.mktemp("user-9")
    target = user_vault / "important.md"
    target.write_text("user note\n", encoding="utf-8")
    (user_vault / "alias.md").symlink_to(target)
    vault_service._user_vault_cache[9] = Path(user_vault)
    user_token = current_user_id.set(9)
    try:
        result = await tools.edit_note_impl("alias.md", "clobber")
        assert "symbolic link" in result, result
        assert "important.md" in result, result
        assert target.read_text(encoding="utf-8") == "user note\n"
    finally:
        current_user_id.reset(user_token)
        vault_service.clear_user_vault_cache()
