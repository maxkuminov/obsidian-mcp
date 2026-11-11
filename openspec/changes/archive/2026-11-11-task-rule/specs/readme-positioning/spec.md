## MODIFIED Requirements

### Requirement: The README SHALL document tasks as an optional capability that works on plain checkboxes

The README SHALL describe task support as an optional capability of the agent-memory server. It SHALL state that:

- plain `- [ ]` checkboxes work without the Obsidian Tasks plugin, whose emoji fields are optional;
- the server owns task mechanics (query, one-line edits, recurrence for a stated closed set of rules) and no task policy.

It SHALL list `list_tasks`, `task_agenda`, `complete_task`, `update_task` and `add_task` with one line each. It SHALL document every `TASKS_*` environment default and the panel's Tasks card. It SHALL NOT claim that every Tasks-plugin feature or recurrence rule is supported.

It SHALL explain, in a few sentences for a reader without the plugin, which checkboxes count as tasks:

- every checkbox, by default;
- the `fields` rule (a task field or a task tag) and the `filter` rule (the plugin's global filter text);
- that the rule applies to default listings and duplicate checks only;
- that `scope="all"` always lists every checkbox;
- that a rule never blocks a write.

#### Scenario: A reader without the plugin
- **WHEN** the README's task section is read
- **THEN** it SHALL state that the Tasks plugin is not required, SHALL name the five task tools, and SHALL list each `TASKS_*` variable with its default

#### Scenario: Recurrence claims are bounded
- **WHEN** the README describes recurring tasks
- **THEN** it SHALL say that unsupported rules are refused with nothing written, and SHALL link to the list of supported rule shapes

#### Scenario: The task rule is explained
- **WHEN** the README's task section is read
- **THEN** it SHALL name the three rules and their default, `TASKS_RULE`, `TASKS_RULE_TAGS` and `TASKS_GLOBAL_FILTER`, and SHALL say that `scope="all"` lists every checkbox
