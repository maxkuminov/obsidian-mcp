"""`oauth_clients.last_used_at`: the marker the expiry sweep reads (#194).

`/register` is unauthenticated RFC 7591 dynamic client registration and
nothing bounds `oauth_clients` — the maintenance pass deletes codes, tokens
and panel sessions and has never touched a client row. Bounding it needs one
fact the schema could not previously state: **has this registration ever been
used?**

## Why a column, and not a query over what is already there

Three candidate signals exist and none of them is sound.

* **`user_id IS NULL`** looks perfect — `/authorize` binds a client to its
  first authorizing user and never rebinds. But in **single-user mode** the
  session user is `None`, so the column stays NULL forever and *every* client
  in the deployment `DEPLOYMENT.md` walks a new operator through would read as
  unused.
* **Absence of child rows.** A **used** `oauth_codes` row is deleted the
  moment it is spent, with no age gate at all, and an `oauth_tokens` row seven
  days after it expires. So a client that was genuinely used, whose grant was
  revoked and whose rows aged out, is indistinguishable from one that never
  was. Rejected as the *sole* signal; the sweep keeps it as a mandatory second
  guard.
* **`usage_logs.actor_ref`** survives credential deletion by design and needs
  no migration — but it records *tool calls*, not issuance, so a client that
  authorized and never called a tool has no row there, and it would couple
  OAuth retention to an analytics table whose own retention may change.

So: one nullable `timestamptz`, no server default, stamped by the application
in the same transaction that issues a code or a token.

## The backfill stamps **every** pre-existing row, and that is the point

Where a client has surviving `oauth_tokens` or `oauth_codes` rows the marker
is the newest of their `created_at`. Where it has neither — the ambiguous
case — it is **the migration's own transaction timestamp, never NULL**.

The first draft left those rows NULL and justified the resulting first-sweep
deletion with "a dynamically registered client re-registers transparently".
That is false in a reachable case: RFC 7591 permits credentials to be packaged
into client software by a developer, and a confidential client configured by
hand months ago, whose token rows have long since been purged, would lose its
registration **and its secret hash** on the first sweep with no automatic
recovery — registering again mints a *different* `client_id` and secret the
configured client does not have.

Stamping instead buys a clean invariant in place of a probabilistic one:
**after 025, `last_used_at IS NULL` means "registered after 025 and never
used" and nothing else**, so the sweep only ever acts on a row whose entire
history is visible to it. The cost is that genuinely unused registrations
predating 025 are never collected — bounded by whatever is in the table today,
and removable by an operator in the panel.

A marker that is already present is **never** overwritten, so the backfill is
safe to re-run: the gate exercises idempotence by `alembic stamp 024` then
`upgrade head`, and a re-run that recomputed markers would rewrite a stamp the
application had since recorded — which is exactly the reassignment-lag mistake
016 refuses to make with vault provenance.

## Reconciliation, and why it is not a bare ADD COLUMN

The stamp-back re-run means this body executes against a database that already
carries its column. Bare DDL raises there; `IF NOT EXISTS` is worse, because
it adopts *any* column of that name — a `NOT NULL` one (no row could then say
"never used", so the sweep collects nothing, for ever and silently), or one
carrying a server default (every fresh registration born with a use it has not
had, same outcome), or one of the wrong type. 013's rule applies: reconcile a
database that demonstrably has our shape, refuse to guess for one that does
not, and **name what disagreed**.

The column carries a `COMMENT` marker, mirrored byte-identically in
`src/models/db.py` as `_OAUTH_CLIENT_LAST_USED_COLUMN_MARKER` so
`alembic check` compares it like any other attribute, and `downgrade()` drops
the column only if it carries that marker.

## Locks

`ADD COLUMN` of a nullable column with no default is metadata-only. The
backfill then writes every row of `oauth_clients` — a table with tens of rows
here — under the `ACCESS EXCLUSIVE` lock the `ALTER` already took, so no
concurrent authorization can insert a client between the add and the stamp and
escape the invariant. `lock_timeout` / `statement_timeout` make a blocked
migration fail fast instead of stalling the deploy, and both are `RESET` at
the end because alembic runs every pending revision in one transaction and
`SET LOCAL` would otherwise leak into a later revision (013 through 024 do the
same).

`search_path` is pinned to `public` for 021's reason and 023's and 024's
repetition of it — each of those `RESET`s its own pin, so 025 needs one — and
the qualified identity is asserted afterwards. A column added to an
`oauth_clients` in a decoy schema would leave the sweep reading a table the
application never writes, which is a sweep that deletes live registrations.

Revision ID: 025
Revises: 024
Create Date: 2026-09-21
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "025"
down_revision: Union[str, None] = "024"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


TABLE = "oauth_clients"
QUALIFIED = "public.oauth_clients"
COLUMN = "last_used_at"

EXPECTED_TYPE = "timestamp with time zone"

# 013's device, and 015's through 024's: the migration marks what it created,
# so `downgrade()` can tell its own work from somebody else's and drop only the
# former. Must stay byte identical to `_OAUTH_CLIENT_LAST_USED_COLUMN_MARKER`
# in `src/models/db.py`, where it is declared as the column comment so
# `alembic check` compares it like any other attribute.
COLUMN_MARKER = (
    "client use marker, stamped at code and token issuance "
    "(025_oauth_client_last_used)"
)


def _quote(value: str) -> str:
    """A single-quoted SQL string literal. `COLUMN_MARKER` is a module constant
    with no quotes in it; the doubling is here so it stays correct if that
    changes."""
    return "'" + value.replace("'", "''") + "'"


# --------------------------------------------------------------------------
# catalogue reads
# --------------------------------------------------------------------------


def _oid(bind, name: str):
    """The OID `name` resolves to, or None. `to_regclass` never raises."""
    return bind.execute(
        sa.text("SELECT CAST(to_regclass(:name) AS oid)"), {"name": name}
    ).scalar()


def _column_state(bind):
    """`(format_type, attnotnull, default_expr, comment)` for the column, or
    None when it is absent."""
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
        {"table": QUALIFIED, "column": COLUMN},
    ).first()


# --------------------------------------------------------------------------
# search_path
# --------------------------------------------------------------------------


def _pin_search_path() -> None:
    """Pin `search_path` to `public` for the rest of this transaction.

    021's device, 023's and 024's repetition of it. `op.add_column`,
    `COMMENT ON` and the backfill's UPDATE are all unqualified and resolve
    through `search_path`; giving them `schema="public"` would make the object
    schema-qualified in alembic's eyes while the model declares no schema, so
    `alembic check` would report drift for ever after. **021, 023 and 024 each
    `RESET` their own pin, which is precisely why 025 cannot rely on one still
    being in force.**
    """
    op.execute("SET LOCAL search_path TO public")


def _assert_is_the_qualified_table(bind) -> None:
    """What the unqualified name resolves to is `public.oauth_clients`.

    Belt and braces behind the pin, in 023's and 024's shape. A column added to
    another schema's `oauth_clients` would leave the application stamping a
    table the sweep never reads — i.e. a sweep that deletes registrations that
    are in daily use.
    """
    qualified = _oid(bind, QUALIFIED)
    unqualified = _oid(bind, TABLE)
    if qualified is None or qualified != unqualified:
        raise RuntimeError(
            f"025's {TABLE} is not the table {QUALIFIED} resolves to "
            f"(search_path-relative oid {unqualified!r}, qualified oid "
            f"{qualified!r}). The OAuth routes and the expiry sweep both read "
            "the unqualified name, so a table elsewhere on the path would "
            "leave the marker written in one place and read in another. Set "
            "the migration role's search_path so `public` comes first, then "
            "re-run."
        )


# --------------------------------------------------------------------------
# the column
# --------------------------------------------------------------------------


def _reconcile_column(bind) -> None:
    state = _column_state(bind)
    if state is None:
        op.add_column(
            TABLE, sa.Column(COLUMN, sa.DateTime(timezone=True), nullable=True)
        )
        # Stamped in the same transaction as the ADD, so the marker and the
        # column can never disagree about who made it. `COMMENT ON` is utility
        # DDL and takes no bind parameter, so the literal is quoted.
        op.execute(f"COMMENT ON COLUMN {TABLE}.{COLUMN} IS {_quote(COLUMN_MARKER)}")
        return

    coltype, notnull, default, comment = state
    problems = []
    if coltype != EXPECTED_TYPE:
        problems.append(f"it is {coltype}, not {EXPECTED_TYPE}")
    if notnull:
        problems.append(
            "it is NOT NULL; 025 creates it nullable, and NULL is the value "
            "that means 'never used' — a NOT NULL column has no way to say it, "
            "so the sweep would collect nothing for ever and silently"
        )
    if default is not None:
        problems.append(
            f"it has a server default of {default!r}; 025 creates none, and a "
            "default is a use every fresh registration is born with, which "
            "also means the sweep can never collect one"
        )
    if comment != COLUMN_MARKER:
        problems.append("it does not carry 025's comment marker")
    if problems:
        raise RuntimeError(
            f"{TABLE}.{COLUMN} already exists but {'; '.join(problems)}. 025 "
            "will not adopt a column of unknown provenance: the maintenance "
            "pass deletes a client registration — and cascades its codes and "
            "tokens — on the strength of this column being NULL, so a shape "
            "this migration did not create is either a sweep that collects "
            "nothing or a sweep that deletes a credential somebody is using. "
            "Resolve by hand — drop it and let 025 create it, or make it "
            "match — then re-run. Nothing has been changed."
        )


def _backfill(bind) -> None:
    """Stamp every row that is still NULL, and never touch one that is not.

    `GREATEST` over two correlated `MAX(created_at)` subqueries, coalesced to
    `now()`: the newest surviving child row of either kind, or — the ambiguous
    case, and the whole argument of D8 — the migration's own transaction
    timestamp where there is none. **PostgreSQL's `GREATEST` ignores NULL
    arguments** and answers NULL only when every argument is NULL, so the
    outer `COALESCE` fires exactly for a client with neither kind of child row.
    `now()` is the transaction timestamp, so every row stamped by this
    statement carries the same instant — which makes "stamped by 025" a
    recognisable value rather than a smear across the backfill's duration.

    `WHERE last_used_at IS NULL` is what makes the body re-runnable: the gate
    stamps back to 024 and upgrades again, and a statement that recomputed
    every marker would overwrite a stamp the application had recorded in
    between, ageing a live client backwards towards the sweep's cutoff.
    """
    op.execute(
        sa.text(
            "UPDATE oauth_clients c SET last_used_at = COALESCE("
            "  GREATEST("
            "    (SELECT MAX(t.created_at) FROM oauth_tokens t "
            "      WHERE t.client_id = c.client_id),"
            "    (SELECT MAX(o.created_at) FROM oauth_codes o "
            "      WHERE o.client_id = c.client_id)"
            "  ),"
            "  now()"
            ") "
            "WHERE c.last_used_at IS NULL"
        )
    )


# --------------------------------------------------------------------------
# upgrade / downgrade
# --------------------------------------------------------------------------


def upgrade() -> None:
    bind = op.get_bind()

    # Fail fast rather than queueing behind a long-lived transaction: the
    # deploy migrates before recreating the container, so a stalled migration
    # is a stalled deploy while the old container is still serving. Per
    # statement and per lock acquisition, not a budget for the transaction.
    op.execute("SET LOCAL lock_timeout = '10s'")
    op.execute("SET LOCAL statement_timeout = '60s'")
    _pin_search_path()

    _assert_is_the_qualified_table(bind)
    _reconcile_column(bind)
    _backfill(bind)

    # `SET LOCAL` is scoped to the transaction and alembic runs every pending
    # revision in *one*, so without this a later revision would silently
    # inherit these settings and blame its own SQL when it tripped them.
    op.execute("RESET lock_timeout")
    op.execute("RESET statement_timeout")
    op.execute("RESET search_path")


def downgrade() -> None:
    """Drop the column only if it carries 025's marker.

    013's rule: a downgrade must undo *this* migration, not delete a column
    somebody else put there under this name. The marker is the only evidence of
    authorship.

    Dropping it loses every recorded use. That is safe in the direction it
    matters: the previous build neither reads nor writes the column, and the
    sweep that reads it does not exist there either, so no client is collected
    while it is absent. A re-upgrade re-adds it and stamps every row from the
    surviving child rows or with its own timestamp — so a client that is in use
    comes back marked, and one that is not is treated as a pre-025 registration
    and is never collected.
    """
    bind = op.get_bind()
    _pin_search_path()
    if _oid(bind, QUALIFIED) is None:
        op.execute("RESET search_path")
        return
    _assert_is_the_qualified_table(bind)

    state = _column_state(bind)
    if state is None:
        op.execute("RESET search_path")
        return
    if state[3] != COLUMN_MARKER:
        raise RuntimeError(
            f"{TABLE}.{COLUMN} does not carry 025's comment marker "
            f"({COLUMN_MARKER!r}), so 025 did not create it and will not drop "
            "it. Nothing has been changed. Remove it by hand if you mean to."
        )
    op.drop_column(TABLE, COLUMN)
    op.execute("RESET search_path")
