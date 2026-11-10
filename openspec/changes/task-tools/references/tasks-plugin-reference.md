# Obsidian Tasks plugin: behaviour reference for server-side task writes

**What this describes:** obsidian-tasks-group/obsidian-tasks at `main` commit `5b92c3549b0631481dc811edd6a9ee8cc006edcc`
(2026-11-10), `package.json` / `manifest.json` version **8.6.0**. The docs in the repo (`docs/`) are the source of
https://publish.obsidian.md/tasks/ (published path = repo path with spaces as `+`, e.g.
`docs/Getting Started/Recurring Tasks.md` -> https://publish.obsidian.md/tasks/Getting+Started/Recurring+Tasks).
I also ran rrule **2.8.1**, the version pinned in the plugin's `yarn.lock`, to check how rules come back as text
(`RRule.parseText` -> `new RRule(...).toText()`).

Source key: `src/...` and `tests/...` are files in the repo at that commit. `docs/...` are documentation pages.
A fact marked **(source)** comes from code or tests because the docs say nothing about it.

---

## 1. Line format ("Tasks Emoji Format")

### 1.1 Line skeleton
- The whole task line is `indentation + listMarker + " [" + statusSymbol + "] " + body + markdownHardBreak`.
  `src/Task/Task.ts` `toFileLineString()`:
  `` `${this.indentation}${this.listMarker} [${this.status.symbol}] ${this.toString()}${this.markdownHardBreak}` ``
- Parse regex (source, `src/Task/TaskRegularExpressions.ts`):
  - indentation `^([\s\t>]*)`, which includes `>` for blockquotes and callouts
  - list marker `([-*+]|[0-9]+[.)])`
  - one or more spaces, then `\[(.)\]` (any single character is the status)
  - the rest is ` *(.*)`
- A trailing hard break of two or more spaces is kept on rewrite. A single trailing space is dropped (source,
  `src/Task/ListItem.ts` `getMarkdownHardBreak`; comment in `Task.ts`: "two or more trailing spaces are meaningful").

### 1.2 Signifiers (DEFAULT_SYMBOLS, `src/TaskSerializer/DefaultTaskSerializer.ts`; docs `docs/Reference/Task Formats/Tasks Emoji Format.md`)
| Field | Written as | Also read as | Value syntax (regex) |
|---|---|---|---|
| created | `➕` | | `\d{4}-\d{2}-\d{2}` |
| start | `🛫` | | date |
| scheduled | `⏳` | `⌛` | date |
| due | `📅` | `📆`, `🗓` | date |
| done | `✅` | | date |
| cancelled | `❌` | | date |
| recurrence | `🔁` | | `[a-zA-Z0-9, !]+` |
| onCompletion | `🏁` | | `[a-zA-Z]+`, either `keep` or `delete` (case-insensitive); anything else = Ignore |
| id | `🆔` | | `[a-zA-Z0-9-_]+` |
| dependsOn | `⛔` | | `id( *, *id *)*`; spaces are removed and the value is split on `,` |
| priority Highest | `🔺` | | no value |
| priority High | `⏫` | | |
| priority Medium | `🔼` | | |
| priority None ("Normal") | (no signifier) | | |
| priority Low | `🔽` | | |
| priority Lowest | `⏬` | | |

- Each field regex is `symbol + '️?' + ' *' + value + '$'`: an optional VS16 after the emoji, then **zero or more**
  spaces, then the value, anchored at the end of the remaining text (source, `fieldRegex`).
- On write, the plugin always emits `" " + symbol + " " + value`: one space before and one after
  (`symbolAndDateValue` / `symbolAndStringValue`). Dates are written `YYYY-MM-DD` (`TaskRegularExpressions.dateFormat`).
- Dates must be `YYYY-MM-DD`, cannot be configured, and cannot carry times. ``📅 today`` is not understood
  (`docs/Getting Started/Dates.md`, "Limitations of task date support").
- Priority order and names: 🔺 highest, ⏫ high, 🔼 medium, none, 🔽 low, ⏬ lowest. "If a task has no priority at all,
  it is considered between low and medium priority" (`docs/Getting Started/Priority.md`). Priority numbers are
  Highest=0, High=1, Medium=2, None=3, Low=4, Lowest=5 (`Task.ts` `priorityNumber`). `task.priorityName` for none is
  `'Normal'` (`docs/Queries/Filters.md`, Priority).
- `dependsOn` is written joined with `,` and no spaces (`task.dependsOn.join(',')`, source).

### 1.3 Serialisation order (what the plugin writes whenever it rewrites a line)
`DefaultTaskSerializer.serialize` walks `TaskLayoutOptions.shownComponents`, which is the `TaskLayoutComponent` enum
order (`src/Layout/TaskLayoutOptions.ts`):

```
description, 🆔 id, ⛔ dependsOn, priority, 🔁 recurrence, 🏁 onCompletion,
➕ created, 🛫 start, ⏳ scheduled, 📅 due, ❌ cancelled, ✅ done, ^blockLink
```

- Example from tests: `- [ ] this is a task 🔁 every day ➕ 2023-03-08 📅 2021-09-13` (`tests/Task/Task.test.ts`
  around line 1521). Another: `🆔 id2 ⛔ id1 🔁 every day 📅 2024-02-13` (around line 1317).
- Empty fields are omitted. A scheduled date that was *inferred* from the filename is not written
  (`scheduledDateIsInferred`, source).
- The recurrence is written as `recurrence.toText()`, which is rrule's **canonical text** plus `" when done"`. It is
  *not* the text the user typed (see 2a).
- The **whole line is rebuilt** on every plugin edit: completing, toggling, or using the modal. Out-of-order fields are
  therefore put back in this order, and the completed line is rewritten as well (`handleNewStatus` builds a new
  `Task` and saves it with `toFileLineString`; source). "when lines are edited by Tasks ... the tags may get moved"
  (`docs/Getting Started/Tags.md`).

### 1.4 Parsing rule
- Docs: "Tasks reads task lines **backwards from the end of the line**, looking for metadata emojis with values, tags
  and block links. As soon as it finds a value that it does not recognise, it stops reading. ... you can only put
  **block links** (`^link-name`) and **tags** after metadata" (`docs/Getting Started/Getting Started.md`, "Order of
  metadata/emojis"). Also `docs/Editing/Auto-Suggest.md`: "As soon as it finds any unrecognised text, it stops
  reading, and ignores any emojis to the left of that unrecognised text."
- Mechanics (source, `deserialize`):
  1. The block link is stripped first. `extractTaskComponents` matches ` \^[a-zA-Z0-9-]+$` at the very end of the
     body, keeps it with its leading space, and writes it back last.
  2. A loop then tries each end-anchored regex in turn, removing every match and trimming:
     priority, done, cancelled, due, scheduled, start, created, recurrence, onCompletion, **trailing `#tag`**, id,
     dependsOn.
  3. The loop repeats while anything matched, at most 21 passes. Fields may therefore appear **in any order** after
     the description, but only as an unbroken run at the end.
  4. Whatever is left is the description.
- If the same field appears twice in that trailing run, the copy further left wins, because it is extracted later and
  overwrites the first (source inference from the loop).
- **Tags among or after signifiers are kept, but moved.** Tags matched from the end (`(^|\s)#[^ !@#$%^&*(),.?":{}|<>]+$`)
  are collected and re-appended to the end of the description. A comment in the source says
  `'Do something #tag1 (due) tomorrow #tag2 (start) today'` gets the description `'Do something #tag1 #tag2'`.
  On the next write they come out **before** the signifiers (source; docs Tags.md "the tags may get moved").
- The block link must use `[a-zA-Z0-9-]`. A `^block_id` with an underscore is not recognised as a block link (source
  regex). Anything else after the signifiers, such as plain words or a `[[link]]`, ends parsing, and the signifiers to
  its left become description text.
- Task recognition and the global filter: `Task.fromLine` returns null (not a task) unless the body contains the
  global filter string (`GlobalFilter.includedIn`, `description.includes(globalFilter)`; source).
- Tasks tag recognition differs from Obsidian's. Any characters except space and `!@#$%^&*(),.?":{}|<>` are allowed;
  `#1234` counts as a tag; tags inside `%%` and `<!-- -->` are recognised (`docs/Getting Started/Tags.md`, "Recognising
  Tags").

### 1.5 Dataview format (not supported, noted for completeness)
- An alternative format, `taskFormat: 'dataview'`, uses bracketed inline fields such as `[due:: 2021-04-09]` and
  `[onCompletion:: delete]`. Tasks always *writes* them with `[]` (`docs/Reference/Task Formats/Dataview Format.md`).
  The default setting is `taskFormat: 'tasksPluginEmoji'` (`src/Config/Settings.ts`).

---

## 2. Recurrence

### 2a. Grammar
- "Every recurrence rule has to start with the word `every`" (`docs/Getting Started/Recurring Tasks.md`, Usage).
- Exact pipeline (source, `src/Task/Recurrence.ts` `fromText`):
  1. The serializer captures `🔁 *([a-zA-Z0-9, !]+)$` and trims it.
  2. It is split with `/^([a-zA-Z0-9, !]+?)( when done)?$/i`. Group 1, trimmed, is the rule text. `when done`
     (case-insensitive) sets `baseOnToday`.
  3. `RRule.parseText(ruleText)` (rrule's natural-language parser) produces `options`.
  4. `options.dtstart` is set to the reference date (start of day, as UTC), or to **today** when `when done` is set
     or the task has no reference date.
  5. `new RRule(options)`.
- An exception or a null result gives `null`. The task is then **non-recurring** and the rule text is **discarded**:
  "Invalid recurrence rules are discarded, and treated as non-recurring" (`tests/Task/Task.test.ts` around line 595,
  `🔁 invalid rule` -> `isRecurring false`, `recurrenceRule ''`). The text has already been removed from the
  description, so a plugin rewrite of the line drops it (source inference).
- Rules come back in **canonical form**. The plugin writes `rrule.toText()` (+ `when done`):
  - `docs/Queries/Filters.md` (Recurrence): "a task with `🔁 every Sunday` will be searched as `every week on Sunday`"
  - `tests/Query/Filter/RecurrenceField.test.ts`: `'every Sunday when done'` -> `'every week on Sunday when done'`
  - checked with rrule 2.8.1: `every monday` -> `every week on Monday`; `Every Day` -> `every day`;
    `every weekend` -> `every week` (lossy!)
  - checked with rrule 2.8.1, unchanged by the round trip: `every day`, `every 3 days`, `every weekday`,
    `every week on Tuesday, Friday`, `every 2 weeks`, `every month`, `every month on the 15th`,
    `every month on the last Friday`, `every 6 months`, `every January on the 15th`, `every year`
  - verified by me, not documented: the docs' Basic Example shows `🔁 every Sunday` unchanged on the next line, which
    contradicts the code. See the conflicts section.
- Documented examples (`docs/Getting Started/Recurring Tasks.md`, "Examples", plus the Usage section and
  `docs/Reference/Task Formats/Tasks Emoji Format.md`):
  - `every weekday` ("every Mon - Fri"), `every day when done`
  - `every 3 days`, `every 10 days when done`
  - `every week`, `every week when done`, `every Sunday`, `every week on Sunday`,
    `every week on Tuesday, Friday`, `every 2 weeks`, `every 3 weeks on Friday`
  - `every month`, `every 2 months`, `every 3 months`, `every month on the 1st`, `every month on the 31st`,
    `every month on the last`, `every month on the last Friday`, `every month on the 2nd last Friday`,
    `every 6 months on the 2nd Wednesday`
  - `every January on the 15th`, `every February on the last`, `every April and December on the 1st and 24th`,
    `every year`, `every year when done`
- `every month on the 15th` and `every 6 months` are not on the docs list, but rrule parses both and they round-trip
  unchanged (checked with rrule 2.8.1).
- Not supported: `for x times`, and `until "date"` (rrule bug, issue #1818). See "Limitations of Recurring Tasks" in
  the same page.
- The value character class `[a-zA-Z0-9, !]` cannot hold `.`, `:`, `-` or `/`, so `🔁` cannot take ISO dates or
  times (source).

### 2b. Computing the next occurrence
- **Reference date:** the first date found in the order **Due -> Scheduled -> Start**. With "Remove scheduled date on
  recurrence" enabled, the order is **Due -> Start -> Scheduled** (Tasks 7.22.0+). Created, done and cancelled dates
  are never the reference date (`docs/Getting Started/Recurring Tasks.md`, "Priority of Dates"; `src/Task/Occurrence.ts`
  `getDatePriorityOrder`).
- **Without `when done`:** the next reference date is the first rrule occurrence strictly **after the end of the
  original reference date's day**, with `dtstart` = reference date (source, `nextReferenceDateFromOriginalReferenceDate`
  -> `nextAfter`). "newly created tasks having dates relative to the original task rather than 'today'". In the docs
  example, a task scheduled 2021-02-06 and completed 2022-02-13 gets its next occurrence one week after 2021-02-06.
  **Missed occurrences are not skipped.**
- **With `when done`:** the rule is rebuilt with `dtstart` = today, i.e. the completion date, and the next date is the
  first occurrence after the end of today (source, `nextReferenceDateFromToday`). "the new task is scheduled based on
  the current date when you completed the task" (same doc). In the Edit Task modal, the Done date field is used as
  "today" (`docs/Editing/Create or edit Task.md`, "Rewriting history").
- **Other dates keep the same offset** from the reference date. "All dates of the next occurring task will have the
  relative distance to the reference date that they had on the original task." Example: `⏳ 2021-10-28 📅 2021-10-30`
  with `every 2 weeks` becomes `⏳ 2021-11-11 📅 2021-11-13`. The offset is rounded to whole days for DST
  (`Occurrence.nextOccurrenceDate`: `Math.round(originalDifference.asDays())`).
- Only start, scheduled and due shift. Created, done and cancelled are handled as described in 2d.
- **Remove scheduled date on recurrence** (default off): when on, the next occurrence has no ⏳, but only if the task
  also has 🛫 or 📅 (`Occurrence.next`, `canRemoveScheduledDate`; docs "Remove scheduled date on recurrence").
- **No dates at all:** a new task *is still created*, with no dates. `Occurrence.next` returns an empty Occurrence
  when there is no reference date. Test: `- [ ] this is a recurring task 🔁 every day` gives
  `['- [ ] this is a recurring task 🔁 every day', '- [x] this is a recurring task 🔁 every day ✅ 2023-05-16']`
  (`tests/Task/Task.test.ts`, "order of recurring tasks"). The docs show the same result:
  `- [x] Do stuff 🔁 every day ✅ 2023-02-11 ... - [ ] Do stuff 🔁 every day` (Recurring Tasks.md, "Recurring tasks
  must have at least one date"). The Create/Edit modal refuses a recurring task with no date (Tasks 5.0.0+), but a
  hand-edited line is allowed.
- **Trigger:** a new occurrence is created only when the status **type changes to `DONE`** from a type that was not
  DONE, and the task has a recurrence (`handleNewStatus`: `newStatus.isCompleted()` is `type === DONE`). Changing to
  CANCELLED creates **no** next occurrence. Un-completing never creates one (source). "It is the Task Status Type
  changing **to** `DONE` that controls when ... new copies of recurring tasks are created"
  (`docs/Getting Started/Statuses.md`).

### 2c. Edge cases
- **Rule without a fixed day, monthly or yearly** (`every month`, `every 3 months`, `every year`): if the target day
  does not exist, the date is moved **backwards** to the last valid day, and later occurrences then follow that new
  day. From the docs:
  - `every month` from 2021-10-31 gives 11-30, 12-30, 01-30, 02-28, 03-28
  - `every 3 months` from 2022-01-31 gives 2022-04-30, then 2022-07-30
  (`docs/Getting Started/Recurring Tasks.md`, "every month: if next calculated date does not exist, move new due date
  earlier" and "Next recurrence is on non-existent date")
  - Implementation: `Recurrence.nextAfterMonths` / `nextAfterYears` step `after` back one day at a time until the
    gap is not more than N months or years. The monthly fix-up is skipped when the canonical text contains `' on '`.
- **Rule that fixes the day** (`every month on the 31st`): invalid dates are **skipped forwards**, per RFC 5545.
  2022-01-31 gives 03-31, 05-31, 07-31, 08-31 (same doc, "every month on the 31st: skips months with fewer than 31
  days"). Use `every month on the last` to get the last day of every month.
- **Feb 29 with `every year`:** `nextAfterYears` applies the same step-back logic, so 2024-02-29 should give
  2025-02-28. **UNVERIFIED:** I did not run this, and no docs example covers it.
- **Invalid reference date** (e.g. `📅 2024-02-30`): the Recurrence cannot be built. When the task is completed, "the
  recurrence rule will disappear, and no new task will be created", and the completed line is written without `🔁`
  (docs "Recurrence rule lost if highest priority date is invalid"; `tests/Task/Recurrence.test.ts` around line 154).
  An invalid *lower-priority* date does not block it ("creates a recurrence if a lower priority date is invalid").
- **`next()` returns null:** only the toggled task is returned and no new line is written (`handleNewStatus`). Which
  rules can actually reach this (e.g. an exhausted rrule) is **UNVERIFIED**. `nextReferenceDate` is typed to return a
  `Date`, so rrule's `null` would probably become an Invalid Date rather than `null`.

### 2d. The two lines after completion
- **Completed line:** status becomes the DONE symbol (default `x`), and `✅ <today>` is added if "Set done date on
  every completed task" is on (default on). All other fields stay: rule, id, dependsOn, created, block link. The line
  is re-serialised in canonical order, so ✅ comes near the end, before any `^block`. Docs: "A 'done' task will have
  the date it was done appended to the end of its line" (`docs/Getting Started/Getting Started.md`). Example:
  `- [x] take out the trash 🔁 every Sunday 📅 2021-04-25 ✅ 2021-04-24` (Recurring Tasks.md, Basic Example).
- **New line** (source, `Task.createNextOccurrence`; docs Recurring Tasks.md):
  - **status:** the "next recurrence status". The plugin takes the DONE status's *next status symbol*, then follows
    the chain until it finds a TODO-type status, otherwise an IN_PROGRESS-type one, otherwise `' '`
    (`StatusRegistry.getNextRecurrenceStatusOrCreate`). With defaults, `x` -> `' '`, so the new line is `[ ]`.
  - **kept, copied from the original:** description (including tags), priority, recurrence rule (canonical text),
    onCompletion, indentation, list marker.
  - **start / scheduled / due:** shifted as in 2b.
  - **done date:** removed (`doneDate = null`).
  - **cancelled date:** removed.
  - **created date:** set to the **real current date** if "Set created date on every added task" is on, and
    **removed otherwise, even if the original had one**. Test: "should not set created date if setting disabled, even
    if original has created date" (`tests/Task/Task.test.ts` around line 1502). The created date uses the actual
    today even when the modal's Done date is different (`handleNewStatus` doc comment; `Create or edit Task.md`).
  - **id and dependsOn:** removed. Docs: "Next recurrence has any dependency fields removed", with the rationale
    "tasks that were 'blocked by' a recurring task were blocked forever"; also `tests/Task/Task.test.ts` around line
    1309.
  - **block link (`^id`):** removed ("New occurrences cannot have the same block link"; source).
- Documented example with created dates on:
  `- [ ] take out the trash 🔁 every Sunday ➕ 2023-03-10 📅 2021-05-02` /
  `- [x] take out the trash 🔁 every Sunday 📅 2021-04-25 ✅ 2023-03-10`.
- **Un-completing** (DONE -> TODO): the done date is removed and no new task is created. In general, a DONE or
  CANCELLED date is set only on entering that type with the setting on, kept while the type stays the same, and
  cleared on leaving it (source, `Task.newDate`; `docs/Getting Started/Statuses.md`: "changing **from** `DONE` ...
  tasks **lose** their Done dates").

### 2e. Placement of the new line
- Default: "**a new task will be put one line above the original task**" (`docs/Getting Started/Recurring Tasks.md`,
  Usage; "Order of the new task": "The default is to put the new task before the original one").
- Setting **"Next recurrence appears on the line below"** (`recurrenceOnNextLine`, default **false**). Settings text:
  "Enabling this will make the next recurrence of a task appear on the line below the completed task. Otherwise the
  next recurrence will appear before the completed one." (`src/i18n/locales/en.json`; `src/Config/Settings.ts`). It
  was introduced in Tasks 3.8.0.
- Implementation: `handleNewStatus` returns `[next, toggled]`, and `putRecurrenceInUsersOrder` reverses it when the
  setting is on. Both lines get the original line's indentation and list marker (source).

### 2f. On Completion (`🏁`)
- Introduced in Tasks 7.8.0. Actions (`docs/Getting Started/On Completion.md`):
  - **keep:** "Nothing will be done with the just-completed task. (This is the default action.)"
  - **delete:** "Removes the completed instance of the task."
- Rules (source, `src/Task/OnCompletion.ts` `handleOnCompletion`):
  - The value is parsed case-insensitively. `keep` and an absent value behave the same, except that `keep` is written
    back.
  - Deletion happens only when the status type **changes to DONE**. A change to CANCELLED, or any other change, keeps
    the line.
  - For a recurring task, the completed instance is removed and the **new occurrence stays**, still carrying
    `🏁 delete`. Docs: "The next instance of the recurring task has replaced the original, completed instance."
    Example: `... 📅 2021-05-20 🔁 every day when done 🏁 delete` -> only `- [ ] ... 📅 2021-05-21 🔁 every day when
    done 🏁 delete` remains.
- Warning in the docs: never put `🏁 delete` on a task with nested children. Removing the line leaves the children
  over-indented, so they become a code block, and Tasks gives no warning.
- Order of operations: next-occurrence creation, then on-completion pruning, then the line-order reversal
  (`putRecurrenceInUsersOrder`; source).

---

## 3. Settings that affect writes (defaults from `src/Config/Settings.ts` `defaultSettings`; labels from `src/i18n/locales/en.json`)
| Setting (UI label) | Key | Default | Effect |
|---|---|---|---|
| Set done date on every completed task | `setDoneDate` | **true** | adds `✅ YYYY-MM-DD` "when a task is toggled to done" |
| Set cancelled date on every cancelled task | `setCancelledDate` | **true** | adds `❌ YYYY-MM-DD` "when a task is toggled to cancelled" |
| Set created date on every added task | `setCreatedDate` | **false** | adds `➕` "when a task is created with 'Create or edit task', or by completing a recurring task" |
| Next recurrence appears on the line below | `recurrenceOnNextLine` | **false** | new occurrence goes above (false) or below (true) the completed one |
| Remove scheduled date on recurrence | `removeScheduledDateOnRecurrence` | **false** | see 2b |
| Global task filter | `globalFilter` | **`''`** (empty) | see below |
| Remove global filter from description | `removeGlobalFilter` | **false** | display-only, see below |
| Task format | `taskFormat` | `'tasksPluginEmoji'` | emoji or dataview |

- **Created date added by hand:** the modal only adds ➕ to a *new* task: one with an empty description, or one where
  the global filter is about to be added (`src/Commands/CreateOrEditTaskParser.ts` `shouldUpdateCreatedDateForTask`).
  It never overwrites an existing ➕.
- **Global filter:** "You can set a global filter in the settings so that Tasks only matches specific checklist items.
  ... It doesn't have to be a tag. It can be any string. Leave it empty to regard all checklist items as tasks"
  (`docs/Getting Started/Global Filter.md`).
  - Matching is a plain substring test: `description.includes(globalFilter)` (source).
  - "Other checklist items will remain normal checklist items and not appear in queries or get a done date set"
    (en.json).
  - The modal **prepends** the filter when saving a task that lacks it: `globalFilter + ' ' + description`
    (`src/ui/EditableTask.ts`, `GlobalFilter.prependTo`).
  - The global filter tag is removed from `task.tags` when the line is read (`Task.parseTaskSignifiers`).
- **Remove global filter from description:** "removes the string that you set as global filter from the task
  description **when displaying** a task" (en.json). It applies in rendering and Quick Search only
  (`TaskLineRenderer.ts`, `QuickSearchTasksModal.ts`). The file is never changed (source).
- **Status types** (`src/Statuses/StatusConfiguration.ts` `StatusType`): `TODO`, `DONE`, `IN_PROGRESS`, `ON_HOLD`,
  `CANCELLED`, `NON_TASK`, plus an internal `EMPTY`. Docs list "one of `TODO`, `IN_PROGRESS`, `ON_HOLD`, `DONE`,
  `CANCELLED`, `NON_TASK`" (`docs/Getting Started/Statuses.md`). Note that **ON_HOLD** exists, which the brief did not
  list.
- **Default statuses** (`src/Config/StatusSettings.ts`; `docs/Getting Started/Statuses/Core Statuses.md`, `Custom
  Statuses.md`):
  | Symbol | Name | Next symbol | Type | Group |
  |---|---|---|---|---|
  | `space` | Todo | `x` | TODO | core |
  | `x` | Done | `space` | DONE | core |
  | `/` | In Progress | `x` | IN_PROGRESS | default custom |
  | `-` | Cancelled | `space` | CANCELLED | default custom |
  - Core symbols cannot be changed. Names, next symbol and type can.
  - Other predefined statuses (not in the defaults): `h` On Hold -> `space`, ON_HOLD; `Q` Non-Task -> `A`, NON_TASK
    (`src/Statuses/Status.ts`).
- **Unknown symbol:** treated as status "Unknown", next symbol `x`, type `TODO` (`Statuses.md`, "Unknown Statuses").
- **Toggle** (click or "Toggle task done"): the new status is the current status's *Next Status Symbol*. An
  unregistered next symbol is created as an Unknown TODO (`StatusRegistry.getNextStatusOrCreate`). Done and cancelled
  dates and recurrence then follow the *type* transition (2b, 2d). Docs: "Next Status Symbol — the status symbol to use
  when the task is toggled" (`Statuses.md`); `docs/Getting Started/Statuses/Recurring Tasks and Custom Statuses.md`.
  - For NON_TASK the docs flag an exception ("Except when the task status has type `NON_TASK`"). Details are
    **UNVERIFIED**; I did not read that subsection.
- **"done" in queries** = DONE, CANCELLED or NON_TASK. **"not done"** = TODO, IN_PROGRESS or ON_HOLD (`Statuses.md`,
  "Related searches"; `Task.isDone`).

---

## 4. Query semantics worth mirroring
- **Date filters** (`docs/Queries/Filters.md`, "Searching for dates"):
  - `on <date>` (the default), `before`, `after`, `on or before`, `on or after`.
  - Relative dates such as `today` are "calculated from today's date" and parsed by chrono.
  - Comparison is by day (`DateField.buildFilterFunction`: `isBefore(range.start)`, `isAfter(range.end)`, and
    inclusive `isSameOrAfter(start) && isSameOrBefore(end)` for `on`).
  - So `due today` means due date == today, and `due before today` means due date < today.
- **Missing date:** `due …`, `scheduled …`, `done …`, `created …`, `cancelled …` and `happens …` do **not** match a
  task without that date (`filterResultIfFieldMissing() = false` in each `*DateField.ts`).
  - Exception: **start** filters *do* match tasks with no start date (`StartDateField.ts` returns `true`). Docs: "When
    filtering queries by start date, the result will include tasks without a start date" (Filters.md, "Making Start
    Date only find tasks with Start").
- **Invalid dates** are excluded from date searches ("These task will silently not be found by date-based searches",
  `Dates.md`). They can be found with `due date is invalid`.
- **happens:** "returns any task for a matching start date, scheduled date, _or_ due date ... Only one of the dates
  needs to match. Invalid start, scheduled or due dates are ignored." `no happens date` = none of the three is set;
  `has happens date` = any is set (Filters.md, "Happens").
- **`no due date` / `has due date`:** whether the field is present (Filters.md, "Due Date").
- **`is recurring` / `is not recurring`:** `task.recurrence !== null`, so a task with an invalid rule is not recurring
  (`Task.isRecurring`; Filters.md, "Recurrence"). `recurrence includes` searches the **standardised** text,
  case-insensitively.
- **`priority is (above|below|not)? (lowest|low|none|medium|high|highest)`:** `none` means no signifier, and it sits
  between low and medium (Filters.md, "Priority"; Priority.md).
- **Tags:** `tags include <tag>` and `tag includes <tag>` are synonyms (Filters.md, "Tags"):
  - "Matches case-insensitive", and the global filter is disregarded.
  - "The `#` is optional ... If the `#` is given, it must be present, so searching for `#home` will match `#home` but
    not `#location/home`."
  - "The match is partial so `tags include foo` will match `#foo/bar` and `#foo-bar`."
  - Implementation: a case-insensitive substring test against **each** tag (`SubstringMatcher.stringIncludesCaseInsensitive`,
    `TagsField.value = task.tags`). So `tags include #waiting` also matches `#waiting/bob` and `#waitingfor`.
  - For an exact match use regex, e.g. `/#book$/i`.
- **`description includes`:** "Matches case-insensitive", disregards the global filter, and searches the description
  after signifiers, the global filter and trailing spaces have been removed. Tags **remain** in the searched text
  (Filters.md, "Description", example table). The regex form is case-sensitive by default.
- **Nothing is hidden by default.** Tasks has no implicit filter on future start or scheduled dates. A start date
  only matters through `starts …` filters ("use the start date as a filter to filter out any tasks that you cannot yet
  work on", Dates.md), and the default Global Query is empty (`globalQuery: ''`, Settings.ts). Based on reading these
  sources; I found no code that hides tasks by default.

---

## 5. Dependencies
- **`🆔 id`:** "one or more of the following allowed characters": a–z, A–Z, 0–9, `_`, `-`. Examples: `1`,
  `hftw84745`, `task_1`, `do-me-first`. Ids are "intended to be **unique across the whole vault** ... However, there
  is no checking for this" (`docs/Getting Started/Task Dependencies.md`; regex `[a-zA-Z0-9-_]+`).
- **`⛔ dependsOn`:** "one or more `id` values of other tasks, separated by commas (`,`) with optional spaces allowed",
  e.g. `dcf64c,0h17ye` or `dcf64c, 0h17ye`. Written back without spaces (source).
- **blocked:** the task is TODO, IN_PROGRESS or ON_HOLD, *and* a task it **directly** depends on is also TODO,
  IN_PROGRESS or ON_HOLD.
- **blocking:** the task has an id, is TODO, IN_PROGRESS or ON_HOLD, and some not-done task directly depends on it.
- In both cases only direct dependencies count, and DONE, CANCELLED and NON_TASK tasks are never blocked or blocking
  (Task Dependencies.md, "Blocking tasks" and "Blocked tasks"; `Task.isBlocked` / `isBlocking`). A dependsOn pointing
  at an id that does not exist does not block.
- Query forms are `is blocked`, `is not blocked` and `is blocking`.
- Recurrence removes `🆔` and `⛔` from the next occurrence (see 2d).

---

## UNVERIFIED / conflicting
1. **Docs vs code, recurrence text:** the Basic Example in `Recurring Tasks.md` shows `🔁 every Sunday` unchanged on
   the next and completed lines. The serializer writes `rrule.toText()`, Filters.md says `every Sunday` is
   standardised to `every week on Sunday`, and `RecurrenceField.test.ts` agrees. **The code is authoritative: the
   plugin canonicalises rule text when it rewrites a line.**
2. **Docs vs code, field order in examples:** the On Completion page shows `📅 … 🔁 … 🏁 delete` kept in that order
   after completion. The serializer would write `🔁 … 🏁 delete 📅 …`. The docs example is illustrative only.
3. **Docs vs code, variation selectors:** `Tasks Emoji Format.md` says "Tasks does not understand unicode Variation
   Selectors" (issue #2273). The current regex accepts an optional `️` after each emoji. The docs look stale for
   reading. The plugin always writes emojis without VS16.
4. **Feb 29 with `every year`:** the expected result (2025-02-28, step-back) is inferred from `nextAfterYears`. I did
   not run it.
5. **When `Recurrence.next()` can return null in practice:** for example, rrule `count` rules such as
   `every 2 days for 3 times` parse, but the docs call them unsupported. Not tested.
6. **Toggle exception for NON_TASK** (in `Recurring Tasks and Custom Statuses.md`): exists, details not read.
7. **Tags-filter wording vs implementation:** the docs say `#home` "will not match `#location/home`". With a plain
   substring test, `#home` is still found in `#home/sub` and `#homework`. I inferred this from the code and did not
   run it.
8. **Duplicate fields:** "leftmost copy wins" is my inference from the parse loop, not documented.
