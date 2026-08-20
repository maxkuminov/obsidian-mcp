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

**Split a family.** The backfill is a *partition* of rows that have no grant,
and that is only true when this migration created the column — then every row
is NULL and the grouping covers all of them. If the column **pre-exists**,
`WHERE grant_id IS NULL` stops being a partition and becomes a patch: a NULL
row beside a stamped sibling would get a **fresh** id, turning one grant into
two, so revoking either would leave the other alive. That is the exact defect
014 exists to remove, reintroduced by the migration.

There is no safe repair for that state — the right id for a NULL row is
whatever its siblings carry, and "sibling" is only definable through the
grouping we would be inventing. So, in 013's spirit, a pre-existing column is
*verified, not adopted*: it must be `character varying(64)`, our index name
must be free or already on it, no row may be NULL, and no existing `grant_id`
may span more than one `(client_id, user_id)` — that last one because every
family operation resolves a family by `grant_id` alone, so adopting a
cross-owner id would let one user's Revoke reach another's grant. Any of those
raises, names the offending rows, and the whole transaction rolls back.

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

# How PostgreSQL prints that type. A pre-existing column of any other shape is
# refused rather than adopted -- see `_require_pre_existing_column_is_ours`.
EXPECTED_TYPE = "character varying(64)"


def _column_shape(bind):
    """`(attnotnull, formatted_type)` for `grant_id`, or None if absent."""
    return bind.execute(
        sa.text(
            "SELECT a.attnotnull, format_type(a.atttypid, a.atttypmod) "
            "FROM pg_attribute a "
            "WHERE a.attrelid = CAST(:table AS regclass) AND a.attname = :column "
            "  AND a.attnum > 0 AND NOT a.attisdropped"
        ),
        {"table": TABLE, "column": COLUMN},
    ).first()


def _table_oid(bind) -> int:
    """The OID `oauth_tokens` resolves to, through the session's search_path.

    Every other statement in this migration names the table unqualified, so
    they all resolve the same way. Taking the OID once and keying the catalog
    lookups off it is what keeps the checks talking about the same object the
    DDL will touch.
    """
    return bind.execute(
        sa.text("SELECT CAST(:table AS regclass)::oid"), {"table": TABLE}
    ).scalar_one()


def _existing_index(bind, table_oid: int):
    """Everything we need to judge a pre-existing index of our name.

    `(indrelid, indisvalid, is_partial, has_expressions, key_attnums)`, or None
    when the name is free **in the table's own schema**.

    Two rounds of narrowing got this here. The first version asked only "which
    column is it on?" and matched with `attnum = ANY(indkey)`, which accepts an
    impostor several ways over: a *partial* index (`WHERE token_type='access'`)
    covers a subset of rows, an *expression* index (`lower(grant_id)`) cannot
    serve an equality lookup on the column, a *multi-column* index leads with
    the wrong key, and an INVALID leftover from a failed `CREATE INDEX
    CONCURRENTLY` is not usable at all. Each would be kept by `CREATE INDEX IF
    NOT EXISTS` and leave `alembic check` permanently dirty while looking
    installed, because autogenerate compares index *names*.

    The second was that the lookup matched `relname` across **every schema**.
    An unrelated `shadow.ix_oauth_tokens_grant_id` is not our index and cannot
    collide with anything we create — `CREATE INDEX` places the index in its
    table's schema — yet it would be found and the migration would refuse a
    database that is perfectly fine. So the index is resolved by OID in the
    namespace of the table we just resolved, which is exactly the name
    `CREATE INDEX IF NOT EXISTS` tests against.

    More than one row is impossible (`pg_class` is unique on
    `(relname, relnamespace)`) and is nevertheless treated as fatal rather than
    silently taking the first: if that assumption ever stops holding, guessing
    which object the DDL will hit is the wrong response.

    `indkey` is an int2vector; `string_to_array(indkey::text, ' ')` is the
    portable way to read it as an array. A zero entry means an expression
    column, which `indexprs IS NOT NULL` already catches.
    """
    rows = bind.execute(
        sa.text(
            "SELECT i.indrelid, "
            "       i.indisvalid, "
            "       (i.indpred IS NOT NULL) AS is_partial, "
            "       (i.indexprs IS NOT NULL) AS has_expressions, "
            "       string_to_array(i.indkey::text, ' ')::int[] AS key_attnums "
            "FROM pg_index i "
            "JOIN pg_class c ON c.oid = i.indexrelid "
            "WHERE c.relname = :index "
            "  AND c.relnamespace = ("
            "        SELECT relnamespace FROM pg_class WHERE oid = :table_oid)"
        ),
        {"index": INDEX, "table_oid": table_oid},
    ).all()
    if len(rows) > 1:
        raise RuntimeError(
            f"{len(rows)} relations named {INDEX} in the schema of {TABLE}. "
            "That should be impossible, and 014 will not guess which one "
            "`CREATE INDEX IF NOT EXISTS` would match. Nothing has been changed."
        )
    return rows[0] if rows else None


def _column_attnum(bind) -> int:
    """`pg_attribute.attnum` for `grant_id` — what `indkey` holds."""
    return bind.execute(
        sa.text(
            "SELECT attnum FROM pg_attribute "
            "WHERE attrelid = CAST(:table AS regclass) AND attname = :column "
            "  AND attnum > 0 AND NOT attisdropped"
        ),
        {"table": TABLE, "column": COLUMN},
    ).scalar_one()


def _require_pre_existing_column_is_ours(bind, shape) -> None:
    """A `grant_id` this migration did not create must already be 014's shape.

    Same philosophy as 013: reconcile a database that has the shape, refuse to
    guess for one that does not. A `text` or `varchar(32)` column under our
    name is not something we can widen and then trust, because we would be
    trusting values we did not write.
    """
    _, formatted_type = shape
    if formatted_type != EXPECTED_TYPE:
        raise RuntimeError(
            f"{TABLE}.{COLUMN} already exists as {formatted_type}, not "
            f"{EXPECTED_TYPE}. 014 reconciles a column it created; it will not "
            "adopt one of a different shape. Inspect it by hand, then re-run. "
            "Nothing has been changed."
        )

    table_oid = _table_oid(bind)
    existing = _existing_index(bind, table_oid)
    if existing is not None:
        indrelid, indisvalid, is_partial, has_expressions, key_attnums = existing
        expected = [_column_attnum(bind)]
        problem = None
        if indrelid != table_oid:
            # Same schema, same name, different table — only reachable if the
            # index was created on another relation and then that relation was
            # renamed into this one's place.
            problem = f"it indexes another relation, not {TABLE!r}"
        elif not indisvalid:
            problem = (
                "it is INVALID (a failed CREATE INDEX CONCURRENTLY leaves one "
                "behind); it cannot serve a lookup"
            )
        elif is_partial:
            problem = (
                "it is a partial index (has a WHERE clause), so it covers only "
                "some rows"
            )
        elif has_expressions:
            problem = "it indexes an expression, not the bare column"
        elif list(key_attnums) != expected:
            problem = (
                f"its key columns are attnums {list(key_attnums)}, not exactly "
                f"[{expected[0]}] ({COLUMN})"
            )
        if problem is not None:
            raise RuntimeError(
                f"index {INDEX} already exists but {problem}. "
                "`CREATE INDEX IF NOT EXISTS` would keep it and `alembic check` "
                "would stay dirty forever, because autogenerate compares index "
                "names. Drop or rename it by hand, then re-run. Nothing has "
                "been changed."
            )


def _assert_no_partial_stamping(bind) -> None:
    """A pre-existing column with NULLs is a *split family* waiting to happen.

    This is the case the first draft got wrong. If some rows carry a
    `grant_id` and their siblings do not, backfilling only the NULLs assigns
    those siblings a **fresh** id -- so one grant becomes two, and revoking
    either one leaves the other alive. That is precisely the defect 014 exists
    to remove, reintroduced by the migration itself.

    There is no safe repair: the correct id for a NULL row is whatever its
    siblings carry, but "sibling" is only definable through the grouping we
    would be inventing. So refuse, name the rows, and let a human decide.
    """
    offenders = bind.execute(
        sa.text(
            f"SELECT id FROM {TABLE} WHERE {COLUMN} IS NULL ORDER BY id LIMIT 20"
        )
    ).scalars().all()
    if not offenders:
        return
    total = bind.execute(
        sa.text(f"SELECT count(*) FROM {TABLE} WHERE {COLUMN} IS NULL")
    ).scalar_one()
    raise RuntimeError(
        f"{TABLE}.{COLUMN} already exists but {total} row(s) are NULL "
        f"(ids: {', '.join(str(i) for i in offenders)}"
        f"{', ...' if total > len(offenders) else ''}). 014 will not backfill "
        "them: a NULL beside a stamped sibling means assigning a fresh id and "
        "splitting one grant into two, so a revocation would miss half of it. "
        "Decide the correct grant_id for these rows by hand, or drop the "
        "column and let 014 create it. Nothing has been changed."
    )


def _assert_no_grant_spans_two_owners(bind) -> None:
    """One `grant_id` must mean one `(client_id, user_id)`.

    That invariant is what lets every family operation resolve a family as
    `grant_id == g` with no `user_id` predicate (see `src/oauth/grants.py`).
    A pre-existing column could carry ids that violate it -- and adopting them
    would make a single panel Revoke reach into another user's grant.

    Sentinel-free: `count(DISTINCT user_id)` ignores NULLs, so the third
    disjunct is what catches a mix of NULL and non-NULL owners.
    """
    offenders = bind.execute(
        sa.text(
            f"SELECT {COLUMN} FROM {TABLE} "
            f"WHERE {COLUMN} IS NOT NULL "
            f"GROUP BY {COLUMN} "
            "HAVING count(DISTINCT client_id) > 1 "
            "    OR count(DISTINCT user_id) > 1 "
            "    OR (count(user_id) > 0 AND count(user_id) < count(*)) "
            f"ORDER BY {COLUMN} LIMIT 20"
        )
    ).scalars().all()
    if offenders:
        raise RuntimeError(
            f"{len(offenders)} pre-existing {COLUMN} value(s) span more than one "
            f"(client_id, user_id): {', '.join(repr(g) for g in offenders)}. "
            "A grant family must belong to exactly one client and one user -- "
            "revocation and scope changes resolve a family by grant_id alone, "
            "so adopting these would let one user's Revoke reach another's "
            "grant. Fix them by hand, then re-run. Nothing has been changed."
        )


def upgrade() -> None:
    bind = op.get_bind()

    # Fail fast instead of queueing behind a long-lived transaction: the deploy
    # migrates before recreating the container, so a stalled migration is a
    # stalled deploy while the old container is still serving. Per statement and
    # per lock acquisition, not a budget for the whole transaction.
    op.execute("SET LOCAL lock_timeout = '10s'")
    op.execute("SET LOCAL statement_timeout = '60s'")

    shape = _column_shape(bind)
    if shape is None:
        # Nullable for the length of this transaction only. ADD COLUMN takes
        # ACCESS EXCLUSIVE and holds it to COMMIT, so the nullable window is
        # never observable by another session, and every row is NULL at this
        # point by construction -- which is what makes the backfill below a
        # partition rather than a patch.
        op.add_column(TABLE, sa.Column(COLUMN, COLUMN_TYPE, nullable=True))
    else:
        # The column pre-exists: this is a re-run, or somebody added it. 014
        # will complete a column it can *verify*, and refuse one it would have
        # to guess about. See each helper for what it refuses and why.
        _require_pre_existing_column_is_ours(bind, shape)
        _assert_no_partial_stamping(bind)
        _assert_no_grant_spans_two_owners(bind)

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
