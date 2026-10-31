## 0. Pre-flight

- [x] 0.1 Base check: the branch `hybrid-any-fill` contains c491326 (#391); `git ls-files .venv` is empty
- [x] 0.2 Owner decisions recorded in `design.md` (2026-10-31): bar narrowed, `explain` uses `1/(k+55)`

## 1. Fusion (`src/services/hybrid.py`)

- [x] 1.1 `HYBRID_ANY_FILL_OFFSET = HYBRID_ARM_DEPTH` beside `HYBRID_ARM_DEPTH`, with a comment stating the D1 bound (`1/(k+51) < 1/(k+50)` for every k)
- [x] 1.2 `_any_fill_offset_override: ContextVar[int | None]` (default `None`), with a comment saying only `scripts/search_eval_arms/hybrid.py` sets it
- [x] 1.3 `fuse(keyword_paths, semantic_paths, k, *, any_paths=frozenset(), any_offset=0)`: the keyword term uses `rank + any_offset` for paths in `any_paths`; the tie-break key stays `min(unoffset ranks)`; return the keyword fusion rank as well (or let the service derive it); every existing caller and test of `fuse` keeps its result with the defaults
- [x] 1.4 `hybrid_search`: `any_paths` = keyword rows with `match == "any"`; `any_offset` = the override when set, else `HYBRID_ANY_FILL_OFFSET`, **only when the semantic arm ran**, else 0 (D3)
- [x] 1.5 `HybridRow.keyword_fusion_rank` set on every keyword row (`keyword_rank + offset` for a fused fill row, else `keyword_rank`); `best_rank` unchanged

## 2. Rendering and description

- [x] 2.1 `_hybrid_server_fields`: the keyword `explain` term uses `keyword_fusion_rank` (falling back to `keyword_rank` when unset, so hand-built rows in tests keep working); `keyword_rank:` and the preview choice stay unoffset; no header token
- [x] 2.2 `hybrid_search` description (`src/mcp_server/server.py`): the fusion sentence drops "with equal votes" and states the same reciprocal-rank formula for semantic and all-terms keyword ranks, with a fill (`match: any`) keyword rank entering offset by 50, below every all-terms row (design D4's wording, 3,976 characters); ≤ 4,000 characters (`tests/test_issue_343_tool_metadata.py`); `hybrid_search_impl` docstring follows

## 3. Harness (`scripts/search_eval_arms/hybrid.py`)

- [x] 3.1 `make_arm_alt` accepts `hybrid_alt_any_offset` ∈ {0, 50} (default 50); any other value → `UsageError` (exit 64) before the generation is reserved; `required_options` unchanged (`hybrid_alt_k`)
- [x] 3.2 With 0, the arm sets `_any_fill_offset_override` to 0 around its service call (`set` / `reset(token)`); module docstring updated
- [x] 3.4 `python -m scripts.search_eval gate` (ADDED search-evaluation requirement): reads `report.json` deltas for one arm against the report's baseline, prints mean / CI95 / PASS|FAIL per (metric × slice), writes a Markdown table to `--out`, exits 0/1/64; refuses a non-`OK` verdict; requires `attachable: true` and `provenance.users == 1` on the candidate's and the baseline's report (`--baseline-report` for a `runfile:` baseline), else exit 64 with no cell printed and `--out` neither created nor replaced; offline tests for each exit code, including the multi-user report (verdict `OK`, baseline, eight cells, `attachable: false` → 64, no file) and a pre-existing `--out` left unchanged
- [x] 3.3 AST guard test: no module under `src/` assigns or `.set()`s `_any_fill_offset_override`

## 4. Tests

- [x] 4.1 `fuse` unit tests (release-bar fixture proof: a fill row can never outrank an all-terms row through the keyword arm alone, for k ∈ {1, 60, 1000} and every fill and all-terms rank in 1–50): the spec scenarios (1/110 vs 1/111; E before J; fill-only below semantic-only rank 50; unoffset tie-break; `any_offset=0` equals the old `fuse` on random inputs)
- [x] 4.2 Byte-identity: every existing fixture with `keyword_any = 0` renders identically to origin/main (pin with the current offline and integration fixtures); the keyword-only path with `any` rows renders identically
- [x] 4.2b Snippet boundary (offline): `KEYWORD_SNIPPET_ROWS=1`, A all-rank-1 only, F any-rank-50 + semantic-50; before: F first with the snippet; after: A first with the snippet and `matched_in`; A's line differs only in `preview_kind`/`preview`/`matched_in`
- [x] 4.3 Identity oracle (integration, `tests/integration/test_hybrid_search_pg.py`), incumbent (override 0) vs candidate: union identity on the full fused lists **before slicing**; at `limit=50` the exchange rule (non-fill relative order unchanged, membership differences only fill-bearing rows exchanged for union members, non-fill lines byte-identical except snippet-boundary reassignment)
- [x] 4.3b Rerank window (offline): 1 `all` + 49 `any` keyword rows and 50 disjoint semantic rows at k=60; pin the first-50 window before (≈ 25 + 25) and after (the `all` row + semantic 1–49), and that `_hybrid_search_reranked` receives the new window
- [x] 4.4 (`tests/test_hybrid_any_fill.py`: the E-before-J scenario and the fill-only-below-semantic case) The issue's shape, synthetic (no vault data in the repo): job-posting-like titles matching one query word plus an opinion note with all words in a near paraphrase; assert the fill-only rows fall below the semantic-only rows and that the explain terms sum to the score
- [x] 4.5 Harness: `make_arm_alt` with `hybrid_alt_any_offset=0` equals the pre-#403 fusion; invalid value exits 64; a `make_arm` query running concurrently with an alt query sees no override (`tests/test_hybrid_search_eval.py`)

## 5. Bounds re-measure

- [x] 5.1 `test_the_fixed_bounds` and the rerank arithmetic in `tests/test_hybrid_rerank.py`: worst rows carry `match: any` with fusion rank 100 on the both-arms path; assert the `rerank=false` structure ≤ 345 (projected 338 from the measured 337) and the `rerank=true` structure ≤ 367; constants unchanged
- [x] 5.2 Re-measure the pinned worst cases (projected: keyword-only 981 / 1,008 → 989 unchanged; both arms `rerank=false` cut 1,000; both arms `rerank=true` omitted 1,008, `explain omitted` 988); the both-arms `rerank=false` minimum-cap test still keeps the 40–45 cut; if a figure differs from the spec delta, correct the delta first
- [x] 5.3 Header bounds 405 / 444 re-asserted unchanged

## 6. Docs

- [x] 6.1 `docs/architecture/search.md`: "Hybrid search (#354)": the fill offset, D1's bound, the unoffset tie-break and preview, the keyword-only exemption, the rerank-window effect, the identity oracle, L1–L3; the "Bounds (D5)" figures; "Measuring it (D9)": `hybrid_alt_any_offset`
- [x] 6.2 CLAUDE.md `hybrid_search` bullet: one clause, "a `match: any` keyword row enters RRF at `rank + 50` (`HYBRID_ANY_FILL_OFFSET` = arm depth; never on the keyword-only path; tie-break unoffset) (#403)"; the floor figures if they moved
- [x] 6.3 README hybrid bullet: one sentence that a keyword note matching only some words counts below every all-words match
- [x] 6.4 DEPLOYMENT.md "Measuring search quality": the `make_arm_alt` `hybrid_alt_any_offset=0` baseline invocation (design D6)
- [x] 6.5 Public-repo grep: no host paths or vault content in the diff; no new setting, so `.env.example` unchanged (verify)

## 7. Gates

7.2 and 7.3 (the real-data snapshot, the candidate-vs-incumbent run and the attachable gate) are left for a separate operator pass; 7.4–7.6 and 8.x follow it.

- [x] 7.1 `openspec validate --all --strict` (41/41); offline `pytest tests` (7,839 passed, with `OMCP_ALLOW_SKIP_TRANSFER_INTEGRATION=1`); `make test-integration SCHEMA_TEST_CONTAINER=haf-integ SCHEMA_TEST_PORT=55464` (1,176 passed, at 8ed4169); `make search-eval` on the synthetic fixture (exit 0); `make audit` (clean). At 8b396c8 (round-1 gate fix, `gate.py` only): offline 7,846 passed, fixture `make search-eval` exit 0, integration not re-run. With the round-2 fix (runner, scoring, gate, schema 3): offline 7,856 passed / 1,179 skipped, fixture `make search-eval` exit 0, `make test-integration SCHEMA_TEST_CONTAINER=haf-integ SCHEMA_TEST_PORT=55464` 1,176 passed, strict validate 41/41
- [ ] 7.2 Search-eval gate (CLAUDE.md row): restore a `BACKUP_FULL=1` copy, `make search-eval-snapshot`, then one generation at the candidate commit with `ARMS="keyword semantic scripts.search_eval_arms.hybrid:make_arm scripts.search_eval_arms.hybrid:make_arm_alt" ARM_OPTS="hybrid_alt_k=60 hybrid_alt_any_offset=0" BASELINE=scripts.search_eval_arms.hybrid:make_arm_alt`; `USER_ID=` export on multi-user; attach `report.json` only if `attachable: true`; `CORPUS_LOSS` invalidates; expected movement in the hybrid arm only
- [ ] 7.3 Release bar: `python -m scripts.search_eval gate --report .search-eval/report.json --arm scripts.search_eval_arms.hybrid:make_arm --metrics recall@10,recall@20,recall@50,ndcg@10 --slices overall,class:identifier --min-ci-lower -0.01 --out .search-eval/hybrid-any-fill-gate.md` must exit 0. FAIL means any of the eight cells has a candidate-minus-baseline CI95 lower bound < −0.01. The two paths are `.search-eval/report.json` (the scored report, attached only if `attachable: true`) and `.search-eval/hybrid-any-fill-gate.md` (the PASS/FAIL table), and the PR body carries **only** a table produced by a successful run of the gate on an attachable single-user report (on a multi-user deployment, run `make search-eval-export USER_ID=…` first; a gate exit of 64 means no table is published); the issue's query added as one named judgment in `.search-eval/manual.jsonl` (tenant data, git-ignored; relevant = the opinion note), and its rank reported for both arms, for information only (no rank target: the query is semantic-led, design D7)
- [ ] 7.4 `openspec-verifier` subagent (not an implementer) against the deltas; iterate to zero blocking gaps
- [ ] 7.5 Adversarial Codex (mandatory: a ranking change an agent acts on without a human seeing the query), framed per CLAUDE.md (silently wrong search results); attack the D1 bound, D3's keyword-only exemption, the override seam and the oracle; two rounds by default, triage per the workflow file, declined findings recorded as accepted limitations
- [ ] 7.6 `openspec archive hybrid-any-fill -y` and the docs above as the last commits; one PR, `Closes #403`

## 8. After merge

- [ ] 8.1 Watch the rollout (CI → image → Flux)
- [ ] 8.2 Live end-to-end (no `user-representative` pass, MCP-only change): re-run `hybrid_search("My opinion on AI replacing software engineers", limit=12)` and compare with design D7's recorded page (expect the two fill-only rows gone from the top 12, the fill rows' `explain` terms at `rank + 50`); a query with `keyword_any = 0` (unchanged page); `hybrid_search(rerank=true)` on the issue's query; report which tools were called
