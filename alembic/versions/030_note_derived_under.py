"""`notes_metadata.derived_under`: per-row re-derive progress (#311).

A scope whose index provenance is unresolved is **re-derived**: every row is
rewritten from the assigned root before the provenance record is stamped. The
pass had no memory of its own progress, so one row-backed unreadable file
made every tick a full-scope rewrite (upsert, keyword vector, links) — #308's
write amplification by a rarer route.

This column records, per row, a SHA-256 digest of the root facts a
re-deriving pass observed (the provenance stamp's own triple) bound to the
row's own `file_path`, `content_hash` and `extraction_version`. A repeated
re-derive skips rows whose marker equals the digest it expects for them, and
the stamp is recorded only when every surviving row matches. See
"Re-derive progress (#311)" in docs/architecture/indexing-and-embeddings.md.

## No backfill

Every existing row reads NULL — "not derived under the current root". A
backfill from the recorded provenance would be pointless as well as a claim
the migration never observed (016's rule): a re-derive happens only when the
observed facts differ from the recorded ones, so a digest of the recorded
facts can never equal what a re-derive expects. A scope in re-derive at
deploy time pays one more full re-derive, then progresses.

## Reconciliation, not adoption

026's shape. The gate stamps back and re-runs this body against a database
that already carries the column, so a same-named column is verified —
`character varying(64)`, nullable, no default, 030's comment marker — and any
other shape is refused by name: a marker column of unknown provenance is a
row the pass may treat as already derived. `downgrade()` drops only a marked
column; the previous build neither reads nor writes it.

`search_path` is pinned and asserted, and `lock_timeout` / `statement_timeout`
set and `RESET`, for 024's and 025's reasons.

Revision ID: 030
Revises: 029
Create Date: 2026-10-10
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "030"
down_revision: Union[str, None] = "029"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


TABLE = "notes_metadata"
QUALIFIED = "public.notes_metadata"
COLUMN = "derived_under"
EXPECTED_TYPE = "character varying(64)"

# Byte identical to `_DERIVED_UNDER_COLUMN_MARKER` in `src/models/db.py`,
# where it is the column's comment so `alembic check` compares it.
MARKER = "re-derive progress digest (030_note_derived_under)"


def _quote(value: str) -> str:
    """A single-quoted SQL string literal (`COMMENT ON` takes no bind)."""
    return "'" + value.replace("'", "''") + "'"


def _oid(bind, name: str):
    """The OID `name` resolves to, or None. `to_regclass` never raises."""
    return bind.execute(
        sa.text("SELECT CAST(to_regclass(:name) AS oid)"), {"name": name}
    ).scalar()


def _column_state(bind):
    """`(format_type, attnotnull, default_expr, comment)`, or None if absent."""
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
        {"table": QUALIFIED, "column": COLUMN},
    ).first()


def _pin_search_path() -> None:
    op.execute("SET LOCAL search_path TO public")


def _assert_is_the_qualified_table(bind) -> None:
    qualified = _oid(bind, QUALIFIED)
    unqualified = _oid(bind, TABLE)
    if qualified is None or qualified != unqualified:
        raise RuntimeError(
            f"030's {TABLE} is not the table {QUALIFIED} resolves to "
            f"(search_path-relative oid {unqualified!r}, qualified oid "
            f"{qualified!r}). Set the migration role's search_path so "
            "`public` comes first, then re-run."
        )


def _reconcile_column(bind) -> None:
    state = _column_state(bind)
    if state is None:
        op.add_column(TABLE, sa.Column(COLUMN, sa.String(64), nullable=True))
        op.execute(f"COMMENT ON COLUMN {TABLE}.{COLUMN} IS {_quote(MARKER)}")
        return
    coltype, notnull, default, comment = state
    problems = []
    if coltype != EXPECTED_TYPE:
        problems.append(f"{TABLE}.{COLUMN} is {coltype}, not {EXPECTED_TYPE}")
    if notnull:
        problems.append(
            f"{TABLE}.{COLUMN} is NOT NULL; 030 creates it nullable, and NULL "
            "is the value that means 'not derived under the current root'"
        )
    if default is not None:
        problems.append(
            f"{TABLE}.{COLUMN} has a server default of {default!r}; 030 "
            "creates none, and a default asserts a derivation nobody performed"
        )
    if comment != MARKER:
        problems.append(f"{TABLE}.{COLUMN} does not carry 030's comment marker")
    if problems:
        raise RuntimeError(
            f"030 will not adopt a re-derive marker column of unknown "
            f"provenance: {'; '.join(problems)}. The index pass skips "
            "re-deriving a row whose marker matches, so a shape this migration "
            "did not create is a row from another vault the pass may certify. "
            "Resolve by hand — drop the column and let 030 create it — then "
            "re-run. Nothing has been changed."
        )


def upgrade() -> None:
    bind = op.get_bind()
    op.execute("SET LOCAL lock_timeout = '10s'")
    op.execute("SET LOCAL statement_timeout = '60s'")
    _pin_search_path()

    _assert_is_the_qualified_table(bind)
    _reconcile_column(bind)

    op.execute("RESET lock_timeout")
    op.execute("RESET statement_timeout")
    op.execute("RESET search_path")


def downgrade() -> None:
    """Drop the column only if it carries 030's marker.

    Losing every marker is safe: the previous build neither reads nor writes
    it, and a re-upgrade leaves every row NULL — "not derived", i.e. one more
    full re-derive for a scope that is in one.
    """
    bind = op.get_bind()
    _pin_search_path()
    if _oid(bind, QUALIFIED) is None:
        op.execute("RESET search_path")
        return
    _assert_is_the_qualified_table(bind)
    state = _column_state(bind)
    if state is not None:
        if state[3] != MARKER:
            print(
                f"030 downgrade: leaving {TABLE}.{COLUMN} in place. It does not "
                f"carry 030's marker ({MARKER!r}) — its comment is "
                f"{state[3]!r} — so 030 did not create it and will not drop it. "
                "Remove it by hand if you mean to.",
                flush=True,
            )
        else:
            op.drop_column(TABLE, COLUMN)
    op.execute("RESET search_path")
