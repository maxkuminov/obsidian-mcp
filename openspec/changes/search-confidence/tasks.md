## 1. Confidence rule and setting

- [ ] 1.1 `src/config.py`: `search_low_confidence_similarity: float = Field(0.0, ge=0.0, le=1.0)`, refusing non-finite values. Test that `1.5`, `-0.1` and `nan` refuse startup, and that the default is 0
- [ ] 1.2 `src/services/search_confidence.py`: the pure `confidence(keyword_all, top_similarity, threshold)` (D1), the two hint constants, and `CONFIDENCE_HINT_MAX = 140`. Table test over `keyword_all` 0/≥1 (with and without `any` rows) × top None/below/equal/above × threshold 0/0.5, plus a negative similarity at threshold 0
- [ ] 1.3 `src/services/hybrid.py`: `HybridResult.top_similarity`, the max over `s_rows`, None when the arm is empty or did not run. No statement changes and no change to `semantic_search` / `semantic_candidates` / `keyword_search` return types. The tool reads `results[0]["similarity"]` (or `candidates[0]` before rerank). The projection-identity suite runs unmodified

## 2. Rendering: token, conditional hint, bounds

- [ ] 2.1 `hybrid_search`: put the token after `arms:` (and after `reranked:`) on every page, zero-result and keyword-only included. Place the hint after the transition line; on the zero-result path, before the keyword-only footer
- [ ] 2.2 `semantic_search`: token on the `rerank=false` header and zero-result line, with an unconditional hint there (that page has no budget). On `rerank=true`: token after the counts, before `reranked:`, and the conditional hint. The provider-input refusal stays unchanged and carries no token
- [ ] 2.3 Conditional hint (D4): render without the hint, then with it reserved. Keep it only when the paths and every row line are byte-identical; otherwise withdraw it before any abbreviation or drop
- [ ] 2.4 `explain omitted` as the last rung of hybrid's first-row ladder. Re-pin `HYBRID_HEADER_MAX` 405 (`rerank=false`), `HYBRID_RERANK_HEADER_MAX` 444, `SEMANTIC_RERANK_HEADER_MAX` 199 and the floor arithmetic of 990. Re-measure every D5 row for both `ok` and low-plus-`any` (50 `any` rows, `all 0`, snippet token), and assert by rendering that each page is ≤ 1,000
- [ ] 2.5 Re-base the #367 `rerank=false` byte-identity fixtures and the header-string assertions. The diff must add exactly ` — confidence: ok` (or `low` plus the hint), nothing else
- [ ] 2.6 Render tests:
  - live gibberish: hybrid, 0 all-rows, 0.467/0.464/0.460, threshold 0.50 → `low` with the hint;
  - `xqzvplorth notes`: 0 all-rows, `any` rows, 0.467 → `low`;
  - live paraphrase: semantic, 0.618 → `ok`, no hint;
  - the strict boundary, and the default 0 → `ok` everywhere;
  - keyword-only: all-rows → `ok`; only `any` / none → `low`, with the hint before the footer;
  - the same value for `rerank` true and false;
  - the multi-row cap-boundary case (hint withdrawn, same 10 rows; 200 characters more → hint kept, same rows);
  - token eviction at the boundary: a page whose 10th row fitted pre-change with < 17 spare shows 9 rows, `shown: 9`, and the cap footer's count (and `more: true` on `rerank=true`);
  - the provider-input refusal: no token, no keys;
  - a note whose title or chunk holds `low confidence:` or `confidence: ok` cannot forge either
- [ ] 2.7 `src/mcp_server/server.py`: one sentence on the token in the `semantic_search` and `hybrid_search` descriptions (≤ 4,000, the #343 test green, the routing sentence unchanged)

## 3. Usage rows

- [ ] 3.1 On every page of both tools, whatever the threshold, record `confidence` and `top_similarity` (3 decimals, when a candidate existed), plus `keyword_all` on hybrid. None of them is an `error` marker. Refusals record none. Test both tools, the zero-result path, the keyword-only path, and threshold 0
- [ ] 3.2 Confirm the panel analytics readers and the miner tolerate the new keys

## 4. Harness

- [ ] 4.1 `scoring.py`: `METRICS` gains `recall@20` and `recall@50`, flowing through `_per_query`, the slices, deltas, CI and the `R@20` / `R@50` columns. Null any recall@k above the arm's `staged_depth`, together with its deltas and CIs. Set `schema_version` to 2 and leave the verdict text unchanged
- [ ] 4.2 `runner.py`: `DEFAULT_DEPTH` 50 (and the `__main__` default). `staged_depth` per arm in the staged metadata and the report: the generation depth, or for a `runfile:` the minimum of the generation depth and the validated source `depth` (missing, non-int or ≤ 0 → exit 64)
- [ ] 4.3 Tests:
  - the metric edge cases (rank 30 → R@20 0, R@50 1/|rel|);
  - end to end, a depth-20 `runfile:` imported into a depth-50 generation → `staged_depth: 20`, null recall@50, null delta and CI;
  - the invalid-depth runfiles → exit 64;
  - CI reproducibility;
  - `assert_no_leak`
- [ ] 4.4 Calibration:
  - `mining.py` writes `confidence-rows.jsonl` (`tool`, `class`, `top_similarity`, `keyword_all`, `clicked`; no text, no path);
  - a new `scripts/search_eval/confidence.py` plus the `confidence` subcommand: post-change rows identified by the `confidence` key, keyword-only rows included; per tool and per class; one PASS/FAIL/INSUFFICIENT_DATA line per tool (≥ 14 days and ≥ 200 clicked-through pages, < 1/50), no aggregate; a JSON of counts and rates carrying provenance and `attachable`, with the `NOT ATTACHABLE` line for multi-user data;
  - fixture rows and tests: per-tool lines, a keyword-only false low, insufficient data (short span, zero denominator), pre-change rows excluded, the two-user fixture non-attachable and the `USER_ID=` fixture attachable, no leak
- [ ] 4.5 Regenerate `tests/fixtures/search_eval/expected-report.json` once, with the reason in the commit message. The diff may contain only the new metric keys, `staged_depth`, depth, `schema_version` and the new deltas

## 5. Docs (same PR)

- [ ] 5.1 README: a settings row for `SEARCH_LOW_CONFIDENCE_SIMILARITY` (default 0 = off, range 0–1, model-dependent, bge-m3 candidate 0.50, see DEPLOYMENT.md) and one line on the token
- [ ] 5.2 `.env.example`: the setting, commented, with the "calibrate first; re-calibrate on model change" warning
- [ ] 5.3 `DEPLOYMENT.md`: a "Calibrating search confidence" section with the D2 recipe (14 days and 200 clicked-through pages per tool, a single-user export, both lines PASS at < 1/50), the rule that only attachable output leaves `.search-eval/`, and the statement that the reference deployment sets a value only after both lines pass
- [ ] 5.4 `docs/architecture/search.md`:
  - a new "Confidence (#389)" section (D1–D7, the live numbers, the asymmetry argument, L1–L13);
  - the hybrid D7 and #367 D9 numbers;
  - the harness section (recall@20/50, depth 50, `staged_depth`, schema 2, the `confidence` subcommand);
  - the threshold beside the fingerprint text
- [ ] 5.5 `docs/architecture/usage-attribution.md`: the three keys. CLAUDE.md: one key-decisions bullet
- [ ] 5.6 Public-repo grep: the setting in README, `.env.example` and DEPLOYMENT.md; no host paths

## 6. Gates

- [ ] 6.1 `pytest tests` green offline. Also green: `make test-integration`, `make search-eval` (fixture) and `openspec validate search-confidence --strict`
- [ ] 6.2 `openspec-verifier` (non-author), iterated to zero blocking gaps
- [ ] 6.3 Adversarial Codex (mandatory: search-path change), `-m gpt-daybreak-blue-latest`, read-only, on the committed tree. Framing:
  - a false `low` on a good page pushes an agent away from the right notes;
  - a missing `low` on noise is the bug being fixed;
  - the hint must never cost a row;
  - the 1,000-character bound must hold on every page.

  Two rounds, triaged per the workflow
- [ ] 6.4 Search eval gate: `make search-eval-snapshot`, then `make search-eval` on a restored copy against the incumbent `BASELINE` (re-staged at depth 50). Attach the report only if `attachable: true`

## 7. Archive

- [ ] 7.1 `openspec archive search-confidence -y` as the last commits, docs included; the PR carries `Closes #389`

## 8. Deploy

- [ ] 8.1 Merge → CI → image → Flux; watch the rollout

## 9. Live check (substitutes for the user-representative pass)

- [ ] 9.1 At the shipped default (0): `hybrid_search("xqzvplorth", limit=5)`, `hybrid_search("xqzvplorth notes")` and `semantic_search("xqzvplorth")` → `confidence: ok`. Their usage rows carry `top_similarity` (and `keyword_all: 0` on hybrid)
- [ ] 9.2 On a non-production instance, or after calibration, with the threshold at 0.50: both `xqzvplorth` and `xqzvplorth notes` read `low` with the hint; a real paraphrase reads `ok` with no hint; an identifier with an all-terms keyword hit reads `ok`
- [ ] 9.3 Report which tools were called, the observed similarities against D2's table, and the size of the rechunk backlog
