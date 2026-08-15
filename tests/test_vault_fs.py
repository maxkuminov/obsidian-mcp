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
    probe_filesystem,
    publish,
    remove,
    soft_delete,
)


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
    assert dest.endswith("-a.png")
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


def test_soft_delete_does_not_unlink_a_replacement(root_fd, vault, monkeypatch):
    """A file that replaces the source between link and unlink must survive.

    `link` + `unlink` is two syscalls. Without an inode check the second one
    removes whatever is at the name *now* — so a writer landing in between has
    its file deleted with no trash copy of it, which is a silent destructive
    delete. The check makes that a `Conflict` instead.
    """
    source = vault / "Attachments" / "a.png"
    source.write_bytes(b"original")

    real_link = os.link

    def swapping_link(*args, **kwargs):
        real_link(*args, **kwargs)
        # The window: the trash now holds `original`, and someone replaces the
        # source name with a different inode.
        replacement = vault / "Attachments" / "a.png.new"
        replacement.write_bytes(b"replacement")
        os.replace(replacement, source)

    monkeypatch.setattr(os, "link", swapping_link)
    with pytest.raises(Conflict):
        soft_delete(root_fd, "Attachments/a.png")
    monkeypatch.undo()

    # The replacement is untouched, and the trash link we made was cleaned up
    # rather than left as a half-finished delete.
    assert source.read_bytes() == b"replacement"
    trash = vault / ".trash"
    assert not trash.exists() or list(trash.iterdir()) == []


def test_soft_delete_succeeds_when_the_source_vanished_after_the_link(
    root_fd, vault, monkeypatch
):
    """Someone else deleted it; our trash copy is still the promised copy."""
    source = vault / "Attachments" / "a.png"
    source.write_bytes(b"original")

    real_link = os.link

    def vanishing_link(*args, **kwargs):
        real_link(*args, **kwargs)
        source.unlink()

    monkeypatch.setattr(os, "link", vanishing_link)
    dest = soft_delete(root_fd, "Attachments/a.png")
    monkeypatch.undo()

    assert (vault / dest).read_bytes() == b"original"
    assert not source.exists()


def test_soft_delete_missing_file(root_fd):
    with pytest.raises(FileNotFoundError):
        soft_delete(root_fd, "Attachments/nope.png")


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


def test_probe_filesystem_passes_on_a_normal_filesystem(root_fd, vault):
    probe_filesystem(root_fd)
    assert _temps(vault) == []


@pytest.mark.parametrize("code", [errno.EXDEV, errno.EPERM, errno.EOPNOTSUPP])
def test_probe_filesystem_maps_link_refusals(root_fd, vault, monkeypatch, code):
    def refuse(*args, **kwargs):
        raise OSError(code, os.strerror(code))

    monkeypatch.setattr(vault_fs.os, "link", refuse)
    with pytest.raises(UnsupportedFilesystem):
        probe_filesystem(root_fd)
    # Even on the failure path the probe leaves nothing behind.
    assert _temps(vault) == []


def test_probe_filesystem_catches_a_cross_device_trash(root_fd, vault, monkeypatch):
    """A `.trash` on a separate mount passes the in-root link and fails this one.

    That combination is the dangerous one: `publish` keeps working, so nothing
    looks wrong until the first `delete_file` cannot link into the trash — by
    which point a naive implementation has already unlinked the original.
    """
    real_link = vault_fs.os.link
    calls = []

    def refuse_second(*args, **kwargs):
        calls.append(kwargs.get("dst_dir_fd"))
        if len(calls) == 1:
            return real_link(*args, **kwargs)
        raise OSError(errno.EXDEV, os.strerror(errno.EXDEV))

    monkeypatch.setattr(vault_fs.os, "link", refuse_second)
    with pytest.raises(UnsupportedFilesystem, match=r"\.trash"):
        probe_filesystem(root_fd)
    assert _temps(vault) == []


def test_probe_filesystem_propagates_unrelated_errors(root_fd, monkeypatch):
    def refuse(*args, **kwargs):
        raise OSError(errno.EIO, "io error")

    monkeypatch.setattr(vault_fs.os, "link", refuse)
    with pytest.raises(OSError) as exc:
        probe_filesystem(root_fd)
    assert not isinstance(exc.value, UnsupportedFilesystem)


def test_check_filesystem_support_caches_the_verdict(vault, monkeypatch):
    vault_fs.reset_filesystem_probe_cache()
    calls = []
    real = vault_fs.probe_filesystem

    def counting(fd):
        calls.append(fd)
        return real(fd)

    monkeypatch.setattr(vault_fs, "probe_filesystem", counting)
    vault_fs.check_filesystem_support(vault)
    vault_fs.check_filesystem_support(vault)
    assert len(calls) == 1
    vault_fs.reset_filesystem_probe_cache()


def test_check_filesystem_support_reraises_the_cached_failure(vault, monkeypatch):
    vault_fs.reset_filesystem_probe_cache()

    def refuse(*args, **kwargs):
        raise OSError(errno.EPERM, "no links")

    monkeypatch.setattr(vault_fs.os, "link", refuse)
    with pytest.raises(UnsupportedFilesystem):
        vault_fs.check_filesystem_support(vault)
    # Second call must fail the same way from the cache, without touching disk.
    monkeypatch.setattr(vault_fs, "probe_filesystem", lambda fd: pytest.fail("reprobed"))
    with pytest.raises(UnsupportedFilesystem):
        vault_fs.check_filesystem_support(vault)
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
