"""`notes_metadata.stat_*`: the stat the scan's shortcut compares (#282).

The index pass used to read and SHA-256 every note on every tick to learn that
almost none of them had changed. The stat shortcut skips a file's read when its
current `(size, mtime_ns, ctime_ns, inode)` equals the tuple recorded for the
bytes that produced its row's `content_hash` — so the tuple has to be stored,
at nanosecond precision, beside the hash it describes.

## Why four new columns, and not `file_size` / `modified_at`

`modified_at` is `timestamptz`, i.e. microseconds: two writes inside one
microsecond are indistinguishable, and a comparison against it would round
away exactly the precision the racy-stat rule reasons about. Both columns also
keep a display meaning (`list_notes`, `get_recent`) that a comparison key must
not be coupled to. `ctime_ns` is the field userspace cannot set — a restore
that preserves `mtime` still moves `ctime` — and the inode is what makes an
editor's write-to-temp-then-rename visible even at an identical size and
timestamp. The inode is stored reinterpreted as **signed** 64-bit
(`ino - 2**64` when `ino >= 2**63`), because `BIGINT` is signed and some
filesystems hand out inode numbers above `2**63`.

## The CHECK: all NULL or all set

NULL is load-bearing: it means "read and hash this file on the next pass",
and it is what the pass writes for a racy stat, what `move_note` writes, and
what every pre-existing row reads after this migration. A half-recorded tuple
would be neither a stat nor its absence, so the database refuses it. Resolved
through `pg_constraint` by its **definition**, never by name — 013's rule: a
`CHECK (true)` carrying the expected name satisfies a lookup by name while
enforcing nothing. The canonical rendering is measured off a scratch TEMP
table (013's device, as 019 and 023 use it), not hand-written, so it cannot
drift with PostgreSQL's normalisation.

## Metadata-only

No backfill and no default: every existing row reads NULL, which is exactly
"unknown — read it", so the first pass after deploy is an ordinary full-hash
pass that records the stats as it goes (the unchanged-hash refresh path). A
backfill here would have to invent a stat for bytes the migration never read.

## Reconciliation, not adoption

The gate stamps back and re-runs this body against a database that already
carries its columns, so bare DDL would raise and `IF NOT EXISTS` would adopt
*any* column of the name. A column of another type (an `integer` `stat_ino`
truncates inode numbers into false matches), a NOT NULL one, one with a
default, or one without 026's comment marker is refused, and the refusal
names it. The same for a CHECK: one carrying 026's name with a different
definition is refused; one with 026's definition must be validated and
marked.

`search_path` is pinned and `lock_timeout` / `statement_timeout` set and
`RESET`, for 024's and 025's reasons.

Revision ID: 026
Revises: 025
Create Date: 2026-09-22
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "026"
down_revision: Union[str, None] = "025"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


TABLE = "notes_metadata"
QUALIFIED = "public.notes_metadata"
COLUMNS = ("stat_size", "stat_mtime_ns", "stat_ctime_ns", "stat_ino")
EXPECTED_TYPE = "bigint"

CHECK_NAME = "ck_notes_metadata_stat_all_or_none"
# Byte identical to `_NOTE_STAT_PREDICATE` in `src/models/db.py`.
STAT_PREDICATE = (
    "(stat_size IS NULL AND stat_mtime_ns IS NULL "
    "AND stat_ctime_ns IS NULL AND stat_ino IS NULL) OR "
    "(stat_size IS NOT NULL AND stat_mtime_ns IS NOT NULL "
    "AND stat_ctime_ns IS NOT NULL AND stat_ino IS NOT NULL)"
)

# 013's device, and 015's through 025's: the migration marks what it created,
# so `downgrade()` can tell its own work from somebody else's. Must stay byte
# identical to `_NOTE_STAT_COLUMN_MARKER` in `src/models/db.py`, where it is
# declared as each column's comment so `alembic check` compares it.
MARKER = "stat of the bytes content_hash was computed from (026_note_stat_columns)"
# The CHECK's own marker. Not compared by `alembic check` (constraint comments
# are not), so it lives here only.
CHECK_MARKER = "all-or-none stat tuple (026_note_stat_columns)"


def _quote(value: str) -> str:
    """A single-quoted SQL string literal (`COMMENT ON` takes no bind)."""
    return "'" + value.replace("'", "''") + "'"


# --------------------------------------------------------------------------
# catalogue reads
# --------------------------------------------------------------------------


def _oid(bind, name: str):
    """The OID `name` resolves to, or None. `to_regclass` never raises."""
    return bind.execute(
        sa.text("SELECT CAST(to_regclass(:name) AS oid)"), {"name": name}
    ).scalar()


def _column_state(bind, column: str):
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
        {"table": QUALIFIED, "column": column},
    ).first()


def _canonical_check(bind) -> str:
    """What this server renders `STAT_PREDICATE` as, measured not guessed.

    A scratch TEMP table carrying the identical declaration — same column
    names, same type, same predicate — so the comparison cannot drift with a
    PostgreSQL major's normalisation.
    """
    scratch = "_omcp_026_check_probe"
    bind.execute(sa.text(f"DROP TABLE IF EXISTS pg_temp.{scratch}"))
    bind.execute(
        sa.text(
            f"CREATE TEMP TABLE {scratch} ("
            "stat_size bigint, stat_mtime_ns bigint, "
            "stat_ctime_ns bigint, stat_ino bigint, "
            f"CONSTRAINT {CHECK_NAME}_probe CHECK ({STAT_PREDICATE}))"
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
    """Every CHECK on the table: `(name, definition, convalidated, comment)`.

    Enumerated through `conrelid` and `contype`; the caller decides by
    definition, and uses the name only to name a disagreement.
    """
    return bind.execute(
        sa.text(
            "SELECT c.conname AS name, "
            "       pg_get_constraintdef(c.oid) AS definition, "
            "       c.convalidated, "
            "       obj_description(c.oid, 'pg_constraint') AS comment "
            "FROM pg_constraint c "
            "WHERE c.conrelid = CAST(:table AS regclass) AND c.contype = 'c' "
            "ORDER BY c.conname"
        ),
        {"table": QUALIFIED},
    ).fetchall()


# --------------------------------------------------------------------------
# search_path
# --------------------------------------------------------------------------


def _pin_search_path() -> None:
    """Pin `search_path` to `public` for the rest of this transaction.

    021's device, repeated by 023, 024 and 025 — each `RESET`s its own pin,
    which is exactly why 026 cannot rely on one still being in force.
    """
    op.execute("SET LOCAL search_path TO public")


def _assert_is_the_qualified_table(bind) -> None:
    """What the unqualified name resolves to is `public.notes_metadata`.

    Columns added to a decoy `notes_metadata` would leave the indexer writing
    stats the shortcut never reads — or, worse, reading stats nobody wrote.
    """
    qualified = _oid(bind, QUALIFIED)
    unqualified = _oid(bind, TABLE)
    if qualified is None or qualified != unqualified:
        raise RuntimeError(
            f"026's {TABLE} is not the table {QUALIFIED} resolves to "
            f"(search_path-relative oid {unqualified!r}, qualified oid "
            f"{qualified!r}). Set the migration role's search_path so "
            "`public` comes first, then re-run."
        )


# --------------------------------------------------------------------------
# the columns
# --------------------------------------------------------------------------


def _reconcile_columns(bind) -> None:
    problems = []
    missing = []
    for column in COLUMNS:
        state = _column_state(bind, column)
        if state is None:
            missing.append(column)
            continue
        coltype, notnull, default, comment = state
        if coltype != EXPECTED_TYPE:
            problems.append(f"{TABLE}.{column} is {coltype}, not {EXPECTED_TYPE}")
        if notnull:
            problems.append(
                f"{TABLE}.{column} is NOT NULL; 026 creates it nullable, and "
                "NULL is the value that means 'read this file on the next pass'"
            )
        if default is not None:
            problems.append(
                f"{TABLE}.{column} has a server default of {default!r}; 026 "
                "creates none, and a default is a stat for bytes nobody read"
            )
        if comment != MARKER:
            problems.append(f"{TABLE}.{column} does not carry 026's comment marker")
    if problems:
        raise RuntimeError(
            f"026 will not adopt stat columns of unknown provenance: "
            f"{'; '.join(problems)}. The index pass skips reading a note "
            "whenever these columns equal the file's current stat, so a shape "
            "this migration did not create is a note whose edits are never "
            "read. Resolve by hand — drop the column and let 026 create it, or "
            "make it match — then re-run. Nothing has been changed."
        )
    for column in missing:
        op.add_column(TABLE, sa.Column(column, sa.BigInteger(), nullable=True))
        op.execute(f"COMMENT ON COLUMN {TABLE}.{column} IS {_quote(MARKER)}")


def _reconcile_check(bind) -> None:
    canonical = _canonical_check(bind)
    checks = _check_constraints(bind)
    ours = [c for c in checks if c.definition == canonical]
    named = [c for c in checks if c.name == CHECK_NAME]

    problems = []
    for c in named:
        if c.definition != canonical:
            problems.append(
                f"a CHECK named {CHECK_NAME} already exists with definition "
                f"{c.definition!r}, not the all-or-none predicate {canonical!r}"
            )
    if len(ours) > 1:
        problems.append(
            f"{len(ours)} CHECK constraints carry the all-or-none predicate "
            f"({[c.name for c in ours]}), not one"
        )
    for c in ours:
        if not c.convalidated:
            problems.append(
                f"the all-or-none CHECK {c.name} is NOT VALID, so existing rows "
                "were never checked"
            )
        if c.comment != CHECK_MARKER:
            problems.append(
                f"the all-or-none CHECK {c.name} does not carry 026's "
                "constraint marker"
            )
    if problems:
        raise RuntimeError(
            f"{TABLE}'s stat CHECK: {'; '.join(problems)}. 026 will not adopt a "
            "constraint of unknown provenance. Resolve by hand, then re-run. "
            "Nothing has been changed."
        )
    if ours:
        return
    op.create_check_constraint(CHECK_NAME, TABLE, STAT_PREDICATE)
    op.execute(
        f"COMMENT ON CONSTRAINT {CHECK_NAME} ON {TABLE} IS {_quote(CHECK_MARKER)}"
    )


# --------------------------------------------------------------------------
# upgrade / downgrade
# --------------------------------------------------------------------------


def upgrade() -> None:
    bind = op.get_bind()

    # Fail fast rather than queueing behind a long-lived transaction (the
    # index pass holds row locks on this table while it commits).
    op.execute("SET LOCAL lock_timeout = '10s'")
    op.execute("SET LOCAL statement_timeout = '60s'")
    _pin_search_path()

    _assert_is_the_qualified_table(bind)
    _reconcile_columns(bind)
    _reconcile_check(bind)

    op.execute("RESET lock_timeout")
    op.execute("RESET statement_timeout")
    op.execute("RESET search_path")


def _skip_unmarked(what: str, found: str | None, expected: str) -> None:
    """Leave an object of one of 026's names that 026 did not create.

    023's rule: a skip, decided per object, printed to stdout because
    `alembic/env.py` configures no logging.
    """
    print(
        f"026 downgrade: leaving {what} in place. It does not carry 026's "
        f"marker ({expected!r}) — its comment is {found!r} — so 026 did not "
        "create it and will not drop it. Remove it by hand if you mean to.",
        flush=True,
    )


def downgrade() -> None:
    """Drop only the CHECK and the columns that carry 026's markers.

    Dropping them loses every recorded stat, which is safe in the direction
    that matters: the previous build neither reads nor writes them, and a
    re-upgrade leaves every row NULL, i.e. "read it on the next pass".
    """
    bind = op.get_bind()
    _pin_search_path()
    if _oid(bind, QUALIFIED) is None:
        op.execute("RESET search_path")
        return
    _assert_is_the_qualified_table(bind)

    if all(_column_state(bind, c) is not None for c in COLUMNS):
        canonical = _canonical_check(bind)
        for c in _check_constraints(bind):
            if c.definition != canonical:
                continue
            if c.comment != CHECK_MARKER:
                _skip_unmarked(f"CHECK {c.name}", c.comment, CHECK_MARKER)
                continue
            op.drop_constraint(c.name, TABLE, type_="check")

    for column in COLUMNS:
        state = _column_state(bind, column)
        if state is None:
            continue
        if state[3] != MARKER:
            _skip_unmarked(f"{TABLE}.{column}", state[3], MARKER)
            continue
        op.drop_column(TABLE, column)

    op.execute("RESET search_path")
