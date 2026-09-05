"""State about the index as a whole, and the chunk-truncation marker (#206, #202).

Two units, one revision, because both exist to stop the same class of failure:
a derived row that no longer describes what it claims to describe, with nothing
on the row or in the database able to say so.

## Why a table

`note_embeddings` records nothing about the provider, model, chunk size or
overlap that produced it, and `notes_metadata.content_tsvector` records nothing
about `FTS_CONFIGS`. Startup catches a *dimension* change (the column width is
a physical fact `pg_attribute` can be asked about); a same-dimension model swap
— bge-m3 for another 1024-dim model — is caught by nothing, mixes two vector
spaces in one column permanently, and makes cosine distance meaningless. The
fix is one stored fingerprint per derived kind, compared at startup.

There is no singleton row to hang those on. `users` is per tenant,
`notes_metadata` is per note (and a per-note copy of a global setting is one
identical string per chunk, whose only use would be the lazy per-note re-embed
that leaves two vector spaces coexisting for the whole migration window), and
`indexer_runs` is an append-only display history that nothing reads for a
decision — its own docstring says so, and this revision must not be the reason
that changes. The three facts stored here — the embedding fingerprint, the
keyword fingerprint and the embed rotation cursor — share one lifecycle ("state
about the index as a whole, written by the pass or by a maintenance command")
and differ in shape, which is what a key/value table is for.

## Why the CHECK, given a key/value table

A key read from this table that does not exist reads as **absent**, and absent
is the state that makes the startup fingerprint check *adopt* the current
configuration rather than refuse. A single mistyped key therefore silently
disables the guard whose entire purpose is to prevent a permanent, undetectable
corruption of the vector space — the guard would report "no fingerprint stored,
assuming the current one" for ever, on every start, while the rows underneath
it were built by another model. `ck_indexer_runs_trigger` (019) exists for the
weaker version of the same argument, where a typo produces only a mislabelled
row.

So the key set is closed in the database, and adding a key becomes a migration.
That is correct rather than inconvenient: every key here has a startup or a
scheduling consequence.

The constraint is resolved through `pg_constraint` and never by name, per 013's
rule — a same-named `CHECK (true)` satisfies every name-level lookup while
enforcing nothing, and `alembic check` does not compare CHECK predicates at
all. Its definition is compared against **the server's own rendering** of the
same declaration, derived at runtime from a scratch `TEMP` table (019's device),
so the comparison is not pinned to one PostgreSQL major's normalization of
`IN (...)`. It must also be `convalidated` — a `NOT VALID` constraint enforces
new rows having never checked the existing ones — and carry this revision's
marker as its constraint comment.

## Why nothing is backfilled

**023 writes no `indexer_state` row.** Deriving a fingerprint from the current
settings at migration time would assert that the stored embedding and keyword
rows were produced by the configuration the `.env` carries *now* — which is
exactly the claim the fingerprint exists to test, and exactly the
reassignment-lag mistake 016 refuses to make with vault provenance. An absent
fingerprint means "unknown", which is the only true statement available here,
and the application's startup adoption rule owns it from there.

## Why the column's server default is the truth for every existing row

`notes_metadata.chunks_truncated BOOLEAN NOT NULL DEFAULT FALSE`, in 022's
shape for 022's reasons. The constant server default is what makes this an
`ADD COLUMN` PostgreSQL satisfies from the catalogue rather than a full rewrite,
on a table with one row per note carrying a `tsvector` and two GIN indexes. And
`false` is not a placeholder: every row that exists when this runs was embedded
by a chunker that had **no cap** and could not truncate, so `false` is the fact.

A column and not merely a log line, again for 022's reasons: the ERROR ring
buffer the ops-health page reads is 100 entries and process-lifetime, while a
capped note's vectors persist indefinitely, and `semantic_search` /
`find_related` would go on presenting a result from the note's head as a result
from the whole note.

## The deploy window

The deploy migrates and *then* recreates the container, so the previous build
serves briefly against the new objects. It neither reads nor writes either of
them and its `notes_metadata` upserts omit the column, so the server default
supplies `false` — the correct value for anything the uncapped chunker produced
— and `indexer_state` stays empty until the new build's first startup adopts.

Because 023 writes no row and backfills nothing, a stamp-back re-run (the schema
gate does `alembic stamp 022` then `upgrade head`) reconciles the existing
objects and writes nothing, so it cannot erase a fingerprint the application has
since recorded or a truncation marker the indexer has since set.

## search_path

`op.create_table`, `COMMENT ON`, `op.add_column` and `op.drop_table` are all
unqualified, so they resolve through `search_path` — and on a database or role
whose path does not start with `public`, `CREATE TABLE indexer_state` lands
somewhere the application never looks. That is 021's lesson, and the schema
gate's redirected-`search_path` case is what found this revision without the
pin: 021 `RESET`s the path at the end of its own `upgrade()`, so a later
revision in the same transaction inherits nothing.

So `upgrade()` and `downgrade()` pin `SET LOCAL search_path TO public` first
and `RESET` it at the end, and `upgrade()` then **asserts** that what it
created or adopted really is the object the unqualified name resolves to.
Pinning rather than passing `schema="public"` to each `op.*` call is
deliberate, for 021's reason: a schema-qualified table in alembic's eyes does
not match a model that declares no schema, and `alembic check` would report
drift for ever after.

## Locks

One `CREATE TABLE`, which blocks nothing already in use, and one
`ALTER TABLE ... ADD COLUMN` with a constant default, which takes ACCESS
EXCLUSIVE on `notes_metadata` and holds it to COMMIT but performs no rewrite.
`lock_timeout` / `statement_timeout` make a blocked migration fail fast instead
of stalling the deploy, and both are `RESET` at the end because alembic runs
every pending revision in one transaction and `SET LOCAL` would otherwise leak
into a later revision (013 through 022 do the same).

Revision ID: 023
Revises: 022
Create Date: 2026-09-05
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "023"
down_revision: Union[str, None] = "022"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


STATE_TABLE = "indexer_state"
QUALIFIED_STATE_TABLE = "public.indexer_state"
CHECK_NAME = "ck_indexer_state_key"

NOTES_TABLE = "notes_metadata"
CHUNKS_COLUMN = "chunks_truncated"
CHUNKS_EXPECTED_TYPE = "boolean"
CHUNKS_EXPECTED_DEFAULT = "false"

# Mirrored from `src.models.db.INDEXER_STATE_KEYS`, and named again in
# `src.services.index_state` as the three `KEY_*` constants. Written out as a
# literal rather than imported: a migration must keep describing the schema it
# created even after the model moves on, so it pins its own copy. 019 does the
# same with `TRIGGERS`.
STATE_KEYS = ("embedding_fingerprint", "fts_fingerprint", "embed_rotation_cursor")

KEY_PREDICATE = "key IN (" + ", ".join(f"'{k}'" for k in STATE_KEYS) + ")"

# 013's device, and 015's through 022's: the migration marks what it created,
# so `downgrade()` can tell its own work from somebody else's and drop only the
# former.
#
# `TABLE_MARKER` and `COLUMN_MARKER` are declared on the ORM table and column
# as well (`src/models/db.py`, `_INDEXER_STATE_TABLE_MARKER` and
# `_CHUNKS_TRUNCATED_COLUMN_MARKER`), so `alembic check` compares them like any
# other attribute: a marker that drifted from the model, or one silently
# dropped, is a dirty check rather than a migration that quietly stops
# recognising its own work. Keep those two byte identical.
#
# `CHECK_MARKER` has no ORM counterpart because autogenerate does not compare
# CHECK constraints at all — which is precisely why this revision reads the
# catalogue for it directly.
TABLE_MARKER = "state about the index as a whole (023_indexer_state)"
CHECK_MARKER = "closed key set for indexer_state (023_indexer_state)"
COLUMN_MARKER = "chunk-cap truncation marker (023_indexer_state)"

# `(column, format_type, attnotnull)` — the whole shape, in creation order.
EXPECTED_COLUMNS = (
    ("key", "character varying(64)", True),
    ("value", "text", True),
    ("updated_at", "timestamp with time zone", True),
)


def _quote(value: str) -> str:
    """A single-quoted SQL string literal. The markers are module constants
    with no quotes in them; the doubling is here so it stays correct if that
    changes."""
    return "'" + value.replace("'", "''") + "'"


# --------------------------------------------------------------------------
# catalogue reads
# --------------------------------------------------------------------------


def _table_exists(bind) -> bool:
    return bool(
        bind.execute(
            sa.text("SELECT to_regclass(:qualified)"), {"qualified": STATE_TABLE}
        ).scalar()
    )


def _pin_search_path() -> None:
    """Pin `search_path` to `public` for the rest of this transaction.

    021's device, and the fix for the unqualified DDL below. `op.create_table`,
    `COMMENT ON`, `op.add_column`, `op.drop_column` and `op.drop_table` all
    resolve through `search_path`; giving each one `schema="public"` would make
    the table a *schema-qualified* object in alembic's eyes while the ORM model
    declares no schema, so autogenerate would see the two disagree and
    `alembic check` would never be clean again. Pinning the path instead makes
    the unqualified names resolve to `public` while leaving both sides
    schema-less.

    `SET LOCAL` is transaction-scoped, which is exactly how this runs —
    `alembic/env.py` executes every pending revision in one transaction — and
    is why `upgrade()` and `downgrade()` `RESET` it at the end rather than
    leaving it for a later revision to inherit. 021 does the same, which is
    precisely why this revision cannot rely on 021's pin still being in force.
    """
    op.execute("SET LOCAL search_path TO public")


def _oid(bind, name: str):
    return bind.execute(
        sa.text("SELECT to_regclass(:name)::oid"), {"name": name}
    ).scalar()


def _assert_is_the_qualified_table(bind) -> None:
    """What the unqualified name resolves to is `public.indexer_state`.

    Belt and braces behind the pin, in 021's shape: if the pin ever fails to
    take effect, this fails closed rather than leaving the fingerprints in a
    table the application never reads — a state whose symptom is a startup that
    silently adopts, for ever, a fingerprint nobody wrote.
    """
    qualified = _oid(bind, QUALIFIED_STATE_TABLE)
    unqualified = _oid(bind, STATE_TABLE)
    if qualified is None or qualified != unqualified:
        raise RuntimeError(
            f"023's {STATE_TABLE} is not the table {QUALIFIED_STATE_TABLE} "
            f"resolves to (search_path-relative oid {unqualified!r}, qualified "
            f"oid {qualified!r}). The startup fingerprint guard reads the "
            "unqualified name, so a table elsewhere on the path would leave it "
            "adopting for ever. Set the migration role's search_path so "
            "`public` comes first, then re-run."
        )


def _table_comment(bind) -> str | None:
    return bind.execute(
        sa.text(
            "SELECT obj_description(c.oid, 'pg_class') "
            "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE c.relname = :table AND c.relkind = 'r' "
            "  AND n.nspname = ANY (current_schemas(false))"
        ),
        {"table": STATE_TABLE},
    ).scalar()


def _columns(bind):
    return [
        (row[0], row[1], row[2])
        for row in bind.execute(
            sa.text(
                "SELECT a.attname, format_type(a.atttypid, a.atttypmod), a.attnotnull "
                "FROM pg_attribute a "
                "WHERE a.attrelid = CAST(:table AS regclass) "
                "  AND a.attnum > 0 AND NOT a.attisdropped "
                "ORDER BY a.attnum"
            ),
            {"table": STATE_TABLE},
        ).fetchall()
    ]


def _primary_key_columns(bind):
    """The PK's column list, or None when the table carries no primary key.

    Read as a definition, not as a name: a table of the right name whose
    primary key is on `value` — or which has none at all — would let two rows
    claim the same key, and `get_state` reads exactly one row per key.
    """
    row = bind.execute(
        sa.text(
            "SELECT (SELECT array_agg(a.attname ORDER BY k.ord) "
            "          FROM unnest(c.conkey) WITH ORDINALITY AS k(attnum, ord) "
            "          JOIN pg_attribute a ON a.attrelid = c.conrelid "
            "                             AND a.attnum = k.attnum) AS pk_columns "
            "FROM pg_constraint c "
            "WHERE c.conrelid = CAST(:table AS regclass) AND c.contype = 'p'"
        ),
        {"table": STATE_TABLE},
    ).first()
    if row is None:
        return None
    return list(row.pk_columns or [])


def _canonical_check(bind) -> str:
    """What this server renders `KEY_PREDICATE` as, measured not guessed.

    013's scratch-TEMP-table device, as 019 uses it. A hand-written expected
    string pins the migration to one PostgreSQL major's normalization of
    `IN (...)`; deriving it from an empty temp table carrying the identical
    declaration — same column name, same type, same predicate — cannot drift.

    Needs the TEMP privilege on the database. The deploy's owner role has it,
    as 013 already requires.
    """
    scratch = "_omcp_023_check_probe"
    bind.execute(sa.text(f"DROP TABLE IF EXISTS pg_temp.{scratch}"))
    bind.execute(
        sa.text(
            f"CREATE TEMP TABLE {scratch} "
            f'("key" varchar(64), CONSTRAINT {CHECK_NAME}_probe '
            f"CHECK ({KEY_PREDICATE}))"
        )
    )
    rendered = bind.execute(
        sa.text(
            "SELECT pg_get_constraintdef(c.oid) FROM pg_constraint c "
            "WHERE c.conrelid = CAST(:scratch AS regclass) AND c.contype = 'c'"
        ),
        {"scratch": f"pg_temp.{scratch}"},
    ).scalar()
    bind.execute(sa.text(f"DROP TABLE IF EXISTS pg_temp.{scratch}"))
    return rendered


def _check_constraints(bind):
    """Every CHECK on the table, `(definition, convalidated, comment)`.

    Resolved through `conrelid` and `contype`, **never by name**: 013's rule.
    A `CHECK (true)` carrying the expected name satisfies a lookup by name
    while enforcing nothing, and the thing it would stop enforcing is the
    closed key set that keeps a mistyped key from reading as "no fingerprint
    stored".
    """
    return bind.execute(
        sa.text(
            "SELECT pg_get_constraintdef(c.oid) AS definition, "
            "       c.convalidated, "
            "       obj_description(c.oid, 'pg_constraint') AS comment "
            "FROM pg_constraint c "
            "WHERE c.conrelid = CAST(:table AS regclass) AND c.contype = 'c' "
            "ORDER BY c.conname"
        ),
        {"table": STATE_TABLE},
    ).fetchall()


def _column_state(bind):
    """`(formatted_type, attnotnull, default_expr, comment)` for
    `notes_metadata.chunks_truncated`, or None if absent."""
    return bind.execute(
        sa.text(
            "SELECT format_type(a.atttypid, a.atttypmod) AS coltype, "
            "       a.attnotnull, "
            "       pg_get_expr(d.adbin, d.adrelid) AS coldefault, "
            "       col_description(a.attrelid, a.attnum) AS comment "
            "FROM pg_attribute a "
            "LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum "
            "WHERE a.attrelid = CAST(:table AS regclass) AND a.attname = :column "
            "  AND a.attnum > 0 AND NOT a.attisdropped"
        ),
        {"table": NOTES_TABLE, "column": CHUNKS_COLUMN},
    ).first()


# --------------------------------------------------------------------------
# unit 1 — indexer_state
# --------------------------------------------------------------------------


def _create_table() -> None:
    op.create_table(
        STATE_TABLE,
        sa.Column("key", sa.String(length=64), primary_key=True),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(KEY_PREDICATE, name=CHECK_NAME),
    )
    # Stamped in the same transaction as the CREATE, so the markers and the
    # objects can never disagree about who made them. `COMMENT ON` is utility
    # DDL and takes no bind parameter, so the literals are quoted.
    op.execute(f"COMMENT ON TABLE {STATE_TABLE} IS {_quote(TABLE_MARKER)}")
    op.execute(
        f"COMMENT ON CONSTRAINT {CHECK_NAME} ON {STATE_TABLE} "
        f"IS {_quote(CHECK_MARKER)}"
    )


def _verify_table(bind) -> None:
    """Accept a pre-existing `indexer_state` only if it is exactly 023's."""
    problems = []

    if _table_comment(bind) != TABLE_MARKER:
        problems.append("it does not carry 023's table comment marker")

    columns = _columns(bind)
    if columns != list(EXPECTED_COLUMNS):
        problems.append(f"its columns are {columns}, not {list(EXPECTED_COLUMNS)}")

    pk = _primary_key_columns(bind)
    if pk != ["key"]:
        problems.append(
            "its primary key is "
            + ("absent" if pk is None else f"on {pk}")
            + ", not on ['key'] — two rows could then claim one key"
        )

    checks = _check_constraints(bind)
    canonical = _canonical_check(bind)
    if len(checks) != 1:
        problems.append(
            f"it carries {len(checks)} CHECK constraints, not one"
            + (f" ({[c.definition for c in checks]})" if checks else "")
        )
    else:
        check = checks[0]
        if check.definition != canonical:
            problems.append(
                f"its CHECK is {check.definition!r}, not the key predicate "
                f"{canonical!r}"
            )
        if not check.convalidated:
            problems.append(
                "its CHECK is NOT VALID, so existing rows were never checked "
                "and the table may already hold a key nothing will ever read"
            )
        if check.comment != CHECK_MARKER:
            problems.append("its CHECK does not carry 023's constraint marker")

    if problems:
        raise RuntimeError(
            f"{STATE_TABLE} already exists but {'; '.join(problems)}. 023 will "
            "not adopt a table of unknown provenance: startup reads it for the "
            "fingerprints that decide whether the stored vectors and keyword "
            "vectors mean what they claim, and a key it cannot see reads as "
            "'nothing stored', which makes the guard adopt instead of refuse. "
            "Resolve by hand — drop it and let 023 create it, or make it match "
            "— then re-run. Nothing has been changed."
        )


# --------------------------------------------------------------------------
# unit 2 — notes_metadata.chunks_truncated
# --------------------------------------------------------------------------


def _reconcile_column(bind) -> None:
    """Create the column, or verify a pre-existing one is exactly 023's.

    013's philosophy, and 022's application of it: reconcile a database that
    demonstrably has our shape, refuse to guess for one that does not. The
    whole shape is checked, not just the name — a nullable column would let the
    vector tools read `NULL` as "not truncated" for a note whose embedding *is*
    capped, and a column defaulting to `true` would make every note claim a
    truncation that never happened. Neither is visible to `alembic check`'s
    notion of "the column exists", and both produce the silently-wrong answer
    the marker exists to prevent — a result from the head of a 2 MB note read
    as a result from the whole note.
    """
    state = _column_state(bind)
    if state is None:
        op.add_column(
            NOTES_TABLE,
            sa.Column(
                CHUNKS_COLUMN,
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("false"),
            ),
        )
        op.execute(
            f"COMMENT ON COLUMN {NOTES_TABLE}.{CHUNKS_COLUMN} "
            f"IS {_quote(COLUMN_MARKER)}"
        )
        return

    coltype, notnull, default, comment = state
    problems = []
    if coltype != CHUNKS_EXPECTED_TYPE:
        problems.append(f"it is {coltype}, not {CHUNKS_EXPECTED_TYPE}")
    if not notnull:
        problems.append("it is nullable; 023 creates it NOT NULL")
    if default != CHUNKS_EXPECTED_DEFAULT:
        problems.append(
            f"its server default is {default!r}, not {CHUNKS_EXPECTED_DEFAULT!r}"
        )
    if comment != COLUMN_MARKER:
        problems.append("it does not carry 023's comment marker")
    if problems:
        raise RuntimeError(
            f"{NOTES_TABLE}.{CHUNKS_COLUMN} already exists but "
            f"{'; '.join(problems)}. 023 will not adopt a column of unknown "
            "provenance: the vector tools read it as whether that note's "
            "embedding covers the whole note, and a wrong value either hides a "
            "capped note from an agent or invents a cap that never happened. "
            "Resolve by hand — drop it and let 023 create it, or make it match "
            "— then re-run. Nothing has been changed."
        )


# --------------------------------------------------------------------------
# upgrade / downgrade
# --------------------------------------------------------------------------


def upgrade() -> None:
    bind = op.get_bind()

    # Fail fast rather than queueing behind a long-lived transaction: the
    # deploy migrates before recreating the container, so a stalled migration
    # is a stalled deploy while the old container is still serving. Per
    # statement and per lock acquisition, not a budget for the transaction.
    op.execute("SET LOCAL lock_timeout = '10s'")
    op.execute("SET LOCAL statement_timeout = '60s'")
    _pin_search_path()

    if _table_exists(bind):
        _verify_table(bind)
    else:
        _create_table()
    _assert_is_the_qualified_table(bind)

    _reconcile_column(bind)

    # **No backfill and no state row**, deliberately — see the module
    # docstring. The column's server default already gives every pre-existing
    # row the one correct value, and a fingerprint written here would assert
    # the very thing it exists to test.

    # `SET LOCAL` is scoped to the transaction and alembic runs every pending
    # revision in *one*, so without this the next revision would silently
    # inherit these timeouts and blame its own SQL when it tripped them. The
    # same applies to the `search_path` pin — 021 `RESET`s its own, which is
    # exactly why this revision needed a pin of its own.
    op.execute("RESET lock_timeout")
    op.execute("RESET statement_timeout")
    op.execute("RESET search_path")


def downgrade() -> None:
    """Drop each unit only if it carries 023's marker.

    013's rule: a downgrade must undo *this* migration, not delete a table or a
    column somebody else put there under these names. The marker is the only
    evidence of authorship, and each unit is decided on its own marker.

    Dropping `indexer_state` discards both fingerprints and the rotation
    cursor. Nothing is lost that cannot be re-derived: the previous build reads
    neither, and a later re-upgrade leaves the table empty, which the startup
    adoption rule reads as "unknown" and re-adopts. It does mean the re-adopted
    fingerprint blesses whatever is configured at that moment, so a
    downgrade/re-upgrade round trip is not a way to change the model.

    Dropping `chunks_truncated` discards every recorded truncation, and
    re-upgrading recreates the column at `false` for every row. **Nothing
    re-derives it on its own:** an unchanged note whose `embedded_content_hash`
    still matches its `content_hash` is not re-selected by the backlog, so its
    marker stays `false` while its stored vectors are still the capped set —
    the vector tools would then present a capped note as complete. After a
    round trip the marker must be repaired by `make reset-embeddings` or a
    reindex that changes every note.
    """
    bind = op.get_bind()
    # `op.drop_column` and `op.drop_table` resolve through `search_path` too,
    # so the same pin decides *which* objects a downgrade would remove.
    _pin_search_path()

    state = _column_state(bind)
    if state is not None:
        if state[3] != COLUMN_MARKER:
            raise RuntimeError(
                f"{NOTES_TABLE}.{CHUNKS_COLUMN} does not carry 023's comment "
                f"marker ({COLUMN_MARKER!r}), so 023 did not create it and "
                "will not drop it. Nothing has been changed. Remove it by hand "
                "if you mean to."
            )
        op.drop_column(NOTES_TABLE, CHUNKS_COLUMN)

    if _table_exists(bind):
        if _table_comment(bind) != TABLE_MARKER:
            raise RuntimeError(
                f"{STATE_TABLE} does not carry 023's comment marker "
                f"({TABLE_MARKER!r}), so 023 did not create it and will not "
                "drop it. Nothing has been changed. Remove it by hand if you "
                "mean to."
            )
        op.drop_table(STATE_TABLE)

    op.execute("RESET search_path")
