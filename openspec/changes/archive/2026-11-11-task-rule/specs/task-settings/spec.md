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
- the global filter text fails the filter rule below;
- the result of merging the submitted values over the stored ones has the rule `filter` and no global filter text.

**The filter rule.** It SHALL be one function, used both for `TASKS_GLOBAL_FILTER` at startup and on save. After surrounding spaces and tabs are stripped, the text SHALL:
- be 1–64 characters long, and not whitespace only;
- be a single line, containing no control character, line break, U+0085, U+2028 or U+2029;
- contain no backtick, no Tasks field emoji and no U+FE0F;
- contain no `%%`, no `<!--` and no `-->` at all, balanced or not;
- pass the structure check applied to caller-supplied task text, which requires:
  - an even number of `%%`;
  - no `<!--` without its `-->`, and no `-->` without its `<!--`;
  - no run of three or more backticks or tildes;
  - no leading list marker, checkbox, heading marker or `>`.

**Serialised saves.** Saves for one owner scope SHALL be serialised by a transaction-scoped advisory lock on that scope, taken before the stored row is read, so that the merged-state check is sound even for two concurrent first saves. The database SHALL also reject a stored `filter` rule without a filter text, and a blank filter text, by a CHECK. A save that violates it SHALL be refused with the same readable message, storing nothing.

**Defensive evaluation.** When the effective settings meet a rule they cannot evaluate, they SHALL treat it as `all` and record why. That is an unknown kind, `filter` with a NULL, empty or blank filter text, or a stored filter text that fails the filter rule. A response SHALL then state that the configured rule cannot be applied. The rule `fields` with no task tags is valid and means fields only.

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
- **WHEN** a user saves as the global filter any of:
  - `#task 📅`;
  - a filter containing a backtick;
  - a filter of 65 characters;
  - a filter containing U+2028;
  - `%%`;
  - `<!--`;
  - `~~~`;
  - `- todo`;
  - `[t]`;
  - `# task`;
  - `> task`;
  - a filter of U+3000 only
- **THEN** each save SHALL be refused with a message naming the problem, and nothing SHALL be stored
- **AND** `#task`, `TODO` and `[task]` SHALL be accepted

#### Scenario: A balanced Obsidian comment is refused as a filter
- **WHEN** a user saves the global filter `%%x%%`
- **THEN** the save SHALL be refused with a message naming the comment marker, and nothing SHALL be stored

#### Scenario: A balanced HTML comment is refused as a filter
- **WHEN** a user saves the global filter `<!-- task -->`
- **THEN** the save SHALL be refused with a message naming the comment marker, and nothing SHALL be stored

#### Scenario: Unsafe filter text in the environment refuses startup
- **WHEN** the server starts with `TASKS_GLOBAL_FILTER=%%` or `TASKS_GLOBAL_FILTER=<!--`
- **THEN** startup SHALL fail with a message naming `TASKS_GLOBAL_FILTER`

#### Scenario: Concurrent first saves cannot leave a filter rule without its text
- **WHEN** a scope has no row, and two saves run concurrently: one submitting `task_rule=filter` with `#task`, the other submitting only a blank global filter
- **THEN** the stored row SHALL never hold the rule `filter` without a filter text, and a refused save SHALL flash the filter-needs-text message with nothing stored

#### Scenario: An unevaluable stored rule falls back to every checkbox, visibly
- **WHEN** a row holds the rule `filter` with no filter text, written outside the server with the CHECK removed
- **THEN** `list_tasks()` SHALL list every checkbox the scope allows, and SHALL state that the configured rule cannot be applied

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
