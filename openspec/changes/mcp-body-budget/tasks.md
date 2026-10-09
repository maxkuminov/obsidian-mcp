# Tasks: mcp-body-budget (#322)

Four code slices and a supervisor docs pass. Each code slice is implemented by
an independent Opus subagent in its own worktree, on a flat branch. A subagent
that needs to edit outside its files or region stops and reports.

| Slice | Branch | Sequencing | Owns |
| --- | --- | --- | --- |
| S1: budget core and config | `wt-bb-s1-core` | none | new `src/services/body_budget.py`, `src/config.py` (the `mcp_body_*` fields, derivation and boot check only), `.env.example` (a new MCP body budget block only), `tests/conftest.py` (env-key list only), new `tests/test_body_budget.py`, new `tests/test_body_budget_config.py` |
| S2: middleware wiring | `wt-bb-s2-wiring` | **after S1 merges** | `src/mcp_server/auth.py` (the reservation step in `APIKeyMiddleware.__call__`, the 429 response helper, the receive-counting wrapper), `src/services/security_events.py` (`reason` vocabulary only, if it is enumerated), `src/main.py` (the startup log line only), new `tests/test_body_budget_middleware.py` |
| S3: `import_from_url` URL cap | `wt-bb-s3-url` | none (parallel with S1) | `src/config.py` (the `MAX_IMPORT_URL_CHARS` constant only), `src/mcp_server/tools.py` (`_url_host`, the `import_from_url_impl` decorator arguments), `src/mcp_server/server.py` (the `import_from_url` docstring), new `tests/test_import_url_cap.py` |
| S4: real-stack measurement and burst test | `wt-bb-s4-stack` | **after S2 merges** | new `tests/integration/test_body_budget_stack_pg.py`, new `scripts/measure_body_amplification.py` |
| S5: docs | supervisor | after S1–S4 merge | docs listed in §5 |

**S1 and S3 both touch `src/config.py`**, in disjoint regions: S3 adds a
module constant beside `MAX_SEARCH_QUERY_CHARS`, and S1 adds `Settings`
fields. The orchestrator merges S3 first.

**Base check, first thing in every brief.** Confirm that
`openspec/changes/mcp-body-budget/design.md` exists and that `git log`
contains the proposal commit. For S2, also confirm that
`src/services/body_budget.py` exists (S1 merged). For S4, confirm that
`APIKeyMiddleware` references `body_budget` (S2 merged).

## 1. S1 — budget core and config

- [ ] 1.1 `Settings`: add `mcp_body_memory_budget_bytes: int | None`
  (≥ 64 MiB when set), `mcp_body_memory_fraction: float = 0.5`
  ([0.1, 0.8]), `mcp_body_memory_multiplier: int = 8` ([4, 32]),
  `mcp_body_budget_wait_seconds: float = 15` ([0, 60], no inf or NaN),
  `mcp_body_budget_waiters: int = 8` ([1, 256]). The budget setting is
  **not** a `NullableLimit`: unset means "derive", never "disabled", and the
  field's comment must say so, because elsewhere null means off.
- [ ] 1.2 Derivation (design D2): a pure function
  `derive_body_budget(settings, cgroup_reader) -> BodyBudgetPlan(source, memory_budget, multiplier, capacity, small_lane, large_lane, cgroup_limit)`.
  The cgroup reader is injectable. It reads v2 `memory.max`, then v1
  `memory.limit_in_bytes`, and treats `max`, unreadable or ≥ 2⁶⁰ as none.
- [ ] 1.3 Boot check in a `Settings` model validator, or at controller
  construction if the cgroup read must stay out of settings load (document
  which). Fail when `large_lane < mcp_max_request_body_bytes` or
  `small_lane < 1 MiB`, with an error naming the four settings.
- [ ] 1.4 `BodyBudget`: two lanes and two FIFOs, one shared waiter bound.
  `async reserve(size, *, small, deadline, disconnected) -> BodyAdmission(admitted, lease, lane, queue_ms, refused_reason)`.
  Implement the borrow rule (small borrows from large only while the large
  FIFO is empty) and strict FIFO with no barging in the large lane. The
  lease's `release()` is idempotent, credits the source lane and re-runs
  admission small-first. Cancellation, timeout and disconnect remove the
  waiter, and a grant that raced the exit is handed back. A process singleton
  `get_body_budget()` with a reset hook for tests, mirroring
  `concurrency.replay_budget()`.
- [ ] 1.5 `.env.example`: a commented block for the five settings with the
  derivation formula, the 2 GiB worked example and the minimum container size
  for the defaults (≈ 1.1 GiB). Add the keys to `tests/conftest.py`'s env list.
- [ ] 1.6 Tests (`tests/test_body_budget.py`): the lane scenarios
  ("Small requests pass while the large lane is full", "Small requests cannot
  starve a waiting large request", "No barging among large requests");
  waiter-bound overflow; zero-wait refuses without suspending; deadline
  expiry; disconnect removes the waiter; cancellation while waiting and the
  grant-race hand-back; release is idempotent and wakes the next eligible
  waiter; the 50-request mixed burst ends at reserved 0 and empty queues.
- [ ] 1.7 Tests (`tests/test_body_budget_config.py`): the four derivation
  scenarios (2 GiB cgroup → 128/16/112 MiB; `max` → fallback 1 GiB; 512 MiB →
  boot error naming four settings; explicit 1.5 GiB → 192 MiB, source
  `setting`); range validation of each setting; v1 fallback path; the boot
  check against a raised `MAX_FILE_WRITE_BYTES`.

## 2. S2 — middleware wiring

- [ ] 2.1 In `APIKeyMiddleware.__call__`, after `_authenticate` returns
  `None` (success) and the auth lease is released: parse `Content-Length`,
  413 above `mcp_max_request_body_bytes` (the SDK's response shape), skip at
  0, and otherwise reserve (small if ≤ 1 MiB declared, large otherwise or when
  unknown). Do this through a second `ReceiveWatch` wrapping the first watch's
  `downstream()`, started only if the reservation suspends (the `_admit`
  pattern), and stopped at the handoff.
- [ ] 2.2 Receive-counting wrapper on the app's `receive`: sum body bytes, and
  deliver `http.disconnect` in place of a message that would cross the
  reservation and for every call after it.
- [ ] 2.3 Release the body lease in the existing `finally`, beside the request
  lease. On disconnect during the wait, return with no response.
- [ ] 2.4 `_body_budget_response(admission)`: the 429 with `Retry-After: 2` and
  `{"error", "code": "body_memory", "scope", "limit"}`. Emit
  `mcp_concurrency_pressure` with `reason="body:memory"`, outcome
  `refused`/`waited` (threshold `WAITED_EVENT_THRESHOLD_MS`). Do **not** touch
  `concurrency.counters()`. If `security_events` enumerates `reason` values
  for this event, add `body:memory`.
- [ ] 2.5 `src/main.py`: log the resolved `BodyBudgetPlan` once at startup
  (INFO, or WARNING for `fallback` or a budget above the cgroup limit).
- [ ] 2.6 Tests (`tests/test_body_budget_middleware.py`), each spec scenario
  in `mcp-request-routing` that is reachable in-process, using a probe
  downstream app and a fake `receive`/`send`:
  - nothing is read before the grant, and the body is byte-identical after it;
  - an oversized declaration gets an immediate 413 and the budget is
    unchanged;
  - a chunked request reserves the per-request limit from the large lane;
  - a lying `receive` gets `http.disconnect`;
  - the budget enforces in `shadow` and `off`;
  - an invalid bearer holds no budget;
  - the five × 20 MiB queue/refuse scenario;
  - a refusal consumes no bucket token, no quota and no usage row;
  - a disconnect while waiting;
  - release on SDK 400, on a raising app, and on cancellation in both
    phases;
  - the 429 shape is identical in keys to `_concurrency_response`.

## 3. S3 — `import_from_url` URL cap

- [ ] 3.1 `MAX_IMPORT_URL_CHARS = 8192` in `src/config.py` beside
  `MAX_SEARCH_QUERY_CHARS`, with a comment giving the presigned-URL rationale
  and saying that it is not the memory bound.
- [ ] 3.2 `import_from_url_impl`: `arg_char_caps={"url": MAX_IMPORT_URL_CHARS}`.
  `_url_host`: return `"<over-long>"` for a value whose `len` exceeds the
  cap, before `str()` or `urlsplit`.
- [ ] 3.3 The `server.py` docstring states the limit.
- [ ] 3.4 Tests (`tests/test_import_url_cap.py`): the three `file-transfer`
  scenarios (8,193 refused with `argument_too_long` and no resolver or client
  call; 8,192 reaches the policy checks; the usage row shows `<over-long>`
  and a patched `urlsplit` is never called with the value).

## 4. S4 — real-stack measurement and burst test

- [ ] 4.1 `scripts/measure_body_amplification.py`: start the app in a fresh
  subprocess (uvicorn, `--workers 1`), send one near-limit `write_file`
  envelope and one `import_from_url`-shaped envelope, and report peak RSS
  growth ÷ body length for each (sampled from `/proc/<pid>/status` `VmHWM`).
  Record the measured ratios in `docs/architecture/rate-limits.md` (S5).
- [ ] 4.2 `tests/integration/test_body_budget_stack_pg.py` (guarded on
  `PGVECTOR_TEST_ADMIN_URL`, like the rest of `tests/integration/`):
  - the multiplier guard (ratio ≤ `MCP_BODY_MEMORY_MULTIPLIER` for both
    shapes);
  - the six-writer burst at a 1 GiB budget: the process alive, peak RSS ≤
    baseline + 1 GiB + 32 MiB + 128 MiB, every request a success or a
    `body_memory` 429, and at least one success.

  Use a real readwrite API key and a temp vault.
- [ ] 4.3 Run `make test-integration` and attach the measured ratios and the
  peak RSS to the PR.

## 5. S5 — docs (supervisor)

- [ ] 5.1 `docs/architecture/rate-limits.md`: a new section, "The body-memory
  budget is always on (#322)", covering D1–D8, the measured multiplier,
  accepted limitations L1–L9, the alternatives rejected, the 429 added to
  "Standing residuals" as a transport refusal outside the contract, and the
  control table row.
- [ ] 5.2 `docs/architecture/vault-tools.md` "Three kinds of size cap": add
  the aggregate in-flight body budget as the process-wide counterpart of the
  per-request transport limit.
- [ ] 5.3 `docs/architecture/file-transfer.md`: the URL cap and the
  non-parsing logging transform.
- [ ] 5.4 `docs/architecture/security-event-logging.md`: the
  `mcp_concurrency_pressure` row gains `reason` `body:memory`.
- [ ] 5.5 `CLAUDE.md` key decisions: one bullet. The `/mcp` body budget is
  always on, has no shadow mode and no off switch, is derived from the cgroup
  limit × 0.5 ÷ 8, refuses to boot when a maximum write cannot fit, keeps a
  small lane, waits up to 15 s, and refuses with a transport 429 outside
  `MCP-REFUSAL`. Point to rate-limits.md.
- [ ] 5.6 `.env.example`: cross-check against S1's block. `README.md` only if
  it documents body or write limits.

## 6. Gates and archive

- [ ] 6.1 `openspec validate mcp-body-budget --strict`, the offline test
  suite, `make test-integration` and `make audit`. No migration is carried,
  so `make test-schema` is not required.
- [ ] 6.2 The `openspec-verifier` subagent against this change, iterating to
  zero blocking gaps.
- [ ] 6.3 Adversarial Codex (mandatory: transport admission and an
  availability control). The brief states that a false 429 on a supported
  write and a reservation leak (permanent budget loss) are the expensive
  failures. Two rounds by default, with triage per the workflow.
- [ ] 6.4 End-to-end against the live server after deploy: `write_file` with a
  ~20 MB base64 payload, `import_from_url` with an over-long URL, and a small
  `read_note` during a large write. Name the tools called in the report.
- [ ] 6.5 `/openspec-archive-change mcp-body-budget` (`openspec archive -y`)
  as the last commits of the feature branch, together with §5, in the same
  PR, which carries `Closes #322`.
