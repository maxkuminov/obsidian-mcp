from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.db import NoteMetadata
from src.services.filters import apply_note_filters
from src.services.fts import combined_tsquery


async def full_text_search(
    session: AsyncSession,
    query: str,
    folder: str | None = None,
    limit: int = 20,
    tags: list[str] | None = None,
    frontmatter: dict | None = None,
    user_id: int | None = None,
) -> list[dict]:
    """Full-text search over notes_metadata using tsvector.

    The tsquery is built from `settings.fts_configs` (see `src/services/fts.py`)
    so query-time configs match the index-time configs in `indexer.py`.
    """
    limit = max(1, min(limit, 500))
    tsquery = combined_tsquery(query)
    rank = func.ts_rank_cd(NoteMetadata.content_tsvector, tsquery).label("rank")

    # The same transaction-scoped hint the vector path uses, for the same
    # reason: the planner's cost model does not see detoast I/O. It costs the
    # `notes_metadata` heap at `relpages` (~481) and happily picks a Seq Scan,
    # which then detoasts every one of ~3,800 tsvectors out of a 36 MB TOAST
    # table — 13,086 buffers per query. Lowering `random_page_cost` flips it to
    # a GIN bitmap scan: 1,146 buffers, 6–9 ms warm instead of 26–37 ms.
    # Lifetime counters before this: 5 index scans vs 3,655 sequential ones.
    # SET LOCAL scopes to this transaction only — the database is shared with
    # other tenants, so no global setting is touched.
    await session.execute(text("SET LOCAL random_page_cost = 1.1"))

    stmt = (
        select(NoteMetadata, rank)
        .where(NoteMetadata.content_tsvector.op("@@")(tsquery))
    )
    stmt = apply_note_filters(
        stmt, folder=folder, tags=tags, frontmatter=frontmatter, user_id=user_id
    )
    # `file_path` breaks rank ties deterministically. `ts_rank_cd` produces
    # plenty of them, and which tied row survived the LIMIT otherwise depended
    # on the plan — so the hint above would have changed *results*, not just
    # speed. With the tie-break, membership and order are stable across plans.
    stmt = stmt.order_by(rank.desc(), NoteMetadata.file_path.asc()).limit(limit)

    result = await session.execute(stmt)
    rows = result.all()
    return [
        {
            "path": nm.file_path,
            "title": nm.title,
            "tags": nm.tags,
            "rank": float(row_rank),
        }
        for nm, row_rank in rows
    ]
