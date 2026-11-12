## MODIFIED Requirements

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

**`scope`.** `scope="default"` SHALL exclude tasks in notes under the caller's excluded folders, or whose note tags hierarchically match an excluded note tag, after one leading `#` is stripped from a stored note tag. `scope="all"` SHALL exclude nothing within the indexed notes. Every scope is defined over indexed notes only: a note the index does not hold (for example a sync-conflict copy excluded under `INDEX_EXCLUDE_CONFLICT_FILES`) contributes no task rows under either scope, and the response SHALL carry no line about it.

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

#### Scenario: A task in an excluded conflict copy
- **WHEN** `INDEX_EXCLUDE_CONFLICT_FILES` is true and the only open task matching a query sits in `N (conflict 2026-05-29-19-45-39).md`
- **THEN** `list_tasks(scope="all")` SHALL return no task for it, and the response SHALL be byte-identical to the same query over an index that never held that note
