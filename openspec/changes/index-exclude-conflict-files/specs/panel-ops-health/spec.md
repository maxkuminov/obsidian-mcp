## ADDED Requirements

### Requirement: The dashboard SHALL show the viewer's excluded conflict-copy count while `INDEX_EXCLUDE_CONFLICT_FILES` is on, through a template-only change

While `INDEX_EXCLUDE_CONFLICT_FILES` is true and the indexer holds an excluded count for the viewer's scope, `dashboard.html` SHALL render one line beside the last-run line: `<N> sync-conflict cop{y|ies} not indexed (INDEX_EXCLUDE_CONFLICT_FILES)`. The count SHALL come from the indexer's in-process per-scope count for the viewer's own scope (`_scope_user_id`), passed by the dashboard route as `excluded_conflicts`. When the setting is false, or no committed pass has counted the scope since the process started, the line SHALL NOT render.

The template change SHALL add no `<script>`, no `style` attribute, no inline event handler and no new CSS class, so the page SHALL load with zero Content Security Policy violations. It SHALL be verified by the owner's manual browser pass.

#### Scenario: Setting on, copies excluded

- **WHEN** the setting is true and the viewer's scope's last committed pass excluded 2 conflict copies
- **THEN** the dashboard SHALL show `2 sync-conflict copies not indexed (INDEX_EXCLUDE_CONFLICT_FILES)`

#### Scenario: Setting off

- **WHEN** the setting is false
- **THEN** the dashboard's rendered HTML SHALL contain no excluded-count line, and SHALL be otherwise identical to its render without this change for the same data

#### Scenario: Another tenant's count is not shown

- **WHEN** in multi-user mode tenant X's pass excluded 5 copies and tenant Y's excluded 0
- **THEN** Y's dashboard SHALL show `0 sync-conflict copies not indexed (INDEX_EXCLUDE_CONFLICT_FILES)` and SHALL NOT show 5
