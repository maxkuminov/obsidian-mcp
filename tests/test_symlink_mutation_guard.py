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
from src.mcp_server.auth import current_permission
from src.services import vault as vault_service
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


async def test_move_through_a_symlinked_folder_keeps_the_index_consistent(
    writable, monkeypatch
):
    """`os.walk` does not follow directory links, so the indexer stores
    `Real/A.md` for a note reachable as `Shared/A.md`. The move has to update
    *those* rows — keying the UPDATEs on the path the caller typed would leave
    `notes_metadata` pointing at a file that no longer exists.
    """
    (writable / "Real").mkdir()
    (writable / "Real" / "A.md").write_text("moved note\n", encoding="utf-8")
    (writable / "Shared").symlink_to(writable / "Real")
    (writable / "src.md").write_text("See [[A]]\n", encoding="utf-8")

    factory, statements = _fake_session_recording(
        [_Row(file_path="Real/A.md", id=1), _Row(file_path="src.md", id=2)],
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
    # real note id (1) rather than the synthetic id a miss would have invented.
    backlink_params = statements[1].compile().params
    assert 1 in backlink_params.values()
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
