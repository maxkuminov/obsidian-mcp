## ADDED Requirements

### Requirement: The index records the vault root it was built from
The system SHALL record, per user, the vault root that the user's `notes_metadata` rows were built from, in a value that is independent of the user's current vault assignment and therefore survives an unassignment. That record SHALL be written only by the index pass that establishes the state it describes, and MUST NOT be written by any operator-facing handler that changes the assignment.

The record is required because `notes_metadata.file_path` is vault-relative: nothing in an index row says which root it came from, and comparing the previous and new values of the assignment cannot answer the question either, since the transition an operator performs is commonly `assigned` → `unassigned` → `assigned elsewhere` and the second step sees no previous root at all.

#### Scenario: A completed pass records the root it scanned

- **WHEN** an index pass runs for a user whose recorded root does not match the assigned root
- **THEN** the recorded root SHALL be updated to the assigned root in the same transaction as any reconciliation the mismatch requires

#### Scenario: The assignment handler does not write the record

- **WHEN** an administrator changes, clears or restores a user's vault assignment through the control panel
- **THEN** the recorded root SHALL be left unchanged by that request

#### Scenario: Single-user mode does not use the record

- **WHEN** an index pass runs with no user identifier
- **THEN** it SHALL neither read nor write the recorded root, because single-user mode has no user row

### Requirement: A reassignment to a different root discards the previous root's index
When a user's assigned vault root differs from the root their index was built from, the index pass SHALL delete that user's `notes_metadata` rows — and, by cascade, their `note_embeddings` and `note_links` rows — before any file under the new root is read, and SHALL then record the new root. The discard MUST happen in one committed transaction, so no pass can leave rows describing one root beside rows describing another.

Serving the previous root's rows is the failure this prevents: the tools served purely from the database — `semantic_search`, `keyword_search`, `list_notes`, `get_recent` and the graph tools — would otherwise return paths, titles, tags, frontmatter and chunk excerpts from a vault the caller no longer has, and a subsequent read of one of those paths can silently return a different note that occupies the same relative path in the new root.

#### Scenario: Reassignment to a different directory

- **WHEN** a user whose index was built from one root is assigned a different root and the next index pass runs
- **THEN** the rows from the previous root SHALL be deleted before the new root is scanned
- **AND** the user's embeddings and link rows SHALL be removed with them

#### Scenario: Reassignment to the recorded root keeps the index

- **WHEN** a user's assignment is cleared and later restored to the same directory the index was built from
- **THEN** no row SHALL be deleted and no note SHALL be re-embedded, preserving the behaviour that makes an unassignment reversible without a full re-index

#### Scenario: An unchanged assignment is a no-op

- **WHEN** an index pass runs for a user whose assigned root equals the recorded root
- **THEN** no reconciliation SHALL be performed and the pass SHALL proceed exactly as before

#### Scenario: No recorded root discards nothing

- **WHEN** an index pass runs for a user whose recorded root is unset
- **THEN** the assigned root SHALL be recorded
- **AND** no row SHALL be deleted, because the root those rows came from was never recorded and MUST NOT be guessed

#### Scenario: Every caller of the index pass inherits the reconciliation

- **WHEN** the index pass is invoked from startup, from the periodic tick, or from an operator-triggered reindex
- **THEN** the reconciliation SHALL run in all three cases, because it lives in the pass rather than in any one caller

#### Scenario: A failed pass after a discard retries cleanly

- **WHEN** the discard commits and the subsequent scan of the new root fails
- **THEN** the next pass SHALL find the recorded root already equal to the assigned root and SHALL simply index, rather than repeating a delete or re-serving the old rows

### Requirement: A reassignment is honoured at the next index pass, not at the moment of assignment
The reconciliation SHALL be performed by the index pass, and the system SHALL NOT claim that a reassignment takes effect immediately. Between the assignment being saved and the next pass completing its reconciliation, the database-backed tools may still answer from the previous root; that window is bounded by the configured index interval plus the duration of a pass already in flight.

This is a declared limitation, at the same level as the other optimistic guarantees in this system. Closing it would require a second writer of index contents inside the panel's request transaction, or refusing every tool — including the disk-backed ones that are already correct against the new root — for the whole interval.

#### Scenario: The bound is the index interval

- **WHEN** an administrator reassigns a user to a different root
- **THEN** the previous root's rows SHALL be gone once the first index pass started after that change has completed its reconciliation

#### Scenario: Disk-backed tools are not refused during the window

- **WHEN** a tool that reads the vault from disk is called during that window
- **THEN** it SHALL operate against the newly assigned root, and SHALL NOT be refused on account of the pending reconciliation
