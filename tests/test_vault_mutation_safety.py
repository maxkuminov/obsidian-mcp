"""Regression tests for vault mutation races and path/link contracts."""

import os
from pathlib import Path

import pytest
from sqlalchemy import select, update

import src.mcp_server.tools as tools
from src.mcp_server.auth import current_permission
from src.services import vault as vault_service
from src.services import vault_fs
from src.models.db import NoteLink, NoteMetadata


@pytest.fixture(autouse=True)
def offline(monkeypatch, tmp_path):
    monkeypatch.setattr(tools.settings, "vault_path", str(tmp_path))

    async def noop(*args, **kwargs):
        return None

    monkeypatch.setattr(tools, "_log_usage", noop)
    token = current_permission.set("readwrite")
    yield tmp_path
    current_permission.reset(token)


def _race_the_publish(monkeypatch, contents: bytes):
    """Create the destination inside `os.link`, just before the real call runs.

    The publish is anchored now, so `destination` is a bare component resolved
    against `dst_dir_fd` — writing to `Path(destination)` would land in the
    process CWD and prove nothing.
    """
    real_link = os.link
    raced: list[bool] = []

    def concurrent_link(source, destination, *args, **kwargs):
        if not raced:
            raced.append(True)
            dst_dir_fd = kwargs.get("dst_dir_fd", kwargs.get("src_dir_fd"))
            fd = os.open(
                destination,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o644,
                dir_fd=dst_dir_fd,
            )
            try:
                os.write(fd, contents)
            finally:
                os.close(fd)
        return real_link(source, destination, *args, **kwargs)

    monkeypatch.setattr(vault_service.os, "link", concurrent_link)
    return raced


async def test_create_note_loses_race_without_clobber(offline, monkeypatch):
    _race_the_publish(monkeypatch, b"other writer")
    result = await tools.create_note_impl("race.md", "ours")
    assert "already exists" in result.lower()
    assert (offline / "race.md").read_text() == "other writer"


def test_raw_write_loses_race_without_clobber(offline, monkeypatch):
    _race_the_publish(monkeypatch, b"other writer")
    with pytest.raises(FileExistsError):
        vault_service.write_bytes("race.bin", b"ours", overwrite=False)
    assert (offline / "race.bin").read_bytes() == b"other writer"


async def test_move_loses_race_without_clobber(offline, monkeypatch):
    """`move_note` publishes with `RENAME_NOREPLACE`, so a destination that
    appears after validation is preserved and the source stays put."""
    source = offline / "source.md"
    source.write_text("source", encoding="utf-8")
    destination = offline / "destination.md"
    real = vault_fs.rename_noreplace
    raced: list[bool] = []

    def concurrent(src_dir_fd, src_name, dst_dir_fd, dst_name):
        if not raced:
            raced.append(True)
            fd = os.open(
                dst_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o644,
                dir_fd=dst_dir_fd,
            )
            try:
                os.write(fd, b"other writer")
            finally:
                os.close(fd)
        return real(src_dir_fd, src_name, dst_dir_fd, dst_name)

    monkeypatch.setattr(vault_fs, "rename_noreplace", concurrent)
    result = await tools.move_note_impl("source.md", "destination.md")

    assert raced
    assert "already exists" in result.lower(), result
    assert source.read_text() == "source"
    assert destination.read_text() == "other writer"


async def test_soft_delete_retries_concurrent_trash_collision(
    offline, monkeypatch
):
    source = offline / "note.md"
    source.write_text("deleted note", encoding="utf-8")
    real = vault_fs.rename_noreplace
    collided: list[str] = []

    def concurrent(src_dir_fd, src_name, dst_dir_fd, dst_name):
        if not collided:
            collided.append(dst_name)
            fd = os.open(
                dst_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o644,
                dir_fd=dst_dir_fd,
            )
            try:
                os.write(fd, b"other trash")
            finally:
                os.close(fd)
        return real(src_dir_fd, src_name, dst_dir_fd, dst_name)

    monkeypatch.setattr(vault_fs, "rename_noreplace", concurrent)
    result = await tools.delete_note_impl("note.md")

    assert "Soft-deleted" in result, result
    assert collided
    collided_destination = offline / ".trash" / collided[0]
    assert collided_destination.read_text() == "other trash"
    trash_files = sorted((offline / ".trash").iterdir())
    assert len(trash_files) == 2
    moved = next(path for path in trash_files if path != collided_destination)
    assert moved.read_text() == "deleted note"
    assert not source.exists()


@pytest.mark.parametrize("operation", ["edit", "frontmatter"])
async def test_note_mutations_reject_hidden_paths(offline, operation):
    hidden = offline / ".obsidian"
    hidden.mkdir()
    (hidden / "config.md").write_text("secret", encoding="utf-8")
    if operation == "edit":
        result = await tools.edit_note_impl(".obsidian/config.md", "changed")
    else:
        result = await tools.set_frontmatter_impl(
            ".obsidian/config.md", updates={"changed": True}
        )
    assert "hidden path denied" in result.lower()
    assert (hidden / "config.md").read_text() == "secret"


async def test_edit_detects_intervening_change(offline, monkeypatch):
    note = offline / "note.md"
    note.write_text("before", encoding="utf-8")
    real_write = tools.write_file_at

    def concurrent_write(*args, **kwargs):
        note.write_text("external edit", encoding="utf-8")
        return real_write(*args, **kwargs)

    monkeypatch.setattr(tools, "write_file_at", concurrent_write)
    result = await tools.edit_note_impl("note.md", "ours")
    assert "changed while editing" in result.lower()
    assert note.read_text() == "external edit"


async def test_frontmatter_detects_intervening_change(offline, monkeypatch):
    note = offline / "note.md"
    note.write_text("---\nstatus: old\n---\nbody\n", encoding="utf-8")
    real_write = tools.write_file_at

    def concurrent_write(*args, **kwargs):
        note.write_text("external edit", encoding="utf-8")
        return real_write(*args, **kwargs)

    monkeypatch.setattr(tools, "write_file_at", concurrent_write)
    result = await tools.set_frontmatter_impl(
        "note.md", updates={"status": "ours"}
    )
    assert "changed while editing" in result.lower()
    assert note.read_text() == "external edit"


def test_markdown_link_rewrite_is_relative_to_source_folder():
    index = {
        "paths": {"old/target.md": 7},
        "stems": {"target": [("old/target.md", 7)]},
    }
    content = "[target](../old/target.md#section)"
    rewritten, count = tools._rewrite_links_in_text(
        content,
        "old/target.md",
        "new/deeper/target.md",
        "sources/note.md",
        index,
    )
    assert count == 1
    assert rewritten == "[target](../new/deeper/target.md#section)"


def test_moved_note_path_style_self_link_is_rewritten():
    index = {
        "paths": {"old/target.md": 7},
        "stems": {"target": [("old/target.md", 7)]},
    }
    rewritten, count = tools._rewrite_links_in_text(
        "Self: [[old/target#section]]",
        "old/target.md",
        "new/renamed.md",
        "old/target.md",
        index,
    )
    assert count == 1
    assert rewritten == "Self: [[new/renamed#section]]"


def test_moved_note_markdown_self_link_is_relative_to_new_folder():
    index = {
        "paths": {"old/folder/target.md": 7},
        "stems": {"target": [("old/folder/target.md", 7)]},
    }
    rewritten, count = tools._rewrite_links_in_text(
        "Self: [target](target.md#section)",
        "old/folder/target.md",
        "new/deeper/renamed.md",
        "old/folder/target.md",
        index,
        output_source_path="new/deeper/renamed.md",
    )
    assert count == 1
    assert rewritten == "Self: [target](renamed.md#section)"


def test_move_rewrite_failure_warning_reports_partial_success():
    warning = tools._rewrite_failure_warning(
        ["sources/one.md", "sources/two.md"]
    )
    assert warning is not None
    assert "partial success: note moved" in warning
    assert "link rewrites failed in 2 note(s)" in warning
    assert "sources/one.md" in warning


def test_single_user_move_queries_are_explicitly_null_owned():
    metadata_select = select(NoteMetadata.id).where(
        tools._note_owner_predicate(None)
    )
    owned_ids = select(NoteMetadata.id).where(tools._note_owner_predicate(None))
    link_update = update(NoteLink).where(NoteLink.source_note_id.in_(owned_ids))
    metadata_sql = str(metadata_select.compile())
    link_sql = str(link_update.compile())
    assert "notes_metadata.user_id IS NULL" in metadata_sql
    assert "notes_metadata.user_id IS NULL" in link_sql


def test_stale_index_still_rewrites_moved_note_self_link():
    index = {"paths": {}, "stems": {}}
    tools._ensure_move_source_in_index(index, "old/target.md")
    rewritten, count = tools._rewrite_links_in_text(
        "Self: [[old/target]]",
        "old/target.md",
        "new/target.md",
        "old/target.md",
        index,
        output_source_path="new/target.md",
    )
    assert count == 1
    assert rewritten == "Self: [[new/target]]"


async def test_move_end_to_end_scopes_null_owner_and_rewrites_unindexed_self_link(
    offline, monkeypatch
):
    source = offline / "old" / "target.md"
    source.parent.mkdir()
    source.write_text("Self: [[old/target]]", encoding="utf-8")
    statements = []

    class EmptyResult:
        def all(self):
            return []

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def execute(self, statement):
            statements.append(statement)
            return EmptyResult()

        async def commit(self):
            return None

    monkeypatch.setattr(tools, "async_session", FakeSession)
    result = await tools.move_note_impl(
        "old/target.md", "new/target.md", rewrite_links=True
    )

    assert "Moved" in result
    assert (offline / "new" / "target.md").read_text() == "Self: [[new/target]]"
    sql = [str(statement.compile()) for statement in statements]
    assert len(sql) == 4  # metadata index, backlinks, metadata update, link update
    assert all("notes_metadata.user_id IS NULL" in query for query in sql)
    assert "source_note_id IN" in sql[3]


def test_bounded_read_uses_open_inode_when_path_is_swapped(offline, monkeypatch):
    path = offline / "data.bin"
    path.write_bytes(b"small")
    real_fstat = os.fstat
    swapped = False

    def fstat_then_swap(fd):
        nonlocal swapped
        info = real_fstat(fd)
        if not swapped:
            swapped = True
            replacement = offline / "replacement"
            replacement.write_bytes(b"x" * 100)
            os.replace(replacement, path)
        return info

    monkeypatch.setattr(vault_service.os, "fstat", fstat_then_swap)
    assert vault_service.read_bytes("data.bin", max_bytes=10) == b"small"


def test_planted_temp_symlink_cannot_be_written_through(offline, monkeypatch):
    """A symlink squatting on the temp name never receives the write.

    `_atomic_write_at` used to stage through `Path.write_bytes`, which follows a
    symlink at the temp name: a process that guessed the name could point it at
    any file the server can write and have the note's bytes truncate it.
    Exclusive `O_CREAT|O_EXCL|O_NOFOLLOW` creation makes that name unusable, so
    the write takes the next candidate and still succeeds.
    """
    decoy = offline / "decoy.txt"
    decoy.write_text("do not clobber", encoding="utf-8")

    names = iter(["planted", "second", "third"])

    def next_candidate(name: str) -> str:
        return f".tmp-{next(names)}"

    monkeypatch.setattr(vault_service, "_temp_candidate", next_candidate)
    planted = offline / ".tmp-planted"
    planted.symlink_to(decoy)

    vault_service.write_file("note.md", "fresh content")

    assert decoy.read_text() == "do not clobber"
    assert (offline / "note.md").read_text() == "fresh content"
    # The squatted name is left exactly as it was — untouched, not published.
    assert planted.is_symlink()
    assert not (offline / ".tmp-second").exists()


def test_planted_temp_symlink_exhausting_every_candidate_fails_closed(
    offline, monkeypatch
):
    monkeypatch.setattr(vault_service, "_temp_candidate", lambda name: ".tmp-fixed")
    decoy = offline / "decoy.txt"
    decoy.write_text("do not clobber", encoding="utf-8")
    (offline / ".tmp-fixed").symlink_to(decoy)

    with pytest.raises(RuntimeError, match="temporary file"):
        vault_service.write_file("note.md", "fresh content")

    assert decoy.read_text() == "do not clobber"
    assert not (offline / "note.md").exists()


def test_written_note_keeps_the_umask_default_mode(offline):
    """Staging at 0600 must not leak into the published note's permissions."""
    vault_service.write_file("mode.md", "body")
    reference = offline / "reference.md"
    reference.write_text("body", encoding="utf-8")

    published = (offline / "mode.md").stat().st_mode & 0o777
    assert published == reference.stat().st_mode & 0o777
    assert published == vault_service._default_file_mode()
