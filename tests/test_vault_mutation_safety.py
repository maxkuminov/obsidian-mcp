"""Regression tests for vault mutation races and path/link contracts."""

import os
from pathlib import Path

import pytest

import src.mcp_server.tools as tools
from src.mcp_server.auth import current_permission
from src.services import vault as vault_service


@pytest.fixture(autouse=True)
def offline(monkeypatch, tmp_path):
    monkeypatch.setattr(tools.settings, "vault_path", str(tmp_path))

    async def noop(*args, **kwargs):
        return None

    monkeypatch.setattr(tools, "_log_usage", noop)
    token = current_permission.set("readwrite")
    yield tmp_path
    current_permission.reset(token)


async def test_create_note_loses_race_without_clobber(offline, monkeypatch):
    real_link = os.link
    raced = False

    def concurrent_link(source, destination, *args, **kwargs):
        nonlocal raced
        if not raced:
            raced = True
            Path(destination).write_text("other writer", encoding="utf-8")
        return real_link(source, destination, *args, **kwargs)

    monkeypatch.setattr(vault_service.os, "link", concurrent_link)
    result = await tools.create_note_impl("race.md", "ours")
    assert "already exists" in result.lower()
    assert (offline / "race.md").read_text() == "other writer"


def test_raw_write_loses_race_without_clobber(offline, monkeypatch):
    real_link = os.link

    def concurrent_link(source, destination, *args, **kwargs):
        Path(destination).write_bytes(b"other writer")
        return real_link(source, destination, *args, **kwargs)

    monkeypatch.setattr(vault_service.os, "link", concurrent_link)
    with pytest.raises(FileExistsError):
        vault_service.write_bytes("race.bin", b"ours", overwrite=False)
    assert (offline / "race.bin").read_bytes() == b"other writer"


def test_move_loses_race_without_clobber(offline, monkeypatch):
    source = offline / "source.md"
    destination = offline / "destination.md"
    source.write_text("source", encoding="utf-8")
    real_link = os.link

    def concurrent_link(src, dst, *args, **kwargs):
        Path(dst).write_text("other writer", encoding="utf-8")
        return real_link(src, dst, *args, **kwargs)

    monkeypatch.setattr(vault_service.os, "link", concurrent_link)
    with pytest.raises(FileExistsError):
        vault_service.move_no_clobber(source, destination)
    assert source.read_text() == "source"
    assert destination.read_text() == "other writer"


async def test_soft_delete_retries_concurrent_trash_collision(
    offline, monkeypatch
):
    source = offline / "note.md"
    source.write_text("deleted note", encoding="utf-8")
    real_link = os.link
    collided_destination: Path | None = None

    def concurrent_link(src, dst, *args, **kwargs):
        nonlocal collided_destination
        if collided_destination is None:
            collided_destination = Path(dst)
            collided_destination.write_text("other trash", encoding="utf-8")
        return real_link(src, dst, *args, **kwargs)

    monkeypatch.setattr(vault_service.os, "link", concurrent_link)
    result = await tools.delete_note_impl("note.md")

    assert "Soft-deleted" in result
    assert collided_destination is not None
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
    real_write = tools.write_file

    def concurrent_write(*args, **kwargs):
        note.write_text("external edit", encoding="utf-8")
        return real_write(*args, **kwargs)

    monkeypatch.setattr(tools, "write_file", concurrent_write)
    result = await tools.edit_note_impl("note.md", "ours")
    assert "changed while editing" in result.lower()
    assert note.read_text() == "external edit"


async def test_frontmatter_detects_intervening_change(offline, monkeypatch):
    note = offline / "note.md"
    note.write_text("---\nstatus: old\n---\nbody\n", encoding="utf-8")
    real_write = tools.write_file

    def concurrent_write(*args, **kwargs):
        note.write_text("external edit", encoding="utf-8")
        return real_write(*args, **kwargs)

    monkeypatch.setattr(tools, "write_file", concurrent_write)
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
