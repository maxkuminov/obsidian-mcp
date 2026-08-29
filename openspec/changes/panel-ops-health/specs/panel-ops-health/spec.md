## ADDED Requirements

### Requirement: Health page
The panel SHALL provide a health page showing indexer run history (most recent 50 runs with start, duration, trigger, counts, and error), the most recent application errors (up to 100, ERROR level and above, since process start, with the observation window stated), and the age of the most recent recorded backup.

#### Scenario: Populated page
- **WHEN** runs, errors, and a backup record exist
- **THEN** all three sections render with their data and the error section states since when it observes

#### Scenario: Empty states
- **WHEN** a fresh install has no runs, no errors, and no backup rows
- **THEN** the page renders explicit empty states and no section errors

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
