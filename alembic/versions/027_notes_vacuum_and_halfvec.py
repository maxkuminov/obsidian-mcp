"""notes_metadata autovacuum settings, and the half-precision vector index (#283).

Two independent changes to how the database keeps the search tables healthy.
Both are recorded in the `performance-2026-09` design (D17–D19) and in
docs/architecture/schema-and-migrations.md, "027".

## 1. `notes_metadata` reloptions (D17)

`ALTER TABLE notes_metadata SET (autovacuum_vacuum_scale_factor = 0.02,
autovacuum_vacuum_insert_scale_factor = 0.02)`. At the defaults (0.2) a table
of ~4,100 rows needs ~870 dead tuples before autovacuum looks at it, and the
production table had never been vacuumed: 565 dead tuples, no visibility map,
and — the one that matters to search — **no GIN metapage statistics**, which
only VACUUM writes (`ginvacuumcleanup`) and which the planner's GIN cost
estimate for `keyword_search` relies on. At 0.02 the threshold is ~132, so
autovacuum visits the table regularly and flushes the tsvector index's pending
list with it. `fastupdate = off` was rejected: it removes the pending list by
making every keyword write slower, and does nothing about the rest.

The settings are per table, so the shared instance's other tenants are
untouched. `ALTER TABLE … SET` is idempotent, so a stamp-back re-run
re-applies the same values.

**No VACUUM here (D18).** VACUUM cannot run inside a transaction block, and
alembic runs this whole chain in one (`env.py`: `context.begin_transaction()`).
`autocommit_block()` would commit the earlier revisions mid-chain and break the
schema gate's stamp-back cases. The table's dead-tuple count is already above
the new threshold, so autovacuum visits it within one `autovacuum_naptime` of
this committing; `make db-vacuum-notes` is the deterministic fallback.

## 2. The `halfvec` HNSW expression index (D19)

Replaces 008's `ix_note_embeddings_embedding_hnsw` (`vector_cosine_ops`, ~146
MB live) with `ix_note_embeddings_embedding_halfvec_hnsw` over
`(embedding::halfvec(D)) halfvec_cosine_ops`, *D* being the configured
`EMBEDDING_DIMENSIONS`. Built only when *D* ≤ 2000, the condition under which
008's index exists; above that nothing is built and nothing is dropped (there
is nothing to drop), and the queries keep ordering by the plain `vector`
distance. The name, the condition and the DDL come from
`src/services/vector_index.py`, the one definition the queries, the pre-warm
and both reset paths also use.

It shipped because the recall gate passed: `tests/integration/
test_search_recall.py`, whose exact baseline stays a full-precision `vector`
sequential scan, measured recall 1.00 on every filter shape and on
`find_related`, on each of three rebuilds — identical to the `vector` index.
The queries select the full-precision distance and re-sort by it, so half
precision only decides which candidates the scan yields.

**Reconcile, don't adopt.** A stamp-back re-run finds the index already there.
It is accepted only if it is valid and `pg_get_indexdef` matches, text for
text, what `create_index_sql(D)` produces on a scratch temp table of the same
column type — measured on this server, not a string guessed here (026's
device). An index of that name with any other definition, or an invalid one,
is refused and named: adopting it would leave the queries' ORDER BY matching
nothing, and the search silently a sequential scan (or, worse, an index over a
different dimension).

**Alembic.** The index is not on the model; `alembic/env.py`'s
`include_object` excludes exactly this name, because autogenerate on SQLAlchemy
2 compares expression indexes and strips the `::halfvec` cast by regex. The
schema gate asserts the index and the reloptions through the catalogue.

**Build cost.** Non-concurrent, under `maintenance_work_mem = 512MB`: ~17.5 k
× 1024 dims builds in tens of seconds, during which writes to
`note_embeddings` wait. That happens in the deploy's migrate step, where
waiting is acceptable. `statement_timeout` is lifted to 15 minutes for the
build; `lock_timeout` stays short so a migration blocked behind a live pass
fails fast instead of stalling the deploy.

`downgrade()` resets both reloptions, recreates 008's index when *D* ≤ 2000,
and drops the half-precision one.

## search_path

Every statement is unqualified, so it is pinned to `public` as 021/024/026 do,
and asserted: what the unqualified names resolve to must be the `public`
tables. Everything this sets is `RESET` at the end, because alembic runs every
pending revision in one transaction.

Revision ID: 027
Revises: 026
Create Date: 2026-09-22
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from src.config import settings
from src.services import vector_index

revision: str = "027"
down_revision: Union[str, None] = "026"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


NOTES = "notes_metadata"
NOTES_QUALIFIED = "public.notes_metadata"
EMBEDDINGS = "note_embeddings"
EMBEDDINGS_QUALIFIED = "public.note_embeddings"

#: The two reloptions, as `pg_class.reloptions` prints them.
RELOPTIONS = (
    "autovacuum_vacuum_scale_factor=0.02",
    "autovacuum_vacuum_insert_scale_factor=0.02",
)

BUILD_STATEMENT_TIMEOUT = "15min"


def _oid(bind, name: str):
    """The OID `name` resolves to, or None. `to_regclass` never raises."""
    return bind.execute(
        sa.text("SELECT CAST(to_regclass(:name) AS oid)"), {"name": name}
    ).scalar()


def _pin_search_path() -> None:
    """021's device: 026 `RESET`s its own pin, so 027 needs its own."""
    op.execute("SET LOCAL search_path TO public")


def _assert_is_public(bind, name: str, qualified: str) -> None:
    qualified_oid = _oid(bind, qualified)
    unqualified_oid = _oid(bind, name)
    if qualified_oid is None or qualified_oid != unqualified_oid:
        raise RuntimeError(
            f"027's {name} is not the table {qualified} resolves to "
            f"(search_path-relative oid {unqualified_oid!r}, qualified oid "
            f"{qualified_oid!r}). Set the migration role's search_path so "
            "`public` comes first, then re-run."
        )


def _index_state(bind, name: str):
    """`(indexdef, indisvalid)` for an index of `name` on `public.note_embeddings`, or None."""
    return bind.execute(
        sa.text(
            "SELECT pg_get_indexdef(i.indexrelid), i.indisvalid "
            "FROM pg_index i "
            "JOIN pg_class c ON c.oid = i.indexrelid "
            "WHERE i.indrelid = CAST(:table AS regclass) AND c.relname = :name"
        ),
        {"table": EMBEDDINGS_QUALIFIED, "name": name},
    ).first()


def _canonical_indexdef(bind, dim: int) -> str:
    """What this server prints for `create_index_sql(dim)`, measured.

    The identical DDL on an empty scratch TEMP table with the same column
    type, then its `pg_get_indexdef` with the scratch names mapped back — so
    the comparison cannot drift with a PostgreSQL or pgvector release's
    normalisation of casts, parentheses or `WITH` options.
    """
    scratch = "_omcp_027_index_probe"
    bind.execute(sa.text(f"DROP TABLE IF EXISTS pg_temp.{scratch}"))
    bind.execute(sa.text(f"CREATE TEMP TABLE {scratch} (embedding vector({dim}))"))
    ddl = vector_index.create_index_sql(dim).replace(
        f" ON {vector_index.TABLE} ", f" ON pg_temp.{scratch} ", 1
    )
    bind.execute(sa.text(ddl))
    rendered = bind.execute(
        sa.text(
            "SELECT pg_get_indexdef(i.indexrelid) FROM pg_index i "
            "WHERE i.indrelid = CAST(:scratch AS regclass)"
        ),
        {"scratch": f"pg_temp.{scratch}"},
    ).scalar()
    bind.execute(sa.text(f"DROP TABLE IF EXISTS pg_temp.{scratch}"))
    schema = rendered.split(" ON ", 1)[1].split(".", 1)[0]
    return rendered.replace(
        f" ON {schema}.{scratch} ", f" ON public.{EMBEDDINGS} ", 1
    )


def _reconcile_index(bind, dim: int) -> None:
    state = _index_state(bind, vector_index.INDEX_NAME)
    if state is None:
        op.execute("SET LOCAL maintenance_work_mem = '512MB'")
        op.execute(f"SET LOCAL statement_timeout = '{BUILD_STATEMENT_TIMEOUT}'")
        op.execute(vector_index.create_index_sql(dim))
        state = _index_state(bind, vector_index.INDEX_NAME)

    expected = _canonical_indexdef(bind, dim)
    indexdef, valid = state
    if indexdef != expected or not valid:
        raise RuntimeError(
            f"027 found an index named {vector_index.INDEX_NAME} on "
            f"{EMBEDDINGS_QUALIFIED} that is not the one it builds "
            f"(valid={valid!r}; definition {indexdef!r}, expected "
            f"{expected!r}). The vector queries order by that exact expression, "
            "so any other index of this name leaves semantic search on a "
            "sequential scan. Drop it by hand, then re-run."
        )


def upgrade() -> None:
    bind = op.get_bind()
    dim = int(settings.embedding_dimensions)

    # Fail fast rather than queueing behind a pass holding row locks.
    op.execute("SET LOCAL lock_timeout = '10s'")
    op.execute("SET LOCAL statement_timeout = '60s'")
    _pin_search_path()
    _assert_is_public(bind, NOTES, NOTES_QUALIFIED)
    _assert_is_public(bind, EMBEDDINGS, EMBEDDINGS_QUALIFIED)

    # 1. D17.
    op.execute(f"ALTER TABLE {NOTES} SET ({', '.join(RELOPTIONS)})")

    # 2. D19. Build the new index before dropping the old one, so a failed
    # build leaves the database exactly as it was (one transaction either way).
    if vector_index.index_enabled(dim):
        _reconcile_index(bind, dim)
        op.execute(f"DROP INDEX IF EXISTS {vector_index.LEGACY_INDEX_NAME}")
    else:
        print(
            f"027: no vector index at EMBEDDING_DIMENSIONS={dim} (limit "
            f"{vector_index.MAX_INDEXED_DIMENSIONS}); semantic_search keeps "
            "its sequential scan.",
            flush=True,
        )

    op.execute("RESET maintenance_work_mem")
    op.execute("RESET lock_timeout")
    op.execute("RESET statement_timeout")
    op.execute("RESET search_path")


def downgrade() -> None:
    bind = op.get_bind()
    dim = int(settings.embedding_dimensions)

    op.execute("SET LOCAL lock_timeout = '10s'")
    op.execute("SET LOCAL statement_timeout = '60s'")
    _pin_search_path()
    _assert_is_public(bind, NOTES, NOTES_QUALIFIED)
    _assert_is_public(bind, EMBEDDINGS, EMBEDDINGS_QUALIFIED)

    op.execute(
        f"ALTER TABLE {NOTES} RESET "
        "(autovacuum_vacuum_scale_factor, autovacuum_vacuum_insert_scale_factor)"
    )

    if vector_index.index_enabled(dim):
        op.execute("SET LOCAL maintenance_work_mem = '512MB'")
        op.execute(f"SET LOCAL statement_timeout = '{BUILD_STATEMENT_TIMEOUT}'")
        op.execute(vector_index.create_legacy_index_sql())
    op.execute(f"DROP INDEX IF EXISTS {vector_index.INDEX_NAME}")

    op.execute("RESET maintenance_work_mem")
    op.execute("RESET lock_timeout")
    op.execute("RESET statement_timeout")
    op.execute("RESET search_path")
