## ADDED Requirements

### Requirement: Schema agrees with the ORM models

After migrating to head, `alembic check` SHALL report no pending operations, on a freshly created database and on the production database. The nine columns declared `NOT NULL` by the models (`api_keys.is_active`, `api_keys.created_at`, `notes_metadata.indexed_at`, `oauth_clients.created_at`, `oauth_codes.used`, `oauth_codes.created_at`, `oauth_tokens.revoked`, `oauth_tokens.created_at`, `usage_logs.created_at`) SHALL be `NOT NULL` in the database, and the CHECK constraint `ck_oauth_clients_auth_method_secret` SHALL exist.

#### Scenario: Fresh database

- **WHEN** an empty database is migrated to head
- **THEN** `alembic check` SHALL report no new upgrade operations

#### Scenario: Drifted database is reconciled

- **WHEN** a database at revision 012 lacks the CHECK constraint and has the nine columns nullable, and migration 013 runs
- **THEN** the columns SHALL become `NOT NULL` (NULLs backfilled with the model's server default), the constraint SHALL exist, and `alembic check` SHALL report no operations

### Requirement: Reconciliation is idempotent and fails loudly on conflicting data

Migration 013 SHALL be safe to run on a database that already satisfies the target state, and SHALL NOT delete or modify rows to satisfy the CHECK constraint: if any `oauth_clients` row violates the predicate, the migration SHALL fail before altering the schema with a message naming the offending `client_id`s.

#### Scenario: Already reconciled

- **WHEN** 013 runs on a database that already has the constraint and NOT NULL columns
- **THEN** it SHALL succeed without error

#### Scenario: Violating rows present

- **WHEN** an `oauth_clients` row has `token_endpoint_auth_method = 'none'` with a non-null `client_secret_hash` (or vice versa) and 013 runs
- **THEN** the migration SHALL raise an error naming that `client_id` and SHALL leave the schema and rows unchanged
