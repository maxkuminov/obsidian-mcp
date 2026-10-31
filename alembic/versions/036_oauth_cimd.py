"""Client ID Metadata Document clients: `oauth_clients.client_id_metadata_url` (#357).

A CIMD client (MCP 2026-07-28, `draft-ietf-oauth-client-id-metadata-document`)
names itself by an `https` URL instead of registering through `/register`. The
authorization server persists a **synthetic** `oauth_clients` row for it, one
per (URL, user), so grant families, #325 lineage, `grant_issued_at`,
`last_used_at`, usage attribution, the panel's per-user listing and #68's
cross-user rule keep their meaning unchanged.

## Why a column, and why `client_id` is not widened

The row's `client_id` is `cimd_` + 59 hex characters of an HMAC over the URL
and the owner (64 characters), so it fits every existing `VARCHAR(64)`
(`oauth_codes.client_id`, `oauth_tokens.client_id`, `usage_logs.actor_ref`,
`transfer_tokens.actor_ref`) and the 64-character log bound. The URL itself has
to be stored somewhere: `/token` and `/revoke` match the caller's `client_id`
(the URL) against it, and the panel shows it. No existing column can hold it
without overloading `client_name` (self-asserted text) or `redirect_uris`.

Storing the URL as `client_id` instead would have widened five columns and
made one row per URL shared by every user, which turns #68's first-authorizer
binding into a refusal for the second user who connects the same client.

## The shape CHECK

`ck_oauth_clients_cimd_shape`: a row carries a metadata URL **exactly when**
its id begins `cimd_`, and such a row is a public client (`none`, no secret
hash). DCR ids are `secrets.token_hex(16)` and can never start with `cimd_`.

## Re-runnable, by reconciliation (025's rule)

The schema gate stamps back and re-runs, so this body may meet its own column
and constraint. It adopts them only when they are demonstrably its own — the
column `character varying(2048)`, nullable, no default, carrying the comment
marker mirrored in `src/models/db.py`; the constraint with exactly the
expected definition — and otherwise refuses, naming what disagreed. No backfill:
every existing row is a DCR registration and already satisfies the CHECK.

## Downgrade

Refuses while any CIMD row exists, naming the count: dropping the column would
leave rows whose id no URL can reach, and deleting them would cascade their
grants away silently. Delete those clients in the panel first.

Revision ID: 036
Revises: 035
Create Date: 2026-10-31
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "036"
down_revision: Union[str, None] = "035"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = "oauth_clients"
COLUMN = "client_id_metadata_url"
EXPECTED_TYPE = "character varying(2048)"
#: Byte-identical to `_OAUTH_CLIENT_CIMD_URL_COLUMN_MARKER` in src/models/db.py.
COLUMN_MARKER = "created by 036_oauth_cimd: Client ID Metadata Document URL (#357)"
CHECK = "ck_oauth_clients_cimd_shape"
CHECK_SQL = (
    "((client_id LIKE 'cimd\\_%') = (client_id_metadata_url IS NOT NULL)) AND "
    "(client_id_metadata_url IS NULL OR "
    "(token_endpoint_auth_method = 'none' AND client_secret_hash IS NULL))"
)


def _quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _column_state(bind):
    return bind.execute(
        sa.text(
            "SELECT format_type(a.atttypid, a.atttypmod), a.attnotnull, "
            "       pg_get_expr(d.adbin, d.adrelid), "
            "       col_description(a.attrelid, a.attnum) "
            "FROM pg_attribute a "
            "LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum "
            "WHERE a.attrelid = CAST('public.oauth_clients' AS regclass) "
            "  AND a.attname = :column AND a.attnum > 0 AND NOT a.attisdropped"
        ),
        {"column": COLUMN},
    ).first()


def _constraint_def(bind):
    return bind.execute(
        sa.text(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conrelid = CAST('public.oauth_clients' AS regclass) "
            "  AND conname = :name AND contype = 'c'"
        ),
        {"name": CHECK},
    ).scalar()


def upgrade() -> None:
    bind = op.get_bind()
    op.execute("SET LOCAL search_path TO public")
    op.execute("SET LOCAL lock_timeout = '10s'")

    state = _column_state(bind)
    if state is None:
        op.add_column(TABLE, sa.Column(COLUMN, sa.String(2048), nullable=True))
        op.execute(f"COMMENT ON COLUMN {TABLE}.{COLUMN} IS {_quote(COLUMN_MARKER)}")
    else:
        coltype, notnull, default, comment = state
        problems = []
        if coltype != EXPECTED_TYPE:
            problems.append(f"it is {coltype}, not {EXPECTED_TYPE}")
        if notnull:
            problems.append("it is NOT NULL")
        if default is not None:
            problems.append(f"it has a server default of {default!r}")
        if comment != COLUMN_MARKER:
            problems.append("it does not carry 036's comment marker")
        if problems:
            raise RuntimeError(
                f"{TABLE}.{COLUMN} already exists but {'; '.join(problems)}. 036 "
                "will not adopt a column of unknown provenance. Resolve by hand "
                "(drop it and let 036 create it), then re-run. Nothing has been "
                "changed."
            )

    existing = _constraint_def(bind)
    if existing is None:
        op.create_check_constraint(CHECK, TABLE, CHECK_SQL)
    else:
        # Compare against what PostgreSQL prints for our own definition, by
        # creating it under a scratch name in this transaction and reading it.
        op.execute(
            f"ALTER TABLE {TABLE} ADD CONSTRAINT ck_036_probe CHECK ({CHECK_SQL}) NOT VALID"
        )
        probe = bind.execute(
            sa.text(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conrelid = CAST('public.oauth_clients' AS regclass) "
                "  AND conname = 'ck_036_probe'"
            )
        ).scalar()
        op.execute(f"ALTER TABLE {TABLE} DROP CONSTRAINT ck_036_probe")
        if existing != probe.replace(" NOT VALID", ""):
            raise RuntimeError(
                f"{CHECK} already exists with a different definition ({existing!r}). "
                "036 will not adopt it. Drop it and re-run. Nothing has been changed."
            )

    op.execute("RESET lock_timeout")
    op.execute("RESET search_path")


def downgrade() -> None:
    bind = op.get_bind()
    count = bind.execute(
        sa.text(f"SELECT count(*) FROM {TABLE} WHERE {COLUMN} IS NOT NULL")
    ).scalar_one()
    if count:
        raise RuntimeError(
            f"036 downgrade refused: {count} Client ID Metadata Document client "
            f"row(s) exist in {TABLE}. Delete those clients in the control panel "
            "(their grants go with them) and run the downgrade again."
        )
    op.drop_constraint(CHECK, TABLE, type_="check")
    op.drop_column(TABLE, COLUMN)
