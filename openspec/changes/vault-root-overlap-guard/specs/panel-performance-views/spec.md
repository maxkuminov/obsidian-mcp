## MODIFIED Requirements

### Requirement: Per-tool latency aggregates
The panel SHALL provide a performance view showing, per tool over a user-selectable window of 24 hours, 7 days, or 30 days: executed-request count, p50/p95/p99 of `duration_ms`, and mean/max `response_size`, computed from `usage_logs` rows that executed the tool body — rows matching the shared pre-body-refusal predicate SHALL be excluded — one helper, used by both the aggregates and the refusal counts, that matches exactly the enumerated pre-body marker values (the quota `over_quota: true` marker, the unencodable-argument error marker, the `no_vault_assigned` marker, and **every vault-quarantine marker written by the admission gate — the overlap marker, the unexaminable-root marker, and the not-ready marker**, all as written by `_tracked`) and nothing else, so rows whose bodies executed and then failed are never excluded from latency and size aggregates and reported as a separate refusal count per tool.

Every marker the admission gate can write before a tool body runs SHALL appear in that enumeration. The helper matches an explicit list rather than a pattern, deliberately — a broad match would catch body-level failures too — and the cost of that choice is that a marker added to the gate and not to the list is silently wrong in both directions at once: the refusal's `duration_ms` is folded into the tool's latency percentiles as though the body had executed, and the refusal itself is never counted. A gate that starts refusing a whole tenant is exactly the traffic an operator would go to this page to understand.

#### Scenario: Percentiles over a window
- **WHEN** the operator selects the 7-day window
- **THEN** each tool with at least one logged call in that window shows count and p50/p95/p99 duration computed from exactly that window's rows

#### Scenario: Empty window
- **WHEN** a window contains no usage rows
- **THEN** the view renders an explicit empty state, not an error

#### Scenario: Refusal loop does not pollute latency
- **WHEN** a window holds 100 executed semantic_search calls and 5000 over-quota refusals for it
- **THEN** the tool's percentiles are computed from the 100 executed rows only and the 5000 appear as its refusal count

#### Scenario: Quarantine refusals do not pollute latency
- **WHEN** a window holds executed calls for a tool alongside calls refused for a vault-root overlap, for an unexaminable root, and for an unpublished snapshot
- **THEN** the percentiles are computed from the executed rows only
- **AND** each refused row is counted as a pre-body refusal for that tool

#### Scenario: The enumeration and the bind set agree
- **WHEN** the pre-body marker enumeration is compared against the bind parameters the predicate emits
- **THEN** every enumerated marker SHALL be bound, so a marker added to one and not the other is a detectable inconsistency rather than a silent omission
