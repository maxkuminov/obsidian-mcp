## ADDED Requirements

### Requirement: The generic note writers that replace an indexed note's bytes SHALL refresh its task rows after a confirmed publication when either side may hold a task

After a confirmed publication, and before its change-feed event, each of these writers SHALL call the write-time task refresh, with the bytes it published and the path's anchored target still open:

- `edit_note`, in every mode;
- `set_frontmatter`;
- `write_file`, when the path is a note path (`change_feed.is_note_path`) and an incumbent stood at it;
- `move_note(rewrite_links=True)`, for each rewrite it published: the moved note's own, at the destination path, and each backlink source's. The call SHALL be made before that rewrite's target is closed.

The following SHALL NOT call the refresh:

- `create_note`;
- `write_file` creating a note, or writing a non-note path;
- `move_note` without link rewrites;
- `import_from_url`;
- `PUT /transfer/upload`;
- `delete_file`.

None of the writers SHALL call it after a refusal, a dry run, a "No changes" answer, a publication that raised (including an in-call conflict), an uncertain publication, or a rewrite that failed or was stopped.

**The gate.** The call SHALL be made only when at least one of these holds:

- `tasks.may_hold_tasks(published)` is true;
- the call read the bytes it replaced and `tasks.may_hold_tasks(replaced)` is true;
- the call replaced an incumbent whose bytes it did not read.

A call that published exactly the bytes it replaced SHALL NOT call it.

`tasks.may_hold_tasks(data)` SHALL be:

- true when `data` is not UTF-8;
- otherwise, whether the extractor's checkbox prefix (`_CHECKBOX_RE`'s pattern through the closing `]`, without its trailing lookahead, anchored with `^` and compiled with `re.MULTILINE`) matches anywhere in `note_hash.canonical_note_text(data)`. The lookahead is left out because the extractor evaluates it on the masked text, where a code span right after `]` has become spaces.

For every input on which `tasks.tasks_from_note_text` returns at least one task, it SHALL return true. It SHALL take time linear in the input, SHALL run off the event loop, and SHALL read no file and no database row. An exception from the gate SHALL count as true.

The refresh SHALL be the one `task_index.refresh_note_tasks` of the task write tools, with its derivation from the published bytes, its own transaction, its root re-check, its owner-scoped row lock with the 2-second lock wait, its byte re-check under that lock (`superseded`), and its stamps. It SHALL NOT take the index generation lock.

#### Scenario: An edit that ticks a box is listed at once
- **WHEN** `list_tasks` shows `- [ ] Buy milk` in `Home.md`, `edit_note(path="Home.md", find="- [ ] Buy milk", content="- [x] Buy milk")` succeeds, and `list_tasks(status="done")` is called before any index pass
- **THEN** the listing SHALL show the task as done with its id, and the open listing SHALL NOT show it

#### Scenario: A rewritten section replaces its tasks
- **WHEN** `edit_note(section=…)` replaces a section holding tasks A and B with one holding B and C, and `list_tasks` is called before any pass
- **THEN** the listing SHALL show exactly B and C for that note

#### Scenario: A frontmatter change keeps line numbers true
- **WHEN** a note has a task on line 6 under a three-key frontmatter block, `set_frontmatter` adds two keys, and `list_tasks` is called before any pass
- **THEN** the task SHALL be listed at line 8 with an unchanged id

#### Scenario: A link rewrite inside a task line updates the task's id
- **WHEN** `B.md` holds `- [ ] review [[Old]]`, `move_note("Old.md", "New.md", rewrite_links=True)` rewrites it, and `list_tasks` is called before any pass
- **THEN** the listing SHALL show `review [[New]]` with the id derived from the new text, and `complete_task` with that id SHALL succeed

#### Scenario: A write to a note without checkboxes does no refresh
- **WHEN** `edit_note` replaces a note whose old and new bytes contain no line-start checkbox
- **THEN** no refresh SHALL be attempted, no database statement SHALL be issued on behalf of the task rows, and the usage row SHALL carry no `task_refresh` key

#### Scenario: An unguarded overwrite assumes the incumbent held tasks
- **WHEN** `write_file(path="A.md", overwrite=True)` without `expected_hash`, in a deployment that does not require one, replaces an indexed note with bytes holding no checkbox
- **THEN** the refresh SHALL run, and the note's task rows SHALL be removed

#### Scenario: A plain move needs no refresh
- **WHEN** `move_note("A.md", "Sub/A.md")` moves a note with tasks, without link rewrites, and `list_tasks` is called before any pass
- **THEN** no refresh SHALL be attempted, and the tasks SHALL be listed under `Sub/A.md` and not under `A.md`

#### Scenario: A created note waits for the pass
- **WHEN** `create_note` creates a note with tasks
- **THEN** no refresh SHALL be attempted, and the tasks SHALL be listed after the next pass

#### Scenario: The gate is a superset of the extractor
- **WHEN** `may_hold_tasks` and `tasks_from_note_text` run over the extractor test corpus and generated inputs, including CRLF and lone-CR notes, a U+0000 inside the brackets, blockquote and callout prefixes, ordered markers and a final line without a terminator
- **THEN** no input SHALL yield a task while the gate returns false

#### Scenario: The gate is linear
- **WHEN** `may_hold_tasks` runs on 10 MiB of `- - - …`, of `> > > …`, or of digits without a delimiter
- **THEN** it SHALL return in under 2 seconds

### Requirement: `delete_note` SHALL clear the deleted note's task rows when it may have held a task

After a confirmed soft or permanent delete, and before its change-feed event, `delete_note` SHALL call the refresh's absence form when the call read the incumbent and `tasks.may_hold_tasks(incumbent)` is true, or when it did not read the incumbent. The absence form SHALL run R1 with these differences:

- under the row lock, it SHALL confirm through the anchored target that nothing is at the path. If something is, it SHALL write nothing and report `superseded`;
- it SHALL delete the note's `note_tasks` rows;
- it SHALL set `tasks_source_hash` to the canonical hash of the empty note, `tasks_refreshed_at` to the database clock, and `tasks_failed` and `tasks_truncated` to false.

It SHALL write no other column, and SHALL NOT delete the `notes_metadata` row.

#### Scenario: A deleted note's tasks leave the listing at once
- **WHEN** `delete_note("Done.md")` deletes a note with three open tasks, and `list_tasks` is called before any pass
- **THEN** none of the three SHALL be listed, and the next pass SHALL prune the row

#### Scenario: A recreated path is not cleared
- **WHEN** `delete_note` deletes `A.md`, another writer creates `A.md` before the clear takes the row lock, and the clear then runs
- **THEN** the clear SHALL write nothing and report `superseded`

#### Scenario: A guarded delete of a note without checkboxes does nothing more
- **WHEN** `delete_note(path, expected_hash=…)` deletes a note whose bytes hold no line-start checkbox
- **THEN** no clear SHALL be attempted

### Requirement: A generic writer's task refresh SHALL NOT change the writer's response, outcome or event, and SHALL be recorded only in the usage row

For every generic writer that refreshes or clears, the response text SHALL be byte-identical to the response the same call gives when no refresh runs, whatever the refresh reports. The outcome classification (disposition, marker, refusal) SHALL be unchanged, and the refresh SHALL NOT raise into the tool.

The change-feed event SHALL be recorded after the refresh returns, whatever it reported.

The usage row SHALL carry the result-side key `task_refresh` only when a refresh or clear was attempted:

- `refreshed` or `lagged:<code>` (with the existing codes) for a single-note writer;
- for `move_note`: `lagged:<code>` of the first refresh in the call that lagged; otherwise `deferred` when the per-call bound left a gated-in rewrite unrefreshed; otherwise `refreshed`.

No tool name, argument, schema, title, annotation or description SHALL change, and `tests/fixtures/tools_list_full_v2.json` SHALL match unchanged.

#### Scenario: A lagged refresh is invisible in the response
- **WHEN** another transaction holds a note's row lock for longer than 2 seconds and `edit_note` changes a task in it
- **THEN** the file SHALL carry the edit, the response SHALL be byte-identical to the response without a refresh, the outcome SHALL be a success, the usage row SHALL record `task_refresh = lagged:lock_timeout`, and the next pass SHALL bring the listing up to date

#### Scenario: Every refresh code leaves the response unchanged
- **WHEN** each of `edit_note`, `set_frontmatter`, `write_file` and `delete_note` runs with the refresh forced to return each code in `task_index.LAG_CODES` and `refreshed`
- **THEN** each tool's response text SHALL be the same for every code

### Requirement: The work a generic writer's task refresh adds SHALL be bounded per call

A single-note writer SHALL attempt at most one refresh or clear per call. Its derivation and the gate SHALL run off the event loop. Its row-lock wait SHALL be the existing 2-second bound.

`move_note(rewrite_links=True)` SHALL attempt refreshes for gated-in rewrites in publication order, and only while all of these hold:

- fewer than `TASK_REFRESH_MAX_NOTES_PER_CALL` (32) refreshes have been attempted in the call;
- the published bytes of the notes already refreshed, plus the current one, total at most `MAX_NOTE_BYTES`;
- no earlier refresh in the call reported `lock_timeout` or `error`.

Every later gated-in rewrite SHALL be deferred to the index pass.

A note with more than `MAX_TASKS_PER_NOTE` tasks SHALL be stored truncated with `tasks_truncated` set, as the pass stores it.

#### Scenario: A move over many task notes is capped
- **WHEN** `move_note(rewrite_links=True)` publishes rewrites of 40 backlink sources that each hold a task
- **THEN** exactly 32 refreshes SHALL be attempted, the usage row SHALL record `task_refresh = deferred` when none lagged, and the next pass SHALL derive the other 8

#### Scenario: A lock timeout stops further refreshes in the call
- **WHEN** the first gated-in rewrite's refresh in a `move_note` call reports `lock_timeout`
- **THEN** no further refresh SHALL be attempted in that call, and the usage row SHALL record `task_refresh = lagged:lock_timeout`

#### Scenario: A maximal note
- **WHEN** `edit_note` publishes exactly `MAX_NOTE_BYTES` holding 5,001 checkboxes
- **THEN** the refresh SHALL store 5,000 task rows with `tasks_truncated = true`, and no single event-loop step SHALL exceed 100 ms on account of the gate or the derivation

### Requirement: Task rows written by a generic writer's refresh SHALL converge with the index pass under the existing rules

The index pass SHALL NOT change. Its keep rule (a row refreshed at or after the scan's start whose `tasks_source_hash` differs from the buffered hash keeps its task rows), and its rule that a row whose `tasks_source_hash` differs from its `content_hash` is never stat-shortcut, SHALL apply to rows written by a generic writer's refresh or clear exactly as to a task tool's.

After the last write to a note, the first pass whose scan starts after both that write and the last refresh SHALL leave `tasks_source_hash` equal to `content_hash`, and the task rows equal to the extraction of the file on disk.

#### Scenario: Refreshes of different writers finishing out of order
- **WHEN** `edit_note` publishes B1 and pauses before its refresh, `complete_task` then publishes B2 and refreshes, and the `edit_note` refresh then runs
- **THEN** the `edit_note` refresh SHALL write nothing and its usage row SHALL record `task_refresh = lagged:superseded`, and the task rows SHALL describe B2

#### Scenario: An outside edit after a refresh is picked up
- **WHEN** `edit_note` refreshes a note from B1, a sync then writes B2 to the file, and two passes run
- **THEN** after the second pass at the latest, the task rows SHALL describe B2 and `tasks_source_hash` SHALL equal `content_hash`

#### Scenario: Another user's note at the same path is untouched
- **WHEN** users 7 and 8 each have `Notes/a.md` with tasks, and user 7 edits and then deletes theirs
- **THEN** user 8's task rows and `tasks_*` columns SHALL be unchanged
