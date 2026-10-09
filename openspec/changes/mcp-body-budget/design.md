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
The rollback lever is size: set `MCP_BODY_MEMORY_BUDGET_BYTES` larger. A
budget larger than the container is still allowed, but the startup line says
so (D2).

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

- `limit` is read once at startup from cgroup v2 `/sys/fs/cgroup/memory.max`,
  falling back to v1 `/sys/fs/cgroup/memory/memory.limit_in_bytes`. A value of
  `max`, an unreadable file, or a value ≥ 2⁶⁰ (v1's "unlimited") counts as no
  limit.
- `MCP_BODY_MEMORY_FRACTION` defaults to 0.5, range [0.1, 0.8]. The other half
  covers the baseline (~57 MiB at idle, 378 MiB observed peak), the replay
  budget (32 MiB), indexer and embedding work, and responses.
- `MCP_BODY_MEMORY_MULTIPLIER` defaults to **8**, integer range [4, 32]. The
  measured 6.7× plus margin. A guard test re-measures it (task 4.2), and a
  failing measurement means raising the default, not loosening the test.
- On 2 GiB: memory budget 1 GiB, capacity 128 MiB, small lane 16 MiB, large
  lane 112 MiB. That admits one 61 MiB body plus 51 MiB of other large
  traffic, with 16 MiB always available to small requests.

**Boot check (fail closed).** Startup raises a configuration error naming
`MCP_BODY_MEMORY_BUDGET_BYTES`, `MCP_BODY_MEMORY_FRACTION`,
`MCP_BODY_MEMORY_MULTIPLIER` and `MAX_FILE_WRITE_BYTES` when either of these
holds:

- `large_lane < mcp_max_request_body_bytes`: a supported maximum write could
  never be admitted;
- `small_lane < 1 MiB`: one small envelope could not be admitted.

With the defaults this needs a cgroup limit of at least ≈ 1.1 GiB. A smaller
container must either lower `MAX_FILE_WRITE_BYTES` (which shrinks the body
limit) or set the budget explicitly. Refusing to boot is preferred to booting
a server whose documented write cap silently cannot work (see the open
question in the report).

The derived figures go into one INFO line at startup: source
(`setting|cgroup|fallback`), memory budget, multiplier, capacity and both
lanes. The line is WARNING when the source is `fallback`, or when the memory
budget exceeds the cgroup limit.

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
- The method is not consulted. Any request carrying a body is charged. A GET
  or DELETE without a body has length 0 and passes through.

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
  usage row, not coalesced.
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
  raise it. Mitigation: the guard measurement (task 4.2) runs in CI, and the
  fraction leaves half the container as slack.
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
