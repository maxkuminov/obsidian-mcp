## MODIFIED Requirements

### Requirement: The schema gate covers both migrations of this wave before deploy

The schema gate SHALL exercise every migration whose behaviour it asserts, including both migrations of the current wave, on a throwaway database in the same run, and SHALL assert `alembic check` clean at the resulting head. The gate SHALL carry a single literal naming the current head revision and SHALL assert that a migrated database reads exactly that literal. The current head literal SHALL be **`025`**, with **`024` present in the applied chain**. The requirement keeps its original heading so this block modifies the existing requirement rather than adding a second one; the current wave is not limited to the 016/017 pair for which it was first written.

`025` is the OAuth client use-marker migration of `asvs-edge-hardening`. `025`'s recorded predecessor SHALL be `024`, and migration to head SHALL apply `024` before `025`. Raising the asserted head is a required part of adding a migration: a later migration added without updating the gate SHALL fail the head assertion rather than silently widening what "head" means. A second requirement naming a different head SHALL NOT be introduced alongside this one — two requirements disagreeing about the head is the exact contradiction the single literal exists to prevent.

The gate module carrying this literal is the one `make test-schema` invokes. A new migration's marker, drift, downgrade and stamp-back cases SHALL live in that module, not in a separate module the gate does not run.

The historical checks SHALL be kept, not replaced. The cases covering 013, 014, 016, 017, 022, 023 and 024 SHALL continue to execute alongside the 025 cases; raising the head SHALL NOT remove the earlier reconciliations' coverage.

Idempotence SHALL be exercised by stamping the revision back and upgrading again, not by a second `upgrade head` — the latter is a no-op at the alembic level and proves nothing about the migration body.

#### Scenario: The gate asserts the current head and 024 is in the chain

- **WHEN** a throwaway database is migrated to head
- **THEN** `alembic_version` SHALL read the single head revision the gate module names
- **AND** `024` SHALL be one of the revisions that ran to reach it
- **AND** `alembic check` SHALL report no new upgrade operations

#### Scenario: The earlier waves' cases still run

- **WHEN** the gate runs at the new head
- **THEN** the cases covering 013, 014, 016, 017, 022, 023 and 024 SHALL all execute and pass
- **AND** none SHALL have been removed in the course of raising the head

#### Scenario: The new migration's cases run inside the gate

- **WHEN** `make test-schema` runs
- **THEN** 025's marker, drift, downgrade and stamp-back cases SHALL be among the tests it executes

#### Scenario: Idempotence is exercised by stamping back

- **WHEN** the gate tests that any covered migration can re-run
- **THEN** it SHALL stamp the database to the preceding revision and upgrade again, so the migration body genuinely re-executes

## ADDED Requirements

### Requirement: Migration 025 owns the OAuth client use marker as one marked unit
Migration 025 SHALL add a single nullable timestamp column recording when an OAuth client was last used, SHALL mark the column it creates with an ownership marker recorded in the database and mirrored byte-identically in `src/models/db.py`, and SHALL refuse a pre-existing column of any other shape rather than adopting it. The column MUST be nullable, MUST carry no server default, and its downgrade MUST drop it only if it carries that marker.

The column exists because no sound signal for "this client has never been used" is derivable from the existing schema, and the reasons are worth recording so the column is not later removed as redundant. A client's owner is NULL for every client in a single-user deployment, so it cannot mean "unused". A used authorization code is deleted the moment it is spent and a token seven days after it expires, so the absence of child rows cannot mean "unused" either.

#### Scenario: The column is created and marked
- **WHEN** the migration runs against a database whose `oauth_clients` table lacks the column
- **THEN** it SHALL add a nullable timestamp column with no server default, carrying the ownership marker

#### Scenario: A pre-existing column of another shape is refused
- **WHEN** the column already exists with a different type, with a NOT NULL constraint, or with a server default
- **THEN** the migration SHALL fail, naming the column and what was found, and change nothing

#### Scenario: A marked column is adopted
- **WHEN** the column already exists with the exact shape and the ownership marker
- **THEN** the migration SHALL proceed without recreating it

#### Scenario: Downgrade leaves a column it did not create
- **WHEN** a downgrade runs and the column does not carry the ownership marker
- **THEN** the column SHALL NOT be dropped
- **AND** the downgrade SHALL fail, naming the unmarked column

#### Scenario: The schema agrees with the model afterwards
- **WHEN** `alembic check` runs after the migration has been applied
- **THEN** it SHALL report no new upgrade operations

### Requirement: The use-marker backfill leaves no pre-existing row unmarked
Migration 025 SHALL set a non-NULL use marker on **every** `oauth_clients` row that exists when it runs. Where surviving authorization-code or token rows belong to the client, the marker SHALL be the newest of their creation times; where none survive, it SHALL be the migration's own timestamp. It MUST NOT leave any pre-existing row NULL, MUST NOT overwrite a marker that is already present, and MUST be safe to run again.

The invariant this buys is the whole point: after 025, a NULL marker means "registered after 025 and never used" and nothing else, so the expiry sweep only ever acts on rows whose entire history is visible to it. Leaving the ambiguous rows NULL instead would expose a client whose evidence was already purged — a confidential client whose credentials a developer configured by hand, which dynamic registration does not re-provision, since registering again mints a different identifier and secret. Stamping the migration's timestamp costs only that genuinely unused pre-025 registrations are never collected, which an operator can resolve in the panel.

#### Scenario: A client with surviving token rows is marked from them
- **WHEN** the migration runs against a client holding one or more token rows
- **THEN** its marker SHALL be set to the newest of those rows' creation times

#### Scenario: A client with surviving code rows is marked from them
- **WHEN** a client holds authorization-code rows but no token rows
- **THEN** its marker SHALL be set to the newest of those rows' creation times

#### Scenario: A client with no surviving child row is stamped, not left NULL
- **WHEN** a client has neither code nor token rows
- **THEN** its marker SHALL be set to the migration's own timestamp
- **AND** it SHALL NOT be left NULL, so the expiry sweep can never reach it

#### Scenario: No pre-existing row survives the migration unmarked
- **WHEN** the migration completes
- **THEN** no `oauth_clients` row that existed before it ran SHALL have a NULL marker

#### Scenario: Re-running the migration changes no marker
- **WHEN** the migration executes again against a database whose rows already carry markers
- **THEN** no existing marker SHALL be changed
