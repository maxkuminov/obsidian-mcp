# Tasks: concurrency-enforce-ready (#188)

The work is four code slices plus a supervisor docs pass. Each code slice is
implemented by an independent Opus subagent in its own worktree, on a flat
branch.

**Files are not all disjoint.** S1 touches `src/mcp_server/auth.py` minimally,
and S2 later owns it. Those slices are **sequential**: S2 starts only after S1
merges. S2 and S3 run in parallel and are file-disjoint. S4 runs after S3. A
subagent that needs to edit outside its files or region stops and reports.

| Slice | Branch | Sequencing | Owns |
| --- | --- | --- | --- |
| S1: controller, config, budget, accumulator | `wt-ce-s1-core` | none | `src/services/concurrency.py`, `src/services/pool_budget.py`, `src/config.py` (the `mcp_concurrency_*` fields and `_validate_concurrency` only), `.env.example` (the MCP concurrency block only), `tests/conftest.py` (the env-key list only), `src/mcp_server/auth.py` (**compatibility only**: await the now-async `request()`/`auth()` with behaviour unchanged, since S2 rewrites this block), `tests/test_issue_261_controller.py`, `tests/test_issue_261_config.py`, new `tests/test_concurrency_queue_mode.py`, new `tests/test_concurrency_metadata.py`, new `tests/test_concurrency_counters_accumulator.py`; plus, **mechanically only**, the literal `resource_class="other"` → `"light"` in every test file that carries it (14 today: `grep -rln "resource_class=[\"']other[\"']" tests`) |
| S2: middleware and `_tracked` wiring | `wt-ce-s2-wiring` | **after S1 merges** | `src/mcp_server/auth.py` (`_concurrency_response`, `_emit_concurrency_pressure`, the admission block in `APIKeyMiddleware.__call__`, a new receive-watch helper), `src/mcp_server/tools.py` (`write_usage_row`, `_rate_refusal_template`, the concurrency region of `_tracked`, and the provenance stamp in every row-building path of `_tracked`/`_record_tool_failure`/the coalescer template), `src/services/security_events.py` (the `mcp_concurrency_pressure` outcome set only), `tests/test_issue_261_auth.py`, `tests/test_issue_261_tool_admission.py`, new `tests/test_concurrency_disconnect.py`, new `tests/test_concurrency_provenance.py`, `tests/integration/test_issue_261_concurrency_pg.py`, new `tests/integration/test_concurrency_tool_checkout_peaks_pg.py` |
| S3: durable counters and pool boundary | `wt-ce-s3-counters` | **after S1 merges** (parallel with S2) | new `alembic/versions/028_concurrency_counters.py`, `src/models/db.py` (the new `ConcurrencyCounter` and `ConcurrencyRun` models, appended), new `src/services/concurrency_counters.py` (run registration, flush, watermark, prune, coverage query), `src/database.py` (the pool subclass and `poolclass=` only), `src/main.py` (flush task start/stop and the shutdown flush, placed after the refusal `flush_all()` and before `engine.dispose()`), `tests/integration/test_schema_check.py` (head `027 → 028` and 028's cases), new `tests/integration/test_concurrency_counters_pg.py`, new `tests/integration/test_pool_timeout_boundary_pg.py` |
| S4: readiness, report, panel | `wt-ce-s4-readiness` | **after S3 merges** (and after S2, for realistic row fixtures) | new `src/services/concurrency_readiness.py`, new `scripts/concurrency_report.py`, `Makefile` (a `concurrency-report` target and its help line), `src/control_panel/routes.py` (`performance_page` only), `src/control_panel/templates/performance.html`, new `tests/test_concurrency_readiness.py`, new `tests/test_panel_concurrency_section.py`, new `tests/integration/test_concurrency_readiness_pg.py` |
| S5: docs | supervisor | after S1–S4 merge | `docs/architecture/rate-limits.md`, `docs/architecture/usage-attribution.md`, `docs/architecture/security-event-logging.md`, `docs/architecture/control-panel.md`, `docs/architecture/schema-and-migrations.md` |

**Base check, first thing in every brief.** Confirm that
`openspec/changes/concurrency-enforce-ready/design.md` exists and contains
"Spec review history", and that `git log` contains `f3ecba5`. For S2 and S3,
also confirm that `src/services/pool_budget.py` defines `CLASS_CONNECTIONS`
(S1 merged). For S4, confirm that `alembic/versions/028_concurrency_counters.py`
exists (S3 merged). Stop and report if any check fails.

**Contract set by S1 and pinned by its tests:**

- `Controller.request(fp, deadline, disconnected)` and
  `Controller.auth(deadline, disconnected)` are `async`.
  - `controller.transport_deadline()` creates the per-request deadline.
  - `disconnected` is an `asyncio.Event`. When it is set, the waiter is
    released and the call returns an admission marked `disconnected`.
- `Admission` gains `overrun: Pressure | None`, `disconnected: bool` and
  `queue_ms`.
- `Controller.snapshot() -> dict`: mode, limits, epoch, `pool_demand`, and live
  active/waiting counts.
- `concurrency.epoch(settings) -> str` and
  `concurrency.provenance() -> {"v": 2, "mode", "epoch"}`.
- `concurrency.shadow_metadata(observations, *, configured_wait_ms)` and
  `concurrency.queue_metadata(observations)`, per design D4.
- `concurrency.counters()` is a process-wide in-memory accumulator. Its API:
  - `record_request(worst, at)`, where `worst` is one of none, pressured,
    waited, overrun or refused;
  - `record(metric, n=1, at=None)` and `gauge(metric, value, at=None)`. `at`
    defaults to now, and entries are keyed by the **event-time minute** of `at`;
  - `drain() -> {(bucket_start, metric): (count, max)}`;
  - `merge_back(drained)`, which keeps the original keys;
  - a `lossy` flag, set when the 60-unflushed-minute cap drops a minute.

  Its metric set is closed (design D8), and it creates no asyncio primitive at
  import time.
- `concurrency.replay_budget()` is the process-wide replay-byte budget:
  `try_reserve(n)` and `release(n)`. It never refuses an already-consumed
  message: reservation happens after `receive`, and the watcher stops when the
  budget reports exhaustion.
- `pool_budget.tool_demand(tools, caps)` and `CLASS_CONNECTIONS`.

**Test gates.**

- **S1's gate is focused.** It runs S1's own test files, the mechanically
  renamed test files, and an import smoke check (`python -c "import src.main"`).
  S1 does not claim the full offline suite: `tests/test_issue_261_auth.py`
  calls the now-async `controller.auth()` directly, and those tests belong to
  S2.
- **The full offline suite (`pytest tests`) is authoritative only after S2
  merges.** S2, S3 and S4 each run it on their merged base, together with
  `make test-integration`. S3 also runs `make
test-schema` (migration 028). `make test-integration` and `make test-schema`
share a container and must never run concurrently. The authoritative gate is
the full offline + integration + schema run on the merged tree (task 5.1).

## 0. Spec review before code

- [x] 0.1 Commit the proposal on `wt-concurrency-enforce`.
- [x] 0.2 Codex spec review round 1: FAIL (7 MAJOR, 2 MINOR), with every finding folded in (see design "Spec review history").
- [x] 0.3 Codex spec review round 2: FAIL (4 MAJOR, 1 MINOR; SR1-8 partial), with every finding folded in (SR2-1 to SR2-5).
- [ ] 0.3a Codex spec review round 3, verification only.
- [x] 0.4 Owner decisions: `queue` mode and migration 028 approved (2026-09-23).

## 1. S1: controller, config, budget, accumulator

- [ ] 1.1 `pool_budget.py`: `CLASS_CONNECTIONS` (write 2, the others 1) and a pure `tool_demand`. Keep `TOOL_CONNECTION_MULTIPLIER = max(CLASS_CONNECTIONS.values())` as a deprecated alias with `TODO(#188)`; S2 removes its last importer and S5 deletes it.
- [ ] 1.2 `concurrency.py`: the five-class `TOOL_CLASSES` per D3.
- [ ] 1.3 `concurrency.py`: `shadow_metadata` and `queue_metadata` per D4. Ordering is by stage. The shadow code is the earliest stage's. The queue code is the earliest **overrun**, or `null`. The deciding observation is never truncated.
- [ ] 1.4 `concurrency.py`: mode `queue`. `_admit_wait` grants with `overrun` wherever enforce would refuse for capacity, grants exactly once when a grant races the timeout, and still refuses on shutdown.
- [ ] 1.5 `concurrency.py`: async `request()` and `auth()` with the shared deadline, the waiter dimensions (request global/fingerprint, auth), fingerprint-entry retention while waiting, and a `disconnected` event that releases the waiter.
- [ ] 1.6 `concurrency.py`: `epoch()`, `provenance()`, `snapshot()`, `replay_budget()`, and the `counters()` accumulator (a closed metric set, event-time minute keys, `record_request` counting each request once by worst outcome, gauges as bucket maxima, `drain()`/`merge_back()` preserving keys, and the lossy flag).
- [ ] 1.7 `config.py`:
  - add `queue` to the mode literal, and the new settings with their defaults per D1 and D3, including `mcp_concurrency_replay_budget_bytes` (default 32 MiB, range 1 MiB..256 MiB);
  - `mcp_concurrency_other: int | None`, where `1` warns and anything else is refused;
  - remove the shadow-requires-zero-wait rule and the class-sum rule;
  - add the full hierarchy, coherence and per-class budget validation, naming every term;
  - log the startup INFO line of effective settings and the epoch.
- [ ] 1.8 `.env.example` block, and `tests/conftest.py` env-key list.
- [ ] 1.9 `auth.py` compatibility: await the new coroutines, passing `controller.transport_deadline()` and a fresh, never-set disconnect event. Behaviour in shadow and off must stay exactly as today. Add no watch logic; S2 adds it.
- [ ] 1.10 Mechanical `resource_class="other"` → `"light"` rename in test probe decorators. Nothing else changes in those files.
- [ ] 1.11 Tests:
  - the updated #261 controller and config tests, with no existing invariant test weakened;
  - queue overrun on deadline, overflow and zero wait;
  - a grant racing a timeout;
  - the transport shared deadline, and auth ceiling 2 with 6 arrivals;
  - a `disconnected` event releasing a request waiter and an auth waiter;
  - metadata ordering and code selection: shadow earliest; queue ordinary wait gives `null`; an earlier wait plus a later overrun gives `slot_timeout`; truncation keeps the deciding observation;
  - `tool_demand` tables (default 14, the 18 > 15 case);
  - each coherence rule;
  - `OTHER=1` warns and `OTHER=3` refuses, through a real env file;
  - all modes validate on the `.env.example` block;
  - the epoch is stable across a mode change and changes on a limit change;
  - the accumulator counts a two-stage-pressured request once;
  - an event at 12:00:50 drained at 12:01:10 keys to 12:00;
  - `merge_back` preserves keys;
  - the lossy flag is set at the cap.
- [ ] 1.12 S1's focused gate is green (see Test gates), then commit on `wt-ce-s1-core`.

## 2. S2: middleware and `_tracked` wiring

- [ ] 2.1 `auth.py`, the disconnect watch (D1).
  - From the first wait until admission ends, a single watcher task is the only caller of `receive()`. It loops **past `more_body: false`** until admission ends, a disconnect arrives, or `replay_budget()` is exhausted.
  - Every consumed message is appended as-is and reserved against the budget.
  - On `http.disconnect`, set the event and return without any response or credential query.
  - At handoff, cancel and await the watcher. A completed `receive()` result is already in the list; a cancelled pending one consumed nothing.
  - Wrap `receive` for the downstream app so it yields the list in order, releases the budget, and then delegates.
  - Check the disconnect flag after the auth grant and before opening the session.
- [ ] 2.2 `auth.py`:
  - record `transport_queue_ms`;
  - in the outer `finally`, report the request's worst transport outcome once through `counters().record_request`, and the `transport_wait_max_ms` gauge;
  - put transport observations in `request_observations` for shadow and queue;
  - keep the 429 shape;
  - emit `mcp_concurrency_pressure` with outcomes `shadow`, `refused`, `overrun` and `waited` (> 100 ms).
- [ ] 2.3 `tools.py`:
  - stamp `params.concurrency = provenance()` on every row `_tracked` writes, covering executed calls, every pre-body refusal and coalesced template, `_record_tool_failure`, and writer-merged rows;
  - build shadow and queue metadata through the S1 functions;
  - record `queue_ms` and `transport_queue_ms` in queue and enforce;
  - a queue overrun takes no `slot_timeout` path and continues to the quota gate unchanged;
  - **the slot lease stays held through telemetry** (D5); do not move the release.
- [ ] 2.4 `tools.py` `write_usage_row`:
  - merge the writer observation through the ranked function;
  - in queue, a writer overrun still writes the row and records `writer_overrun`;
  - in enforce, a writer refusal records `writer_refused`.
- [ ] 2.5 `security_events.py`: the outcome set and catalogue comment.
- [ ] 2.6 Offline tests:
  - a **real `http.disconnect`** delivered through `receive`, without `task.cancel()`, at both transport stages, for three cases: no body, a **complete body (`more_body: false`) followed by a disconnect**, and a fragmented body followed by a disconnect. Each must show immediate cleanup, no credential query and no usage row;
  - byte-exact replay for a single-message body, a multi-message body, **a single message larger than the whole replay budget**, and **fragments crossing the budget**, with the budget-exhausted request deadline-bounded;
  - a handoff race: `receive` completes as admission is granted, and the message is replayed exactly once;
  - provenance on an unpressured row, and on `rate_limited`, coalesced `slot_timeout`, `over_quota` and `tool_exception` rows;
  - a queue overrun followed by a quota refusal stays `over_quota`, with no body and no quota consumed;
  - enforce slot refusal: quota unchanged and both bucket tokens spent (D9);
  - the registry covers the five classes;
  - an enforce batch of 8 parallel `read_note` calls at the defaults sees zero refusals;
  - the MCP-REFUSAL line is unchanged;
  - the auth-burst scenario through the real middleware.
- [ ] 2.7 Integration:
  - the per-tool checkout-peak test over **every** registered tool, asserting peak ≤ `CLASS_CONNECTIONS[class]` and naming tool, class and peak on failure. A tool above its multiplier is a **stop-and-report**.
  - Replace the `TOOL_CONNECTION_MULTIPLIER` import in `test_issue_261_concurrency_pg.py`, and add an auth-wait case whose actual checkouts stay ≤ the auth ceiling.
- [ ] 2.8 `pytest tests` and `make test-integration` green, then commit on `wt-ce-s2-wiring`.

## 3. S3: durable counters and pool boundary

- [ ] 3.1 Migration 028 and the models, per D8.
  - `concurrency_counters`: primary key `(bucket_start, epoch, mode, metric)`, columns `count bigint NOT NULL DEFAULT 0` and `max_value integer NULL`, CHECKs on `mode` and on `metric` (the closed set), and an index on `bucket_start`.
  - `concurrency_runs`: `run_id uuid` primary key, `epoch`, `mode`, `started_at`, `completed_through`, `clean_shutdown bool NOT NULL DEFAULT false` and `lossy bool NOT NULL DEFAULT false`, with an index on `started_at`.
  - `alembic check` must stay clean.
- [ ] 3.2 `concurrency_counters.py`:
  - `register_run()` at lifespan start mints a `run_id` and inserts the run row with `completed_through = started_at`.
  - `flush(clean=False)` runs in one transaction. It reads `t`, drains `counters()`, writes one multi-row `INSERT … ON CONFLICT DO UPDATE` keyed by the **event-time** buckets (count added, `max_value` taken as the greater), and updates the run row: `completed_through = floor_minute(t)`, or `t` with `clean_shutdown = true` when `clean`, plus `lossy` from the accumulator. The commit is synchronous; do **not** add to the `synchronous_commit` allow-list.
  - A failed flush calls `merge_back`, which keeps the original keys, and does not advance the watermark.
  - Prune counters and runs older than 35 days.
  - `coverage(session, start, end, epoch, mode) -> (watermark, uncovered_intervals)` for S4, per the design D8 rules.
- [ ] 3.3 `database.py`: an `AsyncAdaptedQueuePool` subclass overriding `_do_get`. It counts `sqlalchemy.exc.TimeoutError` as `pool_checkout_timeout` and re-raises unchanged, and updates the `pool_high_water` gauge on success. Wire it with `poolclass=`. If the hook is unstable, report and propose the fallback; do not switch silently.
- [ ] 3.4 `main.py`: the 60 s flush task, cancelled at shutdown, and a final `flush()` after `flush_all()` and before `engine.dispose()`. Sandbox mode skips it.
- [ ] 3.5 Tests (integration):
  - flush idempotence and addition across two flushes;
  - the watermark advancing on idle;
  - an incident at 12:00:50 flushed at 12:01:10 landing in bucket 12:00;
  - a failed flush followed by a retry keeping bucket attribution;
  - **a hard kill (no shutdown flush) with a restart under 180 s**, where coverage reports the interval from the killed run's watermark to the new start as uncovered;
  - a clean shutdown with a restart, where the gap is covered;
  - prune;
  - a failed flush re-merges;
  - 10,000 recorded requests produce one statement;
  - a forced real pool checkout timeout through each of MCP auth, the quota gate, a usage writer, a panel route and `/token` counts once each and re-raises the same exception class;
  - an embedding `TimeoutError` does not count;
  - the high-water gauge.
- [ ] 3.6 `pytest tests`, `make test-integration` and `make test-schema` green, then commit on `wt-ce-s3-counters`.

## 4. S4: readiness, report, panel

- [ ] 4.1 `concurrency_readiness.py` `window_stats`.
  - Rows are filtered on `params->'concurrency'->>'v' = '2'` only.
  - Executed and pre-body classification uses the existing `executed_sql` / `pre_body_refusal_sql`, with the weighted `slot_timeout` through the existing guarded cast. Import these; do not edit `usage_stats.py`.
  - Tool-pressured calls are read from `observations`.
  - Counters are read from `concurrency_counters`. Coverage and the durable watermark come from S3's `coverage()` over `concurrency_runs`.
  - Window boundaries are aligned to whole minutes. The default end is the watermark, and an explicit end past it is INSUFFICIENT_DATA.
  - It reports the mode and epoch sets present, the uncovered intervals, the watermark, and the latest qualifying sub-window start.
  - It computes the full window and the last 72 h.
- [ ] 4.2 Pure `evaluate(stats, target)`: Q1–Q3 and E1–E6, the minimum windows, the single-mode/single-epoch rule, coverage, INSUFFICIENT_DATA, and the both-windows rule. Thresholds are module constants referencing the design.
- [ ] 4.3 `scripts/concurrency_report.py` and `make concurrency-report DAYS=… TARGET=queue|enforce`, run through `docker exec`. Output a table plus one JSON line. The exit code is 0 only when every criterion is PASS.
- [ ] 4.4 Panel section per spec. The admin-only block holds live `snapshot()`, the windowed counters, gaps and the verdict. Use existing CSS, with no inline script or handler, and show an empty state.
- [ ] 4.5 Tests:
  - boundary tests for every criterion;
  - E1 at 2 and at 3 of 2,000;
  - an E4 regression in the last 72 h;
  - a thin window;
  - a mixed mode/epoch window;
  - an uncovered interval after an unclean run end, including one under 180 s;
  - an explicit end beyond the watermark giving INSUFFICIENT_DATA, and the default end clamped to the watermark;
  - incidents at 11:59:50 and at 12:59:50 against a 12:00–13:00 window;
  - an incident recorded but not yet flushed not being certified;
  - legacy rows with `queue_ms` but no provenance excluded;
  - Q2 duplicate-source immunity;
  - one pool timeout fails Q3/E6;
  - a queue overrun with `over_quota` counted pre-body;
  - admin versus non-admin rendering, with no inline handlers;
  - integration against real PG with synthetic v2, legacy, coalesced and counter rows.
- [ ] 4.6 `pytest tests` and `make test-integration` green, then commit on `wt-ce-s4-readiness`.

## 5. Merge, seams, docs, gates (supervisor)

- [ ] 5.1 Merge S1, S2, S3, then S4. On the merged tree run the full offline suite, `make test-integration`, `make test-schema`, `openspec validate concurrency-enforce-ready --strict`, `make audit`, and an image build.
- [ ] 5.2 Seams:
  - `counters()` must be drained by S3's flush and fed by S2's middleware and writer paths plus S3's pool subclass;
  - `snapshot()` and `evaluate()` must each have a production caller;
  - delete the `TOOL_CONNECTION_MULTIPLIER` alias;
  - no `other` class may remain.
- [ ] 5.3 Docs (S5):
  - `rate-limits.md`: the concurrency section rewrite covering modes, transport waits and disconnects, classes, budget, D5 kept, D9 token policy, rollout and criteria; the table rows 1a, 1b and 5c.
  - `usage-attribution.md`: `params.concurrency`, `concurrency_queue`, `transport_queue_ms`.
  - `security-event-logging.md`: the outcome values.
  - `control-panel.md`: the section.
  - `schema-and-migrations.md`: a 028 section.
  - CLAUDE.md: add "four modes" and "a concurrency refusal spends rate tokens, no quota" to the concurrency bullet; **keep** "leases remain held through telemetry".
- [ ] 5.4 `openspec-verifier` against this change; iterate to zero blocking gaps.
- [ ] 5.5 Adversarial Codex, two rounds by default. Attack surfaces:
  - a queue overrun turned refusal, or losing a row;
  - lease or waiter leaks across disconnect, cancellation and grant races;
  - body-replay corruption;
  - a double-counted or uncovered readiness numerator;
  - a missed pool timeout;
  - a criterion that can PASS on legacy, mixed or thin data.

## 6. Deploy in shadow (step 0)

- [ ] 6.1 Reconcile the deploy-dir `.env` concurrency block to the new `.env.example` block, then dry-run the settings with the new image against it.
- [ ] 6.2 `make deploy`. Confirm migration 028 applied and `make db-check` is clean. Compare the startup INFO line (effective settings and epoch) with the intended block, since validation alone does not prove it.
- [ ] 6.3 Live MCP exercise with owned temporary fixtures:
  - one tool per class (`read_note`, `keyword_search`, `semantic_search`, `find_related`, and `create_note` + `delete_note` on a scratch note);
  - a parallel batch of 6 `read_note` calls.

  Confirm that every row carries `params.concurrency`, that pressured rows carry v2 metadata, and that the current run's `completed_through` advances each minute. Remove the fixtures, and report which tools were called.
- [ ] 6.4 Owner browser pass on `/admin/performance`: the section renders, the admin block is visible, and there are zero CSP violations.
- [ ] 6.5 `openspec archive concurrency-enforce-ready -y`, commit, push, and link #188. #188 stays open as the rollout tracker.

## 7. Rollout (operator, tracked on #188)

- [ ] 7.1 After a covered shadow window (through the durable watermark) of ≥ 3 days and ≥ 300 calls on one epoch, run `make concurrency-report TARGET=queue` and read the panel verdict. If Q1–Q3 pass, set `MCP_CONCURRENCY_MODE=queue` and recreate. Post the JSON on #188.
- [ ] 7.2 Check the queue rollback triggers daily for 7 days (p95 `queue_ms` > 1,000 ms in any 24 h, or an agent timeout attributable to queueing, means back to shadow).
- [ ] 7.3 After a covered queue window of ≥ 7 days and ≥ 1,000 calls on one epoch, run `make concurrency-report TARGET=enforce` and read the panel verdict. If E1–E6 pass over both the whole window and the last 72 h, set `MCP_CONCURRENCY_MODE=enforce` and recreate. Post the JSON on #188.
- [ ] 7.4 Check the enforce rollback triggers daily for 7 days (`transport_refused` > 0; weighted `slot_timeout` > max(2, 0.2 %) in 24 h; any `pool_checkout_timeout`), which mean back to queue. Close #188 after 7 clean days, and record the final settings in `rate-limits.md`.
