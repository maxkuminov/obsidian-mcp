## MODIFIED Requirements

### Requirement: Search calls record per-phase timing

`semantic_search` SHALL record `embed_ms` (time to obtain the query embedding) and `db_ms` (time in the vector query, including `SET LOCAL`s and fetch); `find_related` SHALL record `db_ms` only (it performs no embedding-provider call; its source-chunk fetch is included in `db_ms`). Values SHALL be non-negative integers stored in `usage_logs.params` alongside the existing whole-call `duration_ms`. Timing SHALL be scoped to the tool call: the `_tracked` decorator SHALL initialise the timing holder at call start and clear it in `finally`, so a value can never be attributed to a different call, and early returns or exceptions SHALL leave partial phases at their measured value or absent, never stale. The service functions' return types SHALL be unchanged; timing travels on a call-scoped holder, not in the return value. A `semantic_search` call with `rerank=true` SHALL also record `reranked` (boolean), `rerank_reason` (only when `reranked` is false), `rerank_ms` (a non-negative integer, only when a rerank request was sent) and `rerank_candidates` (only when candidates were computed), and its `result_count` and `result_paths` SHALL be recorded once, after reranking, from the rows rendered in their final order; for such a call `embed_ms + db_ms + rerank_ms` SHALL NOT exceed `duration_ms`. A call with `rerank=false` or the argument omitted SHALL record none of the four rerank keys.

#### Scenario: Timing fields present

- **WHEN** a `semantic_search` call completes
- **THEN** its usage log row's `params` SHALL contain integer `embed_ms` and `db_ms` such that `embed_ms + db_ms ≤ duration_ms`, and a boolean `exact_fallback`

#### Scenario: No cross-call leakage

- **WHEN** a `semantic_search` call is followed in the same task by a different tracked tool call
- **THEN** the second call's usage row SHALL NOT contain `embed_ms` or `db_ms`

#### Scenario: find_related timing

- **WHEN** a `find_related` call completes (including the "not embedded yet" early return)
- **THEN** its usage row SHALL contain `db_ms` and SHALL NOT contain `embed_ms`

#### Scenario: Rerank timing fields

- **WHEN** a `semantic_search(rerank=true)` call is reranked
- **THEN** its usage row's `params` SHALL contain integer `embed_ms`, `db_ms` and `rerank_ms` with `embed_ms + db_ms + rerank_ms ≤ duration_ms`, `reranked: true`, and `result_paths` in reranked order

#### Scenario: Unreranked calls carry no rerank timing

- **WHEN** a `semantic_search` call omits `rerank`
- **THEN** its usage row SHALL NOT contain `rerank_ms`, `reranked`, `rerank_reason` or `rerank_candidates`
