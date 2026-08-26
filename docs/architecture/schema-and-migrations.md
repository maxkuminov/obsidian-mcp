# Schema drift and migrations

> Deep rationale extracted from `CLAUDE.md`. Read before writing or reviewing a migration.

## Schema drift is not only what `alembic check` sees

`alembic check` autogenerates against the live database and reports what it
would emit. It compares tables, columns, nullability and indexes — it does
**not** compare CHECK constraint predicates. So it is the cheap gate, not the
whole gate.

Issue #53 found both kinds at once. The models declare nine columns NOT NULL
that their migrations left nullable (`api_keys.is_active/created_at`,
`notes_metadata.indexed_at`, `oauth_clients.created_at`,
`oauth_codes.used/created_at`, `oauth_tokens.revoked/created_at`,
`usage_logs.created_at`) — `alembic check` sees that, and a freshly migrated
001→012 database has it too, so it was a migration bug. Separately, the CHECK
`ck_oauth_clients_auth_method_secret` that migration 010 creates was **absent
on the live database** while `alembic_version` read 012 — `alembic check`
cannot see that at all, and neither can a lookup by constraint name, which a
same-named `CHECK (true)` would satisfy while enforcing nothing.

Migration `013_schema_reconciliation` fixes both and enforces the *complete*
010 shape rather than just the missing piece, because how the live database
lost the constraint is unknown. Its rules, all load-bearing:

- The constraint is resolved through `pg_constraint` — `conrelid`, `contype`,
  `convalidated`, and `pg_get_constraintdef` compared against the canonical
  predicate — never by name. The canonical rendering is derived at runtime from
  an empty scratch table (`CREATE TEMP TABLE … (LIKE oauth_clients)`) so it is
  the server's own normalization, not a hand-written string pinned to one
  Postgres major.
- The server default is compared **exactly** — `pg_get_expr` on `pg_attrdef`
  against a canonical default derived the same way, by setting it on a scratch
  `TEMP` table and reading it back. A substring test would accept
  `'not_client_secret_post'`, and autogenerate does not compare server defaults
  at all, so this comparison is the only thing that sees that drift.
- **Rows are never mutated to satisfy a constraint, and nothing the constraint
  reads is written before the constraint has been verified.** The offender
  query runs over the raw rows *first*, and a NULL `token_endpoint_auth_method`
  counts as an offender. **Do not "simplify" this by backfilling the NULL to
  `client_secret_post` before the check** — that was the first draft and review
  caught it. A CHECK passes when its predicate is NULL, so such a row really
  can exist on a drifted database; backfilling it manufactures a passing row,
  i.e. invents an auth method for a client and lets it authenticate with
  whatever secret it happens to carry. Only after the check passes does 013
  touch the column's type/default/NOT NULL — declarative fixes that write no
  row value, and `SET NOT NULL` then needs no backfill because a NULL would
  already have raised. A violating row makes the migration raise, naming the
  `client_id`s; the transaction rolls back and the deploy aborts before the
  container is recreated. If the column is *absent*, 013 refuses rather than
  adding it — 010 always adds it, so its absence means guessing an auth method
  for every existing client.
- `LOCK TABLE oauth_clients IN SHARE ROW EXCLUSIVE MODE` is held from before
  the offender check to the end, so no insert can land between the check and
  `ADD CONSTRAINT`. `lock_timeout=10s` / `statement_timeout=60s` make a
  blocked migration fail fast instead of stalling the deploy; they are **per
  statement / per lock acquisition**, not a budget for the transaction. 013
  `RESET`s both at the end of `upgrade()`: `SET LOCAL` lasts for the
  transaction, and `alembic/env.py` runs *every* pending revision in one, so a
  later revision would otherwise inherit them. 014 does the same for the same
  reason.
- **Lock order is child-first.** The five other tables are backfilled and set
  NOT NULL before `oauth_clients` is locked, matching the app's own direction
  (`src/oauth/routes.py` locks `oauth_codes` `FOR UPDATE`, then reads
  `oauth_clients`, then inserts `oauth_tokens`), so a concurrent OAuth request
  queues behind the migration instead of closing a wait cycle with it. That is
  an ordering guarantee, not an absence of contention: the residual behaviour
  is rollback-and-retry — past `lock_timeout` the whole transaction aborts and
  the deploy fails before the container is recreated. Re-run when quiet.
- Downgrade drops the constraint **only if it carries 013's COMMENT marker**.
  A fresh 001→013 database got its constraint from 010, so downgrading to 012
  must keep it. NOT NULL is never relaxed on downgrade — that would re-create
  the drift the migration exists to remove.
- The scratch `TEMP` tables mean the migration role needs the **TEMP
  privilege** on the database. The deploy's owner role has it; a locked-down
  migration role may not.

`tests/integration/test_schema_check.py` is the gate, and **`make test-schema`
is how you run it** — throwaway `pgvector/pgvector:pg16` up, module, container
removed pass or fail. Run it before any deploy that carries a migration; it
never reads the deploy `.env` or touches the live database. The target sets
`OMCP_REQUIRE_SCHEMA_INTEGRATION=1`, which turns the module's opt-in
`PGVECTOR_TEST_ADMIN_URL` skip into a hard failure — a gate that silently skips
because nobody exported the URL is not a gate. (Plain `pytest tests/` still
skips it, which is what you want on a machine with no Postgres.) It asserts
`alembic check` clean *and* the catalog directly *and* that the two forbidden
inserts are actually rejected, across fresh / drifted / impostor-constraint /
violating-row / NULL-method / missing-column / wrong-default / wrong-type /
`client_secret_hash NOT NULL` / nullable-method / stamp-back / downgrade paths.
Idempotence is exercised by `alembic stamp 012` then `upgrade head`, not by a
second `upgrade head` — the latter is a no-op at the alembic level and proves
nothing about the migration body.

The same module also gates **014**'s backfill, which `alembic check` is blind to
in a different way: it sees the column, its NOT NULL and its index, and nothing
about *which rows got which grant*. So the 014 cases assert the grouping
directly — one family per `(client_id, user_id)`, never a family spanning two
users, never a user's rows split across two families — plus stamp-back
idempotence (which must not re-stamp existing `grant_id`s) and the downgrade.

