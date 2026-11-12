## ADDED Requirements

### Requirement: A row removed because its path is an excluded conflict copy SHALL be recorded as `deleted` with `reason = 'not_indexable'`, and a row-less excluded path SHALL record nothing

When the index pass deletes a row because its path is excluded under `INDEX_EXCLUDE_CONFLICT_FILES`, it SHALL record one `deleted` event at that path with `reason = 'not_indexable'`, `source = 'index'`, rendered `by human/sync`. It SHALL NOT use `reason = 'removed'`, which states that the file is gone. A discovered excluded path with no row SHALL record no event of any kind. No new `reason` value SHALL be introduced, and the `get_changes` description SHALL NOT change.

When the setting is turned off, the first pass that upserts a previously excluded path SHALL record it exactly as any newly discovered note: `created` with its canonical `content_hash`, or `moved` when the existing same-hash pairing applies.

These events SHALL be subject to the existing suppression, ordering and per-pass overflow rules unchanged.

#### Scenario: Turning the setting on

- **WHEN** a scope holds a row at `A (conflict 2026-05-29-19-45-39).md` and the server restarts with the setting true
- **THEN** the first pass SHALL record exactly one event for that path, `deleted` with `reason=not_indexable` and `by human/sync`, and the following pass SHALL record none

#### Scenario: A new conflict copy while the setting is on

- **WHEN** the setting is true and a sync tool writes `F (conflict 2026-06-01-10-00-00).md`, which never had a row
- **THEN** no pass SHALL record any event for that path

#### Scenario: Turning the setting off

- **WHEN** the server restarts with the setting false over a vault holding `A (conflict 2026-05-29-19-45-39).md` with no row
- **THEN** the next pass SHALL record `created` for that path with its content hash and `by human/sync`

#### Scenario: A tool event at an excluded path

- **WHEN** the setting is true and `edit_note` edits an excluded conflict copy
- **THEN** the tool's own `modified` event SHALL be recorded as today, and no pass SHALL record any event for that path
