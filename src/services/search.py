from sqlalchemy import func, select
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
    tsquery = combined_tsquery(query)
    rank = func.ts_rank_cd(NoteMetadata.content_tsvector, tsquery).label("rank")

    stmt = (
        select(NoteMetadata, rank)
        .where(NoteMetadata.content_tsvector.op("@@")(tsquery))
    )
    stmt = apply_note_filters(
        stmt, folder=folder, tags=tags, frontmatter=frontmatter, user_id=user_id
    )
    stmt = stmt.order_by(rank.desc()).limit(limit)

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
