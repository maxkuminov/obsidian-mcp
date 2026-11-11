## ADDED Requirements

### Requirement: The default scope SHALL apply the caller's task rule from indexed columns, through the one shared scope clause

Under `scope="default"`, `list_tasks` and `task_agenda` SHALL list only task rows that are tasks by the caller's effective task rule, together with the existing folder and note-tag exclusions. `scope="all"` SHALL apply neither. The rule SHALL be evaluated at query time from `note_tasks` columns, with no re-index and nothing stored, as follows.

**`all`** applies no predicate.

**`fields`** keeps a row when any of these holds:
- one of `due`, `scheduled`, `start`, `done_on`, `priority`, `recurrence`, `created` and `cancelled` is not NULL;
- the caller has task tags and the row carries one of them, matched hierarchically by `/` segment and case-insensitively, exactly as `list_tasks(tags=…)` matches;
- the caller has task tags and the row's `tags_cut` is true.

An invalid date, stored as NULL, SHALL NOT count as a field. On a row awaiting the version-5 backfill (NULL `task_key`):
- `recurrence`, `created` and `cancelled` SHALL be judged by the presence of `🔁`, `➕` and `❌` in the stored text;
- task tags SHALL be judged by the stored `tags`.

**`filter`** keeps a row when either holds:
- its stored `text` contains the global filter text as a case-sensitive substring;
- its `text_cut` is true.

**One implementation.** The rule SHALL be built in one place and composed into the default-scope keep predicate (`task_query.scope_clause`), which the `list_tasks` page, every `list_tasks` count, the `task_agenda` totals and rows, and `add_task`'s index-side duplicate and similar-task scans SHALL all use. Every statement SHALL keep the owner predicate in its join, so a caller's rule SHALL never widen what another owner's rows can show.

**Counts.** Let *other* be every filter of the call except the default scope, L the folder and note-tag exclusions, and R the rule. Then:
- `excluded by scope` SHALL count rows matching *other* ∧ ¬L, without consulting the rule;
- `excluded by rule` SHALL count *other* ∧ L ∧ ¬R;
- `kept though unsure` SHALL count kept rows whose verdict depended on cut data: `text_cut` without the filter text in the stored text under `filter`; task tags configured, `tags_cut`, and no field or task-tag match under `fields`.

Under `fields`, `task ids pending` SHALL ignore the rule.

**Cursor digest.** It SHALL include the rule only under `scope="default"` with a rule other than `all`:
- the kind and the sorted task tags under `fields`;
- the kind and the filter text under `filter`.

#### Scenario: Plain checklists are left out under fields, and counted
- **WHEN** a note holds `- [ ] Pay rent 📅 2026-11-30`, `- [ ] Chase reply #waiting-for/bob`, `- [ ] Step one` and `- [ ] Buy milk`, and the caller's rule is `fields` with the task tag `waiting-for`
- **THEN** `list_tasks()` SHALL return the first two, and the rule line SHALL state `excluded by rule: 2 tasks`
- **AND** `list_tasks(scope="all")` SHALL return all four

#### Scenario: The filter rule is a case-sensitive substring
- **WHEN** a note holds `- [ ] #task Book flights`, `- [ ] Book hotel #task`, `- [ ] #Task Rent car` and `- [ ] Pack bag`, and the caller's rule is `filter` with the text `#task`
- **THEN** `list_tasks()` SHALL return only the first two, and SHALL state `excluded by rule: 2 tasks`

#### Scenario: Every field family counts, an invalid date does not
- **WHEN** under `fields` with no task tags, the open checkboxes carry respectively only `⏳ 2026-12-01`, only `🛫 2026-12-01`, only `🔼`, only `🔁 every week`, only `➕ 2026-11-01`, only `📅 2026-02-30`, and nothing
- **THEN** the first five SHALL be listed, and the last two SHALL be excluded by rule

#### Scenario: A cut text is kept and stated
- **WHEN** under `filter` with `#task`, a checkbox's text is 1,200 characters and `#task` appears only after character 1,000
- **THEN** the task SHALL be listed, and the rule line SHALL state `kept though unsure (text cut): 1 tasks`

#### Scenario: The counts add up to the unscoped listing
- **WHEN** a caller excludes the folder `Archive/` and uses the rule `fields`, and the vault holds tasks and plain checkboxes both inside and outside `Archive/`
- **THEN** the rows of `list_tasks(scope="all")` SHALL be exactly the rows of `list_tasks()` plus `excluded by scope` rows plus `excluded by rule` rows, with no row counted twice

#### Scenario: Backfill rows are judged without silent loss
- **WHEN** under `fields`, a row awaiting the version-5 backfill has the text `Water plants 🔁 every week` and no date
- **THEN** it SHALL be listed, and the rule line SHALL state that the rule is approximate for the pending rows until the next index pass

#### Scenario: The agenda applies the rule
- **WHEN** under `filter` with `#task`, one overdue checkbox contains `#task` and another does not
- **THEN** `task_agenda()` SHALL show only the first, and its rule line SHALL state `excluded by rule: 1 tasks`

#### Scenario: Two owners, two rules
- **WHEN** user 7's rule is `filter` with `#task`, user 8's is `fields`, and each owns an identical note of tasks and checklists
- **THEN** each user's `list_tasks()`, counts and `task_agenda()` SHALL follow that user's own rule over that user's own rows only

#### Scenario: A rule change refuses an old cursor, and the default rule keeps old cursors
- **WHEN** a caller under `fields` obtains a `next` cursor, then changes the task tags
- **THEN** passing that cursor SHALL be refused with `invalid_argument`
- **AND** a cursor obtained under the rule `all` before this change was deployed SHALL still be accepted

### Requirement: While a task rule is in force, default-scope responses SHALL carry a bounded rule line, and SHALL be unchanged otherwise

Default-scope responses SHALL carry a rule line when `scope="default"` and the effective rule is `fields` or `filter`:
- `list_tasks` SHALL carry the query line, and directly after it one rule line;
- `task_agenda` SHALL carry the same rule line directly after its header line.

The same rule line SHALL appear under `scope="default"` when a configured rule could not be applied and was treated as `all`. It SHALL then read `Rule — task rule: all (the configured rule cannot be applied: <reason>) — pass scope="all" to list every checkbox`.

In every other case, including a chosen or inherited rule `all` and any `scope="all"` call, no rule line SHALL appear, and the response SHALL be byte-identical to its form before this change.

**Wording.** The rule line SHALL read, with every count stated even at zero:

```
Rule — task rule: fields — task tags: <n>[ [<names> +K more]] — excluded by rule: <N> tasks — kept though unsure (tags cut): <U> tasks[ — rule approximate for <P> tasks until the next index pass] — pass scope="all" to list every checkbox
Rule — task rule: filter <filter as a JSON string> — excluded by rule: <N> tasks — kept though unsure (text cut): <U> tasks — pass scope="all" to list every checkbox
```

- The `rule approximate` part SHALL appear only under `fields` when P, the number of matching rows awaiting the version-5 backfill, is above zero.
- Task-tag names SHALL be listed, each cut at 32 characters, while they fit.

**Bounds.**
- The rule line SHALL be at most 320 characters, and no part other than the tag names SHALL be cut.
- With the rule line present, a `list_tasks` call with a match SHALL never be refused when `MAX_READ_RESPONSE_CHARS` is at least 1,921, and SHALL keep at least 40 characters each of path and text from 1,962.
- `task_agenda` with the rule line SHALL be refused with `response_cap_too_small` below 3,121, instead of 2,800.
- A call with no match SHALL never be refused.

#### Scenario: The default rule changes nothing
- **WHEN** a fixed set of `list_tasks` and `task_agenda` calls runs with no rule configured, and again with a stored rule `all` whose task tags and filter text are filled in
- **THEN** every response SHALL be byte-identical to the responses of the version before this change

#### Scenario: The rule line tells the agent how to see everything
- **WHEN** a caller under `filter` with `#task` calls `list_tasks()`
- **THEN** the third line SHALL begin `Rule — task rule: filter "#task"` and SHALL end `pass scope="all" to list every checkbox`

#### Scenario: scope all carries no rule line
- **WHEN** a caller under `fields` calls `list_tasks(scope="all")`
- **THEN** the response SHALL have no rule line, and its query line SHALL state `scope: all`

#### Scenario: The worst-case rule line fits
- **WHEN** the rule line is rendered with every count at ten digits, 32 task tags of 128 characters under `fields`, and a 64-character filter text of double quotes under `filter`
- **THEN** each line SHALL be at most 320 characters, and every count and the `scope="all"` instruction SHALL be present

#### Scenario: The minimum cap with the rule line
- **WHEN** `MAX_READ_RESPONSE_CHARS` is 1,921, every header count has ten digits, echoes are maximal and the rule line is present
- **THEN** `list_tasks` SHALL render one row, abbreviated if necessary, SHALL NOT exceed 1,921 characters and SHALL NOT be refused
