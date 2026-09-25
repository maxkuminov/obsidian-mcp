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

Transport waiting SHALL be disconnect-aware.

From the first wait until admission ends, one watcher SHALL be the only caller of ASGI `receive`. It SHALL keep calling `receive` after a body message with `more_body: false`, and SHALL stop only when admission ends, when a disconnect arrives, or when the replay budget is exhausted.

On `http.disconnect` the middleware SHALL release every waiter count, registry reference and lease held for that request, and SHALL run no credential query.

Every message the watcher consumes SHALL be kept and replayed to the downstream application in order and unchanged, before the application's own calls are delegated to `receive`. At handoff the watcher SHALL end without cancelling an in-flight `receive`; a message whose `receive` had already completed SHALL be included in the replay, and a still-pending `receive` SHALL pass to the replay wrapper, which delivers its result exactly once after the replayed messages.

Consumed messages SHALL count against a process-wide replay budget, `MCP_CONCURRENCY_REPLAY_BUDGET_BYTES` (default 32 MiB). When the budget is exhausted, the watcher SHALL stop consuming, SHALL NOT discard anything already consumed, and the request SHALL remain bounded by the transport deadline.

After the auth permit is granted and before its session opens, the middleware SHALL skip authentication for a request whose client has disconnected.

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

#### Scenario: A real disconnect frees a transport waiter
- **WHEN** `receive` delivers `http.disconnect` while a request waits for the envelope or for the auth permit, and the request task is not cancelled
- **THEN** every waiter count, registry reference and lease captured for that request SHALL be released exactly once and promptly
- **AND** no credential query and no usage row SHALL be issued for that request

#### Scenario: A disconnect after a complete body is still seen
- **WHEN** a waiting request's complete body arrives in one message with `more_body: false`, and the client then disconnects while the request still waits for the envelope or for the auth permit
- **THEN** the waiter SHALL be released promptly, with no credential query and no usage row

#### Scenario: Body messages read while waiting are preserved
- **WHEN** a request's body arrives as one or several `http.request` messages while it waits, and it is then admitted
- **THEN** the downstream application SHALL receive exactly those messages, in order, with byte-identical bodies and `more_body` flags, and SHALL then read any later messages from the real `receive`

#### Scenario: An oversized message is kept losslessly
- **WHEN** a waiting request receives one body message larger than the whole replay budget, or fragments whose total crosses the budget
- **THEN** the message that crossed the budget SHALL be kept, the watcher SHALL stop consuming, and the downstream application SHALL receive the full body byte-exact
- **AND** the request SHALL be released no later than the transport deadline

#### Scenario: A cancelled transport waiter leaks nothing
- **WHEN** the request task is cancelled while it waits at either transport stage
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

A queue-mode overrun SHALL change only the concurrency outcome. The call SHALL then proceed through the remaining non-concurrency gates, whose outcomes and classification SHALL stay authoritative. An overrun SHALL NOT return a refusal and SHALL NOT write a `slot_timeout` row. Quota SHALL be consumed only if the call passes the daily-quota gate. The `concurrency_queue` object SHALL annotate a row and SHALL NOT alter its executed or pre-body classification. Shutdown refusal SHALL remain a refusal in every mode.

#### Scenario: A deadline expiry admits instead of refusing
- **WHEN** a tool call in queue mode waits past its tool deadline and then passes the daily-quota gate
- **THEN** the call SHALL execute and return its normal result
- **AND** its usage row SHALL carry `concurrency_queue` with `overrun: true`, `code: slot_timeout` and the waited milliseconds

#### Scenario: A waiter overflow admits instead of refusing
- **WHEN** a request arrives in queue mode while the fingerprint waiter bound is full
- **THEN** the request SHALL proceed immediately, with no 429
- **AND** the request SHALL be counted once in the `transport_overrun` windowed counter

#### Scenario: An overrun followed by a quota refusal stays a quota refusal
- **WHEN** a queue-mode call overruns its tool deadline and the daily-quota gate then refuses it
- **THEN** the caller SHALL receive the existing `over_quota` refusal, and the body SHALL NOT run
- **AND** no quota SHALL be consumed, and the row SHALL be classified as a pre-body refusal by the existing predicate even though it also carries `concurrency_queue.overrun: true`

#### Scenario: An ordinary wait is measured and sets no code
- **WHEN** a queue-mode call waits 40 ms for a light slot and is then granted
- **THEN** its row SHALL record `queue_ms` of about 40, with `concurrency_queue.overrun` false and `concurrency_queue.code` null

#### Scenario: A writer overrun keeps the row
- **WHEN** a usage writer in queue mode exceeds its writer wait
- **THEN** the row SHALL still be written, and the writer overrun SHALL be counted

### Requirement: Concurrency metadata orders observations by stage and derives its code by mode
The `concurrency_shadow` and `concurrency_queue` objects SHALL list observations in pipeline-stage order (request, auth, tool, writer), SHALL keep at most four observations, and SHALL carry `schema: 2`. They SHALL NOT contain a credential, a fingerprint or a principal identity.

In `concurrency_shadow`, every observation is a zero-wait capacity miss. Its `code` SHALL be the code of the earliest-stage observation, and truncation SHALL always keep that observation.

In `concurrency_queue`, `code` SHALL be the code of the earliest-stage observation marked `overrun: true`, and SHALL be `null` when no observation overran. Truncation SHALL always keep that overrun observation.

The stage codes SHALL be `request_concurrency_limited`, `auth_concurrency_limited`, `slot_timeout` and `writer_concurrency_limited`.

#### Scenario: A later writer observation does not mask tool pressure
- **WHEN** a shadow call observes tool pressure and its usage writer then observes writer pressure
- **THEN** the row's `concurrency_shadow.code` SHALL be `slot_timeout` and its first observation SHALL be the tool stage

#### Scenario: A transport observation outranks the tool in shadow
- **WHEN** a shadow call observes fingerprint pressure at the request stage and class pressure at the tool stage
- **THEN** `code` SHALL be `request_concurrency_limited`

#### Scenario: An earlier wait does not set the queue code
- **WHEN** a queue-mode request waits 40 ms for the auth permit without overrunning, then overruns its tool deadline
- **THEN** `concurrency_queue.code` SHALL be `slot_timeout`, and the auth wait SHALL be listed first with `overrun: false`

#### Scenario: Truncation keeps the deciding observation
- **WHEN** a call carries five distinct observations whose deciding observation (the earliest in shadow, the earliest overrun in queue) would fall outside the first four
- **THEN** exactly four SHALL be stored and the deciding observation SHALL be among them

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

### Requirement: Every tracked usage row carries concurrency provenance
Whenever the concurrency mode is not `off`, every `usage_logs` row written by the tracking decorator or by its refusal, coalescer or failure paths SHALL carry `params.concurrency` with `v: 2`, the current `mode`, and an `epoch`. The `epoch` SHALL be the first 12 hex digits of the SHA-256 of the canonical JSON of every `mcp_concurrency_*` setting except `mode`, together with the class-mapping version. Startup SHALL log the effective concurrency settings and the epoch once at INFO.

#### Scenario: An unpressured call is still marked
- **WHEN** a tool call runs in queue mode with no wait and no overrun
- **THEN** its row SHALL carry `params.concurrency` with `v: 2`, `mode: queue` and the current epoch, and SHALL carry no `concurrency_queue` object

#### Scenario: Refusal and coalesced rows are marked
- **WHEN** a `rate_limited` refusal row, a coalesced `slot_timeout` row, an `over_quota` row or a `tool_exception` row is written
- **THEN** each SHALL carry the same `params.concurrency` provenance

#### Scenario: A mode change keeps the epoch and a limit change moves it
- **WHEN** only `MCP_CONCURRENCY_MODE` changes between two boots
- **THEN** the epoch SHALL be identical; when any other `MCP_CONCURRENCY_*` value changes, the epoch SHALL differ

### Requirement: Transport, writer and pool outcomes are recorded in durable event-time counters with a run watermark
The server SHALL record, in a `concurrency_counters` table keyed by event-time minute bucket, epoch, mode and a metric from a closed set:
- request totals;
- per-request transport outcomes;
- writer overruns and refusals;
- pool checkout timeouts;
- the pool checkout high-water mark;
- the maximum transport wait.

Each observation SHALL be attributed to the minute in which it occurred, and that attribution SHALL be preserved when a failed flush is retried.

Each process run SHALL have a `run_id` and a `concurrency_runs` row carrying its epoch, mode, start time, completed-interval watermark (`completed_through`), clean-shutdown flag and lossy flag. The flush SHALL run every 60 s, whatever the request volume, as one bounded transaction that drains the accumulator at time t, upserts the drained buckets and sets `completed_through` to t rounded down to the minute. The shutdown flush, before the engine is disposed, SHALL set `completed_through` to t exactly and mark the run cleanly shut down. Data older than 35 days SHALL be pruned.

Each request SHALL contribute at most once to each transport metric: its worst transport outcome, recorded when the request completes. Tool-stage figures SHALL come only from usage rows, and transport, writer and pool figures only from these counters.

#### Scenario: Pressure at two transport stages counts once
- **WHEN** one shadow request observes pressure at both the request stage and the auth stage
- **THEN** `transport_pressured` SHALL increase by exactly 1 for that request

#### Scenario: A request that never reaches a tool is still counted
- **WHEN** an `initialize` request overruns the transport deadline in queue mode
- **THEN** `requests` and `transport_overrun` SHALL each increase by 1, and no usage row SHALL be written for it

#### Scenario: A flood does not amplify into writes
- **WHEN** 10,000 requests arrive within one flush interval
- **THEN** the flush SHALL issue one bounded transaction for that interval, not one statement per request

#### Scenario: An incident keeps its event-time minute
- **WHEN** a pool checkout times out at 12:00:50 and the next flush runs at 12:01:10
- **THEN** the timeout SHALL be stored in bucket 12:00, not 12:01

#### Scenario: A failed flush keeps attribution
- **WHEN** a flush that drained a 12:00 incident fails and the next flush at 12:02 succeeds
- **THEN** the incident SHALL be stored in bucket 12:00

#### Scenario: The watermark advances with no traffic
- **WHEN** a flush interval passes with no traffic
- **THEN** the run's `completed_through` SHALL still advance

### Requirement: Pool checkout timeouts are counted at the shared acquisition boundary
The database engine SHALL count every connection-pool checkout timeout, `sqlalchemy.exc.TimeoutError` raised by pool checkout, from every consumer of the engine, and SHALL track the checked-out high-water mark. It SHALL re-raise the original exception unchanged and SHALL NOT count other exceptions named `TimeoutError`.

#### Scenario: Timeouts outside tool bodies are counted
- **WHEN** a pool checkout times out during MCP authentication, during the quota gate, in a usage writer, in a panel request, or in OAuth `/token`
- **THEN** `pool_checkout_timeout` SHALL increase by 1 for each, and each caller SHALL see the same exception as before

#### Scenario: An unrelated timeout is not a pool timeout
- **WHEN** an embedding-provider call raises `TimeoutError` or `asyncio.TimeoutError`
- **THEN** `pool_checkout_timeout` SHALL be unchanged

### Requirement: A concurrency refusal consumes no durable quota and refunds no rate token
A tool call refused at the slot gate SHALL consume no daily-quota slot. Rate-bucket tokens it spent at the earlier bucket gates SHALL remain spent and SHALL NOT be refunded. The guarantee that nothing durable is consumed by a refused call SHALL be stated as covering the durable daily quota only.

#### Scenario: A write refused for capacity
- **WHEN** a write-class call passes both token buckets and is then refused with `slot_timeout` in enforce mode
- **THEN** its daily-quota counter SHALL be unchanged
- **AND** its general-bucket and write-bucket tokens SHALL remain spent, refilling only at their configured rates

### Requirement: A readiness evaluator applies fixed numeric criteria for each mode change
The server SHALL provide one pure evaluator. For a target mode of `queue` or `enforce`, it SHALL return PASS, FAIL or INSUFFICIENT_DATA for each fixed criterion, together with the numbers behind each verdict. It SHALL use only usage rows carrying `params.concurrency.v = 2`, together with `concurrency_counters` and `concurrency_runs` rows, and SHALL exclude every other row unconditionally.

A window SHALL qualify only when all of these hold:
- All of its rows, counters and runs carry the source mode (`shadow` for target `queue`, `queue` for target `enforce`) and a single epoch.
- Its end is no later than the durable watermark, the latest `completed_through` of the qualifying runs rounded down to a whole minute. The default end SHALL be that rounded watermark.
- It is covered:
  - every instant lies within some non-lossy run's `[started_at, completed_through]`, or in a gap that follows a run with a clean-shutdown flush;
  - a gap after a run that ended without a clean-shutdown flush SHALL be uncovered, however short it is.
- Its boundaries are whole minutes, with no exception: the start is rounded up and the end rounded down, including an end that falls at a clean shutdown. A minute bucket shared by two runs SHALL count only toward windows that contain that whole minute.

A non-qualifying or uncovered window SHALL yield INSUFFICIENT_DATA for every criterion.

For target `queue`, the window SHALL be at least 3 days and 300 executed tool calls, and the criteria SHALL be:
- **Q1:** tool-pressured executed calls ≤ 10 % of executed calls (rows);
- **Q2:** `transport_pressured` ≤ 5 % of `requests` (counters);
- **Q3:** zero `pool_checkout_timeout` (counters).

For target `enforce`, the window SHALL be at least 7 days and 1,000 executed tool calls, and each criterion SHALL hold over the whole window and over its last 72 hours:
- **E1:** distinct calls with a tool-stage overrun ≤ max(1, 0.1 % of executed calls) (rows);
- **E2:** zero `transport_overrun` (counters);
- **E3:** zero `writer_overrun` (counters);
- **E4:** tool `queue_ms` p99 ≤ 500 ms (rows);
- **E5:** maximum tool `queue_ms` over every v2 row carrying one — a call later refused pre-body (such as by the daily quota) included — ≤ 50 % of the tool deadline (rows), and maximum `transport_wait_max_ms` ≤ 50 % of the transport deadline (counters);
- **E6:** zero `pool_checkout_timeout`, and maximum `pool_high_water` ≤ 13 (counters).

The same evaluator SHALL back both the panel verdict and `make concurrency-report`.

#### Scenario: A thin window is not a pass
- **WHEN** the enforce evaluation covers 5 days, or fewer than 1,000 executed calls
- **THEN** every enforce criterion SHALL report INSUFFICIENT_DATA and the overall verdict SHALL not be PASS

#### Scenario: E1 boundary
- **WHEN** a qualifying window holds 2,000 executed calls and 2 calls with a tool overrun
- **THEN** E1 SHALL PASS; with 3 such calls E1 SHALL FAIL

#### Scenario: Recent regression fails even when the window average passes
- **WHEN** a 7-day qualifying window meets E4 overall but its last 72 hours have a tool `queue_ms` p99 of 800 ms
- **THEN** E4 SHALL FAIL

#### Scenario: Legacy rows are ignored, including unpressured ones
- **WHEN** the window contains pre-change rows carrying `queue_ms: 0` but no `params.concurrency`
- **THEN** the evaluator SHALL exclude them from every count and denominator

#### Scenario: A mixed-mode window does not qualify
- **WHEN** a 7-day window contains queue-mode rows and, on day 2, shadow-mode rows or rows from a different epoch
- **THEN** the enforce criteria SHALL report INSUFFICIENT_DATA, and the evaluator SHALL report the start of the latest qualifying sub-window

#### Scenario: A quick restart after a hard kill is not a pass
- **WHEN** a run flushes at 12:00, records a transport overrun at 12:00:20, is killed without a shutdown flush at 12:00:40, and a new run with the same settings starts at 12:01, a gap under 180 s
- **THEN** every enforce criterion over a window containing 12:00–12:01, including E2, E3 and E6, SHALL report INSUFFICIENT_DATA rather than PASS

#### Scenario: A clean recreate stays covered
- **WHEN** a run shuts down with its shutdown flush and a new run with the same epoch and mode starts 40 s later
- **THEN** the interval between them SHALL count as covered

#### Scenario: An unflushed tail is not certified
- **WHEN** a pool checkout times out after the latest `completed_through`, and a report is requested with an explicit end after that watermark
- **THEN** the criteria SHALL report INSUFFICIENT_DATA; with the default end, the evaluation SHALL stop at the watermark and report that end

#### Scenario: Two runs in one minute do not leak into the earlier window
- **WHEN** run A, in queue mode, shuts down cleanly at 12:00:20; run B, with the same epoch and mode, starts at 12:00:40 and records a pool checkout timeout at 12:00:50; and an evaluation is requested with an end at run A's shutdown
- **THEN** the evaluation end SHALL be rounded down to 12:00, and bucket 12:00 and run B's timeout SHALL NOT count toward that window
- **AND** a window ending at 12:01 or later SHALL include the timeout and fail Q3 or E6

#### Scenario: Incidents at the window boundaries
- **WHEN** a pool timeout occurs at 11:59:50, just before a window starting at 12:00, and another at 12:59:50, inside a window ending at 13:00 whose watermark is 13:00
- **THEN** the first SHALL be outside the window and the second SHALL make Q3 FAIL

#### Scenario: No double counting across sources
- **WHEN** 300 executed calls include 12 requests that were auth-pressured
- **THEN** Q2 SHALL use `transport_pressured` over `requests` from the counters only, and those 12 requests SHALL be counted once

#### Scenario: A pool timeout blocks the flip
- **WHEN** a qualifying window holds one `pool_checkout_timeout`, from any consumer
- **THEN** Q3 and E6 SHALL FAIL
