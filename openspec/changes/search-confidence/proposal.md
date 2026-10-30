Refs #389

## Why

The search tools are investigation entry points. An agent takes a candidate list, reads the promising notes and follows wikilinks from them. What matters is recall at depth and honest signals about the candidate set, not precision at rank one. Today one of those signals is missing. `hybrid_search("xqzvplorth", limit=5)` returns five rows labelled `arms: both` at similarity 0.36–0.49 with no warning (#389, after #354 deployed). Re-run live on 2026-10-30, the best row scored 0.467. The keyword arm was empty, and the semantic arm always returns its nearest neighbours, however distant. `semantic_search` has the same blind spot. An agent cannot tell this page from a real one, so it reads noise instead of going to the graph. A real paraphrase on the same index scores 0.618 (0.61–0.67 in the operator's probes).

Measuring at depth is the other half. The harness (#356) reports recall@5 and recall@10, but an agent that reads past rank ten is served by recall@20 and recall@50. Fifty is both the largest `limit` the search tools accept and `hybrid_search`'s arm depth, so recall@50 is the ceiling of what one page can reach.

This change adds the honesty signal and the depth metrics. **No row is ever dropped.**

## What Changes

- **A `confidence: ok | low` header token on `semantic_search` and `hybrid_search`**, on every return path. Both tools use one pure rule. The value is `low` exactly when `SEARCH_LOW_CONFIDENCE_SIMILARITY > 0`, the keyword arm returned no note (`semantic_search` has none), and the best semantic similarity is below the threshold (or no semantic note exists). The best similarity is taken before rerank, `limit` and the response cap, so none of them changes the value. `keyword_search` and `find_related` are untouched: neither has a query similarity.
- **A one-line hint when `low`.** For `hybrid_search`: ``low confidence: no keyword match and weak similarity; try `keyword_search` with an identifier, or `get_neighborhood` from a known note``. `semantic_search` gets a sibling line. The hint is fixed server text of at most 140 characters, placed after the header and the `index_transition:` line, before the rows. It is reserved in the response budget.
- **The response bounds are re-measured.** The `hybrid_search` header worst case goes from 400 to 405 (floor 990). The `rerank=true` header goes from 431 to 444, and `semantic_search(rerank=true)` from 185 to 199. At the 1,000-character floor, `hybrid_search`'s abbreviation ladder gains two last steps: `explain omitted`, then withdrawing the hint (the `low` token stays). Every response still fits `MAX_READ_RESPONSE_CHARS ≥ 1,000`.
- **One new setting, `SEARCH_LOW_CONFIDENCE_SIMILARITY`** (float 0–1, default 0.50, `0` = never `low`), documented as model-dependent. The default suits `bge-m3`, and the harness is what tunes it. It changes no ranking and is not part of `retrieval_fingerprint()`.
- **Usage rows record `confidence` and `top_similarity`** on both tools. The harness can then count how often `low` fires per query class (identifier vs prose, which the miner already classifies) and re-tune the threshold offline, without a replay.
- **The harness adds recall@20 and recall@50** to per-arm, per-class and per-source metrics, the baseline deltas with the seeded bootstrap CI, and the table. The default `--depth` goes from 20 to 50. A recall@k beyond an arm's staged depth is null, never computed on a shorter run. The report's `schema_version` goes to 2, and the synthetic golden file is regenerated once, because the metric set changed. The verdict text and the no-threshold rule are unchanged.

**1.0 tool contract:** additive. One new header token, one new optional line that never begins with `- `, and two new usage-row keys. No row, field, ordering, argument or annotation changes. The only byte changes to existing pages are the token, and the hint when `low`. The `rerank=false` byte-identity pins from #367 are re-based on the new header.

No migration.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `hybrid-search`: MODIFIED "Each result SHALL render on one line…" (the header gains `confidence`, plus the hint line), "The response SHALL be bounded…" (405 header, hint reserved, `explain omitted` and hint withdrawal, re-measured worst cases) and "hybrid_search SHALL be tracked…" (`confidence`, `top_similarity`).
- `search-quality`: ADDED the shared confidence rule with `semantic_search`'s header token and hint (`search-quality` has no semantic-header requirement to modify), and ADDED the `SEARCH_LOW_CONFIDENCE_SIMILARITY` setting requirement.
- `search-evaluation`: MODIFIED "Scoring SHALL report…" (recall@20, recall@50, depth 50, null beyond depth, schema 2, golden).

`usage-attribution` has no spec. Its architecture note gets the two keys. `reranker` is not modified: its header-token and bound requirements stay true as written, and its byte-identity requirement is relative to #367.

## Impact

- **Code**: `src/services/search_confidence.py` (new: the rule and the two hint constants); `src/services/hybrid.py` (`HybridResult.top_similarity`, telemetry); `src/services/embeddings.py` (`top_similarity` exposed from the semantic arm/candidates); `src/mcp_server/tools.py` (the semantic header on both encodings, the hybrid header, hint placement and reservation, `explain omitted`, hint withdrawal, the re-pinned constants); `src/mcp_server/server.py` (the two descriptions mention the token, ≤ 4,000); `src/config.py` (setting); `scripts/search_eval/{metrics,scoring,runner}.py`; `tests/fixtures/search_eval/expected-report.json`.
- **Tests**: rule table; render tests for both tools on every return path; the live-shaped gibberish case (0.467, empty keyword arm) and paraphrase case (0.618); the bound tests re-measured; rerank byte-identity fixtures re-based; settings validation; usage keys; harness metrics, null-beyond-depth, golden.
- **Docs**: README (setting, token), `.env.example`, `docs/architecture/search.md` (a "Confidence (#389)" section, bounds, the harness metrics), `docs/architecture/usage-attribution.md` (keys), CLAUDE.md (a key-decisions line).
- **Gates**: search-path change. The adversarial Codex pass is mandatory. The eval-gate row applies: snapshot, then `make search-eval` on a restored copy, with recall@20/50 now in the report.
