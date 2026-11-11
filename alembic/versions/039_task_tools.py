"""Task ids, the write-time task refresh and `task_settings` (#428).

The task tools (`complete_task`, `update_task`, `add_task`, `task_agenda` and
the new `list_tasks` arguments) need a stored id per task, a few derived
fields the listing filters on, two per-note columns that say which bytes a
note's task rows describe, and one durable settings row per owner scope. This
revision creates exactly those objects (design D10, D11, D14 of the
`task-tools` change) and nothing else.

## `note_tasks` (derived; nullable until the version-5 pass)

| column | type | meaning |
| --- | --- | --- |
| `task_key` | `char(12)` | D1 key over the task's uncut text |
| `key_ordinal`, `key_count` | `integer` | this task's rank among same-key tasks of the note, and their number |
| `description_key` | `char(16)` | D9 duplicate-detection key |
| `text_fold` | `text` | NFC + casefold of the stored (cut) text, for `list_tasks(text=…)` |
| `tag_prefixes` | `varchar(128)[]` | every lower-cased `/`-segment prefix of every stored tag; **not capped** (lossless by construction, ≤ 4,096 entries) |
| `recurrence` | `varchar(256)` | the run's `🔁` value, cut at 256 (`recurrence_cut`) |
| `recurrence_cut` | `boolean NOT NULL DEFAULT false` | |
| `created`, `cancelled` | `date` | `➕` / `❌` |
| `context` | `text` | ≤ 8 continuation lines, ≤ 600 characters |
| `context_cut` | `boolean NOT NULL DEFAULT false` | |

Plus `ck_note_tasks_key_shape` (the key triple is all NULL, or all set with
`1 ≤ key_ordinal ≤ key_count`), a GIN index on `tag_prefixes` and a partial
index on `description_key WHERE state = 'open'`.

Every column is nullable or defaulted, so a rolling deploy with the previous
image keeps working: its delete-and-insert writes NULLs, which the CHECK
admits, and it never reads the new columns. The first version-5 pass fills
them (`CURRENT_EXTRACTION_VERSION` 5, #150's mechanism); this migration writes
no row.

## `notes_metadata.tasks_source_hash` / `.tasks_refreshed_at`

`varchar(64) NULL` and `timestamptz NULL`. The canonical hash of the bytes a
note's task rows were derived from, and when a task tool last refreshed them
(D10). NULL on every existing row; the pass and the refresh write them.

## `task_settings` (durable)

One row per owner scope — `user_id` NULL is the single-user scope, `UNIQUE
NULLS NOT DISTINCT`, `ON DELETE CASCADE` from `users` — holding the timezone,
the default-scope exclusions and four nullable write-behaviour booleans (NULL
means "the environment default"). No row means every value is the default, so
an existing deployment behaves exactly as before until someone saves the
card. Dumped with its data by backups (it cannot be rebuilt from the vault).

## Reconciliation, not adoption

The schema gate stamps back and re-runs this body against a database that
already has every unit. Each column is verified by type, nullability, default
and comment marker; the CHECK by its validated definition, compared with what
this server prints for a scratch probe, and its marker; each index by its full
`pg_get_indexdef` against a probe built from the same DDL, its validity, its
table and its marker; the table against a TEMP twin of its own DDL (columns,
defaults, every constraint, indexes) plus the foreign key's printed definition
and its marker. Any other same-named object is refused by name before anything
is created. `downgrade()` drops only marked objects.

**Units marked by this revision are 039's to verify.** 032 compares
`note_tasks` completely, so a stamp-back that re-runs 032 over a head database
would otherwise refuse 039's columns, CHECK and indexes as foreign. 032 leaves
every unit whose comment carries a later revision's marker to that revision.

`search_path` is pinned, and `lock_timeout` / `statement_timeout` set and
`RESET`, for 021's, 024's and 025's reasons. Nothing here needs superuser and
no extension is created (`test_nonsuperuser_migrations_pg.py`).

Revision ID: 039
Revises: 038
Create Date: 2026-11-10
"""
import re
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "039"
down_revision: Union[str, None] = "038"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


TASKS = "note_tasks"
TASKS_QUALIFIED = "public.note_tasks"
NOTES = "notes_metadata"
NOTES_QUALIFIED = "public.notes_metadata"
SETTINGS = "task_settings"
SETTINGS_QUALIFIED = "public.task_settings"

# Byte identical to the `_TASK_*_MARKER` constants in `src/models/db.py`, where
# the column and table markers are the comments `alembic check` compares. The
# CHECK and index markers are not compared by alembic; the schema gate and this
# body read them.
TASK_KEY_MARKER = "task id key and occurrence (039_task_tools)"
DESCRIPTION_KEY_MARKER = "task duplicate-detection key (039_task_tools)"
TEXT_FOLD_MARKER = "case-folded task text (039_task_tools)"
TAG_PREFIXES_MARKER = "hierarchical task tag prefixes (039_task_tools)"
RECURRENCE_MARKER = "task recurrence rule (039_task_tools)"
TASK_DATES_MARKER = "task created and cancelled dates (039_task_tools)"
CONTEXT_MARKER = "task context lines (039_task_tools)"
SOURCE_HASH_MARKER = "hash of the bytes the task rows describe (039_task_tools)"
REFRESHED_AT_MARKER = "task write-time refresh stamp (039_task_tools)"
SETTINGS_MARKER = "task settings per owner scope (039_task_tools)"
KEY_SHAPE_MARKER = "task id key shape (039_task_tools)"
TAG_PREFIXES_INDEX_MARKER = "task tag-prefix filter index (039_task_tools)"
OPEN_DESC_INDEX_MARKER = "open-task duplicate lookup index (039_task_tools)"

# `(table, qualified, column, DDL type, format_type, NOT NULL, default, marker)`
# in creation order. `default` is the declaration; the reconciler compares the
# server's rendering of it, measured off a probe.
COLUMNS = (
    (TASKS, TASKS_QUALIFIED, "task_key", "char(12)", "character(12)", False, None, TASK_KEY_MARKER),
    (TASKS, TASKS_QUALIFIED, "key_ordinal", "integer", "integer", False, None, TASK_KEY_MARKER),
    (TASKS, TASKS_QUALIFIED, "key_count", "integer", "integer", False, None, TASK_KEY_MARKER),
    (TASKS, TASKS_QUALIFIED, "description_key", "char(16)", "character(16)", False, None,
     DESCRIPTION_KEY_MARKER),
    (TASKS, TASKS_QUALIFIED, "text_fold", "text", "text", False, None, TEXT_FOLD_MARKER),
    (TASKS, TASKS_QUALIFIED, "tag_prefixes", "varchar(128)[]", "character varying(128)[]",
     False, None, TAG_PREFIXES_MARKER),
    (TASKS, TASKS_QUALIFIED, "recurrence", "varchar(256)", "character varying(256)", False,
     None, RECURRENCE_MARKER),
    (TASKS, TASKS_QUALIFIED, "recurrence_cut", "boolean", "boolean", True, "false",
     RECURRENCE_MARKER),
    (TASKS, TASKS_QUALIFIED, "created", "date", "date", False, None, TASK_DATES_MARKER),
    (TASKS, TASKS_QUALIFIED, "cancelled", "date", "date", False, None, TASK_DATES_MARKER),
    (TASKS, TASKS_QUALIFIED, "context", "text", "text", False, None, CONTEXT_MARKER),
    (TASKS, TASKS_QUALIFIED, "context_cut", "boolean", "boolean", True, "false", CONTEXT_MARKER),
    (NOTES, NOTES_QUALIFIED, "tasks_source_hash", "varchar(64)", "character varying(64)",
     False, None, SOURCE_HASH_MARKER),
    (NOTES, NOTES_QUALIFIED, "tasks_refreshed_at", "timestamp with time zone",
     "timestamp with time zone", False, None, REFRESHED_AT_MARKER),
)

KEY_SHAPE = "ck_note_tasks_key_shape"
# Byte identical to `_NOTE_TASK_KEY_SHAPE_PREDICATE` in `src/models/db.py`.
KEY_SHAPE_PREDICATE = (
    "(task_key IS NULL AND key_ordinal IS NULL AND key_count IS NULL) OR "
    "(task_key IS NOT NULL AND key_ordinal IS NOT NULL AND key_count IS NOT NULL "
    "AND key_ordinal >= 1 AND key_ordinal <= key_count)"
)

TAG_PREFIXES_INDEX = "ix_note_tasks_tag_prefixes"
OPEN_DESC_INDEX = "ix_note_tasks_open_desc"


def _index_ddl(table: str) -> dict:
    """`{name: CREATE INDEX statement}` for 039's two `note_tasks` indexes."""
    return {
        TAG_PREFIXES_INDEX: (
            f"CREATE INDEX {TAG_PREFIXES_INDEX} ON {table} USING gin (tag_prefixes)",
            TAG_PREFIXES_INDEX_MARKER,
        ),
        OPEN_DESC_INDEX: (
            f"CREATE INDEX {OPEN_DESC_INDEX} ON {table} (description_key) "
            "WHERE state = 'open'",
            OPEN_DESC_INDEX_MARKER,
        ),
    }


# The foreign key cannot live on a TEMP twin (a temporary table may reference
# only temporary tables), so it is compared as PostgreSQL 16 prints it with
# `search_path` pinned to `public`, like 032's.
SETTINGS_FK = "task_settings_user_id_fkey"
SETTINGS_FK_DEFINITION = "FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE"


def _settings_ddl(table: str, *, temp: bool) -> str:
    """`task_settings` without its foreign key (added separately)."""
    return f"""CREATE {'TEMP ' if temp else ''}TABLE {table} (
  id serial NOT NULL,
  user_id integer,
  timezone varchar(64),
  excluded_folders varchar(1024)[] NOT NULL DEFAULT '{{}}',
  excluded_note_tags varchar(128)[] NOT NULL DEFAULT '{{}}',
  set_done_date boolean,
  set_cancelled_date boolean,
  set_created_date boolean,
  next_recurrence_below boolean,
  updated_at timestamp with time zone NOT NULL DEFAULT now(),
  CONSTRAINT task_settings_pkey PRIMARY KEY (id),
  CONSTRAINT uq_task_settings_user_id UNIQUE NULLS NOT DISTINCT (user_id)
)"""


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


def _column_state(bind, qualified: str, column: str):
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
        {"q": qualified, "c": column},
    ).first()


def _constraint(bind, qualified: str, name: str):
    """`(contype, convalidated, normalised definition, comment)` or None."""
    row = bind.execute(
        sa.text(
            "SELECT CAST(contype AS text), convalidated, pg_get_constraintdef(oid), "
            "       obj_description(oid, 'pg_constraint') "
            "FROM pg_constraint "
            "WHERE conrelid = CAST(:q AS regclass) AND conname = :name"
        ),
        {"q": qualified, "name": name},
    ).first()
    if row is None:
        return None
    return row[0], row[1], " ".join(row[2].split()), row[3]


def _index(bind, name: str):
    """`(table oid, usable, normalised indexdef, comment)` of the index named
    `name` in `public`, or None. An index name is schema-wide, so a same-named
    index on another table is found too and refused."""
    row = bind.execute(
        sa.text(
            "SELECT i.indrelid, (i.indisvalid AND i.indisready), "
            "       pg_get_indexdef(i.indexrelid), obj_description(ic.oid, 'pg_class') "
            "FROM pg_class ic JOIN pg_namespace n ON n.oid = ic.relnamespace "
            "JOIN pg_index i ON i.indexrelid = ic.oid "
            "WHERE n.nspname = 'public' AND ic.relname = :name"
        ),
        {"name": name},
    ).first()
    if row is None:
        return None
    return row[0], row[1], _normalise_indexdef(row[2]), row[3]


def _relation_in_public(bind, name: str):
    """The relkind of any relation named `name` in `public` (an index name and
    a table name share one namespace), or None."""
    return bind.execute(
        sa.text(
            "SELECT CAST(c.relkind AS text) FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'public' AND c.relname = :name"
        ),
        {"name": name},
    ).scalar()


def _normalise_indexdef(definition: str) -> str:
    """The definition with the table reference removed, so a probe's prints the
    same as the live index's."""
    return re.sub(r" ON \S+ ", " ON <table> ", " ".join(definition.split()), count=1)


def _table_comment(bind, qualified: str):
    return bind.execute(
        sa.text("SELECT obj_description(CAST(:q AS regclass), 'pg_class')"),
        {"q": qualified},
    ).scalar()


# Units a later revision adds to `task_settings`, by kind and exact name, each
# with the exact marker comment that revision writes. The complete comparison
# below leaves out exactly these — a unit of one of these names *and* this
# comment — because that revision verifies them; otherwise a stamp-back that
# re-runs 039 over a head database would refuse them as foreign (032's device
# for 039's `note_tasks` units). Anything else (another name, the right name
# with another comment) is compared and refused as before.
#
# **A future revision that adds a unit to `task_settings` must extend these
# lists on purpose**, with its literal names and markers; nothing is inferred
# from a comment's shape. Byte identical to 040's literals (#434).
LATER_SETTINGS_COLUMNS = {
    "task_rule": "task rule kind (040_task_rule)",
    "task_tags": "task rule task tags (040_task_rule)",
    "global_filter": "task rule global filter text (040_task_rule)",
}
LATER_SETTINGS_CONSTRAINTS = {
    "ck_task_settings_task_rule": "task rule kinds (040_task_rule)",
    "ck_task_settings_global_filter": "a filter rule has its filter text (040_task_rule)",
}


def _later_revision_unit(allow_list: dict, name, comment) -> bool:
    return comment is not None and allow_list.get(name) == comment


def _shape(bind, qualified: str) -> dict:
    """Everything 039 compares about `task_settings`, from the catalogue (033's
    reader), less a later revision's marked units (`LATER_SETTINGS_*`).
    Foreign keys are excluded here and compared on their own."""
    columns = [
        tuple(r[:4])
        for r in bind.execute(
            sa.text(
                "SELECT a.attname, format_type(a.atttypid, a.atttypmod), a.attnotnull, "
                "       CASE WHEN pg_get_expr(d.adbin, d.adrelid) LIKE 'nextval(%%' "
                "            THEN 'nextval' ELSE pg_get_expr(d.adbin, d.adrelid) END, "
                "       col_description(a.attrelid, a.attnum) "
                "FROM pg_attribute a "
                "LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum "
                "WHERE a.attrelid = CAST(:q AS regclass) AND a.attnum > 0 "
                "  AND NOT a.attisdropped ORDER BY a.attnum"
            ),
            {"q": qualified},
        ).fetchall()
        if not _later_revision_unit(LATER_SETTINGS_COLUMNS, r[0], r[4])
    ]
    constraints = sorted(
        (r[0], r[1], r[2], r[3])
        for r in bind.execute(
            sa.text(
                "SELECT c.conname, CAST(c.contype AS text), "
                "       pg_get_constraintdef(c.oid), c.convalidated, "
                "       obj_description(c.oid, 'pg_constraint') "
                "FROM pg_constraint c WHERE c.conrelid = CAST(:q AS regclass) "
                "  AND c.contype <> 'f'"
            ),
            {"q": qualified},
        ).fetchall()
        if not (r[1] == "c"
                and _later_revision_unit(LATER_SETTINGS_CONSTRAINTS, r[0], r[4]))
    )
    indexes = sorted(
        (r[0], _normalise_indexdef(r[1]), r[2])
        for r in bind.execute(
            sa.text(
                "SELECT ic.relname, pg_get_indexdef(i.indexrelid), "
                "       (i.indisvalid AND i.indisready) "
                "FROM pg_index i JOIN pg_class ic ON ic.oid = i.indexrelid "
                "WHERE i.indrelid = CAST(:q AS regclass)"
            ),
            {"q": qualified},
        ).fetchall()
    )
    return {"columns": columns, "constraints": constraints, "indexes": indexes}


def _foreign_keys(bind, qualified: str) -> list:
    return [
        (r[0], " ".join(r[1].split()), r[2])
        for r in bind.execute(
            sa.text(
                "SELECT conname, pg_get_constraintdef(oid), convalidated "
                "FROM pg_constraint WHERE conrelid = CAST(:q AS regclass) "
                "  AND contype = 'f' ORDER BY conname"
            ),
            {"q": qualified},
        ).fetchall()
    ]


# --------------------------------------------------------------------------
# probes: what this server prints for 039's own declarations
# --------------------------------------------------------------------------

_PROBE = "_omcp_039_probe"


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


def _probe_tasks(bind):
    """A TEMP table with the `note_tasks` columns 039's CHECK and indexes read,
    typed identically, so their printed definitions can be measured."""
    bind.execute(sa.text(f"DROP TABLE IF EXISTS pg_temp.{_PROBE}"))
    bind.execute(
        sa.text(
            f"CREATE TEMP TABLE {_PROBE} (state varchar(16), task_key char(12), "
            "key_ordinal integer, key_count integer, description_key char(16), "
            "tag_prefixes varchar(128)[])"
        )
    )


def _canonical_key_shape(bind) -> str:
    _probe_tasks(bind)
    bind.execute(
        sa.text(
            f"ALTER TABLE pg_temp.{_PROBE} ADD CONSTRAINT {_PROBE}_ck "
            f"CHECK ({KEY_SHAPE_PREDICATE})"
        )
    )
    rendered = bind.execute(
        sa.text(
            "SELECT pg_get_constraintdef(c.oid) FROM pg_constraint c "
            "WHERE c.conrelid = CAST(:s AS regclass) AND c.contype = 'c'"
        ),
        {"s": f"pg_temp.{_PROBE}"},
    ).scalar()
    bind.execute(sa.text(f"DROP TABLE IF EXISTS pg_temp.{_PROBE}"))
    return " ".join(rendered.split())


def _canonical_indexdefs(bind) -> dict:
    _probe_tasks(bind)
    rendered = {}
    for name, (statement, _marker) in _index_ddl(f"pg_temp.{_PROBE}").items():
        # Probe indexes live in pg_temp, so the live names in `public` are free.
        bind.execute(sa.text(statement))
        definition = bind.execute(
            sa.text(
                "SELECT pg_get_indexdef(i.indexrelid) FROM pg_index i "
                "JOIN pg_class ic ON ic.oid = i.indexrelid "
                "WHERE i.indrelid = CAST(:s AS regclass) AND ic.relname = :n"
            ),
            {"s": f"pg_temp.{_PROBE}", "n": name},
        ).scalar()
        rendered[name] = _normalise_indexdef(definition)
    bind.execute(sa.text(f"DROP TABLE IF EXISTS pg_temp.{_PROBE}"))
    return rendered


def _expected_settings_shape(bind) -> dict:
    twin = "_omcp_039_settings_twin"
    bind.execute(sa.text(f"DROP TABLE IF EXISTS pg_temp.{twin}"))
    bind.execute(sa.text(_settings_ddl(twin, temp=True)))
    shape = _shape(bind, f"pg_temp.{twin}")
    bind.execute(sa.text(f"DROP TABLE IF EXISTS pg_temp.{twin}"))
    return shape


# --------------------------------------------------------------------------
# verify
# --------------------------------------------------------------------------


def _pin() -> None:
    op.execute("SET LOCAL search_path TO public")


def _assert_is_the_qualified_table(bind, table: str, qualified: str) -> None:
    resolved = _oid(bind, qualified)
    unqualified = _oid(bind, table)
    if resolved is None or resolved != unqualified:
        raise RuntimeError(
            f"039's {table} is not the table {qualified} resolves to "
            f"(search_path-relative oid {unqualified!r}, qualified oid "
            f"{resolved!r}). The server reads the unqualified name. Set the "
            "migration role's search_path so `public` comes first, then re-run. "
            "Nothing has been changed."
        )


def _column_problems(bind) -> tuple[list, list]:
    """`(problems, missing)` over every column 039 adds."""
    problems, missing = [], []
    canonical_defaults: dict = {}
    for table, qualified, column, ddl_type, fmt, notnull, default, marker in COLUMNS:
        state = _column_state(bind, qualified, column)
        if state is None:
            missing.append((table, column, ddl_type, notnull, default, marker))
            continue
        coltype, live_notnull, live_default, comment = state
        if coltype != fmt:
            problems.append(f"{table}.{column} is {coltype}, not {fmt}")
        if live_notnull != notnull:
            problems.append(
                f"{table}.{column} is {'NOT NULL' if live_notnull else 'nullable'}; "
                f"039 creates it {'NOT NULL' if notnull else 'nullable'}"
            )
        expected_default = None
        if default is not None:
            key = (ddl_type, default)
            if key not in canonical_defaults:
                canonical_defaults[key] = _probe_default(bind, ddl_type, default)
            expected_default = canonical_defaults[key]
        if live_default != expected_default:
            problems.append(
                f"{table}.{column} has the server default {live_default!r}, "
                f"not {expected_default!r}"
            )
        if comment != marker:
            problems.append(f"{table}.{column} does not carry 039's comment marker")
    return problems, missing


def _key_shape_problems(bind) -> list:
    existing = _constraint(bind, TASKS_QUALIFIED, KEY_SHAPE)
    if existing is None:
        return []
    contype, validated, definition, comment = existing
    canonical = _canonical_key_shape(bind)
    if contype == "c" and validated and definition == canonical and comment == KEY_SHAPE_MARKER:
        return []
    return [
        f"{TASKS} already carries a constraint {KEY_SHAPE} that is not 039's "
        f"(type {contype!r}, definition {definition!r}, validated={validated}, "
        f"marker {'present' if comment == KEY_SHAPE_MARKER else 'absent'})"
    ]


def _index_problems(bind) -> list:
    problems = []
    tasks_oid = _oid(bind, TASKS_QUALIFIED)
    canonical = None
    for name, (_statement, marker) in _index_ddl(TASKS).items():
        relkind = _relation_in_public(bind, name)
        if relkind is None:
            continue
        live = _index(bind, name)
        if live is None:
            problems.append(
                f"a relation named {name} (relkind {relkind!r}) already exists and "
                "is not an index"
            )
            continue
        if canonical is None:
            canonical = _canonical_indexdefs(bind)
        table_oid, usable, definition, comment = live
        if table_oid != tasks_oid:
            problems.append(f"an index named {name} already exists on another table")
        elif not usable:
            problems.append(f"the index {name} is not valid")
        elif definition != canonical[name]:
            problems.append(
                f"the index {name} is {definition!r}, not {canonical[name]!r}"
            )
        elif comment != marker:
            problems.append(f"the index {name} does not carry 039's comment marker")
    return problems


def _settings_problems(bind) -> list:
    if _oid(bind, SETTINGS_QUALIFIED) is None:
        return []
    if _table_comment(bind, SETTINGS_QUALIFIED) != SETTINGS_MARKER:
        return [f"{SETTINGS} already exists but it does not carry 039's comment marker"]
    problems = []
    live = _shape(bind, SETTINGS_QUALIFIED)
    expected = _expected_settings_shape(bind)
    for part in ("columns", "constraints", "indexes"):
        if live[part] != expected[part]:
            extra = sorted(set(live[part]) - set(expected[part]))
            absent = sorted(set(expected[part]) - set(live[part]))
            problems.append(
                f"{SETTINGS} already exists but its {part} differ from 039's "
                f"(unexpected: {extra}; missing: {absent})"
            )
    fks = _foreign_keys(bind, SETTINGS_QUALIFIED)
    if fks != [(SETTINGS_FK, SETTINGS_FK_DEFINITION, True)]:
        problems.append(
            f"{SETTINGS} already exists but its foreign keys are {fks}, not "
            f"[{SETTINGS_FK}: {SETTINGS_FK_DEFINITION}] — a user's settings "
            "must go with the user"
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
    _assert_is_the_qualified_table(bind, TASKS, TASKS_QUALIFIED)
    _assert_is_the_qualified_table(bind, NOTES, NOTES_QUALIFIED)

    # Verify every pre-existing unit before creating anything, so a refusal of
    # one leaves the database exactly as it was.
    refusals, missing = _column_problems(bind)
    refusals += _key_shape_problems(bind)
    refusals += _index_problems(bind)
    refusals += _settings_problems(bind)
    if refusals:
        raise RuntimeError(
            ". ".join(refusals)
            + ". 039 will not adopt a unit of unknown provenance: the task tools "
            "resolve writes, list and filter tasks and apply each owner's "
            "settings from these objects, and a shape this migration did not "
            "create can hide a task, list one that does not exist or apply "
            "another scope's settings. Resolve by hand — drop it and let 039 "
            "create it — then re-run. Nothing has been changed."
        )

    for table, column, ddl_type, notnull, default, marker in missing:
        clause = f" NOT NULL DEFAULT {default}" if notnull else ""
        op.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type}{clause}")
        op.execute(f"COMMENT ON COLUMN {table}.{column} IS {_quote(marker)}")

    if _constraint(bind, TASKS_QUALIFIED, KEY_SHAPE) is None:
        # Every existing row reads NULL in the triple, so validation admits it.
        op.execute(
            f"ALTER TABLE {TASKS} ADD CONSTRAINT {KEY_SHAPE} CHECK ({KEY_SHAPE_PREDICATE})"
        )
        op.execute(
            f"COMMENT ON CONSTRAINT {KEY_SHAPE} ON {TASKS} IS {_quote(KEY_SHAPE_MARKER)}"
        )

    for name, (statement, marker) in _index_ddl(TASKS).items():
        if _relation_in_public(bind, name) is None:
            op.execute(statement)
            op.execute(f"COMMENT ON INDEX {name} IS {_quote(marker)}")

    if _oid(bind, SETTINGS_QUALIFIED) is None:
        op.execute(_settings_ddl(SETTINGS, temp=False))
        op.execute(
            f"ALTER TABLE {SETTINGS} ADD CONSTRAINT {SETTINGS_FK} "
            "FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE"
        )
        op.execute(f"COMMENT ON TABLE {SETTINGS} IS {_quote(SETTINGS_MARKER)}")
    _assert_is_the_qualified_table(bind, SETTINGS, SETTINGS_QUALIFIED)

    # No row is written on any path: task rows need the note bodies (the first
    # version-5 pass derives them), and no settings row means "the defaults".
    op.execute("RESET lock_timeout")
    op.execute("RESET statement_timeout")
    op.execute("RESET search_path")


def _leave(unit: str, marker: str, comment) -> None:
    print(
        f"039 downgrade: leaving {unit} in place. It does not carry 039's marker "
        f"({marker!r}) — its comment is {comment!r} — so 039 did not create it "
        "and will not drop it. Remove it by hand if you mean to.",
        flush=True,
    )


def downgrade() -> None:
    """Drop each unit only if it carries 039's marker, each decided on its own,
    printing what is left and why (`alembic/env.py` configures no logging).

    The `note_tasks` and `notes_metadata` columns are derived: the previous
    build neither reads nor writes them. `task_settings` is durable, so its
    rows are lost with it; the count is printed. The previous build does not
    read them either.
    """
    bind = op.get_bind()
    op.execute("SET LOCAL lock_timeout = '10s'")
    op.execute("SET LOCAL statement_timeout = '60s'")
    _pin()

    if _oid(bind, SETTINGS_QUALIFIED) is not None:
        _assert_is_the_qualified_table(bind, SETTINGS, SETTINGS_QUALIFIED)
        comment = _table_comment(bind, SETTINGS_QUALIFIED)
        if comment != SETTINGS_MARKER:
            _leave(SETTINGS, SETTINGS_MARKER, comment)
        else:
            rows = bind.execute(sa.text(f"SELECT count(*) FROM {SETTINGS}")).scalar_one()
            if rows:
                print(
                    f"039 downgrade: dropping {SETTINGS} with {rows} saved settings "
                    "row(s); the previous build does not read them.",
                    flush=True,
                )
            op.execute(f"DROP TABLE {SETTINGS}")

    # A column that a surviving (unmarked) CHECK or index reads is kept too:
    # dropping it would silently drop that object with it.
    kept: set = set()
    if _oid(bind, TASKS_QUALIFIED) is not None:
        _assert_is_the_qualified_table(bind, TASKS, TASKS_QUALIFIED)
        index_columns = {
            TAG_PREFIXES_INDEX: {"tag_prefixes"},
            OPEN_DESC_INDEX: {"description_key"},
        }
        for name, (_statement, marker) in _index_ddl(TASKS).items():
            live = _index(bind, name)
            if live is None:
                continue
            if live[3] != marker:
                _leave(f"index {name}", marker, live[3])
                if live[0] == _oid(bind, TASKS_QUALIFIED):
                    kept |= index_columns[name]
            else:
                op.execute(f"DROP INDEX {name}")
        existing = _constraint(bind, TASKS_QUALIFIED, KEY_SHAPE)
        if existing is not None:
            if existing[3] != KEY_SHAPE_MARKER:
                _leave(f"constraint {KEY_SHAPE} on {TASKS}", KEY_SHAPE_MARKER, existing[3])
                kept |= {"task_key", "key_ordinal", "key_count"}
            else:
                op.execute(f"ALTER TABLE {TASKS} DROP CONSTRAINT {KEY_SHAPE}")

    for table, qualified, column, _ddl, _fmt, _nn, _default, marker in reversed(COLUMNS):
        if _oid(bind, qualified) is None:
            continue
        state = _column_state(bind, qualified, column)
        if state is None:
            continue
        if state[3] != marker:
            _leave(f"{table}.{column}", marker, state[3])
        elif table == TASKS and column in kept:
            print(
                f"039 downgrade: leaving {table}.{column} in place — an object 039 "
                "did not create, and so did not drop, still reads it.",
                flush=True,
            )
        else:
            op.execute(f"ALTER TABLE {table} DROP COLUMN {column}")

    op.execute("RESET lock_timeout")
    op.execute("RESET statement_timeout")
    op.execute("RESET search_path")
