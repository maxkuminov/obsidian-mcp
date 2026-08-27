"""Record which fence grammar derived each note's links, tags and vectors (#150).

The fence-grammar change widens what counts as fenced code, so `note_links`
rows, `notes_metadata.tags` and every embedded vector derived under the old
grammar are stale — and **nothing on the row can see that**. Re-derivation is
gated on `content_hash`, the hash of the note's bytes, and a grammar change
does not touch a single byte on disk. Without a second marker the indexer would
skip every unchanged note forever and the vault would keep answering searches
from vectors built over code the masker now hides.

## Why a new column rather than a `content_hash` trick

`content_hash` is `NOT NULL`, so it cannot be nulled to force a re-derive. A
sentinel value is worse than useless: the indexer's move detection pairs a
disappeared path with a new one **by content hash**, and a row carrying a
sentinel matches nothing, so an external rename during the remediation window
would be seen as delete-plus-insert. That destroys the row and cascade-deletes
its `note_embeddings` — a re-embed of the whole note to fix a marker. So
`content_hash` keeps holding the true hash throughout and the marker lives
beside it.

## Shape, and why the server default is load-bearing

`SMALLINT NOT NULL DEFAULT 0`. The server default is what makes this an
`ADD COLUMN` PostgreSQL can satisfy from the catalogue rather than a full table
rewrite, on a table with one row per note and a `tsvector` and a GIN index on
it. It also gives every pre-existing row the correct value: 0 means "derived by
the pre-#150 grammar", which is exactly true of every row that exists when this
runs.

Version 0's frozen recognizer lives in `src/services/embeddings.py`
(`_EXTRACTION_GRAMMARS`) and stays there while any row still carries 0.

## No backfill, and re-running is therefore non-destructive

Nothing here writes a value: the default supplies 0, and only the indexer ever
advances the marker. A stamp-back re-run (the schema gate does `alembic stamp
017` then `upgrade head`) reconciles the existing column and writes nothing, so
it cannot rewrite a marker the indexer has since recorded.

## The deploy window

The deploy migrates and *then* recreates the container, so the previous code
serves for a few seconds against the new column. That code neither reads nor
writes it, and its upserts omit it, so the default supplies 0 — the value the
new code wants for anything the old code derived. Nothing is mis-recorded.

## Locks

One `ALTER TABLE ... ADD COLUMN` with a constant default, which takes ACCESS
EXCLUSIVE on `notes_metadata` and holds it to COMMIT but performs no rewrite.
`lock_timeout` / `statement_timeout` make a blocked migration fail fast instead
of stalling the deploy, and both are `RESET` at the end because alembic runs
every pending revision in one transaction and `SET LOCAL` would otherwise leak
into a later revision (013 through 017 do the same).

Revision ID: 018
Revises: 017
Create Date: 2026-08-27
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "018"
down_revision: Union[str, None] = "017"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


TABLE = "notes_metadata"
COLUMN = "extraction_version"
EXPECTED_TYPE = "smallint"
EXPECTED_DEFAULT = "0"

# 013's device, and 015's, 016's and 017's: the migration marks what it
# created, so `downgrade()` can tell its own work from somebody else's and drop
# only the former.
#
# Declared on the ORM column too (`src/models/db.py`,
# `_EXTRACTION_VERSION_COLUMN_MARKER`), so `alembic check` compares it like any
# other column attribute: a marker that drifted from the model, or one silently
# dropped, is a dirty check rather than a migration that quietly stops
# recognising its own work. Keep the two byte identical.
MARKER = "fence-grammar derivation marker (018_extraction_version)"


def _quote(value: str) -> str:
    """A single-quoted SQL string literal. `MARKER` is a module constant with
    no quotes in it; the doubling is here so it stays correct if that changes."""
    return "'" + value.replace("'", "''") + "'"


def _column_state(bind):
    """`(formatted_type, attnotnull, default_expr, comment)`, or None if absent."""
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
        {"table": TABLE, "column": COLUMN},
    ).first()


def _reconcile(bind) -> None:
    """Create the column, or verify a pre-existing one is exactly 018's.

    013's philosophy: reconcile a database that demonstrably has our shape,
    refuse to guess for one that does not. The whole shape is checked, not just
    the name — a nullable column, or one defaulting to something other than 0,
    would make the indexer read `NULL`/`3` as a version and either crash the
    pass or certify a derivation that never happened, and neither is visible to
    `alembic check`'s notion of "the column exists".
    """
    state = _column_state(bind)
    if state is None:
        op.add_column(
            TABLE,
            sa.Column(
                COLUMN,
                sa.SmallInteger(),
                nullable=False,
                server_default=sa.text("0"),
            ),
        )
        # Stamped in the same transaction as the ADD, so the marker and the
        # column can never disagree about who made it. `COMMENT ON` is utility
        # DDL and takes no bind parameter, so the literal is quoted.
        op.execute(f"COMMENT ON COLUMN {TABLE}.{COLUMN} IS {_quote(MARKER)}")
        return

    coltype, notnull, default, comment = state
    problems = []
    if coltype != EXPECTED_TYPE:
        problems.append(f"it is {coltype}, not {EXPECTED_TYPE}")
    if not notnull:
        problems.append("it is nullable; 018 creates it NOT NULL")
    if default != EXPECTED_DEFAULT:
        problems.append(
            f"its server default is {default!r}, not {EXPECTED_DEFAULT!r}"
        )
    if comment != MARKER:
        problems.append("it does not carry 018's comment marker")
    if problems:
        raise RuntimeError(
            f"{TABLE}.{COLUMN} already exists but {'; '.join(problems)}. 018 "
            "will not adopt a column of unknown provenance: the indexer reads "
            "it as the grammar version a row's links, tags and vectors were "
            "derived under, and a wrong value silently certifies stale derived "
            "state. Resolve by hand — drop it and let 018 create it, or make "
            "it match — then re-run. Nothing has been changed."
        )


def upgrade() -> None:
    bind = op.get_bind()

    # Fail fast rather than queueing behind a long-lived transaction: the
    # deploy migrates before recreating the container, so a stalled migration
    # is a stalled deploy while the old container is still serving. Per
    # statement and per lock acquisition, not a budget for the transaction.
    op.execute("SET LOCAL lock_timeout = '10s'")
    op.execute("SET LOCAL statement_timeout = '60s'")

    _reconcile(bind)

    # **No backfill**, deliberately — see the module docstring. The server
    # default already gives every pre-existing row the one correct value, and
    # only the indexer advances the marker.

    # `SET LOCAL` is scoped to the transaction and alembic runs every pending
    # revision in *one*, so without this the next revision would silently
    # inherit these timeouts and blame its own SQL when it tripped them.
    op.execute("RESET lock_timeout")
    op.execute("RESET statement_timeout")


def downgrade() -> None:
    """Drop the column only if it carries 018's marker.

    013's rule: a downgrade must undo *this* migration, not delete a column
    somebody else put there under this name. The marker is the only evidence of
    authorship.

    Downgrading discards every recorded marker. Re-upgrading recreates the
    column at 0, which the next pass reads as "derived by the pre-#150
    grammar" and re-derives from — bounded and non-destructive, since
    `content_hash` was never touched and note identity therefore survives.
    """
    bind = op.get_bind()
    state = _column_state(bind)
    if state is None:
        return
    if state[3] != MARKER:
        raise RuntimeError(
            f"{TABLE}.{COLUMN} does not carry 018's comment marker "
            f"({MARKER!r}), so 018 did not create it and will not drop it. "
            "Nothing has been changed. Remove it by hand if you mean to."
        )
    op.drop_column(TABLE, COLUMN)
