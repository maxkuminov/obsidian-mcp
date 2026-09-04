"""Record that a note's link extraction was capped (#203).

Link extraction is now bounded per note by `MAX_LINKS_PER_NOTE` (10,000): one
10 MiB note of `[[a]] ` yielded 1.75 M `ExtractedLink` objects, an 802 MiB peak
against a 2 GB container, multiplied by every such note in one index pass. The
cap keeps the pass alive, but a capped note has *fewer link rows than the note
has links*, and nothing else on the row can see that.

## Why a column and not only a log line

The truncation is already logged at ERROR. That is not enough. The error ring
buffer the ops-health page reads is 100 entries and process-lifetime, so the
one line naming a note capped at deploy time is gone by the next restart, while
the capped `note_links` rows persist indefinitely. `get_links`,
`get_backlinks` and `get_neighborhood` would go on answering from that set as
though it were complete — a silently wrong graph answer handed to an agent that
acts on it without a human ever seeing the query, which is the failure mode
this server ranks highest (`CLAUDE.md`). The marker has to be as durable as the
truncation it describes, so it lives on the row.

Nor is it a skip. A.7a's skip list withholds a re-derive's certification, and a
tenant with a single generated MOC over the cap would then be held in
re-derive mode for ever — a self-inflicted DoS on the index-integrity
machinery. The truncation is deterministic and the rows written are exactly the
rows derived, so the pass's structural claim still holds; the degradation is
carried on the row rather than in the pass's skip list. See
`index-integrity`, "A capped note does not withhold the record".

## Shape, and why the server default is load-bearing

`BOOLEAN NOT NULL DEFAULT FALSE`. The server default is what makes this an
`ADD COLUMN` PostgreSQL satisfies from the catalogue rather than a full table
rewrite, on a table with one row per note carrying a `tsvector` and two GIN
indexes. It also gives every pre-existing row the correct value: every row that
exists when this runs was written by the *unbounded* extractor, which could not
truncate, so `false` is not a placeholder but the truth.

## No backfill, and re-running is therefore non-destructive

Nothing here writes a value: the default supplies `false`, and only the indexer
ever sets or clears the marker (it sets it on a capped note and clears it when a
later extraction of that note completes under the cap). A stamp-back re-run
(the schema gate does `alembic stamp 021` then `upgrade head`) reconciles the
existing column and writes nothing, so it cannot erase a marker the indexer has
since recorded.

## The deploy window

The deploy migrates and *then* recreates the container, so the previous code
serves for a few seconds against the new column. That code neither reads nor
writes it and its upserts omit it, so the default supplies `false` — which is
the correct value for anything the old, unbounded extractor derived. Nothing is
mis-recorded.

The first pass after deploy is a full re-extraction for a different reason —
`CURRENT_EXTRACTION_VERSION` moves to 2 with this change because the link
grammar changed — and it is that pass which first sets this column on any note
genuinely over the cap.

## Locks

One `ALTER TABLE ... ADD COLUMN` with a constant default, which takes ACCESS
EXCLUSIVE on `notes_metadata` and holds it to COMMIT but performs no rewrite.
`lock_timeout` / `statement_timeout` make a blocked migration fail fast instead
of stalling the deploy, and both are `RESET` at the end because alembic runs
every pending revision in one transaction and `SET LOCAL` would otherwise leak
into a later revision (013 through 018 do the same).

Revision ID: 022
Revises: 021
Create Date: 2026-09-04
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "022"
down_revision: Union[str, None] = "021"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


TABLE = "notes_metadata"
COLUMN = "links_truncated"
EXPECTED_TYPE = "boolean"
EXPECTED_DEFAULT = "false"

# 013's device, and 015's through 021's: the migration marks what it created,
# so `downgrade()` can tell its own work from somebody else's and drop only the
# former.
#
# Declared on the ORM column too (`src/models/db.py`,
# `_LINKS_TRUNCATED_COLUMN_MARKER`), so `alembic check` compares it like any
# other column attribute: a marker that drifted from the model, or one silently
# dropped, is a dirty check rather than a migration that quietly stops
# recognising its own work. Keep the two byte identical.
MARKER = "link-extraction truncation marker (022_links_truncated)"


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
    """Create the column, or verify a pre-existing one is exactly 022's.

    013's philosophy: reconcile a database that demonstrably has our shape,
    refuse to guess for one that does not. The whole shape is checked, not just
    the name — a nullable column would let `get_links` read `NULL` as "not
    truncated" for a note whose links *are* truncated, and a column defaulting
    to `true` would make every note claim a truncation that never happened.
    Neither is visible to `alembic check`'s notion of "the column exists", and
    both produce exactly the silently-wrong graph answer this column exists to
    prevent.
    """
    state = _column_state(bind)
    if state is None:
        op.add_column(
            TABLE,
            sa.Column(
                COLUMN,
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("false"),
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
        problems.append("it is nullable; 022 creates it NOT NULL")
    if default != EXPECTED_DEFAULT:
        problems.append(
            f"its server default is {default!r}, not {EXPECTED_DEFAULT!r}"
        )
    if comment != MARKER:
        problems.append("it does not carry 022's comment marker")
    if problems:
        raise RuntimeError(
            f"{TABLE}.{COLUMN} already exists but {'; '.join(problems)}. 022 "
            "will not adopt a column of unknown provenance: `get_links` reads "
            "it as whether that note's link set is complete, and a wrong value "
            "either hides a truncation from an agent or invents one. Resolve "
            "by hand — drop it and let 022 create it, or make it match — then "
            "re-run. Nothing has been changed."
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
    # only the indexer ever sets or clears the marker.

    # `SET LOCAL` is scoped to the transaction and alembic runs every pending
    # revision in *one*, so without this the next revision would silently
    # inherit these timeouts and blame its own SQL when it tripped them.
    op.execute("RESET lock_timeout")
    op.execute("RESET statement_timeout")


def downgrade() -> None:
    """Drop the column only if it carries 022's marker.

    013's rule: a downgrade must undo *this* migration, not delete a column
    somebody else put there under this name. The marker is the only evidence of
    authorship.

    Downgrading discards every recorded truncation, and re-upgrading recreates
    the column at `false` for every row. **Nothing re-derives it on its own:**
    an unchanged note whose `content_hash` and `extraction_version` both still
    match is skipped by the scan, so its links are never re-extracted and its
    marker stays `false` while its persisted link set is still the capped one —
    `get_links` would then report an incomplete set as complete, which is the
    silently-wrong-answer failure the column exists to prevent. After a
    downgrade/re-upgrade round trip the marker must be repaired by a reindex /
    re-derive (`make reindex`) or by an `extraction_version` bump, either of
    which makes every note changed again. Nothing else is lost — the marker is
    derived state, not a fact only this column holds.
    """
    bind = op.get_bind()
    state = _column_state(bind)
    if state is None:
        return
    if state[3] != MARKER:
        raise RuntimeError(
            f"{TABLE}.{COLUMN} does not carry 022's comment marker "
            f"({MARKER!r}), so 022 did not create it and will not drop it. "
            "Nothing has been changed. Remove it by hand if you mean to."
        )
    op.drop_column(TABLE, COLUMN)
