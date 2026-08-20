import base64
import binascii
import inspect
import logging
import mimetypes
import os
import posixpath
import re
import time
from collections import Counter
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from urllib.parse import urlsplit

from mcp.server.fastmcp import Image
from sqlalchemy import text

from src.auth.session import current_user_id
from src.config import MAX_MOVE_REWRITE_BYTES, MAX_NOTE_BYTES, settings
from src.database import async_session
from src.mcp_server.auth import current_api_key_id, current_oauth_token_id, current_permission
from src.models.db import UsageLog
from src.services import timing
from src.services.embeddings import semantic_search
from src.services.filters import apply_note_filters
from src.services.search import full_text_search
from src.services import transfer, vault_fs
from src.services.vault import (
    _vault_root,
    classify_bytes,
    extract_section,
    is_hidden_path,
    list_dir,
    outline_sections,
    read_bytes,
    read_bytes_at,
    read_file,
    validate_mutable_path,
    validate_visible_path,
    write_bytes_at,
    write_file_at,
)

logger = logging.getLogger(__name__)

_VAULT_GUIDE_PRIMER = (Path(__file__).parent / "vault_guide_primer.md").read_text(
    encoding="utf-8"
)

_NO_CLAUDE_MD_MESSAGE = (
    "# Vault-Specific Conventions\n"
    "\n"
    "No `CLAUDE.md` found at the vault root. To teach the agent about your\n"
    "folder structure, file-naming conventions, tag taxonomy, required\n"
    "frontmatter fields, or task-management syntax, create a `CLAUDE.md`\n"
    "file at the root of your vault. The agent will pick it up automatically\n"
    "on the next call.\n"
    "\n"
    "Suggested sections:\n"
    "\n"
    "- **Folder structure** — what lives where, and where new notes belong.\n"
    "- **Naming conventions** — how filenames are formatted.\n"
    "- **Frontmatter** — required and conventional YAML fields.\n"
    "- **Tag taxonomy** — top-level tags and their meaning.\n"
    "- **Task syntax** — any GTD/Dataview/checklist conventions in use.\n"
)


async def _log_usage(tool: str, params: dict, duration_ms: int, response_size: int):
    try:
        async with async_session() as session:
            session.add(UsageLog(
                key_id=current_api_key_id.get(),
                oauth_token_id=current_oauth_token_id.get(),
                user_id=current_user_id.get(),
                tool=tool,
                params=params,
                duration_ms=duration_ms,
                response_size=response_size,
            ))
            await session.commit()
    except Exception as e:
        logger.warning(f"Failed to log usage for {tool}: {e}")


_MAX_PARAM_LEN = 200  # truncate long string params (e.g. note content)
_MAX_QUERY_RESULTS = 500
_MAX_SEMANTIC_RESULTS = 50


def _clamp_limit(limit: int, maximum: int = _MAX_QUERY_RESULTS) -> int:
    """Keep authenticated callers from creating unbounded DB/response work."""
    return max(1, min(limit, maximum))


def _truncate_params(params: dict) -> dict:
    return {
        k: (v[:_MAX_PARAM_LEN] + "…" if isinstance(v, str) and len(v) > _MAX_PARAM_LEN else v)
        for k, v in params.items()
    }


_NO_VAULT_MESSAGE = (
    "Error: no vault is assigned to this account, so no vault tool can run. "
    "Ask an administrator to assign a vault path to your user in the control "
    "panel."
)

# Marker written into `usage_logs.params` for a call refused by the gate. It
# carries no new information — the user id and tool name are already columns.
_NO_VAULT_MARKER = "no_vault_assigned"


def _vault_admission_error() -> str | None:
    """Refuse the call when the caller has no resolvable vault root.

    Unassigning `users.vault_path` used to revoke only the *disk*-touching
    tools: `semantic_search`, `keyword_search`, `list_notes`, `get_recent` and
    every graph tool are served entirely from `notes_metadata` /
    `note_embeddings`, never call `_vault_root`, and those rows are never
    deleted (the indexer skips users with a NULL `vault_path`, so nothing ever
    prunes them). The result was an indefinite, fully queryable mirror of the
    content the user last held — file paths, titles, tags, frontmatter and
    chunk excerpts — reachable with an unchanged API key, while the panel told
    the operator "vault tools error" (issue #66).

    So the gate lives here, in the decorator every tool shares, rather than in
    the individual tools: resolve the root **once, before the body runs**, and
    fail the whole call if it cannot be resolved. Every `_tracked` tool reads
    or writes vault content or vault metadata, so there is nothing to exempt —
    `get_vault_guide` reads the vault's `CLAUDE.md`, `check_upload` reports a
    vault path. Fail closed and keep the list at zero.

    Single-user mode is untouched: `current_user_id` is None there (the
    sentinel's `id` is None and sandbox mode never sets it), and `_vault_root`
    answers from `settings.vault_path` without consulting the cache.

    Preserving the index rows is deliberate — a reassignment of the same path
    should not have to re-embed the vault from scratch.
    """
    uid = current_user_id.get()
    try:
        _vault_root(uid)
    except RuntimeError:
        # Unassigned, deactivated, or a cold cache. All three mean the same
        # thing to the caller and all three must refuse: a cold cache is not
        # permission to serve stale rows.
        logger.warning(
            "tool_refused_no_vault", extra={"user_id": uid}
        )
        return _NO_VAULT_MESSAGE
    return None


def _tracked(tool_name: str, param_keys: list[str], transforms: dict | None = None):
    """Decorator that times the call and logs it to usage_logs.

    `transforms` maps a parameter name to a function applied before the value
    is logged. It exists for `import_from_url`, whose `url` must be reduced to
    its host: the whole point of the allow-list is that a capability or a URL
    carrying one never reaches `usage_logs`, and "just don't log the URL" would
    lose the one field that makes an import auditable.
    """
    transforms = transforms or {}

    def decorator(fn):
        sig = inspect.signature(fn)

        @wraps(fn)
        async def wrapper(*args, **kwargs):
            start = time.monotonic()
            # The decorator owns the per-phase timing holder: a fresh dict per
            # call, reset in `finally`. That is what makes cross-call
            # attribution impossible — an early return or an exception leaves
            # measured phases at their measured value, and the *next* call in
            # the same task starts from empty rather than inheriting them.
            token = timing.begin()
            try:
                # Admission gate: a caller with no resolvable vault root never
                # reaches the tool body, including the DB-only ones. The
                # refusal is still logged, like any other tool error.
                refusal = _vault_admission_error()
                if refusal is not None:
                    result = refusal
                    extra = {"error": _NO_VAULT_MARKER}
                else:
                    result = await fn(*args, **kwargs)
                    extra = {}
                duration_ms = int((time.monotonic() - start) * 1000)
                params = {}
                # Resolve logged params by NAME via the wrapped signature so
                # that a non-logged positional arg between logged ones can't
                # shift the mapping (positional zipping silently mislabelled
                # params before). `transforms` reduces a value before it is
                # logged (e.g. import URL -> host) so no capability leaks.
                try:
                    bound = sig.bind(*args, **kwargs)
                    bound.apply_defaults()
                    params = {
                        key: transforms.get(key, lambda v: v)(bound.arguments[key])
                        for key in param_keys
                        if key in bound.arguments
                    }
                except TypeError:
                    params = {}
                logged = _truncate_params(params)
                logged.update(extra)
                # Whatever the service measured. Absent for tools that measure
                # nothing, so `params` keeps its current shape for them.
                logged.update(timing.current() or {})
                await _log_usage(tool_name, logged, duration_ms, len(str(result)))
                return result
            finally:
                timing.clear(token)

        # Structural marker. `tests/test_issue_66_*` asserts that every tool
        # registered on the MCP server delegates to something carrying it, so
        # "the admission gate is inherited by construction" is checked rather
        # than asserted.
        wrapper.__tracked_tool__ = tool_name
        return wrapper
    return decorator


# The tool this impl backs is registered as `keyword_search` (server.py takes
# the function name), so that is what `usage_logs.tool` must record — the old
# "search_notes" named a tool no client was ever offered, which made the audit
# trail unsearchable in both directions (#78).
@_tracked("keyword_search", ["query", "folder", "limit", "tags", "frontmatter"])
async def search_notes_impl(
    query: str,
    folder: str | None = None,
    limit: int = 20,
    tags: list[str] | None = None,
    frontmatter: dict | None = None,
) -> str:
    """Full-text keyword search across vault notes."""
    limit = _clamp_limit(limit)
    uid = current_user_id.get()
    async with async_session() as session:
        results = await full_text_search(
            session,
            query,
            folder=folder,
            limit=limit,
            tags=tags,
            frontmatter=frontmatter,
            user_id=uid,
        )
    if not results:
        return f"No results for '{query}'"
    lines = [f"Found {len(results)} results for '{query}':\n"]
    for r in results:
        tags_str = f" [{', '.join(r['tags'])}]" if r.get("tags") else ""
        lines.append(f"- **{r['title']}** (`{r['path']}`){tags_str} — rank: {r['rank']:.3f}")
    return "\n".join(lines)


def _window(body: str, offset: int, limit: int) -> tuple[str, int | None]:
    """Slice `body` to a window. Returns `(chunk, next_offset)`.

    `next_offset` is None when the window reached the end of `body`.
    """
    start = max(0, offset)
    chunk = body[start:start + limit]
    end = start + len(chunk)
    return chunk, (end if end < len(body) else None)


_MAX_OUTLINE_TITLE = 80


def _outline_text(content: str, cap: int) -> str | None:
    """Render a navigable heading outline, or None if there are no headings.

    The returned string NEVER exceeds `cap` characters. The outline is appended
    to a response that exists *because* the content was too large, so it is the
    one place where adding context can recreate the problem being solved: a note
    with thousands of headings otherwise produces an outline many times the size
    of the content window it accompanies.

    The budget is enforced in layers, because each has a hole that only shows
    up at an extreme:
      1. Long titles are elided at `_MAX_OUTLINE_TITLE`.
      2. If the complete listing fits, it is emitted as-is. No summary is
         needed when nothing is omitted, so no room is reserved for one —
         reserving unconditionally drops entries that had room and returns a
         near-empty outline for a small note.
      3. Otherwise entries are added only while they fit, with room reserved
         for the omitted-sections summary, which is itself text and must be
         paid for before it is spent.
      4. A final hard truncation, so the guarantee holds unconditionally even
         for a degenerate `cap` (a caller may pass `limit=1`) where not even
         one entry or the bare summary can fit. In that case the outline
         degrades to a marker; there is no `cap`-respecting alternative.
    """
    sections = outline_sections(content)
    if not sections:
        return None
    seen = Counter(s["text"] for s in sections)

    def _summary(omitted: int) -> str:
        return (
            f"- … {omitted:,} more section(s) not shown (outline truncated "
            f"to stay within the response cap). Ordinals run #1–"
            f"#{len(sections)}; request one directly, or narrow with "
            f"`search_notes`."
        )

    def _entry(s: dict) -> str:
        marker = "#" * s["depth"]
        title = s["text"]
        if len(title) > _MAX_OUTLINE_TITLE:
            title = title[:_MAX_OUTLINE_TITLE - 1] + "…"
        flag = "" if s["size"] <= cap else "  ⚠ over the cap, will page"
        # A repeated heading can only be addressed by its ordinal — the
        # path-style form cannot separate duplicate siblings.
        dup = "  ← duplicate title, use the ordinal" if seen[s["text"]] > 1 else ""
        return f"- `#{s['ordinal']}` `{marker} {title}` ({s['size']:,} chars){flag}{dup}"

    entries = [_entry(s) for s in sections]

    # Fast path: if the complete listing fits, emit it. The summary is only
    # needed when something is actually omitted, so charging its reservation
    # here would drop entries that had room — a short outline is a worse
    # answer than a complete one, and the cap is not under threat.
    full = "\n".join(entries)
    if len(full) <= cap:
        return full

    # Truncating: now the summary is real text that must be paid for before
    # it is spent, or appending it pushes the result back over `cap`.
    reserve = len(_summary(len(sections))) + 1
    lines: list[str] = []
    used = 0
    for i, line in enumerate(entries):
        if used + len(line) + 1 + reserve > cap:
            lines.append(_summary(len(sections) - i))
            break
        lines.append(line)
        used += len(line) + 1

    if not lines:
        lines.append(_summary(len(sections)))

    out = "\n".join(lines)
    if len(out) > cap:
        # Degenerate cap: even the summary does not fit. Truncating to a bare
        # marker is better than silently blowing the budget we are enforcing.
        out = out[:cap - 1] + "…" if cap > 1 else out[:cap]
    return out


@_tracked("read_note", ["path", "section", "offset", "limit"])
async def read_note_impl(
    path: str,
    section: str | None = None,
    offset: int = 0,
    limit: int | None = None,
) -> str:
    """Read a note by its vault-relative path, capped to a context-safe size."""
    uid = current_user_id.get()
    try:
        note = read_file(path, user_id=uid)
    except FileNotFoundError:
        return f"Note not found: {path}"
    except ValueError as e:
        return str(e)

    cap = settings.max_read_response_chars
    if limit is not None:
        if limit < 1:
            return f"read_note: limit must be >= 1 (got {limit})."
        cap = min(limit, cap)
    if offset < 0:
        return f"read_note: offset must be >= 0 (got {offset})."

    content = note["content"]
    body = content
    origin = "note"
    if section is not None:
        extracted, err = extract_section(content, section)
        if err is not None:
            return err
        body = extracted
        origin = f"section '{section}'"

    parts = [f"# {note['title']}\n**Path:** `{note['path']}`"]
    if note["tags"]:
        parts.append(f"**Tags:** {', '.join(note['tags'])}")
    if note["frontmatter"]:
        fm_lines = [f"  {k}: {v}" for k, v in note["frontmatter"].items() if k not in ("title", "tags")]
        if fm_lines:
            parts.append("**Frontmatter:**\n" + "\n".join(fm_lines))

    if offset == 0 and len(body) <= cap:
        parts.append(f"\n---\n{body}")
        return "\n".join(parts)

    chunk, next_offset = _window(body, offset, cap)
    if not chunk and offset > 0:
        if offset == len(body):
            return (
                f"read_note: offset {offset:,} is exactly the end of {origin} "
                f"in {path} ({len(body):,} chars) — the whole {origin} has "
                f"been read, there is nothing further."
            )
        return (
            f"read_note: offset {offset:,} is past the end of {origin} in {path} "
            f"({len(body):,} chars)."
        )

    shown_to = min(offset, len(body)) + len(chunk)
    notice = [
        f"\n\n---\n**[TRUNCATED]** Showing chars {offset:,}–{shown_to:,} "
        f"of {len(body):,} for this {origin}."
    ]
    if next_offset is not None:
        notice.append(
            f'Continue with `read_note(path="{path}"'
            + (f', section="{section}"' if section is not None else "")
            + f', offset={next_offset})`.'
        )
    if section is None:
        outline = _outline_text(content, cap)
        if outline:
            notice.append(
                "Prefer jumping straight to what you need — this note's sections "
                f'are listed below. Read one with `read_note(path="{path}", '
                'section="<heading>")`, or by the `#N` ordinal shown '
                '(`section="#7"`). A bare `#N` always selects by position, so '
                'it stays reliable when titles repeat:\n'
                + outline
            )
        notice.append(
            "You can also narrow the search first with `search_notes` instead of "
            "reading the whole note."
        )

    parts.append(f"\n---\n{chunk}")
    parts.append("\n\n".join(notice))
    return "\n".join(parts)


@_tracked("list_notes", ["folder", "limit", "tags", "frontmatter"])
async def list_notes_impl(
    folder: str = "",
    limit: int = 50,
    tags: list[str] | None = None,
    frontmatter: dict | None = None,
) -> str:
    """List notes in a vault folder, sourced from the index."""
    from sqlalchemy import select
    from src.models.db import NoteMetadata

    limit = _clamp_limit(limit)
    uid = current_user_id.get()
    async with async_session() as session:
        stmt = select(NoteMetadata).order_by(NoteMetadata.modified_at.desc())
        stmt = apply_note_filters(
            stmt, folder=folder or None, tags=tags, frontmatter=frontmatter, user_id=uid
        )
        stmt = stmt.limit(limit)
        result = await session.execute(stmt)
        notes = result.scalars().all()

    if not notes:
        return f"No markdown files in '{folder or '/'}'"

    lines = [f"Found {len(notes)} notes in '{folder or '/'}':\n"]
    for n in notes:
        if n.modified_at:
            mod = n.modified_at.astimezone(timezone.utc).strftime("%Y-%m-%d")
        else:
            mod = "unknown"
        size = n.file_size or 0
        lines.append(f"- `{n.file_path}` ({size:,}B, modified {mod})")
    return "\n".join(lines)


@_tracked("get_tags", ["limit"])
async def get_tags_impl(limit: int = 50) -> str:
    """List all tags with counts."""
    from sqlalchemy import func, select
    from src.models.db import NoteMetadata

    limit = _clamp_limit(limit)
    uid = current_user_id.get()
    async with async_session() as session:
        tag_query = select(
            func.unnest(NoteMetadata.tags).label("tag"),
            func.count().label("count"),
        )
        if uid is not None:
            tag_query = tag_query.where(NoteMetadata.user_id == uid)
        result = await session.execute(
            tag_query.group_by("tag")
            .order_by(func.count().desc())
            .limit(limit)
        )
        rows = result.fetchall()

    if not rows:
        return "No tags found"

    lines = [f"Top {len(rows)} tags:\n"]
    for row in rows:
        lines.append(f"- #{row.tag} ({row.count})")
    return "\n".join(lines)


@_tracked("get_recent", ["limit", "folder", "tags", "frontmatter"])
async def get_recent_impl(
    limit: int = 20,
    folder: str | None = None,
    tags: list[str] | None = None,
    frontmatter: dict | None = None,
) -> str:
    """Recently modified notes."""
    from sqlalchemy import select
    from src.models.db import NoteMetadata

    limit = _clamp_limit(limit)
    uid = current_user_id.get()
    async with async_session() as session:
        query = select(NoteMetadata).order_by(NoteMetadata.modified_at.desc())
        query = apply_note_filters(
            query, folder=folder, tags=tags, frontmatter=frontmatter, user_id=uid
        )
        query = query.limit(limit)
        result = await session.execute(query)
        notes = result.scalars().all()

    if not notes:
        return "No recent notes found"

    lines = [f"Last {len(notes)} modified notes:\n"]
    for n in notes:
        mod = n.modified_at.strftime("%Y-%m-%d %H:%M") if n.modified_at else "unknown"
        tags_str = f" [{', '.join(n.tags)}]" if n.tags else ""
        lines.append(f"- `{n.file_path}` — {n.title}{tags_str} (modified {mod})")
    return "\n".join(lines)


@_tracked("semantic_search", ["query", "limit", "folder", "tags", "frontmatter"])
async def semantic_search_impl(
    query: str,
    limit: int = 15,
    folder: str | None = None,
    tags: list[str] | None = None,
    frontmatter: dict | None = None,
) -> str:
    """Vector similarity search using bge-m3 embeddings."""
    limit = _clamp_limit(limit, _MAX_SEMANTIC_RESULTS)
    uid = current_user_id.get()
    async with async_session() as session:
        results = await semantic_search(
            session,
            query,
            limit=limit,
            folder=folder,
            tags=tags,
            frontmatter=frontmatter,
            user_id=uid,
        )
    if not results:
        return f"No semantic results for '{query}' (embeddings may still be building)"
    lines = [f"Found {len(results)} semantic matches for '{query}':\n"]
    for r in results:
        tags_str = f" [{', '.join(r['tags'])}]" if r.get("tags") else ""
        lines.append(f"- **{r['title']}** (`{r['path']}`){tags_str} — similarity: {r['similarity']:.3f}")
        lines.append(f"  > {r['chunk'][:200]}...")
    return "\n".join(lines)


@_tracked("get_vault_guide", [])
async def get_vault_guide_impl() -> str:
    """Return the Obsidian primer plus any vault-specific conventions from CLAUDE.md."""
    uid = current_user_id.get()
    try:
        note = read_file("CLAUDE.md", user_id=uid)
        vault_section = (
            "# Vault-Specific Conventions\n"
            "\n"
            f"{note['content']}"
        )
    except FileNotFoundError:
        vault_section = _NO_CLAUDE_MD_MESSAGE
    except ValueError as e:
        vault_section = f"# Vault-Specific Conventions\n\n{e}"
    return f"{_VAULT_GUIDE_PRIMER}\n\n---\n\n{vault_section}"


def _require_write() -> str | None:
    """Return an error message if current key lacks write permission."""
    if current_permission.get() != "readwrite":
        return "Permission denied: this API key has read-only access. A 'readwrite' key is required."
    return None


def _note_size_error_for(size: int) -> str | None:
    """`_note_size_error` for a caller that already knows the encoded length.

    Split out so a path that must encode the content anyway (the `move_note`
    preflight, which also sums the encoded lengths) can reuse that one encode
    instead of paying for a second one inside the check.
    """
    if size > MAX_NOTE_BYTES:
        return f"Content too large ({size} bytes, max {MAX_NOTE_BYTES})"
    return None


def _note_size_error(content: str) -> str | None:
    """Refuse a note write whose *result* would exceed `MAX_NOTE_BYTES`.

    Every note write tool applies this to the content it is about to write, so
    a supported write is always decided here — with an actionable message —
    rather than by the MCP transport's body limit, which sits well above it.
    """
    return _note_size_error_for(len(content.encode("utf-8")))


@_tracked("create_note", ["path"])
async def create_note_impl(path: str, content: str) -> str:
    """Create a new note in the vault."""
    if err := _require_write():
        return err
    if not path.endswith(".md"):
        path += ".md"
    uid = current_user_id.get()
    # Validate before the size check: a caller naming an alias should learn
    # that the path is a symlink, not that its content is too big — the second
    # message sends it off to trim content that was never the problem.
    try:
        full_path = validate_mutable_path(path, user_id=uid)
    except ValueError as e:
        return str(e)
    if err := _note_size_error(content):
        return err
    try:
        write_file_at(full_path, content, overwrite=False)
        return f"Created note: {path}"
    except FileExistsError:
        return f"Note already exists: {path}. Use edit_note to modify it."
    except ValueError as e:
        return str(e)
    except OSError as e:
        return f"Failed to write {path}: {e}"


@_tracked("get_backlinks", ["path", "limit"])
async def get_backlinks_impl(path: str, limit: int = 50) -> str:
    """Notes that link TO `path` (resolved links only)."""
    from sqlalchemy import select
    from src.models.db import NoteLink, NoteMetadata

    uid = current_user_id.get()
    limit = max(1, min(limit, 500))
    async with async_session() as session:
        target_stmt = select(NoteMetadata).where(NoteMetadata.file_path == path)
        if uid is not None:
            target_stmt = target_stmt.where(NoteMetadata.user_id == uid)
        target = (await session.execute(target_stmt)).scalar_one_or_none()
        if target is None:
            return f"Note not found: {path}"

        SourceMeta = NoteMetadata
        stmt = (
            select(
                SourceMeta.file_path,
                SourceMeta.title,
                NoteLink.link_text,
                NoteLink.position,
                NoteLink.kind,
            )
            .join(SourceMeta, NoteLink.source_note_id == SourceMeta.id)
            .where(NoteLink.target_note_id == target.id)
            .order_by(SourceMeta.file_path, NoteLink.position)
            .limit(limit)
        )
        if uid is not None:
            stmt = stmt.where(SourceMeta.user_id == uid)
        rows = (await session.execute(stmt)).all()

    if not rows:
        return f"No backlinks to `{path}`"
    lines = [f"Found {len(rows)} backlinks to `{path}`:\n"]
    for r in rows:
        excerpt = (r.link_text or "").replace("\n", " ")[:120]
        lines.append(
            f"- **{r.title}** (`{r.file_path}`) — {r.kind} `{excerpt}` @ pos {r.position}"
        )
    return "\n".join(lines)


@_tracked("get_links", ["path"])
async def get_links_impl(path: str) -> str:
    """Outgoing links from `path` — both resolved and dangling."""
    from sqlalchemy import select
    from sqlalchemy.orm import aliased
    from src.models.db import NoteLink, NoteMetadata

    uid = current_user_id.get()
    async with async_session() as session:
        src_stmt = select(NoteMetadata).where(NoteMetadata.file_path == path)
        if uid is not None:
            src_stmt = src_stmt.where(NoteMetadata.user_id == uid)
        source = (await session.execute(src_stmt)).scalar_one_or_none()
        if source is None:
            return f"Note not found: {path}"

        TargetMeta = aliased(NoteMetadata)
        stmt = (
            select(
                NoteLink.kind,
                NoteLink.link_text,
                NoteLink.position,
                NoteLink.target_path,
                NoteLink.target_note_id,
                TargetMeta.file_path,
                TargetMeta.title,
            )
            .outerjoin(TargetMeta, NoteLink.target_note_id == TargetMeta.id)
            .where(NoteLink.source_note_id == source.id)
            .order_by(NoteLink.position)
        )
        rows = (await session.execute(stmt)).all()

    if not rows:
        return f"`{path}` has no outgoing links"
    resolved = [r for r in rows if r.target_note_id is not None]
    dangling = [r for r in rows if r.target_note_id is None]
    lines = [f"`{path}` — {len(resolved)} resolved, {len(dangling)} dangling:\n"]
    if resolved:
        lines.append("**Resolved:**")
        for r in resolved:
            lines.append(
                f"- {r.kind} → **{r.title}** (`{r.file_path}`) — `{r.link_text}`"
            )
    if dangling:
        lines.append("\n**Dangling:**")
        for r in dangling:
            lines.append(f"- {r.kind} → `{r.target_path}` — `{r.link_text}`")
    return "\n".join(lines)


@_tracked("get_neighborhood", ["path", "depth", "limit"])
async def get_neighborhood_impl(path: str, depth: int = 1, limit: int = 50) -> str:
    """BFS over the resolved-link graph treating links as undirected."""
    from sqlalchemy import or_, select
    from src.models.db import NoteLink, NoteMetadata

    uid = current_user_id.get()
    depth = max(1, min(depth, 5))
    limit = max(1, min(limit, 200))

    async with async_session() as session:
        src_stmt = select(NoteMetadata).where(NoteMetadata.file_path == path)
        if uid is not None:
            src_stmt = src_stmt.where(NoteMetadata.user_id == uid)
        source = (await session.execute(src_stmt)).scalar_one_or_none()
        if source is None:
            return f"Note not found: {path}"

        # BFS state.
        seen: dict[int, dict] = {source.id: {"distance": 0, "via": None}}
        frontier: list[int] = [source.id]
        truncated = False

        for d in range(1, depth + 1):
            if not frontier:
                break
            stmt = select(
                NoteLink.source_note_id,
                NoteLink.target_note_id,
            ).where(
                or_(
                    NoteLink.source_note_id.in_(frontier),
                    NoteLink.target_note_id.in_(frontier),
                ),
                NoteLink.target_note_id.isnot(None),
            )
            edges = (await session.execute(stmt)).all()
            next_frontier: list[int] = []
            for src_id, tgt_id in edges:
                # Walk both directions.
                for from_id, to_id in ((src_id, tgt_id), (tgt_id, src_id)):
                    if from_id in seen and to_id not in seen:
                        seen[to_id] = {"distance": d, "via": from_id}
                        next_frontier.append(to_id)
                        if len(seen) - 1 >= limit:
                            truncated = True
                            break
                if truncated:
                    break
            frontier = next_frontier
            if truncated:
                break

        # Hydrate metadata for everything except the source. The BFS edges
        # were already scoped to this user's graph (indexer guarantees the
        # vault_index is per-user), but we filter again here as a defense
        # in depth so a corrupted state can't leak rows across users.
        ids = [nid for nid in seen if nid != source.id]
        if not ids:
            return f"`{path}` has no resolved-link neighbors"
        meta_stmt = select(NoteMetadata).where(NoteMetadata.id.in_(ids))
        if uid is not None:
            meta_stmt = meta_stmt.where(NoteMetadata.user_id == uid)
        meta_rows = (await session.execute(meta_stmt)).scalars().all()
        meta_by_id = {m.id: m for m in meta_rows}
        # Drop any ids that the user_id filter excluded (shouldn't happen
        # under normal operation but keeps the output consistent).
        ids = [i for i in ids if i in meta_by_id]
        if not ids:
            return f"`{path}` has no resolved-link neighbors"
        # We also need `via` paths — fetch those.
        via_ids = {seen[nid]["via"] for nid in ids if seen[nid]["via"] is not None}
        via_paths = {source.id: source.file_path}
        if via_ids - {source.id}:
            via_stmt = select(NoteMetadata.id, NoteMetadata.file_path).where(
                NoteMetadata.id.in_(via_ids)
            )
            if uid is not None:
                via_stmt = via_stmt.where(NoteMetadata.user_id == uid)
            via_rows = (await session.execute(via_stmt)).all()
            for vid, vpath in via_rows:
                via_paths[vid] = vpath

    ordered = sorted(ids, key=lambda nid: (seen[nid]["distance"], meta_by_id[nid].file_path))
    lines = [
        f"Neighborhood of `{path}` (depth ≤ {depth}, {len(ordered)} notes"
        + (", truncated" if truncated else "") + "):\n"
    ]
    for nid in ordered:
        m = meta_by_id[nid]
        info = seen[nid]
        via_path = via_paths.get(info["via"], "?")
        tags_str = f" [{', '.join(m.tags)}]" if m.tags else ""
        lines.append(
            f"- d={info['distance']} **{m.title}** (`{m.file_path}`){tags_str} via `{via_path}`"
        )
    return "\n".join(lines)


def find_related_stmt(source_id: int, avg_embedding: list[float], user_id: int | None,
                      limit: int):
    """The vector statement `find_related` runs, and its overfetch.

    Factored out of `find_related_impl` so the recall benchmark in
    `tests/integration/test_search_recall.py` can EXPLAIN and re-run *this*
    statement rather than a hand-copied lookalike — a benchmark that measures a
    query production does not issue measures nothing.
    """
    from sqlalchemy import select
    from src.models.db import NoteEmbedding, NoteMetadata

    # Pull more than `limit` so we can dedupe by note. Same overfetch as
    # semantic_search so both vector paths share one recall contract.
    overfetch = max(limit * 5, 50)
    distance = NoteEmbedding.embedding.cosine_distance(avg_embedding)
    stmt = (
        select(
            NoteEmbedding.note_id,
            NoteEmbedding.chunk_text,
            NoteMetadata.file_path,
            NoteMetadata.title,
            NoteMetadata.tags,
            distance.label("distance"),
        )
        .join(NoteMetadata, NoteEmbedding.note_id == NoteMetadata.id)
        .where(NoteEmbedding.note_id != source_id)
    )
    if user_id is not None:
        stmt = stmt.where(NoteMetadata.user_id == user_id)
    return stmt.order_by(distance).limit(overfetch)


@_tracked("find_related", ["path", "limit"])
async def find_related_impl(path: str, limit: int = 10) -> str:
    """Semantic neighbors via averaged chunk embeddings."""
    import numpy as np
    from sqlalchemy import select
    from src.models.db import NoteEmbedding, NoteMetadata

    uid = current_user_id.get()
    limit = max(1, min(limit, 50))

    async with async_session() as session:
        # `db_ms` covers every database phase of this tool, the source-chunk
        # fetch included — it is accumulated, so the early returns below still
        # report the work they actually did.
        db_start = time.monotonic()
        src_stmt = select(NoteMetadata).where(NoteMetadata.file_path == path)
        if uid is not None:
            src_stmt = src_stmt.where(NoteMetadata.user_id == uid)
        source = (await session.execute(src_stmt)).scalar_one_or_none()
        if source is None:
            timing.add_ms("db_ms", time.monotonic() - db_start)
            return f"Note not found: {path}"

        chunks = (await session.execute(
            select(NoteEmbedding.embedding).where(NoteEmbedding.note_id == source.id)
        )).scalars().all()
        timing.add_ms("db_ms", time.monotonic() - db_start)
        if not chunks:
            return (
                f"`{path}` has not been embedded yet — "
                "the indexer is still catching up. Try again in a few minutes."
            )

        # The query vector: the mean of this note's own chunk vectors. NumPy is
        # still the right tool here (pgvector returns plain lists); what moved
        # to the database is the *scoring*, below.
        avg_list = np.mean(
            [np.asarray(c, dtype=float) for c in chunks], axis=0
        ).tolist()

        vector_start = time.monotonic()
        # Same HNSW tuning as semantic_search — see embeddings.py for the
        # full rationale, including why iterative_scan is what keeps a
        # filtered vector query from silently coming back empty. This query is
        # always filtered (`note_id != source.id`, plus the user scope), so it
        # is exposed to exactly the same post-filter candidate loss.
        await session.execute(text("SET LOCAL hnsw.ef_search = 80"))
        await session.execute(text("SET LOCAL random_page_cost = 1.1"))
        await session.execute(text("SET LOCAL hnsw.iterative_scan = 'relaxed_order'"))

        stmt = find_related_stmt(source.id, avg_list, uid, limit)
        rows = (await session.execute(stmt)).all()

        # Zero-row exact fallback, as in semantic_search: an empty result from
        # an approximate filtered scan is ambiguous, so re-run the identical
        # statement as an exact sequential scan before believing it.
        exact_fallback = False
        if not rows:
            # Transaction-scoped, like every other SET LOCAL here: it applies
            # to the re-run below and dies with this transaction. The session
            # closes immediately after the re-sort, so nothing else in this
            # call can inherit the exact plan — do not append further
            # statements to this block without re-reading that.
            await session.execute(text("SET LOCAL enable_indexscan = off"))
            rows = (await session.execute(stmt)).all()
            exact_fallback = True
        timing.record("exact_fallback", exact_fallback)
        timing.add_ms("db_ms", time.monotonic() - vector_start)

        # `relaxed_order` does not promise a globally sorted stream; re-sort
        # before dedupe so the presented order is monotone in distance.
        rows = sorted(rows, key=lambda r: r.distance)

    if not rows:
        return f"No related notes for `{path}`"

    # Dedupe by note_id, keeping the nearest chunk — ranked by the *same*
    # cosine distance the database ordered by, never by a distance recomputed
    # here. pgvector compares float32 vectors; NumPy would recompute in
    # float64 from the round-tripped values and order near-ties differently,
    # so a recomputed ranking could invert two rows relative to the ORDER BY
    # that selected them (and relative to the recall baseline). `similarity`
    # is the cosine similarity that distance encodes: `1 - distance`.
    best: dict[int, dict] = {}
    for r in rows:
        dist = float(r.distance)
        prev = best.get(r.note_id)
        if prev is None or dist < prev["distance"]:
            best[r.note_id] = {
                "path": r.file_path,
                "title": r.title,
                "tags": r.tags,
                "distance": dist,
                "chunk": r.chunk_text,
            }

    ranked = sorted(best.values(), key=lambda x: x["distance"])[:limit]
    lines = [f"Top {len(ranked)} related notes for `{path}`:\n"]
    for r in ranked:
        tags_str = f" [{', '.join(r['tags'])}]" if r["tags"] else ""
        snippet = r["chunk"].replace("\n", " ")[:200]
        lines.append(
            f"- **{r['title']}** (`{r['path']}`){tags_str} — sim: {1 - r['distance']:.3f}"
        )
        lines.append(f"  > {snippet}…")
    return "\n".join(lines)


@_tracked("find_orphans", ["folder", "limit"])
async def find_orphans_impl(folder: str | None = None, limit: int = 50) -> str:
    """Notes with zero incoming AND zero outgoing resolved links."""
    from sqlalchemy import select, union
    from src.models.db import NoteLink, NoteMetadata

    uid = current_user_id.get()
    limit = max(1, min(limit, 500))

    async with async_session() as session:
        # The "connected" subquery collects every NoteLink endpoint id.
        # Since `note_links` has no `user_id`, scoping happens implicitly:
        # the outer `NoteMetadata` query filters to this user's notes, so
        # only those rows are candidates for orphan-ness. Any cross-user
        # NoteLink rows (which would only exist on a corrupted state)
        # would still appear in `connected` and exclude the corresponding
        # note id — that's the safe direction (false negatives, not
        # false orphans).
        sources = select(NoteLink.source_note_id.label("nid")).where(
            NoteLink.source_note_id.isnot(None)
        )
        targets = select(NoteLink.target_note_id.label("nid")).where(
            NoteLink.target_note_id.isnot(None)
        )
        connected = union(sources, targets).subquery()
        stmt = select(NoteMetadata).where(NoteMetadata.id.notin_(select(connected.c.nid)))
        stmt = apply_note_filters(stmt, folder=folder, user_id=uid)
        stmt = stmt.order_by(NoteMetadata.modified_at.desc().nullslast()).limit(limit)
        notes = (await session.execute(stmt)).scalars().all()

    if not notes:
        scope = f" in `{folder}`" if folder else ""
        return f"No orphan notes{scope}"
    lines = [f"Found {len(notes)} orphan notes:\n"]
    for n in notes:
        mod = n.modified_at.strftime("%Y-%m-%d") if n.modified_at else "unknown"
        tags_str = f" [{', '.join(n.tags)}]" if n.tags else ""
        lines.append(f"- `{n.file_path}` — {n.title}{tags_str} (modified {mod})")
    return "\n".join(lines)


@_tracked(
    "edit_note",
    ["path", "append", "operation", "find", "section", "replace_all", "dry_run"],
)
async def edit_note_impl(
    path: str,
    content: str,
    append: bool = False,
    operation: str | None = None,
    find: str | None = None,
    section: str | None = None,
    replace_all: bool = False,
    dry_run: bool = False,
) -> str:
    """Edit an existing note in the vault."""
    if err := _require_write():
        return err

    if operation is not None:
        operation = operation.lower()
        if operation not in {"append", "replace"}:
            return (
                'edit_note: operation must be "append" or "replace" '
                f'(got {operation!r}).'
            )
        if operation == "append":
            append = True

    selected = []
    if append:
        selected.append("append=True")
    if find is not None:
        selected.append("find=...")
    if section is not None:
        selected.append("section=...")
    if operation == "replace" and selected:
        selected.append('operation="replace"')
    if len(selected) > 1:
        return (
            "edit_note: choose at most one of append, find, section "
            f"(got {', '.join(selected)})."
        )

    uid = current_user_id.get()
    try:
        from src.services.vault import replace_section
        # Resolved before the read, so every mode — `dry_run` included —
        # refuses an alias rather than diffing (and then reporting on) a note
        # the caller did not name. Everything below acts on this Path: the
        # caller's string is never re-resolved, so an ancestor symlink
        # repointed between the read and the write cannot redirect the write.
        full_path = validate_mutable_path(path, user_id=uid)
    except ValueError as e:
        return str(e)
    if not full_path.exists():
        return f"Note not found: {path}. Use create_note to create it."

    try:
        existing_bytes = read_bytes_at(
            full_path, max_bytes=MAX_NOTE_BYTES, label=path
        )
        existing = existing_bytes.decode("utf-8")
    except Exception as e:
        # Includes OSError: an ELOOP from the `O_NOFOLLOW` read means the leaf
        # became a symlink after validation. That is a refusal, not a crash.
        return f"Failed to read {path}: {e}"

    new_content: str | None = None
    success_message: str = f"Updated note: {path}"

    if section is not None:
        new_content, err = replace_section(existing, section, content)
        if err is not None:
            return err
    elif find is not None:
        if find == "":
            return (
                "edit_note: find must be a non-empty string. "
                "An empty find would match every position and corrupt the note."
            )
        count = existing.count(find)
        if count == 0:
            preview = existing[:500]
            return (
                f"Find text not found in {path}. "
                f"First 500 chars of note:\n---\n{preview}\n---"
            )
        if count > 1 and not replace_all:
            return (
                f"Find text matches {count} locations in {path}. "
                "Provide more surrounding context to match a unique section, "
                "or set replace_all=True."
            )
        if replace_all:
            new_content = existing.replace(find, content)
            success_message = (
                f"Replaced {count} occurrence(s) in {path}"
            )
        else:
            new_content = existing.replace(find, content, 1)
    elif append:
        new_content = existing + "\n" + content
    else:
        new_content = content

    # Bound the *result*, before the diff and before the atomic write, so an
    # over-cap edit is refused by the tool in every mode (including dry_run)
    # and nothing is written. Must stay ahead of the `expected=` write below,
    # which is what detects a concurrent read-modify-write conflict.
    if err := _note_size_error(new_content):
        return err

    if dry_run:
        if new_content == existing:
            return f"No changes for {path}"
        import difflib
        diff = "".join(difflib.unified_diff(
            existing.splitlines(keepends=True),
            new_content.splitlines(keepends=True),
            fromfile=path,
            tofile=path,
            lineterm="",
        ))
        return diff or f"No changes for {path}"

    try:
        write_file_at(full_path, new_content, expected=existing_bytes)
    except (ValueError, RuntimeError) as e:
        return str(e)
    except OSError as e:
        return f"Failed to write {path}: {e}"
    return success_message


# ────────────────────────────────────────────────────────────────────────────
# move_note
# ────────────────────────────────────────────────────────────────────────────


_WIKILINK_REWRITE_RE = re.compile(
    r"(?P<embed>!)?\[\[(?P<target>[^\]\|#\n]+)"
    r"(?P<rest>(?:#[^\]\|\n]*)?(?:\|[^\]\n]*)?)\]\]"
)
_MDLINK_REWRITE_RE = re.compile(
    r"\[(?P<text>[^\]\n]+)\]\((?P<href>[^)\n]+?\.md)(?P<anchor>#[^)]*)?\)"
)


def _rewrite_links_in_text(
    content: str,
    from_rel: str,
    to_rel: str,
    source_path: str,
    pre_move_index: dict,
    output_source_path: str | None = None,
) -> tuple[str, int]:
    """Rewrite any wikilink/embed/markdown-link in `content` whose pre-move
    resolution would have pointed at `from_rel`, so it now refers to `to_rel`.

    Preserves alias (`|...`) and anchor (`#...`) parts. For wikilinks, a bare
    target stays bare (uses the new stem), while a path-style target is
    rewritten to the full new path-style form (preserving any trailing `.md`).
    Markdown links always get the new full path. Code blocks are skipped.
    """
    from src.services.links import mask_code, resolve_target

    paths = pre_move_index.get("paths", {})
    from_id = paths.get(from_rel)
    if from_id is None:
        return content, 0

    to_stem = PurePosixPath(to_rel).stem
    to_no_md = to_rel[:-3] if to_rel.endswith(".md") else to_rel

    masked = mask_code(content)
    rewrites: list[tuple[int, int, str]] = []

    for m in _WIKILINK_REWRITE_RE.finditer(masked):
        target_raw = m.group("target")
        target = target_raw.strip()
        if not target:
            continue
        if resolve_target(target, source_path, pre_move_index) != from_id:
            continue
        target_no_md = target[:-3] if target.endswith(".md") else target
        is_path_style = "/" in target_no_md or target.endswith(".md")
        if is_path_style:
            new_target = to_no_md + (".md" if target.endswith(".md") else "")
        else:
            new_target = to_stem
        embed_prefix = "!" if m.group("embed") else ""
        rest = m.group("rest") or ""
        rewrites.append((m.start(), m.end(), f"{embed_prefix}[[{new_target}{rest}]]"))

    for m in _MDLINK_REWRITE_RE.finditer(masked):
        href = m.group("href").strip()
        if not href:
            continue
        target_for_resolve = href[:-3] if href.endswith(".md") else href
        if resolve_target(target_for_resolve, source_path, pre_move_index) != from_id:
            continue
        anchor = m.group("anchor") or ""
        # Resolve against the original source location, but generate the new
        # href relative to where that source lives after the move. These differ
        # for a moved note rewriting its own Markdown self-link.
        output_path = output_source_path or source_path
        source_dir = PurePosixPath(output_path).parent.as_posix()
        relative_target = posixpath.relpath(to_rel, source_dir)
        rewrites.append((
            m.start(),
            m.end(),
            f"[{m.group('text')}]({relative_target}{anchor})",
        ))

    if not rewrites:
        return content, 0
    rewrites.sort(key=lambda r: r[0], reverse=True)
    out = content
    for start, end, replacement in rewrites:
        out = out[:start] + replacement + out[end:]
    return out, len(rewrites)


def _rewrite_failure_warning(failed_sources: list[str]) -> str | None:
    """Describe backlink rewrites that failed after the move completed."""
    if not failed_sources:
        return None
    preview = ", ".join(failed_sources[:3])
    if len(failed_sources) > 3:
        preview += f", and {len(failed_sources) - 3} more"
    return (
        "partial success: note moved, but link rewrites failed in "
        f"{len(failed_sources)} note(s): {preview}"
    )


def _note_owner_predicate(uid: int | None):
    """Return the exact NoteMetadata ownership predicate for a vault context."""
    from src.models.db import NoteMetadata

    return (
        NoteMetadata.user_id.is_(None)
        if uid is None
        else NoteMetadata.user_id == uid
    )


def _ensure_move_source_in_index(index: dict, from_rel: str) -> None:
    """Make stale/missing metadata unable to suppress moved-note self rewrites."""
    if from_rel in index["paths"]:
        return
    synthetic_id = -1
    index["paths"][from_rel] = synthetic_id
    stem = PurePosixPath(from_rel).stem
    index["stems"].setdefault(stem, []).append((from_rel, synthetic_id))


@_tracked("move_note", ["from_path", "to_path", "rewrite_links"])
async def move_note_impl(
    from_path: str,
    to_path: str,
    rewrite_links: bool = False,
) -> str:
    """Move (rename or relocate) a note inside the vault."""
    if err := _require_write():
        return err

    from sqlalchemy import select, update
    from src.models.db import NoteLink, NoteMetadata
    from src.services.links import build_vault_index
    from src.services.vault import _vault_root, move_no_clobber

    uid = current_user_id.get()
    try:
        src_full = validate_mutable_path(from_path, user_id=uid)
        dst_full = validate_mutable_path(to_path, user_id=uid)
    except ValueError as e:
        return str(e)
    if not src_full.is_file():
        return f"Source note not found: {from_path}"
    vault = _vault_root(uid).resolve()
    # Both paths are already `resolved_parent / name`, so this is the path the
    # indexer stores for a note reached through a symlinked folder — the DB
    # rows below, and the backlink lookup keyed on `from_rel`, line up with it.
    from_rel = src_full.relative_to(vault).as_posix()
    to_rel = dst_full.relative_to(vault).as_posix()

    pre_move_index: dict | None = None
    rewrite_sources: list[str] = [from_rel] if rewrite_links else []
    if rewrite_links:
        async with async_session() as session:
            rows_stmt = select(NoteMetadata.file_path, NoteMetadata.id).where(
                _note_owner_predicate(uid)
            )
            rows = (await session.execute(rows_stmt)).all()
            pre_move_index = build_vault_index([(r.file_path, r.id) for r in rows])
            _ensure_move_source_in_index(pre_move_index, from_rel)
            target_id = pre_move_index["paths"].get(from_rel)
            if target_id is not None:
                src_q = (
                    select(NoteMetadata.file_path)
                    .join(NoteLink, NoteLink.source_note_id == NoteMetadata.id)
                    .where(NoteLink.target_note_id == target_id)
                    .distinct()
                )
                src_q = src_q.where(_note_owner_predicate(uid))
                src_rows = (await session.execute(src_q)).all()
                rewrite_sources.extend(r.file_path for r in src_rows)
                rewrite_sources = list(dict.fromkeys(rewrite_sources))

    # ── Phase 1: preflight ──────────────────────────────────────────────────
    # Compute every rewritten body *before* anything is mutated. If one would
    # exceed the note cap the whole move aborts: the alternative (move, update
    # note_links, then skip the over-cap source) leaves the graph asserting a
    # link the vault bytes do not contain, and an agent acting on that graph
    # never sees the discrepancy.
    #
    # Memory: `read_bytes_at` bounds each source at MAX_NOTE_BYTES, but the number
    # of sources is unbounded — a target with hundreds of near-cap backlinks
    # would buffer gigabytes before a single byte is mutated. So the originals
    # and the rewrites are summed as they accumulate and the move aborts (still
    # before any mutation) once that total would exceed MAX_MOVE_REWRITE_BYTES.
    planned_rewrites: list[tuple[str, Path, bytes, str, int]] = []
    rewrite_bytes_held = 0
    failed_rewrite_sources: list[str] = []
    if rewrite_links and pre_move_index is not None:
        for original_src_path in rewrite_sources:
            # A moved note may link to itself: it is still at its old path now,
            # so read it there, but emit link targets relative to where it is
            # about to land — and write it at its new location.
            moved_note = original_src_path == from_rel
            out_path = to_rel if moved_note else original_src_path
            try:
                # Each source is resolved once here and mutated through that
                # Path in phase 3. Re-passing the string to `write_file` would
                # resolve it a second time, after the move, so an ancestor
                # symlink repointed in between would send the rewritten body
                # somewhere the preflight never checked.
                if moved_note:
                    read_target, write_target = src_full, dst_full
                else:
                    read_target = validate_mutable_path(
                        original_src_path, user_id=uid
                    )
                    write_target = read_target
                if not read_target.is_file():
                    continue
                original_bytes = read_bytes_at(
                    read_target, max_bytes=MAX_NOTE_BYTES, label=original_src_path
                )
                content = original_bytes.decode("utf-8")
                new_content, n = _rewrite_links_in_text(
                    content,
                    from_rel,
                    to_rel,
                    original_src_path,
                    pre_move_index,
                    output_source_path=out_path,
                )
            except Exception as e:
                logger.warning(
                    "Failed to rewrite links in %s: %s", original_src_path, e
                )
                failed_rewrite_sources.append(original_src_path)
                continue
            if n == 0:
                continue
            # A rewrite can only grow a note (the new path is usually longer
            # than the old one), so it is a note write like any other and gets
            # the same cap — enforced here, where refusing costs nothing. The
            # one encode also feeds the aggregate below.
            new_size = len(new_content.encode("utf-8"))
            if err := _note_size_error_for(new_size):
                return (
                    f"Move aborted: rewriting links in {original_src_path} would "
                    f"exceed the note size limit ({err}). Nothing was moved, "
                    "rewritten or reindexed."
                )
            rewrite_bytes_held += len(original_bytes) + new_size
            if rewrite_bytes_held > MAX_MOVE_REWRITE_BYTES:
                return (
                    f"Move aborted: rewriting links across "
                    f"{len(planned_rewrites) + 1} notes would need "
                    f"{rewrite_bytes_held} bytes in memory (limit "
                    f"{MAX_MOVE_REWRITE_BYTES} bytes, "
                    f"{MAX_MOVE_REWRITE_BYTES // (1024 * 1024)} MiB). Nothing "
                    "was moved, rewritten or reindexed. Move without "
                    "rewrite_links and update links in batches instead."
                )
            planned_rewrites.append(
                (out_path, write_target, original_bytes, new_content, n)
            )

    # ── Phase 2: commit ─────────────────────────────────────────────────────
    try:
        move_no_clobber(src_full, dst_full)
    except FileExistsError:
        return f"Destination already exists: {to_path}"
    except OSError as e:
        return f"Move failed: {e}"

    db_failed = False
    try:
        async with async_session() as session:
            nm_update = (
                update(NoteMetadata)
                .where(
                    NoteMetadata.file_path == from_rel,
                    _note_owner_predicate(uid),
                )
                .values(file_path=to_rel)
            )
            await session.execute(nm_update)

            # Scope the NoteLink.target_path update to this user's link rows
            # by joining through their source notes. In single-user mode the
            # subquery selects every notes_metadata row (user_id IS NULL) so
            # the legacy behavior is preserved.
            user_note_ids = select(NoteMetadata.id).where(
                _note_owner_predicate(uid)
            )
            link_update = (
                update(NoteLink)
                .where(
                    NoteLink.target_path == from_rel,
                    NoteLink.source_note_id.in_(user_note_ids),
                )
                .values(target_path=to_rel)
            )
            await session.execute(link_update)
            await session.commit()
    except Exception as e:
        logger.warning(
            "DB update failed after FS move %s → %s: %s", from_rel, to_rel, e
        )
        db_failed = True

    rewrites_done = 0
    files_modified = 0
    for write_path, write_target, original_bytes, new_content, n in planned_rewrites:
        try:
            write_file_at(write_target, new_content, expected=original_bytes)
            rewrites_done += n
            files_modified += 1
        except Exception as e:
            logger.warning("Failed to rewrite links in %s: %s", write_path, e)
            failed_rewrite_sources.append(write_path)

    parts = [f"Moved {from_rel} → {to_rel}"]
    if db_failed:
        parts.append("(warning: DB update failed; reindex will reconcile)")
    if rewrite_links:
        parts.append(
            f"rewrote {rewrites_done} link(s) across {files_modified} note(s)"
        )
        warning = _rewrite_failure_warning(failed_rewrite_sources)
        if warning is not None:
            parts.append(f"(warning: {warning})")
    return " — ".join(parts) if len(parts) > 1 else parts[0]


# ────────────────────────────────────────────────────────────────────────────
# delete_note
# ────────────────────────────────────────────────────────────────────────────


@_tracked("delete_note", ["path", "permanent"])
async def delete_note_impl(path: str, permanent: bool = False) -> str:
    """Soft-delete a note to `.trash/`, or `os.unlink` it when `permanent=True`."""
    if err := _require_write():
        return err

    from src.services.vault import _vault_root, move_no_clobber, validate_mutable_path

    uid = current_user_id.get()
    try:
        full_path = validate_mutable_path(path, user_id=uid)
    except ValueError as e:
        return str(e)
    if not full_path.is_file():
        return f"Note not found: {path}"

    if permanent:
        try:
            os.unlink(full_path)
        except OSError as e:
            return f"Permanent delete failed: {e}"
        return f"Permanently deleted: {path}"

    vault = _vault_root(uid)
    trash = vault / ".trash"
    trash.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    base = f"{timestamp}-{full_path.name}"
    counter = 0
    while True:
        suffix = "" if counter == 0 else f"-{counter}"
        dest = trash / f"{timestamp}{suffix}-{full_path.name}"
        try:
            move_no_clobber(full_path, dest)
            break
        except FileExistsError:
            # Another delete may publish this name after we choose it. Retry
            # rather than replacing existing trash content.
            counter += 1
        except OSError as e:
            return f"Soft-delete failed: {e}"
    rel = dest.relative_to(vault).as_posix()
    return f"Soft-deleted: {path} → {rel}"


# ────────────────────────────────────────────────────────────────────────────
# set_frontmatter
# ────────────────────────────────────────────────────────────────────────────


@_tracked("set_frontmatter", ["path"])
async def set_frontmatter_impl(
    path: str,
    updates: dict | None = None,
    remove: list[str] | None = None,
) -> str:
    """Merge `updates` into a note's YAML frontmatter and drop keys in `remove`."""
    if err := _require_write():
        return err

    updates = dict(updates or {})
    remove = list(remove or [])

    from src.services.vault import parse_frontmatter, serialize_frontmatter

    uid = current_user_id.get()
    try:
        # One resolution for the whole read-modify-write (see `edit_note_impl`).
        full_path = validate_mutable_path(path, user_id=uid)
    except ValueError as e:
        return str(e)
    if not full_path.is_file():
        return f"Note not found: {path}"

    if not updates and not remove:
        return f"No changes for {path} (empty updates and remove)"

    try:
        raw_bytes = read_bytes_at(full_path, max_bytes=MAX_NOTE_BYTES, label=path)
        raw = raw_bytes.decode("utf-8")
    except Exception as e:
        # OSError included — an ELOOP here means the leaf was swapped for a
        # link after validation; report it rather than raising.
        return f"Failed to read {path}: {e}"

    fm, body = parse_frontmatter(raw)

    set_keys: list[str] = []
    for k, v in updates.items():
        fm[k] = v
        set_keys.append(k)
    removed_keys: list[str] = []
    for k in remove:
        if k in fm:
            del fm[k]
            removed_keys.append(k)

    new_raw = serialize_frontmatter(fm, body)
    if new_raw == raw:
        return f"No changes for {path}"

    # Bound the result before writing (see `edit_note_impl`). A remove-only
    # call can only shrink the note, but the check is uniform.
    if err := _note_size_error(new_raw):
        return err

    try:
        write_file_at(full_path, new_raw, expected=raw_bytes)
    except (ValueError, RuntimeError) as e:
        return str(e)
    except OSError as e:
        return f"Failed to write {path}: {e}"

    summary: list[str] = []
    if set_keys:
        summary.append(f"set: {', '.join(set_keys)}")
    if removed_keys:
        summary.append(f"removed: {', '.join(removed_keys)}")
    if not summary:
        summary.append("no key changes (whitespace-only)")
    return f"Updated frontmatter in {path} ({'; '.join(summary)})"


# ────────────────────────────────────────────────────────────────────────────
# Raw file-access tools: read_file / write_file / list_files
# ────────────────────────────────────────────────────────────────────────────


def _base64_payload(path: str, data: bytes, mime: str) -> str:
    """Format raw bytes as a labeled base64 block.

    The header makes the encoding explicit and warns that the body is opaque
    (a skill/client decodes it; the model cannot read it). The base64 string
    is the final block, separated by a blank line.
    """
    b64 = base64.b64encode(data).decode("ascii")
    return (
        "encoding: base64\n"
        f"mime: {mime}\n"
        f"bytes: {len(data)}\n"
        f"path: {path}\n"
        "(opaque bytes — not human-readable; pass to a skill/client to decode)\n\n"
        f"{b64}"
    )


def _capped_text(text: str, path: str, offset: int, cap: int) -> str:
    """Return a context-safe window of decoded file text."""
    if offset == 0 and len(text) <= cap:
        return text
    chunk, next_offset = _window(text, offset, cap)
    if not chunk and offset > 0:
        if offset == len(text):
            return (
                f"read_file: offset {offset:,} is exactly the end of {path} "
                f"({len(text):,} chars) — the whole file has been read, there "
                f"is nothing further."
            )
        return (
            f"read_file: offset {offset:,} is past the end of {path} "
            f"({len(text):,} chars)."
        )
    shown_to = min(offset, len(text)) + len(chunk)
    notice = (
        f"\n\n---\n**[TRUNCATED]** Showing chars {offset:,}–{shown_to:,} "
        f"of {len(text):,} for {path}."
    )
    if next_offset is not None:
        notice += (
            f' Continue with `read_file(path="{path}", offset={next_offset})`.'
        )
    return chunk + notice


@_tracked("read_file", ["path", "encoding", "offset", "limit"])
async def read_file_impl(
    path: str,
    encoding: str = "auto",
    offset: int = 0,
    limit: int | None = None,
):
    """Read any vault file: text, inline image block, or base64 bytes."""
    if encoding not in ("auto", "text", "base64"):
        return f"Invalid encoding '{encoding}'. Use 'auto', 'text', or 'base64'."
    if offset < 0:
        return f"read_file: offset must be >= 0 (got {offset})."
    cap = settings.max_read_response_chars
    if limit is not None:
        if limit < 1:
            return f"read_file: limit must be >= 1 (got {limit})."
        cap = min(limit, cap)

    uid = current_user_id.get()
    try:
        data = read_bytes(path, user_id=uid, max_bytes=settings.max_file_read_bytes)
    except FileNotFoundError:
        return f"File not found: {path}"
    except ValueError as e:
        return str(e)

    if encoding == "text":
        try:
            return _capped_text(data.decode("utf-8"), path, offset, cap)
        except UnicodeDecodeError:
            return (
                f"Cannot decode {path} as UTF-8 text (not valid UTF-8). "
                'Use encoding="base64" for binary files.'
            )

    if encoding == "base64":
        mime = mimetypes.guess_type(path)[0] or "application/octet-stream"
        return _base64_payload(path, data, mime)

    # encoding == "auto"
    kind, mime = classify_bytes(data, path)
    if kind == "text":
        try:
            return _capped_text(data.decode("utf-8"), path, offset, cap)
        except UnicodeDecodeError:
            return _base64_payload(path, data, mime)
    if kind == "image":
        # FastMCP wraps this into an MCP image content block. `format` becomes
        # the `image/<format>` MIME (e.g. "png", "jpeg", "gif", "webp").
        return Image(data=data, format=mime.split("/", 1)[1])
    return _base64_payload(path, data, mime)


@_tracked("write_file", ["path", "encoding", "overwrite"])
async def write_file_impl(
    path: str,
    content: str,
    encoding: str = "base64",
    overwrite: bool = False,
) -> str:
    """Write a file into the vault from base64 or text content."""
    if err := _require_write():
        return err
    if encoding not in ("base64", "text"):
        return f"Invalid encoding '{encoding}'. Use 'base64' or 'text'."

    uid = current_user_id.get()
    # Validate before decoding and before the size check, for the same reason
    # as `create_note_impl`: a symlinked destination is a path problem, and
    # saying so beats sending the caller away to shrink its payload.
    try:
        full_path = validate_mutable_path(path, user_id=uid)
    except ValueError as e:
        return str(e)

    if encoding == "base64":
        try:
            data = base64.b64decode(content, validate=True)
        except (binascii.Error, ValueError):
            return "Invalid base64 content: could not decode. No file was written."
    else:
        data = content.encode("utf-8")

    if len(data) > settings.max_file_write_bytes:
        return (
            f"Content too large ({len(data):,} bytes, "
            f"max {settings.max_file_write_bytes:,}). No file was written."
        )

    try:
        write_bytes_at(full_path, data, overwrite=overwrite)
    except FileExistsError:
        return f"File already exists: {path}. Pass overwrite=True to replace it."
    except ValueError as e:
        return str(e)
    except OSError as e:
        return f"Failed to write {path}: {e}"
    return f"Wrote {len(data):,} bytes to {path}"


@_tracked("list_files", ["folder", "pattern", "recursive", "limit"])
async def list_files_impl(
    folder: str = ".",
    pattern: str = "*",
    recursive: bool = False,
    limit: int = 200,
) -> str:
    """Browse vault files and subdirectories (`ls`-style)."""
    uid = current_user_id.get()
    limit = max(1, min(limit, 1000))
    try:
        entries, truncated = list_dir(
            folder, pattern=pattern, recursive=recursive, limit=limit, user_id=uid
        )
    except NotADirectoryError as e:
        return str(e)
    except ValueError as e:
        return str(e)

    where = folder or "."
    if not entries:
        return f"No entries in '{where}' matching '{pattern}'"

    header = f"{len(entries)} " + ("entry" if len(entries) == 1 else "entries")
    header += f" in '{where}'"
    if pattern != "*":
        header += f" matching '{pattern}'"
    if recursive:
        header += " (recursive)"
    if truncated:
        header += ", truncated"
    lines = [header + ":\n"]
    for e in entries:
        if e["is_dir"]:
            lines.append(f"- 📁 `{e['path']}/`")
        else:
            mod = datetime.fromtimestamp(e["mtime"], timezone.utc).strftime(
                "%Y-%m-%d %H:%M"
            )
            lines.append(f"- `{e['path']}` ({e['size']:,}B, modified {mod})")
    if truncated:
        lines.append(
            f"\n… more than {limit} entries; narrow with `pattern` or a subfolder."
        )
    return "\n".join(lines)


# ────────────────────────────────────────────────────────────────────────────
# File-transfer tools: request_upload / check_upload / request_download /
# import_from_url / delete_file
#
# No MCP client can hand a tool raw attachment bytes, and no agent shell can
# reach into the user's downloads folder. These five close that gap without
# widening the vault's write surface: each mint pins one path, one direction
# and one identity into a short-lived capability, and the `/transfer/*` routes
# will act on nothing but what was pinned.
# ────────────────────────────────────────────────────────────────────────────


_NO_PUBLIC_ORIGIN = (
    "This server has no public origin configured, so it cannot build a "
    "shareable transfer link. Set MCP_HOSTNAME (preferred) or BASE_URL in the "
    "server's environment and restart. Nothing was minted."
)


def _transfer_identity() -> transfer.Identity:
    return transfer.Identity(
        key_id=current_api_key_id.get(),
        oauth_token_id=current_oauth_token_id.get(),
        user_id=current_user_id.get(),
    )


def _vault_context(path: str, uid: int | None) -> tuple[str, str]:
    """`(canonical vault root, canonical vault-relative path)` for a transfer.

    Both are frozen into the token, so both have to be exactly what the routes
    will later re-derive: the root as `_vault_root` yields it, the path as the
    caller named it.

    **The relative path is normalised lexically, not through `resolve()`.**
    `validate_visible_path` still runs — it is the shared traversal and dot-dir
    guard, and it is what refuses a link that points out of the vault — but its
    *return value* is the resolved path, and resolving follows symlinks. Taking
    the relative path from there would silently retarget the operation: a
    `delete_file("Attachments/alias.png")` where `alias.png` links to
    `secret.png` inside the vault would resolve to `secret.png` and delete
    that, reporting success for a path the caller never named. Keeping the
    caller's own components means the anchored `O_NOFOLLOW` walk in
    `vault_fs` is the thing that meets the symlink, and it refuses it.
    """
    root = _vault_root(uid)
    validate_visible_path(path, user_id=uid)

    rel = PurePosixPath(str(path).replace(os.sep, "/"))
    if rel.is_absolute():
        raise ValueError(f"Path traversal denied: {path}")
    parts = [part for part in rel.parts if part not in ("", ".")]
    if any(part == ".." for part in parts):
        raise ValueError(f"Path traversal denied: {path}")
    if not parts:
        raise ValueError(f"Not a file path: {path!r}")
    canonical = "/".join(parts)
    if is_hidden_path(canonical):
        raise ValueError(f"Hidden path denied: {path}")
    return str(root), canonical


def _fingerprint_of(root: str, rel_path: str) -> dict | None:
    """The target's identity at mint time, or `None` when it does not exist.

    `None` is meaningful: on an overwrite token it is the expected-*absence*
    sentinel and the publish step requires the target to still be absent.
    """
    root_fd = vault_fs.open_root(root)
    try:
        dir_fd, name = vault_fs.open_parent(root_fd, rel_path, create=False)
    except FileNotFoundError:
        return None  # the parent folder does not exist yet
    finally:
        os.close(root_fd)
    try:
        return vault_fs.fingerprint(
            dir_fd, name, hash_up_to=settings.max_file_write_bytes
        )
    finally:
        os.close(dir_fd)


def _mint_preflight(path: str, *, need_write: bool) -> tuple | str:
    """Shared front half of the three mint tools: permission, origin, path, FS.

    Returns `(uid, root, rel, base)` or an error string. Every refusal happens
    before a row is written, so a failed mint leaves nothing behind.

    **The filesystem probe runs only for a write.** `probe_publication` creates
    a temp file and a hard link; running it for `request_download` would mean a
    read-only identity's read tool writing to the vault — on a fresh vault, the
    first thing it ever did would be to create files. A download publishes
    nothing, so it needs no proof that publication works.
    """
    if need_write and (err := _require_write()):
        return err
    base = settings.public_base_url
    if base is None:
        return _NO_PUBLIC_ORIGIN
    uid = current_user_id.get()
    try:
        root, rel = _vault_context(path, uid)
    except ValueError as e:
        return str(e)
    except RuntimeError as e:  # cold vault-path cache in multi-user mode
        return str(e)
    if need_write:
        try:
            vault_fs.check_publication_support(root)
        except vault_fs.UnsupportedFilesystem as e:
            return str(e)
        except (OSError, vault_fs.VaultFSError) as e:
            return f"Vault root is not usable: {e}"
    return uid, root, rel, base.rstrip("/")


def _expiry_line(row) -> str:
    return row.expires_at.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")


@_tracked("request_upload", ["path", "overwrite", "expires_in"])
async def request_upload_impl(
    path: str,
    overwrite: bool = False,
    expires_in: int | None = None,
) -> str:
    """Mint a one-shot link a human can drop a file onto."""
    pre = _mint_preflight(path, need_write=True)
    if isinstance(pre, str):
        return pre
    uid, root, rel, base = pre

    try:
        fingerprint = _fingerprint_of(root, rel)
    except vault_fs.UnsafePath as e:
        return str(e)
    if fingerprint is not None and not overwrite:
        return (
            f"File already exists: {rel}. Pass overwrite=True to replace it "
            "(the link will then refuse to publish if the file changes before "
            "the upload). Nothing was minted."
        )

    async with async_session() as session:
        token, row = await transfer.mint_token(
            session,
            "upload",
            rel,
            overwrite=overwrite,
            identity=_transfer_identity(),
            vault_root=root,
            # On a no-overwrite token the publish is a kernel-linearizable
            # hard link, so there is nothing to compare against; the
            # fingerprint only means anything when we intend to replace.
            expected_fingerprint=fingerprint if overwrite else None,
            expires_in=expires_in,
        )

    return (
        f"Upload link for `{rel}` (expires {_expiry_line(row)}):\n\n"
        f"{base}/transfer/upload#{token}\n\n"
        f"upload_id: {row.public_id}\n"
        f"max_bytes: {settings.max_file_write_bytes:,}\n"
        f"overwrite: {overwrite}\n\n"
        "Give the URL to the person you are helping and ask them to open it — "
        "it is a page with a file picker. Treat it as a secret: anyone holding "
        "it can write this one path once, until it expires. From a shell you "
        "can upload directly instead:\n\n"
        f'  curl -H "Authorization: Bearer <the part after the #>" '
        f'-T <file> {base}/transfer/upload\n\n'
        f"Then call `check_upload(\"{row.public_id}\")` to confirm the bytes "
        "landed and get their sha256. Do not paste the token into a query "
        "string — that would put it in access logs."
    )


def _loggable_upload_id(value) -> str:
    """What `check_upload` logs in place of a malformed `upload_id`.

    An `upload_id` is 22 characters of URL-safe base64 and nothing else. An
    agent that passes the whole `…/transfer/upload#<token>` URL, or the token
    itself, would otherwise put a live capability into `usage_logs` — a table
    the panel renders. Anything off-shape is logged as a fixed marker, so the
    log records *that* the tool was misused without recording the secret.
    """
    return value if transfer.is_public_id(value) else "<invalid>"


@_tracked(
    "check_upload", ["upload_id"], transforms={"upload_id": _loggable_upload_id}
)
async def check_upload_impl(upload_id: str) -> str:
    """Report the state of an upload link this identity minted."""
    if not transfer.is_public_id(upload_id):
        # Refused before the lookup *and* before `_tracked` logs it. The
        # message deliberately does not echo the value back: it may be the
        # token, and the tool result is itself model context.
        return (
            "not found: that is not an upload_id. `check_upload` takes the "
            "`upload_id` from `request_upload` (22 characters), not the upload "
            "URL and not the token after the `#`."
        )
    identity = _transfer_identity()
    async with async_session() as session:
        row = await transfer.lookup_by_public_id(
            session, upload_id, identity=identity, direction="upload"
        )
    if row is None:
        # Also the answer for another identity's upload_id: an agent must not
        # be able to probe for handles it did not mint.
        return f"not found: no upload link with id {upload_id} was minted by this identity."

    if row.state == "completed":
        when = row.completed_at.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
        return (
            f"completed: {row.path}\n"
            f"size: {row.size:,} bytes\n"
            f"sha256: {row.sha256}\n"
            f"mime: {row.mime}\n"
            f"completed_at: {when}"
        )
    if row.expires_at.astimezone(timezone.utc) <= datetime.now(timezone.utc):
        return (
            f"expired: the link for {row.path} was never used and can no longer "
            "be redeemed. Call `request_upload` again for a fresh one."
        )
    if row.state == "consumed":
        return (
            f"expired: the upload of {row.path} was cut short (it stalled or ran "
            "past its deadline) and the link is spent. Call `request_upload` "
            "again for a fresh one."
        )
    if row.state == "claimed":
        return (
            f"uploading: someone is sending {row.path} right now. Check again in "
            "a moment. If this persists for more than a few minutes the transfer "
            "died mid-flight — mint a new link with `request_upload`; this one "
            "will not complete."
        )
    return (
        f"pending: nothing has been uploaded to {row.path} yet. The link is "
        f"valid until {_expiry_line(row)}."
    )


@_tracked("request_download", ["path", "expires_in"])
async def request_download_impl(path: str, expires_in: int | None = None) -> str:
    """Mint a link a human can download one vault file from."""
    pre = _mint_preflight(path, need_write=False)
    if isinstance(pre, str):
        return pre
    uid, root, rel, base = pre

    try:
        fingerprint = _fingerprint_of(root, rel)
    except vault_fs.UnsafePath as e:
        # Covers both a symlink and a directory: neither is a file we will
        # hand out, and the message says which.
        return str(e)
    if fingerprint is None:
        return f"File not found: {rel}. Nothing was minted."

    try:
        head = _head_bytes(root, rel)
    except OSError as e:
        return f"Could not read {rel}: {e}. Nothing was minted."
    _kind, mime = classify_bytes(head, PurePosixPath(rel).name)

    async with async_session() as session:
        token, row = await transfer.mint_token(
            session,
            "download",
            rel,
            overwrite=False,
            identity=_transfer_identity(),
            vault_root=root,
            expected_fingerprint=fingerprint,
            expires_in=expires_in,
        )

    return (
        f"Download link for `{rel}` (expires {_expiry_line(row)}):\n\n"
        f"{base}/transfer/download#{token}\n\n"
        f"size: {fingerprint['size']:,} bytes\n"
        f"mime: {mime}\n\n"
        "Give the URL to the person you are helping — it is a page with a save "
        "button, and it keeps working until it expires. Treat it as a secret: "
        "anyone holding it can read this one file. From a shell:\n\n"
        f'  curl -H "Authorization: Bearer <the part after the #>" '
        f'-o <file> {base}/transfer/download/file\n\n'
        "The link is bound to the file as it is right now; if it is edited or "
        "replaced the link stops working and you should mint a new one."
    )


def _head_bytes(root: str, rel_path: str, count: int = 8192) -> bytes:
    """First bytes of a vault file, read through anchored descriptors."""
    root_fd = vault_fs.open_root(root)
    try:
        dir_fd, name = vault_fs.open_parent(root_fd, rel_path, create=False)
    finally:
        os.close(root_fd)
    try:
        fd = os.open(name, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW, dir_fd=dir_fd)
    finally:
        os.close(dir_fd)
    try:
        return os.read(fd, count)
    finally:
        os.close(fd)


def _url_host(url) -> str:
    """What `import_from_url` logs in place of the URL.

    A URL is caller-supplied and can carry a credential in its query string, so
    only the host goes to `usage_logs` — enough to audit where the server was
    made to connect, not enough to replay it.
    """
    try:
        return urlsplit(str(url)).hostname or "<no host>"
    except ValueError:
        return "<unparsable>"


@_tracked(
    "import_from_url", ["url", "path", "overwrite"], transforms={"url": _url_host}
)
async def import_from_url_impl(url: str, path: str, overwrite: bool = False) -> str:
    """Fetch a public URL straight into the vault, under the outbound policy."""
    pre = _mint_preflight(path, need_write=True)
    if isinstance(pre, str):
        return pre
    _uid, root, rel, _base = pre

    try:
        fingerprint = _fingerprint_of(root, rel)
    except vault_fs.UnsafePath as e:
        return str(e)
    if fingerprint is not None and not overwrite:
        return (
            f"File already exists: {rel}. Pass overwrite=True to replace it. "
            "Nothing was fetched."
        )

    # The four fields `stream_to_vault` reads. Not a token row: an import is
    # authenticated by the caller's own MCP identity, so there is no capability
    # to mint — but there is still an identity to re-validate, because the
    # fetch can run for 30 s and the key can die inside that window.
    row = SimpleNamespace(
        vault_root=root,
        path=rel,
        overwrite=overwrite,
        expected_fingerprint=fingerprint if overwrite else None,
    )
    cap = settings.max_file_write_bytes
    identity = _transfer_identity()

    @asynccontextmanager
    async def gate():
        """Lock this caller's own credential and user rows across the publish.

        Same guarantee the upload route's token gate gives, for the tool that
        has no token: a revocation, downgrade, deletion or root reassignment
        committed while the body streams either waits for these locks or beats
        us to them, and in the second case nothing is published.
        """
        async with async_session() as session:
            async with transfer.lock_identity_for_publish(
                session, identity, vault_root=root, need_write=True
            ) as handle:
                yield handle

    try:
        async with transfer.fetch_url_guarded(url, max_bytes=cap) as fetched:
            written = await transfer.stream_to_vault(
                row,
                fetched.chunks,
                max_bytes=cap,
                deadline=time.monotonic() + transfer.DEFAULT_FETCH_DEADLINE,
                idle_timeout=30.0,
                before_publish=gate,
            )
            final_url = fetched.final_url
    except transfer.SSRFError as e:
        return f"Refused to fetch that URL: {e}"
    except transfer.TooLarge as e:
        return f"{e}. Nothing was written."
    except transfer.Timeout as e:
        return f"{e}. Nothing was written."
    except transfer.PrePublishAborted:
        return (
            f"Your credentials are no longer valid for writing to {rel} (the key "
            "was revoked, downgraded, or repointed while the fetch was in "
            "flight). Nothing was written."
        )
    except transfer.PostPublishFailure as e:
        # The one outcome where "failed" would be a lie. The bytes are at
        # `rel`; only the bookkeeping around them did not finish. An agent told
        # "could not write" retries, and a retry of an import that already
        # landed is either a redundant fetch or — with overwrite — a second
        # write over the first. Say what is actually true instead.
        return (
            f"Imported the file to {rel}, but the server could not finish "
            f"recording the import: {e}\n"
            "The file IS in place. Do not retry blindly — check it with "
            "`read_file` or `list_files` first."
        )
    except vault_fs.Conflict as e:
        return f"{e}. Nothing was written."
    except vault_fs.UnsafePath as e:
        return f"{e}. Nothing was written."
    except OSError as e:
        return f"Could not write {rel}: {e}"

    return (
        f"Imported {written['size']:,} bytes to {rel}\n"
        f"sha256: {written['sha256']}\n"
        f"mime: {written['mime']}\n"
        f"source: {final_url}"
    )


@_tracked("delete_file", ["path", "permanent"])
async def delete_file_impl(path: str, permanent: bool = False) -> str:
    """Delete a non-markdown vault file, soft by default."""
    if err := _require_write():
        return err
    uid = current_user_id.get()
    try:
        root, rel = _vault_context(path, uid)
    except (ValueError, RuntimeError) as e:
        return str(e)

    # **Canonicalise first, then refuse.** The markdown guard has to run on the
    # component the filesystem will actually open, because the caller's string
    # and that component are not the same thing: `note.md/.`, `note.md/` and
    # `a//note.md` all reach a `.md` file while failing a naive
    # `path.lower().endswith(".md")`, which is how a note gets deleted by the
    # tool that does not know about the index or the backlink graph.
    if PurePosixPath(rel).name.lower().endswith(".md"):
        return (
            f"{rel} is a markdown note. Use `delete_note` for notes — it is the "
            "tool that knows about the index and about backlinks. `delete_file` "
            "handles everything else."
        )

    if not permanent:
        # Only the soft delete needs the trash to be usable; `permanent=True` is
        # a plain unlink, and probing for it would create `.trash` for a caller
        # who explicitly asked not to use it.
        try:
            vault_fs.check_trash_support(root)
        except vault_fs.UnsupportedFilesystem as e:
            return str(e)
        except (OSError, vault_fs.VaultFSError) as e:
            return f"Vault root is not usable: {e}"

    root_fd = vault_fs.open_root(root)
    try:
        if permanent:
            vault_fs.remove(root_fd, rel)
            return f"Permanently deleted {rel}"
        dest = vault_fs.soft_delete(root_fd, rel)
    except FileNotFoundError:
        return f"File not found: {rel}"
    except vault_fs.UnsafePath as e:
        # A symlink or a directory. Neither is something to delete on the
        # strength of a path an agent chose.
        return str(e)
    except vault_fs.Conflict as e:
        return f"{e}. Nothing was deleted."
    except vault_fs.VaultFSError as e:
        return str(e)
    finally:
        # Bare, this close raising `EIO` would discard the return value of a
        # delete that already happened and surface as a generic OSError.
        vault_fs.close_quietly(root_fd, f"vault root for {rel}")
    return (
        f"Moved {rel} to {dest}. It is out of the vault's visible tree but still "
        "on disk; pass permanent=True to unlink instead."
    )
