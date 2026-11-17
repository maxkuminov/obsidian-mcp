# task-write Specification

## Purpose
The task write tools (`complete_task`, `update_task`, `add_task`, #428): the task id derived from a task's own text and its resolution against the note's current bytes, the minimal one-line editor, the closed recurrence grammar and its date arithmetic, the pre-publication verification, duplicate detection, the write-time refresh of a note's task rows, and the refusal codes and response shape.
## Requirements
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

**Global filter.** When the caller's effective task rule is `filter` and the line so built does not contain the global filter text as a case-sensitive substring after `- [ ] `, the description SHALL instead be the filter text, one space, then `text`, and the line SHALL be built again from it.
- The 1,000-character description bound SHALL apply to that combined description. A combination over it SHALL be refused with `invalid_argument`, naming the global filter, and writing nothing.
- The structure check applied to caller-supplied task text (balanced `%%`, balanced HTML comment markers, no run of three or more backticks or tildes) SHALL be applied again to the final combined description, before a dry run is answered and before anything is published. A failure SHALL be refused with that check's refusal, writing nothing, whatever the stored filter text is.
- The response and the dry run SHALL then carry the line `global filter added: <filter as a JSON string>`.
- Under any other rule, or when the line already contains the filter text, nothing SHALL be added.

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

#### Scenario: The global filter is put at the start of a new task
- **WHEN** the caller's rule is `filter` with `#task`, and `add_task(text="Book flights", due="2026-12-01")` runs
- **THEN** the line SHALL be `- [ ] #task Book flights 📅 2026-12-01`, the response SHALL say `global filter added: "#task"`, and `list_tasks()` SHALL list the new task

#### Scenario: A filter already present is not added again
- **WHEN** the caller's rule is `filter` with `#task`, and `add_task(text="Book flights", tags=["task"])` or `add_task(text="#task Book flights")` runs
- **THEN** the line SHALL contain `#task` exactly once, and the response SHALL NOT say that the global filter was added

#### Scenario: The filter match is case-sensitive
- **WHEN** the caller's rule is `filter` with `#task`, and `add_task(text="Book flights #Task")` runs
- **THEN** the line SHALL be `- [ ] #task Book flights #Task`

#### Scenario: A filter that would open a comment cannot reach the note
- **WHEN** the `add_task` planner is handed the effective rule `filter` with the text `%%` (or `<!--`) directly, bypassing both the settings validation and the settings fallback, and plans `text="Call bank"` for a dry run and for a write
- **THEN** both SHALL be refused with `invalid_argument` naming the comment marker, and nothing SHALL be published
- **AND** when such a text is stored in the database past the save validation, `add_task(text="Call bank")` SHALL treat the rule as `all`, and the note SHALL gain no `%%` or `<!--`

#### Scenario: A non-tag filter is added the same way
- **WHEN** the caller's rule is `filter` with `TODO`, and `add_task(text="Call the bank")` runs
- **THEN** the line SHALL be `- [ ] TODO Call the bank`

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

### Requirement: The task write tools SHALL state when a line they write is not a task under the caller's rule, and the rule SHALL never block a write

`complete_task`, `update_task` and `add_task` SHALL resolve and write any checkbox by path and id, whatever the caller's task rule. An id taken from a `scope="all"` listing SHALL work under every rule.

**The statement.** After a successful `add_task` or `update_task`, and in their dry runs, when the written line is not a task under the caller's effective rule, the response SHALL carry one line, after the line that shows the written task:
- under `fields`: `note: not a task under your task rule "fields" (no task field and no task tag), so default listings will not show it; list_tasks(scope="all") will.`
- under `filter`: `note: not a task under your task rule "filter" (the line does not contain <filter as a JSON string>), so default listings will not show it; list_tasks(scope="all") will.`

**One verdict.** The statement SHALL be decided from the task extracted from the written bytes, by the same rule that the listing applies to a version-5 row. It SHALL NOT change the write, its outcome or its refusal codes.

**Unchanged behaviour.**
- `update_task` SHALL NOT add the global filter text.
- `complete_task`'s writes and responses SHALL be unchanged.

**Duplicate checks.** `add_task`'s index-side exact-duplicate refusal and its similar-task list SHALL consider only rows that are tasks under the caller's rule, through the default scope they already use. The exact-duplicate check against the target note's own current bytes SHALL remain independent of scope and rule.

**Under the rule `filter`.**
- An existing open line SHALL be an exact duplicate when its description key equals either the key of the final line's description (filter text prepended) or the key of the caller's description with the filter text as its trailing token instead of in front.
- The filter text SHALL NOT be a similar-task term on either side.
- Before prepending, the planner SHALL validate the filter text with the same validator the settings use. A filter text that fails SHALL be refused with `invalid_argument`, writing nothing.

#### Scenario: A line without fields is written and explained
- **WHEN** the caller's rule is `fields` with no task tags, `set_created_date` is false, and `add_task(path="Inbox.md", text="Read the report")` runs
- **THEN** the line SHALL be written, and the response SHALL carry the `fields` note line
- **AND** the same call with `due="2026-12-01"`, or with `set_created_date` true, SHALL carry no note line

#### Scenario: Removing the last field is explained
- **WHEN** the caller's rule is `fields` with no task tags, and `update_task(clear=["due"])` names `- [ ] Renew passport 📅 2026-12-01`
- **THEN** the line SHALL become `- [ ] Renew passport`, and the response SHALL carry the `fields` note line

#### Scenario: Dropping a non-tag filter is explained, not repaired
- **WHEN** the caller's rule is `filter` with `TODO`, and `update_task(text="Call the bank today")` names `- [ ] TODO Call the bank`
- **THEN** the line SHALL become `- [ ] Call the bank today`, and the response SHALL carry the `filter` note line naming `"TODO"`

#### Scenario: A non-task can still be completed by id
- **WHEN** the caller's rule is `fields`, and a plain `- [ ] Step one` is listed by `list_tasks(scope="all")` and then named by `complete_task` with its id
- **THEN** the task SHALL be completed as before

#### Scenario: A checklist elsewhere no longer blocks a new task
- **WHEN** the caller's rule is `fields`, `Projects/Alpha.md` holds a plain `- [ ] Renew passport`, and `add_task(path="Inbox.md", text="Renew passport", due="2026-12-01")` runs
- **THEN** the line SHALL be inserted, and the plain checkbox SHALL appear in neither a refusal nor the similar list

#### Scenario: Under filter, a duplicate is caught with the filter in front or as a trailing tag
- **WHEN** the caller's rule is `filter` with `#task`, `Projects/Alpha.md` holds `- [ ] Renew passport #task` and `Projects/Beta.md` holds `- [ ] #task Book flights`, and `add_task(path="Inbox.md", text="Renew passport")` and `add_task(path="Inbox.md", text="Book flights")` run
- **THEN** each SHALL be refused with `duplicate_task` naming the existing line, and `Inbox.md` SHALL be unchanged

#### Scenario: The target note's own bytes are still checked
- **WHEN** the caller's rule is `fields`, `Inbox.md` holds a plain `- [ ] Renew passport`, and `add_task(path="Inbox.md", text="Renew passport", due="2026-12-01")` runs
- **THEN** it SHALL be refused with `duplicate_task`, and `allow_duplicate=true` SHALL insert the line

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

### Requirement: `move_task` SHALL move one top-level task, with its whole continuation block, resolved from the source note's current bytes

`move_task(path, id, to_path, to_section=None, allow_duplicate=False, resume=False, dry_run=False, expected_hash=None, to_expected_hash=None)` SHALL move the task that `id` names in the note `path`, together with its uncapped continuation block (every following line with a greater blockquote depth, or the same blockquote prefix and a greater indentation width, crossing blank lines), to the note `to_path`, which may equal `path`.

- The task SHALL be resolved exactly as `complete_task` resolves one: from the source's bytes read through the anchored descriptor that will be published to, with `task_not_found`, `task_ambiguous`, `task_changed`, the suffixed-id `precondition_required` before any read, and `size_limit` over the per-note task cap. No line number from the index or a listing SHALL be used.
- Any checkbox SHALL be movable whatever its status (open, done, cancelled or a custom marker) and whatever the caller's task rule.
- A task that has sub-tasks SHALL move with its whole subtree. A task line SHALL never be moved without its continuation block.
- The moved task SHALL be a top-level, unquoted task: indentation width 0 and no blockquote prefix. A task nested inside another task's continuation block, and a task carrying a blockquote prefix (a callout task), SHALL be refused with `task_malformed`, writing nothing in either note, and the refusal SHALL say that `edit_note` can make the move by hand.
- `to_path` SHALL name an existing note; `move_task` SHALL never create one.
- The moved lines SHALL NOT be edited: no field, tag, status, rule text, block link or trailing whitespace SHALL change, and no global filter text SHALL be added.

#### Scenario: A task moves with its context and sub-task
- **WHEN** `Inbox.md` holds `- [ ] Call the bank 📅 2026-11-20`, then `    - ask about the fee`, then `    - [ ] find the account number`, and `move_task(path="Inbox.md", id=<id>, to_path="Projects/Money.md")` runs
- **THEN** all three lines SHALL be in `Projects/Money.md`, byte for byte, and none of them SHALL remain in `Inbox.md`

#### Scenario: A done task is moved as it is
- **WHEN** `move_task` names `- [x] Paid rent 🔁 every month 📅 2026-11-01 ✅ 2026-11-02`
- **THEN** the line SHALL arrive unchanged, no next occurrence SHALL be written, and the rule text SHALL NOT be parsed

#### Scenario: A nested task is refused
- **WHEN** `- [ ] Project` is followed by `    - [ ] step one` and `    - [ ] step two`, and `move_task` names `step one`
- **THEN** the call SHALL be refused with `task_malformed`, and neither note SHALL change

#### Scenario: A callout task is refused
- **WHEN** `move_task` names `> - [ ] Renew passport`
- **THEN** the call SHALL be refused with `task_malformed`, and neither note SHALL change

#### Scenario: A missing destination is never created
- **WHEN** `to_path` names no note
- **THEN** the call SHALL be refused with `not_found`, no file SHALL be created and the source SHALL be unchanged

### Requirement: The moved lines SHALL arrive byte for byte at `add_task`'s placement, with the destination's terminators

The content of every moved line SHALL be inserted exactly as it was read, without its terminator: no prefix SHALL be added or removed and each line's list marker SHALL be kept as written, with nothing renumbered.

The move SHALL be refused with `task_malformed`, writing nothing in either note, when the continuation block holds a line that, after its indentation, starts with `>` (a blockquote line), or that opens or closes a fenced code block (three or more backticks or tildes), whether that fence closes inside the block or not.

**Placement** SHALL be `add_task`'s: in the `to_section` body (resolved with `add_task`'s selectors and refusals) or the whole body, after the full continuation block of the last task whose indentation after its blockquote prefix is zero (never a task of the moved block); with no such task, after the region's last non-blank line; in an empty region, after the heading or at the body's start. When that last task carries a blockquote prefix, the move SHALL be refused with `task_malformed`, writing nothing in either note.

**Terminators.** Every moved line SHALL end with the destination's terminator: that of the line it is inserted after, else the note's first terminator, else LF. A file without a final terminator SHALL keep none, at the destination and at the source. Exactly two pre-existing terminators SHALL change, and only in these cases:

- an insertion at the end of a destination with no final terminator SHALL add the terminator to the old last line and leave the last moved line unterminated;
- a removal of a block that ends a source with no final terminator SHALL remove the terminator of the line before the block, which becomes the unterminated last line.

An insertion into an empty file, or after a frontmatter block that ends the file with no terminator, SHALL join the moved lines with the terminator and add none at the end.

#### Scenario: A plain task arrives byte for byte
- **WHEN** a top-level task with a two-line context block and an indented sub-task moves to a note whose tasks are top-level and unquoted
- **THEN** the four inserted lines SHALL be byte-identical to the four removed ones, apart from terminators

#### Scenario: A blockquote line in the block is refused
- **WHEN** the line under a top-level task is `    > quoted context`
- **THEN** the move SHALL be refused with `task_malformed` and neither note SHALL change

#### Scenario: A fence line in the block is refused
- **WHEN** a fenced code block opens on an indented line under the task, whether it closes inside the block or after it
- **THEN** the move SHALL be refused with `task_malformed` and neither note SHALL change

#### Scenario: A quoted destination anchor is refused
- **WHEN** the destination's last top-level task is `> - [ ] a`
- **THEN** the move SHALL be refused with `task_malformed` and neither note SHALL change

#### Scenario: Terminators follow the destination
- **WHEN** a task in a CRLF note moves into an LF note
- **THEN** the moved lines SHALL end in LF, and every other byte of both notes SHALL be unchanged

#### Scenario: End of file without a newline, at both ends
- **WHEN** the source is exactly `Intro` LF `- [ ] x` (no final newline), the destination is exactly `Notes` (no final newline), and `x` is moved
- **THEN** the source SHALL be exactly `Intro`, and the destination SHALL be exactly `Notes` LF `- [ ] x`, neither with a final newline

#### Scenario: The only task of a note, into an empty note
- **WHEN** the source is exactly `- [ ] x` and the destination is the empty file
- **THEN** the source SHALL be the empty file and the destination SHALL be exactly `- [ ] x`

#### Scenario: Terminated files keep every terminator
- **WHEN** the source is `Intro` LF `- [ ] x` LF and the destination is `Notes` LF
- **THEN** the source SHALL be `Intro` LF and the destination `Notes` LF `- [ ] x` LF

### Requirement: Every `move_task` plan SHALL be verified before it is shown or published, and a block that cannot move cleanly SHALL be refused

The move SHALL be refused with `task_malformed`, writing nothing in either note, when the moved block contains an ATX heading line.

The move SHALL be refused with `block_id_conflict`, writing nothing in either note and naming the id and the destination line, when a moved line carries a `^block` id that, compared case-insensitively, already ends a line in the destination's body. A move within one note SHALL NOT run this check.

Before a dry-run answer and before each publication, the plan for that note SHALL be verified from the old and new texts:

**Destination:**
1. the new text SHALL be the old text with exactly one whole-line insertion; every pre-existing line SHALL keep its content and its terminator, except that, for an insertion at the end of a destination without a final terminator, the old last line SHALL gain exactly the inserted terminator;
2. the inserted lines' contents SHALL equal the moved block's, line for line;
3. every pre-existing task SHALL re-extract identically, shifted by the inserted count;
4. the task set SHALL gain exactly the moved tasks, each re-extracting with the same text, key, status, dates, priority, tags and recurrence as at the source;
5. no pre-existing line SHALL change its owning task, and each moved line SHALL be owned as planned;
6. code status, headings and the frontmatter block SHALL be unchanged for every pre-existing line, and each moved line's code status SHALL equal its source code status.

**Source:**
1. the new text SHALL be the old text with exactly the moved lines removed; every remaining line SHALL keep its content and its terminator, except that, when the block ended a source without a final terminator, the line before the block SHALL lose exactly its terminator;
2. every remaining task SHALL re-extract identically, shifted by the removed count;
3. the task set SHALL lose exactly the moved tasks;
4. no remaining line SHALL change its owning task;
5. code status, headings and the frontmatter block SHALL be unchanged.

Any other terminator change SHALL fail verification. A result over `MAX_NOTE_BYTES` or over the per-note task cap, in either note, SHALL be refused with `size_limit` before anything is published. A defective frontmatter block in either note, or an unmatched indented fence with `to_section`, SHALL be refused with `content_unsafe`.

#### Scenario: A heading under the task is refused
- **WHEN** a task's continuation block contains the line `  # Notes`
- **THEN** the move SHALL be refused with `task_malformed` and neither note SHALL change

#### Scenario: A block id that already exists at the destination is refused
- **WHEN** the moved task ends in `^followup` and `Project.md` already holds another line ending in `^FollowUp`
- **THEN** the move SHALL be refused with `block_id_conflict` naming `followup` and that line, and neither note SHALL change

#### Scenario: A failed verification writes nothing
- **WHEN** a plan is altered in a test so that one destination line other than the inserted ones changes, or so that a pre-existing terminator outside the two end-of-file cases changes
- **THEN** the verifier SHALL refuse it with `task_malformed` and nothing SHALL be published in either note

### Requirement: `move_task` SHALL publish the destination first and SHALL write the source only after re-checking that the destination still holds the moved lines

The destination insert SHALL be published first, through the confirmed, descriptor-anchored publication path with the in-call conflict check. The source removal SHALL be attempted only after the destination publication returned success, or, with `resume=true`, after the destination was found to hold exactly one byte-identical copy.

The source removal SHALL be one confirmed publication whose synchronous step performs, with no suspension point between them: the root confirmation; **the destination presence check**, which re-reads the destination through its anchored target and requires the lines at the position where the moved lines were inserted (or found) to have exactly their contents and, under the extraction the index pass uses (code masking included), to still be the moved task and its block: the task and each moved sub-task at its offset with its key, the block still the task's continuation block, each line's code status unchanged; and the source write, with `expected=` the source bytes the removal was planned against. When the presence check reads the destination and finds the moved lines absent or different at that position (or the file absent), the source SHALL NOT be written and the outcome SHALL be `destination_changed`. When it finds the moved lines' bytes still at that position but they no longer extract as the moved task (for example inside an unclosed code fence), the source SHALL NOT be written and the outcome SHALL be `destination_not_a_task`, which SHALL say that the lines are in the destination at that line but no longer read as a task, and SHALL NOT say they are gone. When the presence check cannot read the destination (a read error, a non-regular file, or a file over the read cap) or reads bytes that are not valid UTF-8, the source SHALL NOT be written and the outcome SHALL be `destination_unverified`, which SHALL say that the destination's contents could not be verified and SHALL NOT say whether it holds the lines. A destination task refresh that reports `superseded` SHALL never be followed by a source write unless that check, run afterwards, passes.

Both publications replace an existing regular file, and each SHALL apply the edit rule of the `file-permissions` capability to that note, never the create rule: the replaced file's permission bits, POSIX access ACL and group, including that capability's group translation when the server cannot set the note's group and its mode-only behaviour on a filesystem without POSIX ACLs. A difference between the two permission captures of either publication, or in its staged copy, SHALL be that note's in-call conflict, handled exactly as a byte conflict on that note.

#### Scenario: An older destination saved between the steps stops the move
- **WHEN** after the destination publication an outside editor saves an older `Project.md` without the moved lines, so the destination refresh reports `superseded`
- **THEN** `Inbox.md` SHALL be byte-identical, and the answer SHALL be `task_move_incomplete` with `reason` `destination_changed`, whose `destination:` line says the destination no longer holds the moved lines

#### Scenario: A line added above the inserted lines stops the move
- **WHEN** after the destination publication another writer inserts a line at the top of `Project.md`
- **THEN** `Inbox.md` SHALL be byte-identical, and the answer SHALL be `task_move_incomplete` with `reason` `destination_changed`

#### Scenario: Nothing is removed when the destination did not publish
- **WHEN** the destination publication is refused, or raises an OS error
- **THEN** `Inbox.md` SHALL be byte-identical, and the answer SHALL be the refusal, `io_failure` saying nothing was written for a permission rule that could not be applied, or `publication_uncertain` without `nothing_written` for any other OS error

#### Scenario: Each note keeps its own permissions
- **WHEN** `Inbox.md` has mode 0o600 and `Projects/Money.md` mode 0o664 with a named-group access ACL, each note's group is one the server can set, and a task moves between them
- **THEN** after the move `Inbox.md` SHALL still have mode 0o600 and `Projects/Money.md` mode 0o664 with the same ACL and group

#### Scenario: A permission change during the move is a conflict, not a loss
- **WHEN** the mode of `Inbox.md` changes between the source write's first and second permission captures, during a move called without `expected_hash`, its bytes unchanged
- **THEN** that source write SHALL publish nothing, the source removal SHALL be retried, and the move SHALL complete with no line lost and `Inbox.md` carrying the new mode under the edit rule

#### Scenario: A destination that is not valid UTF-8 is unverified
- **WHEN** after the destination publication another writer leaves the moved lines in place but adds bytes that are not valid UTF-8 to `Project.md`
- **THEN** `Inbox.md` SHALL be byte-identical, and the answer SHALL be `task_move_incomplete` with `reason` `destination_unverified`, which SHALL NOT say that the destination no longer holds the moved lines

#### Scenario: An unreadable destination is not reported as changed
- **WHEN** after the destination publication the presence check's read of `Project.md` fails with a permission error
- **THEN** `Inbox.md` SHALL be byte-identical, and the answer SHALL be `task_move_incomplete` with `reason` `destination_unverified`, whose `destination:` line says the contents could not be verified

### Requirement: Every failure after the destination publication SHALL be a named partial outcome that states only what the tool verified and names the call that completes the move

- **Before the destination publication**, every refusal SHALL leave both notes byte-identical, and SHALL carry `nothing_written: true` wherever its code carries it for the other task write tools (the task codes, the precondition codes and `concurrent_write`); a body code such as `invalid_argument`, `content_unsafe` or `io_failure` SHALL render as it does for those tools. A destination in-call conflict without `to_expected_hash` SHALL repeat the whole sequence from reading both notes, at most 3 attempts, then refuse with `concurrent_write`, nothing written.
- **An OS error from the destination publication** SHALL leave the source untouched. A permission rule that could not be applied (`PermissionsNotApplied`, raised before anything is published) SHALL be answered `io_failure`, nothing written in either note. Any other OS error SHALL be answered `publication_uncertain` as a partial outcome, saying the destination may hold the task and naming both completing calls (`resume=true` if `read_note` shows the lines there, the plain call otherwise).
- **After the destination publication**, a stop SHALL be answered with the code `task_move_incomplete`, a partial outcome that SHALL NOT carry `nothing_written`. Its prose SHALL carry a `destination:` line and a `source:` line, each stating only what this call verified, and a `complete with:` line naming the exact call that completes the move. It SHALL NOT say the source holds the task unless a re-read in this call found it there, nor state any note's current state that this call did not verify (a source this call did not write is described as not changed by this call); a destination line re-checked by the presence check SHALL say so. Its `MCP-REFUSAL` line SHALL carry `path` (the source), `destination_path`, `source_id`, `destination_id` (absent for `destination_changed`, `destination_not_a_task` and `destination_unverified`) and `reason`.
- **Reasons and their completing calls.** Every completing call below SHALL omit `to_expected_hash`, or pass a fresh destination `content_hash`: the destination publication changed the destination, so the original call's `to_expected_hash` is stale and would be refused `stale_precondition`. The response's `complete with:` line SHALL say so whenever the original call supplied one. No completing call SHALL repeat the original call's `expected_hash`, which a concurrent source edit makes stale: a call naming a bare id SHALL omit `expected_hash`; a call naming a suffixed id SHALL name the source's fresh `content_hash` from a `read_note` of it; and a call naming an id the caller takes from a current listing SHALL say to add that fresh `content_hash` when the id carries a suffix.

| `reason` | when | `complete with:` |
|---|---|---|
| `destination_changed` | the presence check read the destination and did not find the moved lines at their position | the same call without `resume`; if that is refused `duplicate_task`, the same call with `resume=true` |
| `destination_not_a_task` | the presence check found the moved lines' bytes at their position, but they no longer extract as the moved task | repair the destination so the lines read as a task again (for example, close the fence), then the same call with `resume=true`; or, after a `read_note` of the source confirms it still holds the task, remove the destination copy by hand |
| `destination_unverified` | the presence check could not read the destination | once the destination is readable, `read_note` of it: if it holds exactly one byte-identical copy, the same call with `resume=true`; otherwise the same call without `resume` (a `duplicate_task` refusal then names any copy elsewhere) |
| `concurrent_write` | the source changed during the call and no retry was allowed or the retries ran out | `read_note` of the source, then: if the id still resolves to a block byte-identical to the moved one, the same call with `resume=true` (with that read's `content_hash` as `expected_hash` when the id is suffixed); if the key resolves once with a different block, the `source_changed` call (its current id and `allow_duplicate=true`); if the key is absent, as `source_missing` |
| `source_missing` | a re-read found no task with the id's key | none if it was removed or moved; if its text was edited, `list_tasks(path=…)` for its new id, then the call with that id and `allow_duplicate=true` |
| `source_changed` | a re-read found the key once with a different block | the call with the current id and `allow_duplicate=true` |
| `source_ambiguous` | a re-read found the key on several tasks | `read_note` of the source, then the call with a suffixed id, `resume=true` and `expected_hash` |
| `vault_assignment_changed`, `vault_anchor_lost_at_publish`, `vault_confirmation_unavailable` | the source publication was refused by the confirmation | the same call with `resume=true` once the assignment is restored |
| `permissions_not_applied` | the source write could not apply the source's permission rule and published nothing; not retried | the same call with `resume=true` once the cause is fixed |
| `io_failure` | the source write raised any other OS error | `read_note` of the source; if it still holds the task, the same call with `resume=true` |

- **Source retry.** When the source write meets an in-call conflict and no `expected_hash` was supplied, the tool SHALL re-read the source, re-derive its tasks, and write the removal again (through the same presence check) only if the moved task's key resolves to exactly one task whose block is byte-identical to the block that was moved; at most 3 attempts. With `expected_hash` supplied it SHALL NOT retry.

#### Scenario: The source removal conflicts
- **WHEN** another writer makes an unrelated edit to `Inbox.md` that leaves the task and its block unchanged, between the destination publication and the source removal of a move called with `expected_hash`
- **THEN** the task SHALL be in both notes, the other writer's bytes SHALL be on disk unchanged, and the answer SHALL be `task_move_incomplete` with `reason` `concurrent_write`, both paths and both ids, a `complete with:` line that says to re-read the source and names `resume=true` for an unchanged block and the `source_changed` call otherwise, and no `nothing_written`

#### Scenario: A conflicting change to the task's block is not completed by resume
- **WHEN** a move called with `expected_hash` ends in `task_move_incomplete` with `reason` `concurrent_write` because another writer added a context line under the task in `Inbox.md`, and the caller re-reads `Inbox.md`
- **THEN** `resume=true` SHALL be refused `resume_unmatched` writing nothing, and the call with the current id and `allow_duplicate=true` SHALL move the changed task beside the earlier copy, losing no line

#### Scenario: A completing call never repeats a stale source hash
- **WHEN** a move with a bare id and `expected_hash` stops with `destination_changed` after another writer also edited an unrelated line of `Inbox.md`
- **THEN** the `complete with:` call SHALL carry no `expected_hash`, and that call, made as printed, SHALL complete the move; with a suffixed id the line SHALL say to read `Inbox.md` first and pass its fresh `content_hash`

#### Scenario: Bytes in place but no longer a task stop the move
- **WHEN** after the destination publication another writer turns the line above the moved lines into an unclosed code fence, leaving the moved bytes at their position
- **THEN** `Inbox.md` SHALL be byte-identical, the answer SHALL be `task_move_incomplete` with `reason` `destination_not_a_task`, whose `destination:` line says the lines are still at that line but no longer read as a task, and after the fence is closed the printed `resume=true` call SHALL complete the move

#### Scenario: A context line added in the source is kept
- **WHEN** another writer adds a context line under the task in `Inbox.md` after the destination publication of a move called without `expected_hash`
- **THEN** the task and its new context line SHALL remain in `Inbox.md`, and the answer SHALL be `task_move_incomplete` with `reason` `source_changed`, naming the current id and the `allow_duplicate=true` call

#### Scenario: A task completed in the source between the steps is reported missing, not present
- **WHEN** another writer completes the task in `Inbox.md` (its text gains a done date) after the destination publication of a move called without `expected_hash`
- **THEN** the completed line SHALL remain in `Inbox.md`, and the answer SHALL be `task_move_incomplete` with `reason` `source_missing`, whose `source:` line does not say that `Inbox.md` holds the task

#### Scenario: Two concurrent moves of one task to different notes
- **WHEN** moves of one task from `A.md` to `B.md` and to `C.md` run at once and both publish their destination
- **THEN** one SHALL succeed, the other SHALL answer `task_move_incomplete` with `reason` `source_missing` without claiming `A.md` holds the task, and no line SHALL be lost

#### Scenario: A permission failure at the source write is partial
- **WHEN** the source's permission rule cannot be applied after the destination publication
- **THEN** `Inbox.md` SHALL be byte-identical, the answer SHALL be `task_move_incomplete` with `reason` `permissions_not_applied`, whose `source:` line says the source was not changed, and a `complete with:` line naming `resume=true`

#### Scenario: A reassignment between the steps is partial
- **WHEN** the caller's vault assignment changes after the destination publication
- **THEN** the source publication SHALL be refused by the confirmation, and the answer SHALL be `task_move_incomplete` with `reason` `vault_assignment_changed` and a `complete with:` line naming `resume=true`

### Requirement: An identical copy at the destination SHALL be refused as a duplicate, and a partial move SHALL be completed only by an explicit `resume`

Before planning an insertion, the destination's current bytes SHALL be checked, never the index. The move SHALL be refused with `duplicate_task`, writing nothing in either note and naming up to 5 matches, when the destination holds a task with the moved task's key, whatever its block, a byte-identical block included, or, when the moved task is open, an open task with the same normalised description (`add_task`'s rule), unless `allow_duplicate=true`. When a match is byte-identical to the moved block, the refusal SHALL say so and name both `allow_duplicate=true` and `resume=true`. `allow_duplicate=true` SHALL only ever add lines.

`resume=true` SHALL mean only: the destination already holds this exact block; insert nothing and remove the source copy. It SHALL require exactly one destination task with the moved key whose block's line contents, terminators excluded, are byte-identical to the moved block's, and SHALL otherwise be refused with `resume_unmatched`, writing nothing. It SHALL then write the source removal through the destination presence check aimed at that copy. It SHALL NOT be combinable with `allow_duplicate`, with `to_section`, or with a move within one note.

A single `move_task` call without `allow_duplicate`, together with its completing calls, SHALL NOT leave more than two copies of a moved block, and two such calls running concurrently SHALL NOT leave more than two once one of their source removals succeeds. Concurrent moves of one task to different destinations whose source removals all stop partial (for example, each source write refused `permissions_not_applied`) can leave the source copy plus one copy in each destination that published: three for two calls, N + 1 for N; never fewer than one copy in all. No call SHALL remove a source line whose content is not present at the destination at the moment of the presence check.

#### Scenario: An independent identical task at the destination is not deleted
- **WHEN** `Inbox.md` and `Project.md` each hold an independent `- [ ] Call Alex` with identical context, before any move, and `move_task` moves the inbox task to `Project.md`
- **THEN** it SHALL be refused with `duplicate_task`, saying the copy is byte-identical, and neither note SHALL change

#### Scenario: A plain retry after a partial outcome writes nothing
- **WHEN** a move ended in `task_move_incomplete` with `reason` `concurrent_write`, and the same call is made again without `resume`
- **THEN** it SHALL be refused with `duplicate_task`, and neither note SHALL change

#### Scenario: Resume completes a partial move
- **WHEN** a move ended in `task_move_incomplete` with `reason` `concurrent_write`, a re-read shows the source task's block still byte-identical to the moved block, and the same call is made with `resume=true` (with that read's `content_hash` if `expected_hash` was used)
- **THEN** nothing SHALL be inserted in the destination, the source copy SHALL be removed, and the response SHALL say the move was resumed, naming the destination line

#### Scenario: Resume without exactly one copy is refused
- **WHEN** `resume=true` is passed and the destination holds no byte-identical copy, or two
- **THEN** it SHALL be refused with `resume_unmatched`, and neither note SHALL change

#### Scenario: An open duplicate at the destination is refused
- **WHEN** `Projects/Money.md` holds `- [ ] call the  Bank` and `move_task` moves `- [ ] Call the bank` there
- **THEN** it SHALL be refused with `duplicate_task` naming that line, and with `allow_duplicate=true` the task SHALL move

#### Scenario: A done task into an archive is not refused for its description
- **WHEN** `Archive.md` holds `- [x] Water plants ✅ 2026-10-01` and `move_task` moves `- [x] Water plants ✅ 2026-11-01` there
- **THEN** the task SHALL move

### Requirement: `move_task` SHALL bind each note by its own precondition, with the task tools' required-mode rules

`expected_hash` SHALL bind the source note's whole bytes and `to_expected_hash` the destination's, each compared by the existing precondition ladder immediately after that note's in-call read, with the existing codes. A `stale_precondition` refusal SHALL name the note whose hash differed.

When the deployment requires preconditions, a bare id SHALL satisfy the requirement for the source, a suffixed id SHALL always require `expected_hash`, and the destination SHALL be exempt, as for `add_task`. A supplied hash SHALL still be compared.

When `path` and `to_path` resolve to the same note, `to_expected_hash` SHALL be absent or equal to `expected_hash`, and `resume` SHALL be false, else the call SHALL be refused with `invalid_argument`. `resume=true` combined with `allow_duplicate=true` or with `to_section` SHALL be refused with `invalid_argument` before any read. When they name the same file through two different vault paths (equal device and inode), the call SHALL be refused with `invalid_argument` before any publication.

#### Scenario: A stale destination hash writes nothing
- **WHEN** `to_expected_hash` does not match the destination
- **THEN** the call SHALL be refused with `stale_precondition` naming `to_path`, and neither note SHALL change

#### Scenario: Required mode with a bare id
- **WHEN** `WRITE_PRECONDITION_REQUIRED=true` and `move_task` is called with a bare id and no hashes
- **THEN** it SHALL proceed

#### Scenario: Two paths to one file are refused
- **WHEN** `path` and `to_path` are two hard links to one file
- **THEN** the call SHALL be refused with `invalid_argument`, and the file SHALL be unchanged

### Requirement: A move within one note SHALL be one plan, one verification and one publication

When `to_path` resolves to the source note, the removal and the insertion SHALL be planned over the original text as one set of non-overlapping spans, with the insertion point computed on the note without the moved block. The plan SHALL be verified once (every line outside the block unchanged, every non-moved task identical, the task set unchanged, each moved task re-extracting identically, no non-moved line changing owner, code status and headings unchanged) and published once, with one refresh and one `modified` event. A task that is already the last top-level task of the destination region (whatever blank or paragraph lines precede it) SHALL be left where it is, and such a call, like any plan that leaves the note unchanged, SHALL answer that there were no changes, with nothing written, no event and no refresh. No partial outcome SHALL be possible.

#### Scenario: Moving a task to another section of its note
- **WHEN** `Inbox.md` has sections `Today` and `Later`, and `move_task(path="Inbox.md", id=<id of a Today task>, to_path="Inbox.md", to_section="Later")` runs
- **THEN** the task and its block SHALL be after `Later`'s last top-level task, one publication SHALL have occurred, and one `modified` event SHALL be recorded

#### Scenario: A task already in place
- **WHEN** the task is the last top-level task of the section named by `to_section` in its own note
- **THEN** the response SHALL say there were no changes, and nothing SHALL be written

### Requirement: `move_task` SHALL share the task write envelope, refresh and record each note it published, and take one write charge

`move_task` SHALL:

- be wrapped by `_tracked` with the write class, be named in `WRITE_REQUIRED_TOOLS`, require write permission, and be charged **one** write-bucket token and one quota slot per call, however many publications it makes;
- hold one `write` concurrency slot for the whole call;
- after each confirmed publication, refresh that note's task rows with the write-time refresh, then record one `modified` change-feed event for that note with `tool = 'move_task'`, the destination's before the source removal is attempted;
- never hold two database locks at once: each refresh SHALL run in its own transaction holding at most one `notes_metadata` row lock, ended before the next begins;
- report each refresh as `index (destination): …` and `index (source): …` (one `index:` line for a same-note move); a source refresh that does not report `refreshed` SHALL never fail the move, and a destination refresh that does not report `refreshed` SHALL NOT stop the source removal;
- refresh each note it published whatever that note's bytes, without the generic writers' `may_hold_tasks` gate, in the task tools' order: publication, refresh, event;
- work on a note at an excluded sync-conflict path exactly as on any note, reporting a destination refresh that finds no index row as `index (destination): may lag (not_indexed)`, and print no exclusion notice;
- log `path`, `id`, `to_path`, `to_section`, `dry_run`, `allow_duplicate`, `resume` and the presence of each hash, and never task text;
- with `dry_run=true`, return both diffs and the moved lines, publishing nothing, refreshing nothing and recording no event.

The caller's task rule SHALL NOT block or alter the move. After planning, the moved task's destination extraction SHALL be checked with the same task-rule test the other task write tools apply to the line they wrote (`task_rule.extracted_is_task` under the caller's effective rule), and when the moved task is not a task under that rule, the success and dry-run responses SHALL carry the same note line (`task_rule.not_task_note`) as `add_task` and `update_task`. When `to_path` lies under a folder the caller's default task scope excludes and the source does not, the response SHALL say that default listings will not show the task and that `list_tasks(scope="all")` will.

#### Scenario: List, move, list
- **WHEN** `list_tasks` shows a task in `Inbox.md`, `move_task` moves it to `Projects/Money.md` and reports `index (destination): refreshed` and `index (source): refreshed`, and `list_tasks` is called before any index pass
- **THEN** the task SHALL be listed in `Projects/Money.md` with its id, and not in `Inbox.md`

#### Scenario: A lagging destination refresh is reported, not hidden
- **WHEN** another transaction holds the destination's row lock for longer than 2 seconds during a move
- **THEN** the move SHALL complete, the response SHALL say `index (destination): may lag (lock_timeout)`, and the next index pass SHALL list the task in the destination

#### Scenario: A checkbox that is not a task under the rule is moved and said so
- **WHEN** the caller's task rule is `fields` and `move_task` moves `- [ ] buy stamps`, which has no task field and no task tag, into an indexed note outside every excluded folder, with and without `dry_run`
- **THEN** the move (or the dry run) SHALL proceed, and the response SHALL carry the note that the line is not a task under the `fields` rule and that `list_tasks(scope="all")` will show it

#### Scenario: A move into an excluded conflict copy
- **WHEN** `INDEX_EXCLUDE_CONFLICT_FILES` is true and a task moves into `N (conflict 2026-05-29-19-45-39).md`
- **THEN** the move SHALL complete, the response SHALL say `index (destination): may lag (not_indexed)`, and it SHALL carry no exclusion notice line

#### Scenario: Two events, attributed
- **WHEN** an API key moves a task between two notes
- **THEN** two `modified` events SHALL be recorded with `tool = 'move_task'`, the destination's first, each carrying the canonical hash of the bytes that note published

#### Scenario: Opposite moves do not deadlock
- **WHEN** a move from `A.md` to `B.md` and a move of another task from `B.md` to `A.md` are released together, repeatedly
- **THEN** every call SHALL finish, and every task SHALL be in exactly one note, or in two with a `task_move_incomplete` answer

#### Scenario: One write token per move
- **WHEN** a principal with a write burst of 2 makes two two-note moves
- **THEN** both SHALL be admitted by the write bucket

### Requirement: The `move_task` response SHALL name the moved task's new line and id, and say when other ids changed

A success SHALL report the destination line of the moved task and its id, the number of lines and tasks moved, the lines removed from the source, each published note's `content_hash`, and each refresh result. Every moved task SHALL keep its key. The id SHALL be bare when the destination holds one task with that key after the move, and suffixed otherwise. When a task in either note that shares a key with a moved task has its ordinal suffix renumbered by the move, the response SHALL say so. When moved lines carry `^block` ids, the response SHALL name them and say that links to them in the source note no longer resolve (a collision with an id already in the destination is refused before publication). Every printed path SHALL be escaped as every task tool escapes one.

#### Scenario: The id survives the move
- **WHEN** a task with the bare id `3f9a0c1b2d4e` moves to a note holding no task with that key
- **THEN** the response SHALL give its id as `3f9a0c1b2d4e`, and `complete_task(path=<destination>, id="3f9a0c1b2d4e")` SHALL resolve it

#### Scenario: A remaining sibling loses its suffix
- **WHEN** `Inbox.md` holds two identical `- [ ] Call` lines, and `move_task` moves `<key>.1of2` with a matching `expected_hash`
- **THEN** the remaining line's id SHALL be the bare key, and the response SHALL say that suffixed ids sharing the moved key were renumbered in `Inbox.md`

