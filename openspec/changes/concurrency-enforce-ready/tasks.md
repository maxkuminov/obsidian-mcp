# Tasks: concurrency-enforce-ready (#188)

The work is three code slices plus a supervisor seam and docs pass. Each code
slice is implemented by an independent Opus subagent in its own worktree, on a
flat branch. File ownership is disjoint. Where a file is shared, the slice owns
a **named region** of it. A subagent that needs to edit outside its files or
region stops and reports instead of editing.

| Slice | Branch | Sequencing | Owns |
| --- | --- | --- | --- |
| S1: controller, config, budget | `wt-ce-s1-core` | none | `src/services/concurrency.py`, `src/services/pool_budget.py`, `src/config.py` (**the concurrency block only**: the `mcp_concurrency_*` fields and `_validate_concurrency`), `.env.example` (the MCP concurrency block only), `tests/test_issue_261_controller.py`, `tests/test_issue_261_config.py`, new `tests/test_concurrency_queue_mode.py`, new `tests/test_concurrency_metadata.py`, new `tests/test_concurrency_transport_wait.py`; plus, **mechanically only**, the literal `resource_class="other"` → `"light"` in every test file that carries it (14 files today, `grep -rln "resource_class=[\"']other[\"']" tests`), including files S2 later edits further |
| S2: middleware and `_tracked` wiring | `wt-ce-s2-wiring` | **after S1 merges** | `src/mcp_server/auth.py` (the `_concurrency_response`/`_emit_concurrency_pressure` helpers and the admission block in `APIKeyMiddleware.__call__`), `src/mcp_server/tools.py` (`write_usage_row`, `_rate_refusal_template`'s shadow merge, and the concurrency region of `_tracked`: transport-observation pickup, tool admission, lease release, `queue_ms`/`transport_queue_ms` recording), `src/services/security_events.py` (the `mcp_concurrency_pressure` catalogue comment/outcome set only), `tests/test_issue_261_auth.py`, `tests/test_issue_261_tool_admission.py`, `tests/integration/test_issue_261_concurrency_pg.py`, new `tests/integration/test_concurrency_tool_checkout_peaks_pg.py` |
| S3: observability | `wt-ce-s3-observe` | **after S1 merges** (parallel with S2) | new `src/services/concurrency_readiness.py`, new `scripts/concurrency_report.py`, `Makefile` (a `concurrency-report` target and its `help` line), `src/database.py` (pool high-water listener only), `src/control_panel/routes.py` (`performance_page` only), `src/control_panel/templates/performance.html`, new `tests/test_concurrency_readiness.py`, new `tests/test_panel_concurrency_section.py`, new `tests/integration/test_concurrency_readiness_pg.py` |
| S4: seams and docs | supervisor | after S2 and S3 merge | `docs/architecture/rate-limits.md`, `docs/architecture/usage-attribution.md`, `docs/architecture/security-event-logging.md`, `docs/architecture/control-panel.md`, `CLAUDE.md` (the concurrency bullet only) |

**Base check, first thing in every brief.** Confirm that
`openspec/changes/concurrency-enforce-ready/design.md` exists and that
`git log` contains `f3ecba5`. For S2 and S3, also confirm that
`src/services/pool_budget.py` defines `CLASS_CONNECTIONS` (S1 merged). Stop and
report if any check fails.

**Contract S2 and S3 build against (set by S1, pinned by S1's tests):**

- `Controller.request(fp)` and `Controller.auth()` are `async`. `auth()` takes
  the request's transport deadline, and the middleware creates that deadline
  with `controller.transport_deadline()`.
- `Admission` gains `overrun: Pressure | None` and `queue_ms`.
- `Controller.snapshot() -> dict` returns mode, limits, `pool_demand`, live
  counts, `counters: {(stage, scope, outcome): int}` and `started_at`.
- `concurrency.shadow_metadata(observations, *, configured_wait_ms)` and
  `concurrency.queue_metadata(observations)` implement design D4 and D7.
- `pool_budget.tool_demand(tools, caps)` and `CLASS_CONNECTIONS`.

**Integration tests.** S2 and S3 change database-observable behaviour and run
`make test-integration`. A local `pytest tests` run skips `tests/integration/`
and does not count as proof. `make test-integration` and `make test-schema`
share a container and must never run concurrently. No migration: `make
test-schema` is not required.

## 0. Spec review before code

- [ ] 0.1 Commit this proposal and open the coordination PR, linking #188.
- [ ] 0.2 Codex spec review, framed as a defensive PASS/FAIL review. "Wrong" for this product means a legitimate agent call refused (a live-path breakage), a destructive write, or a silently wrong search result. Ask specifically:
  - (a) Can any admissible class mix exceed the D2 `tool_demand` bound, given atomic admission and independent class ceilings?
  - (b) Can D5's early release let a call's usage write or tail telemetry check out a connection that the budget does not count?
  - (c) Can a queue-mode overrun consume quota, write a pre-body row, or change a body outcome?
  - (d) Does the shared transport deadline let a request hold the auth permit while it waits?
  - (e) Are the E1–E6 and Q1–Q3 thresholds testable and unambiguous?
  - (f) Does removing "shadow requires wait 0" create any path where shadow waits?

  Fold the findings in before implementation. Two rounds maximum, triaged per the workflow.
- [ ] 0.3 Owner decision on the open question (`queue` mode). If the owner declines it, drop D7, the queue scenarios and Q1–Q3, and make E1–E6 read zero-wait shadow data instead.

## 1. S1: controller, config, budget

- [ ] 1.1 `pool_budget.py`: add `CLASS_CONNECTIONS` (write 2, the others 1) and a pure `tool_demand(tools, caps)` (greedy, highest multiplier first, then by name). Keep `TOOL_CONNECTION_MULTIPLIER = max(CLASS_CONNECTIONS.values())` as a deprecated alias with a `TODO(#188)` comment. `tests/integration/test_issue_261_concurrency_pg.py` (S2's file) still imports it; S2 removes that import and S4 deletes the alias.
- [ ] 1.2 `concurrency.py`: replace `other` with `light` and `scan` in `TOOL_CLASSES` per design D3, and set `CLASSES` to the five names.
- [ ] 1.3 `concurrency.py`: stage ranking and `shadow_metadata` per D4: earliest stage first, never truncated, `schema: 2`, `configured_wait_ms`. Add `queue_metadata`.
- [ ] 1.4 `concurrency.py`: add mode `queue`. `_admit_wait` returns an overrun grant instead of `Admission(None, …)` on deadline, overflow or zero-wait miss. The timeout branch must grant exactly once, including when a grant raced the timeout. Shutdown remains a refusal.
- [ ] 1.5 `concurrency.py`: async `request()` and `auth()` through `_admit_wait` in queue and enforce, sharing a per-request deadline object. Add waiter dimensions for request (global, fingerprint) and auth. Retain the fingerprint registry entry while waiting. Shadow and off stay immediate.
- [ ] 1.6 `concurrency.py`: bounded since-boot counters keyed `(stage, scope, outcome)`, incremented inside the existing non-awaiting transitions, plus `snapshot()`.
- [ ] 1.7 `config.py`:
  - add `queue` to the mode literal;
  - add `mcp_concurrency_transport_wait_seconds` (default 2, range 0..5), `mcp_concurrency_request_waiters` (64), `mcp_concurrency_fingerprint_waiters` (16), `mcp_concurrency_auth_waiters` (32), `mcp_concurrency_light` (4) and `mcp_concurrency_scan` (2);
  - change the defaults for `wait_seconds` (5, maximum 10), `requests` (64), `fingerprint` (20), `tools` (6), `tenant` (4), `principal` (3), `waiters` (64), `tenant_waiters` (32) and `principal_waiters` (16);
  - make `mcp_concurrency_other` `int | None = None`: `1` is ignored with a WARNING, any other value is refused.
- [ ] 1.8 `config.py` `_validate_concurrency`: apply the full hierarchy and coherence rules from the spec, the per-class budget through `tool_demand`, and error messages that name every term. Delete the shadow-requires-zero-wait rule and the class-sum rule.
- [ ] 1.9 `.env.example`: rewrite the concurrency block with the new defaults, the removed `OTHER` line, a comment explaining the four modes, and a comment explaining the budget formula.
- [ ] 1.10 Mechanical rename of `resource_class="other"` to `"light"` in the probe decorators of every test file that carries it. Change nothing else in those files.
- [ ] 1.11 Tests. Update the existing #261 controller and config tests to the new classes and defaults without weakening any existing invariant test (atomicity, eligible-FIFO, overflow stickiness, cancellation before and after a grant). New tests:
  - queue overrun on deadline, overflow and zero wait;
  - queue grant racing a timeout;
  - transport wait: shared deadline, auth ceiling 2 with 6 arrivals, cancellation while waiting at each stage;
  - metadata ranking and truncation;
  - `tool_demand` table tests, including the spec's 18 > 15 case and the default 14;
  - every coherence rule, one at a time;
  - `OTHER=1` warns and `OTHER=3` refuses, through a real written env file;
  - all three modes validate on the `.env.example` block.
- [ ] 1.12 `pytest tests` green, then commit on `wt-ce-s1-core`.

## 2. S2: middleware and `_tracked` wiring

- [ ] 2.1 `auth.py`: await `controller.request()` and `controller.auth()` with one transport deadline. The auth permit still encloses only `_authenticate`'s session. Record `transport_queue_ms`, and record transport observations for shadow **and** queue into `request_observations`. Keep the 429 shape. Emit `mcp_concurrency_pressure` with outcomes `shadow`, `refused`, `overrun`, and `waited` (only when the wait is over 100 ms).
- [ ] 2.2 `tools.py` `_tracked`:
  - pick up transport observations in shadow and queue;
  - build `concurrency_shadow` through the new ranked function with `configured_wait_ms`;
  - in queue, build `concurrency_queue` from the transport and tool admissions' waited and overrun data;
  - record `queue_ms` and `transport_queue_ms` in queue and enforce;
  - an overrun is not a refusal: take no slot_timeout path.
- [ ] 2.3 `tools.py` `_tracked`: release `slot_lease` immediately after the body returns or raises, and before `_log_usage`, `_record_tool_failure`, `tool_body_outcome` and the tail. Keep the `finally` backstop.
- [ ] 2.4 `tools.py` `write_usage_row`: merge the writer observation through the ranked function, so it never displaces an earlier-stage `code`. In queue, a writer overrun writes the row and marks the observation.
- [ ] 2.5 `security_events.py`: update the `mcp_concurrency_pressure` catalogue comment and outcome set. The field allow-list is unchanged.
- [ ] 2.6 Tests (offline):
  - the tool-registry mapping covers five classes, with no `other`;
  - early release: light ceiling 1, a blocked writer, and a second call granted;
  - a body exception releases before its failure row;
  - cancellation during the body releases exactly once;
  - a queue overrun row executes, consumes quota and writes no `slot_timeout` row;
  - an enforce parallel batch of 8 `read_note` calls at the defaults sees zero refusals;
  - the MCP-REFUSAL line is unchanged for an enforced `slot_timeout`;
  - the auth-burst scenario through the real middleware.
- [ ] 2.7 Tests (integration, real PG): new `test_concurrency_tool_checkout_peaks_pg.py`, parametrized over **every** registered tool with minimal valid arguments against a fixture vault. Assert per-task checkout peak ≤ `CLASS_CONNECTIONS[class]`, and name the tool, class and peak on failure. In `test_issue_261_concurrency_pg.py`, replace the `TOOL_CONNECTION_MULTIPLIER` import with `CLASS_CONNECTIONS`, then extend it: auth wait with actual checkouts ≤ auth ceiling, and the global checkout peak under a default-config mixed burst ≤ `auth + tool_demand + writers`. If any tool measures above its class multiplier, **stop and report**: that is a design decision (D2), not an S2 edit.
- [ ] 2.8 `pytest tests` and `make test-integration` green, then commit on `wt-ce-s2-wiring`.

## 3. S3: observability

- [ ] 3.1 `concurrency_readiness.py`: `window_stats(session, window, user_id)`, one read-only aggregation per design D6. It counts tool pressure from `observations`, not from `code`. It uses weighted `1 + suppressed` for `slot_timeout` through the existing guarded cast helper in `usage_stats.py` (import it; do not edit `usage_stats.py`), and computes the last-72 h sub-window alongside the full window. It excludes rows without `schema: 2` or `queue_ms`.
- [ ] 3.2 `concurrency_readiness.py`: pure `evaluate(stats, process, target)` implementing Q1–Q3 and E1–E6 with their minimum windows, the both-windows rule, the uptime rule, INSUFFICIENT_DATA, and SEE_PANEL when `process` is `None`. The thresholds are module constants with a comment pointing at the design.
- [ ] 3.3 `database.py`: `checkout`/`checkin` listeners that keep a checked-out high-water mark, and a `pool_gauge()` accessor. No other change.
- [ ] 3.4 `scripts/concurrency_report.py` and `make concurrency-report DAYS=… TARGET=queue|enforce` (run through `docker exec` in the container, like the existing maintenance targets). Output a table plus one JSON line. The exit code is 0 only when every criterion the script can evaluate passes.
- [ ] 3.5 `routes.py` `performance_page` and `performance.html`: the concurrency section per the panel spec. Aggregates use the page's scoping, and the process block is admin-only. No inline script or `on*=` handler; use existing CSS classes; show an empty state.
- [ ] 3.6 Tests:
  - `evaluate` boundary tests for every criterion (E1 at 2 and 3 of 2,000; E4 regression in the last 72 h; thin window; uptime under 72 h; legacy rows excluded; one `TimeoutError` fails Q3 and E6);
  - the panel renders for admin and non-admin, with the process block hidden for non-admins, and the page carries no inline handler;
  - integration: `window_stats` against real PG with synthetic schema-2, queue, legacy and coalesced rows.
- [ ] 3.7 `pytest tests` and `make test-integration` green, then commit on `wt-ce-s3-observe`.

## 4. Merge, seams, docs (supervisor)

- [ ] 4.1 Merge S1, then S2 and S3. On the merged tree run the full offline suite, `make test-integration`, `openspec validate concurrency-enforce-ready --strict`, `make audit`, and an image build.
- [ ] 4.2 Seams: grep for production callers of `snapshot()`, `pool_gauge()`, `queue_metadata`, `tool_demand` and `evaluate`, since each must have one. Delete the `TOOL_CONNECTION_MULTIPLIER` alias once nothing imports it, and confirm that nothing still names the `other` class.
- [ ] 4.3 Docs. Rewrite the concurrency section and the table rows 1a, 1b and 5c of `rate-limits.md` (modes, transport wait, classes, budget formula, early release, rollout and criteria), and move the superseded #261 text into the alternatives and history. Also update:
  - `usage-attribution.md`: `schema: 2`, `concurrency_queue`, `transport_queue_ms`;
  - `security-event-logging.md`: outcome values;
  - `control-panel.md`: the concurrency section and its admin-only block;
  - the CLAUDE.md concurrency bullet: replace "leases remain held through telemetry" with "the slot lease ends with the body", and name the four modes.

## 5. Verification gates

- [ ] 5.1 `openspec-verifier` against this change. Iterate to zero blocking gaps.
- [ ] 5.2 Adversarial Codex (a mandatory trigger: admission, gate order, usage attribution). Attack surfaces:
  - a queue overrun that turns into a refusal or loses a row;
  - a lease leaked or double-released across early release, cancellation and grant races;
  - the transport deadline holding the auth permit or a connection;
  - a pool demand bound violated by a real tool;
  - a readiness criterion that can PASS on thin or legacy data.

  Two rounds by default; declined findings go into the design's limitations.

## 6. Deploy in shadow (step 0)

- [ ] 6.1 Before the recreate, reconcile the deploy-dir `.env` concurrency block to the new `.env.example` block (the old pinned `FINGERPRINT=4` now fails validation). Dry-run the new image's settings against the deploy `.env` (`docker run --rm --env-file … <image> python -c "from src.config import settings"`). Confirm `MCP_CONCURRENCY_MODE=shadow`.
- [ ] 6.2 `make deploy`. `make db-check` stays clean (no migration).
- [ ] 6.3 Live MCP exercise, with owned temporary fixtures: at least one tool per class (`read_note`, `keyword_search`, `semantic_search`, `find_related`, and `create_note` + `delete_note` on a scratch note), plus a parallel batch of 6 `read_note` calls. Confirm the rows carry `queue_ms` or schema-2 metadata where pressured, and remove the fixtures. Report which tools were called.
- [ ] 6.4 Owner browser pass on `/admin/performance` with devtools open: the section renders, the admin block is visible, and there are zero CSP violations.
- [ ] 6.5 Archive the change (`openspec archive concurrency-enforce-ready -y`), commit and push, and link #188. Keep #188 open as the rollout tracker.

## 7. Rollout (operator, after archive; tracked on #188)

- [ ] 7.1 After ≥ 3 days and ≥ 300 calls: run `make concurrency-report TARGET=queue` and read the panel verdict. If Q1–Q3 pass, set `MCP_CONCURRENCY_MODE=queue` and recreate. Post the report JSON on #188.
- [ ] 7.2 Check the queue rollback triggers daily for 7 days (tool `queue_ms` p95 > 1,000 ms in any 24 h, or an agent-side timeout attributable to queueing, means back to shadow).
- [ ] 7.3 After ≥ 7 days, ≥ 1,000 calls and ≥ 72 h uptime: `make concurrency-report TARGET=queue` → `TARGET=enforce`, plus the panel verdict. If E1–E6 pass on both windows, set `MCP_CONCURRENCY_MODE=enforce` and recreate. Post the report JSON on #188.
- [ ] 7.4 Check the enforce rollback triggers daily for 7 days (any `mcp_concurrency_pressure` outcome `refused`; weighted `slot_timeout` > max(2, 0.2 %) in 24 h; any pool timeout) → back to `queue`. Close #188 after 7 clean days, and record the final settings in `rate-limits.md`.
