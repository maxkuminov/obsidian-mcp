"""Opt-in per-key daily request quota (#162).

Three artifacts, one revision, because they are one feature:

1. `api_keys.daily_request_limit` — a nullable integer. NULL is unlimited and
   is what every existing row keeps, so the deploy changes no key's behaviour.
2. `quota_counters(key_id, day, count)` — the admission counter the tool layer
   increments conditionally.
3. `ix_usage_logs_key_id_created_at` — the composite index the usage page's
   per-key filter reads.

## Why a counter table and not a COUNT over `usage_logs`

A quota that counts rows and then decides is raceable in exactly the way that
matters: two concurrent calls both read "99 used, limit 100" and both run. The
counter row is the lock. Admission is one statement —

    INSERT INTO quota_counters (key_id, day, count) VALUES (:k, :d, 1)
    ON CONFLICT (key_id, day) DO UPDATE SET count = quota_counters.count + 1
    WHERE quota_counters.count < :limit
    RETURNING count

— and PostgreSQL evaluates that `WHERE` while holding the conflicting row's
lock, so exactly `limit` calls are admitted per UTC day however many arrive at
once. A returned row admits; no row refuses before the tool body.

The composite PK `(key_id, day)` is what `ON CONFLICT` targets, so it is
load-bearing rather than tidy: without it the statement has no arbiter and
raises. The FK is `ON DELETE CASCADE` because a counter is meaningless without
the key it counts, and because the panel's key delete would otherwise be
blocked by a row the operator cannot see.

`day` is a **date**, and the writer supplies the UTC date. Not `now()::date`,
which is the server's timezone — a limit that resets at an hour nobody
administering it can name is not a limit anybody can reason about.

## The CHECK, and why zero is refused

`daily_request_limit IS NULL OR (>= 1 AND <= 1000000)`. Zero would make the
guarded UPDATE decline every call forever, and a key that refuses everything
reads to its operator as an outage rather than as a setting — revoking the key
is the way to stop it. Negatives are the same fact typed differently. The upper
bound keeps a fat-fingered limit an integer the counter can hold and the copy
can render. The panel validates too; the constraint is what makes the invariant
true of the *database* rather than of one code path.

Mirrored in `src/models/db.py` as `DAILY_REQUEST_LIMIT_MIN` /
`DAILY_REQUEST_LIMIT_MAX` and written out here as a literal rather than
imported: a migration must keep describing the schema it created even after the
model moves on.

## No backfill

There is nothing to backfill. NULL is the correct value for every existing key
— none of them had a limit — and the counter table starts empty because
consumption is defined as admissions *since a limit was enabled*, which no key
has yet done.

## Reconciliation, and why none of the three is a bare CREATE/ADD

The schema gate exercises idempotence by `alembic stamp 019` then
`upgrade head`, so this revision runs against a database that already carries
its work. Bare DDL raises there; `IF NOT EXISTS` is worse, because it adopts
*any* object of that name — a `quota_counters` with a single-column PK (the
`ON CONFLICT` arbiter gone, so every admission raises and every tool call
fails), a `daily_request_limit` that is `NOT NULL DEFAULT 0` (every key
instantly refusing everything), an index of the right name on the wrong
columns. So 013's rule applies: reconcile a database that demonstrably has our
shape, refuse to guess for one that does not.

Each artifact carries a COMMENT marker, and `downgrade()` keys on it: an object
of one of these names that 020 did not create is never dropped.

**The verification is of definitions, not of names.** The FK's referenced table
*and* column, both referential actions and its validated state; the PK's column
list *in order*; the CHECK compared against the server's own normalization of
the same declaration (013's scratch-`TEMP`-table device, so the migration is
not pinned to one PostgreSQL major); each index's column list, uniqueness,
validity and whether it is partial or over an expression.

## Locks

`ADD COLUMN` of a nullable column with no default is metadata-only. The CHECK
is added `NOT VALID` and then `VALIDATE`d, so the exclusive lock is held for
the catalog change and not for a scan of `api_keys` — a table with tens of rows
where that is theatre, and a habit that stops being theatre on a big one.
`CREATE INDEX` on `usage_logs` takes a `SHARE` lock that blocks writes for its
duration; `usage_logs` is append-only and the deploy migrates before recreating
the container, so the blocked writers are the old container's log inserts,
which `_log_usage` already treats as best-effort. `lock_timeout` /
`statement_timeout` make a blocked migration fail fast instead of stalling the
deploy, and both are `RESET` at the end because alembic runs every pending
revision in one transaction and `SET LOCAL` would otherwise leak into a later
revision (013 through 019 do the same).

Revision ID: 020
Revises: 019
Create Date: 2026-08-29
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "020"
down_revision: Union[str, None] = "019"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


KEYS_TABLE = "api_keys"
LIMIT_COLUMN = "daily_request_limit"
CHECK_NAME = "ck_api_keys_daily_request_limit"

COUNTERS_TABLE = "quota_counters"
COUNTERS_PK = "quota_counters_pkey"

USAGE_TABLE = "usage_logs"
USAGE_INDEX = "ix_usage_logs_key_id_created_at"

# Mirrored from `src.models.db`. Written out as literals rather than imported,
# for the reason 019 gives: a migration must keep describing the schema it
# created even after the model moves on, so it pins its own copy.
LIMIT_MIN = 1
LIMIT_MAX = 1_000_000

LIMIT_PREDICATE = (
    f"{LIMIT_COLUMN} IS NULL OR "
    f"({LIMIT_COLUMN} >= {LIMIT_MIN} AND {LIMIT_COLUMN} <= {LIMIT_MAX})"
)

# 013's device, and 015's through 019's: the migration marks what it created,
# so `downgrade()` can tell its own work from somebody else's and drop only the
# former. Three markers because three separately-droppable objects; each must
# stay byte identical to its mirror in `src/models/db.py`.
COLUMN_MARKER = "opt-in per-key daily admission ceiling (020_daily_request_limit)"
TABLE_MARKER = "per-(key, UTC day) admission counter (020_daily_request_limit)"
CHECK_MARKER = "created by 020_daily_request_limit"

#: `(column, format_type, attnotnull)` — the whole shape, in creation order.
EXPECTED_COUNTER_COLUMNS = (
    ("key_id", "integer", True),
    ("day", "date", True),
    ("count", "integer", True),
)

EXPECTED_COUNT_DEFAULT = "0"

#: `(columns, unique, usable, restricted)` for the index 020 adds to
#: `usage_logs`.
EXPECTED_USAGE_INDEX = (["key_id", "created_at"], False, True, False)


def _quote(value: str) -> str:
    """A single-quoted SQL string literal. The markers are module constants
    with no quotes in them; the doubling is here so it stays correct if that
    changes."""
    return "'" + value.replace("'", "''") + "'"


# --- catalog readers -------------------------------------------------------


def _table_exists(bind, table: str) -> bool:
    return bool(
        bind.execute(
            sa.text("SELECT to_regclass(:qualified)"), {"qualified": table}
        ).scalar()
    )


def _table_comment(bind, table: str) -> str | None:
    return bind.execute(
        sa.text(
            "SELECT obj_description(c.oid, 'pg_class') "
            "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE c.relname = :table AND c.relkind = 'r' "
            "  AND n.nspname = ANY (current_schemas(false))"
        ),
        {"table": table},
    ).scalar()


def _column_state(bind, table: str, column: str):
    """`(format_type, attnotnull, default_expr, comment)` or None."""
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
        {"table": table, "column": column},
    ).first()


def _columns(bind, table: str):
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
            {"table": table},
        ).fetchall()
    ]


def _check_state(bind):
    """`(constraintdef, convalidated, comment)` for 020's CHECK, or None.

    Resolved by name *and* re-read as a definition. A same-named
    `CHECK (true)` satisfies a lookup by name and enforces nothing — that is
    issue #53, which is why every migration since 013 compares the rendered
    predicate rather than the constraint's existence.
    """
    return bind.execute(
        sa.text(
            "SELECT pg_get_constraintdef(c.oid) AS def, c.convalidated, "
            "       obj_description(c.oid, 'pg_constraint') AS comment "
            "FROM pg_constraint c "
            "WHERE c.conrelid = CAST(:table AS regclass) AND c.contype = 'c' "
            "  AND c.conname = :name"
        ),
        {"table": KEYS_TABLE, "name": CHECK_NAME},
    ).first()


def _canonical_check(bind) -> str:
    """What this server renders `LIMIT_PREDICATE` as, measured not guessed.

    013's scratch-`TEMP`-table device. A hand-written expected string pins the
    migration to one PostgreSQL major's normalization of a boolean expression;
    deriving it from an empty temp table carrying the identical declaration
    cannot drift.
    """
    scratch = "_omcp_020_check_probe"
    bind.execute(sa.text(f"DROP TABLE IF EXISTS pg_temp.{scratch}"))
    bind.execute(
        sa.text(
            f"CREATE TEMP TABLE {scratch} "
            f"({LIMIT_COLUMN} integer, CONSTRAINT {CHECK_NAME}_probe "
            f"CHECK ({LIMIT_PREDICATE}))"
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


def _regclass_text(bind, name: str) -> str:
    """How this server renders `name` as a `regclass`, so both sides of the
    foreign-key comparison are rendered by the same code rather than one of
    them being a guess at whether `public.` is included."""
    return bind.execute(
        sa.text("SELECT CAST(CAST(:name AS regclass) AS text)"), {"name": name}
    ).scalar()


def _foreign_keys(bind, table: str):
    """Every FK on `table`, completely described — 019's reader verbatim.

    The delete action alone is not the constraint. A `quota_counters` carrying
    020's comment whose `key_id` FK points at `users(id)` passes a
    delete-action check unchanged, and then a quota counts a *user's* id as a
    key's.
    """
    return bind.execute(
        sa.text(
            "SELECT c.conname, "
            "       CAST(c.confrelid AS regclass)::text AS referenced_table, "
            "       c.confdeltype::text AS delete_action, "
            "       c.confupdtype::text AS update_action, "
            "       c.convalidated, "
            "       (SELECT array_agg(a.attname ORDER BY k.ord) "
            "          FROM unnest(c.conkey) WITH ORDINALITY AS k(attnum, ord) "
            "          JOIN pg_attribute a ON a.attrelid = c.conrelid "
            "                             AND a.attnum = k.attnum) AS local_columns, "
            "       (SELECT array_agg(a.attname ORDER BY k.ord) "
            "          FROM unnest(c.confkey) WITH ORDINALITY AS k(attnum, ord) "
            "          JOIN pg_attribute a ON a.attrelid = c.confrelid "
            "                             AND a.attnum = k.attnum) AS referenced_columns "
            "FROM pg_constraint c "
            "WHERE c.conrelid = CAST(:table AS regclass) AND c.contype = 'f' "
            "ORDER BY c.conname"
        ),
        {"table": table},
    ).fetchall()


def _primary_key_columns(bind, table: str):
    """The PK's columns **in order**, or None when there is no PK.

    Order matters and a set comparison would miss it: `ON CONFLICT (key_id,
    day)` needs an arbiter unique index on exactly those two columns, and a PK
    on `(day, key_id)` is a different index. It resolves the same conflict in
    practice — but a table whose PK is `(key_id)` alone collapses every day's
    counter onto one row, which is a quota that never resets, and that is
    invisible to a name-level check.
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
        {"table": table},
    ).first()
    return list(row.columns) if row is not None and row.columns else None


def _index_definitions(bind, table: str) -> dict:
    """`{name: (columns, unique, usable, restricted)}` — 019's reader verbatim.

    Names are not definitions. `ix_usage_logs_key_id_created_at` recreated on
    `(key_id, duration_ms)` keeps the name an existence check looks for while
    the filtered scan the usage page depends on has nothing to lean on.
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
        {"table": table},
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


def _refuse(what: str, problems: list, consequence: str) -> None:
    raise RuntimeError(
        f"{what} already exists but {'; '.join(problems)}. 020 will not adopt "
        f"an object of unknown provenance: {consequence} Resolve by hand — "
        "drop it and let 020 create it, or make it match — then re-run. "
        "Nothing has been changed."
    )


# --- the three artifacts ---------------------------------------------------


def _reconcile_limit_column(bind) -> None:
    state = _column_state(bind, KEYS_TABLE, LIMIT_COLUMN)
    if state is None:
        op.add_column(KEYS_TABLE, sa.Column(LIMIT_COLUMN, sa.Integer(), nullable=True))
        # Stamped in the same transaction as the ADD, so the marker and the
        # column can never disagree about who made it. `COMMENT ON` is utility
        # DDL and takes no bind parameter, so the literal is quoted.
        op.execute(f"COMMENT ON COLUMN {KEYS_TABLE}.{LIMIT_COLUMN} IS {_quote(COLUMN_MARKER)}")
        return

    coltype, notnull, default, comment = state
    problems = []
    if coltype != "integer":
        problems.append(f"it is {coltype}, not integer")
    if notnull:
        problems.append(
            "it is NOT NULL; 020 creates it nullable, and NULL is the value "
            "that means 'unlimited' — a NOT NULL column has no way to say it"
        )
    if default is not None:
        problems.append(
            f"it has a server default of {default!r}; 020 creates none, and a "
            "default is a limit every new key silently acquires"
        )
    if comment != COLUMN_MARKER:
        problems.append("it does not carry 020's comment marker")
    if problems:
        _refuse(
            f"{KEYS_TABLE}.{LIMIT_COLUMN}",
            problems,
            "the tool layer refuses a call whenever this column is non-null "
            "and the day's admissions have reached it, so a wrong shape here "
            "is either a key that cannot call anything or a ceiling that is "
            "never enforced.",
        )


def _reconcile_check(bind) -> None:
    canonical = _canonical_check(bind)
    state = _check_state(bind)
    if state is None:
        # `NOT VALID` then `VALIDATE`: the exclusive lock is held for the
        # catalog change, and the scan of existing rows runs under a lock that
        # does not block readers or writers. Every existing row is NULL, so the
        # validation finds nothing — but the pattern is what keeps this
        # migration honest on a table that is not tiny.
        op.execute(
            f"ALTER TABLE {KEYS_TABLE} ADD CONSTRAINT {CHECK_NAME} "
            f"CHECK ({LIMIT_PREDICATE}) NOT VALID"
        )
        op.execute(f"ALTER TABLE {KEYS_TABLE} VALIDATE CONSTRAINT {CHECK_NAME}")
        op.execute(
            f"COMMENT ON CONSTRAINT {CHECK_NAME} ON {KEYS_TABLE} IS "
            f"{_quote(CHECK_MARKER)}"
        )
        return

    live_def, validated, comment = state
    problems = []
    if live_def != canonical:
        problems.append(f"its predicate is {live_def!r}, not {canonical!r}")
    if not validated:
        problems.append(
            "it is NOT VALID, so the existing rows were never checked and the "
            "table may already hold a zero or negative limit"
        )
    if comment != CHECK_MARKER:
        problems.append("it does not carry 020's comment marker")
    if problems:
        _refuse(
            f"constraint {CHECK_NAME} on {KEYS_TABLE}",
            problems,
            "a same-named CHECK (true) satisfies a lookup by name and enforces "
            "nothing, which is issue #53 exactly — and the value it would let "
            "through, a limit of 0, is a key that refuses every call forever.",
        )


def _create_counters() -> None:
    op.create_table(
        COUNTERS_TABLE,
        sa.Column("key_id", sa.Integer(), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column(
            "count", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
        sa.PrimaryKeyConstraint("key_id", "day"),
        sa.ForeignKeyConstraint(["key_id"], ["api_keys.id"], ondelete="CASCADE"),
    )
    # Stamped in the same transaction as the CREATE, so the marker and the
    # table can never disagree about who made it.
    op.execute(f"COMMENT ON TABLE {COUNTERS_TABLE} IS {_quote(TABLE_MARKER)}")


def _counter_foreign_key_problems(bind) -> list:
    """Exactly one FK, and it is `key_id -> api_keys.id ON DELETE CASCADE`."""
    fks = _foreign_keys(bind, COUNTERS_TABLE)
    if len(fks) != 1:
        return [
            "it carries "
            + (
                "no foreign key at all"
                if not fks
                else f"{len(fks)} foreign keys ({[f.conname for f in fks]}), not one"
            )
        ]

    fk = fks[0]
    expected_target = _regclass_text(bind, KEYS_TABLE)
    problems = []
    if list(fk.local_columns or []) != ["key_id"]:
        problems.append(
            f"its foreign key is on {list(fk.local_columns or [])}, not ['key_id']"
        )
    if fk.referenced_table != expected_target:
        problems.append(
            f"its foreign key references {fk.referenced_table!r}, not "
            f"{expected_target!r} — the counter would be keyed by another "
            "table's ids"
        )
    if list(fk.referenced_columns or []) != ["id"]:
        problems.append(
            f"its foreign key references column(s) "
            f"{list(fk.referenced_columns or [])}, not ['id']"
        )
    if fk.delete_action != "c":
        problems.append(
            f"its key_id foreign key deletes with {fk.delete_action!r}, not "
            "CASCADE ('c') — a counter row the operator cannot see would block "
            "the panel's key delete"
        )
    if fk.update_action != "a":
        problems.append(
            f"its key_id foreign key updates with {fk.update_action!r}, not "
            "NO ACTION ('a')"
        )
    if not fk.convalidated:
        problems.append(
            "its key_id foreign key is NOT VALID, so existing rows were never "
            "checked and the table may already count a key that does not exist"
        )
    return problems


def _reconcile_counters(bind) -> None:
    if not _table_exists(bind, COUNTERS_TABLE):
        _create_counters()
        return

    problems = []
    if _table_comment(bind, COUNTERS_TABLE) != TABLE_MARKER:
        problems.append("it does not carry 020's comment marker")

    columns = _columns(bind, COUNTERS_TABLE)
    if columns != list(EXPECTED_COUNTER_COLUMNS):
        problems.append(
            f"its columns are {columns}, not {list(EXPECTED_COUNTER_COLUMNS)}"
        )

    default = _column_state(bind, COUNTERS_TABLE, "count")
    if default is not None and default[2] != EXPECTED_COUNT_DEFAULT:
        problems.append(
            f"its count default is {default[2]!r}, not {EXPECTED_COUNT_DEFAULT!r}"
        )

    pk = _primary_key_columns(bind, COUNTERS_TABLE)
    if pk != ["key_id", "day"]:
        problems.append(
            f"its primary key is {pk}, not ['key_id', 'day'] — that is the "
            "arbiter `ON CONFLICT (key_id, day)` names, so admission would "
            "either raise on every call or collapse every day onto one row"
        )

    problems.extend(_counter_foreign_key_problems(bind))

    if problems:
        _refuse(
            COUNTERS_TABLE,
            problems,
            "this table is the lock that makes the quota atomic and the number "
            "the keys page shows an operator as consumption.",
        )


def _reconcile_usage_index(bind) -> None:
    live = _index_definitions(bind, USAGE_TABLE).get(USAGE_INDEX)
    if live is None:
        op.create_index(USAGE_INDEX, USAGE_TABLE, ["key_id", "created_at"])
        return
    if live != EXPECTED_USAGE_INDEX:
        columns, unique, usable, restricted = live
        _refuse(
            f"index {USAGE_INDEX} on {USAGE_TABLE}",
            [
                f"it is on {columns} (unique={unique}, usable={usable}, "
                f"partial-or-expression={restricted}), not "
                f"{EXPECTED_USAGE_INDEX[0]} as a plain, valid, non-unique index"
            ],
            "the usage page's per-key filter reads it, and a UNIQUE index of "
            "this name would reject a second call by the same key in the same "
            "microsecond.",
        )


def upgrade() -> None:
    bind = op.get_bind()

    # Fail fast rather than queueing behind a long-lived transaction: the
    # deploy migrates before recreating the container, so a stalled migration
    # is a stalled deploy while the old container is still serving. Per
    # statement and per lock acquisition, not a budget for the transaction.
    op.execute("SET LOCAL lock_timeout = '10s'")
    op.execute("SET LOCAL statement_timeout = '60s'")

    _reconcile_limit_column(bind)
    _reconcile_check(bind)
    _reconcile_counters(bind)
    _reconcile_usage_index(bind)

    # **No backfill**, deliberately — see the module docstring. NULL is the
    # right value for every existing key, and the counter table starts empty
    # because consumption is defined as admissions since a limit was enabled.

    # `SET LOCAL` is scoped to the transaction and alembic runs every pending
    # revision in *one*, so without this the next revision would silently
    # inherit these timeouts and blame its own SQL when it tripped them.
    op.execute("RESET lock_timeout")
    op.execute("RESET statement_timeout")


def downgrade() -> None:
    """Undo *this* migration, and nothing that merely shares its names.

    013's rule. Each of the three objects is dropped only if it carries 020's
    marker; an unmarked one raises rather than being removed, because the
    marker is the only evidence of authorship.

    Dropping loses every configured limit and every day's counter. That is a
    downgrade to "unlimited", which is the pre-020 behaviour and fails open
    rather than locking anybody out — the enforcement code no-ops when the
    column is missing because the limit it reads is bound from the key row and
    is then simply absent.
    """
    bind = op.get_bind()

    live_index = _index_definitions(bind, USAGE_TABLE).get(USAGE_INDEX)
    if live_index is not None:
        # No marker is available for an index (PostgreSQL takes a comment on
        # one, but `alembic check` does not compare it and nothing else reads
        # it). The definition is the evidence instead: 020 drops an index of
        # this name only when it is exactly the one 020 creates.
        if live_index != EXPECTED_USAGE_INDEX:
            raise RuntimeError(
                f"{USAGE_INDEX} is not the index 020 created (it is on "
                f"{live_index[0]}), so 020 will not drop it. Nothing has been "
                "changed."
            )
        op.drop_index(USAGE_INDEX, table_name=USAGE_TABLE)

    if _table_exists(bind, COUNTERS_TABLE):
        if _table_comment(bind, COUNTERS_TABLE) != TABLE_MARKER:
            raise RuntimeError(
                f"{COUNTERS_TABLE} does not carry 020's comment marker "
                f"({TABLE_MARKER!r}), so 020 did not create it and will not "
                "drop it. Nothing has been changed. Remove it by hand if you "
                "mean to."
            )
        op.drop_table(COUNTERS_TABLE)

    check = _check_state(bind)
    if check is not None:
        if check[2] != CHECK_MARKER:
            raise RuntimeError(
                f"constraint {CHECK_NAME} on {KEYS_TABLE} does not carry 020's "
                f"comment marker ({CHECK_MARKER!r}), so 020 did not create it "
                "and will not drop it. Nothing has been changed."
            )
        op.drop_constraint(CHECK_NAME, KEYS_TABLE, type_="check")

    column = _column_state(bind, KEYS_TABLE, LIMIT_COLUMN)
    if column is not None:
        if column[3] != COLUMN_MARKER:
            raise RuntimeError(
                f"{KEYS_TABLE}.{LIMIT_COLUMN} does not carry 020's comment "
                f"marker ({COLUMN_MARKER!r}), so 020 did not create it and "
                "will not drop it. Nothing has been changed."
            )
        op.drop_column(KEYS_TABLE, LIMIT_COLUMN)
