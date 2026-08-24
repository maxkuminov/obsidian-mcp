## ADDED Requirements

### Requirement: The index records the vault root it was built from
The system SHALL record, per user, the vault root that user's `notes_metadata` rows were built from, in a value that is independent of the user's current vault assignment and therefore survives an unassignment. That record SHALL be written only by the index pass that establishes the state it describes, and MUST NOT be written by any operator-facing handler that changes the assignment.

The record is required because `notes_metadata.file_path` is vault-relative: nothing in an index row says which root it came from. Comparing the previous and new values of the assignment cannot answer the question either, because the transition an operator performs is commonly `assigned` → `unassigned` → `assigned elsewhere`, and the second step sees no previous root at all — it is indistinguishable from the restore-to-the-same-directory case the index is deliberately preserved for.

The recorded value SHALL be the same root the pass is about to scan, normalised the same way on both sides of every comparison, so that a purely cosmetic difference in how a root is spelled can never be read as a reassignment.

#### Scenario: A completed pass records the root it scanned

- **WHEN** an index pass runs for a user whose recorded root does not match the assigned root
- **THEN** the recorded root SHALL be updated to the assigned root in the same transaction as any reconciliation the mismatch requires

#### Scenario: The assignment handler does not write the record

- **WHEN** an administrator changes, clears or restores a user's vault assignment through the control panel
- **THEN** the recorded root SHALL be left unchanged by that request

#### Scenario: Single-user mode does not use the record

- **WHEN** an index pass runs with no user identifier
- **THEN** it SHALL neither read nor write the recorded root, because single-user mode has no user row
- **AND** the pass SHALL behave exactly as it does today

#### Scenario: A cosmetic difference in spelling is not a reassignment

- **WHEN** the assigned root and the recorded root denote the same directory but differ only in a trailing separator or another normalisation-equivalent form
- **THEN** the pass SHALL treat them as equal and SHALL delete nothing

### Requirement: A reassignment to a different root discards the previous root's index
When a user's assigned vault root differs from the root their index was built from, the index pass SHALL delete that user's `notes_metadata` rows — and, by cascade, their `note_embeddings` and `note_links` rows — before any file under the new root is read, and SHALL then record the new root. The discard and the stamp SHALL commit as one transaction, so no pass can leave rows describing one root beside a record naming another.

Serving the previous root's rows is the failure this prevents. The tools served purely from the database — `semantic_search`, `keyword_search`, `list_notes`, `get_recent` and the graph tools — would otherwise return paths, titles, tags, frontmatter and chunk excerpts from a vault the caller no longer has, and a subsequent read of one of those paths can silently return a different note that occupies the same relative path in the new root.

The pass's existing prune by relative path does not make this redundant. A note whose relative path **and** content hash are identical in both roots is classified as unchanged and skipped, so its links are never re-extracted; the notes it pointed at are pruned, and because `note_links.target_note_id` is `ON DELETE SET NULL` the link row survives with its target resolution lost. That link never heals, because the note is never re-parsed again.

#### Scenario: Reassignment to a different directory

- **WHEN** a user whose index was built from one root is assigned a different root and the next index pass runs
- **THEN** the rows from the previous root SHALL be deleted before the new root is scanned
- **AND** the user's `note_embeddings` and `note_links` rows SHALL be removed with them

#### Scenario: A note identical in both roots does not keep a broken link

- **WHEN** a note has the same relative path and the same content hash in the previous and the new root, and the notes it linked to exist only in the previous root
- **THEN** after the reconciling pass that note SHALL NOT retain a link row whose resolution was silently dropped by the prune
- **AND** the graph tools SHALL report that note's neighbourhood from the new root alone

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

- **WHEN** the index pass is invoked from the startup pass, from the periodic tick, or from an operator-triggered reindex
- **THEN** the reconciliation SHALL run in all three cases, because it lives in the pass rather than in any one caller

#### Scenario: A failed pass after a discard retries cleanly

- **WHEN** the discard commits and the subsequent scan of the new root fails
- **THEN** the next pass SHALL find the recorded root already equal to the assigned root and SHALL simply index, rather than repeating a delete or re-serving the old rows

#### Scenario: The discard precedes the first read of the new root

- **WHEN** a reconciling pass runs
- **THEN** the delete and the stamp SHALL be committed before any file under the newly assigned root is opened, so a failure while scanning cannot leave the previous root's rows queryable

### Requirement: A reassignment is honoured at the next index pass, not at the moment of assignment
The reconciliation SHALL be performed by the index pass, and the system SHALL NOT claim that a reassignment takes effect immediately. Between the assignment being saved and the next pass completing its reconciliation, the database-backed tools may still answer from the previous root; that window is bounded by the configured index interval plus the duration of a pass already in flight, and it SHALL be documented as a limitation rather than left to be discovered.

Closing the window would require either a second writer of index contents inside the panel's request transaction — which is how two deletion paths drift apart — or refusing every tool for the whole interval, including the disk-backed tools that are already correct against the new root. This is the same optimistic level the system declares for `edit_note(expected=…)` and the transfer fingerprint check.

#### Scenario: The bound is the index interval

- **WHEN** an administrator reassigns a user to a different root
- **THEN** the previous root's rows SHALL be gone once the first index pass started after that change has completed its reconciliation

#### Scenario: Disk-backed tools are not refused during the window

- **WHEN** a tool that reads the vault from disk is called during that window
- **THEN** it SHALL operate against the newly assigned root, and SHALL NOT be refused on account of the pending reconciliation

#### Scenario: The panel does not delete index rows

- **WHEN** an administrator saves a change to a user's vault assignment
- **THEN** that request SHALL NOT delete any `notes_metadata`, `note_embeddings` or `note_links` row
