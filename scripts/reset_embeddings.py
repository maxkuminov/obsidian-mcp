"""Drop and recreate `note_embeddings.embedding` at the configured dim, clear
every `embedded_content_hash` so the indexer re-embeds the entire vault on its
next pass, and record the embedding fingerprint the rebuilt rows will be
produced under.

Invoked by `make reset-embeddings`. Reads `EMBEDDING_DIMENSIONS` and the rest
of the embedding configuration from settings.

## Why the fingerprint is written here, in this transaction

The startup guard refuses when the stored fingerprint names a different
configuration than the process is running (#206). This reset is one of the two
operations that *makes* the stored rows match a new configuration, so it is one
of the only writers of that fingerprint — which is what closes the loop: a
configuration change refuses startup, the operator runs the repair, the repair
records the new configuration, and the next startup is silent because the rows
really were produced under it.

`make reset-embeddings` runs `docker compose run --rm` deliberately (#142), so
this one-off container reads the *edited* `.env` and works whether the service
is up or down. That is what makes it write the **new** fingerprint rather than
the one the running service was started with.

The record is inside the same transaction as the column recreate and the hash
wipe, and a failure rolls the whole reset back (design D7d). "Recording never
fails the operation" governs *instrumentation* — the run history, the rotation
cursor — where a lost write costs an operator a view. A fingerprint is not
instrumentation: it is the claim a later startup refuses on. A reset that wiped
the column and then swallowed a failed record would leave a stored value naming
the **previous** configuration over rows about to be built under the new one,
and every later startup would refuse over a database that is actually
consistent.

## Ordering

`acquire_generation_lock` is the **first statement of the transaction**, before
the `DROP INDEX` and before any other lock (design D7c). The rule is a property
of the transaction, not of the statement that happens to need the fingerprint:
the advisory lock is taken before any row or table lock, in one direction
everywhere, so it cannot close a cycle with the locks the index pass and the
panel already contend for. It is also what makes this reset an interlock rather
than a hope — an old-configuration container's certification cannot commit
between this wipe and this record, because it takes the same lock and re-reads
the fingerprint under it.

The documented runbook is deploy-then-reset: edit `.env` → `make deploy` (the
new image refuses at the fingerprint or dimension guard and stays down,
embedding nothing) → `make reset-embeddings` while it is down → restart. In
that order nothing holds the generation lock and this waits for nobody. An
operator who ignores the runbook waits for the in-flight pass to commit instead
— the lock turns a skipped step into lost time rather than lost correctness.
"""
import asyncio
import sys

from sqlalchemy import text

from src.config import settings
from src.database import async_session, engine
from src.services.index_state import (
    KEY_EMBEDDING_FINGERPRINT,
    acquire_generation_lock,
    embedding_fingerprint,
    set_state,
)

#: pgvector refuses to build an HNSW index above 2000 dimensions. The panel's
#: reset path and the pre-warm probe already apply this condition; this script
#: used to create the index unconditionally, so a deployment configured above
#: the limit got a wiped column, no index, and an aborted transaction.
HNSW_MAX_DIMENSIONS = 2000


async def reset() -> None:
    dim = int(settings.embedding_dimensions)
    # Rendered before the transaction opens so the value written is the one
    # this process is configured with, read from the edited `.env`.
    fingerprint = embedding_fingerprint()
    hnsw = dim <= HNSW_MAX_DIMENSIONS
    print(f"Resetting embeddings to vector({dim})...")
    async with async_session() as session:
        # FIRST — before the DROP INDEX, before the DELETE, before any row or
        # table lock this transaction takes (design D7c's ordering rule).
        #
        # It runs under the engine's 60s `statement_timeout`, because raising
        # that with `SET LOCAL` would put a statement ahead of the lock. In the
        # documented deploy-then-reset order nothing holds the lock, so there
        # is nothing to wait for; an operator resetting against a live service
        # may have to wait for an in-flight pass to commit and, past 60s, is
        # told so and can retry. Deliberately no short `lock_timeout`: waiting
        # for a pass is the behaviour we want, since a reset must not land
        # mid-pass.
        await acquire_generation_lock(session)
        # The app's per-connection statement_timeout is too tight for the
        # CREATE INDEX step. Lift it for the rest of this transaction.
        await session.execute(text("SET LOCAL statement_timeout = '5min'"))
        # ALTER COLUMN TYPE on a vector column with a dependent HNSW index
        # is unsafe across pgvector versions — drop and recreate explicitly.
        await session.execute(
            text("DROP INDEX IF EXISTS ix_note_embeddings_embedding_hnsw")
        )
        await session.execute(text("DELETE FROM note_embeddings"))
        await session.execute(
            text(f"ALTER TABLE note_embeddings ALTER COLUMN embedding TYPE vector({dim})")
        )
        await session.execute(
            text("UPDATE notes_metadata SET embedded_content_hash = NULL")
        )
        if hnsw:
            await session.execute(
                text(
                    "CREATE INDEX ix_note_embeddings_embedding_hnsw "
                    "ON note_embeddings USING hnsw (embedding vector_cosine_ops) "
                    "WITH (m = 16, ef_construction = 64)"
                )
            )
        else:
            print(
                f"Skipping HNSW index: EMBEDDING_DIMENSIONS={dim} exceeds "
                f"pgvector's {HNSW_MAX_DIMENSIONS}-dim HNSW limit; "
                "semantic_search will use a sequential scan."
            )
        # Same transaction as the wipe it describes, and a failure takes the
        # whole reset with it (D7d) — never logged and swallowed.
        try:
            await set_state(session, KEY_EMBEDDING_FINGERPRINT, fingerprint)
        except Exception as exc:
            await session.rollback()
            raise RuntimeError(
                "the embedding fingerprint could not be recorded, so the "
                "whole reset was rolled back — the column and the stored "
                "fingerprint are exactly as they were. Recording it is not "
                "instrumentation: it is the claim the next startup verifies "
                f"against. Underlying error: {exc}"
            ) from exc
        await session.commit()
    await engine.dispose()
    print(
        f"Done. Column is vector({dim}); all notes flagged for re-embedding "
        "on the next indexer pass."
    )
    print(f"Recorded embedding fingerprint: {fingerprint}")


if __name__ == "__main__":
    try:
        asyncio.run(reset())
    except Exception as e:
        print(f"reset_embeddings failed: {e}", file=sys.stderr)
        sys.exit(1)
