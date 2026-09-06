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

The schema gate SHALL exercise every migration whose behaviour it asserts, including both migrations of the current wave, on a throwaway database in the same run, and SHALL assert `alembic check` clean at the resulting head. The gate SHALL carry a single literal naming the current head revision and SHALL assert that a migrated database reads exactly that literal. The current head literal SHALL be **`024`**, with **`023` present in the applied chain**. The requirement keeps its original heading so this block modifies the existing requirement rather than adding a second one; the current wave is not limited to the 016/017 pair for which it was first written.

`023` is the index-state migration of `index-integrity-hardening`; `024` is the session-registry migration of `panel-sessions-and-consent`. `024`'s recorded predecessor SHALL be `023`, and migration to head SHALL apply `023` before `024`. The head assertion moves to `024` only with `023` present in the merged history. If `023` is abandoned, `024` SHALL be rebased onto the then-current head before merge and this requirement updated to name it.

Raising the asserted head is a required part of adding a migration. A later migration added without updating the gate SHALL fail the head assertion, rather than silently widening what "head" means or treating `023` as a permanent head. The literal is the current chosen head, while each covered migration remains asserted in its applied chain.

The historical checks SHALL be kept, not replaced. The cases covering 013, 014, 016, 017 and 022 SHALL continue to execute alongside the 023 and 024 cases; raising the head SHALL NOT remove the earlier reconciliations' coverage.

Idempotence SHALL be exercised by stamping the revision back and upgrading again, not by a second `upgrade head` — the latter is a no-op at the alembic level and proves nothing about the migration body.

#### Scenario: The gate asserts the current head and 023 is in the chain

- **WHEN** a throwaway database is migrated to head
- **THEN** `alembic_version` SHALL read the single head revision the gate module names
- **AND** `023` SHALL be one of the revisions that ran to reach it
- **AND** `alembic check` SHALL report no new upgrade operations

#### Scenario: The earlier waves' cases still run

- **WHEN** the gate runs at the new head
- **THEN** the cases covering 013, 014, 016, 017 and 022 SHALL all execute and pass
- **AND** none SHALL have been removed in the course of raising the head

#### Scenario: Idempotence is exercised by stamping back

- **WHEN** the gate tests that any covered migration can re-run
- **THEN** it SHALL stamp the database to the preceding revision and upgrade again, so the migration body genuinely re-executes

#### Scenario: Head at 024

- **WHEN** a throwaway database is migrated to head
- **THEN** `alembic_version` SHALL read `024`
- **AND** `alembic check` SHALL report no new upgrade operations

#### Scenario: The ordering against the sibling migration holds

- **WHEN** the merged migration history is inspected
- **THEN** `024`'s recorded predecessor SHALL be `023`
- **AND** migrating to head SHALL apply `023` before `024`
