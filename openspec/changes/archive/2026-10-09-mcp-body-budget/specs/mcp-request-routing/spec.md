## ADDED Requirements

### Requirement: Authenticated MCP request bodies SHALL be admitted against a process-wide body-memory budget before any body byte is read by the application
`APIKeyMiddleware` SHALL, after a request has authenticated successfully and after its auth permit has been released, and before it invokes the downstream MCP application, reserve the request's body size against a single process-wide body budget, and SHALL hold that reservation until the downstream ASGI call returns or raises.

The reserved size SHALL be the declared `Content-Length` when it is present and a valid non-negative integer, and `mcp_max_request_body_bytes` otherwise. A declared length of zero SHALL reserve nothing. A declared length greater than `mcp_max_request_body_bytes` SHALL be answered with HTTP 413 without reserving, without waiting and without invoking the application. The application SHALL NOT be given a body message before the reservation is granted. Bytes a transport concurrency watcher read while the request waited are already accounted for by the replay budget, and are replayed unchanged.

The receive callable handed to the application SHALL count delivered body bytes. If delivering a message would bring the total above the reservation, it SHALL deliver `http.disconnect` in place of that message, and no further body message.

Admission SHALL apply only to `POST` requests, the only method whose body the MCP SDK buffers. A `GET` (including a long-lived SSE stream), a `DELETE` or any other method SHALL bypass the budget: it SHALL neither reserve nor wait, whatever its headers.

The budget SHALL be enforced in every `MCP_CONCURRENCY_MODE` (`off`, `shadow`, `queue`, `enforce`) and SHALL have no setting that disables it. A request refused or abandoned by an earlier gate (failed-authentication budget, missing bearer, invalid credential, concurrency refusal, disconnect) SHALL never reserve or wait for body budget. Sandbox mode, which bypasses the middleware, is exempt.

#### Scenario: The application reads nothing before the grant
- **WHEN** an authenticated request declares a `Content-Length` of 10 MiB and the budget has no room, and the downstream application is a probe that records every `receive` call
- **THEN** the probe SHALL observe no `receive` call until the reservation is granted
- **AND** once it is granted, the probe SHALL receive the complete body byte-identical to what the client sent

#### Scenario: An oversized declaration is refused at once
- **WHEN** an authenticated request declares a `Content-Length` of `mcp_max_request_body_bytes + 1`
- **THEN** the response SHALL be HTTP 413
- **AND** the budget's reserved total SHALL be unchanged, and no waiter SHALL be enqueued

#### Scenario: A chunked body is costed at the per-request limit
- **WHEN** an authenticated POST carries no `Content-Length`
- **THEN** it SHALL reserve exactly `mcp_max_request_body_bytes` from the large lane

#### Scenario: More bytes than reserved are never delivered
- **WHEN** the downstream receive yields body messages totalling more than the reserved size (a fake `receive` declaring 1,000 bytes and sending 2,000)
- **THEN** the application SHALL receive `http.disconnect` in place of the message that crosses the reservation, and no body message after it

#### Scenario: The budget enforces in shadow and off
- **WHEN** `MCP_CONCURRENCY_MODE` is `shadow`, and separately `off`, and the budget is full
- **THEN** a further large request SHALL wait and then be refused exactly as in `enforce`

#### Scenario: A long-lived GET stream holds no budget
- **WHEN** an authenticated `GET` with no `Content-Length` opens a stream that the downstream application keeps open, and while it is open an authenticated `POST` declaring `mcp_max_request_body_bytes` arrives
- **THEN** the `GET` SHALL have reserved nothing and SHALL not be a waiter
- **AND** the `POST` SHALL be admitted at once from the large lane

#### Scenario: An unauthenticated body never holds budget
- **WHEN** a request carrying an invalid bearer token declares a `Content-Length` larger than the budget's free capacity
- **THEN** it SHALL receive the authentication 401 with no wait
- **AND** the budget's reserved total and waiter count SHALL be unchanged

### Requirement: The body budget SHALL be derived from the container memory limit and SHALL refuse at startup a configuration that cannot admit one supported maximum body
The memory budget SHALL be `MCP_BODY_MEMORY_BUDGET_BYTES` when set. Otherwise it SHALL be `floor(MCP_BODY_MEMORY_FRACTION × L)`, where `L` is the process's own effective cgroup memory limit, read once at web-application startup. When no finite limit is readable, it SHALL be 1 GiB. The raw-byte capacity SHALL be `memory_budget // MCP_BODY_MEMORY_MULTIPLIER`. The small lane SHALL be `capacity // 8`, and the large lane the remainder.

`L` SHALL be resolved as follows. For cgroup v2, the process's cgroup path SHALL be taken from the `0::` line of `/proc/self/cgroup` and the v2 mount point and mount root from the `cgroup2` entry of `/proc/self/mountinfo` (default `/sys/fs/cgroup`), and `L` SHALL be the minimum finite `memory.max` across that cgroup's directory and every ancestor up to the mount point. Only when cgroup v2 yields no finite limit, `L` SHALL be the cgroup v1 `memory.limit_in_bytes` at the memory controller's mount point (from mountinfo, default `/sys/fs/cgroup/memory`); a nested v1 path is not resolved. The value `max`, an unreadable or unparseable file, and a value of at least 2⁶⁰ SHALL each count as no limit.

`MCP_BODY_MEMORY_FRACTION` SHALL default to 0.5 within [0.1, 0.5]. `MCP_BODY_MEMORY_MULTIPLIER` SHALL default to 8 within [8, 32]; 8 is the measured-amplification floor. `MCP_BODY_MEMORY_BUDGET_BYTES` SHALL be unset by default and, when set, at least 64 MiB. When `L` is readable, the safe allocation SHALL be `min(floor(0.5 × L), L − (384 MiB + MCP_CONCURRENCY_REPLAY_BUDGET_BYTES))`.

Startup of the web application SHALL fail with a configuration error naming `MCP_BODY_MEMORY_BUDGET_BYTES`, `MCP_BODY_MEMORY_FRACTION`, `MCP_BODY_MEMORY_MULTIPLIER` and `MAX_FILE_WRITE_BYTES` when the large lane is smaller than `mcp_max_request_body_bytes`, when the small lane is smaller than 1 MiB, or when `L` is readable and the memory budget exceeds the safe allocation. The derivation and this check SHALL run only at web-application (lifespan) startup, never in `Settings` construction or on import of `src.config`, so that a process that only imports the settings (the migration init container) is unaffected by its own memory limit. Startup SHALL log the resolved derivation once, naming its source (`setting`, `cgroup` or `fallback`), the memory budget, the multiplier, the capacity and both lanes. The line SHALL be WARNING when no cgroup limit was readable, and INFO otherwise.

#### Scenario: The reference deployment derives 128 MiB
- **WHEN** the cgroup v2 limit reads `2147483648` and no body-budget setting is configured
- **THEN** the memory budget SHALL be 1 GiB, the capacity 128 MiB, the small lane 16 MiB and the large lane 112 MiB
- **AND** the startup line SHALL name source `cgroup`

#### Scenario: No limit falls back to 1 GiB with a warning
- **WHEN** every `memory.max` on the process's v2 path reads `max` and no v1 memory controller is mounted
- **THEN** the memory budget SHALL be 1 GiB and the startup line SHALL be WARNING with source `fallback`

#### Scenario: A nested v2 cgroup takes the smallest ancestor limit
- **WHEN** `/proc/self/cgroup` names `/a/b/c`, `memory.max` reads `max` at `c`, `1073741824` at `b` and `4294967296` at `a`
- **THEN** `L` SHALL be 1 GiB

#### Scenario: A private cgroup namespace reads the mount root
- **WHEN** `/proc/self/cgroup` reads `0::/` and `memory.max` at the v2 mount point reads `2147483648`
- **THEN** `L` SHALL be 2 GiB

#### Scenario: A cgroup v1 container reads the controller root
- **WHEN** no cgroup2 mount exists and the v1 memory controller's mount point holds `memory.limit_in_bytes` of `2147483648`
- **THEN** `L` SHALL be 2 GiB

#### Scenario: A container too small for one maximum write refuses to boot
- **WHEN** the cgroup limit is 512 MiB with default settings and `MAX_FILE_WRITE_BYTES` at its default
- **THEN** web-application startup SHALL fail with an error naming all four settings

#### Scenario: Importing the settings under a small limit does not fail
- **WHEN** the cgroup limit is 512 MiB and a process imports `src.config` and constructs `Settings` (as the migration init container does)
- **THEN** no error SHALL be raised and no cgroup file SHALL have been read

#### Scenario: An unsafe override is refused
- **WHEN** the cgroup limit is 2 GiB and `MCP_BODY_MEMORY_BUDGET_BYTES` is 1.5 GiB, or `MCP_BODY_MEMORY_MULTIPLIER` is 4, or `MCP_BODY_MEMORY_FRACTION` is 0.8
- **THEN** the explicit budget SHALL fail the startup check, and the multiplier and the fraction SHALL fail settings validation as out of range

#### Scenario: An explicit budget overrides the derivation
- **WHEN** `MCP_BODY_MEMORY_BUDGET_BYTES=1610612736`, the multiplier is 8, and the cgroup limit is 4 GiB
- **THEN** the capacity SHALL be 192 MiB and the startup line SHALL name source `setting`

### Requirement: The body budget SHALL keep a small-request lane that large requests cannot consume
A request whose declared `Content-Length` is at most 1 MiB SHALL be admitted from the small lane when it has room. When the small lane is full, it SHALL be admitted from free large-lane bytes only while no large request is waiting, and otherwise SHALL wait in the small-lane queue. Any other request SHALL be admitted only from the large lane, in strict arrival order, and a later large request SHALL NOT be admitted while an earlier one waits. A release SHALL be credited to the lane its bytes came from and SHALL re-run admission for the small queue and then the large queue.

#### Scenario: Small requests pass while the large lane is full
- **WHEN** large-lane reservations hold the whole large lane and a large request is waiting
- **THEN** a 4 KiB request SHALL be admitted immediately from the small lane

#### Scenario: Small requests cannot starve a waiting large request
- **WHEN** a large request is waiting for large-lane bytes and the small lane is full
- **THEN** a further small request SHALL wait in the small queue rather than borrow large-lane bytes

#### Scenario: No barging among large requests
- **WHEN** a 60 MiB request is waiting at the head of the large queue and a 2 MiB request arrives while 10 MiB of the large lane is free
- **THEN** the 2 MiB request SHALL wait behind the 60 MiB request
- **AND** when enough bytes are released, the 60 MiB request SHALL be admitted first

### Requirement: A request that does not fit SHALL wait a bounded, disconnect-aware interval and then receive a transport 429
A request whose reservation cannot be granted SHALL wait for at most `MCP_BODY_BUDGET_WAIT_SECONDS` (default 15, range [0, 60]), measured from its first reservation attempt. The total number of waiters across both lanes SHALL be at most `MCP_BODY_BUDGET_WAITERS` (default 8, range [1, 256]), and a request arriving when the bound is full SHALL be refused without waiting. While waiting, the request SHALL hold no database connection and no auth permit, and its wait SHALL be disconnect-aware through the existing receive-watch mechanism, replaying every consumed message intact. A client disconnect SHALL end the wait with no response sent and nothing reserved. On every exit from the middleware, including a disconnect or a cancellation before the handoff, both the transport watcher and the body-budget watcher SHALL be torn down: every byte they charged to the replay budget SHALL be released, and no `receive` task they started SHALL remain pending.

Deadline expiry or waiter overflow SHALL return HTTP 429 with header `Retry-After: 2` and the JSON body `{"error": "MCP request body memory budget is unavailable", "code": "body_memory", "scope": <"small" or "large">, "limit": <that lane's capacity in bytes>}`. This refusal is a transport refusal outside the in-band `MCP-REFUSAL` contract. It SHALL consume no rate-limit token and no daily-quota slot, and SHALL write no `usage_logs` row. A refusal SHALL emit `mcp_concurrency_pressure` with `reason` `body:memory` and outcome `refused`. A grant after waiting more than 100 ms SHALL emit the same event with outcome `waited`. Neither SHALL be recorded in the concurrency durable counters.

#### Scenario: Several near-limit requests are queued and refused within the budget
- **WHEN** the large lane is configured to hold exactly two 20 MiB bodies, the downstream application holds each request until released, the wait is 1 s and the waiter bound is 2, and five authenticated requests each declaring 20 MiB arrive together
- **THEN** two SHALL be admitted, two SHALL wait, and one SHALL receive the 429 at once
- **AND** the budget's reserved total SHALL never exceed the large lane's capacity
- **AND** when one admitted request completes, exactly one waiter SHALL be admitted
- **AND** the remaining waiter SHALL receive the 429 with `code` `body_memory` when its 1 s deadline expires

#### Scenario: A refused request consumes nothing durable
- **WHEN** a request is refused with `code` `body_memory`
- **THEN** no general or write bucket token SHALL have been taken for its principal, no `quota_counters` increment SHALL have occurred, and no `usage_logs` row SHALL exist for it

#### Scenario: A disconnect releases the waiter
- **WHEN** a waiting request's client sends `http.disconnect`
- **THEN** the waiter SHALL leave its queue within one event-loop iteration, no response SHALL be sent, and the waiter count SHALL decrease by one

#### Scenario: Watchers leave nothing behind
- **WHEN** a request waiting for body budget has had body bytes read by its watcher, and its client disconnects, and separately its task is cancelled, before the handoff
- **THEN** the replay budget's used bytes SHALL return to their value before the request
- **AND** no `receive` task started by either watcher SHALL remain pending

#### Scenario: A zero wait refuses at once
- **WHEN** `MCP_BODY_BUDGET_WAIT_SECONDS=0` and a large request does not fit
- **THEN** it SHALL receive the 429 without suspending

### Requirement: A body reservation SHALL be released on every exit from the downstream call
The reservation SHALL be released exactly once when the downstream ASGI call returns normally, returns after the SDK answered 400 or 413, raises, or is cancelled, and when the client disconnects at any point. A waiter that is cancelled, times out or disconnects SHALL be removed from its queue, and SHALL return any grant that completed concurrently with that exit. After every request has finished, the reserved total and the waiter count SHALL both be zero.

#### Scenario: Validation failure releases
- **WHEN** an admitted request's body is not valid JSON and the SDK answers 400
- **THEN** the reserved total SHALL return to its value before the request

#### Scenario: A raising application releases
- **WHEN** the downstream application raises after reading the body
- **THEN** the reservation SHALL be released and a waiting request that now fits SHALL be admitted

#### Scenario: Cancellation releases
- **WHEN** the task running an admitted request is cancelled while the application runs, and separately while the request waits for budget
- **THEN** in both cases the reserved total and the waiter count SHALL return to their prior values

#### Scenario: A burst leaves nothing held
- **WHEN** fifty requests of mixed sizes complete, fail validation, disconnect or are refused
- **THEN** the reserved total SHALL be zero and both queues SHALL be empty

### Requirement: Concurrent near-limit requests SHALL NOT exhaust process memory on the real stack
Against the full application stack, the server process SHALL remain alive while concurrent maximum-size requests exceed the body budget. Its peak resident memory SHALL stay within its pre-burst baseline plus the memory budget plus the replay budget plus 128 MiB, and a supported maximum-size `write_file` SHALL succeed once capacity frees. A guard measurement SHALL establish that one maximum-size envelope's peak resident growth divided by its body size is at most `MCP_BODY_MEMORY_MULTIPLIER`. These real-stack measurements SHALL run under `make test-integration` behind a dedicated opt-in variable (`BODY_BUDGET_RSS_TESTS=1`) that the CI test job does not set.

#### Scenario: Six maximum-size writes against a 2 GiB-derived budget
- **WHEN** the server runs with a 1 GiB memory budget and six authenticated clients simultaneously send `write_file` envelopes of `mcp_max_request_body_bytes − 4 KiB`
- **THEN** the server process SHALL still be serving `/health` afterwards
- **AND** its peak RSS SHALL not exceed the baseline + 1 GiB + 32 MiB + 128 MiB
- **AND** every request SHALL either succeed or receive the `body_memory` 429, and at least one SHALL succeed

#### Scenario: The multiplier covers the measured amplification
- **WHEN** one `import_from_url`-shaped envelope and one `write_file` envelope, each near `mcp_max_request_body_bytes`, are sent sequentially to a fresh server process
- **THEN** for each, the peak RSS growth divided by the body length SHALL be at most `MCP_BODY_MEMORY_MULTIPLIER`
