## MODIFIED Requirements

### Requirement: Per-tool latency aggregates

The panel SHALL provide a performance view showing, per tool over a user-selectable window of 24 hours, 7 days, or 30 days: executed-request count, p50/p95/p99 of `duration_ms`, and mean/max `response_size`, computed from `usage_logs` rows that executed the tool body. Rows matching the shared pre-body-refusal predicate SHALL be excluded from those aggregates and reported as a separate refusal count per tool. One helper, used by both the aggregates and refusal counts, SHALL match exactly the enumerated pre-body marker values: the quota `over_quota: true` marker, the unencodable-argument error marker, `no_vault_assigned`, every vault-quarantine marker written by the admission gate (the overlap, unexaminable-root, and not-ready markers), `rate_limited`, and `argument_too_long`, all as written by `_tracked`, and nothing else.

Every marker the admission gate can write before a tool body runs SHALL appear in that enumeration. The helper matches an explicit list rather than a pattern: a broad match would catch body-level failures too, while an omitted admission marker would pollute the latency aggregates and disappear from refusal counts. Every enumerated marker SHALL be bound by the predicate, and the writer's and reader's copies of each marker string SHALL be pinned equal by a test.

The `permission_denied` and `tool_exception` markers are classified **post-body** and SHALL NOT be added to the predicate: the first is written inside a body that has already passed every pre-body gate and consumed its quota slot, and the second is written by a body that ran. Other body-level errors, including `vault_assignment_changed`, `related_source_not_found`, and the provider-input-rejection marker written after an embedding round trip, SHALL likewise remain inside latency and response-size aggregates. Each marker belongs to exactly one side of the body/no-body distinction.

Because rate refusals are coalesced at the write site, the per-tool refusal count SHALL sum `1 + suppressed` over matching rows rather than counting rows. It SHALL read `suppressed` as an integer through a **guarded** cast so that a malformed value cannot take the page down for the whole window.

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

#### Scenario: Quarantine refusals do not pollute latency
- **WHEN** a window holds executed calls for a tool alongside calls refused for a vault-root overlap, for an unexaminable root, and for an unpublished snapshot
- **THEN** the percentiles are computed from the executed rows only
- **AND** each refused row is counted as a pre-body refusal for that tool

#### Scenario: The enumeration and the bind set agree
- **WHEN** the pre-body marker enumeration is compared against the bind parameters the predicate emits
- **THEN** every enumerated marker SHALL be bound, so a marker added to one and not the other is a detectable inconsistency rather than a silent omission

#### Scenario: Coalesced rate refusals are counted in full
- **WHEN** a window holds 40 executed `semantic_search` calls and coalesced `rate_limited` rows whose `1 + suppressed` values sum to 3000
- **THEN** the percentiles are computed from the 40 executed rows only and the refusal count for that tool is 3000, not the number of rows

#### Scenario: Over-long-argument refusals are excluded from latency
- **WHEN** a window holds rows carrying `error: "argument_too_long"`, which are written one per refusal rather than coalesced
- **THEN** those rows are excluded from the latency and response-size aggregates and counted as refusals

#### Scenario: A malformed suppressed value does not break the page
- **WHEN** a row carries a `suppressed` value that is not an integer
- **THEN** the view renders the window without raising, because the cast is guarded

#### Scenario: Post-body errors still count as executed
- **WHEN** a window holds rows carrying `vault_assignment_changed`, `related_source_not_found`, or the provider-input-rejection marker written after an embedding round trip
- **THEN** those rows remain inside the latency and size aggregates, because their tool bodies ran — the provider rejection in particular SHALL NOT be enumerated as a pre-body refusal, or a real network round trip would be dropped out of the percentiles
