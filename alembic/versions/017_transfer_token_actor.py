"""Record the minting actor on `transfer_tokens` (issue #92, item 2).

#77 gave every MCP tool call a denormalised actor on `usage_logs` —
`actor_kind`, `actor_label`, `actor_ref`, bound by `APIKeyMiddleware` into
`current_actor` and written by `_log_usage` — because both credential FKs are
allowed to lose their target while the log row stays, and both do so on the
operator's most urgent path. **The transfer routes were not covered, and
CLAUDE.md recorded the gap as known.**

`src/transfer/routes.py::_log_row` builds its own `UsageLog` from the *minting*
identity carried on the `transfer_tokens` row — `key_id`, `oauth_token_id`,
`user_id`, and nothing else — because a redemption request is session-less and
authenticates with a **capability, not a credential**: there is no
request-scoped actor to read. So those rows were attributed by LEFT JOIN,
exactly as every row was before #77. Delete the OAuth client and every
`upload_file` / `download_file` line it produced renders "unknown"; NULL a key's
`usage_logs.key_id` before deleting it and the same thing happens. Those are
the rows an operator reviewing a suspect connector most wants to read: the ones
where bytes entered or left the vault.

The fix is to record the actor where the credential *is* still in hand — at
mint, in `mint_token`, from the ContextVar the middleware has already bound —
and to copy it onto the usage row at redemption. This migration adds the three
columns and labels the rows that already exist.

## The backfill, and how it differs from 015's

Every token row whose credential still resolves is labelled from that
credential: `api_keys` for a key-minted row, `oauth_tokens` -> `oauth_clients`
for an OAuth-minted one. The difference from 015 is worth stating precisely,
because it changes what the NULL rows *are*: `transfer_tokens.key_id` and
`.oauth_token_id` are both **`ON DELETE CASCADE`**, so a row whose minting
credential has been deleted does not survive to be labelled at all. The rows
this backfill leaves NULL are therefore the ones carrying no credential FK —
a single-user or sandbox mint — and they stay NULL rather than being inferred
from `user_id`.

**Nothing is invented.** A row's label comes from the credential its own FK
points at, or it stays NULL, for 015's reason: two of a user's keys are
different actors, and a wrong label is worse than an admitted gap when the
whole value of the column is that an operator can trust it.

**Nothing is re-stamped.** The backfill is guarded on `actor_kind IS NULL`, so
a re-run (the schema gate does `alembic stamp 016` then `upgrade head`) leaves
every recorded label alone. That matters beyond idempotence: the label is a
snapshot of the credential *at mint time*, and re-deriving it from the
credential's present state would silently rewrite history whenever a key is
renamed.

**A label beside a NULL `actor_kind` aborts the migration.** `actor_kind IS
NULL` is the backfill's *only* guard, so such a row would be relabelled from
whatever credential its FK points at now — overwriting a recorded attribution,
the one thing these columns must never do. 015 runs the same offender query for
the same reason; the first draft of this change claimed to follow 015 exactly
and omitted it. No current application path produces that state, which is
precisely why the check is cheap insurance against drift rather than a hot
path, and it is the invariant that makes the `actor_kind IS NULL` guard safe on
a stamp-back re-run rather than merely well typed.

## This migration writes nothing to `usage_logs`

A transfer `usage_logs` row written before 017 carries no reference back to the
token that produced it — there is no `transfer_token_id` on `usage_logs`, and
adding one in order to label history would be inventing a join that never
existed. The only other available backfill is a re-run of migration 015's own
credential join, which would put a **second writer** on three columns 015 owns
and guards; that is the "second path that resolves the same thing" shape #64
argued against, and it is how the two writers' guards start disagreeing.

So the rows in the 015 -> 017 gap keep join-only attribution: they render
through the panel's existing pre-015 fallback and show "unknown (credential
deleted)" when that join resolves to nothing. A bounded set that only shrinks,
stated here rather than left to be found.

## Locks

One `ALTER TABLE ... ADD COLUMN` per column, which takes ACCESS EXCLUSIVE on
`transfer_tokens` and holds it to COMMIT, plus two `UPDATE ... FROM` statements
that read `api_keys` / `oauth_tokens` / `oauth_clients` (ACCESS SHARE).
`transfer_tokens` is a child of all three, so the order is child-then-parent —
the direction 013 established and the direction the app itself takes, and
therefore not a new way to close a wait cycle.

`lock_timeout` / `statement_timeout` make a blocked or slow migration fail fast
instead of stalling the deploy, and both are `RESET` at the end because alembic
runs every pending revision in one transaction and `SET LOCAL` would otherwise
leak into a later revision (013, 014, 015 and 016 do the same).

Revision ID: 017
Revises: 016
Create Date: 2026-08-24
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "017"
down_revision: Union[str, None] = "016"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


TABLE = "transfer_tokens"

# 013's device, and 015's and 016's: the migration marks what it created, so
# `downgrade()` can tell its own work from somebody else's and drop only the
# former. Here it does 015's second job too — it is the evidence that the
# values in these columns were written by *this* attribution scheme, which is
# the only reason the panel may present them to an operator as an audit trail.
#
# A distinct string from 015's, because a distinct migration owns these three
# columns on a different table; sharing one marker would let either
# `downgrade()` claim the other's work.
#
# Declared on the ORM columns too (`src/models/db.py`,
# `TransferToken._ACTOR_COLUMN_MARKER`), so `alembic check` compares it like
# any other column attribute: a marker that drifted from the model, or one
# silently dropped, is a dirty check rather than a migration that quietly stops
# recognising its own work. Keep the two byte identical.
MARKER = "denormalised actor, recorded at mint (017_transfer_token_actor)"

# (name, SQLAlchemy type, how PostgreSQL prints it). Identical to 015's, on
# purpose: the two tables' actor columns are written through one reader
# (`src.auth.session.actor_columns`), and a width that differed between them
# would make that reader truncate correctly for one writer and wrongly for the
# other. The three are one owned unit: all absent, or all present in exactly
# this shape. See `_reconcile`.
COLUMNS = (
    ("actor_kind", sa.String(20), "character varying(20)"),
    ("actor_label", sa.String(255), "character varying(255)"),
    ("actor_ref", sa.String(64), "character varying(64)"),
)
COLUMN_NAMES = tuple(name for name, _t, _e in COLUMNS)


def _quote(value: str) -> str:
    """A single-quoted SQL string literal. `MARKER` is a module constant with
    no quotes in it; the doubling is here so it stays correct if that changes."""
    escaped = value.replace("'", "''")
    return f"'{escaped}'"


def _column_state(bind, column: str):
    """`(formatted_type, attnotnull, default_expr, comment)`, or None if absent."""
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
        {"table": TABLE, "column": column},
    ).first()


def _describe(column: str, state) -> str:
    if state is None:
        return f"{column}: absent"
    coltype, notnull, default, comment = state
    return (
        f"{column}: {coltype}"
        f"{' NOT NULL' if notnull else ''}"
        f"{f' DEFAULT {default}' if default else ''}"
        f"{'' if comment == MARKER else f' comment={comment!r}'}"
    )


def _refuse(states, why: str) -> None:
    found = "; ".join(_describe(name, states[name]) for name in COLUMN_NAMES)
    raise RuntimeError(
        f"{TABLE} actor columns: {why} Found -- {found}. 017 owns these three "
        "columns as one unit: it creates all of them and marks each with the "
        f"comment {MARKER!r}, and it completes only a set it can prove it "
        "wrote. It will not adopt columns of unknown provenance, because the "
        "labels in them are copied onto `usage_logs` at redemption and shown "
        "to an operator as an audit trail. Resolve by hand -- drop them and "
        "let 017 create them, or make them match -- then re-run. Nothing has "
        "been changed."
    )


def _assert_no_orphan_labels(bind) -> None:
    """No row may carry a label while its `actor_kind` is NULL.

    015's rule, ported to this table. The backfill's guard is `actor_kind IS
    NULL`, so such a row would be re-labelled from whatever credential its FK
    currently points at -- overwriting an attribution somebody else wrote,
    which is the one thing these columns must never do.
    """
    offenders = bind.execute(
        sa.text(
            f"SELECT id FROM {TABLE} "
            "WHERE actor_kind IS NULL "
            "  AND (actor_label IS NOT NULL OR actor_ref IS NOT NULL) "
            "ORDER BY id LIMIT 20"
        )
    ).scalars().all()
    if not offenders:
        return
    total = bind.execute(
        sa.text(
            f"SELECT count(*) FROM {TABLE} "
            "WHERE actor_kind IS NULL "
            "  AND (actor_label IS NOT NULL OR actor_ref IS NOT NULL)"
        )
    ).scalar_one()
    raise RuntimeError(
        f"{total} {TABLE} row(s) carry an actor label with a NULL actor_kind "
        f"(ids: {', '.join(str(i) for i in offenders)}"
        f"{', ...' if total > len(offenders) else ''}). The backfill selects on "
        "`actor_kind IS NULL`, so it would overwrite those labels from the "
        "credentials the rows point at now. 017 will not rewrite an "
        "attribution it did not write. Nothing has been changed."
    )


def _reconcile(bind) -> None:
    """Create the three columns, or verify a pre-existing set is 017's own.

    013's, 014's, 015's and 016's philosophy: reconcile a database that
    demonstrably has our shape, refuse to guess for one that does not. Three
    details are load-bearing:

    - **All three or none.** A *partial* set is not a re-run; it is a database
      somebody edited. Adding the missing two beside a foreign `actor_kind`
      would run the backfill against a guard column of unknown meaning.
    - **The whole shape, not just the width.** A `NOT NULL` `actor_label`, or
      one carrying a server default, is not what 017 creates -- and neither is
      visible to `alembic check`'s notion of "the column exists". Nullability
      is the load-bearing half: these columns must stay nullable so a mint that
      cannot name its actor (single-user, sandbox, any path outside a request)
      still produces a token.
    - **The marker.** Type and width alone are a coincidence anyone could
      reproduce; the comment is the only evidence that *this* migration wrote
      the values. Without it a hand-made `varchar(255)` full of arbitrary text
      would be adopted, copied onto `usage_logs` at redemption, and rendered as
      attribution.
    """
    states = {name: _column_state(bind, name) for name in COLUMN_NAMES}
    present = [name for name in COLUMN_NAMES if states[name] is not None]

    if not present:
        for column, type_, _expected in COLUMNS:
            op.add_column(TABLE, sa.Column(column, type_, nullable=True))
            # Stamped immediately, in the same transaction as the ADD, so the
            # marker and the column can never disagree about who made it.
            # `COMMENT ON` takes no bind parameter -- it is utility DDL, not a
            # planned statement -- so the literal is quoted rather than bound.
            op.execute(f"COMMENT ON COLUMN {TABLE}.{column} IS {_quote(MARKER)}")
        return

    if len(present) != len(COLUMN_NAMES):
        missing = [name for name in COLUMN_NAMES if states[name] is None]
        _refuse(states, f"only {', '.join(present)} exist ({', '.join(missing)} absent).")

    for column, _type, expected in COLUMNS:
        coltype, notnull, default, comment = states[column]
        if coltype != expected:
            _refuse(states, f"{column} is {coltype}, not {expected}.")
        if notnull:
            _refuse(states, f"{column} is NOT NULL; 017 creates it nullable.")
        if default is not None:
            _refuse(states, f"{column} carries a server default; 017 creates none.")
        if comment != MARKER:
            _refuse(states, f"{column} does not carry 017's comment marker.")


def upgrade() -> None:
    bind = op.get_bind()

    # Fail fast rather than queueing behind a long-lived transaction: the
    # deploy migrates before recreating the container, so a stalled migration
    # is a stalled deploy while the old container is still serving. Per
    # statement and per lock acquisition, not a budget for the transaction.
    op.execute("SET LOCAL lock_timeout = '10s'")
    op.execute("SET LOCAL statement_timeout = '60s'")

    _reconcile(bind)

    # Before anything is written, and *after* the columns are known to exist:
    # the guard column the backfill selects on must not already be lying about
    # which rows are unlabelled.
    _assert_no_orphan_labels(bind)

    # API-key mints. `actor_kind IS NULL` is what makes this a one-time stamp
    # rather than a re-derivation: a label is a snapshot of the credential at
    # mint time, and rewriting it from the credential's present state would
    # quietly rewrite history every time a key is renamed.
    op.execute(
        """
        UPDATE transfer_tokens AS tt
        SET actor_kind  = 'api_key',
            actor_label = ak.name,
            actor_ref   = ak.key_prefix
        FROM api_keys AS ak
        WHERE tt.key_id = ak.id
          AND tt.actor_kind IS NULL
        """
    )

    # OAuth mints. The same join `/admin/usage` performs for a transfer row, so
    # the backfilled label is exactly what the page renders today while the
    # credential still resolves -- this migration changes what *survives*, not
    # what is displayed. An inner join throughout: a token whose client row is
    # gone (impossible today, `client_id` is a FK) leaves the row NULL rather
    # than half-labelled.
    op.execute(
        """
        UPDATE transfer_tokens AS tt
        SET actor_kind  = 'oauth',
            actor_label = oc.client_name,
            actor_ref   = oc.client_id
        FROM oauth_tokens AS ot
        JOIN oauth_clients AS oc ON oc.client_id = ot.client_id
        WHERE tt.oauth_token_id = ot.id
          AND tt.actor_kind IS NULL
        """
    )

    # Deliberately no statement against `usage_logs` -- see the module
    # docstring. Rows in the 015 -> 017 gap keep join-only attribution.

    # `SET LOCAL` is scoped to the transaction and alembic runs every pending
    # revision in *one*, so without this the next revision would silently
    # inherit these timeouts and blame its own SQL when it tripped them.
    op.execute("RESET lock_timeout")
    op.execute("RESET statement_timeout")


def downgrade() -> None:
    """Drop only the columns carrying 017's marker, all or nothing.

    013's rule, for the same reason: a downgrade must undo *this* migration,
    not delete a column somebody else put there under one of these names. The
    marker is the only evidence of authorship, so it is what the drop keys on --
    an unmarked `actor_label` is left in place and reported, rather than
    destroyed on the way past.

    Downgrading destroys the recorded attribution for every token whose
    credential has since been deleted -- which, `ON DELETE CASCADE` being what
    it is, means every such token row is gone too. What it really costs is the
    labels already copied onto `usage_logs` staying while the tokens that would
    re-derive them do not: re-upgrading can only read credentials that still
    exist.
    """
    bind = op.get_bind()
    states = {name: _column_state(bind, name) for name in COLUMN_NAMES}

    # Decide before touching anything, so the downgrade is all-or-nothing
    # rather than "dropped two of three and then raised".
    foreign = [
        name
        for name in COLUMN_NAMES
        if states[name] is not None and states[name][3] != MARKER
    ]
    if foreign:
        raise RuntimeError(
            f"{TABLE}.{', '.join(foreign)} do not carry 017's comment marker "
            f"({MARKER!r}), so 017 did not create them and will not drop them. "
            "Nothing has been changed. Remove them by hand if you mean to."
        )

    for name in COLUMN_NAMES:
        if states[name] is not None:
            op.drop_column(TABLE, name)
