## ADDED Requirements

### Requirement: Requests are admitted under a process ceiling before any authentication work

`APIKeyMiddleware` SHALL maintain a process-wide count of in-flight `/mcp` requests, incremented at entry and decremented in a `finally` block, and SHALL refuse a request that arrives while the count is at `MCP_MAX_INFLIGHT_REQUESTS` with HTTP 503 and a `Retry-After` header. The check SHALL happen **before any database session is opened and before any credential lookup**, so that a flood of requests bearing valid credentials cannot exhaust the connection pool on authentication alone. The refusal SHALL write no `usage_logs` row, because no authenticated call exists to attribute one to. `MCP_MAX_INFLIGHT_REQUESTS` SHALL be greater than or equal to the global tool-call ceiling, since a request waiting for a concurrency slot holds an in-flight count.

#### Scenario: A valid-credential flood does not exhaust the pool

- **WHEN** a single valid credential issues far more concurrent `/mcp` requests than `MCP_MAX_INFLIGHT_REQUESTS`
- **THEN** the excess SHALL receive HTTP 503 with `Retry-After` without opening a database session, and a second tenant's request issued during the flood SHALL obtain a connection and be served

#### Scenario: The ceiling is released when a request finishes

- **WHEN** requests admitted at the ceiling complete, including one whose handler raises
- **THEN** the in-flight count SHALL return to its prior value and subsequent requests SHALL be admitted

#### Scenario: The ceiling is refused as a server condition

- **WHEN** a request is refused by this ceiling
- **THEN** the status SHALL be 503, not 429, because the condition is server capacity rather than the caller's allowance

### Requirement: Failed authentication on /mcp is budgeted per client address

`APIKeyMiddleware` SHALL count authentication failures per client address over a window of `MCP_AUTH_FAILURE_WINDOW_SECONDS` and SHALL refuse a request from an address whose recorded count for the current window is already greater than or equal to `MCP_AUTH_FAILURE_LIMIT`, with HTTP 429, a `Retry-After` header, and the existing `WWW-Authenticate` header. The check SHALL run before the credential lookup, so a refused request costs no database session and no query, and a refused request SHALL NOT increment the count. **Every** 401 branch SHALL increment — missing bearer token, unknown credential, ownerless credential, inactive user, expired credential, cross-user grant, and missing vault scope — so that no branch is a cheaper probe than another. The address SHALL be the one established by the application's restricted proxy-header handling and SHALL NEVER be read directly from a request header; a request carrying no client address SHALL be charged to a single reserved slot shared by all such requests rather than exempted. `Retry-After` SHALL be the whole number of seconds remaining in the current window, minimum 1. Setting `MCP_AUTH_FAILURE_LIMIT` to null SHALL disable the control. The first refusal for a slot in a window SHALL emit one WARNING naming the address.

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

`APIKeyMiddleware` SHALL bind a request-scoped principal identity for every authenticated `/mcp` request, derived from the credential row it has already loaded and therefore issuing no additional database statement. The identity SHALL be `("api_key", <api_keys.id>)` for an API-key request and `("oauth", <oauth_tokens.grant_id>)` for an OAuth request. The OAuth key SHALL be the **grant**, never the access-token id and never a `(client_id, user_id)` pair: a grant is the unit `/authorize` created and the unit revocation acts on, every rotation inherits it, and two grants for the same client and user are independently revocable and SHALL therefore hold independent allowances. The binding SHALL be reset in the same `finally` block as the other request-scoped auth context variables. A caller for which no principal is bound — sandbox mode, or a direct in-process caller that never passed the middleware — SHALL be exempt from every control keyed on the principal rather than refused or crashed; the in-flight ceiling and the failed-authentication budget are not keyed on the principal and SHALL apply regardless.

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

Every `_tracked` tool call SHALL pass a per-principal general token bucket before any other admission gate, refilling at `MCP_RATE_LIMIT_PER_MINUTE` tokens per minute with capacity `MCP_RATE_LIMIT_BURST`. Every call to a tool in the `write` class SHALL additionally pass a second per-principal bucket refilling at `MCP_WRITE_RATE_LIMIT_PER_MINUTE` with capacity `MCP_WRITE_RATE_LIMIT_BURST`, checked immediately after the general bucket. Each bucket SHALL be updated synchronously with no `await` between reading and writing its state, SHALL issue no database statement, and SHALL hold no lock. A token SHALL be consumed by every call that reaches a bucket, including calls a later gate then refuses — a token refills and a durable quota slot does not. Both buckets SHALL be understood as **velocity** bounds: they bound the rate at which work is created and SHALL NOT be described as bounding the total damage a credential can do, which is bounded by the daily quota. Either bucket's rate and burst SHALL be set together or be null together; null SHALL disable that bucket and zero SHALL be rejected as a configuration value.

#### Scenario: A burst from one principal does not delay another principal's call

- **WHEN** principal A exhausts its burst capacity and continues calling, while principal B issues a single ordinary tool call
- **THEN** B's call SHALL be admitted and executed without waiting on A, and A's excess calls SHALL be refused rather than queued

#### Scenario: Write tools pass both buckets

- **WHEN** a principal calls `delete_note` at a rate under `MCP_RATE_LIMIT_PER_MINUTE` but above `MCP_WRITE_RATE_LIMIT_PER_MINUTE`
- **THEN** the excess calls SHALL be refused with `scope` naming the write bucket, while that principal's read tools SHALL continue to be admitted

#### Scenario: Sustained rate is admitted

- **WHEN** a principal calls at or below the configured sustained rate, having started with a full bucket
- **THEN** every call SHALL be admitted

#### Scenario: The admitted path performs no additional I/O

- **WHEN** a call is admitted by both buckets
- **THEN** the gates SHALL have issued no SQL statement and no session checkout, proven by a statement-counting test

### Requirement: Concurrency slots bound expensive classes per principal, per tenant, and per process

Tool calls SHALL acquire concurrency slots after the argument screens and **before daily-quota admission**, and SHALL release them in a `finally` block that also covers the quota gate and the tool body, so that neither a quota refusal nor a raising body leaks a slot. The slots SHALL be acquired in exactly this order — per-principal-per-class, per-principal across all classes (`MCP_MAX_CONCURRENT_PER_PRINCIPAL`), **per-tenant-per-class**, per-class, then the global ceiling (`MCP_MAX_CONCURRENT_TOOL_CALLS`) — narrowest scope first, so that a task waiting on a broader resource holds only narrower ones, and so that the fixed total order makes deadlock impossible. The classes SHALL be `embedding` (`semantic_search`), `vector` (`find_related`) and `write` (`create_note`, `edit_note`, `move_note`, `delete_note`, `set_frontmatter`, `write_file`, `delete_file`, `import_from_url`); every other tool SHALL take only the per-principal and global slots. The tenant SHALL be the owning user, with ownerless and single-user traffic treated as one tenant. All acquisitions for one call SHALL share **one** deadline of approximately `MCP_SLOT_WAIT_SECONDS`, never one timeout per acquisition, and the elapsed wait SHALL NOT materially exceed that budget. Waiting SHALL never be unbounded and no queue SHALL be offered in place of a refusal.

#### Scenario: A timed-out slot wait is refused with retry-after

- **WHEN** the `embedding` class is fully occupied and a further `semantic_search` call waits out the budget without obtaining a slot
- **THEN** the call SHALL be refused in band with `code` `slot_timeout`, a `scope` naming the contended slot and a numeric `retry_after_seconds`, SHALL NOT execute the tool body, SHALL NOT be queued further, and SHALL NOT return an empty result set

#### Scenario: A slot timeout consumes no daily quota

- **WHEN** a key carrying a `daily_request_limit` times out waiting for a slot
- **THEN** its `quota_counters` row SHALL be unchanged, because slots are acquired before quota admission

#### Scenario: A quota refusal releases the slots it held

- **WHEN** a call acquires its slots and is then refused by the daily quota
- **THEN** every slot SHALL be released before the refusal is returned, and a concurrent call SHALL acquire them immediately

#### Scenario: One tenant cannot monopolise the embedding class through two credentials

- **WHEN** one user issues concurrent `semantic_search` calls under both an API key and an OAuth grant, and a second user issues one
- **THEN** the first user SHALL hold no more than `MCP_MAX_CONCURRENT_EMBEDDING_PER_USER` embedding slots in total across both credentials, and the second user's call SHALL obtain a slot

#### Scenario: The total wait is one budget, not one per level

- **WHEN** a call contends for more than one slot on its way to the body
- **THEN** the sum of its waits SHALL be approximately `MCP_SLOT_WAIT_SECONDS` and SHALL NOT approach a multiple of it

#### Scenario: A failing body releases its slots

- **WHEN** an admitted tool body raises an exception
- **THEN** every slot the call acquired SHALL be released and a subsequent call SHALL acquire them immediately

#### Scenario: Unclassed tools do not consume class slots

- **WHEN** `read_note` and `list_notes` are called while the `write` class is saturated
- **THEN** they SHALL be admitted subject only to the per-principal and global slots

### Requirement: Every pre-body refusal carries one caller-visible structured shape

Every refusal produced by a pre-body gate SHALL be returned to the caller as an ordinary tool result — never a protocol error, never an empty result set — and SHALL end with a single machine-readable final line introduced by a fixed sentinel token and carrying a one-line JSON object with the fields `code`, `scope`, `limit`, `limit_unit` and `retry_after_seconds`. `code` SHALL come from a closed set that includes `rate_limited`, `slot_timeout`, `argument_too_long`, `over_quota`, `no_vault_assigned` and `argument_not_encodable`. `retry_after_seconds` SHALL be a JSON number of at least 1 wherever retrying can succeed — the whole seconds until a token is available for a bucket refusal, the wait budget for a slot timeout, the seconds to the next UTC reset for a quota refusal — and SHALL be absent where retrying cannot help, so that no refusal invites a loop that cannot end. A tool returning a plain string SHALL receive the human prose followed by that line; a tool declaring a structured output type SHALL receive the identical complete text in its declared error field through the existing refusal mapping, so both kinds of tool expose the same fields and no output-schema validation can fail. The three pre-existing pre-body refusals SHALL adopt the line additively, leaving their existing prose unchanged.

#### Scenario: A string-returning tool exposes the fields

- **WHEN** `keyword_search` is refused by the general bucket
- **THEN** the returned string SHALL contain the sentinel line, and parsing the JSON on it SHALL yield `code` `rate_limited` and a numeric `retry_after_seconds` of at least 1

#### Scenario: A structured tool refuses in its own shape with the same fields

- **WHEN** `read_note` is refused by the same gate
- **THEN** the caller SHALL receive the tool's declared structured result whose error field carries the identical text including the sentinel line, and SHALL NOT receive a bare string or an output-schema validation failure

#### Scenario: A futile retry is not invited

- **WHEN** a call is refused for having no vault assignment or for an unencodable argument
- **THEN** the sentinel line SHALL carry the corresponding `code` and SHALL NOT carry a `retry_after_seconds` value

#### Scenario: Existing wording is preserved

- **WHEN** any pre-existing pre-body refusal is produced
- **THEN** its established prose SHALL still be present unchanged, with the sentinel line appended

### Requirement: Refused calls are recorded as bounded, coalesced pre-body refusals

A call refused by a token bucket SHALL be recorded in `usage_logs` with `params.error` set to `rate_limited`; a call refused by a slot wait SHALL be recorded with `slot_timeout`; a call refused by the argument length cap SHALL be recorded with `argument_too_long`. Each SHALL also carry a `rate_limit_scope` string naming which control fired. All three markers SHALL be classified **pre-body** and SHALL be enumerated by the shared pre-body-refusal predicate, so refused calls are counted as refusals rather than folded into latency percentiles. To stop a refusal loop becoming an INSERT amplifier, `rate_limited` and `slot_timeout` rows SHALL be **coalesced**: at most one row per `(principal, tool, marker)` per `MCP_REFUSAL_LOG_INTERVAL_SECONDS`, with the written row carrying `suppressed` — a JSON integer counting the refusals it stands for, zero when it stands only for itself. Refusals SHALL NOT be silently discarded: the counts SHALL remain exact in aggregate. `rate_limit_scope` SHALL be a JSON string that no reader casts; `suppressed` SHALL be a JSON integer read with a guarded cast.

#### Scenario: A refused call leaves a marked usage row

- **WHEN** a principal's first refusal in a coalescing interval occurs
- **THEN** exactly one `usage_logs` row SHALL be written for it, naming the same tool and actor an executed call would, carrying the marker and a `rate_limit_scope`, and the tool body SHALL NOT have run

#### Scenario: A refusal loop is bounded in the log but exact in total

- **WHEN** one principal is refused 1,200 times for one tool inside one coalescing interval
- **THEN** the number of rows written SHALL be bounded by the interval rather than by the number of refusals, and the sum of `1 + suppressed` across those rows SHALL equal 1,200

#### Scenario: Refusals do not make a tool look fast

- **WHEN** a window holds 100 executed `semantic_search` calls and coalesced refusal rows accounting for 5,000 refusals of it
- **THEN** the performance view SHALL compute that tool's percentiles from the 100 executed rows only and SHALL report 5,000 as its refusal count

#### Scenario: The markers are distinct and separable

- **WHEN** an operator reads the marker register
- **THEN** `rate_limited`, `slot_timeout` and `argument_too_long` SHALL each be used by exactly one refusal branch, SHALL be distinguishable without parsing a scope string, and SHALL differ from every existing marker including `no_vault_assigned`, `argument_not_encodable` and `over_quota`

### Requirement: Over-long search query arguments are refused before any embedding call

`keyword_search` and `semantic_search` SHALL refuse a `query` longer than `MAX_SEARCH_QUERY_CHARS` (8,192 characters) before the tool body runs, and therefore before any embedding-provider request, before any `tsquery` parsing, before any search or quota statement, and before the query is interpolated into any server-authored result or error string. The check SHALL be expressed declaratively on the shared tracking decorator, beside the existing unencodable-argument screen, so that it is a pre-body refusal and the mechanism generalises to any future argument. The refusal SHALL name the argument, its actual length and the limit, and SHALL NOT echo the argument. The usage row SHALL still be written, subject to the coalescing rule.

#### Scenario: An over-long query is refused before any embedding call

- **WHEN** `semantic_search` is invoked with a `query` of 8,193 or more characters
- **THEN** the call SHALL be refused with a message naming `MAX_SEARCH_QUERY_CHARS`, no embedding-provider request SHALL be made, no search statement and no quota statement SHALL be issued, and the query text SHALL NOT be echoed back

#### Scenario: A query at the limit is accepted

- **WHEN** `keyword_search` is invoked with a `query` of exactly 8,192 characters
- **THEN** the search SHALL proceed normally

#### Scenario: The refusal is still recorded

- **WHEN** an over-long query is refused
- **THEN** a `usage_logs` row SHALL carry `error: "argument_too_long"` and SHALL be excluded from latency aggregates by the shared pre-body-refusal predicate

### Requirement: Limiter state is bounded in cardinality and reclaimed safely

Every limiter registry SHALL be bounded in memory by construction or by an enforced cap, and SHALL declare its overflow behaviour. Principal- and tenant-keyed state SHALL be held in a registry capped at `MCP_LIMITER_MAX_TRACKED_PRINCIPALS` entries with time-to-live eviction swept amortised on insert, performing a bounded amount of eviction work per admission and never requiring a background task. An entry SHALL be evicted only when it is **full and idle**: an entry whose bucket is depleted SHALL NOT be evicted, because a fresh entry starts full and evicting it would grant free capacity, and an entry holding in-flight slots or having waiters SHALL NOT be evicted, because its counts are live. On overflow, further principals SHALL share a single overflow bucket — never fail-open, which would let the flood succeed, and never fail-closed, which would turn a bookkeeping cap into an outage for a legitimate credential. Address-keyed failed-authentication state SHALL instead use a fixed-size table of `MCP_AUTH_FAILURE_TABLE_SIZE` counters indexed by a per-process randomly salted hash of the address, so its memory is bounded with nothing to evict; collisions SHALL only make the control stricter, and the salt SHALL be per-process and random so that a caller cannot choose to collide with another address.

#### Scenario: High principal cardinality stays bounded

- **WHEN** far more distinct principals than `MCP_LIMITER_MAX_TRACKED_PRINCIPALS` are seen
- **THEN** the registry SHALL not exceed its cap, the overflow principals SHALL be limited through the shared overflow bucket, and no legitimate credential SHALL be refused merely for being evicted or overflowed

#### Scenario: High address cardinality stays bounded

- **WHEN** requests arrive from a very large number of distinct addresses
- **THEN** memory used by the failed-authentication state SHALL remain proportional to `MCP_AUTH_FAILURE_TABLE_SIZE` and independent of the number of addresses seen

#### Scenario: A depleted or busy entry is not evicted

- **WHEN** the sweep runs while one entry's bucket is depleted and another holds an in-flight slot or has a waiter
- **THEN** neither SHALL be evicted, and a principal SHALL NOT regain capacity by idling long enough to be swept

### Requirement: Limiter state is in-process and the worker count is part of the contract

All in-flight, bucket, slot and failed-authentication state SHALL live in the worker process and SHALL NOT be persisted, replicated or shared between processes. A restart SHALL therefore begin with every bucket full, every slot free and every counter zero. This SHALL be sound only while the server runs exactly one uvicorn worker; the deployment SHALL keep `--workers 1` and SHALL record, at the definition of the worker count and in the architecture notes, that raising it multiplies every in-process control by the worker count and requires this design to be revisited. The durable per-day ceiling SHALL remain the database-backed `daily_request_limit`, of which these controls are the burst layer.

#### Scenario: Restart clears the buckets

- **WHEN** the container is recreated while a principal is over its rate limit
- **THEN** that principal's next call SHALL be admitted, while its `daily_request_limit` consumption SHALL be unchanged because it is stored in the database

#### Scenario: The worker count is documented where it is set

- **WHEN** the deployment's worker count is read
- **THEN** a comment at that definition SHALL name this dependency, and the architecture note SHALL state it in prose

### Requirement: An incoherent limiter configuration is refused at startup

Settings validation SHALL refuse, at process start, a limiter configuration whose parts contradict each other: a global tool-call ceiling below the **sum** of the per-class ceilings; a per-principal in-flight cap greater than the global ceiling; a per-principal class cap greater than the per-tenant class cap, or a per-tenant class cap greater than that class's ceiling; an in-flight request ceiling below the global tool-call ceiling; a bucket with only one of its rate and burst configured; or a zero value for any rate, concurrency or window setting. The sum relationship SHALL be enforced rather than merely documented, because the acquisition-order argument depends on the global ceiling never being what a classed call queues behind.

#### Scenario: The sum relationship is enforced

- **WHEN** `MCP_MAX_CONCURRENT_TOOL_CALLS` is set below the sum of the embedding, vector and write ceilings
- **THEN** startup SHALL fail with an error naming the settings and the sum

#### Scenario: Contradictory per-scope caps refuse the boot

- **WHEN** `MCP_MAX_CONCURRENT_PER_PRINCIPAL` exceeds `MCP_MAX_CONCURRENT_TOOL_CALLS`, or the per-principal embedding cap exceeds the per-tenant embedding cap
- **THEN** startup SHALL fail with an error naming both settings involved

#### Scenario: Zero is rejected

- **WHEN** any rate, concurrency or window setting is set to 0
- **THEN** startup SHALL fail, because a control that refuses every call reads to an operator as an outage rather than as a setting

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
