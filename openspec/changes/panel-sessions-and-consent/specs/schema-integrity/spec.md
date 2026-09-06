## ADDED Requirements

### Requirement: Migration 024 SHALL own the `user_sessions` table as a marked unit and SHALL reconcile rather than adopt

Migration 024 SHALL create the `user_sessions` table and its indexes, SHALL stamp its own comment marker on the table in the same transaction as the create, and SHALL write no rows. The marker SHALL be mirrored in `src/models/db.py` so `alembic check` compares it like any other attribute; a marker that drifts from the model shows up as a pending alteration rather than as silence.

There is nothing to backfill: a session that predates the table has no identifier the registry could resolve, and inventing rows for existing cookies would grandfather exactly the credentials the change exists to invalidate.

The table SHALL carry a primary key holding the one-way hash of the session identifier, a `user_id` foreign key to `users.id` with `ON DELETE CASCADE`, creation, last-seen and expiry timestamps that are not nullable, a nullable revocation timestamp, and a nullable user-agent hash. The cascade is load-bearing: a permanent user delete removes the sessions with no handler code.

Where the table already exists, the migration SHALL **verify the complete shape it would have created** — every column's type and nullability, the foreign key's delete action resolved through the catalog rather than by constraint name, and each index resolved through the index catalog — and SHALL **refuse** a foreign or partial shape rather than patching it. A table of that name created by something else is not this migration's table, and adopting it would leave the registry running against a schema nothing verified. Refusal SHALL name what disagreed.

`downgrade()` SHALL drop the table **only if it carries 024's marker**, so a same-named table this migration did not create survives a downgrade.

A stamp-back re-run SHALL preserve existing rows: the reconciliation path writes and deletes nothing, so live sessions survive a gate exercise rather than being silently signed out by it.

Migration 024's `down_revision` SHALL be the revision immediately preceding it in the merged history, and the migration SHALL NOT be merged ahead of that revision.

#### Scenario: Fresh database

- **WHEN** an empty database is migrated to head
- **THEN** `user_sessions` SHALL exist with its primary key, its `user_id` foreign key carrying `ON DELETE CASCADE`, its indexes and its comment marker
- **AND** `alembic check` SHALL report no new upgrade operations

#### Scenario: The migration writes nothing

- **WHEN** migration 024 runs against a database with existing users
- **THEN** `user_sessions` SHALL contain no rows

#### Scenario: Deleting a user removes their sessions

- **WHEN** a user row with session rows is deleted
- **THEN** those session rows SHALL be removed by the cascade

#### Scenario: A foreign table of the same name is refused

- **WHEN** a table named `user_sessions` exists that this migration did not create
- **THEN** the migration SHALL refuse and SHALL name what disagreed
- **AND** it SHALL NOT alter that table

#### Scenario: A partial shape is refused

- **WHEN** a table named `user_sessions` exists carrying the marker but missing a column, an index, or the foreign key's delete action
- **THEN** the migration SHALL refuse rather than patch it

#### Scenario: A stamp-back re-run preserves seeded rows

- **WHEN** rows exist in `user_sessions`, the database is stamped back to the preceding revision, and it is upgraded again
- **THEN** those rows SHALL still be present

#### Scenario: Downgrade drops only its own table

- **WHEN** the migration is downgraded and the table carries 024's marker
- **THEN** `user_sessions` SHALL no longer exist

#### Scenario: Downgrade preserves a table it did not create

- **WHEN** the migration is downgraded and the table does not carry 024's marker
- **THEN** the migration SHALL refuse to drop it

## MODIFIED Requirements

### Requirement: The schema gate covers both migrations of this wave before deploy
The schema gate SHALL exercise each migration of the current wave on a throwaway database, in the same run, and SHALL assert `alembic check` clean at the resulting head. The head revision the gate asserts SHALL be **`024`**, so a later migration added without updating the gate fails loudly rather than silently widening what "head" means. (The requirement keeps its original heading so this block modifies the existing requirement rather than adding a second one; "this wave" now means the migrations current at head, not the 016/017 pair it was first written for.)

`024` is the session-registry migration of this change. It is ordered **after `023`**, the migration belonging to the sibling `index-integrity-hardening` change: `024`'s `down_revision` is `"023"`, so `024` cannot be merged or migrated ahead of it, and the gate's head assertion SHALL move to `024` only once `023` is present in the merged history. If `023` is abandoned, `024` SHALL be rebased onto the then-current head before merge and this requirement updated to name it.

Idempotence SHALL be exercised by stamping the revision back and upgrading again, not by a second `upgrade head` — the latter is a no-op at the alembic level and proves nothing about the migration body.

#### Scenario: Head at 024

- **WHEN** a throwaway database is migrated to head
- **THEN** `alembic_version` SHALL read `024`
- **AND** `alembic check` SHALL report no new upgrade operations

#### Scenario: The ordering against the sibling migration holds

- **WHEN** the merged migration history is inspected
- **THEN** `024`'s recorded predecessor SHALL be `023`
- **AND** migrating to head SHALL apply `023` before `024`

#### Scenario: Idempotence is exercised by stamping back

- **WHEN** the gate tests that a migration of this wave can re-run
- **THEN** it SHALL stamp the database to the preceding revision and upgrade again, so the migration body genuinely re-executes
