## ADDED Requirements

### Requirement: Every task SHALL carry an id derived from its own uncut text, with an ordinal-of-count suffix when a note repeats it

The extractor SHALL compute, for each task, `task_key` as the first 12 lowercase hex characters of SHA-256 over the bytes `omcp-task-v1`, a NUL byte, and the UTF-8 encoding of the task's uncut text. The uncut text is the original line after the closing `]` and its following horizontal whitespace, with trailing whitespace removed, before the 1,000-character cut. The status marker, indentation, list marker and blockquote prefix SHALL NOT enter the key.

Within one note's extracted tasks, in document order and under the same per-note cap, `key_count` SHALL be the number of tasks sharing the key and `key_ordinal` the task's 1-based rank among them. The rendered id SHALL be the bare key when `key_count` is 1, and `<key>.<ordinal>of<count>` otherwise. The key, ordinal and count SHALL be stored on the task row. Nothing SHALL ever be written into a note to establish identity.

#### Scenario: The id survives the text cut
- **WHEN** a task's text is 1,500 characters long
- **THEN** its stored `text` SHALL be cut at 1,000 characters, and its `task_key` SHALL equal the key computed over all 1,500 characters

#### Scenario: Identical lines get ordinals
- **WHEN** a note holds three lines `- [ ] Call` and one line `- [x] Call`
- **THEN** all four SHALL share one key, with ids `<key>.1of4` … `<key>.4of4` in document order

#### Scenario: Indentation and status do not change the key
- **WHEN** `- [ ] Pay rent` is re-indented under a parent, or its marker changes to `[/]`
- **THEN** its key SHALL be unchanged

### Requirement: A task write SHALL resolve its target from the note's current bytes and SHALL refuse, writing nothing, unless exactly the expected line matches

`complete_task` and `update_task` SHALL take a note `path` and a task `id`. Each SHALL read the note through the anchored descriptor that will be published to, and re-derive every task with the same function the index pass uses. The id SHALL then be resolved against those bytes only:

- no matching key → `task_not_found`;
- a bare key matching more than one task → `task_ambiguous`;
- a suffixed id whose count differs from the current count → `task_changed`.

A suffixed id SHALL be accepted only with an `expected_hash` that matches the note's current bytes, in every deployment mode. Without one, the call SHALL be refused with `precondition_required` before any read, and the refusal SHALL say to `read_note` the note and pass its `content_hash`. With a matching hash, the ordinal SHALL be relative to exactly those bytes.

No line number from the index, from a listing or from an earlier read SHALL be used to locate the line. Publication SHALL be bound to the bytes the id was resolved against.

**Retry on conflict.** When publication reports that the file changed during the call and the caller supplied no `expected_hash`, the tool SHALL repeat the whole sequence from the read, up to 3 attempts in total: re-read, re-resolve, re-plan, re-verify, publish. It SHALL carry nothing over from an earlier attempt. After the third conflict it SHALL refuse with `concurrent_write`. With `expected_hash` supplied it SHALL NOT retry. A note whose extraction exceeds the per-note task cap SHALL be refused with `size_limit`. Every refusal SHALL leave the file byte-identical. A task-specific refusal code SHALL carry the explicit nothing-written marker. A generic code reused from `edit_note` (`not_found`, `validation_failed`, `size_limit`, `io_failure`, and the `content_unsafe` payload) SHALL keep its existing payload, as in `edit_note`, and nothing SHALL be written in those cases either.

#### Scenario: A short text does not match inside a longer one
- **WHEN** a note holds `- [ ] Call Bob` and `- [ ] Call Bob about the lease`, and `complete_task` names the id of `Call Bob`
- **THEN** only the line `- [ ] Call Bob` SHALL change

#### Scenario: A bare key over duplicates is refused
- **WHEN** a note holds two lines `- [ ] Water plants` and `complete_task` is called with the bare key
- **THEN** it SHALL be refused with `task_ambiguous`, the response SHALL list both suffixed ids, and the file SHALL be unchanged

#### Scenario: A suffixed id without a hash is refused
- **WHEN** a listing returned `<key>.2of2` and `complete_task` is called with it and no `expected_hash`, on a deployment that does not require preconditions
- **THEN** it SHALL be refused with `precondition_required`, telling the caller to `read_note` for `content_hash`, and the file SHALL be unchanged

#### Scenario: Swapped identical lines cannot redirect a suffixed id
- **WHEN** two identical `- [ ] Call` lines with different context below them swap places after the caller read the note, and `complete_task` is called with `<key>.1of2` and the old `content_hash`
- **THEN** it SHALL be refused with `stale_precondition` and the file SHALL be unchanged
- **AND** the same call with the current `content_hash` SHALL complete the first occurrence in the current bytes

#### Scenario: A line changed since the listing is not found
- **WHEN** the task's text was edited after the listing and `update_task` is called with the old id
- **THEN** it SHALL be refused with `task_not_found` and the file SHALL be unchanged

#### Scenario: A concurrent edit with a hash is refused, not retried
- **WHEN** another writer changes the note after `update_task` (called with a matching `expected_hash`) read it and before it published
- **THEN** the call SHALL be refused, and the other writer's bytes SHALL be on disk unchanged

#### Scenario: Parallel completions of different tasks all succeed
- **WHEN** three `complete_task` calls without `expected_hash`, naming three different tasks of one note, are issued at once
- **THEN** all three SHALL succeed, and the note SHALL contain all three completions and every other byte unchanged

#### Scenario: Parallel completions of the same task succeed once
- **WHEN** two `complete_task` calls name the same task at once
- **THEN** exactly one SHALL succeed, and the other SHALL be refused with `already_completed` or `task_not_found`

#### Scenario: Persistent conflict gives up after three attempts
- **WHEN** every publication attempt of one call meets an in-call conflict
- **THEN** the call SHALL be refused with `concurrent_write` after its third attempt, having written nothing

#### Scenario: Checkboxes the extractor does not recognise are never targets
- **WHEN** a note holds `- [ ] example` inside a fenced block, or `- [ ] x` inside its YAML frontmatter
- **THEN** no id SHALL resolve to either line

### Requirement: A task write SHALL change only the fields the call names, in place, and SHALL preserve every other byte

A task write SHALL replace only the token spans its call names, according to these rules:

- **Changing a value** replaces only the value characters, keeping the signifier as written, including variant signifiers and U+FE0F.
- **Removing a field** deletes the token and one adjacent separating space.
- **Adding a field** inserts one space, the plugin's write signifier, one space and the value into the trailing field run. It goes before the first existing field of higher canonical rank when the run is in the canonical order (id, dependsOn, priority, recurrence, onCompletion, created, start, scheduled, due, cancelled, done). Otherwise it goes at the end of the run. In both cases it goes before a trailing `^block` link and before trailing whitespace.
- **Adding a tag** places it at the end of the description region.
- **Removing a tag** removes whole-tag, case-insensitive matches only.
- **Replacing the description** replaces its words only. Every tag of the old description region that does not occur in the new text is re-appended at the end of the new description region, in its original order and spelling. A tag present in the new text is not duplicated. `[[links]]` are ordinary description text and are replaced with it. Only `remove_tags` removes a tag.

The trailing field run SHALL be identified as the Tasks plugin parses it: block link first, then repeated end-anchored field and trailing-tag matches.

The indentation, list marker, blockquote prefix, the gap after the box, every untouched token, the order of the run, the rule text unless the call changes it, the trailing whitespace and the line terminator SHALL be preserved byte for byte. Every other line of the note SHALL be byte-identical, except for an inserted next-occurrence or context line. A write SHALL NOT re-serialise a line in the plugin's canonical order.

#### Scenario: A non-canonical order is preserved
- **WHEN** `update_task` sets the due date on `- [ ] Pay #home 📅 2026-11-01 🔁 every month ^rent` to 2026-11-05
- **THEN** the line SHALL become `- [ ] Pay #home 📅 2026-11-05 🔁 every month ^rent`, with nothing else changed

#### Scenario: An added field goes to its canonical slot
- **WHEN** `update_task` adds a scheduled date 2026-11-03 to `- [ ] Pay 🔁 every month 📅 2026-11-05`
- **THEN** the line SHALL become `- [ ] Pay 🔁 every month ⏳ 2026-11-03 📅 2026-11-05`

#### Scenario: Prefix, hard break and CRLF are preserved
- **WHEN** `complete_task` completes `> \t1. [ ] Ship it  ` followed by CRLF, in a note whose other lines end in LF
- **THEN** the result SHALL be `> \t1. [x] Ship it ✅ <date>  ` followed by CRLF, and every other byte of the file SHALL be unchanged

#### Scenario: A variant signifier is kept
- **WHEN** `update_task` changes the due date of `- [ ] x 📆 2026-01-01`
- **THEN** the signifier SHALL remain `📆`

### Requirement: A task write SHALL refuse a line it cannot edit unambiguously

A task write SHALL be refused with `content_unsafe`, writing nothing, when the note's frontmatter diagnosis reports a defect (line 1 is exactly `---` and either no closing `---` follows or the YAML between them is invalid). It SHALL also be refused that way for `add_task(section=…)` on an unmatched indented fence. Caller-supplied description or context text SHALL be refused with `invalid_argument` when it holds an odd number of `%%`, an unpaired `<!--` or `-->`, or a run of three or more backticks or tildes. Context text SHALL also be refused that way when it starts with a list marker, a checkbox, a `#` heading or `>`. A note containing NUL SHALL be refused with `task_malformed`. A task write SHALL be refused with `task_malformed`, writing nothing, in any of these cases:

- a field family the call changes, removes or adds occurs more than once in the task text;
- such a family occurs only in the description region, where the Tasks plugin would not parse it;
- the masked and original text differ inside the trailing field run while the call changes a field;
- a date field the call changes holds a value that is not a calendar date;
- for the completion of a recurring task, any start, scheduled, due, created, done or cancelled field is duplicated, stranded or invalid, or the reference date is invalid.

Before any dry-run diff or publication, every plan SHALL be verified, and a failed verification SHALL be refused with `task_malformed`, writing nothing. The verification SHALL check that:

1. the new note equals the old note outside the declared spans;
2. the target and any inserted line re-extract as tasks with exactly the intended status and fields;
3. every other task of the note re-extracts identically, with lines after an insertion shifted by the inserted count;
4. no undeclared task appears;
5. no pre-existing line changes its owning task. A line's owning task is the nearest preceding task whose continuation block contains it;
6. the target's tag set, compared whole-tag and case-insensitively, equals the set before, plus the tags added (through `add_tags`, or typed by the caller inside a replacement `text`), minus the tags removed through `remove_tags`.

A task's **continuation block** is every following line that has a greater blockquote depth, or the same blockquote prefix and a greater indentation width (a tab counts as 4). Blank lines are crossed while a later line still belongs. The block has no line or character cap.

#### Scenario: A duplicated due date is refused
- **WHEN** `update_task` sets the due date on `- [ ] x 📅 2026-01-01 📅 2026-02-01`
- **THEN** it SHALL be refused with `task_malformed` and the file SHALL be unchanged

#### Scenario: A stranded signifier is refused
- **WHEN** `update_task` changes the due date on `- [ ] move 📅 2026-01-01 to the archive`
- **THEN** it SHALL be refused with `task_malformed`

#### Scenario: A context line that would create a task is refused
- **WHEN** `update_task` is called with `append_context="[ ] sneaky"`
- **THEN** it SHALL be refused, and the file SHALL be unchanged

### Requirement: `complete_task` SHALL mark one task done and, for a recurring task, write its next occurrence or refuse

`complete_task(path, id, done_on=None, dry_run=False, expected_hash=None)` SHALL behave as follows:

- It SHALL change the status of an open task (any marker other than `x`, `X` and `-`) to `[x]`.
- When the effective `set_done_date` setting is true, it SHALL add `✅ <done_on>`, or replace the value of a single existing `✅` in the run. `done_on` defaults to today in the caller's task timezone and accepts the relative-date forms.
- A task already `[x]` or `[X]` SHALL be refused with `already_completed`, and a `[-]` task with `task_cancelled`.
- A task whose run carries `🏁 delete` (case-insensitive) SHALL be refused with `on_completion_unsupported`.
- When the task carries a `🔁` rule, the rule SHALL be computed by the closed recurrence grammar, and the next occurrence SHALL be written in the same publication. A rule outside the grammar SHALL be refused with `recurrence_unsupported`, writing nothing.

A task SHALL never be completed without its next occurrence. Every refusal SHALL write nothing.

#### Scenario: A plain completion
- **WHEN** `complete_task` completes `- [ ] Send invoice 📅 2026-11-10` on 2026-11-10 in the caller's timezone, with defaults
- **THEN** the line SHALL become `- [x] Send invoice 📅 2026-11-10 ✅ 2026-11-10`

#### Scenario: Done date off
- **WHEN** the caller's `set_done_date` is false
- **THEN** only the marker SHALL change

#### Scenario: Completing a completed task
- **WHEN** `complete_task` names a `[x]` task
- **THEN** it SHALL be refused with `already_completed` and the file SHALL be unchanged

#### Scenario: A custom marker completes to x
- **WHEN** `complete_task` names `- [>] Forwarded item`
- **THEN** the line SHALL become `- [x] Forwarded item ✅ <date>`, and the response SHALL name the replaced marker `>`

#### Scenario: An unsupported rule refuses the whole completion
- **WHEN** `complete_task` names `- [ ] Review 🔁 every month on the last Friday 📅 2026-11-27`
- **THEN** it SHALL be refused with `recurrence_unsupported`, and the file SHALL be unchanged

#### Scenario: On-completion delete is refused
- **WHEN** the task's run carries `🏁 delete`
- **THEN** the call SHALL be refused with `on_completion_unsupported` and nothing SHALL be written

### Requirement: Recurrence SHALL be a closed grammar of rule texts whose dates match the Tasks plugin's

The recurrence engine SHALL accept, case-insensitively, only these rule texts, with or without a trailing ` when done`, where `N` is an integer from 2 to 999:

- `every day`, `every N days`;
- `every weekday`;
- `every week`, `every N weeks`;
- `every week on <weekday>[, <weekday>…]` and `every <weekday>[, <weekday>…]`, with full English weekday names;
- `every month`, `every N months`;
- `every month on the <K>`, with `1st`–`31st` and the correct English suffix;
- `every month on the last`;
- `every year`, `every N years`;
- `every <month> on the <K>`, with a full English month name and a day valid for that month.

It SHALL refuse every other text with `recurrence_unsupported`, including `every weekend`, `every 1 day` and abbreviations, and SHALL never pass rule text to a general parser.

**Reference date.** The reference date SHALL be the due date, else the scheduled date, else the start date.

**Anchor.** Without `when done`, the anchor is the reference date, and missed occurrences are not skipped. With `when done`, the anchor is the completion date.

**Next date.** The next reference date SHALL be the first date strictly after the anchor that satisfies the rule, where:

- `every N months` with a nonexistent day falls back to the last day of that month;
- `every month on the <K>` skips months lacking day K;
- `every year` from February 29 SHALL be refused with `recurrence_unsupported`.

**Other dates.** Start, scheduled and due SHALL keep their whole-day offset from the reference date. A task with none of these three SHALL still get a next occurrence with no dates.

#### Scenario: Month-end falls back
- **WHEN** a task due 2021-10-31 with `every month` is completed four times, each time on its due date
- **THEN** the successive next due dates SHALL be 2021-11-30, 2021-12-30, 2022-01-30 and 2022-02-28

#### Scenario: Every three months from January 31
- **WHEN** a task due 2022-01-31 with `every 3 months` is completed
- **THEN** the next due date SHALL be 2022-04-30

#### Scenario: A fixed day skips short months
- **WHEN** a task due 2022-01-31 with `every month on the 31st` is completed
- **THEN** the next due date SHALL be 2022-03-31

#### Scenario: Offsets are kept
- **WHEN** `- [ ] x 🔁 every 2 weeks ⏳ 2021-10-28 📅 2021-10-30` is completed
- **THEN** the next occurrence SHALL carry `⏳ 2021-11-11` and `📅 2021-11-13`

#### Scenario: A late completion does not skip missed occurrences
- **WHEN** a task scheduled 2021-02-06 with `every week` and no due date is completed on 2022-02-13
- **THEN** the next occurrence SHALL be scheduled 2021-02-13

#### Scenario: When done anchors on the completion date
- **WHEN** a task due 2026-11-01 with `every 10 days when done` is completed with `done_on` 2026-11-05
- **THEN** the next due date SHALL be 2026-11-15

#### Scenario: Unsupported and lossy rules are refused
- **WHEN** the rule is `every weekend`, `every 2 weeks on Monday`, `every month on the 2nd Wednesday`, or `every year` with the due date 2024-02-29
- **THEN** the completion SHALL be refused with `recurrence_unsupported`

#### Scenario: Weekday lists
- **WHEN** a task due Friday 2026-11-13 with `every week on Tuesday, Friday` is completed
- **THEN** the next due date SHALL be Tuesday 2026-11-17

### Requirement: The next occurrence SHALL be a copy of the original line with only the plugin's changes, placed per the caller's setting

The next-occurrence line SHALL be the completed task's original line, before completion, with only these changes:

- the status set to a space;
- the start, scheduled and due values replaced in place by the shifted dates;
- `✅`, `❌`, `🆔`, `⛔` and a trailing `^block` link removed;
- `➕` set to today in the caller's timezone when `set_created_date` is true, and removed otherwise.

Every other byte SHALL be kept: prefix, field order, rule text, tags, priority, `🏁 keep` and trailing whitespace.

**Placement.** The line SHALL be placed immediately above the completed line when the effective `next_recurrence_below` is false. When that setting is true, it SHALL be placed after the completed task's full continuation block, so no line changes its owning task. In both placements it SHALL use the original task line's own terminator, or, when that line has none, the note's first terminator, else LF. A file without a final terminator SHALL still have none.

#### Scenario: The plugin's dropped fields are dropped
- **WHEN** `- [ ] Rent 🆔 r1 ⛔ a2 🔁 every month ➕ 2026-01-01 📅 2026-11-01 ✅ 2026-10-01 ^b1` is completed on 2026-11-02, with `set_created_date` false and defaults otherwise
- **THEN** the next line SHALL be `- [ ] Rent 🔁 every month 📅 2026-12-01`, placed above the completed line
- **AND** the completed line SHALL keep `🆔 r1`, `⛔ a2`, `➕ 2026-01-01` and `^b1`, with the `✅` value replaced by 2026-11-02

#### Scenario: Below, at end of file without a newline
- **WHEN** the completed task is the last line of a file with no final newline, and `next_recurrence_below` is true
- **THEN** the file SHALL end with the completed line, one terminator, and the next line, with no final terminator

#### Scenario: The terminator follows the original line in a mixed note
- **WHEN** a recurring task whose line ends in CRLF, in a note whose other lines end in LF and whose last context line under the task ends in LF, is completed with `next_recurrence_below` true, and again with it false
- **THEN** in both cases the next-occurrence line SHALL end in CRLF, and every other terminator SHALL be unchanged

#### Scenario: Below keeps the completed task's children
- **WHEN** a recurring task followed by three indented context lines is completed with `next_recurrence_below` true
- **THEN** the next occurrence SHALL be inserted after the third context line, and all three SHALL still belong to the completed task

#### Scenario: Rule text is not canonicalised
- **WHEN** a task with `🔁 every Sunday` is completed
- **THEN** the next line SHALL carry `🔁 every Sunday`, not `every week on Sunday`

### Requirement: `update_task` SHALL change fields, status, description or context of one task and SHALL never complete it

`update_task(path, id, due=None, scheduled=None, start=None, priority=None, add_tags=None, remove_tags=None, recurrence=None, status=None, text=None, append_context=None, clear=None, dry_run=False, expected_hash=None)` SHALL accept these changes:

- **Dates.** It SHALL set due, scheduled or start (an ISO date or a relative-date form).
- **Priority.** It SHALL set the priority (`highest`, `high`, `medium`, `low`, `lowest`).
- **Tags.** It SHALL add and remove tags (each matching the inline-tag grammar, at most 32).
- **Recurrence.** It SHALL set the recurrence rule, which must be accepted by the closed grammar or be refused with `recurrence_unsupported`.
- **Clearing.** It SHALL clear any of `due`, `scheduled`, `start`, `priority` and `recurrence` named in `clear`.
- **Status.** It SHALL change the status to `open` (`[ ]`, removing `✅` and `❌`), `in_progress` (`[/]`, removing `✅` and `❌`) or `cancelled` (`[-]`, removing `✅`, and adding `❌ <today>` when the effective `set_cancelled_date` is true and none exists).
- **Description.** It SHALL replace the words of the description region with `text`, keeping every tag of the old description region (re-appended in original order when `text` does not contain it). The new `text` which must be a single line of 1–1,000 characters, contain no field signifier, `^block` suffix, line break or line-separator character, and not change how the line parses.
- **Context.** It SHALL append one context line under the task (`append_context`, the same text rules), after the task's full, uncapped continuation block. The line SHALL copy the leading text of the task's first shallowest direct child list item when one exists, and otherwise the task's own leading text plus a tab (when the task or the note nests with tabs) or four spaces.

The following SHALL be refused with `invalid_argument`, writing nothing:

- `status="done"`;
- a field both set and cleared;
- a call with no change.

Cancelling SHALL write no next occurrence. A call that changes no byte SHALL answer that there were no changes, with nothing written.

#### Scenario: Reopening removes the done date
- **WHEN** `update_task(status="open")` names `- [x] Ship ✅ 2026-11-01`
- **THEN** the line SHALL become `- [ ] Ship`

#### Scenario: Cancelling a recurring task writes no next occurrence
- **WHEN** `update_task(status="cancelled")` names `- [ ] Rent 🔁 every month 📅 2026-11-01` on 2026-11-02, with defaults
- **THEN** the line SHALL become `- [-] Rent 🔁 every month 📅 2026-11-01 ❌ 2026-11-02`, and no line SHALL be added

#### Scenario: Asking update_task to complete is refused
- **WHEN** `update_task(status="done")` is called
- **THEN** it SHALL be refused with `invalid_argument` naming `complete_task`

#### Scenario: Replacing the description keeps a trailing tag
- **WHEN** `update_task(text="Pay landlord")` names `- [ ] Pay rent #finance 📅 2026-12-01`
- **THEN** the line SHALL become `- [ ] Pay landlord #finance 📅 2026-12-01`

#### Scenario: Replacing the description keeps a tag from its middle
- **WHEN** `update_task(text="Call the agent")` names `- [ ] Call #bob about the [[Lease]] 📅 2026-12-01`
- **THEN** the line SHALL become `- [ ] Call the agent #bob 📅 2026-12-01`, and the `[[Lease]]` link SHALL be gone with the replaced words

#### Scenario: A tag already in the new text is not duplicated
- **WHEN** `update_task(text="Ping #Bob about keys")` names `- [ ] Call #bob about the lease`
- **THEN** the line SHALL become `- [ ] Ping #Bob about keys`

#### Scenario: Text combined with remove_tags
- **WHEN** `update_task(text="Pay landlord", remove_tags=["finance"])` names `- [ ] Pay rent #finance #home 📅 2026-12-01`
- **THEN** the line SHALL become `- [ ] Pay landlord #home 📅 2026-12-01`

#### Scenario: Removing a tag is not hierarchical
- **WHEN** `update_task(remove_tags=["waiting-for"])` names `- [ ] Chase #waiting-for/bob`
- **THEN** the call SHALL change nothing and answer that there were no changes

#### Scenario: A context line is appended under the task's existing context
- **WHEN** `update_task(append_context="called, left message")` names a task followed by two indented context lines
- **THEN** a line `    - called, left message` SHALL be inserted after those two lines, and no other line SHALL change

#### Scenario: A long continuation block is not split by append_context
- **WHEN** a task is followed by 10 continuation lines, including a blank line and a deeper child task, and `update_task(append_context=…)` runs
- **THEN** the new line SHALL be inserted after the tenth line, and no line SHALL change its owning task

### Requirement: `add_task` SHALL insert one new task line next to the region's task list in canonical order, and SHALL never create a note

`add_task(path, text, section=None, due=None, scheduled=None, start=None, priority=None, tags=None, recurrence=None, context=None, allow_duplicate=False, dry_run=False, expected_hash=None)` SHALL refuse a missing note as the existing write tools do, and SHALL never create one.

**Line.** It SHALL build the line `- [ ] ` + `text`, followed by any tags (`#tag`), then priority, recurrence, created date (when `set_created_date` is true), start, scheduled and due, in that order, each as one space, the signifier, one space and the value.

**Region.** The region SHALL be the named section's body, resolved with the same selectors and refusals as the section mode of `edit_note`, or the whole body when no section is given.

**Placement.** The line SHALL be inserted:

1. after the full, uncapped continuation block of the last task line in the region whose indentation after its blockquote prefix is zero, copying that task's blockquote prefix and bullet, with an ordered marker incremented by one;
2. or, with no such task, after the region's last non-blank line;
3. or, for an empty region, after the section heading, or at the start of the body.

A file without a final terminator SHALL keep none.

#### Scenario: Added after the section's task list
- **WHEN** section `Today` holds a paragraph, then `- [ ] a` and `- [ ] b` with an indented context line under `b`, then a blank line and more prose, and `add_task(section="Today", text="c", due="2026-11-12")` runs
- **THEN** `- [ ] c 📅 2026-11-12` SHALL be inserted directly after `b`'s context line, and no other line SHALL change

#### Scenario: A nine-line continuation block is not split
- **WHEN** the last top-level task of the region is followed by nine indented continuation lines and `add_task` runs
- **THEN** the new line SHALL be inserted after the ninth line, and every one of the nine SHALL still belong to that task

#### Scenario: Canonical serialisation
- **WHEN** `add_task(text="Book flights", tags=["travel"], priority="high", recurrence="every year", scheduled="2026-11-20", due="2026-12-01")` runs with `set_created_date` true on 2026-11-10
- **THEN** the line SHALL be `- [ ] Book flights #travel ⏫ 🔁 every year ➕ 2026-11-10 ⏳ 2026-11-20 📅 2026-12-01`

#### Scenario: Never at end of file when the region is a section
- **WHEN** the named section has no tasks and is followed by another section
- **THEN** the line SHALL be inserted after the named section's last non-blank line, not at the end of the file

#### Scenario: No note is created
- **WHEN** `add_task` names a path with no note
- **THEN** it SHALL be refused, and no file SHALL be created

### Requirement: `add_task` SHALL refuse an exact open duplicate in the caller's scope and SHALL list similar open tasks without blocking

`add_task` SHALL normalise a description by:

1. removing the trailing field run and block link (tags kept);
2. applying NFC and Unicode case folding;
3. collapsing whitespace runs.

**Exact duplicates.** It SHALL refuse with `duplicate_task`, writing nothing, when an open task with the same normalised description exists among the target note's current bytes (its index rows are not consulted), or among the caller's other indexed tasks under the default scope. The exception is `allow_duplicate=true`. The refusal SHALL name up to 5 matches.

**Similar tasks.** It SHALL list, in the response and in the dry run, up to 5 similar open tasks from the caller's default scope. These are tasks whose term-set Jaccard similarity is at least 0.5, or whose term set contains (or is contained in) the new task's with at least 2 terms in the smaller set, scanning at most 2,000 candidates and saying so when the bound stops the scan. A similar task SHALL never cause a refusal.

#### Scenario: An exact duplicate elsewhere in scope is refused
- **WHEN** `Projects/Alpha.md` holds `- [ ] Renew passport #admin 📅 2026-12-01`, and `add_task(path="Inbox.md", text="renew  Passport #admin")` runs
- **THEN** it SHALL be refused with `duplicate_task` naming `Projects/Alpha.md` and the task's id, and `Inbox.md` SHALL be unchanged
- **AND** the same call with `allow_duplicate=true` SHALL insert the line

#### Scenario: A similar task is listed, not refused
- **WHEN** an open task `Renew passport for the trip` exists and `add_task(text="Renew passport")` runs
- **THEN** the line SHALL be inserted, and the response SHALL list the similar task

#### Scenario: Duplicate checks never cross owners
- **WHEN** user 8 holds an identical open task and user 7 calls `add_task`
- **THEN** the call SHALL succeed, and user 8's task SHALL appear in neither the refusal nor the similar list

### Requirement: The task write tools SHALL share the write safety envelope of `edit_note`

`complete_task`, `update_task` and `add_task` SHALL each:

- be wrapped by `_tracked` with the write class, so they pass the write rate bucket;
- be named in `WRITE_REQUIRED_TOOLS`;
- require write permission;
- accept `dry_run`, which returns a unified diff and the new line(s) and publishes nothing;
- publish through the confirmed, descriptor-anchored publication path with the in-call conflict check;
- record a `modified` change-feed event attributed to the caller after a confirmed publication;
- return typed body outcomes whose refusals end with one `MCP-REFUSAL` line.

**`expected_hash`.** Each SHALL accept an optional `expected_hash`, compared against the whole file immediately after the in-call read, with the existing codes. When the deployment requires a precondition, `complete_task` and `update_task` with a bare id SHALL treat the resolved `(path, id)` as satisfying it, and `add_task` SHALL be exempt. A suffixed id always requires `expected_hash`.

**Response.** A success response SHALL include:

- the task's new line and its new id;
- for a recurring completion, the next occurrence's line, position and id;
- the published `content_hash`;
- the timezone and resolved dates when a relative date or "today" was used.

Usage logs SHALL NOT record task description or context text.

#### Scenario: Dry run writes nothing
- **WHEN** `complete_task(dry_run=true)` names a recurring task
- **THEN** the response SHALL contain the diff and both new lines, the file SHALL be unchanged, and no event SHALL be recorded

#### Scenario: A read-only credential is refused
- **WHEN** a credential without write permission calls `add_task`
- **THEN** it SHALL be refused with `permission_denied` and nothing SHALL be written

#### Scenario: Required mode is satisfied by the id
- **WHEN** `WRITE_PRECONDITION_REQUIRED=true` and `complete_task` is called with a bare id and without `expected_hash`
- **THEN** it SHALL proceed, and a supplied `expected_hash` that does not match SHALL be refused with `stale_precondition`

#### Scenario: Usage params hold no task text
- **WHEN** `add_task(text="Call the bank about the mortgage", context="ref 1234")` runs
- **THEN** the usage row's `params` SHALL contain neither string

### Requirement: After a successful task write, task listings SHALL reflect it without waiting for the index pass, and a failed refresh SHALL NOT fail the write

After a confirmed publication, and before the change-feed event, each task write tool SHALL refresh the task rows of the note it wrote. The refresh SHALL:

1. derive the tasks from the published bytes before opening its transaction, holding no lock or connection while it does so, and then run in its own transaction;
2. re-check the caller's root binding;
3. lock the caller's `notes_metadata` row for the path within a 2-second lock wait;
4. under that lock, re-read the file through the call's anchored target and confirm it holds exactly the bytes this call published. If it does not, write nothing and report `index: may lag (superseded)`;
5. use the tasks derived in step 1 from the bytes this call published, with the index pass's function;
6. replace that note's `note_tasks` rows;
7. set `tasks_source_hash` to the canonical hash of those bytes and `tasks_refreshed_at` to the database clock.

It SHALL NOT change `content_hash`, the stat columns, `extraction_version` or `derived_under`, and SHALL NOT take the index generation lock.

When the refresh cannot run (lock wait exceeded, no index row, root changed, file superseded, or any error), the write SHALL stand, the outcome SHALL remain a success, and the response SHALL say `index: may lag (<code>)`. A successful refresh SHALL be reported as `index: refreshed`.

#### Scenario: List, complete, list
- **WHEN** `list_tasks` shows an open task, `complete_task` completes it, and `list_tasks` is called again before any index pass
- **THEN** the second listing SHALL show the task as done with its new id, and the next occurrence as open

#### Scenario: A held row lock makes the listing lag, not the write fail
- **WHEN** another transaction holds the note's row lock for longer than 2 seconds during `update_task`
- **THEN** the file SHALL carry the update, the response SHALL say `index: may lag (lock_timeout)`, and the next pass SHALL bring the listing up to date

#### Scenario: Refreshes finishing out of order cannot install older rows
- **WHEN** task write A publishes and pauses before its refresh, task write B then publishes and refreshes the same note, and A's refresh then runs
- **THEN** A's refresh SHALL write nothing and report `index: may lag (superseded)`, and the task rows SHALL describe B's bytes

### Requirement: Task line parsing and planning SHALL be linear-time and SHALL run off the event loop

Parsing a task line's trailing field run SHALL take time linear in the line's length. A 10 MiB adversarial line SHALL parse in under 2 seconds. Planning and verifying a task write SHALL run off the event loop. A plan that changes no byte SHALL answer that there were no changes, and SHALL publish nothing, record no event and run no refresh.

#### Scenario: A 10 MiB line parses quickly
- **WHEN** `task_line.parse` runs on a 10 MiB line of repeated date fields, or of repeated spaces
- **THEN** it SHALL return in under 2 seconds

#### Scenario: A no-op plan writes nothing
- **WHEN** `update_task(add_tags=["home"])` names a task already tagged `#Home`
- **THEN** the response SHALL say there were no changes, the file SHALL be unchanged, and no event SHALL be recorded

#### Scenario: A defective frontmatter block refuses every task write
- **WHEN** a note's line 1 is `---` and no closing `---` follows, and `complete_task`, `update_task` or `add_task` names it
- **THEN** each SHALL be refused with `content_unsafe`, and the file SHALL be unchanged
