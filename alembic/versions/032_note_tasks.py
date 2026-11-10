"""`note_tasks`, `notes_metadata.tasks_truncated` and `.tasks_failed` (#350).

The server had no way to answer "which tasks are open" without reading every
note that might hold one (the 2026-10-23 morning-briefing incident: ~400
`read_note` calls in ~100 s). This revision creates the derived table the index
pass fills with one row per checkbox list item, and the two per-note bits that
say when that table is not the whole truth for a note.

## `note_tasks`

Keyed by `note_id` (FK to `notes_metadata.id`, `ON DELETE CASCADE`), with **no
path and no owner column** (design D1): both come from the note's row through
the join, so a task row cannot outlive its note, cannot exist before it, and
follows an id-preserving move without being rewritten. `UNIQUE (note_id,
line)` holds because a line carries at most one checkbox, and it catches a
double insert in the pass. `state` and `priority` are closed sets guarded by
CHECKs. `tags` is `varchar(128)[]` — the extractor stores at most the first 64
distinct inline tags of at most 128 characters, and `tags_cut` says when it
dropped one.

## `tasks_truncated` / `tasks_failed`

`tasks_truncated` works like 022's `links_truncated`: set when the per-note
cap (`MAX_TASKS_PER_NOTE`) bit. `tasks_failed` marks a note whose task
derivation failed twice in the pass (design D4); it is **not** a quarantine.
Both `BOOLEAN NOT NULL DEFAULT false`, so the ADD is metadata-only and every
pre-existing row reads "not capped, not failed" — true of a row no task stage
has seen yet, which `list_tasks` counts separately as "not yet scanned"
through `extraction_version < 4`.

## No backfill

The migration writes no row. The rows need the note bodies, which the
database does not hold; the first pass after deploy re-derives every note
because `CURRENT_EXTRACTION_VERSION` moved to 4 (#150's mechanism).

## Reconciliation, not adoption

028's shape for the table, 022's for the columns. The gate stamps back and
re-runs this body against a database that already has every unit, so a
same-named unit is verified — the table completely (columns, primary key,
defaults, CHECKs, UNIQUE, FK, indexes, marker), each column by type,
nullability, default and marker — and any other shape is refused by name
before anything is created. `downgrade()` drops only marked units.

`search_path` is pinned, and `lock_timeout` / `statement_timeout` set and
`RESET`, for 021's, 024's and 025's reasons.

Revision ID: 032
Revises: 031
Create Date: 2026-10-29
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "032"
down_revision: Union[str, None] = "031"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


TABLE = "note_tasks"
QUALIFIED = "public.note_tasks"
NOTES = "notes_metadata"
NOTES_QUALIFIED = "public.notes_metadata"

# Byte identical to `_NOTE_TASKS_TABLE_MARKER`, `_TASKS_TRUNCATED_COLUMN_MARKER`
# and `_TASKS_FAILED_COLUMN_MARKER` in `src/models/db.py`, where they are the
# table and column comments so `alembic check` compares them.
TABLE_MARKER = "task rows derived per note (032_note_tasks)"
TRUNCATED_MARKER = "task-extraction truncation marker (032_note_tasks)"
FAILED_MARKER = "task-derivation failure marker (032_note_tasks)"

# Pinned here rather than imported from `src.services.tasks`: a migration must
# keep describing the schema it created after the application moves on.
STATES = ("open", "done", "cancelled")
PRIORITIES = ("highest", "high", "medium", "low", "lowest")


def _in_list(column: str, values) -> str:
    return f"{column} IN (" + ", ".join(f"'{v}'" for v in values) + ")"


STATE_PREDICATE = _in_list("state", STATES)
PRIORITY_PREDICATE = _in_list("priority", PRIORITIES)

STATE_CHECK = "ck_note_tasks_state"
PRIORITY_CHECK = "ck_note_tasks_priority"
UNIQUE = "uq_note_tasks_note_id_line"
NOTE_INDEX = "ix_note_tasks_note"
STATE_DUE_INDEX = "ix_note_tasks_state_due"

# `(column, format_type, attnotnull)` in creation order.
COLUMNS = (
    ("id", "integer", True),
    ("note_id", "integer", True),
    ("line", "integer", True),
    ("status_char", "character varying(1)", True),
    ("state", "character varying(16)", True),
    ("text", "text", True),
    ("text_cut", "boolean", True),
    ("due", "date", False),
    ("scheduled", "date", False),
    ("start", "date", False),
    ("done_on", "date", False),
    ("priority", "character varying(8)", False),
    ("tags", "character varying(128)[]", False),
    ("tags_cut", "boolean", True),
    ("heading_path", "text", False),
)
DEFAULTS = {"text_cut": ("boolean", "false"), "tags_cut": ("boolean", "false")}
CHECKS = {
    STATE_CHECK: ("state", "varchar(16)", STATE_PREDICATE),
    PRIORITY_CHECK: ("priority", "varchar(8)", PRIORITY_PREDICATE),
}
# The two non-CHECK constraints, as PostgreSQL 16 renders them with
# `search_path` pinned to `public`.
UNIQUE_DEFINITION = "UNIQUE (note_id, line)"
FK_DEFINITION = "FOREIGN KEY (note_id) REFERENCES notes_metadata(id) ON DELETE CASCADE"
# `{name: (columns, unique, usable, restricted)}`.
INDEXES = {
    NOTE_INDEX: (["note_id"], False, True, False),
    STATE_DUE_INDEX: (["state", "due"], False, True, False),
}

NOTE_COLUMNS = {"tasks_truncated": TRUNCATED_MARKER, "tasks_failed": FAILED_MARKER}


# Units a later revision adds to `note_tasks`, by kind and exact name, each
# with the exact marker comment that revision writes. The complete comparison
# below leaves out exactly these — a unit of one of these names *and* this
# comment — because that revision verifies them; otherwise a stamp-back that
# re-runs 032 over a head database would refuse them as foreign. Anything
# else (another name, the right name with another comment, any other marker)
# is compared and refused as before. On a database without these units
# (every database 032 itself meets) nothing changes.
#
# **A future revision that adds a unit to `note_tasks` must extend these
# lists on purpose**, with its literal names and markers; nothing is inferred
# from a comment's shape. Byte identical to 039's literals (#428).
_039_TASK_KEY = "task id key and occurrence (039_task_tools)"
_039_RECURRENCE = "task recurrence rule (039_task_tools)"
_039_DATES = "task created and cancelled dates (039_task_tools)"
_039_CONTEXT = "task context lines (039_task_tools)"
LATER_COLUMNS = {
    "task_key": _039_TASK_KEY,
    "key_ordinal": _039_TASK_KEY,
    "key_count": _039_TASK_KEY,
    "description_key": "task duplicate-detection key (039_task_tools)",
    "text_fold": "case-folded task text (039_task_tools)",
    "tag_prefixes": "hierarchical task tag prefixes (039_task_tools)",
    "recurrence": _039_RECURRENCE,
    "recurrence_cut": _039_RECURRENCE,
    "created": _039_DATES,
    "cancelled": _039_DATES,
    "context": _039_CONTEXT,
    "context_cut": _039_CONTEXT,
}
LATER_CONSTRAINTS = {
    "ck_note_tasks_key_shape": "task id key shape (039_task_tools)",
}
LATER_INDEXES = {
    "ix_note_tasks_tag_prefixes": "task tag-prefix filter index (039_task_tools)",
    "ix_note_tasks_open_desc": "open-task duplicate lookup index (039_task_tools)",
}


def _later_revision_unit(allow_list: dict, name, comment) -> bool:
    return comment is not None and allow_list.get(name) == comment


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


def _table_comment(bind):
    return bind.execute(
        sa.text("SELECT obj_description(CAST(:q AS regclass), 'pg_class')"),
        {"q": QUALIFIED},
    ).scalar()


def _columns(bind):
    return [
        (r[0], r[1], r[2])
        for r in bind.execute(
            sa.text(
                "SELECT a.attname, format_type(a.atttypid, a.atttypmod), a.attnotnull, "
                "       col_description(a.attrelid, a.attnum) "
                "FROM pg_attribute a WHERE a.attrelid = CAST(:q AS regclass) "
                "  AND a.attnum > 0 AND NOT a.attisdropped ORDER BY a.attnum"
            ),
            {"q": QUALIFIED},
        ).fetchall()
        if not _later_revision_unit(LATER_COLUMNS, r[0], r[3])
    ]


def _column_state(bind, qualified: str, column: str):
    """`(format_type, attnotnull, default_expr, comment)`, or None if absent."""
    return bind.execute(
        sa.text(
            "SELECT format_type(a.atttypid, a.atttypmod) AS coltype, "
            "       a.attnotnull, "
            "       pg_get_expr(d.adbin, d.adrelid) AS coldefault, "
            "       col_description(a.attrelid, a.attnum) AS comment "
            "FROM pg_attribute a "
            "LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum "
            "WHERE a.attrelid = CAST(:q AS regclass) AND a.attname = :c "
            "  AND a.attnum > 0 AND NOT a.attisdropped"
        ),
        {"q": qualified, "c": column},
    ).first()


def _primary_key(bind):
    """`(columns, index name)` of the primary key, or None."""
    row = bind.execute(
        sa.text(
            "SELECT (SELECT array_agg(a.attname ORDER BY k.ord) "
            "          FROM unnest(c.conkey) WITH ORDINALITY AS k(attnum, ord) "
            "          JOIN pg_attribute a ON a.attrelid = c.conrelid "
            "                             AND a.attnum = k.attnum) AS columns, "
            "       ic.relname AS index_name "
            "FROM pg_constraint c JOIN pg_class ic ON ic.oid = c.conindid "
            "WHERE c.conrelid = CAST(:q AS regclass) AND c.contype = 'p'"
        ),
        {"q": QUALIFIED},
    ).first()
    if row is None:
        return None
    return list(row.columns or []), row.index_name


def _other_constraints(bind):
    """Every CHECK, UNIQUE, EXCLUSION and FOREIGN KEY constraint on the table,
    resolved through `conrelid` — never by name (013's rule)."""
    rows = bind.execute(
        sa.text(
            "SELECT c.conname, CAST(c.contype AS text) AS contype, "
            "       pg_get_constraintdef(c.oid) AS definition, c.convalidated, "
            "       ic.relname AS index_name, "
            "       obj_description(c.oid, 'pg_constraint') AS comment "
            "FROM pg_constraint c LEFT JOIN pg_class ic ON ic.oid = c.conindid "
            "WHERE c.conrelid = CAST(:q AS regclass) "
            "  AND c.contype IN ('c', 'u', 'x', 'f') ORDER BY c.conname"
        ),
        {"q": QUALIFIED},
    ).fetchall()
    return [
        r for r in rows
        if not (r.contype == "c"
                and _later_revision_unit(LATER_CONSTRAINTS, r.conname, r.comment))
    ]


def _index_definitions(bind) -> dict:
    """`{name: (columns, unique, usable, restricted)}` — 028's reader."""
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
            "       (i.indpred IS NOT NULL OR i.indexprs IS NOT NULL) AS restricted, "
            "       obj_description(ic.oid, 'pg_class') AS comment "
            "FROM pg_index i JOIN pg_class ic ON ic.oid = i.indexrelid "
            "WHERE i.indrelid = CAST(:q AS regclass)"
        ),
        {"q": QUALIFIED},
    ).fetchall()
    return {
        r.name: (list(r.columns or []), r.indisunique, r.usable, r.restricted)
        for r in rows
        if not _later_revision_unit(LATER_INDEXES, r.name, r.comment)
    }


def _canonical_check(bind, column: str, sqltype: str, predicate: str) -> str:
    """How this server renders `predicate`, measured off a scratch TEMP table
    carrying the identical declaration (013's device, as 028 uses it)."""
    scratch = "_omcp_032_check_probe"
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
    scratch = "_omcp_032_default_probe"
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
# verify
# --------------------------------------------------------------------------


def _pin_search_path() -> None:
    op.execute("SET LOCAL search_path TO public")


def _assert_is_the_qualified_table(bind, table: str, qualified: str) -> None:
    resolved = _oid(bind, qualified)
    unqualified = _oid(bind, table)
    if resolved is None or resolved != unqualified:
        raise RuntimeError(
            f"032's {table} is not the table {qualified} resolves to "
            f"(search_path-relative oid {unqualified!r}, qualified oid "
            f"{resolved!r}). The index pass and `list_tasks` read the "
            "unqualified name. Set the migration role's search_path so `public` "
            "comes first, then re-run."
        )


def _table_problems(bind) -> list:
    """Everything about an existing `note_tasks` that is not exactly 032's
    shape. Complete, not minimal (024's rule): constraint and index sets are
    compared as sets, so an extra UNIQUE or CHECK makes it not 032's table."""
    problems = []
    if _table_comment(bind) != TABLE_MARKER:
        problems.append("it does not carry 032's comment marker")

    columns = _columns(bind)
    if columns != list(COLUMNS):
        problems.append(f"its columns are {columns}, not {list(COLUMNS)}")

    pk = _primary_key(bind)
    if pk is None or pk[0] != ["id"]:
        problems.append(
            "its primary key is "
            + ("absent" if pk is None else f"on {pk[0]}")
            + ", not on ['id']"
        )
    id_state = _column_state(bind, QUALIFIED, "id")
    id_default = id_state[2] if id_state is not None else None
    if not (id_default or "").startswith("nextval("):
        problems.append(f"its id default is {id_default!r}, not a sequence")

    for column, (sqltype, declaration) in DEFAULTS.items():
        state = _column_state(bind, QUALIFIED, column)
        live = state[2] if state is not None else None
        canonical = _canonical_default(bind, sqltype, declaration)
        if live != canonical:
            problems.append(f"its {column} default is {live!r}, not {canonical!r}")

    expected_checks = {
        _canonical_check(bind, column, sqltype, predicate): name
        for name, (column, sqltype, predicate) in CHECKS.items()
    }
    seen_checks: set = set()
    seen_unique = seen_fk = False
    unique_index = None
    for row in _other_constraints(bind):
        if not row.convalidated:
            problems.append(f"its constraint {row.conname!r} is NOT VALID")
        if row.contype == "c":
            name = expected_checks.get(row.definition)
            if name is None or name in seen_checks:
                problems.append(
                    f"it carries a CHECK {row.conname!r} ({row.definition}) that "
                    "is not one of 032's closed-set predicates"
                )
                continue
            seen_checks.add(name)
        elif row.contype == "u" and row.definition == UNIQUE_DEFINITION and not seen_unique:
            seen_unique = True
            unique_index = row.index_name
        elif row.contype == "f" and row.definition == FK_DEFINITION and not seen_fk:
            seen_fk = True
        else:
            kind = {"u": "UNIQUE", "x": "EXCLUSION", "f": "FOREIGN KEY"}.get(
                row.contype, row.contype
            )
            problems.append(
                f"it carries an unexpected {kind} constraint {row.conname!r} "
                f"({row.definition}) that 032 does not create"
            )
    for missing in sorted(set(expected_checks.values()) - seen_checks):
        problems.append(f"it is missing the closed-set CHECK {missing}")
    if not seen_unique:
        problems.append(f"it is missing the constraint {UNIQUE_DEFINITION}")
    if not seen_fk:
        problems.append(
            f"it is missing the constraint {FK_DEFINITION} — a task row could "
            "then outlive its note"
        )

    live_indexes = _index_definitions(bind)
    for name, expected in INDEXES.items():
        actual = live_indexes.get(name)
        if actual is None:
            problems.append(f"it is missing index {name}")
        elif actual != expected:
            problems.append(
                f"its index {name} is {actual}, not {expected[0]} as a plain, "
                "valid, non-unique index"
            )
    permitted = set(INDEXES)
    if pk is not None:
        permitted.add(pk[1])
    if unique_index is not None:
        permitted.add(unique_index)
    for name in sorted(set(live_indexes) - permitted):
        problems.append(
            f"it carries an unexpected index {name!r} on {live_indexes[name][0]} "
            "that 032 does not create"
        )
    return problems


def _column_problems(bind, column: str, marker: str) -> list:
    state = _column_state(bind, NOTES_QUALIFIED, column)
    if state is None:
        return []
    coltype, notnull, default, comment = state
    problems = []
    if coltype != "boolean":
        problems.append(f"{NOTES}.{column} is {coltype}, not boolean")
    if not notnull:
        problems.append(f"{NOTES}.{column} is nullable; 032 creates it NOT NULL")
    if default != "false":
        problems.append(
            f"{NOTES}.{column} has the server default {default!r}, not 'false'"
        )
    if comment != marker:
        problems.append(f"{NOTES}.{column} does not carry 032's comment marker")
    return problems


# --------------------------------------------------------------------------
# create
# --------------------------------------------------------------------------


def _create_table() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("note_id", sa.Integer(), nullable=False),
        sa.Column("line", sa.Integer(), nullable=False),
        sa.Column("status_char", sa.String(1), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("text_cut", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("due", sa.Date(), nullable=True),
        sa.Column("scheduled", sa.Date(), nullable=True),
        sa.Column("start", sa.Date(), nullable=True),
        sa.Column("done_on", sa.Date(), nullable=True),
        sa.Column("priority", sa.String(8), nullable=True),
        sa.Column("tags", sa.ARRAY(sa.String(128)), nullable=True),
        sa.Column("tags_cut", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("heading_path", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["note_id"], ["notes_metadata.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("note_id", "line", name=UNIQUE),
        sa.CheckConstraint(STATE_PREDICATE, name=STATE_CHECK),
        sa.CheckConstraint(PRIORITY_PREDICATE, name=PRIORITY_CHECK),
    )
    op.create_index(NOTE_INDEX, TABLE, ["note_id"])
    op.create_index(STATE_DUE_INDEX, TABLE, ["state", "due"])
    op.execute(f"COMMENT ON TABLE {TABLE} IS {_quote(TABLE_MARKER)}")


def _create_column(column: str, marker: str) -> None:
    op.add_column(
        NOTES,
        sa.Column(column, sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.execute(f"COMMENT ON COLUMN {NOTES}.{column} IS {_quote(marker)}")


# --------------------------------------------------------------------------
# upgrade / downgrade
# --------------------------------------------------------------------------


def upgrade() -> None:
    bind = op.get_bind()
    op.execute("SET LOCAL lock_timeout = '10s'")
    op.execute("SET LOCAL statement_timeout = '60s'")
    _pin_search_path()
    _assert_is_the_qualified_table(bind, NOTES, NOTES_QUALIFIED)

    # Verify every pre-existing unit before creating anything, so a refusal of
    # one leaves the database exactly as it was.
    refusals = []
    if _oid(bind, QUALIFIED) is not None:
        problems = _table_problems(bind)
        if problems:
            refusals.append(f"{TABLE} already exists but {'; '.join(problems)}")
    for column, marker in NOTE_COLUMNS.items():
        refusals.extend(_column_problems(bind, column, marker))
    if refusals:
        raise RuntimeError(
            ". ".join(refusals)
            + ". 032 will not adopt a unit of unknown provenance: `list_tasks` "
            "answers 'which tasks are open' from these rows and bits, and a "
            "shape this migration did not create can list a task that does not "
            "exist or hide one that does. Resolve by hand — drop it and let 032 "
            "create it — then re-run. Nothing has been changed."
        )

    if _oid(bind, QUALIFIED) is None:
        _create_table()
    _assert_is_the_qualified_table(bind, TABLE, QUALIFIED)
    for column, marker in NOTE_COLUMNS.items():
        if _column_state(bind, NOTES_QUALIFIED, column) is None:
            _create_column(column, marker)

    # No row is written on any path: the rows need the note bodies, and the
    # first pass after deploy derives them (extraction version 4).
    op.execute("RESET lock_timeout")
    op.execute("RESET statement_timeout")
    op.execute("RESET search_path")


def downgrade() -> None:
    """Drop each unit only if it carries 032's marker, each decided on its own,
    printing what is left and why (`alembic/env.py` configures no logging).

    Losing the rows is safe: they are derived, and the previous build neither
    reads nor writes them. A re-upgrade leaves the table empty and both bits
    false until rows are re-derived — which needs an extraction-version bump or
    a reindex, because an unchanged v4 row is not re-derived on its own.
    """
    bind = op.get_bind()
    _pin_search_path()
    if _oid(bind, QUALIFIED) is not None:
        _assert_is_the_qualified_table(bind, TABLE, QUALIFIED)
        if _table_comment(bind) != TABLE_MARKER:
            print(
                f"032 downgrade: leaving {TABLE} in place — it does not carry "
                f"032's marker ({TABLE_MARKER!r}), so 032 did not create it. "
                "Remove it by hand if you mean to.",
                flush=True,
            )
        else:
            op.drop_index(STATE_DUE_INDEX, table_name=TABLE)
            op.drop_index(NOTE_INDEX, table_name=TABLE)
            op.drop_table(TABLE)
    if _oid(bind, NOTES_QUALIFIED) is not None:
        for column, marker in NOTE_COLUMNS.items():
            state = _column_state(bind, NOTES_QUALIFIED, column)
            if state is None:
                continue
            if state[3] != marker:
                print(
                    f"032 downgrade: leaving {NOTES}.{column} in place. It does "
                    f"not carry 032's marker ({marker!r}) — its comment is "
                    f"{state[3]!r} — so 032 did not create it and will not drop "
                    "it. Remove it by hand if you mean to.",
                    flush=True,
                )
            else:
                op.drop_column(NOTES, column)
    op.execute("RESET search_path")
