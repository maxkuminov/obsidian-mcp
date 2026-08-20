"""Note mutations are anchored to a descriptor, not re-resolved per syscall (#59).

`validate_mutable_path` resolved the parent once and handed back
`resolved_parent / name`, which closed the *static* alias vector (#54). It did
not close the live one: every syscall the write then made — the temp create,
the `expected=` read, the publish — handed that **pathname** back to the
kernel, which walked it again. A process that renamed the resolved parent and
dropped a symlink at its name in between sent the write somewhere nobody
validated, and `expected=` could not see it because the decoy may hold
byte-identical bytes.

Every test here fires exactly that race, and every one of them **fails against
the pre-change code** — the write, the delete or the move lands in the decoy.

The sabotage is hung off `open_mutable` so it runs at the one moment the design
cares about: validation has produced its verdict and the tool has not yet acted
on it.
"""
import base64
import errno
import os
from pathlib import Path

import pytest

import src.mcp_server.tools as tools
from src.mcp_server.auth import current_permission
from src.services import vault as vault_service
from src.services import vault_fs


@pytest.fixture
def vault(monkeypatch, tmp_path):
    async def noop(*args, **kwargs):
        return None

    monkeypatch.setattr(vault_service.settings, "vault_path", str(tmp_path))
    monkeypatch.setattr(tools, "_log_usage", noop)
    vault_fs.reset_filesystem_probe_cache()
    token = current_permission.set("readwrite")
    yield tmp_path
    current_permission.reset(token)
    vault_fs.reset_filesystem_probe_cache()


@pytest.fixture
def sabotage(monkeypatch):
    """Run `action()` once, immediately after a target has been validated."""

    def install(action):
        original = tools.open_mutable
        fired: list[bool] = []

        def hooked(relative_path, user_id=None):
            target = original(relative_path, user_id=user_id)
            if not fired:
                fired.append(True)
                action()
            return target

        monkeypatch.setattr(tools, "open_mutable", hooked)
        return fired

    return install


def _swap_directory_for_a_link(real: Path, decoy: Path):
    """Rename `real` away and leave a symlink to `decoy` at its name.

    The #59 vector in one call: nothing about the *pathname* the tool validated
    has changed, but it now leads somewhere else entirely.
    """

    def action():
        moved = real.with_name(real.name + "-moved")
        os.rename(real, moved)
        real.symlink_to(decoy)

    return action


def _twin_directories(root: Path) -> tuple[Path, Path]:
    """`Real/note.md` and `Decoy/note.md`, byte-identical on purpose.

    Identical because that is what defeats `expected=`: an optimistic
    compare-and-swap against a decoy holding the same bytes passes.
    """
    (root / "Real").mkdir()
    (root / "Decoy").mkdir()
    (root / "Real" / "note.md").write_text("before\n", encoding="utf-8")
    (root / "Decoy" / "note.md").write_text("before\n", encoding="utf-8")
    return root / "Real", root / "Decoy"


# ── the parent directory renamed out from under a write ─────────────────────


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(
            lambda: tools.edit_note_impl("Real/note.md", "after\n"), id="edit_note"
        ),
        pytest.param(
            lambda: tools.edit_note_impl("Real/note.md", "tail", append=True),
            id="edit_note_append",
        ),
        pytest.param(
            lambda: tools.set_frontmatter_impl(
                "Real/note.md", updates={"status": "done"}
            ),
            id="set_frontmatter",
        ),
    ],
)
async def test_renaming_the_parent_mid_write_cannot_redirect_it(
    vault, sabotage, mutate
):
    real, decoy = _twin_directories(vault)
    fired = sabotage(_swap_directory_for_a_link(real, decoy))

    result = await mutate()

    assert fired, "the race never ran"
    assert "note.md" in result, result
    # The write landed in the directory validation opened, wherever its name
    # went…
    moved = vault / "Real-moved" / "note.md"
    assert moved.read_text(encoding="utf-8") != "before\n"
    # …and the decoy the name now points at was never touched.
    assert (decoy / "note.md").read_text(encoding="utf-8") == "before\n"


async def test_renaming_the_parent_mid_create_cannot_redirect_it(vault, sabotage):
    (vault / "Real").mkdir()
    (vault / "Decoy").mkdir()
    fired = sabotage(_swap_directory_for_a_link(vault / "Real", vault / "Decoy"))

    result = await tools.create_note_impl("Real/new.md", "body\n")

    assert fired
    assert "Created note" in result, result
    assert (vault / "Real-moved" / "new.md").read_text(encoding="utf-8") == "body\n"
    assert not (vault / "Decoy" / "new.md").exists()


async def test_renaming_the_parent_mid_raw_write_cannot_redirect_it(
    vault, sabotage
):
    (vault / "Real").mkdir()
    (vault / "Decoy").mkdir()
    fired = sabotage(_swap_directory_for_a_link(vault / "Real", vault / "Decoy"))

    result = await tools.write_file_impl(
        "Real/a.bin", base64.b64encode(b"bytes").decode()
    )

    assert fired
    assert "Wrote" in result, result
    assert (vault / "Real-moved" / "a.bin").read_bytes() == b"bytes"
    assert not (vault / "Decoy" / "a.bin").exists()


async def test_renaming_the_parent_mid_delete_cannot_redirect_it(vault, sabotage):
    real, decoy = _twin_directories(vault)
    fired = sabotage(_swap_directory_for_a_link(real, decoy))

    result = await tools.delete_note_impl("Real/note.md")

    assert fired
    assert "Soft-deleted" in result, result
    # The note that moved is the one that was validated…
    assert not (vault / "Real-moved" / "note.md").exists()
    # …and the decoy's note of the same name is still there.
    assert (decoy / "note.md").read_text(encoding="utf-8") == "before\n"
    trashed = list((vault / ".trash").iterdir())
    assert len(trashed) == 1
    assert trashed[0].read_text(encoding="utf-8") == "before\n"


async def test_renaming_the_parent_mid_permanent_delete_cannot_redirect_it(
    vault, sabotage
):
    real, decoy = _twin_directories(vault)
    fired = sabotage(_swap_directory_for_a_link(real, decoy))

    result = await tools.delete_note_impl("Real/note.md", permanent=True)

    assert fired
    assert "Permanently deleted" in result, result
    assert not (vault / "Real-moved" / "note.md").exists()
    assert (decoy / "note.md").read_text(encoding="utf-8") == "before\n"


async def test_renaming_the_source_parent_mid_move_cannot_redirect_it(
    vault, sabotage, monkeypatch
):
    real, decoy = _twin_directories(vault)

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def execute(self, statement):
            class Empty:
                def all(self):
                    return []

            return Empty()

        async def commit(self):
            return None

    monkeypatch.setattr(tools, "async_session", FakeSession)
    fired = sabotage(_swap_directory_for_a_link(real, decoy))

    result = await tools.move_note_impl("Real/note.md", "moved.md")

    assert fired
    assert "Moved" in result, result
    assert (vault / "moved.md").read_text(encoding="utf-8") == "before\n"
    # The decoy's copy is untouched: the move took the inode behind the
    # descriptor, not the one the pathname now reaches.
    assert (decoy / "note.md").read_text(encoding="utf-8") == "before\n"
    assert not (vault / "Real-moved" / "note.md").exists()


# ── the same hazard one level up: the vault root itself ─────────────────────


async def test_renaming_the_real_vault_root_mid_write_cannot_redirect_it(
    vault, sabotage, monkeypatch, tmp_path_factory
):
    """The vault root is a symlink and the directory *behind* it is renamed.

    Resolving the root once is not enough — the resolved pathname is still a
    pathname. Only the open root descriptor survives this.
    """
    roots = tmp_path_factory.mktemp("roots")
    real_root = roots / "real"
    decoy_root = roots / "decoy"
    real_root.mkdir()
    decoy_root.mkdir()
    (real_root / "note.md").write_text("before\n", encoding="utf-8")
    (decoy_root / "note.md").write_text("before\n", encoding="utf-8")
    link = roots / "vault"
    link.symlink_to(real_root)
    monkeypatch.setattr(vault_service.settings, "vault_path", str(link))

    fired = sabotage(_swap_directory_for_a_link(real_root, decoy_root))

    result = await tools.edit_note_impl("note.md", "after\n")

    assert fired
    assert "Updated note" in result, result
    assert (roots / "real-moved" / "note.md").read_text(
        encoding="utf-8"
    ) == "after\n"
    assert (decoy_root / "note.md").read_text(encoding="utf-8") == "before\n"


async def test_renaming_the_real_vault_root_mid_delete_cannot_redirect_the_trash(
    vault, sabotage, monkeypatch, tmp_path_factory
):
    """`.trash` is anchored to the same root descriptor as the note."""
    roots = tmp_path_factory.mktemp("roots")
    real_root = roots / "real"
    decoy_root = roots / "decoy"
    real_root.mkdir()
    decoy_root.mkdir()
    (real_root / "note.md").write_text("before\n", encoding="utf-8")
    link = roots / "vault"
    link.symlink_to(real_root)
    monkeypatch.setattr(vault_service.settings, "vault_path", str(link))

    fired = sabotage(_swap_directory_for_a_link(real_root, decoy_root))

    result = await tools.delete_note_impl("note.md")

    assert fired
    assert "Soft-deleted" in result, result
    assert (roots / "real-moved" / ".trash").is_dir()
    assert not (decoy_root / ".trash").exists()


# ── the temp file, the fsync, and the ordering around publication ───────────


def test_the_payload_is_fsynced_before_it_is_published(vault, monkeypatch):
    """Without the `fsync`, a crash just after the rename can publish a note
    whose data blocks never reached the disk — the truncation the atomic write
    exists to make impossible. The temp must also live in the destination
    directory, so the publish is a same-directory rename."""
    (vault / "Folder").mkdir()
    order: list[str] = []
    staged: list[list[str]] = []
    real_fsync = os.fsync
    real_replace = os.replace

    def recording_fsync(fd):
        order.append("fsync")
        staged.append(sorted(os.listdir(vault / "Folder")))
        return real_fsync(fd)

    def recording_replace(*args, **kwargs):
        order.append("publish")
        return real_replace(*args, **kwargs)

    monkeypatch.setattr(vault_service.os, "fsync", recording_fsync)
    monkeypatch.setattr(vault_service.os, "replace", recording_replace)

    vault_service.write_file("Folder/note.md", "body")

    assert order == ["fsync", "publish"]
    # Staged next to the destination, not in `.transfer-tmp` or /tmp — a
    # cross-directory publish could not be an atomic rename.
    assert any(name.startswith(".tmp-") for name in staged[0]), staged[0]
    # …and nothing is left behind afterwards.
    assert sorted(p.name for p in (vault / "Folder").iterdir()) == ["note.md"]


def test_a_crash_between_staging_and_publication_leaves_the_note_intact(
    vault, monkeypatch
):
    note = vault / "note.md"
    note.write_text("before\n", encoding="utf-8")

    def boom(*args, **kwargs):
        raise KeyboardInterrupt("killed mid-write")

    monkeypatch.setattr(vault_service.os, "replace", boom)

    with pytest.raises(KeyboardInterrupt):
        vault_service.write_file("note.md", "after\n")

    assert note.read_text(encoding="utf-8") == "before\n"
    assert [p.name for p in vault.iterdir()] == ["note.md"]


# ── no silent fallback when the filesystem cannot do it ─────────────────────


async def test_a_no_clobber_write_refuses_when_hard_links_are_unsupported(
    vault, monkeypatch
):
    """`create_note` must never degrade to a replacing rename."""
    (vault / "note.md").write_text("existing\n", encoding="utf-8")

    def refuse(*args, **kwargs):
        raise OSError(errno.EPERM, "operation not permitted")

    monkeypatch.setattr(vault_service.os, "link", refuse)

    result = await tools.create_note_impl("note.md", "ours\n")

    assert "hard link" in result.lower(), result
    assert (vault / "note.md").read_text(encoding="utf-8") == "existing\n"


async def test_soft_delete_refuses_when_rename_noreplace_is_unsupported(
    vault, monkeypatch
):
    (vault / "note.md").write_text("keep me\n", encoding="utf-8")
    monkeypatch.setattr(
        vault_fs, "_renameat2_raw", lambda *a, **k: errno.ENOSYS
    )

    result = await tools.delete_note_impl("note.md")

    assert "delete_file" in result or "non-replacing" in result, result
    assert (vault / "note.md").read_text(encoding="utf-8") == "keep me\n"


# ── unchanged guarantees ────────────────────────────────────────────────────


async def test_a_missing_note_still_does_not_create_the_trash(vault):
    result = await tools.delete_note_impl("gone.md")

    assert "not found" in result.lower(), result
    assert not (vault / ".trash").exists()


async def test_soft_delete_keeps_a_symlinked_folder_working(vault):
    (vault / "Real").mkdir()
    (vault / "Real" / "note.md").write_text("body\n", encoding="utf-8")
    (vault / "Shared").symlink_to(vault / "Real")

    result = await tools.delete_note_impl("Shared/note.md")

    assert "Soft-deleted" in result, result
    assert "Real/note.md" in result or "Shared/note.md" in result
    trashed = list((vault / ".trash").iterdir())
    assert len(trashed) == 1
    assert trashed[0].read_text(encoding="utf-8") == "body\n"


async def test_a_second_delete_of_the_same_name_gets_a_distinct_trash_entry(vault):
    for _ in range(2):
        (vault / "note.md").write_text("body\n", encoding="utf-8")
        assert "Soft-deleted" in await tools.delete_note_impl("note.md")

    assert len(list((vault / ".trash").iterdir())) == 2
