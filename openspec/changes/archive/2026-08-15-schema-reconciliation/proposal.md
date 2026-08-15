## Why

`alembic check` against the live database (and against a freshly migrated
throwaway one) reports drift, surfaced by the alembic 1.19 bump (#49): nine
columns the ORM declares `NOT NULL` were left nullable by their migrations
(`api_keys.is_active/created_at`, `notes_metadata.indexed_at`,
`oauth_clients.created_at`, `oauth_codes.used/created_at`,
`oauth_tokens.revoked/created_at`, `usage_logs.created_at`), and the CHECK
constraint `ck_oauth_clients_auth_method_secret` declared by migration 010 and
the model is **absent on the live database** even though `alembic_version` is
past 010. The constraint is a real integrity guard (public OAuth clients must
not carry a secret; confidential ones must). Drift also means the next
autogenerate would emit noise, and a clean `alembic check` is the cheapest
regression gate for schema/model agreement. Issue #53.

## What Changes

- New migration `013_schema_reconciliation`: for each of the nine columns,
  backfill NULLs with the model's server default (`true`/`false`/`now()` as
  appropriate; `indexed_at` → `now()`), then `ALTER … SET NOT NULL`; create
  `ck_oauth_clients_auth_method_secret` **only if absent**, after verifying no
  row violates it — if any row does, the migration SHALL fail with a message
  listing the offending `client_id`s rather than dropping or mutating rows.
  Idempotent: safe on a database that already has the constraint / NOT NULLs.
- Downgrade: drop the constraint if we created it; leave NOT NULL in place
  (relaxing nullability on downgrade would re-create the drift the change
  exists to remove — documented).
- New CI-style gate: an opt-in integration test (reusing the
  `PGVECTOR_TEST_ADMIN_URL` throwaway-DB harness) that migrates from empty to
  head and asserts `alembic check` reports no diffs; and a second case that
  simulates the live drift (create the schema at 012 minus the constraint and
  with nullable columns) and asserts 013 reconciles it.
- `CLAUDE.md`: note that `alembic check` must be clean and how to run it.

## Capabilities

### New Capabilities

- `schema-integrity`: the database schema SHALL agree with the ORM models
  (`alembic check` clean) and reconciliation migrations SHALL be idempotent and
  fail loudly instead of mutating conflicting rows.

### Modified Capabilities

_None._

## Impact

- `alembic/versions/013_schema_reconciliation.py`, `tests/integration/test_schema_check.py`, `CLAUDE.md`, `Makefile` (optional `make db-check` target running `alembic check` in the container).
- Deploy: `make deploy` runs the migration before recreating the container; the migration is a metadata-only change on small tables (no rewrite of `usage_logs` rows is needed since no NULLs exist — verified on the live DB: 0 NULLs in all nine columns, 0 CHECK violations across 16 clients).
- Adversarial-Codex trigger: data-integrity migration on a live database.
