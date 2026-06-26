import logging
import mimetypes
import os
import re
import shutil
import uuid
from pathlib import Path

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


def read_file(relative_path: str, user_id: int | None = None) -> dict:
    """Read a note, returning frontmatter + content."""
    path = validate_path(relative_path, user_id=user_id)
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


def _atomic_write(path: Path, *, text: str | None = None, data: bytes | None = None) -> Path:
    """Write `text` (UTF-8) or `data` (raw bytes) to `path` atomically.

    Shared core for `write_file` (notes) and `write_bytes` (raw files): writes
    a tmp file in the same directory then `os.replace`s it into place. A crash
    between the tmp-file write and the rename leaves the destination untouched.
    `os.replace` is atomic on POSIX same-FS renames; if the vault ever spans
    filesystems (EXDEV), fall back to a non-atomic `shutil.move` and log a
    warning. Creates missing parent directories.
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
            os.replace(tmp, path)
        except OSError as e:
            if getattr(e, "errno", None) == 18:  # EXDEV
                logger.warning(
                    "Cross-FS rename for %s; falling back to shutil.move (non-atomic)",
                    path.name,
                )
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


def write_file(relative_path: str, content: str, user_id: int | None = None) -> Path:
    """Write content to a note atomically (tmp file in same dir + os.replace)."""
    path = validate_path(relative_path, user_id=user_id)
    return _atomic_write(path, text=content)


def read_bytes(
    relative_path: str,
    user_id: int | None = None,
    max_bytes: int | None = None,
) -> bytes:
    """Read raw bytes of an arbitrary vault file (dot-dirs rejected).

    Validates path + visibility, then stat-checks the on-disk size against
    `max_bytes` (when given) before reading so an over-cap file is refused
    without loading it into memory. Raises `FileNotFoundError` for a missing
    file and `ValueError` for traversal, a hidden path, or an over-cap size
    (the message reports the actual size and path).
    """
    path = validate_visible_path(relative_path, user_id=user_id)
    if not path.is_file():
        raise FileNotFoundError(f"File not found: {relative_path}")
    size = path.stat().st_size
    if max_bytes is not None and size > max_bytes:
        raise ValueError(
            f"File too large: {relative_path} is {size:,} bytes "
            f"(max {max_bytes:,})"
        )
    return path.read_bytes()


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
    untouched.
    """
    path = validate_visible_path(relative_path, user_id=user_id)
    if path.exists() and not overwrite:
        raise FileExistsError(f"File already exists: {relative_path}")
    return _atomic_write(path, data=data)


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
    if not headings:
        return None, (
            f"Section heading '{heading}' not found: note has no ATX headings."
        )

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
                f"({len(candidates)} matches). Add more ancestors to the path."
            )
        idx = candidates[0]
    else:
        candidates = [i for i, h in enumerate(headings) if h["text"] == heading]
        if not candidates:
            return None, (
                f"Section heading '{heading}' not found. "
                f"Headings present: {_format_heading_list(headings)}."
            )
        if len(candidates) > 1:
            return None, (
                f"Section heading '{heading}' matches {len(candidates)} headings. "
                "Use the path-style form 'Parent/Child' to disambiguate."
            )
        idx = candidates[0]

    matched = headings[idx]
    matched_depth = matched["depth"]

    next_heading_line_start: int | None = None
    for j in range(idx + 1, len(headings)):
        if headings[j]["depth"] <= matched_depth:
            next_heading_line_start = headings[j]["line_start"]
            break

    body_start = matched["line_end"]
    if body_start < len(text) and text[body_start] == "\n":
        body_start += 1
    if next_heading_line_start is not None:
        body_end = next_heading_line_start
    else:
        body_end = len(text)

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
