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

### Requirement: The error buffer MUST survive logging reconfiguration

Reconfiguring the application's logging SHALL NOT remove, close or detach the error ring buffer's handler from any logger it was attached to, in either call order, so that the health page keeps reporting errors after the root logger has been taken back from the MCP SDK. Attaching the buffer SHALL remain idempotent across a reconfiguration, and the observation window the page states SHALL NOT be reset by one.

#### Scenario: Reconfiguration after attachment

- **WHEN** the buffer is attached and the logging configuration is then applied
- **THEN** an ERROR logged afterwards SHALL appear in the buffer and the page's observation window SHALL be unchanged

#### Scenario: Attachment after reconfiguration

- **WHEN** the logging configuration is applied first and the buffer is attached afterwards, as the running server does
- **THEN** an ERROR logged afterwards SHALL appear in the buffer exactly once, including one logged on `uvicorn.error`

#### Scenario: Tool failures reach the page

- **WHEN** a tracked tool body raises
- **THEN** the resulting ERROR record SHALL appear in the buffer and therefore on the health page

### Requirement: A vault-root quarantine SHALL be surfaced to administrators, with each reason worded apart
The dashboard's health strip and the health page SHALL show, to administrators, that the published quarantine snapshot names one or more accounts, naming each account, the root it is assigned, and its reason — for an overlap, the conflicting account and the relation found; for a root that could not be examined, the error number and the explicit statement that no conflicting account was observed. The surface SHALL be visible while the condition stands and SHALL disappear once a later snapshot no longer names the account; it SHALL NOT be a transient flash message.

A quarantine is a misconfiguration that persists until an operator acts, and every other record of it decays. The container log rotates with the container; the in-process error ring buffer holds a hundred entries for the life of the process, so the line naming the condition is gone after a restart while the two roots are still overlapping. The panel is the surface an operator opens when something is wrong, and the strip is the newest thing on it — a condition that has silently disabled a tenant's tools has to be legible there, not reconstructed from a run row.

The two reasons are worded apart because they need different fixes and the wrong wording sends the operator to the wrong place: an overlap is corrected by changing an assignment or a mount, while an unexaminable root is corrected by restoring a mount, and describing the latter as an overlap sends an administrator hunting for a second account that does not exist.

Naming the accounts and the roots is correct here and only here: this surface is admin-only, the operator has to know which assignments to look at, and the same detail is deliberately withheld from the tool-facing refusal, whose reader is a tenant's agent.

The condition SHALL be read from the published snapshot, not recomputed by the request handler — the panel must not open directories on a page render, and two independent computations of "do these roots overlap" is how the panel and the enforcement come to disagree. The accounts and roots SHALL be named from the facts the snapshot recorded at detection time, and the surfaces SHALL NOT re-read the `users` rows to name them; they SHALL present those facts as observed at the last check. An operator's first response to the condition is to edit or delete one of the accounts it names, and a render-time resolution shows a changed path — or a blank where a deleted account was — beside a condition that is still in force.

#### Scenario: The strip names each affected account and its reason

- **WHEN** an administrator opens the dashboard while the snapshot names two accounts for an overlap
- **THEN** the health strip SHALL state that a vault-root quarantine is in force
- **AND** SHALL name both accounts, both assigned roots, and the relation found

#### Scenario: An unexaminable root is not described as an overlap

- **WHEN** an administrator opens the dashboard while the snapshot names one account because its root could not be examined
- **THEN** the surface SHALL state that the root could not be examined, with the error number
- **AND** SHALL NOT name a conflicting account or describe the account as overlapping another

#### Scenario: The health page carries the same condition

- **WHEN** an administrator opens the health page while the condition stands
- **THEN** the page SHALL show the same condition alongside the run history, the error buffer and the backup age

#### Scenario: A non-administrator does not see the operator detail

- **WHEN** a regular panel user opens the dashboard while the condition stands
- **THEN** the page SHALL NOT name another account or another account's vault path, consistent with the existing operator-only split on the strip

#### Scenario: The surface clears when the condition is corrected

- **WHEN** an administrator corrects the condition and a later snapshot names neither account
- **THEN** the strip and the page SHALL stop showing it, with no operator dismissal required

#### Scenario: An empty snapshot renders nothing, and an unpublished one says so

- **WHEN** the published snapshot is empty
- **THEN** neither surface SHALL show the condition, and neither SHALL treat its absence as an error state
- **AND WHEN** no snapshot has been published in this process, the surfaces SHALL say that the roots have not been checked yet rather than rendering an all-clear

#### Scenario: The pair is still named after the peer is edited or deleted

- **WHEN** an overlap is shown and an administrator then corrects the peer's assignment, or deletes the peer account, before a later detection publishes
- **THEN** the surface SHALL still name both accounts and both roots from the recorded facts
- **AND** SHALL present them as observed at the last check rather than as the current state

#### Scenario: The panel opens no directories

- **WHEN** the dashboard or the health page renders the condition
- **THEN** the handler SHALL read the published snapshot and SHALL NOT itself open, stat or resolve any vault root, nor re-read the `users` rows to name the accounts the snapshot records

### Requirement: The dashboard reports embedding currency beside coverage
The dashboard SHALL show, next to the embedding coverage bar, the number of notes whose vectors are **not current** — never embedded, or embedded before the note's indexed content — and the number of notes whose embedding was truncated at the per-note chunk cap. Both counts SHALL be scoped to the viewer exactly as the coverage numbers beside them are, so that a non-admin never reads another tenant's backlog as their own.

The existing coverage bar SHALL keep its present meaning — the proportion of notes holding at least one vector row — and SHALL NOT be redefined. Coverage answers "is this note represented at all" and the pending count answers "is that representation current"; they are different questions, they disagree during every embed backlog, and collapsing them would silently change what every previously recorded coverage figure meant.

The predicate behind the pending count SHALL be the same one the re-embedding progress endpoint uses, expressed once and called from both, so the page and the poller cannot come to disagree about what "pending" means. The progress endpoint SHALL keep its existing admin-only, whole-database behaviour.

The pending count is the operator-visible consequence of two things nothing else surfaces on this page: a provider outage, which now marks the pass record but leaves coverage reading whatever it read yesterday; and a tenant whose embedding is repeatedly stopped at its per-pass budget, which is deliberately not recorded as a pass error. A backlog that does not shrink across passes is the signal, and it is a property of the index rather than of any one pass.

#### Scenario: A stale vault reads as fully covered and not current

- **WHEN** every note has vectors and every note has been edited since it was last embedded
- **THEN** the coverage bar SHALL read 100%
- **AND** the pending count beside it SHALL equal the number of notes

#### Scenario: The counts are scoped like their neighbours

- **WHEN** a non-admin user with an assigned vault opens the dashboard while another tenant has a large embedding backlog
- **THEN** the pending count SHALL count only that user's notes

#### Scenario: A truncated note is counted

- **WHEN** a note's embedding was capped at the per-note chunk cap
- **THEN** the dashboard SHALL report at least one note with a truncated embedding

#### Scenario: A healthy vault reports zero pending

- **WHEN** every note's stored vectors match its indexed content
- **THEN** the pending count SHALL be zero and SHALL still be rendered

#### Scenario: The progress endpoint is unchanged

- **WHEN** an administrator polls the re-embedding progress endpoint
- **THEN** it SHALL return the same whole-database counts under the same keys as before this change

