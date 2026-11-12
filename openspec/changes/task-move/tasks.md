Refs #430. Read `proposal.md`, `design.md` (D1–D19, the open questions, the accepted limitations and the adversarial scenarios) and `specs/` before starting. The archived `openspec/changes/archive/2026-11-10-task-tools/` and `2026-11-11-task-rule/` are the source of every invariant this builds on.

**Do not start implementing until the owner has answered the open questions Q1–Q9 in `design.md`** (or accepted the recommendations), and the Codex spec review has been folded in.

**Base check, every slice:** `openspec/changes/task-move/` exists, `src/services/task_rule.py` exists, and no migration is added by any slice.

There are four slices (design §"Slice plan"): **M1** and **M2** in wave 1, in parallel; **M3** and **M4** in wave 2. Touch only the files the slice's row lists, and do not bundle other findings. Group 5 runs on the merged result.

## 0. Before implementation

- [ ] 0.1 Owner decisions on Q1–Q9 recorded in `design.md` ("Owner decisions"); the design and specs amended where a decision differs from the recommendation.
- [ ] 0.2 Codex spec review (round 1, and a verification round 2), framed as a defensive PASS/FAIL review whose expensive failure is a lost or clobbered line in either note; findings folded into `design.md` ("Spec review record").
- [ ] 0.3 `openspec validate task-move --strict` green after the folds.

## 1. M1: the pure planner (`src/services/task_write.py`, additions only)

- [ ] 1.1 `MoveArgs`, `MovedBlock`, `MovePlan` and the five functions with the D19 signatures. Every existing function keeps its behaviour; `Plan` gains only defaulted fields.
- [ ] 1.2 The moved block (D3): resolution through `_locate`; the uncapped block; the heading refusal; the nested-under line; the `^block` ids carried.
- [ ] 1.3 Re-framing (D4): the prefix rule, the blank-line rule, the refusal naming the line.
- [ ] 1.4 Placement (D5) on the destination (after removal, for the same note), the marker kept, the planned owners.
- [ ] 1.5 Terminators (D6) and the removal span with its end-of-file rule (D7).
- [ ] 1.6 `verify_move_insert` and `verify_removal` (D8), and the combined same-note verification (D13).
- [ ] 1.7 Duplicates and convergence at the destination (D9).
- [ ] 1.8 `plan_relocated_removal` for the phase-2 retry (D10 rows 8–9).
- [ ] 1.9 `tests/test_task_move_plan.py`: the unit cases and the corpus property test in design §"Test plan"; `tests/test_task_write.py` runs unmodified and green.

## 2. M2: wiring and the partial outcome

- [ ] 2.1 `src/services/refusals.py`: `task_move_incomplete` in `BODY_CODES`; optional `destination_path`, `source_id`, `destination_id`, `reason` on `Refusal`, rendered absent-not-null, allowed only with that code (a construction check); update any test that pins the code set or the field list.
- [ ] 2.2 `src/mcp_server/tools.py`: `move_task_impl` under `_tracked("move_task", [...], transforms=…, write_class=True, arg_char_caps=…)` (D15 params), and a two-phase runner implementing D10–D14 exactly: both targets opened and anchored, the same-file check (D13), the per-note ladders (D12), planning off the loop, size checks, dry run, destination publish with its retry, destination refresh then event, source publish with its re-locating retry, source refresh then event, and every outcome of the D10 table with the D11 and D18 renderings. `_run_task_write` keeps its behaviour.
- [ ] 2.3 Until M1 merges, `task_write.plan_move*` are stubs with the D19 signatures marked `TODO(#430)`; swap them after the merge.
- [ ] 2.4 `tests/test_task_move_e2e.py`: every row of the D10 table by monkeypatching each publication; the D12 matrix; usage params and result fields; event and refresh order; dry run.
- [ ] 2.5 `tests/integration/test_task_move_pg.py` on a throwaway database (own container name and port): read-your-writes for both notes, two attributed events, the opposite-moves race, a held row lock, tenancy, and convergence of `tasks_source_hash` after the next pass.

## 3. M3: the published surface (after M1 and M2)

- [ ] 3.1 `src/mcp_server/server.py`: `@mcp.tool(title="Move task", annotations=_DESTRUCTIVE)` registered after `add_task`; a description ≤ 4,000 characters leading with its contract (every argument, `readwrite`, the id rule, both hashes, destination-first and `task_move_incomplete`, convergence on retry, `dry_run`, the codes); the instructions sentence of D17 (lead byte-identical, total ≤ 2,000); the annotation comment.
- [ ] 3.2 `src/mcp_server/tool_permissions.py`: `move_task` added (13 names).
- [ ] 3.3 `src/services/concurrency.py`: `move_task` in `write`; `CLASS_MAPPING_VERSION = 7` with its history line.
- [ ] 3.4 `src/mcp_server/vault_guide_primer.md` and its test: the `move_task` sentence (neutral framing).
- [ ] 3.5 Fixtures: `tests/fixtures/tools_list_full_v3.json` (new, generated deliberately over HTTP as v2 was); `tools_list_schema_additions.json` with `move_task` appended; `tools_list_readonly_oauth.json` untouched; a test that v3 minus `move_task` equals v2 byte for byte.
- [ ] 3.6 Every count and registry in the D17 table: re-grep for `32`, `twelve`, `eleven`, `TWELVE`, `== 11`, `CLASS_MAPPING_VERSION`, `ADDED_AFTER_INVENTORY`, `COVERAGE`, and every per-tool map that lists `add_task`, and add `move_task` (with a refused and a success reference in the coverage registry).

## 4. M4: documentation

- [ ] 4.1 `docs/architecture/vault-tools.md`: a "Moving a task (#430)" section under "Task tools (#428)" carrying D3–D14 and the limitations; the Tool metadata table; "Who sees which tools" (13 names, v3 oracle); "Refusal codes" (`task_move_incomplete`); "Metadata and visibility".
- [ ] 4.2 `docs/architecture/usage-attribution.md` (params and result fields), `rate-limits.md` (write class, one charge per move), `indexing-and-embeddings.md` (two refreshes per move, never two row locks at once).
- [ ] 4.3 `README.md`: tool count 33, the task tool list (the destination-first sentence the readme-positioning delta requires), the twelve write tools, the thirteen write-required tools, the destructive list, the upgrade note.
- [ ] 4.4 `CLAUDE.md`: counts (33 tools, 20 of 33, thirteen names, twelve-tool write bucket, v3 oracle), the MCP tools "Tasks" paragraph, and a short key-decision bullet: destination first, partial outcome, convergence, re-framing.
- [ ] 4.5 `DEPLOYMENT.md` and `docs/deployment-kubernetes.md`: the upgrade note (33 tools, write bucket 12, `CLASS_MAPPING_VERSION` 7 starts a new readiness epoch; no migration).

## 5. Verification on the merged result

- [ ] 5.1 Grep for a production caller of every seam in design §"Slice plan".
- [ ] 5.2 Full offline suite; the task and move integration modules on a throwaway database; `openspec validate task-move --strict`.
- [ ] 5.3 `openspec-verifier` against this change.
- [ ] 5.4 Adversarial Codex, mandatory (a write tool on the destructive-write surface): focused passes over the 33 scenarios in design §"Adversarial scenarios", each traced to SAFE or a finding with file and line; triage per the review budget.
- [ ] 5.5 After deploy: an end-to-end exercise of `move_task` against the live server (two notes, same note, a nested task, a dry run, and the convergence path by moving onto an identical copy; a partial outcome cannot be forced live), naming the tools actually called. No `user-representative` pass (no panel change).
- [ ] 5.6 Archive with `openspec archive task-move -y` as the last commit of the feature branch, with the docs.
