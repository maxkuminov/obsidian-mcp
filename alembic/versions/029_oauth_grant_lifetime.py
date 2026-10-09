"""OAuth code lineage and grant issuance time (#325, #326).

Two columns, owned as one marked unit.

## `oauth_codes.grant_id` — the lineage a replayed code revokes (#325)

RFC 6749 §4.1.2: when an authorization code is presented twice, the server
should revoke what the first exchange issued. Before this revision nothing on
the code row named that family, so a replay could only be refused. The token
endpoint now writes the `grant_id` it mints onto the code row, in the same
transaction that marks the code used. Nullable, no default, no index (codes are
only ever looked up by the unique `code_hash`), no FK (there is no grants
table). **No backfill** — a code spent before 029 has no recoverable lineage,
and NULL is exactly what says so (the replay branch refuses and revokes
nothing on it, L3).

## `oauth_tokens.grant_issued_at` — the clock the absolute lifetime runs on (#326)

The deadline of a grant family is `grant_issued_at + OAUTH_GRANT_ABSOLUTE_
LIFETIME_DAYS`, derived at use from the current setting. The value is set once
at the code exchange and copied verbatim by every rotation — the same
inheritance rule `grant_id` follows — so a family is uniform by construction.

**Backfill: the migration transaction's timestamp, one value for every
pre-existing row.** Owner decision: no connector is logged out by the deploy;
every existing family reaches its deadline the configured lifetime after the
migration (L2). One value for all rows keeps every family uniform. A value
already present is never overwritten, so the stamp-back re-run the schema gate
performs changes nothing.

**Why a server default, when 025 refused one.** During a rolling deploy the
previous image keeps serving on the migrated schema and inserts token rows
without this column. Without a default that is a NOT NULL violation — a 500 on
every token exchange for the length of the rollout. With one, an old-image row
gets its insert time (bounded, L9). The opposite risk — a future mint site
that forgets to copy the value silently restarts a family's clock, which is
#326 again — is closed by an AST test requiring every `OAuthToken(`
construction under `src/` to pass `grant_issued_at=` explicitly. Application
code must never rely on the default.

## Reconciliation

013's rule, 025's shape: reconcile a column that demonstrably has our shape
and carries our marker, refuse to guess for any other, and **name what
disagreed**. Both columns are checked before anything is written, so a refusal
on the second changes nothing about the first. Each column carries a
`COMMENT` marker mirrored byte-identically in `src/models/db.py`, so
`alembic check` compares it, and `downgrade()` drops only marked columns.

## Locks and order

`oauth_codes` first, then `oauth_tokens` — the application's own direction
(the code row is locked, then tokens inserted), so a concurrent exchange
queues behind the migration rather than closing a wait cycle. `lock_timeout`
/ `statement_timeout` make a blocked migration fail fast; `search_path` is
pinned to `public` and the qualified identity asserted; all three are `RESET`
because alembic runs every pending revision in one transaction.

Revision ID: 029
Revises: 028
Create Date: 2026-10-09
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "029"
down_revision: Union[str, None] = "028"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Must stay byte identical to `_OAUTH_CODE_GRANT_ID_COLUMN_MARKER` and
# `_OAUTH_GRANT_ISSUED_AT_COLUMN_MARKER` in `src/models/db.py`, where they are
# declared as the column comments so `alembic check` compares them.
CODE_GRANT_MARKER = (
    "grant family the exchange of this code issued (029_oauth_grant_lifetime)"
)
ISSUED_AT_MARKER = (
    "grant family issuance time, inherited by rotation (029_oauth_grant_lifetime)"
)

# (table, column, expected format_type, expect NOT NULL, expected default
#  expression as pg_get_expr prints it, marker). `oauth_codes` first.
COLUMNS = (
    ("oauth_codes", "grant_id", "character varying(64)", False, None, CODE_GRANT_MARKER),
    (
        "oauth_tokens",
        "grant_issued_at",
        "timestamp with time zone",
        True,
        "now()",
        ISSUED_AT_MARKER,
    ),
)


def _quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _oid(bind, name: str):
    return bind.execute(
        sa.text("SELECT CAST(to_regclass(:name) AS oid)"), {"name": name}
    ).scalar()


def _column_state(bind, table: str, column: str):
    """`(format_type, attnotnull, default_expr, comment)` or None when absent."""
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


def _pin_search_path() -> None:
    """021's device: every earlier revision `RESET`s its own pin, so 029 cannot
    rely on one still being in force."""
    op.execute("SET LOCAL search_path TO public")


def _assert_is_the_qualified_table(bind, table: str) -> None:
    qualified = _oid(bind, f"public.{table}")
    unqualified = _oid(bind, table)
    if qualified is None or qualified != unqualified:
        raise RuntimeError(
            f"029's {table} is not the table public.{table} resolves to "
            f"(search_path-relative oid {unqualified!r}, qualified oid "
            f"{qualified!r}). The token endpoint reads the unqualified name, so "
            "a column added elsewhere on the path would be written in one place "
            "and read in another. Set the migration role's search_path so "
            "`public` comes first, then re-run."
        )


def _problems(state, coltype, notnull, default, marker) -> list[str]:
    found_type, found_notnull, found_default, found_comment = state
    problems = []
    if found_type != coltype:
        problems.append(f"it is {found_type}, not {coltype}")
    if bool(found_notnull) != notnull:
        problems.append(
            "it is NOT NULL; 029 creates it nullable — NULL is what says a code "
            "has no recorded lineage"
            if found_notnull
            else "it is nullable; 029 creates it NOT NULL — a family with no "
            "issuance time has no absolute deadline, which is #326"
        )
    if found_default != default:
        if default is None:
            problems.append(
                f"it has a server default of {found_default!r}; 029 creates none"
            )
        elif found_default is None:
            problems.append(
                f"it has no server default; 029 creates {default!r}, without which "
                "a process on the previous image cannot insert a token during a "
                "rolling deploy"
            )
        else:
            problems.append(
                f"its server default is {found_default!r}, not {default!r}"
            )
    if found_comment != marker:
        problems.append("it does not carry 029's comment marker")
    return problems


def _check_existing(bind) -> list[str]:
    """Every disagreement across both columns, before anything is written."""
    refusals = []
    for table, column, coltype, notnull, default, marker in COLUMNS:
        state = _column_state(bind, table, column)
        if state is None:
            continue
        problems = _problems(state, coltype, notnull, default, marker)
        if problems:
            refusals.append(f"{table}.{column} already exists but {'; '.join(problems)}")
    return refusals


def _add_code_grant_id(bind) -> None:
    if _column_state(bind, "oauth_codes", "grant_id") is not None:
        return
    op.add_column("oauth_codes", sa.Column("grant_id", sa.String(64), nullable=True))
    op.execute(f"COMMENT ON COLUMN oauth_codes.grant_id IS {_quote(CODE_GRANT_MARKER)}")


def _add_grant_issued_at(bind) -> None:
    if _column_state(bind, "oauth_tokens", "grant_issued_at") is not None:
        # A marked column of the exact shape: NOT NULL, so there is nothing to
        # backfill and nothing to overwrite. The stamp-back re-run lands here.
        return
    # Added nullable, stamped, then defaulted and constrained — explicit rather
    # than relying on ADD COLUMN ... DEFAULT now()'s fast-default evaluation, so
    # the backfill reads as what it is: one transaction timestamp for every row,
    # never overwriting a value that is already there.
    op.add_column(
        "oauth_tokens",
        sa.Column("grant_issued_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute(
        "UPDATE oauth_tokens SET grant_issued_at = now() WHERE grant_issued_at IS NULL"
    )
    op.execute("ALTER TABLE oauth_tokens ALTER COLUMN grant_issued_at SET DEFAULT now()")
    op.execute("ALTER TABLE oauth_tokens ALTER COLUMN grant_issued_at SET NOT NULL")
    op.execute(
        f"COMMENT ON COLUMN oauth_tokens.grant_issued_at IS {_quote(ISSUED_AT_MARKER)}"
    )


def upgrade() -> None:
    bind = op.get_bind()
    op.execute("SET LOCAL lock_timeout = '10s'")
    op.execute("SET LOCAL statement_timeout = '60s'")
    _pin_search_path()

    _assert_is_the_qualified_table(bind, "oauth_codes")
    _assert_is_the_qualified_table(bind, "oauth_tokens")

    refusals = _check_existing(bind)
    if refusals:
        raise RuntimeError(
            ". ".join(refusals) + ". 029 will not adopt a column of unknown "
            "provenance: the token endpoint revokes a grant family on the "
            "strength of oauth_codes.grant_id and ends one on the strength of "
            "oauth_tokens.grant_issued_at, so a shape this migration did not "
            "create is either a replay that revokes the wrong thing or a grant "
            "with no deadline. Resolve by hand — drop it and let 029 create it, "
            "or make it match — then re-run. Nothing has been changed."
        )

    _add_code_grant_id(bind)
    _add_grant_issued_at(bind)

    op.execute("RESET lock_timeout")
    op.execute("RESET statement_timeout")
    op.execute("RESET search_path")


def downgrade() -> None:
    """Drop both columns, but only if each carries 029's marker.

    Both are checked before either is dropped, so an unmarked one leaves the
    database exactly as it was. Dropping them is safe in the direction that
    matters: the previous build neither reads nor writes either column. A
    re-upgrade restarts every family's clock at the re-upgrade (the backfill),
    and spent codes lose their lineage (their replay then revokes nothing).
    """
    bind = op.get_bind()
    _pin_search_path()
    present = []
    for table, column, _type, _nn, _default, marker in COLUMNS:
        if _oid(bind, f"public.{table}") is None:
            continue
        _assert_is_the_qualified_table(bind, table)
        state = _column_state(bind, table, column)
        if state is None:
            continue
        if state[3] != marker:
            raise RuntimeError(
                f"{table}.{column} does not carry 029's comment marker "
                f"({marker!r}), so 029 did not create it and will not drop it. "
                "Nothing has been changed. Remove it by hand if you mean to."
            )
        present.append((table, column))
    # Reverse of creation order: tokens, then codes.
    for table, column in reversed(present):
        op.drop_column(table, column)
    op.execute("RESET search_path")
