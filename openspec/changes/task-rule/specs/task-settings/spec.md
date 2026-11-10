## ADDED Requirements

### Requirement: Task settings SHALL hold a task rule that decides which checkboxes count as tasks, inherited from the environment as one unit

Each owner scope's `task_settings` row SHALL hold three more fields:

- `task_rule`: `all`, `fields`, `filter`, or NULL for "the server's rule";
- `task_tags`: at most 32 tags, stored lower-case without `#`;
- `global_filter`: 1–64 characters, or NULL.

When the scope has no row, or its `task_rule` is NULL, the effective rule SHALL be the server's whole rule from the environment:

| Variable | Default |
|---|---|
| `TASKS_RULE` | `all` |
| `TASKS_RULE_TAGS` | empty (comma-separated) |
| `TASKS_GLOBAL_FILTER` | empty |

Otherwise the effective rule SHALL be the row's kind, the row's task tags and the row's global filter, and no environment value of the rule SHALL apply.

The effective rule SHALL be part of the effective settings, read once per tool call with no cache, so a saved change applies from the caller's next call.

**The rules.**
- `all`: every checkbox is a task.
- `fields`: a checkbox is a task when it carries at least one of the eight task fields (due, scheduled, start, created, done or cancelled date; priority; recurrence rule), or a task tag matched as `list_tasks(tags=…)` matches.
- `filter`: a checkbox is a task when its text contains the global filter text as a case-sensitive substring.

**Startup.** It SHALL be refused, naming the variable, when:
- `TASKS_RULE` is not one of the three;
- a `TASKS_RULE_TAGS` entry fails the tag rule, or there are more than 32;
- `TASKS_GLOBAL_FILTER` fails the filter rule;
- `TASKS_RULE=filter` is set with an empty `TASKS_GLOBAL_FILTER` (naming both variables).

**Validation on save.** Saving SHALL refuse the whole save with a flashed message, storing nothing, when:
- the rule value is not `default`, `all`, `fields` or `filter`;
- a task tag, after one leading `#` is stripped and the tag is lower-cased, does not match the tag grammar, is over 128 characters, or ends in `/`;
- more than 32 task tags are given;
- the global filter text, after surrounding spaces and tabs are stripped, is longer than 64 characters, or contains a control character, a line break, a line or paragraph separator, a backtick, a Tasks field emoji or U+FE0F;
- the result of merging the submitted values over the stored ones has the rule `filter` and no global filter text.

`default` SHALL store NULL. A blank global filter SHALL store NULL. A field absent from the submitted form SHALL leave its stored value unchanged. Task tags or a filter text stored while another rule is chosen SHALL be kept and unused.

Every variable SHALL be documented in `README.md` and `.env.example`.

#### Scenario: No row means every checkbox
- **WHEN** a deployment sets none of the new variables and a user has no `task_settings` row
- **THEN** the user's effective rule SHALL be `all`, and every task tool response SHALL be byte-identical to the response before this change

#### Scenario: The server default is inherited as one unit
- **WHEN** the environment sets `TASKS_RULE=fields` and `TASKS_RULE_TAGS=someday`, and a user's row stores `task_rule = 'fields'` with no task tags
- **THEN** that user's effective rule SHALL be `fields` with no task tags, and a user whose row stores `task_rule` NULL SHALL get `fields` with `someday`

#### Scenario: A filter rule without its text is refused on save
- **WHEN** a user's stored row has no global filter, and a save submits `task_rule=filter` with a blank global filter
- **THEN** the save SHALL be refused with a message saying the filter rule needs a global filter text, and the stored row SHALL be unchanged

#### Scenario: A filter rule without its text refuses startup
- **WHEN** the server starts with `TASKS_RULE=filter` and no `TASKS_GLOBAL_FILTER`
- **THEN** startup SHALL fail with a message naming both variables

#### Scenario: Unsafe filter text is refused
- **WHEN** a user saves the global filter `#task 📅`, a filter containing a backtick, a filter of 65 characters, or a filter containing U+2028
- **THEN** each save SHALL be refused with a message naming the problem, and nothing SHALL be stored

#### Scenario: Task tags are normalised and bounded
- **WHEN** a user saves the task tags `#Someday` and `waiting-for`
- **THEN** they SHALL be stored as `someday` and `waiting-for`
- **AND** a save of 33 task tags, or of `waiting-for/`, SHALL be refused

#### Scenario: An absent rule field keeps its stored value
- **WHEN** a user's row stores `task_rule = 'filter'` and `global_filter = '#task'`, and a save submits only `timezone`
- **THEN** the rule and the filter text SHALL be unchanged

#### Scenario: A rule change applies on the next call
- **WHEN** a user saves `task_rule = 'fields'` between two `list_tasks()` calls
- **THEN** the second call SHALL apply the `fields` rule

### Requirement: The Tasks card SHALL let a person choose the task rule without knowing the Tasks plugin

The shared Tasks card SHALL show a block titled "Which checkboxes are tasks", with three controls:

- a select `task_rule` whose options are: server default (naming the server's rule), every checkbox, only checkboxes with task details or a task tag, and only checkboxes containing the global filter text;
- a task-tags textarea, one tag per line, labelled with the rule that uses it;
- a global-filter text input of at most 64 characters, labelled with the rule that uses it.

**Help text.** The block SHALL explain, for a reader who does not use the Tasks plugin:
- that a task is a Markdown checkbox;
- what each option means;
- what the task details are (the optional emoji fields, listed);
- that the global filter is the Tasks plugin's "Global task filter";
- that agents can always ask for every checkbox.

It SHALL state the rule in use now, and whether it comes from the server default or from this card.

**CSP and routes.** The block SHALL contain no script, no inline event handler, no inline `style` attribute and no `javascript:` URL. The two existing task-settings routes SHALL pass the three new fields to the save, and only the card's own field names.

#### Scenario: The card shows the rule in use
- **WHEN** the server default is `all` and a user's row stores `task_rule = 'fields'` with the task tag `someday`
- **THEN** the card SHALL show `fields` selected, `someday` in the task-tags field, and an "in use now" line naming the `fields` rule and the tag as set on this card

#### Scenario: The card passes the CSP checks with the rule block
- **WHEN** the account and settings pages are rendered with the rule block and scanned
- **THEN** neither SHALL contain an `on*=` attribute, a `style=` attribute or a `javascript:` URL, and the account-page goldens SHALL match their regenerated versions

#### Scenario: A user cannot set another user's rule
- **WHEN** a signed-in user posts the card with an extra `user_id` field naming another user
- **THEN** only the signed-in user's own row SHALL change
