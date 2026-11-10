## MODIFIED Requirements

### Requirement: Tasks-plugin fields SHALL be parsed from the task line, and an unparseable value SHALL NOT fail the note

For each task, the extractor SHALL parse each of the following dates, written as the signifier, an optional U+FE0F, optional horizontal whitespace, and `YYYY-MM-DD` not followed by a digit:

- due (📅, 📆, 🗓);
- scheduled (⏳, ⌛);
- start (🛫);
- done (✅);
- created (➕);
- cancelled (❌).

It SHALL also parse:

- **Priority**: 🔺 highest, ⏫ high, 🔼 medium, 🔽 low, ⏬ lowest, with an optional U+FE0F.
- **Inline tags** (the shared inline-tag rule). A tag longer than 128 characters SHALL NOT be stored. The first 64 distinct remaining tags in document order SHALL be stored in document order. The row's `tags_cut` SHALL be true exactly when any tag on the line was not stored for either reason.
- **Tag prefixes**: each stored tag is lower-cased, has every run of `/` collapsed to one, and has a trailing `/` stripped. Its prefixes are that normalised tag cut immediately before each `/`, plus the whole normalised tag. The array is deduplicated and never capped. A tag of at most 128 characters has at most 64 non-empty segments, since 2k − 1 ≤ 128, so the array holds at most 64 × 64 = 4,096 entries.
- **Recurrence rule**: the value of a `🔁` field in the line's trailing field run, identified as the Tasks plugin parses it, cut at 256 characters with `recurrence_cut`.
- **Description key**: the 16-hex normalised-description key of the `task-write` capability.
- **Folded text**: the stored text with Unicode case folding applied.
- **Context**: at most 8 lines, each cut to 160 characters, 600 characters in total, with `context_cut`. These are the non-blank lines directly below the task that have a greater blockquote depth, or the same blockquote prefix with a greater indentation width (a tab counts as 4). The run stops at a blank line, a heading, a fence line or the cap. This cap bounds only what is stored and rendered; write placement uses the uncapped continuation block of the `task-write` capability.
- **Task key, ordinal and count** of the `task-write` capability.

Dates and priority SHALL be read from the masked text of the line, so a signifier inside inline code is not a field. When a date or priority family occurs more than once, the last occurrence SHALL win. A date that is not a valid calendar date, or a signifier with no date, SHALL store NULL for that field, and the task SHALL still be stored with its full text. A field value SHALL never cause an extraction error or a quarantine. With no priority signifier, the priority SHALL be NULL.

#### Scenario: Dates, priority and tags are parsed
- **WHEN** a task reads `- [ ] ship 📅 2026-11-01 ⏳ 2026-10-30 🛫 2026-10-28 ⏫ #work #alpha`
- **THEN** due SHALL be 2026-11-01, scheduled 2026-10-30, start 2026-10-28, priority `high`, and tags `work`, `alpha` in that order, with `tags_cut` false

#### Scenario: Created, cancelled and recurrence are parsed
- **WHEN** a task reads `- [-] Rent 🔁 every month ➕ 2026-01-01 📅 2026-11-01 ❌ 2026-11-02`
- **THEN** the recurrence SHALL be `every month`, created 2026-01-01 and cancelled 2026-11-02

#### Scenario: A rule outside the trailing run is not a recurrence
- **WHEN** a task reads `- [ ] the 🔁 every day plan is late [[Notes]]`
- **THEN** its recurrence SHALL be NULL

#### Scenario: Tag prefixes support hierarchical matching
- **WHEN** a task carries `#Waiting-For/Chase`
- **THEN** its stored tags SHALL be `Waiting-For/Chase`, and its tag prefixes SHALL be `waiting-for` and `waiting-for/chase`

#### Scenario: Tag prefixes are lossless at the largest buildable case
- **WHEN** a task carries the 64 distinct tags `#t00/b/b/…/b` … `#t63/b/b/…/b`, each 127 characters long, made of a 3-character first segment and 62 one-character segments
- **THEN** its tag prefixes SHALL hold 64 × 63 = 4,032 entries, `tags_cut` SHALL be false, and `list_tasks(tags=[<any stored tag>])` and `list_tasks(exclude_tags=["t05"])` SHALL agree with the stored tags

#### Scenario: Empty segments are normalised identically on both sides
- **WHEN** a task carries `#Waiting-For//Chase/`, and `list_tasks(tags=["waiting-for//chase"])` and `list_tasks(tags=["waiting-for"])` are called
- **THEN** its tag prefixes SHALL be exactly `waiting-for` and `waiting-for/chase`, and both calls SHALL return the task

#### Scenario: Context lines are captured
- **WHEN** `- [ ] Call bank` is followed by `    - ref 1234`, `    more notes`, a blank line and `- [ ] next`
- **THEN** the task's context SHALL be those two indented lines, and `next` SHALL NOT be part of it

#### Scenario: An invalid date stores NULL and keeps the text
- **WHEN** a task reads `- [ ] pay 📅 2026-02-30`
- **THEN** the task SHALL be stored with due NULL and text `pay 📅 2026-02-30`
- **AND** the note SHALL be indexed normally, with no quarantine and no skip

#### Scenario: A signifier inside inline code is not a field
- **WHEN** a task reads ``- [ ] document the `📅 2026-01-01` syntax``
- **THEN** its due date SHALL be NULL

#### Scenario: The tag cap keeps the first 64 distinct tags and says so
- **WHEN** a task line carries 65 distinct inline tags `#t01` … `#t65`, with `#t01` repeated at the end
- **THEN** the stored tags SHALL be `t01` … `t64` in document order, `tags_cut` SHALL be true, and `t65` SHALL NOT be stored

#### Scenario: An over-long tag is not stored and is flagged
- **WHEN** a task line carries `#ok` and a 129-character tag
- **THEN** the stored tags SHALL be `ok` and `tags_cut` SHALL be true

#### Scenario: The last occurrence wins
- **WHEN** a task reads `- [ ] move 📅 2026-01-01 then 📅 2026-02-01`
- **THEN** its due date SHALL be 2026-02-01

### Requirement: Task rows SHALL be derived state that is written only with their note's row and can never outlive it

The index pass SHALL store tasks in `note_tasks`, keyed by `note_id` with a foreign key to `notes_metadata.id` `ON DELETE CASCADE`. It SHALL NOT store a path or owner on the task row.

**The pass's derivation.** For every changed path of a pass (the upserted and the moved paths), and for every row whose `tasks_source_hash` differs from its `content_hash`, the pass SHALL do the following in the same transaction as the note's row, immediately after that note's link rebuild:

- delete the note's task rows and insert the rows extracted from the body the scan buffered;
- set `tasks_source_hash` to the buffered body's canonical hash and clear `tasks_refreshed_at`.

The exception is a row whose `tasks_refreshed_at` is at or after the attempt's scan start and whose `tasks_source_hash` differs from the buffered hash: its task rows SHALL be kept, because a task tool wrote them from bytes published after the scan began.

**Reading.** The pass SHALL NOT read a file outside the scan. The scan SHALL read every row whose `tasks_source_hash` differs from its `content_hash`, regardless of the stat shortcut. A changed path with no buffered body or no index row SHALL be recorded as a skip that withholds a re-derive's record.

**The only other writer** of a note's task rows SHALL be the write-time task refresh of the `task-write` capability. It holds that note's row lock and derives only from the bytes its own call published.

**Savepoints and isolation.** Each note's task deletion and insertion SHALL run in their own savepoint. A task-derivation failure SHALL be isolated to that note's task rows, as the `index-integrity` requirement on task-derivation isolation states.

**Lifecycle.**

- Deleting a note's row by any route (prune, quarantine removal, provenance discard, user deletion) SHALL remove its task rows.
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

### Requirement: `list_tasks` SHALL answer task queries from the index in one bounded, owner-scoped call

The MCP server SHALL expose a read tool `list_tasks`, registered with a title and the shared read-only annotations and wrapped by `_tracked`. Its arguments are `folder`, `status="open"`, `due_by`, `scheduled_by`, `tags`, `limit=100`, `cursor`, `due_from`, `scheduled_from`, `done_from`, `done_by`, `undated`, `has_due`, `text`, `priority`, `recurring`, `exclude_tags`, `path`, `order_by="due"`, `detail="full"`, `context=false` and `scope="default"`. Every argument after `cursor` is optional and additive. The tool SHALL read only the index and never a file.

**Status.** `status` SHALL be one of `open`, `done`, `cancelled` and `all`. `open` excludes cancelled tasks, `cancelled` returns only them, and `all` returns every state.

**Dates.** Date arguments SHALL be validated against the caller's effective settings and one clock snapshot, so an ordering check is evaluated in the caller's timezone. Every date argument SHALL accept `YYYY-MM-DD` or a relative-date form. `*_from` means on or after, and `*_by` means on or before. A bound SHALL exclude tasks that have no date of that kind. A `from` later than its `by` SHALL be refused with `invalid_argument`.

**Simple filters.**
- `undated=true` SHALL return only tasks with neither a due nor a scheduled date. It SHALL be refused with `invalid_argument` when combined with a due or scheduled bound or with `has_due=true`.
- `has_due` SHALL select tasks with or without a due date.
- `folder` SHALL be a path prefix with the shared note filter's semantics, and `path` SHALL select exactly one note.
- `priority` SHALL be a list drawn from `highest`, `high`, `medium`, `none`, `low` and `lowest`, where `none` means no priority.
- `recurring` SHALL select tasks with or without a recurrence rule.

**`text`.** `text` SHALL be 1–256 characters and at most 8 whitespace-separated terms. Every term, after NFC and Unicode case folding, SHALL occur in the task's folded text.

**`tags` and `exclude_tags`.** Each entry is stripped of one leading `#` and SHALL be 1–128 characters. It is then normalised exactly like a stored tag prefix (lower-cased, `/` runs collapsed, a trailing `/` stripped, so `work/` means `work`), and the result SHALL match `[a-z][a-z0-9_/-]*`. There are at most 32 entries. A row awaiting the version-5 backfill is matched by the same rule over its stored tags. `tags` SHALL require every entry and `exclude_tags` SHALL reject every entry. Both match hierarchically by `/` segment and case-insensitively against the task's own tags, so `waiting-for` matches `waiting-for` and `Waiting-For/x` but not `waiting-forever`. Note-level tags are not consulted.

**`scope`.** `scope="default"` SHALL exclude tasks in notes under the caller's excluded folders, or whose note tags hierarchically match an excluded note tag, after one leading `#` is stripped from a stored note tag. `scope="all"` SHALL exclude nothing.

**Other arguments.**
- `limit` SHALL be clamped to 1–500.
- `order_by` SHALL be one of `due`, `priority`, `scheduled`, `done` and `path`, with the orders of the rendering requirement.
- `detail` SHALL be `full` or `brief`.
- Any invalid argument SHALL be refused with an `MCP-REFUSAL` line carrying `invalid_argument`, before any query.

**Tenancy and consistency.** Rows SHALL be restricted by joining `notes_metadata` under the total owner mapping, so that a caller with no user identifier sees only NULL-owned notes' tasks, and a named user only their own. The page, the counts and the cursor SHALL be read in one consistent snapshot.

#### Scenario: Open tasks across the vault in one call
- **WHEN** 21 notes hold 120 open and 300 done tasks and `list_tasks()` is called
- **THEN** one call SHALL return the first 100 open tasks in order with a `next` cursor, each with its id, and no `read_note` SHALL be needed

#### Scenario: Cancelled tasks are excluded from open and included in all
- **WHEN** a note holds `- [ ] a`, `- [/] b`, `- [-] c` and `- [x] d`
- **THEN** `list_tasks(status="open")` SHALL return `a` and `b` only
- **AND** `list_tasks(status="cancelled")` SHALL return `c` only, rendered with the marker `[-]`
- **AND** `list_tasks(status="all")` SHALL return all four, each line showing its raw marker

#### Scenario: Due filter is inclusive
- **WHEN** tasks are due 2026-10-28, 2026-10-29 and 2026-10-30, one has no due date, and `list_tasks(due_by="2026-10-29")` is called
- **THEN** exactly the first two SHALL be returned

#### Scenario: A window with a lower bound
- **WHEN** tasks are due 2026-11-01, 2026-11-11 and 2026-11-20, today is 2026-11-10 in the caller's timezone, and `list_tasks(due_from="tomorrow", due_by="+7d")` is called
- **THEN** only the task due 2026-11-11 SHALL be returned

#### Scenario: Nested tags match and lookalikes do not
- **WHEN** tasks carry `#waiting-for/bob`, `#Waiting-For` and `#waiting-forever`, and `list_tasks(tags=["waiting-for"])` is called
- **THEN** the first two SHALL be returned and the third SHALL NOT

#### Scenario: Text search is case-insensitive in any script
- **WHEN** a task reads `Позвонить в БАНК`, and `list_tasks(text="банк позвонить")` is called
- **THEN** that task SHALL be returned

#### Scenario: Invalid arguments are refused
- **WHEN** `list_tasks(status="pending")`, `list_tasks(due_by="next week")`, `list_tasks(undated=true, due_by="today")` or `list_tasks(order_by="title")` is called
- **THEN** the response SHALL end with `MCP-REFUSAL {"code":"invalid_argument"…}`

#### Scenario: Tag argument bounds are enforced in the body
- **WHEN** `list_tasks` is called with 33 tags, a tag of 129 characters, or `tags=["#"]`
- **THEN** each call SHALL be refused with `invalid_argument` naming the bound, and no query SHALL run
- **AND** a call with 32 tags of 128 characters each SHALL be accepted

#### Scenario: A tag filter miss caused by the cap is reported
- **WHEN** a task carries 65 distinct tags and `list_tasks(tags=["t65"])` is called
- **THEN** that task SHALL NOT be returned, and the header SHALL report `tasks with cut tags: 1`

#### Scenario: Tenancy
- **WHEN** NULL-owned, user-7 and user-8 notes all hold open tasks, and `list_tasks` runs as user 7
- **THEN** only user 7's tasks SHALL appear, and running with no user identifier SHALL return only the NULL-owned tasks

#### Scenario: Scope excludes and says so
- **WHEN** the caller's settings exclude `Work/Tickets/`, which holds 40 open tasks, and `list_tasks()` is called
- **THEN** none of those tasks SHALL be returned, and the header SHALL state `scope: default` and `excluded by scope: 40 tasks`
- **AND** `list_tasks(scope="all")` SHALL return them

### Requirement: `list_tasks` SHALL page with a keyset cursor bound to its filters and SHALL never exceed the read-response cap

When more rows match than were rendered, the response SHALL say so and SHALL end with a `next:` cursor (version 2). The cursor SHALL encode:

- an owner-scope discriminator;
- a digest of the call's normalised filters, with resolved dates, the scope and the effective exclusion lists;
- the `order_by`;
- the sort key of the last rendered row.

The sort keys are:

| `order_by` | Sort key |
|---|---|
| `due` | `(due or 'infinity', path, line)` ascending |
| `priority` | `(rank, due or 'infinity', path, line)`, with ranks highest 0, high 1, medium 2, none 3, low 4, lowest 5 |
| `scheduled` | `(scheduled or 'infinity', path, line)` |
| `done` | `(done or '-infinity')` descending, then path and line ascending |
| `path` | `(path, line)` |

**Key encoding.**
- No date component SHALL be null.
- The path SHALL be carried verbatim or, when that would not fit the remaining response room or the 2,048-character argument cap, as a hash resolved only against the caller's own rows.
- Neither the cursor nor any rendered line SHALL contain a `notes_metadata.id` or another database-global identifier.

A call with a cursor SHALL return rows strictly after that key in that order.

**Refusals.** A cursor SHALL be refused with `invalid_argument` when it does not decode, has the wrong version or shape, carries an owner discriminator other than the caller's, or was issued for different filters, a different order or different settings. A hashed-form cursor naming a path the caller no longer has indexed SHALL be refused the same way. A version-1 cursor is refused, because its version is wrong. The cursor argument SHALL be capped at 2,048 characters.

**Response cap.** Rendering SHALL stop before the response would exceed `MAX_READ_RESPONSE_CHARS`. The cursor SHALL then be issued from the last row actually rendered, so that no matching row is skipped between pages of an unchanged index.

#### Scenario: Paging reaches every row in every order
- **WHEN** 1,234 open tasks exist and a caller follows `next` until none is returned, for each `order_by`
- **THEN** the union of the pages SHALL be exactly the 1,234 tasks, each once, in that order, on an index that did not change between calls

#### Scenario: A page boundary between two undated tasks
- **WHEN** three undated open tasks exist and `list_tasks(limit=1)` is called, followed by its `next` cursor twice
- **THEN** the three calls SHALL return the three tasks, one each, in order, and the third response SHALL carry no cursor

#### Scenario: The character cap stops a page early
- **WHEN** 500 tasks of 900-character text match and `limit=500`
- **THEN** the response SHALL be at most `MAX_READ_RESPONSE_CHARS` characters, SHALL say `more: yes`, and its cursor SHALL resume at the first unrendered task

#### Scenario: A cursor from another owner is refused
- **WHEN** user 7 obtains a cursor from `list_tasks(limit=1)` and user 8 (or an ownerless caller) passes it with the same filters
- **THEN** the call SHALL be refused with `invalid_argument` and SHALL return no rows

#### Scenario: No global identifier is exposed
- **WHEN** a cursor is decoded and every line of a response is inspected, including an abbreviated row with a cut path
- **THEN** neither SHALL contain the `notes_metadata.id` of any note

#### Scenario: A long path still pages
- **WHEN** the boundary task's path is 1,024 characters of three-byte UTF-8 characters, or `MAX_READ_RESPONSE_CHARS` is 1,000
- **THEN** the issued cursor SHALL be at most 2,048 characters and fit the response, and following it SHALL return the next task

#### Scenario: A cursor from another query is refused
- **WHEN** a cursor issued for `status="open"` is passed with `status="done"`, or one issued for `order_by="due"` is passed with `order_by="priority"`
- **THEN** the call SHALL be refused with `invalid_argument`

#### Scenario: A version-1 cursor is refused
- **WHEN** a cursor issued before this change is passed
- **THEN** the call SHALL be refused with `invalid_argument`

### Requirement: `list_tasks` SHALL render one server-authored line per task and SHALL always state what it could not see

**Header line 1.** The response SHALL begin with one header line that always carries:

- the count shown and whether more rows exist;
- the number of the caller's notes not yet scanned for tasks (indexed at an extraction version below 4);
- the number of the caller's matching notes whose tasks could not be indexed (`tasks_failed`);
- the number whose task extraction was capped;
- the number of matching task rows whose task id is pending (a NULL `task_key`, awaiting the version-5 backfill).

Each count is stated even when it is zero. When `tags` or `exclude_tags` is given, line 1 SHALL also carry the number of tasks matching every other filter whose tags were cut. When `text` is given, it SHALL also carry the number of such tasks whose text was cut. Echoed values on line 1 SHALL be cut at 64 characters, and line 1 SHALL be at most 480 characters.

**Header line 2, the query line.** A second header line SHALL be present whenever a relative date was resolved, any argument after `cursor` was given, or the default scope excludes anything. It SHALL be at most 600 characters. It SHALL always state:

- the timezone and today's date in it;
- every resolved date, never cut;
- the scope;
- the number of tasks excluded by scope;
- the number of excluded folders and excluded note tags.

It SHALL name the excluded items while they fit.

**Full rows.** With `detail="full"`, each task SHALL be rendered as exactly one line holding, in order:

1. the raw marker;
2. the path and line;
3. `id <id>` whenever the row's `task_key` is populated, whatever the note's extraction version, otherwise `id pending`;
4. any present dates and the priority;
5. the tags and the heading path;
6. the text, last.

**Brief rows.** With `detail="brief"`, each task SHALL be one line holding the marker, the id, the path and line, the dates, the priority, the tags (cut to 48 characters) and the description (cut to 80 characters). It SHALL be at most 260 characters plus the escaped path, and the path SHALL never be cut except in an abbreviated first row.

**Context.** With `context=true`, a task's stored context lines SHALL follow it, each prefixed by four spaces and `┆ `.

**Escaping and labels.** Every note-controlled field (text, heading path, tags, context) SHALL be rendered with every whitespace run, line breaks included, collapsed to one space. A path SHALL be rendered through one escaping helper that escapes each such character, doubles each literal backslash and escapes each backtick as `\u0060`, injectively, so that a file name cannot close the backtick-delimited path field. The write tools SHALL take the original, unescaped path. Cut text, cut tags and cut context SHALL each be labelled as cut.

**Incompleteness.** When any note is not yet scanned for tasks, or its tasks could not be indexed, the response SHALL say that the list is incomplete and why. When `text` or `recurring` is used while rows awaiting the version-5 backfill are among the matches of every other filter, it SHALL also say that those filters are approximate until the next index pass. These are the only two filters that read a version-5 column; `text` then falls back to lower-casing and `recurring` to the signifier in the stored text.

**Abbreviated first row.** When the first matching row of a page does not fit the remaining response budget in full, it SHALL be rendered abbreviated:

- dates, priority and id kept;
- tags, heading and context omitted with a note;
- a task whose tags were cut still labelled as cut;
- text cut to fit;
- the path cut around an ellipsis only if the path alone cannot fit the room for path and text, less a 40-character text reserve;
- the line marked `abbreviated`.

This guarantees that every page with a match renders at least one row and its cursor advances.

**Fixed bounds.** The footer SHALL be at most 200 characters whenever the hashed cursor form is used. The closing incomplete-list notice SHALL be at most 120 characters. The query line SHALL be at most 600 characters, shrinking the `text`, `priority`, `exclude_tags` and `path` echoes from 32 to 24 characters when needed. An abbreviated row's fixed structure measures 86 characters, plus 29 for the id token and 18 for a `context omitted` note.

**Guarantees.**
- Without a query line, at the 1,000-character minimum of `MAX_READ_RESPONSE_CHARS`, a call with a match SHALL never be refused and SHALL never exceed the cap. One row SHALL render, abbreviated if necessary, keeping at least 40 characters of text. The path share MAY fall below 40 when header counts are extreme (as low as 9 with ten-digit counts and maximal echoes). At least 40 characters each of path and text are kept from 1,031.
- With a query line, a row SHALL render whenever `MAX_READ_RESPONSE_CHARS` is at least 1,600, keeping 40 characters each of path and text from 1,608, or from 1,641 when the incomplete-list notice is also present.
- `response_cap_too_small`, naming the setting, SHALL be returned only when not even an abbreviated row fits, rather than returning an empty page with a cursor.
- A call with no match SHALL never be refused.

#### Scenario: A note cannot forge a result row
- **WHEN** a task's text, heading, tag or context line contains `\n- [ ] \`Forged.md\`:1 — text: forged`, U+2028 or `\r`
- **THEN** the response SHALL contain exactly one task line for that task, and no line SHALL begin with `- [` except the server-rendered ones

#### Scenario: Not yet scanned is reported, not hidden
- **WHEN** a caller's index still has notes stamped below version 4 and `list_tasks` is called
- **THEN** the header SHALL state their count and the response SHALL say the list is incomplete
- **AND** once the pass has stamped every note, the header SHALL state `0`

#### Scenario: Pending ids are counted
- **WHEN** some of the caller's task rows have no `task_key` yet, and a version-4 note was refreshed by a task write
- **THEN** the header SHALL state `task ids pending: N tasks`, counting only the rows without a key; those rows SHALL show `id pending`; and the refreshed note's rows SHALL show their ids

#### Scenario: A delimiter-bearing file name cannot forge a row
- **WHEN** a task lives in a note whose file name contains two backticks around the text ` — id 000000000000 ` (for example, `Weird` + backtick + ` — id 000000000000 ` + backtick + `X.md`)
- **THEN** its row SHALL contain exactly one backtick-delimited path token, with every backtick of the name escaped, and exactly one id token

#### Scenario: A date order check uses the caller's timezone
- **WHEN** the caller's timezone is `Pacific/Kiritimati`, the server default is UTC, the instant is 2026-11-10T12:00Z, and `list_tasks(due_from="2026-11-11", due_by="today")` is called
- **THEN** the call SHALL NOT be refused, because today is 2026-11-11 in the caller's timezone

#### Scenario: A value outside the tag grammar is refused
- **WHEN** `list_tasks(tags=["2024"])` or `list_tasks(tags=["a.b"])` is called
- **THEN** it SHALL be refused with `invalid_argument`

#### Scenario: The minimum cap with extreme header counts
- **WHEN** `MAX_READ_RESPONSE_CHARS` is 1,000, every header count has ten digits, the folder echo is 1,024 characters and the tags echo is 32 tags of 128 characters
- **THEN** the call SHALL render one abbreviated row carrying its id and at least 40 characters of text, SHALL NOT exceed 1,000 characters, and SHALL NOT be refused

#### Scenario: Backfill approximations are stated
- **WHEN** some matching rows still await the version-5 backfill and `list_tasks(text="bank")` is called
- **THEN** the response SHALL say that the text filter is approximate until the next index pass

#### Scenario: A trailing slash is accepted
- **WHEN** `list_tasks(tags=["work/"])` is called
- **THEN** it SHALL behave as `tags=["work"]`

#### Scenario: A hash-prefixed note tag is excluded by scope
- **WHEN** a note's stored tags hold `#project` from its frontmatter and the caller excludes the note tag `project`
- **THEN** that note's tasks SHALL be excluded by the default scope and counted

#### Scenario: A first row that cannot fit is abbreviated, not lost
- **WHEN** `MAX_READ_RESPONSE_CHARS` is 1,000, no query line applies, and the only matching tasks have a 1,024-character path, 1,000 characters of text and 64 tags of 128 characters
- **THEN** each page SHALL be at most 1,000 characters, SHALL render exactly one row marked `abbreviated` carrying its id, and SHALL carry a cursor that reaches the next task, until every task has been returned once

#### Scenario: A query line under a tiny cap is refused, not silent
- **WHEN** `MAX_READ_RESPONSE_CHARS` is 1,000, the caller's settings exclude a folder, and no abbreviated row fits
- **THEN** the call SHALL be refused with `response_cap_too_small`

#### Scenario: The query line states the time zone and resolved dates
- **WHEN** a caller whose timezone is `Asia/Tokyo` calls `list_tasks(due_by="+7d")`
- **THEN** the query line SHALL state `timezone: Asia/Tokyo`, today's date in Tokyo and `+7d=` followed by the resolved date

#### Scenario: Brief rows stay within their bound
- **WHEN** `list_tasks(detail="brief")` renders a task with four dates, a priority, 64 tags and 1,000 characters of text
- **THEN** its line SHALL be at most 260 characters plus its escaped path, and SHALL contain its id

#### Scenario: Notes whose tasks could not be indexed are counted
- **WHEN** one of the caller's notes has `tasks_failed = true`
- **THEN** every `list_tasks` header SHALL report `tasks could not be indexed: 1 notes`, and the response SHALL say the list is incomplete

#### Scenario: Empty result
- **WHEN** no task matches
- **THEN** the response SHALL be the header with zero shown and no cursor, and SHALL NOT be a refusal

## ADDED Requirements

### Requirement: Relative dates SHALL be resolved once per call in the caller's task timezone and echoed

Every date argument of `list_tasks`, `task_agenda`, `complete_task`, `update_task` and `add_task` SHALL accept, case-insensitively:

- `YYYY-MM-DD`;
- `today`, `tomorrow` and `yesterday`;
- `+Nd` and `-Nd`, with N from 0 to 3660.

Relative forms SHALL be resolved against the current date in the caller's effective task timezone, from a clock read once per call, so every relative date and the echoed `today` of one response describe the same instant. A malformed date SHALL be refused with wording that names both the `YYYY-MM-DD` form and the relative forms. Each response SHALL state the timezone and every date it resolved. Any other form SHALL be refused with `invalid_argument`.

#### Scenario: Today differs by time zone
- **WHEN** the instant is 2026-11-10T23:30Z, and one caller's timezone is `UTC` and another's is `Pacific/Kiritimati`
- **THEN** `today` SHALL resolve to 2026-11-10 for the first caller and 2026-11-11 for the second

#### Scenario: Out-of-range offsets are refused
- **WHEN** a date argument is `+3661d` or `next friday`
- **THEN** the call SHALL be refused with `invalid_argument`

### Requirement: `task_agenda` SHALL return disjoint, counted sections from one snapshot, each with an exact continuation

The MCP server SHALL expose a read tool `task_agenda(date="today", days=7, folder=None, tags=None, exclude_tags=None, scope="default")`. It is registered with a title and the shared read-only annotations, wrapped by `_tracked`, and in the `scan` concurrency class.

Let D be the resolved date, with `days` from 1 to 31. The tool SHALL return, from one read-only snapshot and for open tasks under the owner mapping, the scope and the given filters, these sections:

| Section | Predicate | Cap |
|---|---|---|
| overdue | due before D | 25 |
| due today | due on D | 25 |
| scheduled and now due, no due date | scheduled on or before D, no due date | 15 |
| scheduled and now due, due later | scheduled on or before D, due after D+days | 15 |
| upcoming | due from D+1 through D+days, grouped by due date | 50 |

Sections render in brief rows with ids, and no task SHALL appear in more than one section. Each section header SHALL state its total count. A section with more rows than shown SHALL end with the `list_tasks` arguments that continue it, with dates resolved. When the section shows at least one row, it SHALL add a version-2 cursor minted for exactly that `list_tasks` query from the last row shown. A section that shows no row SHALL give the arguments without a cursor, and a cursor SHALL never be minted except from a shown row. The header SHALL carry the timezone, the date, `days`, the scope, the number excluded by scope and the `list_tasks` header counts.

A section that does not fit the response cap SHALL render its header with `not shown (response cap)` and its continuation. When `MAX_READ_RESPONSE_CHARS` is below 2,800, the call SHALL be refused with `response_cap_too_small`.

#### Scenario: Every open task in the horizon appears exactly once
- **WHEN** a generated set of open tasks covers every combination of due date and scheduled date relative to D (none, before, on, within the horizon, after the horizon)
- **THEN** each task whose due date is on or before D+days, or whose scheduled date is on or before D, SHALL appear in exactly one section, and no other task SHALL appear

#### Scenario: A capped section continues exactly
- **WHEN** 40 tasks are overdue and `task_agenda()` is called
- **THEN** the overdue section SHALL show 25 rows and state 40, and calling `list_tasks` with the stated arguments and cursor (and the same folder, tags, exclude_tags and scope) SHALL return exactly the remaining 15, in order

#### Scenario: A section with no room shows its continuation without a cursor
- **WHEN** the overdue section fills the response budget and the due-today section holds 5 tasks
- **THEN** the due-today section SHALL state 5, show `not shown (response cap)`, and give `list_tasks` arguments with no cursor that return exactly those 5 tasks

#### Scenario: Agenda respects the caller and the scope
- **WHEN** user 7 calls `task_agenda` while user 8 has overdue tasks and user 7's settings exclude `Archive/`
- **THEN** no user-8 task and no task under `Archive/` SHALL appear, and the header SHALL state the number excluded by scope
