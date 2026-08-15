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

import errno
import hashlib
import logging
import os
import secrets
import stat
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

# How many `-<n>` suffixes to try when a `.trash/` entry already exists.
_TRASH_ATTEMPTS = 1000

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
    """The vault filesystem cannot support atomic no-clobber publication.

    Hard links (`EPERM`/`EOPNOTSUPP`) or a cross-device `.trash` (`EXDEV`) mean
    the no-clobber and soft-delete guarantees do not hold. The transfer tools
    refuse rather than silently degrading to an overwriting move.
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
                "no-clobber publish depends on (see probe_filesystem)"
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


def soft_delete(root_fd: int, rel_path: str | Path, trash_dir: str = TRASH_DIR) -> str:
    """Link a regular file into `trash_dir` under a timestamped name, then unlink it.

    Returns the trash-relative path that was created. Never clobbers an
    existing trash entry: the link is no-clobber and collisions get a `-<n>`
    suffix, so two files with the same basename deleted in the same second both
    survive. `.trash` lives inside the vault root, so it is same-device by
    construction and the link cannot fail `EXDEV` on a sane filesystem.

    **The unlink is inode-verified.** `link` + `unlink` is two syscalls, and a
    writer that replaces the source name in between would otherwise have its
    replacement unlinked with no trash copy of it — a silent destructive
    delete, which is the one thing this module exists to prevent. So after the
    link we reopen the source name `O_NOFOLLOW` and only unlink it if it is
    still the inode we just linked; if it is not, the *new* file is left alone,
    the trash link we made is removed, and the caller gets a `Conflict`.

    That check is optimistic, not linearizable: `renameat2(RENAME_NOREPLACE)`
    is what would close the window properly and Python exposes no binding for
    it. The window is now a few microseconds between an `fstat` and an
    `unlink` rather than the whole link-then-unlink pair, and — this is the
    part that matters — losing the race can no longer destroy data that was
    never copied.
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
        base = f"{stamp}-{name}"
        for attempt in range(_TRASH_ATTEMPTS):
            candidate = base if attempt == 0 else f"{base}-{attempt}"
            try:
                os.link(
                    name,
                    candidate,
                    src_dir_fd=src_fd,
                    dst_dir_fd=trash_fd,
                    follow_symlinks=False,
                )
            except FileExistsError:
                continue
            except OSError as exc:
                if exc.errno in (errno.EPERM, errno.EOPNOTSUPP, errno.EXDEV):
                    raise UnsupportedFilesystem(
                        "The vault filesystem does not support hard links into "
                        f"{trash_dir}/ (see probe_filesystem)"
                    ) from exc
                raise
            _unlink_if_same_inode(
                src_fd, name, trash_fd, candidate, rel_path=rel_path, trash_dir=trash_dir
            )
            return f"{trash_dir}/{candidate}"
        raise Conflict(
            f"Could not find a free name in {trash_dir}/ for {name}"
        )
    finally:
        os.close(src_fd)
        if trash_fd is not None:
            os.close(trash_fd)


def _unlink_if_same_inode(
    src_fd: int,
    name: str,
    trash_fd: int,
    candidate: str,
    *,
    rel_path,
    trash_dir: str,
) -> None:
    """Unlink `name` only if it is still the inode now sitting in the trash."""
    linked = os.stat(candidate, dir_fd=trash_fd, follow_symlinks=False)
    try:
        probe = os.open(name, os.O_RDONLY | _O_COMMON, dir_fd=src_fd)
    except FileNotFoundError:
        # Someone else removed the source after we copied it. The trash entry
        # is the copy we promised; there is nothing left to unlink.
        return
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            # The name became a symlink under us — never follow it, and never
            # unlink it blind.
            _unlink_quietly(trash_fd, candidate, published=False)
            raise Conflict(
                f"Source was replaced by a symlink while being trashed: {rel_path}"
            ) from None
        raise
    try:
        current = os.fstat(probe)
    finally:
        os.close(probe)

    if (current.st_dev, current.st_ino) != (linked.st_dev, linked.st_ino):
        # A different file lives at that name now. Unlinking it would destroy
        # content nothing has a copy of.
        _unlink_quietly(trash_fd, candidate, published=False)
        raise Conflict(
            f"Source was replaced while being moved to {trash_dir}/: {rel_path}"
        )
    os.unlink(name, dir_fd=src_fd)


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


def probe_filesystem(root_fd: int, trash_dir: str = TRASH_DIR) -> None:
    """Verify the vault filesystem supports the operations this module needs.

    Two links, not one: within the root (which is what `publish` needs) **and**
    from the root into `trash_dir` (which is what `soft_delete` needs). A vault
    whose `.trash` is a separate mount passes the first and fails the second
    with `EXDEV` — and would then silently lose every soft-deleted file, so it
    has to be caught here rather than at the first delete.

    Raises `UnsupportedFilesystem` when links are refused (`EPERM`/
    `EOPNOTSUPP`) or would cross a device (`EXDEV`). Cached per root by
    `check_filesystem_support`.
    """
    fd, tmp_name = create_temp(root_fd)
    os.close(fd)
    trash_fd: int | None = None
    try:
        _probe_link(root_fd, tmp_name, root_fd, f"{tmp_name}-probe", "within the vault root")
        trash_fd = open_dir_beneath(root_fd, trash_dir, create=True)
        _probe_link(root_fd, tmp_name, trash_fd, f"{tmp_name}-probe", f"into {trash_dir}/")
    finally:
        if trash_fd is not None:
            os.close(trash_fd)
        _unlink_quietly(root_fd, tmp_name, published=False)


# Cached per vault root: the probe touches the disk, and every transfer tool
# needs the answer. `None` means "supported"; an exception instance means the
# tools must refuse with that message.
_probe_cache: dict[str, UnsupportedFilesystem | None] = {}


def check_filesystem_support(root: Path | str) -> None:
    """Cached `probe_filesystem` for one vault root; raises when unsupported."""
    key = str(root)
    if key not in _probe_cache:
        root_fd = open_root(root)
        try:
            probe_filesystem(root_fd)
            _probe_cache[key] = None
        except UnsupportedFilesystem as exc:
            _probe_cache[key] = exc
        finally:
            os.close(root_fd)
    cached = _probe_cache[key]
    if cached is not None:
        raise cached


def reset_filesystem_probe_cache() -> None:
    """Forget cached probe results (tests, and vault-root reassignment)."""
    _probe_cache.clear()
