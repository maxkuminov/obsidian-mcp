## ADDED Requirements

### Requirement: Schema agrees with the ORM models

After migrating to head, `alembic check` SHALL report no pending operations, on a freshly created database and on the production database. The nine columns declared `NOT NULL` by the models (`api_keys.is_active`, `api_keys.created_at`, `notes_metadata.indexed_at`, `oauth_clients.created_at`, `oauth_codes.used`, `oauth_codes.created_at`, `oauth_tokens.revoked`, `oauth_tokens.created_at`, `usage_logs.created_at`) SHALL be `NOT NULL` in the database, and the CHECK constraint `ck_oauth_clients_auth_method_secret` SHALL exist on `oauth_clients` as a validated CHECK whose definition equals the canonical predicate (`(token_endpoint_auth_method = 'none' AND client_secret_hash IS NULL) OR (token_endpoint_auth_method = 'client_secret_post' AND client_secret_hash IS NOT NULL)`), verified in the catalog — not by name alone — and enforced (a public client with a secret, or a confidential client without one, SHALL be rejected on insert).

#### Scenario: Fresh database

- **WHEN** an empty database is migrated to head
- **THEN** `alembic check` SHALL report no new upgrade operations

#### Scenario: Drifted database is reconciled

- **WHEN** a database at revision 012 lacks the CHECK constraint and has the nine columns nullable, and migration 013 runs
- **THEN** the columns SHALL become `NOT NULL` (NULLs backfilled with the established defaults: booleans `true`/`false` as the model declares, timestamps `now()`), the constraint SHALL exist with the canonical predicate and be validated, negative inserts SHALL be rejected, and `alembic check` SHALL report no operations

#### Scenario: Same-named but wrong constraint is replaced

- **WHEN** `oauth_clients` carries a constraint named `ck_oauth_clients_auth_method_secret` whose definition differs from the canonical predicate (e.g. `CHECK (true)`) and 013 runs on data with no violations
- **THEN** 013 SHALL replace it with the exact predicate in the same transaction

#### Scenario: A wrong same-named constraint cannot block the column repair

- **WHEN** a constraint named `ck_oauth_clients_auth_method_secret` exists whose definition differs from the canonical rendering **for the column types the database currently has**, or which is `NOT VALID`, and `token_endpoint_auth_method` has also drifted to another type (e.g. `text` carrying a squatting `CHECK (pg_typeof(token_endpoint_auth_method) = 'text'::regtype)`)
- **THEN** 013 SHALL drop that constraint after verifying the rows and **before** altering the column's type, default or nullability — `ALTER COLUMN ... TYPE` re-validates every dependent CHECK against the live rows, so leaving the impostor in place would abort the repair on every run — and SHALL then add the canonical validated constraint, marked with its COMMENT so a downgrade to 012 removes it

#### Scenario: Other 010 effects verified

- **WHEN** 013 runs
- **THEN** `oauth_clients.client_secret_hash` SHALL be nullable and `token_endpoint_auth_method` SHALL be `character varying(32)` NOT NULL with default `client_secret_post`, reconciled if drifted, where the default is compared **exactly** against the server's own rendering of it (`pg_get_expr` on `pg_attrdef` versus a canonical default derived from a scratch `TEMP` table) — not by substring, which would accept `'not_client_secret_post'`

### Requirement: Reconciliation is idempotent and fails loudly on conflicting data

Migration 013 SHALL be safe to run on a database that already satisfies the target state, and SHALL NOT delete or modify rows to satisfy the CHECK constraint: if any `oauth_clients` row violates the predicate, the migration SHALL fail before altering the schema with a message naming the offending `client_id`s. The offender check SHALL run over the raw rows **before** any change to a column the predicate reads, and SHALL count a NULL `token_endpoint_auth_method` as a violation rather than backfilling it. If `token_endpoint_auth_method` is absent entirely the migration SHALL refuse rather than add it.

#### Scenario: Already reconciled

- **WHEN** 013 runs again on a database that already has the constraint and NOT NULL columns (the throwaway database is stamped back to 012 and upgraded, so the revision genuinely re-executes)
- **THEN** it SHALL succeed without error and change nothing

#### Scenario: Data check cannot race an insert

- **WHEN** 013 verifies `oauth_clients` for violating rows
- **THEN** it SHALL hold a table lock that blocks concurrent DML for the remainder of its transaction, under bounded `lock_timeout`/`statement_timeout`, and it SHALL `RESET` both before returning so a later revision in the same alembic transaction does not inherit them

#### Scenario: Locks are taken child-first

- **WHEN** 013 backfills and tightens the nine columns
- **THEN** it SHALL complete the child/other tables (`api_keys`, `notes_metadata`, `oauth_codes`, `oauth_tokens`, `usage_logs`) before locking `oauth_clients`, matching the application's child→parent order, so a concurrent OAuth request queues behind the migration; the residual behaviour under contention SHALL be a whole-transaction rollback and a retried deploy, not a partially applied migration

#### Scenario: NULL auth method is an offender, not a backfill

- **WHEN** an `oauth_clients` row has a NULL `token_endpoint_auth_method` (possible on a drifted database, where the CHECK passes because the predicate evaluates to NULL) and 013 runs
- **THEN** the migration SHALL name that `client_id` and fail, leaving the row's `token_endpoint_auth_method` NULL and the schema unchanged

#### Scenario: Downgrade preserves a pre-existing constraint

- **WHEN** a fresh 001→013 database is downgraded to 012
- **THEN** the CHECK created by 010 SHALL remain; on a drifted database where 013 created it (marked by its COMMENT) the downgrade SHALL drop it

#### Scenario: Violating rows present

- **WHEN** an `oauth_clients` row has `token_endpoint_auth_method = 'none'` with a non-null `client_secret_hash` (or vice versa) and 013 runs
- **THEN** the migration SHALL raise an error naming that `client_id` and SHALL leave the schema and rows unchanged
