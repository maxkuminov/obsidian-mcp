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

# 013's device: the migration marks what it created, so `downgrade()` can tell
# its own work from somebody else's and drop only the former. Here it does a
# second job -- it is also the evidence that the values in these columns were
# written by *this* attribution scheme, which is the only reason the panel may
# present them as an audit trail.
#
# It is also declared on the ORM columns (`src/models/db.py`), so `alembic
# check` compares it like any other column attribute: a marker that drifted
# from the model, or one silently dropped, is a dirty check rather than a
# migration that quietly stops recognising its own work.
MARKER = "denormalised actor, written at call time (015_usage_log_actor)"

# (name, SQLAlchemy type, how PostgreSQL prints it). The three are one owned
# unit: all absent, or all present in exactly this shape. See `_reconcile`.
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
        f"{TABLE} actor columns: {why} Found -- {found}. 015 owns these three "
        "columns as one unit: it creates all of them and marks each with the "
        f"comment {MARKER!r}, and it completes only a set it can prove it "
        "wrote. It will not adopt columns of unknown provenance, because the "
        "labels in them would be presented to an operator as an audit trail. "
        "Resolve by hand -- drop them and let 015 create them, or make them "
        "match -- then re-run. Nothing has been changed."
    )


def _assert_no_orphan_labels(bind) -> None:
    """No row may carry a label while its `actor_kind` is NULL.

    The backfill's guard is `actor_kind IS NULL`, so such a row would be
    re-labelled from whatever credential its FK currently points at --
    overwriting an attribution somebody else wrote, which is the one thing
    these columns must never do.
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
        "credentials the rows point at now. 015 will not rewrite an "
        "attribution it did not write. Nothing has been changed."
    )


def _reconcile(bind) -> None:
    """Create the three columns, or verify a pre-existing set is 015's own.

    Same philosophy as 013's constraint and 014's column: reconcile a database
    that demonstrably has our shape, refuse to guess for one that does not.
    Three details are load-bearing and were each a hole in the first draft:

    - **All three or none.** A *partial* set is not a re-run; it is a database
      somebody edited. Adding the missing two beside a foreign `actor_kind`
      would run the backfill against a guard column of unknown meaning.
    - **The whole shape, not just the width.** A `NOT NULL` `actor_label`, or
      one carrying a server default, is not what 015 creates -- and neither is
      visible to `alembic check`'s notion of "the column exists". Nullability
      is the load-bearing half: these columns must stay nullable so a call that
      cannot name its actor is still recorded.
    - **The marker.** Type and width alone are a coincidence anyone could
      reproduce; the comment is the only evidence that *this* migration wrote
      the values. Without it a hand-made `varchar(255)` full of arbitrary text
      would be adopted and rendered to an operator as attribution.
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
            _refuse(states, f"{column} is NOT NULL; 015 creates it nullable.")
        if default is not None:
            _refuse(states, f"{column} carries a server default; 015 creates none.")
        if comment != MARKER:
            _refuse(states, f"{column} does not carry 015's comment marker.")

    # A verified re-run. The rows are still checked before the guard column is
    # used to decide what to write.
    _assert_no_orphan_labels(bind)


def upgrade() -> None:
    bind = op.get_bind()

    # Fail fast rather than queueing behind a long-lived transaction: the
    # deploy migrates before recreating the container, so a stalled migration
    # is a stalled deploy while the old container is still serving. Per
    # statement and per lock acquisition, not a budget for the transaction.
    op.execute("SET LOCAL lock_timeout = '10s'")
    op.execute("SET LOCAL statement_timeout = '60s'")

    _reconcile(bind)

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
    """Drop only the columns carrying 015's marker.

    013's rule, for the same reason: a downgrade must undo *this* migration,
    not delete a column somebody else put there under one of these names. The
    marker is the only evidence of authorship, so it is what the drop keys on —
    an unmarked `actor_label` is left in place and reported, rather than
    destroyed on the way past.

    Downgrading does destroy attribution for every row whose credential has
    since been deleted, and re-upgrading cannot recover it: the backfill can
    only read credentials that still exist.
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
            f"{TABLE}.{', '.join(foreign)} do not carry 015's comment marker "
            f"({MARKER!r}), so 015 did not create them and will not drop them. "
            "Nothing has been changed. Remove them by hand if you mean to."
        )

    for name in COLUMN_NAMES:
        if states[name] is not None:
            op.drop_column(TABLE, name)
