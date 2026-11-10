## ADDED Requirements

### Requirement: Task settings SHALL be stored per owner scope with environment defaults matching the Tasks plugin's own

The server SHALL store task settings in a `task_settings` table holding at most one row per owner scope. The scope is a user id, or NULL for single-user mode, unique with NULLs not distinct, and a user's row SHALL cascade on user deletion. A row SHALL hold:

- a timezone (an IANA name, or NULL for the server default);
- excluded folders (at most 32 vault-relative prefixes);
- excluded note tags (at most 32, stored lower-case without `#`);
- four nullable booleans: `set_done_date`, `set_cancelled_date`, `set_created_date` and `next_recurrence_below`.

The effective value of each NULL or absent field SHALL come from the environment defaults:

| Variable | Default |
|---|---|
| `TASKS_DEFAULT_TIMEZONE` | `UTC` |
| `TASKS_SET_DONE_DATE` | `true` |
| `TASKS_SET_CANCELLED_DATE` | `true` |
| `TASKS_SET_CREATED_DATE` | `false` |
| `TASKS_NEXT_RECURRENCE_BELOW` | `false` |

With no exclusions configured, the default scope SHALL exclude nothing. Startup SHALL be refused when `TASKS_DEFAULT_TIMEZONE` does not name a timezone available to this build. Every variable SHALL be documented in `README.md` and `.env.example`. The server SHALL NOT expose the plugin's "Remove scheduled date on recurrence" setting, and SHALL behave as the plugin does with it off.

#### Scenario: No row means plugin defaults
- **WHEN** a user has no `task_settings` row and completes a task
- **THEN** a done date SHALL be added, a completed recurring task's next occurrence SHALL be placed above it, and no created date SHALL be added

#### Scenario: An invalid default timezone refuses startup
- **WHEN** the server starts with `TASKS_DEFAULT_TIMEZONE=Mars/Olympus`
- **THEN** startup SHALL fail with a message naming the variable

#### Scenario: Deleting a user removes their settings
- **WHEN** a user with a `task_settings` row is permanently deleted
- **THEN** the row SHALL be gone

### Requirement: Task settings SHALL be validated on save and read once per tool call

Each write-behaviour field SHALL take `on`, `off` or `default`. `default` stores NULL, so the environment default applies, and a field absent from the submitted form SHALL leave its stored value unchanged (NULL for a new row). Saving settings SHALL validate the following, refusing the whole save with a flashed message and nothing stored when any check fails:

- the timezone SHALL be a member of the available IANA timezones, and SHALL NOT be a host pseudo-zone (`localtime`, `Factory`, `posixrules`, or a name under `posix/` or `right/`);
- each excluded folder SHALL be vault-relative, contain no `..` segment and no leading `/`, be at most `MAX_PATH_CHARS` long, and be stored normalised with a trailing `/`;
- each excluded note tag SHALL match the inline-tag grammar after one leading `#` is stripped;
- there SHALL be at most 32 of each list.

Each task tool SHALL read the caller's effective settings once per call. A stored timezone that this build cannot load SHALL fall back to UTC, and the tool's response SHALL say so. That fallback SHALL NOT be a refusal.

#### Scenario: An invalid folder is refused
- **WHEN** a user saves an excluded folder `../other`
- **THEN** the save SHALL be refused with a flashed message, and the stored settings SHALL be unchanged

#### Scenario: Tags are normalised
- **WHEN** a user saves the excluded note tag `#Ticket`
- **THEN** it SHALL be stored as `ticket` and SHALL exclude notes tagged `#ticket`, `#Ticket` or `#ticket/ops`

### Requirement: The control panel SHALL offer a Tasks settings card for the caller's own scope in both modes, under the panel's CSP rules

In multi-user mode, the account page SHALL show a "Tasks" card that edits the signed-in user's own row (`POST /admin/account/tasks`, 404 in single-user mode). In single-user mode, where the account page does not exist, the Settings page SHALL show the same card, editing the NULL-scope row (`POST /admin/settings/tasks`, administrators only, 404 in multi-user mode). Both pages SHALL render one shared card template.

The card SHALL offer:

- a timezone input with suggestions from a fixed list of common IANA names, accepting any valid IANA name;
- excluded folders and excluded note tags, one per line;
- the four write behaviours, each as a three-option select (server default, showing its current value; on; off), labelled with the Tasks plugin's own setting name.

The card SHALL show the effective defaults. Each form SHALL carry the CSRF token, answer with a session flash and a 303 redirect, and SHALL contain no inline event handler, no inline `style` attribute and no `javascript:` URL. Any `<script>` or `<style>` SHALL carry the CSP nonce. No user SHALL be able to read or change another user's task settings through these routes.

#### Scenario: Multi-user save
- **WHEN** a signed-in user sets the timezone `Europe/Berlin` and excludes `Archive/` on the account page
- **THEN** that user's row SHALL hold both values, and `list_tasks` called with that user's credential SHALL state `timezone: Europe/Berlin` and `excluded folders: 1`

#### Scenario: Single-user mode uses the Settings page
- **WHEN** the server runs in single-user mode
- **THEN** `POST /admin/account/tasks` SHALL answer 404, the Settings page SHALL show the Tasks card, and saving it SHALL update the NULL-scope row that MCP callers there use

#### Scenario: The card passes the CSP checks
- **WHEN** the account and settings pages are rendered and scanned
- **THEN** neither SHALL contain an `on*=` attribute, a `style=` attribute or a `javascript:` URL, and every `<script>` and `<style>` SHALL carry the nonce

#### Scenario: Pseudo-zones are refused
- **WHEN** a user saves the timezone `localtime`, `Factory` or `posix/Europe/Berlin`
- **THEN** the save SHALL be refused, and nothing SHALL be stored

#### Scenario: An absent field keeps its stored value
- **WHEN** a user's row stores `set_done_date = false`, and a save submits the form without the `set_done_date` field
- **THEN** `set_done_date` SHALL remain false, and a save submitting `default` SHALL store NULL
