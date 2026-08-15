import asyncio
import fnmatch
import hashlib
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import delete, func, literal, or_, select, text
from sqlalchemy.dialects.postgresql import insert

from src.config import settings
from src.database import async_session
from src.models.db import NoteEmbedding, NoteLink, NoteMetadata, OAuthCode, OAuthToken, User
from src.services.embeddings import embed_note
from src.services.fts import index_tsvector_sql
from src.services.links import build_vault_index, extract_links, resolve_target
from src.services.vault import (
    _vault_root,
    extract_tags,
    parse_frontmatter,
    warm_user_vault_cache,
)

# Module-level flag the dashboard reads to surface "link extraction in
# progress" while the one-shot backfill is running.
link_backfill_in_progress: bool = False

# Serializes full index/embed passes so the periodic loop and a
# panel-triggered on-demand reindex can never run index_vault/embed_vault
# concurrently for the same scope. Two overlapping passes share no DB lock and
# would race on move-detection, deleted-path removal, and per-note embedding
# delete+insert (duplicate-key errors, lost/duplicated rows). Both
# `run_indexer_loop` and `_reindex_background` acquire this before doing work.
index_pass_lock: asyncio.Lock = asyncio.Lock()


def _sanitize_value(v):
    """Recursively coerce a frontmatter value into a JSON-serializable form.

    Lists and dicts are walked element-by-element; non-string dict keys and
    any non-serializable scalar (e.g. a YAML date/datetime) are stringified.
    """
    if isinstance(v, (str, int, float, bool, type(None))):
        return v
    elif isinstance(v, list):
        return [_sanitize_value(i) for i in v]
    elif isinstance(v, dict):
        return {
            (k if isinstance(k, str) else str(k)): _sanitize_value(val)
            for k, val in v.items()
        }
    else:
        return str(v)


def _sanitize_frontmatter(fm: dict) -> dict:
    """Convert non-JSON-serializable values (dates, etc) to strings."""
    return _sanitize_value(fm)

logger = logging.getLogger(__name__)


def _content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


async def index_vault(user_id: int | None = None):
    """Scan vault, upsert notes_metadata with tsvector, remove deleted files.

    Single-user mode (`user_id is None`) keeps the legacy behavior: queries
    and inserts do not filter by `user_id` (NULL passes through every guard).
    Multi-user mode (`user_id` int) scopes existing-row lookups and stamps
    `user_id` on every upserted row.
    """
    vault = _vault_root(user_id)
    log_suffix = f" (user_id={user_id})" if user_id is not None else ""
    logger.info(f"Starting vault index scan...{log_suffix}")

    # Collect all .md files (skip dot-dirs)
    files: dict[str, Path] = {}
    for p in vault.rglob("*.md"):
        rel = p.relative_to(vault)
        if any(part.startswith(".") for part in rel.parts):
            continue
        files[str(rel)] = p

    logger.info(f"Found {len(files)} markdown files{log_suffix}")

    async with async_session() as session:
        # Get existing hashes (scoped to this user when set)
        existing_stmt = select(NoteMetadata.file_path, NoteMetadata.content_hash)
        if user_id is None:
            existing_stmt = existing_stmt.where(NoteMetadata.user_id.is_(None))
        else:
            existing_stmt = existing_stmt.where(NoteMetadata.user_id == user_id)
        result = await session.execute(existing_stmt)
        existing = {row.file_path: row.content_hash for row in result.fetchall()}

        # Determine changes
        to_upsert = []
        # Body text parsed during this scan, keyed by rel_path. The tsvector
        # loop below reuses these instead of re-reading from disk — a concurrent
        # delete between the two passes would otherwise raise FileNotFoundError
        # and leave the just-committed row's content_tsvector null/stale.
        path_to_content: dict[str, str] = {}
        for rel_path, full_path in files.items():
            try:
                raw = full_path.read_text(encoding="utf-8", errors="strict")
            except UnicodeDecodeError:
                logger.warning(f"Skipping non-UTF8 file: {rel_path}")
                continue
            except Exception as e:
                logger.warning(f"Failed to read {rel_path}: {e}")
                continue

            h = _content_hash(raw)
            if rel_path in existing and existing[rel_path] == h:
                continue  # No change

            frontmatter, content = parse_frontmatter(raw)
            path_to_content[rel_path] = content
            title = frontmatter.get("title") or full_path.stem
            tags = extract_tags(raw, frontmatter)
            stat = full_path.stat()

            to_upsert.append({
                "user_id": user_id,
                "file_path": rel_path,
                "title": title,
                "tags": tags,
                "frontmatter": _sanitize_frontmatter(frontmatter),
                "content_hash": h,
                "file_size": stat.st_size,
                "modified_at": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
            })

        # Compute deleted paths up front so the move-detection block can
        # repair them before the delete/insert pipeline tears them apart.
        deleted_paths = set(existing.keys()) - set(files.keys())

        # ── Move detection ────────────────────────────────────────────────
        # An external move (file dragged in Obsidian) looks like
        # "delete old + insert new" from a path-only POV. Pair deleted paths
        # with genuinely-new paths sharing the same content_hash and update
        # `file_path` in place — preserving the row id keeps embeddings,
        # incoming `note_links.target_note_id` refs, and avoids a dangling-
        # link window. Falls back to delete+insert when a hash matches more
        # than one path on either side (ambiguous — could be a real duplicate).
        new_by_hash: dict[str, list[str]] = {}
        for e in to_upsert:
            if e["file_path"] not in existing:
                new_by_hash.setdefault(e["content_hash"], []).append(e["file_path"])
        deleted_by_hash: dict[str, list[str]] = {}
        for p in deleted_paths:
            deleted_by_hash.setdefault(existing[p], []).append(p)

        moves: list[tuple[str, str]] = []
        for h, olds in deleted_by_hash.items():
            news = new_by_hash.get(h, [])
            if len(olds) == 1 and len(news) == 1:
                moves.append((olds[0], news[0]))

        moved_new_paths: set[str] = set()
        if moves:
            user_clause = "user_id IS NULL" if user_id is None else "user_id = :uid"
            move_upd_sql = (
                "UPDATE notes_metadata "
                "SET file_path = :new, file_size = :size, "
                "modified_at = :mtime, indexed_at = now() "
                f"WHERE file_path = :old AND {user_clause}"
            )
            # Rewrite stored `target_path` strings that referenced the old
            # path. Two forms: the full path (`Folder/Old.md`) and the
            # extension-stripped form (`Folder/Old`) the extractor stores for
            # markdown links. Bare-name `[[noteName]]` references survive
            # untouched — their `target_note_id` is preserved via id reuse
            # and the stem doesn't change on a folder-only move.
            move_tp_sql = (
                "UPDATE note_links SET target_path = :new "
                "WHERE target_path = :old "
                f"AND source_note_id IN (SELECT id FROM notes_metadata WHERE {user_clause})"
            )

            entry_by_path = {e["file_path"]: e for e in to_upsert}
            for old, new in moves:
                e = entry_by_path[new]
                params: dict = {
                    "new": new, "old": old,
                    "size": e["file_size"], "mtime": e["modified_at"],
                }
                if user_id is not None:
                    params["uid"] = user_id
                await session.execute(text(move_upd_sql), params)

                old_no_ext = old[:-3] if old.endswith(".md") else old
                new_no_ext = new[:-3] if new.endswith(".md") else new
                for o, n in [(old, new), (old_no_ext, new_no_ext)]:
                    tp_params: dict = {"new": n, "old": o}
                    if user_id is not None:
                        tp_params["uid"] = user_id
                    await session.execute(text(move_tp_sql), tp_params)

                moved_new_paths.add(new)

            logger.info(
                f"Detected {len(moves)} file move(s) — preserved ids{log_suffix}"
            )

            # The existing delete+insert pipeline below mustn't touch the
            # paths we just repaired in place.
            to_upsert = [e for e in to_upsert if e["file_path"] not in moved_new_paths]
            deleted_paths -= {old for old, _ in moves}

        # Upsert changed files
        if to_upsert:
            for batch_start in range(0, len(to_upsert), 100):
                batch = to_upsert[batch_start:batch_start + 100]
                stmt = insert(NoteMetadata).values(batch)
                stmt = stmt.on_conflict_do_update(
                    # Match the composite UNIQUE(user_id, file_path) on
                    # notes_metadata (migration 009). The constraint is
                    # declared NULLS NOT DISTINCT so single-user-mode
                    # rows (user_id IS NULL) still collide and upsert
                    # correctly. Without NULLS NOT DISTINCT, PG 15+ would
                    # treat each NULL user_id as distinct and silently
                    # duplicate rows on every indexer pass.
                    index_elements=["user_id", "file_path"],
                    set_={
                        "title": stmt.excluded.title,
                        "tags": stmt.excluded.tags,
                        "frontmatter": stmt.excluded.frontmatter,
                        "content_hash": stmt.excluded.content_hash,
                        "file_size": stmt.excluded.file_size,
                        "modified_at": stmt.excluded.modified_at,
                        "indexed_at": text("now()"),
                    },
                )
                await session.execute(stmt)
            logger.info(f"Upserted {len(to_upsert)} notes")

        # Update tsvectors for changed notes
        if to_upsert:
            paths = [n["file_path"] for n in to_upsert]
            # In multi-user mode the same `file_path` can exist for multiple
            # users, so the UPDATE scopes by user: `user_id IS NULL` in
            # single-user mode, `user_id = :uid` (never NULL) in multi-user
            # mode. The tsvector expression is built from `settings.fts_configs`
            # (see `src/services/fts.py`) so index-time configs match the
            # query-time configs in `search.py`.
            tsv_frag, tsv_params = index_tsvector_sql("content")
            if user_id is None:
                tsv_sql = f"""
                    UPDATE notes_metadata
                    SET content_tsvector = {tsv_frag}
                    WHERE file_path = :path
                      AND user_id IS NULL
                """
            else:
                tsv_sql = f"""
                    UPDATE notes_metadata
                    SET content_tsvector = {tsv_frag}
                    WHERE file_path = :path
                      AND user_id = :uid
                """
            for path in paths:
                # Reuse the body parsed during the scan loop above instead of
                # re-reading from disk; a concurrent delete between the passes
                # would otherwise leave content_tsvector null/stale (issue #18).
                if path not in path_to_content:
                    continue
                content = path_to_content[path]
                try:
                    params: dict = {"content": content[:100000], "path": path, **tsv_params}
                    if user_id is not None:
                        params["uid"] = user_id
                    await session.execute(text(tsv_sql), params)
                except Exception:
                    logger.exception(f"Failed to update tsvector for {path}")
                    raise
            logger.info(f"Updated tsvectors for {len(paths)} notes{log_suffix}")

        # Remove deleted files (scoped to this user when set). `deleted_paths`
        # was computed earlier and any entries that turned out to be moves
        # have already been stripped out by the move-detection block above.
        if deleted_paths:
            del_stmt = delete(NoteMetadata).where(
                NoteMetadata.file_path.in_(deleted_paths)
            )
            if user_id is None:
                del_stmt = del_stmt.where(NoteMetadata.user_id.is_(None))
            else:
                del_stmt = del_stmt.where(NoteMetadata.user_id == user_id)
            await session.execute(del_stmt)
            logger.info(f"Removed {len(deleted_paths)} deleted notes{log_suffix}")

        # ── Link extraction for changed notes ───────────────────────────
        # We rebuild the vault_index here (post-commit), then for each
        # changed note delete-and-reinsert its rows in `note_links`. New or
        # renamed notes also get a re-resolution pass that updates any
        # previously-dangling rows now matching their path.
        # Moved notes need outgoing-link re-extraction too: same-folder
        # resolution can change once the note sits in a different directory.
        if to_upsert or deleted_paths or moved_new_paths:
            await _update_links_for_changed(
                session,
                vault,
                [n["file_path"] for n in to_upsert] + list(moved_new_paths),
                user_id=user_id,
            )

        # Metadata hashes, keyword vectors, deletions, and link rows describe
        # one filesystem snapshot. Commit them together so a failure in a
        # later stage cannot leave a new hash paired with stale search data
        # (which would make the next scan incorrectly skip the note).
        await session.commit()

    logger.info(f"Vault index scan complete{log_suffix}")


async def _update_links_for_changed(
    session,
    vault: Path,
    changed_paths: list[str],
    user_id: int | None = None,
):
    """Re-extract and upsert links for the given changed paths.

    Builds a fresh `vault_index` from `notes_metadata`, then for every changed
    note: deletes existing rows, extracts links, resolves targets, inserts.
    Finally, runs a re-resolution pass to attach previously-dangling rows
    whose `target_path` matches any of the changed notes.

    In multi-user mode the vault_index is scoped to `user_id` so a user's
    wikilinks cannot resolve to another user's note (they share the same
    `file_path` string but live in distinct `notes_metadata.id`s).
    """
    # Build vault_index once for the entire pass — scoped to this user when set.
    vi_stmt = select(NoteMetadata.file_path, NoteMetadata.id)
    if user_id is None:
        vi_stmt = vi_stmt.where(NoteMetadata.user_id.is_(None))
    else:
        vi_stmt = vi_stmt.where(NoteMetadata.user_id == user_id)
    rows = (await session.execute(vi_stmt)).all()
    vault_index = build_vault_index([(r.file_path, r.id) for r in rows])
    paths_to_id: dict[str, int] = vault_index["paths"]

    if changed_paths:
        # Process changed notes' outgoing links.
        change_ids = [paths_to_id[p] for p in changed_paths if p in paths_to_id]
        if change_ids:
            await session.execute(
                delete(NoteLink).where(NoteLink.source_note_id.in_(change_ids))
            )
            new_rows: list[dict] = []
            for path in changed_paths:
                src_id = paths_to_id.get(path)
                if src_id is None:
                    continue
                full_path = vault / path
                try:
                    raw = full_path.read_text(encoding="utf-8", errors="strict")
                except (UnicodeDecodeError, FileNotFoundError, OSError):
                    continue
                _, content = parse_frontmatter(raw)
                for link in extract_links(content):
                    target_id = resolve_target(link.target, path, vault_index)
                    new_rows.append({
                        "source_note_id": src_id,
                        "target_note_id": target_id,
                        "target_path": link.target[:1024],
                        "link_text": link.link_text,
                        "kind": link.kind,
                        "position": link.position,
                    })
            if new_rows:
                for batch_start in range(0, len(new_rows), 1000):
                    await session.execute(
                        insert(NoteLink).values(
                            new_rows[batch_start:batch_start + 1000]
                        )
                    )
            logger.info(
                f"Re-extracted links for {len(change_ids)} notes "
                f"({len(new_rows)} link rows)"
            )

    # Re-resolution pass: any newly-arrived note may resolve previously
    # dangling rows. We patch `target_note_id` for rows whose `target_path`
    # matches one of the changed paths in a few canonical forms.
    #
    # In multi-user mode we restrict the UPDATE to rows whose source note
    # belongs to the same user — otherwise alice's newly-created `foo.md`
    # would silently get attached as the target of bob's dangling
    # `[[foo]]` link.
    #
    # The bare-stem form (`[[Foo]]`) is only safe to match when exactly one
    # note in the vault carries that stem. With a shared stem the resolver
    # (`resolve_target`) uses same-folder preference and an alphabetical
    # tie-break, so a blind `target_path = stem` match here would mis-attach
    # dangling rows that belong to a *different* note. Ambiguous stems stay
    # dangling and resolve later when their own source note is reindexed.
    stems: dict[str, list[tuple[str, int]]] = vault_index["stems"]
    for path in changed_paths:
        nid = paths_to_id.get(path)
        if nid is None:
            continue
        stem = os.path.splitext(os.path.basename(path))[0]
        path_no_ext = path[:-3] if path.endswith(".md") else path
        # Always-safe canonical forms keyed to the exact stored path.
        params: dict = {
            "nid": nid,
            "full": path,
            "no_ext": path_no_ext,
        }
        # Only fold in the bare stem when it maps to a single note.
        if len(stems.get(stem, [])) == 1:
            params["stem"] = stem
        in_clause = ", ".join(f":{p}" for p in ("full", "no_ext", "stem")
                              if p in params)
        where_extra = ""
        if user_id is None:
            where_extra = (
                " AND source_note_id IN ("
                "SELECT id FROM notes_metadata WHERE user_id IS NULL)"
            )
        else:
            params["uid"] = user_id
            where_extra = (
                " AND source_note_id IN ("
                "SELECT id FROM notes_metadata WHERE user_id = :uid)"
            )
        reresolve_sql = (
            "UPDATE note_links "
            "SET target_note_id = :nid "
            "WHERE target_note_id IS NULL "
            f"AND target_path IN ({in_clause})"
            f"{where_extra}"
        )
        await session.execute(text(reresolve_sql), params)


async def link_backfill_pass(user_id: int | None = None):
    """One-shot backfill that populates `note_links` for every note.

    Runs on startup when this user's graph has no rows. Rebuilds the graph in one transaction so a
    restart after any batch rolls back cleanly instead of mistaking a partial
    graph for a completed backfill.

    In multi-user mode each user's pass scopes its scan + vault_index to its
    own `notes_metadata` rows and replaces only links sourced by those rows.
    """
    global link_backfill_in_progress
    vault = _vault_root(user_id)
    async with async_session() as session:
        # Completion is inferred per user, never from the global table. The
        # rebuild itself commits atomically below, so any visible row proves a
        # prior pass for this scope completed (a zero-link vault is harmlessly
        # rescanned on the next startup).
        existing_stmt = (
            select(func.count(NoteLink.id))
            .join(NoteMetadata, NoteLink.source_note_id == NoteMetadata.id)
        )
        if user_id is None:
            existing_stmt = existing_stmt.where(NoteMetadata.user_id.is_(None))
        else:
            existing_stmt = existing_stmt.where(NoteMetadata.user_id == user_id)
        existing = (await session.execute(existing_stmt)).scalar() or 0
        if existing > 0:
            return

        rows_stmt = select(NoteMetadata.id, NoteMetadata.file_path)
        if user_id is None:
            rows_stmt = rows_stmt.where(NoteMetadata.user_id.is_(None))
        else:
            rows_stmt = rows_stmt.where(NoteMetadata.user_id == user_id)
        rows = (await session.execute(rows_stmt)).all()
        if not rows:
            return

        link_backfill_in_progress = True
        log_suffix = f" (user_id={user_id})" if user_id is not None else ""
        logger.info(f"Starting link backfill across {len(rows)} notes{log_suffix}")

        vault_index = build_vault_index([(r.file_path, r.id) for r in rows])

        try:
            note_ids = [r.id for r in rows]
            await session.execute(
                delete(NoteLink).where(NoteLink.source_note_id.in_(note_ids))
            )
            buffer: list[dict] = []
            for i, row in enumerate(rows, start=1):
                full_path = vault / row.file_path
                try:
                    raw = full_path.read_text(encoding="utf-8", errors="strict")
                except (UnicodeDecodeError, FileNotFoundError, OSError):
                    continue
                _, content = parse_frontmatter(raw)
                for link in extract_links(content):
                    target_id = resolve_target(link.target, row.file_path, vault_index)
                    buffer.append({
                        "source_note_id": row.id,
                        "target_note_id": target_id,
                        "target_path": link.target[:1024],
                        "link_text": link.link_text,
                        "kind": link.kind,
                        "position": link.position,
                    })
                if len(buffer) >= 1000:
                    await session.execute(insert(NoteLink).values(buffer))
                    buffer.clear()
                if i % 500 == 0:
                    logger.info(f"Link backfill: {i}/{len(rows)} notes")

            if buffer:
                await session.execute(insert(NoteLink).values(buffer))

            await session.commit()

            logger.info(f"Link backfill complete: {len(rows)} notes scanned")
        finally:
            link_backfill_in_progress = False


async def embed_vault(user_id: int | None = None):
    """Embed notes that don't have embeddings yet or have changed.

    Multi-user mode: only embeds notes belonging to `user_id`. Each note's
    embeddings go into `note_embeddings`, which inherits user scope via its
    `note_id` FK back to `notes_metadata`. No `user_id` column on
    `note_embeddings` itself.
    """
    vault = _vault_root(user_id)
    log_suffix = f" (user_id={user_id})" if user_id is not None else ""
    logger.info(f"Starting embedding pass...{log_suffix}")

    async with async_session() as session:
        # Find notes without embeddings or with stale embeddings, scoped
        # to this user when set. We bind the user_id parameter even in
        # single-user mode and compare with `IS NOT DISTINCT FROM` so the
        # NULL case still selects all rows without a separate branch.
        if user_id is None:
            sql = """
                SELECT nm.id, nm.file_path, nm.content_hash
                FROM notes_metadata nm
                WHERE nm.user_id IS NULL
                  AND (nm.embedded_content_hash IS NULL
                       OR nm.embedded_content_hash != nm.content_hash)
                ORDER BY nm.modified_at DESC
            """
            params: dict = {}
        else:
            sql = """
                SELECT nm.id, nm.file_path, nm.content_hash
                FROM notes_metadata nm
                WHERE nm.user_id = :uid
                  AND (nm.embedded_content_hash IS NULL
                       OR nm.embedded_content_hash != nm.content_hash)
                ORDER BY nm.modified_at DESC
            """
            params = {"uid": user_id}
        result = await session.execute(text(sql), params)
        unembedded = result.fetchall()

        if not unembedded:
            logger.info(f"All notes already embedded{log_suffix}")
            return

        logger.info(f"Embedding {len(unembedded)} notes...{log_suffix}")
        exclude_patterns = settings.embedding_exclude_patterns or []
        total_chunks = 0
        skipped_excluded = 0
        for i, row in enumerate(unembedded):
            # Re-check the pause flag every iteration so a panel-driven pause
            # (e.g. reset-embeddings) stops an in-flight embed pass promptly
            # instead of grinding through the whole backlog first (issue #19).
            if _is_paused():
                logger.info(f"Embedding pass paused, stopping early{log_suffix}")
                break
            try:
                # Skip files matching exclude patterns. Drop any pre-existing
                # embeddings (in case the file was indexed before exclusion was
                # configured) and stamp embedded_content_hash so the indexer
                # doesn't keep re-checking it.
                if any(fnmatch.fnmatch(row.file_path, pat) for pat in exclude_patterns):
                    await session.execute(
                        delete(NoteEmbedding).where(NoteEmbedding.note_id == row.id)
                    )
                    await session.execute(
                        text(
                            "UPDATE notes_metadata SET embedded_content_hash = :h "
                            "WHERE id = :i"
                        ),
                        {"h": row.content_hash, "i": row.id},
                    )
                    await session.commit()
                    skipped_excluded += 1
                    continue

                full_path = vault / row.file_path
                try:
                    raw = full_path.read_text(encoding="utf-8", errors="strict")
                except UnicodeDecodeError:
                    logger.warning(f"Skipping non-UTF8 file: {row.file_path}")
                    continue
                _, content = parse_frontmatter(raw)

                # Get the NoteMetadata object
                note_result = await session.execute(
                    select(NoteMetadata).where(NoteMetadata.id == row.id)
                )
                note = note_result.scalar_one()

                chunks = await embed_note(session, note, content)
                total_chunks += chunks
                await session.commit()

                if (i + 1) % 50 == 0:
                    logger.info(f"Embedded {i + 1}/{len(unembedded)} notes ({total_chunks} chunks)")
            except Exception as e:
                logger.warning(f"Failed to embed {row.file_path}: {e}")
                await session.rollback()

        logger.info(
            f"Embedding complete{log_suffix}: {len(unembedded)} notes, {total_chunks} chunks"
            + (f", {skipped_excluded} skipped by exclude patterns" if skipped_excluded else "")
        )


async def rebuild_tsvectors(session, user_id: int | None = None) -> int:
    """Recompute `content_tsvector` for every indexed note under the currently
    configured `FTS_CONFIGS` (see `src/services/fts.py`). Returns the count of
    notes updated.

    Run this after changing `FTS_CONFIGS`, since `notes_metadata` stores no raw
    body column — the tsvector must be rebuilt by re-reading each note's file.
    This rebuilds the KEYWORD index only: it does NOT touch embeddings and makes
    NO API calls, so it's cheap (seconds for a few thousand notes), unlike
    `reset-embeddings`.

    Scoped to `user_id` when set (multi-user mode); single-user mode passes
    `None` and rebuilds every note. Reuses `index_tsvector_sql` so the rebuilt
    tsvector is byte-identical to what the indexer would write for the same
    config(s).
    """
    vault = _vault_root(user_id)
    log_suffix = f" (user_id={user_id})" if user_id is not None else ""
    tsv_frag, tsv_params = index_tsvector_sql("content")
    upd_sql = text(
        f"UPDATE notes_metadata SET content_tsvector = {tsv_frag} WHERE id = :id"
    )

    rows_stmt = select(NoteMetadata.id, NoteMetadata.file_path)
    if user_id is None:
        rows_stmt = rows_stmt.where(NoteMetadata.user_id.is_(None))
    else:
        rows_stmt = rows_stmt.where(NoteMetadata.user_id == user_id)
    rows = (await session.execute(rows_stmt)).all()
    logger.info(f"Rebuilding tsvectors for {len(rows)} notes{log_suffix}")

    updated = 0
    for row in rows:
        full_path = vault / row.file_path
        try:
            raw = full_path.read_text(encoding="utf-8", errors="strict")
        except (UnicodeDecodeError, FileNotFoundError, OSError):
            continue
        _, content = parse_frontmatter(raw)
        await session.execute(
            upd_sql, {"content": content[:100000], "id": row.id, **tsv_params}
        )
        updated += 1
        if updated % 500 == 0:
            await session.commit()
            logger.info(f"rebuild_tsvectors: {updated}/{len(rows)} notes{log_suffix}")
    await session.commit()
    logger.info(f"rebuild_tsvectors complete: {updated} notes{log_suffix}")
    return updated


async def cleanup_expired_tokens():
    """Delete expired/revoked OAuth codes and tokens older than 7 days."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)

    async with async_session() as session:
        # Clean up expired/used auth codes
        result = await session.execute(
            delete(OAuthCode).where(
                or_(
                    OAuthCode.expires_at < cutoff,
                    OAuthCode.used == True,
                )
            )
        )
        codes_deleted = result.rowcount

        # Clean up expired/revoked tokens
        result = await session.execute(
            delete(OAuthToken).where(
                or_(
                    OAuthToken.expires_at < cutoff,
                    OAuthToken.revoked == True,
                )
            )
        )
        tokens_deleted = result.rowcount

        await session.commit()

        if codes_deleted or tokens_deleted:
            logger.info(f"Token cleanup: {codes_deleted} codes, {tokens_deleted} tokens removed")


def _is_paused() -> bool:
    """Check if a panel-driven action has paused the indexer."""
    try:
        from src.control_panel import routes as panel_routes
        return bool(getattr(panel_routes, "indexer_paused", False))
    except Exception:
        return False


async def _active_user_ids() -> list[int]:
    """Return ids of active users with a non-null `vault_path`. Empty list in
    single-user mode (the caller already takes the legacy NULL-user path)."""
    async with async_session() as session:
        # Warm the in-process vault-path cache for every active user before
        # the indexer kicks off — saves a per-user lookup later.
        await warm_user_vault_cache(session)
        result = await session.execute(
            select(User.id).where(
                User.is_active.is_(True),
                User.vault_path.isnot(None),
            )
        )
        return [row[0] for row in result.all()]


# One wall-clock bound for the whole pre-warm. It runs while holding
# `index_pass_lock`, so an unbounded hang would block the panel's reindex and
# reset-embeddings actions indefinitely.
PREWARM_TIMEOUT_SECONDS = 15.0

_HNSW_INDEX_NAME = "ix_note_embeddings_embedding_hnsw"

# Tri-state cache for "does an HNSW index exist on note_embeddings.embedding".
# None = not yet looked up. Deployments with EMBEDDING_DIMENSIONS > 2000 have
# no such index (pgvector's HNSW limit) and must not pay a sequential scan
# every five minutes just to warm a cache. `reset_embeddings` drops and
# recreates the index, so it invalidates this via `invalidate_hnsw_index_cache`.
_hnsw_index_present: bool | None = None


def invalidate_hnsw_index_cache() -> None:
    """Forget the cached `pg_indexes` lookup.

    Called by the panel's reset-embeddings action, which drops the HNSW index
    and only recreates it when the configured dimension allows one — so the
    cached answer can go stale in either direction.
    """
    global _hnsw_index_present
    _hnsw_index_present = None


async def _hnsw_index_exists(session) -> bool:
    global _hnsw_index_present
    if _hnsw_index_present is None:
        result = await session.execute(
            text(
                "SELECT 1 FROM pg_indexes "
                "WHERE tablename = 'note_embeddings' AND indexname = :name"
            ),
            {"name": _HNSW_INDEX_NAME},
        )
        _hnsw_index_present = result.first() is not None
    return _hnsw_index_present


def _probe_vector() -> list[float]:
    """A deterministic non-zero unit vector of `EMBEDDING_DIMENSIONS`.

    Non-zero matters: a zero vector has no cosine direction, so
    `embedding <=> '[0,...]'` is undefined and the scan would not traverse the
    graph — it would warm nothing.
    """
    dim = int(settings.embedding_dimensions)
    return [1.0] + [0.0] * (dim - 1)


# The planner hint the search path uses. Named so the probe and the test that
# EXPLAINs it cannot drift apart from each other.
PROBE_PLANNER_SETTING = "SET LOCAL random_page_cost = 1.1"


def probe_statement():
    """The HNSW probe statement, exactly as `_prewarm_once` issues it.

    Factored out so `tests/integration/test_prewarm_probe.py` can EXPLAIN the
    statement production runs (under `PROBE_PLANNER_SETTING`) instead of a
    hand-copied lookalike: the whole point of the probe is that it walks the
    HNSW index, and only the plan of *this* statement can show that.
    """
    return (
        select(literal(1))
        .select_from(NoteEmbedding)
        .order_by(NoteEmbedding.embedding.cosine_distance(_probe_vector()))
        .limit(1)
    )


async def _prewarm_once() -> tuple[float | None, float | None]:
    """The body of the pre-warm. Returns `(embed_ms, probe_ms)`, either None
    when that half was skipped. Raises freely — the caller contains it."""
    embed_ms: float | None = None
    probe_ms: float | None = None

    # Only local providers have warm state worth keeping. A remote API would
    # just be billed once per tick for nothing.
    if settings.embedding_provider == "ollama":
        from src.services.embeddings import get_embedding

        start = time.monotonic()
        await get_embedding("warmup")
        embed_ms = (time.monotonic() - start) * 1000

    async with async_session() as session:
        if not await _hnsw_index_exists(session):
            logger.info(
                "Pre-warm: HNSW probe skipped (no %s index; "
                "embedding_dimensions=%s exceeds pgvector's 2000-dim limit?)",
                _HNSW_INDEX_NAME,
                settings.embedding_dimensions,
            )
            return embed_ms, None

        # Same planner hint the search path uses, so the probe walks the index
        # and pulls the pages a real search would need — a seq scan here would
        # warm the heap instead, which is not what goes cold.
        await session.execute(text(PROBE_PLANNER_SETTING))
        stmt = probe_statement()
        start = time.monotonic()
        await session.execute(stmt)
        probe_ms = (time.monotonic() - start) * 1000

    return embed_ms, probe_ms


async def prewarm_search_caches() -> None:
    """Keep the embedding model resident and the HNSW hot pages cached.

    `semantic_search` latency is bimodal: ~0.47 s warm, ~17.5 s cold — 14 s of
    that is Ollama reloading bge-m3 after eviction, ~3 s is HNSW index pages
    missing from a 128 MB `shared_buffers` shared with another tenant. As the
    median gap between calls grew from 135 s to 1,676 s, more calls paid the
    cold price (p50 1.2 s → 4.8 s over five weeks). One warm-up per indexer
    tick costs ≈ 0.4 s + 6 ms per five minutes and removes both.

    Runs under `index_pass_lock` (the caller holds it), so it can never overlap
    an index pass, a panel reindex, or a reset-embeddings. Never raises for an
    ordinary failure: a broken embedding provider is the indexer's business,
    not the pre-warm's, and the loop's failure counter must not react to it.
    `CancelledError` is re-raised so lifespan shutdown still stops the loop.
    """
    if settings.mcp_sandbox_mode:
        return
    # Re-checked here, not just before the pass: a panel action can set the
    # pause flag *during* a long index pass, and it does so precisely because
    # it is about to run destructive statements.
    if _is_paused():
        logger.info("Pre-warm skipped (paused)")
        return

    try:
        embed_ms, probe_ms = await asyncio.wait_for(
            _prewarm_once(), timeout=PREWARM_TIMEOUT_SECONDS
        )
    except asyncio.CancelledError:
        # Lifespan shutdown. Must propagate, or the indexer task outlives the
        # app and keeps holding DB sessions.
        raise
    except TimeoutError:
        logger.warning(
            "Pre-warm exceeded %.0fs and was abandoned", PREWARM_TIMEOUT_SECONDS
        )
    except Exception as e:  # noqa: BLE001 - pre-warm must never break the loop
        logger.warning("Pre-warm failed (non-fatal): %s", e)
    else:
        logger.info(
            "Pre-warm complete (embed_ms=%s, probe_ms=%s)",
            "skipped" if embed_ms is None else f"{embed_ms:.0f}",
            "skipped" if probe_ms is None else f"{probe_ms:.0f}",
        )


async def _index_pass_once(user_id: int | None) -> None:
    """One full index + embed pass for a single user (or single-user mode)."""
    try:
        await index_vault(user_id=user_id)
    except Exception as e:
        logger.error(f"Index failed (user_id={user_id}): {e}")
    try:
        await embed_vault(user_id=user_id)
    except Exception as e:
        logger.error(f"Embedding failed (user_id={user_id}): {e}")


async def run_indexer_loop():
    """Run indexer on startup and then periodically.

    Multi-user mode iterates active users sequentially per pass (v1 simplicity;
    parallelism can come later). Single-user mode runs one legacy pass with
    `user_id=None`.
    """
    # Hold `index_pass_lock` for the initial pass too, so a panel-triggered
    # `_reindex_background` fired during startup is serialized against it.
    async with index_pass_lock:
        if settings.multi_user_mode:
            # Initial pass per user.
            user_ids = await _active_user_ids()
            for uid in user_ids:
                try:
                    await index_vault(user_id=uid)
                except Exception as e:
                    logger.error(f"Initial index failed (user_id={uid}): {e}")
            try:
                # Link backfill still uses the global "table empty" guard but
                # runs the per-user pass when triggered. Iterate every user so
                # each user's notes get their links resolved against their own
                # vault_index.
                for uid in user_ids:
                    await link_backfill_pass(user_id=uid)
            except Exception as e:
                logger.error(f"Link backfill failed: {e}")
            for uid in user_ids:
                try:
                    await embed_vault(user_id=uid)
                except Exception as e:
                    logger.error(f"Initial embedding failed (user_id={uid}): {e}")
        else:
            try:
                await index_vault()
            except Exception as e:
                logger.error(f"Initial index failed: {e}")

            try:
                await link_backfill_pass()
            except Exception as e:
                logger.error(f"Link backfill failed: {e}")

            try:
                await embed_vault()
            except Exception as e:
                logger.error(f"Initial embedding failed: {e}")

    consecutive_failures = 0
    logger.info(
        f"Periodic indexer loop armed (interval={settings.index_interval_seconds}s, "
        f"multi_user={settings.multi_user_mode})"
    )
    while True:
        await asyncio.sleep(settings.index_interval_seconds)
        logger.info("Periodic indexer tick")
        if _is_paused():
            logger.info("Periodic tick skipped (paused)")
            continue
        try:
            # Hold `index_pass_lock` for the whole index/embed pass so a
            # concurrent panel-triggered `_reindex_background` cannot run a
            # second index_vault/embed_vault over the same scope.
            async with index_pass_lock:
                if settings.multi_user_mode:
                    # Re-fetch the user list every cycle so newly-added or
                    # newly-deactivated users are picked up without a restart.
                    for uid in await _active_user_ids():
                        await _index_pass_once(uid)
                else:
                    await index_vault()
                    await embed_vault()
                # Still under the lock: serialised against a panel reindex and
                # against reset-embeddings, which also takes this lock. It
                # never raises, so `consecutive_failures` cannot react to it,
                # and it delays the next tick by at most PREWARM_TIMEOUT_SECONDS.
                await prewarm_search_caches()
            await cleanup_expired_tokens()
            consecutive_failures = 0
        except Exception as e:
            consecutive_failures += 1
            logger.error(f"Periodic task failed ({consecutive_failures} consecutive): {e}")
            if consecutive_failures >= 5:
                logger.critical("Indexer has failed 5+ consecutive times — manual intervention required")
