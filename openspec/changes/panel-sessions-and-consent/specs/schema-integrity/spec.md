## ADDED Requirements

### Requirement: Migration 024 owns the `user_sessions` table and SHALL create it without backfilling

Migration 024 SHALL create the `user_sessions` table and its indexes and SHALL write no rows. There is nothing to backfill: a session that predates the table has no identifier the registry could resolve, and inventing rows for existing cookies would grandfather exactly the credentials the change exists to invalidate.

The table SHALL carry a primary key holding the one-way hash of the session identifier, a `user_id` foreign key to `users.id` with `ON DELETE CASCADE`, creation, last-seen and expiry timestamps that are not nullable, a nullable revocation timestamp, and a nullable user-agent hash. The cascade is load-bearing: a permanent user delete removes the sessions with no handler code, and without it a deleted user's rows would outlive the account.

The model in `src/models/db.py` and the migration SHALL agree on every column, nullability, server default and index name, so that `alembic check` reports no new upgrade operations at the resulting head.

Migration 024's `down_revision` SHALL be the revision immediately preceding it in the merged history; the migration SHALL NOT be merged ahead of that revision.

#### Scenario: Fresh database

- **WHEN** an empty database is migrated to head
- **THEN** `user_sessions` SHALL exist with its primary key, its `user_id` foreign key carrying `ON DELETE CASCADE`, and its indexes
- **AND** `alembic check` SHALL report no new upgrade operations

#### Scenario: The migration writes nothing

- **WHEN** migration 024 runs against a database with existing users
- **THEN** `user_sessions` SHALL contain no rows

#### Scenario: Deleting a user removes their sessions

- **WHEN** a user row with session rows is deleted
- **THEN** those session rows SHALL be removed by the cascade

#### Scenario: Idempotence is exercised by stamping back

- **WHEN** the gate tests that 024 can re-run
- **THEN** it SHALL stamp the database to the preceding revision and upgrade again, so the migration body genuinely re-executes

#### Scenario: Downgrade removes the table

- **WHEN** the migration is downgraded
- **THEN** `user_sessions` SHALL no longer exist

### Requirement: The schema gate SHALL assert the new head before this change is deployed

The schema gate's recorded head revision SHALL be updated to the head produced by this change, so that a later migration added without updating the gate fails loudly rather than silently widening what "head" means. The gate SHALL be run on a throwaway database before any deploy that carries this migration.

#### Scenario: Head assertion moves with the migration

- **WHEN** a throwaway database is migrated to head
- **THEN** the recorded revision SHALL equal the gate's declared head
- **AND** `alembic check` SHALL report no new upgrade operations
