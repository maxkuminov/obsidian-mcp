## ADDED Requirements

### Requirement: Per-tool latency aggregates
The panel SHALL provide a performance view showing, per tool over a user-selectable window of 24 hours, 7 days, or 30 days: executed-request count, p50/p95/p99 of `duration_ms`, and mean/max `response_size`, computed from `usage_logs` rows that executed the tool body — rows matching the shared pre-body-refusal predicate SHALL be excluded — one helper, used by both the aggregates and the refusal counts, that matches exactly the enumerated pre-body marker values (the quota `over_quota: true` marker, the unencodable-argument error marker, and the `no_vault_assigned` marker, all as written by `_tracked`) and nothing else, so rows whose bodies executed and then failed are never excluded from latency and size aggregates and reported as a separate refusal count per tool.

#### Scenario: Percentiles over a window
- **WHEN** the operator selects the 7-day window
- **THEN** each tool with at least one logged call in that window shows count and p50/p95/p99 duration computed from exactly that window's rows

#### Scenario: Empty window
- **WHEN** a window contains no usage rows
- **THEN** the view renders an explicit empty state, not an error

#### Scenario: Refusal loop does not pollute latency
- **WHEN** a window holds 100 executed semantic_search calls and 5000 over-quota refusals for it
- **THEN** the tool's percentiles are computed from the 100 executed rows only and the 5000 appear as its refusal count

### Requirement: Search phase breakdown
The performance view SHALL show, for calls whose logged params carry phase timings (`embed_ms`, `db_ms`), the mean and p95 of each phase over the selected window.

#### Scenario: Phase stats from params
- **WHEN** semantic_search calls with embed_ms/db_ms in params exist in the window
- **THEN** the view shows mean and p95 embed_ms and db_ms for them, and rows lacking the keys are excluded rather than counted as zero

### Requirement: Slowest requests table
The performance view SHALL list the N slowest requests of the selected window (N ≤ 50) with time, tool, actor, duration, and response size.

#### Scenario: Slowest listing
- **WHEN** the operator opens the slowest-requests table for a window
- **THEN** rows are ordered by duration descending, capped at 50, and each shows the actor attribution already recorded in usage_logs

### Requirement: Indexer pass record
Every index/embed pass SHALL persist one `indexer_runs` row — start, finish, trigger (startup, scheduled, manual, or backfill), the pass's `user_id` when the pass is per-user (nullable, ON DELETE SET NULL), notes scanned/indexed/embedded, and error text when the pass failed — written even when the pass raises, and the table SHALL be pruned to the newest 500 rows. Run displays SHALL label per-user runs by owner.

#### Scenario: Failed pass still recorded
- **WHEN** a pass raises after starting
- **THEN** its row exists with finished_at set and the error recorded

#### Scenario: Pruning
- **WHEN** more than 500 runs exist after an insert
- **THEN** only the newest 500 remain

#### Scenario: Startup pass recorded
- **WHEN** the process starts and runs its initial pass
- **THEN** a run row with trigger startup exists

#### Scenario: Two-user history
- **WHEN** passes ran for two different users
- **THEN** their run rows carry the respective user_id and displays distinguish them
