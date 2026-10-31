## MODIFIED Requirements

### Requirement: Search calls record per-phase timing

`semantic_search` SHALL record `embed_ms` (time to obtain the query embedding) and `db_ms` (time in the vector query, including `SET LOCAL`s and fetch); `find_related` SHALL record `db_ms` only (it performs no embedding-provider call; its source-chunk fetch is included in `db_ms`). Values SHALL be non-negative integers stored in `usage_logs.params` alongside the existing whole-call `duration_ms`. Timing SHALL be scoped to the tool call: the `_tracked` decorator SHALL initialise the timing holder at call start and clear it in `finally`, so a value can never be attributed to a different call, and early returns or exceptions SHALL leave partial phases at their measured value or absent, never stale. The service functions' return types SHALL be unchanged; timing travels on a call-scoped holder, not in the return value. A `semantic_search` call with `rerank=true` SHALL also record `reranked` (boolean), `rerank_reason` (only when `reranked` is false), `rerank_ms` (a non-negative integer, only when a rerank request was sent), `rerank_candidates` and `rerank_documents` (both only when candidates were computed), and its `result_count` and `result_paths` SHALL be recorded once, after reranking, from the rows rendered in their final order; for such a call `embed_ms + db_ms + rerank_ms` SHALL NOT exceed `duration_ms`. A call with `rerank=false` or the argument omitted SHALL record none of the five rerank keys.

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
- **THEN** its usage row's `params` SHALL contain integer `embed_ms`, `db_ms` and `rerank_ms` with `embed_ms + db_ms + rerank_ms ≤ duration_ms`, `reranked: true`, an integer `rerank_documents` at most `RERANK_MAX_DOCUMENTS`, and `result_paths` in reranked order

#### Scenario: Unreranked calls carry no rerank timing

- **WHEN** a `semantic_search` call omits `rerank`
- **THEN** its usage row SHALL NOT contain `rerank_ms`, `reranked`, `rerank_reason`, `rerank_candidates` or `rerank_documents`

### Requirement: Each fresh vector-search row SHALL name the section it matched as a quoted heading path and a server-authored ordinal
`semantic_search` and `find_related` SHALL append a section locator to the result line of each row whose representative chunk has a recorded section and whose note is not stale. For ordinal N ≥ 1, the locator SHALL be ` · § ` followed by the heading path joined with `/` and rendered as one JSON string literal, with C0 and C1 controls, DEL, U+2028 and U+2029 escaped, followed by ` (#N)`. For ordinal 0, it SHALL be ` · § (top of note)`. A heading path longer than 160 characters SHALL keep its innermost components and start with `…/`. A row whose chunk predates migration 034 (no recorded section) SHALL carry no locator. A row marked stale SHALL carry no locator, because a positional ordinal recorded for superseded content may name another section. Both statements SHALL project the two locator columns and SHALL NOT project `embed_prefix` or the stored vector, with one exception: the `semantic_search` and `hybrid_search` overfetch statement issued for a `rerank=true` call (the `with_rerank_text` flag) MAY project exactly `note_embeddings.embed_prefix` and `notes_metadata.embedded_chunking_version` in addition, and nothing else (never the stored vector), with its predicates, ordering, limit, settings and statement count unchanged; `rerank=false` statements and `find_related` SHALL keep the prohibition. The projected prefix SHALL NOT be rendered or logged. On a reranked `rerank=true` row the locator SHALL name the winning chunk as the `reranker` capability defines.

#### Scenario: The follow-up resolves
- **WHEN** a fresh row prints `§ "Plan/Risks" (#7)`
- **THEN** `read_note(path, section="#7")` on the same bytes SHALL resolve to the raw section headed `Risks`
- **AND** the row's chunk SHALL be a substring of that section's body with the whole-body fence spans removed (the containment contract of index-integrity's chunking requirement), not necessarily of the raw body

#### Scenario: A heading cannot forge the ordinal
- **WHEN** a note's heading reads `Risks" (#2) · § "x`
- **THEN** the rendered locator SHALL keep that text inside one escaped string literal, and the only ordinal outside it SHALL be the stored one

#### Scenario: Pre-034 and stale rows
- **WHEN** a row's chunk has no recorded section, or its note is stale
- **THEN** its result line SHALL be exactly what it would be without this requirement

#### Scenario: Preamble
- **WHEN** the representative chunk is from text before the first heading, or from a note with no headings
- **THEN** the locator SHALL be `§ (top of note)`

#### Scenario: The rerank=true projection carve-out
- **WHEN** the statements of `semantic_search` and `hybrid_search` are captured with `rerank=false`, with `rerank=true`, and for `find_related`
- **THEN** the `rerank=false` and `find_related` statements SHALL project neither `embed_prefix` nor the vector, and the `rerank=true` overfetch statement SHALL project exactly `note_embeddings.embed_prefix` and `notes_metadata.embedded_chunking_version` in addition to the `rerank=false` projection, and never the stored vector, with identical predicates, ordering, limit, settings and statement count
