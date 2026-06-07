"""Recompute every note's `content_tsvector` under the currently configured
`FTS_CONFIGS` (see `src/config.py` and `src/services/fts.py`).

Run this after changing `FTS_CONFIGS` in `.env` and redeploying. Stored
tsvectors are built at index time and go stale when the config list changes;
this script re-reads each note's file and rebuilds them.

Invoked by `make rebuild-tsvectors`. This rebuilds the KEYWORD index only — it
does NOT touch embeddings/vectors and makes NO API calls, so it completes in
seconds for a few thousand notes (unlike the expensive `make reset-embeddings`
flow, which it must not be confused with).
"""
import asyncio
import sys

from src.config import settings
from src.database import async_session, engine
from src.services.fts import validate_fts_configs
from src.services.indexer import _active_user_ids, rebuild_tsvectors


async def main() -> None:
    print(f"Rebuilding tsvectors for FTS_CONFIGS={settings.fts_configs}...")
    async with async_session() as session:
        # Fail fast on an unknown config name before touching any rows.
        await validate_fts_configs(session)
        if settings.multi_user_mode:
            total = 0
            for uid in await _active_user_ids():
                total += await rebuild_tsvectors(session, user_id=uid)
        else:
            total = await rebuild_tsvectors(session, user_id=None)
    await engine.dispose()
    print(
        f"Done. Recomputed tsvectors for {total} note(s). Keyword search now "
        "reflects FTS_CONFIGS; embeddings were not touched."
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        print(f"rebuild_tsvectors failed: {e}", file=sys.stderr)
        sys.exit(1)
