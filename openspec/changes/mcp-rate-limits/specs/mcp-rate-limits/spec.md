## ADDED Requirements

### Requirement: Failed authentication on /mcp is budgeted per client address

`APIKeyMiddleware` SHALL count authentication failures per client address over a window of `MCP_AUTH_FAILURE_WINDOW_SECONDS` and SHALL refuse a request from an address whose recorded count for the current window is already greater than or equal to `MCP_AUTH_FAILURE_LIMIT`, with HTTP 429, a `Retry-After` header, and the existing `WWW-Authenticate` header. The check SHALL run before the credential lookup, so a refused request costs no database session and no query — bounding the database work an unauthenticated caller can force, which is what this control exists for; it is **not** a defence against credential guessing. A refused request SHALL NOT increment the count. **Every** 401 branch SHALL increment — missing bearer token, unknown credential, ownerless credential, inactive user, expired credential, cross-user grant, and missing vault scope — so that no branch is a cheaper probe than another. The address SHALL be the one established by the application's restricted proxy-header handling and SHALL NEVER be read directly from a request header; a request carrying no client address SHALL be charged to a single reserved slot shared by all such requests rather than exempted. `Retry-After` SHALL be the whole number of seconds remaining in the current window, minimum 1. Setting `MCP_AUTH_FAILURE_LIMIT` to null SHALL disable the control. The first refusal for a slot in a window SHALL emit one WARNING naming the address.

#### Scenario: The threshold is exact

- **WHEN** an address records exactly `MCP_AUTH_FAILURE_LIMIT` failures within the window and sends one more request
- **THEN** that next request SHALL be refused with 429, and the failure count SHALL NOT increase further while refusals continue

#### Scenario: Every 401 branch is budgeted

- **WHEN** an address produces failures spread across each 401 branch in turn
- **THEN** each SHALL increment the same counter, proven by a test parameterised over every branch

#### Scenario: A refused probe touches no database

- **WHEN** an over-budget address sends a further request
- **THEN** no database session SHALL be opened and no credential query SHALL be issued

#### Scenario: A valid credential under budget is unaffected

- **WHEN** an address below its budget presents a valid credential
- **THEN** the request SHALL be authenticated and served as before, and the counter SHALL NOT increment

#### Scenario: A request with no client address is charged, not exempted

- **WHEN** a request arrives with no resolvable client address
- **THEN** it SHALL be charged to the shared reserved slot and SHALL be subject to the same threshold

#### Scenario: The window rolls

- **WHEN** an over-budget address stops for longer than `MCP_AUTH_FAILURE_WINDOW_SECONDS` and then presents a valid credential
- **THEN** the request SHALL be served

#### Scenario: The control can be turned off

- **WHEN** `MCP_AUTH_FAILURE_LIMIT` is null
- **THEN** no failure counting SHALL occur and every request SHALL reach the credential lookup as it does today

### Requirement: Every authenticated request binds a principal identity

`APIKeyMiddleware` SHALL bind a request-scoped principal identity for every authenticated `/mcp` request, derived from the credential row it has already loaded and therefore issuing no additional database statement. The identity SHALL be `("api_key", <api_keys.id>)` for an API-key request and `("oauth", <oauth_tokens.grant_id>)` for an OAuth request. The OAuth key SHALL be the **grant**, never the access-token id and never a `(client_id, user_id)` pair: a grant is the unit `/authorize` created and the unit revocation acts on, every rotation inherits it, and two grants for the same client and user are independently revocable and SHALL therefore hold independent allowances. The binding SHALL be reset in the same `finally` block as the other request-scoped auth context variables. A caller for which no principal is bound — sandbox mode, or a direct in-process caller that never passed the middleware — SHALL be exempt from every control keyed on the principal rather than refused or crashed; the failed-authentication budget is not keyed on the principal and SHALL apply regardless.

#### Scenario: API-key and OAuth requests both carry a principal

- **WHEN** a tool call is authenticated by an API key, and another by an OAuth access token
- **THEN** each SHALL have a principal bound, the two SHALL be distinct, and neither binding SHALL cause a database statement beyond the credential lookup the middleware already performs

#### Scenario: Refreshing an access token keeps the principal

- **WHEN** an OAuth client refreshes its access token within one grant and calls a tool with the new token
- **THEN** the principal SHALL be identical to the one the previous access token carried, so any per-principal allowance continues from its existing state

#### Scenario: Two grants for the same client and user are distinct principals

- **WHEN** one user completes `/authorize` twice for the same client, producing two grants, and calls a tool with a token from each
- **THEN** the two calls SHALL carry different principals, so revoking one grant SHALL NOT alter the other's allowance

#### Scenario: No principal is exempt, not refused

- **WHEN** a tool implementation is invoked in-process without passing `APIKeyMiddleware`, or the server runs with `MCP_SANDBOX_MODE=true`
- **THEN** the call SHALL proceed without consulting any per-principal control and SHALL NOT raise

### Requirement: Two per-principal token buckets admit every tool call

Every `_tracked` tool call SHALL pass a per-principal general token bucket before any other gate in the decorator, refilling at `MCP_RATE_LIMIT_PER_MINUTE` tokens per minute with capacity `MCP_RATE_LIMIT_BURST`. Every call to a vault-mutating tool — `create_note`, `edit_note`, `move_note`, `delete_note`, `set_frontmatter`, `write_file`, `delete_file`, `import_from_url` — SHALL additionally pass a second per-principal bucket refilling at `MCP_WRITE_RATE_LIMIT_PER_MINUTE` with capacity `MCP_WRITE_RATE_LIMIT_BURST`, checked immediately after the general bucket. Each bucket SHALL be updated synchronously with no `await` between reading and writing its state, SHALL issue no database statement, and SHALL hold no lock. A token SHALL be consumed by every call that reaches a bucket, including calls a later gate then refuses — a token refills and a durable quota slot does not. Both buckets SHALL be **velocity** bounds and SHALL NOT be claimed to bound the total work a credential can do in a day, which is bounded only by the daily quota and only for the credentials that quota reaches. Either bucket's rate and burst SHALL be set together or be null together; null SHALL disable that bucket and zero SHALL be rejected as a configuration value.

**The write bucket SHALL also be consumed where vault bytes are written outside a tool call.** `PUT /transfer/upload` publishes into the vault by redeeming a capability and never passes through the tool decorator, so bounding only the eight tools would leave the write rate escapable by minting capabilities and redeeming them. Redemption SHALL therefore consume the write bucket of the principal **that minted the token** — `("api_key", key_id)` when the transfer row names a key, and `("oauth", grant_id)` taken from the minting token row that identity resolution already loads, so no additional query is required and no schema change is needed. `request_upload`, `request_download` and `check_upload` only mint or read capability rows and SHALL NOT consume the write bucket; `import_from_url` SHALL continue to consume it at the tool call, as an ordinary write tool, and SHALL NOT be charged again.

#### Scenario: A capability redemption consumes the minting principal's write bucket

- **WHEN** a principal mints upload capabilities and redeems them through `PUT /transfer/upload` faster than `MCP_WRITE_RATE_LIMIT_PER_MINUTE`
- **THEN** the excess redemptions SHALL be refused, and the write rate SHALL NOT be escapable by moving writes from the tools onto the transfer route

#### Scenario: Minting is not double-charged

- **WHEN** a principal calls `request_upload` and later redeems the capability
- **THEN** the write bucket SHALL be consumed once, at redemption, and `import_from_url` SHALL continue to consume it once, at its tool call

#### Scenario: A burst from one principal does not delay another principal's call

- **WHEN** principal A exhausts its burst capacity and continues calling, while principal B issues a single ordinary tool call
- **THEN** B's call SHALL be admitted and executed without waiting on A, and A's excess calls SHALL be refused rather than queued

#### Scenario: Write tools pass both buckets

- **WHEN** a principal calls `delete_note` at a rate under `MCP_RATE_LIMIT_PER_MINUTE` but above `MCP_WRITE_RATE_LIMIT_PER_MINUTE`
- **THEN** the excess calls SHALL be refused with a scope naming the write bucket, while that principal's read tools SHALL continue to be admitted

#### Scenario: Sustained rate is admitted

- **WHEN** a principal calls at or below the configured sustained rate, having started with a full bucket
- **THEN** every call SHALL be admitted

#### Scenario: The admitted path performs no additional I/O

- **WHEN** a call is admitted by both buckets
- **THEN** the gates SHALL have issued no SQL statement and no session checkout, proven by a statement-counting test

### Requirement: The rate gates run above every other pre-body gate and consume nothing durable

The two token buckets SHALL be the first gates in the shared tool decorator, running before vault resolution, before the argument screens and before daily-quota admission. The daily quota SHALL remain the **last** pre-body gate, so that a call refused for its rate, for having no resolvable vault root, for an unencodable argument, or for an over-long argument consumes no daily quota slot. No gate ordering SHALL be introduced that allows a durable counter to be consumed by a call whose body does not run.

#### Scenario: A rate-limited call consumes no quota

- **WHEN** a key carrying a `daily_request_limit` is refused by either token bucket
- **THEN** its `quota_counters` row SHALL be unchanged

#### Scenario: The quota remains the last pre-body gate

- **WHEN** a limited key sends a call that any earlier gate refuses
- **THEN** no quota statement SHALL be issued for it

### Requirement: Every refusal raised inside the tool decorator carries one caller-visible structured shape

Every refusal produced by a gate inside the shared tool decorator SHALL be returned to the caller as an ordinary tool result — never a protocol error, never an empty result set — and SHALL end with a single machine-readable final line introduced by a fixed sentinel token and carrying a one-line JSON object with the fields `code`, `scope`, `limit`, `limit_unit` and `retry_after_seconds`. `code` SHALL come from a closed set that includes `rate_limited`, `argument_too_long`, `over_quota`, `no_vault_assigned` and `argument_not_encodable`. `retry_after_seconds` SHALL be a JSON number of at least 1 wherever retrying can succeed — the whole seconds until a token is available for a bucket refusal, the interval to the next UTC reset for a quota refusal — and SHALL be absent where retrying cannot help, so that no refusal invites a loop that cannot end. A tool returning a plain string SHALL receive the human prose followed by that line; a tool declaring a structured output type SHALL receive the identical complete text in its declared error field through the existing refusal mapping, so both kinds of tool expose the same fields and no output-schema validation can fail. The three pre-existing pre-body refusals SHALL adopt the line additively, leaving their existing prose unchanged.

This contract SHALL be scoped to refusals raised inside the decorator, where a tool call exists to answer. The failed-authentication budget's HTTP 429 is a **transport-level** refusal of a request that never authenticated — there is no tool call, no principal and no usage row — and SHALL carry `Retry-After` and `WWW-Authenticate` headers instead. That exception SHALL be documented as an accepted limitation rather than worked around by fabricating a tool result for an unauthenticated request.

#### Scenario: A string-returning tool exposes the fields

- **WHEN** `keyword_search` is refused by the general bucket
- **THEN** the returned string SHALL contain the sentinel line, and parsing the JSON on it SHALL yield the rate-limited code and a numeric retry-after of at least 1

#### Scenario: A structured tool refuses in its own shape with the same fields

- **WHEN** `read_note` is refused by the same gate
- **THEN** the caller SHALL receive the tool's declared structured result whose error field carries the identical text including the sentinel line, and SHALL NOT receive a bare string or an output-schema validation failure

#### Scenario: A futile retry is not invited

- **WHEN** a call is refused for having no vault assignment or for an unencodable argument
- **THEN** the sentinel line SHALL carry the corresponding code and SHALL NOT carry a retry-after value

#### Scenario: The transport refusal is outside the contract

- **WHEN** an over-budget address is refused by the failed-authentication budget
- **THEN** the response SHALL be an HTTP 429 carrying `Retry-After` and `WWW-Authenticate`, SHALL NOT carry a sentinel line, and SHALL NOT be presented as a tool result

#### Scenario: Existing wording is preserved

- **WHEN** any pre-existing pre-body refusal is produced
- **THEN** its established prose SHALL still be present unchanged, with the sentinel line appended

### Requirement: A quota refusal derives its retry interval from the admission's own clock read

The retry interval quoted by an over-quota refusal SHALL be derived from the single clock reading the admission already performed, carried on the admission result alongside the accounting day, and SHALL NOT be computed from any later reading of the clock. A refusal that straddles UTC midnight SHALL therefore report the interval remaining from the instant the decision was made, never an interval measured from a day after the one the statement was bound to.

#### Scenario: A refusal at the UTC boundary reports a short interval

- **WHEN** the admission statement decides against a day whose reset is milliseconds away, and the refusal message is built after that midnight has passed
- **THEN** the reported retry interval SHALL be the small interval from the decision instant to that reset, and SHALL NOT be an interval approaching two days

#### Scenario: The clock is read once

- **WHEN** an over-quota refusal is produced
- **THEN** no code path between the admission statement and the rendered refusal SHALL read the clock again

### Requirement: Rate refusals are recorded through a self-contained coalescer

A call refused by either token bucket SHALL be recorded in `usage_logs` with the `rate_limited` marker and a `rate_limit_scope` string naming which bucket fired; a call refused by the argument length cap SHALL be recorded with the `argument_too_long` marker. Both markers SHALL be classified **pre-body** and SHALL be enumerated by the shared pre-body-refusal predicate, so refused calls are counted as refusals rather than folded into latency percentiles.

Because a rate refusal occurs at the caller's arrival rate — the very rate nothing else bounds — `rate_limited` rows SHALL be **coalesced** on the key `(principal, tool, marker, scope)`, the scope included so a write-bucket refusal is never attributed to the general one. At most one row SHALL be written per key per `MCP_REFUSAL_LOG_INTERVAL_SECONDS`; refusals arriving inside an open window SHALL increment an in-memory pending count and SHALL issue no statement of any kind, neither an INSERT nor an UPDATE.

**The arithmetic SHALL be exact, and the two flush paths differ.** `pending` SHALL count the refusals observed since the last row was written that no row yet represents, and every reader SHALL take a row to represent `1 + suppressed` refusals.

- **Window opening.** The first refusal for a key SHALL write its own row with `suppressed = 0` and set `pending` to zero — that row represents exactly itself.
- **Rollover, triggered by a new refusal.** A refusal arriving after the window has closed SHALL write a row with `suppressed = pending`, **the new refusal being that row's base**, and SHALL then reset the window and set `pending` to zero.
- **Standalone flush, triggered by the periodic indexer tick or by shutdown.** A closed window with `pending` greater than zero SHALL write a row with `suppressed = pending − 1`, because there is no new refusal to serve as the row's base and the row itself must stand for one of the pending refusals. A closed window with `pending` of zero SHALL write **no** row, because the refusal that opened it already has one.

The sum of `1 + suppressed` across every row written for a key SHALL therefore equal the number of refusals observed for that key, with no double counting at a rollover and no double counting at a flush.

**The coalescer entry SHALL hold the complete, immutable attribution of the row it will write** — the owning user, the credential identifiers, the denormalised actor triple, the tool, the marker, the scope and the bounded params — captured when the window opened. A deferred flush SHALL therefore read no request-scoped context variable and SHALL NOT depend on the credential still existing; a flush whose foreign key no longer resolves SHALL land through the existing usage-log recovery that clears the credential identifiers and keeps the denormalised actor columns.

`argument_too_long` SHALL NOT be coalesced: it is refused below the general bucket, so its rate is already bounded by that bucket, and a second mechanism would buy nothing. Coalescer cardinality SHALL be bounded by the same registry cap as the buckets, with keys beyond the cap folded into shared overflow entries keyed on `(tool, marker, scope)` — dropping only the principal from the key, so an overflowed row still names the tool, the marker and the control that fired and loses per-principal attribution alone. `rate_limit_scope` SHALL be a JSON string that no reader casts; `suppressed` SHALL be a JSON integer read with a guarded cast.

#### Scenario: A refused call leaves a marked usage row

- **WHEN** a principal's first refusal for a key occurs
- **THEN** exactly one `usage_logs` row SHALL be written for it, naming the same tool and actor an executed call would, carrying the marker and a `rate_limit_scope`, and the tool body SHALL NOT have run

#### Scenario: A refusal loop writes no statement per refusal

- **WHEN** one principal is refused many times for one tool and scope inside one coalescing interval
- **THEN** no INSERT and no UPDATE SHALL be issued for the refusals after the first, proven by a statement-counting test

#### Scenario: A deferred flush needs neither request context nor a live credential

- **WHEN** a pending count is flushed by the periodic tick or at shutdown, after every request-scoped context variable has been cleared and after the credential that produced the refusals has been deleted
- **THEN** the row SHALL still be written with the attribution captured when the window opened, SHALL carry the denormalised actor columns, and SHALL NOT raise

#### Scenario: A single refusal followed by a flush writes exactly one row

- **WHEN** exactly one refusal occurs for a key and the window later closes with no further refusal, and the tick or shutdown flush runs
- **THEN** the flush SHALL write **no** row, only the opening row SHALL exist, its `suppressed` SHALL be 0, and the sum of `1 + suppressed` SHALL be 1

#### Scenario: A standalone flush does not double-count the opening refusal

- **WHEN** five refusals occur for a key inside one window and the tick or shutdown flush then runs
- **THEN** two rows SHALL exist — the opening row with `suppressed` 0 and the flush row with `suppressed` 3 — and the sum of `1 + suppressed` SHALL be exactly 5

#### Scenario: A rollover counts the triggering refusal as the row's base

- **WHEN** five refusals occur for a key inside one window, the window closes, and a sixth refusal then arrives
- **THEN** the rollover row SHALL carry `suppressed` 4 with the sixth refusal as its base, the sum of `1 + suppressed` across both rows SHALL be exactly 6, and the pending count SHALL be reset to zero

#### Scenario: Pending counts are flushed and the totals are exact

- **WHEN** many refusals occur for one key across several windows, mixing rollovers with a final tick or shutdown flush
- **THEN** every refusal SHALL be represented exactly once, and the sum of `1 + suppressed` across every written row SHALL equal the number of refusals

#### Scenario: Scopes are not merged

- **WHEN** one principal is refused by the general bucket and by the write bucket for the same tool inside one interval
- **THEN** the two SHALL be coalesced separately and each scope's count SHALL be attributable on its own row

#### Scenario: An over-long argument writes its own row

- **WHEN** a principal is refused repeatedly by the argument length cap
- **THEN** each refusal SHALL write its own `usage_logs` row rather than being coalesced, bounded by the general bucket that runs above it

#### Scenario: The markers are distinct and separable

- **WHEN** an operator reads the marker register
- **THEN** `rate_limited` and `argument_too_long` SHALL each be used by exactly one refusal branch, SHALL be distinguishable without parsing a scope string, and SHALL differ from every existing marker

### Requirement: Over-long search query arguments are refused before any embedding call

`keyword_search` and `semantic_search` SHALL refuse a `query` longer than `MAX_SEARCH_QUERY_CHARS` (8,192 characters) before the tool body runs, and therefore before any embedding-provider request, before any `tsquery` parsing, before any search or quota statement, and before the query is interpolated into any server-authored result or error string. The check SHALL be expressed declaratively on the shared tracking decorator, beside the existing unencodable-argument screen, so that it is a pre-body refusal and the mechanism generalises to any future argument. The refusal SHALL name the argument, its actual length and the limit, and SHALL NOT echo the argument. The usage row SHALL still be written.

A character cap SHALL NOT be claimed to guarantee that the provider will accept the input: a query within the character cap can still exceed a provider's token limit, in scripts and languages that tokenize densely.

#### Scenario: An over-long query is refused before any embedding call

- **WHEN** `semantic_search` is invoked with a `query` of 8,193 or more characters
- **THEN** the call SHALL be refused with a message naming `MAX_SEARCH_QUERY_CHARS`, no embedding-provider request SHALL be made, no search statement and no quota statement SHALL be issued, and the query text SHALL NOT be echoed back

#### Scenario: A query at the limit is accepted

- **WHEN** `keyword_search` is invoked with a `query` of exactly 8,192 characters
- **THEN** the search SHALL proceed normally

#### Scenario: The refusal is still recorded

- **WHEN** an over-long query is refused
- **THEN** a `usage_logs` row SHALL carry the `argument_too_long` marker and SHALL be excluded from latency aggregates by the shared pre-body-refusal predicate

### Requirement: A provider input-limit rejection is translated into the same caller-facing refusal

When an embedding provider rejects a request because its input exceeds the provider's own limit, the tool SHALL translate that response into the same caller-facing refusal shape and the same `argument_too_long` code, carrying the provider's stated reason, rather than surfacing a raw provider error or a generic failure. The caller SHALL therefore see one actionable failure mode for "the query was too large", whether the limit that applied was this server's character cap or the provider's token limit. The exception type carrying that rejection SHALL be declared in the same dependency-free module as the refusal shape, so that the code raising it and the code handling it share one contract and neither depends on the other's module. Because the tool body has run, resolved a vault and made a network call before this branch can be reached, the **usage-log marker SHALL be distinct** — a post-body marker of its own — and SHALL NOT be enumerated by the pre-body-refusal predicate, so that a real provider round trip is never dropped out of the latency percentiles. This is the classification rule applied deliberately: the caller-facing code and the operator-facing marker answer different questions and are permitted to differ.

#### Scenario: Dense non-ASCII input within the character cap

- **WHEN** `semantic_search` is invoked with a query under `MAX_SEARCH_QUERY_CHARS` characters that nonetheless exceeds the provider's token limit, in a script that tokenizes densely
- **THEN** the caller SHALL receive the standard refusal shape with the `argument_too_long` code and the provider's reason, and SHALL NOT receive a raw provider error

#### Scenario: The provider rejection stays inside the latency aggregates

- **WHEN** such a call is logged
- **THEN** its row SHALL carry the distinct post-body marker, SHALL NOT match the pre-body-refusal predicate, and SHALL remain inside the per-tool latency and response-size aggregates

### Requirement: Limiter state is bounded in cardinality and reclaimed safely

Every limiter registry SHALL be bounded in memory by construction or by an enforced cap, and SHALL declare its overflow behaviour. State keyed on something a caller can mint freely — the client address — SHALL use a **fixed-size table** of counters (`MCP_AUTH_FAILURE_TABLE_SIZE`) indexed by a **per-process randomly salted** hash, so memory is bounded with nothing to evict; collisions SHALL only make the control stricter, and the salt SHALL be per-process and random so that a caller cannot choose to collide with another address. Principal-keyed bucket and coalescer state SHALL be held in a registry capped at `MCP_LIMITER_MAX_TRACKED_PRINCIPALS` entries with time-to-live eviction swept amortised on insert, performing a bounded amount of eviction work per admission and never requiring a background task. An entry SHALL be evicted only when it is **full and idle**: an entry whose bucket is depleted SHALL NOT be evicted, because a fresh entry starts full and evicting it would grant free capacity, and an entry holding an unflushed pending refusal count SHALL NOT be evicted, because that count would be lost. Past the cap, further principals SHALL share a single overflow bucket, and their coalescer entries SHALL fold onto shared entries keyed on `(tool, marker, scope)` so that only the principal is dropped from the key.

The shared overflow entries SHALL be documented as an accepted limitation: while they are in use, one overflowing principal's traffic can cause an unrelated overflowing principal to be refused, and coalesced rows written from the overflow entry lose per-principal attribution while keeping their count. This is the bounded-memory trade-off, it applies only beyond the registry cap, and it is preferred to failing open (which would let a flood succeed) and to failing closed (which would turn a bookkeeping cap into an outage for a legitimate credential).

#### Scenario: High principal cardinality stays bounded

- **WHEN** far more distinct principals than `MCP_LIMITER_MAX_TRACKED_PRINCIPALS` are seen
- **THEN** the registry SHALL not exceed its cap, the overflow principals SHALL be limited through the shared entries, and a principal inside the cap SHALL be unaffected

#### Scenario: High address cardinality stays bounded

- **WHEN** requests arrive from a very large number of distinct addresses
- **THEN** memory used by the failed-authentication state SHALL remain proportional to the configured table size and independent of the number of distinct addresses seen, and colliding addresses SHALL share a budget rather than escape one

#### Scenario: A depleted or unflushed entry is not evicted

- **WHEN** the sweep runs while one entry's bucket is depleted and another holds an unflushed pending refusal count
- **THEN** neither SHALL be evicted, and a principal SHALL NOT regain capacity by idling long enough to be swept

### Requirement: Limiter state is in-process and the worker count is part of the contract

All bucket, coalescing and failed-authentication state SHALL live in the worker process and SHALL NOT be persisted, replicated or shared between processes. A restart SHALL therefore begin with every bucket full and every counter zero. This SHALL be sound only while the server runs exactly one uvicorn worker; the deployment SHALL keep `--workers 1` and SHALL record, at the definition of the worker count and in the architecture notes, that raising it multiplies every in-process rate by the worker count and requires this design to be revisited. The durable per-day ceiling SHALL remain the database-backed `daily_request_limit`, of which these controls are the burst layer.

#### Scenario: Restart clears the buckets

- **WHEN** the container is recreated while a principal is over its rate limit
- **THEN** that principal's next call SHALL be admitted, while its `daily_request_limit` consumption SHALL be unchanged because it is stored in the database

#### Scenario: The worker count is documented where it is set

- **WHEN** the deployment's worker count is read
- **THEN** a comment at that definition SHALL name this dependency, and the architecture note SHALL state it in prose

### Requirement: An incoherent limiter configuration is refused at startup

Settings validation SHALL refuse, at process start, a limiter configuration whose parts contradict each other: a bucket with only one of its rate and burst configured, a `DEFAULT_DAILY_REQUEST_LIMIT` outside the 1..1,000,000 domain that applies to any other limit, or a zero value for any rate, window or table-size setting. Zero SHALL be rejected because a control that refuses every call reads to an operator as an outage rather than as a setting. Settings validation SHALL NOT read connection-pool constants, and `src/database.py` SHALL be unchanged by this capability.

#### Scenario: A half-configured bucket refuses the boot

- **WHEN** a bucket's rate is set and its burst is null, or the reverse
- **THEN** startup SHALL fail with an error naming both settings

#### Scenario: Zero is rejected

- **WHEN** any rate, window or table-size setting is set to 0
- **THEN** startup SHALL fail

#### Scenario: An out-of-domain default refuses the boot

- **WHEN** `DEFAULT_DAILY_REQUEST_LIMIT` is set outside 1..1,000,000
- **THEN** startup SHALL fail with an error naming the domain

### Requirement: Nullable limiter settings have one textual representation for "off"

Every nullable limiter setting SHALL accept exactly one documented textual representation of "off" from the environment — an empty value, or the literal `null` or `none` ignoring case and surrounding whitespace — resolved centrally by a single shared validator rather than per field, so that no two settings are disabled differently. Null SHALL be the only way to disable such a control. The representation SHALL be exercised by a test that loads an actual environment file, not only by constructing the settings object with a Python `None`.

#### Scenario: An empty value disables a control

- **WHEN** an environment file sets `MCP_RATE_LIMIT_PER_MINUTE=` with no value
- **THEN** the setting SHALL resolve to null, the general bucket SHALL be disabled, and every other control SHALL continue to apply

#### Scenario: The literal spellings are accepted

- **WHEN** an environment file sets a nullable limiter setting to `null` or to `None`
- **THEN** it SHALL resolve to null

#### Scenario: The behaviour is proven through a real environment file

- **WHEN** the settings are loaded from a written environment file rather than from constructor arguments
- **THEN** the disable representation SHALL behave identically, proven by a test that writes and loads such a file
