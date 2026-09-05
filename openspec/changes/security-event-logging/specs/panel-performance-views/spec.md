## MODIFIED Requirements

### Requirement: Per-tool latency aggregates

The panel SHALL provide a performance view showing, per tool over a user-selectable window of 24 hours, 7 days, or 30 days: executed-request count, p50/p95/p99 of `duration_ms`, and mean/max `response_size`, computed from `usage_logs` rows that executed the tool body — rows matching the shared pre-body-refusal predicate SHALL be excluded — one helper, used by both the aggregates and the refusal counts, that matches exactly the enumerated pre-body marker values (the quota `over_quota: true` marker, the unencodable-argument error marker, and the `no_vault_assigned` marker, all as written by `_tracked`) and nothing else, so rows whose bodies executed and then failed are never excluded from latency and size aggregates and reported as a separate refusal count per tool. The `permission_denied` and `tool_exception` markers are classified **post-body** and SHALL NOT be added to that predicate: the first is written inside a body that has already passed every pre-body gate and consumed its quota slot, and the second is written by a body that ran.

#### Scenario: Percentiles over a window
- **WHEN** the operator selects the 7-day window
- **THEN** each tool with at least one logged call in that window shows count and p50/p95/p99 duration computed from exactly that window's rows

#### Scenario: Empty window
- **WHEN** a window contains no usage rows
- **THEN** the view renders an explicit empty state, not an error

#### Scenario: Refusal loop does not pollute latency
- **WHEN** a window holds 100 executed semantic_search calls and 5000 over-quota refusals for it
- **THEN** the tool's percentiles are computed from the 100 executed rows only and the 5000 appear as its refusal count

#### Scenario: The new markers stay out of the predicate
- **WHEN** a window holds rows carrying `permission_denied` and rows carrying `tool_exception`
- **THEN** both SHALL be counted as executed rows and included in the latency aggregates, and neither SHALL be counted as a pre-body refusal
