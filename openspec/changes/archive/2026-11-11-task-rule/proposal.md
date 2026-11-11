Refs #434 (follow-up to #428, v1.3.0)

## Why

The task tools treat every Markdown checkbox as a task. In a real vault that has used them since #428, that is the wrong population:

- Of about 1,870 open checkboxes, about 210 carry a Tasks-plugin field (a date, a priority or a recurrence rule) and about 100 more carry one of the owner's workflow tags: about 310 real tasks. The other ~1,550 (83%) are plain checkboxes: work-ticket steps, runbooks, checklists, reference cards.
- In the folder that holds the owner's task lists, every open checkbox carries a field or a workflow tag; there are no plain checkboxes there at all. Of about 1,280 done checkboxes, all but ~30 carry a field (the done date).
- So "a task is a checkbox with task details" separates the two populations cleanly, while "every checkbox is a task" drowns `list_tasks`, its `text` filter and `add_task`'s duplicate and similar-task checks in non-tasks. `task_agenda` is barely affected: its sections are date-based.
- The default scope of #428 (excluded folders and excluded note tags) cannot express this. Plain checklists and real tasks sit in the same folders and the same notes.

The Obsidian Tasks plugin has its own answer, the **global filter** (a text every task line must contain, such as `#task`). #428 deliberately did not model it (its L12). This change models it, and adds a second rule for vaults that never set one.

## What Changes

- **A per-user task rule**, "which checkboxes count as tasks", next to the existing task settings, with a server-wide default from the environment:
  - `all`: every checkbox is a task. **The default**, exactly today's behaviour, and what the Tasks plugin does with no global filter.
  - `fields`: a checkbox is a task only if it carries a task field (due, scheduled, start, created, done or cancelled date; priority; recurrence rule) or one of the user's **task tags** (matched like `list_tasks(tags=…)`: by `/` segment, case-insensitively).
  - `filter`: a checkbox is a task only if its text contains the user's **global filter** text, the plugin's own mechanism (case-sensitive substring).
- **Where it applies: the default scope only, at query time, with no re-index.** `list_tasks` and `task_agenda` with `scope="default"`, and `add_task`'s index-side duplicate refusal and similar-task list, consider only checkboxes that are tasks by the caller's rule. `scope="all"` lifts the rule together with the folder and note-tag exclusions, as today.
- **Nothing is silently left out.** While a rule other than `all` is in force, a default-scope response carries one more bounded header line stating the rule, how many checkboxes it left out, how many it kept only because their text or tags were cut, and `pass scope="all" to list every checkbox`.
- **The rule never blocks a write.** The three write tools still address any checkbox by path and id.
  - `add_task` under `filter` puts the global filter text at the start of the new task's text when the line would not otherwise contain it (as the plugin's own task modal does), so a task the server adds is a task by the user's own rule.
  - `add_task` under `fields` still writes a line that has no field and no task tag, and the response says plainly that default listings will not show it. `update_task` says the same when its result is not a task by the rule.
- **Settings:** three new `task_settings` columns (migration 040), three environment defaults (`TASKS_RULE`, `TASKS_RULE_TAGS`, `TASKS_GLOBAL_FILTER`), validation, and a rule section on the panel's Tasks card written for people who do not use the plugin.

## Non-goals

- Changing the default rule for any existing deployment or user.
- Evaluating Tasks-plugin query blocks, the plugin's "remove global filter from description" display option, or custom statuses.
- Re-indexing, or storing the rule's verdict in the index.
- A per-call argument that picks a rule. The caller already has `scope`.
- New tool arguments or schema changes. Only descriptions and response text change.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `task-settings`: the task rule, its storage, environment defaults, validation and panel controls.
- `note-tasks`: the rule inside the default scope of `list_tasks` and `task_agenda`, its counts, the rule line and its bounds, and the cursor digest.
- `task-write`: `add_task` under each rule, the duplicate and similar scans, and the rule statement in `add_task` and `update_task` responses.
- `schema-integrity`: migration 040.
- `readme-positioning`: the README explains which checkboxes are tasks.

## Impact

- **Code:** a new pure module `src/services/task_rule.py`; `src/services/task_query.py` (`scope_clause` and the counts), `src/mcp_server/task_render.py`, `src/services/task_settings.py`, `src/config.py`, `src/models/db.py`, `src/services/task_write.py`, `src/mcp_server/tools.py`, `src/mcp_server/server.py` (three descriptions), the Tasks card template and its two routes.
- **Schema:** migration 040 adds three marked columns and two CHECKs to `task_settings`. 039 gains an allow-list for exactly those units, as 032 did for 039's. No data is written, nothing is re-derived and nothing is re-embedded. A running previous-image process keeps working, but a restarted previous image cannot start at 040. Rollback is roll-forward, or `alembic downgrade 039` with the new image first, which discards saved rules.
- **Behaviour:** none until someone chooses a rule other than `all`. Under `all`, every response is byte-identical to v1.3.0 and every v1.3.0 cursor stays valid.
- **Docs:** README (settings table and a short "which checkboxes are tasks"), `.env.example`, `DEPLOYMENT.md` (an upgrade note for 040), `docs/architecture/vault-tools.md`, `control-panel.md`, `schema-and-migrations.md`, `CLAUDE.md`.
