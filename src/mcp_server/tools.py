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
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path, PurePosixPath

from mcp.server.fastmcp import Image
from sqlalchemy import text

from src.auth.session import current_user_id
from src.config import settings
from src.database import async_session
from src.mcp_server.auth import current_api_key_id, current_oauth_token_id, current_permission
from src.models.db import UsageLog
from src.services.embeddings import semantic_search
from src.services.filters import apply_note_filters
from src.services.search import full_text_search
from src.services.vault import (
    classify_bytes,
    extract_section,
    list_dir,
    outline_sections,
    read_bytes,
    read_file,
    write_bytes,
    write_file,
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


def _tracked(tool_name: str, param_keys: list[str]):
    """Decorator that times the call and logs it to usage_logs."""
    def decorator(fn):
        sig = inspect.signature(fn)

        @wraps(fn)
        async def wrapper(*args, **kwargs):
            start = time.monotonic()
            result = await fn(*args, **kwargs)
            duration_ms = int((time.monotonic() - start) * 1000)
            params = {}
            # Resolve logged params by NAME via the wrapped signature so that
            # a non-logged positional arg between logged ones can't shift the
            # mapping (positional zipping silently mislabelled params before).
            try:
                bound = sig.bind(*args, **kwargs)
                bound.apply_defaults()
                params = {
                    key: bound.arguments[key]
                    for key in param_keys
                    if key in bound.arguments
                }
            except TypeError:
                params = {}
            await _log_usage(tool_name, _truncate_params(params), duration_ms, len(str(result)))
            return result
        return wrapper
    return decorator


@_tracked("search_notes", ["query", "folder", "limit", "tags", "frontmatter"])
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


def _outline_text(content: str, cap: int) -> str | None:
    """Render a navigable heading outline, or None if there are no headings."""
    sections = outline_sections(content)
    if not sections:
        return None
    seen = Counter(s["text"] for s in sections)
    lines = []
    for s in sections:
        marker = "#" * s["depth"]
        flag = "" if s["size"] <= cap else "  ⚠ over the cap, will page"
        # A repeated heading can only be addressed by its ordinal — the
        # path-style form cannot separate duplicate siblings.
        dup = "  ← duplicate title, use the ordinal" if seen[s["text"]] > 1 else ""
        lines.append(
            f"- `#{s['ordinal']}` `{marker} {s['text']}` "
            f"({s['size']:,} chars){flag}{dup}"
        )
    return "\n".join(lines)


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
                '(`section="#7"`), which always works even when titles repeat:\n'
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


MAX_NOTE_BYTES = 10 * 1024 * 1024  # 10 MB


@_tracked("create_note", ["path"])
async def create_note_impl(path: str, content: str) -> str:
    """Create a new note in the vault."""
    if err := _require_write():
        return err
    encoded = content.encode("utf-8")
    if len(encoded) > MAX_NOTE_BYTES:
        return f"Content too large ({len(encoded)} bytes, max {MAX_NOTE_BYTES})"
    if not path.endswith(".md"):
        path += ".md"
    uid = current_user_id.get()
    try:
        write_file(path, content, user_id=uid, overwrite=False)
        return f"Created note: {path}"
    except FileExistsError:
        return f"Note already exists: {path}. Use edit_note to modify it."
    except ValueError as e:
        return str(e)


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


@_tracked("find_related", ["path", "limit"])
async def find_related_impl(path: str, limit: int = 10) -> str:
    """Semantic neighbors via averaged chunk embeddings."""
    import numpy as np
    from sqlalchemy import select
    from src.models.db import NoteEmbedding, NoteMetadata

    uid = current_user_id.get()
    limit = max(1, min(limit, 50))

    async with async_session() as session:
        src_stmt = select(NoteMetadata).where(NoteMetadata.file_path == path)
        if uid is not None:
            src_stmt = src_stmt.where(NoteMetadata.user_id == uid)
        source = (await session.execute(src_stmt)).scalar_one_or_none()
        if source is None:
            return f"Note not found: {path}"

        chunks = (await session.execute(
            select(NoteEmbedding.embedding).where(NoteEmbedding.note_id == source.id)
        )).scalars().all()
        if not chunks:
            return (
                f"`{path}` has not been embedded yet — "
                "the indexer is still catching up. Try again in a few minutes."
            )

        avg = np.mean([np.asarray(c, dtype=float) for c in chunks], axis=0)
        avg_list = avg.tolist()

        # Same HNSW tuning as semantic_search — see embeddings.py for context.
        await session.execute(text("SET LOCAL hnsw.ef_search = 80"))
        await session.execute(text("SET LOCAL random_page_cost = 1.1"))

        # Pull more than `limit` so we can dedupe by note.
        stmt = (
            select(
                NoteEmbedding.note_id,
                NoteEmbedding.chunk_text,
                NoteEmbedding.embedding,
                NoteMetadata.file_path,
                NoteMetadata.title,
                NoteMetadata.tags,
            )
            .join(NoteMetadata, NoteEmbedding.note_id == NoteMetadata.id)
            .where(NoteEmbedding.note_id != source.id)
            .order_by(NoteEmbedding.embedding.cosine_distance(avg_list))
            .limit(limit * 5)
        )
        if uid is not None:
            stmt = stmt.where(NoteMetadata.user_id == uid)
        rows = (await session.execute(stmt)).all()

    if not rows:
        return f"No related notes for `{path}`"

    # Dedupe by note_id, keeping the highest-similarity chunk.
    avg_norm = float(np.linalg.norm(avg)) or 1.0
    best: dict[int, dict] = {}
    for r in rows:
        emb = np.asarray(r.embedding, dtype=float)
        sim = float(np.dot(emb, avg) / ((np.linalg.norm(emb) or 1.0) * avg_norm))
        prev = best.get(r.note_id)
        if prev is None or sim > prev["similarity"]:
            best[r.note_id] = {
                "path": r.file_path,
                "title": r.title,
                "tags": r.tags,
                "similarity": sim,
                "chunk": r.chunk_text,
            }

    ranked = sorted(best.values(), key=lambda x: x["similarity"], reverse=True)[:limit]
    lines = [f"Top {len(ranked)} related notes for `{path}`:\n"]
    for r in ranked:
        tags_str = f" [{', '.join(r['tags'])}]" if r["tags"] else ""
        snippet = r["chunk"].replace("\n", " ")[:200]
        lines.append(
            f"- **{r['title']}** (`{r['path']}`){tags_str} — sim: {r['similarity']:.3f}"
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
        from src.services.vault import replace_section, validate_visible_path
        full_path = validate_visible_path(path, user_id=uid)
    except ValueError as e:
        return str(e)
    if not full_path.exists():
        return f"Note not found: {path}. Use create_note to create it."

    try:
        existing_bytes = read_bytes(path, user_id=uid, max_bytes=MAX_NOTE_BYTES)
        existing = existing_bytes.decode("utf-8")
    except Exception as e:
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
        write_file(path, new_content, user_id=uid, expected=existing_bytes)
    except (ValueError, RuntimeError) as e:
        return str(e)
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
    from src.services.vault import _vault_root, move_no_clobber, validate_visible_path

    uid = current_user_id.get()
    try:
        src_full = validate_visible_path(from_path, user_id=uid)
        dst_full = validate_visible_path(to_path, user_id=uid)
    except ValueError as e:
        return str(e)
    if not src_full.is_file():
        return f"Source note not found: {from_path}"
    vault = _vault_root(uid).resolve()
    from_rel = src_full.resolve().relative_to(vault).as_posix()
    to_rel = dst_full.resolve().relative_to(vault).as_posix()

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
    failed_rewrite_sources: list[str] = []
    if rewrite_links and pre_move_index is not None:
        for original_src_path in rewrite_sources:
            try:
                # A moved note may link to itself. Read it at its new location,
                # but resolve its old link text in the pre-move location.
                src_path = (
                    to_rel if original_src_path == from_rel else original_src_path
                )
                src_file = validate_visible_path(src_path, user_id=uid)
                if not src_file.is_file():
                    continue
                original_bytes = read_bytes(
                    src_path, user_id=uid, max_bytes=MAX_NOTE_BYTES
                )
                content = original_bytes.decode("utf-8")
                new_content, n = _rewrite_links_in_text(
                    content,
                    from_rel,
                    to_rel,
                    original_src_path,
                    pre_move_index,
                    output_source_path=src_path,
                )
                if n > 0:
                    write_file(
                        src_path, new_content, user_id=uid, expected=original_bytes
                    )
                    rewrites_done += n
                    files_modified += 1
            except Exception as e:
                logger.warning("Failed to rewrite links in %s: %s", src_path, e)
                failed_rewrite_sources.append(src_path)

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

    from src.services.vault import _vault_root, move_no_clobber, validate_visible_path

    uid = current_user_id.get()
    try:
        full_path = validate_visible_path(path, user_id=uid)
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

    from src.services.vault import (
        parse_frontmatter,
        serialize_frontmatter,
        validate_visible_path,
    )

    uid = current_user_id.get()
    try:
        full_path = validate_visible_path(path, user_id=uid)
    except ValueError as e:
        return str(e)
    if not full_path.is_file():
        return f"Note not found: {path}"

    if not updates and not remove:
        return f"No changes for {path} (empty updates and remove)"

    try:
        raw_bytes = read_bytes(path, user_id=uid, max_bytes=MAX_NOTE_BYTES)
        raw = raw_bytes.decode("utf-8")
    except Exception as e:
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

    try:
        write_file(path, new_raw, user_id=uid, expected=raw_bytes)
    except (ValueError, RuntimeError) as e:
        return str(e)

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

    uid = current_user_id.get()
    try:
        write_bytes(path, data, overwrite=overwrite, user_id=uid)
    except FileExistsError:
        return f"File already exists: {path}. Pass overwrite=True to replace it."
    except ValueError as e:
        return str(e)
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
