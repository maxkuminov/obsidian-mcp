Refs #428

## Why

The server has one task verb, `list_tasks` (#350), and it is read-only. Agents are the main consumer of tasks, and everything else they need they do by reading notes and string-editing lines. Production usage between 2026-10-30 and 2026-11-10 (56 `list_tasks` calls from four kinds of client) shows the gaps:

- **Date windows have no lower bound.** The most common call is `due_by` plus `status`. Its responses average about 29,000 characters and reach the 40,000-character response cap, so 8 of the 56 calls are cursor continuation pages. A caller asking for "next week" passes `due_by = today+7` and gets every overdue task as well.
- **Tag filters return wrong answers silently.** `tags=["waiting-for"]` returned nothing twice, because the tasks are tagged `#waiting-for/<who>` and matching is exact.
- **A listing is not enough to act on.** Within five minutes of each `list_tasks` call, the same credential makes about four `read_note` and two `keyword_search` calls.
- **Tasks are changed by substring replace.** In 30 days, 37 `edit_note(find=…)` calls carried a checkbox in `find`. The `path:line` a row prints is usable by no tool. A short task text matches inside a longer one, identical lines are `match_ambiguous`, nothing adds a done date, completing a recurring task by text edit silently ends the series, and `list_tasks` shows the old state for up to one index pass afterwards.
- **Other clients have rebuilt the missing verbs outside the server**: a grep-based agent, cron prompts, and a separate service with its own scanner, task id and one-line completion writer that refuses recurring tasks. That writer's rules set the safety bar: derive the id from the task text, re-derive it under the lock from the bytes about to be replaced, refuse on ambiguity, never reformat.
- **An unscoped listing is mostly noise.** In a real vault of about 3,900 notes there are 1,872 open checkboxes, only about 12% of them dated, and about half are checklists and work-ticket boxes rather than personal tasks.

This is a public project. Many deployments are generic agent memory over markdown, with no Obsidian Tasks plugin installed. Everything here therefore works on plain `- [ ]` checkboxes, treats the Tasks-plugin emoji fields as optional, ships with safe defaults, and is documented for people who are not the owner.

**Principle.** The server owns task **mechanics**: querying, identifying one line, changing one line safely, recurrence, and canonical formatting of a new line. It owns no task **policy**: where tasks belong, GTD rules, or what counts as a duplicate worth blocking. Policy stays in the vault guide and in skills.

## What Changes

- **`list_tasks` gains additive arguments.**
  - Date windows: `due_from`, `scheduled_from`, `done_from` and `done_by`.
  - Relative dates on every date argument: `today`, `tomorrow`, `yesterday`, `+Nd` and `-Nd`, resolved in the caller's task timezone and echoed in the header.
  - Filters: `undated`, `has_due`, `text` (all terms, case-insensitive substring, any language), `priority`, `recurring`, `exclude_tags` and `path` (exactly one note).
  - `order_by`: `due`, `priority`, `scheduled`, `done` or `path`.
  - `detail`: `full` or `brief`.
  - `context`: also return the lines under a task.
  - `scope`: `default`, which applies the caller's settings, or `all`.
  - **Every row carries a task id.**
  - **`tags` is amended on purpose.** Matching becomes hierarchical by segment and case-insensitive, as Obsidian treats nested tags: `waiting-for` matches `#waiting-for` and `#Waiting-For/chase`, but not `#waiting-forever`.
  - Exclusions are never silent. The header states the timezone, the resolved dates, the scope in force, and how many tasks the scope excluded.
- **A new read tool, `task_agenda`.** It returns, from one snapshot, counted sections for a date: overdue, due today, scheduled and now due (split into "no due date" and "due later"), and upcoming, grouped by date. Rows are brief and carry ids. Each section is capped, and a capped section ends with an exact `list_tasks` continuation, cursor included.
- **Three new write tools, each changing one task line in place.** All three take a note `path` plus a task id, never a line number.
  - `complete_task` marks one task done, adds a done date (a setting, on by default) and, for a recurring task, writes the next occurrence with the dates the Tasks plugin would compute. Recurrence is a **closed** grammar of rule texts. Any other rule is refused (`recurrence_unsupported`) with nothing written.
  - `update_task` sets or clears dates, priority, tags and the recurrence rule, changes status (`open`, `in_progress`, `cancelled`), replaces the description, or appends a context line.
  - `add_task` inserts one new line into an existing note, next to that section's (or note's) task list, serialised in the plugin's field order. It refuses an exact open duplicate in the caller's scope unless `allow_duplicate=true`, and lists similar open tasks without blocking on them.
  - All three share the `edit_note` safety envelope: `dry_run`, the anchored publish path with its in-call conflict check, `expected_hash`, the write permission gate, read-only-grant hiding, the write rate bucket, typed outcomes and `MCP-REFUSAL` codes, attributed change-feed events and usage attribution.
  - **Edits are minimal.** Only the fields a call names change, in place, and the rest of the line keeps its bytes: indentation, list marker, blockquote prefix, field order, tag positions, rule text and line ending.
  - **Resolution is bound to the bytes being replaced.** The id is re-derived from the note's current bytes, and any ambiguity or change refuses with nothing written.
  - **Read-your-writes.** After a successful write, a following `list_tasks` or `task_agenda` reflects it without waiting for the index pass. If that refresh fails, the write still stands and the response says the listing may lag.
- **Per-user task settings**, with a "Tasks" card in the control panel (single-user mode included) and environment defaults:
  - timezone;
  - excluded folders and excluded note tags, which form the default scope;
  - the Tasks plugin's own write settings, each with the plugin's default: done date on completion, cancelled date on cancel, created date on add, and the next recurrence above or below the completed line.
- **Index changes (migration 039, `CURRENT_EXTRACTION_VERSION` 4 → 5).**
  - `note_tasks` gains:
    - the stored task key, so ids work beyond the 1,000-character text cut;
    - its ordinal and count within the note;
    - a description key, used for duplicate checks;
    - a case-folded text, used for `text`;
    - the tag prefixes, used for hierarchical matching;
    - the recurrence rule, the created and cancelled dates;
    - the capped context lines.
  - `notes_metadata` gains the two columns that make the write-time task refresh safe against a concurrent pass (`tasks_source_hash` and `tasks_refreshed_at`).
  - A new table, `task_settings`, holds the settings.
  - The one-time re-derive costs one index pass and re-embeds nothing.
- **Tool surface: 28 → 32 tools.** A read-only grant lists 20 instead of 19. The three write tools join `WRITE_REQUIRED_TOOLS` and the write rate bucket. `add_task` is additive, not destructive. `CLASS_MAPPING_VERSION` goes 5 → 6.
- **Documentation in the same PR**: README, `.env.example`, `DEPLOYMENT.md` upgrade notes, server instructions, the vault-guide primer, every tool description, the architecture notes and `CLAUDE.md`.

## Non-goals

- Evaluating Tasks-plugin or Dataview query blocks.
- The Dataview inline-field task format (`[due:: …]`).
- Moving a task between notes (a follow-up issue).
- Task hygiene audits and policy checks (misplaced tasks, missing priority, and so on).
- Dependencies (`🆔` / `⛔`), beyond leaving them intact on the line and dropping them from a next occurrence as the plugin does.
- Writing identity markers into notes.
- Creating notes from `add_task`.
- Rebuilding a line in the plugin's canonical order. Edits are minimal (design D4).
- The plugin's "Remove scheduled date on recurrence", custom status cycles, and the global task filter.
- Refreshing the task index from `edit_note` and the other generic write tools (L5).

## Capabilities

### New Capabilities

- `task-write`: the task id and its resolution against current bytes, the minimal one-line editor, the closed recurrence grammar and date arithmetic, `complete_task`, `update_task`, `add_task`, duplicate detection, the write-time task index refresh, refusal codes and the response shape.
- `task-settings`: per-user task settings (timezone, default scope, plugin-mirroring write behaviour), their storage, environment defaults, validation and the panel card.

### Modified Capabilities

- `note-tasks`:
  - MODIFIED: the extraction requirement (version 5 and the new derived columns), the lifecycle requirement (version 5 and the write-time refresh), the `list_tasks` contract (new arguments, amended `tags`, scope), paging (cursor version 2, every `order_by`) and rendering (ids, the query line, brief rows, context, bounds).
  - ADDED: `task_agenda` and relative dates.
- `index-integrity`: ADDED, "the write-time task refresh SHALL compose with the index pass and converge".
- `schema-integrity`: ADDED, "migration 039 SHALL own the task-id columns, the refresh columns and `task_settings` as marked units".
- `mcp-request-routing`: MODIFIED, covering tool metadata (32 tools, the destructive rule), the tool surface (additions, the `list_tasks` additions-entry amendment, the v2 full-list oracle) and the scoped list (`WRITE_REQUIRED_TOOLS` of 12, read-only grant lists 20).
- `mcp-rate-limits`: MODIFIED, "Two per-principal token buckets admit every tool call" (eleven vault-mutating tools).
- `change-feed`: MODIFIED, "MCP write tools SHALL record attributed events" (the three task write tools record `modified`).
- `note-filters`: MODIFIED, "Read-path owner scoping is total" (`task_agenda`, plus the duplicate check of `add_task`).
- `vault-guide`: MODIFIED, "Guide points at `list_tasks`", which now also names the task write tools and `task_agenda`.
- `readme-positioning`: ADDED, "tasks are documented as an optional capability".

## Impact

- **Schema**: `alembic/versions/039_task_tools.py` (`down_revision = "038"`) adds:
  - nullable columns on `note_tasks` and `notes_metadata`, with their markers;
  - the `task_settings` table;
  - two indexes.
  
  No data is written. `alembic check` stays clean. Downgrade drops only the marked objects.
- **Code (new modules)**:
  - `src/services/task_line.py` (pure line parser and editor);
  - `src/services/task_recurrence.py` (pure grammar and arithmetic);
  - `src/services/task_dates.py` (relative dates and timezones);
  - `src/services/task_settings.py`;
  - `src/services/task_index.py` (write-time refresh);
  - `src/services/task_query.py` (`list_tasks` and `task_agenda` queries);
  - `src/services/task_write.py` (resolution, plans and duplicate scan);
  - `src/mcp_server/task_render.py`.
- **Code (changed)**:
  - `src/services/tasks.py` (extraction version 5);
  - `src/services/indexer.py` (version 5, the scan start time, task-dirty rows, the refresh guard);
  - `src/services/embeddings.py` (the version-5 cleaner alias);
  - `src/models/db.py`;
  - `src/config.py`;
  - `src/mcp_server/tools.py` (thin implementations, `list_tasks_impl` rewired);
  - `src/mcp_server/server.py` (four registrations, the `list_tasks` description, the instructions);
  - `src/mcp_server/tool_permissions.py`;
  - `src/services/concurrency.py`;
  - `src/mcp_server/vault_guide_primer.md`;
  - the panel: `src/control_panel/routes.py`, `templates/account.html` and `templates/settings.html`.
- **Dependencies**: `tzdata` (pinned), so `zoneinfo` works without OS time-zone data. No `python-dateutil` and no rrule port (design D7).
- **Tests**:
  - unit tests for the id, the line editor, the recurrence table, relative dates and rendering bounds;
  - integration tests (`tests/integration/`) for resolution, refresh, convergence with a concurrent pass, tenancy, scope and the agenda;
  - tool-surface tests and fixtures (additions and amendment, `tools_list_full_v2.json`, the 20-entry read-only fixture);
  - panel tests and the `page_account_*` goldens.
- **Operations**:
  - one full re-derive pass after deploy. Nothing is re-embedded. `move_note(rewrite_links=True)` returns `index_not_ready` until the pass finishes.
  - `CLASS_MAPPING_VERSION` 6 starts a new concurrency-readiness epoch.
  - `list_tasks` cursors issued before the deploy are refused once (`invalid_argument`).
  - A new boot refusal: an invalid `TASKS_DEFAULT_TIMEZONE`.
