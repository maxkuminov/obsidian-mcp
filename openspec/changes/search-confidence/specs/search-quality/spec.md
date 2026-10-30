## ADDED Requirements

### Requirement: `semantic_search` and `hybrid_search` SHALL state a `confidence` token computed by one rule from the keyword arm and the best semantic similarity
Every response of `semantic_search` and `hybrid_search`, on every return path (rows, the zero-result line, `rerank=true` and `rerank=false`, reranked or falling back, and `hybrid_search`'s keyword-only path), SHALL carry exactly one header token `confidence: ok` or `confidence: low`. The value SHALL be computed by one pure function shared by both tools, from three inputs: whether the keyword arm returned at least one note (`keyword_search`-style rows; `semantic_search` has no keyword arm and always passes "none"), the best similarity among the semantic arm's notes (`top_similarity`, none when the semantic arm returned no note or did not run), and the threshold `T = SEARCH_LOW_CONFIDENCE_SIMILARITY`. The value SHALL be `low` exactly when `T > 0`, the keyword arm returned no note, and `top_similarity` is none or strictly below `T`; otherwise `ok`. `top_similarity` SHALL be the full-precision similarity of the semantic arm's best note under the call's owner and filters, taken before reranking, before truncation to `limit` and before the response cap, so that neither `rerank`, `limit` nor the cap can change the value. The token SHALL NOT drop, reorder or alter any row. `keyword_search` and `find_related` SHALL NOT carry the token.

On `semantic_search` the token SHALL follow the stale and truncated counts: `Found N semantic matches for '<q>' — S stale, T truncated — confidence: ok:` on the `rerank=false` page, `No semantic results for '<q>' (embeddings may still be building) — confidence: low` on its zero-result line (when `T > 0`), and after the counts and before the `reranked` token on a `rerank=true` header. When the value is `low`, the response SHALL carry the fixed line ``low confidence: weak similarity and no keyword arm; try `keyword_search` with an identifier, or `get_neighborhood` from a known note`` (at most 140 characters) after the header and after the `index_transition:` line when present, before the first row. On a `rerank=true` page the line SHALL be reserved in the response budget, and the `semantic_search(rerank=true)` worst-case header SHALL be at most 199 characters (`SEMANTIC_RERANK_HEADER_MAX`), so that header 199 + hint 133 + transition line 128 + abbreviated structure 235 + title 45 + path 45 + cap footer 76 ≤ 1,000. The `rerank=false` page SHALL otherwise be byte-identical to the page before this change.

#### Scenario: Gibberish on semantic_search
- **WHEN** `semantic_search("xqzvplorth")` returns notes whose best similarity is 0.467 and `SEARCH_LOW_CONFIDENCE_SIMILARITY=0.50`
- **THEN** the header SHALL end `— confidence: low:`, the semantic hint line SHALL follow the header (after the `index_transition:` line when present), and every row SHALL be rendered exactly as it is with the threshold at 0

#### Scenario: A real paraphrase on semantic_search
- **WHEN** `semantic_search("how do I keep an agent from overwriting a note it did not read first")` returns notes whose best similarity is 0.618
- **THEN** the header SHALL carry `confidence: ok` and no hint line SHALL appear

#### Scenario: The boundary is strict
- **WHEN** the best similarity equals the threshold exactly
- **THEN** the value SHALL be `ok`

#### Scenario: A zero threshold never fires
- **WHEN** `SEARCH_LOW_CONFIDENCE_SIMILARITY=0` and the best similarity is negative, or the semantic arm returned nothing
- **THEN** the value SHALL be `ok` on every return path

#### Scenario: The rule is shared
- **WHEN** the rule's table test runs over (keyword notes 0 or ≥ 1) × (top similarity none, below, equal, above) × (threshold 0, 0.5)
- **THEN** `semantic_search` and `hybrid_search` SHALL both call the one function, and each case SHALL give the value the rule states

#### Scenario: Usage row on semantic_search
- **WHEN** a `semantic_search` call renders `confidence: low` with best similarity 0.4671
- **THEN** its usage row SHALL carry `confidence: "low"` and `top_similarity: 0.467`, and SHALL NOT carry an `error` marker; a call whose semantic arm returned no note SHALL carry `confidence` and no `top_similarity`

### Requirement: SEARCH_LOW_CONFIDENCE_SIMILARITY SHALL set where `confidence: low` begins, within a validated range, documented as model-dependent
The setting `SEARCH_LOW_CONFIDENCE_SIMILARITY` SHALL be a float from 0.0 to 1.0 inclusive, default 0.50, read once at startup; a value outside that range or not finite SHALL refuse startup. `0` SHALL mean the token is always `ok`. The token SHALL be present whatever the value; there SHALL be no setting that removes it. The setting SHALL NOT be a per-call argument and SHALL NOT change any ranking, so it SHALL NOT be part of `retrieval_fingerprint()`. The README settings table, `.env.example` and `docs/architecture/search.md` SHALL document it with its default, its range, that the default was chosen for `bge-m3` (live, 2026-10-30: gibberish best similarity 0.36–0.49, real paraphrases 0.61–0.67), that it is model-dependent and must be re-chosen when the embedding model in the retrieval fingerprint changes, and that the search-evaluation harness is the instrument that tunes it.

#### Scenario: Out of range
- **WHEN** the server starts with `SEARCH_LOW_CONFIDENCE_SIMILARITY=1.5` or `-0.1` or `nan`
- **THEN** settings validation SHALL fail and the server SHALL NOT start

#### Scenario: Documented
- **WHEN** README.md, `.env.example` and `docs/architecture/search.md` are searched for `SEARCH_LOW_CONFIDENCE_SIMILARITY`
- **THEN** each SHALL name it with its default 0.50 and its range, and `search.md` SHALL state that it is model-dependent and tuned by the harness
