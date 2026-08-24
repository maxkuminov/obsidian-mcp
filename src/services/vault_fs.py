"""Descriptor-anchored filesystem primitives (design D6, extended by #59).

A *path-based* helper resolves a pathname and then operates by name. Between
the resolve and the operation, anyone who can create a symlink — or rename a
directory and leave a link at its name — gets a TOCTOU window. That was never
acceptable for `/transfer/*`, a public route family redeemed with a bearer
capability, and #59 established it is not acceptable for the note write tools
either: `src/services/vault.py` now anchors every mutation here too, through
`MutableTarget` / `open_mutable`.

So every operation here is anchored to an open directory descriptor, and that
descriptor comes from **one** kernel-enforced beneath-root lookup:
``openat2(2)`` carrying ``RESOLVE_BENEATH | RESOLVE_NO_SYMLINKS |
RESOLVE_NO_MAGICLINKS`` (see ``open_dir_beneath``). A symlink anywhere in the
chain — ancestor or final component — raises instead of being followed, and
the kernel proves the whole path stayed beneath the root inside the single
call.

This replaces a per-component ``O_NOFOLLOW`` walk (#87). Each open in that walk
was individually safe; the *sequence* was not. Between opening ancestor ``A``
and opening its child ``B``, another process could rename ``<vault>/A`` out of
the vault, and the descriptor the walk went on to return — with every mutation
anchored to it — was then outside the root, with nothing later in the call able
to notice. There is no interval between components to race any more, and there
is deliberately **no fallback** to the old walk: a containment guard that
degrades quietly is the failure mode being removed.

What that entitles this module to claim, in the words every artifact of #87
uses: *every below-root directory descriptor a call uses as a pathname anchor
comes from a lookup the kernel proved beneath the vault root at the moment it
resolved, and no directory descriptor retained from a creation descent is ever
returned to a caller or used as a pathname anchor — so no operation is ever
redirected into a directory that was never beneath the root.* This is a claim
about **directory** descriptors used as pathname anchors: a call's own staged
payload descriptor is created by that call and published through by descriptor,
and never anchors a pathname lookup. Two residuals sit beside it and are stated
rather than implied — the bounded empty-directory cost of a creation descent
(``open_dir_beneath``, D22) and the fact that a lookup proves containment when
it *resolves* and not afterwards (D26): a descriptor keeps naming its directory
however that directory's pathname is later renamed, which is the property #59
relies on, so a rename landing after the lookup and before the publish carries
the call with it.

One syscall the stdlib does not expose *is* reached for directly, through
``ctypes``: ``renameat2(RENAME_NOREPLACE)`` (see ``rename_noreplace``). It is
the only way to move a file to a name we do not already own without a
check-then-act window, and the soft delete is built on it — ``os.rename``
replaces, and every scheme for reserving the destination first leaves a window
in which somebody else's file can be sitting at that pathname when the rename
lands on it.

Publication is deliberately split in two:

* **no-clobber** (`overwrite=False`) is ``link()``, which the kernel makes
  atomic: it either creates the name or fails ``EEXIST``. Nothing can be
  destroyed by a no-clobber publish, ever.
* **overwrite** is ``stat`` + optional re-hash + ``replace()``. That is
  check-then-act, i.e. **optimistic** conflict detection — a writer that lands
  between the check and the rename is still overwritten. This is the same
  guarantee `edit_note(expected=…)` gives today, it is declared rather than
  implied, and `tests/test_vault_fs.py` has a barrier test that demonstrates
  the window rather than pretending it is closed.

`vault._atomic_write_at` has adopted these primitives (#59): it stages and
publishes against the parent descriptor `open_mutable` opened, `move_note`
publishes with `rename_noreplace`, and `delete_note` soft-deletes through
`soft_delete_at`. `vault` keeps its own staging (a `.tmp-<name>-…` file in the
destination directory rather than `.transfer-tmp/`) because a note write
completes in one call — there is no minutes-long stream to survive, and staging
beside the destination keeps the publish a same-directory rename.
"""
from __future__ import annotations

import ctypes
import errno
import hashlib
import logging
import os
import platform
import secrets
import stat
import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath

logger = logging.getLogger(__name__)

# Bytes per read when hashing a file through a descriptor.
_HASH_CHUNK = 1024 * 1024

# How many `.tmp-<hex>` names to try before giving up (collision is
# astronomically unlikely; the retry exists so a hostile pre-creation loop
# cannot wedge an upload silently).
_TEMP_ATTEMPTS = 8

# How many trash names to try before giving up. Each candidate carries 32 bits
# of randomness and is claimed by the `RENAME_NOREPLACE` rename itself, so one
# attempt effectively always wins; the retry exists so a hostile pre-creation
# loop cannot wedge a delete.
_TRASH_ATTEMPTS = 8

# How stale an abandoned `.transfer-tmp/.tmp-*` file must be before the
# first-use sweep removes it. A day is far longer than any upload may run
# (`TRANSFER_MAX_UPLOAD_SECONDS` is capped at minutes), so nothing in flight can
# be inside the window.
STALE_STAGING_SECONDS = 24 * 60 * 60

# Leaf opens through an already-anchored parent descriptor. Directory
# *descents* do not use this — they go through `open_dir_beneath`, whose
# `RESOLVE_NO_SYMLINKS` is the stronger form of the same intent.
_O_COMMON = os.O_CLOEXEC | os.O_NOFOLLOW

# Where in-flight uploads are staged. A dot-directory directly under the vault
# root, so it is invisible to the indexer and to every dot-dir-guarded tool,
# and — being inside the root — always on the same device as any destination,
# which is what makes the final `link`/`replace` possible.
#
# Staging here rather than in the destination folder is deliberate: an upload
# may stream for minutes, and a directory descriptor opened before the stream
# keeps pointing at the same directory even if that directory is renamed or
# moved. Publishing through it would follow the move. The destination parent is
# therefore resolved fresh, under the caller's lock, at publish time.
STAGING_DIR = ".transfer-tmp"
TRASH_DIR = ".trash"


class VaultFSError(Exception):
    """Base class for anchored-filesystem failures."""


class UnsafePath(VaultFSError):
    """A component was a symlink, an escape, or not the kind of file expected.

    Never a "not found": absence is reported as `None` / `FileNotFoundError` so
    callers can tell "you may not touch this" from "there is nothing there".
    """


class Conflict(VaultFSError):
    """The target is not in the state the caller committed to at mint time.

    Raised for a no-clobber publish whose target now exists, an overwrite
    publish whose target's fingerprint changed, and an expected-absence
    overwrite token whose target appeared.
    """


class UnsupportedFilesystem(VaultFSError):
    """The vault filesystem cannot support an operation this module needs.

    Two independent capabilities, probed separately because different callers
    need different ones: hard links within the root (`EPERM`/`EOPNOTSUPP`/
    `EXDEV`) for the no-clobber publish, and a same-device `rename` into
    `.trash` (`EXDEV`) for the soft delete. The transfer tools refuse rather
    than silently degrading to an overwriting move.
    """


Fingerprint = dict


# ── directory anchoring ─────────────────────────────────────────────────────


def open_root(root: Path | str) -> int:
    """Open the vault root as an anchor descriptor.

    The root itself is the one path resolved by name — it is operator
    configuration (a bind mount), not attacker input. Every component *below*
    it is opened one at a time with `O_NOFOLLOW`.
    """
    try:
        return os.open(str(root), os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    except FileNotFoundError:
        raise FileNotFoundError(f"Vault root does not exist: {root}") from None
    except NotADirectoryError:
        raise UnsafePath(f"Vault root is not a directory: {root}") from None


def _split(rel: str | Path) -> list[str]:
    """Vault-relative path → components, rejecting anything that could escape.

    `..` is rejected outright rather than normalised: normalising `a/../../b`
    to `b` would silently accept a path the caller did not mean, and the whole
    point of this module is that nothing is resolved on our behalf.
    """
    p = PurePosixPath(str(rel).replace(os.sep, "/"))
    if p.is_absolute():
        raise UnsafePath(f"Absolute path not allowed: {rel}")
    parts: list[str] = []
    for part in p.parts:
        if part in ("", "."):
            continue
        if part == "..":
            raise UnsafePath(f"Parent traversal not allowed: {rel}")
        if "/" in part or "\0" in part:
            raise UnsafePath(f"Illegal path component: {part!r}")
        parts.append(part)
    return parts


# ── the beneath-root lookup ─────────────────────────────────────────────────

# `openat2(2)` resolve flags (`<linux/openat2.h>`).
#
# `RESOLVE_NO_XDEV` is deliberately **not** among them (D16). It buys nothing
# for containment — that is `RESOLVE_BENEATH`'s job, and a mount point beneath
# the root is still beneath the root — while setting it would refuse *lookups*
# through a mount point, which is what every read, `delete_file`, the note
# tools and the transfer path share. It would break every path that works
# across a nested mount and fix none of the three that do not.
#
# `RESOLVE_IN_ROOT` was rejected for the same reason `_split` exists: it scopes
# `..` and absolute paths chroot-style rather than refusing them, so `a/../../b`
# would be silently accepted as something the caller did not write.
RESOLVE_NO_MAGICLINKS = 0x02
RESOLVE_NO_SYMLINKS = 0x04
RESOLVE_BENEATH = 0x08

_RESOLVE_STRICT = RESOLVE_BENEATH | RESOLVE_NO_SYMLINKS | RESOLVE_NO_MAGICLINKS

# `O_NOFOLLOW` is not set: `RESOLVE_NO_SYMLINKS` already refuses a symlink at
# *every* component including the trailing one (measured), and saying it once,
# in the kernel's own vocabulary, is the point of this change.
_O_LOOKUP = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC

# `syscall(2)` numbers for `openat2`. Unlike `_SYS_RENAMEAT2` this table is not
# a fallback for an old glibc — it is the implementation. **glibc exports no
# `openat2` wrapper at any version** (D24): measured on glibc 2.36 and 2.39,
# where `renameat2`, `statx`, `close_range` and `getrandom` all resolve through
# `ctypes.CDLL(None)` and `openat2` raises `AttributeError`.
#
# The number is 437 everywhere, because the syscall postdates the unified
# numbering convention. That uniformity is a convenience, not a licence to
# guess: an architecture absent from this table is treated as "no `openat2`",
# which now means the server refuses to start rather than that a rarely-taken
# branch is skipped. A wrong number would call a *different* syscall.
_SYS_OPENAT2 = {
    "x86_64": 437,
    "aarch64": 437,
    "armv7l": 437,
    "armv8l": 437,
    "i686": 437,
    "i386": 437,
    "ppc64le": 437,
    "s390x": 437,
}

# How many times a lookup is re-issued before it is refused. `EAGAIN` (the
# kernel could not decide containment because the path was being renamed
# underneath it) and `EINTR` (a signal, and nothing else) are the only two
# retried; see `_lookup_dir` for why both must be.
_LOOKUP_ATTEMPTS = 8


class _OpenHow(ctypes.Structure):
    """`struct open_how` (`<linux/openat2.h>`): three `__u64`s, in this order.

    `sizeof` is passed as the syscall's `size` argument, which is how the
    kernel versions the structure. Getting that wrong is not a soft failure —
    see `_lookup_dir`'s `EINVAL`/`E2BIG` branch.
    """

    _fields_ = [
        ("flags", ctypes.c_uint64),
        ("mode", ctypes.c_uint64),
        ("resolve", ctypes.c_uint64),
    ]


_openat2_cache: tuple | None = None


def _resolve_openat2():
    """Find a callable raw `openat2`, or `None` if this platform has none.

    There is no wrapper branch to prefer here (D24), so every part of this is
    load-bearing and none of it is `pragma: no cover`: the per-architecture
    number must be right, and an architecture missing from the table means the
    server will not start.
    """
    try:
        libc = ctypes.CDLL(None, use_errno=True)
    except OSError:  # pragma: no cover - no libc to bind against
        return None
    number = _SYS_OPENAT2.get(platform.machine())
    if number is None:
        return None
    raw = libc.syscall
    raw.restype = ctypes.c_long

    def _via_syscall(dir_fd, path, how, size):
        return raw(
            ctypes.c_long(number),
            ctypes.c_int(dir_fd),
            ctypes.c_char_p(path),
            ctypes.byref(how),
            ctypes.c_size_t(size),
        )

    return _via_syscall


def _openat2_fn():
    """Cached `_resolve_openat2`; the lookup is per-process, not per-call."""
    global _openat2_cache
    if _openat2_cache is None:
        _openat2_cache = (_resolve_openat2(),)
    return _openat2_cache[0]


def _openat2_raw(dir_fd: int, path: str, flags: int, resolve: int) -> tuple[int, int]:
    """Call `openat2`; return `(fd, 0)` on success or `(-1, errno)` on failure.

    Deliberately the *only* place the syscall is touched, and deliberately
    returns an errno rather than raising: everything above it is errno mapping,
    which is the part worth testing, and a test can drive every branch by
    monkeypatching this one function instead of arranging an exotic kernel.
    """
    fn = _openat2_fn()
    if fn is None:
        return -1, errno.ENOSYS
    how = _OpenHow(flags=flags, mode=0, resolve=resolve)
    ctypes.set_errno(0)
    rc = fn(dir_fd, os.fsencode(path), how, ctypes.sizeof(how))
    if rc >= 0:
        return int(rc), 0
    return -1, ctypes.get_errno() or errno.EIO


def _lookup_dir(root_fd: int, parts: list[str], rel_dir) -> int:
    """One beneath-root lookup of `parts` under `root_fd`. Never creates.

    The errno contract distinguishes four kinds of failure, because they tell
    an operator to do four different things:

    * **A refused path.** `ELOOP` (a symlink at some component) and `ENOTDIR`
      raise `UnsafePath`, and so does `EXDEV` — which here means *containment
      was violated*, not "cross-device" as it does in `rename_noreplace`
      (D17). Mapping it to `UnsupportedFilesystem` would tell an operator to
      change filesystems in response to a blocked escape. Measured: both `..`
      and an absolute path give `EXDEV`, so it is the ordinary answer for
      "this path leaves the root" rather than an exotic one. `ENOENT` stays a
      `FileNotFoundError` so callers keep telling absence from refusal.
    * **A transient condition**, retried a bounded number of times and then
      refused. `EAGAIN` means the kernel could not prove containment because
      the path was being renamed concurrently; treating it as a refusal would
      fail a legitimate write whenever anything else renamed a directory, and
      retrying forever would let an adversary renaming in a loop hold the
      request open. **`EINTR` joins it for a different reason**: the walk this
      replaced went through `os.open`, which retries `EINTR` transparently
      under PEP 475, and a raw `ctypes` syscall does not — so without this a
      signal delivered without `SA_RESTART` would become a false failure of
      `create_note`, `delete_file`, a transfer or a download.
    * **An unavailable syscall** (`ENOSYS`, `EPERM` — an older Docker seccomp
      profile blocks it either way round). `UnsupportedFilesystem`, naming the
      syscall, the kernel version and the profile. Never a per-component walk.
    * **An ABI disagreement** (`EINVAL` — a `size` smaller than any version
      the kernel knows, or a flag or `resolve` bit it does not recognise;
      `E2BIG` — nonzero extension data past the size this kernel knows).
      Neither is reachable from a correct binding, which is exactly why
      neither may escape as a generic `OSError`: they are what a binding bug
      or a future ABI revision looks like, and a containment lookup that never
      ran must never be mistaken for one that passed. Measured, and the first
      draft had these two the wrong way round.

    The traversal error names the **requested vault-relative path** and not the
    offending component (D25). One `openat2` reports `ELOOP` for the resolution
    as a whole and says nothing about which component caused it, and a
    diagnostic walk issued afterwards is not a substitute: by then the link may
    be gone, or a different component may have become one, so it would report
    no link at all or the wrong one — authoritatively, about a state the kernel
    never saw. Nothing load-bearing is lost: naming a symlinked **leaf** with
    its canonical target is `vault.open_mutable`'s `lstat` through the parent
    descriptor, which is a different check and is untouched.
    """
    path = "/".join(parts) if parts else "."
    attempts = 0
    while True:
        fd, code = _openat2_raw(root_fd, path, _O_LOOKUP, _RESOLVE_STRICT)
        if code == 0:
            return fd
        if code in (errno.EAGAIN, errno.EINTR):
            attempts += 1
            if attempts < _LOOKUP_ATTEMPTS:
                continue
            raise VaultFSError(
                f"Could not resolve {str(rel_dir)!r} beneath the vault root "
                f"after {_LOOKUP_ATTEMPTS} attempts "
                f"({errno.errorcode.get(code, code)}); the path is being "
                "renamed concurrently or the process is being signalled"
            )
        break
    if code == errno.ENOENT:
        raise FileNotFoundError(code, os.strerror(code), str(rel_dir))
    if code == errno.EXDEV:
        raise UnsafePath(
            f"Refusing a path that resolves outside the vault root: "
            f"{str(rel_dir)!r}"
        )
    if code in (errno.ELOOP, errno.ENOTDIR, errno.EMLINK):
        raise UnsafePath(
            f"Refusing to traverse a symlink or non-directory in "
            f"{str(rel_dir)!r} ({errno.errorcode.get(code, code)})"
        )
    if code in (errno.ENOSYS, errno.EPERM):
        raise UnsupportedFilesystem(
            f"openat2(2) is unavailable ({errno.errorcode.get(code, code)}). "
            "A beneath-root lookup needs it: the kernel must be 5.6 or newer "
            "and the container seccomp profile must permit the syscall. There "
            "is no fallback to a per-component walk."
        )
    if code in (errno.EINVAL, errno.E2BIG):
        raise UnsupportedFilesystem(
            f"openat2(2) rejected this struct open_how "
            f"({errno.errorcode.get(code, code)}); the binding and the "
            "kernel's ABI disagree, so no containment check was performed. "
            "There is no fallback to a per-component walk."
        )
    raise OSError(code, os.strerror(code), str(rel_dir))


def open_dir_beneath(
    root_fd: int,
    rel_dir: str | Path,
    *,
    create: bool = False,
    created: list[str] | None = None,
) -> int:
    """Open a directory below `root_fd` with one beneath-root lookup.

    Always returns a **new** descriptor (even for the root itself), so callers
    can close the result unconditionally without risking the anchor.

    Raises `UnsafePath` when a component is a symlink or not a directory or the
    path would escape the root, `FileNotFoundError` when a component is missing
    and `create` is false, and `UnsupportedFilesystem` when the syscall is
    unavailable — never a fallback to opening one component at a time.

    `_split` stays in front of the syscall and keeps refusing `..`, absolute
    paths and NUL bytes with a message naming the offending path. It is not
    redundant: `RESOLVE_BENEATH` *scopes* `..` rather than forbidding it, so
    `A/../A` succeeds at the kernel (measured), and this module's posture is
    that nothing is normalised on our behalf.

    **The creation side keeps a bounded residual, and it is stated rather than
    claimed closed (D22).** `openat2` resolves; it does not create, `mkdirat`
    has no beneath-root form, and no syscall creates a directory *and* proves
    the path it created it under stayed beneath a root. So creation is still
    one component at a time — but **no directory descriptor is carried across
    a creation**: each `mkdirat` is issued through a descriptor from a fresh
    beneath-root lookup of the prefix that already exists, that descriptor is
    dropped immediately, and the descriptor the caller finally receives comes
    from a fresh single lookup of the whole path performed *after* the creation
    finishes. The window is therefore one syscall per component instead of the
    whole descent, and what a race can cost is at most one **empty** directory
    per component **per creation descent** — never a file, never file content,
    and never something a tool then reports success about, because the write
    goes through the post-creation lookup, which either resolves beneath the
    root or refuses. The bound is per descent and one call can have more than
    one: an upload walks its destination twice with creation enabled (a cheap
    up-front walk so a bad path costs one syscall rather than a whole body, and
    the authoritative walk inside the publish gate), a note write once.

    **Do not try to clean that up.** An `rmdir` by a name the caller chose is
    the same delete-the-substitute hazard `_discard_temp` and `soft_delete`
    already refuse: the thing at that name may no longer be the thing we made.
    The process that wins the race already holds rename rights on the prefix's
    parent and write access wherever it moved it, so the empty directory lands
    somewhere it already controls.

    **`created`**, when given, is appended with the vault-relative path of every
    directory this call actually made, outermost first. It is how a caller
    learns which directory *entries* its own write brought into existence and
    therefore has to flush (#97): flushing the destination's parent alone
    leaves the entry that names *it* unflushed, so a crash can lose the whole
    new folder and with it a file the caller was told about. A component that
    was already there — or that another creator won the race to — is not
    listed: it is not this call's entry to make durable.
    """
    parts = _split(rel_dir)
    try:
        return _lookup_dir(root_fd, parts, rel_dir)
    except FileNotFoundError:
        if not create:
            raise
    _create_descent(root_fd, parts, rel_dir, created)
    return _lookup_dir(root_fd, parts, rel_dir)


def _create_descent(
    root_fd: int, parts: list[str], rel_dir, created: list[str] | None = None
) -> None:
    """Create the missing components of `parts`, carrying no descriptor across.

    One `mkdirat` per component, each through a descriptor obtained by a fresh
    beneath-root lookup of the prefix that already exists and dropped as soon
    as the `mkdirat` returns. Nothing this function opens is returned to a
    caller or used as a pathname anchor — `open_dir_beneath` re-looks-up the
    whole path afterwards, which is what keeps the residual at empty
    directories (see its docstring).

    `EEXIST` is a benign race with another creator, or an already-present
    component: either way the authoritative answer is the post-creation lookup,
    not the `mkdir`, so it is passed over here and decided there. A symlink
    sitting at the name likewise gives `EEXIST` and is refused by that lookup.
    It is also why `EEXIST` does **not** record the component in `created`: the
    entry this call has to make durable is one this call wrote, and a directory
    somebody else created is theirs to flush.
    """
    for depth in range(len(parts)):
        prefix_fd = _lookup_dir(root_fd, parts[:depth], rel_dir)
        try:
            os.mkdir(parts[depth], 0o755, dir_fd=prefix_fd)
        except FileExistsError:
            pass
        else:
            if created is not None:
                created.append("/".join(parts[: depth + 1]))
        finally:
            close_quietly(prefix_fd, f"prefix of {str(rel_dir)!r}")


def probe_beneath_root_lookup() -> None:
    """Refuse if this kernel and container cannot do a beneath-root lookup.

    **Read-only: it creates nothing**, which is what lets it run at startup
    rather than on the first write. One `openat2` of `"."` relative to a
    directory descriptor of the process's own working directory, with the same
    resolve flags every real lookup uses.

    Unlike `probe_publication` and `probe_trash` this is not a per-root probe
    and is not cached per root (D21). Those test *filesystem and mount*
    properties, which genuinely differ per vault root in multi-user mode, and
    they **write**. `openat2` availability is a property of the kernel and of
    this container's seccomp profile: one answer for the whole process,
    identical for every root, knowable before a single request arrives. So it
    belongs in `lifespan` beside `_check_pgvector_version`, and
    `src/main.py::_check_openat2_support` is what calls it.

    Only the availability errnos are a verdict here. Anything else the cwd
    happens to answer is not an answer about the syscall, and this probe does
    not invent one: the call sites raise on their own paths.
    """
    fd = os.open(".", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        probe_fd, code = _openat2_raw(fd, ".", _O_LOOKUP, _RESOLVE_STRICT)
    finally:
        close_quietly(fd, "probe working directory")
    if code == 0:
        close_quietly(probe_fd, "beneath-root lookup probe")
        return
    if code in (errno.ENOSYS, errno.EPERM):
        raise UnsupportedFilesystem(
            f"openat2(2) is unavailable ({errno.errorcode.get(code, code)}): "
            "the kernel must be 5.6 or newer and the container seccomp "
            "profile must permit the syscall."
        )
    if code in (errno.EINVAL, errno.E2BIG):
        raise UnsupportedFilesystem(
            f"openat2(2) rejected this struct open_how "
            f"({errno.errorcode.get(code, code)}): the binding and the "
            "kernel's ABI disagree, so no containment check can be performed."
        )


# Owner-only: nothing but this process has any business in the staging
# directory. See `open_staging_dir`.
STAGING_DIR_MODE = 0o700


def open_staging_dir(root_fd: int, *, create: bool = True) -> int:
    """Open `.transfer-tmp` beneath `root_fd`, enforcing owner-only access.

    Staged bytes are relaxed to `default_file_mode()` before publication, so
    the directory — not the file — is what keeps an in-flight upload private.
    That matters because staging is the window in which the bytes exist but
    the publish gate has not yet revalidated the credential: a body that is
    about to be *refused* is on disk for the duration, and under a
    group-writable umask a peer could also alter it after `_drain` computed
    its digest, so the server would publish bytes that do not match the
    sha256 it reports. 0700 removes both, whatever the staged file's own mode
    and whatever the operator's umask.

    The mode is enforced on every open, not just at creation: `mkdir` is
    masked by the umask, and a `.transfer-tmp` left at 0755 by an older
    release (or by a `mkdir -p` from anywhere else) must be corrected rather
    than trusted. `fchmod` goes through the descriptor we just opened
    `O_NOFOLLOW`, so it cannot be redirected to another directory by a
    rename in between.

    Destination directories are deliberately *not* treated this way — they
    hold published vault content and get the ordinary 0755 from `_open_child`.
    """
    fd = open_dir_beneath(root_fd, STAGING_DIR, create=create)
    try:
        st = os.fstat(fd)
        if st.st_uid != os.geteuid():
            raise UnsafePath(
                f"{STAGING_DIR} is owned by uid {st.st_uid}, not by this "
                f"process (uid {os.geteuid()}); refusing to stage uploads in it"
            )
        if stat.S_IMODE(st.st_mode) != STAGING_DIR_MODE:
            os.fchmod(fd, STAGING_DIR_MODE)
            logger.info(
                "Tightened %s from %o to %o",
                STAGING_DIR,
                stat.S_IMODE(st.st_mode),
                STAGING_DIR_MODE,
            )
            # Verify rather than trust. A filesystem that accepts `fchmod` and
            # does not apply it (or applies it partially) would otherwise leave
            # the isolation guarantee false while every call reported success —
            # and that guarantee is the only thing protecting a staged upload,
            # because the staged file itself is relaxed to the umask default
            # before the publish gate. Same posture as the publication and
            # trash probes: refuse the operation rather than degrade silently.
            effective = stat.S_IMODE(os.fstat(fd).st_mode)
            if effective != STAGING_DIR_MODE:
                raise UnsupportedFilesystem(
                    f"Could not restrict {STAGING_DIR} to {STAGING_DIR_MODE:o}; "
                    f"it is {effective:o} after chmod. Uploads need a staging "
                    f"directory no other user can read."
                )
    except BaseException:
        close_quietly(fd, STAGING_DIR)
        raise
    return fd


# ── temp files and fingerprints ─────────────────────────────────────────────


# Mode a plain `open(..., "w")` produces, cached for the life of the process.
_default_file_mode_cache: int | None = None


def default_file_mode() -> int:
    """The mode a plain `open(..., "w")` would give a new file: 0o666 & ~umask.

    Every staging descriptor in this package is created at 0o600 so the
    content is never readable by anyone else while it is being written, and
    publication is a `linkat`/`rename` of that same inode — so the staging
    mode *is* the published mode unless a writer relaxes it first. Without
    that step a file the server wrote silently drops from the umask default
    (0o644 on the container) to 0o600 and becomes unreadable to anything else
    sharing the vault: another container, a backup agent, a sync client (#95).

    Both writers call this immediately before publishing — `vault`'s
    `_atomic_write_at` for notes and raw file writes, `transfer`'s
    `stream_to_vault` for uploads and imports. It lives here because `vault`
    and `transfer` both depend on this module and neither depends on the
    other; a second copy is how the two paths drifted apart in the first place.

    Read from `/proc/self/status` where available; the `os.umask` read-restore
    dance is the portable fallback and is only reached once.
    """
    global _default_file_mode_cache
    if _default_file_mode_cache is None:
        mask: int | None = None
        try:
            with open("/proc/self/status", encoding="ascii") as status:
                for line in status:
                    if line.startswith("Umask:"):
                        mask = int(line.split()[1], 8)
                        break
        except OSError:
            mask = None
        if mask is None:
            mask = os.umask(0o022)
            os.umask(mask)
        _default_file_mode_cache = 0o666 & ~mask
    return _default_file_mode_cache


def create_temp(dir_fd: int) -> tuple[int, str]:
    """Create an exclusive `.tmp-<32 hex>` file in `dir_fd`; return (fd, name).

    Mode 0600 and `O_EXCL|O_NOFOLLOW`: the name cannot pre-exist, cannot be a
    symlink, and the content is never world-readable while it is being written.
    That 0600 is for the *staging* window only — publication links this inode
    into place, so the caller must relax the mode with `default_file_mode()`
    before publishing or the file lands unreadable to everything else (#95).
    The `.tmp-` prefix keeps it invisible to the indexer and to every dot-dir
    guarded tool, so a crashed upload leaves nothing an agent can see.
    """
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_COMMON
    last: OSError | None = None
    for _ in range(_TEMP_ATTEMPTS):
        name = f".tmp-{secrets.token_hex(16)}"
        try:
            return os.open(name, flags, 0o600, dir_fd=dir_fd), name
        except FileExistsError as exc:  # pragma: no cover - 128-bit collision
            last = exc
    raise VaultFSError("Could not create a temporary file in the target directory") from last


def _lstat(dir_fd: int, name: str) -> os.stat_result | None:
    try:
        return os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None


def _require_regular(st: os.stat_result, name: str) -> None:
    if stat.S_ISLNK(st.st_mode):
        raise UnsafePath(f"Refusing to operate on a symlink: {name}")
    if not stat.S_ISREG(st.st_mode):
        raise UnsafePath(f"Not a regular file: {name}")


def _hash_regular(dir_fd: int, name: str, *, expect_ino: int, expect_dev: int) -> str:
    """SHA-256 of `name` read through one `O_NOFOLLOW` descriptor.

    The descriptor is `fstat`ed and matched against the inode the caller
    already saw, so the bytes hashed provably belong to the file that was
    compared — not to something swapped in between the `stat` and the open.
    """
    fd = os.open(name, os.O_RDONLY | _O_COMMON, dir_fd=dir_fd)
    try:
        st = os.fstat(fd)
        if st.st_ino != expect_ino or st.st_dev != expect_dev:
            raise Conflict(f"File was replaced while it was being verified: {name}")
        digest = hashlib.sha256()
        while True:
            chunk = os.read(fd, _HASH_CHUNK)
            if not chunk:
                break
            digest.update(chunk)
        return digest.hexdigest()
    finally:
        os.close(fd)


def fingerprint(dir_fd: int, name: str, *, hash_up_to: int | None) -> Fingerprint | None:
    """Identity of the file at `name`, or `None` when it does not exist.

    `hash_up_to` is the largest size worth hashing (`MAX_FILE_WRITE_BYTES` at
    the call sites); above it `sha256` is `None` and the binding is
    metadata-only — a documented limitation, not an oversight, because hashing
    multi-GB media at mint is not acceptable tool latency.

    `None` (no file) and `{... "sha256": None}` (a file too big to hash) are
    deliberately different values: the first is the expected-absence sentinel.
    """
    st = _lstat(dir_fd, name)
    if st is None:
        return None
    _require_regular(st, name)
    digest: str | None = None
    if hash_up_to is not None and st.st_size <= hash_up_to:
        digest = _hash_regular(dir_fd, name, expect_ino=st.st_ino, expect_dev=st.st_dev)
    return {
        "dev": st.st_dev,
        "inode": st.st_ino,
        "size": st.st_size,
        "mtime_ns": st.st_mtime_ns,
        "ctime_ns": st.st_ctime_ns,
        "sha256": digest,
    }


_METADATA_FIELDS = ("dev", "inode", "size", "mtime_ns", "ctime_ns")


def _metadata_matches(want: Fingerprint, got: Fingerprint) -> bool:
    return all(want.get(f) == got.get(f) for f in _METADATA_FIELDS)


# ── publication ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Published:
    """Outcome of `publish`.

    `published` is what decides whether the caller may mark the transfer
    complete. `temp_removed` is bookkeeping: once `link`/`replace` has
    succeeded the upload *is* published, and a failing trailing unlink of the
    temp file is a janitorial problem to log — never a reason to fail the
    request or release the claim.
    """

    name: str
    published: bool
    temp_removed: bool


def publish(
    dir_fd: int,
    tmp_name: str,
    final_name: str,
    *,
    overwrite: bool,
    expected_fingerprint: Fingerprint | None,
    dst_dir_fd: int | None = None,
) -> Published:
    """Move `tmp_name` into place as `final_name`, atomically.

    `dir_fd` anchors the *source* (the staging directory holding the temp
    file); `dst_dir_fd` anchors the destination and defaults to `dir_fd` for
    the same-directory case. Splitting them is what lets a caller stage bytes
    somewhere stable for minutes and only then resolve — and re-resolve — the
    destination directory. Both must be on the same device, which holding both
    inside the vault root guarantees.

    `overwrite=False` → hard-link no-clobber (kernel-linearizable).
    `overwrite=True` with `expected_fingerprint=None` → the target was absent
    at mint and must still be absent, so this also takes the no-clobber path
    (the sentinel means "expect absence", never "skip the check").
    `overwrite=True` with a fingerprint → compare, re-hash when the mint
    recorded one, then `replace`.

    Raises `Conflict` when the target is not in the committed state and
    `UnsafePath` when it is a symlink. The temp file is unlinked in `finally`
    either way.
    """
    if dst_dir_fd is None:
        dst_dir_fd = dir_fd
    published = False
    try:
        # Inside the try, not before it: `publish` owns the temp file from the
        # moment it is called, so every exit path — including a rejected
        # argument — must leave the directory clean.
        if "/" in final_name or final_name in ("", ".", ".."):
            raise UnsafePath(f"Illegal final component: {final_name!r}")
        if not overwrite or expected_fingerprint is None:
            _link_no_clobber(dir_fd, tmp_name, final_name, dst_dir_fd=dst_dir_fd)
            published = True
        else:
            current = _lstat(dst_dir_fd, final_name)
            if current is None:
                raise Conflict(
                    f"Target disappeared since the token was minted: {final_name}"
                )
            _require_regular(current, final_name)
            got = {
                "dev": current.st_dev,
                "inode": current.st_ino,
                "size": current.st_size,
                "mtime_ns": current.st_mtime_ns,
                "ctime_ns": current.st_ctime_ns,
            }
            if not _metadata_matches(expected_fingerprint, got):
                raise Conflict(
                    f"Target changed since the token was minted: {final_name}"
                )
            if expected_fingerprint.get("sha256") is not None:
                digest = _hash_regular(
                    dst_dir_fd,
                    final_name,
                    expect_ino=current.st_ino,
                    expect_dev=current.st_dev,
                )
                if digest != expected_fingerprint["sha256"]:
                    raise Conflict(
                        f"Target contents changed since the token was minted: "
                        f"{final_name}"
                    )
            # Check-then-act: a writer landing here still gets overwritten.
            # Declared optimistic conflict detection, not linearizable
            # replacement — see the module docstring and D5.
            os.replace(tmp_name, final_name, src_dir_fd=dir_fd, dst_dir_fd=dst_dir_fd)
            published = True
    finally:
        temp_removed = _unlink_quietly(dir_fd, tmp_name, published=published)

    return Published(name=final_name, published=published, temp_removed=temp_removed)


def _link_no_clobber(dir_fd: int, src: str, dst: str, *, dst_dir_fd: int | None = None) -> None:
    if dst_dir_fd is None:
        dst_dir_fd = dir_fd
    try:
        os.link(src, dst, src_dir_fd=dir_fd, dst_dir_fd=dst_dir_fd, follow_symlinks=False)
    except FileExistsError:
        # Covers a plain file, a directory, *and* a symlink at the target: the
        # kernel refuses all three identically, which is exactly the promise.
        raise Conflict(f"Target already exists: {dst}") from None
    except OSError as exc:
        if exc.errno in (errno.EPERM, errno.EOPNOTSUPP, errno.EXDEV):
            raise UnsupportedFilesystem(
                "The vault filesystem does not support hard links, which the "
                "no-clobber publish depends on (see probe_publication)"
            ) from exc
        raise


def _unlink_quietly(dir_fd: int, name: str, *, published: bool) -> bool:
    try:
        os.unlink(name, dir_fd=dir_fd)
        return True
    except FileNotFoundError:
        # `replace` consumed it. Normal.
        return True
    except OSError as exc:
        if published:
            logger.warning(
                "Published upload but could not remove temp file %s: %s", name, exc
            )
        else:
            logger.warning("Could not remove temp file %s: %s", name, exc)
        return False


def discard_temp(dir_fd: int, name: str) -> bool:
    """Remove a temp file that never got published; never raises.

    The abandon path of a failed upload. It must not be able to turn one
    failure (a 413, a disconnect) into a second, noisier one, so an unlink that
    itself fails is logged and swallowed.
    """
    return _unlink_quietly(dir_fd, name, published=False)


# ── non-replacing rename ────────────────────────────────────────────────────

# `renameat2(2)`'s RENAME_NOREPLACE (`<linux/fs.h>`): fail with `EEXIST` rather
# than replacing an existing destination. This is the one primitive that makes
# a *move into a name we do not already own* safe, and Python's stdlib does not
# expose it — `os.rename` always replaces.
RENAME_NOREPLACE = 1

# `syscall(2)` numbers, used only when glibc is too old (< 2.28) to export the
# `renameat2` wrapper. An architecture that is not listed is treated as "no
# renameat2" rather than guessed at: a wrong number calls a *different*
# syscall, which is far worse than refusing.
_SYS_RENAMEAT2 = {
    "x86_64": 316,
    "aarch64": 276,  # asm-generic; 38 is renameat (replacing!) — never confuse them
    "armv7l": 382,
    "armv8l": 382,
    "i686": 353,
    "i386": 353,
    "ppc64le": 357,
    "s390x": 347,
}

_renameat2_cache: tuple | None = None


def _resolve_renameat2():
    """Find a callable `renameat2`, or `None` if this platform has none.

    Preferred: the glibc wrapper (present since 2.28; the deployment image is
    Debian bookworm's python:3.12-slim, glibc 2.36). Fallback: the raw syscall,
    for the pre-2.28 case only.
    """
    try:
        libc = ctypes.CDLL(None, use_errno=True)
    except OSError:  # pragma: no cover - no libc to bind against
        return None
    try:
        fn = libc.renameat2
    except AttributeError:  # pragma: no cover - glibc < 2.28
        pass
    else:
        fn.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        fn.restype = ctypes.c_int
        return fn

    number = _SYS_RENAMEAT2.get(platform.machine())  # pragma: no cover
    if number is None:  # pragma: no cover
        return None
    raw = libc.syscall  # pragma: no cover
    raw.restype = ctypes.c_long  # pragma: no cover

    def _via_syscall(src_dir_fd, src, dst_dir_fd, dst, flags):  # pragma: no cover
        return raw(
            ctypes.c_long(number),
            ctypes.c_int(src_dir_fd),
            ctypes.c_char_p(src),
            ctypes.c_int(dst_dir_fd),
            ctypes.c_char_p(dst),
            ctypes.c_uint(flags),
        )

    return _via_syscall  # pragma: no cover


def _renameat2_fn():
    """Cached `_resolve_renameat2`; the lookup is per-process, not per-call."""
    global _renameat2_cache
    if _renameat2_cache is None:
        _renameat2_cache = (_resolve_renameat2(),)
    return _renameat2_cache[0]


def _renameat2_raw(
    src_dir_fd: int, src_name: str, dst_dir_fd: int, dst_name: str, flags: int
) -> int:
    """Call `renameat2`; return 0 on success or the `errno` it failed with.

    Deliberately the *only* place the syscall is touched, and deliberately
    returns an errno rather than raising: everything above it is errno mapping,
    which is the part worth testing, and a test can drive every branch by
    monkeypatching this one function instead of hunting for an exotic mount.
    """
    fn = _renameat2_fn()
    if fn is None:  # pragma: no cover - modern Linux always has it
        return errno.ENOSYS
    ctypes.set_errno(0)
    rc = fn(
        src_dir_fd,
        os.fsencode(src_name),
        dst_dir_fd,
        os.fsencode(dst_name),
        flags,
    )
    if rc == 0:
        return 0
    return ctypes.get_errno() or errno.EIO


def rename_noreplace(
    src_dir_fd: int, src_name: str, dst_dir_fd: int, dst_name: str
) -> None:
    """Move `src_name` to `dst_name`, **never** replacing what is already there.

    One syscall, and the kernel makes it atomic against both endpoints:

    * the destination is created or the call fails `EEXIST` — nothing at
      `dst_name` can be clobbered, whoever put it there and whenever;
    * whatever inode currently sits at `src_name` is what moves, so a file that
      replaced the source a microsecond ago is relocated intact rather than
      lost.

    That pair is why the soft delete needs no placeholder to reserve its
    destination. Reserving a name with `O_EXCL` and then `os.rename`-ing onto
    it looks equivalent and is not: `rename` replaces, so between the
    reservation and the rename anything may take that pathname over and be
    silently destroyed — and the error path would then unlink *that* file while
    cleaning up "its own" placeholder. There is nothing to reserve and nothing
    to clean up here.

    Raises `FileExistsError` (EEXIST — the caller retries with another name),
    `FileNotFoundError` (ENOENT), `UnsupportedFilesystem` (EINVAL/ENOSYS/
    EOPNOTSUPP/EXDEV — the kernel or filesystem cannot do a non-replacing
    rename here), `UnsafePath` (EISDIR/ENOTDIR — the two names are not the same
    kind of object), and plain `OSError` for anything else.
    """
    code = _renameat2_raw(
        src_dir_fd, src_name, dst_dir_fd, dst_name, RENAME_NOREPLACE
    )
    if code == 0:
        return
    if code in (errno.EEXIST, errno.ENOTEMPTY):
        raise FileExistsError(code, os.strerror(code), dst_name)
    if code == errno.ENOENT:
        raise FileNotFoundError(code, os.strerror(code), src_name)
    if code in (errno.EINVAL, errno.ENOSYS, errno.EOPNOTSUPP, errno.EXDEV):
        raise UnsupportedFilesystem(
            f"renameat2(RENAME_NOREPLACE) is not available for this rename "
            f"({errno.errorcode.get(code, code)}); a non-replacing move is "
            "required and there is no safe fallback"
        )
    if code in (errno.EISDIR, errno.ENOTDIR):
        raise UnsafePath(
            f"Refusing a rename between mismatched kinds: {src_name} → "
            f"{dst_name} ({errno.errorcode.get(code, code)})"
        )
    raise OSError(code, os.strerror(code), src_name, None, dst_name)


# ── deletion ────────────────────────────────────────────────────────────────


def remove(root_fd: int, rel_path: str | Path) -> None:
    """Permanently unlink a regular file below `root_fd`.

    Refuses symlinks and directories: `delete_file` must never follow a link
    out of the vault, and must never take a directory with it.
    """
    dir_fd, name = _open_parent(root_fd, rel_path, create=False)
    try:
        st = _lstat(dir_fd, name)
        if st is None:
            raise FileNotFoundError(f"File not found: {rel_path}")
        _require_regular(st, str(rel_path))
        os.unlink(name, dir_fd=dir_fd)
    finally:
        os.close(dir_fd)


def _rename_into_trash(
    src_dir_fd: int, name: str, trash_fd: int, base: str
) -> str:
    """Move `name` into the trash under a fresh `<base>-<8 hex>`; return it.

    The name is claimed *by the move itself* — `RENAME_NOREPLACE` either
    creates it or fails `EEXIST` — so there is no reserved placeholder, no
    window in which the destination pathname exists without our data in it,
    and nothing to unlink if a later step fails.

    The random suffix is what makes two same-second, same-basename deletes land
    on different names; the retry is for the `EEXIST` case, which in practice
    means somebody is pre-creating names to try to wedge the delete.
    """
    last: OSError | None = None
    for _ in range(_TRASH_ATTEMPTS):
        candidate = f"{base}-{secrets.token_hex(4)}"
        try:
            rename_noreplace(src_dir_fd, name, trash_fd, candidate)
        except FileExistsError as exc:
            last = exc
            continue
        return candidate
    raise Conflict(
        f"Could not find a free name in the trash for {base}"
    ) from last


def _refuse_a_moved_directory(
    src_dir_fd: int,
    name: str,
    trash_fd: int,
    created: str,
    rel_path: str | Path,
    trash_dir: str,
) -> None:
    """Undo the move when the thing that moved turned out to be a directory.

    The pre-check `lstat` refuses directories, but the rename that follows it
    moves *whichever inode sits at the source when it runs* — the very property
    that keeps a swapped-in replacement from being destroyed. It cuts both
    ways: put a directory at that name between the check and the rename and the
    whole subtree lands in the trash, reported as a successful file delete. A
    symlink may ride along (it is inert in the trash and was never followed); a
    directory may not, because it carries files nobody asked to delete.

    The rollback is itself `RENAME_NOREPLACE`, so putting the directory back
    can never clobber whatever now holds the source name. If it cannot go back
    — the name is occupied again — the directory stays in the trash and the
    error says exactly where it is. Either way the caller is told no, and
    nothing is lost silently.
    """
    st = _lstat(trash_fd, created)
    if st is None or not stat.S_ISDIR(st.st_mode):
        return
    try:
        rename_noreplace(trash_fd, created, src_dir_fd, name)
    except (OSError, VaultFSError) as exc:
        raise UnsafePath(
            f"Refused: {rel_path} became a directory after the check, and it "
            f"could not be moved back ({exc}). The directory is at "
            f"{trash_dir}/{created} — restore it from there; nothing was "
            "removed."
        ) from exc
    raise UnsafePath(
        f"Refused: {rel_path} became a directory after the check. It was moved "
        "back and nothing was deleted."
    )


def close_quietly(fd: int, what: str) -> None:
    """Close a descriptor; a failing close never reverses a settled verdict.

    These run once the rename has already succeeded or already failed, so a
    `close` returning `EIO` is pure bookkeeping. Left bare it would turn a
    completed soft delete into a reported failure — an agent then retries a
    delete that already happened — and, raising out of a `finally`, skip the
    sibling close and leak that descriptor. Logged at debug and dropped.

    Not for a descriptor whose close is part of the write itself: a staged
    upload's own fd must fail loudly, because a failure there means the data
    may never have reached the disk.
    """
    try:
        os.close(fd)
    except OSError as exc:
        logger.debug("Could not close the %s descriptor: %s", what, exc)


def soft_delete(root_fd: int, rel_path: str | Path, trash_dir: str = TRASH_DIR) -> str:
    """Move a regular file into `trash_dir` under a timestamped name, atomically.

    Returns the trash-relative path that was created:
    `<trash_dir>/<YYYYMMDD-HHMMSS>-<basename>-<8 hex>`.

    **The move is one `renameat2(RENAME_NOREPLACE)`, and nothing is ever
    unlinked or pre-created.** The kernel either creates the trash name or
    fails `EEXIST`, and it moves whatever inode currently sits at the source.
    Both halves matter and both are why the earlier shapes were wrong:

    * `link` + `unlink` could unlink a *different* inode than the one it had
      copied, silently destroying a file that replaced the source in between;
    * `O_EXCL` placeholder + `os.rename` fixed that end but not the other —
      `rename` **replaces**, so between reserving the placeholder and renaming
      onto it a writer could take that trash pathname over and have it
      destroyed, and the error path would unlink that writer's file while
      tidying up "our" placeholder. With `RENAME_NOREPLACE` there is no
      placeholder, no reservation window, and no cleanup to get wrong: a name
      that is already taken costs one `EEXIST` and a fresh random suffix.

    The `lstat` refusal of symlinks and non-regular files still runs first, and
    for a **symlink** it is deliberately *not* re-checked: one swapped in after
    the `lstat` is moved into the trash intact — never followed, never
    dereferenced, nothing outside the vault touched — which is a harmless
    outcome for an operation the caller already asked to remove that name.

    A **directory** swapped in is not harmless, so that one *is* re-checked
    after the fact: the moved name is `lstat`ed in the trash and, if it is a
    directory, put back with a second `RENAME_NOREPLACE` and the delete is
    refused (`_refuse_a_moved_directory`). A subtree carries files nobody asked
    to delete, and "moved 40 notes to .trash" reported as a file delete is the
    kind of success an agent never questions.

    `.trash` lives inside the vault root, so the rename is same-device by
    construction; `EXDEV` (separate mount) and `EINVAL`/`ENOSYS` (a filesystem
    or kernel without `RENAME_NOREPLACE`) raise `UnsupportedFilesystem`, which
    `probe_trash` catches up front rather than at the first delete.
    """
    src_fd, name = _open_parent(root_fd, rel_path, create=False)
    try:
        return soft_delete_at(
            src_fd, name, root_fd, trash_dir=trash_dir, label=rel_path
        )
    finally:
        close_quietly(src_fd, f"source directory for {rel_path}")


def soft_delete_at(
    src_dir_fd: int,
    name: str,
    root_fd: int,
    *,
    trash_dir: str = TRASH_DIR,
    stamp: str | None = None,
    label: str | Path | None = None,
) -> str:
    """`soft_delete` against a parent descriptor the caller already holds.

    Same guarantees, same failure modes; the only difference is that the walk
    from the root to the file's parent has already happened. That matters for
    the note tools: `vault.open_mutable` resolves and opens the parent exactly
    once for the whole call, and re-deriving it here from a pathname would
    reopen the very window the anchoring exists to close.

    `stamp` overrides the trash-name timestamp (`delete_note` stamps in UTC);
    `label` is the vault-relative path used in error messages, defaulting to
    `name`. The caller owns `src_dir_fd` and `root_fd` — neither is closed here.
    """
    rel_path = name if label is None else label
    trash_fd: int | None = None
    try:
        st = _lstat(src_dir_fd, name)
        if st is None:
            raise FileNotFoundError(f"File not found: {rel_path}")
        _require_regular(st, str(rel_path))

        trash_fd = open_dir_beneath(root_fd, trash_dir, create=True)
        if stamp is None:
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        try:
            created = _rename_into_trash(
                src_dir_fd, name, trash_fd, f"{stamp}-{name}"
            )
        except FileNotFoundError:
            # Somebody else removed the source between the `lstat` and here.
            # Nothing was created in the trash, so there is nothing to undo.
            raise FileNotFoundError(f"File not found: {rel_path}") from None
        except UnsupportedFilesystem as exc:
            raise UnsupportedFilesystem(
                f"{trash_dir}/ cannot receive a non-replacing rename from the "
                f"vault ({exc}), so a soft delete cannot be atomic "
                "(see probe_trash)"
            ) from exc
        except UnsafePath:
            # The name became a directory after the `lstat`. Never take a
            # directory with us.
            raise UnsafePath(f"Not a regular file: {rel_path}") from None
        _refuse_a_moved_directory(
            src_dir_fd, name, trash_fd, created, rel_path, trash_dir
        )
        return f"{trash_dir}/{created}"
    finally:
        # The verdict is settled by here; a failing close must not change it.
        # `src_dir_fd` belongs to the caller and is deliberately left open.
        if trash_fd is not None:
            close_quietly(trash_fd, f"{trash_dir} directory")


def _open_parent(
    root_fd: int,
    rel_path: str | Path,
    *,
    create: bool,
    created: list[str] | None = None,
) -> tuple[int, str]:
    parts = _split(rel_path)
    if not parts:
        raise UnsafePath(f"Not a file path: {rel_path!r}")
    name = parts[-1]
    dir_fd = open_dir_beneath(
        root_fd, "/".join(parts[:-1]), create=create, created=created
    )
    return dir_fd, name


def open_parent(
    root_fd: int,
    rel_path: str | Path,
    *,
    create: bool = False,
    created: list[str] | None = None,
) -> tuple[int, str]:
    """Public form of `_open_parent`: (directory fd, final component).

    The caller owns the returned descriptor and must close it. `created` is
    passed straight through to `open_dir_beneath` — see there for what it
    records and why the durability flush needs it.
    """
    return _open_parent(root_fd, rel_path, create=create, created=created)


# ── durability ──────────────────────────────────────────────────────────────


def flush_dir_fd(dir_fd: int) -> None:
    """`fsync` an open **directory** descriptor.

    Publication is a *directory* operation: the payload's own `fsync` makes the
    contents durable and says nothing about the entry that names them. After a
    crash the two are independent, so without this the vault can hold no entry
    at all at a path an upload has already recorded `completed`, or — for a
    note — lose a write the tool reported.

    It deliberately does not decide what a failure means. The two write paths
    take **opposite** directions on that (D18): the transfer path surfaces it as
    a post-publication failure, because the source bytes are gone and the
    ambiguity has to reach the human; the note path logs it and reports the
    write as the success it is, because a false failure gets retried and a
    retried `edit_note(append=True)` appends the same block twice.
    """
    os.fsync(dir_fd)


def flush_created_ancestors(root_fd: int, created: Iterable[str]) -> None:
    """`fsync` the parent of every directory a call created, innermost first.

    `created` is what `open_dir_beneath(create=True)` / `open_parent` recorded:
    the vault-relative paths of the directories *this* call brought into
    existence, outermost first. Making `New/Folder/x.md` durable means flushing
    `New/Folder` (the destination's own parent — the caller's job, it already
    holds that descriptor), then `New` for the entry naming `Folder`, then the
    root for the entry naming `New`. Stop there: the first pre-existing
    directory's entry was already somebody else's to make durable.

    Each parent is re-opened by a fresh beneath-root lookup rather than kept
    from the descent — `_create_descent` carries no descriptor across, and this
    must not become the exception that does.

    Raises like `flush_dir_fd`; the caller decides what a failure means.
    """
    seen: set[str] = set()
    for rel in reversed(list(created)):
        parent = rel.rsplit("/", 1)[0] if "/" in rel else ""
        if parent in seen:
            continue
        seen.add(parent)
        fd = open_dir_beneath(root_fd, parent)
        try:
            flush_dir_fd(fd)
        finally:
            close_quietly(fd, f"created ancestor {parent or '.'!r}")


# ── startup probe ───────────────────────────────────────────────────────────


def _probe_link(src_dir_fd: int, name: str, dst_dir_fd: int, link_name: str, what: str) -> None:
    try:
        os.link(
            name,
            link_name,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
            follow_symlinks=False,
        )
    except OSError as exc:
        if exc.errno in (errno.EPERM, errno.EOPNOTSUPP, errno.EXDEV, errno.ENOSYS):
            raise UnsupportedFilesystem(
                f"The vault filesystem does not support hard links {what} "
                f"({errno.errorcode.get(exc.errno, exc.errno)}). Transfer "
                "uploads and delete_file rely on them for atomic no-clobber "
                "publication and are disabled."
            ) from exc
        raise
    _unlink_quietly(dst_dir_fd, link_name, published=False)


# Errnos a `fsync` returns when the filesystem, the kernel or the container
# will not do it *at all*, as opposed to failing this particular flush. They
# are what a probe has to convert into a refusal; anything else (`EIO`,
# `ENOSPC`) is a sick device and propagates as the `OSError` it is.
_FLUSH_UNSUPPORTED = (
    errno.EINVAL,
    errno.ENOSYS,
    errno.EOPNOTSUPP,
    errno.EPERM,
    errno.EACCES,
    errno.EROFS,
)


def _probe_flush(fd: int, what: str) -> None:
    """`fsync` a probe descriptor, mapping "cannot" to `UnsupportedFilesystem`."""
    try:
        os.fsync(fd)
    except OSError as exc:
        code = getattr(exc, "errno", None)
        if code in _FLUSH_UNSUPPORTED:
            raise UnsupportedFilesystem(
                f"The vault filesystem cannot flush {what} to durable storage "
                f"({errno.errorcode.get(code, code)}). Every publish flushes "
                "the staged payload before publication and the destination "
                "directory afterwards, so an upload or an import is refused "
                "here rather than after a whole body has been streamed and "
                "published."
            ) from exc
        raise


def probe_publication(root_fd: int) -> None:
    """Verify a publish can work: hard links, and both flushes, in the root.

    **This probe writes** (a temp file and a link, both removed again), so it
    belongs only on paths that are about to write. A read — a download, a
    `check_upload` — must never call it: a read-only capability that creates
    files, however briefly, is a write the caller did not ask for.

    It exercises every primitive the publish depends on and can test from the
    root: the hard link, a **payload flush** and a **directory flush** (#97).
    The two flushes are not decoration. A filesystem or container that links
    happily and rejects `fsync` on a directory would otherwise pass this probe,
    accept a token, take a whole 25 MB body, publish it — and only then strand
    the claim on the post-publication flush, which is the one failure the
    transfer path deliberately cannot undo. The point of a probe is that the
    environment is refused *before* a body is streamed.

    Raises `UnsupportedFilesystem` when links are refused (`EPERM`/
    `EOPNOTSUPP`/`ENOSYS`), would cross a device (`EXDEV`), or when either
    flush is refused.
    """
    fd, tmp_name = create_temp(root_fd)
    try:
        try:
            _probe_flush(fd, "a staged file")
        finally:
            os.close(fd)
        _probe_link(root_fd, tmp_name, root_fd, f"{tmp_name}-probe", "within the vault root")
        _probe_flush(root_fd, "a directory")
    finally:
        # One `finally` around everything after the temp exists: a refused
        # payload flush must not leave the probe's own file in the vault.
        _unlink_quietly(root_fd, tmp_name, published=False)


def probe_trash(root_fd: int, trash_dir: str = TRASH_DIR) -> None:
    """Verify a soft delete can work: a `rename` from the root into `trash_dir`.

    Separate from `probe_publication` because it tests a different syscall on a
    different pair of directories, and because only `delete_file` needs it. A
    vault whose `.trash` is a separate mount passes the publication probe and
    fails this one with `EXDEV` — and would then be unable to soft-delete at
    all, so it has to be caught here rather than at the first delete.

    Note it probes `rename`, not `link`: `soft_delete` moves the file with one
    rename, so a filesystem that refuses hard links but renames fine is
    perfectly able to soft-delete and must not be refused.

    It probes the **exact** primitive the delete uses —
    `renameat2(RENAME_NOREPLACE)` via `_rename_into_trash`, not a plain
    `os.rename`. A kernel or filesystem that renames happily but rejects the
    `RENAME_NOREPLACE` flag (`EINVAL`/`ENOSYS`) would otherwise pass this probe
    and fail every delete, which is precisely the failure mode the probe
    exists to move to startup.
    """
    fd, tmp_name = create_temp(root_fd)
    os.close(fd)
    trash_fd: int | None = None
    created: str | None = None
    try:
        trash_fd = open_dir_beneath(root_fd, trash_dir, create=True)
        try:
            created = _rename_into_trash(
                root_fd, tmp_name, trash_fd, f"{tmp_name}-probe"
            )
        except UnsupportedFilesystem as exc:
            raise UnsupportedFilesystem(
                f"The vault filesystem cannot move files into {trash_dir}/ "
                f"with a non-replacing rename ({exc}). `delete_file`'s soft "
                "delete relies on it and is disabled; pass permanent=True to "
                "unlink instead."
            ) from exc
        except OSError as exc:
            if exc.errno in (errno.EPERM, errno.EACCES):
                raise UnsupportedFilesystem(
                    f"The vault filesystem cannot move files into {trash_dir}/ "
                    f"({errno.errorcode.get(exc.errno, exc.errno)}). "
                    "`delete_file`'s soft delete relies on an atomic rename and "
                    "is disabled; pass permanent=True to unlink instead."
                ) from exc
            raise
        _unlink_quietly(trash_fd, created, published=False)
    finally:
        if trash_fd is not None:
            os.close(trash_fd)
        if created is None:
            _unlink_quietly(root_fd, tmp_name, published=False)


def prune_stale_staging(
    root_fd: int, *, max_age_seconds: float = STALE_STAGING_SECONDS
) -> int:
    """Remove `.transfer-tmp/.tmp-*` files older than `max_age_seconds`.

    A crash or a hard kill mid-upload leaves a staged temp file nothing will
    ever publish. It is invisible to the indexer and to every dot-dir-guarded
    tool, so it would otherwise sit there consuming disk forever.

    Age is `mtime`, and the comparison is strictly older-than: a file being
    written *right now* has a fresh mtime, so an in-flight upload can never be
    swept out from under itself. Never raises — janitorial work must not be
    able to fail an operation.
    """
    try:
        # Tighten before listing, never `open_dir_beneath` directly: this is
        # the one other opener of the directory, and on a deployment upgrading
        # from a release that left it 0755 the prune runs first (#95).
        staging_fd = open_staging_dir(root_fd, create=False)
    except (FileNotFoundError, VaultFSError, OSError):
        return 0
    removed = 0
    try:
        cutoff = time.time() - max_age_seconds
        try:
            entries = os.listdir(staging_fd)
        except OSError as exc:  # pragma: no cover - unreadable staging dir
            logger.warning("Could not list %s for pruning: %s", STAGING_DIR, exc)
            return 0
        for entry in entries:
            if not entry.startswith(".tmp-"):
                continue
            try:
                st = os.stat(entry, dir_fd=staging_fd, follow_symlinks=False)
            except OSError:
                continue
            if not stat.S_ISREG(st.st_mode) or st.st_mtime >= cutoff:
                continue
            if _unlink_quietly(staging_fd, entry, published=False):
                removed += 1
    finally:
        # Janitorial work must never fail the operation that triggered it, and
        # a bare `os.close` can raise (EIO). `prune_stale_staging` is called
        # from the publication probe, i.e. on the foreground upload path.
        close_quietly(staging_fd, STAGING_DIR)
    if removed:
        logger.info("Pruned %d stale staged upload(s) from %s", removed, STAGING_DIR)
    return removed


# Cached per (vault root, probe kind): a probe touches the disk, and every
# transfer tool needs the answer. `None` means "supported"; an exception
# instance means the caller must refuse with that message.
_probe_cache: dict[tuple[str, str], UnsupportedFilesystem | None] = {}


def _cached_probe(root: Path | str, kind: str, probe, root_fd: int | None = None) -> None:
    key = (str(root), kind)
    if key not in _probe_cache:
        # A caller that already holds an anchored root descriptor passes it:
        # re-opening the root *by name* would walk the pathname again, and the
        # probe writes, so a root symlink repointed since the caller anchored
        # would have the probe create `.trash` in somebody else's directory.
        owned = root_fd is None
        fd = open_root(root) if owned else root_fd
        try:
            probe(fd)
            _probe_cache[key] = None
        except UnsupportedFilesystem as exc:
            _probe_cache[key] = exc
        finally:
            if owned:
                os.close(fd)
    cached = _probe_cache[key]
    if cached is not None:
        raise cached


def check_publication_support(root: Path | str) -> None:
    """Cached `probe_publication` for one root; raises when unsupported.

    First use per root is also where abandoned staged uploads are swept, since
    it is the one moment we already know the root is writable and have not yet
    charged anybody for the walk.
    """
    key = (str(root), "publication")
    first = key not in _probe_cache
    _cached_probe(root, "publication", probe_publication)
    if first:
        root_fd = open_root(root)
        try:
            prune_stale_staging(root_fd)
        finally:
            os.close(root_fd)


def check_trash_support(root: Path | str, *, root_fd: int | None = None) -> None:
    """Cached `probe_trash` for one vault root; raises when unsupported.

    `root_fd`, when given, is the descriptor the probe runs against instead of
    re-opening `root` by name — `delete_note` holds one from validation and the
    probe must not be able to write into a directory the root's pathname has
    since been repointed at. The caller keeps ownership of it.
    """
    _cached_probe(root, "trash", probe_trash, root_fd)


def reset_filesystem_probe_cache() -> None:
    """Forget cached probe results (tests, and vault-root reassignment)."""
    _probe_cache.clear()
