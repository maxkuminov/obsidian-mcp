"""Nested-mount cases — run ONLY inside a private mount namespace.

`tests/test_nested_mount_publication.py` spawns this module with

    unshare -Urm --propagation private python -m pytest tests/_nested_mount_cases.py

because it needs a real `mount --bind` and because the settings it depends on
(`VAULT_PATH`, `MCP_SANDBOX_MODE`, `BASE_URL`) are read at import time. The
leading underscore keeps it out of normal collection.

Everything in the sibling module stubs the mount id, which pins the *policy*
and cannot pin the premise. These pin the premise: a bind mount of a directory
of the same filesystem, beneath the vault root, reports the **same `st_dev`** on
both sides and a **different `STATX_MNT_ID`**, and a `link` across it really does
return `EXDEV`. That is the whole reason the check is on mount identity.

Sandbox mode leaves the auth contextvars untouched, so each case sets
`current_permission` itself — a fake readwrite identity, exactly as
`tests/_transport_body_limit_cases.py` does.
"""
from __future__ import annotations

import errno
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import src.mcp_server.tools as tools  # noqa: E402
from src.mcp_server.auth import current_permission  # noqa: E402
from src.services import transfer, vault_fs  # noqa: E402
from src.services.transfer import stream_to_vault  # noqa: E402

VAULT = Path(os.environ["OMCP_NESTED_MOUNT_VAULT"])


def bind(source: Path, target: Path) -> None:
    subprocess.run(
        ["mount", "--bind", str(source), str(target)], check=True, timeout=30
    )


def unbind(target: Path) -> None:
    subprocess.run(["umount", "-l", str(target)], check=False, timeout=30)


@pytest.fixture(autouse=True)
def clean_vault():
    """A vault with `M/` bound over itself — same filesystem, its own mount."""
    for child in VAULT.iterdir():
        if child.name == "M":
            continue
        subprocess.run(["rm", "-rf", str(child)], check=False)
    (VAULT / "Attachments").mkdir(exist_ok=True)
    mount_point = VAULT / "M"
    mount_point.mkdir(exist_ok=True)
    for child in mount_point.iterdir():
        subprocess.run(["rm", "-rf", str(child)], check=False)
    vault_fs.reset_filesystem_probe_cache()
    vault_fs.reset_named_staging_state()
    token = current_permission.set("readwrite")
    yield mount_point
    current_permission.reset(token)
    unbind(mount_point)
    vault_fs.reset_filesystem_probe_cache()


@dataclass
class FakeRow:
    vault_root: str
    path: str
    overwrite: bool = False
    expected_fingerprint: dict | None = None


async def chunks_of(*parts: bytes):
    for part in parts:
        yield part


def deadline_in(seconds: float) -> float:
    return time.monotonic() + seconds


def _fd(path) -> int:
    return os.open(str(path), os.O_RDONLY | os.O_DIRECTORY)


# ── the premise ─────────────────────────────────────────────────────────────


def test_a_same_filesystem_bind_mount_shares_st_dev_and_not_the_mount_id(
    clean_vault,
):
    """This is the whole reason the comparison is not on `st_dev`."""
    bind(clean_vault, clean_vault)
    root, mounted = _fd(VAULT), _fd(clean_vault)
    try:
        assert os.fstat(root).st_dev == os.fstat(mounted).st_dev
        assert vault_fs.mount_id_of(root) != vault_fs.mount_id_of(mounted)
        assert vault_fs.same_mount(root, mounted) is False
    finally:
        os.close(root)
        os.close(mounted)


def test_a_link_across_the_boundary_really_does_fail(clean_vault):
    """An `st_dev` preflight passes here and the publish fails afterwards."""
    bind(clean_vault, clean_vault)
    (VAULT / "source.bin").write_bytes(b"payload")
    root, mounted = _fd(VAULT), _fd(clean_vault)
    try:
        with pytest.raises(OSError) as exc:
            os.link(
                "source.bin", "dest.bin",
                src_dir_fd=root, dst_dir_fd=mounted, follow_symlinks=False,
            )
        assert exc.value.errno == errno.EXDEV
    finally:
        os.close(root)
        os.close(mounted)


# ── 4.4: the mint refuses before anything moves ─────────────────────────────


async def test_request_upload_refuses_and_mints_nothing(clean_vault):
    """No database work happens: `_mint_preflight` refuses before the row."""
    bind(clean_vault, clean_vault)
    result = await tools.request_upload_impl("M/a.bin")
    assert isinstance(result, str)
    assert "different mount" in result
    assert "M/a.bin" in result
    assert "hard link" not in result
    assert list(clean_vault.iterdir()) == []


async def test_import_from_url_refuses_before_it_opens_a_connection(
    clean_vault, monkeypatch
):
    bind(clean_vault, clean_vault)
    calls: list = []

    async def never(*args, **kwargs):
        calls.append(args)
        raise AssertionError("a connection was opened for a refused destination")

    monkeypatch.setattr(transfer, "fetch_url_guarded", never)
    result = await tools.import_from_url_impl("https://example.com/a.bin", "M/a.bin")
    assert "different mount" in result
    assert calls == []
    assert list(clean_vault.iterdir()) == []


async def test_a_destination_beside_the_mount_is_unaffected(clean_vault):
    """A vault with a nested mount still publishes normally everywhere else."""
    bind(clean_vault, clean_vault)
    row = FakeRow(str(VAULT), "Attachments/a.bin")
    await stream_to_vault(
        row, chunks_of(b"payload"), max_bytes=1000, deadline=deadline_in(30)
    )
    assert (VAULT / "Attachments" / "a.bin").read_bytes() == b"payload"


async def test_a_vault_with_no_nested_mount_behaves_exactly_as_before(clean_vault):
    """`M` is an ordinary directory until it is bound over."""
    row = FakeRow(str(VAULT), "M/a.bin")
    await stream_to_vault(
        row, chunks_of(b"payload"), max_bytes=1000, deadline=deadline_in(30)
    )
    assert (clean_vault / "a.bin").read_bytes() == b"payload"
    result = await tools.request_upload_impl("M/b.bin")
    assert "different mount" not in str(result)


# ── 4.5: a mount established after the mint, caught in the gate ─────────────


class Gate:
    def __init__(self):
        self.entered = 0
        self.completions = []

    def __call__(self):
        return self

    async def __aenter__(self):
        self.entered += 1

        async def record(result, published):
            self.completions.append((result, published))

        return transfer.GateHandle(ok=True, session=self, on_complete=record)

    async def __aexit__(self, *exc):
        return False


async def test_a_mount_established_after_the_mint_is_refused_in_the_gate(
    clean_vault, monkeypatch
):
    """**After the body has streamed in full, before anything is published.**

    That ordering is the point and it is asserted rather than glossed: only the
    mint-time check spares the bytes, and only where the boundary already
    existed. This half costs the whole body and saves the write.

    It also asserts that `publish` is never *reached*. Without the in-gate check
    this test would still see a `MountBoundary` — the `link` would return
    `EXDEV` and the residual mapping would name the boundary — so a test that
    only looked at the exception type would pass against a tree with no in-gate
    check at all, which is exactly the gap under review.
    """
    (clean_vault / "incumbent.bin").write_bytes(b"do not touch")
    published: list = []
    real_publish = vault_fs.publish

    def spying_publish(*args, **kwargs):
        published.append(args)
        return real_publish(*args, **kwargs)

    monkeypatch.setattr(vault_fs, "publish", spying_publish)
    delivered: list[int] = []

    async def chunks():
        yield b"first-half"
        delivered.append(1)
        yield b"second-half"
        delivered.append(2)
        # The boundary appears between the last byte of the body and the gate.
        bind(clean_vault, clean_vault)

    gate = Gate()
    row = FakeRow(str(VAULT), "M/a.bin")
    with pytest.raises(vault_fs.MountBoundary) as exc:
        await stream_to_vault(
            row,
            chunks(),
            max_bytes=1000,
            deadline=deadline_in(30),
            before_publish=gate,
        )

    assert delivered == [1, 2], "the refusal did not follow the whole body"
    assert gate.entered == 1, "the gate never opened"
    assert published == [], (
        "the refusal came from publish's residual EXDEV mapping, not from the "
        "in-gate mount check that is supposed to precede it"
    )
    assert gate.completions == [], "a completion was recorded for a refusal"
    assert "different mount" in str(exc.value)
    # Pre-publication, so the caller releases the claim rather than stranding it.
    assert not isinstance(exc.value, transfer.PostPublishFailure)
    assert not (clean_vault / "a.bin").exists()
    assert (clean_vault / "incumbent.bin").read_bytes() == b"do not touch"


async def test_the_staged_bytes_are_discarded_when_the_gate_refuses(clean_vault):
    async def chunks():
        yield b"payload"
        bind(clean_vault, clean_vault)

    row = FakeRow(str(VAULT), "M/a.bin")
    with pytest.raises(vault_fs.MountBoundary):
        await stream_to_vault(
            row, chunks(), max_bytes=1000, deadline=deadline_in(30),
            before_publish=Gate(),
        )
    staging = VAULT / vault_fs.STAGING_DIR
    assert sorted(p.name for p in staging.iterdir()) == []


# ── the leaf: a bind mount on the destination *file* ────────────────────────


def bind_leaf(vault_dir) -> None:
    """Bind a different regular file over `<vault>/target.bin`."""
    (VAULT / "other.bin").write_bytes(b"bound")
    bind(VAULT / "other.bin", VAULT / "target.bin")


def test_a_leaf_bind_mount_shares_st_dev_and_breaks_the_rename(clean_vault):
    """The premise the parent check cannot see, established by the kernel."""
    (VAULT / "target.bin").write_bytes(b"old")
    bind_leaf(clean_vault)
    root = _fd(VAULT)
    try:
        assert os.stat(VAULT).st_dev == os.stat(VAULT / "target.bin").st_dev
        leaf = os.open("target.bin", os.O_PATH | os.O_NOFOLLOW, dir_fd=root)
        try:
            assert vault_fs.mount_id_of(root) != vault_fs.mount_id_of(leaf)
        finally:
            os.close(leaf)
        assert vault_fs.leaf_is_separate_mount(root, root, "target.bin") is True
        # And the syscall the publish would make really does refuse.
        (VAULT / ".tmp-probe").write_bytes(b"new")
        try:
            os.replace(".tmp-probe", "target.bin", src_dir_fd=root, dst_dir_fd=root)
            raise AssertionError("the rename onto a mount point succeeded")
        except OSError as exc:
            assert exc.errno == errno.EBUSY
    finally:
        os.close(root)
    unbind(VAULT / "target.bin")


async def test_the_mint_refuses_an_overwrite_onto_a_leaf_mount(clean_vault):
    (VAULT / "target.bin").write_bytes(b"old")
    bind_leaf(clean_vault)
    try:
        result = await tools.request_upload_impl("target.bin", overwrite=True)
        assert isinstance(result, str)
        assert "mount point" in result
        assert (VAULT / "target.bin").read_bytes() == b"bound"
    finally:
        unbind(VAULT / "target.bin")


async def test_a_leaf_mount_established_after_the_mint_is_refused_in_the_gate(
    clean_vault, monkeypatch
):
    """Same shape as the directory case: after the whole body, before the write."""
    (VAULT / "target.bin").write_bytes(b"old")
    root = _fd(VAULT)
    try:
        want = vault_fs.fingerprint(root, "target.bin", hash_up_to=10_000)
    finally:
        os.close(root)

    published: list = []
    real_publish = vault_fs.publish

    def spying_publish(*args, **kwargs):
        published.append(args)
        return real_publish(*args, **kwargs)

    monkeypatch.setattr(vault_fs, "publish", spying_publish)
    delivered: list[int] = []

    async def chunks():
        yield b"payload"
        delivered.append(1)
        bind_leaf(clean_vault)

    row = FakeRow(str(VAULT), "target.bin", overwrite=True, expected_fingerprint=want)
    try:
        with pytest.raises(vault_fs.MountBoundary) as exc:
            await stream_to_vault(
                row, chunks(), max_bytes=1000, deadline=deadline_in(30),
                before_publish=Gate(),
            )
        assert delivered == [1], "the refusal did not follow the whole body"
        assert published == [], (
            "the refusal came from publish's EBUSY reclassification, not from "
            "the in-gate leaf check that is supposed to precede it"
        )
        assert "mount point" in str(exc.value)
        assert (VAULT / "target.bin").read_bytes() == b"bound"
    finally:
        unbind(VAULT / "target.bin")


def test_a_leaf_mount_reaching_the_rename_is_reclassified(clean_vault, monkeypatch):
    """The residual: `EBUSY` out of the real syscall, reclassified only because
    a fresh look establishes the cause.

    It takes a **self**-bind to get here. Binding a *different* file over the
    target changes the inode, so the fingerprint check refuses first with
    `Conflict` — which is the right answer and means the ordinary
    substitute-a-file shape never reaches the rename at all. `mount --bind
    target.bin target.bin` keeps the inode, so the fingerprint still matches and
    the mount is the only thing left to refuse. That is the narrow case the
    reclassification exists for.
    """
    (VAULT / "target.bin").write_bytes(b"old")
    root = _fd(VAULT)
    try:
        want = vault_fs.fingerprint(root, "target.bin", hash_up_to=10_000)
        bind(VAULT / "target.bin", VAULT / "target.bin")
        staging = vault_fs.open_staging_dir(root)
        try:
            fd, tmp = vault_fs.create_temp(staging)
            os.write(fd, b"new")
            os.close(fd)
            with pytest.raises(vault_fs.MountBoundary) as exc:
                vault_fs.publish(
                    staging, tmp, "target.bin",
                    overwrite=True, expected_fingerprint=want, dst_dir_fd=root,
                )
            assert "EBUSY" in str(exc.value)
            assert "mount point" in str(exc.value)
        finally:
            os.close(staging)
    finally:
        os.close(root)
        unbind(VAULT / "target.bin")
    assert (VAULT / "target.bin").read_bytes() == b"old"


def test_a_leaf_bind_of_a_different_file_is_caught_by_the_fingerprint_first(
    clean_vault,
):
    """The ordinary shape, and the reason the `EBUSY` case above needs a
    self-bind: an overwrite is bound to the incumbent's identity at mint, so a
    mount that swaps the inode is refused as a `Conflict` before the rename."""
    (VAULT / "target.bin").write_bytes(b"old")
    root = _fd(VAULT)
    try:
        want = vault_fs.fingerprint(root, "target.bin", hash_up_to=10_000)
        bind_leaf(clean_vault)
        staging = vault_fs.open_staging_dir(root)
        try:
            fd, tmp = vault_fs.create_temp(staging)
            os.write(fd, b"new")
            os.close(fd)
            with pytest.raises(vault_fs.Conflict):
                vault_fs.publish(
                    staging, tmp, "target.bin",
                    overwrite=True, expected_fingerprint=want, dst_dir_fd=root,
                )
        finally:
            os.close(staging)
    finally:
        os.close(root)
        unbind(VAULT / "target.bin")
    assert (VAULT / "target.bin").read_bytes() == b"old"


# ── nested-mount-honest-refusals: the vault side (#108, #109) ───────────────
#
# The sibling module stubs `mount_id_of`, which pins the *policy*. These pin
# the premise for the vault path the way the transfer cases above pin it for
# publication: a real bind mount, a real `renameat2`, and the kernel deciding.
# The database is not reachable from this sandbox, so the "no row was touched"
# half of the contract is asserted against a session spy in the sibling module;
# what is asserted here is the filesystem and the message.


def test_a_rename_across_the_boundary_really_does_fail(clean_vault):
    """`renameat2(RENAME_NOREPLACE)` refuses across a mount, on a filesystem
    that renames perfectly well — the premise the old message denied."""
    bind(clean_vault, clean_vault)
    (clean_vault / "a.md").write_bytes(b"body\n")
    root, mounted = _fd(VAULT), _fd(clean_vault)
    try:
        with pytest.raises(vault_fs.MountBoundary) as exc:
            vault_fs.rename_noreplace(mounted, "a.md", root, "a.md")
    finally:
        os.close(root)
        os.close(mounted)
    assert "different mounts" in str(exc.value)
    assert "not available" not in str(exc.value)
    assert (clean_vault / "a.md").read_bytes() == b"body\n"


async def test_delete_note_refuses_across_the_boundary(clean_vault):
    bind(clean_vault, clean_vault)
    note = clean_vault / "a.md"
    note.write_text("body\n", encoding="utf-8")

    result = await tools.delete_note_impl("M/a.md")

    assert "different mounts" in result, result
    assert "permanent=True" in result
    assert "cannot receive a non-replacing rename" not in result
    assert note.read_text(encoding="utf-8") == "body\n"
    trash = VAULT / ".trash"
    assert not trash.exists() or list(trash.iterdir()) == []


async def test_delete_file_refuses_across_the_boundary(clean_vault):
    """Same refusal through the other tool: both reach one primitive."""
    bind(clean_vault, clean_vault)
    blob = clean_vault / "a.bin"
    blob.write_bytes(b"bytes")

    result = await tools.delete_file_impl("M/a.bin")

    assert "different mounts" in result, result
    assert "permanent=True" in result
    assert blob.read_bytes() == b"bytes"


async def test_a_permanent_delete_still_works_across_the_boundary(clean_vault):
    """The workaround the message names has to actually work: an unlink
    crosses no mount boundary."""
    bind(clean_vault, clean_vault)
    (clean_vault / "a.bin").write_bytes(b"bytes")

    result = await tools.delete_file_impl("M/a.bin", permanent=True)

    assert "different mounts" not in result, result
    assert not (clean_vault / "a.bin").exists()


async def test_move_note_refuses_across_the_boundary(clean_vault):
    bind(clean_vault, clean_vault)
    note = clean_vault / "a.md"
    note.write_text("body\n", encoding="utf-8")

    result = await tools.move_note_impl("M/a.md", "a.md")

    assert "different mounts" in result, result
    assert "mount layout is what refuses" in result
    assert "not available" not in result
    assert note.read_text(encoding="utf-8") == "body\n"
    assert not (VAULT / "a.md").exists()


async def test_a_cross_mount_move_to_a_missing_folder_creates_nothing(clean_vault):
    """Codex finding 4, with the kernel supplying the boundary: the preflight
    compares against the destination's deepest *existing* ancestor, so it never
    materialises `New/` in order to ask which mount it would be on."""
    bind(clean_vault, clean_vault)
    (clean_vault / "a.md").write_text("body\n", encoding="utf-8")

    result = await tools.move_note_impl("M/a.md", "New/Sub/a.md")

    assert "different mounts" in result, result
    assert not (VAULT / "New").exists(), "the preflight created a directory"
    assert (clean_vault / "a.md").read_text(encoding="utf-8") == "body\n"


async def test_a_move_on_one_side_of_the_boundary_still_works(clean_vault):
    """The refusal is per pair, so a vault that merely contains a nested mount
    keeps every move that does not cross it."""
    bind(clean_vault, clean_vault)
    (VAULT / "a.md").write_text("body\n", encoding="utf-8")

    result = await tools.move_note_impl("a.md", "Attachments/a.md")

    assert "different mounts" not in result, result
    assert (VAULT / "Attachments" / "a.md").read_text(encoding="utf-8") == "body\n"


def test_a_trash_on_its_own_mount_is_named_by_the_probe(clean_vault):
    """Codex finding 3. `probe_trash` renames root→`.trash`, so its `EXDEV`
    means `.trash` is itself a mount — and the generic re-wrap would have
    erased both the subtype and the cause into "the vault filesystem cannot
    move files … with a non-replacing rename", which is false: it can.
    """
    trash = VAULT / vault_fs.TRASH_DIR
    trash.mkdir(exist_ok=True)
    bind(trash, trash)
    root = _fd(VAULT)
    try:
        with pytest.raises(vault_fs.MountBoundary) as exc:
            vault_fs.probe_trash(root)
    finally:
        os.close(root)
        unbind(trash)
    message = str(exc.value)
    assert "different mounts" in message
    assert vault_fs.TRASH_DIR in message
    assert "cannot move files" not in message
    assert "permanent=True" in message
