## MODIFIED Requirements

### Requirement: Per-key daily quota enforcement
A key with a non-null `daily_request_limit` SHALL have tool calls admitted through an atomic per-(key, UTC-day) counter — a single conditional-increment statement that admits at most `limit` calls per UTC day under any concurrency — with over-limit calls refused before the tool body via a structured error naming the limit and the UTC reset. The admission increment SHALL run AFTER every existing pre-body gate (the per-principal rate bucket, credential/vault resolution, argument screening) so pre-body refusals never consume a slot, and SHALL be committed in its own transaction before the tool body runs, so a body that later fails still consumes its slot. The refusal SHALL be logged with the over-quota marker `"over_quota": true` in params (one shared constant used by every writer and reader; NULL-safe exclusion predicate `COALESCE((params->>'over_quota')::boolean, false)`) and SHALL NOT increment the counter. Enforcement SHALL never affect other keys; keys with a null limit SHALL behave exactly as today. **The OAuth exemption is scoped to this daily quota only** — OAuth-authenticated traffic SHALL NOT consume or be refused by `daily_request_limit`, and SHALL be subject in full to the per-principal rate bucket and the concurrency slots, which key on the OAuth grant. Enabling a limit on an unlimited key (NULL to value) SHALL delete the key's current-day counter row in the same transaction, so consumption restarts at zero at each enablement; changing one non-null limit to another SHALL keep the counter.

#### Scenario: Limit reached
- **WHEN** a key with limit 100 makes its 101st call of the UTC day
- **THEN** the call is refused before the tool body runs, the refusal is logged with the over-quota marker, and a different key's calls proceed

#### Scenario: Pre-body refusals consume nothing; body failures consume
- **WHEN** a limited key sends a call refused by an existing pre-body gate, then an admitted call whose tool body raises
- **THEN** the first consumes no slot and the second consumes exactly one

#### Scenario: A rate-limited call consumes no quota
- **WHEN** a limited key is refused by the per-principal rate bucket
- **THEN** its daily counter is unchanged, because the rate bucket runs before the quota admission

#### Scenario: Concurrent boundary
- **WHEN** a key with limit N receives more than N concurrent calls in one UTC day
- **THEN** exactly N tool bodies execute and every excess call is refused, proven by a concurrency test

#### Scenario: Day rollover
- **WHEN** the same key calls after the next UTC midnight
- **THEN** the call executes normally

#### Scenario: Refusals do not consume quota
- **WHEN** an over-limit key is refused 50 times
- **THEN** its counter remains at the limit, and at rollover exactly the limit-many new calls are admitted

#### Scenario: OAuth is exempt from the quota but not from the rate controls
- **WHEN** an OAuth-authenticated caller issues tool calls at a rate above `MCP_RATE_LIMIT_PER_MINUTE`
- **THEN** no `quota_counters` row is written for it and the excess calls are nonetheless refused by the per-principal rate bucket

## ADDED Requirements

### Requirement: New API keys receive a configurable default daily request limit

Key creation SHALL apply `DEFAULT_DAILY_REQUEST_LIMIT` (default 5,000) as the `daily_request_limit` of a newly created key when the creator did not choose a value, on both the JSON API and the control panel paths. The default SHALL be applied in application code and SHALL NOT be expressed as a database column default, so that keys created before this change keep whatever limit they carry — including NULL — with no migration and no backfill. On the JSON API an **omitted** `daily_request_limit` field SHALL mean "apply the default" while an **explicit null** SHALL continue to mean unlimited, and the two SHALL be distinguished by whether the field was set on the request rather than by the value's truthiness. Setting `DEFAULT_DAILY_REQUEST_LIMIT` to null SHALL restore the previous behaviour exactly, with new keys created unlimited. The configured default SHALL be subject to the same 1..1,000,000 domain as any other limit.

#### Scenario: Existing keys keep their current quota
- **WHEN** the change is deployed to a database whose active keys all carry `daily_request_limit = NULL`
- **THEN** every one of those keys SHALL still be unlimited, no counter row SHALL be created for them, and their behaviour SHALL be byte-for-byte what it was before the deploy

#### Scenario: A new key gets the default
- **WHEN** an operator creates a key through the control panel without touching the limit field
- **THEN** the created key SHALL carry `daily_request_limit = DEFAULT_DAILY_REQUEST_LIMIT` and the keys page SHALL show it

#### Scenario: Omitted and explicit null differ on the JSON API
- **WHEN** one create request omits `daily_request_limit` entirely and another sends `{"daily_request_limit": null}`
- **THEN** the first key SHALL carry the configured default and the second SHALL be unlimited

#### Scenario: An explicit value still wins
- **WHEN** a create request sends `{"daily_request_limit": 250}`
- **THEN** the created key SHALL carry 250 regardless of the configured default

#### Scenario: The default can be turned off
- **WHEN** `DEFAULT_DAILY_REQUEST_LIMIT` is null
- **THEN** a key created without a chosen limit SHALL be unlimited, as it is today
