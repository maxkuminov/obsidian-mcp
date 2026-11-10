## ADDED Requirements

### Requirement: The write-time task refresh SHALL compose with the index pass without a lock cycle and SHALL always converge to disk

The index pass SHALL read the database clock as its scan start before its first file read, once per pass invocation. An earlier instant only makes the pass keep more refreshed rows, and a kept row stays task-dirty. The scan SHALL treat a row whose `tasks_source_hash` differs from its `content_hash` (a task-dirty row) as ineligible for the stat shortcut and read it. The task stage SHALL then derive it even when its content hash is unchanged.

When the pass derives a note, it SHALL keep that note's task rows unchanged in exactly one case: the row's `tasks_refreshed_at` is at or after the attempt's scan start, and its `tasks_source_hash` differs from the buffered body's hash. Otherwise it SHALL replace the task rows, set `tasks_source_hash` to the buffered hash and clear `tasks_refreshed_at`. Keeping rows SHALL NOT be a skip and SHALL NOT be a failure.

The write-time refresh SHALL take only the note's row lock, with a bounded wait, and SHALL never take the index generation lock, so it cannot form a cycle with a pass, which takes the generation lock before row locks.

After the last write to a note, the first pass whose scan starts after both that write and the last refresh SHALL leave the note's task rows derived from its current disk bytes, with `tasks_source_hash` equal to `content_hash`.

#### Scenario: Interleavings converge
- **WHEN** a pass's scan, a task tool's publication and refresh, and a later generic edit of the same note are interleaved in every order
- **THEN** after at most two further passes with no write, the note's task rows SHALL equal a fresh extraction of the file, and `tasks_source_hash` SHALL equal `content_hash`

#### Scenario: A task-dirty row is never stuck behind the stat shortcut
- **WHEN** a note's task rows were refreshed from bytes that differ from the row's `content_hash`, and the file's stat matches the stored stat
- **THEN** the next pass SHALL read the file and re-derive the note's tasks

#### Scenario: No lock cycle with a running pass
- **WHEN** a task write refreshes a note while a pass holds the generation lock and has already upserted that note
- **THEN** the refresh SHALL wait at most its bounded lock wait and either run after the pass commits or report `index: may lag`, and neither transaction SHALL deadlock

#### Scenario: Derivations agree
- **WHEN** the same note bytes are derived by the pass and by the write-time refresh
- **THEN** the resulting `note_tasks` rows SHALL be identical, column for column
