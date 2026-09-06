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
from src.services.indexer import (
    VaultRootQuarantined,
    _active_user_ids,
    rebuild_tsvectors,
)
from src.services.vault_overlap import detect_and_publish


async def main() -> None:
    print(f"Rebuilding tsvectors for FTS_CONFIGS={settings.fts_configs}...")
    # E5 — this is a **separate process**: no lifespan, no indexer loop, and its
    # own `_active_user_ids()` loop that reaches a pass without touching either.
    # A detection installed in the indexer loop would not be installed here, so
    # it publishes its own snapshot and consumes only what it published. A
    # detection that raises here is fatal by design: the caller is an operator
    # at a terminal who can read the error and re-run, and rewriting every
    # keyword vector in the vault against roots nothing has checked is exactly
    # the pass this guard exists to stop.
    await detect_and_publish()
    async with async_session() as session:
        # Fail fast on an unknown config name before touching any rows.
        await validate_fts_configs(session)
        if settings.multi_user_mode:
            total = 0
            for uid in await _active_user_ids():
                try:
                    total += await rebuild_tsvectors(session, user_id=uid)
                except VaultRootQuarantined as e:
                    # One quarantined tenant must not stop the rebuild for the
                    # rest: the condition is a property of specific roots and
                    # says nothing about a third tenant's vault. Nothing was
                    # read or written for this user.
                    print(f"Skipped: {e}", file=sys.stderr)
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
