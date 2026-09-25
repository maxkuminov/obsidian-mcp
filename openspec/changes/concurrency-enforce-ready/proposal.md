## Why

#261 shipped process-local concurrency admission for `/mcp` in **shadow** mode
(issue #188, archived change `2026-09-06-mcp-concurrency-slots`). Shadow data
from 2026-09-13 to 2026-09-21 covered 8.2 days, 1,073 tool calls and two
tenants, all on defaults. It showed that `enforce` cannot be switched on as
built:

- At the defaults, enforcement would have refused about **9.6 % of legitimate
  tool calls** as `slot_timeout`: `read_note` 71, `keyword_search` 16 and
  `get_vault_guide` 14. It would also have answered up to **about 153
  requests** with a transport 429 (auth stage 107, fingerprint 30). The daily
  trend was rising.
- **Blocker 1.** `Controller.auth()` never waits in any mode, and the request
  stage is zero-wait only.
- **Blocker 2.** The pool budget is exactly saturated
  (`auth 2 + 2·tools 4 + writers 1 + headroom 4 = 15`), and the class ceilings
  must sum to at most `tools`. Only one configuration validates, which pins
  `other = 1` although `other` holds 15 of the 25 tools.
- **Blocker 3.** `shadow_metadata` reports the **last** observation as `code`.
  Counting by `code` under-reports tool pressure about 11×.
- **Blocker 4.** There is no panel surface, and the data could not answer the
  flip question anyway.
- **Blocker 5.** The tool lease is held through telemetry, so a writer wait
  extends every slot hold. Shadow never waits, so there is no evidence about
  what that does.

PGDATA has since moved to NVMe (2026-09-23), cutting commits from about
40–100 ms to about 2.5 ms. Connection holds are now far shorter, so the old
numbers overstate current pressure and are **not** evidence for any flip. They
remain evidence about the shape of the problem: zero-wait ceilings of 1–2
refuse ordinary parallel reads from one agent.

A false refusal of a legitimate agent call breaks the live path. The design
**waits within bounded time before refusing**, and it earns each step towards
`enforce` with durable, covered numbers.

## What Changes

- **Bounded, disconnect-aware transport waits (blocker 1).**
  - Request and auth admission share one per-request deadline,
    `MCP_CONCURRENCY_TRANSPORT_WAIT_SECONDS` (default 2 s, max 5 s), with
    bounded waiters. A waiting request holds no DB connection.
  - While a request waits, the middleware watches ASGI `receive` for
    `http.disconnect` until admission ends, including after the body is
    complete. It is the only caller of `receive` during that time.
  - Every message it consumes is kept and replayed intact to the app.
  - A process-wide replay budget (32 MiB) stops further consumption but never
    drops anything. Worst-case memory is about 62 MiB: the budget plus 96 waiters × one uvicorn message of up to about 320 KiB.
  - A disconnected waiter is freed immediately and runs no credential query.
  - The auth permit is kept.
- **Tunable pool budget and reclassification (blocker 2).**
  - Class ceilings become independent (each ≤ `tools`).
  - The connection multiplier is set per class (write 2, others 1), pinned by a
    real-PG per-tool checkout-peak test.
  - `other` splits into `light` (9 tools) and `scan` (6 tools).
  - The new defaults give pool demand 14 of 15 connections.
  - Adds the coherence rule `fingerprint ≥ principal + principal_waiters`.
  - `MCP_CONCURRENCY_OTHER=1` is ignored with a WARNING; any other value is
    refused at boot.
- **Metadata (blocker 3).** Observations are ordered by pipeline stage.
  - In shadow, `code` is the earliest stage's code.
  - In queue, `code` comes from overruns only, and is `null` when nothing
    overran.
  - Both objects carry `schema: 2`.
- **Blocker 5: kept, bounded and measured.** The slot lease stays held through
  telemetry. Early release was proposed and rejected in spec review because it
  loses audit rows for completed writes. The writer wait is ≤ 0.25 s, and queue
  mode measures its real effect on `queue_ms` before enforce is possible.
- **`queue` mode.** It waits exactly as `enforce` does. Where enforce would
  refuse for capacity, it admits the call with an `overrun` mark. All later
  gates (quota included) and the existing outcome classification stay
  authoritative.
- **Durable, request-level evidence (blocker 4).**
  - Every tracked usage row carries `params.concurrency {v: 2, mode, epoch}`.
  - Request totals, per-request worst transport outcomes, writer overruns,
    and pool checkout timeouts and high-water go to event-time minute buckets
    in `concurrency_counters`.
  - A per-run `concurrency_runs` row carries a completed-interval watermark
    and a clean-shutdown flag.
  - Both tables are added by **migration 028** (owner-approved) and written by
    one bounded transaction per minute.
  - Evidence counts only through the durable watermark. The gap after an
    unclean run end is uncovered, however short.
  - Pool timeouts are counted at the shared pool checkout boundary, for every
    consumer.
- **Panel and readiness.** `/admin/performance` gains a Concurrency section.
  One pure evaluator, shared by the panel and `make concurrency-report`,
  applies fixed numeric criteria. It reads only v2 rows and counters from one
  mode and one epoch over a covered window, and otherwise returns
  INSUFFICIENT_DATA.
- **Rollout.** The default stays `shadow`. The operator steps `shadow` →
  `queue` → `enforce` by `.env` edit and recreate, with numeric entry criteria
  and rollback triggers at each step. Rollback always changes one line.

Invariants kept:

- buckets are the first gates; the daily quota is the last pre-body gate;
- the `MCP-REFUSAL` contract is unchanged, and transport 429s stay outside it;
- `--workers 1`;
- **a call refused for concurrency consumes no durable quota.** "Nothing
  durable consumed" covers the durable daily quota only. Rate-bucket tokens
  already spent by the buckets-first gates stay spent, as for every other
  pre-body refusal, and are not refunded.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `mcp-request-routing`:
  - transport waits and disconnects;
  - class set and ceilings;
  - pool demand;
  - metadata;
  - queue mode;
  - configuration coherence;
  - row provenance;
  - windowed counters;
  - pool timeout accounting;
  - the rate-token statement;
  - the readiness evaluator.
- `panel-performance-views`: the concurrency section, and the rule that queue
  and shadow metadata never override classification.

## Impact

- **Code:**
  - `src/services/concurrency.py`
  - `src/services/pool_budget.py`
  - the `src/config.py` concurrency block
  - `src/mcp_server/auth.py` (admission, disconnect watch, per-request outcome)
  - `src/mcp_server/tools.py` (`_tracked` concurrency region, row provenance,
    `write_usage_row`)
  - `src/services/security_events.py` (outcome values)
  - `src/database.py` (pool subclass)
  - new `src/services/concurrency_counters.py`
  - `src/models/db.py` and `alembic/versions/028_concurrency_counters.py`
  - `src/main.py` (flush task and shutdown flush)
  - new `src/services/concurrency_readiness.py` and
    `scripts/concurrency_report.py`
  - the panel's `performance_page` and template
  - `Makefile`, `.env.example`, `tests/conftest.py`
- **Schema.** Migration 028 adds two tables, `concurrency_counters` and `concurrency_runs`. `make test-schema` and `make
  db-check` apply.
- **Operations.** The deploy-dir `.env` pins the old concurrency block. Validation
  can pass on those legacy values, so the deploy task reconciles the block and
  compares the startup INFO line of effective settings.
- **Docs:** `rate-limits.md`, `usage-attribution.md`,
  `security-event-logging.md`, `control-panel.md` and `schema-and-migrations.md`.
  CLAUDE.md is **not** changed on lease lifetime: the lease still spans
  telemetry.
- **Out of scope:**
  - the pool size;
  - per-tenant reservations;
  - Traefik limits;
  - bucket, quota or failed-auth changes;
  - refunding rate tokens;
  - automatic mode promotion.
