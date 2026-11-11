## MODIFIED Requirements

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

## ADDED Requirements

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
