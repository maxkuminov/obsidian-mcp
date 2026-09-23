## Why

#261 shipped process-local concurrency admission for `/mcp` in **shadow** mode
(issue #188, archived change `2026-09-06-mcp-concurrency-slots`). Shadow data
from 2026-09-13 to 2026-09-21 covered 8.2 days, 1,073 tool calls and two tenants,
all on defaults. It showed that `enforce` cannot be switched on as built:

- At the defaults, enforcement would have refused about **9.6 % of legitimate
  tool calls** as `slot_timeout`: `read_note` 71, `keyword_search` 16 and
  `get_vault_guide` 14. It would also have answered up to **about 153
  requests** with a transport 429 (auth stage 107, fingerprint 30). The daily
  trend was rising.
- **Blocker 1.** `Controller.auth()` never waits in any mode, so
  `MCP_CONCURRENCY_WAIT_SECONDS` cannot soften the auth-stage 429s. The
  request stage (global/fingerprint) is also zero-wait only.
- **Blocker 2.** The pool budget is exactly saturated:
  `auth 2 + 2·tools 4 + writers 1 + headroom 4 = 15`. The class ceilings must
  also *sum* to at most `tools`, so four classes of ≥ 1 force `other = 1`.
  Only one configuration validates. `other` holds 15 of the 25 tools.
- **Blocker 3.** `shadow_metadata` reports the **last** observation as `code`,
  not the most severe one. Counting rows by `code` under-reports tool pressure
  about 11× (9 rows against 103).
- **Blocker 4.** No panel surface shows concurrency pressure. It can only be
  read with a JSONB query.
- **Blocker 5.** The tool lease is held through telemetry. A writer wait of up
  to 0.25 s extends every slot hold, and shadow never waits, so there is no
  evidence about what that does under enforcement.

Since then the database moved from a spinning disk to NVMe (2026-09-23). Commit
cost fell from about 40–100 ms to about 2.5 ms. Every connection hold behind
the shadow numbers above is now far shorter, so the 9.6 % figure overstates
today's pressure. It is still evidence about the **shape** of the problem:
zero-wait ceilings of 1–2 refuse ordinary parallel reads from one agent.

For this product a false refusal of a legitimate agent call breaks the live
path. The agent sees an error for a read that would have taken 6 ms. The design
must therefore **wait within a bounded time before it refuses**, and it must
earn the switch to `enforce` with numbers rather than guesses.

## What Changes

- **Bounded transport waits (blocker 1).** Request (global + fingerprint) and
  auth-session admission get one bounded monotonic deadline per request,
  `MCP_CONCURRENCY_TRANSPORT_WAIT_SECONDS` (default 2 s, max 5 s). The deadline
  has its own bounded waiter counts. A waiting request holds no DB connection
  and no auth permit. `Controller.auth()` becomes awaitable. The transport 429
  now happens only on deadline expiry or waiter overflow.
- **Tunable pool budget (blocker 2).**
  - Class ceilings become **independent ceilings** (each ≤ `tools`), no longer a
    partition that must sum to `tools`.
  - The flat `TOOL_CONNECTION_MULTIPLIER = 2` becomes a per-class table:
    `write` 2, every other class 1. Real-PostgreSQL tests pin each value by
    measuring the per-task checkout peak of **every** registered tool.
  - The budget becomes `auth + tool_demand + writers + headroom ≤ 15`, where
    `tool_demand` is the worst admissible class mix.
  - The pool stays 5 + 10.
- **Reclassify `other` (blocker 2).** `other` is split into `light` (point
  reads and transfer minting, 9 tools) and `scan` (whole-vault or graph work, 6
  tools). The class set becomes embedding, vector, write, scan and light.
  `MCP_CONCURRENCY_OTHER` is retired. A pinned legacy value of `1` is ignored
  with a WARNING. Any other value is refused at boot and the error names the
  replacements.
- **New defaults.**
  - Tool ceilings: tools 6; light 4, scan 2, write 1, embedding 1, vector 1;
    tenant 4, principal 3.
  - Waiters: principal 16, tenant 32, global 64.
  - Request envelope: 64 global, fingerprint 20.
  - Tool wait: 5 s (max 10 s).
  - Pool demand at these defaults is `2 + 7 + 1 + 4 = 14`.
  - A new coherence rule requires `fingerprint ≥ principal + principal_waiters`,
    so the transport stage never refuses a call the tool stage would have
    queued.
- **Worst-code shadow metadata (blocker 3).** `concurrency_shadow.code` becomes
  the code of the **earliest pipeline stage** under pressure (request → auth →
  tool → writer). That is the refusal zero-wait enforcement would actually have
  returned. That observation is never truncated. Rows carry `schema: 2` so
  readers can tell them from the legacy last-code rows.
- **Release the tool lease before telemetry (blocker 5).** The slot lease is
  released once the body returns (or raises) and before any usage-row or
  security-event work. The writer permit bounds logging connections on its own.
  The shadow observation for the call is captured before the release.
- **`queue` mode, a rehearsal step between `shadow` and `enforce`.**
  - It waits exactly as enforcement does: the same deadlines, FIFO order and
    waiter bounds.
  - Where enforcement would refuse, it **admits anyway** and records an
    `overrun` instead.
  - It produces measured `queue_ms` and overrun counts under real queueing
    dynamics. Shadow's zero-wait predicate cannot provide those.
  - Its only cost to callers is bounded added latency. It never refuses.
- **Panel surface and readiness report (blocker 4).**
  - `/admin/performance` gains a Concurrency section. Everyone sees windowed
    per-tool/class pressure, waits and overruns, scoped as the page already
    scopes.
  - Admins also see live occupancy, the since-boot transport counters, the pool
    checkout high-water mark, the effective limits and the computed pool demand.
  - A readiness evaluator in `src/services/concurrency_readiness.py` backs both
    the panel verdict and `make concurrency-report`. It checks the fixed
    numeric flip criteria and returns PASS / FAIL / INSUFFICIENT_DATA for each.
- **Rollout.** The default mode stays `shadow`. Each step is an operator `.env`
  change followed by a container recreate: `shadow` → `queue` → `enforce`. Each
  step has numeric entry criteria and rollback triggers (design §Rollout).
  Rollback always changes one line, `MCP_CONCURRENCY_MODE`.

No schema change and no migration. Every existing invariant stays:

- the rate buckets are the first gates and the daily quota is the last
  pre-body gate;
- nothing durable is consumed by a call that does not run;
- the `MCP-REFUSAL` line contract is unchanged;
- transport 429s stay outside that contract;
- `--workers 1`.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `mcp-request-routing`:
  - transport waits;
  - class set and ceilings;
  - pool demand;
  - shadow metadata;
  - lease lifetime;
  - `queue` mode;
  - configuration coherence;
  - the readiness evaluator.
- `panel-performance-views`: the concurrency section, and the executed-work
  classification of queue-mode rows.

## Impact

- **Code:**
  - `src/services/concurrency.py`
  - `src/services/pool_budget.py`
  - `src/config.py` (the concurrency block only)
  - `src/mcp_server/auth.py` (the admission block)
  - `src/mcp_server/tools.py` (`_tracked` admission and lease release,
    `write_usage_row` metadata)
  - `src/services/security_events.py` (`mcp_concurrency_pressure` outcome
    values)
  - `src/database.py` (pool high-water listener)
  - new `src/services/concurrency_readiness.py`
  - new `scripts/concurrency_report.py`
  - `src/control_panel/routes.py` (`performance_page`) and
    `templates/performance.html`
  - `Makefile` (a `concurrency-report` target)
  - `.env.example`
- **Operations.** The deploy-dir `.env` pins every `MCP_CONCURRENCY_*` value
  from the old `.env.example`. It must be reconciled **before** the recreate,
  because the old pinned `FINGERPRINT=4` fails the new coherence rule. The deploy
  task includes a config dry-run.
- **Docs:**
  - `docs/architecture/rate-limits.md` (the concurrency section is rewritten);
  - `docs/architecture/usage-attribution.md` (the metadata register);
  - `docs/architecture/security-event-logging.md` (outcome values);
  - `docs/architecture/control-panel.md`;
  - the CLAUDE.md concurrency bullet (the "leases remain held through
    telemetry" wording is reversed).
- **Not in scope:**
  - raising the pool size;
  - per-tenant reservations or starvation SLAs;
  - Traefik-level limits;
  - any change to the token buckets or the quota;
  - automatic promotion between modes.
