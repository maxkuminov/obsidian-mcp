"""Descriptor-anchored filesystem primitives for the transfer paths (design D6).

Everything else in `src/services/vault.py` is *path-based*: it resolves a
pathname and then operates by name. Between the resolve and the operation an
attacker who can create a symlink anywhere in the chain gets a TOCTOU window.
That is tolerable for the note tools, which are reachable only through an
authenticated MCP session; it is not tolerable for `/transfer/*`, which is a
public route family redeemed with a bearer capability.

So every operation here is anchored to an open directory descriptor and walks
one component at a time with ``O_NOFOLLOW``. A symlink anywhere in the chain —
ancestor or final component — raises instead of being followed, and nothing can
name a path outside the root because no pathname is ever resolved by the kernel
across more than one component at a time.

``openat2(RESOLVE_BENEATH)`` would state the intent directly but Python's stdlib
does not expose it. Per-component ``O_NOFOLLOW`` from an anchored root fd gives
the same guarantee for our purposes.

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

The helper is written so `vault._atomic_write` can adopt it later (follow-up).
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

_O_COMMON = os.O_CLOEXEC | os.O_NOFOLLOW
_O_DIR = os.O_RDONLY | os.O_DIRECTORY | _O_COMMON

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


def open_dir_beneath(root_fd: int, rel_dir: str | Path, *, create: bool = False) -> int:
    """Open a directory below `root_fd`, one `O_NOFOLLOW` component at a time.

    Always returns a **new** descriptor (even for the root itself), so callers
    can close the result unconditionally without risking the anchor.

    Raises `UnsafePath` when any component is a symlink or not a directory, and
    `FileNotFoundError` when a component is missing and `create` is false.
    """
    parts = _split(rel_dir)
    current = os.open(".", _O_DIR, dir_fd=root_fd)
    try:
        for part in parts:
            child = _open_child(current, part, create=create, rel_dir=rel_dir)
            os.close(current)
            current = child
    except BaseException:
        os.close(current)
        raise
    return current


def _open_child(parent_fd: int, part: str, *, create: bool, rel_dir) -> int:
    """Descend one component. Never closes `parent_fd` — the caller owns it."""
    try:
        return _open_dir_nofollow(parent_fd, part, rel_dir)
    except FileNotFoundError:
        if not create:
            raise
    try:
        os.mkdir(part, 0o755, dir_fd=parent_fd)
    except FileExistsError:
        # Lost a benign race with another creator; the reopen below decides
        # whether what landed there is acceptable.
        pass
    # Re-open rather than trusting the mkdir: between the two, the name could
    # have been replaced by a symlink, and only the O_NOFOLLOW open is
    # authoritative about what we ended up anchored to.
    return _open_dir_nofollow(parent_fd, part, rel_dir)


def _open_dir_nofollow(parent_fd: int, part: str, rel_dir) -> int:
    try:
        return os.open(part, _O_DIR, dir_fd=parent_fd)
    except FileNotFoundError:
        raise
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR, errno.EMLINK):
            raise UnsafePath(
                f"Refusing to traverse a symlink or non-directory at "
                f"{part!r} in {str(rel_dir)!r}"
            ) from None
        raise


# ── temp files and fingerprints ─────────────────────────────────────────────


def create_temp(dir_fd: int) -> tuple[int, str]:
    """Create an exclusive `.tmp-<32 hex>` file in `dir_fd`; return (fd, name).

    Mode 0600 and `O_EXCL|O_NOFOLLOW`: the name cannot pre-exist, cannot be a
    symlink, and the content is never world-readable while it is being written.
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
    "aarch64": 38,
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
    is deliberately *not* re-checked: a symlink swapped in after the `lstat` is
    moved into the trash intact — never followed, never dereferenced, nothing
    outside the vault touched — which is a harmless outcome for an operation
    the caller already asked to remove that name.

    `.trash` lives inside the vault root, so the rename is same-device by
    construction; `EXDEV` (separate mount) and `EINVAL`/`ENOSYS` (a filesystem
    or kernel without `RENAME_NOREPLACE`) raise `UnsupportedFilesystem`, which
    `probe_trash` catches up front rather than at the first delete.
    """
    src_fd, name = _open_parent(root_fd, rel_path, create=False)
    trash_fd: int | None = None
    try:
        st = _lstat(src_fd, name)
        if st is None:
            raise FileNotFoundError(f"File not found: {rel_path}")
        _require_regular(st, str(rel_path))

        trash_fd = open_dir_beneath(root_fd, trash_dir, create=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        try:
            created = _rename_into_trash(src_fd, name, trash_fd, f"{stamp}-{name}")
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
        return f"{trash_dir}/{created}"
    finally:
        os.close(src_fd)
        if trash_fd is not None:
            os.close(trash_fd)


def _open_parent(root_fd: int, rel_path: str | Path, *, create: bool) -> tuple[int, str]:
    parts = _split(rel_path)
    if not parts:
        raise UnsafePath(f"Not a file path: {rel_path!r}")
    name = parts[-1]
    dir_fd = open_dir_beneath(root_fd, "/".join(parts[:-1]), create=create)
    return dir_fd, name


def open_parent(root_fd: int, rel_path: str | Path, *, create: bool = False) -> tuple[int, str]:
    """Public form of `_open_parent`: (directory fd, final component).

    The caller owns the returned descriptor and must close it.
    """
    return _open_parent(root_fd, rel_path, create=create)


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


def probe_publication(root_fd: int) -> None:
    """Verify a no-clobber publish can work: hard links within the vault root.

    **This probe writes** (a temp file and a link, both removed again), so it
    belongs only on paths that are about to write. A read — a download, a
    `check_upload` — must never call it: a read-only capability that creates
    files, however briefly, is a write the caller did not ask for.

    Raises `UnsupportedFilesystem` when links are refused (`EPERM`/
    `EOPNOTSUPP`/`ENOSYS`) or would cross a device (`EXDEV`).
    """
    fd, tmp_name = create_temp(root_fd)
    os.close(fd)
    try:
        _probe_link(root_fd, tmp_name, root_fd, f"{tmp_name}-probe", "within the vault root")
    finally:
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
        staging_fd = open_dir_beneath(root_fd, STAGING_DIR, create=False)
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
        os.close(staging_fd)
    if removed:
        logger.info("Pruned %d stale staged upload(s) from %s", removed, STAGING_DIR)
    return removed


# Cached per (vault root, probe kind): a probe touches the disk, and every
# transfer tool needs the answer. `None` means "supported"; an exception
# instance means the caller must refuse with that message.
_probe_cache: dict[tuple[str, str], UnsupportedFilesystem | None] = {}


def _cached_probe(root: Path | str, kind: str, probe) -> None:
    key = (str(root), kind)
    if key not in _probe_cache:
        root_fd = open_root(root)
        try:
            probe(root_fd)
            _probe_cache[key] = None
        except UnsupportedFilesystem as exc:
            _probe_cache[key] = exc
        finally:
            os.close(root_fd)
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


def check_trash_support(root: Path | str) -> None:
    """Cached `probe_trash` for one vault root; raises when unsupported."""
    _cached_probe(root, "trash", probe_trash)


def reset_filesystem_probe_cache() -> None:
    """Forget cached probe results (tests, and vault-root reassignment)."""
    _probe_cache.clear()
