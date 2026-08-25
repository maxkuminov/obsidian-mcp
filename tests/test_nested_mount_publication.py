"""Transfer publication refuses a destination on another mount (D23, group 4).

A mount beneath the vault root is still *beneath the root*, so the beneath-root
lookup admits it and every read, note write and permanent delete across it works.
Publication does not: an upload stages in a root-level `.transfer-tmp` and
publishes with a `link` (no-clobber) or a `rename` (overwrite), and both return
`EXDEV` across a mount boundary. The publication probe cannot see it — it links
root→root and is cached per root, while this is a property of the *pair*.

**The comparison is of mount identity, never `st_dev`.** A bind mount of a
directory of the same filesystem, mounted beneath the vault root, reports the
same `st_dev` on both sides and still refuses the link and the rename. An
`st_dev` preflight passes and the publish fails after the body has streamed;
that was the first draft and review caught it. The real bind-mount cases live in
`tests/_nested_mount_cases.py` and run in their own mount namespace.

**The two checks promise different things and the difference is not cosmetic.**
The mint-time check spares the body, but only where the boundary already existed
when the capability was minted or the fetch began. The in-gate check catches one
established afterwards — still pre-*publication*, so nothing is written and the
claim is released, but by then the body has streamed in full. Nothing here
asserts a pre-body refusal for that second case, because there is none.
"""
from __future__ import annotations

import ctypes
import errno
import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest

from src.services import vault_fs

ROOT = Path(__file__).resolve().parent.parent
CASES = ROOT / "tests" / "_nested_mount_cases.py"


@pytest.fixture
def vault(tmp_path):
    (tmp_path / "Attachments").mkdir()
    vault_fs.reset_filesystem_probe_cache()
    vault_fs.reset_named_staging_state()
    yield tmp_path
    vault_fs.reset_filesystem_probe_cache()
    vault_fs.reset_named_staging_state()


def _fd(path: str) -> int:
    return os.open(path, os.O_RDONLY | os.O_DIRECTORY)


# ── 4.1 / 4.2: the statx binding ────────────────────────────────────────────


def test_the_statx_struct_matches_the_kernels():
    """A short buffer is how a `ctypes` binding corrupts memory rather than
    failing, so the layout is pinned rather than assumed."""
    assert ctypes.sizeof(vault_fs._Statx) == 256
    assert vault_fs._Statx.stx_mnt_id.offset == 144
    assert vault_fs._Statx.stx_mask.offset == 0


def test_statx_is_reached_through_the_glibc_wrapper_unlike_openat2():
    """D24's raw-syscall reasoning is about `openat2` and does not carry over:
    glibc exports `statx` and exports no `openat2` at any version."""
    assert vault_fs._statx_fn() is not None
    with pytest.raises(AttributeError):
        ctypes.CDLL(None).openat2


def test_two_real_mounts_are_told_apart():
    """A positive control that needs no privileges: `/` and `/proc` are
    different mounts on any Linux host."""
    root, proc = _fd("/"), _fd("/proc")
    try:
        assert vault_fs.mount_id_of(root) != vault_fs.mount_id_of(proc)
        assert vault_fs.same_mount(root, proc) is False
        assert vault_fs.same_mount(root, root) is True
    finally:
        os.close(root)
        os.close(proc)


def test_a_directory_and_the_root_it_lives_in_are_one_mount(vault):
    root, sub = _fd(str(vault)), _fd(str(vault / "Attachments"))
    try:
        assert vault_fs.same_mount(root, sub) is True
    finally:
        os.close(root)
        os.close(sub)


# ── 4.7: no mount-id bit means refuse, never fall back ──────────────────────


def _statx_without_the_mnt_id_bit(monkeypatch):
    """A kernel that answers `statx` successfully and sets no `STATX_MNT_ID`."""
    real = vault_fs._statx_fn()

    def stubbed(fd, path, flags, mask, buf):
        rc = real(fd, path, flags, mask, buf)
        buf.contents.stx_mask &= ~vault_fs.STATX_MNT_ID
        buf.contents.stx_mnt_id = 0
        return rc

    monkeypatch.setattr(vault_fs, "_statx_cache", stubbed)


def test_a_kernel_without_the_mount_id_bit_refuses_rather_than_using_st_dev(
    vault, monkeypatch
):
    """`st_dev` is exactly the comparison this replaces — a same-filesystem bind
    mount defeats it — so degrading to it would answer the wrong question while
    looking like it worked."""
    _statx_without_the_mnt_id_bit(monkeypatch)
    fd = _fd(str(vault))
    try:
        with pytest.raises(vault_fs.UnsupportedFilesystem) as exc:
            vault_fs.same_mount(fd, fd)
    finally:
        os.close(fd)
    message = str(exc.value)
    assert "STATX_MNT_ID" in message
    assert "st_dev" in message


def test_no_statx_at_all_refuses(vault, monkeypatch):
    monkeypatch.setattr(vault_fs, "_statx_cache", False)
    fd = _fd(str(vault))
    try:
        with pytest.raises(vault_fs.UnsupportedFilesystem, match="statx"):
            vault_fs.mount_id_of(fd)
    finally:
        os.close(fd)


def test_an_errno_from_statx_is_not_swallowed(vault, monkeypatch):
    def failing(fd, path, flags, mask, buf):
        ctypes.set_errno(errno.EACCES)
        return -1

    monkeypatch.setattr(vault_fs, "_statx_cache", failing)
    fd = _fd(str(vault))
    try:
        with pytest.raises(vault_fs.UnsupportedFilesystem, match="EACCES"):
            vault_fs.mount_id_of(fd)
    finally:
        os.close(fd)


# ── 4.4: the mint-time check ────────────────────────────────────────────────


def _mount_ids(monkeypatch, mapping: dict[str, int], default: int = 1):
    """Answer `mount_id_of` from the vault-relative name a descriptor points at."""

    def fake(fd: int) -> int:
        where = os.readlink(f"/proc/self/fd/{fd}")
        for suffix, value in mapping.items():
            if where.endswith(suffix):
                return value
        return default

    monkeypatch.setattr(vault_fs, "mount_id_of", fake)


def test_the_mint_check_refuses_a_destination_on_another_mount(vault, monkeypatch):
    _mount_ids(monkeypatch, {"/Attachments": 99})
    root_fd = _fd(str(vault))
    try:
        with pytest.raises(vault_fs.MountBoundary) as exc:
            vault_fs.require_destination_mount(root_fd, "Attachments/a.bin")
    finally:
        os.close(root_fd)
    message = str(exc.value)
    assert "different mount" in message
    assert "Attachments/a.bin" in message
    # Never a message blaming hard-link support: the filesystem is fine.
    assert "hard link" not in message


def test_the_mint_check_passes_on_a_vault_with_no_nested_mount(vault):
    root_fd = _fd(str(vault))
    try:
        vault_fs.require_destination_mount(root_fd, "Attachments/a.bin")
        vault_fs.require_destination_mount(root_fd, "a.bin")
    finally:
        os.close(root_fd)


def test_the_mint_check_uses_the_deepest_existing_ancestor(vault, monkeypatch):
    """A directory created beneath an ancestor is created on *that* ancestor's
    mount, so the ancestor answers the question the missing parent would."""
    asked: list[str] = []

    def fake(fd: int) -> int:
        asked.append(os.readlink(f"/proc/self/fd/{fd}"))
        return 1

    monkeypatch.setattr(vault_fs, "mount_id_of", fake)
    root_fd = _fd(str(vault))
    try:
        vault_fs.require_destination_mount(root_fd, "Attachments/New/Deep/a.bin")
    finally:
        os.close(root_fd)
    assert any(p.endswith("/Attachments") for p in asked), asked
    assert not any("New" in p for p in asked), asked


def test_the_mint_check_does_not_create_the_staging_directory(vault, monkeypatch):
    """A mint must not be the thing that creates `.transfer-tmp`, and it must
    not fail on a staging-directory policy check that belongs to the write."""
    _mount_ids(monkeypatch, {})
    root_fd = _fd(str(vault))
    try:
        vault_fs.require_destination_mount(root_fd, "Attachments/a.bin")
    finally:
        os.close(root_fd)
    assert not (vault / vault_fs.STAGING_DIR).exists()


# ── 5.0 item 3: the residual EXDEV, mapped the same way in both modes ───────


def _exdev(monkeypatch, name: str):
    def boom(*args, **kwargs):
        raise OSError(errno.EXDEV, "invalid cross-device link")

    monkeypatch.setattr(vault_fs.os, name, boom)


def test_a_cross_mount_no_clobber_link_names_the_boundary(vault, monkeypatch):
    """It used to say "the vault filesystem does not support hard links" —
    false, and it tells an operator to change filesystems in response to a
    mount layout."""
    root_fd = _fd(str(vault))
    try:
        dir_fd = vault_fs.open_dir_beneath(root_fd, "Attachments")
        try:
            fd, tmp = vault_fs.create_temp(dir_fd)
            os.close(fd)
            _exdev(monkeypatch, "link")
            with pytest.raises(vault_fs.MountBoundary) as exc:
                vault_fs.publish(
                    dir_fd, tmp, "a.bin",
                    overwrite=False, expected_fingerprint=None,
                )
        finally:
            os.close(dir_fd)
    finally:
        os.close(root_fd)
    assert "different mount" in str(exc.value)
    assert "hard link" not in str(exc.value)
    # Still an UnsupportedFilesystem, so every surface that already answers one
    # keeps answering it without a new branch.
    assert isinstance(exc.value, vault_fs.UnsupportedFilesystem)


def test_a_cross_mount_overwrite_rename_names_the_boundary(vault, monkeypatch):
    """The overwrite branch used to let `EXDEV` escape as a bare `OSError`:
    correctly classified pre-publication, but it reached the upload route's
    generic handler and gave the person a server error where the other mode
    gave a 503."""
    target = vault / "Attachments" / "a.bin"
    target.write_bytes(b"old")
    root_fd = _fd(str(vault))
    try:
        dir_fd = vault_fs.open_dir_beneath(root_fd, "Attachments")
        try:
            want = vault_fs.fingerprint(dir_fd, "a.bin", hash_up_to=10_000)
            fd, tmp = vault_fs.create_temp(dir_fd)
            os.close(fd)
            _exdev(monkeypatch, "replace")
            with pytest.raises(vault_fs.MountBoundary) as exc:
                vault_fs.publish(
                    dir_fd, tmp, "a.bin",
                    overwrite=True, expected_fingerprint=want,
                )
        finally:
            os.close(dir_fd)
    finally:
        os.close(root_fd)
    assert "different mount" in str(exc.value)
    assert isinstance(exc.value, vault_fs.UnsupportedFilesystem)
    assert target.read_bytes() == b"old"


def test_a_real_missing_hard_link_feature_still_says_so(vault, monkeypatch):
    """Splitting `EXDEV` out must not make the other errnos vague."""
    root_fd = _fd(str(vault))
    try:
        dir_fd = vault_fs.open_dir_beneath(root_fd, "Attachments")
        try:
            fd, tmp = vault_fs.create_temp(dir_fd)
            os.close(fd)

            def eperm(*args, **kwargs):
                raise OSError(errno.EPERM, "no links here")

            monkeypatch.setattr(vault_fs.os, "link", eperm)
            with pytest.raises(vault_fs.UnsupportedFilesystem) as exc:
                vault_fs.publish(
                    dir_fd, tmp, "a.bin",
                    overwrite=False, expected_fingerprint=None,
                )
        finally:
            os.close(dir_fd)
    finally:
        os.close(root_fd)
    assert "hard links" in str(exc.value)
    assert not isinstance(exc.value, vault_fs.MountBoundary)


# ── the probe cache is bound to the root's identity, not to its name ────────


def test_repointing_a_symlinked_root_re_probes_rather_than_reusing_a_verdict(
    tmp_path, monkeypatch
):
    """The finding's exact scenario.

    A configured root is a *name*. Repointing it from filesystem A — where the
    fallback's primitives work — to filesystem B, where a directory `fsync` is
    refused, must not reuse A's verdict and A's staging mode for B: that mints a
    token, stages a whole body under a name on a root nothing ever probed,
    publishes it, and strands the claim on the first directory flush.
    """
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    link = tmp_path / "vault-link"
    link.symlink_to(a)

    probed: list[tuple] = []

    def probe(root_fd):
        st = os.fstat(root_fd)
        probed.append((st.st_dev, st.st_ino))
        if (st.st_dev, st.st_ino) == (a.stat().st_dev, a.stat().st_ino):
            return vault_fs.STAGING_MODE_NAMED
        raise vault_fs.UnsupportedFilesystem("B cannot flush a directory")

    monkeypatch.setattr(vault_fs, "probe_publication", probe)

    assert vault_fs.check_publication_support(link) == vault_fs.STAGING_MODE_NAMED
    assert len(probed) == 1

    # Same name, same string key, different filesystem.
    link.unlink()
    link.symlink_to(b)

    with pytest.raises(vault_fs.UnsupportedFilesystem, match="cannot flush"):
        vault_fs.check_publication_support(link)
    assert len(probed) == 2, "B's verdict was taken from A's cache entry"

    vault_fs.reset_filesystem_probe_cache()


def test_an_unchanged_root_is_still_probed_exactly_once(tmp_path, monkeypatch):
    """The identity check must not turn the cache into a no-op: the whole point
    of caching is that a root is probed once."""
    a = tmp_path / "a"
    a.mkdir()
    link = tmp_path / "vault-link"
    link.symlink_to(a)

    probed = []

    def probe(root_fd):
        probed.append(True)
        return vault_fs.STAGING_MODE_UNNAMED

    monkeypatch.setattr(vault_fs, "probe_publication", probe)
    for _ in range(3):
        assert (
            vault_fs.check_publication_support(link)
            == vault_fs.STAGING_MODE_UNNAMED
        )
    assert len(probed) == 1
    vault_fs.reset_filesystem_probe_cache()


def test_a_supplied_root_descriptor_is_verified_before_the_cache_is_reused(
    tmp_path, monkeypatch
):
    """`_stream_locked` passes its own anchored descriptor. If that descriptor
    is not the root the entry was probed against, the entry is not about it."""
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    probed: list[int] = []

    def probe(root_fd):
        probed.append(os.fstat(root_fd).st_ino)
        return vault_fs.STAGING_MODE_UNNAMED

    monkeypatch.setattr(vault_fs, "probe_publication", probe)
    fd_a, fd_b = _fd(str(a)), _fd(str(b))
    try:
        # One configured name, two different anchored roots behind it.
        vault_fs.check_publication_support(a, root_fd=fd_a)
        vault_fs.check_publication_support(a, root_fd=fd_b)
    finally:
        os.close(fd_a)
        os.close(fd_b)
    assert probed == [a.stat().st_ino, b.stat().st_ino]
    vault_fs.reset_filesystem_probe_cache()


# ── 4.7: the real bind-mount cases, in their own mount namespace ────────────


def _unshare_available() -> bool:
    try:
        result = subprocess.run(
            ["unshare", "-Urm", "--propagation", "private", "true"],
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def test_nested_mount_cases_pass_in_a_mount_namespace(tmp_path):
    """A real `mount --bind` of a directory of the same filesystem.

    Everything above stubs the mount id, which pins the *policy* and cannot pin
    the premise. This one establishes the boundary for real and lets the kernel
    answer: same `st_dev` on both sides, different `STATX_MNT_ID`, and a `link`
    across it that really does return `EXDEV`.
    """
    if not _unshare_available():
        pytest.skip("unprivileged user+mount namespaces are unavailable here")

    vault = tmp_path / "vault"
    vault.mkdir()
    env = {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "HOME": str(tmp_path),
        "MCP_SANDBOX_MODE": "true",
        "VAULT_PATH": str(vault),
        "BASE_URL": "https://vault.example.test",
        "DATABASE_URL": "postgresql+asyncpg://test:test@localhost/test",
        "SECRET_KEY": "test",
        "MCP_HOSTNAME": "",
        "OMCP_TEST_TRUST_ENV": "1",
        "OMCP_NESTED_MOUNT_VAULT": str(vault),
    }
    result = subprocess.run(
        [
            "unshare", "-Urm", "--propagation", "private",
            sys.executable, "-m", "pytest", str(CASES),
            "-q", "-p", "no:cacheprovider", "-p", "no:randomly",
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert result.returncode == 0, (
        f"nested-mount cases failed\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )


# ── the leaf: a mount on the destination *file*, not its directory ──────────


def test_a_leaf_mount_is_refused_at_the_mint_for_an_overwrite(vault, monkeypatch):
    """The parent check cannot see this one: the boundary is the target itself."""
    (vault / "Attachments" / "a.bin").write_bytes(b"old")
    _mount_ids(monkeypatch, {"/Attachments/a.bin": 99})
    root_fd = _fd(str(vault))
    try:
        with pytest.raises(vault_fs.MountBoundary) as exc:
            vault_fs.require_destination_mount(
                root_fd, "Attachments/a.bin", overwrite=True
            )
    finally:
        os.close(root_fd)
    assert "itself a mount point" in str(exc.value)


def test_a_leaf_mount_is_not_consulted_for_a_no_clobber_mint(vault, monkeypatch):
    """A no-clobber publish onto an existing name is `EEXIST` whatever the
    mount layout, and "target already exists" is the accurate thing to say —
    replacing it with a mount-boundary refusal would be a worse message."""
    (vault / "Attachments" / "a.bin").write_bytes(b"old")
    _mount_ids(monkeypatch, {"/Attachments/a.bin": 99})
    root_fd = _fd(str(vault))
    try:
        vault_fs.require_destination_mount(root_fd, "Attachments/a.bin")
    finally:
        os.close(root_fd)


def test_a_missing_leaf_is_not_a_boundary(vault, monkeypatch):
    _mount_ids(monkeypatch, {})
    root_fd = _fd(str(vault))
    try:
        vault_fs.require_destination_mount(
            root_fd, "Attachments/absent.bin", overwrite=True
        )
    finally:
        os.close(root_fd)


def test_an_unreadable_leaf_mount_id_is_not_evidence_of_a_boundary(
    vault, monkeypatch
):
    """Inventing a boundary from a kernel that cannot answer would refuse a
    publish that would have worked."""
    (vault / "Attachments" / "a.bin").write_bytes(b"old")
    _statx_without_the_mnt_id_bit(monkeypatch)
    root_fd = _fd(str(vault))
    try:
        dir_fd = vault_fs.open_dir_beneath(root_fd, "Attachments")
        try:
            assert (
                vault_fs.leaf_is_separate_mount(root_fd, dir_fd, "a.bin") is False
            )
        finally:
            os.close(dir_fd)
    finally:
        os.close(root_fd)


def test_a_leaf_mount_does_not_follow_a_symlink(vault, monkeypatch):
    """`O_PATH|O_NOFOLLOW`: the mount identity read is of the name as written,
    never of whatever it points at."""
    target = vault / "Attachments" / "real.bin"
    target.write_bytes(b"x")
    os.symlink(target, vault / "Attachments" / "alias.bin")
    opened: list[int] = []
    real_open = vault_fs.os.open

    def recording_open(path, flags, *args, **kwargs):
        if path == "alias.bin":
            opened.append(flags)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(vault_fs.os, "open", recording_open)
    root_fd = _fd(str(vault))
    try:
        dir_fd = vault_fs.open_dir_beneath(root_fd, "Attachments")
        try:
            vault_fs.leaf_mount_id(dir_fd, "alias.bin")
        finally:
            os.close(dir_fd)
    finally:
        os.close(root_fd)
    assert opened, "the leaf was never opened"
    assert all(f & os.O_NOFOLLOW for f in opened), opened
    assert all(f & os.O_PATH for f in opened), opened


def test_rename_ebusy_is_only_a_boundary_when_a_fresh_check_says_so(
    vault, monkeypatch
):
    """`EBUSY` has other sources. Labelling all of them a mount boundary would
    send an operator after a mount that is not there."""
    target = vault / "Attachments" / "a.bin"
    target.write_bytes(b"old")
    root_fd = _fd(str(vault))
    try:
        dir_fd = vault_fs.open_dir_beneath(root_fd, "Attachments")
        try:
            want = vault_fs.fingerprint(dir_fd, "a.bin", hash_up_to=10_000)
            fd, tmp = vault_fs.create_temp(dir_fd)
            os.close(fd)

            def busy(*args, **kwargs):
                raise OSError(errno.EBUSY, "device or resource busy")

            monkeypatch.setattr(vault_fs.os, "replace", busy)
            # No leaf mount: the EBUSY is something else and stays an OSError.
            with pytest.raises(OSError) as exc:
                vault_fs.publish(
                    dir_fd, tmp, "a.bin",
                    overwrite=True, expected_fingerprint=want,
                )
            assert not isinstance(exc.value, vault_fs.MountBoundary)
            assert exc.value.errno == errno.EBUSY

            # Now a fresh check does establish that cause.
            fd, tmp2 = vault_fs.create_temp(dir_fd)
            os.close(fd)
            monkeypatch.setattr(
                vault_fs, "leaf_is_separate_mount", lambda *a, **k: True
            )
            with pytest.raises(vault_fs.MountBoundary, match="itself a mount point"):
                vault_fs.publish(
                    dir_fd, tmp2, "a.bin",
                    overwrite=True, expected_fingerprint=want,
                )
        finally:
            os.close(dir_fd)
    finally:
        os.close(root_fd)
    assert target.read_bytes() == b"old"


# ── the route says which refusal it is ──────────────────────────────────────


def test_the_route_bodies_are_distinguishable_and_path_free():
    """A mount boundary must not be reported as "the filesystem does not
    support atomic no-clobber publication" — false, and flatly so for an
    `overwrite=True` link, which does not use that publish at all."""
    from src.transfer import routes

    generic = routes.UNSUPPORTED_FS_BODY["error"]
    boundary = routes.MOUNT_BOUNDARY_BODY["error"]
    assert generic != boundary
    assert "mount" in boundary
    assert "no-clobber" not in boundary
    # Path-free: this route is unauthenticated beyond the bearer token.
    assert "/" not in boundary


def test_the_route_catches_mount_boundary_before_unsupported_filesystem():
    """`MountBoundary` subclasses `UnsupportedFilesystem`, so Python takes the
    first matching clause — the ordering is the whole fix."""
    import inspect

    from src.transfer import routes

    source = inspect.getsource(routes.upload)
    mount_at = [
        i for i, line in enumerate(source.splitlines())
        if "except vault_fs.MountBoundary" in line
    ]
    generic_at = [
        i for i, line in enumerate(source.splitlines())
        if "except vault_fs.UnsupportedFilesystem" in line
    ]
    # Two of each: the pre-stream probe and the publish itself.
    assert len(mount_at) == len(generic_at) == 2, (mount_at, generic_at)
    for mount, generic in zip(mount_at, generic_at):
        assert mount < generic, (
            "MountBoundary is caught after UnsupportedFilesystem in one of the "
            f"handlers, so it can never be reached: {mount_at} {generic_at}"
        )
    assert issubclass(vault_fs.MountBoundary, vault_fs.UnsupportedFilesystem)


# ── the 5.8 floor is a transfer-write minimum, and it is checked ────────────


def test_the_mount_identity_probe_creates_nothing(vault, monkeypatch):
    monkeypatch.chdir(vault)
    before = sorted(p.name for p in vault.iterdir())
    vault_fs.probe_mount_identity()
    assert sorted(p.name for p in vault.iterdir()) == before


def test_a_kernel_without_the_mount_id_bit_warns_and_keeps_serving(
    vault, monkeypatch, caplog
):
    """The decision, asserted: `STATX_MNT_ID` is a transfer-write minimum, not
    a server floor. Exiting would trade a transfer-only capability for a
    whole-server outage — the false-positive direction."""
    import src.main as main_module

    _statx_without_the_mnt_id_bit(monkeypatch)
    vault_fs.reset_mount_identity_state()
    try:
        with caplog.at_level(logging.WARNING, logger="src.main"):
            main_module._check_mount_identity_support()  # must not raise or exit
        assert vault_fs.mount_identity_available() is False
        warned = [r for r in caplog.records if "STATX_MNT_ID" in r.getMessage()]
        assert len(warned) == 1, [r.getMessage() for r in caplog.records]
        message = warned[0].getMessage()
        # It says exactly what is degraded and what is not.
        assert "request_upload" in message
        assert "not a server floor" in message
        assert warned[0].levelno == logging.WARNING
    finally:
        vault_fs.reset_mount_identity_state()


def test_a_working_kernel_records_the_capability(vault, monkeypatch):
    import src.main as main_module

    vault_fs.reset_mount_identity_state()
    try:
        assert vault_fs.mount_identity_available() is None
        main_module._check_mount_identity_support()
        assert vault_fs.mount_identity_available() is True
    finally:
        vault_fs.reset_mount_identity_state()


async def test_health_reports_the_transfer_mount_capability(vault, monkeypatch):
    import json

    import src.main as main_module

    vault_fs.reset_mount_identity_state()
    try:
        body = json.loads((await main_module.health()).body)
        # Never probed in this process: reported as found, not guessed.
        assert body["transfer_mount_check_available"] is None
        main_module._check_mount_identity_support()
        body = json.loads((await main_module.health()).body)
        assert body["transfer_mount_check_available"] is True
    finally:
        vault_fs.reset_mount_identity_state()


# ════════════════════════════════════════════════════════════════════════════
# nested-mount-honest-refusals — the vault side (#108, #109)
#
# The mount ids are stubbed here, which pins the *policy*. The premise — a
# same-filesystem bind mount really does refuse the rename — is pinned by the
# namespace harness above, where the kernel answers for itself.
# ════════════════════════════════════════════════════════════════════════════


@pytest.fixture
def note_vault(monkeypatch, tmp_path):
    """A vault the note tools can act on: settings, permission, no usage log."""
    import src.mcp_server.tools as tools_mod
    from src.mcp_server.auth import current_permission
    from src.services import vault as vault_service

    async def noop(*args, **kwargs):
        return None

    monkeypatch.setattr(vault_service.settings, "vault_path", str(tmp_path))
    monkeypatch.setattr(tools_mod, "_log_usage", noop)
    vault_fs.reset_filesystem_probe_cache()
    token = current_permission.set("readwrite")
    yield tmp_path
    current_permission.reset(token)
    vault_fs.reset_filesystem_probe_cache()


class _SessionSpy:
    """An `async_session` stand-in that records what a tool asks the database.

    `move_note` reads before it moves — the backlink plan and the vault index
    are SELECTs — so a zero-SQL contract is unimplementable and would be the
    wrong thing to assert. What a refusal owes is that no *mutating* statement
    ran and nothing was committed, which is what this separates: `dml` holds
    every `Update`/`Insert`/`Delete`, `commits` counts the commits, and the
    planning SELECTs are answered from `rows`/`backlinks` and otherwise
    ignored.
    """

    def __init__(self, rows=(), backlinks=()):
        self._rows = list(rows)
        self._backlinks = list(backlinks)
        self.dml: list = []
        self.commits = 0
        self._selects = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def execute(self, statement):
        from sqlalchemy.sql import Delete, Insert, Update

        if isinstance(statement, (Update, Insert, Delete)):
            self.dml.append(statement)
            return _Rows([])
        self._selects += 1
        # First SELECT is the vault index, the second the backlink sources.
        return _Rows(self._rows if self._selects == 1 else self._backlinks)

    async def commit(self):
        self.commits += 1


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _Row:
    def __init__(self, file_path, note_id=None):
        self.file_path = file_path
        self.id = note_id


def _install_session(monkeypatch, spy: "_SessionSpy") -> None:
    import src.mcp_server.tools as tools_mod

    monkeypatch.setattr(tools_mod, "async_session", lambda: spy)


async def test_a_cross_mount_move_is_refused_and_touches_no_database_row(
    note_vault, monkeypatch
):
    """#109. The refusal comes from the preflight, before the rename — and the
    proof that nothing was mutated is taken from the session, not read out of
    the refusal text. A tool that returned this message *after* updating
    `notes_metadata` would leave the index pointing at a path the vault does
    not have, which is exactly the class of silent wrongness this product ranks
    highest.
    """
    import src.mcp_server.tools as tools_mod

    (note_vault / "M").mkdir()
    (note_vault / "M" / "a.md").write_text("body\n", encoding="utf-8")
    _mount_ids(monkeypatch, {"/M": 99})
    spy = _SessionSpy()
    _install_session(monkeypatch, spy)

    result = await tools_mod.move_note_impl("M/a.md", "a.md")

    assert "different mounts" in result, result
    assert "mount layout is what refuses" in result
    # Never the message the primitive used to give for this layout.
    assert "not available" not in result
    assert (note_vault / "M" / "a.md").read_text() == "body\n"
    assert not (note_vault / "a.md").exists()
    assert spy.dml == [], "a refused move executed a mutating statement"
    assert spy.commits == 0, "a refused move committed"


async def test_a_cross_mount_move_with_planned_rewrites_mutates_nothing(
    note_vault, monkeypatch
):
    """The same claim with the expensive path taken: at least one backlink
    rewrite is *planned* — read, rewritten in memory, its descriptor pinned —
    and then the move is refused. Nothing is written: not the note, not the
    backlink source, not a row.
    """
    import src.mcp_server.tools as tools_mod

    (note_vault / "M").mkdir()
    (note_vault / "M" / "a.md").write_text("body\n", encoding="utf-8")
    backlink = note_vault / "b.md"
    backlink.write_text("see [[M/a]] for more\n", encoding="utf-8")
    before = backlink.read_bytes()

    planned: list[tuple[str, int]] = []
    real_rewrite = tools_mod._rewrite_links_in_text

    def spying_rewrite(content, from_rel, to_rel, source_path, *args, **kwargs):
        new, n = real_rewrite(content, from_rel, to_rel, source_path, *args, **kwargs)
        planned.append((source_path, n))
        return new, n

    monkeypatch.setattr(tools_mod, "_rewrite_links_in_text", spying_rewrite)
    _mount_ids(monkeypatch, {"/M": 99})
    spy = _SessionSpy(
        rows=[_Row("M/a.md", 1), _Row("b.md", 2)], backlinks=[_Row("b.md")]
    )
    _install_session(monkeypatch, spy)

    result = await tools_mod.move_note_impl("M/a.md", "a.md", rewrite_links=True)

    assert "different mounts" in result, result
    assert [p for p in planned if p[0] == "b.md" and p[1] > 0], planned
    assert backlink.read_bytes() == before, "a refused move rewrote a backlink"
    assert (note_vault / "M" / "a.md").read_text() == "body\n"
    assert not (note_vault / "a.md").exists()
    assert spy.dml == []
    assert spy.commits == 0


async def test_a_cross_mount_move_to_a_missing_folder_creates_nothing(
    note_vault, monkeypatch
):
    """Codex finding 4. `MutableTarget.dir_fd` creates a missing parent on
    first use, so a preflight that asked it the mount question would `mkdir`
    the destination folder and *then* refuse — a mutation performed by the
    check whose whole point is to refuse before any mutation. The comparison
    runs against the deepest existing ancestor instead.
    """
    import src.mcp_server.tools as tools_mod

    (note_vault / "M").mkdir()
    (note_vault / "M" / "a.md").write_text("body\n", encoding="utf-8")
    _mount_ids(monkeypatch, {"/M": 99})
    spy = _SessionSpy()
    _install_session(monkeypatch, spy)

    result = await tools_mod.move_note_impl("M/a.md", "New/Sub/a.md")

    assert "different mounts" in result, result
    assert not (note_vault / "New").exists(), "the preflight created a directory"
    assert not (note_vault / "New" / "Sub").exists()
    assert (note_vault / "M" / "a.md").read_text() == "body\n"
    assert spy.dml == []
    assert spy.commits == 0


async def test_a_move_on_one_side_of_the_boundary_still_works(
    note_vault, monkeypatch
):
    """The refusal is per pair. A vault that contains a nested mount somewhere
    keeps every move that does not cross it."""
    import src.mcp_server.tools as tools_mod

    (note_vault / "M").mkdir()
    (note_vault / "Folder").mkdir()
    (note_vault / "a.md").write_text("body\n", encoding="utf-8")
    _mount_ids(monkeypatch, {"/M": 99})
    _install_session(monkeypatch, _SessionSpy())

    result = await tools_mod.move_note_impl("a.md", "Folder/a.md")

    assert "different mounts" not in result, result
    assert (note_vault / "Folder" / "a.md").read_text() == "body\n"


async def test_a_cross_mount_soft_delete_names_the_layout_and_the_workaround(
    note_vault, monkeypatch
):
    """#108, at the preflight. `.trash` is opened beneath the *root*, so a note
    on a mount nested below it can never be soft-deleted — but the message used
    to blame `.trash/`'s ability to receive a non-replacing rename, which is a
    property of the filesystem and has nothing to say about the layout.
    """
    import src.mcp_server.tools as tools_mod

    (note_vault / "M").mkdir()
    (note_vault / "M" / "a.md").write_text("body\n", encoding="utf-8")
    _mount_ids(monkeypatch, {"/M": 99})
    _install_session(monkeypatch, _SessionSpy())

    result = await tools_mod.delete_note_impl("M/a.md")

    assert "different mounts" in result, result
    assert "permanent=True" in result
    assert "cannot receive a non-replacing rename" not in result
    assert (note_vault / "M" / "a.md").read_text() == "body\n"
    trash = note_vault / ".trash"
    assert not trash.exists() or list(trash.iterdir()) == []


async def test_a_cross_mount_delete_file_names_the_layout_too(
    note_vault, monkeypatch
):
    """`delete_note` and `delete_file` share the primitive, which is the point
    of putting the refusal in `soft_delete_at` rather than in either tool."""
    import src.mcp_server.tools as tools_mod

    (note_vault / "M").mkdir()
    (note_vault / "M" / "a.bin").write_bytes(b"bytes")
    _mount_ids(monkeypatch, {"/M": 99})

    result = await tools_mod.delete_file_impl("M/a.bin")

    assert "different mounts" in result, result
    assert "permanent=True" in result
    assert (note_vault / "M" / "a.bin").read_bytes() == b"bytes"


async def test_a_permanent_delete_still_crosses_the_boundary(
    note_vault, monkeypatch
):
    """An unlink crosses no mount boundary, so the workaround the message names
    has to actually work."""
    import src.mcp_server.tools as tools_mod

    (note_vault / "M").mkdir()
    (note_vault / "M" / "a.bin").write_bytes(b"bytes")
    _mount_ids(monkeypatch, {"/M": 99})

    result = await tools_mod.delete_file_impl("M/a.bin", permanent=True)

    assert "different mounts" not in result, result
    assert not (note_vault / "M" / "a.bin").exists()


# ── the degraded kernel: fail *open* here, fail *closed* on the transfer path ─


def _no_mount_ids(monkeypatch) -> None:
    """A kernel between the `openat2` floor (5.6) and `STATX_MNT_ID` (5.8)."""

    def refuse(fd: int) -> int:
        raise vault_fs.UnsupportedFilesystem("no STATX_MNT_ID on this kernel")

    monkeypatch.setattr(vault_fs, "mount_id_of", refuse)


def test_the_helper_answers_false_when_the_kernel_cannot_tell(vault, monkeypatch):
    _no_mount_ids(monkeypatch)
    fd = _fd(str(vault))
    try:
        assert vault_fs.cross_mount_definitely(fd, fd) is False
    finally:
        os.close(fd)


async def test_a_degraded_kernel_keeps_its_soft_delete(note_vault, monkeypatch):
    """Failing closed here would remove soft delete from a deployment that
    serves it correctly — the failure a preflight prevents costs nothing,
    because the rename refuses immediately and accurately either way."""
    import src.mcp_server.tools as tools_mod

    (note_vault / "a.bin").write_bytes(b"bytes")
    _no_mount_ids(monkeypatch)

    result = await tools_mod.delete_file_impl("a.bin")

    assert "different mounts" not in result, result
    assert not (note_vault / "a.bin").exists()
    assert len(list((note_vault / ".trash").iterdir())) == 1


async def test_a_degraded_kernel_keeps_its_moves(note_vault, monkeypatch):
    import src.mcp_server.tools as tools_mod

    (note_vault / "Folder").mkdir()
    (note_vault / "a.md").write_text("body\n", encoding="utf-8")
    _no_mount_ids(monkeypatch)
    _install_session(monkeypatch, _SessionSpy())

    result = await tools_mod.move_note_impl("a.md", "Folder/a.md")

    assert "different mounts" not in result, result
    assert (note_vault / "Folder" / "a.md").read_text() == "body\n"


def test_the_transfer_path_still_fails_closed_on_the_same_kernel(
    vault, monkeypatch
):
    """The two directions are opposite on purpose and this is where that is
    pinned. A late `EXDEV` on the transfer path costs a body that has already
    streamed in full, so "cannot check" must not mint; on the vault path the
    same answer costs nothing, so it proceeds."""
    _no_mount_ids(monkeypatch)
    fd = _fd(str(vault))
    try:
        with pytest.raises(vault_fs.UnsupportedFilesystem):
            vault_fs.require_same_mount(fd, fd, "Attachments/a.bin")
    finally:
        os.close(fd)
