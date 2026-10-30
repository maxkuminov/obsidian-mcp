Refs #389

## Why

The search tools are investigation entry points: an agent takes a candidate list, reads the promising notes and follows wikilinks. What matters is recall at depth and honest signals about the candidate set, more than precision at rank one.

One of those signals is missing. `hybrid_search("xqzvplorth", limit=5)` returns five rows labelled `arms: both`, at similarity 0.36–0.49, with no warning (#389). A re-run live on 2026-10-30 put the best at 0.467. The keyword arm was empty, and the semantic arm always returns its nearest neighbours. `semantic_search` has the same blind spot. A real paraphrase on the same index scores 0.618. An agent cannot tell the first page from the second, so it reads noise instead of going to the graph.

Measuring at depth is the other half. The harness (#356) reports recall@5 and recall@10, but an agent reading past rank ten is served by recall@20 and recall@50. Fifty is both the largest `limit` the search tools accept and `hybrid_search`'s arm depth, so recall@50 is the ceiling of what one page can reach.

This change adds the honesty signal and the depth metrics. **No row is ever dropped.**

## What Changes

- **A `confidence: ok | low` header token on every page of `semantic_search` and `hybrid_search`**, part of the base response budget. Both tools use one pure rule. The token reads `low` exactly when three things hold:
  - `SEARCH_LOW_CONFIDENCE_SIMILARITY > 0`;
  - no keyword note matched all terms (`keyword_all == 0`; `match: any` filler is not evidence; `semantic_search` has no keyword arm);
  - the best similarity of the call's actual candidate set is below the threshold, or no candidate exists.

  `keyword_search` and `find_related` are untouched. A `semantic_search` provider-input rejection is a refusal, not a page, so it carries no token.
- **A conditional one-line hint when `low`.** For `hybrid_search`: ``low confidence: no keyword match on all terms and weak similarity; try `keyword_search` with an identifier, or `get_neighborhood` from a known note``. `semantic_search` gets a sibling line. The hint is at most 140 characters and sits after the header and the `index_transition:` line. It is withdrawn before it could change the shown rows: hint first, then the abbreviation ladder, then row drops.
- **Bounds re-measured.**
  - Headers: `hybrid_search` `rerank=false` 400 → 405 (floor 990); `rerank=true` 431 → 444; `semantic_search(rerank=true)` 185 → 199. The `confidence: ok` and low-plus-`any` worst cases measure equal.
  - `hybrid_search`'s first-row ladder gains a last `explain omitted` step. Without it, the both-arms `rerank=true` page with the transition line reaches 1,002 at the 1,000 floor.
- **`SEARCH_LOW_CONFIDENCE_SIMILARITY`** (float 0–1, **default 0 = hard off at ship**). It is model-dependent and is not part of `retrieval_fingerprint()`. bge-m3's candidate value is 0.50.
  - `DEPLOYMENT.md` gets a calibration recipe: accrue usage rows, tabulate, accept a threshold only if fewer than 1 in 50 clicked-through pages would read `low`.
  - The reference deployment sets a value only after that.
- **Usage rows always record `confidence` and `top_similarity`**, and `keyword_all` on `hybrid_search`. They are recorded whatever the threshold, so calibration has evidence from day one.
- **The harness gains:**
  - recall@20 and recall@50, reported per arm, class and source, with deltas, the seeded bootstrap CI and the table;
  - default `--depth` 20 → 50;
  - a per-arm `staged_depth`, preserved and validated from a `runfile:` source, which nulls any recall@k beyond it;
  - `schema_version` 2, and the golden regenerated once;
  - a read-only `confidence` subcommand that tabulates calibration evidence.

  The verdict text and the no-threshold rule are unchanged.

**1.0 tool contract: additive.** The change adds a header token, an optional conditional line that never begins with `- `, three usage keys and one labelled abbreviation rung at the 1,000-character floor. Rows, fields, ordering, arguments and annotations are unchanged. At the shipped default, the only byte change on an existing page is ` — confidence: ok`. The reranker's `rerank=false` byte-identity baseline is re-based in its spec.

No migration.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- **`hybrid-search`, MODIFIED:**
  - "Each result SHALL render on one line…": the token, the conditional hint, the `keyword_all` evidence;
  - "The response SHALL be bounded…": 405 for `rerank=false` and 444 for `rerank=true`, both for `ok` and low-plus-`any`, `explain omitted`, the re-measured worst cases;
  - "hybrid_search SHALL be tracked…": `keyword_all`, `confidence`, `top_similarity`.
- **`search-quality`, ADDED:** the shared confidence rule with `semantic_search`'s token, hint and refusal behaviour; the `SEARCH_LOW_CONFIDENCE_SIMILARITY` setting and its calibration recipe.
- **`reranker`, MODIFIED:** the byte-identity baseline is re-based to the search-confidence page.
- **`search-evaluation`:**
  - MODIFIED "Scoring SHALL report…": recall@20/50, depth 50, `staged_depth` with nulling, schema 2, golden;
  - ADDED the confidence calibration tabulation.

`usage-attribution` has no spec, so only its architecture note gets the keys.

## Impact

- **Code**:
  - `src/services/search_confidence.py` (new);
  - `src/services/hybrid.py` (`HybridResult.top_similarity`, telemetry);
  - `src/mcp_server/tools.py` (headers, the conditional hint, `explain omitted`, re-pinned constants, reads `top_similarity` from the first returned row; no semantic/keyword service API change);
  - `src/mcp_server/server.py` (descriptions);
  - `src/config.py`;
  - `scripts/search_eval/{metrics,scoring,runner,mining,__main__}.py`, plus a new `confidence.py`;
  - `tests/fixtures/search_eval/expected-report.json`.
- **Docs**: README, `.env.example`, `DEPLOYMENT.md` (recipe), `docs/architecture/search.md`, `docs/architecture/usage-attribution.md`, CLAUDE.md.
- **Gates**: this is a search-path change. The adversarial Codex pass is mandatory, and the eval-gate row applies.
