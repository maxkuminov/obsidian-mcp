"""The task rule on `task_settings` (#434).

Which Markdown checkboxes count as tasks, per owner scope (design D8, D11 of
the `task-rule` change). This revision adds exactly these units to the
existing `task_settings` table and nothing else:

| unit | definition |
| --- | --- |
| `task_rule` | `varchar(8) NULL` — `all`, `fields`, `filter`; NULL = the server's rule |
| `task_tags` | `varchar(128)[] NOT NULL DEFAULT '{}'` — task tags for `fields` |
| `global_filter` | `varchar(64) NULL` — the filter text for `filter` |
| `ck_task_settings_task_rule` | `task_rule IN ('all', 'fields', 'filter')` (NULL passes) |
| `ck_task_settings_global_filter` | a filter text is NULL or not blank, and a `filter` rule has one |

**No data is written.** No row is updated; `ADD COLUMN … DEFAULT '{}'` is
metadata-only on PostgreSQL 16, and both CHECKs admit every existing row
(NULL, `'{}'`, NULL). An already-running previous-image process keeps
working: its insert names only its own columns (the new ones take NULL,
`'{}'`, NULL) and its update touches only its own. A restarted previous image
cannot start at 040 (its migration step does not know the revision); rollback
is roll-forward, or `alembic downgrade 039` run with this image first, which
drops the three columns and both CHECKs — and with them every saved task
rule, task tag and filter text.

## Reconciliation, not adoption

The schema gate stamps back and re-runs this body against a database that
already has every unit. Each column is verified by type, nullability, default
(the server's rendering, measured off a probe) and comment marker; each CHECK
by its validated definition, compared with what this server prints for a
TEMP-table probe carrying the same declaration, and its marker. A same-named
object of another shape, or one without the marker, is refused by name before
anything is created. `downgrade()` drops only marked objects, the CHECKs
first.

039 compares `task_settings` completely, so it leaves exactly these five
units — each only with its exact marker below — to this revision (039's
allow-list, the second after 032's).

`search_path` is pinned, and `lock_timeout` / `statement_timeout` set and
`RESET`. Nothing here needs superuser and no extension is created
(`test_nonsuperuser_migrations_pg.py`).

Revision ID: 040
Revises: 039
Create Date: 2026-11-10
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "040"
down_revision: Union[str, None] = "039"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


SETTINGS = "task_settings"
SETTINGS_QUALIFIED = "public.task_settings"

# Byte identical to the `_TASK_RULE_*` constants in `src/models/db.py` and to
# 039's allow-list.
TASK_RULE_MARKER = "task rule kind (040_task_rule)"
TASK_TAGS_MARKER = "task rule task tags (040_task_rule)"
GLOBAL_FILTER_MARKER = "task rule global filter text (040_task_rule)"
RULE_CHECK_MARKER = "task rule kinds (040_task_rule)"
FILTER_CHECK_MARKER = "a filter rule has its filter text (040_task_rule)"

# `(column, DDL type, format_type, NOT NULL, default, marker)` in creation order.
COLUMNS = (
    ("task_rule", "varchar(8)", "character varying(8)", False, None, TASK_RULE_MARKER),
    ("task_tags", "varchar(128)[]", "character varying(128)[]", True, "'{}'",
     TASK_TAGS_MARKER),
    ("global_filter", "varchar(64)", "character varying(64)", False, None,
     GLOBAL_FILTER_MARKER),
)

RULE_CHECK = "ck_task_settings_task_rule"
FILTER_CHECK = "ck_task_settings_global_filter"
# Byte identical to `_TASK_RULE_CHECK_PREDICATE` / `_TASK_GLOBAL_FILTER_CHECK_
# PREDICATE` in `src/models/db.py`.
RULE_CHECK_PREDICATE = "task_rule IN ('all', 'fields', 'filter')"
FILTER_CHECK_PREDICATE = (
    "(global_filter IS NULL OR btrim(global_filter) <> '') AND "
    "(task_rule IS DISTINCT FROM 'filter' OR global_filter IS NOT NULL)"
)
# `(name, predicate, marker, columns it reads)` in creation order.
CHECKS = (
    (RULE_CHECK, RULE_CHECK_PREDICATE, RULE_CHECK_MARKER, {"task_rule"}),
    (FILTER_CHECK, FILTER_CHECK_PREDICATE, FILTER_CHECK_MARKER, {"task_rule", "global_filter"}),
)


def _quote(value: str) -> str:
    """A single-quoted SQL string literal (`COMMENT ON` takes no bind)."""
    return "'" + value.replace("'", "''") + "'"


# --------------------------------------------------------------------------
# catalogue reads
# --------------------------------------------------------------------------


def _oid(bind, name: str):
    return bind.execute(
        sa.text("SELECT CAST(to_regclass(:name) AS oid)"), {"name": name}
    ).scalar()


def _column_state(bind, column: str):
    """`(format_type, attnotnull, default_expr, comment)`, or None if absent."""
    return bind.execute(
        sa.text(
            "SELECT format_type(a.atttypid, a.atttypmod), a.attnotnull, "
            "       pg_get_expr(d.adbin, d.adrelid), "
            "       col_description(a.attrelid, a.attnum) "
            "FROM pg_attribute a "
            "LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum "
            "WHERE a.attrelid = CAST(:q AS regclass) AND a.attname = :c "
            "  AND a.attnum > 0 AND NOT a.attisdropped"
        ),
        {"q": SETTINGS_QUALIFIED, "c": column},
    ).first()


def _constraint(bind, name: str):
    """`(contype, convalidated, normalised definition, comment)` or None."""
    row = bind.execute(
        sa.text(
            "SELECT CAST(contype AS text), convalidated, pg_get_constraintdef(oid), "
            "       obj_description(oid, 'pg_constraint') "
            "FROM pg_constraint "
            "WHERE conrelid = CAST(:q AS regclass) AND conname = :name"
        ),
        {"q": SETTINGS_QUALIFIED, "name": name},
    ).first()
    if row is None:
        return None
    return row[0], row[1], " ".join(row[2].split()), row[3]


# --------------------------------------------------------------------------
# probes: what this server prints for 040's own declarations
# --------------------------------------------------------------------------

_PROBE = "_omcp_040_probe"


def _probe_default(bind, sqltype: str, declaration: str) -> str:
    bind.execute(sa.text(f"DROP TABLE IF EXISTS pg_temp.{_PROBE}"))
    bind.execute(sa.text(f"CREATE TEMP TABLE {_PROBE} (v {sqltype} DEFAULT {declaration})"))
    rendered = bind.execute(
        sa.text(
            "SELECT pg_get_expr(d.adbin, d.adrelid) FROM pg_attrdef d "
            "WHERE d.adrelid = CAST(:s AS regclass)"
        ),
        {"s": f"pg_temp.{_PROBE}"},
    ).scalar()
    bind.execute(sa.text(f"DROP TABLE IF EXISTS pg_temp.{_PROBE}"))
    return rendered


def _canonical_checks(bind) -> dict:
    """`{name: printed definition}` for both CHECKs, off a TEMP probe typed
    like the live columns."""
    bind.execute(sa.text(f"DROP TABLE IF EXISTS pg_temp.{_PROBE}"))
    bind.execute(
        sa.text(f"CREATE TEMP TABLE {_PROBE} (task_rule varchar(8), global_filter varchar(64))")
    )
    rendered = {}
    for name, predicate, _marker, _cols in CHECKS:
        probe_name = f"{_PROBE}_{name[-6:]}"
        bind.execute(
            sa.text(
                f"ALTER TABLE pg_temp.{_PROBE} ADD CONSTRAINT {probe_name} CHECK ({predicate})"
            )
        )
        definition = bind.execute(
            sa.text(
                "SELECT pg_get_constraintdef(c.oid) FROM pg_constraint c "
                "WHERE c.conrelid = CAST(:s AS regclass) AND c.conname = :n"
            ),
            {"s": f"pg_temp.{_PROBE}", "n": probe_name},
        ).scalar()
        rendered[name] = " ".join(definition.split())
    bind.execute(sa.text(f"DROP TABLE IF EXISTS pg_temp.{_PROBE}"))
    return rendered


# --------------------------------------------------------------------------
# verify
# --------------------------------------------------------------------------


def _pin() -> None:
    op.execute("SET LOCAL search_path TO public")


def _assert_is_the_qualified_table(bind) -> None:
    resolved = _oid(bind, SETTINGS_QUALIFIED)
    unqualified = _oid(bind, SETTINGS)
    if resolved is None or resolved != unqualified:
        raise RuntimeError(
            f"040's {SETTINGS} is not the table {SETTINGS_QUALIFIED} resolves to "
            f"(search_path-relative oid {unqualified!r}, qualified oid {resolved!r}). "
            "039 creates it; the server reads the unqualified name. Set the "
            "migration role's search_path so `public` comes first, then re-run. "
            "Nothing has been changed."
        )


def _column_problems(bind) -> tuple[list, list]:
    """`(problems, missing)` over every column 040 adds."""
    problems, missing = [], []
    for column, ddl_type, fmt, notnull, default, marker in COLUMNS:
        state = _column_state(bind, column)
        if state is None:
            missing.append((column, ddl_type, notnull, default, marker))
            continue
        coltype, live_notnull, live_default, comment = state
        if coltype != fmt:
            problems.append(f"{SETTINGS}.{column} is {coltype}, not {fmt}")
        if live_notnull != notnull:
            problems.append(
                f"{SETTINGS}.{column} is {'NOT NULL' if live_notnull else 'nullable'}; "
                f"040 creates it {'NOT NULL' if notnull else 'nullable'}"
            )
        expected_default = _probe_default(bind, ddl_type, default) if default else None
        if live_default != expected_default:
            problems.append(
                f"{SETTINGS}.{column} has the server default {live_default!r}, "
                f"not {expected_default!r}"
            )
        if comment != marker:
            problems.append(f"{SETTINGS}.{column} does not carry 040's comment marker")
    return problems, missing


def _check_problems(bind) -> list:
    problems = []
    canonical = None
    for name, _predicate, marker, _cols in CHECKS:
        existing = _constraint(bind, name)
        if existing is None:
            continue
        if canonical is None:
            canonical = _canonical_checks(bind)
        contype, validated, definition, comment = existing
        if contype == "c" and validated and definition == canonical[name] and comment == marker:
            continue
        problems.append(
            f"{SETTINGS} already carries a constraint {name} that is not 040's "
            f"(type {contype!r}, definition {definition!r}, validated={validated}, "
            f"marker {'present' if comment == marker else 'absent'})"
        )
    return problems


# --------------------------------------------------------------------------
# upgrade / downgrade
# --------------------------------------------------------------------------


def upgrade() -> None:
    bind = op.get_bind()
    op.execute("SET LOCAL lock_timeout = '10s'")
    op.execute("SET LOCAL statement_timeout = '60s'")
    _pin()
    _assert_is_the_qualified_table(bind)

    # Verify every pre-existing unit before creating anything, so a refusal of
    # one leaves the database exactly as it was.
    refusals, missing = _column_problems(bind)
    refusals += _check_problems(bind)
    if refusals:
        raise RuntimeError(
            ". ".join(refusals)
            + ". 040 will not adopt a unit of unknown provenance: the task tools "
            "decide from these columns which checkboxes each owner's listings "
            "show, and a shape this migration did not create can hide a task or "
            "apply a rule nobody chose. Resolve by hand — drop it and let 040 "
            "create it — then re-run. Nothing has been changed."
        )

    for column, ddl_type, notnull, default, marker in missing:
        clause = f" NOT NULL DEFAULT {default}" if notnull else ""
        op.execute(f"ALTER TABLE {SETTINGS} ADD COLUMN {column} {ddl_type}{clause}")
        op.execute(f"COMMENT ON COLUMN {SETTINGS}.{column} IS {_quote(marker)}")

    for name, predicate, marker, _cols in CHECKS:
        if _constraint(bind, name) is None:
            # Every existing row reads NULL / '{}' / NULL, which both admit.
            op.execute(f"ALTER TABLE {SETTINGS} ADD CONSTRAINT {name} CHECK ({predicate})")
            op.execute(f"COMMENT ON CONSTRAINT {name} ON {SETTINGS} IS {_quote(marker)}")

    # No row is written on any path.
    op.execute("RESET lock_timeout")
    op.execute("RESET statement_timeout")
    op.execute("RESET search_path")


def _leave(unit: str, marker: str, comment) -> None:
    print(
        f"040 downgrade: leaving {unit} in place. It does not carry 040's marker "
        f"({marker!r}) — its comment is {comment!r} — so 040 did not create it "
        "and will not drop it. Remove it by hand if you mean to.",
        flush=True,
    )


def downgrade() -> None:
    """Drop each unit only if it carries 040's marker, the CHECKs first, each
    decided on its own, printing what is left and why. The rest of
    `task_settings` is untouched; the saved task rules, task tags and filter
    texts go with their columns (the count is printed)."""
    bind = op.get_bind()
    op.execute("SET LOCAL lock_timeout = '10s'")
    op.execute("SET LOCAL statement_timeout = '60s'")
    _pin()

    if _oid(bind, SETTINGS_QUALIFIED) is not None:
        _assert_is_the_qualified_table(bind)
        kept: set = set()
        for name, _predicate, marker, cols in CHECKS:
            existing = _constraint(bind, name)
            if existing is None:
                continue
            if existing[3] != marker:
                _leave(f"constraint {name} on {SETTINGS}", marker, existing[3])
                kept |= cols
            else:
                op.execute(f"ALTER TABLE {SETTINGS} DROP CONSTRAINT {name}")

        present = {c for c, *_ in COLUMNS if _column_state(bind, c) is not None}
        if {"task_rule", "task_tags", "global_filter"} <= present:
            rows = bind.execute(sa.text(
                f"SELECT count(*) FROM {SETTINGS} WHERE task_rule IS NOT NULL "
                "OR global_filter IS NOT NULL OR cardinality(task_tags) > 0"
            )).scalar_one()
            if rows:
                print(
                    f"040 downgrade: discarding the task rule, task tags or global "
                    f"filter text of {rows} {SETTINGS} row(s); the previous build "
                    "does not read them and lists every checkbox.",
                    flush=True,
                )
        for column, _ddl, _fmt, _nn, _default, marker in reversed(COLUMNS):
            state = _column_state(bind, column)
            if state is None:
                continue
            if state[3] != marker:
                _leave(f"{SETTINGS}.{column}", marker, state[3])
            elif column in kept:
                print(
                    f"040 downgrade: leaving {SETTINGS}.{column} in place — an object "
                    "040 did not create, and so did not drop, still reads it.",
                    flush=True,
                )
            else:
                op.execute(f"ALTER TABLE {SETTINGS} DROP COLUMN {column}")

    op.execute("RESET lock_timeout")
    op.execute("RESET statement_timeout")
    op.execute("RESET search_path")
