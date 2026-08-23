## ADDED Requirements

### Requirement: Migration 016 owns the indexed-root column and its backfill
Migration 016 SHALL add `users.indexed_vault_path` as a nullable `character varying(1024)` with no server default, SHALL backfill it from `users.vault_path` for every row whose `vault_path` is not null, SHALL leave it null for every row whose `vault_path` is null, and SHALL guard the backfill so that a re-run cannot overwrite a value the indexer has since written. `downgrade()` SHALL drop the column. After migrating to head, `alembic check` SHALL report no pending operations.

The backfill asserts a fact the indexer's own scoping rule guarantees — only a user with a non-null `vault_path` is ever indexed, so an assigned user's rows were built from the root currently assigned. It asserts nothing about an unassigned user, whose previous root was never recorded anywhere; such an account is left null and therefore gets one reassignment without reconciliation. That is a one-time consequence of introducing the column, not a rule, and it MUST NOT be closed by guessing a root.

#### Scenario: Fresh database

- **WHEN** an empty database is migrated to head
- **THEN** `users.indexed_vault_path` SHALL exist as nullable `character varying(1024)` with no server default
- **AND** `alembic check` SHALL report no new upgrade operations

#### Scenario: Backfill on a populated database

- **WHEN** 016 runs on a database holding assigned and unassigned users
- **THEN** every assigned user's `indexed_vault_path` SHALL equal that user's own `vault_path`
- **AND** every unassigned user's `indexed_vault_path` SHALL be null

#### Scenario: Re-running the migration does not overwrite a stamp

- **WHEN** the database is stamped back to 015 and upgraded again, after the indexer has recorded a root that differs from the current `vault_path`
- **THEN** the recorded root SHALL be left unchanged

#### Scenario: Downgrade

- **WHEN** a database at 016 is downgraded to 015
- **THEN** the column SHALL be dropped
- **AND** no other column SHALL be altered
