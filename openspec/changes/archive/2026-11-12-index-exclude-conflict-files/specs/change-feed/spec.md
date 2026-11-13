## ADDED Requirements

### Requirement: A row removed because its path is an excluded conflict copy SHALL be recorded as `deleted` with `reason = 'not_indexable'`, and a row-less excluded path SHALL record nothing

When the index pass deletes a row because its path is excluded under `INDEX_EXCLUDE_CONFLICT_FILES`, it SHALL record one `deleted` event at that path with `reason = 'not_indexable'`, `source = 'index'`, rendered `by human/sync`. It SHALL NOT use `reason = 'removed'`, which states that the file is gone. A discovered excluded path with no row SHALL record no event of any kind. No new `reason` value SHALL be introduced, and the `get_changes` description SHALL NOT change.

When the setting is turned off, the first pass that upserts a previously excluded path SHALL treat it exactly as any newly discovered note, under the existing rules and with no new exception:

- normally `created`, with its canonical `content_hash`;
- `moved`, when the existing same-hash pairing applies;
- one `reset`, instead of per-note creations, when the scope had no rows at the start of that pass (the existing baseline rule);
- nothing, when the existing suppression applies. That happens when the latest event at the path is an MCP content event with the same hash, which an agent's `edit_note` or `create_note` on the copy while it was excluded leaves behind (limitation L9).

These events SHALL be subject to the existing suppression, ordering and per-pass overflow rules unchanged.

#### Scenario: Turning the setting on

- **WHEN** a scope holds a row at `A (conflict 2026-05-29-19-45-39).md` and the server restarts with the setting true
- **THEN** the first pass SHALL record exactly one event for that path, `deleted` with `reason=not_indexable` and `by human/sync`, and the following pass SHALL record none

#### Scenario: A new conflict copy while the setting is on

- **WHEN** the setting is true and a sync tool writes `F (conflict 2026-06-01-10-00-00).md`, which never had a row
- **THEN** no pass SHALL record any event for that path

#### Scenario: Turning the setting off

- **WHEN** the server restarts with the setting false over a vault holding `A.md` (indexed) and `A (conflict 2026-05-29-19-45-39).md` with no row, and no MCP event at the copy's path
- **THEN** the next pass SHALL record `created` for the copy's path with its content hash and `by human/sync`

#### Scenario: Turning the setting off over an empty scope

- **WHEN** the scope's only notes are excluded conflict copies, so it has no rows, and the server restarts with the setting false
- **THEN** the next pass SHALL record one `reset` and no per-note `created`

#### Scenario: Turning the setting off after an agent edited the copy (pinned limitation)

- **WHEN** the setting is true, `edit_note` writes the excluded copy (its `modified` event, `source = 'mcp'`, is the latest at that path), and the server restarts with the setting false
- **THEN** the next pass SHALL upsert the copy's row and SHALL record no event for that path, because the existing suppression treats the MCP event with the same hash as already saying it

#### Scenario: A tool event at an excluded path

- **WHEN** the setting is true and `edit_note` edits an excluded conflict copy
- **THEN** the tool's own `modified` event SHALL be recorded as today, and no pass SHALL record any event for that path while the setting stays true
