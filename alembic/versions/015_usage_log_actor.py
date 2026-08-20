"""Denormalise the actor label onto `usage_logs` (issue #77).

`/admin/usage` resolved the actor of every log line by LEFT JOIN — through
`api_keys` for a key, through `oauth_tokens` -> `oauth_clients` for an OAuth
grant. Both of those joins are allowed to go NULL while the log row stays, and
both of them do so on the operator's own most urgent path:

- `usage_logs.oauth_token_id` is `ON DELETE SET NULL`, and
  `oauth_tokens.client_id` is `ON DELETE CASCADE`. So the panel's one-click
  "Delete this client" — labelled as a *revocation* — cascaded the client's
  tokens and unattributed every historical line that client had produced. An
  operator who suspects a connector misbehaved, stops it, and then opens
  `/admin/usage` to review what it did is shown "unknown" for every row: the
  evidence they opened the page to read, destroyed by the button they pressed
  to stop the client.
- `usage_logs.key_id` has no `ON DELETE` at all, so `delete_key_form` and
  `delete_all_revoked` explicitly `UPDATE usage_logs SET key_id = NULL` before
  deleting the key. Same outcome, pre-existing, and by the same mechanism.

The fix is to stop deriving a historical fact from a live row. Three nullable
columns record who the caller *was* at the moment of the call:

- `actor_kind`  — 'api_key' | 'oauth'
- `actor_label` — the key's `name`, or the OAuth client's `client_name`
- `actor_ref`   — the key's `omcp_` prefix, or the `client_id`

Name and identifier stay separate rather than being concatenated into one
label: joined, the row stops being a record, since a key named "audit (prod)"
cannot be recovered from "audit (prod) (omcp_a1b2c3)".

New rows get these from `APIKeyMiddleware`, which binds them from the
credential row it has already loaded (`src/auth/session.py::current_actor`), so
attribution costs no extra query and cannot be taken away by a later delete.
This migration does the same thing for the rows that already exist.

## The backfill, and what it deliberately does not do

Every log row whose credential still resolves is labelled from that credential,
exactly as the panel's join would have rendered it today. Rows whose credential
is *already* gone — the ones this bug has claimed — have nothing to recover
from and stay NULL; the panel renders them "unknown (credential deleted)",
which is the honest answer and a materially better one than "unknown".

**Nothing is invented.** A row's label comes from the credential its own FK
points at, or it stays NULL. There is no guess-by-user_id fallback: two of a
user's keys are different actors, and labelling a row with the wrong one would
be worse than admitting the label is lost — the whole point of the column is
that an operator can trust it when deciding whether a connector misbehaved.

**Nothing is re-stamped.** Both statements are guarded on
`actor_kind IS NULL`, so a re-run (the schema gate does `alembic stamp 014`
then `upgrade head`) leaves every existing label alone. That matters beyond
idempotence: a row's label is a snapshot of the credential *at call time*, and
re-deriving it from the credential's current state would silently rewrite
history whenever a key is renamed.

**Nothing becomes NOT NULL.** Additive and nullable, on purpose — a log write
that cannot name its actor must still record that the call happened. The
columns are display and audit only; nothing authorizes against them.

## Locks

One `ALTER TABLE ... ADD COLUMN`, which takes ACCESS EXCLUSIVE on `usage_logs`
and holds it to COMMIT, plus two `UPDATE ... FROM` statements that read
`api_keys` / `oauth_tokens` / `oauth_clients` (ACCESS SHARE). `usage_logs` is a
child of all three, so the order is child-then-parent — the same direction 013
established and the same direction the app itself takes, and therefore not a
new way to close a wait cycle.

`lock_timeout` / `statement_timeout` make a blocked or slow migration fail fast
instead of stalling the deploy, and both are `RESET` at the end because alembic
runs every pending revision in one transaction and `SET LOCAL` would otherwise
leak into a later revision. The backfill scans `usage_logs` once per statement;
on a table large enough to exceed the 60 s budget the migration aborts, the
whole transaction rolls back, and the deploy stops before the container is
recreated — re-run it when quiet, or raise the timeout by hand.

Revision ID: 015
Revises: 014
Create Date: 2026-08-20
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "015"
down_revision: Union[str, None] = "014"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


TABLE = "usage_logs"

# (name, SQLAlchemy type, how PostgreSQL prints it). The printed form is what a
# pre-existing column is checked against — see `_verify_or_add`.
COLUMNS = (
    ("actor_kind", sa.String(20), "character varying(20)"),
    ("actor_label", sa.String(255), "character varying(255)"),
    ("actor_ref", sa.String(64), "character varying(64)"),
)


def _column_type(bind, column: str) -> str | None:
    """How PostgreSQL prints `usage_logs.<column>`, or None if absent."""
    return bind.execute(
        sa.text(
            "SELECT format_type(a.atttypid, a.atttypmod) FROM pg_attribute a "
            "WHERE a.attrelid = CAST(:table AS regclass) AND a.attname = :column "
            "  AND a.attnum > 0 AND NOT a.attisdropped"
        ),
        {"table": TABLE, "column": column},
    ).scalar_one_or_none()


def _verify_or_add(bind, column: str, type_: sa.types.TypeEngine, expected: str) -> None:
    """Create the column, or verify a pre-existing one is the shape we mean.

    Same philosophy as 013 and 014: reconcile a database that has our shape,
    refuse to guess for one that does not. A `text` or `varchar(8)` column
    under one of these names holds values this migration did not write, and
    adopting it would mean trusting an attribution of unknown provenance —
    which is precisely the trust these columns exist to make possible.
    """
    actual = _column_type(bind, column)
    if actual is None:
        op.add_column(TABLE, sa.Column(column, type_, nullable=True))
        return
    if actual != expected:
        raise RuntimeError(
            f"{TABLE}.{column} already exists as {actual}, not {expected}. "
            "015 reconciles a column it created; it will not adopt one of a "
            "different shape, because the labels in it were written by "
            "something else. Inspect it by hand, then re-run. Nothing has "
            "been changed."
        )


def upgrade() -> None:
    bind = op.get_bind()

    # Fail fast rather than queueing behind a long-lived transaction: the
    # deploy migrates before recreating the container, so a stalled migration
    # is a stalled deploy while the old container is still serving. Per
    # statement and per lock acquisition, not a budget for the transaction.
    op.execute("SET LOCAL lock_timeout = '10s'")
    op.execute("SET LOCAL statement_timeout = '60s'")

    for column, type_, expected in COLUMNS:
        _verify_or_add(bind, column, type_, expected)

    # API-key rows. `actor_kind IS NULL` is what makes this a one-time stamp
    # rather than a re-derivation: a label is a snapshot of the credential at
    # call time, and rewriting it from the credential's present state would
    # quietly rewrite history every time a key is renamed.
    op.execute(
        """
        UPDATE usage_logs AS ul
        SET actor_kind  = 'api_key',
            actor_label = ak.name,
            actor_ref   = ak.key_prefix
        FROM api_keys AS ak
        WHERE ul.key_id = ak.id
          AND ul.actor_kind IS NULL
        """
    )

    # OAuth rows. The join is the same one `/admin/usage` performs, so the
    # backfilled label is exactly what the page renders today for a row whose
    # credential still resolves — this migration changes what survives, not
    # what is displayed. An inner join throughout: a token whose client row is
    # gone (impossible today, `client_id` is a FK) would leave the row NULL
    # rather than half-labelled.
    op.execute(
        """
        UPDATE usage_logs AS ul
        SET actor_kind  = 'oauth',
            actor_label = oc.client_name,
            actor_ref   = oc.client_id
        FROM oauth_tokens AS ot
        JOIN oauth_clients AS oc ON oc.client_id = ot.client_id
        WHERE ul.oauth_token_id = ot.id
          AND ul.actor_kind IS NULL
        """
    )

    # `SET LOCAL` is scoped to the transaction and alembic runs every pending
    # revision in *one*, so without this the next revision would silently
    # inherit these timeouts and blame its own SQL when it tripped them.
    op.execute("RESET lock_timeout")
    op.execute("RESET statement_timeout")


def downgrade() -> None:
    """Drop the denormalised attribution.

    Nothing to preserve and no ownership marker needed: the columns did not
    exist before 015, so no pre-015 database can be carrying them. Downgrading
    does destroy attribution for every row whose credential has since been
    deleted — that history is not recoverable by re-upgrading, because the
    backfill can only read credentials that still exist.
    """
    for column, _type, _expected in COLUMNS:
        op.drop_column(TABLE, column)
