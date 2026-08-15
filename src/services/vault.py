import logging
import mimetypes
import os
import posixpath
import re
import shutil
import stat
import uuid
from pathlib import Path, PurePosixPath

import yaml
from sqlalchemy import select

from src.config import settings

logger = logging.getLogger(__name__)


# Process-level cache of `user_id -> Path(user.vault_path)` for multi-user mode.
# Populated via `warm_user_vault_cache(session, ...)` and invalidated by
# `clear_user_vault_cache(user_id=...)` when the admin edits a user. Single-user
# mode never touches this cache because `_vault_root()` is called with
# `user_id=None` everywhere.
_user_vault_cache: dict[int, Path] = {}


async def warm_user_vault_cache(session, user_id: int | None = None) -> None:
    """Populate `_user_vault_cache` for one user (or every active user).

    Called by the indexer at the start of each multi-user pass, by the API-key
    middleware after authenticating a user, and (in phase 4) by panel routes
    before they hit vault tools. In single-user mode the cache is unused so
    callers can skip the warmup; nothing breaks if they don't.
    """
    from src.models.db import User

    if user_id is not None:
        result = await session.execute(
            select(User.id, User.vault_path).where(
                User.id == user_id,
                User.is_active.is_(True),
                User.vault_path.isnot(None),
            )
        )
        row = result.first()
        if row is not None:
            _user_vault_cache[row.id] = Path(row.vault_path)
        return

    result = await session.execute(
        select(User.id, User.vault_path).where(
            User.is_active.is_(True),
            User.vault_path.isnot(None),
        )
    )
    for row in result.all():
        _user_vault_cache[row.id] = Path(row.vault_path)


def clear_user_vault_cache(user_id: int | None = None) -> None:
    """Drop one user (or every user) from the in-process vault-path cache.

    Phase 4's admin user-edit endpoint calls this whenever it mutates
    `users.vault_path` so the next vault op picks up the new value. With no
    argument, clears the whole cache (useful for tests).
    """
    if user_id is None:
        _user_vault_cache.clear()
    else:
        _user_vault_cache.pop(user_id, None)


def _vault_root(user_id: int | None = None) -> Path:
    """Return the vault root for the given user.

    Single-user mode / `user_id is None` → `settings.vault_path` (legacy
    behavior). Multi-user mode → cached `users.vault_path` lookup. The cache
    must have been warmed for this user (auth middleware / indexer / panel
    routes do this before invoking tools); a miss raises a clear RuntimeError
    rather than silently falling back to the global path or silently blocking
    the event loop on a sync DB call.
    """
    if user_id is None:
        return Path(settings.vault_path)
    cached = _user_vault_cache.get(user_id)
    if cached is None:
        raise RuntimeError(
            f"Vault path for user_id={user_id} is not in cache. "
            "Call `warm_user_vault_cache(session, user_id)` before using "
            "vault tools, or check that the user has `vault_path` set and "
            "`is_active=True`."
        )
    return cached


def validate_vault_root_path(p: str) -> tuple[str | None, str | None]:
    """Validate an absolute container path as an acceptable vault root.

    Returns ``(normalized_path, error)`` — at most one of the two is set.
    Empty / None input → ``(None, None)``: callers that allow clearing a
    vault_path assignment treat this as "no change".

    Accepted values:
    - ``settings.vault_path`` (the legacy single-user ``/obsidian`` mount)
    - Any non-empty subpath of ``/vaults/``

    The ``/vaults/`` restriction prevents an admin from accidentally (or
    deliberately) pointing a user at ``/etc``, the host home dir, or another
    container path that happens to be visible.  The directory-existence check
    catches a docker-compose mount that was configured but not yet applied.
    """
    raw = (p or "").strip()
    if not raw:
        return None, None
    if ".." in Path(raw).parts:
        return None, "Vault path may not contain '..' traversal."
    normalized = os.path.normpath(raw)
    legacy = settings.vault_path.rstrip("/")
    if normalized != legacy and not normalized.startswith("/vaults/"):
        return None, (
            f"Vault path must be either '{legacy}' (legacy mount) or a "
            "subpath of '/vaults/'."
        )
    if not Path(normalized).is_dir():
        return None, (
            f"Vault path '{normalized}' does not exist as a directory "
            "inside the container. Check the docker-compose volume mount."
        )
    return normalized, None


def validate_path(relative_path: str, user_id: int | None = None) -> Path:
    """Resolve a relative path within the vault, preventing traversal."""
    vault = _vault_root(user_id)
    resolved = (vault / relative_path).resolve()
    try:
        resolved.relative_to(vault.resolve())
    except ValueError:
        raise ValueError(f"Path traversal denied: {relative_path}")
    return resolved


def is_hidden_path(rel: "str | Path") -> bool:
    """True if any component of a vault-relative path starts with a dot.

    Mirrors the indexer's visibility rule
    (`any(part.startswith(".") for part in rel.parts)`) so the file-access
    tools keep `.obsidian`, `.git`, `.trash`, `.smart-env`, … out of reach.
    """
    return any(part.startswith(".") for part in Path(rel).parts)


def validate_visible_path(relative_path: str, user_id: int | None = None) -> Path:
    """`validate_path` plus the dot-dir visibility guard.

    Rejects path traversal (via `validate_path`) and any path that resolves
    into a dot-directory. Used by the raw file-access tools so they expose
    exactly the files the indexer would consider.
    """
    resolved = validate_path(relative_path, user_id=user_id)
    vault = _vault_root(user_id).resolve()
    rel = resolved.relative_to(vault)
    if is_hidden_path(rel):
        raise ValueError(f"Hidden path denied: {relative_path}")
    return resolved


def validate_mutable_path(relative_path: str, user_id: int | None = None) -> Path:
    """Validate a path a tool is about to **mutate**, refusing a symlinked leaf.

    `validate_path` returns `(vault / rel).resolve()`, which follows symlinks —
    so an in-vault alias `alias.md -> important.md` made every write tool act on
    `important.md` while reporting success for `alias.md`. That is a destructive
    write on a path nobody named. Reads may keep following links (an alias
    reading as its target is what a user expects); mutations may not.

    The rule (see `openspec/specs/vault-write`):

    - the *parent* directory is resolved and must stay inside the vault, so
      symlinked directories inside the vault (shared attachment folders, a
      common Obsidian setup) keep working while an escaping one is still
      rejected by the containment check;
    - the final component is taken **as named** and `os.lstat`-ed: if it is a
      symbolic link — including a dangling one — the operation is refused with
      an error naming the link's canonical vault-relative target;
    - the returned path is `resolved_parent / name`, i.e. the real directory
      entry the indexer sees. Callers that record vault-relative paths in the
      database (`move_note`) derive them from this path, so they match what the
      indexer stores for notes under a symlinked directory.

    Resolving the parent once, here, also means an allowed symlinked ancestor is
    never re-traversed during the mutation: repointing it afterwards cannot
    redirect the write — **provided the caller mutates the returned `Path` and
    never re-passes its own string**. That is why the `*_at` helpers below
    exist; see their docstrings. The residual TOCTOU — the leaf swapped for a
    link between this `lstat` and the write — is the same optimistic level as
    every other note write today, and closes when these paths migrate to the
    anchored `vault_fs` helpers.

    Raises `ValueError` for traversal, a hidden (dot-directory) path, a
    non-file-shaped path, an in-vault `..` segment, or a symlinked final
    component.
    """
    vault = _vault_root(user_id)
    vault_resolved = vault.resolve()

    raw = str(relative_path).replace(os.sep, "/")
    if "\x00" in raw:
        raise ValueError(f"Path traversal denied: {relative_path}")
    if raw.endswith("/"):
        raise ValueError(f"Not a file path: {relative_path!r}")
    rel = PurePosixPath(raw)
    if rel.is_absolute():
        raise ValueError(f"Path traversal denied: {relative_path}")
    parts = [part for part in rel.parts if part not in ("", ".")]
    if any(part == ".." for part in parts):
        # A `..` that stays inside the vault is not an attack — it is an agent
        # passing a path it built by hand. Refusing it is still right (a
        # mutating tool must not resolve components away; that is the whole
        # point of this validator), but "Path traversal denied" tells the
        # caller nothing it can act on. Name the normalised path instead.
        normalised = posixpath.normpath("/".join(parts))
        if normalised == ".":
            raise ValueError(f"Not a file path: {relative_path!r}")
        if normalised == ".." or normalised.startswith("../"):
            raise ValueError(f"Path traversal denied: {relative_path}")
        raise ValueError(
            f"{relative_path} contains a '..' segment. Mutating tools take the "
            "path as named and never resolve a component away — pass the "
            f"normalised path instead: {normalised}. (Reads still accept '..'.)"
        )
    if not parts:
        raise ValueError(f"Not a file path: {relative_path!r}")

    name = parts[-1]
    resolved_parent = vault.joinpath(*parts[:-1]).resolve()
    try:
        parent_rel = resolved_parent.relative_to(vault_resolved)
    except ValueError:
        raise ValueError(f"Path traversal denied: {relative_path}") from None

    target = resolved_parent / name
    if is_hidden_path(parent_rel / name):
        raise ValueError(f"Hidden path denied: {relative_path}")

    try:
        info = os.lstat(target)
    except (FileNotFoundError, NotADirectoryError):
        info = None
    if info is not None and stat.S_ISLNK(info.st_mode):
        raise ValueError(
            f"{relative_path} is a symbolic link to "
            f"{_link_target_label(target, vault_resolved)} — mutating tools act "
            "only on the named file; operate on the target instead."
        )
    return target


def _link_target_label(link: Path, vault_resolved: Path) -> str:
    """Name a symlink's ultimate target for an error message.

    Vault-relative POSIX when the target lands inside the vault (dangling links
    included — the caller still learns which note the alias was meant to name),
    otherwise the literal string `outside the vault`.
    """
    try:
        destination = Path(os.path.realpath(link))
        return destination.relative_to(vault_resolved).as_posix()
    except (ValueError, OSError):
        return "outside the vault"


def read_file(relative_path: str, user_id: int | None = None) -> dict:
    """Read a note, returning frontmatter + content."""
    path = validate_visible_path(relative_path, user_id=user_id)
    if not path.is_file():
        raise FileNotFoundError(f"Note not found: {relative_path}")
    raw = path.read_text(encoding="utf-8")
    frontmatter, content = parse_frontmatter(raw)
    title = frontmatter.get("title") or path.stem
    tags = extract_tags(raw, frontmatter)
    return {
        "path": relative_path,
        "title": title,
        "frontmatter": frontmatter,
        "tags": tags,
        "content": content,
        "size": path.stat().st_size,
        "modified": path.stat().st_mtime,
    }


def _atomic_write(
    path: Path,
    *,
    text: str | None = None,
    data: bytes | None = None,
    overwrite: bool = True,
    expected: bytes | None = None,
) -> Path:
    """Write `text` (UTF-8) or `data` (raw bytes) to `path` atomically.

    Shared core for `write_file` (notes) and `write_bytes` (raw files): writes
    a tmp file in the same directory then `os.replace`s it into place. A crash
    between the tmp-file write and the rename leaves the destination untouched.
    `os.replace` is atomic on POSIX same-FS renames. No-clobber publication
    uses a hard link from the same-directory temp file. Creates missing parent
    directories.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(
        f".tmp-{path.name}-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    )
    try:
        if data is not None:
            tmp.write_bytes(data)
        else:
            tmp.write_text(text or "", encoding="utf-8")
        try:
            if expected is not None:
                try:
                    current = _read_path_bytes(path)
                except FileNotFoundError:
                    raise RuntimeError(f"File changed while editing: {path.name}")
                if current != expected:
                    raise RuntimeError(f"File changed while editing: {path.name}")
            if overwrite:
                os.replace(tmp, path)
            else:
                # A hard link publishes the completed temp inode only when the
                # destination does not exist. Unlike exists()+replace(), this
                # is an atomic no-clobber operation on the same filesystem.
                os.link(tmp, path)
                tmp.unlink()
        except OSError as e:
            if getattr(e, "errno", None) == 18:  # EXDEV
                # Temp and destination deliberately share a directory, so an
                # EXDEV indicates an unusual/non-POSIX filesystem. Never turn
                # a promised no-clobber write into an overwriting move.
                if not overwrite:
                    raise
                logger.warning("Cross-FS rename for %s; using shutil.move", path.name)
                shutil.move(str(tmp), str(path))
            else:
                raise
    except Exception:
        try:
            if tmp.exists():
                tmp.unlink()
        except Exception:
            pass
        raise
    return path


def _read_path_bytes(path: Path, max_bytes: int | None = None) -> bytes:
    """Read and bound a regular file through one descriptor.

    The size check and read refer to the same opened inode. ``O_NOFOLLOW``
    prevents a final-component symlink swap on platforms that provide it.
    """
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError(f"Not a regular file: {path.name}")
        if max_bytes is not None and info.st_size > max_bytes:
            raise ValueError(
                f"File too large: {path.name} is {info.st_size:,} bytes "
                f"(max {max_bytes:,})"
            )
        with os.fdopen(fd, "rb", closefd=False) as stream:
            data = stream.read(None if max_bytes is None else max_bytes + 1)
        if max_bytes is not None and len(data) > max_bytes:
            raise ValueError(
                f"File too large: {path.name} exceeds {max_bytes:,} bytes"
            )
        return data
    finally:
        os.close(fd)


# ────────────────────────────────────────────────────────────────────────────
# Resolve once, then act on the Path — the `*_at` helpers
# ────────────────────────────────────────────────────────────────────────────
#
# `validate_mutable_path` resolves the parent exactly once and hands back the
# real directory entry. That guarantee only holds if the caller then *uses*
# that Path. A tool that validates a string, keeps the Path for a guard, and
# then calls `read_bytes(path_str)` / `write_file(path_str)` re-resolves the
# caller's string a second and a third time: an ancestor symlink repointed
# between the read and the write redirects the write to a note nobody named,
# and the `expected=` compare-and-swap does not catch it (the decoy can hold
# byte-identical content).
#
# So every read-modify-write inside one tool call goes through the helpers
# below, which take an ALREADY-VALIDATED absolute Path and never touch the
# vault root or the caller's string again. The string-taking `read_bytes` /
# `write_file` / `write_bytes` wrappers remain for single-shot callers and are
# now thin: validate, then delegate.


def write_file_at(
    path: Path,
    content: str,
    *,
    overwrite: bool = True,
    expected: bytes | None = None,
) -> Path:
    """Atomically write a note to an already-validated absolute `path`.

    `path` MUST come from `validate_mutable_path` (or be another path the
    caller has already proven safe) — no traversal, visibility or symlink
    check happens here.
    """
    return _atomic_write(
        path, text=content, overwrite=overwrite, expected=expected
    )


def write_bytes_at(
    path: Path,
    data: bytes,
    overwrite: bool = False,
) -> Path:
    """Atomically write raw bytes to an already-validated absolute `path`.

    Same contract as `write_file_at`. Raises `FileExistsError` when the target
    exists and `overwrite` is False.
    """
    return _atomic_write(path, data=data, overwrite=overwrite)


def read_bytes_at(
    path: Path,
    max_bytes: int | None = None,
    *,
    label: str | None = None,
) -> bytes:
    """Read an already-validated absolute `path` through one `O_NOFOLLOW` fd.

    The counterpart to `write_file_at`: the read and the subsequent write refer
    to the same resolved location, so nothing between them can redirect either.
    `label` (a vault-relative path) replaces the bare filename in size errors so
    the caller sees the path it passed. Raises `FileNotFoundError` when the file
    is gone, `OSError` (ELOOP) when the leaf has been swapped for a symlink, and
    `ValueError` for a non-regular file or an over-cap size.
    """
    try:
        return _read_path_bytes(path, max_bytes=max_bytes)
    except ValueError as exc:
        if label is None:
            raise
        raise ValueError(str(exc).replace(path.name, label, 1)) from exc


def write_file(
    relative_path: str,
    content: str,
    user_id: int | None = None,
    *,
    overwrite: bool = True,
    expected: bytes | None = None,
) -> Path:
    """Write content to a note atomically (tmp file in same dir + os.replace).

    Validation goes through `validate_mutable_path`, so a symlinked final
    component is refused rather than silently retargeted, and `_atomic_write`
    receives `resolved_parent / name` — a real directory for its temp file.

    Single-shot convenience only. A tool that already validated the path (or
    that reads before it writes) must use `write_file_at` instead: re-passing
    the string here resolves it again.
    """
    path = validate_mutable_path(relative_path, user_id=user_id)
    return write_file_at(
        path, content, overwrite=overwrite, expected=expected
    )


def read_bytes(
    relative_path: str,
    user_id: int | None = None,
    max_bytes: int | None = None,
) -> bytes:
    """Read raw bytes of an arbitrary vault file (dot-dirs rejected).

    Validates path + visibility, then reads through a single `O_NOFOLLOW`
    descriptor whose `fstat` bounds the size against `max_bytes` (when given),
    so an over-cap file is refused without loading it into memory. Raises
    `FileNotFoundError` for a missing file and `ValueError` for traversal, a
    hidden path, or an over-cap size (the message reports the actual size and
    path).

    Reads follow symlinks by design (`validate_visible_path` resolves), so this
    is *not* the helper for the read half of a read-modify-write — use
    `read_bytes_at` on the validated mutable path for that.
    """
    path = validate_visible_path(relative_path, user_id=user_id)
    if not path.is_file():
        raise FileNotFoundError(f"File not found: {relative_path}")
    return read_bytes_at(path, max_bytes=max_bytes, label=relative_path)


def write_bytes(
    relative_path: str,
    data: bytes,
    overwrite: bool = False,
    user_id: int | None = None,
) -> Path:
    """Write raw bytes to an arbitrary vault file atomically (dot-dirs rejected).

    Validates path + visibility, enforces no-clobber unless `overwrite`,
    creates any missing parent directories, and routes through the shared
    atomic temp-file + `os.replace` write. Raises `FileExistsError` when the
    target exists and `overwrite` is False, leaving the existing file
    untouched, and `ValueError` when the final component is a symlink
    (`validate_mutable_path`) — writing through an alias would clobber the
    target under a path the caller never named.
    """
    path = validate_mutable_path(relative_path, user_id=user_id)
    try:
        return write_bytes_at(path, data, overwrite=overwrite)
    except FileExistsError:
        raise FileExistsError(f"File already exists: {relative_path}") from None


def move_no_clobber(source: Path, destination: Path) -> None:
    """Move one regular file without ever replacing ``destination``.

    On the normal same-filesystem vault layout, link+unlink is an atomic
    no-clobber publication. The EXDEV fallback uses exclusive creation and
    copies the bytes before unlinking the source; it preserves no-clobber but
    cannot make the cross-filesystem move itself atomic.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(source, destination, follow_symlinks=False)
        source.unlink()
        return
    except OSError as exc:
        if getattr(exc, "errno", None) != 18:  # EXDEV
            raise

    src_fd = os.open(
        source,
        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    dst_fd: int | None = None
    try:
        dst_fd = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
            0o600,
        )
        with os.fdopen(src_fd, "rb", closefd=False) as src, os.fdopen(
            dst_fd, "wb", closefd=False
        ) as dst:
            shutil.copyfileobj(src, dst)
            dst.flush()
            os.fsync(dst_fd)
        source.unlink()
    except Exception:
        if dst_fd is not None:
            try:
                destination.unlink()
            except OSError:
                pass
        raise
    finally:
        os.close(src_fd)
        if dst_fd is not None:
            os.close(dst_fd)


# ────────────────────────────────────────────────────────────────────────────
# File-access helpers: MIME classification + directory listing
# ────────────────────────────────────────────────────────────────────────────


def _sniff_image_mime(head: bytes) -> str | None:
    """Return an image MIME type if `head` carries a known image signature.

    Covers the common web image formats (PNG, JPEG, GIF, WebP). Used to
    confirm — and to catch mislabeled — images independent of the file
    extension. Returns None when no signature matches.
    """
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    return None


def _is_text_like_mime(mime: str | None) -> bool:
    """Whether a MIME type is safe to return as decoded text.

    `text/*` plus the common structured-text application types
    (JSON, JavaScript, XML, and any `*+xml`/`*+json` suffix).
    """
    if not mime:
        return False
    if mime.startswith("text/"):
        return True
    if mime in {
        "application/json",
        "application/javascript",
        "application/xml",
        "application/x-yaml",
        "application/yaml",
    }:
        return True
    return mime.endswith("+xml") or mime.endswith("+json")


def classify_bytes(data: bytes, name: str) -> tuple[str, str]:
    """Classify a file as ``"text"`` / ``"image"`` / ``"other"`` plus its MIME.

    Detection uses a magic-byte sniff (authoritative for images, so a
    mislabeled image still renders and a non-image with an image extension
    does not) and falls back to stdlib `mimetypes` for the text/binary split.
    """
    img_mime = _sniff_image_mime(data[:16])
    if img_mime is not None:
        return "image", img_mime
    mime, _ = mimetypes.guess_type(name)
    if _is_text_like_mime(mime):
        return "text", mime  # type: ignore[return-value]
    return "other", mime or "application/octet-stream"


def list_dir(
    folder: str = ".",
    pattern: str = "*",
    recursive: bool = False,
    limit: int = 200,
    user_id: int | None = None,
) -> tuple[list[dict], bool]:
    """Browse the vault filesystem, excluding dot-directories.

    Returns ``(entries, truncated)``. Each entry is a dict with
    ``path`` (vault-relative POSIX), ``is_dir``, ``size`` (bytes), and
    ``mtime`` (epoch float). Dot-directories — and a dot-directory `folder` —
    are rejected/omitted via `validate_visible_path`.

    Non-recursive (default): immediate children only — subdirectories (always)
    and files whose name matches `pattern`. Recursive: files matching `pattern`
    beneath `folder`, pruning dot-directories. At most `limit` entries are
    returned; `truncated` is True when more matched.
    """
    import fnmatch

    base = validate_visible_path(folder or ".", user_id=user_id)
    if not base.is_dir():
        raise NotADirectoryError(f"Not a directory: {folder}")
    vault = _vault_root(user_id).resolve()

    def _entry(p: Path, is_dir: bool) -> dict:
        st = p.stat()
        return {
            "path": p.resolve().relative_to(vault).as_posix(),
            "is_dir": is_dir,
            "size": st.st_size,
            "mtime": st.st_mtime,
        }

    entries: list[dict] = []
    truncated = False

    if recursive:
        for root, dirnames, filenames in os.walk(base):
            # Prune dot-directories in place so os.walk never descends them.
            dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
            for fname in sorted(filenames):
                if fname.startswith(".") or not fnmatch.fnmatch(fname, pattern):
                    continue
                if len(entries) >= limit:
                    truncated = True
                    return entries, truncated
                entries.append(_entry(Path(root) / fname, False))
    else:
        children = sorted(base.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        for child in children:
            if child.name.startswith("."):
                continue
            is_dir = child.is_dir()
            if not is_dir and not fnmatch.fnmatch(child.name, pattern):
                continue
            if len(entries) >= limit:
                truncated = True
                break
            entries.append(_entry(child, is_dir))

    return entries, truncated


def parse_frontmatter(raw: str) -> tuple[dict, str]:
    """Split YAML frontmatter from content.

    The fence (`---`) MUST be on line 1 (Obsidian's rule). Anything else is
    treated as no frontmatter, even if a `---` fence appears further down.

    Returns `(metadata, body)`. `body` preserves leading whitespace exactly
    as it appears after the closing `---\n`; only a single newline separator
    is consumed.
    """
    if not raw.startswith("---"):
        return {}, raw
    # Require the opening fence to occupy line 1 alone (allow trailing CR).
    first_line_end = raw.find("\n")
    if first_line_end == -1:
        return {}, raw
    first_line = raw[:first_line_end].rstrip("\r")
    if first_line != "---":
        return {}, raw

    # Find the closing fence on its own line.
    rest = raw[first_line_end + 1:]
    closing_re = re.compile(r"(?m)^---[ \t]*\r?$")
    m = closing_re.search(rest)
    if m is None:
        return {}, raw
    yaml_text = rest[:m.start()]
    body_start = m.end()
    # Skip the single newline after the closing fence, if present.
    if body_start < len(rest) and rest[body_start] == "\n":
        body_start += 1
    body = rest[body_start:]
    try:
        fm = yaml.safe_load(yaml_text)
    except yaml.YAMLError:
        return {}, raw
    if not isinstance(fm, dict):
        return {}, raw
    return fm, body


def serialize_frontmatter(meta: dict, body: str) -> str:
    """Re-assemble a note from a frontmatter dict and a body string.

    Empty / missing `meta` → returns `body` unchanged (no fence is emitted).
    Otherwise emits `---\\n<yaml>---\\n<body>`. PyYAML `safe_dump` does NOT
    preserve YAML comments — callers should document this caveat.
    """
    if not meta:
        return body
    yaml_text = yaml.safe_dump(
        meta,
        default_flow_style=False,
        sort_keys=False,
        allow_unicode=True,
    )
    return f"---\n{yaml_text}---\n{body}"


_ATX_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)


def _scan_headings(text: str) -> list[dict]:
    """Find ATX headings (1–6 `#`) outside code, with byte positions.

    Returns dicts with `depth`, `text` (trimmed), `line_start` (byte pos at
    the leading `#`) and `line_end` (byte pos at end of heading text, before
    any trailing newline). Code blocks are masked first so headings inside
    fenced or inline code are ignored.
    """
    from src.services.links import mask_code

    masked = mask_code(text)
    out: list[dict] = []
    for m in _ATX_HEADING_RE.finditer(masked):
        out.append({
            "depth": len(m.group(1)),
            "text": m.group(2).strip(),
            "line_start": m.start(),
            "line_end": m.end(),
        })
    return out


def _format_heading_list(headings: list[dict]) -> str:
    if not headings:
        return "(no headings)"
    return "; ".join(f"{'#' * h['depth']} {h['text']}" for h in headings)


def _format_ordinal_choices(candidates: list[int]) -> str:
    """Render candidate indices as the `#N` selectors a caller can pass back."""
    return ", ".join(f"#{i + 1}" for i in candidates)


def _path_chain_match(target_idx: int, headings: list[dict], ancestors: list[str]) -> bool:
    """Check if the heading at `target_idx` has `ancestors` (outermost-first)
    as a leading sequence of its enclosing-heading chain (innermost-first).
    """
    if not ancestors:
        return True
    target = headings[target_idx]
    cur_depth = target["depth"]
    chain: list[str] = []  # innermost-first
    for j in range(target_idx - 1, -1, -1):
        if headings[j]["depth"] < cur_depth:
            chain.append(headings[j]["text"])
            cur_depth = headings[j]["depth"]
    expected = list(reversed(ancestors))
    if len(expected) > len(chain):
        return False
    return chain[: len(expected)] == expected


def _resolve_section_index(
    headings: list[dict], heading: str
) -> tuple[int | None, str | None]:
    """Resolve a heading selector to an index into `headings`.

    Returns `(idx, error)`. `heading` may be plain heading text (`Tasks`), a
    path-style chain (`Parent/Child`) where the last part is the target and the
    preceding parts are ancestors in outermost-first order, or a `#N` ordinal
    (`#7`) selecting the Nth heading in document order, 1-based.

    The ordinal form is the only way to address duplicate *sibling* headings,
    which the path-style form cannot disambiguate because they share ancestors.
    Auto-generated notes hit this routinely (e.g. the same source filename
    extracted twice under one parent).

    **A bare `#N` selector always means the ordinal**, even if some heading's
    literal text is also `#N`. The outline emitted with a truncated read
    advertises these ordinals as the reliable way to reach a section, so they
    cannot be shadowed by note content — otherwise a section could become
    unaddressable by the very selector we told the caller to use. A heading
    genuinely titled `#2` stays reachable two ways: by its own ordinal, or by
    the path-style form (`Parent/#2`), which never takes the ordinal branch.
    """
    if not headings:
        return None, (
            f"Section heading '{heading}' not found: note has no ATX headings."
        )

    ordinal = heading.strip()
    if "/" not in ordinal and ordinal.startswith("#") and ordinal[1:].isdigit():
        n = int(ordinal[1:])
        if not 1 <= n <= len(headings):
            return None, (
                f"Section ordinal '{heading}' is out of range: this note has "
                f"{len(headings)} headings (valid #1–#{len(headings)})."
            )
        return n - 1, None

    if "/" in heading:
        parts = [p.strip() for p in heading.split("/")]
        ancestors = parts[:-1]
        target_text = parts[-1]
        candidates = [
            i for i, h in enumerate(headings)
            if h["text"] == target_text and _path_chain_match(i, headings, ancestors)
        ]
        if not candidates:
            return None, (
                f"Section heading '{heading}' not found. "
                f"Headings present: {_format_heading_list(headings)}."
            )
        if len(candidates) > 1:
            return None, (
                f"Section heading '{heading}' is still ambiguous "
                f"({len(candidates)} matches). Add more ancestors to the path, "
                f"or select one by ordinal: {_format_ordinal_choices(candidates)}."
            )
        return candidates[0], None

    candidates = [i for i, h in enumerate(headings) if h["text"] == heading]
    if not candidates:
        return None, (
            f"Section heading '{heading}' not found. "
            f"Headings present: {_format_heading_list(headings)}."
        )
    if len(candidates) > 1:
        return None, (
            f"Section heading '{heading}' matches {len(candidates)} headings. "
            "Use the path-style form 'Parent/Child' to disambiguate, or select "
            f"one by ordinal: {_format_ordinal_choices(candidates)}."
        )
    return candidates[0], None


def _section_body_span(text: str, headings: list[dict], idx: int) -> tuple[int, int]:
    """Return `(body_start, body_end)` for the section at `idx`.

    The body runs from the line after the matched heading up to (but not
    including) the next heading at depth less than or equal to the matched
    depth, or end of text.
    """
    matched = headings[idx]
    matched_depth = matched["depth"]

    body_end = len(text)
    for j in range(idx + 1, len(headings)):
        if headings[j]["depth"] <= matched_depth:
            body_end = headings[j]["line_start"]
            break

    body_start = matched["line_end"]
    if body_start < len(text) and text[body_start] == "\n":
        body_start += 1
    return body_start, body_end


def outline_sections(text: str) -> list[dict]:
    """Summarise a note's ATX headings without returning their bodies.

    Each entry carries `depth`, `text`, `size` (characters spanned by the
    heading line plus its body, i.e. what `extract_section` would return), and
    `ordinal` (1-based document order, usable as an `#N` section selector).
    Used to let a caller navigate a note too large to return whole.
    """
    headings = _scan_headings(text)
    out: list[dict] = []
    for i, h in enumerate(headings):
        _, body_end = _section_body_span(text, headings, i)
        out.append({
            "depth": h["depth"],
            "text": h["text"],
            "size": body_end - h["line_start"],
            "ordinal": i + 1,
        })
    return out


def extract_section(text: str, heading: str) -> tuple[str | None, str | None]:
    """Return the heading line plus its body for a named ATX heading.

    Returns `(section_text, error)`; exactly one is non-None. Selector syntax
    matches `replace_section`.
    """
    headings = _scan_headings(text)
    idx, err = _resolve_section_index(headings, heading)
    if err is not None:
        return None, err
    _, body_end = _section_body_span(text, headings, idx)
    return text[headings[idx]["line_start"]:body_end], None


def replace_section(text: str, heading: str, new_body: str) -> tuple[str | None, str | None]:
    """Replace the body under a named ATX heading.

    Returns `(new_text, error)`. On success: `error is None`. On failure:
    `new_text is None` and `error` is an actionable message.

    `heading` may be a plain heading text (e.g. `Tasks`) or a path-style
    chain (`Parent/Child`, `Outer/Inner/Leaf`, …) where the last part is
    the target heading and the preceding parts are ancestors in
    outermost-first order. The replacement runs from the line after the
    matched heading up to (but not including) the next heading at depth
    less than or equal to the matched depth, or end of file.
    """
    headings = _scan_headings(text)
    idx, err = _resolve_section_index(headings, heading)
    if err is not None:
        return None, err

    body_start, body_end = _section_body_span(text, headings, idx)
    # `body_end` lands on the next same-or-shallower heading, or end of text.
    next_heading_line_start = body_end if body_end < len(text) else None

    inserted = new_body
    # If the retained prefix lacks a trailing newline (an end-of-file heading
    # with no trailing newline, where the heading match consumed to EOF),
    # the new body would glue directly onto the heading text. Prepend one
    # newline to separate them. No-op when the prefix already ends in "\n".
    prefix = text[:body_start]
    if prefix and not prefix.endswith("\n"):
        inserted = "\n" + inserted
    if next_heading_line_start is not None and not inserted.endswith("\n"):
        inserted = inserted + "\n"

    new_text = text[:body_start] + inserted + text[body_end:]
    return new_text, None


def extract_tags(raw: str, frontmatter: dict) -> list[str]:
    """Extract tags from frontmatter and inline #tags."""
    tags = set()
    # Frontmatter tags
    fm_tags = frontmatter.get("tags", [])
    if isinstance(fm_tags, list):
        tags.update(str(t) for t in fm_tags)
    elif isinstance(fm_tags, str):
        tags.update(t.strip() for t in fm_tags.split(","))
    # Inline #tags (not inside code blocks)
    from src.services.links import mask_code

    masked = mask_code(raw)
    for match in re.finditer(r"(?:^|\s)#([a-zA-Z][a-zA-Z0-9_/-]*)", masked):
        tags.add(match.group(1))
    return sorted(tags)
