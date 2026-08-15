## Context
Live DB: `alembic_version` = 012; the nine columns are nullable; the CHECK from 010 is missing (probably 010 was applied by hand or stamped). Measured 2026-08-15: 0 NULLs in every affected column, 0 rows violating the CHECK across 16 `oauth_clients`. Fresh throwaway DBs migrated 001→012 have the CHECK but the same nullability drift, so the nullability part is a migration bug, the CHECK part is live-only drift.

## Goals / Non-Goals
Goals: `alembic check` clean on fresh and live DBs; migration idempotent; never silently mutate/drop conflicting rows. Non-goals: changing model definitions; touching `transfer_tokens`.

## Decisions
1. **Backfill-then-SET NOT NULL per column, each guarded.** Use the model's server default for the backfill (`is_active`→true, `used`/`revoked`→false, `created_at`/`indexed_at`→`now()`); on the live DB this is a no-op. Idempotency: `ALTER COLUMN … SET NOT NULL` is idempotent in Postgres.
2. **CHECK constraint: verify, then create if absent.** `SELECT client_id FROM oauth_clients WHERE NOT (<predicate>)`; if any → `raise RuntimeError` listing them (operator decides); else `CREATE CONSTRAINT` guarded by a `pg_constraint` existence check. Never `DELETE`.
3. **Downgrade** drops the CHECK only if this migration created it (record via a comment/`pg_constraint` check); leaves NOT NULL.
4. **Gate:** integration test asserting `alembic check` clean after `upgrade head` on a throwaway DB, plus a drift-simulation case (apply 001→012, `ALTER TABLE oauth_clients DROP CONSTRAINT`, run 013, assert clean, and a violating-row variant asserting the loud failure).

## Risks / Trade-offs
- [Live rows violate the CHECK at deploy time] → migration fails loudly before altering anything; deploy aborts (make deploy migrates before recreating the container, so the old container keeps serving). Measured 0 today.
- [SET NOT NULL locks the table] → tables are small; `usage_logs` is the largest (~10k rows) — sub-second.
- [autogenerate still noisy for other reasons] → the test tells us.

## Migration Plan
`make deploy` (migrate step). Post-deploy: `docker exec obsidian-mcp alembic check` → "No new upgrade operations detected."; `pg_constraint` shows the CHECK. Rollback: `alembic downgrade 012` (drops CHECK only).
