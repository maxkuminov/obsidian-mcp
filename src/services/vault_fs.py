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
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath

from src.config import settings

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

# How a root stages an in-flight transfer. Decided **once per root**, by
# `probe_publication`, and recorded in the probe's cached result — never
# re-decided per call, per token or per body. A root that staged one upload
# without a name and the next one under a name would make the window each
# upload ran in unknowable after the fact.
STAGING_MODE_UNNAMED = "unnamed"
STAGING_MODE_NAMED = "named"

# How many transient overwrite names to try before giving up. Each carries 128
# bits of randomness and is claimed by a no-clobber `linkat`, so one attempt
# effectively always wins; the retry exists so a hostile pre-creation loop
# cannot wedge a publish.
_TRANSIENT_ATTEMPTS = 8

# Whether this process has actually staged under a name (`VAULT_ALLOW_NAMED_
# STAGING_FALLBACK`). Process state, set on **first exercise** — not when the
# flag is set and not when a probe merely selects the mode. That distinction is
# the whole value of the signal: it separates an operator who enabled the flag
# defensively from a mount that is taking the fallback.
_named_staging_exercised = False
_named_staging_exercised_lock = threading.Lock()


# Which write path took the fallback, for the one warning below. The two
# staging *locations* are not equivalent (D27): a transfer stages in
# `.transfer-tmp` — `0700`, owner-checked, dot-prefixed, hidden from every
# vault tool — while a note write stages beside its destination, in an
# ordinary vault directory the vault's own write tools can reach, which is
# the wider of the two windows. One warning that named only `.transfer-tmp`
# was therefore false for every note-path exercise, so the warning names both
# locations and says which path fired it.
NAMED_STAGING_NOTE_PATH = "note write"
NAMED_STAGING_TRANSFER_PATH = "transfer upload"


def note_named_staging_exercised(path_kind: str) -> None:
    """Record that a call has staged under a name, warning once per process.

    Shared by both write paths, so one warning and one `/health` field answer
    for the note path and the transfer path together (D27). Call it at the
    moment the staging name **has been created**, never earlier: a creation
    that failed every attempt staged nothing, and must neither spend the
    warn-once budget nor flip `/health` to a fallback this process never took.

    `path_kind` is `NAMED_STAGING_NOTE_PATH` or `NAMED_STAGING_TRANSFER_PATH`
    — the path that reached here *first*. The warning states both locations
    regardless, because it fires once and the other path may well exercise the
    fallback afterwards with no second warning; that is the declared
    warn-once semantic, not an accident to correct with a second message.

    The check-then-set is locked: a sync request handler can run this from a
    thread-pool worker, and without the lock two concurrent first-time
    fallback writes (racing to be the first exercise, one from each write
    path) can both observe `False` and both log — the flag still ends up
    `True` either way, but the warning is meant to fire once per process, not
    once per race.
    """
    global _named_staging_exercised
    with _named_staging_exercised_lock:
        if _named_staging_exercised:
            return
        _named_staging_exercised = True
    logger.warning(
        "VAULT_ALLOW_NAMED_STAGING_FALLBACK is set and this vault's "
        "filesystem cannot stage without a directory entry, so writes are "
        "staging under a name. The first exercise in this process was a %s; "
        "note writes stage beside the destination, in an ordinary vault "
        "directory, and transfer uploads stage in %s/. The staged name exists "
        "for the whole write, which reopens the substitution window unnamed "
        "staging closes; it is narrowed, not closed, by the identity check "
        "that precedes every publish, and the note path's window is the wider "
        "of the two. This fires once per process, so a later exercise by the "
        "other path is not announced again. Unset the flag to refuse instead.",
        path_kind,
        STAGING_DIR,
    )


def named_staging_fallback_active() -> bool:
    """Whether the named-staging fallback has been exercised in this process.

    What `/health` reports. It reads process state and **never probes** — a
    probe writes, and a health check must not create a file in the vault.
    """
    return _named_staging_exercised


def reset_named_staging_state() -> None:
    """Forget that the fallback was exercised (tests only)."""
    global _named_staging_exercised
    _named_staging_exercised = False


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
    need different ones: hard links within the root (`EPERM`/`EOPNOTSUPP`) for
    the no-clobber publish, and a `rename` into `.trash` (`EINVAL`/`ENOSYS`/
    `EOPNOTSUPP`) for the soft delete. The transfer tools refuse rather than
    silently degrading to an overwriting move.

    `EXDEV` is deliberately **not** in either list. It is a mount boundary, not
    a missing capability, and it raises `MountBoundary` — a subclass, so every
    surface that answers this one keeps answering it while the message names
    the layout instead of the filesystem.
    """


Fingerprint = dict


# ── directory anchoring ─────────────────────────────────────────────────────


class MountBoundary(UnsupportedFilesystem):
    """The two directories an operation must span are on different mounts.

    A subclass, so every surface that already answers `UnsupportedFilesystem`
    — the tools' error string, `PUT /transfer/upload`'s 503 — keeps answering
    it without a new branch. What it buys is a *message that names the real
    cause*: `link(2)` and `rename(2)` return `EXDEV` across a mount boundary,
    and reporting that as "the vault filesystem does not support hard links"
    tells an operator to change filesystems in response to a mount layout.
    A bind mount of a directory of the same filesystem, mounted beneath the
    vault root, has the same `st_dev` on both sides and still refuses the link
    and the rename — which is why the check that produces this is on mount
    identity and never on `st_dev` (D23).
    """


def open_root(root: Path | str) -> int:
    """Open the vault root as an anchor descriptor.

    The root itself is the one path resolved by name — it is operator
    configuration (a bind mount), not attacker input. Everything *below* it is
    reached with a single `openat2(RESOLVE_BENEATH | RESOLVE_NO_SYMLINKS |
    RESOLVE_NO_MAGICLINKS)` from this descriptor (#87), never by opening one
    component at a time: the kernel proves containment for the whole path
    inside that one call, so there is no interval between components for a
    rename to exploit. See `open_dir_beneath`.

    Because the root *is* resolved by name, a repointed root pathname is a real
    substitution surface — which is why callers that already hold one pass it
    down rather than re-opening, and why a cached probe result is bound to this
    descriptor's identity as well as to the configured string
    (`root_identity`).
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

    **This is the whole-server floor: Linux 5.6.** `mount_id_of`'s
    `STATX_MNT_ID` needs 5.8, but that is a **transfer-write minimum**, not a
    server floor — `probe_mount_identity` checks it separately and warns rather
    than exiting, because its absence refuses a publish instead of admitting an
    unchecked one. The message here mentions it so an operator fixing this one
    knows the other exists.
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
            "profile must permit the syscall. Transfer *writes* additionally "
            "need Linux 5.8 for statx(2)'s STATX_MNT_ID; that is a "
            "transfer-write minimum, checked separately, and it does not stop "
            "the server."
        )
    if code in (errno.EINVAL, errno.E2BIG):
        raise UnsupportedFilesystem(
            f"openat2(2) rejected this struct open_how "
            f"({errno.errorcode.get(code, code)}): the binding and the "
            "kernel's ABI disagree, so no containment check can be performed."
        )


# ── mount identity ──────────────────────────────────────────────────────────

# `statx(2)`'s `STATX_MNT_ID` (Linux **5.8**) — the mount a descriptor was
# resolved through, which is the only thing that distinguishes a bind mount of
# a directory of the *same* filesystem from the filesystem it was bound from.
# `AT_EMPTY_PATH` makes the call operate on the descriptor itself.
STATX_MNT_ID = 0x00001000
AT_EMPTY_PATH = 0x1000


class _StatxTimestamp(ctypes.Structure):
    _fields_ = [
        ("tv_sec", ctypes.c_int64),
        ("tv_nsec", ctypes.c_uint32),
        ("__reserved", ctypes.c_int32),
    ]


class _Statx(ctypes.Structure):
    """`struct statx` (`<linux/stat.h>`), through the field the kernel added last.

    Laid out field by field so `ctypes` computes the offsets rather than a
    hand-written constant: `stx_mnt_id` lands at 144 and the struct at 256
    bytes, which is what the running kernel reports. The trailing `__spare3`
    is what makes the buffer the full size the kernel may write into — a short
    buffer is how this kind of binding corrupts memory rather than failing.
    """

    _fields_ = [
        ("stx_mask", ctypes.c_uint32),
        ("stx_blksize", ctypes.c_uint32),
        ("stx_attributes", ctypes.c_uint64),
        ("stx_nlink", ctypes.c_uint32),
        ("stx_uid", ctypes.c_uint32),
        ("stx_gid", ctypes.c_uint32),
        ("stx_mode", ctypes.c_uint16),
        ("__spare0", ctypes.c_uint16),
        ("stx_ino", ctypes.c_uint64),
        ("stx_size", ctypes.c_uint64),
        ("stx_blocks", ctypes.c_uint64),
        ("stx_attributes_mask", ctypes.c_uint64),
        ("stx_atime", _StatxTimestamp),
        ("stx_btime", _StatxTimestamp),
        ("stx_ctime", _StatxTimestamp),
        ("stx_mtime", _StatxTimestamp),
        ("stx_rdev_major", ctypes.c_uint32),
        ("stx_rdev_minor", ctypes.c_uint32),
        ("stx_dev_major", ctypes.c_uint32),
        ("stx_dev_minor", ctypes.c_uint32),
        ("stx_mnt_id", ctypes.c_uint64),
        ("stx_dio_mem_align", ctypes.c_uint32),
        ("stx_dio_offset_align", ctypes.c_uint32),
        ("__spare3", ctypes.c_uint64 * 12),
    ]


_statx_cache: object | None = None


def _statx_fn():
    """The glibc `statx` wrapper, or `None` where it does not exist.

    **Reached through the wrapper, unlike `openat2`** — and the difference is
    measured rather than assumed. In the running container `statx` resolves
    through `ctypes.CDLL(None)` and `openat2` raises `AttributeError`: glibc
    has exported `statx` since 2.28 and exports no `openat2` at any version,
    which is why D24's raw-syscall-with-a-number-table reasoning applies there
    and not here. A wrapper also spares us a per-architecture syscall table
    that a wrong entry would turn into a call to a *different* syscall.
    """
    global _statx_cache
    if _statx_cache is None:
        try:
            libc = ctypes.CDLL(None, use_errno=True)
            fn = libc.statx
        except (OSError, AttributeError):  # pragma: no cover - no glibc statx
            _statx_cache = False
        else:
            fn.restype = ctypes.c_int
            fn.argtypes = [
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_int,
                ctypes.c_uint,
                ctypes.POINTER(_Statx),
            ]
            _statx_cache = fn
    return _statx_cache or None


def mount_id_of(fd: int) -> int:
    """The id of the mount `fd` was resolved through.

    Raises `UnsupportedFilesystem` when `statx` is unavailable or when the
    kernel answers without setting `STATX_MNT_ID` in `stx_mask` — **never a
    fall back to `st_dev`, and never to the errno**. `st_dev` is precisely the
    comparison this exists to replace: a bind mount of an ext4 directory
    beneath the vault root reports the same `st_dev` on both sides (measured:
    66306) and different mount ids (653 vs 6036), while `link` and `rename`
    across it both return `EXDEV`. A preflight that fell back to `st_dev` would
    pass and let the publish fail after the whole body had streamed — that was
    the first draft and review caught it. A guard that degrades quietly is the
    failure mode, so this refuses instead.

    `STATX_MNT_ID` is Linux **5.8**, above `openat2`'s 5.6, so this is what
    sets the change's kernel floor; the startup probe's message says so.
    `STATX_MNT_ID_UNIQUE` (6.8) is deliberately **not** required — it would
    raise the floor again for a property `same_mount` does not need, because
    `same_mount` never compares across time (D23).
    """
    fn = _statx_fn()
    if fn is None:
        raise UnsupportedFilesystem(
            "statx(2) is unavailable, so the mount a directory lives on "
            "cannot be established. Transfer publication needs it to refuse a "
            "destination on a different mount before a body is streamed."
        )
    buf = _Statx()
    ctypes.set_errno(0)
    # `pointer`, not `byref`: `byref` produces a light-weight argument object
    # with no `.contents`, which a test cannot reach into. 4.7 requires a
    # `statx` stubbed to answer *without* the mount-id bit — the one case where
    # falling back to `st_dev` would look like it worked — so the binding has
    # to be drivable from a stub.
    rc = fn(fd, b"", AT_EMPTY_PATH, STATX_MNT_ID, ctypes.pointer(buf))
    if rc != 0:
        code = ctypes.get_errno()
        raise UnsupportedFilesystem(
            f"statx(2) could not read the mount id "
            f"({errno.errorcode.get(code, code)}); refusing rather than "
            "comparing st_dev, which a same-filesystem bind mount defeats."
        )
    if not buf.stx_mask & STATX_MNT_ID:
        raise UnsupportedFilesystem(
            "This kernel's statx(2) does not report STATX_MNT_ID (Linux 5.8 "
            "and later do), so a destination on a different mount cannot be "
            "detected before a body is streamed. Refusing rather than "
            "comparing st_dev, which a same-filesystem bind mount defeats."
        )
    return int(buf.stx_mnt_id)


# Whether this kernel can answer `STATX_MNT_ID`, as established by the startup
# probe. `None` until it has run (or in a process that never runs it).
_mount_identity_available: bool | None = None


def probe_mount_identity() -> None:
    """Refuse if this kernel cannot report a descriptor's mount.

    **Read-only: it creates nothing** — one `statx` of the process's own working
    directory — so it belongs at startup beside `probe_beneath_root_lookup`.

    Unlike that one it is **not a reason to refuse to start**, and the
    asymmetry is the point. `openat2` is a *containment* guard: without it every
    vault write would anchor to a descriptor whose containment nobody checked,
    so serving is worse than not serving. `STATX_MNT_ID` guards one refusal on
    one feature — it makes transfer publication decline a destination on another
    mount before a body streams. Without it `mount_id_of` raises and every
    transfer *write* refuses, which is the safe direction; every other path in
    the server — reads, search, the note tools, the panel, OAuth — is correct
    and unaffected. Killing all of that to defend a transfer-only check would be
    the false-positive direction: a whole-server outage in response to a partial
    capability.

    So `src/main.py::_check_mount_identity_support` logs a warning, records the
    partial service through `record_mount_identity_support`, and starts.
    """
    fd = os.open(".", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        mount_id_of(fd)
    finally:
        close_quietly(fd, "mount identity probe working directory")


def record_mount_identity_support(available: bool) -> None:
    """Record what the startup probe found, for `/health` to report."""
    global _mount_identity_available
    _mount_identity_available = available


def mount_identity_available() -> bool | None:
    """What the startup probe found: `True`, `False`, or `None` if it never ran.

    `None` is not "unknown and probably fine" — it is a process that skipped the
    probe (`MCP_SANDBOX_MODE`) or never had one. `/health` reports it as-is
    rather than guessing, and **never probes**: this is process state.
    """
    return _mount_identity_available


def reset_mount_identity_state() -> None:
    """Forget the startup probe's verdict (tests only)."""
    global _mount_identity_available
    _mount_identity_available = None


def same_mount(fd_a: int, fd_b: int) -> bool:
    """Whether two open descriptors live on the same mount.

    **Both ids are read inside this one call and compared here.** Never persist
    a mount id and compare it against a later reading: plain `STATX_MNT_ID` is
    reused once its mount is gone, and the only thing that makes it sufficient
    without `STATX_MNT_ID_UNIQUE` is that no comparison here ever spans time
    (D23). If you find yourself storing one, you need the unique form and a
    higher kernel floor.
    """
    return mount_id_of(fd_a) == mount_id_of(fd_b)


def require_same_mount(staging_fd: int, dst_fd: int, dest_label) -> None:
    """Refuse when the destination is not on the staging directory's mount.

    Uploads and imports stage in a root-level staging directory and publish
    from there into the destination with a hard link (no-clobber) or a
    replacing rename (overwrite), and both refuse to cross a mount boundary
    with `EXDEV`. The publication probe links root→root and is cached per root,
    so it cannot see this — it is a property of the *pair*, not of the root.
    """
    if not same_mount(staging_fd, dst_fd):
        raise MountBoundary(
            f"{dest_label} is on a different mount from the vault's staging "
            "directory, so an upload cannot be published there: the link or "
            "rename that publishes it cannot cross a mount boundary. The "
            "filesystem is fine; the mount layout is what refuses. Choose a "
            "destination on the same mount as the vault root."
        )


def cross_mount_definitely(fd_a: int, fd_b: int) -> bool:
    """Whether two descriptors are **definitely** on different mounts.

    The fail-*open* form of `same_mount`, and the policy is written here once
    rather than at each call: "cannot establish" answers `False`, so the caller
    proceeds and the syscall's own `EXDEV` — mapped by `rename_noreplace` — is
    what names the cause. Only a clean reading of both mount ids that differ
    is a refusal.

    **Not for the transfer path.** `require_same_mount` fails *closed* on the
    same question and that is deliberate: a late `EXDEV` there costs a body
    that has already streamed in full, so "cannot check" must not mint a
    capability. Here the failure a preflight would prevent costs nothing — the
    rename fails immediately and accurately either way — while failing closed
    on a kernel between the `openat2` floor (5.6) and `STATX_MNT_ID` (5.8)
    would take soft delete and `move_note` away from a deployment that serves
    them correctly today. The two directions are opposite on purpose; do not
    reuse this one to "simplify" the other.

    `same_mount`'s no-time-spanning rule is respected by construction: both ids
    are read inside its single call, immediately before the act, and neither is
    stored. Like every preflight in this module it is check-then-act — a mount
    appearing between it and the rename is caught by the residual mapping,
    which is why the mapping and not this is the correctness layer.
    """
    try:
        return not same_mount(fd_a, fd_b)
    except UnsupportedFilesystem:
        return False


def leaf_mount_id(dst_dir_fd: int, name: str) -> int | None:
    """The mount id of the file at `name`, or `None` when it cannot be read.

    `O_PATH|O_NOFOLLOW`, so it neither opens the file for I/O nor follows a
    symlink at the final component — and `statx(AT_EMPTY_PATH)` works on an
    `O_PATH` descriptor, which is one of the things that descriptor kind is for.
    `None` covers "no such name" and "this kernel cannot answer", both of which
    mean the caller has nothing to refuse on.
    """
    try:
        fd = os.open(name, os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=dst_dir_fd)
    except OSError:
        return None
    try:
        return mount_id_of(fd)
    except UnsupportedFilesystem:
        return None
    finally:
        close_quietly(fd, f"leaf mount check for {name}")


def leaf_is_separate_mount(staging_fd: int, dst_dir_fd: int, name: str) -> bool:
    """Whether `name` is itself a mount point distinct from the staging mount.

    **The parent check does not cover this.** A bind mount can be established on
    the destination *file*, not its directory: the parent then compares equal to
    the staging directory and the publish still fails, because a replacing
    rename onto a mount point is `EBUSY` (measured on the deployment kernel,
    with the same `st_dev` on both sides). It only matters for an overwrite —
    a no-clobber publish onto an existing name is `EEXIST` either way, and
    "target already exists" is the accurate thing to say there.

    True only when **both** ids are read cleanly and differ. Anything unreadable
    is not evidence of a boundary, and inventing one would refuse a publish that
    would have worked.
    """
    leaf = leaf_mount_id(dst_dir_fd, name)
    if leaf is None:
        return False
    try:
        return mount_id_of(staging_fd) != leaf
    except UnsupportedFilesystem:
        return False


def require_leaf_on_same_mount(
    staging_fd: int, dst_dir_fd: int, name: str, dest_label
) -> None:
    """Refuse when the destination *file* is itself a separate mount point."""
    if leaf_is_separate_mount(staging_fd, dst_dir_fd, name):
        raise MountBoundary(
            f"{dest_label} is itself a mount point, so the rename that "
            "publishes an overwrite cannot replace it. The filesystem is fine; "
            "the mount layout is what refuses. Unmount it, or choose a "
            "destination that is an ordinary file."
        )


def deepest_existing_dir(root_fd: int, rel_dir: str | Path) -> tuple[int, str]:
    """Open the deepest ancestor of `rel_dir` that exists; return `(fd, rel)`.

    For the mint-time mount check, where the destination's parent may not exist
    yet: a directory created beneath an ancestor is created on **that
    ancestor's** mount, so the ancestor answers the question the parent would.
    Each attempt is one beneath-root lookup, and the caller owns the descriptor.
    """
    parts = _split(rel_dir)
    while True:
        rel = "/".join(parts)
        try:
            return open_dir_beneath(root_fd, rel), rel
        except FileNotFoundError:
            if not parts:  # pragma: no cover - the root always exists
                raise
            parts = parts[:-1]


def require_destination_mount(
    root_fd: int, rel_path: str | Path, *, overwrite: bool = False
) -> None:
    """Mint-time half of the mount check: staging directory vs. destination.

    Runs **before a capability is minted or a fetch begins**, so a boundary that
    is already there costs a syscall rather than a whole body. Where the
    destination's own parent does not exist yet the deepest existing ancestor
    is compared instead, because that is the mount any directory created
    beneath it will land on.

    The staging directory is used when it exists and the **root** when it does
    not — `.transfer-tmp` is created as a direct child of the root, so it lands
    on the root's mount, and a mint must not be the thing that creates it. The
    in-gate half (`transfer._publish_into_current_parent`) always has the real
    staging descriptor.

    It reaches the staging directory with a plain beneath-root lookup, **not**
    `open_staging_dir`: that one enforces the 0700 owner-only policy and
    refuses a directory it cannot tighten, which is the right thing to do when
    a call is about to put bytes there and the wrong thing to do here. A mint
    must not fail — or succeed — on the strength of a policy check that belongs
    to the write. `_stream_locked` and the prune still run the enforcing form.

    This is check-then-act, deliberately: it spares the body where the boundary
    already exists, and the in-gate re-check is what covers a mount established
    afterwards. Neither makes the residual `EXDEV` mapping unnecessary.
    """
    rel_dir = str(PurePosixPath(str(rel_path)).parent)
    rel_dir = "" if rel_dir == "." else rel_dir
    try:
        staging_fd = open_dir_beneath(root_fd, STAGING_DIR)
    except (FileNotFoundError, UnsafePath):
        staging_fd = open_dir_beneath(root_fd, "")
    try:
        dst_fd, rel = deepest_existing_dir(root_fd, rel_dir)
        try:
            require_same_mount(staging_fd, dst_fd, rel_path)
            # And the destination *file*, when this mint is for an overwrite: a
            # bind mount on the target itself leaves the parent comparing equal
            # and still fails the rename with `EBUSY`. Only when the parent
            # actually resolved — under a missing parent there is no leaf.
            if overwrite and rel == rel_dir:
                require_leaf_on_same_mount(
                    staging_fd,
                    dst_fd,
                    PurePosixPath(str(rel_path)).name,
                    rel_path,
                )
        finally:
            close_quietly(dst_fd, f"mount check for {rel_path}")
    finally:
        close_quietly(staging_fd, "mount check staging directory")


def check_destination_mount(
    root: Path | str, rel_path: str | Path, *, overwrite: bool = False
) -> None:
    """`require_destination_mount` for a caller holding the root as a pathname.

    The mint tools' form, mirroring `check_publication_support(root)`. It is
    deliberately **not** cached: unlike a probe this answers about a *pair*, the
    destination differs per call, and a mount can appear at any time.
    """
    root_fd = open_root(root)
    try:
        require_destination_mount(root_fd, rel_path, overwrite=overwrite)
    finally:
        close_quietly(root_fd, "vault root")


# Owner-only: nothing but this process has any business in the staging
# directory. See `open_staging_dir`.
STAGING_DIR_MODE = 0o700


def open_staging_dir(root_fd: int, *, create: bool = True) -> int:
    """Open `.transfer-tmp` beneath `root_fd`, enforcing owner-only access.

    The directory stays even though the unnamed staging mode puts nothing in
    it: `O_TMPFILE` takes a *directory* to choose the filesystem the inode is
    allocated on, so `.transfer-tmp` is still what selects where staged bytes
    live (D19). What went away is its contents.

    Staged bytes are relaxed to `default_file_mode()` before publication, so
    the directory — not the file — is what keeps an in-flight upload private.
    That matters because staging is the window in which the bytes exist but
    the publish gate has not yet revalidated the credential: a body that is
    about to be *refused* is on disk for the duration, and under a
    group-writable umask a peer could also alter it after `_drain` computed
    its digest, so the server would publish bytes that do not match the
    sha256 it reports. 0700 removes both, whatever the staged file's own mode
    and whatever the operator's umask.

    **What the 0700 is now for.** In the unnamed mode it is no longer the only
    thing protecting staged bytes — an inode with no directory entry cannot be
    opened by name at all, so that window closes structurally rather than by
    permissions. The mode enforcement stays as defence in depth, and it is a
    live guard on the two names that do appear here: the overwrite publish's
    transient name, which exists for two syscalls inside the gate, and — where
    `VAULT_ALLOW_NAMED_STAGING_FALLBACK` selects the named mode — a staging
    name that lives for the whole streaming window.

    The mode is enforced on every open, not just at creation: `mkdir` is
    masked by the umask, and a `.transfer-tmp` left at 0755 by an older
    release (or by a `mkdir -p` from anywhere else) must be corrected rather
    than trusted. `fchmod` goes through the descriptor we just opened
    `O_NOFOLLOW`, so it cannot be redirected to another directory by a
    rename in between.

    Destination directories are deliberately *not* treated this way — they hold
    published vault content and get the ordinary 0755 that
    `open_dir_beneath(create=True)`'s `mkdirat` produces under the umask.
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


def create_nameless_temp(dir_fd: int) -> int:
    """Stage into an **unnamed** inode inside `dir_fd`; return the descriptor.

    `O_TMPFILE` gives a file with no directory entry at all — nothing for
    another process to observe, replace or race for the whole streaming window,
    and nothing to clean up afterwards. That last part is not a convenience: a
    named staging file has to be unlinked, and an unlink is by *name*, so it can
    only ever be guarded by an identity check followed by the removal —
    check-then-act, which could delete a substitute planted in between. With no
    name there is no such step, and an abandoned upload's bytes are reclaimed by
    the kernel when the last descriptor closes rather than sitting in
    `.transfer-tmp` for a day waiting for the sweep.

    `dir_fd` is the **staging directory**, and it is what selects the filesystem
    the inode is allocated on — which is why `O_TMPFILE` does not retire
    `.transfer-tmp` or any of `open_staging_dir`'s guarantees about it (D19).
    What goes away is the directory's *contents*.

    **`O_EXCL` must not be set.** With `O_TMPFILE` it means "this file may never
    be linked into the filesystem", which makes `link_staged_inode` fail
    `ENOENT` — the opposite of its usual meaning, and an easy thing to add by
    reflex.

    A filesystem that refuses it (`EOPNOTSUPP`, or `EISDIR`/`EINVAL`/`ENOSYS`
    on kernels that report it that way) raises `UnsupportedFilesystem`. ext4 and
    xfs both do it; TrueNAS SCALE's NFS server does not (#103), which is what
    `VAULT_ALLOW_NAMED_STAGING_FALLBACK` exists for — and the refusal names it,
    so an operator meeting this does not have to read the source to find the
    escape valve. Selecting that fallback is `probe_publication`'s job, once per
    root; this function only ever refuses.
    """
    flags = os.O_TMPFILE | os.O_RDWR | getattr(os, "O_CLOEXEC", 0)
    try:
        return os.open(".", flags, 0o600, dir_fd=dir_fd)
    except OSError as exc:
        if getattr(exc, "errno", None) in (
            errno.EOPNOTSUPP,
            errno.EISDIR,
            errno.ENOSYS,
            errno.EINVAL,
        ):
            raise UnsupportedFilesystem(
                "The vault filesystem does not support O_TMPFILE, which "
                "staging uses so that no temporary name is ever exposed. "
                "Refusing rather than staging under a name; set "
                "VAULT_ALLOW_NAMED_STAGING_FALLBACK to take named staging "
                "back on this mount."
            ) from exc
        raise


# Whether `/proc/self/fd` is usable for publishing a staged inode. Cached: it
# is a property of the container, not of the call.
_proc_fd_available_cache: bool | None = None


def proc_fd_available() -> bool:
    global _proc_fd_available_cache
    if _proc_fd_available_cache is None:
        _proc_fd_available_cache = os.path.isdir("/proc/self/fd")
    return _proc_fd_available_cache


def link_staged_inode(fd: int, dir_fd: int, name: str) -> None:
    """Publish the inode behind `fd` as `name` in `dir_fd`, no-clobber.

    `linkat(AT_FDCWD, "/proc/self/fd/<fd>", dir_fd, name, AT_SYMLINK_FOLLOW)`.
    The magic link resolves to the open file description, so what gets published
    is provably the inode this call wrote — no name is consulted, so there is
    nothing a peer could have substituted and nothing to check. `fd` is an
    `O_TMPFILE` staging descriptor with no directory entry at all.

    Two kernel details worth recording, because both look like blockers and
    neither is: the `AT_EMPTY_PATH` form of this call needs
    `CAP_DAC_READ_SEARCH`, which an ordinary container does not have, while the
    `/proc` magic link does not; and the "cannot link a zero-link inode" rule
    applies to an inode whose names have all been *removed*, not to one created
    `O_TMPFILE`. Verified on the deployment's kernel with `CapEff=0`.

    Linux-only, which the declared filesystem semantics already require; without
    `/proc` there is no way to publish an inode by descriptor and we refuse
    rather than fall back to publishing whatever a staging *name* points at.
    `EEXIST` is the ordinary no-clobber refusal — a plain file, a directory and
    a symlink at the destination all produce it — and propagates as
    `FileExistsError` for the caller to phrase.

    This lives here rather than in `vault.py` because both write paths publish
    this way and a second copy is how the two drifted apart before (#59, #92).
    """
    if not proc_fd_available():
        raise UnsupportedFilesystem(
            "/proc is not available, so a staged file cannot be published by "
            "descriptor; refusing rather than publishing by name. Set "
            "VAULT_ALLOW_NAMED_STAGING_FALLBACK to take named staging back."
        )
    try:
        os.link(
            f"/proc/self/fd/{fd}",
            name,
            dst_dir_fd=dir_fd,
            follow_symlinks=True,
        )
    except FileExistsError:
        raise
    except FileNotFoundError as exc:
        raise Conflict(
            "The staged copy could not be published; nothing was written. "
            "Retry the operation."
        ) from exc
    except OSError as exc:
        code = getattr(exc, "errno", None)
        if code == errno.EXDEV:
            raise MountBoundary(
                f"{name} is on a different mount from the directory the bytes "
                "were staged in, so the staged inode cannot be linked there "
                "(EXDEV). The filesystem is fine; the mount layout is what "
                "refuses."
            ) from exc
        if code in (errno.EPERM, errno.EOPNOTSUPP):
            raise UnsupportedFilesystem(
                "The vault filesystem does not support hard links, which the "
                "no-clobber publish depends on; refusing rather than replacing "
                "an existing file."
            ) from exc
        raise


def _materialise_staged_name(staged_fd: int, dir_fd: int) -> str:
    """Give the staged inode a transient name in `dir_fd`; return that name.

    The overwrite publish cannot consume an unnamed inode — `renameat` has no
    by-descriptor form, and `RENAME_EXCHANGE` does not help because it still
    names the source. So the choice is not "name or no name" but *when* the name
    exists, and this is called **inside the publish gate**, immediately before
    the fingerprint check and the rename (D20). The name then exists for two
    syscalls, in a 0700 directory owned by this process, instead of for the
    whole multi-minute body plus an unbounded wait on the gate's row locks.

    `dir_fd` is the **staging** directory, never the destination directory —
    one respect in which the transfer path ends up stronger than the note path,
    which stages beside its destination in a directory the vault's own tools can
    write to.

    No-clobber, with a bounded `EEXIST` retry: a hostile pre-creation loop can
    cost this publish, and must not be able to make it overwrite anything.
    """
    last: FileExistsError | None = None
    for _ in range(_TRANSIENT_ATTEMPTS):
        name = f".tmp-{secrets.token_hex(16)}"
        try:
            link_staged_inode(staged_fd, dir_fd, name)
            return name
        except FileExistsError as exc:  # pragma: no cover - 128-bit collision
            last = exc
    raise VaultFSError(
        "Could not give the staged file a transient name in the staging "
        "directory"
    ) from last


def staged_identity_matches(dir_fd: int, name: str, staged: os.stat_result) -> bool:
    """Whether `name` in `dir_fd` still refers to the inode we staged."""
    try:
        current = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except OSError:
        return False
    return (current.st_dev, current.st_ino) == (staged.st_dev, staged.st_ino)


def require_staged_name(dir_fd: int, name: str, staged: os.stat_result) -> None:
    """Refuse unless `name` still refers to the bytes this call staged.

    Both by-name publications need it — the `link` of a fallback-mode
    no-clobber publish and the `renameat` of either mode's overwrite — because
    both act on whatever is at the source name when they run, and neither can be
    made to carry an inode the way `linkat` through `/proc/self/fd` can.

    It **narrows** the substitution window to the single publishing syscall; it
    does not close it, and the spec says "refused" only for the interval where
    refusal is achievable. A substitution observable here is refused; one landing
    between here and the publish is still published, and that is a declared
    residual (D20) rather than a gap — an actor who can create a name in a 0700
    directory owned by this process can also rewrite the destination directly.
    """
    if not staged_identity_matches(dir_fd, name, staged):
        raise Conflict(
            "The staged copy was replaced before it could be published; "
            "nothing was written. Retry the operation."
        )


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
    complete. `temp_removed` is bookkeeping: once the `link`/`replace` has
    succeeded the upload *is* published, and a failing trailing unlink of a
    staging name is a janitorial problem to log — never a reason to fail the
    request or release the claim.

    In the unnamed staging mode there is usually no name at all, so
    `temp_removed` is true by construction: the discard is closing the
    descriptor, which the caller owns. The one exception is the overwrite
    publish's transient name, which `publish` creates and therefore reports on.
    """

    name: str
    published: bool
    temp_removed: bool


def publish(
    dir_fd: int,
    tmp_name: str | None,
    final_name: str,
    *,
    overwrite: bool,
    expected_fingerprint: Fingerprint | None,
    dst_dir_fd: int | None = None,
    staged_fd: int | None = None,
    staged_st: os.stat_result | None = None,
) -> Published:
    """Move the staged file into place as `final_name`, atomically.

    `dir_fd` anchors the *staging* directory; `dst_dir_fd` anchors the
    destination and defaults to `dir_fd` for the same-directory case. Splitting
    them is what lets a caller stage bytes somewhere stable for minutes and only
    then resolve — and re-resolve — the destination directory. Both must be on
    the same mount, which the mint-time and in-gate mount-identity checks
    establish; holding both inside the vault root is not on its own enough,
    because a bind mount beneath the root reports the same `st_dev`.

    **Two staging modes, selected once per root by `probe_publication` and
    passed in here by the shape of the arguments** (D27):

    * **unnamed** — `tmp_name is None` and `staged_fd` is the `O_TMPFILE`
      descriptor. A no-clobber publish is `linkat` through `/proc/self/fd/<fd>`,
      so what lands is provably the inode this call wrote and no name is
      consulted at any point. An overwrite publish materialises a transient
      name in the **staging** directory immediately before the fingerprint
      check and the rename, and discards it under the identity guard below.
    * **named** (the `VAULT_ALLOW_NAMED_STAGING_FALLBACK` fallback, for mounts
      that refuse `O_TMPFILE`) — `tmp_name` is a `.tmp-*` created through
      `create_temp`. Both publications are by name, and both run the identity
      check first. The name exists for the whole streaming window: that is the
      declared residual of the fallback, narrowed — not closed — by the check.

    `overwrite=False` → hard-link no-clobber (kernel-linearizable).
    `overwrite=True` with `expected_fingerprint=None` → the target was absent at
    mint and must still be absent, so this also takes the no-clobber path (the
    sentinel means "expect absence", never "skip the check").
    `overwrite=True` with a fingerprint → compare, re-hash when the mint
    recorded one, then `replace`.

    `staged_st` is the `fstat` of the staging descriptor, taken by the caller
    that created it — the strongest form of the identity the check compares
    against. Omitting it in named mode falls back to the identity `publish`
    reads at entry, which still refuses a substitution landing during the
    publish but cannot see one that landed before `publish` was called; the
    transfer path always passes the real one. In unnamed mode it is derived from
    `staged_fd` and the parameter is ignored. If the name is already gone when
    that fallback `lstat` runs, the identity stays `None` and the `finally`
    below **leaves** whatever now holds the name rather than unlinking it —
    `discard_staged_name` will not remove a name it cannot prove is ours.

    Raises `Conflict` when the target is not in the committed state or the
    staged file was substituted, and `UnsafePath` when the target is a symlink.
    Any name this function is responsible for is discarded in `finally` — under
    the identity guard, so a substitute is left in place and logged rather than
    unlinked.
    """
    if dst_dir_fd is None:
        dst_dir_fd = dir_fd
    unnamed = tmp_name is None
    if unnamed and staged_fd is None:
        raise ValueError("Unnamed staging requires the staged descriptor")
    if unnamed:
        staged_st = os.fstat(staged_fd)
    elif staged_st is None:
        staged_st = _lstat(dir_fd, tmp_name)

    published = False
    transient: str | None = None
    try:
        # Inside the try, not before it: `publish` owns the staged file from the
        # moment it is called, so every exit path — including a rejected
        # argument — must leave the staging directory clean.
        if "/" in final_name or final_name in ("", ".", ".."):
            raise UnsafePath(f"Illegal final component: {final_name!r}")
        no_clobber = not overwrite or expected_fingerprint is None
        if unnamed and no_clobber:
            # The whole point: nothing was ever named, so there is nothing to
            # verify and nothing to race. `link()` is kernel-atomic.
            try:
                link_staged_inode(staged_fd, dst_dir_fd, final_name)
            except FileExistsError:
                # A plain file, a directory *and* a symlink at the target all
                # produce this, which is exactly the promise.
                raise Conflict(f"Target already exists: {final_name}") from None
            published = True
        else:
            source = tmp_name
            if unnamed:
                # Inside the gate, and only now: two syscalls of exposure in a
                # 0700 directory instead of a name that lives for minutes (D20).
                transient = _materialise_staged_name(staged_fd, dir_fd)
                source = transient
            if not no_clobber:
                _require_committed_target(
                    dst_dir_fd, final_name, expected_fingerprint
                )
            if staged_st is not None:
                require_staged_name(dir_fd, source, staged_st)
            if no_clobber:
                _link_no_clobber(dir_fd, source, final_name, dst_dir_fd=dst_dir_fd)
            else:
                # Check-then-act: a writer landing here still gets overwritten.
                # Declared optimistic conflict detection, not linearizable
                # replacement — see the module docstring and D5.
                try:
                    os.replace(
                        source, final_name, src_dir_fd=dir_fd, dst_dir_fd=dst_dir_fd
                    )
                except OSError as exc:
                    # The overwrite branch used to let `EXDEV` escape as a bare
                    # `OSError`: correctly classified pre-publication, but it
                    # reached the upload route's generic handler and gave the
                    # person a server error where the no-clobber branch gave a
                    # 503. Both branches name the boundary now.
                    code = getattr(exc, "errno", None)
                    if code == errno.EXDEV:
                        raise MountBoundary(
                            f"{final_name} is on a different mount from the "
                            "staging directory, so the rename that publishes "
                            "an overwrite cannot reach it (EXDEV). The "
                            "filesystem is fine; the mount layout is what "
                            "refuses."
                        ) from exc
                    # `EBUSY` is what a rename **onto a mount point** returns,
                    # and it is the one the parent-directory check cannot see.
                    # It is reclassified only when a fresh look establishes that
                    # cause: `EBUSY` has other sources, and labelling all of
                    # them a mount boundary would send an operator after a mount
                    # that is not there.
                    if code == errno.EBUSY and leaf_is_separate_mount(
                        dir_fd, dst_dir_fd, final_name
                    ):
                        raise MountBoundary(
                            f"{final_name} is itself a mount point, so the "
                            "rename that publishes an overwrite cannot replace "
                            "it (EBUSY). The filesystem is fine; the mount "
                            "layout is what refuses."
                        ) from exc
                    raise
            published = True
    finally:
        owned = transient if unnamed else tmp_name
        if owned is None:
            # Unnamed, no-clobber: the inode never had a name. Discarding it is
            # the caller closing the descriptor it owns.
            temp_removed = True
        else:
            temp_removed = discard_staged_name(
                dir_fd, owned, staged_st, published=published
            )

    return Published(name=final_name, published=published, temp_removed=temp_removed)


def _require_committed_target(
    dst_dir_fd: int, final_name: str, expected_fingerprint: Fingerprint
) -> None:
    """Refuse unless the destination is still the file the mint bound to."""
    current = _lstat(dst_dir_fd, final_name)
    if current is None:
        raise Conflict(f"Target disappeared since the token was minted: {final_name}")
    _require_regular(current, final_name)
    got = {
        "dev": current.st_dev,
        "inode": current.st_ino,
        "size": current.st_size,
        "mtime_ns": current.st_mtime_ns,
        "ctime_ns": current.st_ctime_ns,
    }
    if not _metadata_matches(expected_fingerprint, got):
        raise Conflict(f"Target changed since the token was minted: {final_name}")
    if expected_fingerprint.get("sha256") is not None:
        digest = _hash_regular(
            dst_dir_fd,
            final_name,
            expect_ino=current.st_ino,
            expect_dev=current.st_dev,
        )
        if digest != expected_fingerprint["sha256"]:
            raise Conflict(
                f"Target contents changed since the token was minted: {final_name}"
            )


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
        if exc.errno == errno.EXDEV:
            # Not a filesystem that cannot link — a boundary that cannot be
            # crossed. The preflight makes this rare rather than gone (it is
            # check-then-act), so the mapping still has to be right.
            raise MountBoundary(
                f"{dst} is on a different mount from the staging directory, "
                "so the link that publishes an upload cannot reach it "
                "(EXDEV). The filesystem is fine; the mount layout is what "
                "refuses."
            ) from exc
        if exc.errno in (errno.EPERM, errno.EOPNOTSUPP):
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


def discard_staged_name(
    dir_fd: int,
    name: str,
    staged: os.stat_result | None,
    *,
    published: bool,
) -> bool:
    """Remove a staging name — and never anybody else's. Never raises.

    Reached on every by-name publication path: a successful `replace` (which
    consumed the name, so this is a no-op), a `link` publish that leaves its
    source behind, and every failure after staging.

    **The unlink runs only while the name still refers to the inode this call
    staged.** If it does not, the file is left in place and logged. Answering an
    attempted substitution by deleting the substitute is the same
    destructive-write class this module exists to prevent, just aimed at a
    different file, so the failure direction is to leave litter rather than
    remove something we cannot prove is ours — the same posture `soft_delete`
    and `_discard_temp` in `vault.py` already take.

    The pre-change transfer path unlinked its staging name unconditionally. The
    named-staging fallback deliberately does **not** inherit that: a name that
    lives for the whole streaming window has more need of the guard than the
    transient overwrite name it was introduced for, not less (D27).

    **An absent name is not a substitution.** A successful overwrite publish is
    a `renameat` that *consumes* the staging name, so by the time this runs
    there is nothing there — the ordinary case, and it must not be reported as
    somebody having taken the name over. Only a name that exists and refers to
    a different inode is a substitution.

    **`staged is None` refuses to unlink at all.** It means the caller could
    not establish what it staged — an `fstat` that failed after the exclusive
    creation, or a name already gone when `publish` looked for it — so nothing
    here can prove the name still refers to our inode, and a concurrent
    replacement would be destroyed by a write that published nothing. That is
    the same destructive-write class the identity check above refuses, reached
    by a path that has *less* evidence rather than more, so it takes the same
    direction: warn, leave the litter, return False. The pre-change transfer
    path removed it unguarded; the fallback deliberately does not inherit
    that (#104 review).

    **An absent name is ordinary only after a publish that consumes it.** With
    `published=True` a `renameat` took the name and there is nothing to do.
    With `published=False` the staging name disappeared while the write was
    still in flight — a substitution's first half, or somebody else's cleanup
    — which is exactly the kind of event this function exists to surface, so
    it is warned about and reported as a failed discard.
    """
    if staged is None:
        logger.warning(
            "Cannot confirm what was staged under %s, so it is left in place "
            "rather than unlinked: removing a name whose inode we cannot "
            "prove is ours is the destructive write this guard exists to "
            "refuse.",
            name,
        )
        return False
    try:
        current = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        if published:
            # The publish consumed it. The ordinary case.
            return True
        logger.warning(
            "Staging name %s disappeared before its write was published; "
            "nothing was removed by this cleanup.",
            name,
        )
        return False
    except OSError as exc:
        logger.warning(
            "Could not confirm that staging name %s is still ours (%s); "
            "leaving it in place.",
            name,
            exc,
        )
        return False
    if (current.st_dev, current.st_ino) != (staged.st_dev, staged.st_ino):
        logger.warning(
            "Staging name %s no longer refers to the file we staged; "
            "leaving it in place rather than unlinking a file we did not "
            "create.",
            name,
        )
        return False
    return _unlink_quietly(dir_fd, name, published=published)


def discard_temp(
    dir_fd: int, name: str, staged: os.stat_result | None = None
) -> bool:
    """Discard a staged file that never got published; never raises.

    The abandon path of a failed upload in the named-staging mode. It must not
    be able to turn one failure (a 413, a disconnect) into a second, noisier
    one, so an unlink that itself fails is logged and swallowed — and it goes
    through `discard_staged_name`, so a substitute is left alone, and so is
    the name when `staged` is `None` (the caller never got an identity to
    compare against, which is strictly less evidence, not more).

    The unnamed mode never calls this: there is no name to remove, and closing
    the descriptor frees the inode.
    """
    return discard_staged_name(dir_fd, name, staged, published=False)


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
    EOPNOTSUPP — the kernel or filesystem cannot do a non-replacing rename
    here), `MountBoundary` (EXDEV), `UnsafePath` (EISDIR/ENOTDIR — the two
    names are not the same kind of object), and plain `OSError` for anything
    else.

    **`EXDEV` is not one of the unsupported cases and must not be folded back
    in with them.** The other three mean this kernel or this filesystem cannot
    perform a non-replacing rename at all; `EXDEV` from `renameat2` means,
    definitionally, that the two names sit on different mounts — the filesystem
    renames perfectly well. Reporting it as "renameat2 is not available" sends
    an operator, or an agent acting on the text, to change filesystems in
    response to a mount layout, which is the defect class `MountBoundary`
    exists to remove. Every caller inherits the accurate cause from here, so a
    caller that re-wraps rename failures in its own prose has to catch the
    subclass **before** `UnsupportedFilesystem` or its wrapper is a lie and the
    subclass branch unreachable — see `soft_delete_at` and `probe_trash`.
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
    if code == errno.EXDEV:
        raise MountBoundary(
            f"{src_name} and {dst_name} are on different mounts, so the "
            "non-replacing rename that moves one to the other cannot cross "
            "the boundary (EXDEV). The filesystem is fine; the mount layout "
            "is what refuses."
        )
    if code in (errno.EINVAL, errno.ENOSYS, errno.EOPNOTSUPP):
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
        # An unlink is a directory operation like any other publication: an
        # entry that survives a crash resurrects a file the caller was told is
        # gone. Swallowed on failure — the file *is* unlinked, and reporting a
        # failure would invite a retry of a delete that already happened (#97).
        flush_dir_quietly(dir_fd, f"parent directory of {rel_path}")
    finally:
        os.close(dir_fd)


def _trash_mount_boundary(
    rel_path: str | Path, trash_dir: str, detail: str | None = None
) -> MountBoundary:
    """The soft delete's mount-boundary refusal, in one place.

    Raised from two points that must not drift: the best-effort preflight, and
    the residual mapping of an `EXDEV` that reached the rename. Both say the
    same thing because it is the same fact — `.trash` is opened beneath the
    vault *root*, so a file on a mount nested beneath that root has a source
    parent the trash rename cannot reach.

    It names `permanent=True` because that is the workaround an agent can
    actually act on: an unlink crosses no mount boundary. A per-mount trash
    would make the soft delete work and is explicitly out of scope here; what
    this closes is a message that blamed `.trash/`'s ability to receive a
    non-replacing rename for a layout the filesystem has no say in.
    """
    because = f" ({detail})" if detail else ""
    return MountBoundary(
        f"{rel_path} and the vault root's {trash_dir}/ are on different "
        f"mounts, so the rename that soft-deletes it cannot cross the "
        f"boundary{because}. The filesystem is fine; the mount layout is what "
        f"refuses — {trash_dir}/ lives beside the vault root and the file does "
        "not. Pass permanent=True to unlink the file instead."
    )


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
    # The rollback is a publication too, and the one whose loss is worst: a
    # restore that evaporates in a crash leaves the subtree in `.trash` under a
    # name the refusal message never mentioned (#97).
    flush_dir_quietly(src_dir_fd, f"parent directory of {rel_path}")
    flush_dir_quietly(trash_fd, f"{trash_dir} directory")
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
    construction — but not same-*mount* by construction: a directory bind
    mounted beneath the root shares the root's `st_dev` and still refuses the
    rename. That case raises `MountBoundary` naming the layout (best-effort
    before the rename, and from the rename's own `EXDEV` otherwise), while
    `EINVAL`/`ENOSYS`/`EOPNOTSUPP` — a filesystem or kernel without
    `RENAME_NOREPLACE` — raise `UnsupportedFilesystem`, which `probe_trash`
    catches up front rather than at the first delete.
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
    # `.trash` on the very first soft delete of a vault. Flushing the directory
    # persists its *contents*; the entry in the root that names it is a
    # separate write, and losing that loses the note we just moved into it.
    created_dirs: list[str] = []
    try:
        st = _lstat(src_dir_fd, name)
        if st is None:
            raise FileNotFoundError(f"File not found: {rel_path}")
        _require_regular(st, str(rel_path))

        trash_fd = open_dir_beneath(
            root_fd, trash_dir, create=True, created=created_dirs
        )
        # Best-effort, and before anything is renamed: where the kernel can
        # answer the mount question a boundary that is already there is
        # refused with an accurate cause instead of an `EXDEV` the caller has
        # to interpret. `cross_mount_definitely` fails open, so a kernel
        # without `STATX_MNT_ID` keeps its soft delete and the mapping below
        # is the backstop. Nothing has been created in the trash yet beyond
        # `trash_dir` itself, which the next delete needs anyway.
        if cross_mount_definitely(src_dir_fd, trash_fd):
            raise _trash_mount_boundary(rel_path, trash_dir)
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
        except MountBoundary as exc:
            # **Above the `UnsupportedFilesystem` branch, or it is
            # unreachable** — `MountBoundary` is a subclass. It is also the one
            # cause the generic wrapper below would actively misreport: a mount
            # boundary says nothing about whether `.trash/` can receive a
            # non-replacing rename, and it can.
            raise _trash_mount_boundary(rel_path, trash_dir, str(exc)) from exc
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
        # Both ends of the rename, after the directory refusal has had its say
        # — flushing before it would make an intermediate state durable and
        # then undo it. The source's entry is gone and the trash's is new, and
        # a crash that keeps only one of those leaves the note either
        # duplicated or lost (#97). Swallowed: the delete has happened, and a
        # reported failure would invite a retry of it.
        flush_dir_quietly(src_dir_fd, f"parent directory of {rel_path}")
        flush_dir_quietly(trash_fd, f"{trash_dir} directory")
        # And the entry that *names* `trash_dir`, if this call is what brought
        # it into existence. Without it a crash can durably remove
        # `Folder/note.md` and lose the whole `.trash` directory with the only
        # copy of the note inside it — the same created-ancestor class the note
        # and transfer publishes already flush. Same D18 direction as the two
        # above: the delete has happened, so a reported failure would invite a
        # retry of an operation that already landed.
        flush_publication_ancestors_quietly(
            root_fd, trash_dir, created_dirs, f"the soft delete of {rel_path}"
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


def flush_dir_quietly(dir_fd: int, what: str) -> None:
    """`fsync` a directory descriptor; a failure is logged and swallowed (D18).

    The form every *rename* publication uses — `move_note`'s `renameat2`, the
    soft delete's rename into `.trash`, and the permanent unlink. All of them
    take the note path's failure direction, and for the note path's reason: the
    rename has already happened, so an error here says only that the new state
    may not survive a crash. A tool that reported that as a failure would be
    *retried*, and a retried move or delete after a rename that landed is its
    own hazard — the second attempt finds the source gone and either refuses
    with a message that contradicts the vault or, worse, acts on whatever has
    since taken the name. What is lost by swallowing is a warning; what is lost
    by raising is the caller's picture of where the file is.

    `flush_dir_fd` is the raising form, for the one place that must classify
    the failure rather than absorb it: the transfer path's post-publication
    flush, which has to reach the human because the source bytes are gone.
    """
    try:
        os.fsync(dir_fd)
    except OSError as exc:
        logger.warning(
            "Could not flush the %s to durable storage: %s. The operation "
            "stands; only its durability across a crash is unconfirmed.",
            what,
            exc,
        )


def _ancestors_up_to_root(rel_dir: str) -> list[str]:
    """Strict ancestors of `rel_dir`, innermost first, ending with the root.

    `"New/Folder"` → `["New", ""]`; `"Attachments"` → `[""]`; `""` → `[]`.
    """
    parts = [p for p in str(rel_dir).split("/") if p not in ("", ".")]
    return ["/".join(parts[:i]) for i in range(len(parts) - 1, -1, -1)]


def publication_flush_dirs(rel_dir: str, created: Iterable[str] = ()) -> list[str]:
    """Directories whose *entries* a publication into `rel_dir` must make durable.

    `rel_dir` is the vault-relative destination **parent**; the caller flushes
    that one itself, through the descriptor it already holds. This returns
    everything above it — innermost first, ending with the root — plus the
    parent of every directory the call recorded creating, deduplicated.

    **Why the whole chain and not just this call's creations.** Attributing the
    flush to the call that ran the `mkdir` looks precise and is not durable
    across an *abort*. An upload that creates `New/Folder` and then dies before
    publication (a 413, a disconnect, a refused deadline) releases its claim and
    flushes nothing — correctly, since nothing was published. The retry finds
    both directories already there, records no creations, publishes, and flushes
    only `New/Folder`. The entry naming `New` was never made durable by anybody,
    so a crash can take the whole folder and with it a file `check_upload` has
    already reported `completed`. The same shape reaches a note write through
    `MutableTarget.ensure_parent`, and `.trash` through a probe or a delete that
    failed after creating it.

    Per-call provenance cannot close that: the obligation outlives the call that
    incurred it, and it outlives the *process*. Recording it durably would mean
    a journal, which is a database for a problem that costs one `fsync` per path
    component. Vault paths are two or three deep and a directory `fsync` is
    metadata-only, so the conservative rule is the cheap one — flush the chain
    on every successful publication, whoever created what.

    The `created` term is kept even though it is currently always a subset: it is
    what a caller that creates a directory *outside* the destination's own chain
    would need, and dropping it would make that a silent hole rather than a
    covered case.
    """
    seen: dict[str, None] = {}
    for rel in _ancestors_up_to_root(str(rel_dir)):
        seen.setdefault(rel, None)
    for rel in created:
        parent = rel.rsplit("/", 1)[0] if "/" in rel else ""
        seen.setdefault(parent, None)
    # Innermost first, so a crash midway through leaves the *outer* entries
    # unflushed rather than the inner ones — the same order
    # the entries depend on each
    # other in.
    return sorted(seen, key=lambda r: len([p for p in r.split("/") if p]), reverse=True)


def flush_publication_ancestors(
    root_fd: int, rel_dir: str, created: Iterable[str] = ()
) -> None:
    """`fsync` every directory `publication_flush_dirs` names, innermost first.

    Making `New/Folder/x.md` durable means flushing `New/Folder` (the caller's
    job — it holds that descriptor) and then `New`, for the entry naming
    `Folder`, and the root, for the entry naming `New`.

    Each is re-opened by a fresh beneath-root lookup rather than kept from a
    descent — `_create_descent` carries no descriptor across, and this must not
    become the exception that does.

    Raises like `flush_dir_fd`; the caller decides what a failure means.
    """
    for rel in publication_flush_dirs(rel_dir, created):
        fd = open_dir_beneath(root_fd, rel)
        try:
            flush_dir_fd(fd)
        finally:
            close_quietly(fd, f"ancestor {rel or '.'!r}")


def flush_publication_ancestors_quietly(
    root_fd: int, rel_dir: str, created: Iterable[str], what: str
) -> None:
    """`flush_publication_ancestors`, every failure logged and swallowed (D18).

    The form the *note* side uses — the soft delete and, through
    `vault._flush_target_dirs`, every note publish. All of them run after the
    operation has already landed, so an error here says only that the new state
    may not survive a crash, and a tool that reported it as a failure would be
    retried: a retried delete finds the source gone, a retried
    `edit_note(append=True)` appends the same block twice. The transfer path
    keeps the raising form, because there the source bytes are gone and the
    ambiguity has to reach the human.
    """
    try:
        flush_publication_ancestors(root_fd, rel_dir, created)
    except (OSError, VaultFSError, RuntimeError) as exc:
        logger.warning(
            "Completed %s but could not flush the directories above %s: %s. "
            "The operation stands; only its durability across a crash is "
            "unconfirmed.",
            what,
            rel_dir or ".",
            exc,
        )


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


def _probe_unnamed_staging(root_fd: int) -> None:
    """Exercise `O_TMPFILE` staging and by-descriptor publication at the root.

    Raises `UnsupportedFilesystem` when either is unavailable. Creates nothing
    that survives: the inode has no name until the probe links it, and that link
    is removed again.
    """
    fd = create_nameless_temp(root_fd)
    linked = f".tmp-{secrets.token_hex(16)}-probe"
    try:
        _probe_flush(fd, "a staged file with no directory entry")
        try:
            link_staged_inode(fd, root_fd, linked)
        except FileExistsError:  # pragma: no cover - 128-bit collision
            raise VaultFSError(
                "Could not publish the probe's staged inode by descriptor"
            ) from None
        _unlink_quietly(root_fd, linked, published=False)
    finally:
        close_quietly(fd, "unnamed staging probe")


def probe_publication(root_fd: int) -> str:
    """Verify a publish can work here, and decide **how** this root stages.

    **This probe writes** (a temp file, an unnamed inode and a link, all removed
    again), so it belongs only on paths that are about to write. A read — a
    download, a `check_upload` — must never call it: a read-only capability that
    creates files, however briefly, is a write the caller did not ask for. It
    creates no `.transfer-tmp` either; everything is exercised at the root,
    which is the same filesystem the staging directory lives on.

    It exercises every primitive the publish depends on and can test from the
    root: the hard link, allocation of a file with **no directory entry**,
    publication of such a file **by descriptor**, a **payload flush** and a
    **directory flush** (#97). None of them is decoration. A filesystem or
    container that links happily and rejects `fsync` on a directory would
    otherwise pass, accept a token, take a whole 25 MB body, publish it — and
    only then strand the claim on the post-publication flush, which is the one
    failure the transfer path deliberately cannot undo. The point of a probe is
    that the environment is refused *before* a body is streamed.

    **It returns the staging mode, and that is the whole of how the mode is
    decided** (D27). `_cached_probe` keeps the answer per root, so a root stages
    one way for the life of the cached result and never re-decides per call, per
    token or per body — a root that staged one upload without a name and the
    next one under a name would make the window each upload ran in unknowable
    after the fact.

    * unnamed staging works → `STAGING_MODE_UNNAMED`.
    * it does not and `VAULT_ALLOW_NAMED_STAGING_FALLBACK` is **off** → raise
      `UnsupportedFilesystem`, naming the missing capability *and* the flag, so
      no token is minted and no body is streamed.
    * it does not and the flag is **on** → `STAGING_MODE_NAMED`, but only after
      the primitives *that* mode needs have been established here too: the
      exclusive, non-symlink-following creation, the hard link within the root,
      the payload flush and the directory flush. A root that fails any of them
      is still refused rather than accepting a body it cannot publish.

    **What it cannot answer for.** It links root→root and is cached per root, so
    it answers only for properties the root and the destination share. A
    destination directory whose filesystem or mount differs from the root's can
    refuse a primitive the root accepted, and this probe cannot see it (D23).

    The one such difference known to occur — a destination on a mount beneath
    the vault root, which refuses the link and the rename the publish depends
    on — is now covered by the **mount-identity preflight**
    (`require_destination_mount` at mint or fetch start,
    `require_same_mount` inside the publish gate), not by this probe: it is a
    property of the *pair*, and this probe answers about one root. What remains
    uncovered is narrower and has no known instance: a capability difference
    between two directories on the **same** mount. That is detected at the
    operation itself, which is why the residual `EXDEV` mapping in `publish`
    stays even with the preflight in front of it.

    The guarantee is therefore "an environment that fails at the root is
    refused before any body is streamed", not "an environment that passes will
    publish".
    """
    # The named primitives first, and unconditionally: they are what the
    # fallback needs, and the hard link and the two flushes are needed by both
    # modes. Doing them before the O_TMPFILE attempt means the fallback is only
    # ever selected for a root that has already proved it can take one.
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

    try:
        _probe_unnamed_staging(root_fd)
    except UnsupportedFilesystem:
        if not settings.vault_allow_named_staging_fallback:
            raise
        # Every primitive the fallback needs has been exercised above. Note the
        # warning is *not* logged here: selecting the mode is not exercising it,
        # and the distinction between "an operator enabled this defensively" and
        # "this mount is taking the fallback" is the whole value of the signal.
        return STAGING_MODE_NAMED
    return STAGING_MODE_UNNAMED


def probe_trash(root_fd: int, trash_dir: str = TRASH_DIR) -> None:
    """Verify a soft delete can work: a `rename` from the root into `trash_dir`.

    Separate from `probe_publication` because it tests a different syscall on a
    different pair of directories, and because only `delete_file` needs it. A
    vault whose `.trash` is a separate mount passes the publication probe and
    fails this one with `EXDEV` — and would then be unable to soft-delete at
    all, so it has to be caught here rather than at the first delete. That case
    is re-raised as `MountBoundary` with the layout named, not as generic
    filesystem inability: see the handler below.

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
    # The probe does not remove `trash_dir` afterwards — a soft delete is about
    # to need it — so if this is what created it, this is what owes the root's
    # entry for it a flush.
    created_dirs: list[str] = []
    try:
        trash_fd = open_dir_beneath(
            root_fd, trash_dir, create=True, created=created_dirs
        )
        try:
            created = _rename_into_trash(
                root_fd, tmp_name, trash_fd, f"{tmp_name}-probe"
            )
        except MountBoundary as exc:
            # **Before the `UnsupportedFilesystem` branch**, which would
            # otherwise erase the subtype and, worse, the cause: this probe
            # renames root→`trash_dir`, so its `EXDEV` means `trash_dir` is
            # itself a mount distinct from the root's. That is the layout this
            # probe meets first and the one the generic wording is furthest
            # from — the filesystem moves files with a non-replacing rename
            # perfectly well.
            raise MountBoundary(
                f"The vault root and {trash_dir}/ are on different mounts "
                f"({exc}), so the rename that performs a soft delete cannot "
                f"reach {trash_dir}/. The filesystem is fine; the mount "
                "layout is what refuses. `delete_file`'s soft delete is "
                f"disabled on this layout — unmount {trash_dir}/ or pass "
                "permanent=True to unlink instead."
            ) from exc
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
        flush_publication_ancestors_quietly(
            root_fd, trash_dir, created_dirs, f"the {trash_dir} probe"
        )
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

    **The sweep stays, and what it collects depends on the mode** (D19, D27).
    In the unnamed mode it has nothing *new* to collect: an inode with no
    directory entry is freed by the kernel when the last descriptor closes,
    which happens in `_stream_locked`'s unwinding for an abandoned upload and at
    process death for a crash. It is retained regardless — the live vault has
    pre-change staging files, and a rolling deploy runs both versions at once,
    so this is the only thing that collects them. Where
    `VAULT_ALLOW_NAMED_STAGING_FALLBACK` selects the named mode an abandoned or
    killed upload leaves a staged file exactly as the pre-change path did, so
    the sweep keeps a live purpose there. Removing it is a separate decision for
    a later release, once no staging file can exist on any path.

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


def root_identity(fd: int) -> tuple:
    """What a cached probe result is bound to, besides the configured pathname.

    `(st_dev, st_ino, mount_id)` of the anchored root. The first two already
    separate two *filesystems* reached through one pathname; the mount id
    additionally separates two mounts of the same directory, which is the case
    `st_dev` alone cannot see and the reason `same_mount` exists.

    The mount id is **optional here and required nowhere else**. `same_mount`
    refuses when `STATX_MNT_ID` is unavailable, because there a fallback to
    `st_dev` would silently answer the wrong question. This is a different
    job: it is a *supplementary* binding on top of the pathname, and on a
    kernel without the bit, `(dev, ino)` is strictly better than the pathname
    alone rather than a wrong answer. So it degrades to `None` and says so.
    """
    st = os.fstat(fd)
    try:
        mnt = mount_id_of(fd)
    except UnsupportedFilesystem:  # pragma: no cover - kernel < 5.8
        mnt = None
    return (st.st_dev, st.st_ino, mnt)


# Cached per (vault root, probe kind): a probe touches the disk, and every
# transfer tool needs the answer. An exception instance means the caller must
# refuse with that message; anything else is the probe's own result — the
# staging mode for `probe_publication`, `None` for `probe_trash`.
#
# The publication entry is what makes the staging mode a property of the root
# rather than of a call: it is computed once and every publication on that root
# reads it back.
#
# **The key is the configured pathname; the entry additionally carries the
# identity of the root that was actually probed.** A configured root is a
# *name*, and a name can be repointed: a symlinked vault root moved from
# filesystem A (where `O_TMPFILE` fails but the fallback's primitives work) to
# filesystem B (where a directory `fsync` is refused) would otherwise reuse A's
# verdict and A's staging mode for B, mint a token, stage a whole body under a
# name on a root nothing ever probed, publish it, and strand the claim on the
# first directory flush. So every hit re-reads the identity and re-probes on a
# mismatch. That is one `fstat` plus one `statx` per cached call — the walk the
# cache exists to avoid is the probe, not this.
_probe_cache: dict[tuple[str, str], tuple[tuple, UnsupportedFilesystem | str | None]] = {}


def _cached_probe(root: Path | str, kind: str, probe, root_fd: int | None = None):
    key = (str(root), kind)
    # A caller that already holds an anchored root descriptor passes it:
    # re-opening the root *by name* would walk the pathname again, and the
    # probe writes, so a root symlink repointed since the caller anchored
    # would have the probe create `.trash` in somebody else's directory.
    owned = root_fd is None
    fd = open_root(root) if owned else root_fd
    try:
        identity = root_identity(fd)
        entry = _probe_cache.get(key)
        if entry is None or entry[0] != identity:
            if entry is not None:
                logger.info(
                    "Vault root %s now resolves to a different root "
                    "(%s -> %s); re-running the %s probe rather than reusing "
                    "a verdict for a filesystem this is not.",
                    root,
                    entry[0],
                    identity,
                    kind,
                )
            try:
                _probe_cache[key] = (identity, probe(fd))
            except UnsupportedFilesystem as exc:
                _probe_cache[key] = (identity, exc)
    finally:
        if owned:
            close_quietly(fd, "vault root")
    cached = _probe_cache[key][1]
    if isinstance(cached, UnsupportedFilesystem):
        raise cached
    return cached


def check_publication_support(
    root: Path | str, *, root_fd: int | None = None
) -> str:
    """Cached `probe_publication` for one root; raises when unsupported.

    Returns the **staging mode** that root uses — `STAGING_MODE_UNNAMED`, or
    `STAGING_MODE_NAMED` where unnamed staging is unavailable and
    `VAULT_ALLOW_NAMED_STAGING_FALLBACK` permits the fallback. Decided once, by
    the probe, and read back here for every later publication on that root: the
    mode is never re-decided per call (D27).

    `root_fd`, when given, is the descriptor the probe runs against instead of
    re-opening `root` by name — the same reason `check_trash_support` takes one.
    The caller keeps ownership of it.

    First use per root is also where abandoned staged uploads are swept, since
    it is the one moment we already know the root is writable and have not yet
    charged anybody for the walk.
    """
    key = (str(root), "publication")
    # "First" means the first probe of *this root*, not of this pathname: a
    # re-probe triggered by the identity check is looking at a filesystem
    # nothing has swept, and its own pre-change litter is exactly what the
    # sweep exists for.
    before = _probe_cache.get(key)
    mode = _cached_probe(root, "publication", probe_publication, root_fd)
    first = before is None or before[0] != _probe_cache[key][0]
    if first:
        owned = root_fd is None
        fd = open_root(root) if owned else root_fd
        try:
            prune_stale_staging(fd)
        finally:
            if owned:
                os.close(fd)
    return mode


def check_trash_support(root: Path | str, *, root_fd: int | None = None) -> None:
    """Cached `probe_trash` for one vault root; raises when unsupported.

    `root_fd`, when given, is the descriptor the probe runs against instead of
    re-opening `root` by name — `delete_note` holds one from validation and the
    probe must not be able to write into a directory the root's pathname has
    since been repointed at. The caller keeps ownership of it.
    """
    _cached_probe(root, "trash", probe_trash, root_fd)


def reset_filesystem_probe_cache() -> None:
    """Forget cached probe results (tests, and vault-root reassignment).

    This drops the recorded staging mode with them, so the next publication on
    that root re-probes and re-decides. That is the only way the mode ever
    changes; nothing on a call path may clear it.
    """
    _probe_cache.clear()
