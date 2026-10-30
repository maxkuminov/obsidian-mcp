## 1. Confidence rule and setting

- [ ] 1.1 `src/config.py`: `search_low_confidence_similarity: float = Field(0.50, ge=0.0, le=1.0)`, with non-finite values refused; test out-of-range (`1.5`, `-0.1`, `nan`) refuses startup
- [ ] 1.2 `src/services/search_confidence.py`: the pure `confidence(keyword_notes, top_similarity, threshold)` rule (D1), the two hint constants, and `CONFIDENCE_HINT_MAX = 140`; a table test over keyword 0/≥1 × top None/below/equal/above × threshold 0/0.5, plus negative similarity at threshold 0
- [ ] 1.3 `src/services/hybrid.py`: `HybridResult.top_similarity`, the max over the semantic arm's deduped notes, None when the arm returned none or did not run; `src/services/embeddings.py`: expose the arm's best similarity from `semantic_search` / `semantic_candidates` before truncation to `limit` (no new statement, no change to ordering or projection; the projection-identity suite runs unmodified)

## 2. Rendering: token, hint, bounds

- [ ] 2.1 `hybrid_search`: put `confidence: …` after `arms:` (and after `reranked:` on `rerank=true`) on every return path; place the hint after the transition line and before the rows (on the zero-result path, before the keyword-only footer)
- [ ] 2.2 `semantic_search` `rerank=false`: add the token to the header and the zero-result line, and the semantic hint; `rerank=true`: add the token after the counts and before `reranked:`, with the hint reserved in the budget
- [ ] 2.3 Reserve the header with the call's actual value, plus `1 + len(hint)` when `low`; extend `_abbreviated_row` for hybrid with the `explain omitted` rung, then the hint-withdrawal rung (D5), both after the existing omitted forms
- [ ] 2.4 Re-pin `HYBRID_HEADER_MAX` 405, `HYBRID_RERANK_HEADER_MAX` 444, `SEMANTIC_RERANK_HEADER_MAX` 199 and the floor arithmetic (990). Re-measure every D5 row in a test (cut-sum and omitted-sum for keyword-only, both+transition, both+transition+rerank, low+transition+hint, low+transition+hint+rerank, semantic rerank low), and assert each response ≤ 1,000 by rendering, not only by arithmetic
- [ ] 2.5 Re-base the #367 `rerank=false` byte-identity fixtures and every existing header-string assertion. The diff must add exactly ` — confidence: ok` (or `low` and the hint), nothing else
- [ ] 2.6 Render tests: the live gibberish case (hybrid, keyword arm empty, similarities 0.467/0.464/0.460, threshold 0.50 → `low`, hint present, rows identical to the threshold-0 render), the live paraphrase case (semantic, 0.618/0.618/0.611 → `ok`, no hint), the strict boundary (= threshold → `ok`), the keyword-only path (rows → `ok`; empty → `low`, hint before the footer), the same value for `rerank` true/false and for `limit=1`, and a note whose title or chunk holds `low confidence:` / `confidence: ok` (it cannot forge the header token or a hint line)
- [ ] 2.7 `src/mcp_server/server.py`: one sentence on the `confidence` token in the `semantic_search` and `hybrid_search` descriptions (still ≤ 4,000 on the wire, the #343 test green; the routing sentence unchanged)

## 3. Usage rows

- [ ] 3.1 Record `confidence` on every executed `semantic_search` / `hybrid_search` call, and `top_similarity` (3 decimals) when a semantic note existed. No `error` marker. Test both tools, the zero-result path and the keyword-only path
- [ ] 3.2 Confirm the panel analytics readers and the search-eval miner ignore the unknown keys (the existing tests stay green)

## 4. Harness: recall@20 and recall@50

- [ ] 4.1 `scripts/search_eval/scoring.py`: `METRICS` gains `recall@20` and `recall@50`; `_per_query`, the slices, deltas, bootstrap CI and the table columns `R@20` and `R@50`; a recall@k with k > the arm's staged depth is null (delta and CI null); `schema_version` 2; the verdict text unchanged
- [ ] 4.2 `scripts/search_eval/runner.py`: `DEFAULT_DEPTH` 50 (and `__main__` `--depth` default); `metrics.py` docstring updated
- [ ] 4.3 Tests: metric edge cases (relevant at rank 30 → R@20 0, R@50 1/|rel|), a depth-20 `runfile:` baseline (null recall@50, null delta), CI reproducibility with the new metrics, and `assert_no_leak` still green
- [ ] 4.4 Regenerate `tests/fixtures/search_eval/expected-report.json` once, with the reason in the commit message. The diff must contain only the new metric keys, depth, `schema_version` and the deltas for the new metrics

## 5. Docs (same PR)

- [ ] 5.1 README: settings table row for `SEARCH_LOW_CONFIDENCE_SIMILARITY` (default 0.50, range 0–1, `0` = never low, model-dependent); one line on the `confidence` token in the search-tools section
- [ ] 5.2 `.env.example`: the setting, commented, with the bge-m3 rationale and the "re-choose on model change; 0 is safe for an untuned model" warning
- [ ] 5.3 `docs/architecture/search.md`: a new "Confidence (#389)" section (D1–D7, the live numbers, the asymmetry argument, L1–L11); update the hybrid D7 bound paragraph and the #367 D9 numbers; the harness section gains recall@20/50, depth 50, null beyond depth and schema 2; mention the threshold beside the retrieval-fingerprint text
- [ ] 5.4 `docs/architecture/usage-attribution.md`: the two keys
- [ ] 5.5 CLAUDE.md: one key-decisions bullet (the token, the rule, the threshold is model-dependent, no row dropped)
- [ ] 5.6 Public-repo grep: the setting name in README and `.env.example`; no host paths

## 6. Gates

- [ ] 6.1 `pytest tests` offline green; `make test-integration` green (projection identity, hybrid integration); `make search-eval` (fixture) green; `openspec validate search-confidence --strict`
- [ ] 6.2 `openspec-verifier` subagent (non-author) against the spec deltas; iterate to zero blocking gaps
- [ ] 6.3 Adversarial Codex (mandatory: search-path change), `-m gpt-daybreak-blue-latest`, read-only, committed tree. Framing: a false `low` on a good page pushes an agent away from the right notes; a missing `low` on noise is the bug being fixed; the threshold errs toward silence; and the 1,000-character bound must hold on every path. Two rounds by default; triage per the workflow; declined findings go to the limitations list
- [ ] 6.4 Search eval gate: `make search-eval-snapshot`, then `make search-eval` on a restored copy with the incumbent as `BASELINE`; attach `report.json` only if `attachable: true`; read recall@20/50. From the export, tabulate `top_similarity` for `identifier` vs `prose` once rows accrue (informs Q2; no gate)

## 7. Archive

- [ ] 7.1 `openspec archive search-confidence -y` as the last commits of the feature branch, docs included; PR with `Closes #389`

## 8. Deploy

- [ ] 8.1 Merge → CI → image → Flux; watch the rollout

## 9. Live check (substitutes the user-representative pass)

- [ ] 9.1 `hybrid_search("xqzvplorth", limit=5)` → `confidence: low` with the hint, 5 rows. `semantic_search("xqzvplorth")` → `low`
- [ ] 9.2 A real paraphrase on both tools → `confidence: ok`, no hint. An identifier on `hybrid_search` (keyword hit) → `ok`
- [ ] 9.3 The usage rows carry `confidence` / `top_similarity`; record the observed similarities against D2's table (rechunk backlog size noted) and report which tools were called
