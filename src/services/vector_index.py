"""The one definition of the `note_embeddings` vector index (#283, design D19).

Five places create, drop or rely on this index: migration 027, the panel's
reset-embeddings route, `scripts/reset_embeddings.py`, the indexer's pre-warm
probe, and the two vector queries (`semantic_search`, `find_related_stmt`).
Before this module each spelled the name and the DDL out for itself, so a
change to one could silently stop matching the others — and an index whose
expression the query does not repeat *exactly* is an index the planner never
uses. Everything below is derived from one expression builder so the DDL and
the query cannot drift.

## What the index is

An HNSW expression index over `(embedding::halfvec(D)) halfvec_cosine_ops`,
where *D* is the configured `EMBEDDING_DIMENSIONS` — never a literal, because
an OpenAI deployment runs at 1536 or 3072. Half precision halves the index
(the graph stores the indexed value), and only decides **which candidates** the
scan yields: both queries also select the full-precision `embedding <=> q`,
re-sort the fetched candidates by it before the per-note dedupe, and report
`similarity = 1 - full_distance`. The recall gate in
`tests/integration/test_search_recall.py` measures what is left — membership at
the overfetch boundary — against a full-precision sequential baseline.

## When it exists

Only when *D* ≤ 2000, which is the condition under which the legacy `vector`
HNSW index existed. `halfvec` could index up to 4,000 dimensions, but building
it for 2000 < *D* ≤ 4000 would turn those deployments' exact scan into an
approximate one — a recall decision this change does not take. Above 2000
nothing is built and the queries order by the plain `vector` distance.

## Alembic

The index is **not** declared on the model: `alembic/env.py`'s
`include_object` excludes exactly `INDEX_NAME` from autogenerate, and the
schema gate verifies it through the catalogue instead. See
docs/architecture/schema-and-migrations.md, "027".
"""
from __future__ import annotations

from sqlalchemy import bindparam, cast, column
from sqlalchemy.dialects import postgresql

from pgvector.sqlalchemy import HALFVEC, Vector

from src.config import settings

#: The index this module owns.
INDEX_NAME = "ix_note_embeddings_embedding_halfvec_hnsw"
#: The full-precision `vector_cosine_ops` index migration 008 created and 027
#: replaces. Dropped (`IF EXISTS`) wherever the index is dropped, so a reset on
#: a database that has not yet reached 027 still clears the column's dependant.
LEGACY_INDEX_NAME = "ix_note_embeddings_embedding_hnsw"

#: pgvector refuses an HNSW index over `vector` above 2000 dimensions; above
#: it, no index of either kind exists (see "When it exists" above).
MAX_INDEXED_DIMENSIONS = 2000

#: HNSW build parameters, unchanged from migration 008.
HNSW_M = 16
HNSW_EF_CONSTRUCTION = 64

TABLE = "note_embeddings"
COLUMN = "embedding"
OPCLASS = "halfvec_cosine_ops"


def _dim(dim: int | None) -> int:
    return int(settings.embedding_dimensions if dim is None else dim)


def index_enabled(dim: int | None = None) -> bool:
    """True when the index exists at this dimension (defaults to the configured one)."""
    return _dim(dim) <= MAX_INDEXED_DIMENSIONS


def _halfvec_of(expr, dim: int):
    """`CAST(expr AS HALFVEC(dim))` — the one spelling of the indexed value."""
    return cast(expr, HALFVEC(dim))


def index_expression_sql(dim: int | None = None) -> str:
    """The index expression as DDL text, compiled from the same builder the
    query uses — so "the query repeats the index expression" is a fact about
    one function, not about two strings kept in step by hand."""
    expr = _halfvec_of(column(COLUMN), _dim(dim))
    return str(expr.compile(dialect=postgresql.dialect()))


def create_index_sql(dim: int | None = None) -> str:
    """`CREATE INDEX` for the configured dimension.

    Callers must check `index_enabled(dim)` first; this refuses rather than
    emitting DDL pgvector would reject or a build the design does not allow.
    """
    d = _dim(dim)
    if not index_enabled(d):
        raise ValueError(
            f"no vector index is built at {d} dimensions "
            f"(limit {MAX_INDEXED_DIMENSIONS}); check index_enabled() first"
        )
    return (
        f"CREATE INDEX {INDEX_NAME} ON {TABLE} "
        f"USING hnsw (({index_expression_sql(d)}) {OPCLASS}) "
        f"WITH (m = {HNSW_M}, ef_construction = {HNSW_EF_CONSTRUCTION})"
    )


def drop_index_sql() -> str:
    """Drop this index and the legacy one, each only if it exists.

    Both reset paths run this before `ALTER COLUMN TYPE`: an index over the
    column (either kind) must not survive the type change.
    """
    return f"DROP INDEX IF EXISTS {INDEX_NAME}, {LEGACY_INDEX_NAME}"


def create_legacy_index_sql() -> str:
    """Migration 008's index, for 027's `downgrade()` only."""
    return (
        f"CREATE INDEX IF NOT EXISTS {LEGACY_INDEX_NAME} ON {TABLE} "
        f"USING hnsw ({COLUMN} vector_cosine_ops) "
        f"WITH (m = {HNSW_M}, ef_construction = {HNSW_EF_CONSTRUCTION})"
    )


def full_distance_expr(query_vec):
    """Full-precision cosine distance, `embedding <=> q` over `vector`.

    What every reported number and every final ordering is computed from.
    """
    from src.models.db import NoteEmbedding

    return NoteEmbedding.embedding.cosine_distance(query_vec)


def _query_dim(query_vec, dim: int | None) -> int:
    """The dimension a query casts to: the query vector's own length.

    In production this *is* `EMBEDDING_DIMENSIONS` — the lifespan refuses to
    start when the column's dimension disagrees with the setting
    (`_check_embedding_dim`), and a query vector of any other length fails
    against the `vector(D)` column anyway. Taking it from the vector rather
    than the setting means the cast always agrees with the column the query
    runs against, which is also what lets a test database built at a small
    dimension exercise the same statement.
    """
    return int(dim) if dim is not None else len(query_vec)


def order_expr(query_vec, dim: int | None = None):
    """The ORDER BY expression that lets the planner use the index.

    With the index enabled: `CAST(embedding AS HALFVEC(D)) <=> CAST(q AS
    HALFVEC(D))`, the index expression verbatim. Otherwise the plain
    full-precision distance, since no index exists to match.
    """
    from src.models.db import NoteEmbedding

    d = _query_dim(query_vec, dim)
    if not index_enabled(d):
        return full_distance_expr(query_vec)
    query = bindparam(None, query_vec, type_=Vector(d))
    return _halfvec_of(NoteEmbedding.embedding, d).cosine_distance(
        _halfvec_of(query, d)
    )


def order_and_full_distance(query_vec, dim: int | None = None):
    """`(order, full)` for a vector query.

    When the index is disabled the two are the *same* expression object, so the
    statement is exactly the pre-027 one (one bound vector, `ORDER BY` the
    selected distance) rather than sending the query vector twice.
    """
    full = full_distance_expr(query_vec)
    d = _query_dim(query_vec, dim)
    if not index_enabled(d):
        return full, full
    return order_expr(query_vec, d), full
