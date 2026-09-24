"""Durable concurrency counters and the per-run coverage table (#188, D8).

The readiness evaluator (`concurrency-enforce-ready`, design D8) must answer
"did any request overrun, any writer overrun, any pool checkout time out in
this window?" from **durable** evidence, over arbitrary windows that cross
process restarts. Usage rows cannot carry outcomes for requests that never
reach a tool, #261 forbids an ownerless usage row per pressured request (an
unauthenticated flood would become writes), and security-event logs are not
queryable. So this revision creates two tables and **writes no row**.

## `concurrency_counters`

`(bucket_start timestamptz, epoch text, mode text, metric text)` primary key,
`count bigint NOT NULL DEFAULT 0`, `max_value integer NULL`, and an index on
`bucket_start` for the window scan and the 35-day prune.

- `bucket_start` is the **event-time** minute: the minute in which the request
  completed, the writer overran or the checkout timed out, never the flush
  time. A flush-time bucket moved incidents across window boundaries (SR2-4).
- `metric` is a **closed set**, guarded by `ck_concurrency_counters_metric`.
  A metric name the evaluator does not read is a counter nobody will ever see,
  and a typo'd `pool_checkout_timout` would make a real pool timeout invisible
  to the criterion that exists to block the flip on it — a false PASS. So the
  set lives in the database, as 023's closed key set does, and adding a metric
  is a migration.
- `mode` is closed too (`ck_concurrency_counters_mode`): the evaluator filters
  a window to one mode, and a mode it does not recognise must not exist.
- `max_value` is NULL except for the two gauges (`pool_high_water`,
  `transport_wait_max_ms`), where it holds the bucket maximum.

The flush writes at most one multi-row `INSERT … ON CONFLICT DO UPDATE` per
60 s, whatever the request volume.

## `concurrency_runs`

One row per process run: `run_id uuid` primary key, `epoch`, `mode`,
`started_at`, `completed_through` (the completed-interval watermark),
`clean_shutdown bool NOT NULL DEFAULT false`, `lossy bool NOT NULL DEFAULT
false`, and an index on `started_at`. A gap between two runs is covered only
if the earlier one flushed cleanly at shutdown; a hard kill leaves the gap
uncovered however short (SR2-3). Heartbeat spacing alone could not tell a
clean recreate from a kill, which is why a table exists at all.

## House shape

Marker-owned, as 023/024 are: each table carries a `COMMENT ON TABLE` marker
mirrored in `src/models/db.py` (so `alembic check` compares it), and each CHECK
carries a constraint-comment marker. A pre-existing table of either name is
**verified** against the complete shape — columns, primary key, the three
server defaults, the CHECK set **by definition** (measured off a scratch TEMP
table, 013's device; `alembic check` does not compare CHECK predicates), and
the exact index set — and refused, naming what disagreed, rather than adopted.
`upgrade()` refuses as a whole; `downgrade()` decides each table on its own
marker (023's rule) and prints what it leaves.

`search_path` is pinned and asserted, `lock_timeout` / `statement_timeout` set
and `RESET`, for 021's, 024's and 025's reasons. Needs the TEMP privilege for
the probe, as 013 already does.

Revision ID: 028
Revises: 027
Create Date: 2026-09-23
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "028"
down_revision: Union[str, None] = "027"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


COUNTERS = "concurrency_counters"
RUNS = "concurrency_runs"

# Pinned here rather than imported from `src.services.concurrency`: a migration
# must keep describing the schema it created after the application moves on
# (023's and 019's rule). The schema gate asserts both sets still equal
# `concurrency.METRICS` and the mode literal, so they cannot drift apart.
MODES = ("off", "shadow", "queue", "enforce")
METRICS = (
    "requests",
    "transport_pressured",
    "transport_waited",
    "transport_overrun",
    "transport_refused",
    "writer_overrun",
    "writer_refused",
    "pool_checkout_timeout",
    "pool_high_water",
    "transport_wait_max_ms",
)


def _in_list(column: str, values) -> str:
    return f"{column} IN (" + ", ".join(f"'{v}'" for v in values) + ")"


MODE_PREDICATE = _in_list("mode", MODES)
METRIC_PREDICATE = _in_list("metric", METRICS)

COUNTERS_MODE_CHECK = "ck_concurrency_counters_mode"
COUNTERS_METRIC_CHECK = "ck_concurrency_counters_metric"
RUNS_MODE_CHECK = "ck_concurrency_runs_mode"

COUNTERS_INDEX = "ix_concurrency_counters_bucket_start"
RUNS_INDEX = "ix_concurrency_runs_started_at"

# Byte identical to `_CONCURRENCY_COUNTERS_TABLE_MARKER` /
# `_CONCURRENCY_RUNS_TABLE_MARKER` in `src/models/db.py`.
COUNTERS_MARKER = "event-time minute concurrency counters (028_concurrency_counters)"
RUNS_MARKER = "one row per process run, coverage watermark (028_concurrency_counters)"
# No ORM counterpart: autogenerate does not compare CHECKs at all, which is why
# this revision reads the catalogue for them.
CHECK_MARKER = "closed set (028_concurrency_counters)"

# Each unit: `(column, format_type, attnotnull)` in creation order, the PK
# columns in order, `{column: default declaration}` for the defaults, the
# CHECKs `{name: (column, sql type, predicate)}`, and the index set.
UNITS = {
    COUNTERS: {
        "qualified": "public." + COUNTERS,
        "marker": COUNTERS_MARKER,
        "columns": (
            ("bucket_start", "timestamp with time zone", True),
            ("epoch", "text", True),
            ("mode", "text", True),
            ("metric", "text", True),
            ("count", "bigint", True),
            ("max_value", "integer", False),
        ),
        "pk": ["bucket_start", "epoch", "mode", "metric"],
        "defaults": {"count": ("bigint", "0")},
        "checks": {
            COUNTERS_MODE_CHECK: ("mode", "text", MODE_PREDICATE),
            COUNTERS_METRIC_CHECK: ("metric", "text", METRIC_PREDICATE),
        },
        "indexes": {COUNTERS_INDEX: (["bucket_start"], False, True, False)},
    },
    RUNS: {
        "qualified": "public." + RUNS,
        "marker": RUNS_MARKER,
        "columns": (
            ("run_id", "uuid", True),
            ("epoch", "text", True),
            ("mode", "text", True),
            ("started_at", "timestamp with time zone", True),
            ("completed_through", "timestamp with time zone", True),
            ("clean_shutdown", "boolean", True),
            ("lossy", "boolean", True),
        ),
        "pk": ["run_id"],
        "defaults": {
            "clean_shutdown": ("boolean", "false"),
            "lossy": ("boolean", "false"),
        },
        "checks": {RUNS_MODE_CHECK: ("mode", "text", MODE_PREDICATE)},
        "indexes": {RUNS_INDEX: (["started_at"], False, True, False)},
    },
}


def _quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


# --------------------------------------------------------------------------
# catalogue reads
# --------------------------------------------------------------------------


def _oid(bind, name: str):
    return bind.execute(
        sa.text("SELECT CAST(to_regclass(:name) AS oid)"), {"name": name}
    ).scalar()


def _table_comment(bind, qualified: str):
    return bind.execute(
        sa.text("SELECT obj_description(CAST(:q AS regclass), 'pg_class')"),
        {"q": qualified},
    ).scalar()


def _columns(bind, qualified: str):
    return [
        (r[0], r[1], r[2])
        for r in bind.execute(
            sa.text(
                "SELECT a.attname, format_type(a.atttypid, a.atttypmod), a.attnotnull "
                "FROM pg_attribute a WHERE a.attrelid = CAST(:q AS regclass) "
                "  AND a.attnum > 0 AND NOT a.attisdropped ORDER BY a.attnum"
            ),
            {"q": qualified},
        ).fetchall()
    ]


def _column_default(bind, qualified: str, column: str):
    return bind.execute(
        sa.text(
            "SELECT pg_get_expr(d.adbin, d.adrelid) FROM pg_attribute a "
            "LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum "
            "WHERE a.attrelid = CAST(:q AS regclass) AND a.attname = :c "
            "  AND a.attnum > 0 AND NOT a.attisdropped"
        ),
        {"q": qualified, "c": column},
    ).scalar()


def _primary_key_columns(bind, qualified: str):
    row = bind.execute(
        sa.text(
            "SELECT (SELECT array_agg(a.attname ORDER BY k.ord) "
            "          FROM unnest(c.conkey) WITH ORDINALITY AS k(attnum, ord) "
            "          JOIN pg_attribute a ON a.attrelid = c.conrelid "
            "                             AND a.attnum = k.attnum) AS columns "
            "FROM pg_constraint c "
            "WHERE c.conrelid = CAST(:q AS regclass) AND c.contype = 'p'"
        ),
        {"q": qualified},
    ).first()
    return list(row.columns) if row is not None and row.columns else None


def _pk_index_name(bind, qualified: str):
    return bind.execute(
        sa.text(
            "SELECT ic.relname FROM pg_constraint c "
            "JOIN pg_class ic ON ic.oid = c.conindid "
            "WHERE c.conrelid = CAST(:q AS regclass) AND c.contype = 'p'"
        ),
        {"q": qualified},
    ).scalar()


def _other_constraints(bind, qualified: str):
    """Every CHECK, UNIQUE, EXCLUSION and FOREIGN KEY constraint on the table,
    resolved through `conrelid` — **never by name** (013's rule)."""
    return bind.execute(
        sa.text(
            "SELECT c.conname, CAST(c.contype AS text) AS contype, "
            "       pg_get_constraintdef(c.oid) AS definition, c.convalidated, "
            "       obj_description(c.oid, 'pg_constraint') AS comment "
            "FROM pg_constraint c "
            "WHERE c.conrelid = CAST(:q AS regclass) "
            "  AND c.contype IN ('c', 'u', 'x', 'f') ORDER BY c.conname"
        ),
        {"q": qualified},
    ).fetchall()


def _index_definitions(bind, qualified: str) -> dict:
    """`{name: (columns, unique, usable, restricted)}` — 019's/021's/024's reader."""
    rows = bind.execute(
        sa.text(
            "SELECT ic.relname AS name, "
            "       (SELECT array_agg(a.attname ORDER BY k.ord) "
            "          FROM unnest(string_to_array(CAST(i.indkey AS text), ' ')) "
            "               WITH ORDINALITY AS k(attnum, ord) "
            "          JOIN pg_attribute a ON a.attrelid = i.indrelid "
            "                             AND a.attnum = CAST(k.attnum AS smallint)"
            "       ) AS columns, "
            "       i.indisunique, (i.indisvalid AND i.indisready) AS usable, "
            "       (i.indpred IS NOT NULL OR i.indexprs IS NOT NULL) AS restricted "
            "FROM pg_index i JOIN pg_class ic ON ic.oid = i.indexrelid "
            "WHERE i.indrelid = CAST(:q AS regclass)"
        ),
        {"q": qualified},
    ).fetchall()
    return {
        r.name: (list(r.columns or []), r.indisunique, r.usable, r.restricted)
        for r in rows
    }


def _canonical_check(bind, column: str, sqltype: str, predicate: str) -> str:
    """How this server renders `predicate`, measured off a scratch TEMP table
    carrying the identical declaration (013's device, as 023 uses it)."""
    scratch = "_omcp_028_check_probe"
    bind.execute(sa.text(f"DROP TABLE IF EXISTS pg_temp.{scratch}"))
    bind.execute(
        sa.text(
            f'CREATE TEMP TABLE {scratch} ("{column}" {sqltype}, '
            f"CONSTRAINT {scratch}_ck CHECK ({predicate}))"
        )
    )
    rendered = bind.execute(
        sa.text(
            "SELECT pg_get_constraintdef(c.oid) FROM pg_constraint c "
            "WHERE c.conrelid = CAST(:s AS regclass) AND c.contype = 'c'"
        ),
        {"s": f"pg_temp.{scratch}"},
    ).scalar()
    bind.execute(sa.text(f"DROP TABLE IF EXISTS pg_temp.{scratch}"))
    return rendered


def _canonical_default(bind, sqltype: str, declaration: str) -> str:
    scratch = "_omcp_028_default_probe"
    bind.execute(sa.text(f"DROP TABLE IF EXISTS pg_temp.{scratch}"))
    bind.execute(
        sa.text(f"CREATE TEMP TABLE {scratch} (v {sqltype} DEFAULT {declaration})")
    )
    rendered = bind.execute(
        sa.text(
            "SELECT pg_get_expr(d.adbin, d.adrelid) FROM pg_attrdef d "
            "WHERE d.adrelid = CAST(:s AS regclass)"
        ),
        {"s": f"pg_temp.{scratch}"},
    ).scalar()
    bind.execute(sa.text(f"DROP TABLE IF EXISTS pg_temp.{scratch}"))
    return rendered


# --------------------------------------------------------------------------
# create / verify
# --------------------------------------------------------------------------


def _pin_search_path() -> None:
    """021's device: unqualified `op.*` names resolve to `public` while the
    model stays schema-less, so `alembic check` keeps agreeing."""
    op.execute("SET LOCAL search_path TO public")


def _assert_is_the_qualified_table(bind, table: str) -> None:
    qualified = _oid(bind, UNITS[table]["qualified"])
    unqualified = _oid(bind, table)
    if qualified is None or qualified != unqualified:
        raise RuntimeError(
            f"028's {table} is not the table {UNITS[table]['qualified']} "
            f"resolves to (search_path-relative oid {unqualified!r}, qualified "
            f"oid {qualified!r}). The flush and the readiness evaluator read the "
            "unqualified name. Set the migration role's search_path so `public` "
            "comes first, then re-run."
        )


def _create_counters() -> None:
    op.create_table(
        COUNTERS,
        sa.Column("bucket_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("epoch", sa.Text(), nullable=False),
        sa.Column("mode", sa.Text(), nullable=False),
        sa.Column("metric", sa.Text(), nullable=False),
        sa.Column("count", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("max_value", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("bucket_start", "epoch", "mode", "metric"),
        sa.CheckConstraint(MODE_PREDICATE, name=COUNTERS_MODE_CHECK),
        sa.CheckConstraint(METRIC_PREDICATE, name=COUNTERS_METRIC_CHECK),
    )
    op.create_index(COUNTERS_INDEX, COUNTERS, ["bucket_start"])
    op.execute(f"COMMENT ON TABLE {COUNTERS} IS {_quote(COUNTERS_MARKER)}")
    for name in (COUNTERS_MODE_CHECK, COUNTERS_METRIC_CHECK):
        op.execute(f"COMMENT ON CONSTRAINT {name} ON {COUNTERS} IS {_quote(CHECK_MARKER)}")


def _create_runs() -> None:
    op.create_table(
        RUNS,
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("epoch", sa.Text(), nullable=False),
        sa.Column("mode", sa.Text(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_through", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "clean_shutdown", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column("lossy", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.PrimaryKeyConstraint("run_id"),
        sa.CheckConstraint(MODE_PREDICATE, name=RUNS_MODE_CHECK),
    )
    op.create_index(RUNS_INDEX, RUNS, ["started_at"])
    op.execute(f"COMMENT ON TABLE {RUNS} IS {_quote(RUNS_MARKER)}")
    op.execute(
        f"COMMENT ON CONSTRAINT {RUNS_MODE_CHECK} ON {RUNS} IS {_quote(CHECK_MARKER)}"
    )


CREATORS = {COUNTERS: _create_counters, RUNS: _create_runs}


def _problems(bind, table: str) -> list:
    """Everything about an existing `table` that is not exactly 028's shape.

    **Complete, not minimal** (024's rule): the constraint and index sets are
    compared as sets, so a table that has everything 028 makes plus a hand-added
    `UNIQUE` or an extra CHECK is not 028's table.
    """
    unit = UNITS[table]
    qualified = unit["qualified"]
    problems = []

    if _table_comment(bind, qualified) != unit["marker"]:
        problems.append("it does not carry 028's comment marker")

    columns = _columns(bind, qualified)
    if columns != list(unit["columns"]):
        problems.append(f"its columns are {columns}, not {list(unit['columns'])}")

    pk = _primary_key_columns(bind, qualified)
    if pk != unit["pk"]:
        problems.append(
            "its primary key is "
            + ("absent" if pk is None else f"on {pk}")
            + f", not on {unit['pk']} — two rows could then claim one bucket, "
            "and the flush's ON CONFLICT would have nothing to conflict on"
        )

    for column, (sqltype, declaration) in unit["defaults"].items():
        live = _column_default(bind, qualified, column)
        canonical = _canonical_default(bind, sqltype, declaration)
        if live != canonical:
            problems.append(f"its {column} default is {live!r}, not {canonical!r}")

    expected_checks = {
        _canonical_check(bind, column, sqltype, predicate): name
        for name, (column, sqltype, predicate) in unit["checks"].items()
    }
    seen = set()
    for row in _other_constraints(bind, qualified):
        if row.contype != "c":
            kind = {"u": "UNIQUE", "x": "EXCLUSION", "f": "FOREIGN KEY"}[row.contype]
            problems.append(
                f"it carries an unexpected {kind} constraint {row.conname!r} "
                f"({row.definition}) that 028 does not create"
            )
            continue
        name = expected_checks.get(row.definition)
        if name is None or name in seen:
            problems.append(
                f"it carries a CHECK {row.conname!r} ({row.definition}) that is "
                "not one of 028's closed-set predicates"
            )
            continue
        seen.add(name)
        if not row.convalidated:
            problems.append(f"its CHECK {row.conname!r} is NOT VALID")
        if row.comment != CHECK_MARKER:
            problems.append(
                f"its CHECK {row.conname!r} does not carry 028's constraint marker"
            )
    for missing in sorted(set(expected_checks.values()) - seen):
        problems.append(f"it is missing the closed-set CHECK {missing}")

    live_indexes = _index_definitions(bind, qualified)
    for name, expected in unit["indexes"].items():
        actual = live_indexes.get(name)
        if actual is None:
            problems.append(f"it is missing index {name}")
        elif actual != expected:
            problems.append(
                f"its index {name} is {actual}, not {expected[0]} as a plain, "
                "valid, non-unique index"
            )
    permitted = set(unit["indexes"])
    pk_index = _pk_index_name(bind, qualified)
    if pk_index is not None:
        permitted.add(pk_index)
    for name in sorted(set(live_indexes) - permitted):
        problems.append(
            f"it carries an unexpected index {name!r} on {live_indexes[name][0]} "
            "that 028 does not create"
        )
    return problems


# --------------------------------------------------------------------------
# upgrade / downgrade
# --------------------------------------------------------------------------


def upgrade() -> None:
    bind = op.get_bind()
    op.execute("SET LOCAL lock_timeout = '10s'")
    op.execute("SET LOCAL statement_timeout = '60s'")
    _pin_search_path()

    # Verify every pre-existing unit **before** creating anything, so a refusal
    # of one leaves the database exactly as it was.
    refusals = []
    for table in UNITS:
        if _oid(bind, UNITS[table]["qualified"]) is not None:
            problems = _problems(bind, table)
            if problems:
                refusals.append(f"{table} already exists but {'; '.join(problems)}")
    if refusals:
        raise RuntimeError(
            ". ".join(refusals)
            + ". 028 will not adopt a table of unknown provenance: the readiness "
            "evaluator certifies a concurrency-mode flip from these rows, and a "
            "shape nothing verified can certify a false PASS. Resolve by hand — "
            "drop it and let 028 create it, or make it match — then re-run. "
            "Nothing has been changed."
        )

    for table, create in CREATORS.items():
        if _oid(bind, UNITS[table]["qualified"]) is None:
            create()
        _assert_is_the_qualified_table(bind, table)

    # No row is written on any path: there is no evidence to backfill from, and
    # a stamp-back re-run must keep the counters and runs it finds.
    op.execute("RESET lock_timeout")
    op.execute("RESET statement_timeout")
    op.execute("RESET search_path")


def downgrade() -> None:
    """Drop each table only if it carries 028's marker, each decided on its own
    (023's rule), printing what is left and why (`alembic/env.py` configures no
    logging, so a `logger.warning` here would reach nothing)."""
    bind = op.get_bind()
    _pin_search_path()
    for table, index in ((RUNS, RUNS_INDEX), (COUNTERS, COUNTERS_INDEX)):
        qualified = UNITS[table]["qualified"]
        if _oid(bind, qualified) is None:
            continue
        _assert_is_the_qualified_table(bind, table)
        if _table_comment(bind, qualified) != UNITS[table]["marker"]:
            print(
                f"028 downgrade: leaving {table} in place — it does not carry "
                f"028's marker ({UNITS[table]['marker']!r}), so 028 did not "
                "create it. Remove it by hand if you mean to."
            )
            continue
        op.drop_index(index, table_name=table)
        op.drop_table(table)
    op.execute("RESET search_path")
