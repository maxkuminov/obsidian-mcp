"""Record one row per successful database backup (#163).

Backups are written **host-side** by `make db-backup` into `$(DATA_DIR)/backups`
— a directory the container deliberately cannot see, and must not: mounting it
would put a host-specific path into a public repo's compose file and give the
application write access to its own backups. So backup freshness reaches the
panel through the database instead. The target inserts a row here through the
same `docker exec … psql` channel it already uses for `pg_dump`, and the health
page answers "when was the last backup" without touching the filesystem it is
reporting on.

## Shape

`id, created_at, filename, size_bytes`. `filename` is the **basename** of the
dump, not its path — the directory is a deployment-local constant that differs
between the repo default and the real host (`Makefile.local`), and writing it
into a shared table would put a host path in the one place this repo keeps them
out of. `size_bytes` is `bigint`: a dump gzips to megabytes today and an
`integer` stops being able to hold one at 2 GiB, which a few hundred thousand
notes reach without anything going wrong.

The CHECK `size_bytes >= 1` is the one invariant worth enforcing here. `db-backup`
already refuses a zero-byte dump before it records anything (that refusal is
issue-era history: `pg_dump` can write nothing and still exit 0 when it is
pointed at the wrong thing); this makes the same refusal true of the *database*
rather than of one shell script, because a recorded backup of zero bytes is not
a backup and the page would render it as one. Mirrored in `src/models/db.py` as
`BACKUP_MIN_SIZE_BYTES` and written out here as a literal rather than imported:
a migration must keep describing the schema it created even after the model
moves on.

## Bootstrap, and why the writer is guarded rather than this migration

`make deploy` runs `db-backup` **before** `db-migrate`, so on the deploy that
ships this revision the dump is taken against a database that has no
`backups_log` in it yet. That ordering is not a bug to fix here — the backup is
the only way back from a bad migration, so it has to precede the migration. The
guard therefore lives in the writer: `docker/record-backup.sh` checks
`to_regclass('public.backups_log')`, warns loudly and exits 0 when the table is
absent, and fails the target when the table exists and the insert does not
land. The first recorded backup is the first one taken after this revision is
live.

## No backfill

There is nothing to backfill from. The dumps on disk are the only record any
deployment has, they live on a host this migration cannot read, and their
filenames encode a timestamp in the *host's* local time. Inventing rows from
them would put a claim in the table that nothing verified. A fresh table is the
honest starting point, and the page renders "no backup recorded yet" until the
next `db-backup`.

## Reconciliation, and why it is not a bare CREATE TABLE

The schema gate exercises idempotence by `alembic stamp 020` then
`upgrade head`, so this revision runs against a database that already carries
its table. A bare `CREATE TABLE` raises there; `IF NOT EXISTS` is worse, because
it adopts *any* table of that name — including one whose `created_at` is
nullable (a row the page would sort to nowhere), one with no size CHECK, or one
whose `ix_backups_log_created_at` was recreated on `size_bytes` — and the panel
then reports whatever it holds to an operator as the age of their last backup.
That is a claim about disaster recovery, which is the worst possible thing to
be casually wrong about. So 013's rule applies: reconcile a database that
demonstrably has our shape, refuse to guess for one that does not.

The table carries a COMMENT marker, exactly as 013/015/016/017/018/019/020 mark
what they create, and that marker is what `downgrade()` keys on: a table of this
name that 021 did not create is never dropped.

**The verification is of definitions, not of names** (019's rule, verbatim).
The CHECK predicate and the `created_at` default are both compared against the
server's own normalization of the same declaration, derived at runtime from a
scratch `TEMP` table rather than pinned to one PostgreSQL major — which needs
the TEMP privilege on the database, as 013 already requires. The index is read
as a column list plus uniqueness, validity and whether it is partial or over an
expression: an index of this name recreated on `filename` keeps the name an
existence check looks for while the newest-first read the page and the strip
both perform has nothing to lean on.

The **primary key and both server defaults** are read for a reason `alembic
check` makes necessary: autogenerate compares neither. It reports a table whose
PK has been dropped as being in perfect agreement with the model, and it never
looks at a server default at all. The default is the quietest of the two:
`ALTER COLUMN created_at SET DEFAULT now() + interval '100 years'` leaves every
column, type, nullability, constraint and index exactly as 021 made them, and
every backup recorded afterwards reads as permanently fresh — so the staleness
warning this whole feature exists to raise can never fire again. `id`'s default
is compared against `nextval` on the sequence `pg_get_serial_sequence` says the
column actually owns, both sides rendered by the server so neither is a guess
about the schema prefix.

**Every catalog lookup resolves `public.backups_log`, qualified.** The writer
and the panel both name it that way, and an unqualified reference resolves
through `search_path` — so the three could otherwise be addressing three
different tables. `op.create_table` still takes no schema (matching 019/020),
so `upgrade()` asserts afterwards that what it created is the object those two
consumers address, and `downgrade()` asserts the same before it drops anything.

## Locks

`CREATE TABLE` blocks nothing already in use — there is no foreign key here, so
not even the `ACCESS SHARE` that 019 takes to validate one. `lock_timeout` /
`statement_timeout` make a blocked migration fail fast instead of stalling the
deploy, and both are `RESET` at the end because alembic runs every pending
revision in one transaction and `SET LOCAL` would otherwise leak into a later
revision (013 through 020 do the same).

Revision ID: 021
Revises: 020
Create Date: 2026-08-29
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "021"
down_revision: Union[str, None] = "020"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


TABLE = "backups_log"

# **Every catalog lookup below resolves this**, not the bare name. The writer
# (`docker/record-backup.sh`) inserts into `public.backups_log` and the panel
# (`src/services/ops_health.py`) selects from it, both qualified, because an
# unqualified reference resolves through `search_path` — so a role or database
# pointing somewhere else would have the writer, the reader and this migration
# each addressing a different table of that name. `op.create_table` has no
# schema argument here (repo convention, matching 019/020), so `upgrade()` ends
# by asserting that what it created or adopted really is the object those two
# address; see `_assert_is_the_qualified_table`.
QUALIFIED = "public.backups_log"

CHECK_NAME = "ck_backups_log_size_bytes"
CREATED_INDEX = "ix_backups_log_created_at"

# Mirrored from `src.models.db.BACKUP_MIN_SIZE_BYTES`. Written out as a literal
# rather than imported, for the reason 019 and 020 give: a migration must keep
# describing the schema it created even after the model moves on.
MIN_SIZE_BYTES = 1

SIZE_PREDICATE = f"size_bytes >= {MIN_SIZE_BYTES}"

# 013's device, and 015's through 020's: the migration marks what it created,
# so `downgrade()` can tell its own work from somebody else's and drop only the
# former. Must stay byte identical to its mirror in `src/models/db.py`.
MARKER = "one row per recorded database backup (021_backups_log)"

# `(column, format_type, attnotnull)` — the whole shape, in creation order.
EXPECTED_COLUMNS = (
    ("id", "integer", True),
    ("created_at", "timestamp with time zone", True),
    ("filename", "text", True),
    ("size_bytes", "bigint", True),
)

#: `(columns, unique, usable, restricted)` for the one index `_create` makes.
EXPECTED_INDEXES = {
    CREATED_INDEX: (["created_at"], False, True, False),
}


def _quote(value: str) -> str:
    """A single-quoted SQL string literal. `MARKER` is a module constant with
    no quotes in it; the doubling is here so it stays correct if that changes."""
    return "'" + value.replace("'", "''") + "'"


def _oid(bind, name: str):
    """The OID `name` resolves to, or None. `to_regclass` never raises."""
    return bind.execute(
        sa.text("SELECT CAST(to_regclass(:name) AS oid)"), {"name": name}
    ).scalar()


def _table_exists(bind) -> bool:
    return _oid(bind, QUALIFIED) is not None


def _table_comment(bind) -> str | None:
    return bind.execute(
        sa.text("SELECT obj_description(CAST(:qualified AS regclass), 'pg_class')"),
        {"qualified": QUALIFIED},
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
            {"table": QUALIFIED},
        ).fetchall()
    ]


def _column_default(bind, column: str) -> str | None:
    """The rendered server default for `column`, or None when it has none."""
    return bind.execute(
        sa.text(
            "SELECT pg_get_expr(d.adbin, d.adrelid) "
            "FROM pg_attribute a "
            "LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum "
            "WHERE a.attrelid = CAST(:table AS regclass) AND a.attname = :column "
            "  AND a.attnum > 0 AND NOT a.attisdropped"
        ),
        {"table": QUALIFIED, "column": column},
    ).scalar()


def _primary_key_columns(bind):
    """The PK's columns **in order**, or None when there is no PK.

    Not tidiness. Without a primary key the `id` an operator (or a future
    reader) uses to name one backup row is not unique, and `alembic check` does
    not compare primary keys at all — it reports a table of the right columns as
    perfectly in agreement with the model while the constraint the model
    declares is simply gone.
    """
    row = bind.execute(
        sa.text(
            "SELECT (SELECT array_agg(a.attname ORDER BY k.ord) "
            "          FROM unnest(c.conkey) WITH ORDINALITY AS k(attnum, ord) "
            "          JOIN pg_attribute a ON a.attrelid = c.conrelid "
            "                             AND a.attnum = k.attnum) AS columns "
            "FROM pg_constraint c "
            "WHERE c.conrelid = CAST(:table AS regclass) AND c.contype = 'p'"
        ),
        {"table": QUALIFIED},
    ).first()
    return list(row.columns) if row is not None and row.columns else None


def _canonical_check(bind) -> str:
    """What this server renders `SIZE_PREDICATE` as, measured not guessed.

    013's scratch-`TEMP`-table device. A hand-written expected string pins the
    migration to one PostgreSQL major's normalization of a comparison;
    deriving it from an empty temp table carrying the identical declaration
    cannot drift.
    """
    scratch = "_omcp_021_check_probe"
    bind.execute(sa.text(f"DROP TABLE IF EXISTS pg_temp.{scratch}"))
    bind.execute(
        sa.text(
            f"CREATE TEMP TABLE {scratch} "
            f"(size_bytes bigint, CONSTRAINT {CHECK_NAME}_probe "
            f"CHECK ({SIZE_PREDICATE}))"
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


def _canonical_created_at_default(bind) -> str:
    """What this server renders `now()` as for a `timestamptz` column.

    The CHECK's scratch-`TEMP`-table device, applied to a default. The threat it
    closes is specific and silent: `ALTER COLUMN created_at SET DEFAULT now() +
    interval '100 years'` leaves every column, type, nullability, constraint and
    index exactly as 021 made them — `alembic check` does not compare server
    defaults at all — and every backup recorded afterwards reads as permanently
    fresh, so the staleness warning this whole feature exists to raise can never
    fire again.
    """
    scratch = "_omcp_021_default_probe"
    bind.execute(sa.text(f"DROP TABLE IF EXISTS pg_temp.{scratch}"))
    bind.execute(
        sa.text(
            f"CREATE TEMP TABLE {scratch} "
            "(created_at timestamptz NOT NULL DEFAULT now())"
        )
    )
    rendered = bind.execute(
        sa.text(
            "SELECT pg_get_expr(d.adbin, d.adrelid) "
            "FROM pg_attribute a "
            "JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum "
            "WHERE a.attrelid = CAST(:scratch AS regclass) AND a.attname = 'created_at'"
        ),
        {"scratch": f"pg_temp.{scratch}"},
    ).scalar()
    bind.execute(sa.text(f"DROP TABLE IF EXISTS pg_temp.{scratch}"))
    return rendered


def _canonical_id_default(bind) -> str | None:
    """`nextval('<this table's own id sequence>'::regclass)`, or None.

    Derived, never written out: `pg_get_serial_sequence` names the sequence the
    column actually owns, and the regclass is rendered back to text by the same
    code path `pg_get_expr` uses — so the two sides of the comparison cannot
    disagree about whether the schema prefix is included. None means the column
    owns no sequence at all, which is a serial that stopped being one.
    """
    sequence = bind.execute(
        sa.text("SELECT pg_get_serial_sequence(:qualified, 'id')"),
        {"qualified": QUALIFIED},
    ).scalar()
    if sequence is None:
        return None
    rendered = bind.execute(
        sa.text("SELECT CAST(CAST(:seq AS regclass) AS text)"), {"seq": sequence}
    ).scalar()
    return f"nextval('{rendered}'::regclass)"


def _check_state(bind):
    """`(constraintdef, convalidated)` for the size CHECK, or None.

    Resolved by name *and* re-read as a definition. A same-named `CHECK (true)`
    satisfies a lookup by name and enforces nothing — that is issue #53, which
    is why every migration since 013 compares the rendered predicate rather
    than the constraint's existence.
    """
    return bind.execute(
        sa.text(
            "SELECT pg_get_constraintdef(c.oid) AS def, c.convalidated "
            "FROM pg_constraint c "
            "WHERE c.conrelid = CAST(:table AS regclass) AND c.contype = 'c' "
            "  AND c.conname = :name"
        ),
        {"table": QUALIFIED, "name": CHECK_NAME},
    ).first()


def _index_definitions(bind) -> dict:
    """`{name: (columns, unique, usable, restricted)}` — 019's reader verbatim.

    Names are not definitions. `ix_backups_log_created_at` dropped and recreated
    on `filename` keeps the name an existence check looks for while the
    newest-first read the health page and the dashboard strip both perform has
    nothing to lean on.

    `indkey` is an `int2vector`, which has no direct array cast; going through
    its text rendering is the portable idiom. An expression index has `attnum`
    0 there and joins to nothing, which is why `restricted` is read separately
    rather than inferred from a short column list.
    """
    rows = bind.execute(
        sa.text(
            "SELECT ic.relname AS name, "
            "       (SELECT array_agg(a.attname ORDER BY k.ord) "
            "          FROM unnest(string_to_array(CAST(i.indkey AS text), ' ')) "
            "               WITH ORDINALITY AS k(attnum, ord) "
            "          JOIN pg_attribute a ON a.attrelid = i.indrelid "
            "                             AND a.attnum = CAST(k.attnum AS smallint)"
            "       ) AS columns, "
            "       i.indisunique, "
            "       (i.indisvalid AND i.indisready) AS usable, "
            "       (i.indpred IS NOT NULL OR i.indexprs IS NOT NULL) AS restricted "
            "FROM pg_index i JOIN pg_class ic ON ic.oid = i.indexrelid "
            "WHERE i.indrelid = CAST(:table AS regclass)"
        ),
        {"table": QUALIFIED},
    ).fetchall()
    return {
        row.name: (
            list(row.columns or []),
            row.indisunique,
            row.usable,
            row.restricted,
        )
        for row in rows
    }


def _create() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("filename", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.CheckConstraint(SIZE_PREDICATE, name=CHECK_NAME),
    )
    op.create_index(CREATED_INDEX, TABLE, ["created_at"])
    # Stamped in the same transaction as the CREATE, so the marker and the
    # table can never disagree about who made it. `COMMENT ON` is utility DDL
    # and takes no bind parameter, so the literal is quoted.
    op.execute(f"COMMENT ON TABLE {TABLE} IS {_quote(MARKER)}")


def _index_problems(bind) -> list:
    """The index present *and defined as 021 defines it*."""
    live = _index_definitions(bind)
    problems = []
    for name, expected in EXPECTED_INDEXES.items():
        actual = live.get(name)
        if actual is None:
            problems.append(f"it is missing index {name}")
            continue
        if actual != expected:
            columns, unique, usable, restricted = actual
            problems.append(
                f"its index {name} is on {columns} "
                f"(unique={unique}, usable={usable}, partial-or-expression="
                f"{restricted}), not {expected[0]} as a plain, valid, "
                "non-unique index"
            )
    return problems


def _check_problems(bind) -> list:
    state = _check_state(bind)
    if state is None:
        return [
            "it carries no size CHECK, so a zero-byte backup can be recorded "
            "and the page will render it as a backup"
        ]
    live_def, validated = state
    canonical = _canonical_check(bind)
    problems = []
    if live_def != canonical:
        problems.append(f"its CHECK is {live_def!r}, not the size predicate {canonical!r}")
    if not validated:
        problems.append(
            "its size CHECK is NOT VALID, so the existing rows were never "
            "checked and the table may already record a zero-byte backup"
        )
    return problems


def _assert_is_the_qualified_table(bind) -> None:
    """What `op.create_table` just made is what the writer and reader address.

    `op.create_table` takes no schema here (repo convention: 019 and 020 do the
    same), so it creates in the first schema of `search_path`. Both consumers —
    `docker/record-backup.sh`'s INSERT and `src/services/ops_health.py`'s
    SELECT — name `public.backups_log` explicitly. On a database whose
    `search_path` does not start with `public` those are two different tables,
    and the failure is silent in the worst direction: the target records
    backups into a table the panel never reads, so the page warns that no
    backup has been taken while one is taken every day.
    """
    qualified = _oid(bind, QUALIFIED)
    unqualified = _oid(bind, TABLE)
    if qualified is None or qualified != unqualified:
        raise RuntimeError(
            f"021 created {TABLE} in a schema that is not the one "
            f"{QUALIFIED} resolves to (search_path-relative oid "
            f"{unqualified!r}, qualified oid {qualified!r}). `make db-backup` "
            "writes to the qualified name and the panel reads it, so the table "
            "just created would never be written to or read. Set the migration "
            "role's search_path so `public` comes first, then re-run."
        )


def _primary_key_problems(bind) -> list:
    """The PK is `(id)`, and it is there at all.

    `alembic check` does not compare primary keys, so a table whose PK has been
    dropped reports as being in perfect agreement with the model — while the
    `id` the dashboard strip puts in a `#run-…`-style anchor, and the column
    `ORDER BY created_at DESC, id DESC` breaks ties on, is no longer unique.
    """
    pk = _primary_key_columns(bind)
    if pk == ["id"]:
        return []
    if pk is None:
        return [
            "it has no primary key at all, so its id is not unique and the "
            "newest-row read has no deterministic tie-break"
        ]
    return [f"its primary key is {pk}, not ['id']"]


def _default_problems(bind) -> list:
    """Both server defaults, compared exactly against server-derived canonicals.

    `alembic check` does not compare server defaults (`compare_server_default`
    is off by default), so this comparison is the only thing that sees either
    drift — and one of them is the quietest failure this table has:
    `created_at DEFAULT now() + interval '100 years'` leaves every column, type,
    constraint and index exactly as 021 made them, and every backup recorded
    afterwards reads as permanently fresh, so the staleness warning can never
    fire again.
    """
    problems = []

    live_created = _column_default(bind, "created_at")
    canonical_created = _canonical_created_at_default(bind)
    if live_created != canonical_created:
        problems.append(
            f"its created_at default is {live_created!r}, not {canonical_created!r} "
            "— a default that is not the current time makes every backup this "
            "table records read as a different age than it is, and the "
            "staleness warning is computed from exactly that value"
        )

    live_id = _column_default(bind, "id")
    canonical_id = _canonical_id_default(bind)
    if canonical_id is None:
        problems.append(
            "its id column owns no sequence, so it is not the serial 021 "
            f"creates (its default is {live_id!r})"
        )
    elif live_id != canonical_id:
        problems.append(
            f"its id default is {live_id!r}, not {canonical_id!r} — the "
            "sequence the column owns is not the one it draws from"
        )

    return problems


def _verify(bind) -> None:
    """Accept a pre-existing table only if it is exactly 021's."""
    problems = []

    if _table_comment(bind) != MARKER:
        problems.append("it does not carry 021's comment marker")

    columns = _columns(bind)
    if columns != list(EXPECTED_COLUMNS):
        problems.append(f"its columns are {columns}, not {list(EXPECTED_COLUMNS)}")

    problems.extend(_primary_key_problems(bind))
    problems.extend(_default_problems(bind))
    problems.extend(_check_problems(bind))
    problems.extend(_index_problems(bind))

    if problems:
        raise RuntimeError(
            f"{TABLE} already exists but {'; '.join(problems)}. 021 will not "
            "adopt a table of unknown provenance: the panel reads its newest "
            "row and tells an operator that is the age of their last database "
            "backup, which is a claim about disaster recovery and the worst "
            "possible thing to be casually wrong about. Resolve by hand — drop "
            "it and let 021 create it, or make it match — then re-run. Nothing "
            "has been changed."
        )


def upgrade() -> None:
    bind = op.get_bind()

    # Fail fast rather than queueing behind a long-lived transaction: the
    # deploy migrates before recreating the container, so a stalled migration
    # is a stalled deploy while the old container is still serving. Per
    # statement and per lock acquisition, not a budget for the transaction.
    op.execute("SET LOCAL lock_timeout = '10s'")
    op.execute("SET LOCAL statement_timeout = '60s'")

    if _table_exists(bind):
        _verify(bind)
    else:
        _create()
        _assert_is_the_qualified_table(bind)

    # **No backfill**, deliberately — see the module docstring. The dumps on
    # disk live on a host this migration cannot read.

    # `SET LOCAL` is scoped to the transaction and alembic runs every pending
    # revision in *one*, so without this the next revision would silently
    # inherit these timeouts and blame its own SQL when it tripped them.
    op.execute("RESET lock_timeout")
    op.execute("RESET statement_timeout")


def downgrade() -> None:
    """Drop the table only if it carries 021's marker.

    013's rule: a downgrade must undo *this* migration, not delete a table
    somebody else put there under this name. The marker is the only evidence of
    authorship.

    Dropping loses the record of which backups were taken and nothing else —
    the dumps themselves are files on the host and are untouched, and no code
    reads this table for a decision. The page renders its "no backup recorded
    yet" empty state, and the next `db-backup` after a re-upgrade starts a
    fresh history.
    """
    bind = op.get_bind()
    if not _table_exists(bind):
        return
    # `op.drop_table` resolves through `search_path`; every check above resolved
    # `public.backups_log`. Refuse rather than drop a table we did not inspect.
    _assert_is_the_qualified_table(bind)
    if _table_comment(bind) != MARKER:
        raise RuntimeError(
            f"{TABLE} does not carry 021's comment marker ({MARKER!r}), so 021 "
            "did not create it and will not drop it. Nothing has been changed. "
            "Remove it by hand if you mean to."
        )
    op.drop_index(CREATED_INDEX, table_name=TABLE)
    op.drop_table(TABLE)
