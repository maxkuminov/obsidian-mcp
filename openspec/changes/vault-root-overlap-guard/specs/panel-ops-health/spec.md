## ADDED Requirements

### Requirement: A vault-root quarantine SHALL be surfaced to administrators, with each reason worded apart
The dashboard's health strip and the health page SHALL show, to administrators, that the published quarantine snapshot names one or more accounts, naming each account, the root it is assigned, and its reason — for an overlap, the conflicting account and the relation found; for a root that could not be examined, the error number and the explicit statement that no conflicting account was observed. The surface SHALL be visible while the condition stands and SHALL disappear once a later snapshot no longer names the account; it SHALL NOT be a transient flash message.

A quarantine is a misconfiguration that persists until an operator acts, and every other record of it decays. The container log rotates with the container; the in-process error ring buffer holds a hundred entries for the life of the process, so the line naming the condition is gone after a restart while the two roots are still overlapping. The panel is the surface an operator opens when something is wrong, and the strip is the newest thing on it — a condition that has silently disabled a tenant's tools has to be legible there, not reconstructed from a run row.

The two reasons are worded apart because they need different fixes and the wrong wording sends the operator to the wrong place: an overlap is corrected by changing an assignment or a mount, while an unexaminable root is corrected by restoring a mount, and describing the latter as an overlap sends an administrator hunting for a second account that does not exist.

Naming the accounts and the roots is correct here and only here: this surface is admin-only, the operator has to know which assignments to look at, and the same detail is deliberately withheld from the tool-facing refusal, whose reader is a tenant's agent.

The condition SHALL be read from the published snapshot, not recomputed by the request handler — the panel must not open directories or parse a mount table on a page render, and two independent computations of "do these roots overlap" is how the panel and the enforcement come to disagree. The accounts and roots SHALL be named from the facts the snapshot recorded at detection time, and the surfaces SHALL NOT re-read the `users` rows to name them; they SHALL present those facts as observed at the last check. An operator's first response to the condition is to edit or delete one of the accounts it names, and a render-time resolution shows a changed path — or a blank where a deleted account was — beside a condition that is still in force.

Where the mount-graft condition could not run because the process's mount table could not be read, the health page SHALL say so, so an operator knows the coverage is reduced. That statement is separate from the transfer-write mount-identity report on `/health`, whose meaning is unchanged: the two capabilities are independent and a reader must not take one as evidence about the other.

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

#### Scenario: Reduced graft coverage is stated

- **WHEN** the mount table could not be read, so the mount-graft condition was skipped
- **THEN** the health page SHALL state that graft coverage is unavailable
- **AND** SHALL NOT change what `/health` reports about transfer-write mount identity

#### Scenario: The panel opens no directories

- **WHEN** the dashboard or the health page renders the condition
- **THEN** the handler SHALL read the published snapshot and SHALL NOT itself open, stat or resolve any vault root, nor read the process's mount table, nor re-read the `users` rows to name the accounts the snapshot records
