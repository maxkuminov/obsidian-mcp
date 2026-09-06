"""Recompute every note's `content_tsvector` under the currently configured
`FTS_CONFIGS` (see `src/config.py` and `src/services/fts.py`), and record the
keyword fingerprint that certifies the result.

Run this after changing `FTS_CONFIGS` in `.env` and redeploying. Stored
tsvectors are built at index time and go stale when the config list changes;
this script re-reads each note's file and rebuilds them.

Invoked by `make rebuild-tsvectors`. This rebuilds the KEYWORD index only — it
does NOT touch embeddings/vectors and makes NO API calls, so it completes in
seconds for a few thousand notes (unlike the expensive `make reset-embeddings`
flow, which it must not be confused with).

**It rebuilds every scope that holds rows, not every active user, and it is
all-or-nothing.** The fingerprint it writes is one stored row asserting
something about *every* retained row in the database, and that is not a claim
that can be established one tenant at a time: a scope the rebuild skipped —
provenance unsettled, no assigned vault path, a quarantined root, or ownerless
rows under `MULTI_USER_MODE` — keeps its previous-configuration vectors, and a
startup that fails closed on the fingerprint would then pass while keyword
search was exactly as wrong as before. So a skipped scope aborts the whole
operation, names itself and its reason, rolls every rebuilt scope back and
records nothing.

**It checks every root it will open, before it takes the lock.** The published
quarantine snapshot speaks for *active* users holding an assignment, because
that is whom the server serves — and this command opens an **inactive** owner's
retained root too. An inactive owner retaining `/vaults/team` beside an active
tenant at `/vaults/team/private` is therefore named by nothing the snapshot
publishes, and reading it would file that tenant's notes' keyword vectors under
the inactive owner's scope, under a fingerprint certifying the result. So the
driver runs the same two checks over its own read set first
(`indexer.survey_rebuild_roots`) and aborts naming the pair. The survey is
bounded and off the loop and happens **before** the generation lock, so a hung
mount cannot hold that lock while an `open` waits on it.

**It waits for an in-flight index pass.** The rebuild takes the index
generation lock before it reads its first row, and the periodic pass holds that
lock for the duration of its transaction — so this command blocks until the
pass commits rather than interleaving with it. That is the required behaviour,
and it is why nothing here sets a short `lock_timeout`.

Five ways forward when it refuses, in order of preference: settle the scope
(assign or delete that user, or let an in-progress re-derive finish), correct
the overlapping assignment the survey named, restore a root it could not
examine, delete or reassign ownerless rows, or put `FTS_CONFIGS` back to the
value the stored fingerprint names — which clears the startup refusal
immediately with no rebuild at all.
"""
import asyncio
import sys

from src.config import settings
from src.database import async_session, engine
from src.services.fts import validate_fts_configs
from src.services.indexer import rebuild_tsvectors_all_scopes
from src.services.vault_overlap import detect_and_publish


async def main() -> None:
    print(f"Rebuilding tsvectors for FTS_CONFIGS={settings.fts_configs}...")
    # E5 — this is a **separate process**: no lifespan, no indexer loop, and it
    # reaches a pass without touching either. A detection installed in the
    # indexer loop would not be installed here, so it publishes its own
    # snapshot and consumes only what it published. A detection that raises
    # here is fatal by design: the caller is an operator at a terminal who can
    # read the error and re-run, and rewriting every keyword vector in the
    # vault against roots nothing has checked is exactly the pass this guard
    # exists to stop.
    await detect_and_publish()
    print(
        "Waiting for any in-flight index pass to commit before the first row "
        "is read (the generation lock)."
    )
    async with async_session() as session:
        # Fail fast on an unknown config name before touching any rows — and
        # before taking the lock, so a typo does not queue behind a long pass.
        await validate_fts_configs(session)
        outcomes = await rebuild_tsvectors_all_scopes(session)
    await engine.dispose()
    total = sum(outcome.rows for outcome in outcomes.values())
    for owner, outcome in outcomes.items():
        label = "single-user scope" if owner is None else f"user_id={owner}"
        print(f"  {label}: {outcome.describe()}")
    print(
        f"Done. Recomputed tsvectors for {total} note(s) across "
        f"{len(outcomes)} scope(s), and recorded the keyword fingerprint. "
        "Keyword search now reflects FTS_CONFIGS; embeddings were not touched."
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        print(f"rebuild_tsvectors failed: {e}", file=sys.stderr)
        sys.exit(1)
