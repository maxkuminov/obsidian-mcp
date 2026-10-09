## Context

Issue #322: near-limit `/mcp` bodies can OOM the single worker before tool
admission. The relevant facts:

- **Per-request limit.** `Settings.mcp_max_request_body_bytes` (≈ 61 MiB) is
  passed to `FastMCP(max_request_body_size=)`. The SDK's
  `RequestBodyLimitMiddleware` checks a declared `Content-Length` and then
  accumulates the whole body (`bytearray` → `bytes`) before `json.loads` and
  Pydantic validation. Every tool gate in `_tracked` (L2 → L6) runs after all
  of that.
- **Amplification.** The ASVS reproduction measured 57,096 KiB → 467,284 KiB
  RSS for one 60 MiB envelope: ≈ 410 MiB of transient memory, about 6.7× the
  body. The highest peak came from `_url_host` (`urlsplit(str(url))`), which
  `named_params()` runs on the full argument.
- **uvicorn applies flow control.** A body that nobody `receive()`s costs at
  most `HIGH_WATER_LIMIT` (64 KiB) plus one socket read per connection (the
  #188 L11 derivation). Bytes therefore enter process memory only when someone
  calls `receive`. A gate placed before the first `receive` really does bound
  what the process accepts.
- **Who calls `receive` before the app.** Only `ReceiveWatch`, while a
  transport concurrency stage waits. It is bounded by the process-wide replay
  budget (32 MiB), and in the default `shadow` mode it never runs, because
  shadow never waits.
- **Single worker.** `--workers 1` is part of the contract, so in-process
  accounting is complete. There is no second process to coordinate with.
- **Container limit.** The compose file sets 2 GiB. The production pod limit
  on k3s is set in the infra repo and may differ, which is why the budget is
  derived from the cgroup rather than hard-coded.

## Goals / Non-Goals

**Goals**

- Bound aggregate in-flight `/mcp` body memory **before** the process reads
  enough bytes to exceed it.
- Keep every supported large write working. The per-request limit does not
  change, and a configuration that could not admit one maximum-size body is
  refused at boot.
- Ordinary small requests must never queue behind large ones.
- Release on every exit path.
- Add the `import_from_url` URL cap and remove the canonicalisation peak.

**Non-Goals**

- Lowering `mcp_max_request_body_bytes`, or streaming JSON parsing (the SDK
  owns the parse).
- Reducing the amplification factor itself. That would mean patching the SDK,
  and the budget makes the factor a parameter, not a vulnerability.
- `/transfer/upload`, which streams to disk under its own semaphore.
- Per-principal fairness within the large lane (L4).

## Decisions

### D1 — A separate, always-on guard, not a fifth concurrency mode

The concurrency controller is a **tuning** control. It defaults to `shadow`
because a false refusal of a legitimate agent call breaks the live path, and
each step to `enforce` is earned with evidence. The body budget is a
**memory-safety** bound: running it in shadow would leave the bug in place. It
is therefore its own module (`src/services/body_budget.py`), it enforces in
every `MCP_CONCURRENCY_MODE` including `off`, and it has **no off switch**.
Every override moves only in the safe direction (D2): the multiplier cannot
go below its measured floor, the fraction cannot eat the fixed headroom, and
an explicit budget above the safe allocation of a readable cgroup limit is
refused at startup. No setting re-opens the OOM. The operator's levers are a
larger container or a smaller `MAX_FILE_WRITE_BYTES`.

**Owner decisions (2026-10-09, after the Codex spec review).** The guard is
always on (no shadow mode, no off switch), and startup refusal of a budget too
small for one maximum write stays. Both were re-confirmed after the review.

Why not fold it into the request envelope: the envelope counts requests, has
a 2–5 s deadline sized for authentication, and is mode-gated. A body wait is
a different quantity with a different natural deadline.

Why not refuse every concurrent large body (a 1-slot semaphore): with 128 MiB
of capacity, one maximum write plus any number of small requests fit. Bytes
are the honest unit. A count would either over-admit (61 MiB × N) or
under-admit (two 30 MiB writes are fine).

### D2 — Derivation: memory budget ÷ multiplier = raw capacity

```
memory_budget  = MCP_BODY_MEMORY_BUDGET_BYTES            if set
               = floor(MCP_BODY_MEMORY_FRACTION × limit) if a cgroup limit is readable
               = 1 GiB                                   otherwise (logged WARNING)
capacity       = memory_budget // MCP_BODY_MEMORY_MULTIPLIER    (raw body bytes)
small_lane     = capacity // 8
large_lane     = capacity − small_lane
```

- `limit` is read once, at web-application startup, as the **process's own**
  effective cgroup limit (Codex spec review, finding 5):
  - **cgroup v2.** The process's cgroup path comes from the `0::` line of
    `/proc/self/cgroup`, and the v2 mount point and the mount's root from the
    `cgroup2` entry of `/proc/self/mountinfo` (default `/sys/fs/cgroup` when
    mountinfo is unreadable). The limit is the **minimum finite**
    `memory.max` across that directory and each ancestor up to the mount
    point. This covers a private cgroup namespace (path `/`, the container's
    own root) and a host or nested namespace (a systemd service several levels
    below the mount), where a smaller ancestor limit applies.
  - **cgroup v1**, only when v2 yields no finite limit (pure v1 or hybrid):
    `memory.limit_in_bytes` at the memory controller's mount point (from
    mountinfo, default `/sys/fs/cgroup/memory`). The controller root only: in
    a Docker or Kubernetes v1 container that root is the container's own
    cgroup. A v1 process nested below its mount's root is not resolved (L10).
  - `max`, an unreadable or unparseable file, or a value ≥ 2⁶⁰ (v1's
    "unlimited") counts as no limit. Nothing readable means the 1 GiB
    fallback, logged WARNING.
- `MCP_BODY_MEMORY_FRACTION` defaults to 0.5, range **[0.1, 0.5]**. The other
  half covers the baseline (~57 MiB at idle, 378 MiB observed peak), the replay
  budget (32 MiB), indexer and embedding work, and responses. The ceiling is
  0.5 because above it the fixed headroom is no longer guaranteed (finding 1).
- `MCP_BODY_MEMORY_MULTIPLIER` defaults to **8**, integer range **[8, 32]**.
  The measured 6.7× plus margin, and 8 is also the **floor**: a smaller
  multiplier admits more raw bytes than the measured amplification allows
  (finding 1: fraction 0.8 with multiplier 4 on 2 GiB admitted five 61 MiB
  bodies, which is the reproduced OOM). A guard test re-measures it (task
  4.2), and a failing measurement means raising the default and the floor,
  not loosening the test.
- **Safe allocation.** With a readable limit `L`,
  `safe(L) = min(floor(0.5 × L), L − (384 MiB + MCP_CONCURRENCY_REPLAY_BUDGET_BYTES))`.
  384 MiB is the fixed non-body headroom (the 378 MiB observed peak baseline,
  rounded up). Startup refuses a memory budget above `safe(L)`, whether it
  came from the fraction or from `MCP_BODY_MEMORY_BUDGET_BYTES`. An explicit
  budget is taken as given only when no limit is readable, and the startup
  line is then WARNING, as for the fallback.
- On 2 GiB: memory budget 1 GiB, capacity 128 MiB, small lane 16 MiB, large
  lane 112 MiB. That admits one 61 MiB body plus 51 MiB of other large
  traffic, with 16 MiB always available to small requests.

**Boot check (fail closed).** The web application's lifespan refuses to
start, with an error naming `MCP_BODY_MEMORY_BUDGET_BYTES`,
`MCP_BODY_MEMORY_FRACTION`, `MCP_BODY_MEMORY_MULTIPLIER` and
`MAX_FILE_WRITE_BYTES`, when any of these holds:

- `large_lane < mcp_max_request_body_bytes`: a supported maximum write could
  never be admitted;
- `small_lane < 1 MiB`: one small envelope could not be admitted;
- the memory budget exceeds `safe(L)` for a readable limit `L`.

**Where the check runs** (finding 4). Derivation and the boot check run in
`body_budget.configure()`, called from the FastAPI lifespan after the sandbox
short-circuit and before the first database check. They never run in
`Settings` construction or on `import src.config`. The reason: the Kubernetes
`alembic` initContainer imports `src.config.settings` under its own 1 GiB
limit, which derives a 56 MiB large lane. A check in `Settings` would fail
every migration before the 2 GiB app container is launched. The `Settings`
fields carry only their static ranges. Sandbox mode bypasses the middleware,
so it skips `configure()` as it skips the other startup guards.

With the defaults this needs a cgroup limit of at least ≈ 1.1 GiB. A smaller
container must either lower `MAX_FILE_WRITE_BYTES` (which shrinks the body
limit) or set the budget explicitly. Refusing to boot is preferred to booting
a server whose documented write cap silently cannot work (see the open
question in the report).

The derived figures go into one INFO line at startup: source
(`setting|cgroup|fallback`), memory budget, multiplier, capacity and both
lanes. The line is WARNING when no cgroup limit was readable (source
`fallback`, or source `setting` with no limit to check it against).

### D3 — Accounting: declared length up front, the maximum when unknown

- **`Content-Length` present and valid.** Reserve exactly that many bytes.
  A request whose declared length is above `mcp_max_request_body_bytes` gets
  an immediate **413**, the SDK's own response shape, with no reservation and
  no wait. A length of 0 reserves nothing and skips the budget.
- **Absent or unparseable (chunked).** Reserve `mcp_max_request_body_bytes`
  in the large lane. MCP clients send `Content-Length` on JSON POSTs, so this
  is rare and is costed at the worst case rather than guessed.
- **Streamed counting** is a receive wrapper on what the app reads. It sums
  the body bytes delivered. If the sum would exceed the reservation, it
  delivers `http.disconnect` to the app instead of the message: the client is
  treated as gone, the SDK stops reading, and nothing beyond the reservation
  is ever buffered. uvicorn already enforces `Content-Length` framing, and the
  SDK 413s a chunked body above the limit, so this branch is defence in
  depth. A unit test with a lying fake `receive` pins it.
- **Why not incremental growth for chunked bodies:** growing a reservation
  while a body is half-read means a request can block holding part of the
  budget. Two such requests can each hold half and wait for the other, which
  is deadlock or livelock. Reserving the full amount up front makes admission
  atomic and order-independent.
- **POST only** (Codex spec review, finding 2). Admission applies only to
  the method whose body the SDK buffers. `RequestBodyLimitMiddleware` (SDK
  1.29) buffers `POST` alone and passes every other method straight through,
  so GET (the SSE stream), DELETE and anything else bypass the budget. Costing
  a bodyless GET at the chunked worst case would reserve about 61 MiB for the
  lifetime of an SSE stream, and one such stream would stop a maximum write
  from ever fitting.

### D4 — Placement: after authentication, before the app

In `APIKeyMiddleware.__call__`:

```
auth-failure budget → bearer check → request envelope → auth permit
  → watch.stop() → _authenticate() → [413 check → body reservation] → self.app
```

- **After authentication**, so an unauthenticated caller can never hold or
  wait for budget. A failed credential is answered without the body being
  read, as today.
- **After the auth permit is released**, so a body wait holds no DB
  connection. It still holds its request-envelope lease, which already lasts
  for the whole downstream request.
- **Before `self.app`.** Nothing has called `receive` for this request except
  a concurrency watcher, whose bytes the replay budget already accounts for.
  When the reservation is granted, those replayed bytes become part of the
  reserved body, so they are counted once in each budget for the time they sit
  in each.
- `MCP_SANDBOX_MODE` short-circuits the middleware before all of this and
  stays exempt. Sandbox is never production, and `Settings` refuses a sandbox
  boot with a public hostname.

### D5 — Lanes and ordering: small requests are never starved

- **Small** means a declared `Content-Length` ≤ 1 MiB
  (`_MCP_ENVELOPE_ALLOWANCE_BYTES`). It is admitted when the small lane has
  room. Otherwise it may **borrow** free large-lane bytes, but only while the
  large FIFO is empty, so that a stream of small requests cannot keep slipping
  in front of a waiting large one. Otherwise it waits in the small FIFO.
- **Large** means anything else, including chunked. It uses only the large
  lane, in strict FIFO order with no barging: a waiting 61 MiB head is not
  overtaken by a later 2 MiB request, so a maximum-size write cannot starve.
  The cost is head-of-line blocking among large requests (L3).
- A release is credited to the lane the bytes came from, a borrowed
  reservation to the large lane. Each release then re-runs admission for both
  FIFOs, small first.
- Both FIFOs share one waiter bound, `MCP_BODY_BUDGET_WAITERS` (8, range
  [1, 256]). Overflow refuses at once. Waiting is cheap (uvicorn holds the
  body in the socket), but the bound keeps the wait queue from becoming a
  connection-holding amplifier.

### D6 — Wait, bounded and disconnect-aware; then a transport 429

- **Deadline.** `MCP_BODY_BUDGET_WAIT_SECONDS`, default 15, range [0, 60],
  started when the reservation is first requested and independent of the
  concurrency transport deadline (2–5 s, sized for authentication). A large
  holder releases when its tool finishes, and a 25 MB write takes seconds, so
  15 s lets a short burst of large writes serialise rather than fail. 0 means
  never wait.
- **Disconnect awareness** reuses the #188 mechanism unchanged. A fresh
  `ReceiveWatch` wraps the first watch's `downstream()` (that composition
  preserves the replay order), starts only if the reservation actually
  suspends, and is stopped at the handoff. Bytes it reads while waiting draw on
  the shared replay budget, so L8 of #188 (deadline-bounded once the replay
  budget is exhausted) carries over unchanged. A disconnect ends the wait with
  no response and nothing reserved.
- **Teardown of both watchers** (Codex spec review, finding 3). The outer
  `finally` aborts the body-budget watcher and then the transport watcher, on
  every exit including a disconnect or a cancellation before the handoff. The
  body watcher goes first because its in-flight `receive` is the transport
  watcher's `downstream()`. `abort()` releases the bytes each one charged to
  the replay budget and cancels any still-pending `receive` task, so neither
  replay usage nor a pending task outlives the request.
- **Refusal.** HTTP **429**,
  `{"error": "MCP request body memory budget is unavailable", "code": "body_memory", "scope": "<small|large>", "limit": <lane capacity in bytes>}`,
  with `Retry-After: 2`. This is the same keys and status as the concurrency
  transport 429, so a client that handles one handles both.
  - 429 rather than 503: the condition is load the caller can back off from,
    and the precedent on this transport is 429.
  - Not a 413: the body is within the limit and a retry can succeed.
- **The `MCP-REFUSAL` contract does not apply.** No tool call has been parsed
  (the body has not been read), so there is nothing to answer in band. The
  same reasoning as for the failed-auth budget and the concurrency 429:
  fabricating a tool result for an unparsed request is worse than an honest
  HTTP error. Nothing durable is consumed. `_tracked` never ran, so there is no
  rate token, no quota slot and no usage row.
- **Telemetry.** `mcp_concurrency_pressure` with `reason="body:memory"`,
  outcome `refused` (deadline, waiter overflow) or `waited` (a grant after
  more than 100 ms, the existing `WAITED_EVENT_THRESHOLD_MS`). The event name
  and the outcome vocabulary already exist, so the catalogue gains only a
  `reason` value. These outcomes are **not** fed to `concurrency.counters()`:
  body pressure must not count as `transport_refused` in the enforce-readiness
  evidence (D8 of #188), because it is not concurrency pressure.

### D7 — Release on every exit

The grant returns a lease with an idempotent `release()`. `__call__`'s
existing `finally` calls it. That one site covers:

- normal completion;
- SDK 400 (JSON or JSON-RPC validation) and 413, which return from `self.app`;
- a tool exception, which surfaces as a JSON-RPC error or raises out of
  `self.app`;
- client disconnect while running, since the SDK returns;
- cancellation (`CancelledError` passes through the `finally`).

A waiter that is cancelled, disconnected or timed out removes itself from its
FIFO, and if it had been granted in the same tick it was cancelled, it hands
the bytes back. This is the `_admit` orphan-release pattern, reused.

**Reservation lifetime equals the downstream call.** In `stateless_http` mode
the SDK runs the request's server task inside a task group that exits before
`handle_request` returns, so every parse copy is unreachable by then. Python
frees it by refcount. Whether the allocator returns pages to the OS is L2.

### D8 — `import_from_url` URL cap, and the logging transform

- `MAX_IMPORT_URL_CHARS = 8192` in `src/config.py`, next to
  `MAX_SEARCH_QUERY_CHARS`. Presigned S3 or GCS URLs with session tokens reach
  2–4 KB, and 8 KiB clears them while staying 7,000× below the body limit.
- Applied as `arg_char_caps={"url": MAX_IMPORT_URL_CHARS}` on
  `import_from_url_impl`, the existing L5b screen. The result is the existing
  `argument_too_long` refusal: pre-body, carrying the `MCP-REFUSAL` line, a
  usage row, not coalesced. The screen keeps its place in `_tracked`'s
  existing gate order (rate buckets, vault-root admission, encoding screen,
  argument-length screen, slot, quota). It is **not** claimed to run before
  vault-root resolution, which precedes it (Codex spec review, finding 7).
- `_url_host` returns the fixed string `"<over-long>"` for a value longer than
  `MAX_IMPORT_URL_CHARS`, **before** `str()` or `urlsplit`. Without this, the
  refusal path would still run the transform (`named_params()` runs on every
  row) and keep the peak the cap was meant to remove.
- The docstring in `server.py` states the cap.

## Risks / Trade-offs

- **A false 429 on a legitimate large write** under concurrent large traffic.
  Mitigation: a 15 s wait, a FIFO with no barging, and on the reference
  deployment one maximum write plus 51 MiB alongside it. Before this change the
  same load could restart the process for every tenant.
- **The multiplier is measured, not proven.** An SDK or Pydantic upgrade can
  raise it. Mitigation: the guard measurement (task 4.2) runs under
  `make test-integration`, and the fraction leaves half the container as
  slack. By owner decision it does **not** run in CI (finding 6): it is gated
  on its own opt-in variable, `BODY_BUDGET_RSS_TESTS=1`, which the Makefile
  target sets and CI's `tests` job does not. A peak-RSS bound on a shared CI
  runner measures the runner.
- **Boot refusal on small containers.** That is the intent (D2), but it could
  surprise an operator on upgrade. The error names the knobs, and the
  `.env.example` comment gives the minimum.

## Accepted limitations

- **L1** The budget bounds body-derived memory by a measured multiplier, not
  by accounting each allocation. A code path that amplifies more than
  `MCP_BODY_MEMORY_MULTIPLIER` is only partly bounded until the multiplier is
  re-measured.
- **L2** Released memory may not return to the OS (pymalloc arenas, glibc
  heap fragmentation). RSS can stay high after a burst. That memory is reused
  by the next request, so the peak stays bounded, but the RSS shown in
  `docker stats` will not fall back to baseline.
- **L3** Large requests are strict FIFO, so a waiting maximum-size write
  blocks smaller large writes behind it (head-of-line). This is the price of
  never starving the maximum write.
- **L4** No per-principal share of the large lane. One principal can keep the
  large lane busy, bounded by its write bucket (60/min, burst 15) for write
  tools and by the general bucket otherwise. Small traffic from every tenant
  is unaffected (small lane).
- **L5** A chunked request is costed at the full per-request limit, so a small
  chunked body is admitted as if it were large.
- **L6** Bytes the transport watcher reads while a request waits sit in the
  replay budget (≤ ≈ 62 MiB, #188 L11), not in this one. Worst case:
  `memory_budget + replay budget + per-connection uvicorn buffers`, with the
  last of these bounded by connection count, as before.
- **L7** The derived budget reads the cgroup once at startup. A live
  `docker update` of the memory limit takes effect at the next restart.
- **L8** Sandbox mode is exempt, because it bypasses `APIKeyMiddleware`
  entirely.
- **L9** The response side is not budgeted. Read responses are already capped
  (`MAX_READ_RESPONSE_CHARS`, `MAX_FILE_READ_BYTES`).
- **L10** On cgroup v1 only the memory controller's mount root is read. In a
  container that root is the container's own cgroup; a v1 process nested
  below it (a v1 host running the app outside a container) is not resolved,
  and a smaller ancestor limit there is missed. cgroup v2, which every current
  distribution and k3s default to, resolves the process's own path and its
  ancestors.
- **L11** Methods other than POST bypass the budget, because the SDK buffers
  no body for them. A future SDK that buffered another method's body would
  need this re-checked.

## Alternatives rejected

- **Lowering the global body limit.** It breaks the documented 25 MB
  `write_file`, and the issue rules it out.
- **Folding the budget into concurrency `enforce`.** Mode-gated and default
  shadow, so the bug stays open in the default configuration (D1).
- **Incremental reservation while streaming.** Partial-hold deadlock (D3).
- **Refusing instead of waiting.** Two agents' 20 MB writes in the same second
  would see a 429 where a few seconds' wait suffices. Waiting is cheap because
  uvicorn holds the body in the socket.
- **An in-band `MCP-REFUSAL`.** Nothing is parsed yet, and parsing is the
  memory being protected.
- **A URL cap alone.** Every other near-limit envelope still buffers (issue
  constraint).
