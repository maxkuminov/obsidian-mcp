"""Anchored filesystem primitives (task 2.3, design D6).

The properties under test are the ones that make `/transfer/*` safe to expose:
no symlink is ever traversed or replaced, a no-clobber publish cannot destroy
anything, an overwrite publish only lands on the exact bytes the token was
minted against, and a temp file never survives a failure.

Two tests deliberately *document* a limitation rather than assert a guarantee
(`test_overwrite_window_is_optimistic_not_linearizable`,
`test_null_fingerprint_is_expected_absence_not_skip_the_check`). They are the
executable form of the "optimistic conflict detection" claim in the design; if
someone later closes the window, the first one is the test that should change.
"""
import errno
import hashlib
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from src.services import vault_fs
from src.services.vault_fs import (
    Conflict,
    UnsafePath,
    UnsupportedFilesystem,
    create_temp,
    fingerprint,
    open_dir_beneath,
    open_root,
    probe_publication,
    probe_trash,
    publish,
    remove,
    soft_delete,
)

TRASH_NAME = re.compile(r"^\d{8}-\d{6}-(?P<base>.+)-[0-9a-f]{8}$")


@pytest.fixture
def vault(tmp_path):
    (tmp_path / "Attachments").mkdir()
    return tmp_path


@pytest.fixture
def root_fd(vault):
    fd = open_root(vault)
    yield fd
    os.close(fd)


def _write_temp(dir_fd, data: bytes) -> str:
    fd, name = create_temp(dir_fd)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)
    return name


def _temps(directory: Path) -> list[str]:
    return [p.name for p in directory.iterdir() if p.name.startswith(".tmp-")]


# ── directory anchoring ─────────────────────────────────────────────────────


def test_open_dir_beneath_returns_a_distinct_fd_for_the_root(root_fd, vault):
    fd = open_dir_beneath(root_fd, "")
    try:
        assert fd != root_fd
        assert os.fstat(fd).st_ino == os.stat(vault).st_ino
    finally:
        os.close(fd)


def test_open_dir_beneath_walks_nested_directories(root_fd, vault):
    (vault / "a" / "b").mkdir(parents=True)
    fd = open_dir_beneath(root_fd, "a/b")
    try:
        assert os.fstat(fd).st_ino == os.stat(vault / "a" / "b").st_ino
    finally:
        os.close(fd)


def test_open_dir_beneath_creates_when_asked(root_fd, vault):
    fd = open_dir_beneath(root_fd, "new/deep", create=True)
    os.close(fd)
    assert (vault / "new" / "deep").is_dir()


def test_missing_directory_without_create_is_not_found(root_fd):
    with pytest.raises(FileNotFoundError):
        open_dir_beneath(root_fd, "nope")


def test_symlinked_ancestor_is_refused(root_fd, vault, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside")
    (outside / "sub").mkdir()
    os.symlink(outside, vault / "link")
    with pytest.raises(UnsafePath):
        open_dir_beneath(root_fd, "link/sub")


def test_symlinked_ancestor_is_refused_even_when_it_points_inside(root_fd, vault):
    (vault / "real").mkdir()
    os.symlink(vault / "real", vault / "alias")
    # Refused on principle: the guarantee is "no symlink is traversed", not
    # "no symlink that happens to escape is traversed". A link that points
    # inside today can be repointed outside a microsecond later.
    with pytest.raises(UnsafePath):
        open_dir_beneath(root_fd, "alias")


def test_symlinked_ancestor_is_refused_with_create(root_fd, vault, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside2")
    os.symlink(outside, vault / "link")
    with pytest.raises(UnsafePath):
        open_dir_beneath(root_fd, "link/made", create=True)
    assert not (outside / "made").exists()


def test_parent_traversal_is_refused(root_fd):
    with pytest.raises(UnsafePath):
        open_dir_beneath(root_fd, "../escape")
    with pytest.raises(UnsafePath):
        open_dir_beneath(root_fd, "Attachments/../../escape")


def test_absolute_path_is_refused(root_fd):
    with pytest.raises(UnsafePath):
        open_dir_beneath(root_fd, "/etc")


def test_file_in_the_ancestor_chain_is_refused(root_fd, vault):
    (vault / "afile").write_bytes(b"x")
    with pytest.raises(UnsafePath):
        open_dir_beneath(root_fd, "afile/sub")


# ── temp files ──────────────────────────────────────────────────────────────


def test_create_temp_is_private_and_hidden(root_fd, vault):
    fd, name = create_temp(root_fd)
    os.close(fd)
    assert name.startswith(".tmp-")
    st = os.stat(vault / name)
    assert st.st_mode & 0o777 == 0o600


def test_create_temp_refuses_to_reuse_a_name(root_fd, vault, monkeypatch):
    """`O_EXCL` means a pre-created name is never opened, only retried."""
    monkeypatch.setattr(vault_fs.secrets, "token_hex", lambda n: "a" * (2 * n))
    (vault / f".tmp-{'a' * 32}").write_bytes(b"squatted")
    with pytest.raises(vault_fs.VaultFSError):
        create_temp(root_fd)
    assert (vault / f".tmp-{'a' * 32}").read_bytes() == b"squatted"


# ── fingerprints ────────────────────────────────────────────────────────────


def test_fingerprint_of_a_missing_file_is_none(root_fd):
    assert fingerprint(root_fd, "nothing", hash_up_to=1000) is None


def test_fingerprint_hashes_below_the_cap(root_fd, vault):
    (vault / "f.bin").write_bytes(b"hello")
    fp = fingerprint(root_fd, "f.bin", hash_up_to=1000)
    assert fp["sha256"] == hashlib.sha256(b"hello").hexdigest()
    assert fp["size"] == 5
    assert set(fp) == {"dev", "inode", "size", "mtime_ns", "ctime_ns", "sha256"}


def test_fingerprint_above_the_cap_is_metadata_only(root_fd, vault):
    (vault / "big.bin").write_bytes(b"x" * 100)
    fp = fingerprint(root_fd, "big.bin", hash_up_to=10)
    assert fp["sha256"] is None
    assert fp["size"] == 100


def test_fingerprint_refuses_a_symlink(root_fd, vault):
    (vault / "target").write_bytes(b"x")
    os.symlink(vault / "target", vault / "alias")
    with pytest.raises(UnsafePath):
        fingerprint(root_fd, "alias", hash_up_to=1000)


# ── publish: no-clobber ─────────────────────────────────────────────────────


def test_no_clobber_publish_happy_path(root_fd, vault):
    dir_fd = open_dir_beneath(root_fd, "Attachments")
    try:
        tmp = _write_temp(dir_fd, b"payload")
        result = publish(dir_fd, tmp, "a.png", overwrite=False, expected_fingerprint=None)
    finally:
        os.close(dir_fd)
    assert result.published is True
    assert (vault / "Attachments" / "a.png").read_bytes() == b"payload"
    assert _temps(vault / "Attachments") == []


def test_no_clobber_publish_loses_a_race_without_touching_the_incumbent(root_fd, vault):
    """The target appears between the temp write and the publish."""
    dir_fd = open_dir_beneath(root_fd, "Attachments")
    try:
        tmp = _write_temp(dir_fd, b"mine")
        (vault / "Attachments" / "a.png").write_bytes(b"theirs")
        with pytest.raises(Conflict):
            publish(dir_fd, tmp, "a.png", overwrite=False, expected_fingerprint=None)
    finally:
        os.close(dir_fd)
    assert (vault / "Attachments" / "a.png").read_bytes() == b"theirs"
    assert _temps(vault / "Attachments") == []


def test_no_clobber_publish_refuses_a_symlink_target(root_fd, vault, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside3")
    victim = outside / "victim"
    victim.write_bytes(b"precious")
    os.symlink(victim, vault / "Attachments" / "a.png")

    dir_fd = open_dir_beneath(root_fd, "Attachments")
    try:
        tmp = _write_temp(dir_fd, b"mine")
        with pytest.raises(Conflict):
            publish(dir_fd, tmp, "a.png", overwrite=False, expected_fingerprint=None)
    finally:
        os.close(dir_fd)
    assert victim.read_bytes() == b"precious"


def test_publish_refuses_a_final_component_with_a_separator(root_fd, vault):
    dir_fd = open_dir_beneath(root_fd, "Attachments")
    try:
        tmp = _write_temp(dir_fd, b"x")
        with pytest.raises(UnsafePath):
            publish(dir_fd, tmp, "sub/a.png", overwrite=False, expected_fingerprint=None)
    finally:
        os.close(dir_fd)
    assert _temps(vault / "Attachments") == []


def test_temp_is_cleaned_when_publish_raises_an_unexpected_error(root_fd, vault, monkeypatch):
    dir_fd = open_dir_beneath(root_fd, "Attachments")
    try:
        tmp = _write_temp(dir_fd, b"x")

        def boom(*args, **kwargs):
            raise OSError(errno.EIO, "disk on fire")

        monkeypatch.setattr(vault_fs.os, "link", boom)
        with pytest.raises(OSError):
            publish(dir_fd, tmp, "a.png", overwrite=False, expected_fingerprint=None)
    finally:
        os.close(dir_fd)
    assert _temps(vault / "Attachments") == []
    assert not (vault / "Attachments" / "a.png").exists()


def test_hard_links_unavailable_is_reported_as_unsupported_filesystem(root_fd, monkeypatch):
    dir_fd = open_dir_beneath(root_fd, "Attachments")
    try:
        tmp = _write_temp(dir_fd, b"x")

        def eperm(*args, **kwargs):
            raise OSError(errno.EPERM, "no links here")

        monkeypatch.setattr(vault_fs.os, "link", eperm)
        with pytest.raises(UnsupportedFilesystem):
            publish(dir_fd, tmp, "a.png", overwrite=False, expected_fingerprint=None)
    finally:
        os.close(dir_fd)


def test_failed_temp_unlink_after_a_successful_publish_is_not_a_failure(
    root_fd, vault, monkeypatch
):
    """Once `link` succeeded the upload is published; janitorial noise is logged."""
    dir_fd = open_dir_beneath(root_fd, "Attachments")
    try:
        tmp = _write_temp(dir_fd, b"payload")

        real_unlink = vault_fs.os.unlink

        def flaky(name, *args, **kwargs):
            if name == tmp:
                raise OSError(errno.EIO, "unlink failed")
            return real_unlink(name, *args, **kwargs)

        monkeypatch.setattr(vault_fs.os, "unlink", flaky)
        result = publish(dir_fd, tmp, "a.png", overwrite=False, expected_fingerprint=None)
    finally:
        os.close(dir_fd)
    assert result.published is True
    assert result.temp_removed is False
    assert (vault / "Attachments" / "a.png").read_bytes() == b"payload"


# ── publish: overwrite ──────────────────────────────────────────────────────


def test_overwrite_publish_with_a_matching_fingerprint(root_fd, vault):
    target = vault / "Attachments" / "a.png"
    target.write_bytes(b"old")
    dir_fd = open_dir_beneath(root_fd, "Attachments")
    try:
        want = fingerprint(dir_fd, "a.png", hash_up_to=1000)
        tmp = _write_temp(dir_fd, b"new")
        result = publish(dir_fd, tmp, "a.png", overwrite=True, expected_fingerprint=want)
    finally:
        os.close(dir_fd)
    assert result.published is True
    assert target.read_bytes() == b"new"
    assert _temps(vault / "Attachments") == []


def test_overwrite_publish_with_a_changed_target_is_a_conflict(root_fd, vault):
    target = vault / "Attachments" / "a.png"
    target.write_bytes(b"old")
    dir_fd = open_dir_beneath(root_fd, "Attachments")
    try:
        want = fingerprint(dir_fd, "a.png", hash_up_to=1000)
        target.write_bytes(b"someone else's edit")
        tmp = _write_temp(dir_fd, b"new")
        with pytest.raises(Conflict):
            publish(dir_fd, tmp, "a.png", overwrite=True, expected_fingerprint=want)
    finally:
        os.close(dir_fd)
    assert target.read_bytes() == b"someone else's edit"
    assert _temps(vault / "Attachments") == []


def test_overwrite_publish_rehashes_when_metadata_is_identical(root_fd, vault):
    """Equal `dev/inode/size/mtime_ns/ctime_ns`, different bytes → Conflict.

    Every metadata field is made to agree by construction, so the *only* thing
    that can detect the change is a fresh hash read through the descriptor. If
    the re-hash is ever dropped as an optimisation this is the test that fails.
    """
    target = vault / "Attachments" / "a.png"
    target.write_bytes(b"AAAA")
    dir_fd = open_dir_beneath(root_fd, "Attachments")
    try:
        stale_hash = hashlib.sha256(b"AAAA").hexdigest()
        # Mutate in place, same length, then take the *current* metadata and
        # graft the stale hash onto it. ctime_ns cannot be restored by any
        # syscall, so grafting is the only way to isolate the hash.
        with open(target, "r+b") as fh:
            fh.write(b"BBBB")
        current = fingerprint(dir_fd, "a.png", hash_up_to=None)
        want = {**current, "sha256": stale_hash}
        assert want["sha256"] != hashlib.sha256(b"BBBB").hexdigest()

        tmp = _write_temp(dir_fd, b"new")
        with pytest.raises(Conflict):
            publish(dir_fd, tmp, "a.png", overwrite=True, expected_fingerprint=want)
    finally:
        os.close(dir_fd)
    assert target.read_bytes() == b"BBBB"


def test_overwrite_publish_above_the_hash_cap_is_metadata_only(root_fd, vault):
    """A null `sha256` inside a fingerprint means "too big to hash", not "absent".

    The metadata comparison still runs; only the content check is skipped. This
    is the documented weaker binding for files above MAX_FILE_WRITE_BYTES.
    """
    target = vault / "Attachments" / "big.bin"
    target.write_bytes(b"X" * 64)
    dir_fd = open_dir_beneath(root_fd, "Attachments")
    try:
        want = fingerprint(dir_fd, "big.bin", hash_up_to=1)  # below size → no hash
        assert want["sha256"] is None
        tmp = _write_temp(dir_fd, b"new")
        result = publish(dir_fd, tmp, "big.bin", overwrite=True, expected_fingerprint=want)
    finally:
        os.close(dir_fd)
    assert result.published is True
    assert target.read_bytes() == b"new"


def test_overwrite_publish_refuses_a_symlink_target(root_fd, vault, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside4")
    victim = outside / "victim"
    victim.write_bytes(b"precious")

    target = vault / "Attachments" / "a.png"
    target.write_bytes(b"old")
    dir_fd = open_dir_beneath(root_fd, "Attachments")
    try:
        want = fingerprint(dir_fd, "a.png", hash_up_to=1000)
        target.unlink()
        os.symlink(victim, target)
        tmp = _write_temp(dir_fd, b"new")
        with pytest.raises(UnsafePath):
            publish(dir_fd, tmp, "a.png", overwrite=True, expected_fingerprint=want)
    finally:
        os.close(dir_fd)
    assert victim.read_bytes() == b"precious"


def test_overwrite_publish_when_the_target_vanished_is_a_conflict(root_fd, vault):
    target = vault / "Attachments" / "a.png"
    target.write_bytes(b"old")
    dir_fd = open_dir_beneath(root_fd, "Attachments")
    try:
        want = fingerprint(dir_fd, "a.png", hash_up_to=1000)
        target.unlink()
        tmp = _write_temp(dir_fd, b"new")
        with pytest.raises(Conflict):
            publish(dir_fd, tmp, "a.png", overwrite=True, expected_fingerprint=want)
    finally:
        os.close(dir_fd)
    assert not target.exists()


def test_null_fingerprint_is_expected_absence_not_skip_the_check(root_fd, vault):
    """`overwrite=True` + null fingerprint = "was absent at mint, must still be".

    Treating null as "no comparison to make" would turn an overwrite token
    minted against nothing into a licence to clobber whatever appeared since.
    """
    dir_fd = open_dir_beneath(root_fd, "Attachments")
    try:
        tmp = _write_temp(dir_fd, b"mine")
        (vault / "Attachments" / "a.png").write_bytes(b"appeared later")
        with pytest.raises(Conflict):
            publish(dir_fd, tmp, "a.png", overwrite=True, expected_fingerprint=None)
    finally:
        os.close(dir_fd)
    assert (vault / "Attachments" / "a.png").read_bytes() == b"appeared later"
    assert _temps(vault / "Attachments") == []


def test_overwrite_window_is_optimistic_not_linearizable(root_fd, vault, monkeypatch):
    """Documents the check-then-act window between the fingerprint and `replace`.

    A writer that lands *inside* the window is overwritten. This is declared
    behaviour (design D5), at the same guarantee level as `edit_note`'s
    `expected=` guard. The test exists so the window is visible and measured
    rather than assumed away; closing it would need an exclusive lock the vault
    filesystem does not offer.
    """
    target = vault / "Attachments" / "a.png"
    target.write_bytes(b"old")
    dir_fd = open_dir_beneath(root_fd, "Attachments")
    real_replace = vault_fs.os.replace
    try:
        want = fingerprint(dir_fd, "a.png", hash_up_to=1000)
        tmp = _write_temp(dir_fd, b"new")

        def racing_replace(*args, **kwargs):
            target.write_bytes(b"a concurrent edit")
            return real_replace(*args, **kwargs)

        monkeypatch.setattr(vault_fs.os, "replace", racing_replace)
        result = publish(dir_fd, tmp, "a.png", overwrite=True, expected_fingerprint=want)
    finally:
        os.close(dir_fd)
    assert result.published is True
    # The concurrent edit is lost. Asserting it makes the limitation explicit.
    assert target.read_bytes() == b"new"


# ── deletion ────────────────────────────────────────────────────────────────


def test_soft_delete_moves_the_file_into_trash(root_fd, vault):
    (vault / "Attachments" / "a.png").write_bytes(b"bytes")
    dest = soft_delete(root_fd, "Attachments/a.png")
    assert dest.startswith(".trash/")
    assert TRASH_NAME.fullmatch(dest.split("/", 1)[1]).group("base") == "a.png"
    assert not (vault / "Attachments" / "a.png").exists()
    assert (vault / dest).read_bytes() == b"bytes"


def test_soft_delete_suffixes_colliding_basenames(root_fd, vault):
    (vault / "one").mkdir()
    (vault / "two").mkdir()
    (vault / "one" / "shot.png").write_bytes(b"first")
    (vault / "two" / "shot.png").write_bytes(b"second")

    a = soft_delete(root_fd, "one/shot.png")
    b = soft_delete(root_fd, "two/shot.png")
    assert a != b
    assert (vault / a).read_bytes() == b"first"
    assert (vault / b).read_bytes() == b"second"


def test_soft_delete_refuses_a_symlink(root_fd, vault, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside5")
    victim = outside / "victim"
    victim.write_bytes(b"precious")
    os.symlink(victim, vault / "Attachments" / "alias.png")
    with pytest.raises(UnsafePath):
        soft_delete(root_fd, "Attachments/alias.png")
    assert victim.exists()
    assert (vault / "Attachments" / "alias.png").is_symlink()


def test_soft_delete_refuses_a_directory(root_fd, vault):
    with pytest.raises(UnsafePath):
        soft_delete(root_fd, "Attachments")
    assert (vault / "Attachments").is_dir()


def test_soft_delete_moves_a_replacement_rather_than_destroying_it(
    root_fd, vault, monkeypatch
):
    """A file that replaces the source before the move must not be unlinked.

    `link` + `unlink` was two syscalls, and the second one removed whatever
    was at the name *then* — a writer landing in between had its file deleted
    with no trash copy of it, a silent destructive delete. A rename moves
    whichever inode is at the source when it runs, so the replacement lands in
    the trash intact and nothing is ever unlinked.
    """
    source = vault / "Attachments" / "a.png"
    source.write_bytes(b"original")

    real_rename = vault_fs.rename_noreplace

    def racing_rename(src_dir_fd, src_name, dst_dir_fd, dst_name):
        # The window: someone replaces the source name with a different inode
        # after the `lstat` said it was a plain file, and immediately before
        # the move runs.
        replacement = vault / "Attachments" / "a.png.new"
        replacement.write_bytes(b"replacement")
        os.replace(replacement, source)
        return real_rename(src_dir_fd, src_name, dst_dir_fd, dst_name)

    monkeypatch.setattr(vault_fs, "rename_noreplace", racing_rename)
    dest = soft_delete(root_fd, "Attachments/a.png")
    monkeypatch.undo()

    # The replacement was moved, not destroyed, and the trash holds exactly one
    # entry — no orphaned placeholder from a half-finished delete.
    assert (vault / dest).read_bytes() == b"replacement"
    assert not source.exists()
    assert [p.name for p in (vault / ".trash").iterdir()] == [dest.split("/", 1)[1]]


def test_soft_delete_reports_a_source_that_vanished_before_the_move(
    root_fd, vault, monkeypatch
):
    """Someone else deleted it first: nothing to move, nothing left behind."""
    source = vault / "Attachments" / "a.png"
    source.write_bytes(b"original")

    real_rename = vault_fs.rename_noreplace

    def vanishing_rename(src_dir_fd, src_name, dst_dir_fd, dst_name):
        source.unlink()
        return real_rename(src_dir_fd, src_name, dst_dir_fd, dst_name)

    monkeypatch.setattr(vault_fs, "rename_noreplace", vanishing_rename)
    with pytest.raises(FileNotFoundError):
        soft_delete(root_fd, "Attachments/a.png")
    monkeypatch.undo()

    # Nothing is left in the trash — and with no placeholder there is nothing
    # that *could* be left: an empty file there would claim a copy exists when
    # none does, and the old design had to remember to unlink it.
    assert list((vault / ".trash").iterdir()) == []


def test_soft_delete_never_replaces_a_name_somebody_else_holds(
    root_fd, vault, monkeypatch
):
    """The BLOCKER this primitive exists for.

    The old shape reserved the trash name with `O_EXCL` and then `os.rename`d
    onto it — and `rename` *replaces*. Anything that took that pathname over in
    between was silently destroyed, and the error path would have unlinked it
    too while tidying up "our" placeholder. `RENAME_NOREPLACE` cannot: the
    kernel refuses `EEXIST` and the delete simply picks another suffix.
    """
    source = vault / "Attachments" / "a.png"
    source.write_bytes(b"original")
    trash = vault / ".trash"
    trash.mkdir()
    taken: list[str] = []

    real_rename = vault_fs.rename_noreplace

    def contended_rename(src_dir_fd, src_name, dst_dir_fd, dst_name):
        if not taken:
            taken.append(dst_name)
            # A different writer gets to this exact name first.
            (trash / dst_name).write_bytes(b"someone else's file")
        return real_rename(src_dir_fd, src_name, dst_dir_fd, dst_name)

    monkeypatch.setattr(vault_fs, "rename_noreplace", contended_rename)
    dest = soft_delete(root_fd, "Attachments/a.png")
    monkeypatch.undo()

    created = dest.split("/", 1)[1]
    assert created != taken[0], "the delete reused the contended name"
    # Neither file was lost: the squatter's bytes are untouched and ours landed
    # somewhere else.
    assert (trash / taken[0]).read_bytes() == b"someone else's file"
    assert (vault / dest).read_bytes() == b"original"
    assert sorted(p.name for p in trash.iterdir()) == sorted([taken[0], created])


def test_rename_noreplace_refuses_an_existing_destination(root_fd, vault):
    """The primitive itself, with no `soft_delete` around it."""
    (vault / "Attachments" / "a.png").write_bytes(b"source")
    (vault / "Attachments" / "b.png").write_bytes(b"incumbent")
    dir_fd = open_dir_beneath(root_fd, "Attachments")
    try:
        with pytest.raises(FileExistsError):
            vault_fs.rename_noreplace(dir_fd, "a.png", dir_fd, "b.png")
    finally:
        os.close(dir_fd)
    assert (vault / "Attachments" / "a.png").read_bytes() == b"source"
    assert (vault / "Attachments" / "b.png").read_bytes() == b"incumbent"


def test_rename_noreplace_moves_across_directories(root_fd, vault):
    (vault / "Attachments" / "a.png").write_bytes(b"source")
    (vault / "dest").mkdir()
    src_fd = open_dir_beneath(root_fd, "Attachments")
    dst_fd = open_dir_beneath(root_fd, "dest")
    try:
        vault_fs.rename_noreplace(src_fd, "a.png", dst_fd, "moved.png")
    finally:
        os.close(src_fd)
        os.close(dst_fd)
    assert not (vault / "Attachments" / "a.png").exists()
    assert (vault / "dest" / "moved.png").read_bytes() == b"source"


def test_renameat2_is_available_on_this_platform():
    """A missing `renameat2` would disable every soft delete, loudly.

    Asserted rather than skipped around: the deployment is Linux/x86_64 on
    `python:3.12-slim` (glibc 2.36), where the wrapper has existed since 2.28.
    If this ever fails, the answer is a real port, not a silent fallback to a
    replacing rename.
    """
    assert vault_fs._renameat2_fn() is not None


@pytest.mark.parametrize(
    "code", [errno.EINVAL, errno.ENOSYS, errno.EXDEV, errno.EOPNOTSUPP]
)
def test_rename_noreplace_maps_unsupported_errnos(root_fd, vault, monkeypatch, code):
    """A kernel or filesystem without RENAME_NOREPLACE is a refusal, not a fallback."""
    monkeypatch.setattr(
        vault_fs, "_renameat2_raw", lambda *a, **k: code
    )
    with pytest.raises(UnsupportedFilesystem):
        vault_fs.rename_noreplace(root_fd, "a", root_fd, "b")


@pytest.mark.parametrize("code", [errno.EISDIR, errno.ENOTDIR])
def test_rename_noreplace_maps_kind_errnos(root_fd, monkeypatch, code):
    monkeypatch.setattr(vault_fs, "_renameat2_raw", lambda *a, **k: code)
    with pytest.raises(UnsafePath):
        vault_fs.rename_noreplace(root_fd, "a", root_fd, "b")


def test_rename_noreplace_propagates_unrelated_errnos(root_fd, monkeypatch):
    monkeypatch.setattr(vault_fs, "_renameat2_raw", lambda *a, **k: errno.EIO)
    with pytest.raises(OSError) as exc:
        vault_fs.rename_noreplace(root_fd, "a", root_fd, "b")
    assert not isinstance(exc.value, (UnsupportedFilesystem, UnsafePath))


def test_rename_noreplace_without_the_syscall_is_unsupported(root_fd, monkeypatch):
    """Symbol missing (glibc < 2.28 on an unlisted arch) → refuse, never replace."""
    monkeypatch.setattr(vault_fs, "_renameat2_fn", lambda: None)
    with pytest.raises(UnsupportedFilesystem):
        vault_fs.rename_noreplace(root_fd, "a", root_fd, "b")


def test_soft_delete_uses_a_no_replace_rename(root_fd, vault, monkeypatch):
    (vault / "Attachments" / "a.png").write_bytes(b"bytes")
    flags: list[int] = []
    real_raw = vault_fs._renameat2_raw

    def spy(src_dir_fd, src_name, dst_dir_fd, dst_name, flag):
        flags.append(flag)
        return real_raw(src_dir_fd, src_name, dst_dir_fd, dst_name, flag)

    monkeypatch.setattr(vault_fs, "_renameat2_raw", spy)
    soft_delete(root_fd, "Attachments/a.png")
    monkeypatch.undo()
    assert flags == [vault_fs.RENAME_NOREPLACE]


def test_concurrent_soft_deletes_of_the_same_basename_get_distinct_names(
    root_fd, vault
):
    """Same second, same basename, two threads: two files, two names."""
    for i in range(8):
        (vault / f"d{i}").mkdir()
        (vault / f"d{i}" / "shot.png").write_bytes(f"body-{i}".encode())

    with ThreadPoolExecutor(max_workers=8) as pool:
        dests = list(
            pool.map(lambda i: soft_delete(root_fd, f"d{i}/shot.png"), range(8))
        )

    assert len(set(dests)) == 8
    for i, dest in enumerate(dests):
        assert TRASH_NAME.fullmatch(dest.split("/", 1)[1]).group("base") == "shot.png"
        assert (vault / dest).read_bytes() == f"body-{i}".encode()
    assert len(list((vault / ".trash").iterdir())) == 8


def test_soft_delete_missing_file(root_fd):
    with pytest.raises(FileNotFoundError):
        soft_delete(root_fd, "Attachments/nope.png")


@pytest.mark.parametrize("code", [errno.EXDEV, errno.EINVAL, errno.ENOSYS])
def test_soft_delete_maps_an_unusable_trash(root_fd, vault, monkeypatch, code):
    """`.trash` on another mount, or a filesystem without RENAME_NOREPLACE.

    Both refuse the delete outright. Neither may degrade to a replacing
    `os.rename`, which is exactly what would make the trash name clobberable
    again.
    """
    (vault / "Attachments" / "a.png").write_bytes(b"bytes")

    monkeypatch.setattr(vault_fs, "_renameat2_raw", lambda *a, **k: code)
    with pytest.raises(UnsupportedFilesystem):
        soft_delete(root_fd, "Attachments/a.png")
    monkeypatch.undo()

    assert (vault / "Attachments" / "a.png").read_bytes() == b"bytes"
    assert list((vault / ".trash").iterdir()) == []


def test_remove_unlinks_permanently(root_fd, vault):
    (vault / "Attachments" / "a.png").write_bytes(b"bytes")
    remove(root_fd, "Attachments/a.png")
    assert not (vault / "Attachments" / "a.png").exists()
    assert not (vault / ".trash").exists()


def test_remove_refuses_a_symlink(root_fd, vault, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside6")
    victim = outside / "victim"
    victim.write_bytes(b"precious")
    os.symlink(victim, vault / "Attachments" / "alias.png")
    with pytest.raises(UnsafePath):
        remove(root_fd, "Attachments/alias.png")
    assert victim.exists()


def test_remove_refuses_a_directory(root_fd, vault):
    with pytest.raises(UnsafePath):
        remove(root_fd, "Attachments")
    assert (vault / "Attachments").is_dir()


def test_deletion_through_a_symlinked_ancestor_is_refused(root_fd, vault, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside7")
    (outside / "victim.png").write_bytes(b"precious")
    os.symlink(outside, vault / "link")
    with pytest.raises(UnsafePath):
        remove(root_fd, "link/victim.png")
    assert (outside / "victim.png").exists()


# ── filesystem probe ────────────────────────────────────────────────────────


def test_probe_publication_passes_on_a_normal_filesystem(root_fd, vault):
    probe_publication(root_fd)
    assert _temps(vault) == []


def test_probe_publication_does_not_create_the_trash(root_fd, vault):
    """It probes publication only; `.trash` belongs to the delete path."""
    probe_publication(root_fd)
    assert not (vault / ".trash").exists()


@pytest.mark.parametrize("code", [errno.EXDEV, errno.EPERM, errno.EOPNOTSUPP])
def test_probe_publication_maps_link_refusals(root_fd, vault, monkeypatch, code):
    def refuse(*args, **kwargs):
        raise OSError(code, os.strerror(code))

    monkeypatch.setattr(vault_fs.os, "link", refuse)
    with pytest.raises(UnsupportedFilesystem):
        probe_publication(root_fd)
    # Even on the failure path the probe leaves nothing behind.
    assert _temps(vault) == []


def test_probe_trash_passes_and_leaves_nothing(root_fd, vault):
    probe_trash(root_fd)
    assert _temps(vault) == []
    assert list((vault / ".trash").iterdir()) == []


@pytest.mark.parametrize("code", [errno.EXDEV, errno.EINVAL, errno.ENOSYS])
def test_probe_trash_catches_an_unusable_trash(root_fd, vault, monkeypatch, code):
    """A `.trash` on a separate mount, or one that cannot do RENAME_NOREPLACE.

    That combination is the dangerous one: `publish` keeps working, so nothing
    looks wrong until the first `delete_file` cannot move anything into the
    trash — and a naive implementation would have unlinked the original by then.
    `EINVAL`/`ENOSYS` are here because the probe must exercise the *flag*: a
    filesystem that renames fine but rejects `RENAME_NOREPLACE` would otherwise
    pass and then fail every single delete.
    """
    monkeypatch.setattr(vault_fs, "_renameat2_raw", lambda *a, **k: code)
    with pytest.raises(UnsupportedFilesystem, match=r"\.trash"):
        probe_trash(root_fd)
    monkeypatch.undo()
    assert _temps(vault) == []


def test_probe_trash_uses_a_no_replace_rename(root_fd, vault, monkeypatch):
    """The probe must exercise the same primitive the delete does."""
    flags: list[int] = []
    real_raw = vault_fs._renameat2_raw

    def spy(src_dir_fd, src_name, dst_dir_fd, dst_name, flag):
        flags.append(flag)
        return real_raw(src_dir_fd, src_name, dst_dir_fd, dst_name, flag)

    monkeypatch.setattr(vault_fs, "_renameat2_raw", spy)
    probe_trash(root_fd)
    monkeypatch.undo()
    assert flags == [vault_fs.RENAME_NOREPLACE]
    assert _temps(vault) == []
    assert list((vault / ".trash").iterdir()) == []


def test_probe_trash_tolerates_a_filesystem_without_hard_links(
    root_fd, vault, monkeypatch
):
    """A soft delete renames; it never links. Refusing here would be wrong."""
    def refuse(*args, **kwargs):
        raise OSError(errno.EPERM, "no links")

    monkeypatch.setattr(vault_fs.os, "link", refuse)
    probe_trash(root_fd)


def test_probe_publication_propagates_unrelated_errors(root_fd, monkeypatch):
    def refuse(*args, **kwargs):
        raise OSError(errno.EIO, "io error")

    monkeypatch.setattr(vault_fs.os, "link", refuse)
    with pytest.raises(OSError) as exc:
        probe_publication(root_fd)
    assert not isinstance(exc.value, UnsupportedFilesystem)


def test_the_probes_are_cached_independently(vault, monkeypatch):
    vault_fs.reset_filesystem_probe_cache()
    calls = []
    real_pub, real_trash = vault_fs.probe_publication, vault_fs.probe_trash

    monkeypatch.setattr(
        vault_fs, "probe_publication", lambda fd: (calls.append("pub"), real_pub(fd))[1]
    )
    monkeypatch.setattr(
        vault_fs, "probe_trash", lambda fd: (calls.append("trash"), real_trash(fd))[1]
    )
    vault_fs.check_publication_support(vault)
    vault_fs.check_publication_support(vault)
    vault_fs.check_trash_support(vault)
    vault_fs.check_trash_support(vault)
    assert calls == ["pub", "trash"]
    vault_fs.reset_filesystem_probe_cache()


def test_check_publication_support_reraises_the_cached_failure(vault, monkeypatch):
    vault_fs.reset_filesystem_probe_cache()

    def refuse(*args, **kwargs):
        raise OSError(errno.EPERM, "no links")

    monkeypatch.setattr(vault_fs.os, "link", refuse)
    with pytest.raises(UnsupportedFilesystem):
        vault_fs.check_publication_support(vault)
    # Second call must fail the same way from the cache, without touching disk.
    monkeypatch.setattr(
        vault_fs, "probe_publication", lambda fd: pytest.fail("reprobed")
    )
    with pytest.raises(UnsupportedFilesystem):
        vault_fs.check_publication_support(vault)
    vault_fs.reset_filesystem_probe_cache()


# ── stale staging sweep ─────────────────────────────────────────────────────


def _stage(vault: Path, name: str, age_seconds: float) -> Path:
    staging = vault / vault_fs.STAGING_DIR
    staging.mkdir(exist_ok=True)
    path = staging / name
    path.write_bytes(b"abandoned")
    when = time.time() - age_seconds
    os.utime(path, (when, when))
    return path


def test_prune_stale_staging_removes_only_the_old_ones(root_fd, vault):
    old = _stage(vault, ".tmp-old", 25 * 3600)
    fresh = _stage(vault, ".tmp-fresh", 5)
    kept = _stage(vault, "not-a-temp", 25 * 3600)

    assert vault_fs.prune_stale_staging(root_fd) == 1
    assert not old.exists()
    assert fresh.exists()
    assert kept.exists()


def test_prune_stale_staging_is_a_no_op_without_a_staging_dir(root_fd, vault):
    assert vault_fs.prune_stale_staging(root_fd) == 0
    assert not (vault / vault_fs.STAGING_DIR).exists()


def test_the_first_publication_probe_sweeps_stale_staging(vault, monkeypatch):
    """The sweep rides the probe: one walk, once per root, on a write path."""
    vault_fs.reset_filesystem_probe_cache()
    old = _stage(vault, ".tmp-old", 25 * 3600)
    fresh = _stage(vault, ".tmp-fresh", 5)

    vault_fs.check_publication_support(vault)
    assert not old.exists()
    assert fresh.exists()

    # Cached: a second call neither probes nor sweeps again.
    second = _stage(vault, ".tmp-old-2", 25 * 3600)
    vault_fs.check_publication_support(vault)
    assert second.exists()
    vault_fs.reset_filesystem_probe_cache()


# ── descriptor hygiene ──────────────────────────────────────────────────────


def _open_fd_count() -> int:
    return len(os.listdir("/proc/self/fd"))


@pytest.mark.skipif(not Path("/proc/self/fd").exists(), reason="needs /proc")
def test_failed_walks_do_not_leak_descriptors(root_fd, vault):
    (vault / "a").mkdir()
    os.symlink(vault / "a", vault / "a" / "loop")
    before = _open_fd_count()
    for _ in range(50):
        with pytest.raises(UnsafePath):
            open_dir_beneath(root_fd, "a/loop")
        with pytest.raises(FileNotFoundError):
            open_dir_beneath(root_fd, "a/missing")
    assert _open_fd_count() <= before + 2


def test_renameat2_syscall_table_matches_kernel_headers():
    """The raw-syscall fallback must never point at a *different* syscall:
    on arm64 ``__NR_renameat`` is 38 (a replacing rename) and
    ``__NR_renameat2`` is 276 (asm-generic). Pin the numbers we ship."""
    from src.services import vault_fs

    assert vault_fs._SYS_RENAMEAT2["x86_64"] == 316
    assert vault_fs._SYS_RENAMEAT2["aarch64"] == 276
    assert vault_fs._SYS_RENAMEAT2["armv7l"] == 382
    assert vault_fs._SYS_RENAMEAT2["i686"] == 353
    assert vault_fs._SYS_RENAMEAT2["ppc64le"] == 357
    assert vault_fs._SYS_RENAMEAT2["s390x"] == 347
