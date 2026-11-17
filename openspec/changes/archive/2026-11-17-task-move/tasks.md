Refs #430. Read `proposal.md`, `design.md` (D1–D19, the owner decisions, the accepted limitations and the adversarial scenarios) and `specs/` before starting. The archived `openspec/changes/archive/2026-11-10-task-tools/` and `2026-11-11-task-rule/` are the source of every invariant this builds on.

**Scope: the smaller first version** (owner decision, Max, 2026-11-16: Q0). Build only what the spec deltas require. `design.md`'s last section, "Full design: the documented next step", is **not** part of this change: no nested-task moves, no callout tasks, no re-framing, and no task below implements them. Do not start implementing until Codex spec review round 2 has been folded in (0.2).

**Base check, every slice:** `openspec/changes/task-move/` exists, `openspec/specs/file-permissions/spec.md` exists (merged in #448; the base must be at or after fa27234), and no migration is added by any slice.

There are four slices (design §"Slice plan"): **M1** and **M2** in wave 1, in parallel; **M3** and **M4** in wave 2. Touch only the files the slice's row lists, and do not bundle other findings. Group 5 runs on the merged result.

## 0. Before implementation

- [x] 0.1 Owner decisions (Max, 2026-11-16): every recommendation accepted (Q0 the smaller first version, Q3–Q10; Q1/Q2 do not apply), recorded in `design.md` ("Owner decisions") and folded into the design, specs and these tasks.
- [x] 0.1a Rebased onto `main` at fa27234 and reconciled with #443, #444 and #448 (`design.md`, "Spec review record").
- [x] 0.2a Codex spec review round 1 (FAIL, 7 findings), folded into `design.md` ("Spec review record").
- [x] 0.2 Codex spec review round 2, verification only, framed as a defensive PASS/FAIL review whose expensive failure is a lost or clobbered line in either note; findings folded into `design.md` ("Spec review record").
- [x] 0.3 `openspec validate task-move --strict` green after the folds.

## 1. M1: the pure planner (`src/services/task_write.py`, additions only)

- [x] 1.1 `MoveArgs`, `MovedBlock`, `MovePlan` and the five functions with the D19 signatures. Every existing function keeps its behaviour; `Plan` gains only defaulted fields.
- [x] 1.2 The moved block (D3): resolution through `_locate`; the uncapped block; the `^block` ids carried.
- [x] 1.3 The scope refusals (D3, D5), each `task_malformed` naming the line and pointing at `edit_note`: a nested task, a quoted task, a blockquote line, a fence line or a heading line in the block, a quoted destination anchor. Every accepted block's line contents are inserted unchanged (D4).
- [x] 1.4 Placement (D5) on the destination (after removal, for the same note), the marker kept, no prefix copied, the planned owners.
- [x] 1.5 Terminators (D6) and the removal span with its end-of-file rule (D7).
- [x] 1.6 `verify_move_insert` and `verify_removal` (D8), and the combined same-note verification (D13).
- [x] 1.7 Duplicates at the destination and `resume` (D9); the block-id collision check (D8); `destination_still_holds` (D10 6(b)).
- [x] 1.8 `plan_relocated_removal` for the phase-2 retry (D10 rows 9–12, returning which of `source_missing`, `source_changed`, `source_ambiguous`).
- [x] 1.9 `tests/test_task_move_plan.py`: the unit cases and the corpus property test in design §"Test plan" (not the full design's frame-pair matrix); `tests/test_task_write.py` green, its only change being the two new codes added to its pinned task-code set (per 2.1).

## 2. M2: wiring and the partial outcome

- [x] 2.1 `src/services/refusals.py`: `task_move_incomplete` in `BODY_CODES`, `resume_unmatched` and `block_id_conflict` in `TASK_CODES`; optional `destination_path`, `source_id`, `destination_id`, `reason` on `Refusal`, rendered absent-not-null, allowed only with that code (a construction check); `reason` limited to the D11 values (`permissions_not_applied` included); update any test that pins the code set or the field list.
- [x] 2.2 `src/mcp_server/tools.py`: `move_task_impl` under `_tracked("move_task", [...], transforms=…, write_class=True, arg_char_caps=…)` (D15 params), and a two-phase runner implementing D10–D14 exactly: both targets opened and anchored, the same-file check (D13), the per-note ladders (D12), planning off the loop, size checks, dry run, destination publish with its retry, destination refresh then event, source publish whose synchronous callback runs the destination presence check before the write (D10 step 6), its re-locating retry, the `resume` path, source refresh then event, and every outcome of the D10 table with the D11 and D18 renderings. `vault_fs.PermissionsNotApplied` is told apart from other `OSError`s by type: at the destination it is row 6a (`io_failure`, nothing written), at the source row 14a (`permissions_not_applied`, no retry); a `File changed while editing` from either write, #447's P1/P2 and staged-bytes differences included, is that note's in-call conflict (rows 4, 9–12). Both writes go through `write_file_at(…, expected=)`, so both get the #447 edit rule; nothing in the runner chooses a permission rule. The presence check's three results (holds, changed → `destination_changed`, unreadable → `destination_unverified`); the task-rule note through the existing `_task_rule_note` on success and dry run (D16). `_run_task_write` keeps its behaviour.
- [x] 2.3 (Not needed: M1 and M2 were built in one worktree, so no stub was written.) Until M1 merges, `task_write.plan_move*` are stubs with the D19 signatures marked `TODO(#430)`; swap them after the merge.
- [x] 2.4 `tests/test_task_move_e2e.py`: every row of the D10 table (6a and 14a included) by monkeypatching each publication; the D12 matrix; usage params and result fields; event and refresh order; dry run; each note's mode, ACL and group kept across a move (the #447 edit rule, on a filesystem with POSIX ACLs where the test host has them, else the mode); a destination at an excluded sync-conflict path (`index (destination): may lag (not_indexed)`, no notice line).
- [x] 2.5 (Written; not run by the implementer, see the implementation report.) `tests/integration/test_task_move_pg.py` on a throwaway database (own container name and port): read-your-writes for both notes (and the Q10 lag case: the move completes and the response names the lagging note), two attributed events, the opposite-moves race, a held row lock, tenancy, and convergence of `tasks_source_hash` after the next pass.

## 3. M3: the published surface (after M1 and M2)

- [x] 3.1 `src/mcp_server/server.py`: `@mcp.tool(title="Move task", annotations=_DESTRUCTIVE)` registered after `add_task`; a description ≤ 4,000 characters leading with its contract (every argument, `readwrite`, the id rule, top-level unquoted tasks only, both hashes, destination-first with the presence check, `task_move_incomplete` and its `complete with:` line, `resume`, the L17 window in one sentence, `dry_run`, the codes); the instructions sentence of D17 (lead byte-identical, total ≤ 2,000); the annotation comment.
- [x] 3.2 `src/mcp_server/tool_permissions.py`: `move_task` added (13 names).
- [x] 3.3 `src/services/concurrency.py`: `move_task` in `write`; `CLASS_MAPPING_VERSION = 7` with its history line.
- [x] 3.4 `src/mcp_server/vault_guide_primer.md` and its test: the `move_task` sentence (neutral framing).
- [x] 3.5 Fixtures: `tests/fixtures/tools_list_full_v3.json` (new, generated deliberately over HTTP as v2 was); `tools_list_schema_additions.json` with `move_task` appended; `tools_list_readonly_oauth.json` untouched; a test that v3 minus `move_task` equals v2 byte for byte.
- [x] 3.6 Every count and registry in the D17 table (including `tests/integration/test_scoped_tools_list_pg.py`, `tests/test_hybrid_search_offline.py`, `tests/test_semantic_rerank.py`): re-run the D17 searches, at least for `32`, `twelve`, `eleven`, `TWELVE`, `== 11`, `CLASS_MAPPING_VERSION`, `ADDED_AFTER_INVENTORY`, `COVERAGE`, and every per-tool map that lists `add_task`, and add `move_task` (with a refused and a success reference in the coverage registry).

## 4. M4: documentation

- [x] 4.1 `docs/architecture/vault-tools.md`: a "Moving a task (#430)" section under "Task tools (#428)" carrying D3–D16 as built (the smaller first version, with one paragraph naming the full design as the documented next step) and the limitations; the Tool metadata table; "Who sees which tools" (13 names, v3 oracle); "Refusal codes" (`task_move_incomplete` and its `reason` values); "Metadata and visibility"; "Published file permissions (#447)": `move_task`'s two publications in the edit rule's list.
- [x] 4.2 `docs/architecture/usage-attribution.md` (params and result fields), `rate-limits.md` (write class, one charge per move), `indexing-and-embeddings.md` (two refreshes per move, never two row locks at once).
- [x] 4.3 `README.md`: tool count 33, the task tool list (the destination-first sentence the readme-positioning delta requires), the twelve write tools, the thirteen write-required tools, the destructive list, the upgrade note.
- [x] 4.4 `CLAUDE.md`: counts (33 tools, 20 of 33, thirteen names, twelve-tool write bucket, v3 oracle), the MCP tools "Tasks" paragraph, and a short key-decision bullet: destination first, the presence check inside the source publication, partial outcomes that state only what was verified, explicit `resume` (never automatic), top-level unquoted tasks only (byte for byte, no re-framing), each note keeps its own permissions, the L17 window.
- [x] 4.5 `DEPLOYMENT.md` and `docs/deployment-kubernetes.md`: the upgrade note (33 tools, write bucket 12, `CLASS_MAPPING_VERSION` 7 starts a new readiness epoch; no migration).

## 5. Verification on the merged result

- [x] 5.1 Grep for a production caller of every seam in design §"Slice plan".
- [x] 5.2 Full offline suite; the task and move integration modules on a throwaway database; `openspec validate task-move --strict`.
- [x] 5.3 `openspec-verifier` against this change.
- [x] 5.4 Adversarial Codex, mandatory (a write tool on the destructive-write surface): focused passes over the 48 scenarios in design §"Adversarial scenarios", each traced to SAFE or a finding with file and line; triage per the review budget.
- [ ] 5.5 After deploy: an end-to-end exercise of `move_task` against the live server (two notes, same note, a task with an indented sub-task, a nested task refused, a dry run, the duplicate refusal by moving onto an identical copy, then `resume=true` on it; a partial outcome cannot be forced live), naming the tools actually called. No `user-representative` pass (no panel change).
- [ ] 5.6 Archive with `openspec archive task-move -y` as the last commit of the feature branch, with the docs.
