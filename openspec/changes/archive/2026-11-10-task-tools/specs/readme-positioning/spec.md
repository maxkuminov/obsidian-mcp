## ADDED Requirements

### Requirement: The README SHALL document tasks as an optional capability that works on plain checkboxes

The README SHALL describe task support as an optional capability of the agent-memory server. It SHALL state that:

- plain `- [ ]` checkboxes work without the Obsidian Tasks plugin, whose emoji fields are optional;
- the server owns task mechanics (query, one-line edits, recurrence for a stated closed set of rules) and no task policy.

It SHALL list `list_tasks`, `task_agenda`, `complete_task`, `update_task` and `add_task` with one line each. It SHALL document every `TASKS_*` environment default and the panel's Tasks card. It SHALL NOT claim that every Tasks-plugin feature or recurrence rule is supported.

#### Scenario: A reader without the plugin
- **WHEN** the README's task section is read
- **THEN** it SHALL state that the Tasks plugin is not required, SHALL name the five task tools, and SHALL list each `TASKS_*` variable with its default

#### Scenario: Recurrence claims are bounded
- **WHEN** the README describes recurring tasks
- **THEN** it SHALL say that unsupported rules are refused with nothing written, and SHALL link to the list of supported rule shapes
