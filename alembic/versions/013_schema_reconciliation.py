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

**Rows are never mutated to satisfy the CHECK, and nothing that the CHECK
reads is touched before it has been verified.** The offender query runs over
the raw `oauth_clients` rows, *before* any reconciliation of
`token_endpoint_auth_method` -- a NULL there is counted as an offender rather
than quietly backfilled to `client_secret_post`, because backfilling it is
exactly what would turn a row the constraint should reject (NULL method with a
secret, or without one) into a row that passes. If any row violates the
predicate the migration raises, naming the offending `client_id`s, and the
whole transaction rolls back: schema and rows unchanged, `make deploy` aborts
before recreating the container, the old container keeps serving. Only after
that check passes does 013 touch the column's *type, default and NOT NULL* --
declarative fixes that change no row's value. (`oauth_clients.created_at` is
backfilled, but it is not an input to the CHECK.)

If `token_endpoint_auth_method` is absent entirely the migration refuses: 010
always adds it, so its absence means the database never had 010's shape at all
and 013's job -- reconciling a database that *did* -- is not the right repair.

**A non-canonical constraint squatting on our name is resolved before the
column is touched, not after.** `ALTER COLUMN ... TYPE` re-validates every CHECK
that reads the column against the live rows, so an impostor does not merely fail
to enforce the right thing -- it *blocks the repair*: on a column widened to
`text`, a same-named `CHECK (pg_typeof(token_endpoint_auth_method) =
'text'::regtype)` makes the `ALTER ... TYPE character varying(32)` abort with
"check constraint ... is violated by some row", and 013 never reaches the step
that would have replaced it. So the sequence is: verify the rows, drop a
same-named constraint that is not the canonical one *for the current column
types* (or that is NOT VALID), then fix type/default/nullability, then add the
canonical constraint. Dropping is only safe in that position because the rows
have already been verified and the table lock is already held; if anything later
in the migration fails, the DROP rolls back with it.

Locks and timeouts (design D3a/D3b): `SET NOT NULL` takes ACCESS EXCLUSIVE and
scans the table without rewriting it; `usage_logs` (~10k rows) scans in
milliseconds. `LOCK TABLE oauth_clients IN SHARE ROW EXCLUSIVE MODE` blocks
concurrent DML for the rest of the transaction so the offender check cannot
race an insert.

**Lock order is child-first.** The five other tables (`api_keys`,
`notes_metadata`, `oauth_codes`, `oauth_tokens`, `usage_logs`) are backfilled
and set NOT NULL *before* `oauth_clients` is locked, so this migration never
holds the parent's lock while asking for a child's. That matches the
application's own order: the token exchange in `src/oauth/routes.py` takes
`SELECT ... FOR UPDATE` on `oauth_codes` first, then reads `oauth_clients`,
then inserts `oauth_tokens`. Same direction on both sides means a concurrent
OAuth request queues behind the migration instead of closing a wait cycle with
it.

That is an ordering guarantee, **not** a promise of no contention. The residual
behaviour under concurrent OAuth traffic is *rollback and retry*: a request
already holding a row lock the migration needs will make the migration wait,
and if it waits past `lock_timeout` the migration aborts, the transaction rolls
back whole, and the deploy fails before the container is recreated -- re-run it
when traffic is quiet. Note the timeouts are per **statement** and per **lock
acquisition**, not a budget for the transaction as a whole: a migration made of
many fast statements can exceed 60s in total without any single one tripping
`statement_timeout`.

`_canonical_constraintdef` and `_canonical_default` build a scratch `TEMP`
table, so the migration role needs the **TEMP privilege** on the database
(`GRANT TEMPORARY ON DATABASE ... TO ...`). The owner/superuser role the deploy
uses has it; a locked-down migration role may not.

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

# What 010 declares for the column, and what the model's server_default says.
AUTH_METHOD_TYPE = "character varying(32)"
AUTH_METHOD_DEFAULT = "client_secret_post"

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

# Split by table so the child tables can be done before `oauth_clients` is
# locked -- see the lock-order paragraph in the module docstring.
OTHER_NOT_NULL_COLUMNS = tuple(c for c in NOT_NULL_COLUMNS if c[0] != "oauth_clients")
OAUTH_CLIENTS_NOT_NULL_COLUMNS = tuple(
    c for c in NOT_NULL_COLUMNS if c[0] == "oauth_clients"
)


def _scratch_like_oauth_clients(bind, name: str) -> None:
    """An empty TEMP table with `oauth_clients`' exact current column shapes.

    `ON COMMIT DROP` is belt-and-braces: every caller drops it explicitly so a
    second call in the same transaction does not collide on the name.
    """
    bind.execute(sa.text(f"CREATE TEMP TABLE {name} (LIKE oauth_clients) ON COMMIT DROP"))


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
    _scratch_like_oauth_clients(bind, "_ck_canon")
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


def _canonical_default(bind) -> str:
    """What this server stores for `DEFAULT 'client_secret_post'` on that column.

    Same reasoning as `_canonical_constraintdef`: the server normalizes a
    default expression (`'client_secret_post'::character varying`), and the
    rendering depends on the column's type, so comparing against a hand-written
    string would either miss a real drift or "reconcile" a correct default on
    every run. Callers fix the column *type* first, so the scratch table copies
    the canonical type and the derived default is the one we want to see.
    """
    _scratch_like_oauth_clients(bind, "_def_canon")
    bind.execute(
        sa.text(
            "ALTER TABLE _def_canon ALTER COLUMN token_endpoint_auth_method "
            f"SET DEFAULT '{AUTH_METHOD_DEFAULT}'"
        )
    )
    default_expr = _column(bind, "_def_canon", "token_endpoint_auth_method")[2]
    bind.execute(sa.text("DROP TABLE _def_canon"))
    return _normalize(default_expr)


def _normalize(definition: str) -> str:
    """Whitespace-insensitive form of a catalog-printed expression."""
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


def _require_check_columns(bind) -> None:
    """Both columns the CHECK predicate reads must exist before we query it.

    Historically impossible for `token_endpoint_auth_method`: 010 always adds
    it, and the live database is past 010. If it is nevertheless gone, the
    database is not one that ever had 010's shape, so silently re-adding the
    column here would invent auth methods for every existing client -- the
    operator has to decide what those clients are.
    """
    for column, hint in (
        (
            "client_secret_hash",
            "migration 002 creates it; the database is not at the shape 010 expects",
        ),
        (
            "token_endpoint_auth_method",
            "migration 010 always adds it, so its absence means 010's shape was "
            "never applied. Apply 010's column by hand (deciding the right "
            "token_endpoint_auth_method for each existing client -- 013 will not "
            "guess it) and re-run",
        ),
    ):
        if _column(bind, "oauth_clients", column) is None:
            raise RuntimeError(
                f"oauth_clients.{column} is missing: {hint}. Nothing has been changed."
            )


def _assert_no_violating_rows(bind) -> None:
    """No row may violate the CHECK -- read *before* anything is reconciled.

    Ordering is the whole point: this runs over the raw rows, so a NULL
    `token_endpoint_auth_method` is reported as an offender instead of being
    backfilled into a passing row first. `IS DISTINCT FROM true` rather than
    `NOT (...)`: a three-valued NULL result (possible only on a drifted,
    nullable column) is a row the constraint would reject, and `NOT NULL` is
    NULL, so a plain NOT would skip it and let ADD CONSTRAINT fail with a far
    less useful message. The explicit `IS NULL` disjunct is redundant with that
    -- it is there so the intent survives an edit to the predicate.
    """
    offenders = bind.execute(
        sa.text(
            f"SELECT client_id FROM oauth_clients "
            f"WHERE token_endpoint_auth_method IS NULL "
            f"   OR ({CANONICAL_PREDICATE}) IS DISTINCT FROM true "
            f"ORDER BY client_id"
        )
    ).scalars().all()
    if offenders:
        raise RuntimeError(
            f"{len(offenders)} oauth_clients row(s) violate {CONSTRAINT_NAME}: "
            + ", ".join(repr(c) for c in offenders)
            + ". A public client (token_endpoint_auth_method='none') must have "
            "no client_secret_hash and a confidential one must have it, and the "
            "method must not be NULL. Migration 013 will not delete or rewrite "
            "these rows -- fix them by hand, then re-run. Nothing has been changed."
        )


def _existing_named_check(bind):
    """`(convalidated, pg_get_constraintdef)` for our CHECK, or None if absent."""
    return bind.execute(
        sa.text(
            "SELECT convalidated, pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conrelid = 'oauth_clients'::regclass AND contype = 'c' "
            "  AND conname = :name"
        ),
        {"name": CONSTRAINT_NAME},
    ).first()


def _drop_stale_named_check(bind) -> None:
    """Clear a non-canonical constraint off our name *before* the column changes.

    An impostor under our name is not just wrong, it is *obstructive*: `ALTER
    COLUMN ... TYPE` re-validates every CHECK that reads the column, so a
    same-named `CHECK (pg_typeof(token_endpoint_auth_method) = 'text'::regtype)`
    on a widened column aborts the type repair before 013 ever gets to the step
    that would replace it. Resolving the constraint first breaks that knot.

    The comparison is against the canonical rendering **for the current column
    types**, which is exactly what `_canonical_constraintdef` computes -- its
    scratch table is `LIKE oauth_clients`, so on a `text` column it renders the
    predicate the way this server renders it for a `text` column. A constraint
    matching that is the real one, merely carried along by an earlier drift; it
    is left in place so `ALTER ... TYPE` rebuilds it and 013 stays a no-op for
    it (which is what keeps the plain wrong-type path from acquiring a marker it
    did not earn). Anything else -- a different predicate, or ours but NOT VALID
    -- is dropped here.

    Safe only in this position: `_assert_no_violating_rows` has already proved
    every row satisfies the canonical predicate, and the SHARE ROW EXCLUSIVE
    lock is already held, so nothing can slip in during the window where the
    table carries no constraint. The window closes inside the same transaction,
    and a later failure rolls the DROP back with everything else.
    """
    existing = _existing_named_check(bind)
    if existing is None:
        return
    validated, definition = existing
    if validated and _normalize(definition) == _canonical_constraintdef(bind):
        return
    op.execute(f"ALTER TABLE oauth_clients DROP CONSTRAINT {CONSTRAINT_NAME}")


def _reconcile_oauth_clients_shape(bind) -> None:
    """Everything 010 promised about `oauth_clients` other than the CHECK.

    Declarative fixes only -- column type, default, nullability. No row's value
    is written here. Runs *after* `_assert_no_violating_rows`, which is what
    makes `SET NOT NULL` on `token_endpoint_auth_method` safe without a
    backfill: a NULL would have been reported as an offender and we would never
    have got here.
    """
    secret = _column(bind, "oauth_clients", "client_secret_hash")
    if secret[0]:  # attnotnull -- 010 made it nullable for public clients
        op.execute("ALTER TABLE oauth_clients ALTER COLUMN client_secret_hash DROP NOT NULL")

    attnotnull, formatted_type, default_expr = _column(
        bind, "oauth_clients", "token_endpoint_auth_method"
    )

    if formatted_type != AUTH_METHOD_TYPE:
        op.execute(
            "ALTER TABLE oauth_clients ALTER COLUMN token_endpoint_auth_method "
            f"TYPE {AUTH_METHOD_TYPE}"
        )
        # Changing the type re-coerces any stored default, so the value read
        # before the ALTER is stale.
        default_expr = _column(bind, "oauth_clients", "token_endpoint_auth_method")[2]

    # Exact comparison against the server's own rendering, derived now that the
    # type is canonical. `"client_secret_post" in default_expr` would accept
    # `'not_client_secret_post'`.
    if default_expr is None or _normalize(default_expr) != _canonical_default(bind):
        op.execute(
            "ALTER TABLE oauth_clients ALTER COLUMN token_endpoint_auth_method "
            f"SET DEFAULT '{AUTH_METHOD_DEFAULT}'"
        )

    if not attnotnull:
        op.execute(
            "ALTER TABLE oauth_clients ALTER COLUMN token_endpoint_auth_method SET NOT NULL"
        )


def _reconcile_check_constraint(bind) -> None:
    """The final word on the constraint, judged against the *reconciled* types.

    `_drop_stale_named_check` already cleared anything non-canonical, but it
    judged against the column types as they were *then*. The type may have
    changed since, which re-renders a constraint Postgres rebuilt for us, so the
    comparison is made again here against the canonical rendering for the types
    the column now has.
    """
    canonical = _canonical_constraintdef(bind)
    existing = _existing_named_check(bind)

    if existing is not None:
        validated, definition = existing
        if validated and _normalize(definition) == canonical:
            # 010 (or a previous 013) already did it -- or `ALTER ... TYPE`
            # rebuilt 010's constraint canonically for the repaired type. Leave
            # the comment alone: its presence or absence is what downgrade()
            # reads, and 013 adding nothing here must not claim ownership.
            return
        # Backstop. Reaching it means a rebuild landed on a rendering the server
        # does not produce for a freshly created constraint; the data was
        # verified above, so replacing it in this transaction is safe.
        op.execute(f"ALTER TABLE oauth_clients DROP CONSTRAINT {CONSTRAINT_NAME}")

    op.create_check_constraint(CONSTRAINT_NAME, "oauth_clients", CANONICAL_PREDICATE)
    # COMMENT ON takes a string *literal*, not a bind parameter, so the marker
    # is escaped and inlined. It is a module constant we control, but escaping
    # it keeps the next person from inlining something they don't.
    marker = MARKER_COMMENT.replace("'", "''")
    op.execute(
        f"COMMENT ON CONSTRAINT {CONSTRAINT_NAME} ON oauth_clients IS '{marker}'"
    )


def _set_not_null(columns) -> None:
    for table, column, default_expr in columns:
        op.execute(
            f"UPDATE {table} SET {column} = {default_expr} WHERE {column} IS NULL"
        )
        # Idempotent in Postgres: SET NOT NULL on an already-NOT NULL column is
        # accepted and does nothing.
        op.execute(f"ALTER TABLE {table} ALTER COLUMN {column} SET NOT NULL")


def upgrade() -> None:
    bind = op.get_bind()

    # Fail fast instead of queueing behind a long-lived transaction: the deploy
    # migrates before recreating the container, so a stalled migration is a
    # stalled deploy while the old container is still serving. Per statement and
    # per lock acquisition, not a budget for the whole transaction.
    op.execute("SET LOCAL lock_timeout = '10s'")
    op.execute("SET LOCAL statement_timeout = '60s'")

    # Child tables first, before `oauth_clients` is locked, so the migration
    # never holds the parent lock while requesting a child's -- same direction
    # as the OAuth token exchange. See the module docstring.
    _set_not_null(OTHER_NOT_NULL_COLUMNS)

    # Held for the rest of the transaction: blocks INSERT/UPDATE/DELETE (but
    # not SELECT) on oauth_clients, so no row can slip in between the offender
    # check and ADD CONSTRAINT.
    op.execute("LOCK TABLE oauth_clients IN SHARE ROW EXCLUSIVE MODE")

    # Order is load-bearing, twice over. Verify the rows the CHECK reads before
    # touching anything that feeds it -- and clear a non-canonical constraint
    # off the name before `ALTER ... TYPE`, which would otherwise re-validate
    # that impostor against the live rows and abort the repair.
    _require_check_columns(bind)
    _assert_no_violating_rows(bind)
    _drop_stale_named_check(bind)
    _reconcile_oauth_clients_shape(bind)
    _reconcile_check_constraint(bind)
    _set_not_null(OAUTH_CLIENTS_NOT_NULL_COLUMNS)

    # `SET LOCAL` is scoped to the transaction, and alembic runs every pending
    # revision in *one* transaction -- so without this a future 014 would
    # silently inherit 013's 10s lock timeout and 60s statement timeout and
    # blame its own SQL when a long index build trips them.
    op.execute("RESET lock_timeout")
    op.execute("RESET statement_timeout")


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
