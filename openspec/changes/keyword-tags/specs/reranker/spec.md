## MODIFIED Requirements

### Requirement: A rerank=true response SHALL be bounded by the read-response character cap, measured on rendered length
A `semantic_search` response with `rerank=true` SHALL be at most `MAX_READ_RESPONSE_CHARS` characters, measured on rendered (escaped) length under the same rules the `hybrid-search` capability defines for `hybrid_search`. The query echo SHALL be cut by rendered length with `query_cut` in the header. The header and a response-cap footer of at most 75 characters SHALL be reserved. Rows SHALL be appended in order while each fits, and later rows SHALL be dropped whole. This rule SHALL apply identically to reranked pages and to every fallback. Whenever a row is dropped, the header's `shown` SHALL count the rendered rows, and the footer SHALL state how many rows the cap omitted and carry `more: true`. `semantic_search` has no cursor or offset, so the footer SHALL name a smaller `limit` or a `folder` filter as the remedy, and a capped page SHALL never read as complete. The first row SHALL be abbreviated in hybrid's order (tags, preview, then title and path cut to 40–45 rendered characters, then the omitted form) with server labels after the separator, never splitting an escape. The worst case SHALL be computed with the longest `reranked` token (`reranked: false (malformed_response)` or `reranked: true (candidates: 50)`), the longest `rerank_score` rendering, and both markers. It SHALL fit `MAX_READ_RESPONSE_CHARS=1000`. For `hybrid_search(rerank=true)`, hybrid's header and abbreviated-row worst cases SHALL be recomputed with the rerank token and `rerank_score` included, the abbreviated first row's title and path cut floor SHALL be 20 rendered characters (kept 20–25) instead of 40, and the result SHALL still fit 1,000. Since #391 the longest `matched_in` value is `title+tags+body`, so with #389's `confidence` token the pinned `hybrid_search(rerank=true)` worst cases SHALL be header ≤ 444 and abbreviated-row structure ≤ 367 (362 before #391), and the cut-form sum is 444 + 367 + 25 + 25 + 75 + 75 + 5 = 1,016 > 1,000: in that worst case the first row SHALL continue to the labelled omitted form (`"title":""` / `"path":""` with `title omitted` / `path omitted`; at most 444 + 367 + 31 + 75 + 75 + 5 = 997), and then, where both arms ran with the `index_transition:` line, to `explain omitted`; the 20–25 character title/path cut form is not guaranteed there. `semantic_search(rerank=true)` carries no `matched_in`, and its bounds (header ≤ 199 since #389, structure ≤ 235, 40–45 cut) are unchanged by #391.

#### Scenario: Worst case at the minimum cap
- **WHEN** `MAX_READ_RESPONSE_CHARS=1000`, the query is 8,192 control characters, and the first reranked result has a 512-control-character title, a 1,024-character path holding controls, `"` and `\`, 1,000 tags and a chunk
- **THEN** the response SHALL be at most 1,000 rendered characters, with `query_cut`, the `reranked` token and one abbreviated row whose title and path literals hold 40–45 rendered characters each

#### Scenario: Several rows at the cap, reranked and fallback
- **WHEN** `MAX_READ_RESPONSE_CHARS=1000`, `limit=10`, and the ten candidates each carry a long control-heavy title and path, so that only three rows fit; the call is made once with a fake reranker that scores the candidates in their unreranked order (success) and once with the reranker timing out (fallback)
- **THEN** both responses SHALL be at most 1,000 rendered characters, both SHALL show the same three rows in the same order with the same fields and preview selection, the success SHALL differ only by `rerank_score` and its header token, and both footers SHALL state that seven rows were omitted with `more: true`

#### Scenario: Hybrid worst case with rerank
- **WHEN** the hybrid worst-case inputs are searched with `rerank=true` and `HYBRID_RRF_K=1000` on the keyword-only path, and the rerank falls back with `malformed_response`
- **THEN** the response SHALL be at most 1,000 rendered characters

#### Scenario: Hybrid rerank worst case with the longest matched_in value

- **WHEN** `MAX_READ_RESPONSE_CHARS=1000`, `HYBRID_RRF_K=1000`, `rerank=true` falls back with `malformed_response`, and the first result carries the hybrid worst-case control-heavy title, path, 1,000 tags and preview with `matched_in: title+tags+body`
- **THEN** the response SHALL be at most 1,000 rendered characters, the abbreviated row's structure SHALL be at most the pinned 367, and when the cut form does not fit the row SHALL render the omitted form with `title omitted` and `path omitted`, never a split escape

#### Scenario: The rerank=false page keeps its cut form with the longest matched_in value

- **WHEN** the same inputs are searched with `rerank=false`
- **THEN** the response SHALL be at most 1,000 rendered characters and the abbreviated row SHALL keep 40–45 rendered characters each of title and path
