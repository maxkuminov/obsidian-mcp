## MODIFIED Requirements

### Requirement: Per-tool latency aggregates
The panel SHALL provide a performance view showing, per tool over a user-selectable window of 24 hours, 7 days, or 30 days: executed-request count, p50/p95/p99 of `duration_ms`, and mean/max `response_size`, computed from `usage_logs` rows that executed the tool body — rows matching the shared pre-body-refusal predicate SHALL be excluded — one helper, used by both the aggregates and the refusal counts, that matches exactly the enumerated pre-body marker values (the quota `over_quota: true` marker, the unencodable-argument error marker, the `no_vault_assigned` marker, the `rate_limited` marker, the `slot_timeout` marker, and the `argument_too_long` marker, all as written by `_tracked`) and nothing else, so rows whose bodies executed and then failed are never excluded from latency and size aggregates and reported as a separate refusal count per tool. Because rate and slot refusals are coalesced at the write site, the per-tool refusal count SHALL sum `1 + suppressed` over the matching rows rather than counting rows, reading `suppressed` as an integer through a **guarded** cast so that a malformed value cannot take the page down for the whole window. Each newly enumerated value SHALL be classified pre-body deliberately, on the rule that a marker belongs to exactly one side of the body/no-body line, and the writer's and reader's copies of each marker string SHALL be pinned equal by a test.

#### Scenario: Percentiles over a window
- **WHEN** the operator selects the 7-day window
- **THEN** each tool with at least one logged call in that window shows count and p50/p95/p99 duration computed from exactly that window's rows

#### Scenario: Empty window
- **WHEN** a window contains no usage rows
- **THEN** the view renders an explicit empty state, not an error

#### Scenario: Refusal loop does not pollute latency
- **WHEN** a window holds 100 executed semantic_search calls and 5000 over-quota refusals for it
- **THEN** the tool's percentiles are computed from the 100 executed rows only and the 5000 appear as its refusal count

#### Scenario: Coalesced rate refusals are counted in full
- **WHEN** a window holds 40 executed `semantic_search` calls and coalesced `rate_limited` rows whose `1 + suppressed` values sum to 3000
- **THEN** the percentiles are computed from the 40 executed rows only and the refusal count for that tool is 3000, not the number of rows

#### Scenario: Slot timeouts and over-long arguments are excluded from latency
- **WHEN** a window holds rows carrying `error: "slot_timeout"` or `error: "argument_too_long"`
- **THEN** those rows are excluded from the latency and response-size aggregates and counted as refusals

#### Scenario: A malformed suppressed value does not break the page
- **WHEN** a row carries a `suppressed` value that is not an integer
- **THEN** the view renders the window without raising, because the cast is guarded

#### Scenario: Post-body errors still count as executed
- **WHEN** a window holds rows carrying `vault_assignment_changed`, `related_source_not_found`, or the provider-input-rejection marker written after an embedding round trip
- **THEN** those rows remain inside the latency and size aggregates, because their tool bodies ran — the provider rejection in particular SHALL NOT be enumerated as a pre-body refusal, or a real network round trip would be dropped out of the percentiles
