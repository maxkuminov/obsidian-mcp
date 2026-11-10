## MODIFIED Requirements

### Requirement: Guide points at `list_tasks` for task queries

The primer returned by `get_vault_guide` SHALL state, in its Tasks section and in neutral framing, that:

- `list_tasks` lists checkbox tasks across the vault from the index, without reading notes;
- `task_agenda` returns a dated overview;
- `complete_task`, `update_task` and `add_task` change one task line, addressed by the note path and the task id a listing prints;
- plain checkboxes work without the Tasks plugin, whose emoji fields are optional;
- Tasks-plugin query blocks are literal text this server does not evaluate.

It SHALL NOT state any vault-specific task policy.

The server instructions SHALL name `list_tasks`, `task_agenda`, `complete_task`, `update_task` and `add_task` in one sentence outside the 512-character lead, and SHALL stay within the 2,000-character cap.

#### Scenario: The primer names the task tools

- **WHEN** `get_vault_guide` is invoked
- **THEN** the primer's Tasks section SHALL mention `list_tasks`, `task_agenda`, `complete_task`, `update_task` and `add_task`, SHALL say that write tools take a path plus a task id, and SHALL say that query blocks are not evaluated
- **AND** the wording SHALL NOT use "MUST", "IMPORTANT" or an equivalent imperative to rank any tool above another

#### Scenario: The instructions name the task tools within their caps

- **WHEN** the server's `instructions` are inspected
- **THEN** they SHALL contain `list_tasks`, `task_agenda`, `complete_task`, `update_task` and `add_task`, the first 512 characters SHALL be unchanged by this change, and the total SHALL be at most 2,000 characters
