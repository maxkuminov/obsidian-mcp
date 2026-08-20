"""Give every OAuth token the grant family it was minted from (issue #64).

`oauth_tokens` had `client_id` and `user_id` but nothing tying an access token
to the refresh token minted beside it. So the panel could only offer per-row
controls, and both of them were near no-ops:

- **Revoke** flipped one row. The sibling refresh token was untouched, and the
  client's ordinary 401-then-refresh cycle minted a fresh, identically-scoped
  access token within the hour. The operator saw the row disappear and read
  that as success.
- **Downgrade** wrote one row's scope. `_handle_refresh` copies the *refresh*
  token's scope, so a downgrade applied to the access row silently restored
  itself on the next rotation -- write access the owner believed they had
  removed.

`grant_id` is the missing identifier. One value per consent event: both tokens
minted from a single `/authorize` approval share it, and every pair produced by
later rotation inherits it. Revocation and downgrade then act on the family.

## Backfill

Existing rows predate the column, so one `grant_id` is assigned per distinct
`(client_id, user_id)` group. That is imperfect exactly where a client genuinely
backed two concurrent sessions for the same user -- those collapse into one
family and are revoked together -- but it is strictly better than today's
per-row behaviour, it fails *closed* (over-revoking, never under-revoking), and
it converges: every grant issued after this migration is exact.

`user_id IS NOT DISTINCT FROM` rather than `=`: single-user-mode tokens carry
`user_id IS NULL`, and `NULL = NULL` is NULL, so an `=` join would leave every
single-user token unmatched and the `SET NOT NULL` below would fail. `GROUP BY`
already treats NULLs as one group, which is the semantic we want on both sides.

Revoked rows are backfilled too, and deliberately: the column is NOT NULL, and
the decision in #64 was explicit that leaving `grant_id` nullable with a
fallback "find the family" path is how this bug comes back. Two code paths for
resolving a family is the defect, not a safety margin. Grouping revoked rows in
with the live ones only affects *display* -- family writes touch non-revoked
rows only -- and it is what makes the panel able to show a grant's revocation
history at all.

## What this migration must never do

**Invent a grant.** Rows that already carry a `grant_id` are not re-stamped:
the backfill's group source and its target are both restricted to
`grant_id IS NULL`. Re-running the migration (the schema gate does exactly
that, via `alembic stamp 013` then `upgrade head`) therefore cannot split a
family in two by handing its rows fresh identifiers.

**Merge two users.** The group key is `(client_id, user_id)`, so a family never
spans users. That is the invariant every family operation in
`src/oauth/grants.py` leans on -- it is why those operations do not re-filter
by `user_id`, which would give incomplete revocation a way back in.

**Leave a NULL behind.** The `ADD COLUMN` takes ACCESS EXCLUSIVE on
`oauth_tokens` and holds it for the whole transaction, so no row can be
inserted between the backfill and `SET NOT NULL`. The explicit post-check is
belt-and-braces: it raises with a count rather than letting `SET NOT NULL`
report a generic constraint failure.

Lock footprint is one table, and a child one at that (`oauth_tokens` references
both `oauth_clients` and `users`), so this migration cannot close a wait cycle
with 013's parent-then-child order or with the app's own
`oauth_codes -> oauth_clients -> oauth_tokens` direction. `lock_timeout` /
`statement_timeout` make a blocked migration fail fast instead of stalling the
deploy, and both are RESET at the end because alembic runs every pending
revision in one transaction and `SET LOCAL` would otherwise leak into 015.

Revision ID: 014
Revises: 013
Create Date: 2026-08-20
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "014"
down_revision: Union[str, None] = "013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


TABLE = "oauth_tokens"
COLUMN = "grant_id"
INDEX = "ix_oauth_tokens_grant_id"

# Matches `OAuthToken.grant_id` (String(64)). The application mints
# `secrets.token_urlsafe(24)` (32 chars); the backfill writes a uuid (36).
COLUMN_TYPE = sa.String(64)


def _column_exists(bind) -> bool:
    return bind.execute(
        sa.text(
            "SELECT 1 FROM pg_attribute "
            "WHERE attrelid = CAST(:table AS regclass) AND attname = :column "
            "  AND attnum > 0 AND NOT attisdropped"
        ),
        {"table": TABLE, "column": COLUMN},
    ).scalar_one_or_none() is not None


def upgrade() -> None:
    bind = op.get_bind()

    # Fail fast instead of queueing behind a long-lived transaction: the deploy
    # migrates before recreating the container, so a stalled migration is a
    # stalled deploy while the old container is still serving. Per statement and
    # per lock acquisition, not a budget for the whole transaction.
    op.execute("SET LOCAL lock_timeout = '10s'")
    op.execute("SET LOCAL statement_timeout = '60s'")

    if not _column_exists(bind):
        # Nullable for the length of this transaction only. ADD COLUMN takes
        # ACCESS EXCLUSIVE and holds it to COMMIT, so the nullable window is
        # never observable by another session.
        op.add_column(TABLE, sa.Column(COLUMN, COLUMN_TYPE, nullable=True))

    # One id per (client_id, user_id) over the rows that do not have one yet.
    #
    # MATERIALIZED is load-bearing: `gen_random_uuid()` is volatile, and an
    # inlined subquery could be re-evaluated per outer row, which would hand
    # every token its own private "family" -- the exact opposite of the point.
    # Materialising the CTE forces one evaluation per group.
    #
    # Both the group source and the update target are restricted to
    # `grant_id IS NULL`, so a row that already belongs to a family keeps it.
    # That is what makes a re-run (stamp 013 -> upgrade head) a no-op rather
    # than a re-partitioning of live grants.
    op.execute(
        """
        WITH families AS MATERIALIZED (
            SELECT client_id,
                   user_id,
                   gen_random_uuid()::text AS grant_id
            FROM oauth_tokens
            WHERE grant_id IS NULL
            GROUP BY client_id, user_id
        )
        UPDATE oauth_tokens AS t
        SET grant_id = f.grant_id
        FROM families AS f
        WHERE t.grant_id IS NULL
          AND t.client_id = f.client_id
          AND t.user_id IS NOT DISTINCT FROM f.user_id
        """
    )

    remaining = bind.execute(
        sa.text(f"SELECT count(*) FROM {TABLE} WHERE {COLUMN} IS NULL")
    ).scalar_one()
    if remaining:
        raise RuntimeError(
            f"{remaining} {TABLE} row(s) still have a NULL {COLUMN} after the "
            "backfill, so the column cannot be made NOT NULL. Nothing has been "
            "changed -- the whole migration rolls back."
        )

    # Idempotent in Postgres: SET NOT NULL on an already-NOT NULL column is
    # accepted and does nothing.
    op.execute(f"ALTER TABLE {TABLE} ALTER COLUMN {COLUMN} SET NOT NULL")

    # The name matters: `OAuthToken.grant_id` declares `index=True`, so
    # autogenerate expects exactly SQLAlchemy's default `ix_<table>_<column>`.
    # A differently-named index would leave `alembic check` dirty forever.
    op.execute(f"CREATE INDEX IF NOT EXISTS {INDEX} ON {TABLE} ({COLUMN})")

    # `SET LOCAL` is scoped to the transaction and alembic runs every pending
    # revision in *one* -- without this a future 015 would silently inherit
    # these timeouts and blame its own SQL when a long index build trips them.
    op.execute("RESET lock_timeout")
    op.execute("RESET statement_timeout")


def downgrade() -> None:
    """Drop the family identifier.

    Unlike 013's nullability fix there is nothing to preserve: the column did
    not exist before 014, so no pre-014 database can be missing it and no
    ownership marker is needed. Downgrading does destroy the grouping -- a
    subsequent re-upgrade re-derives it from `(client_id, user_id)`, which is
    the same approximation the original backfill made.
    """
    op.execute(f"DROP INDEX IF EXISTS {INDEX}")
    op.drop_column(TABLE, COLUMN)
