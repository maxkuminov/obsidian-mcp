"""Heading-aware chunks: the chunk locator, the context prefix and the
per-note chunking version (#355, design D3a/D4/D5).

The indexer now splits a note at its ATX headings (the section-addressing
scanner, so a chunk's ordinal is the one `read_note(section="#N")` resolves)
and embeds a `Title > H2 > H3` context prefix ahead of each chunk. This
revision adds what that needs to be recorded:

| table | column | type |
|---|---|---|
| `note_embeddings` | `heading_path` | `text[]` (`{}` for section 0) |
| `note_embeddings` | `section_ordinal` | `integer` (0 for section 0) |
| `note_embeddings` | `embed_prefix` | `text` (≤ 300 characters, app-enforced) |
| `notes_metadata` | `embedded_chunking_version` | `smallint` (NULL: pre-034) |
| `notes_metadata` | `embedded_input_digest` | `varchar(64)` (NULL: pre-034) |

All nullable, with no default, **no backfill and no index**. Every pre-034 row
reads NULL in all five, which is exactly what it is: a legacy chunk with no
recorded section and a note certified under the pre-034 chunker. The embed
backlog re-selects such notes per note (`embedded_chunking_version IS DISTINCT
FROM CHUNKING_VERSION`); the stored embedding fingerprint is untouched, so the
deploy neither refuses nor resets (design D4).

## The CHECKs

`ck_note_embeddings_heading_meta`: the three row columns all NULL, or all set
with `section_ordinal >= 0` and `(section_ordinal = 0) = (cardinality(
heading_path) = 0)`. `ck_notes_metadata_embedded_chunking_version`: NULL or
`>= 1`. Both are added `NOT VALID` and then `VALIDATE`d, so the scan of
`note_embeddings` holds only `SHARE UPDATE EXCLUSIVE`. They are resolved
through `pg_constraint` by **definition**, measured off a TEMP twin (026's
device), never by name.

## Reconciliation, not adoption

The gate stamps back and re-runs this body against a database that already
carries its units. A same-named column must have the exact type, be nullable,
carry no default and carry 034's marker; a CHECK of 034's name must have 034's
definition, be validated and be marked. Anything else is refused by name
before anything is changed (026/030's shape). `search_path` is pinned and
asserted; `lock_timeout` / `statement_timeout` are set and `RESET`.

## Downgrade

In one transaction holding the index generation lock (written literally):
clear `embedded_content_hash` for every note holding a row with a non-NULL
`embed_prefix`, delete those rows, then drop only the marked constraints and
columns. The 033 build reuses a stored vector on exact `chunk_text` alone, so
a vector of a *prefixed* input left in place would be reused as if it were the
vector of the bare chunk. Deleting them costs semantic search those notes until
the old build re-embeds them (L7). Both tables are derived for backups (#348).

Revision ID: 034
Revises: 033
Create Date: 2026-10-30
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "034"
down_revision: Union[str, None] = "033"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


#: `index_state.INDEX_GENERATION_LOCK_KEY`, written out literally as a
#: migration must; `tests/test_migration_034_literals.py` pins equality.
INDEX_GENERATION_LOCK_KEY = 8029183045093649969

# Byte identical to the markers in `src/models/db.py`, where they are the
# column comments so `alembic check` compares them.
HEADING_META_MARKER = "chunk section locator (034_heading_chunks)"
EMBED_PREFIX_MARKER = "embedded context prefix (034_heading_chunks)"
CHUNKING_VERSION_MARKER = "chunking version of the stored vectors (034_heading_chunks)"
INPUT_DIGEST_MARKER = "digest of the embedded provider inputs (034_heading_chunks)"

#: `(table, column, DDL type, format_type rendering, marker)` in creation order.
COLUMNS = (
    ("note_embeddings", "heading_path", "text[]", "text[]", HEADING_META_MARKER),
    ("note_embeddings", "section_ordinal", "integer", "integer", HEADING_META_MARKER),
    ("note_embeddings", "embed_prefix", "text", "text", EMBED_PREFIX_MARKER),
    (
        "notes_metadata", "embedded_chunking_version", "smallint", "smallint",
        CHUNKING_VERSION_MARKER,
    ),
    (
        "notes_metadata", "embedded_input_digest", "varchar(64)",
        "character varying(64)", INPUT_DIGEST_MARKER,
    ),
)

# Byte identical to `_HEADING_META_PREDICATE` / `_CHUNKING_VERSION_PREDICATE`
# in `src/models/db.py`.
HEADING_META_PREDICATE = (
    "(heading_path IS NULL AND section_ordinal IS NULL AND embed_prefix IS NULL) OR "
    "(heading_path IS NOT NULL AND section_ordinal IS NOT NULL "
    "AND embed_prefix IS NOT NULL AND section_ordinal >= 0 "
    "AND (section_ordinal = 0) = (cardinality(heading_path) = 0))"
)
CHUNKING_VERSION_PREDICATE = (
    "embedded_chunking_version IS NULL OR embedded_chunking_version >= 1"
)

#: `(name, table, predicate, scratch columns DDL, marker)`. Constraint comments
#: are not compared by `alembic check`, so the markers live here only.
CHECKS = (
    (
        "ck_note_embeddings_heading_meta",
        "note_embeddings",
        HEADING_META_PREDICATE,
        "heading_path text[], section_ordinal integer, embed_prefix text",
        "all-or-none chunk locator (034_heading_chunks)",
    ),
    (
        "ck_notes_metadata_embedded_chunking_version",
        "notes_metadata",
        CHUNKING_VERSION_PREDICATE,
        "embedded_chunking_version smallint",
        "chunking version floor (034_heading_chunks)",
    ),
)

TABLES = ("note_embeddings", "notes_metadata")


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


def _column_state(bind, table: str, column: str):
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
        {"table": f"public.{table}", "column": column},
    ).first()


def _canonical_check(bind, name: str, columns_ddl: str, predicate: str) -> str:
    """What this server renders `predicate` as, measured not guessed (026)."""
    scratch = f"_omcp_034_probe_{name[-12:]}"
    bind.execute(sa.text(f"DROP TABLE IF EXISTS pg_temp.{scratch}"))
    bind.execute(
        sa.text(
            f"CREATE TEMP TABLE {scratch} ({columns_ddl}, "
            f"CONSTRAINT {scratch}_ck CHECK ({predicate}))"
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


def _check_constraints(bind, table: str):
    """Every CHECK on `table`: `(name, definition, convalidated, comment)`."""
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
        {"table": f"public.{table}"},
    ).fetchall()


# --------------------------------------------------------------------------
# search_path
# --------------------------------------------------------------------------


def _pin_search_path() -> None:
    op.execute("SET LOCAL search_path TO public")


def _assert_is_the_qualified_table(bind, table: str) -> None:
    qualified = _oid(bind, f"public.{table}")
    unqualified = _oid(bind, table)
    if qualified is None or qualified != unqualified:
        raise RuntimeError(
            f"034's {table} is not the table public.{table} resolves to "
            f"(search_path-relative oid {unqualified!r}, qualified oid "
            f"{qualified!r}). Set the migration role's search_path so "
            "`public` comes first, then re-run."
        )


# --------------------------------------------------------------------------
# reconciliation
# --------------------------------------------------------------------------


def _column_problems(bind) -> list[str]:
    problems = []
    for table, column, _ddl, expected, marker in COLUMNS:
        state = _column_state(bind, table, column)
        if state is None:
            continue
        coltype, notnull, default, comment = state
        if coltype != expected:
            problems.append(f"{table}.{column} is {coltype}, not {expected}")
        if notnull:
            problems.append(
                f"{table}.{column} is NOT NULL; 034 creates it nullable, and "
                "NULL is the value that means 'pre-034'"
            )
        if default is not None:
            problems.append(
                f"{table}.{column} has a server default of {default!r}; 034 "
                "creates none, and a default asserts a chunking nobody performed"
            )
        if comment != marker:
            problems.append(f"{table}.{column} does not carry 034's comment marker")
    return problems


def _check_problems(bind, canonical: dict[str, str]) -> list[str]:
    problems = []
    for name, table, _predicate, _cols, marker in CHECKS:
        expected = canonical.get(name)
        checks = _check_constraints(bind, table)
        for c in checks:
            if c.name == name and c.definition != expected:
                problems.append(
                    f"a CHECK named {name} already exists on {table} with "
                    f"definition {c.definition!r}, not {expected!r}"
                )
        ours = [c for c in checks if c.definition == expected]
        if len(ours) > 1:
            problems.append(
                f"{len(ours)} CHECK constraints on {table} carry 034's predicate "
                f"({[c.name for c in ours]}), not one"
            )
        for c in ours:
            if not c.convalidated:
                problems.append(f"the CHECK {c.name} on {table} is NOT VALID")
            if c.comment != marker:
                problems.append(
                    f"the CHECK {c.name} on {table} does not carry 034's "
                    "constraint marker"
                )
    return problems


def _canonical_checks(bind) -> dict[str, str]:
    return {
        name: _canonical_check(bind, name, cols, predicate)
        for name, _table, predicate, cols, _marker in CHECKS
    }


# --------------------------------------------------------------------------
# upgrade / downgrade
# --------------------------------------------------------------------------


def upgrade() -> None:
    bind = op.get_bind()
    # Fail fast rather than queueing behind the index pass's row locks.
    op.execute("SET LOCAL lock_timeout = '10s'")
    op.execute("SET LOCAL statement_timeout = '60s'")
    _pin_search_path()
    for table in TABLES:
        _assert_is_the_qualified_table(bind, table)

    canonical = _canonical_checks(bind)
    refusals = _column_problems(bind) + _check_problems(bind, canonical)
    if refusals:
        raise RuntimeError(
            "034 will not adopt a unit of unknown provenance: "
            + "; ".join(refusals)
            + ". The embed pass reuses a stored vector only when its prefix and "
            "chunking version match, and the vector tools render the locator as "
            "the server's own, so a shape this migration did not create is a "
            "vector or a section of unknown origin. Resolve by hand — drop it and "
            "let 034 create it — then re-run. Nothing has been changed."
        )

    for table, column, ddl, _expected, marker in COLUMNS:
        if _column_state(bind, table, column) is None:
            op.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
            op.execute(f"COMMENT ON COLUMN {table}.{column} IS {_quote(marker)}")

    for name, table, predicate, _cols, marker in CHECKS:
        if any(c.definition == canonical[name] for c in _check_constraints(bind, table)):
            continue
        # NOT VALID first, then VALIDATE: the scan holds only
        # SHARE UPDATE EXCLUSIVE, so the index pass is not blocked by it.
        op.execute(
            f"ALTER TABLE {table} ADD CONSTRAINT {name} CHECK ({predicate}) NOT VALID"
        )
        op.execute(f"ALTER TABLE {table} VALIDATE CONSTRAINT {name}")
        op.execute(f"COMMENT ON CONSTRAINT {name} ON {table} IS {_quote(marker)}")

    op.execute("RESET lock_timeout")
    op.execute("RESET statement_timeout")
    op.execute("RESET search_path")


def _skip_unmarked(what: str, found: str | None, expected: str) -> None:
    print(
        f"034 downgrade: leaving {what} in place. It does not carry 034's "
        f"marker ({expected!r}) — its comment is {found!r} — so 034 did not "
        "create it and will not drop it. Remove it by hand if you mean to.",
        flush=True,
    )


def downgrade() -> None:
    """Delete heading-era vectors, un-certify their notes, then drop only the
    marked constraints and columns (design D5)."""
    bind = op.get_bind()
    # Bounded, like upgrade(): the generation-lock wait below fails after
    # 10 s behind an in-flight pass instead of queueing indefinitely.
    op.execute("SET LOCAL lock_timeout = '10s'")
    op.execute("SET LOCAL statement_timeout = '60s'")
    _pin_search_path()
    for table in TABLES:
        if _oid(bind, f"public.{table}") is None:
            op.execute("RESET lock_timeout")
            op.execute("RESET statement_timeout")
            op.execute("RESET search_path")
            return
        _assert_is_the_qualified_table(bind, table)

    # The generation lock first, before any row lock: an in-flight index or
    # embed pass finishes its transaction, and no pass of either build can
    # certify between the un-certify and the delete below.
    bind.execute(
        sa.text("SELECT pg_advisory_xact_lock(:key)"),
        {"key": INDEX_GENERATION_LOCK_KEY},
    )
    prefix = _column_state(bind, "note_embeddings", "embed_prefix")
    if prefix is not None and prefix[3] == EMBED_PREFIX_MARKER:
        op.execute(
            "UPDATE notes_metadata SET embedded_content_hash = NULL "
            "WHERE id IN (SELECT note_id FROM note_embeddings "
            "WHERE embed_prefix IS NOT NULL)"
        )
        op.execute("DELETE FROM note_embeddings WHERE embed_prefix IS NOT NULL")

    canonical = _canonical_checks(bind)
    for name, table, _predicate, _cols, marker in CHECKS:
        for c in _check_constraints(bind, table):
            if c.definition != canonical[name]:
                continue
            if c.comment != marker:
                _skip_unmarked(f"CHECK {c.name}", c.comment, marker)
                continue
            op.execute(f"ALTER TABLE {table} DROP CONSTRAINT {c.name}")

    for table, column, _ddl, _expected, marker in reversed(COLUMNS):
        state = _column_state(bind, table, column)
        if state is None:
            continue
        if state[3] != marker:
            _skip_unmarked(f"{table}.{column}", state[3], marker)
            continue
        op.execute(f"ALTER TABLE {table} DROP COLUMN {column}")

    op.execute("RESET lock_timeout")
    op.execute("RESET statement_timeout")
    op.execute("RESET search_path")
