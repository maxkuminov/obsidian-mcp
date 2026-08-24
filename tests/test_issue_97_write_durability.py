"""Staged bytes and their publication are made durable, on both paths (#97).

Nothing on the transfer path ever called `fsync`, and the note path flushed the
payload and not the directory. After a crash the two halves are independent: the
directory entry can be durable while the contents are not, or the contents can be
durable with no entry naming them at all. Either way the vault ends up
contradicting something the server has already told an agent — a `completed`
upload whose `sha256` the bytes no longer match, or a `create_note` that reported
success and left no note.

The property under test is therefore an **ordering**, not an end state: which
flush happens, relative to which publication, and — because it decides what an
upload route does with a failed one — on which side of the publish gate.

**The two paths fail in opposite directions and both directions are asserted
here (D18).** A failed directory flush on the transfer path strands the token and
surfaces as `PostPublishFailure`, because the source bytes are gone and the
ambiguity has to reach the human. The same failure on the note path is logged and
the write reported as the success it is, because a note tool that reports a false
failure gets *retried* — and `edit_note(append=True)` retried after a write that
landed appends the same block twice. Reporting a false failure there manufactures
a destructive outcome; on the transfer path it merely wastes a link.
"""
from __future__ import annotations

import asyncio
import base64
import errno
import logging
import os
import stat
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

import src.mcp_server.tools as tools
from src.mcp_server.auth import current_permission
from src.services import transfer, vault_fs
from src.services import vault as vault_service
from src.services.transfer import stream_to_vault


# ── shared scaffolding ──────────────────────────────────────────────────────


@dataclass
class FakeRow:
    """The four fields `stream_to_vault` actually reads off a token row."""

    vault_root: str
    path: str
    overwrite: bool = False
    expected_fingerprint: dict | None = None


async def chunks_of(*parts: bytes):
    for part in parts:
        yield part


def deadline_in(seconds: float) -> float:
    return time.monotonic() + seconds


def temps_under(directory: Path) -> list[str]:
    return [str(p.relative_to(directory)) for p in directory.rglob(".tmp-*")]


def prime_publication_probe(directory: Path) -> None:
    """Run and cache the publication probe before a recorder is installed.

    In production the probe has always run by the time a body is streamed — the
    mint tools and the upload route all call it before a token is handed out,
    and since #92 item 1 it is also what decides the root's staging mode. These
    tests call `stream_to_vault` directly with a `FakeRow`, so without this the
    probe's own `fsync`s and `link` would land in the recording and the
    assertion would be about the probe rather than about the upload.
    """
    vault_fs.check_publication_support(directory)


@pytest.fixture
def vault(tmp_path):
    (tmp_path / "Attachments").mkdir()
    vault_fs.reset_filesystem_probe_cache()
    yield tmp_path
    vault_fs.reset_filesystem_probe_cache()


@pytest.fixture
def note_vault(monkeypatch, tmp_path):
    """A vault the note tools write into, with usage logging stubbed out."""

    async def noop(*args, **kwargs):
        return None

    monkeypatch.setattr(vault_service.settings, "vault_path", str(tmp_path))
    monkeypatch.setattr(tools, "_log_usage", noop)
    vault_fs.reset_filesystem_probe_cache()
    token = current_permission.set("readwrite")
    yield tmp_path
    current_permission.reset(token)
    vault_fs.reset_filesystem_probe_cache()


def _is_dir_fd(fd: int) -> bool:
    return stat.S_ISDIR(os.fstat(fd).st_mode)


def _dir_flush_recorder(monkeypatch, vault_root: Path) -> list[str]:
    """Record the vault-relative path of every directory that gets flushed.

    `/proc/self/fd/<n>` is how a descriptor is asked which directory it is —
    the point of the ancestor flush is *which* directories are made durable, and
    a bare count cannot tell `New` from the root.
    """
    seen: list[str] = []
    real = os.fsync

    def record(fd):
        if _is_dir_fd(fd):
            try:
                where = Path(os.readlink(f"/proc/self/fd/{fd}"))
            except OSError:  # pragma: no cover - defensive
                where = Path("?")
            try:
                seen.append(str(where.relative_to(vault_root.resolve())))
            except ValueError:  # pragma: no cover - a directory outside the vault
                seen.append(str(where))
        return real(fd)

    monkeypatch.setattr(os, "fsync", record)
    return seen


def _fail_dir_fsync(monkeypatch, code: int = errno.EIO) -> list[int]:
    """Make every *directory* `fsync` fail; leave payload flushes working."""
    failed: list[int] = []
    real = os.fsync

    def maybe_fail(fd):
        if _is_dir_fd(fd):
            failed.append(fd)
            raise OSError(code, os.strerror(code))
        return real(fd)

    monkeypatch.setattr(os, "fsync", maybe_fail)
    return failed


# ════════════════════════════════════════════════════════════════════════════
# 2.1 / 2.2 / 2.3 — the transfer path's payload flush
# ════════════════════════════════════════════════════════════════════════════


class _Gate:
    """Minimal stand-in for the locked pre-publication transaction."""

    def __init__(self):
        self.entered = 0

    def __call__(self):
        return self

    async def __aenter__(self):
        self.entered += 1

        async def record(result, published):
            return None

        return transfer.GateHandle(ok=True, session=self, on_complete=record)

    async def __aexit__(self, *exc):
        return False


async def test_the_payload_is_flushed_after_the_body_and_before_the_gate(
    vault, monkeypatch
):
    """Both halves of the placement, in one ordering.

    *After the body*, or the flush would promise durability for bytes that had
    not arrived. *Before the gate*, because the gate holds `SELECT … FOR UPDATE`
    on the token, the credential and the user across the publish — every
    revocation of that credential would otherwise queue behind up to 25 MB of
    device I/O.
    """
    order: list[str] = []
    real = transfer._fsync_payload

    def record(fd):
        order.append("payload-flush")
        return real(fd)

    monkeypatch.setattr(transfer, "_fsync_payload", record)

    class Gate(_Gate):
        async def __aenter__(self):
            order.append("gate")
            return await super().__aenter__()

    async def watched():
        yield b"one"
        order.append("chunk")
        yield b"two"
        order.append("chunk")

    row = FakeRow(str(vault), "Attachments/a.bin")
    await stream_to_vault(
        row, watched(), max_bytes=100, deadline=deadline_in(30), before_publish=Gate()
    )

    assert order == ["chunk", "chunk", "payload-flush", "gate"], order
    assert (vault / "Attachments" / "a.bin").read_bytes() == b"onetwo"


async def test_the_payload_flush_does_not_block_the_event_loop(vault, monkeypatch):
    """A 25 MB `fsync` is a different order of stall from a per-chunk write.

    Two independent proofs, because either alone is weak: the flush runs on a
    *different thread*, and the loop demonstrably keeps running while it is in
    progress — a ticker scheduled beforehand advances before the flush returns.
    """
    main_thread = threading.get_ident()
    ran_on: list[int] = []
    release = threading.Event()
    ticks = 0

    async def ticker():
        nonlocal ticks
        while True:
            await asyncio.sleep(0.005)
            ticks += 1
            if ticks >= 3:
                release.set()

    real = transfer._fsync_payload

    def slow(fd):
        ran_on.append(threading.get_ident())
        # Returns only once the loop has proved it is still turning.
        release.wait(timeout=5)
        return real(fd)

    monkeypatch.setattr(transfer, "_fsync_payload", slow)

    beat = asyncio.ensure_future(ticker())
    try:
        row = FakeRow(str(vault), "Attachments/a.bin")
        await stream_to_vault(
            row, chunks_of(b"payload"), max_bytes=100, deadline=deadline_in(30)
        )
    finally:
        beat.cancel()

    assert ran_on and main_thread not in ran_on, "the flush ran on the event loop"
    assert ticks >= 3, "the loop was blocked for the duration of the flush"
    assert (vault / "Attachments" / "a.bin").read_bytes() == b"payload"


async def test_a_failing_payload_flush_publishes_nothing_and_is_pre_publication(
    vault, monkeypatch
):
    """Nothing has been linked into place, so this must not be dressed up as a
    post-publication failure — the route reads that as "strand the token"."""

    def boom(fd):
        raise OSError(errno.EIO, "flush failed")

    monkeypatch.setattr(transfer, "_fsync_payload", boom)
    gate = _Gate()
    row = FakeRow(str(vault), "Attachments/a.bin")

    with pytest.raises(OSError) as exc:
        await stream_to_vault(
            row,
            chunks_of(b"payload"),
            max_bytes=100,
            deadline=deadline_in(30),
            before_publish=gate,
        )

    assert not isinstance(exc.value, transfer.PostPublishFailure)
    assert gate.entered == 0, "the gate was opened despite a failed payload flush"
    assert not (vault / "Attachments" / "a.bin").exists()
    assert temps_under(vault) == [], "the staged bytes were left behind"


# ════════════════════════════════════════════════════════════════════════════
# 2.4 / 2.5 — the transfer path's directory flush
# ════════════════════════════════════════════════════════════════════════════


async def test_the_destination_directory_is_flushed_after_publication(
    vault, monkeypatch
):
    prime_publication_probe(vault)
    order: list[str] = []
    real_fsync = os.fsync
    real_link = os.link

    def record_fsync(fd):
        order.append("dir-flush" if _is_dir_fd(fd) else "payload-flush")
        return real_fsync(fd)

    def record_link(*args, **kwargs):
        order.append("publish")
        return real_link(*args, **kwargs)

    monkeypatch.setattr(os, "fsync", record_fsync)
    monkeypatch.setattr(os, "link", record_link)

    row = FakeRow(str(vault), "Attachments/a.bin")
    await stream_to_vault(
        row, chunks_of(b"payload"), max_bytes=100, deadline=deadline_in(30)
    )

    # Two directory flushes after the publish: the destination parent, then the
    # chain above it up to the root. The *ordering* is what this test is for —
    # every directory flush follows the publish and the payload's precedes it.
    assert order[:2] == ["payload-flush", "publish"], order
    assert set(order[2:]) == {"dir-flush"} and len(order) > 2, order


async def test_a_failing_directory_flush_is_a_post_publication_failure(
    vault, monkeypatch
):
    """The classification *is* the feature.

    A bare `OSError` out of `stream_to_vault` means "demonstrably
    pre-publication" to the upload route, which answers by releasing the claim —
    handing back a replayable token over a path that already holds the file.
    """
    prime_publication_probe(vault)
    _fail_dir_fsync(monkeypatch)
    row = FakeRow(str(vault), "Attachments/a.bin")

    with pytest.raises(transfer.PostPublishFailure):
        await stream_to_vault(
            row,
            chunks_of(b"payload"),
            max_bytes=100,
            deadline=deadline_in(30),
            before_publish=_Gate(),
        )

    # The bytes are there — that is exactly why the failure may not be read as
    # "nothing was published".
    assert (vault / "Attachments" / "a.bin").read_bytes() == b"payload"
    assert temps_under(vault) == []


async def test_directories_the_upload_created_are_flushed_outward(vault, monkeypatch):
    """Flushing the destination alone leaves the entry that *names* it unflushed.

    A crash then loses the whole new folder, and with it a file `check_upload`
    has already reported `completed`.
    """
    prime_publication_probe(vault)
    seen = _dir_flush_recorder(monkeypatch, vault)
    row = FakeRow(str(vault), "New/Folder/a.bin")

    await stream_to_vault(
        row, chunks_of(b"payload"), max_bytes=100, deadline=deadline_in(30)
    )

    assert set(seen) == {"New/Folder", "New", "."}, seen
    assert (vault / "New" / "Folder" / "a.bin").read_bytes() == b"payload"


async def test_the_whole_chain_above_the_destination_is_flushed(vault, monkeypatch):
    """Not only the directories this call created — the chain to the root.

    The earlier rule flushed the destination parent plus whatever *this call*
    made, which looks precise and is not durable across an abort: an upload that
    creates a folder and then dies before publication flushes nothing (rightly,
    it published nothing), and the retry finds the folder there, records no
    creation, and would leave the entry naming it durable nowhere. The walk is
    bounded by path depth, not by vault size, and a directory `fsync` is
    metadata-only. See `vault_fs.publication_flush_dirs`.
    """
    prime_publication_probe(vault)
    seen = _dir_flush_recorder(monkeypatch, vault)
    row = FakeRow(str(vault), "Attachments/a.bin")

    await stream_to_vault(
        row, chunks_of(b"payload"), max_bytes=100, deadline=deadline_in(30)
    )

    assert seen == ["Attachments", "."], seen


# ════════════════════════════════════════════════════════════════════════════
# 2.7 — `import_from_url` inherits all of it through `stream_to_vault`
# ════════════════════════════════════════════════════════════════════════════


async def test_import_from_url_shares_the_streaming_publish(vault, monkeypatch):
    """`import_from_url` has no token, but it holds a network stream open for up
    to 30 s and publishes through the same helper — so it gets the same two
    flushes and the same classification, without a second implementation."""
    order: list[str] = []
    real_fsync = os.fsync
    payload_flushes: list[int] = []

    def record(fd):
        order.append("dir-flush" if _is_dir_fd(fd) else "payload-flush")
        return real_fsync(fd)

    def record_payload(fd):
        payload_flushes.append(fd)
        order.append("payload-flush")
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", record)
    monkeypatch.setattr(transfer, "_fsync_payload", record_payload)

    class Gate(_Gate):
        async def __aenter__(self):
            order.append("gate")
            return await super().__aenter__()

    row = FakeRow(str(vault), "New/Imported/a.bin")
    await stream_to_vault(
        row,
        chunks_of(b"fetched"),
        max_bytes=100,
        deadline=deadline_in(30),
        before_publish=Gate(),
    )

    assert payload_flushes, "the fetched body was published without being flushed"
    assert order.index("payload-flush") < order.index("gate")
    assert order[-1] == "dir-flush"
    assert (vault / "New" / "Imported" / "a.bin").read_bytes() == b"fetched"


# ════════════════════════════════════════════════════════════════════════════
# 2.5a — the probe refuses a filesystem that cannot flush, before any mint
# ════════════════════════════════════════════════════════════════════════════


def _refuse_fsync(monkeypatch, *, on_dirs: bool):
    real = os.fsync

    def refuse(fd):
        if _is_dir_fd(fd) is on_dirs:
            raise OSError(errno.EINVAL, "not supported")
        return real(fd)

    monkeypatch.setattr(os, "fsync", refuse)


@pytest.mark.parametrize("on_dirs", [True, False])
def test_the_publication_probe_refuses_a_filesystem_that_cannot_flush(
    vault, monkeypatch, on_dirs
):
    """The probe is where an environment that cannot do this gets refused.

    A filesystem that hard-links happily and rejects a directory `fsync` would
    otherwise pass the probe, take a token, accept a whole body, publish it —
    and only then strand the claim on a post-publication failure, which is the
    one outcome the transfer path deliberately cannot undo.
    """
    _refuse_fsync(monkeypatch, on_dirs=on_dirs)

    with pytest.raises(vault_fs.UnsupportedFilesystem) as exc:
        vault_fs.check_publication_support(vault)

    assert "durable storage" in str(exc.value)
    assert ("a directory" in str(exc.value)) is on_dirs
    assert temps_under(vault) == [], "the probe left its temp file behind"


def test_a_sick_device_is_not_reported_as_an_unsupported_filesystem(vault, monkeypatch):
    """`EIO` is a failing disk, not a filesystem that cannot do this. Telling an
    operator to change filesystems in response would be the wrong instruction."""
    real = os.fsync

    def io_error(fd):
        if _is_dir_fd(fd):
            raise OSError(errno.EIO, "input/output error")
        return real(fd)

    monkeypatch.setattr(os, "fsync", io_error)

    with pytest.raises(OSError) as exc:
        vault_fs.check_publication_support(vault)
    assert not isinstance(exc.value, vault_fs.UnsupportedFilesystem)


# ════════════════════════════════════════════════════════════════════════════
# 2.6 / 2.6a / 2.6b — the note path, and its opposite failure direction
# ════════════════════════════════════════════════════════════════════════════


NOTE_WRITES = [
    pytest.param(
        lambda: tools.create_note_impl("New/Folder/x.md", "body\n"),
        "New/Folder/x.md",
        id="create_note",
    ),
    pytest.param(
        lambda: tools.write_file_impl(
            "New/Folder/x.md", base64.b64encode(b"body\n").decode()
        ),
        "New/Folder/x.md",
        id="write_file-no-clobber",
    ),
    pytest.param(
        lambda: tools.write_file_impl(
            "New/Folder/x.md",
            base64.b64encode(b"body\n").decode(),
            overwrite=True,
        ),
        "New/Folder/x.md",
        id="write_file-overwrite",
    ),
]


@pytest.mark.parametrize("write,path", NOTE_WRITES)
async def test_a_note_write_flushes_its_directory_and_the_folders_it_created(
    note_vault, monkeypatch, write, path
):
    """2.6b: the flush belongs to the shared helper, so `write_file` gets it in
    **both** publication modes — an implementation that satisfied the note-tool
    list literally and skipped the raw-byte write would leave the one path that
    carries a human's file undurable."""
    seen = _dir_flush_recorder(monkeypatch, note_vault)

    result = await write()

    assert "Created note" in result or "Wrote" in result, result
    assert set(seen) == {"New/Folder", "New", "."}, seen
    assert (note_vault / path).read_bytes() == b"body\n"


async def test_an_overwrite_of_an_existing_note_flushes_its_directory(
    note_vault, monkeypatch
):
    (note_vault / "note.md").write_text("before\n", encoding="utf-8")
    seen = _dir_flush_recorder(monkeypatch, note_vault)

    await tools.edit_note_impl("note.md", "after\n")

    assert seen == ["."], seen
    assert (note_vault / "note.md").read_text(encoding="utf-8") == "after\n"


@pytest.mark.parametrize("write,path", NOTE_WRITES)
async def test_a_failed_directory_flush_never_turns_a_landed_note_write_into_a_failure(
    note_vault, monkeypatch, caplog, write, path
):
    """D18, the direction that matters most.

    The payload is already durable and the note is already at the path. A tool
    that answered "the write failed" here would get *retried*, and
    `edit_note(append=True)` retried after a write that landed appends the same
    block twice — a false failure on this path manufactures the destructive
    outcome the whole module exists to prevent.
    """
    failed = _fail_dir_fsync(monkeypatch)

    with caplog.at_level("WARNING"):
        result = await write()

    assert failed, "no directory flush was attempted"
    assert "Created note" in result or "Wrote" in result, result
    assert (note_vault / path).read_bytes() == b"body\n"
    assert any("could not flush" in r.getMessage() for r in caplog.records)


async def test_a_failed_ancestor_flush_is_logged_and_not_reported(
    note_vault, monkeypatch, caplog
):
    """Same direction for the outward walk: `New/Folder` is durable, the entry
    naming `New` is not, and the note is still on disk and still reported."""
    real = os.fsync
    calls: list[int] = []

    def fail_after_the_first_dir(fd):
        if _is_dir_fd(fd):
            calls.append(fd)
            if len(calls) > 1:
                raise OSError(errno.EIO, "input/output error")
        return real(fd)

    monkeypatch.setattr(os, "fsync", fail_after_the_first_dir)

    with caplog.at_level("WARNING"):
        result = await tools.create_note_impl("New/Folder/x.md", "body\n")

    assert len(calls) > 1, "the ancestor flush never ran"
    assert "Created note" in result, result
    assert (note_vault / "New" / "Folder" / "x.md").read_text() == "body\n"
    assert any("could not flush" in r.getMessage() for r in caplog.records)


async def test_a_note_write_into_an_existing_folder_flushes_the_chain(
    note_vault, monkeypatch
):
    """`ensure_parent` has the same abort shape as an upload — a write that
    creates a folder and then fails leaves it behind unflushed, and the retry
    records no creations — so the note path takes the same chain rule."""
    (note_vault / "Folder").mkdir()
    seen = _dir_flush_recorder(monkeypatch, note_vault)

    await tools.create_note_impl("Folder/x.md", "body\n")

    assert seen == ["Folder", "."], seen


async def test_a_folder_another_writer_created_is_flushed_anyway(
    note_vault, monkeypatch
):
    """Provenance is still recorded, and it is **no longer** what decides.

    `ensure_parent` lists only what it made — a component another creator won
    the race to is not listed. But the flush no longer follows that list: an
    entry somebody else created and never flushed is exactly the case the chain
    rule exists for, and from here it is indistinguishable from the abort-retry
    shape. So `New` is flushed even though this call did not make it.
    """
    (note_vault / "New").mkdir()
    seen = _dir_flush_recorder(monkeypatch, note_vault)

    await tools.create_note_impl("New/Folder/x.md", "body\n")

    assert seen == ["New/Folder", "New", "."], seen


# ════════════════════════════════════════════════════════════════════════════
# The rename publications: move, soft delete, permanent unlink
#
# A note tool publishes in three ways, and the staged-payload helper is only
# one of them. `renameat2` writes **two** directory entries at once — the new
# name and the removal of the old — so a crash that makes only one of them
# durable leaves the note duplicated or gone; an `unlink` writes one, and an
# entry that survives resurrects a note the agent was told is deleted. All of
# these take D18's direction: the rename already happened, and a tool that
# reported it as failed would be retried against a source that is no longer
# there.
# ════════════════════════════════════════════════════════════════════════════


def _no_db(monkeypatch) -> list:
    """`async_session` stand-in: the index write is not what is under test."""
    executed: list = []

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def execute(self, statement):
            executed.append(statement)

            class Empty:
                def all(self):
                    return []

            return Empty()

        async def commit(self):
            return None

    monkeypatch.setattr(tools, "async_session", FakeSession)
    return executed


def _swap_in_a_directory(path: Path) -> None:
    """Replace `path` with a directory holding a file nobody asked to delete."""
    path.unlink()
    path.mkdir()
    (path / "keep.txt").write_bytes(b"not yours to delete")


async def test_a_move_flushes_both_parent_directories(note_vault, monkeypatch):
    """One `renameat2`, two directory entries, two flushes — after the rename.

    Flushing only the destination leaves the source's removal in the page
    cache: a crash then restores a name whose note is also at the new path, and
    the index — which the tool has already updated — names only one of them.
    """
    (note_vault / "From").mkdir()
    (note_vault / "To").mkdir()
    (note_vault / "From" / "note.md").write_text("body\n", encoding="utf-8")
    _no_db(monkeypatch)

    order: list[str] = []
    real_fsync = os.fsync
    real_rename = vault_fs.rename_noreplace

    def record_fsync(fd):
        if _is_dir_fd(fd):
            where = Path(os.readlink(f"/proc/self/fd/{fd}"))
            order.append(str(where.relative_to(note_vault.resolve())))
        return real_fsync(fd)

    def record_rename(*args, **kwargs):
        order.append("rename")
        return real_rename(*args, **kwargs)

    monkeypatch.setattr(os, "fsync", record_fsync)
    monkeypatch.setattr(vault_fs, "rename_noreplace", record_rename)

    result = await tools.move_note_impl("From/note.md", "To/note.md")

    assert "Moved" in result or "moved" in result, result
    assert order[0] == "rename", order
    # Both ends, plus the root above each of them (deduplicated per end by the
    # helper, not across the two calls).
    assert set(order[1:]) == {"To", "From", "."}, order
    assert "To" in order[1:] and "From" in order[1:], order
    assert (note_vault / "To" / "note.md").read_text() == "body\n"


async def test_a_move_into_a_folder_it_created_flushes_that_chain_too(
    note_vault, monkeypatch
):
    (note_vault / "note.md").write_text("body\n", encoding="utf-8")
    _no_db(monkeypatch)
    seen = _dir_flush_recorder(monkeypatch, note_vault)

    result = await tools.move_note_impl("note.md", "New/Folder/note.md")

    assert "Moved" in result or "moved" in result, result
    # The destination's own parent, the two entries that name it outward, and
    # the source's parent — which here is the root, already in the set.
    assert set(seen) == {"New/Folder", "New", "."}, seen
    assert (note_vault / "New" / "Folder" / "note.md").read_text() == "body\n"


async def test_a_failed_flush_never_turns_a_landed_move_into_a_failure(
    note_vault, monkeypatch, caplog
):
    """D18 again. A move reported as failed is retried, and the retry finds the
    source gone — so it either contradicts the vault or acts on whatever has
    since taken the name."""
    (note_vault / "note.md").write_text("body\n", encoding="utf-8")
    _no_db(monkeypatch)
    failed = _fail_dir_fsync(monkeypatch)

    with caplog.at_level("WARNING"):
        result = await tools.move_note_impl("note.md", "moved.md")

    assert failed, "no directory flush was attempted"
    assert "Moved" in result or "moved" in result, result
    assert (note_vault / "moved.md").read_text() == "body\n"
    assert not (note_vault / "note.md").exists()
    assert any("flush" in r.getMessage() for r in caplog.records)


def test_a_rollback_rename_is_flushed_by_the_same_helper(note_vault, monkeypatch):
    """The refusal paths put a file back with a second `renameat2`, and a
    restore that evaporates in a crash is the same reverted-rename class.

    `_verify_the_moved_inode` rolls back by calling `move_file_no_clobber` with
    the targets swapped, so this asserts the property where it lives: the
    publication primitive flushes both ends whichever direction it is used in.
    """
    (note_vault / "From").mkdir()
    (note_vault / "To").mkdir()
    (note_vault / "To" / "note.md").write_text("body\n", encoding="utf-8")
    seen = _dir_flush_recorder(monkeypatch, note_vault)

    with vault_service.open_mutable("To/note.md") as landed, vault_service.open_mutable(
        "From/note.md"
    ) as back:
        vault_service.move_file_no_clobber(landed, back)

    assert set(seen) == {"From", "To", "."}, seen
    assert (note_vault / "From" / "note.md").read_text() == "body\n"


async def test_a_soft_delete_flushes_the_source_parent_and_the_trash(
    note_vault, monkeypatch
):
    (note_vault / "Folder").mkdir()
    (note_vault / "Folder" / "note.md").write_text("body\n", encoding="utf-8")

    order: list[str] = []
    real_fsync = os.fsync
    real_rename = vault_fs.rename_noreplace

    def record_fsync(fd):
        if _is_dir_fd(fd):
            where = Path(os.readlink(f"/proc/self/fd/{fd}"))
            order.append(str(where.relative_to(note_vault.resolve())))
        return real_fsync(fd)

    def record_rename(*args, **kwargs):
        order.append("rename")
        return real_rename(*args, **kwargs)

    monkeypatch.setattr(os, "fsync", record_fsync)
    monkeypatch.setattr(vault_fs, "rename_noreplace", record_rename)

    result = await tools.delete_note_impl("Folder/note.md")

    assert "Soft-deleted" in result, result
    # The probe runs first and renames its own temp file; the flushes under
    # test are the ones after the *delete's* rename, which is the last one.
    tail = order[len(order) - order[::-1].index("rename") :]
    assert set(tail) == {"Folder", ".trash", "."}, order
    assert not (note_vault / "Folder" / "note.md").exists()


async def test_a_failed_flush_never_turns_a_landed_soft_delete_into_a_failure(
    note_vault, monkeypatch, caplog
):
    (note_vault / "note.md").write_text("body\n", encoding="utf-8")
    # The trash probe renames a temp file and would trip the fault first, so
    # let it run before arming — the probe is not what is under test here.
    vault_fs.check_trash_support(note_vault)
    failed = _fail_dir_fsync(monkeypatch)

    with caplog.at_level("WARNING"):
        result = await tools.delete_note_impl("note.md")

    assert failed, "no directory flush was attempted"
    assert "Soft-deleted" in result, result
    assert not (note_vault / "note.md").exists()
    assert len(list((note_vault / ".trash").iterdir())) == 1
    assert any("flush" in r.getMessage() for r in caplog.records)


def test_a_soft_delete_rollback_flushes_both_ends(note_vault, monkeypatch):
    """A directory swapped in after the check rides the rename into `.trash`
    and is put back. That restore is a publication too — and the refusal is
    reported whether or not its flushes worked."""
    root_fd = vault_fs.open_root(note_vault)
    try:
        source = note_vault / "note.md"
        source.write_bytes(b"body")

        seen = _dir_flush_recorder(monkeypatch, note_vault)
        real_rename = vault_fs.rename_noreplace
        swapped: list[bool] = []

        def swapping_rename(src_dir_fd, src_name, dst_dir_fd, dst_name):
            if not swapped:
                swapped.append(True)
                _swap_in_a_directory(source)
            return real_rename(src_dir_fd, src_name, dst_dir_fd, dst_name)

        monkeypatch.setattr(vault_fs, "rename_noreplace", swapping_rename)

        with pytest.raises(vault_fs.UnsafePath):
            vault_fs.soft_delete(root_fd, "note.md")
        monkeypatch.undo()

        assert swapped, "the race never ran"
        assert set(seen) == {".", ".trash"}, seen
        assert (source / "keep.txt").read_bytes() == b"not yours to delete"
        assert list((note_vault / ".trash").iterdir()) == []
    finally:
        os.close(root_fd)


async def test_a_permanent_delete_flushes_the_parent_after_the_unlink(
    note_vault, monkeypatch
):
    """An entry that survives a crash resurrects a note the agent was told is
    gone — and the agent has no reason to look again."""
    (note_vault / "Folder").mkdir()
    (note_vault / "Folder" / "note.md").write_text("body\n", encoding="utf-8")

    order: list[str] = []
    real_fsync = os.fsync
    real_unlink = os.unlink

    def record_fsync(fd):
        if _is_dir_fd(fd):
            where = Path(os.readlink(f"/proc/self/fd/{fd}"))
            order.append(str(where.relative_to(note_vault.resolve())))
        return real_fsync(fd)

    def record_unlink(*args, **kwargs):
        order.append("unlink")
        return real_unlink(*args, **kwargs)

    monkeypatch.setattr(os, "fsync", record_fsync)
    monkeypatch.setattr(os, "unlink", record_unlink)

    result = await tools.delete_note_impl("Folder/note.md", permanent=True)

    assert "Permanently deleted" in result, result
    assert order == ["unlink", "Folder"], order
    assert not (note_vault / "Folder" / "note.md").exists()


async def test_a_failed_flush_never_turns_a_landed_permanent_delete_into_a_failure(
    note_vault, monkeypatch, caplog
):
    (note_vault / "note.md").write_text("body\n", encoding="utf-8")
    failed = _fail_dir_fsync(monkeypatch)

    with caplog.at_level("WARNING"):
        result = await tools.delete_note_impl("note.md", permanent=True)

    assert failed, "no directory flush was attempted"
    assert "Permanently deleted" in result, result
    assert not (note_vault / "note.md").exists()
    assert any("flush" in r.getMessage() for r in caplog.records)


def test_the_raw_file_permanent_delete_flushes_too(note_vault, monkeypatch):
    """`delete_file(permanent=True)` reaches the unlink through `vault_fs.remove`,
    which is where the flush lives — the note tool and the byte tool must not
    differ in whether a delete survives a crash."""
    (note_vault / "Attachments").mkdir()
    (note_vault / "Attachments" / "a.bin").write_bytes(b"x")
    root_fd = vault_fs.open_root(note_vault)
    try:
        seen = _dir_flush_recorder(monkeypatch, note_vault)
        vault_fs.remove(root_fd, "Attachments/a.bin")
        assert seen == ["Attachments"], seen
        assert not (note_vault / "Attachments" / "a.bin").exists()
    finally:
        os.close(root_fd)


# ════════════════════════════════════════════════════════════════════════════
# 2.8 — the entry that names a freshly created `.trash`
# ════════════════════════════════════════════════════════════════════════════
#
# Flushing `.trash` persists its *contents*. The entry in the vault root that
# names `.trash` is a separate write, and on the first soft delete of a vault
# that entry is brand new. A crash can therefore durably remove
# `Folder/note.md` and lose the whole `.trash` directory with the only copy of
# the note inside it — the same created-ancestor class the note and transfer
# publishes already flush, on the one path that had been missed.


def test_a_soft_delete_that_creates_trash_flushes_the_root_that_names_it(
    vault, monkeypatch
):
    (vault / "Attachments" / "a.png").write_bytes(b"bytes")
    assert not (vault / ".trash").exists()
    seen = _dir_flush_recorder(monkeypatch, vault)

    root_fd = vault_fs.open_root(vault)
    try:
        vault_fs.soft_delete(root_fd, "Attachments/a.png")
    finally:
        os.close(root_fd)

    assert "." in seen, (
        f"the root entry naming the new .trash was never flushed: {seen}"
    )
    assert "Attachments" in seen
    assert ".trash" in seen


def test_a_soft_delete_into_an_existing_trash_flushes_the_root_anyway(
    vault, monkeypatch
):
    """The chain rule reaches `.trash` too, with its own version of the abort
    shape: a probe or a delete that created `.trash` and then failed leaves it
    there unflushed, and the next delete finds it existing. Nobody can tell the
    two apart afterwards. `.trash` is a direct child of the root, so this is one
    extra metadata `fsync`."""
    (vault / ".trash").mkdir()
    (vault / "Attachments" / "a.png").write_bytes(b"bytes")
    seen = _dir_flush_recorder(monkeypatch, vault)

    root_fd = vault_fs.open_root(vault)
    try:
        vault_fs.soft_delete(root_fd, "Attachments/a.png")
    finally:
        os.close(root_fd)

    assert sorted(seen) == [".", ".trash", "Attachments"], seen


def test_a_failing_flush_of_the_new_trash_entry_does_not_fail_the_delete(
    vault, monkeypatch, caplog
):
    """D18's note-path direction. The delete has already happened; reporting a
    failure invites a retry, and a retried delete finds the source gone and
    either contradicts the vault or acts on whatever has taken the name."""
    (vault / "Attachments" / "a.png").write_bytes(b"bytes")
    real = os.fsync

    def refuse_root(fd):
        where = os.readlink(f"/proc/self/fd/{fd}")
        if _is_dir_fd(fd) and Path(where) == vault.resolve():
            raise OSError(errno.EIO, "input/output error")
        return real(fd)

    monkeypatch.setattr(os, "fsync", refuse_root)

    root_fd = vault_fs.open_root(vault)
    try:
        with caplog.at_level(logging.WARNING, logger="src.services.vault_fs"):
            dest = vault_fs.soft_delete(root_fd, "Attachments/a.png")
    finally:
        os.close(root_fd)

    assert dest.startswith(".trash/")
    assert not (vault / "Attachments" / "a.png").exists()
    assert (vault / dest).read_bytes() == b"bytes"
    assert [r for r in caplog.records if "could not flush" in r.getMessage()], (
        "the failure was swallowed without a word"
    )


def test_the_trash_probe_flushes_the_root_when_it_creates_trash(vault, monkeypatch):
    """The probe leaves `.trash` behind — a soft delete is about to need it — so
    the probe is what owes the root's entry for it a flush."""
    assert not (vault / ".trash").exists()
    seen = _dir_flush_recorder(monkeypatch, vault)

    vault_fs.check_trash_support(vault)

    assert (vault / ".trash").is_dir()
    assert "." in seen, seen


def test_the_trash_probe_flushes_the_root_even_for_an_existing_trash(
    vault, monkeypatch
):
    """Same rule, same reason: the probe cannot know whether the `.trash` it
    found was ever made durable by whoever created it."""
    (vault / ".trash").mkdir()
    seen = _dir_flush_recorder(monkeypatch, vault)

    vault_fs.check_trash_support(vault)

    assert "." in seen, seen


# ════════════════════════════════════════════════════════════════════════════
# 2.9 — the obligation outlives the call that incurred it
#
# The whole reason the flush follows the *chain* and not this call's creation
# list. An attempt that creates a directory and then aborts flushes nothing —
# correctly, it published nothing — and the retry finds the directory already
# there and records no creation. Under a provenance rule the entry naming it
# would be made durable by nobody, and a crash could take a completed upload,
# a written note or a soft-deleted file with it.
#
# Each case runs the abort and the retry against the same vault and asserts on
# the *retry's* flushes: the ones a provenance rule would have skipped.
# ════════════════════════════════════════════════════════════════════════════


async def test_a_transfer_retry_flushes_dirs_an_aborted_attempt_created(
    vault, monkeypatch
):
    prime_publication_probe(vault)

    # The abort: the body runs past the cap once the directories exist.
    with pytest.raises(transfer.TooLarge):
        await stream_to_vault(
            FakeRow(str(vault), "New/Folder/a.bin"),
            chunks_of(b"x" * 200),
            max_bytes=10,
            deadline=deadline_in(30),
        )
    assert (vault / "New" / "Folder").is_dir(), "the abort did not create them"
    assert not (vault / "New" / "Folder" / "a.bin").exists()

    # The retry: same path, directories already there, nothing recorded created.
    created_by_the_retry: list[str] = []
    real_open_parent = vault_fs.open_parent

    def watching_open_parent(root_fd, rel, *, create=False, created=None):
        result = real_open_parent(root_fd, rel, create=create, created=created)
        if created is not None:
            created_by_the_retry.extend(created)
        return result

    monkeypatch.setattr(vault_fs, "open_parent", watching_open_parent)
    seen = _dir_flush_recorder(monkeypatch, vault)

    await stream_to_vault(
        FakeRow(str(vault), "New/Folder/a.bin"),
        chunks_of(b"payload"),
        max_bytes=100,
        deadline=deadline_in(30),
    )

    assert created_by_the_retry == [], (
        "the retry recorded a creation, so this is not the case under test"
    )
    assert seen == ["New/Folder", "New", "."], (
        f"the retry skipped entries only the aborted attempt created: {seen}"
    )
    assert (vault / "New" / "Folder" / "a.bin").read_bytes() == b"payload"


async def test_a_note_retry_flushes_dirs_an_aborted_write_created(
    note_vault, monkeypatch
):
    """`edit_note(expected=…)` losing its race is the note path's abort: the
    parent chain is created by `ensure_parent`, then the write refuses."""
    (note_vault / "New").mkdir()

    with pytest.raises(RuntimeError):
        vault_service.write_file(
            "New/Folder/note.md", "body\n", overwrite=True, expected=b"not there"
        )
    assert (note_vault / "New" / "Folder").is_dir(), "the abort did not create it"
    assert not (note_vault / "New" / "Folder" / "note.md").exists()

    seen = _dir_flush_recorder(monkeypatch, note_vault)
    await tools.create_note_impl("New/Folder/note.md", "body\n")

    assert seen == ["New/Folder", "New", "."], (
        f"the retry skipped entries only the aborted write created: {seen}"
    )
    assert (note_vault / "New" / "Folder" / "note.md").read_text() == "body\n"


def test_a_soft_delete_flushes_a_trash_an_earlier_attempt_left_behind(
    vault, monkeypatch
):
    """`.trash`'s version of the same shape: a delete that created the directory
    and then refused leaves it there unflushed, and the next delete finds it."""
    (vault / "Attachments" / "a.png").write_bytes(b"bytes")
    (vault / "Attachments" / "b.png").write_bytes(b"bytes")

    real_rename = vault_fs.rename_noreplace

    def failing_rename(*args, **kwargs):
        raise OSError(errno.EIO, "input/output error")

    root_fd = vault_fs.open_root(vault)
    try:
        # `.trash` is opened (and created) before the rename, so a rename that
        # fails leaves the directory behind with nothing having flushed the
        # entry that names it — and nothing published, so nothing should have.
        monkeypatch.setattr(vault_fs, "rename_noreplace", failing_rename)
        with pytest.raises(OSError):
            vault_fs.soft_delete(root_fd, "Attachments/b.png")
        monkeypatch.setattr(vault_fs, "rename_noreplace", real_rename)
        assert (vault / ".trash").is_dir(), "the abort did not create it"
        assert (vault / "Attachments" / "b.png").exists(), "it published something"

        seen = _dir_flush_recorder(monkeypatch, vault)
        vault_fs.soft_delete(root_fd, "Attachments/a.png")
    finally:
        os.close(root_fd)

    assert "." in seen, (
        f"the entry naming a .trash an earlier attempt created was skipped: {seen}"
    )
