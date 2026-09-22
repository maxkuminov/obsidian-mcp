# usage-quotas Specification

## Purpose
TBD - created by archiving change panel-usage-slicing-quotas. Update Purpose after archive.
## Requirements
### Requirement: Per-key daily quota enforcement
A key with a non-null `daily_request_limit` SHALL have tool calls admitted through an atomic per-(key, UTC-day) counter — a single conditional-increment statement that admits at most `limit` calls per UTC day under any concurrency — with over-limit calls refused before the tool body via a structured error naming the limit and the UTC reset. The admission increment SHALL remain the **last** pre-body gate, running after every other one — the per-principal token buckets, credential and vault resolution, and the argument screens — so that no call which is going to be refused for any other reason consumes a slot, and SHALL be committed in its own transaction before the tool body runs, so a body that later fails still consumes its slot. The refusal SHALL be logged with the over-quota marker `"over_quota": true` in params (one shared constant used by every writer and reader; NULL-safe exclusion predicate `COALESCE((params->>'over_quota')::boolean, false)`) and SHALL NOT increment the counter. Enforcement SHALL never affect other keys; keys with a null limit SHALL perform no quota accounting, issue no quota statement, and be unaffected by this requirement exactly as today — while remaining subject to the rate controls, which are not scoped to keys carrying a limit. **The OAuth exemption is scoped to this daily quota alone** — OAuth-authenticated traffic SHALL NOT consume or be refused by `daily_request_limit`, and SHALL be subject in full to the per-principal token buckets, which key on the OAuth grant. It follows, and SHALL be stated rather than implied, that **OAuth principals and API keys carrying no limit have velocity bounds only**: no durable ceiling bounds the total work they may do in a day. That is an accepted limitation of this change, deliberately not closed here — a quota for OAuth was rejected in #162 because panel OAuth is the operator and an operator locked out by their own ceiling cannot raise it, and backfilling a limit onto existing keys is precisely the regression grandfathering exists to prevent. The operator remedy is to set a limit on the keys that warrant one. The refusal's retry interval SHALL be derived from the clock reading the admission already performed, carried on the admission result, and SHALL NOT be recomputed from a later reading. Enabling a limit on an unlimited key (NULL to value) SHALL delete the key's current-day counter row in the same transaction, so consumption restarts at zero at each enablement; changing one non-null limit to another SHALL keep the counter.

#### Scenario: Limit reached
- **WHEN** a key with limit 100 makes its 101st call of the UTC day
- **THEN** the call is refused before the tool body runs, the refusal is logged with the over-quota marker, and a different key's calls proceed

#### Scenario: Pre-body refusals consume nothing; body failures consume
- **WHEN** a limited key sends a call refused by an existing pre-body gate, then an admitted call whose tool body raises
- **THEN** the first consumes no slot and the second consumes exactly one

#### Scenario: A rate-limited call consumes no quota
- **WHEN** a limited key is refused by either per-principal token bucket
- **THEN** its daily counter is unchanged, because the buckets run before the quota admission

#### Scenario: An over-long argument consumes no quota
- **WHEN** a limited key sends a query longer than `MAX_SEARCH_QUERY_CHARS`
- **THEN** its daily counter is unchanged, because the argument screens run before the quota admission

#### Scenario: Concurrent boundary
- **WHEN** a key with limit N receives more than N concurrent calls in one UTC day
- **THEN** exactly N tool bodies execute and every excess call is refused, proven by a concurrency test

#### Scenario: Day rollover
- **WHEN** the same key calls after the next UTC midnight
- **THEN** the call executes normally

#### Scenario: Refusals do not consume quota
- **WHEN** an over-limit key is refused 50 times
- **THEN** its counter remains at the limit, and at rollover exactly the limit-many new calls are admitted

#### Scenario: A NULL-limit key still issues no quota statement
- **WHEN** a key with no limit makes tool calls
- **THEN** no quota statement is issued and no counter row is created for it, exactly as before this change, even though its calls pass the rate controls

#### Scenario: OAuth is exempt from the quota but not from the rate controls
- **WHEN** an OAuth-authenticated caller issues tool calls at a rate above `MCP_RATE_LIMIT_PER_MINUTE`
- **THEN** no `quota_counters` row is written for it and the excess calls are nonetheless refused by the per-principal bucket

#### Scenario: The velocity-only residual is explicit
- **WHEN** an OAuth grant, or an API key carrying no limit, calls write tools all day within its rate buckets
- **THEN** no durable daily ceiling stops it, this is the documented accepted limitation rather than an oversight, and the panel offers the operator the remedy of setting a limit on that key

#### Scenario: A refusal at the UTC boundary quotes a short interval
- **WHEN** the admission decides against a day whose reset is milliseconds away and the refusal is rendered after that midnight has passed
- **THEN** the quoted retry interval is the small interval measured from the decision instant, not an interval approaching two days

### Requirement: Quota limit domain
`daily_request_limit` SHALL be a nullable integer where NULL means unlimited and non-null values are constrained to 1..1000000 by both server-side validation and a database CHECK constraint; zero and negative values SHALL be rejected.

#### Scenario: Invalid value rejected
- **WHEN** an admin submits a limit of 0, -5, or 10000001
- **THEN** the submission is rejected with a visible error and the stored value is unchanged

#### Scenario: Enable resets, change keeps
- **WHEN** an admin clears a key's limit mid-day after 40 admissions and re-enables a limit of 100 an hour later, then later raises it to 200
- **THEN** consumption shows 0/100 at re-enablement (counter reset with the enable), and the later change to 200 keeps the accumulated count

#### Scenario: Unlimited default
- **WHEN** a key has no limit set
- **THEN** no quota query alters its behavior

### Requirement: Concurrency refusal consumes no daily quota
The tool slot gate SHALL run before the durable quota gate, and every denied or
cancelled admission SHALL leave daily quota counters unchanged.

#### Scenario: A tool has no capacity
- **WHEN** enforcement refuses a tool for slot timeout or waiter overflow
- **THEN** its body SHALL not run and it SHALL consume no daily quota

#### Scenario: Quota refuses after slots were acquired
- **WHEN** a tool obtains slots and the daily quota gate refuses
- **THEN** its slot lease SHALL be released and its existing quota refusal SHALL retain precedence

### Requirement: New API keys receive a configurable default daily request limit

Key creation SHALL apply `DEFAULT_DAILY_REQUEST_LIMIT` (default 5,000) as the `daily_request_limit` of a newly created key when the creator did not choose a value, and SHALL apply it in application code rather than as a database column default, so that keys created before this change keep whatever limit they carry — including NULL — with no migration and no backfill. On the JSON API an **omitted** `daily_request_limit` field SHALL mean "apply the default" while an **explicit null** SHALL continue to mean unlimited, distinguished by whether the field was set on the request rather than by the value's truthiness. On the control panel the default SHALL be materialised only as the create form's pre-filled value: a **blank submitted field SHALL mean unlimited**, and the create handler SHALL NOT substitute the default for a blank submission, so that what the operator saw is what they get and there is exactly one place the default can be overridden. Setting `DEFAULT_DAILY_REQUEST_LIMIT` to null SHALL restore the previous behaviour exactly. The configured default SHALL be subject to the same 1..1,000,000 domain as any other limit and SHALL be rejected at startup if outside it.

#### Scenario: Existing keys keep their current quota
- **WHEN** the change is deployed to a database whose active keys all carry `daily_request_limit = NULL`
- **THEN** every one of those keys SHALL still be unlimited, no counter row SHALL be created for them, and their quota accounting SHALL be byte-for-byte what it was before the deploy

#### Scenario: A new key gets the default from the pre-filled form
- **WHEN** an operator creates a key through the control panel without altering the pre-filled limit field
- **THEN** the created key SHALL carry `daily_request_limit = DEFAULT_DAILY_REQUEST_LIMIT` and the keys page SHALL show it

#### Scenario: A blank panel field means unlimited, with no substitution
- **WHEN** the operator clears the pre-filled limit field and submits
- **THEN** the created key SHALL be unlimited, and the create handler SHALL NOT have substituted the configured default

#### Scenario: Omitted and explicit null differ on the JSON API
- **WHEN** one create request omits `daily_request_limit` entirely and another sends `{"daily_request_limit": null}`
- **THEN** the first key SHALL carry the configured default and the second SHALL be unlimited

#### Scenario: An explicit value still wins
- **WHEN** a create request sends `{"daily_request_limit": 250}`
- **THEN** the created key SHALL carry 250 regardless of the configured default

#### Scenario: The default can be turned off
- **WHEN** `DEFAULT_DAILY_REQUEST_LIMIT` is null
- **THEN** a key created without a chosen limit SHALL be unlimited, as it is today

