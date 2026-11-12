## MODIFIED Requirements

### Requirement: Task rows SHALL be derived state that is written only with their note's row and can never outlive it

The index pass SHALL store tasks in `note_tasks`, keyed by `note_id` with a foreign key to `notes_metadata.id` `ON DELETE CASCADE`. It SHALL NOT store a path or owner on the task row.

**The pass's derivation.** For every changed path of a pass (the upserted and the moved paths), and for every row whose `tasks_source_hash` differs from its `content_hash`, the pass SHALL do the following in the same transaction as the note's row, immediately after that note's link rebuild:

- delete the note's task rows and insert the rows extracted from the body the scan buffered;
- set `tasks_source_hash` to the buffered body's canonical hash and clear `tasks_refreshed_at`.

The exception is a row whose `tasks_refreshed_at` is at or after the attempt's scan start and whose `tasks_source_hash` differs from the buffered hash: its task rows SHALL be kept, because a write-time refresh (by a task tool or a generic note writer) or a delete clear wrote them after the scan began.

**Reading.** The pass SHALL NOT read a file outside the scan. The scan SHALL read every row whose `tasks_source_hash` differs from its `content_hash`, regardless of the stat shortcut. A changed path with no buffered body or no index row SHALL be recorded as a skip that withholds a re-derive's record.

**The only other writer** of a note's task rows SHALL be the write-time task refresh of the `task-write` capability, called by the task write tools and by the generic note writers that capability names, including its absence form after `delete_note`. It holds that note's row lock and derives only from the bytes its own call published, or, in the absence form, writes no task row after confirming that nothing is at the path.

**Savepoints and isolation.** Each note's task deletion and insertion SHALL run in their own savepoint. A task-derivation failure SHALL be isolated to that note's task rows, as the `index-integrity` requirement on task-derivation isolation states.

**Lifecycle.**

- Deleting a note's row by any route (prune, quarantine removal, provenance discard, user deletion) SHALL remove its task rows.
- `delete_note` SHALL attempt to remove the deleted note's task rows in the same call through the delete clear of the `task-write` capability; the pass SHALL later prune the row itself, and SHALL remove the task rows if the clear could not run.
- A row protected from the prune by an unlistable directory SHALL keep its task rows.
- An id-preserving move SHALL leave the rows attached to the moved note.
- `CURRENT_EXTRACTION_VERSION` SHALL be 5, with versions 4 and 5 sharing version 3's embedding cleaner, so that neither bump re-embeds anything.

#### Scenario: Editing a note replaces its tasks
- **WHEN** a note with tasks A and B is edited to hold tasks B and C, and the next pass commits
- **THEN** `note_tasks` SHALL hold exactly B and C for that note

#### Scenario: Deleting a note removes its tasks
- **WHEN** a note with tasks is deleted from disk and the next pass commits
- **THEN** no task row SHALL reference it

#### Scenario: A moved note keeps its tasks under the new path
- **WHEN** a note with tasks is renamed on disk (same content) and the pass's id-preserving move runs
- **THEN** `list_tasks` SHALL list those tasks under the new path and never under the old one

#### Scenario: A failing task insert costs only that note's task rows
- **WHEN** one note's task insert fails with a poison SQLSTATE on the attempt and on its retry
- **THEN** that note SHALL keep its row, keyword vector, links and embeddings, SHALL have no task rows and `tasks_failed = true`, SHALL NOT be quarantined, and every other note of the pass SHALL be committed with its tasks

#### Scenario: An unlistable directory protects its tasks
- **WHEN** a directory holding indexed notes with tasks cannot be listed during a pass
- **THEN** those notes' rows and task rows SHALL remain

#### Scenario: The version bump backfills without re-embedding
- **WHEN** a vault indexed at version 4 is indexed by this build
- **THEN** every note SHALL gain its task keys, ordinals, counts and the other version-5 columns and be stamped 5, and no note's `embedded_content_hash` SHALL be cleared on account of the bump

#### Scenario: A pass that read older bytes keeps a tool's newer task rows
- **WHEN** a pass begins its scan, a task tool then publishes and refreshes a note, and the pass then derives that note from bytes it read before the publication
- **THEN** the pass SHALL keep the tool's task rows, and a later pass whose scan starts after the refresh SHALL re-derive the note from disk, leaving `tasks_source_hash` equal to `content_hash`

#### Scenario: A pass that read older bytes keeps a generic writer's newer task rows
- **WHEN** a pass begins its scan, `edit_note` then publishes and refreshes a note, and the pass then derives that note from bytes it read before the publication
- **THEN** the pass SHALL keep the refreshed task rows, and a later pass whose scan starts after the refresh SHALL re-derive the note from disk, leaving `tasks_source_hash` equal to `content_hash`

#### Scenario: A pass that read a deleted note keeps the delete clear
- **WHEN** a pass begins its scan and buffers a note with tasks, `delete_note` then deletes it and clears its task rows, and the pass then reaches that note
- **THEN** the pass SHALL keep the cleared (empty) task set, and the next pass SHALL prune the row
