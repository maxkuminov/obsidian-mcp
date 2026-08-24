"""Every below-root directory descriptor comes from one `openat2` (#87).

`vault_fs.open_dir_beneath` used to open one component at a time with
`O_NOFOLLOW`. Each open was individually safe; the *sequence* was not. Between
opening ancestor `A` and opening its child `B`, another process could rename
`<vault>/A` out of the vault, and the descriptor the walk went on to return —
with every mutation of the call anchored to it — was then outside the root,
with nothing later able to notice.

It is now one `openat2(RESOLVE_BENEATH | RESOLVE_NO_SYMLINKS |
RESOLVE_NO_MAGICLINKS)`, so there is no interval between components to race,
and there is deliberately no fallback to the old walk.

Three things these tests are careful about, because each was a way to write a
test that passes while the guarantee is gone:

* **`_split` still runs in front of the syscall.** `RESOLVE_BENEATH` *scopes*
  `..` rather than forbidding it — `A/../A` succeeds at the kernel, which
  `test_the_kernel_would_accept_a_normalised_parent_traversal` pins — so a
  test that only asserted "`..` is refused" would keep passing if the lexical
  guard were deleted.
* **Unavailability must refuse, never degrade.** A containment guard that
  quietly falls back is the failure mode being removed, and it would be
  invisible in every test, because tests run on a kernel that has the syscall.
  So the unavailability errnos are injected.
* **`EINTR` must be retried.** The walk being replaced went through `os.open`,
  which retries it transparently under PEP 475; a raw `ctypes` syscall does
  not. Without an explicit retry an ordinary signal becomes a false failure of
  a write.
"""
import errno
import os
import subprocess
import sys
import textwrap
import threading
from pathlib import Path

import pytest

import src.mcp_server.tools as tools
from src.mcp_server.auth import current_permission
from src.services import vault as vault_service
from src.services import vault_fs
from src.services.vault_fs import (
    UnsafePath,
    UnsupportedFilesystem,
    VaultFSError,
    open_dir_beneath,
    open_root,
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


def _inject(monkeypatch, codes):
    """Make `_openat2_raw` answer from `codes`, recording every call.

    `codes` is a list of errnos; `0` means "fall through to the real syscall".
    The list is consumed in order and its last entry repeats forever, so a
    single-element list is a permanent condition.
    """
    real = vault_fs._openat2_raw
    calls: list[tuple[str, int]] = []
    remaining = list(codes)

    def fake(dir_fd, path, flags, resolve):
        code = remaining.pop(0) if len(remaining) > 1 else remaining[0]
        calls.append((path, code))
        if code == 0:
            return real(dir_fd, path, flags, resolve)
        return -1, code

    monkeypatch.setattr(vault_fs, "_openat2_raw", fake)
    return calls


# ── the flags and the number are the whole guarantee ─────────────────────────


def test_the_resolve_flags_are_the_reviewed_set():
    """`RESOLVE_NO_XDEV` is deliberately absent (D16).

    It buys nothing for containment — that is `RESOLVE_BENEATH`'s job, and a
    mount point beneath the root is still beneath the root — while setting it
    would refuse *lookups* through a mount point, which every read,
    `delete_file`, the note tools and the transfer path share. It would break
    every path that works across a nested mount and fix none of the three that
    do not.
    """
    assert vault_fs.RESOLVE_BENEATH == 0x08
    assert vault_fs.RESOLVE_NO_SYMLINKS == 0x04
    assert vault_fs.RESOLVE_NO_MAGICLINKS == 0x02
    assert vault_fs._RESOLVE_STRICT == 0x08 | 0x04 | 0x02
    assert vault_fs._RESOLVE_STRICT & 0x01 == 0, "RESOLVE_NO_XDEV must not be set"


def test_openat2_syscall_table_pins_reviewed_constants():
    """Pin the syscall numbers checked against the Linux tables during review.

    Unlike `_SYS_RENAMEAT2` this table is not a fallback for an old glibc — it
    is the implementation, because glibc exports no `openat2` wrapper at any
    version (D24). A wrong number would call a *different* syscall.
    """
    assert vault_fs._SYS_OPENAT2["x86_64"] == 437
    assert vault_fs._SYS_OPENAT2["aarch64"] == 437
    assert set(vault_fs._SYS_OPENAT2.values()) == {437}


def test_glibc_exports_no_openat2_wrapper():
    """The measurement D24 rests on, re-run against the interpreter's own libc.

    If a future glibc ever grew the wrapper this would fail and the raw-syscall
    reasoning in `_resolve_openat2` would deserve a second look. Nothing breaks
    either way — the raw path stays correct — so this is a record, not a gate.
    """
    import ctypes

    libc = ctypes.CDLL(None, use_errno=True)
    with pytest.raises(AttributeError):
        libc.openat2


def test_struct_open_how_is_three_u64s():
    """The `size` argument is how the kernel versions the structure, so the
    layout and its `sizeof` are part of the ABI contract, not an internal
    detail. A wrong size is `EINVAL` — see the ABI branch below."""
    import ctypes

    assert ctypes.sizeof(vault_fs._OpenHow) == 24
    assert [name for name, _ in vault_fs._OpenHow._fields_] == [
        "flags",
        "mode",
        "resolve",
    ]


def test_the_per_component_walk_is_gone():
    """No silent fallback means no walk left to fall back *to*."""
    assert not hasattr(vault_fs, "_open_child")
    assert not hasattr(vault_fs, "_open_dir_nofollow")


# ── one lookup, not a walk ───────────────────────────────────────────────────


def test_a_lookup_is_one_syscall_not_a_per_component_walk(monkeypatch, root_fd, vault):
    """The property, stated as a count.

    A three-component path costs exactly one `openat2`. There is no "between
    two opens" for a rename to land in, which is the whole of #87.
    """
    (vault / "a" / "b" / "c").mkdir(parents=True)
    calls = _inject(monkeypatch, [0])
    fd = open_dir_beneath(root_fd, "a/b/c")
    os.close(fd)
    assert [path for path, _ in calls] == ["a/b/c"]


def test_the_root_itself_is_one_lookup_of_dot(monkeypatch, root_fd):
    calls = _inject(monkeypatch, [0])
    fd = open_dir_beneath(root_fd, "")
    os.close(fd)
    assert [path for path, _ in calls] == ["."]


def test_a_renaming_ancestor_never_yields_a_foreign_directory(
    root_fd, vault, tmp_path_factory
):
    """The race the change exists to close, fired for real.

    A thread renames `<vault>/A` out of the vault and back while the main
    thread resolves `A/B`. Every lookup that *succeeds* must have resolved our
    own `A/B` — identified through the returned descriptor — and every failure
    must use this module's own vocabulary rather than an unmapped `OSError`.

    **This test does not discriminate against the per-component walk, and it is
    not trying to.** It cannot: `A/B` keeps its inode across the rename, so the
    walk's outside-the-root descriptor holds the same marker; and asserting the
    descriptor's *location* instead would be wrong in the other direction,
    because a lookup proves containment when it resolves and not afterwards
    (D26) — a rename landing microseconds after a correct lookup legitimately
    puts a correct descriptor outside the root.

    What it is for is the invariant under real concurrency: no foreign
    directory, no unmapped errno, no descriptor leak. The deterministic
    discriminator against the old walk is
    `test_a_lookup_is_one_syscall_not_a_per_component_walk` — with one syscall
    there is no "between two opens" for a rename to land in.
    """
    outside = tmp_path_factory.mktemp("outside")
    (vault / "A" / "B").mkdir(parents=True)
    (vault / "A" / "B" / "marker").write_bytes(b"ours")

    stop = threading.Event()

    def churn():
        while not stop.is_set():
            try:
                os.rename(vault / "A", outside / "A")
                os.rename(outside / "A", vault / "A")
            except OSError:
                pass

    mover = threading.Thread(target=churn, daemon=True)
    mover.start()
    try:
        for _ in range(2000):
            try:
                fd = open_dir_beneath(root_fd, "A/B")
            except (FileNotFoundError, UnsafePath, VaultFSError):
                continue
            try:
                names = os.listdir(fd)
            finally:
                os.close(fd)
            assert names == ["marker"], f"resolved a directory we never made: {names}"
    finally:
        stop.set()
        mover.join(timeout=5)
    # Put it back if the churn thread died mid-rename, so teardown can clean up.
    if (outside / "A").exists() and not (vault / "A").exists():
        os.rename(outside / "A", vault / "A")


# ── the lexical guard stays in front of the syscall ──────────────────────────


def test_parent_traversal_is_refused_before_the_syscall(monkeypatch, root_fd):
    calls = _inject(monkeypatch, [0])
    with pytest.raises(UnsafePath, match="Parent traversal not allowed"):
        open_dir_beneath(root_fd, "Attachments/../../escape")
    assert calls == [], "`..` must be refused lexically, not by an errno"


def test_absolute_paths_are_refused_before_the_syscall(monkeypatch, root_fd):
    calls = _inject(monkeypatch, [0])
    with pytest.raises(UnsafePath, match="Absolute path not allowed"):
        open_dir_beneath(root_fd, "/etc")
    assert calls == []


def test_nul_bytes_are_refused_before_the_syscall(monkeypatch, root_fd):
    calls = _inject(monkeypatch, [0])
    with pytest.raises(UnsafePath, match="Illegal path component"):
        open_dir_beneath(root_fd, "Attach\0ments")
    assert calls == []


def test_the_kernel_would_accept_a_normalised_parent_traversal(root_fd, vault):
    """Why `_split` is not redundant with `RESOLVE_BENEATH` (D17).

    `RESOLVE_BENEATH` scopes `..` rather than forbidding it, so `A/../A`
    resolves fine at the kernel. This module's posture is that nothing is
    normalised on our behalf, so the lexical guard refuses it first — and this
    test is what would fail if somebody deleted the guard as "already covered".
    """
    (vault / "A").mkdir()
    fd, code = vault_fs._openat2_raw(
        root_fd, "A/../A", vault_fs._O_LOOKUP, vault_fs._RESOLVE_STRICT
    )
    assert code == 0, "the kernel is expected to accept this; _split is what does not"
    os.close(fd)
    with pytest.raises(UnsafePath, match="Parent traversal not allowed"):
        open_dir_beneath(root_fd, "A/../A")


# ── errno mapping, one test per branch (1.4) ─────────────────────────────────


def test_a_symlinked_component_is_a_traversal_refusal(root_fd, vault, tmp_path_factory):
    outside = tmp_path_factory.mktemp("linked")
    (outside / "sub").mkdir()
    os.symlink(outside, vault / "link")
    with pytest.raises(UnsafePath) as caught:
        open_dir_beneath(root_fd, "link/sub")
    assert "link/sub" in str(caught.value)


def test_a_symlinked_component_pointing_inside_is_refused_too(root_fd, vault):
    """The guarantee is "no symlink is traversed", not "no symlink that happens
    to escape is traversed": a link pointing inside today can be repointed
    outside a microsecond later."""
    (vault / "real").mkdir()
    os.symlink(vault / "real", vault / "alias")
    with pytest.raises(UnsafePath):
        open_dir_beneath(root_fd, "alias")


def test_the_traversal_error_names_the_requested_path_not_a_component(
    monkeypatch, root_fd
):
    """D25. One `openat2` reports `ELOOP` for the resolution as a whole and
    says nothing about which component caused it, and a diagnostic walk issued
    afterwards would report a different state than the one the kernel refused —
    no link at all, or the wrong one, authoritatively. So the message names the
    requested vault-relative path and claims nothing about components."""
    _inject(monkeypatch, [errno.ELOOP])
    with pytest.raises(UnsafePath) as caught:
        open_dir_beneath(root_fd, "A/B/C")
    message = str(caught.value)
    assert "A/B/C" in message
    assert "'B'" not in message and '"B"' not in message


def test_exdev_is_a_containment_refusal_not_an_unsupported_filesystem(
    monkeypatch, root_fd
):
    """D17. `rename_noreplace` maps `EXDEV` to `UnsupportedFilesystem` — there
    it means "different devices". From `openat2` it means the resolution would
    have escaped the root, which is the opposite kind of event: an attack was
    blocked, or a path was wrong. Telling an operator to change filesystems in
    response to a blocked escape is the failure this pins."""
    _inject(monkeypatch, [errno.EXDEV])
    with pytest.raises(UnsafePath) as caught:
        open_dir_beneath(root_fd, "A/B")
    assert not isinstance(caught.value, UnsupportedFilesystem)
    assert "outside the vault root" in str(caught.value)
    assert "A/B" in str(caught.value)


def test_enoent_is_still_a_not_found(monkeypatch, root_fd):
    """Absence must stay distinguishable from refusal — callers branch on it."""
    _inject(monkeypatch, [errno.ENOENT])
    with pytest.raises(FileNotFoundError):
        open_dir_beneath(root_fd, "nope")


def test_enotdir_is_a_traversal_refusal(root_fd, vault):
    (vault / "afile").write_bytes(b"x")
    with pytest.raises(UnsafePath) as caught:
        open_dir_beneath(root_fd, "afile/sub")
    assert "afile/sub" in str(caught.value)


@pytest.mark.parametrize("code", [errno.ENOSYS, errno.EPERM])
def test_an_unavailable_syscall_names_the_kernel_and_the_seccomp_profile(
    monkeypatch, root_fd, code
):
    _inject(monkeypatch, [code])
    with pytest.raises(UnsupportedFilesystem) as caught:
        open_dir_beneath(root_fd, "Attachments")
    message = str(caught.value)
    assert "openat2" in message
    assert "5.6" in message
    assert "seccomp" in message


@pytest.mark.parametrize("code", [errno.EINVAL, errno.E2BIG])
def test_an_abi_disagreement_refuses_rather_than_escaping_as_an_oserror(
    monkeypatch, root_fd, code
):
    """D24. Neither is reachable from a correct binding — `EINVAL` is a `size`
    the kernel does not know or an unrecognised flag, `E2BIG` is nonzero
    extension data past the size it does know — which is exactly why neither
    may escape as a generic `OSError`. They are what a binding bug looks like,
    and a containment lookup that never ran must never be mistaken for one that
    passed."""
    _inject(monkeypatch, [code])
    with pytest.raises(UnsupportedFilesystem) as caught:
        open_dir_beneath(root_fd, "Attachments")
    assert "open_how" in str(caught.value)


def test_an_unmapped_errno_stays_an_oserror(monkeypatch, root_fd):
    """`EACCES` is a real permission problem, not a containment verdict and not
    a missing capability. It must not be dressed up as either."""
    _inject(monkeypatch, [errno.EACCES])
    with pytest.raises(OSError) as caught:
        open_dir_beneath(root_fd, "Attachments")
    assert not isinstance(caught.value, VaultFSError)
    assert caught.value.errno == errno.EACCES


# ── the transient class: bounded retry (1.4, D17) ────────────────────────────


def test_eagain_is_retried_a_bounded_number_of_times_and_then_refused(
    monkeypatch, root_fd
):
    """`EAGAIN` means the kernel could not prove containment because the path
    was being renamed concurrently. Treating it as a refusal would fail a
    legitimate write whenever anything else renamed a directory; retrying
    forever would let an adversary renaming in a loop hold the request open."""
    calls = _inject(monkeypatch, [errno.EAGAIN])
    with pytest.raises(VaultFSError) as caught:
        open_dir_beneath(root_fd, "Attachments")
    assert not isinstance(caught.value, (UnsafePath, UnsupportedFilesystem))
    assert len(calls) == vault_fs._LOOKUP_ATTEMPTS
    assert "Attachments" in str(caught.value)


def test_eagain_that_clears_succeeds(monkeypatch, root_fd, vault):
    calls = _inject(monkeypatch, [errno.EAGAIN, errno.EAGAIN, 0])
    fd = open_dir_beneath(root_fd, "Attachments")
    os.close(fd)
    assert len(calls) == 3


def test_eintr_is_retried_and_then_refused(monkeypatch, root_fd):
    calls = _inject(monkeypatch, [errno.EINTR])
    with pytest.raises(VaultFSError):
        open_dir_beneath(root_fd, "Attachments")
    assert len(calls) == vault_fs._LOOKUP_ATTEMPTS


def test_eintr_then_success_resolves(monkeypatch, root_fd, vault):
    calls = _inject(monkeypatch, [errno.EINTR, 0])
    fd = open_dir_beneath(root_fd, "Attachments")
    try:
        assert os.fstat(fd).st_ino == (vault / "Attachments").stat().st_ino
    finally:
        os.close(fd)
    assert len(calls) == 2


# ── EINTR must not fail a write (D17) ────────────────────────────────────────


@pytest.fixture
def note_vault(monkeypatch, tmp_path):
    async def noop(*args, **kwargs):
        return None

    monkeypatch.setattr(vault_service.settings, "vault_path", str(tmp_path))
    monkeypatch.setattr(tools, "_log_usage", noop)
    vault_fs.reset_filesystem_probe_cache()
    token = current_permission.set("readwrite")
    yield tmp_path
    current_permission.reset(token)
    vault_fs.reset_filesystem_probe_cache()


@pytest.mark.asyncio
async def test_one_eintr_then_success_completes_a_note_write(monkeypatch, note_vault):
    """The reason `EINTR` is in the retry class at all.

    `os.open` retried it transparently under PEP 475, so the walk this replaced
    never saw it. A raw `ctypes` syscall does not, so without an explicit retry
    a signal delivered without `SA_RESTART` — nothing to do with paths, nothing
    to do with containment — turns into a false failure of `create_note`. A
    false failure on the note path is not free: `edit_note(append=True)`
    retried after a write that landed appends the same block twice.
    """
    _inject(monkeypatch, [errno.EINTR, 0])
    result = await tools.create_note_impl("Folder/new.md", "body\n")
    assert "Folder/new.md" in result
    assert (note_vault / "Folder" / "new.md").read_text() == "body\n"


@pytest.mark.asyncio
async def test_an_unavailable_syscall_refuses_a_note_write_and_writes_nothing(
    monkeypatch, note_vault
):
    """No fallback: the tool refuses and the vault is untouched.

    `UnsupportedFilesystem` propagates to become a tool error, which is the
    same shape `create_note` already produces when the filesystem cannot do a
    no-clobber publish. What matters here is that it is a refusal at all —
    there is no per-component walk left to quietly succeed through.
    """
    _inject(monkeypatch, [errno.ENOSYS])
    with pytest.raises(UnsupportedFilesystem) as caught:
        await tools.create_note_impl("Folder/new.md", "body\n")
    assert "openat2" in str(caught.value)
    assert "no fallback" in str(caught.value)
    assert not (note_vault / "Folder").exists()
    assert sorted(os.listdir(note_vault)) == []


# ── the creation descent (1.5) ───────────────────────────────────────────────


def _record_descent(monkeypatch):
    """Record the lookups and the `mkdir`s, in order, with their descriptors."""
    real_lookup = vault_fs._lookup_dir
    real_mkdir = os.mkdir
    events: list[tuple] = []

    def lookup(root_fd, parts, rel_dir):
        fd = real_lookup(root_fd, parts, rel_dir)
        events.append(("lookup", "/".join(parts) if parts else ".", fd))
        return fd

    def mkdir(path, mode=0o777, *, dir_fd=None):
        events.append(("mkdir", path, dir_fd))
        return real_mkdir(path, mode, dir_fd=dir_fd)

    monkeypatch.setattr(vault_fs, "_lookup_dir", lookup)
    monkeypatch.setattr(os, "mkdir", mkdir)
    return events


def test_each_creation_goes_through_a_fresh_lookup_of_the_existing_prefix(
    monkeypatch, root_fd, vault
):
    """D22, made concrete.

    `mkdirat` has no beneath-root form, so creation is still one component at a
    time — but **no directory descriptor is carried across a creation**. Each
    `mkdirat` is issued through a descriptor from a fresh lookup of the prefix
    that already exists, and that descriptor is dropped immediately. The window
    is one syscall per component instead of the whole descent, which is what
    keeps the cost at empty directories.
    """
    events = _record_descent(monkeypatch)
    fd = open_dir_beneath(root_fd, "A/B/C", create=True)
    os.close(fd)

    lookups = [(path, d) for kind, path, d in events if kind == "lookup"]
    mkdirs = [(path, d) for kind, path, d in events if kind == "mkdir"]

    # The failed probe, then one lookup per component, then the final one.
    assert [p for p, _ in lookups] == [".", "A", "A/B", "A/B/C"]
    assert [p for p, _ in mkdirs] == ["A", "B", "C"]

    # Every mkdir went through the descriptor of the lookup immediately before
    # it — never one carried over from the previous creation.
    for (mk_path, mk_fd), (lk_path, lk_fd) in zip(mkdirs, lookups):
        assert mk_fd == lk_fd, f"{mk_path} was created through a stale descriptor"


def test_the_returned_descriptor_comes_from_a_lookup_after_the_creation(
    monkeypatch, root_fd, vault
):
    """The part that keeps the residual at *empty directories*.

    No directory descriptor a creation produced is returned to a caller or used
    as a pathname anchor: the write goes through the post-creation lookup,
    which either resolves beneath the root or refuses. So a prefix renamed out
    mid-descent costs an empty directory and never a file.
    """
    events = _record_descent(monkeypatch)
    fd = open_dir_beneath(root_fd, "A/B", create=True)
    try:
        kinds = [kind for kind, _, _ in events]
        # The last thing that happened was a lookup, not a mkdir.
        assert kinds[-1] == "lookup"
        last_kind, last_path, last_fd = events[-1]
        assert last_path == "A/B"
        assert fd == last_fd
        # And it happened after every creation.
        assert kinds.index("mkdir") < len(kinds) - 1
    finally:
        os.close(fd)


def test_a_fully_existing_path_performs_no_creation_descent(monkeypatch, root_fd, vault):
    """`create=True` on a path that is already there must not `mkdir` anything —
    the descent is only reached when the first lookup says `ENOENT`."""
    (vault / "A" / "B").mkdir(parents=True)
    events = _record_descent(monkeypatch)
    fd = open_dir_beneath(root_fd, "A/B", create=True)
    os.close(fd)
    assert [kind for kind, _, _ in events] == ["lookup"]


def test_a_symlinked_ancestor_is_refused_before_anything_is_created(
    root_fd, vault, tmp_path_factory
):
    outside = tmp_path_factory.mktemp("outside_create")
    os.symlink(outside, vault / "link")
    with pytest.raises(UnsafePath):
        open_dir_beneath(root_fd, "link/made", create=True)
    assert not (outside / "made").exists()


def test_the_deferred_parent_creation_re_looks_up_before_the_write(
    monkeypatch, note_vault
):
    """`MutableTarget.ensure_parent` is the one deferred-creation site, and it
    inherits the descent whole: the descriptor the write anchors to comes from
    a fresh lookup of the whole parent, performed after the directories exist."""
    events = _record_descent(monkeypatch)
    with vault_service.open_mutable("New/Deep/x.md") as target:
        assert target.parent_fd is None, "validation must create nothing"
        dir_fd = target.dir_fd
        kinds = [kind for kind, _, _ in events]
        assert "mkdir" in kinds
        last_kind, last_path, last_fd = events[-1]
        assert last_kind == "lookup"
        assert last_path == "New/Deep"
        assert dir_fd == last_fd


# ── a mount point beneath the root still resolves (D16) ──────────────────────


_MOUNT_CASE = textwrap.dedent(
    """
    import os, sys
    sys.path.insert(0, {repo!r})
    from src.services import vault_fs

    root = {root!r}
    os.makedirs(root + "/M", exist_ok=True)
    os.makedirs(root + "/src", exist_ok=True)
    open(root + "/src/marker", "w").close()
    import subprocess
    subprocess.run(["mount", "--bind", root + "/src", root + "/M"], check=True)
    assert os.path.exists(root + "/M/marker")
    # Prove the mount is really there, so the test cannot pass vacuously on a
    # plain directory.
    with open("/proc/self/mountinfo") as fh:
        mounted = [ln for ln in fh if " " + root + "/M " in ln]
    assert mounted, "the bind mount is not in mountinfo"
    os.makedirs(root + "/M/inner", exist_ok=True)

    rfd = vault_fs.open_root(root)
    fd = vault_fs.open_dir_beneath(rfd, "M")
    assert sorted(os.listdir(fd)) == ["inner", "marker"]
    os.close(fd)
    fd = vault_fs.open_dir_beneath(rfd, "M/inner")
    os.close(fd)
    os.close(rfd)
    print("MOUNT_LOOKUP_OK")
    """
)


def _can_unshare() -> bool:
    probe = subprocess.run(
        ["unshare", "-Ur", "-m", "--propagation", "private", "true"],
        capture_output=True,
    )
    return probe.returncode == 0


@pytest.mark.skipif(not _can_unshare(), reason="needs an unprivileged mount namespace")
def test_a_mount_point_beneath_the_root_still_resolves(tmp_path):
    """`RESOLVE_NO_XDEV` is not set, so a nested mount is still beneath the root.

    Setting it would refuse *lookups* through a mount point — and lookups are
    what every read, `delete_file`, the note tools and the transfer path share.
    A vault with a bind mount inside it is a supported deployment.
    """
    repo = str(Path(__file__).resolve().parent.parent)
    script = _MOUNT_CASE.format(repo=repo, root=str(tmp_path))
    result = subprocess.run(
        [
            "unshare",
            "-Ur",
            "-m",
            "--propagation",
            "private",
            sys.executable,
            "-c",
            script,
        ],
        capture_output=True,
        text=True,
    )
    assert "MOUNT_LOOKUP_OK" in result.stdout, result.stderr


# ── the startup probe (1.7) ──────────────────────────────────────────────────


def test_the_startup_probe_passes_on_this_kernel():
    vault_fs.probe_beneath_root_lookup()


def test_the_startup_probe_creates_nothing(tmp_path, monkeypatch):
    """It is a read-only probe, which is the whole reason it can run at startup
    rather than on the first write (D21). `probe_publication` and `probe_trash`
    write, which is why no read path may call them."""
    monkeypatch.chdir(tmp_path)
    before = sorted(os.listdir(tmp_path))
    vault_fs.probe_beneath_root_lookup()
    assert sorted(os.listdir(tmp_path)) == before == []


@pytest.mark.parametrize(
    "code, phrase",
    [
        (errno.ENOSYS, "seccomp"),
        (errno.EPERM, "seccomp"),
        (errno.EINVAL, "open_how"),
        (errno.E2BIG, "open_how"),
    ],
)
def test_the_startup_probe_refuses_when_the_syscall_is_unavailable(
    monkeypatch, code, phrase
):
    monkeypatch.setattr(
        vault_fs, "_openat2_raw", lambda *a, **k: (-1, code)
    )
    with pytest.raises(UnsupportedFilesystem) as caught:
        vault_fs.probe_beneath_root_lookup()
    assert "openat2" in str(caught.value)
    assert phrase in str(caught.value)


def test_the_startup_probe_does_not_invent_a_verdict_from_an_unrelated_errno(
    monkeypatch,
):
    """It probes syscall availability, not the working directory. An errno that
    says nothing about availability is not turned into a refusal to start."""
    monkeypatch.setattr(
        vault_fs, "_openat2_raw", lambda *a, **k: (-1, errno.EACCES)
    )
    vault_fs.probe_beneath_root_lookup()


def test_lifespan_guard_exits_with_the_named_message(monkeypatch, caplog):
    from src import main as main_module

    monkeypatch.setattr(
        vault_fs,
        "probe_beneath_root_lookup",
        lambda: (_ for _ in ()).throw(
            UnsupportedFilesystem("openat2(2) is unavailable (EPERM)")
        ),
    )
    exits: list[int] = []

    def fake_exit(code=0):
        exits.append(code)
        raise SystemExit(code)

    monkeypatch.setattr(main_module.sys, "exit", fake_exit)
    with caplog.at_level("CRITICAL"):
        with pytest.raises(SystemExit):
            main_module._check_openat2_support()
    assert exits == [1]
    logged = caplog.text
    assert "openat2" in logged
    assert "5.6" in logged
    assert "seccomp" in logged


def test_lifespan_guard_is_quiet_on_the_happy_path(caplog):
    from src import main as main_module

    with caplog.at_level("WARNING"):
        main_module._check_openat2_support()
    assert caplog.text == ""


def test_the_sandbox_skip_is_the_only_way_past_the_startup_guard():
    """`MCP_SANDBOX_MODE` returns from `lifespan` before any guard runs, which
    is what makes it the one configuration in which a call site can be reached
    with the syscall unavailable — and why the call site refuses too."""
    import inspect

    from src import main as main_module

    source = inspect.getsource(main_module.lifespan)
    sandbox_return = source.index("        return\n")
    guard = source.index("_check_openat2_support()")
    assert sandbox_return < guard
