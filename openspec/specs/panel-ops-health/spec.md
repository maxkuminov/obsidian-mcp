# panel-ops-health Specification

## Purpose
TBD - created by archiving change panel-ops-health. Update Purpose after archive.
## Requirements
### Requirement: Health page
The panel SHALL provide a health page showing indexer run history (most recent 50 runs with start, duration, trigger, counts, and error), the most recent application errors (up to 100, ERROR level and above, since process start, with the observation window stated), and the age of the most recent recorded backup. The run history SHALL be scoped as the performance page scopes it; the error and backup sections SHALL be shown to administrators only, because neither has an owner to scope by — the error buffer holds whatever the process logged and a backup covers the whole database.

#### Scenario: Populated page
- **WHEN** runs, errors, and a backup record exist
- **THEN** all three sections render with their data and the error section states since when it observes

#### Scenario: Empty states
- **WHEN** a fresh install has no runs, no errors, and no backup rows
- **THEN** the page renders explicit empty states and no section errors

#### Scenario: Non-admin viewer
- **WHEN** a non-admin opens the health page
- **THEN** they see only their own run history, the error and backup sections are absent with copy saying they are administrators-only, and no `backups_log` query is issued for that request

### Requirement: Backup recency record
A successful `make db-backup` SHALL insert a `backups_log` row (timestamp, filename, size) through the same channel as the dump when the table exists; when the table does not yet exist (any pre-021 database, including the deploy that ships migration 021, whose backup step precedes its migrate step) the target SHALL skip the insert with a loud warning and still succeed. Once the table exists, a failed insert SHALL fail the target loudly. The panel SHALL warn when the most recent row is older than 8 days.

#### Scenario: Backup recorded
- **WHEN** `make db-backup` completes successfully against a database where backups_log exists
- **THEN** a new row exists whose filename matches the dump written

#### Scenario: Bootstrap deploy
- **WHEN** the deploy that ships migration 021 runs its backup step before migrating
- **THEN** the dump is written, the target warns that the backup is unrecorded, the deploy proceeds, and the next backup is recorded

#### Scenario: Staleness warning
- **WHEN** the newest row is older than 8 days
- **THEN** the health page and dashboard strip show a staleness warning

### Requirement: Dashboard health strip
The dashboard SHALL show a compact strip with the last indexer pass outcome, the last backup age, and the count of errors since process start, linking to the health page.

#### Scenario: Strip reflects failure
- **WHEN** the most recent indexer run recorded an error
- **THEN** the strip shows a failed state linking to the run's row on the health page

