## ADDED Requirements

### Requirement: A caller the quarantine snapshot names SHALL be refused by the admission gate
Every MCP tool call by a user the published quarantine snapshot names SHALL be refused by the shared admission gate, through the same mechanism as a caller with no vault assignment: the root resolution SHALL raise and the decorator SHALL fail the call before the tool body runs. The refusal SHALL apply to every registered tool with no exemptions, SHALL NOT delete the caller's index rows, and SHALL be recorded in `usage_logs` under a marker distinct from the no-assignment marker and distinct per reason — one marker for an overlap, another for a root that could not be examined.

Refusing to *index* a quarantined pair is not sufficient and must not be mistaken for the whole control. The database-backed tools answer from `notes_metadata` and `note_embeddings` and never touch the disk, so rows a previous pass already wrote for the other tenant's notes stay queryable; and the write tools resolve beneath the caller's root, which physically contains the other tenant's files, so `edit_note`, `move_note`, `delete_note` and `write_file` reach them and the beneath-root containment check agrees they are contained. The write path consults no indexer. A cross-tenant destructive write is the failure this product ranks highest, and the admission gate is the only control that is total over it.

The markers are distinct because the markers already distinguish things an operator would act on differently. Recording a quarantine as "no vault assigned" tells an operator that an administrator unassigned a user whose users page plainly shows an assignment; recording an unexaminable root as an overlap sends them looking for a second account that does not exist.

The refusal message the caller receives SHALL name no other user, no other vault path and no note path, for any reason. The caller is a tenant's agent; the operator-facing surfaces are where the affected accounts, reasons and roots are named.

#### Scenario: Database-backed tools are refused

- **WHEN** a user the snapshot names calls `semantic_search`, `keyword_search`, `list_notes`, `get_recent`, `get_tags` or any graph tool with an unchanged, still-active credential
- **THEN** the call SHALL be refused with a tool error naming no note path, title, tag, frontmatter value or chunk excerpt

#### Scenario: Write tools are refused

- **WHEN** the same caller calls `create_note`, `edit_note`, `move_note`, `delete_note`, `set_frontmatter`, `write_file` or `delete_file`
- **THEN** the call SHALL be refused before any path is resolved beneath the root and before any byte is written

#### Scenario: The refusal names no other tenant

- **WHEN** any tool call is refused for a quarantine, under either reason
- **THEN** the message SHALL NOT contain another user's username, another user's vault path, or any note path

#### Scenario: Each reason carries its own marker

- **WHEN** one call is refused for an overlap and another for a root that could not be examined
- **THEN** the two `usage_logs` rows SHALL carry different error markers
- **AND** both SHALL differ from the marker used for a caller with no vault assignment

#### Scenario: The index survives the refusal

- **WHEN** the quarantine is corrected and the caller's assignment is unchanged
- **THEN** the caller's previously indexed rows SHALL still be present

#### Scenario: Unrelated callers are unaffected

- **WHEN** a user the snapshot does not name calls any tool
- **THEN** the call SHALL be admitted exactly as before

#### Scenario: Single-user mode is unaffected

- **WHEN** the server runs in single-user mode, where the caller has no user id and the root comes from settings
- **THEN** no quarantine test SHALL apply and admission SHALL behave exactly as it does today

#### Scenario: The panel vault browser refuses the same user

- **WHEN** a user the snapshot names opens the panel's vault browser
- **THEN** the page SHALL render the existing unavailable-vault empty state rather than listing a directory tree that may contain another tenant's notes

### Requirement: A tool call SHALL be refused until a quarantine snapshot has been published in this process
Until the shared detection has published a snapshot in the serving process, the admission gate SHALL refuse every multi-user tool call with a refusal typed distinctly from both the overlap refusal and the no-assignment refusal. The startup path SHALL publish synchronously before the application serves, so this state is normally never observed; it SHALL remain reachable and SHALL fail closed when it is.

Publishing asynchronously and serving permissively in the meantime has two failure modes and both are silent. A tool call between the first accepted connection and the first published snapshot is served against roots nothing has checked — the whole window the guard exists to close, reopened once per restart. And a first detection that *raised* would leave the process permissive for the life of the container, because nothing would ever revisit the decision.

Failing closed is cheap here precisely because a detection failure is not a per-root failure. A root that cannot be opened is a per-user verdict; the routine itself fails only when the user enumeration fails, which means the database is unavailable and the tools cannot serve anyway. Single-user mode and sandbox mode SHALL NOT be affected: the former never consults the snapshot, and the latter publishes an empty one at startup without touching the filesystem.

#### Scenario: A call before the first snapshot is refused

- **WHEN** a multi-user tool call reaches the gate in a process where no snapshot has been published
- **THEN** the call SHALL be refused
- **AND** the refusal SHALL be typed distinctly from the overlap and no-assignment refusals

#### Scenario: The startup publication precedes serving

- **WHEN** the application starts normally
- **THEN** the snapshot SHALL be published before the first request is served, so an ordinary caller never observes the not-ready refusal

#### Scenario: A failed first detection keeps the gate closed

- **WHEN** the first detection raises and a tool call arrives
- **THEN** the call SHALL be refused rather than admitted on the strength of a detection that did not complete

#### Scenario: Single-user mode is never gated on readiness

- **WHEN** the server runs in single-user mode
- **THEN** the readiness state SHALL not be consulted and every tool call SHALL be admitted as it is today

## MODIFIED Requirements

### Requirement: The admission gate performs no database work
Resolving the caller's vault root for admission SHALL NOT issue a database statement and SHALL NOT perform filesystem I/O, so the check costs nothing on the hot path. The quarantine and readiness tests the gate performs SHALL be lookups into a snapshot already published by the shared detection, and they SHALL be able only to refuse — they MUST NOT be capable of admitting a caller the rest of the gate would refuse.

The gate runs on every tool call and the per-request cache warm is what makes a cache read correct there; a query would be a query per call, and detection needs a query for every other user's assignment plus an `open`, `fstat` and `realpath` per root — the latter dispatched to a worker thread under a deadline, which is not something a per-call gate can do. All of it belongs in the detection, which already does it once per pass. Because the tests can only refuse, it is safe to consult them ahead of the request's immutable vault-root snapshot: unlike an assignment — where a stale read must never re-admit a revoked caller, which is why the snapshot outranks the process-global cache — a quarantine has no direction in which staleness admits anyone.

#### Scenario: Assigned caller invokes a tool

- **WHEN** an assigned caller's tool call passes the admission gate
- **THEN** the gate SHALL have opened no database session

#### Scenario: The quarantine test opens nothing

- **WHEN** a tool call is refused for a quarantine or for readiness
- **THEN** the gate SHALL have opened no database session and made no filesystem call

#### Scenario: The quarantine test cannot admit

- **WHEN** a caller has no vault assignment and is also absent from the quarantine snapshot
- **THEN** the call SHALL still be refused for having no assignment

### Requirement: The users list MUST NOT report a note count the tools will not serve
The control panel's user list SHALL NOT render a note count for an account that holds no vault assignment, nor for an account the published quarantine snapshot names. It SHALL render an explicit not-served state instead, stating for an unassigned account that every MCP tool is refused and the index is kept for reassignment, and stating for a quarantined account which reason applies — an overlap with a named account, or a root that could not be examined — so the operator reads the same fact the admission gate enforces.

A number rendered beside `(unassigned)` reads as capacity the account has, when in fact every tool call from that account is refused before its body runs. This is the same over-reporting of liveness as the revoked-key count the panel used to present as an unqualified total. A quarantined account is in exactly that position and worse: it is assigned, it is indexed, its row count is real, and nothing will serve it. Rendering the count unqualified beside a healthy-looking assignment is the most misleading of the three states, because the operator has no other cue that the account is dark.

#### Scenario: Unassigned account

- **WHEN** the user list renders an account whose vault assignment is empty
- **THEN** the note column SHALL show a not-served state rather than a number
- **AND** SHALL state that the tools are refused and the index is retained for reassignment

#### Scenario: Quarantined account

- **WHEN** the user list renders an account the quarantine snapshot names
- **THEN** the note column SHALL show a not-served state rather than a number
- **AND** SHALL state the reason, naming the conflicting account for an overlap and stating that the root could not be examined for the other reason

#### Scenario: Assigned account

- **WHEN** the user list renders an account that holds a vault assignment and is not named by the snapshot
- **THEN** the note column SHALL show that account's note count as before

#### Scenario: The retained rows are not deleted to make the display true

- **WHEN** the display changes for an unassigned or quarantined account
- **THEN** the account's `notes_metadata`, `note_embeddings` and `note_links` rows SHALL remain in the database, so a corrected assignment still resumes without a full re-index
