## MODIFIED Requirements

### Requirement: Concurrency ships as zero-wait shadow observation
The server SHALL default concurrency control to shadow mode, evaluate the same zero-wait capacity predicate as enforcement against observed occupancy, and leave actual admission, results and outcomes unchanged by these concurrency ceilings.

Shadow SHALL never wait on any stage, whatever wait durations are configured. Configured positive waits SHALL be accepted in shadow mode, SHALL NOT be applied, and SHALL be reported in the shadow metadata as `configured_wait_ms` so that no reader can mistake a zero-wait observation for the result of a wait. Changing between `shadow`, `queue` and `enforce` SHALL require changing no setting other than `MCP_CONCURRENCY_MODE`.

#### Scenario: An overloaded call still runs in shadow
- **WHEN** observed concurrent work exceeds a configured ceiling in shadow mode
- **THEN** the call SHALL execute subject to existing non-concurrency gates without a concurrency wait or refusal
- **AND** an actual tool usage row SHALL carry bounded namespaced shadow metadata without changing its actual error, disposition or request count

#### Scenario: A positive wait is not applied in shadow
- **WHEN** shadow mode is configured with `MCP_CONCURRENCY_WAIT_SECONDS=5` and `MCP_CONCURRENCY_TRANSPORT_WAIT_SECONDS=2`
- **THEN** startup SHALL succeed
- **AND** a pressured call SHALL be admitted without awaiting any concurrency primitive
- **AND** its shadow metadata SHALL carry `basis: observed_occupancy_zero_wait` and `configured_wait_ms` of `{"transport": 2000, "tool": 5000}`

#### Scenario: A mode change is one line
- **WHEN** the default `.env.example` concurrency block is loaded once with mode `shadow`, once with `queue` and once with `enforce`
- **THEN** all three SHALL pass configuration validation with no other edit

### Requirement: Request and authentication occupancy have separate lifetimes
The middleware SHALL control global and fingerprint request occupancy before DB lookup and control auth-session occupancy only while its own session is open.

In `queue` and `enforce` modes, the request stage and the auth stage SHALL share one monotonic deadline per request of `MCP_CONCURRENCY_TRANSPORT_WAIT_SECONDS` (default 2, maximum 5). Waiters SHALL be bounded by `MCP_CONCURRENCY_REQUEST_WAITERS`, `MCP_CONCURRENCY_FINGERPRINT_WAITERS` and `MCP_CONCURRENCY_AUTH_WAITERS`. A request waiting at either stage SHALL hold no database connection and no auth permit, and SHALL be granted in eligible-FIFO order when capacity frees. In `enforce` mode, expiry of the deadline or a full waiter bound SHALL produce the transport refusal. In `queue` mode it SHALL produce an overrun admission instead.

#### Scenario: Authentication capacity is full in enforce mode
- **WHEN** a new request cannot acquire request or auth capacity before the transport deadline, or the relevant waiter bound is full
- **THEN** it SHALL receive a transport refusal without a new credential query or per-request usage INSERT

#### Scenario: A short auth burst waits instead of failing
- **WHEN** enforce mode has auth ceiling 2 and six requests arrive together, each of whose auth sessions completes in 10 ms
- **THEN** all six SHALL authenticate within the transport deadline and none SHALL receive a 429
- **AND** no more than two auth sessions SHALL be open at any instant

#### Scenario: One deadline covers both transport stages
- **WHEN** a request waits 1.5 s for the request envelope under a 2 s transport deadline and then finds the auth ceiling full
- **THEN** it SHALL wait at most the remaining 0.5 s for the auth permit before the deadline outcome for its mode applies

#### Scenario: A cancelled transport waiter leaks nothing
- **WHEN** a client disconnects while its request waits for the envelope or for the auth permit
- **THEN** every waiter count, registry reference and lease captured for that request SHALL be released exactly once

#### Scenario: A stream outlives authentication
- **WHEN** an authenticated SSE or other request remains open after auth completes
- **THEN** its request/fingerprint lease SHALL remain held and its auth permit SHALL be released
- **AND** completion, exceptions and cancellation SHALL release each lease exactly once

#### Scenario: Authentication sends a refusal slowly
- **WHEN** the transport stalls while sending an invalid-credential response
- **THEN** the middleware SHALL already have exited its auth DB session and released that auth permit

### Requirement: Tool admission acquires the entire class lattice atomically
Every registered tool SHALL have exactly one explicit class out of embedding, vector, write, scan or light, and SHALL acquire class, tenant, principal and global dimensions together.

The closed mapping SHALL be:
- `semantic_search` → embedding;
- `find_related` → vector;
- the eight write-class tools → write;
- `keyword_search`, `list_notes`, `get_tags`, `get_neighborhood`, `find_orphans` and `list_files` → scan;
- `read_note`, `read_file`, `get_recent`, `get_vault_guide`, `get_backlinks`, `get_links`, `request_upload`, `check_upload` and `request_download` → light.

Each class ceiling SHALL be an independent ceiling no greater than the global `tools` ceiling. Class ceilings SHALL NOT be required to sum to at most `tools`.

#### Scenario: Every registered tool is classified
- **WHEN** the registered tool set is compared with the class mapping
- **THEN** every tool SHALL map to exactly one of the five classes, no class SHALL be named `other`, and an unmapped registration SHALL fail the registry test

#### Scenario: Class ceilings may overlap the global ceiling
- **WHEN** settings configure tools 6 with light 4, scan 2, write 1, embedding 1 and vector 1
- **THEN** validation SHALL accept them
- **AND** no more than six tools SHALL be admitted at once whatever mix of classes arrives

#### Scenario: A busy class cannot occupy another class's capacity while waiting
- **WHEN** a class is full and another eligible class has capacity
- **THEN** the waiting call SHALL hold no partial permits and the eligible call SHALL be able to proceed

#### Scenario: Rotating credentials does not reset tenant capacity
- **WHEN** calls share a user across API keys or OAuth grants, or an OAuth grant refreshes its token
- **THEN** tenant limits SHALL remain shared and grant-level principal limits SHALL remain stable across refresh

#### Scenario: Positive waiting has one deadline
- **WHEN** an enforced call waits on several saturated dimensions
- **THEN** it SHALL have one monotonic bounded deadline and bounded waiter registration
- **AND** timeout, zero-wait miss or waiter overflow SHALL return the typed slot_timeout result

#### Scenario: A parallel read batch waits rather than failing
- **WHEN** enforce mode runs with default settings and one principal issues eight concurrent `read_note` calls, each taking 10 ms
- **THEN** all eight SHALL complete successfully, and no `slot_timeout` SHALL be returned or logged

#### Scenario: Cancellation races with a grant
- **WHEN** a queued call is cancelled before or immediately after a grant
- **THEN** no active permit or queued entry SHALL be leaked or released twice

### Requirement: Configured MCP pool demand includes refusal logging
The server SHALL validate auth, per-class tool and usage-writer connection demand against its single defined 15-connection pool capacity with explicit headroom.

Tool demand SHALL be the maximum of Σ multiplier(class) × admitted(class) over every admissible mix, where each class is capped by its ceiling and the total is capped by `tools`. Per-class multipliers SHALL be defined once in `pool_budget.py` as write 2 and every other class 1. The validated sum SHALL be `auth + tool_demand + writers + 4 ≤ 15`.

A real-PostgreSQL test SHALL invoke every registered tool through the tracking decorator and assert that its per-task checkout peak is at most its class multiplier.

#### Scenario: Configuration exceeds the pool budget
- **WHEN** auth + tool demand + writers + headroom exceeds pool capacity, for example auth 2, tools 8 with write 3, writers 1
- **THEN** startup SHALL refuse and name every term of the sum

#### Scenario: Defaults leave a spare connection
- **WHEN** the default settings are validated
- **THEN** the computed demand SHALL be 2 + 7 + 1 + 4 = 14

#### Scenario: A tool overlaps more sessions than its class allows
- **WHEN** a registered tool's measured per-task checkout peak exceeds its class multiplier
- **THEN** the per-tool checkout test SHALL fail and name the tool, the class and the measured peak

#### Scenario: Refusal logging is flooded
- **WHEN** enforced usage writers or their bounded waiting registry are full
- **THEN** no more than the writer ceiling SHALL check out logging connections
- **AND** coalesced refused-row counts SHALL survive unsuccessful writes or cancellation
- **AND** a completed tool result SHALL not fail because its usage row could not be written

#### Scenario: Shared consumers use the headroom
- **WHEN** non-MCP components use the shared pool
- **THEN** operational documentation SHALL state that arithmetic headroom is not reserved capacity or a universal availability guarantee

## ADDED Requirements

### Requirement: Queue mode waits like enforcement and never refuses for capacity
The server SHALL accept `MCP_CONCURRENCY_MODE=queue`. In that mode every stage (request, auth, tool and writer) SHALL wait with enforcement's deadlines, waiter bounds and eligible-FIFO order, and SHALL admit the work with an `overrun` mark wherever enforcement would have refused for capacity.

A queue-mode overrun SHALL NOT return a refusal, SHALL NOT write a `slot_timeout` row, and SHALL consume quota exactly as any executed call does. Shutdown refusal SHALL remain a refusal in every mode.

#### Scenario: A deadline expiry admits instead of refusing
- **WHEN** a tool call in queue mode waits past its tool deadline
- **THEN** the call SHALL execute and return its normal result
- **AND** its usage row SHALL carry `concurrency_queue` with `overrun: true`, `code: slot_timeout` and the waited milliseconds

#### Scenario: A waiter overflow admits instead of refusing
- **WHEN** a request arrives in queue mode while the fingerprint waiter bound is full
- **THEN** the request SHALL proceed immediately, with no 429
- **AND** the since-boot counter for `(request, fingerprint, overrun)` SHALL increment

#### Scenario: An ordinary wait is measured
- **WHEN** a queue-mode call waits 40 ms for a light slot and is then granted
- **THEN** its row SHALL record `queue_ms` of about 40, with `concurrency_queue.overrun` false

#### Scenario: A writer overrun keeps the row
- **WHEN** a usage writer in queue mode exceeds its writer wait
- **THEN** the row SHALL still be written, and the writer overrun SHALL be counted

### Requirement: Concurrency metadata names the refusal enforcement would have returned first
The `concurrency_shadow` and `concurrency_queue` objects SHALL set `code` to the code of the earliest pipeline stage observed, in the order request, auth, tool, writer. They SHALL list that observation first and never drop it when truncating to four observations, and SHALL carry `schema: 2`.

The stage codes SHALL be `request_concurrency_limited`, `auth_concurrency_limited`, `slot_timeout` and `writer_concurrency_limited`. Neither object SHALL contain a credential, a fingerprint or a principal identity.

#### Scenario: A later writer observation does not mask tool pressure
- **WHEN** a shadow call observes tool pressure and its usage writer then observes writer pressure
- **THEN** the row's `concurrency_shadow.code` SHALL be `slot_timeout` and its first observation SHALL be the tool stage

#### Scenario: A transport observation outranks the tool
- **WHEN** a call observes fingerprint pressure at the request stage and class pressure at the tool stage
- **THEN** `code` SHALL be `request_concurrency_limited`

#### Scenario: Truncation keeps the worst observation
- **WHEN** a call carries five distinct observations whose earliest stage arrived last
- **THEN** exactly four SHALL be stored and the earliest-stage observation SHALL be among them, listed first

### Requirement: A tool's slot lease ends when its body ends
The tracking decorator SHALL release a tool's slot lease once the body has returned or raised, and SHALL release it before writing the usage row, recording a tool failure, or emitting tail security events. The release SHALL come after every value the usage row needs has been captured.

#### Scenario: A slow usage write does not extend the slot
- **WHEN** a light-class call's body returns while its usage writer is blocked, in enforce mode with light ceiling 1
- **THEN** a second light-class call SHALL be granted the slot before the first call's usage write completes

#### Scenario: A body exception releases before its failure row
- **WHEN** a tool body raises
- **THEN** the slot SHALL be released before `_record_tool_failure` runs, and the original exception SHALL still reach the caller

#### Scenario: Cancellation still releases exactly once
- **WHEN** a call is cancelled during its body
- **THEN** its lease SHALL be released exactly once, whether by the post-body release or by the backstop in `finally`

### Requirement: Concurrency configuration is validated as a coherent hierarchy
Startup SHALL refuse any concurrency configuration that breaks a hierarchy or coherence rule, and the error SHALL name the settings involved.

The rules SHALL be:
- `fingerprint ≤ requests`;
- `principal ≤ tenant ≤ tools`;
- every class ceiling ≤ `tools`;
- `principal_waiters ≤ tenant_waiters ≤ waiters`;
- `fingerprint_waiters ≤ request_waiters`;
- `fingerprint ≥ principal + principal_waiters`;
- the pool budget.

`MCP_CONCURRENCY_OTHER` SHALL be ignored with one WARNING when it is set to `1`, and SHALL be refused at startup, with a message naming `MCP_CONCURRENCY_LIGHT` and `MCP_CONCURRENCY_SCAN`, when it is set to any other value.

#### Scenario: The transport envelope cannot refuse what the tool stage would queue
- **WHEN** settings configure principal 3, principal waiters 16 and fingerprint 4
- **THEN** startup SHALL refuse and name `MCP_CONCURRENCY_FINGERPRINT`, `MCP_CONCURRENCY_PRINCIPAL` and `MCP_CONCURRENCY_PRINCIPAL_WAITERS`

#### Scenario: A pinned legacy default is tolerated
- **WHEN** the environment carries `MCP_CONCURRENCY_OTHER=1` with otherwise valid settings
- **THEN** startup SHALL succeed and log one WARNING naming the replacement settings

#### Scenario: A tuned legacy value is refused
- **WHEN** the environment carries `MCP_CONCURRENCY_OTHER=3`
- **THEN** startup SHALL refuse with a message naming `MCP_CONCURRENCY_LIGHT` and `MCP_CONCURRENCY_SCAN`

### Requirement: A readiness evaluator applies fixed numeric criteria for each mode change
The server SHALL provide one pure evaluator. For a target mode of `queue` or `enforce`, it SHALL return PASS, FAIL or INSUFFICIENT_DATA for each fixed criterion, together with the numbers behind each verdict. It SHALL consider only usage rows written with `schema: 2` metadata, or rows carrying `queue_ms` in queue or enforce mode.

For target `queue`, the window SHALL be at least 3 days and 300 executed tool calls, and the criteria SHALL be:
- **Q1:** tool-pressured calls ≤ 10 % of executed calls;
- **Q2:** transport-pressured requests ≤ 5 % of executed calls;
- **Q3:** zero pool timeouts.

For target `enforce`, the window SHALL be at least 7 days and 1,000 executed tool calls of queue-mode data, and each criterion SHALL hold over the whole window and over its last 72 hours:
- **E1:** tool overruns ≤ max(1, 0.1 % of executed calls);
- **E2:** zero transport overruns;
- **E3:** zero writer overruns;
- **E4:** tool `queue_ms` p99 ≤ 500 ms;
- **E5:** maximum tool wait ≤ 50 % of the tool deadline, and maximum transport wait ≤ 50 % of the transport deadline;
- **E6:** zero pool timeouts, and a pool checkout high-water of at most 13.

Criteria that need process counters SHALL report INSUFFICIENT_DATA when process uptime is under 72 hours. The same evaluator SHALL back both the panel verdict and `make concurrency-report`.

#### Scenario: A thin window is not a pass
- **WHEN** the enforce evaluation covers 5 days, or fewer than 1,000 executed calls
- **THEN** every enforce criterion SHALL report INSUFFICIENT_DATA and the overall verdict SHALL not be PASS

#### Scenario: E1 boundary
- **WHEN** a qualifying window holds 2,000 executed calls and 2 tool overruns
- **THEN** E1 SHALL PASS; with 3 overruns E1 SHALL FAIL

#### Scenario: Recent regression fails even when the window average passes
- **WHEN** a 7-day window meets E4 overall but its last 72 hours have a tool `queue_ms` p99 of 800 ms
- **THEN** E4 SHALL FAIL

#### Scenario: Legacy shadow rows are ignored
- **WHEN** the window contains pre-change rows whose `concurrency_shadow` has no `schema`
- **THEN** the evaluator SHALL exclude them from every count and denominator

#### Scenario: A pool timeout blocks the flip
- **WHEN** the window holds one `tool_exception` row with `error_type` `TimeoutError`
- **THEN** Q3 and E6 SHALL FAIL
