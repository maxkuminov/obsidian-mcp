"""Reconcile the database with the ORM models (issue #53).

Two independent drifts, both making `alembic check` fail:

1. **Nullability.** Nine columns the models declare NOT NULL were created
   nullable by their migrations (`api_keys.is_active/created_at`,
   `notes_metadata.indexed_at`, `oauth_clients.created_at`,
   `oauth_codes.used/created_at`, `oauth_tokens.revoked/created_at`,
   `usage_logs.created_at`). This one is a *migration* bug: a freshly migrated
   001->012 database has it too.

2. **The CHECK from 010 is missing on the live database** even though
   `alembic_version` is past 010. 010's committed body creates it and Postgres
   DDL is transactional, so the live database was either stamped past 010 or
   the constraint was dropped afterwards. 013 does not depend on which: it
   verifies and enforces the *complete* 010 shape (nullable
   `client_secret_hash`; `token_endpoint_auth_method` varchar(32) NOT NULL
   default `client_secret_post`; the CHECK, validated, with the exact
   predicate) so the reconciled database is indistinguishable from one that
   ran 010 correctly.

The constraint is a real integrity guard: a public PKCE client
(`token_endpoint_auth_method = 'none'`) must not carry a secret, and a
confidential one must. It is resolved through the catalog, never by name
alone -- a same-named `CHECK (true)` would satisfy a name lookup while
enforcing nothing.

**Rows are never mutated to satisfy the CHECK.** If any `oauth_clients` row
violates the predicate the migration raises, naming the offending
`client_id`s, and the whole transaction rolls back: schema and rows unchanged,
`make deploy` aborts before recreating the container, the old container keeps
serving.

Locks and timeouts (design D3a): `SET NOT NULL` takes ACCESS EXCLUSIVE and
scans the table without rewriting it; `usage_logs` (~10k rows) scans in
milliseconds. `LOCK TABLE oauth_clients IN SHARE ROW EXCLUSIVE MODE` blocks
concurrent DML for the rest of the transaction so the offender check cannot
race an insert. `lock_timeout`/`statement_timeout` bound the whole thing, so a
long-lived transaction elsewhere makes the deploy fail fast instead of
stalling behind a lock.

Revision ID: 013
Revises: 012
Create Date: 2026-08-15
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "013"
down_revision: Union[str, None] = "012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


CONSTRAINT_NAME = "ck_oauth_clients_auth_method_secret"

# Verbatim from 010 / the model's CheckConstraint. Postgres reprints this in
# its own normalized form (casts made explicit, parens added); we never
# compare against *this* string, only feed it to CREATE -- see
# `_canonical_constraintdef`.
CANONICAL_PREDICATE = (
    "(token_endpoint_auth_method = 'none' AND client_secret_hash IS NULL) OR "
    "(token_endpoint_auth_method = 'client_secret_post' AND client_secret_hash IS NOT NULL)"
)

# Written as a COMMENT on the constraint when *this* migration creates it, so
# `downgrade()` can tell "013 added it" from "010 added it and 013 left it
# alone". A fresh 001->013 database must keep 010's constraint on downgrade to
# 012; a database where 013 repaired the drift must lose it again.
MARKER_COMMENT = "created by 013_schema_reconciliation"

# (table, column, backfill expression) for the nine NOT NULL drifts. The
# expressions are the models' own server defaults.
NOT_NULL_COLUMNS = (
    ("api_keys", "is_active", "true"),
    ("api_keys", "created_at", "now()"),
    ("notes_metadata", "indexed_at", "now()"),
    ("oauth_clients", "created_at", "now()"),
    ("oauth_codes", "used", "false"),
    ("oauth_codes", "created_at", "now()"),
    ("oauth_tokens", "revoked", "false"),
    ("oauth_tokens", "created_at", "now()"),
    ("usage_logs", "created_at", "now()"),
)


def _canonical_constraintdef(bind) -> str:
    """What this server prints for the 010 predicate on `oauth_clients`.

    Hand-writing the expected `pg_get_constraintdef` output would pin us to one
    Postgres version's rendering (PG16 prints
    `CHECK (((((token_endpoint_auth_method)::text = 'none'::text) AND ...`),
    and a rendering change in a future major would silently turn every run into
    a drop-and-re-add. Instead we ask the server: build an empty scratch table
    with `oauth_clients`' exact column types, put the constraint on it, and
    read back how it was printed. The table is temporary and empty, so
    validating the constraint is free, and it disappears with the transaction.
    """
    bind.execute(sa.text("CREATE TEMP TABLE _ck_canon (LIKE oauth_clients) ON COMMIT DROP"))
    bind.execute(
        sa.text(
            f"ALTER TABLE _ck_canon ADD CONSTRAINT {CONSTRAINT_NAME} "
            f"CHECK ({CANONICAL_PREDICATE})"
        )
    )
    definition = bind.execute(
        sa.text(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conrelid = '_ck_canon'::regclass AND contype = 'c'"
        )
    ).scalar_one()
    bind.execute(sa.text("DROP TABLE _ck_canon"))
    return _normalize(definition)


def _normalize(definition: str) -> str:
    """Whitespace-insensitive form of a constraint definition."""
    return " ".join(definition.split())


def _column(bind, table: str, column: str):
    """`(attnotnull, formatted_type, default_expr)` or None if absent."""
    return bind.execute(
        sa.text(
            "SELECT a.attnotnull, format_type(a.atttypid, a.atttypmod), "
            "       pg_get_expr(d.adbin, d.adrelid) "
            "FROM pg_attribute a "
            "LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum "
            "WHERE a.attrelid = CAST(:table AS regclass) AND a.attname = :column "
            "  AND a.attnum > 0 AND NOT a.attisdropped"
        ),
        {"table": table, "column": column},
    ).first()


def _reconcile_oauth_clients_shape(bind) -> None:
    """Everything 010 promised about `oauth_clients` other than the CHECK."""
    secret = _column(bind, "oauth_clients", "client_secret_hash")
    if secret is None:  # pragma: no cover - would mean 002 never ran
        raise RuntimeError(
            "oauth_clients.client_secret_hash is missing; the database is not "
            "at the shape migration 010 expects."
        )
    if secret[0]:  # attnotnull -- 010 made it nullable for public clients
        op.execute("ALTER TABLE oauth_clients ALTER COLUMN client_secret_hash DROP NOT NULL")

    method = _column(bind, "oauth_clients", "token_endpoint_auth_method")
    if method is None:
        op.add_column(
            "oauth_clients",
            sa.Column(
                "token_endpoint_auth_method",
                sa.String(32),
                nullable=False,
                server_default="client_secret_post",
            ),
        )
        return

    attnotnull, formatted_type, default_expr = method
    if formatted_type != "character varying(32)":
        op.execute(
            "ALTER TABLE oauth_clients ALTER COLUMN token_endpoint_auth_method "
            "TYPE character varying(32)"
        )
    if default_expr is None or "client_secret_post" not in default_expr:
        op.execute(
            "ALTER TABLE oauth_clients ALTER COLUMN token_endpoint_auth_method "
            "SET DEFAULT 'client_secret_post'"
        )
    if not attnotnull:
        # 010 added the column with this default, so every pre-existing row got
        # it; a NULL here means someone relaxed the column afterwards. Backfill
        # with the declared default rather than inferring intent from the
        # secret -- if that leaves the row inconsistent, the offender check
        # below names it and nothing is written.
        op.execute(
            "UPDATE oauth_clients SET token_endpoint_auth_method = 'client_secret_post' "
            "WHERE token_endpoint_auth_method IS NULL"
        )
        op.execute(
            "ALTER TABLE oauth_clients ALTER COLUMN token_endpoint_auth_method SET NOT NULL"
        )


def _assert_no_violating_rows(bind) -> None:
    # `IS DISTINCT FROM true` rather than `NOT (...)`: a three-valued NULL
    # result (possible only on a drifted, nullable column) is a row the
    # constraint would reject, and `NOT NULL` is NULL, so a plain NOT would
    # skip it and let ADD CONSTRAINT fail with a far less useful message.
    offenders = bind.execute(
        sa.text(
            f"SELECT client_id FROM oauth_clients "
            f"WHERE ({CANONICAL_PREDICATE}) IS DISTINCT FROM true "
            f"ORDER BY client_id"
        )
    ).scalars().all()
    if offenders:
        raise RuntimeError(
            f"{len(offenders)} oauth_clients row(s) violate {CONSTRAINT_NAME}: "
            + ", ".join(repr(c) for c in offenders)
            + ". A public client (token_endpoint_auth_method='none') must have "
            "no client_secret_hash and a confidential one must have it. "
            "Migration 013 will not delete or rewrite these rows -- fix them by "
            "hand, then re-run. Nothing has been changed."
        )


def _reconcile_check_constraint(bind) -> None:
    canonical = _canonical_constraintdef(bind)
    existing = bind.execute(
        sa.text(
            "SELECT convalidated, pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conrelid = 'oauth_clients'::regclass AND contype = 'c' "
            "  AND conname = :name"
        ),
        {"name": CONSTRAINT_NAME},
    ).first()

    if existing is not None:
        validated, definition = existing
        if validated and _normalize(definition) == canonical:
            # 010 (or a previous 013) already did it. Leave the comment alone:
            # its presence or absence is what downgrade() reads.
            return
        # Same name, different meaning (e.g. `CHECK (true)`) or NOT VALID.
        # Data was verified above, so replacing it in this transaction is safe.
        op.execute(f"ALTER TABLE oauth_clients DROP CONSTRAINT {CONSTRAINT_NAME}")

    op.create_check_constraint(CONSTRAINT_NAME, "oauth_clients", CANONICAL_PREDICATE)
    # COMMENT ON takes a string *literal*, not a bind parameter, so the marker
    # is escaped and inlined. It is a module constant we control, but escaping
    # it keeps the next person from inlining something they don't.
    marker = MARKER_COMMENT.replace("'", "''")
    op.execute(
        f"COMMENT ON CONSTRAINT {CONSTRAINT_NAME} ON oauth_clients IS '{marker}'"
    )


def upgrade() -> None:
    bind = op.get_bind()

    # Fail fast instead of queueing behind a long-lived transaction: the deploy
    # migrates before recreating the container, so a stalled migration is a
    # stalled deploy while the old container is still serving.
    op.execute("SET LOCAL lock_timeout = '10s'")
    op.execute("SET LOCAL statement_timeout = '60s'")

    # Held for the rest of the transaction: blocks INSERT/UPDATE/DELETE (but
    # not SELECT) on oauth_clients, so no row can slip in between the offender
    # check and ADD CONSTRAINT.
    op.execute("LOCK TABLE oauth_clients IN SHARE ROW EXCLUSIVE MODE")

    _reconcile_oauth_clients_shape(bind)
    _assert_no_violating_rows(bind)
    _reconcile_check_constraint(bind)

    for table, column, default_expr in NOT_NULL_COLUMNS:
        op.execute(
            f"UPDATE {table} SET {column} = {default_expr} WHERE {column} IS NULL"
        )
        # Idempotent in Postgres: SET NOT NULL on an already-NOT NULL column is
        # accepted and does nothing.
        op.execute(f"ALTER TABLE {table} ALTER COLUMN {column} SET NOT NULL")


def downgrade() -> None:
    """Drop the CHECK only if *this* migration created it.

    The NOT NULL columns stay: relaxing them would re-create exactly the drift
    this migration exists to remove, and every one of them is NOT NULL in the
    models, so a downgraded database would immediately fail `alembic check`
    again. Downgrading past 013 is a rollback of the constraint, not of the
    nullability fix.
    """
    bind = op.get_bind()
    comment = bind.execute(
        sa.text(
            "SELECT obj_description(oid, 'pg_constraint') FROM pg_constraint "
            "WHERE conrelid = 'oauth_clients'::regclass AND contype = 'c' "
            "  AND conname = :name"
        ),
        {"name": CONSTRAINT_NAME},
    ).scalar_one_or_none()

    if comment == MARKER_COMMENT:
        op.execute(f"ALTER TABLE oauth_clients DROP CONSTRAINT {CONSTRAINT_NAME}")
