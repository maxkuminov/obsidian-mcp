Slice A (renderer) and slice B (outcome + coalescer drop) touch different functions. B depends on A only in the integration test. Run them sequentially in one worktree, or in parallel with A owning `src/services/usage_params.py` and B owning `rate_limits.py` / `security_events.py`. Both edit `tools.py` `_write_usage_row_admitted`, so the second to merge rebases. The gates in §5 are run by non-authors.

## 1. Slice A: the renderer (D1–D4)

- [x] 1.1 New `src/services/usage_params.py`: `render_usage_params(params: dict) -> dict` per design D1. It is iterative with an explicit stack and no depth limit, renders `<cycle>` for a container already on the current path and `<unrenderable>` on an internal failure, and never raises. It reuses `src.services.vault.non_finite_token` for the float tokens. `RENDERED_PARAMS_KEY = "rendered_params"`. Its docstring states the escape grammar and why `\x00` and not `\0`.
- [x] 1.2 `src/mcp_server/tools.py` `_write_usage_row_admitted`: as its first statement, render `params` only when the key is present and not `None` (`if values.get("params") is not None: values = dict(values, params=render_usage_params(values["params"]))`). An absent key stays absent, and `None` stays `None`. Add regression tests for both (Codex spec review, finding 4). This way the FK-cleared `retry` is built from the rendered dict. Update the docstring and `_MAX_PARAM_LEN`'s comment with the D4 bound (≤ 6 × 200 + 1 stored characters per top-level string).
- [x] 1.3 Unit tests `tests/test_issue_310_usage_params.py`: every D1 table row, the backslash/literal-escape distinction, clean input → identical object contents and no marker, nested key/value positions, sorted `rendered_params` listing only affected keys, 10,000-deep list, self-referential dict, raising `__str__`, `{nan: 1, ".nan": 2}` first-key-wins, and tuple/set → list.
- [x] 1.4 Reserved-name test: iterate every tool registered in `src/mcp_server/server.py` and assert that no parameter is named `rendered_params`. Assert that no `_*_MARKER` constant in `tools.py`, no `refusals` code and no key written by `src/services/timing.py` equals it.
- [x] 1.5 Transfer pin (design D3): the `{"path": …, "size": int}` shapes `_log_row` receives in `src/transfer/routes.py` are unchanged by `render_usage_params`. This is a test only, with no transfer code change.

## 2. Slice B: write outcome and the coalescer drop (D5, D6)

- [x] 2.1 `src/mcp_server/tools.py`:
  - `UsageWriteOutcome` (`landed`, `failed`, `unstorable`);
  - `_write_usage_row_admitted` returns it, classifying the terminal exception with `poison_sqlstate`, imported lazily from `src.services.indexer` with a `# local: avoids a cycle` comment;
  - `write_usage_row_outcome(values)` is the admission-wrapped writer (the writer-permit refusal is `failed`);
  - `write_usage_row(values) -> bool` becomes `await write_usage_row_outcome(values) is UsageWriteOutcome.landed`.

  `usage_log_failed` emissions are unchanged in fields and reasons.
- [x] 2.2 `src/services/rate_limits.py` `write_planned_row`: call `write_usage_row_outcome`. On `unstorable`, `planned.entry.in_flight -= 1`, no `requeue`, and emit `usage_refusal_row_dropped` (`tool`, `reason` = the row's `params["error"]`, `count` = `planned.weight`, `user_id` = `planned.values.get("user_id")`, subject `subject_for(user_id=…)`), wrapped in `try/except Exception: pass`. Every other branch is unchanged. Update the docstring and the `PlannedRow` docstring ("acknowledged, requeued, or dropped").
- [x] 2.3 `src/services/security_events.py`: add `"usage_refusal_row_dropped": frozenset({"tool", "reason", "count", "user_id"})` with a comment pointing at #310.
- [x] 2.4 Unit tests: the outcome classification matrix (design Tests 3), and the coalescer behaviour (design Tests 4), including the statement-counting assertion that a dropped flushed row issues no INSERT on the next tick, `in_flight` returning to 0, an open window's `pending` not inflated, `failed` still requeued with its original start, and exactly one emitter call per drop under the strict field check.

## 3. Integration (real Postgres)

- [x] 3.1 `tests/integration/test_issue_310_usage_params_pg.py`, guarded on `PGVECTOR_TEST_ADMIN_URL` like its neighbours. For each bad value in design Tests 6, `write_usage_row` returns `True`, and the row read back has the D1 rendering and `rendered_params`.
- [x] 3.2 Same file, end to end: a test-registered tracked tool called with a NUL argument lands its row; a lone-surrogate argument refused with `argument_not_encodable` lands its refusal row; a `rate_limited` refusal for a lone-surrogate argument lands its coalesced row on the first attempt.
- [x] 3.3 Same file: with `render_usage_params` monkeypatched to the identity, a NUL-bearing flushed planned row gives exactly one failing INSERT, one `usage_refusal_row_dropped` with the right `count`, and no INSERT on the following `flush_expired` call.
- [x] 3.4 Run `SCHEMA_TEST_CONTAINER=omcp-schema-w2a SCHEMA_TEST_PORT=55444 make test-integration`, and do not run it concurrently with `test-schema`. Also run the offline suite (`pytest tests`).

## 4. Docs (same PR)

- [x] 4.1 `docs/architecture/usage-attribution.md`: a new section, "What reaches `params` is rendered, not trusted (#310)". It covers the three PostgreSQL rejections and why `json.dumps` passes them; the escape grammar and `rendered_params`; why the renderer sits at `_write_usage_row_admitted` and not in `named_params()`; the transfer exclusion; the D4 length bound; and L1, L2, L4.
- [x] 4.2 `docs/architecture/rate-limits.md`, "A planned row is acknowledged, and a failed one is requeued": add the unstorable exception (drop, no window re-created, one `usage_refusal_row_dropped` with the weight), the revised Σ arithmetic, and L3 and L5.
- [x] 4.3 `docs/architecture/security-event-logging.md`: a catalogue row for `usage_refusal_row_dropped`, and one sentence on the `usage_log_failed` row ("a `DataError` here after #310 is a renderer defect, not a caller input").
- [x] 4.4 `CLAUDE.md`: no new key-decisions bullet unless review asks for one. The usage-attribution note is the home. Confirm that no README or `.env.example` change is needed, because there is no new setting (grep-check before the PR).

## 5. Gates

- [x] 5.1 `openspec validate usage-params-sanitiser --strict`.
- [ ] 5.2 `openspec-verifier` against this change.
- [ ] 5.3 Adversarial Codex pass. This change touches the usage-log write path that an agent's audit trail depends on, and the coalescer's arithmetic. Frame it so that the expensive failures are a row silently lost, a rendering that can be confused with literal input, and a non-data failure wrongly dropped.
- [ ] 5.4 After deploy, an end-to-end exercise against the live server: call `read_note` with a NUL in `section` (a logged argument), and `list_notes` with a NaN in its logged `frontmatter` filter (`{"x": NaN}` has to be sent as a raw JSON-RPC body, because most clients refuse to serialise NaN). Confirm both rows on `/admin/usage`, carrying `rendered_params`. Report which tools were called.
- [ ] 5.5 Archive (`openspec archive usage-params-sanitiser -y`) as the last commit of the feature PR, with `Closes #310`.
