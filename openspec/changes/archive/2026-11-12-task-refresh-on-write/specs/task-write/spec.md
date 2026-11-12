## ADDED Requirements

### Requirement: `edit_note`, `set_frontmatter` and `write_file` to a note path SHALL attempt a task refresh after publication and event when either side may hold a task

After a confirmed publication and after its change-feed event, each of these writers SHALL call the write-time task refresh. The call SHALL pass the bytes the writer published and SHALL be made while the path's anchored target is still open:

- `edit_note`, in every mode;
- `set_frontmatter`;
- `write_file`, after every publication to a note path (`change_feed.is_note_path`). The decision SHALL NOT depend on any existence check made before the publication. A creation finds no index row, and the refresh then writes nothing (`not_indexed`).

These SHALL NOT call the refresh: `create_note`, `write_file` writing a non-note path, `move_note` (with or without link rewrites), `import_from_url`, `PUT /transfer/upload` and `delete_file`. No writer SHALL call it after a refusal, a dry run, a "No changes" answer, a publication that raised (including an in-call conflict), or an uncertain publication.

**The gate.** The call SHALL be made only when at least one of these holds:

- `tasks.may_hold_tasks(published)` is true;
- the call read the bytes it replaced, and `tasks.may_hold_tasks(replaced)` is true;
- the call does not hold the bytes it replaced: every `write_file` except a guarded overwrite, whose publication is bound to the incumbent it read.

A call that holds the bytes it replaced and published exactly those bytes SHALL NOT call the refresh.

`tasks.may_hold_tasks(data)` SHALL be true when `data` is not UTF-8. Otherwise it SHALL be whether the extractor's checkbox prefix matches anywhere in `note_hash.canonical_note_text(data)`. The prefix is `_CHECKBOX_RE`'s pattern through the closing `]`, without its trailing lookahead, anchored with `^` and compiled with `re.MULTILINE`.

`tasks.may_hold_tasks` SHALL:

- return true for every input on which `tasks.tasks_from_note_text` returns at least one task;
- take time linear in the input;
- run in a worker thread, never on the event loop's thread;
- read no file and no database row.

An exception from the gate SHALL count as true.

**The refresh** SHALL be the one `task_index.refresh_note_tasks` of the task write tools. It keeps that function's derivation from the published bytes, its own transaction, its root re-check, its owner-scoped row lock with the 2-second lock wait, its byte re-check under that lock (`superseded`), and its stamps. It SHALL NOT take the index generation lock.

When the refresh runs, a following `list_tasks` SHALL reflect the write. When it cannot run, the task rows SHALL be left to the next index pass, as before this change.

#### Scenario: An edit that ticks a box is listed at once when the refresh runs
- **WHEN** `list_tasks` shows `- [ ] Buy milk` in `Home.md`, `edit_note(path="Home.md", find="- [ ] Buy milk", content="- [x] Buy milk")` succeeds, its refresh reports `refreshed`, and `list_tasks(status="done")` is called before any index pass
- **THEN** the listing SHALL show the task as done, and the open listing SHALL NOT show it

#### Scenario: Removing the last checkbox removes the rows
- **WHEN** `edit_note` replaces a note holding two tasks with text holding no checkbox, and its refresh runs
- **THEN** the note SHALL have no task rows before any pass

#### Scenario: A frontmatter change keeps line numbers true
- **WHEN** a note has a task on line 6 under a three-key frontmatter block, `set_frontmatter` adds two keys, and its refresh runs
- **THEN** the task SHALL be listed at line 8 with an unchanged id, before any pass

#### Scenario: A write to a note without checkboxes does no refresh
- **WHEN** `edit_note` replaces a note whose old and new bytes contain no line-start checkbox
- **THEN** no refresh SHALL be attempted, and the usage row SHALL carry no `task_refresh` key

#### Scenario: An unguarded overwrite assumes the incumbent held tasks, even with equal bytes
- **WHEN** `write_file(path="A.md", overwrite=True)` runs without `expected_hash`, in a deployment that does not require one, and publishes bytes holding no checkbox, equal or not to the incumbent
- **THEN** the refresh SHALL be attempted

#### Scenario: A dry run never refreshes
- **WHEN** `edit_note(dry_run=true)` runs on a note with tasks
- **THEN** no refresh SHALL be attempted

#### Scenario: A note moved onto the path between `write_file`'s existence check and its publication is refreshed
- **WHEN** `write_file(path="A.md", overwrite=True)` samples the path while it is absent, a move then lands an indexed note with tasks at `A.md`, and `write_file` then overwrites it with different task lines
- **THEN** the refresh SHALL be attempted for `A.md`

#### Scenario: A `write_file` creation attempts a refresh that does nothing
- **WHEN** `write_file` creates `fresh.md`, which has no index row
- **THEN** the refresh SHALL be attempted, the usage row SHALL record `task_refresh = lagged:not_indexed`, and no `notes_metadata` or `note_tasks` row SHALL be written

#### Scenario: A created note and a link rewrite wait for the pass
- **WHEN** `create_note` creates a note with tasks, or `move_note(rewrite_links=True)` rewrites a link inside another note's task line
- **THEN** no refresh SHALL be attempted, and the listing SHALL reflect the change after the next pass

#### Scenario: The gate is a superset of the extractor
- **WHEN** `may_hold_tasks` and `tasks_from_note_text` run over thousands of deterministic generated notes that hold tasks
- **THEN** no input SHALL yield a task while the gate returns false
- **AND** the generated notes SHALL include list markers, checkbox states, indentation, blockquote and callout prefixes, fenced and inline code (including a code span directly after `]`), Unicode whitespace, CRLF and lone CR, and U+0000 inside the brackets

#### Scenario: The gate is linear
- **WHEN** `may_hold_tasks` runs on 10 MiB of `- - - …`, of `> > > …`, or of digits without a delimiter
- **THEN** it SHALL return in under 2 seconds

### Requirement: `delete_note` SHALL clear the deleted note's task rows after every confirmed delete

After every confirmed soft or permanent delete, and after its change-feed event, `delete_note` SHALL call the refresh's absence form, whatever bytes it read before the delete. The absence form SHALL run the refresh's steps with these differences:

- under the row lock, it SHALL confirm through the anchored target that nothing is at the path. If something is, it SHALL write nothing and report `superseded`;
- it SHALL delete the note's `note_tasks` rows;
- it SHALL set `tasks_source_hash` to the canonical hash of the empty note, `tasks_refreshed_at` to the database clock, and `tasks_failed` and `tasks_truncated` to false.

It SHALL write no other column, and SHALL NOT delete the `notes_metadata` row.

#### Scenario: A deleted note's tasks leave the listing
- **WHEN** `delete_note("Done.md")` deletes a note with three open tasks, its clear reports `refreshed`, and `list_tasks` is called before any pass
- **THEN** none of the three SHALL be listed, and the next pass SHALL prune the row

#### Scenario: A task added between the delete's read and the delete is cleared
- **WHEN** `delete_note(path, expected_hash=…)` reads a note with no checkbox, a second writer then adds a task to it and refreshes, and the delete then removes the file
- **THEN** the clear SHALL still run, and no task of the deleted note SHALL be listed

#### Scenario: A recreated path is not cleared
- **WHEN** `delete_note` deletes `A.md`, another writer creates `A.md` before the clear takes the row lock, and the clear then runs
- **THEN** the clear SHALL write nothing and report `superseded`

### Requirement: A generic writer's task refresh SHALL NOT change the writer's response, outcome or change-feed event, and SHALL be recorded only in the usage row

A generic writer's refresh or clear SHALL leave the call's visible result as it would be without one. For every generic writer that refreshes or clears:

- the response text SHALL be byte-identical to the response the same call gives without a refresh, whatever the refresh reports;
- the outcome classification (disposition, marker, refusal) SHALL be unchanged;
- the refresh SHALL NOT raise into the tool;
- the change-feed event SHALL be recorded exactly as before this change, before the refresh starts.

The usage row SHALL carry the result-side key `task_refresh` only when a refresh or clear was attempted, with the value `refreshed` or `lagged:<code>` (the existing codes).

No tool name, argument, schema, title, annotation or description SHALL change, and `tests/fixtures/tools_list_full_v2.json` SHALL match unchanged.

A call cancelled during its refresh SHALL leave:

- the publication standing;
- its change-feed event recorded;
- the task rows either unchanged (cancelled before the refresh's commit began) or fully replaced by the rows derived from the published bytes (cancelled during or after that commit), never a partial set;
- no response and no usage row;
- its concurrency lease released.

The three task write tools SHALL keep their order: publication, refresh, event.

#### Scenario: A lagged refresh is invisible in the response
- **WHEN** another transaction holds a note's row lock for longer than 2 seconds, and `edit_note` changes a task in that note
- **THEN** the file SHALL carry the edit, and the response SHALL be byte-identical to the response without a refresh
- **AND** the outcome SHALL be a success, the `modified` event SHALL be recorded, and the usage row SHALL record `task_refresh = lagged:lock_timeout`
- **AND** the next pass SHALL bring the listing up to date

#### Scenario: Every refresh code leaves the response unchanged
- **WHEN** each of `edit_note`, `set_frontmatter`, `write_file` and `delete_note` runs with the refresh forced to return `refreshed`, and then each code in `task_index.LAG_CODES`
- **THEN** each tool's response text SHALL be the same for every code, and its event SHALL be recorded each time

#### Scenario: A cancelled call keeps its write and its event
- **WHEN** `edit_note` publishes, records its event, and is cancelled while its refresh waits for the row lock
- **THEN** the file SHALL carry the edit and the event SHALL be recorded
- **AND** the task rows SHALL be unchanged, because the cancellation came before the refresh's commit began
- **AND** no usage row SHALL be written, and the call's lease SHALL be released

#### Scenario: A cancellation can never leave a partial set of task rows
- **WHEN** a call is cancelled at any point of its refresh, including during or after the refresh's commit
- **THEN** the note's task rows SHALL be either the rows from before the refresh or exactly the rows derived from the published bytes (none, for a delete)
- **AND** every task-row write of the refresh SHALL happen in one database transaction with a single commit

### Requirement: A generic writer SHALL attempt at most one bounded task refresh per call

`edit_note`, `set_frontmatter`, `write_file` and `delete_note` SHALL each attempt at most one refresh or clear per call. The gate and the derivation SHALL run in a worker thread (`asyncio.to_thread`), never on the event loop's thread. The row-lock wait SHALL be the existing 2-second bound. A note with more than `MAX_TASKS_PER_NOTE` tasks SHALL be stored truncated with `tasks_truncated` set, as the pass stores it.

#### Scenario: A maximal note
- **WHEN** `edit_note` publishes exactly `MAX_NOTE_BYTES` holding 5,001 checkboxes
- **THEN** the refresh SHALL store 5,000 task rows with `tasks_truncated = true`
- **AND** the gate and the task derivation SHALL each run in a worker thread, not on the event loop's thread

### Requirement: Task rows written by a generic writer's refresh SHALL converge with the index pass under the existing rules

The index pass SHALL NOT change. Two of its rules SHALL apply to rows written by a generic writer's refresh or clear exactly as they apply to a task tool's:

- the keep rule: a row refreshed at or after the scan's start, whose `tasks_source_hash` differs from the buffered hash, keeps its task rows;
- the rule that a row whose `tasks_source_hash` differs from its `content_hash` is never stat-shortcut.

After the last write to a note, the first pass whose scan starts after both that write and the last refresh SHALL leave `tasks_source_hash` equal to `content_hash`, and the task rows equal to the extraction of the file on disk.

#### Scenario: Refreshes of different writers finishing out of order
- **WHEN** `edit_note` publishes B1 and pauses before its refresh, `complete_task` then publishes B2 and refreshes, and the `edit_note` refresh then runs
- **THEN** the `edit_note` refresh SHALL write nothing and its usage row SHALL record `task_refresh = lagged:superseded`
- **AND** the task rows SHALL describe B2

#### Scenario: An outside edit after a refresh is picked up
- **WHEN** `edit_note` refreshes a note from B1, a sync then writes B2 to the file, and two passes run
- **THEN** after the second pass at the latest, the task rows SHALL describe B2 and `tasks_source_hash` SHALL equal `content_hash`

#### Scenario: Another user's note at the same path is untouched
- **WHEN** users 7 and 8 each have `Notes/a.md` with tasks, and user 7 edits and then deletes theirs
- **THEN** user 8's task rows and `tasks_*` columns SHALL be unchanged
