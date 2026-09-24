## Context

#261 (archived `2026-09-06-mcp-concurrency-slots`) built one process-local
controller in `src/services/concurrency.py`. It has three admission points:

- `Controller.request()` counts global and bearer-fingerprint occupancy for the
  full ASGI request lifetime.
- `Controller.auth()` is a permit around the middleware's own DB session.
- `Controller.tool()` admits the class, tenant, principal and global dimensions
  atomically. Only this stage can wait, and only in `enforce`.

Separately, `Controller.writer()` admits usage writers. The pool arithmetic
(pool 5 + 10, multiplier 2, headroom 4) lives in `src/services/pool_budget.py`
and is checked by `Settings._validate_concurrency`.

The 2026-09-21 shadow evidence on #188 names five blockers (proposal §Why).
**The host changed after that evidence was gathered.** PGDATA moved to NVMe on
2026-09-23, and commits went from about 40–100 ms to about 2.5 ms.

- Auth sessions and light tool bodies (`read_note` p50 6 ms) now hold their
  permits for single-digit milliseconds. A small bounded wait therefore absorbs
  a large burst: a 2 s transport deadline at auth = 2 and about 5 ms per session
  drains about 800 queued authentications. Queueing is cheap and refusing is
  not.
- The old shadow percentages overstate current pressure. **No pre-change row is
  evidence for any flip.** The readiness evaluator excludes legacy rows
  unconditionally (D8).
- The pool is still 15 connections shared with the indexer, the panel, OAuth
  and transfer. Shorter holds make it less likely to saturate. They do not
  change the arithmetic that bounds MCP's share of it.

## Goals / Non-Goals

**Goals**

- `enforce` is safe to switch on in production. A legitimate agent pattern,
  such as a batch of 6–10 parallel reads or two agents on one tenant, waits
  milliseconds and is never refused.
- Every ceiling can be tuned within the budget, and the single validated
  configuration is gone.
- An operator can see pressure, waits and overruns on the panel. The same code
  gives a PASS / FAIL / INSUFFICIENT_DATA verdict computed only from **durable,
  covered** evidence.
- A measured rehearsal (`queue`) runs before any capacity refusal is possible.
- Rollback at every step is a one-line `.env` change and a recreate.

**Non-Goals**

- A universal pool-availability guarantee. Headroom is still not reserved
  against `/token`, the panel or other consumers.
- Per-tenant reservation or a starvation SLA.
- Raising the pool size.
- Automatic promotion between modes.
- Any change to the token buckets, the quota, the failed-auth budget or the
  `MCP-REFUSAL` contract.
- Refunding rate-bucket tokens (D9).

## Blocker → decision

| # | Blocker (#188, 2026-09-21) | Decision |
| --- | --- | --- |
| 1 | `Controller.auth()` never waits; the fingerprint stage also refused calls. | **D1.** The request and auth stages share one bounded transport deadline (`MCP_CONCURRENCY_TRANSPORT_WAIT_SECONDS`, default 2, max 5) with bounded waiter counts and eligible-FIFO order. Waiting is **disconnect-aware**: the middleware watches ASGI `receive` for `http.disconnect` while it waits, and it owns `receive` exclusively until admission ends. Every message it reads is kept losslessly and replayed to the app. A process-wide replay budget stops further consumption, but never drops anything. The separate auth permit is **kept**. Defaults rise to requests 64 and fingerprint 20. A coherence rule requires `fingerprint ≥ principal + principal_waiters`. |
| 2a | The pool budget is exactly saturated, and the class-sum rule forces `other = 1`. | **D2.** Class ceilings become independent (each ≤ tools). The multiplier becomes per class (write 2, the others 1) and is pinned by a real-PG checkout-peak test over every tool. Budget: `auth + tool_demand + writers + 4 ≤ 15`. Defaults give 14. |
| 2b | `other` holds 15 of 25 tools. | **D3.** Split into `light` (9 tools, ceiling 4) and `scan` (6 tools, ceiling 2). `MCP_CONCURRENCY_OTHER=1` is ignored with a WARNING; any other value is refused. |
| 3 | `shadow_metadata` reports the last code. | **D4.** Observations are kept in pipeline-stage order and the earliest is never truncated. **Code selection is separate from ordering.** In shadow, every observation is a zero-wait would-refuse, so `code` is the earliest stage's. In queue, `code` comes from **overrun** observations only (the earliest overrun), and is `null` when nothing overran. |
| 4 | No panel surface; the data could not answer the flip question. | **D6 + D8.** A panel section plus one evaluator. Every row that `_tracked` writes carries an unconditional provenance object `{v, mode, epoch}`. Request-level transport outcomes, writer overruns and pool checkout timeouts go to **durable event-time windowed counters** plus a per-run table with a completed-interval watermark and a clean-shutdown flag (migration 028). Numerators count distinct requests from exactly one source each, and uncovered windows return INSUFFICIENT_DATA. |
| 5 | The tool lease is held through telemetry, so a writer wait extends the slot hold. | **D5 (revised after spec review).** **The lease stays held through telemetry, as today.** Early release was rejected: it removes the backpressure that currently limits lost audit rows for completed writes (Codex SR1-1). The coupling is bounded instead: the writer wait is at most 0.25 s, and the NVMe move made writer holds ~ms. It is also **measured**: queue mode records the actual `queue_ms` that includes any writer-extended hold, so E1/E4 see its effect before enforce is possible. |
| — | There is no evidence of how positive waits behave. | **D7.** `queue` mode waits like enforce and admits with an `overrun` mark wherever enforce would refuse **for capacity**. Every later gate and outcome stays authoritative. |
| — | The write token is spent on a concurrency refusal (Codex SR1-7). | **D9.** Existing policy is kept and documented: the non-consumption guarantee covers the **durable daily quota only**. Rate-bucket tokens spent by the buckets-first gates stay spent, and there is no refund machinery. |
| — | There is no reliable pool-timeout signal (Codex SR1-5). | **D10.** A pool subclass in `src/database.py` counts `sqlalchemy.exc.TimeoutError` raised by checkout for **every** engine consumer, plus the checkout high-water, into the durable counters. |

## Decisions

### D1 — Transport stages wait, within one per-request deadline, and notice disconnects

**The deadline.**

- The request stage and the auth stage share one monotonic deadline. It starts
  when the middleware first asks for the request lease.
- Waiter bounds:
  - request stage: `MCP_CONCURRENCY_REQUEST_WAITERS` (global, 64) and
    `MCP_CONCURRENCY_FINGERPRINT_WAITERS` (16);
  - auth stage: `MCP_CONCURRENCY_AUTH_WAITERS` (32).
- A request waiting for the envelope holds nothing but a registry reference. A
  request waiting for the auth permit holds its request lease. Neither holds a
  DB connection.
- The auth permit still encloses only the middleware's own session.
- In `enforce`, deadline expiry or waiter overflow returns the existing
  transport 429 shape (`code`, `scope`, `limit`, `Retry-After: 1`), outside the
  in-band refusal contract.

**Disconnects.** Uvicorn reports a client disconnect through ASGI `receive`. It
does not cancel the middleware's coroutine, so cancellation alone never frees a
transport waiter.

**Exclusive ownership.** From the moment a request first waits until its
admission ends (granted, refused, overrun or disconnected), one watcher task is
the only caller of the real `receive()`.

- The watcher loops on `receive()` until admission ends. It does **not** stop
  when a body message has `more_body: false`. After the last body message, the
  next `receive()` blocks until the client disconnects, which is exactly what
  the watcher is waiting for.
- `http.disconnect` sets the request's `disconnected` event and ends the loop.
  - The controller removes the waiter, and the middleware releases the
    registry references and any request lease at once.
  - No credential query runs and no response is sent.
- Every consumed message, `http.request` or `http.disconnect`, is appended to a
  per-request replay list **as received**. Nothing is dropped, split, merged or
  reordered.

**Handoff.** The handoff never cancels a `receive()` (impl-R1-1). Each real
`receive()` runs in its own task, which the watcher awaits through a shield.
When admission ends, the middleware cancels only the watcher loop and awaits it.
The `receive` it holds may be wrapped (the security-header `BaseHTTPMiddleware`
in `src/main.py`), and a wrapper can suspend after taking a message from the
server and before returning it, so cancelling that call would lose the message.

- If the pending `receive()` had already completed and been appended, its
  message is in the list: the append and the clearing of the pending call are
  one step, with no await between.
- Otherwise the in-flight call passes to the replay wrapper as it is.
- The downstream app then gets a wrapper `receive` that yields the replay list
  in order, then that in-flight call's own result (a message or a disconnect,
  exactly once), and afterwards delegates to the real `receive`. From there the
  app is the only caller.
- If the app never calls `receive` again (a GET), request teardown cancels the
  in-flight call after the response is complete, when nobody needs its result.

**Disconnect before auth.** After the auth grant, and before opening its
session, the middleware checks `disconnected`. A request whose client left runs
no credential query.

**Memory: lossless and honestly bounded.** The transport limit on an `/mcp`
body is `Settings.mcp_max_request_body_bytes`, **61 MiB** at the defaults. It is
enforced downstream by the SDK, not before this middleware. So "body limit ×
waiter bound" is 96 × 61 MiB ≈ 5.7 GiB, which is not an acceptable bound. The
SDK's 4 MiB default does not apply; the server overrides it.

The watcher therefore reads under a **process-wide replay budget**,
`MCP_CONCURRENCY_REPLAY_BUDGET_BYTES` (default 32 MiB), instead of a
per-request cap.

- Before each `receive()`, the watcher checks the budget. If it is exhausted,
  the watcher **stops calling `receive()`** and keeps everything already
  consumed, including a message that crossed the budget.
  - That request stays deadline-bounded (≤ 5 s) rather than disconnect-aware.
  - Its unread bytes remain in the transport's flow-controlled buffers, where
    they would have been anyway.
- Nothing already consumed is ever discarded, so replay is always byte-exact.
- The honest bound is budget + transport waiters × the largest ASGI message
  uvicorn can hand back. That message size is set by uvicorn's code (the
  image's CMD uses the default `auto` HTTP implementation, which is httptools
  with uvloop):
  - `on_body` appends every parsed chunk to `cycle.body`, and pauses reading
    only once `len(body) > HIGH_WATER_LIMIT`, which is 65,536 bytes
    (`flow_control.py:7`, `httptools_impl.py:316–318`). `receive` then returns
    **everything accumulated** (`httptools_impl.py:575`).
  - The chunk that crosses the threshold is one socket read. Both asyncio's
    selector transport (`max_size = 256 KiB`) and uvloop read at most 256 KiB
    at a time.
  - h11 has the same flow-control structure.
  - So one message is at most HIGH_WATER_LIMIT + one read ≈ 64 KiB + 256 KiB
    = **320 KiB**.
  - That gives 32 MiB + 96 × 320 KiB ≈ **62 MiB** process-wide.
  - Up to 320 KiB per request already sits in uvicorn's own `cycle.body`
    whether or not this watcher exists. The watcher moves it rather than
    duplicating it.
- The budget is released when the replay list has been yielded to the app, or
  when the request ends.

**Tests.** A real `http.disconnect` is delivered through `receive`, never
`task.cancel()`, at both transport stages, for:

- no body;
- a complete body (`more_body: false`) followed by a disconnect;
- a fragmented body followed by a disconnect.

They assert immediate release of the waiter, the references and the lease, with
no credential query and no usage row. Replay tests send a single oversized
message (larger than the budget), and a fragmented body whose fragments cross
the budget. The bytes must arrive downstream exactly, and the budget-exhausted
request must become deadline-bounded.

**Worst-case added latency for a caller** is transport wait + tool wait +
writer wait (2 + 5 + 0.25 s at the defaults). The writer wait is part of the
telemetry tail that `_tracked` awaits before returning, as it is today.

### D2 — Pool budget with independent class ceilings and per-class multipliers

`pool_budget.py` exports:

- `POOL_SIZE = 5`, `POOL_OVERFLOW = 10`, `POOL_CAPACITY = 15`;
- `MCP_POOL_HEADROOM = 4`;
- `CLASS_CONNECTIONS = {"write": 2, "embedding": 1, "vector": 1, "scan": 1, "light": 1}`;
- `tool_demand(tools, caps)`, which fills slots highest multiplier first. That
  greedy fill is the maximum of `Σ m_c·n_c` subject to `n_c ≤ cap_c` and
  `Σ n_c ≤ tools`.

The validator refuses `auth + tool_demand + writers + headroom > 15` and names
every term.

The multiplier can drop to 1 for non-write classes because the inspected paths
are sequential:

- the quota admission commits and releases its connection before the body;
- the auth session is closed before the tool runs;
- the usage write happens after the body, under the writer permit, whose
  connections are budgeted in `writers`.

A real-PG test invokes **every registered tool** through `_tracked` and asserts
each tool's per-task checkout peak ≤ `CLASS_CONNECTIONS[class]`. The per-task
meter counts the tool body's and the quota gate's checkouts. The writer's
checkout is counted under `writers` because it runs under the writer permit. A
tool that measures higher either raises its class's multiplier in this change
or is reported back.

Dropping the class-sum rule does not weaken the pool bound. The global `tools`
counter already bounds total admitted tools, and admission is atomic.
Independent class ceilings bound class **shares** (for example embedding ≤ 1
protects the provider).

### D3 — Class set

| Class | Tools | Default ceiling | Multiplier |
| --- | --- | --- | --- |
| embedding | `semantic_search` | 1 | 1 |
| vector | `find_related` | 1 | 1 |
| write | `create_note`, `edit_note`, `move_note`, `delete_note`, `set_frontmatter`, `write_file`, `delete_file`, `import_from_url` | 1 | 2 |
| scan | `keyword_search`, `list_notes`, `get_tags`, `get_neighborhood`, `find_orphans`, `list_files` | 2 | 1 |
| light | `read_note`, `read_file`, `get_recent`, `get_vault_guide`, `get_backlinks`, `get_links`, `request_upload`, `check_upload`, `request_download` | 4 | 1 |

Other defaults:

- tools 6;
- tenant 4, principal 3;
- waiters: principal 16, tenant 32, global 64;
- requests 64, fingerprint 20;
- tool wait 5 s, with the maximum raised from 5 to 10 s.

At these defaults pool demand is `2 + 7 + 1 + 4 = 14`.

Legacy `MCP_CONCURRENCY_OTHER`:

- The value `1` is the old `.env.example` default and expresses no intent. It
  is ignored with one WARNING.
- Any other value is refused, and the error names `MCP_CONCURRENCY_LIGHT` and
  `MCP_CONCURRENCY_SCAN`.

**Validation passing does not prove the intended settings are live.** A fully
pinned legacy `.env` can still validate: `FINGERPRINT=4 ≥ PRINCIPAL 2 +
PRINCIPAL_WAITERS 2`. So startup logs one INFO line with the effective
concurrency settings and their epoch (D8). The deploy task compares that line
with the intended block.

### D4 — Metadata ordering and code selection

Ordering is the same for both objects: deduplicate, sort by stage (request 0,
auth 1, tool 2, writer 3), and cap at 4. The earliest-stage observation is
always kept.

- **Shadow (`concurrency_shadow`).** Every observation is a zero-wait capacity
  miss, which is exactly what enforce at zero wait would refuse on. `code` is
  the earliest-stage observation's code. The object keeps `basis:
  observed_occupancy_zero_wait`, and adds `schema: 2` and `configured_wait_ms`.
- **Queue (`concurrency_queue`).** Observations include ordinary waits (`waited_ms > 0`,
  `overrun: false`) and overruns. `code` is the code of the earliest-stage
  observation with `overrun: true`, and is `null` when no observation overran.
  An ordinary wait therefore never sets a code. An auth wait followed by a tool
  overrun yields `slot_timeout`.
  - The earliest overrun is kept when truncating, even if more than four
    earlier-stage waits exist.
- `write_usage_row` merges the writer observation through the same function.

### D5 — The slot lease stays held through telemetry (unchanged)

The spec review rejected early release (Codex SR1-1):

- `write_usage_row` returns `False` after the 0.25 s writer wait, and an
  ordinary completed-call row is **not** requeued.
- Today, a tool's lease being held through its own usage write throttles how
  fast further completed writes can queue behind a slow writer.
- Early release removes that throttle. The reviewer's controller reproduction
  showed a second completed write losing its audit row that the current
  lifetime saves.
- A lost audit row for a completed destructive write is worse than a few ms of
  added slot hold.

Blocker 5 is resolved by bounding and measuring the effect instead of removing
it (table above).

### D6 — Observability: panel section and evaluator

`src/services/concurrency_readiness.py`:

- `window_stats(session, window, user_id)` is a read-only aggregation over
  `usage_logs` rows with `params->'concurrency'->>'v' = '2'`, plus
  `concurrency_counters` (D8). For each tool and class it computes:
  - executed calls, using the existing `executed_sql`;
  - tool-pressured calls (shadow rows with a tool-stage observation);
  - tool overruns (queue rows with a tool-stage `overrun: true`);
  - weighted `slot_timeout` refusals (`1 + suppressed`);
  - `queue_ms` p50, p95, p99 and max.

  From the counters it reads, per window: request totals, transport-pressured
  requests, transport-overrun requests, writer overruns, pool checkout
  timeouts, the pool high-water and coverage.
- `evaluate(stats, target)` is pure. It returns a PASS / FAIL /
  INSUFFICIENT_DATA verdict per criterion (§Rollout), with its inputs.

`/admin/performance`:

- Everyone gets windowed per-tool/class aggregates, scoped as the page scopes.
- Admins only get:
  - live in-process occupancy (`Controller.snapshot()`);
  - the windowed counters, coverage gaps, and the effective limits, epoch and
    pool demand;
  - the readiness verdict for the next mode.
- There is no inline script or handler, so the nonce CSP is untouched.

`scripts/concurrency_report.py` (`make concurrency-report DAYS=7
TARGET=queue|enforce`) runs `window_stats` and `evaluate` from the database
alone. Everything it needs is durable, so it and the panel agree. Output is a
table plus one JSON line. The exit code is 0 only when every criterion is PASS.

### D7 — `queue` mode

`MCP_CONCURRENCY_MODE = off | shadow | queue | enforce`, default `shadow`.

- In `queue`, every stage (request, auth, tool, writer) runs `_admit_wait` with
  enforcement's deadlines, waiter bounds and eligible-FIFO order.
- Where `enforce` would refuse **for capacity** (deadline expiry, waiter
  overflow, or a zero-wait miss), `queue` grants the lease and marks the
  admission `overrun`.
- **Only the concurrency outcome changes.** The call then proceeds through the
  **remaining non-concurrency gates**, and those gates keep their authority:
  - A daily-quota refusal after a slot overrun is an ordinary `over_quota`
    pre-body refusal. It consumes no quota, the body does not run, and
    `executed_sql` / `pre_body_refusal_sql` classify it exactly as today.
  - The row's `concurrency_queue` object only annotates. It never changes
    classification.
- An overrun writes no `slot_timeout` row and returns no refusal.
- Shutdown refusal stays a refusal in every mode.

Queue bounds latency, not occupancy (limitation L1).

**Configuration.** The rule "shadow requires `WAIT_SECONDS = 0`" is removed.
Shadow ignores configured waits and reports them as `configured_wait_ms`, so
changing mode is a single-line edit.

### D8 — Provenance, durable counters and coverage

**Row provenance.**

- Whenever mode ≠ `off`, every `usage_logs` row that `_tracked` or its
  refusal/coalescer/failure paths write carries
  `params.concurrency = {"v": 2, "mode": "<mode>", "epoch": "<12 hex>"}`.
  That covers executed calls, unpressured calls, every pre-body refusal
  including coalesced `rate_limited`/`slot_timeout` rows, `tool_exception`
  rows, and writer-merged rows.
- `epoch` is the first 12 hex digits of the SHA-256 of the canonical JSON of
  every `mcp_concurrency_*` setting **except** `mode`, plus the class mapping
  version.
- Legacy rows lack `params.concurrency.v` and are excluded from every readiness
  count and denominator unconditionally.

**Durable windowed counters (migration 028: tables `concurrency_counters` and `concurrency_runs`).**

`concurrency_counters`:

- Primary key: `(bucket_start timestamptz, epoch text, mode text, metric text)`.
- Columns: `count bigint`, and `max_value integer`. `max_value` is NULL except
  for the two gauge metrics, where it holds the bucket maximum.
- `metric` is from a closed set, guarded by a CHECK:
  - `requests`, one per `/mcp` request that reaches the admission path;
  - `transport_pressured` (shadow), `transport_waited`, `transport_overrun` and
    `transport_refused`, each **at most once per request**, by the request's
    worst outcome;
  - `writer_overrun`, `writer_refused` and `pool_checkout_timeout`;
  - the gauges `pool_high_water` and `transport_wait_max_ms`.
- **`bucket_start` is the event-time minute.** It is the minute in which the
  request completed, the writer was refused or overran, or the checkout timed
  out. The flush time is not used.
  - The in-process accumulator is keyed `(bucket_start, metric)`.
  - A failed flush merges its drained entries back **with their original
    keys**, so attribution survives retries.
  - The accumulator is bounded: one entry per metric per minute, with
    unflushed minutes capped at 60. Past that cap, the oldest unflushed minute
    is dropped **and the run is marked lossy** (see coverage below). Nothing
    is ever silently re-attributed.

`concurrency_runs`: one row per process run.

- Columns:
  - `run_id uuid` (primary key), minted in the lifespan;
  - `epoch`, `mode`;
  - `started_at`;
  - `completed_through timestamptz`;
  - `clean_shutdown bool`;
  - `lossy bool`.
- The flush runs every 60 s. In **one transaction** it drains the accumulator
  at time `t`, upserts the drained buckets, and sets
  `completed_through = floor_minute(t)`.
  - Every event before `t` was recorded before the drain.
  - So for this run, every bucket that ends by `floor_minute(t)` is complete
    and durable. That is the **completed-interval watermark**.
- The shutdown flush, after the refusal coalescer's `flush_all()` and before
  `engine.dispose()`, sets `completed_through = t` exactly and
  `clean_shutdown = true`.
- Pruning deletes counters and runs older than 35 days.
- The commit is synchronous. It is one statement per minute, and it does not
  join the `synchronous_commit` allow-list.

**Coverage and the watermark.**

- A run covers `[started_at, completed_through]`, unless it is `lossy`, in
  which case it covers nothing.
- A gap between consecutive runs **is covered only if the earlier run has
  `clean_shutdown = true`**. The process was not serving, so no incident could
  occur.
- A run that ended without a clean-shutdown flush (a hard kill, OOM or crash)
  leaves `(completed_through, next run's started_at)` **uncovered**. This holds
  however short the restart, because the partial bucket's buffered incidents
  may be lost.
- The evaluator's default window end is the durable watermark, **rounded down
  to a whole minute**: the latest `completed_through` over the current epoch
  and mode. An explicitly requested end beyond that gives INSUFFICIENT_DATA.
- **Every** window boundary is a whole minute. The start is rounded up and the
  end rounded down, and **clean-shutdown ends are rounded down too**. The exact
  `completed_through` of a clean shutdown serves only coverage, never as an
  evaluation end.
  - Reason: a bucket is keyed by (minute, epoch, mode), not by run. If a run
    shuts down cleanly at 12:00:20 and a same-configuration run starts at
    12:00:40, both write bucket 12:00. An exact 12:00:20 end would pull the
    second run's 12:00:50 incident into the earlier window (SR3-1).
  - Rounding down leaves that shared bucket out of the earlier window. It
    belongs only to windows that contain the whole minute.
- Event-time minute buckets therefore never straddle a boundary.
- A window is **covered** when the union of covered run intervals and covered
  gaps contains it entirely. An uncovered window gives INSUFFICIENT_DATA for
  **every** criterion, row-based and counter-based alike, because a quiet
  covered window is real evidence and an uncovered one is not. The operator
  then restarts the observation window.
- A window qualifies for a target only if every row, counter and run inside it
  carries the qualifying **mode and one epoch**. Otherwise the result is
  INSUFFICIENT_DATA, together with the latest qualifying sub-window start.

**Tool-stage figures come only from usage rows. Transport, writer and pool
figures come only from counters.** No numerator mixes the sources, so nothing is
double-counted. Usage rows are written with the call, and their `created_at`
is event time. The watermark clamp applies to them too.

Why tables and not the other options:

- They are the simplest durable form that makes Q2/E2/E3/E6 computable over
  arbitrary windows across restarts.
- Usage rows cannot carry outcomes for requests that never reach a tool, and
  #261 forbids ownerless per-request rows.
- Security-event logs are not queryable and they rotate.
- Heartbeat spacing alone cannot tell a clean recreate from a hard kill
  (SR2-3). A run table with a clean-shutdown flag and a watermark can.

### D9 — Rate tokens on a concurrency refusal

The gate order is buckets → vault → argument screens → tool slots → quota.

- A call refused at the slot gate has already spent a general-bucket token, and
  a write token for write-class tools.
- This matches existing policy for every later pre-body refusal (#162's "a tool
  that always fails would be free" reasoning, recorded under the alternatives
  in `rate-limits.md`).
- The guarantee "nothing durable is consumed by a refused call" covers the
  **durable daily quota** (`quota_counters`) only.
- Rate tokens are in-memory velocity state. They refill at their configured
  rate, and they are not refunded.

The acceptance wording in the proposal and the spec says exactly this. Queue
mode never refuses for capacity, so it never produces this case.

### D10 — Pool checkout timeouts at the shared boundary

- `src/database.py` sets `poolclass=` to a thin `AsyncAdaptedQueuePool`
  subclass that overrides `_do_get`.
  - It catches `sqlalchemy.exc.TimeoutError` (the pool's own checkout timeout,
    a distinct class from `asyncio.TimeoutError` or `TimeoutError` raised by a
    provider). It increments `pool_checkout_timeout`, then re-raises unchanged.
  - On success it updates `pool_high_water` from `checkedout()`.
- This covers every consumer of the engine: MCP auth, quota, tool bodies, usage
  writers, the panel, OAuth `/token`, transfer and the indexer.
- Tests force a real checkout timeout through each of the auth, quota,
  usage-writer, panel and `/token` paths (tiny `pool_timeout` on a test engine
  with the pool held), and check that an unrelated `TimeoutError` does not
  count.
- If the subclass hook proves unstable across SQLAlchemy versions, the fallback
  is a `checkout` wrapper on `async_session`. The implementer reports it; they
  do not switch silently.

## Rollout

All steps are operator `.env` changes followed by a recreate. Every step starts
only after the previous one's criteria pass.

**Step 0 — deploy in shadow.**

- Reconcile the deploy-dir `.env` concurrency block to the new `.env.example`
  block. Dry-run the settings against it with the new image.
- After the recreate, compare the startup INFO line with the intended settings
  (validation alone is insufficient, D3).
- `make db-check` must be clean after migration 028.
- Exercise at least one tool per class live.
- Confirm that rows carry `params.concurrency`, that a `concurrency_runs` row
  exists for the new run, and that its `completed_through` advances each
  minute.

**Step 1 — shadow → queue.** `make concurrency-report TARGET=queue`, over a
covered window of **≥ 3 days** and **≥ 300 executed calls**, all in mode
`shadow` with one epoch:

| ID | Criterion (source) | Threshold |
| --- | --- | --- |
| Q1 | Tool-pressured executed calls / executed calls (rows) | ≤ 10 % |
| Q2 | `transport_pressured` / `requests` (counters) | ≤ 5 % |
| Q3 | `pool_checkout_timeout` (counters) | 0 |

**Step 2 — queue → enforce.** A covered window of **≥ 7 days** and **≥ 1,000
executed calls**, all in mode `queue` with one epoch. Each criterion must hold
over the whole window **and** over its last 72 h:

| ID | Criterion (source) | Threshold |
| --- | --- | --- |
| E1 | Distinct calls with a tool-stage overrun (rows) | ≤ max(1, 0.1 % of executed calls) |
| E2 | `transport_overrun` requests (counters) | 0 |
| E3 | `writer_overrun` (counters) | 0 |
| E4 | Tool `queue_ms` p99 across executed calls (rows) | ≤ 500 ms |
| E5 | Maximum tool `queue_ms` / tool wait (rows); `transport_wait_max_ms` / transport wait (counters) | ≤ 0.5 each |
| E6 | `pool_checkout_timeout` (counters); maximum `pool_high_water` | 0; ≤ 13 |

**Rollback triggers**, checked daily for the first 7 days of each new mode:

- **queue → shadow:** any 24 h with tool `queue_ms` p95 > 1,000 ms, or an
  agent-side timeout attributable to queueing.
- **enforce → queue:** any `transport_refused` > 0; weighted `slot_timeout` >
  max(2, 0.2 % of calls) in any 24 h; or any `pool_checkout_timeout`.

Rollback is `MCP_CONCURRENCY_MODE=<previous>` followed by a recreate. Changing
mode alone keeps the epoch, so data already collected stays attributable.

## Alternatives rejected

- **Early lease release (D5 as first proposed).** It lost audit rows for
  completed writes under writer pressure (Codex SR1-1). Rejected by the
  supervisor, and the existing lifetime is kept.
- **Refunding rate tokens on a slot refusal.** It would need reservation
  machinery in the hottest path, and it contradicts #162's rule that failure is
  not free. See D9.
- **Drop the auth permit and rely on the pool.** The pool's own queue is a 30 s
  timeout ending in a 500, shared with the panel and `/token`.
- **Raise the pool, or derive it from the budget.** The Postgres instance is
  shared, and the pool was never the measured bottleneck.
- **A shadow counterfactual wait estimator.** Its estimate resolves after the
  row is written, and it is biased by calls enforce would not have run. `queue`
  measures the same quantity exactly.
- **Transport evidence from security-event logs, or from in-process since-boot
  counters.** Neither is queryable over arbitrary windows across restarts (D8).
- **An ownerless usage row per transport-pressured request.** #261 forbids it:
  it turns an unauthenticated flood into writes. The windowed counters write
  one statement per minute.
- **Counting pool timeouts from `tool_exception` rows.** It misses auth, quota,
  writer, panel and `/token` failures, and it confuses unrelated
  `TimeoutError`s (D10).
- **Unbounded body buffering while watching for disconnect**, and **body
  limit × waiters as the bound.** Bodies can reach 61 MiB
  (`mcp_max_request_body_bytes`), which gives about 5.7 GiB at 96 waiters. The
  process-wide replay budget with stop-consuming semantics bounds memory at
  about 62 MiB and loses nothing (D1).
- **A per-request 64 KiB cap that drops the crossing message** (first
  revision). It was lossy, because a message is sized only after `receive()`
  has consumed it (SR2-2).
- **Heartbeat-spacing coverage** (first revision). A hard kill with a quick
  restart looked covered while its buffered incidents were lost (SR2-3). It is
  replaced by run rows with a clean-shutdown flag and a watermark.
- **Flush-time buckets** (first revision). They moved incidents across window
  boundaries (SR2-4). Replaced by event-time buckets.
- **Going straight from shadow to enforce with long waits**, and **per-class
  tool waits**: unchanged from the first draft.
- **Keeping `other` and just raising its ceiling.** One tenant's scans could
  then hold every cheap-read slot.

## Accepted limitations

- **L1.** Queue mode bounds added latency, not occupancy. During an overrun,
  occupancy may exceed ceilings, as in shadow today.
- **L2.** A hard kill loses the unflushed tail of its run, up to about 60 s of
  counter increments. The interval from its last watermark to the next run's
  start is uncovered, so windows spanning it read INSUFFICIENT_DATA rather than
  a false PASS, however quick the restart.
- **L3.** Worst-case added latency is transport + tool + writer wait (7.25 s at
  the defaults).
- **L4.** The pool budget bounds only MCP's configured contribution, and
  headroom is not reserved (unchanged from #261).
- **L5.** A shared credential is a shared fingerprint and principal.
- **L6.** The thresholds are calibrated to current traffic (about 130 calls a
  day, two tenants). Re-run the report after a tenant is added.
- **L7.** The per-class multiplier is measured on fixtures. Input-size-dependent
  paths keep `write` at 2 for exactly that reason.
- **L8.** While the process-wide replay budget (32 MiB) is exhausted, a waiting
  request stops being disconnect-aware for the rest of its wait. This takes
  several concurrently waiting large write bodies. Such a request is freed at
  the transport deadline (≤ 5 s), and one whose client left may authenticate
  once. It never loses body bytes. The budget can be overshot by at most one
  message per waiter, and one message can be up to about 320 KiB (L11).
- **L11.** Worst-case replay memory is about **62 MiB** process-wide.
  - It is the budget (32 MiB) plus 96 transport waiters × one uvicorn message.
    That message is at most HIGH_WATER_LIMIT 64 KiB + one 256 KiB socket read
    ≈ 320 KiB.
  - The figure depends on uvicorn's flow-control constant and on the read
    size of the event loop. A uvicorn upgrade that changes either must
    re-derive it; S2's tests pin it by asserting the constant's value.
- **L9.** Rate tokens spent by a request refused for concurrency are not
  refunded (D9).
- **L10.** Writer-extended slot holds remain (D5). Their effect is measured by
  queue mode, not removed.

## Spec review history

| Round | Reviewer | Finding | Severity | Disposition |
| --- | --- | --- | --- | --- |
| SR1-1 | Codex | Early lease release loses completed-write audit rows under writer pressure, and the claim that writer latency leaves the caller was wrong. | MAJOR | **Accepted.** D5 dropped; the lease stays held through telemetry. Recorded under alternatives, and the latency statement corrected (D1, L3). |
| SR1-2 | Codex | The readiness predicate admits legacy `queue_ms` rows and cannot identify queue-mode coverage; unpressured rows lack provenance. | MAJOR | **Accepted.** Unconditional `params.concurrency {v, mode, epoch}` on every row; legacy rows excluded unconditionally; single mode and epoch per window (D8). |
| SR1-3 | Codex | Client disconnect is not task cancellation; transport waiters leak and can later authenticate. | MAJOR | **Accepted.** Disconnect-aware admission with a bounded, replayed body buffer; a disconnect check before the credential query; tests with real `http.disconnect` (D1, L8). |
| SR1-4 | Codex | Q2/E2 double-count rows and counters, and since-boot counters cannot cover windows with restarts. | MAJOR | **Accepted.** Migration 028 windowed counters with request-level worst-outcome-once counting, one source per numerator, heartbeat coverage, and INSUFFICIENT_DATA on gaps (D8). |
| SR1-5 | Codex | `tool_exception`/`TimeoutError` does not measure pool timeouts. | MAJOR | **Accepted.** Pool subclass at the checkout boundary for all consumers, distinguishing `sqlalchemy.exc.TimeoutError` (D10). |
| SR1-6 | Codex | A queue overrun is not executed work when quota refuses next. | MAJOR | **Accepted.** Existing outcome and pre-body classification stay authoritative, the queue requirement is qualified, and a scenario is added (D7). |
| SR1-7 | Codex | A write token is spent on a slot refusal, which contradicts "nothing consumed". | MAJOR | **Accepted as documentation.** Existing policy is kept: the guarantee covers durable quota only, and rate tokens stay spent (D9, L9). |
| SR1-8 | Codex | S1 cannot be green alone (async callers, `tests/conftest.py` env-key list). | MINOR | **Accepted.** S1 owns `tests/conftest.py` and a minimal compatibility edit in `auth.py`. S1's gate is focused, and the full offline suite is authoritative only after S2 merges (tasks; completed in SR2-5). |
| SR1-9 | Codex | The queue `code` rule contradicts the earliest-stage rule. | MINOR | **Accepted.** Ordering is separated from code selection: queue `code` comes from overruns only and is `null` when none (D4). |
| SR1-note | Codex | A pinned legacy `.env` validates, so validation does not prove the intended settings. | note | **Accepted.** Startup INFO line with effective settings and epoch, compared at deploy (D3, Step 0). |
| SR2-1 | Codex | The watcher stops at `more_body: false`, so a disconnect after a complete body is missed. | MAJOR | **Accepted.** The watcher owns `receive` exclusively until admission ends and keeps watching after the last body message. The handoff ends the watcher without cancelling an in-flight `receive` (impl-R1-1), then a replay wrapper yields the buffered messages, the in-flight call's result, and delegates. Tests cover a complete body followed by a disconnect at both stages (D1). |
| SR2-2 | Codex | The 64 KiB overflow is either lossy or its bound is dishonest. | MAJOR | **Accepted, with one deviation from the supervisor's triage.** There is no per-request cap: every consumed message is kept losslessly. The supervisor's proposed bound, body limit × waiters, is **61 MiB × 96 ≈ 5.7 GiB**, not the ~4 MiB assumed: the server overrides the SDK's 4 MiB default with `mcp_max_request_body_bytes`. So a process-wide replay budget (32 MiB) stops *consuming*, never drops, and the honest bound is about 38 MiB. Tests cover an oversized single message and a fragmented body arriving byte-exact (D1, L8, L11). |
| SR2-3 | Codex | A hard kill inside the heartbeat tolerance loses buffered incidents yet looks covered. | MAJOR | **Accepted.** `concurrency_runs` records `run_id`, a completed-interval watermark and `clean_shutdown`. A gap after an unclean run end is uncovered whatever its length. Test: a hard kill and restart under 180 s gives INSUFFICIENT_DATA (D8, L2). |
| SR2-4 | Codex | Flush-time buckets move incidents across window boundaries, and reports can certify an unflushed tail. | MAJOR | **Accepted.** Buckets use event time, keyed through retries. Evaluation runs only through the durable watermark, and an end beyond it gives INSUFFICIENT_DATA. Minute-aligned boundaries. Tests cover incidents at both boundaries and before the next flush (D8). |
| SR2-5 | Codex | S1's full-suite gate still conflicts with S2-owned auth tests. | MINOR | **Accepted.** S1's gate is focused: its own modules and tests plus an import smoke test. The full offline suite is authoritative after S2 merges (tasks). |

| SR3-1 | Codex | SR2-4 was partial, and a MAJOR: the exact clean-shutdown end lets a minute bucket shared with a later same-configuration run count toward the earlier window. | MAJOR | **Accepted.** Every evaluation end is rounded down to a whole minute, clean-shutdown ends included; the exact `completed_through` serves coverage only. A two-runs-in-one-minute scenario and test are added (D8). |
| SR3-2 | Codex | SR2-2 was partial: the 38 MiB figure assumed 64 KiB messages, but uvicorn returns everything accumulated up to its flow-control threshold plus the crossing read. | MAJOR (partial) | **Accepted.** The bound is re-derived from the code: `HIGH_WATER_LIMIT` is 64 KiB (`flow_control.py:7`, `httptools_impl.py:316–318, 575`) plus one socket read of at most 256 KiB, so about 320 KiB per message and **about 62 MiB** process-wide. Stated in D1, L8 and L11, and pinned by a guard test on the constant. |

Round 3 was the final spec round.

## Implementation review history

| Round | Reviewer | Finding | Severity | Disposition |
| --- | --- | --- | --- | --- |
| impl-R1-1 | Codex | The handoff cancels the watcher's in-flight `receive`. The security-header `BaseHTTPMiddleware` wraps `receive` and can suspend in task-group cleanup after taking a message from uvicorn and before returning it, so the cancel loses the message: the app receives nothing, the request hangs and keeps its request lease (queue/enforce only). | MAJOR | **Fixed.** The handoff is cancellation-free: each `receive` runs in its own task awaited through a shield, the in-flight call passes to the replay wrapper and its result is delivered exactly once after the buffered messages; teardown cancels it only after the response. Regression through the production `BaseHTTPMiddleware` wrapping in queue and enforce (D1). |

## Owner decisions

- 2026-09-23: the owner **approved** migration 028 and the `queue` mode.
