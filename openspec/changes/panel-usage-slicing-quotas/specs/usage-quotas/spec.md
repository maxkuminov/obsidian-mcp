## ADDED Requirements

### Requirement: Per-key daily quota enforcement
A key with a non-null `daily_request_limit` SHALL have tool calls admitted through an atomic per-(key, UTC-day) counter — a single conditional-increment statement that admits at most `limit` calls per UTC day under any concurrency — with over-limit calls refused before the tool body via a structured error naming the limit and the UTC reset. The refusal SHALL be logged with the over-quota marker `"over_quota": true` in params (one shared constant used by every writer and reader; NULL-safe exclusion predicate `COALESCE((params->>'over_quota')::boolean, false)`) and SHALL NOT increment the counter. Enforcement SHALL never affect other keys or OAuth traffic; keys with a null limit SHALL behave exactly as today.

#### Scenario: Limit reached
- **WHEN** a key with limit 100 makes its 101st call of the UTC day
- **THEN** the call is refused before the tool body runs, the refusal is logged with the over-quota marker, and a different key's calls proceed

#### Scenario: Concurrent boundary
- **WHEN** a key with limit N receives more than N concurrent calls in one UTC day
- **THEN** exactly N tool bodies execute and every excess call is refused, proven by a concurrency test

#### Scenario: Day rollover
- **WHEN** the same key calls after the next UTC midnight
- **THEN** the call executes normally

#### Scenario: Refusals do not consume quota
- **WHEN** an over-limit key is refused 50 times
- **THEN** its counter remains at the limit, and at rollover exactly the limit-many new calls are admitted

### Requirement: Quota limit domain
`daily_request_limit` SHALL be a nullable integer where NULL means unlimited and non-null values are constrained to 1..1000000 by both server-side validation and a database CHECK constraint; zero and negative values SHALL be rejected.

#### Scenario: Invalid value rejected
- **WHEN** an admin submits a limit of 0, -5, or 10000001
- **THEN** the submission is rejected with a visible error and the stored value is unchanged

#### Scenario: Unlimited default
- **WHEN** a key has no limit set
- **THEN** no quota query alters its behavior
