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

### Requirement: The use-marker backfill infers use only from rows that still exist
Migration 025 SHALL backfill the use marker from the newest surviving authorization-code or token row belonging to each client, and SHALL leave the marker NULL for a client with no such row. It MUST NOT infer a value from any weaker association, MUST NOT overwrite a marker that is already present, and MUST be safe to run again.

A client whose evidence of use was already purged before the migration ran therefore starts with a NULL marker. That is the honest answer — the database no longer holds the fact — and it is bounded by the other two guards the expiry predicate applies.

#### Scenario: A client with surviving token rows is backfilled
- **WHEN** the migration runs against a client holding one or more token rows
- **THEN** its marker SHALL be set to the newest of those rows' creation times

#### Scenario: A client with surviving code rows is backfilled
- **WHEN** a client holds authorization-code rows but no token rows
- **THEN** its marker SHALL be set to the newest of those rows' creation times

#### Scenario: A client with no surviving child row stays NULL
- **WHEN** a client has neither code nor token rows
- **THEN** its marker SHALL remain NULL and no value SHALL be inferred from its owner or its registration time

#### Scenario: Re-running the migration changes no marker
- **WHEN** the migration executes again against a database whose rows already carry markers
- **THEN** no existing marker SHALL be changed

### Requirement: The schema gate covers migration 025 before it is deployed
The schema gate SHALL run against migration 025 on a throwaway pgvector container before any deploy that carries it, and `alembic check` SHALL be clean after that deploy. A migration is not covered by the offline test subset, and a dirty check hides a real missing constraint inside the noise of the next autogenerate.

#### Scenario: The gate runs before the deploy
- **WHEN** a deploy carries migration 025
- **THEN** the schema gate SHALL have been run against it and SHALL have passed

#### Scenario: The check is clean after the deploy
- **WHEN** the deploy completes and the migration has been applied
- **THEN** `alembic check` SHALL report no new upgrade operations
