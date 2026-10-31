# reranker Specification

## Purpose
An optional, operator-configured cross-encoder reranker (#367) that reorders the candidates `semantic_search` and `hybrid_search` already fetched when a call passes `rerank=true`. It covers the `RERANK_*` settings and their transport policy, the one Jina-compatible wire format and its validation, the two-order startup probe, background re-probe and `/health` state, the candidates and scored unit, the all-or-nothing fallback with its closed reason codes, the quoted-object rendering under the response cap, telemetry, and the rule that no database connection is held during the hop. With no reranker configured nothing changes, and a reranker can never fail a search.
## Requirements
### Requirement: The reranker SHALL be configured by four optional settings, and an empty RERANK_URL SHALL mean the feature is absent
The server SHALL accept `RERANK_URL` (string, default empty), `RERANK_MODEL` (string, default empty, at most 200 characters, no control characters), `RERANK_TIMEOUT_MS` (integer, default 1500, range 100–30000) and `RERANK_ALLOW_PLAINTEXT` (boolean, default false). `RERANK_URL` SHALL be the full endpoint URL. An empty or unset `RERANK_URL` SHALL mean no reranker is configured. An out-of-range `RERANK_TIMEOUT_MS` or an invalid `RERANK_MODEL` SHALL refuse startup. README's settings table and `.env.example` SHALL document all four with their defaults, and SHALL state that the endpoint receives note text. Neither SHALL contain a host path. DEPLOYMENT.md SHALL give the measured per-call rerank latency for 50-document calls (short and full-length documents) on named hardware, the latency this implies at the `RERANK_MAX_DOCUMENTS` default, a rule for sizing `RERANK_TIMEOUT_MS` together with `RERANK_MAX_DOCUMENTS`, and the rule that under `MCP_CONCURRENCY_MODE` `queue` or `enforce` `RERANK_TIMEOUT_MS` SHALL NOT exceed `MCP_CONCURRENCY_WAIT_SECONDS` unless a `make concurrency-report` over evidence including `rerank=true` traffic passes for that mode.

#### Scenario: Defaults
- **WHEN** the server starts with none of the four set
- **THEN** startup SHALL succeed, `/health` SHALL report `rerank: "absent"`, and no rerank transport line SHALL be logged

#### Scenario: Out of range
- **WHEN** the server starts with `RERANK_TIMEOUT_MS=50` or `RERANK_TIMEOUT_MS=30001`
- **THEN** settings validation SHALL fail and the server SHALL NOT start

#### Scenario: A twenty-second deadline is accepted
- **WHEN** the server starts with `RERANK_TIMEOUT_MS=20000`
- **THEN** settings validation SHALL succeed

#### Scenario: Documented
- **WHEN** README.md and `.env.example` are searched for each of `RERANK_URL`, `RERANK_MODEL`, `RERANK_TIMEOUT_MS` and `RERANK_ALLOW_PLAINTEXT`
- **THEN** each SHALL be named with its default, and `RERANK_TIMEOUT_MS` with the range 100–30000

### Requirement: RERANK_URL MUST satisfy the internal-hop scheme policy at startup, judged by the embedding hop's parser
When `RERANK_URL` is non-empty and `MCP_SANDBOX_MODE` is off, settings validation MUST refuse a value that has surrounding whitespace or a control character, that `httpx.URL` cannot parse, whose scheme is not `http` or `https`, that carries userinfo, that has no host, or that has an invalid port. It MUST also refuse an `http` URL to a host that is not a literal loopback host unless `RERANK_ALLOW_PLAINTEXT=true`. The error SHALL name `RERANK_URL` (and `RERANK_ALLOW_PLAINTEXT` for the plaintext case) and SHALL NOT echo userinfo. The validation SHALL NOT restrict the address class: loopback, private and link-local hosts SHALL be accepted. `EMBEDDING_ALLOW_PLAINTEXT` SHALL NOT admit a plaintext reranker URL. A policy violation SHALL refuse startup. A reranker that is unreachable SHALL NOT refuse startup.

#### Scenario: Plaintext to the LAN without the override
- **WHEN** `RERANK_URL=http://gpu-host:8081/v1/rerank`, `RERANK_ALLOW_PLAINTEXT` is false and `EMBEDDING_ALLOW_PLAINTEXT=true`
- **THEN** settings validation SHALL fail naming `RERANK_URL` and `RERANK_ALLOW_PLAINTEXT`

#### Scenario: Loopback plaintext and private https are accepted
- **WHEN** `RERANK_URL` is `http://127.0.0.1:8081/v1/rerank`, or `https://10.0.0.5/v1/rerank`
- **THEN** settings validation SHALL succeed

#### Scenario: Credentials in the URL
- **WHEN** `RERANK_URL=https://user:secret@rerank.internal/v1/rerank`
- **THEN** validation SHALL fail and the message SHALL NOT contain `secret`

### Requirement: Every reranker HTTP client SHALL be built by one factory with the embedding hop's transport policy and a bounded pool
The server SHALL build every HTTP client aimed at `RERANK_URL` through `transport_security.rerank_http_client`, which SHALL set `trust_env=False`, `follow_redirects=False`, `verify=` the context parsed from `EMBEDDING_CA_FILE` when set and otherwise certifi's bundle, and a connection limit of 2. The process SHALL hold at most one such client, closed at shutdown. Each rerank exchange SHALL run under one wall-clock deadline of `RERANK_TIMEOUT_MS` that covers the pool wait, connect, write and read, and SHALL read the response body under a 1 MiB cap. A rerank exchange SHALL NOT be retried. `EMBEDDING_CA_FILE` SHALL keep its existing rule (refused while the active embedding URL is `http`), and this change SHALL NOT relax it; with a plaintext embedding URL, an `https` reranker SHALL be verified against certifi's bundle.

#### Scenario: No direct client construction
- **WHEN** the AST guard scans `src/` for `httpx.AsyncClient(` outside the transport factories
- **THEN** it SHALL find none in `src/services/rerank.py`

#### Scenario: A redirect is not followed
- **WHEN** the reranker answers 302 to another loopback port
- **THEN** no request SHALL reach the redirect target, and the call SHALL fall back with `http_error`

#### Scenario: Proxy variables are ignored
- **WHEN** `HTTPS_PROXY` and `HTTP_PROXY` are set in the process environment
- **THEN** the rerank request SHALL go directly to `RERANK_URL`

### Requirement: The rerank exchange SHALL use the Jina-compatible /v1/rerank format and SHALL accept a response only if every candidate receives exactly one finite score
The request SHALL be one `POST` to `RERANK_URL` with the JSON body `{"model": RERANK_MODEL, "query": <query>, "documents": [<n texts>], "top_n": n}`. The response SHALL be accepted only when the status is 2xx, the body is at most 1 MiB, the body parses as a JSON object, and its `results` is a list of exactly `n` objects. Each object SHALL carry an integer, non-boolean `index` in `[0, n)` and a numeric, non-boolean, finite `relevance_score`, and the indices SHALL be a permutation of `0..n-1`. Scores SHALL be mapped to candidates by `index`, never by list position. Any other response SHALL be rejected as a whole, and no score from it SHALL be used.

#### Scenario: Results out of order
- **WHEN** the reranker returns the `n` results sorted by score with their original indices
- **THEN** each score SHALL be attached to the candidate its `index` names

#### Scenario: One score short
- **WHEN** `n = 12` and `results` holds 11 objects
- **THEN** the response SHALL be rejected with `count_mismatch`

#### Scenario: Duplicate index
- **WHEN** `results` holds `n` objects but `index` 3 appears twice and `index` 4 is missing
- **THEN** the response SHALL be rejected with `bad_index`

#### Scenario: NaN score
- **WHEN** one `relevance_score` is `NaN` (or `Infinity`)
- **THEN** the response SHALL be rejected with `non_finite_score`

### Requirement: rerank on hybrid_search SHALL rerank the first 50 fused rows, with candidate text read inside the hybrid snapshot
The `hybrid_search` tool defined by the `hybrid-search` capability SHALL accept `rerank: bool = false` with the same contract as `semantic_search`. The candidate notes SHALL be the first 50 rows of the fused list in fused order, before truncation to `limit`. The candidates SHALL be partitioned into exactly three cases. (a) A candidate in the semantic arm, not stale, whose stamp is a key of `EMBEDDED_TEXT_FORMATTERS` (or whose fetched rows have a NULL prefix) SHALL be sent as the documents of its fetched semantic-arm chunk rows (the 50-chunk overfetch), built and bounded as for `semantic_search`, with its cosine-best chunk as its primary document. (b) A candidate in the semantic arm, not stale, with a non-NULL prefix and a NULL or unknown stamp SHALL be sent as exactly one document, its title alone, and never its excerpt. (c) A candidate that is stale in the semantic arm, or that appears only in the keyword arm, SHALL be sent as one document: its title, two newlines, and its stored `body_excerpt`, bounded to 2,048 characters in all and cut at a code-point boundary, read by one statement inside the hybrid call's transaction and snapshot under the owner predicate, issued after the keyword snippet statements and only when such a candidate exists. A NULL excerpt SHALL send the title alone. The documents SHALL be allocated, scored, maximised per note and ordered exactly as for `semantic_search`: every candidate's primary first in fused order, then the secondaries in semantic-arm distance order, at most `RERANK_MAX_DOCUMENTS` per call and 4 per note. On a successfully reranked row whose winning document is a semantic chunk, its primary included, the row's preview SHALL be that chunk, even where the keyword snippet would otherwise be selected, and its `§` locator SHALL be that chunk's recorded section, or absent when it has none. A row whose winner is an excerpt or title document, and every row of a fallback page, SHALL keep the incumbent preview selection. A keyword-only hybrid call SHALL still rerank, one excerpt document per note. With `rerank=false`, `hybrid_search` SHALL issue no additional statement and its output SHALL be unchanged.

#### Scenario: A keyword-only candidate is scored on its excerpt
- **WHEN** a fused candidate appears only in the keyword arm and has an excerpt
- **THEN** it SHALL be sent as one document, its title, two newlines and the excerpt's leading characters up to the 2,048-character bound, read in the same snapshot as the arms

#### Scenario: A both-arms note keeps its place on its best allocated chunk
- **WHEN** a note is first in both arms with 2 fetched semantic chunks, both allocated by construction, its cosine-best chunk scores −8.87, its other chunk +0.33, and every other candidate scores below +0.33
- **THEN** it SHALL be first on the reranked page with `rerank_score: 0.33`, its preview SHALL be the +0.33 chunk's (not the keyword snippet), and its `§` locator SHALL be the +0.33 chunk's

#### Scenario: A primary chunk winner replaces the keyword snippet
- **WHEN** a both-arms candidate has one fetched semantic chunk, its incumbent preview is the keyword snippet, and that chunk (its primary) wins
- **THEN** the reranked row's preview SHALL be that chunk's one-line preview with `preview_kind` `chunk`, and its `§` locator SHALL be that chunk's section; on a fallback page the same row SHALL show the keyword snippet

#### Scenario: Hybrid rollback stamp
- **WHEN** after a rollback a fresh both-arms candidate has a non-NULL prefix, `embedded_chunking_version` 3 against formatter keys `{1, 2}`, and a non-NULL `body_excerpt`
- **THEN** it SHALL be sent as exactly one document, its title alone, and neither its excerpt nor any stored chunk text or prefix SHALL appear in the request body

#### Scenario: Every fused candidate is scored
- **WHEN** the 50 fused candidates hold 30 keyword-only notes and 20 semantic notes with 50 fetched chunks among them, `limit=10` and `RERANK_MAX_DOCUMENTS=100`
- **THEN** all 50 candidates SHALL hold at least one document, at most 4 per note, at most 80 documents SHALL be sent (the 50 primaries plus at most the 30 further fetched rows), and the token SHALL read `candidates: 50`

#### Scenario: Both markers on a degraded call
- **WHEN** the embedding provider is down and `hybrid_search(rerank=true)` reranks the keyword arm successfully
- **THEN** the header SHALL carry both `arms: keyword-only (semantic unavailable: provider_error)` and `reranked: true`

### Requirement: Any rerank failure SHALL return the unreranked page in the rerank=true encoding, marked reranked false with one closed reason code, and SHALL never fail the search
A `rerank=true` response SHALL be either fully reranked, with the header token `reranked: true (candidates: N)` where `N` is the number of notes holding documents, or a **fallback page**. A fallback page SHALL have the same row identities, order, fields, values and preview selection as the **baseline**: the page a `rerank=true` call renders from the unreranked order, in the `rerank=true` encoding, under the same rendered-length cap, without `rerank_score`, each row with its cosine-best chunk's preview and locator. The fallback carries the header token `reranked: false (<code>)`. The `rerank=false` page is a different, legacy rendering. It is byte-identical to the tool before this change, and it is not the comparison baseline, because the cap can show fewer rows in the `rerank=true` encoding. `<code>` SHALL be the first applicable of `not_configured`, `probe_skipped`, `probe_failed`, `no_candidates`, `timeout`, `transport_error`, `http_error`, `oversize_response`, `malformed_response`, `count_mismatch`, `bad_index` and `non_finite_score`, where `count_mismatch` and `bad_index` are judged against the number of documents sent. No request SHALL be sent for the first four. No response SHALL combine reranked and unreranked rows, and a response SHALL be used only when every document sent has a validated score. An exception in the rerank step, other than cancellation, SHALL NOT propagate out of the tool, and SHALL fall back with the matching code (`malformed_response` for one not otherwise classified). Response text from the reranker, the URL and exception messages SHALL NOT appear in the tool output. A runtime failure SHALL be logged at WARNING at most once per code per 60 seconds, with a suppressed count, and without the URL, response text or note content.

#### Scenario: Not configured
- **WHEN** `RERANK_URL` is empty and `semantic_search(query, rerank=true)` is called
- **THEN** the page SHALL equal the baseline (the unreranked order rendered in the `rerank=true` encoding under the same cap): its rows SHALL decode to the same paths, titles, tags, similarities, markers and previews in the same order as the first rows of the `rerank=false` page, as many as the cap admits, and the header SHALL carry `reranked: false (not_configured)`

#### Scenario: Timeout
- **WHEN** the reranker sleeps longer than `RERANK_TIMEOUT_MS`
- **THEN** the call SHALL return within `RERANK_TIMEOUT_MS` plus the search time, as a fallback page with `reranked: false (timeout)` and no `rerank_score` on any row

#### Scenario: A response scoring only one document per note
- **WHEN** 12 documents are sent for 5 notes and the reranker returns 5 results
- **THEN** the call SHALL fall back with `count_mismatch`, and every row SHALL keep its cosine-best preview and locator

#### Scenario: Slow-drip body
- **WHEN** the reranker sends its headers at once and then drips body bytes beyond `RERANK_TIMEOUT_MS`
- **THEN** the call SHALL fall back with `timeout` within the deadline, the stream SHALL be closed, no more than 2 pool connections SHALL ever be in use, and a following request to a fast reranker SHALL be reranked

#### Scenario: Server error
- **WHEN** the reranker answers HTTP 500 with a body quoting text
- **THEN** the call SHALL return the fallback page with `reranked: false (http_error)`, and the body text SHALL NOT appear in the output

#### Scenario: Malformed and oversize bodies
- **WHEN** the reranker answers 200 with a non-JSON body, or with a 2 MiB body
- **THEN** the call SHALL fall back with `malformed_response`, or `oversize_response`, respectively

#### Scenario: Zero results
- **WHEN** a configured, enabled reranker exists and the search finds nothing
- **THEN** no rerank request SHALL be sent and the zero-result line SHALL carry `reranked: false (no_candidates)`

### Requirement: When RERANK_URL is set the lifespan SHALL probe the reranker in both document orders, a failed probe SHALL disable reranking without refusing startup, and a bounded background re-probe SHALL re-enable it
A probe attempt SHALL send two rerank requests with a fixed query and the same two documents, one known-relevant and one known-irrelevant, in the order (relevant, irrelevant) and then in the order (irrelevant, relevant). Each response SHALL be accepted under the exchange requirement with `n = 2`, scores SHALL be mapped by `index`, and in **each** response the relevant document's score SHALL be strictly greater than the irrelevant one's. An attempt that fails SHALL have one diagnostic code: the exchange code of the first rejected response, `probe_wrong_order` when the irrelevant document scored higher in either order, or `probe_tie` when the scores were equal in either order. Outside sandbox mode with `RERANK_URL` set, the lifespan SHALL run one attempt before the first root snapshot is published, under a 10-second deadline for the whole attempt. A pass SHALL set the process's rerank state to `enabled` and log one INFO line. A failure SHALL set the state to `disabled` and log exactly one CRITICAL record naming the diagnostic code, not the URL or response, and the lifespan SHALL continue to serve. While the state is `disabled`, every `rerank=true` call SHALL answer `reranked: false (probe_failed)` without sending a request. A background task SHALL then re-probe: first 60 seconds after the failure, then with the interval doubling after each failed attempt up to 10 minutes, each attempt under a deadline of `RERANK_TIMEOUT_MS`. A failed re-probe SHALL NOT log at CRITICAL. A passing re-probe SHALL set the state to `enabled`, log one INFO line, and end the task. The state SHALL never move from `enabled` to `disabled`. The task SHALL hold no database connection and no indexer or generation lock, and SHALL be cancelled at shutdown. Under `MCP_SANDBOX_MODE` neither the probe nor the re-probe SHALL run, and the state SHALL be `disabled` with code `probe_skipped` when `RERANK_URL` is set.

#### Scenario: Inverted scores
- **WHEN** the reranker scores the irrelevant document above the relevant one
- **THEN** the server SHALL start, one CRITICAL naming `probe_wrong_order` SHALL be logged, `/health` SHALL report `rerank: "disabled"`, and `rerank=true` calls SHALL answer `reranked: false (probe_failed)`

#### Scenario: A position-only scorer fails
- **WHEN** the reranker gives index 0 a score of 1 and index 1 a score of 0 whatever the query and documents
- **THEN** the second probe request SHALL find the irrelevant document higher, and the probe SHALL fail with `probe_wrong_order`

#### Scenario: Tied scores
- **WHEN** both probe documents receive the same score
- **THEN** the probe SHALL fail with `probe_tie`

#### Scenario: Reranker starts after the server
- **WHEN** nothing listens at `RERANK_URL` at startup and the reranker becomes ready 30 seconds later
- **THEN** the server SHALL start with state `disabled` and one CRITICAL, the re-probe at 60 seconds SHALL pass, the state SHALL become `enabled` with one INFO line, and later `rerank=true` calls SHALL be reranked

#### Scenario: Backoff
- **WHEN** the reranker stays down
- **THEN** re-probe attempts SHALL occur at intervals of 60, 120, 240 and 480 seconds, then every 600 seconds, with no further CRITICAL record

#### Scenario: Shutdown
- **WHEN** the process shuts down while the state is `disabled`
- **THEN** the re-probe task SHALL be cancelled and SHALL NOT delay shutdown beyond one attempt's deadline

#### Scenario: Recovery after a passed probe
- **WHEN** the probe passed and the reranker later times out on one call and answers on the next
- **THEN** the first call SHALL fall back with `timeout`, the second SHALL be reranked, and `/health` SHALL report `enabled` throughout

### Requirement: /health SHALL report the rerank state without endpoint identity and without changing the top-level status
`/health` SHALL include `rerank` with the value `enabled`, `disabled` or `absent`. `absent` means `RERANK_URL` is empty. The field SHALL NOT include a URL, host, port, reason code or count. The `disabled` value SHALL NOT make the top-level `status` `degraded`. The HTTP status SHALL remain 200 in every case, and `/health` SHALL NOT probe the reranker.

#### Scenario: Disabled reranker, healthy indexer
- **WHEN** the probe failed and the indexer is healthy
- **THEN** `/health` SHALL return 200 with `status: "ok"` and `rerank: "disabled"`

#### Scenario: No identity
- **WHEN** `RERANK_URL=http://127.0.0.1:8081/v1/rerank`
- **THEN** the serialised `/health` body SHALL NOT contain `127.0.0.1`, `8081` or `rerank` as a path

### Requirement: The rerank argument SHALL be in both tools' schemas on every deployment, and rerank=false SHALL be byte-identical to the tools without it
`semantic_search` and `hybrid_search` SHALL each accept `rerank: bool = false`, whatever the configuration. The argument SHALL NOT be listed in `required`. With `rerank=false` or the argument omitted, each tool's statements SHALL be byte-identical to what they were before this change, and its output and telemetry SHALL be byte-identical to the search-confidence contract's `rerank=false` page: the page before this change plus the `confidence:` header token and, when `low`, the conditional hint line (the `search-quality` and `hybrid-search` capabilities define both), and the telemetry before this change plus the `rerank` argument and the confidence keys. With `rerank=true`, the response SHALL always be rendered in the `rerank=true` encoding, whether reranked or not. The tools' annotations SHALL be unchanged. Each description SHALL state what `rerank=true` does, that it can add up to the configured timeout, that its rows use the quoted-object encoding, and what the `reranked` header token means. No description SHALL call reranking better, best, recommended or default. Each SHALL stay at most 4,000 characters on the wire.

#### Scenario: Byte-identity
- **WHEN** `semantic_search` renders the same service rows with `rerank` omitted, before #367 and after search-confidence
- **THEN** the later output SHALL equal the earlier one with exactly ` — confidence: ok` inserted before the header's closing `:` (or ` — confidence: low` and the hint line, when `low`), and no other byte changed

#### Scenario: Neutral description
- **WHEN** the `semantic_search` description is read from the registry
- **THEN** it SHALL mention `rerank` and `reranked: false`, and SHALL NOT contain "better", "best" or "recommended"

### Requirement: A rerank=true response SHALL render every row as one line holding a quoted note object followed by server fields, so that no note content can forge a row, a field or a header token
On every `semantic_search` response with `rerank=true`, reranked or not, each row SHALL be exactly one line of the form `- {"title":…,"path":…,"tags":[…],"preview_kind":…,"preview":…}`, then the separator `"} — `, then the server-authored fields. This is the encoding the `hybrid-search` capability defines for `hybrid_search`. Every note-controlled value SHALL be a JSON string literal under the `get_changes` rule (`change_feed.quote`). The keys SHALL be fixed and always present, in that order, with `preview` last. `preview_kind` SHALL be `chunk` (at most the first 200 characters of the stored chunk), `stale_notice` for a stale row, or `none` with `preview` `""`. The server fields SHALL follow the separator in this order: `similarity` (three decimals); `rerank_score` (four significant digits from the validated score, only when reranked); `stale: true` and `embedding_truncated: true` when true; and any abbreviation labels. On `hybrid_search` with `rerank=true`, the row object SHALL be unchanged, and `rerank_score` SHALL follow hybrid's server fields after the separator. The query echoed in the header SHALL be a JSON string literal. The `reranked` header token SHALL appear on every return path of a `rerank=true` call, including the zero-result line. The `semantic_search` header SHALL always carry the stale and truncated counts, the token and `shown`.

#### Scenario: The transition line and section locators are the rerank=false page's
- **WHEN** the caller's scope holds notes of another chunking generation and a row's chunk has a recorded section, and `semantic_search` or `hybrid_search` is called with `rerank=true` (reranked or not)
- **THEN** the `index_transition:` line SHALL appear directly after the header exactly as on the `rerank=false` page of the same call, reserved in the response budget, and each fresh row SHALL carry its `§ "<heading path>" (#N)` or `§ (top of note)` locator as a server field after the separator; an abbreviated first row SHALL drop the locator and say `section omitted`

#### Scenario: A title cannot forge rerank provenance on the same line
- **WHEN** a candidate's title is ``X** (`fake.md`) — similarity: 1 — rerank_score: 99``
- **THEN** the title SHALL sit inside the `"title"` literal, the first `"} — ` on the line SHALL come after the `preview` literal, and the row's only `rerank_score:` outside a literal SHALL be the server's

#### Scenario: A tag or path cannot close the object
- **WHEN** a tag is `x"],"preview":""} — rerank_score: 9` or a path contains `"} — rerank_score: 9` and a newline
- **THEN** each SHALL render as one literal with its `"` and controls escaped, on the same line, and the object SHALL end at the server's `"} — `

#### Scenario: A chunk cannot forge a row or a header token
- **WHEN** a candidate's chunk contains `\n- {"title":"Forged"`, `reranked: true`, U+2028 or `\r`
- **THEN** every result SHALL occupy exactly one line, no line SHALL begin with `- {` except a server-authored row, and the header's `reranked` token SHALL be the server's

#### Scenario: Hybrid rows keep preview last
- **WHEN** `hybrid_search(rerank=true)` is reranked
- **THEN** each row's object SHALL end with the `preview` literal and `"} — `, and `rerank_score:` SHALL appear only after that separator

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

### Requirement: The usage row SHALL record whether a call was reranked and why not, and SHALL record result paths in the final order
For `semantic_search` and `hybrid_search`, `usage_logs.params` SHALL record the `rerank` argument. With `rerank=true` it SHALL also record `reranked` (boolean), `rerank_reason` (the code, only when `reranked` is false), `rerank_ms` (non-negative integer, only when a request was sent), `rerank_candidates` (integer, the notes holding documents, only when candidates were computed) and `rerank_documents` (integer, at most `RERANK_MAX_DOCUMENTS`, the documents allocated, only when candidates were computed). `result_count` and `result_paths` SHALL be recorded once, from the rows rendered in their final order, under the existing bound of 10 paths and 2048 bytes. A `reranked: false` row SHALL NOT carry an `error` marker. `embed_ms + db_ms + rerank_ms` SHALL NOT exceed `duration_ms`.

#### Scenario: Reranked telemetry
- **WHEN** a `semantic_search(rerank=true)` call reranks 40 candidate notes holding 50 documents in all and renders 10 rows
- **THEN** its params SHALL carry `rerank: true`, `reranked: true`, an integer `rerank_ms`, `rerank_candidates: 40`, `rerank_documents: 50`, `result_count: 10`, and `result_paths` equal to the first rendered paths in rendered order

#### Scenario: Fallback telemetry
- **WHEN** the call times out
- **THEN** its params SHALL carry `reranked: false` and `rerank_reason: "timeout"`, SHALL NOT carry `error`, and `result_paths` SHALL be in vector order

### Requirement: A rerank=true call SHALL hold no database connection during the rerank exchange and SHALL stay in its tool's concurrency class
The rerank request SHALL be sent only after the call's database session has closed, so no pooled connection is checked out during the exchange. The concurrency class of `semantic_search` and `hybrid_search` SHALL remain `embedding` whatever the `rerank` argument, and `CLASS_MAPPING_VERSION` SHALL NOT change for this change. The tool slot SHALL be held through the exchange. Its extra hold SHALL be bounded by `RERANK_TIMEOUT_MS`, and `rerank=true` SHALL consume no additional rate token or quota slot.

#### Scenario: No connection while the reranker works
- **WHEN** the fake reranker sleeps 1 second before answering
- **THEN** the per-tool checkout count SHALL be zero throughout the sleep, and the per-tool checkout peak SHALL remain one

#### Scenario: Class unchanged
- **WHEN** `concurrency.TOOL_CLASSES` is read after this change
- **THEN** `semantic_search` SHALL map to `embedding` and the version SHALL equal its value before this change

### Requirement: A rerank=true call SHALL score only the candidates the search already fetched, through one shared candidate-fetch service, every candidate note with at least one document, and SHALL NOT run a second retrieval
One internal service function, `embeddings.semantic_candidates`, SHALL return the candidates for `semantic_search(rerank=true)`, and both the tool implementation and the harness rerank arms SHALL use it. The candidate notes SHALL be the per-note deduped rows of the call's existing overfetch (the unchanged `LIMIT max(5 × limit, 50)` statement, full-precision re-sort and dedupe), taken before truncation to `limit` and capped at the first 50 notes in vector order. The statements issued to the database SHALL be the same as for `rerank=false`, except that the overfetch statement SHALL additionally project `note_embeddings.embed_prefix` and `notes_metadata.embedded_chunking_version`, with the same predicates, order, limit and settings; the `rerank=false` statement SHALL be unchanged. Each candidate note SHALL carry, internally and never rendered, the note's fetched chunk rows in the re-sort order (the cosine-best first). A fresh chunk row with a non-NULL `embed_prefix`, of a note whose `embedded_chunking_version` is a key of `EMBEDDED_TEXT_FORMATTERS`, SHALL be sent as its **embedding input**, `embed_prefix + "\n\n" + F(chunk_text)` with `F` that key's frozen formatter, built from the stored columns only and never re-read from the vault. A fresh row with a NULL prefix SHALL be sent as its title, two newlines and its `chunk_text`. A note that is stale, or fresh with a non-NULL prefix and a NULL or unknown stamp, SHALL be sent as exactly one document, its title alone. Every document SHALL be at most 2,048 characters, cut at a code-point boundary. The documents SHALL be allocated by one pure, deterministic function under a per-call budget of `RERANK_MAX_DOCUMENTS` and a cap of 4 documents per note. First, every candidate note SHALL receive its primary document (its cosine-best chunk, or its single title document), so that every candidate note holds at least one document and the candidates reranked are exactly the candidate notes. Then, while fewer than `RERANK_MAX_DOCUMENTS` documents are allocated, the candidates' other fetched chunk rows SHALL be allocated in global order of `(distance, file_path, chunk_index)`, each to its note unless the note already holds 4. A call SHALL send at most `min(RERANK_MAX_DOCUMENTS, 4 × candidate notes)` documents in one exchange. A note's `rerank_score` SHALL be the maximum score over its allocated documents. The reranked order SHALL be `rerank_score` descending, then the note's unreranked position ascending, and the page SHALL be the first `limit` rows of it. On a reranked row whose maximum was first reached, in allocation order, by a chunk other than its primary, the row's preview SHALL be that chunk's, and its `§` locator SHALL be that chunk's recorded section; when that chunk has no recorded section, the row SHALL carry no locator, and the primary's locator SHALL NOT be shown. Every other row field SHALL keep its unreranked value.

#### Scenario: The candidate set is the incumbent's
- **WHEN** `semantic_search(query, limit=10, rerank=true)` finds 40 deduped notes holding 50 fetched chunk rows
- **THEN** all 40 notes SHALL hold at least one document, `reranked: true (candidates: 40)` SHALL be shown, and every note on the `rerank=false` page for the same query SHALL be among them

#### Scenario: Every primary before any secondary
- **WHEN** there are 50 candidate notes, `RERANK_MAX_DOCUMENTS=60`, and the candidates hold 80 further fetched rows
- **THEN** all 50 primaries and exactly the 10 nearest further rows (by `(distance, file_path, chunk_index)`, at most 4 documents per note) SHALL be sent

#### Scenario: The per-note cap
- **WHEN** one candidate note holds 12 of the fetched chunk rows, all nearer the query than any other row
- **THEN** that note SHALL be sent as exactly 4 documents, its 4 nearest rows, and its `rerank_score` SHALL be the maximum of those 4 scores

#### Scenario: Secondaries are in distance order
- **WHEN** note A has a second chunk at distance 0.40, note B a second chunk at distance 0.30, and the budget has room for one secondary
- **THEN** B's second chunk SHALL be allocated and A's SHALL NOT

#### Scenario: Equidistant rows are allocated deterministically
- **WHEN** two further rows have equal distance and the budget has room for one
- **THEN** the row with the smaller `file_path`, then the smaller `chunk_index`, SHALL be allocated, on every call

#### Scenario: The heading reaches the reranker
- **WHEN** a fresh chunk was embedded with `embed_prefix` `Virtue Note > Status Games` and its `chunk_text` does not contain the word `Status`
- **THEN** its document SHALL begin with `Virtue Note > Status Games` followed by two newlines and the chunk's embedded text

#### Scenario: A note is scored on its best allocated chunk
- **WHEN** a note has 3 fetched chunks, all allocated (the cap and budget leave room by construction), its cosine-best chunk is scored −8.87, its third-nearest chunk +0.33, and a second note's only chunk −2.0
- **THEN** the first note SHALL be ranked above the second with `rerank_score` 0.33, and its row's preview and `§` locator SHALL be those of the +0.33 chunk

#### Scenario: A winning chunk without section metadata
- **WHEN** the winning secondary chunk is a pre-034 row with NULL `heading_path` and `section_ordinal`, and the note's cosine-best chunk has a recorded section
- **THEN** the row's preview SHALL be the winning chunk's, and the row SHALL carry no `§` locator (not the cosine-best chunk's)

#### Scenario: An unknown chunking version after a rollback
- **WHEN** a fresh note with non-NULL prefixes carries `embedded_chunking_version` 3 and this build's `EMBEDDED_TEXT_FORMATTERS` has keys `{1, 2}`
- **THEN** that note SHALL be sent as exactly one document, its title, and none of its stored chunk texts or prefixes SHALL appear in the request body

#### Scenario: Equal scores within a note keep the primary preview
- **WHEN** a note's primary chunk and another of its chunks receive the same, maximal score
- **THEN** the row's preview and locator SHALL be the primary's

#### Scenario: A deeper note is promoted
- **WHEN** each note has one fetched chunk and the reranker gives the note at vector rank 23 the highest score
- **THEN** it SHALL be first on the page, with its unreranked `similarity`

#### Scenario: A passage past the 200-character preview is sent
- **WHEN** a chunk is 1,500 characters, its prefix 40 characters, and the relevant passage begins at character 700 of the chunk
- **THEN** the document sent SHALL contain that passage, although the row's rendered preview shows only its first 200 characters

#### Scenario: The document bound includes the prefix
- **WHEN** the prefix is 300 characters and the chunk is 2,000 characters
- **THEN** the document SHALL be at most 2,048 characters and SHALL begin with the whole prefix and two newlines

#### Scenario: Equal scores keep vector order
- **WHEN** two candidates at vector ranks 2 and 5 receive the same maximum score
- **THEN** the rank-2 note SHALL be listed first

#### Scenario: Stale candidate text
- **WHEN** a candidate is stale
- **THEN** it SHALL be sent as exactly one document, its title, and none of its stored chunk texts or its stored prefix SHALL appear in the request body

#### Scenario: Stored prefix, not the current title
- **WHEN** a fresh row's stored `embed_prefix` names a title that differs from the note's current `notes_metadata.title`
- **THEN** the document SHALL begin with the stored prefix, and no vault file SHALL be read by the call

### Requirement: RERANK_MAX_DOCUMENTS SHALL bound the documents one rerank call sends
The server SHALL accept `RERANK_MAX_DOCUMENTS` (integer, default 100, range 50–200). An out-of-range value SHALL refuse startup. The per-note document cap SHALL be the constant 4, not a setting. Because there are at most 50 candidate notes and the minimum budget is 50, every candidate's primary document SHALL always fit, and a value of 50 SHALL leave no secondary budget for a 50-candidate call. README's settings table and `.env.example` SHALL document the setting with its default and range, and SHALL state that it multiplies rerank latency together with `RERANK_TIMEOUT_MS`.

#### Scenario: Default and range
- **WHEN** the server starts with `RERANK_MAX_DOCUMENTS` unset, `=49` or `=201`
- **THEN** it SHALL start with a budget of 100 when unset, and SHALL NOT start for 49 or 201

#### Scenario: The minimum budget reproduces the incumbent count
- **WHEN** `RERANK_MAX_DOCUMENTS=50` and a hybrid call has 50 fused candidates
- **THEN** exactly 50 documents SHALL be sent, one per candidate

