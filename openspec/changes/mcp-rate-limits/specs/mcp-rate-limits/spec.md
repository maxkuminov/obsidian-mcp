## ADDED Requirements

### Requirement: Every authenticated request binds a principal identity

`APIKeyMiddleware` SHALL bind a request-scoped principal identity for every authenticated `/mcp` request, derived from the credential row it has already loaded and therefore issuing no additional database statement. The identity SHALL be `("api_key", <api_keys.id>)` for an API-key request and `("oauth", <client_id>, <user_id>)` for an OAuth request — the **grant**, never the access token id, so that refreshing an access token does not reset any allowance keyed on it. The binding SHALL be reset in the same `finally` block as the other request-scoped auth context variables, so it can never label another request. A caller for which no principal is bound — sandbox mode, or a direct in-process caller that never passed the middleware — SHALL be exempt from every control keyed on the principal rather than refused or crashed.

#### Scenario: API-key and OAuth requests both carry a principal

- **WHEN** a tool call is authenticated by an API key, and another by an OAuth access token
- **THEN** each call SHALL have a principal bound, the two SHALL be distinct, and neither binding SHALL cause a database statement beyond the credential lookup the middleware already performs

#### Scenario: Refreshing an OAuth access token keeps the principal

- **WHEN** an OAuth client refreshes its access token and calls a tool with the new token
- **THEN** the principal SHALL be identical to the one the previous access token carried, so any per-principal allowance continues from its existing state

#### Scenario: Two users of one OAuth client are different principals

- **WHEN** two different users hold grants issued by the same `client_id` and each calls a tool
- **THEN** the two calls SHALL carry different principals

#### Scenario: No principal is exempt, not refused

- **WHEN** a tool implementation is invoked in-process without passing `APIKeyMiddleware`, or the server runs with `MCP_SANDBOX_MODE=true`
- **THEN** the call SHALL proceed without consulting any per-principal control and SHALL NOT raise

### Requirement: A per-principal request-rate bucket admits every tool call

Every `_tracked` tool call SHALL pass a per-principal token bucket before any other admission gate, refilling at `MCP_RATE_LIMIT_PER_MINUTE` tokens per minute with a capacity of `MCP_RATE_LIMIT_BURST` tokens. The bucket SHALL be updated synchronously with no `await` between reading and writing its state, SHALL issue no database statement, and SHALL hold no lock. A call with no token available SHALL be refused before the tool body runs. A token SHALL be consumed by every call that reaches the gate, including calls that a later gate then refuses — a token refills and a quota slot does not. Setting `MCP_RATE_LIMIT_PER_MINUTE` or `MCP_RATE_LIMIT_BURST` to null SHALL disable the control; zero SHALL be rejected as a configuration value.

#### Scenario: A burst from one principal does not delay another principal's call

- **WHEN** principal A exhausts its burst capacity and continues calling, while principal B issues a single ordinary tool call
- **THEN** B's call SHALL be admitted and executed without waiting on A, and A's excess calls SHALL be refused rather than queued

#### Scenario: Sustained rate is admitted

- **WHEN** a principal calls at or below `MCP_RATE_LIMIT_PER_MINUTE` sustained, having started with a full bucket
- **THEN** every call SHALL be admitted

#### Scenario: The admitted path performs no additional I/O

- **WHEN** a call is admitted by the bucket
- **THEN** the gate SHALL have issued no SQL statement and no session checkout, proven by a statement-counting test

#### Scenario: A rate refusal consumes no daily quota

- **WHEN** a key that also carries a `daily_request_limit` is refused by the rate bucket
- **THEN** its `quota_counters` row SHALL be unchanged

### Requirement: Expensive tool classes are bounded by concurrency slots with a bounded wait

Tool calls SHALL acquire concurrency slots after every admission gate and before the tool body, and SHALL release them in a `finally` block so that a body which raises never leaks a slot. The slots SHALL be: a per-principal-per-class cap, a per-principal in-flight cap (`MCP_MAX_CONCURRENT_PER_PRINCIPAL`), a per-class cap, and a global ceiling (`MCP_MAX_CONCURRENT_TOOL_CALLS`), acquired in exactly that order — narrowest scope first — so that a task waiting on a broader resource holds only narrower ones and the fixed total order makes deadlock impossible. The classes SHALL be `embedding` (`semantic_search`), `vector` (`find_related`), and `write` (`create_note`, `edit_note`, `move_note`, `delete_note`, `set_frontmatter`, `write_file`, `delete_file`, `import_from_url`); every other tool SHALL take only the per-principal and global slots. All acquisitions for one call SHALL share **one** deadline of `MCP_SLOT_WAIT_SECONDS`, never one timeout per acquisition. Waiting SHALL never be unbounded and no queue SHALL be offered in place of a refusal.

#### Scenario: A timed-out slot wait is refused with retry-after

- **WHEN** the `embedding` class is fully occupied and a further `semantic_search` call waits out `MCP_SLOT_WAIT_SECONDS` without obtaining a slot
- **THEN** the call SHALL be refused in band with a message naming the contended control and a `retry_after_seconds` value, SHALL NOT execute the tool body, SHALL NOT be queued further, and SHALL NOT return an empty result set

#### Scenario: The total wait is one budget, not four

- **WHEN** a call contends for more than one slot on its way to the body
- **THEN** the sum of its waits SHALL NOT exceed `MCP_SLOT_WAIT_SECONDS`

#### Scenario: A failing body releases its slots

- **WHEN** an admitted tool body raises an exception
- **THEN** every slot the call acquired SHALL be released and a subsequent call SHALL acquire them immediately

#### Scenario: One principal cannot monopolise the embedding class

- **WHEN** principal A has `MCP_MAX_CONCURRENT_EMBEDDING_PER_PRINCIPAL` embedding calls in flight and issues another, while principal B issues one
- **THEN** B SHALL acquire an embedding slot and A's additional call SHALL wait against the shared deadline and then be refused

#### Scenario: Unclassed tools do not consume class slots

- **WHEN** `read_note` and `list_notes` are called while the `write` class is saturated
- **THEN** they SHALL be admitted subject only to the per-principal and global slots

### Requirement: Rate and slot refusals are typed, in-band, and actionable

Every refusal produced by the rate bucket or by a slot wait SHALL be returned to the caller as an ordinary tool result, never as a protocol error and never as an empty result set, and SHALL name the control that fired, the ceiling that applied, and a `retry_after_seconds` value the caller can wait out. For the rate bucket the value SHALL be the whole number of seconds until one token is available; for a slot wait it SHALL be the wait budget. The refusal SHALL travel through the existing `refusal_result` mapping so that a tool declaring a structured output type refuses in its own declared shape rather than by breaking the wire format.

#### Scenario: A structured tool refuses in its own shape

- **WHEN** `read_note` is refused by the rate bucket
- **THEN** the caller SHALL receive the tool's declared structured result carrying the refusal message, not a bare string and not an output-schema validation failure

#### Scenario: The refusal is actionable

- **WHEN** any rate or slot refusal is returned
- **THEN** its text SHALL name the limit and a `retry_after_seconds` of at least 1, and SHALL NOT be an empty or zero-result answer that a caller could mistake for "nothing matched"

### Requirement: A refused call is recorded as a pre-body refusal in the usage log

A call refused by the rate bucket or by a slot wait SHALL be written to `usage_logs` like any other call, with `params.error` set to the marker `rate_limited` and a `rate_limit_scope` string naming which control fired (`principal`, `principal_slots`, `principal_slot:<class>`, `slot:<class>`, or `slot:global`). A call refused by the argument length cap SHALL be recorded with `params.error` set to `argument_too_long`. Both markers SHALL be classified as **pre-body** and SHALL be enumerated by the shared pre-body-refusal predicate, so refused calls are counted as refusals rather than folded into latency percentiles. `rate_limit_scope` SHALL be a JSON string and SHALL NOT be cast by any reader, keeping it outside the reserved numeric/boolean `params` keys.

#### Scenario: A refused call leaves a marked usage row

- **WHEN** a principal is refused by the rate bucket
- **THEN** exactly one `usage_logs` row SHALL be written for that call, naming the same tool and actor as an executed call would, carrying `error: "rate_limited"` and a `rate_limit_scope` value, and the tool body SHALL NOT have run

#### Scenario: Refusals do not make a tool look fast

- **WHEN** a window holds 100 executed `semantic_search` calls and 5,000 `rate_limited` refusals for it
- **THEN** the performance view SHALL compute that tool's percentiles from the 100 executed rows only and SHALL report the 5,000 as its refusal count

#### Scenario: The marker names are distinct

- **WHEN** the marker register is read
- **THEN** `rate_limited` and `argument_too_long` SHALL each be a value used by exactly one refusal branch and SHALL differ from every existing marker, including `no_vault_assigned`, `argument_not_encodable`, `over_quota`, and the markers introduced by any parallel change

### Requirement: Failed authentication on /mcp is budgeted per client IP

`APIKeyMiddleware` SHALL maintain a per-client-IP count of authentication failures on `/mcp` over a sliding window of `MCP_AUTH_FAILURE_WINDOW_SECONDS`, and SHALL refuse further requests from an address that has exceeded `MCP_AUTH_FAILURE_LIMIT` failures within that window with HTTP 429 and a `Retry-After` header, retaining the existing `WWW-Authenticate` header. The over-budget check SHALL run before the credential lookup, so a refused request costs no database session and no query. The client address SHALL be the one established by the application's restricted proxy-header handling, never a raw request header. Only failed authentications SHALL increment the counter. Setting `MCP_AUTH_FAILURE_LIMIT` to null SHALL disable the control. The first refusal in a window SHALL emit one WARNING naming the address.

#### Scenario: A probing address is bounded without touching the database

- **WHEN** an address sends more than `MCP_AUTH_FAILURE_LIMIT` requests with invalid bearer tokens inside the window
- **THEN** subsequent requests from that address SHALL receive HTTP 429 with `Retry-After`, and SHALL NOT cause a credential lookup

#### Scenario: A valid credential under budget is unaffected

- **WHEN** an address is below its failure budget and presents a valid credential
- **THEN** the request SHALL be authenticated and served exactly as before, and the counter SHALL NOT increment

#### Scenario: The window rolls

- **WHEN** an over-budget address stops for longer than `MCP_AUTH_FAILURE_WINDOW_SECONDS` and then presents a valid credential
- **THEN** the request SHALL be served

#### Scenario: The control can be turned off

- **WHEN** `MCP_AUTH_FAILURE_LIMIT` is null
- **THEN** no failure counting SHALL occur and every request SHALL reach the credential lookup as it does today

### Requirement: Over-long search query arguments are refused before any embedding call

`keyword_search` and `semantic_search` SHALL refuse a `query` longer than `MAX_SEARCH_QUERY_CHARS` (8,192 characters) before the tool body runs, and therefore before any embedding-provider request, before any `tsquery` parsing, before any database statement, and before the query is interpolated into any server-authored result or error string. The check SHALL be expressed declaratively on the shared tracking decorator, beside the existing unencodable-argument screen, so that it is a pre-body refusal and so that the mechanism generalises to any future argument. The refusal SHALL name the argument, its actual length, and the limit.

#### Scenario: An over-long query is refused before any embedding call

- **WHEN** `semantic_search` is invoked with a `query` of 8,193 or more characters
- **THEN** the call SHALL be refused with a message naming `MAX_SEARCH_QUERY_CHARS`, no embedding-provider request SHALL be made, no database statement SHALL be issued for the search, and the query text SHALL NOT be echoed back

#### Scenario: A query at the limit is accepted

- **WHEN** `keyword_search` is invoked with a `query` of exactly 8,192 characters
- **THEN** the search SHALL proceed normally

#### Scenario: The refusal is recorded as pre-body

- **WHEN** an over-long query is refused
- **THEN** the `usage_logs` row SHALL carry `error: "argument_too_long"` and SHALL be excluded from latency aggregates by the shared pre-body-refusal predicate

### Requirement: Limiter state is in-process and the worker count is part of the contract

All rate-bucket and concurrency state SHALL live in the worker process and SHALL NOT be persisted, replicated, or shared between processes. A restart SHALL therefore begin with every bucket full and every slot free. This SHALL be sound only while the server runs exactly one uvicorn worker; the deployment SHALL keep `--workers 1` and SHALL record, at the definition of the worker count and in the architecture notes, that raising it multiplies every in-process control by the worker count and requires this design to be revisited. The durable per-day ceiling SHALL remain the database-backed `daily_request_limit`, of which these controls are the burst layer.

#### Scenario: Restart clears the buckets

- **WHEN** the container is recreated while a principal is over its rate limit
- **THEN** that principal's next call SHALL be admitted, while its `daily_request_limit` consumption SHALL be unchanged because it is stored in the database

#### Scenario: The worker count is documented where it is set

- **WHEN** the deployment's worker count is read
- **THEN** a comment at that definition SHALL name this dependency, and the architecture note SHALL state it in prose

### Requirement: An incoherent limiter configuration is refused at startup

Settings validation SHALL refuse, at process start, a limiter configuration whose parts contradict each other: a per-principal in-flight cap greater than the global ceiling, a per-principal embedding cap greater than the embedding class limit, a global ceiling below the largest class limit, or a zero value for any rate or concurrency setting. Null SHALL be the only way to disable a nullable control, following the established NULL-means-unlimited convention; zero SHALL be rejected because a control that refuses every call reads to an operator as an outage rather than as a setting.

#### Scenario: Contradictory concurrency settings refuse the boot

- **WHEN** `MCP_MAX_CONCURRENT_PER_PRINCIPAL` exceeds `MCP_MAX_CONCURRENT_TOOL_CALLS`
- **THEN** startup SHALL fail with an error naming both settings

#### Scenario: Zero is rejected, null disables

- **WHEN** `MCP_RATE_LIMIT_PER_MINUTE` is set to 0, and separately to null
- **THEN** the 0 SHALL be rejected at startup and the null SHALL disable the rate bucket while every other control continues to apply
