## ADDED Requirements

### Requirement: `semantic_search` and `hybrid_search` SHALL state a `confidence` token computed by one rule from all-terms keyword evidence and the best similarity of the call's candidate set
Every **page** of `semantic_search` and `hybrid_search` — every response that carries a search header or zero-result line: rows, the zero-result line, `rerank=true` and `rerank=false`, reranked or falling back, and `hybrid_search`'s keyword-only path — SHALL carry exactly one header token `confidence: ok` or `confidence: low`. A refusal is not a page: `semantic_search`'s provider-input rejection (`_semantic_provider_refusal`, `provider_input_rejected`) and every pre-body refusal SHALL carry no `confidence` token and no hint, and their usage rows SHALL record neither `confidence` nor `top_similarity`. (`hybrid_search` has no such refusal: a provider rejection is its keyword-only page, which carries the token.)

The value SHALL be computed by one pure function shared by both tools, from three inputs: `keyword_all`, the number of `match: all` notes in the keyword arm (`match: any` rows are not evidence; `semantic_search` has no keyword arm and passes 0); `top_similarity`; and the threshold `T = SEARCH_LOW_CONFIDENCE_SIMILARITY`. The value SHALL be `low` exactly when `T > 0`, `keyword_all = 0`, and `top_similarity` is none or strictly below `T`; otherwise `ok`.

`top_similarity` SHALL be defined over the call's **actual candidate set**, with no new or changed statement: for `semantic_search`, the similarity of the first row of the ordered, deduplicated candidate list the call already fetched (the existing `max(5 × limit, 50)`-chunk overfetch), taken before reranking and before the response cap; for `hybrid_search`, the best similarity among its semantic arm's notes (the existing 50-chunk overfetch, independent of `limit`), computed by the hybrid service from those rows and carried on its result. It is none when that set is empty or the semantic arm did not run. Because `semantic_search`'s overfetch depends on `limit`, a different `limit` can change its candidate set and therefore, at the margin, its token. The `semantic_search`, `semantic_candidates`, `keyword_search` service return types SHALL be unchanged: the tool reads `top_similarity` from the first returned row. The token SHALL NOT drop, reorder or alter any row; it is part of every page's header and of the base response budget. `keyword_search` and `find_related` SHALL NOT carry the token.

On `semantic_search` the token SHALL follow the stale and truncated counts: `Found N semantic matches for '<q>' — S stale, T truncated — confidence: ok:` on the `rerank=false` page, `No semantic results for '<q>' (embeddings may still be building) — confidence: low` on its zero-result line (when `T > 0`), and after the counts and before the `reranked` token on a `rerank=true` header. When the value is `low`, the page SHALL carry the fixed line ``low confidence: weak similarity and no keyword arm; try `keyword_search` with an identifier, or `get_neighborhood` from a known note`` (at most 140 characters) after the header and after the `index_transition:` line when present, before the first row. On a `rerank=true` page the hint SHALL be rendered only when it changes neither the shown paths nor any row line, as on `hybrid_search`; the `semantic_search(rerank=true)` worst-case header SHALL be at most 199 characters (`SEMANTIC_RERANK_HEADER_MAX`). The `rerank=false` page SHALL otherwise be byte-identical to the page before this change.

#### Scenario: Gibberish on semantic_search
- **WHEN** `semantic_search("xqzvplorth")` returns notes whose best similarity is 0.467 and `SEARCH_LOW_CONFIDENCE_SIMILARITY=0.50`
- **THEN** the header SHALL end `— confidence: low:`, the semantic hint line SHALL follow the header (after the `index_transition:` line when present), and every row SHALL be rendered exactly as it is with the threshold at 0

#### Scenario: A real paraphrase on semantic_search
- **WHEN** `semantic_search("how do I keep an agent from overwriting a note it did not read first")` returns notes whose best similarity is 0.618 with the threshold at 0.50
- **THEN** the header SHALL carry `confidence: ok` and no hint line SHALL appear

#### Scenario: The boundary is strict
- **WHEN** the best similarity equals the threshold exactly
- **THEN** the value SHALL be `ok`

#### Scenario: The shipped default never fires
- **WHEN** `SEARCH_LOW_CONFIDENCE_SIMILARITY` is unset (default 0), or is 0 and the best similarity is negative, or the semantic arm returned nothing
- **THEN** the value SHALL be `ok` on every page

#### Scenario: The rule is shared
- **WHEN** the rule's table test runs over `keyword_all` (0 or ≥ 1, with and without `any` rows) × `top_similarity` (none, below, equal, above) × threshold (0, 0.5)
- **THEN** `semantic_search` and `hybrid_search` SHALL both call the one function, and each case SHALL give the value the rule states

#### Scenario: Provider-input rejection is a refusal
- **WHEN** the embedding provider rejects a `semantic_search` query as too large
- **THEN** the response SHALL be the existing refusal with no search header, no `confidence` token and no hint, and the usage row SHALL record the refusal and neither `confidence` nor `top_similarity`

#### Scenario: Usage row on semantic_search
- **WHEN** a `semantic_search` page is rendered, whatever the threshold, and its candidate list's first row has similarity 0.4671
- **THEN** its usage row SHALL carry `confidence` and `top_similarity: 0.467`, and SHALL NOT carry an `error` marker; a page whose candidate list is empty SHALL carry `confidence` and no `top_similarity`

### Requirement: SEARCH_LOW_CONFIDENCE_SIMILARITY SHALL set where `confidence: low` begins, shipping hard-off, documented as model-dependent with a calibration recipe
The setting `SEARCH_LOW_CONFIDENCE_SIMILARITY` SHALL be a float from 0.0 to 1.0 inclusive, default **0** (hard off: the token always reads `ok`), read once at startup; a value outside that range or not finite SHALL refuse startup. The token SHALL be present whatever the value; there SHALL be no setting that removes it. The setting SHALL NOT be a per-call argument and SHALL NOT change any ranking, so it SHALL NOT be part of `retrieval_fingerprint()`. The README settings table, `.env.example` and `docs/architecture/search.md` SHALL document it with its default, its range, that `ok` at 0 means "not judged", that it is model-dependent and must be re-chosen when the embedding model in the retrieval fingerprint changes, and the bge-m3 evidence so far (live, 2026-10-30: gibberish best similarity 0.36–0.49, real paraphrases 0.61–0.67), stated as a candidate value, not a default. `DEPLOYMENT.md` SHALL carry the calibration recipe: after the deployment has accrued usage rows carrying `top_similarity`, run the harness's confidence tabulation per tool and per query class, pick the threshold at the observed noise ceiling, accept it only if the false-low rate on clicked-through pages is below 1 in 50, set it, and re-check after a further period. The reference deployment SHALL set a non-zero value only after that tabulation passes.

#### Scenario: Out of range
- **WHEN** the server starts with `SEARCH_LOW_CONFIDENCE_SIMILARITY=1.5` or `-0.1` or `nan`
- **THEN** settings validation SHALL fail and the server SHALL NOT start

#### Scenario: Documented
- **WHEN** README.md, `.env.example`, `DEPLOYMENT.md` and `docs/architecture/search.md` are searched for `SEARCH_LOW_CONFIDENCE_SIMILARITY`
- **THEN** README and `.env.example` SHALL name it with its default 0 and its range, `search.md` SHALL state that it is model-dependent and tuned by the harness, and `DEPLOYMENT.md` SHALL give the calibration recipe with its 1-in-50 acceptance line
