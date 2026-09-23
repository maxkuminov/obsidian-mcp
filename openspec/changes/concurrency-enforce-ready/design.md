## Context

#261 (archived `2026-09-06-mcp-concurrency-slots`) built one process-local
controller in `src/services/concurrency.py`. It has three admission stages:

- **Request envelope:** `Controller.request()` counts global and
  presented-bearer-fingerprint occupancy for the full ASGI request lifetime. It
  is zero-wait in every mode.
- **Auth session:** `Controller.auth()` is a permit around the middleware's own
  DB session. It is also zero-wait in every mode.
- **Tool slots:** `Controller.tool()` admits the class, tenant, principal and
  global dimensions atomically, with one optional bounded wait (enforce only).
  Separately, `Controller.writer()` admits usage writers.

The pool arithmetic lives in `src/services/pool_budget.py`: pool 5 + 10,
multiplier 2, headroom 4. `Settings._validate_concurrency` checks it.

The 2026-09-21 shadow evidence on #188 names five blockers (proposal §Why).
**The host changed after that evidence was gathered.** PGDATA moved to NVMe on
2026-09-23 and commits went from about 40–100 ms to about 2.5 ms. The
consequences for this design:

- Auth sessions (one credential SELECT, plus at most one async-commit UPDATE per
  60 s) and light tool bodies (`read_note` p50 6 ms) now hold their permits for
  single-digit milliseconds. Even a small bounded wait therefore absorbs a large
  burst. A 2 s transport deadline at auth = 2 and about 5 ms per session drains
  about 800 queued authentications. Queueing is cheap and refusing is not.
- The old shadow percentages were measured against longer holds, so they
  overstate current pressure. **No old row is used for the flip decision.** The
  readiness evaluator reads only `schema: 2` rows written after this change.
- The pool is still 15 connections shared with the indexer, the panel, OAuth
  and transfer. Shorter holds make the pool less likely to saturate. They do
  not change the arithmetic that bounds the MCP share of it.

## Goals / Non-Goals

**Goals**

- `enforce` is safe to switch on in production. A legitimate agent pattern,
  such as a batch of 6–10 parallel reads or two agents on one tenant, waits
  milliseconds and is never refused.
- Every ceiling can be tuned within the budget, and the one validated
  configuration is gone.
- An operator can see pressure, waits and overruns on the panel and get a
  PASS/FAIL readiness verdict from the same code.
- A measured rehearsal (`queue`) runs before any refusal is possible.
- Rollback at every step is a one-line `.env` change and a recreate.

**Non-Goals**

- A universal pool-availability guarantee. Headroom is still not reserved.
- Per-tenant reservation or a starvation SLA. Rejected in #261 for good reason.
- Raising the pool size (see Alternatives).
- Automatic promotion between modes.
- Any change to the token buckets, the quota, the failed-auth budget or the
  `MCP-REFUSAL` contract.

## Blocker → decision

| # | Blocker (#188, 2026-09-21) | Decision |
| --- | --- | --- |
| 1 | `Controller.auth()` never waits, so `WAIT_SECONDS` cannot soften auth-stage 429s. The request (fingerprint) stage also refused 30. | **D1.** The request and auth stages share one bounded transport deadline, `MCP_CONCURRENCY_TRANSPORT_WAIT_SECONDS` (default 2, max 5), with bounded waiter counts. They use the same eligible-FIFO machinery as tools. `auth()` becomes async. A waiting request holds its request lease (while waiting for auth) or nothing (while waiting for the envelope). It never holds a DB connection. Defaults are raised to requests 64 and fingerprint 20. Coherence requires `fingerprint ≥ principal + principal_waiters`, so the envelope never refuses what the tool stage would have queued. **The separate auth permit is kept**, because without it the 64-request envelope could open 64 auth sessions against a 15-connection pool. |
| 2a | The pool budget is exactly saturated and the class sum ≤ tools forces `other = 1`, so only one configuration validates. | **D2.** Class ceilings become independent (each ≤ tools, no sum rule). The global `tools` ceiling is what bounds the pool. The multiplier becomes per-class (write 2, the others 1), and real-PG tests measure every tool's checkout peak to pin it. The budget is `auth + tool_demand + writers + 4 ≤ 15`, where `tool_demand` fills `tools` slots highest-multiplier-first up to each class ceiling. Defaults: `2 + (2·1 + 1·5) + 1 + 4 = 14`. The operator has one spare connection plus a real range of class trade-offs. |
| 2b | `other` holds 15 of 25 tools, and its observed peak overlap is 2. | **D3.** Split `other` into `light` (9 tools, ceiling 4) and `scan` (6 tools, ceiling 2). The closed registry test is updated. `MCP_CONCURRENCY_OTHER=1` (the pinned legacy default) is ignored with a WARNING, and any other value is refused at boot. |
| 3 | `shadow_metadata` reports the last observation's code, which under-reports about 11×. | **D4.** `code` is the code of the earliest-stage observation in pipeline order (request < auth < tool < writer): what zero-wait enforcement would actually have returned. That observation is kept first and never truncated. `schema: 2` marks the new rows. The same rule applies to the new `concurrency_queue` object. |
| 4 | No panel surface; pressure is readable only by JSONB query. | **D6.** A Concurrency section on `/admin/performance` and a readiness evaluator shared by the panel and `make concurrency-report`. Durable numbers come from `usage_logs`. Process-scoped numbers (transport pressure on requests that never reach a tool, live occupancy, pool high-water) come from bounded in-process counters, shown to admins only and labelled "since boot". |
| 5 | The tool lease is held through telemetry, so a writer wait extends the slot hold. | **D5.** Release the slot lease when the body returns or raises, before `_log_usage`, `_record_tool_failure` and the tail security events. Pool arithmetic is unaffected, because logging connections are bounded by the writer permit and were never counted in the tool demand. |
| — | There is no evidence of how positive waits behave, because shadow is zero-wait by design. | **D7.** `queue` mode: enforcement's waits, but admit-with-`overrun` instead of refuse. It produces real `queue_ms` and overrun counts. Rollout is shadow → queue → enforce, each step with numeric criteria (§Rollout). |

## Decisions

### D1 — Transport stages wait, within one per-request deadline

- The request stage and the auth stage share **one** monotonic deadline. It
  starts when the middleware first asks for the request lease. A request that
  waits 1.5 s for the envelope has 0.5 s left for the auth permit.
  - Waiter bounds for the request stage: `MCP_CONCURRENCY_REQUEST_WAITERS`
    (global, 64) and `MCP_CONCURRENCY_FINGERPRINT_WAITERS` (16). The request
    stage retains the fingerprint registry entry while it waits, exactly as
    tool waiters retain tenant and principal entries, so overflow stickiness
    covers transport waiters too.
  - Waiter bound for the auth stage: `MCP_CONCURRENCY_AUTH_WAITERS` (32).
- While it waits for the auth permit, a request holds its request lease. It has
  opened no DB session. The existing invariant stays: the auth permit encloses
  only the middleware's session, and responses are sent after it closes.
- The tool deadline (`MCP_CONCURRENCY_WAIT_SECONDS`) is separate. So the
  caller-visible worst case of added latency is `transport_wait + tool_wait`
  (7 s at the defaults). The writer adds nothing to the caller after D5.
- In `enforce`, deadline expiry and waiter overflow keep the transport 429
  shape: `code`, `scope`, `limit`, `Retry-After: 1`. The 429 stays outside the
  in-band refusal contract, as before.
- Cancellation (client disconnect) while waiting releases every captured
  registry reference and waiter count. The #261 cancellation tests are extended
  to the transport stages.

### D2 — Pool budget with independent class ceilings and per-class multipliers

`pool_budget.py` exports:

- `POOL_SIZE = 5`, `POOL_OVERFLOW = 10`, `POOL_CAPACITY = 15`;
- `MCP_POOL_HEADROOM = 4`;
- `CLASS_CONNECTIONS = {"write": 2, "embedding": 1, "vector": 1, "scan": 1, "light": 1}`;
- `tool_demand(tools, caps)`. It is pure. It sorts classes by multiplier, then
  by name, and fills `min(cap, remaining)` slots greedily. That greedy fill is
  the maximum of `Σ m_c·n_c` subject to `n_c ≤ cap_c` and `Σ n_c ≤ tools`.

The validator refuses `auth + tool_demand + writers + headroom > 15` and names
every term.

Why the multiplier can drop to 1 for non-write classes. The inspected paths are
sequential:

- the quota admission commits on its own connection, which is released before
  the body starts;
- the auth session is closed before the tool runs;
- after D5, the usage write happens after the slot is released and under the
  writer permit.

The #261 real-PG meter (`task_peaks`) is extended into a parametrized test that
invokes **every registered tool** through `_tracked` against a fixture vault. It
asserts each tool's per-task checkout peak ≤ `CLASS_CONNECTIONS[class]`. If a
tool measures 2, its class multiplier becomes 2 in the same change. The test
enforces the constant, so a later tool path that overlaps sessions fails CI
instead of silently overrunning the budget.

Why the class-sum rule can go. It made the classes a static partition of
`tools`, which was the thing that pinned `other = 1`. The global `tools` counter
already bounds total admitted tools, and admission is atomic across all
dimensions. Independent class ceilings therefore bound class **shares** (for
example, embedding ≤ 1 protects the provider) without adding to the pool bound.
Eligible-FIFO means a saturated class still cannot park global capacity.

### D3 — Class set

| Class | Tools | Default ceiling | Multiplier |
| --- | --- | --- | --- |
| embedding | `semantic_search` | 1 | 1 |
| vector | `find_related` | 1 | 1 |
| write | `create_note`, `edit_note`, `move_note`, `delete_note`, `set_frontmatter`, `write_file`, `delete_file`, `import_from_url` | 1 | 2 |
| scan | `keyword_search`, `list_notes`, `get_tags`, `get_neighborhood`, `find_orphans`, `list_files` | 2 | 1 |
| light | `read_note`, `read_file`, `get_recent`, `get_vault_guide`, `get_backlinks`, `get_links`, `request_upload`, `check_upload`, `request_download` | 4 | 1 |

- **scan** holds every tool whose cost grows with vault size or graph size:
  FTS, filtered listing, tag aggregation, BFS to depth 5, the orphan anti-join
  and directory walks.
- **light** holds point reads and token minting.
- The `write` ceiling stays at 1. Writes are about 3 ms after NVMe, a
  one-writer queue is what the amplification bound (#188's family, "writes feed
  the next index pass") wants, and waiting is cheap.

Other defaults:

- `tools` 6. Pool demand is 14.
- `tenant` 4, `principal` 3.
- Waiters: principal 16, tenant 32, global 64. A waiter costs one future and
  holds no DB connection. Waiter overflow is an immediate refusal in `enforce`,
  so these bounds are sized to exceed an agent's plausible parallel batch.
- `requests` 64, `fingerprint` 20 (≥ 3 + 16).
- `MCP_CONCURRENCY_WAIT_SECONDS` default 5, maximum raised from 5 to 10. The
  default was 0.

Legacy `MCP_CONCURRENCY_OTHER`:

- The old `.env.example` pinned `MCP_CONCURRENCY_OTHER=1`, so production very
  likely carries it.
- The value `1` is the old default and expresses no operator intent. It is
  accepted, ignored, and logged once at WARNING.
- Any other value was a deliberate tuning that cannot be honoured, and boot
  refuses it with a message naming `MCP_CONCURRENCY_LIGHT` and
  `MCP_CONCURRENCY_SCAN`.

### D4 — Metadata names the refusal enforcement would have returned

`shadow_metadata(observations)` changes:

- It deduplicates and ranks by stage order request (0) < auth (1) < tool (2) <
  writer (3).
- `code` is the rank-0 observation's code.
- The list is emitted in rank order, capped at 4, so the worst observation is
  always the first element and always present.
- It adds `schema: 2`. `basis` stays `observed_occupancy_zero_wait`.
- It adds `configured_wait_ms`: `{"transport": …, "tool": …}`. This informs
  readers only. Shadow still never waits.

Legacy rows (no `schema`) are left untouched. Readers that need tool pressure
on legacy rows must scan `observations`, and the readiness evaluator ignores
legacy rows altogether.

`write_usage_row`'s writer observation merge re-ranks through the same function,
so appending a writer observation can never displace an earlier-stage `code`.

### D5 — Slot lease lifetime ends with the body

In `_tracked`:

- The quota gate, the body, and result shaping (the `ReadNoteResult`
  conversion) run under the slot lease.
- Immediately after the body returns or raises, and before `_log_usage`,
  `_record_tool_failure`, `tool_body_outcome` and the tail, the lease is
  released through `Lease.release()`, which is idempotent. The `finally`
  keeps its release as the backstop for cancellation and early exits.
- Everything the row needs (`queue_ms`, shadow/queue metadata) is recorded into
  `timing` before the release.

A quota refusal still releases before its refusal row is written. That is
unchanged in effect.

This reverses a sentence in #261's design ("hold a tool lease through quota,
body and its telemetry tail"). That sentence was a conservative default, not a
pool requirement. Logging connections were always budgeted under `writers`, not
under the tool multiplier.

### D6 — Observability: panel section and readiness evaluator

`src/services/concurrency_readiness.py`:

- `window_stats(session, window, user_id)` is a single read-only aggregation
  over `usage_logs` rows with `params->'concurrency_shadow'->>'schema' = '2'`,
  `params ? 'concurrency_queue'`, or `params ? 'queue_ms'`. For each tool and
  class it returns:
  - executed calls;
  - pressured calls (any tool-stage observation);
  - transport-pressured calls;
  - overruns;
  - weighted `slot_timeout` refusals (`1 + suppressed`);
  - `queue_ms` p50, p95, p99 and max;
  - `transport_queue_ms` p95 and max;
  - `tool_exception` rows with `error_type = 'TimeoutError'`.
- `evaluate(stats, process, target_mode)` returns one PASS / FAIL /
  INSUFFICIENT_DATA verdict per criterion (§Rollout), plus the numbers that
  drove each. It is pure, with no I/O, and unit-tested on synthetic inputs.
- `process` is `Controller.snapshot()` (below) plus the pool gauge.

In-process counters, added to `Controller`:

- `snapshot()` returns mode, effective limits, computed pool demand, live
  `active`/`waiting` per global and class counter, and process start time.
- It also returns since-boot counters keyed by `(stage, scope, outcome)`, where
  outcome is one of `pressure_shadow`, `waited`, `overrun` or `refused`. The
  key set is closed and bounded, with no identities.
- Counters increment inside the existing non-awaiting transitions.

`src/database.py` adds `checkout`/`checkin` listeners that keep a pool
checked-out high-water mark (two integers) and expose it.

`/admin/performance` gets a Concurrency section:

- It uses the page's own window and scoping. A regular user sees only their own
  rows' aggregates.
- **Admin only:** live occupancy, since-boot counters, pool high-water,
  effective limits and demand, and the readiness verdict for the next mode.
  These are server-wide and have no owner, which is the same reasoning the
  health page uses.
- Tables and text only, and no new script. The panel's nonce CSP is untouched.

`scripts/concurrency_report.py` (`make concurrency-report DAYS=7 TARGET=queue|enforce`):

- It runs as a separate process (`docker exec`), so it cannot see the live
  controller.
- It evaluates the durable criteria from `usage_logs`, and reports the
  process-scoped criteria (E2's counter half, E6's high-water, Q2's counter
  half) as `SEE_PANEL`. The panel runs the same `evaluate()` with the live
  snapshot, so it shows the complete verdict. The script needs no HTTP
  credentials.
- Output: a table and one JSON line. The exit code is 0 only when every
  criterion the script can evaluate passes.

### D7 — `queue` mode

`MCP_CONCURRENCY_MODE = off | shadow | queue | enforce`, default `shadow`.

In `queue`, every stage (request, auth, tool, writer) runs `_admit_wait` with
enforcement's deadlines, waiter bounds and eligible-FIFO. Where `enforce` would
return `Admission(None, pressure)` (deadline expiry, waiter overflow, or a
zero-wait miss), `queue` instead grants the lease, marks the admission `overrun`
with that pressure, and carries on:

- the call runs;
- no refusal is emitted;
- no `slot_timeout` row is written;
- quota is consumed normally because the call executes.

Overruns may push `active` above a ceiling, exactly as shadow does today. Queue
bounds latency, not occupancy.

Rows in queue and enforce modes carry `queue_ms` (tool, as now) and
`transport_queue_ms` (new). A queue-mode row with any wait > 0 or any overrun
also carries:

```json
"concurrency_queue": {"schema": 2, "code": "<earliest-stage overrun code or null>",
  "overrun": true, "observations": [{"stage": "...", "scope": "...", "limit": 0,
  "waited_ms": 0.0, "overrun": true}]}
```

It has the same ordering and cap as D4, and no identity fields.

Shutdown refusal (`scope: shutdown`) stays a refusal in every mode, as today.

`mcp_concurrency_pressure` gains outcomes `overrun` (queue) and `waited` (a
transport wait above 100 ms, bounded by the existing emitter's rules).

Why it is worth a fourth mode:

- Shadow's zero-wait predicate cannot say whether a 5 s wait would have
  sufficed. The first 2026-09 attempt showed that a counterfactual guess is not
  good enough evidence to break callers on.
- `queue` measures the actual quantity that `enforce` turns into refusals, with
  real FIFO dynamics, at a cost of bounded latency that only pressured calls
  pay.

**Configuration validation.** The rule "shadow requires `WAIT_SECONDS = 0`" is
**removed**:

- Shadow ignores configured waits. It reports `basis:
  observed_occupancy_zero_wait` and `configured_wait_ms`, so no reader can
  mistake it for a wait result.
- Keeping the rule would make every mode change a multi-line edit, which is a
  rollback hazard. `queue` and `enforce` accept a zero tool wait, meaning
  immediate admit-or-overrun and immediate admit-or-refuse respectively.

## Rollout

All steps are operator `.env` changes followed by a recreate (`docker compose up
-d` in the deploy dir; no rebuild). Every step starts only after the previous
one's criteria pass.

**Step 0 — deploy in shadow.**

- Reconcile the deploy-dir `.env` concurrency block to the new `.env.example`
  block first. The old pinned `FINGERPRINT=4` fails coherence against
  `principal + principal_waiters`.
- Dry-run the settings against the deploy `.env` with the new image before the
  recreate.
- After deploy: exercise the affected MCP tools live (at least one per class),
  confirm the new rows carry `schema: 2` metadata where pressured, and confirm
  the panel section renders with zero CSP violations.

**Step 1 — shadow → queue.** Entry criteria, computed by
`make concurrency-report TARGET=queue` over **≥ 3 days** and **≥ 300 tool
calls** of schema-2 data:

| ID | Criterion | Threshold |
| --- | --- | --- |
| Q1 | Tool-stage pressured calls (zero-wait predicate) / executed calls | ≤ 10 % |
| Q2 | Transport-pressured requests (rows plus since-boot counter) / executed calls | ≤ 5 % |
| Q3 | Pool timeouts (`tool_exception` with `TimeoutError`) | 0 |

Queue never refuses, so these criteria bound only the added latency. If Q1 or Q2
fails, raise the relevant ceiling within the budget first.

**Step 2 — queue → enforce.** Entry criteria over **≥ 7 days** and **≥ 1,000
tool calls** of queue-mode data. Every criterion must hold both for the whole
window **and** for its last 72 h. The process must also have **≥ 72 h uptime**
at evaluation, or the process-scoped criteria report INSUFFICIENT_DATA.

| ID | Criterion | Threshold |
| --- | --- | --- |
| E1 | Tool-stage overruns (what enforce would refuse) | ≤ max(1, 0.1 % of executed calls) |
| E2 | Transport-stage overruns (rows plus since-boot counter) | 0 |
| E3 | Writer overruns | 0 |
| E4 | Tool `queue_ms` p99 across all executed calls | ≤ 500 ms |
| E5 | Maximum tool `queue_ms` / tool wait, and maximum `transport_queue_ms` / transport wait | ≤ 0.5 each |
| E6 | Pool timeouts in window; pool high-water since boot | 0; ≤ 13 (capacity − 2) |

**Rollback triggers**, checked daily for the first 7 days of each new mode:

- **queue → shadow:** any 24 h with tool `queue_ms` p95 > 1,000 ms, or any
  report of an agent-side timeout attributable to queueing.
- **enforce → queue:**
  - any `mcp_concurrency_pressure` event with outcome `refused`, since a
    transport 429 to a legitimate client is a breakage;
  - weighted `slot_timeout` > max(2, 0.2 % of calls) in any 24 h;
  - any pool timeout.
- Rollback is `MCP_CONCURRENCY_MODE=<previous>` followed by a recreate, with no
  data or schema step. Tuned limits may stay.

The flip criteria are recorded in the spec delta. The evaluator is the single
implementation of these thresholds, and tests pin every boundary.

## Alternatives rejected

- **Drop the auth permit and rely on the pool.** The pool's own queue is a 30 s
  `pool_timeout` that ends in a 500. It is shared with the panel and OAuth, so
  the 64-request envelope could starve `/token` and the panel login for 30 s. A
  bounded auth wait turns the same pressure into a short queue with a precise
  429 at the end.
- **Raise the pool (for example 10 + 20).** The Postgres instance is shared
  with other services on the host. The pool was never the measured bottleneck:
  the refusals came from ceilings of 1–2 with zero wait. After NVMe, holds are
  milliseconds. The budget rework buys tunability without touching a shared
  resource. It can be revisited later as a separate change with its own
  `max_connections` review.
- **Derive the pool size from the budget.** The same shared-resource objection
  applies, plus it makes an `.env` edit resize a database-facing pool at boot.
- **A shadow counterfactual wait estimator** (virtual waiters resolved on
  release). It was considered and rejected. The estimate resolves after the
  call's own row is written, so it cannot ride on the row without deferring
  telemetry or adding background tasks. It is also biased by calls that
  enforcement would not have run. `queue` measures the same quantity exactly.
- **Going straight from shadow to enforce with long waits.** The first refusal
  would be the first data point.
- **Per-class tool waits.** More knobs for no demonstrated need. One tool
  deadline plus class ceilings is enough, and queue data will show whether one
  class needs more.
- **Keep `other` and just raise its ceiling.** That lumps `find_orphans` and
  `keyword_search` with `read_note`, so one tenant's scans could hold every
  cheap-read slot.

## Accepted limitations

- **L1.** Queue mode bounds added latency, not occupancy. During an overrun,
  occupancy may exceed the ceilings exactly as in shadow today. Queue is a
  rehearsal, not a protection mode.
- **L2.** Since-boot counters reset on every restart. Process-scoped criteria
  (E2's counter half, E6's high-water) need ≥ 72 h of uptime and otherwise read
  INSUFFICIENT_DATA. Transport pressure on requests that never reach a tool is
  durable only in the security-event log.
- **L3.** The caller-visible worst-case added latency is transport + tool wait
  (7 s at the defaults, 15 s at the maxima). Waiting is preferred over refusing.
- **L4.** The pool budget still bounds only MCP's configured contribution.
  Headroom is not reserved (unchanged from #261).
- **L5.** A shared credential is a shared fingerprint and principal. Two heavy
  agents on one key contend (unchanged).
- **L6.** The tool-stage p99 criterion (E4) is measured on current traffic
  (about 130 calls a day across two tenants). A new heavy tenant invalidates it.
  Re-run the report after any tenant is added.
- **L7.** The per-class multiplier is only as good as the per-tool checkout
  test. A tool whose peak depends on input size (for example `import_from_url`
  on a large body) is measured on the fixture only. `write` keeps multiplier 2
  as the conservative class for exactly that reason.

## Open question for the owner

- Is a fourth mode (`queue`) acceptable, or should the rollout go straight from
  shadow to enforce once the new defaults show low zero-wait pressure? The
  design recommends `queue`, because it is the only way to get positive-wait
  evidence without refusing a caller.
