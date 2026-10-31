## RENAMED Requirements

- FROM: `### Requirement: A rerank=true call SHALL score only the candidates the search already fetched, through one shared candidate-fetch service, one document per note, and SHALL NOT run a second retrieval`
- TO: `### Requirement: A rerank=true call SHALL score only the candidates the search already fetched, through one shared candidate-fetch service, one document per fetched chunk, and SHALL NOT run a second retrieval`

## MODIFIED Requirements

### Requirement: The reranker SHALL be configured by four optional settings, and an empty RERANK_URL SHALL mean the feature is absent
The server SHALL accept `RERANK_URL` (string, default empty), `RERANK_MODEL` (string, default empty, at most 200 characters, no control characters), `RERANK_TIMEOUT_MS` (integer, default 1500, range 100–30000) and `RERANK_ALLOW_PLAINTEXT` (boolean, default false). `RERANK_URL` SHALL be the full endpoint URL. An empty or unset `RERANK_URL` SHALL mean no reranker is configured. An out-of-range `RERANK_TIMEOUT_MS` or an invalid `RERANK_MODEL` SHALL refuse startup. README's settings table and `.env.example` SHALL document all four with their defaults, and SHALL state that the endpoint receives note text. Neither SHALL contain a host path. DEPLOYMENT.md SHALL give the measured per-call rerank latency for a 50-document call (short and full-length documents) on named hardware and a rule for sizing `RERANK_TIMEOUT_MS` from it.

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

### Requirement: A rerank=true call SHALL score only the candidates the search already fetched, through one shared candidate-fetch service, one document per fetched chunk, and SHALL NOT run a second retrieval
One internal service function, `embeddings.semantic_candidates`, SHALL return the candidates for `semantic_search(rerank=true)`, and both the tool implementation and the harness rerank arms SHALL use it. The candidate notes SHALL be the per-note deduped rows of the call's existing overfetch (the unchanged `LIMIT max(5 × limit, 50)` statement, full-precision re-sort and dedupe), taken before truncation to `limit` and capped at the first 50 notes in vector order. The statements issued to the database SHALL be the same as for `rerank=false`, except that the overfetch statement SHALL additionally project `note_embeddings.embed_prefix` and `notes_metadata.embedded_chunking_version`, with the same predicates, order, limit and settings; the `rerank=false` statement SHALL be unchanged. Each candidate note SHALL carry, internally and never rendered, the note's fetched chunk rows in the re-sort order (the cosine-best first), or none when the note is stale. A fresh chunk row with a non-NULL `embed_prefix` SHALL be sent as its **embedding input**, `embed_prefix + "\n\n" + F(chunk_text)`, where `F` is the frozen formatter of the note's `embedded_chunking_version` (`EMBEDDED_TEXT_FORMATTERS`) or the identity when that stamp is not a key, built from the stored columns only and never re-read from the vault; a fresh row with a NULL prefix SHALL be sent as its title, two newlines and its `chunk_text`; a stale note SHALL be sent as one document, its title alone. Every document SHALL be at most 2,048 characters, cut at a code-point boundary. The documents SHALL be allocated by one pure, deterministic function under a budget of 50 documents per call and a cap of 3 documents per note. First, the primary document (the cosine-best chunk, or the stale title) of each of the first `min(limit, m)` candidate notes, where `m` is the number of candidate notes. Then, while fewer than 50 documents are allocated, every remaining fetched chunk row of a fresh candidate note, in global order of `(distance, file_path, chunk_index)`, each given to its note unless the note already holds 3 documents (a note's first document SHALL be its primary). Then, while fewer than 50 are allocated, the primary documents of the notes still holding none, in vector order. A call SHALL send at most 50 documents, at most 3 per note, in one exchange. The candidates reranked SHALL be the notes holding at least one document. A note's `rerank_score` SHALL be the maximum score over its documents, and the reranked order SHALL be `rerank_score` descending, then the note's unreranked position ascending; the page SHALL be the first `limit` rows of it. On a reranked row whose maximum was reached first by a chunk other than the cosine-best chunk, the row's preview chunk and section locator SHALL be that chunk's; every other row field SHALL keep its unreranked value.

#### Scenario: The unreranked page is contained in the candidates
- **WHEN** `semantic_search(query, limit=10, rerank=true)` finds 40 deduped notes holding 50 fetched chunk rows, none stale and none holding more than 3 rows
- **THEN** the reranker SHALL receive exactly those 50 rows as 50 documents, and every note on the `rerank=false` page for the same query SHALL be among the reranked candidates

#### Scenario: The per-note cap
- **WHEN** one candidate note holds 12 of the fetched chunk rows, all nearer the query than any other row
- **THEN** that note SHALL be sent as exactly 3 documents, its 3 nearest rows, and its `rerank_score` SHALL be the maximum of those 3 scores

#### Scenario: Pass 2 is in distance order
- **WHEN** `limit=1`, note A (vector rank 1) has a second chunk at distance 0.40, note B (vector rank 2) has its only chunk at distance 0.30, and the budget has room for one more document after A's primary
- **THEN** B's chunk SHALL be allocated before A's second chunk

#### Scenario: Equidistant rows are allocated deterministically
- **WHEN** two remaining rows have equal distance
- **THEN** the row with the smaller `file_path`, then the smaller `chunk_index`, SHALL be allocated first, on every call

#### Scenario: The document budget binds
- **WHEN** `semantic_search(query, limit=30, rerank=true)` fetches 150 chunk rows belonging to 50 deduped notes, none stale
- **THEN** the reranker SHALL receive at most 50 documents, at most 3 per note, and each of the first 30 notes in vector order SHALL hold at least one

#### Scenario: The heading reaches the reranker
- **WHEN** a fresh chunk was embedded with `embed_prefix` `Virtue Note > Status Games` and its `chunk_text` does not contain the word `Status`
- **THEN** its document SHALL begin with `Virtue Note > Status Games` followed by two newlines and the chunk's embedded text

#### Scenario: A note is scored on its best fetched chunk
- **WHEN** a note's cosine-best chunk is scored −8.87 and another of its fetched chunks +0.33, and a second note's only chunk −2.0
- **THEN** the first note SHALL be ranked above the second with `rerank_score` 0.33, and its row's preview and `§` locator SHALL be those of the +0.33 chunk

#### Scenario: Equal scores within a note keep the cosine-best preview
- **WHEN** a note's cosine-best chunk and another of its chunks receive the same, maximal score
- **THEN** the row's preview and locator SHALL be the cosine-best chunk's

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

### Requirement: rerank on hybrid_search SHALL rerank the first 50 fused rows, with candidate text read inside the hybrid snapshot
The `hybrid_search` tool defined by the `hybrid-search` capability SHALL accept `rerank: bool = false` with the same contract as `semantic_search`. The candidate notes SHALL be the first 50 rows of the fused list in fused order, before truncation to `limit`. A candidate that is in the semantic arm and not stale SHALL be sent as the embedding inputs of its fetched semantic-arm chunk rows (the 50-chunk overfetch), built and bounded as for `semantic_search`, its cosine-best chunk as its primary document. Any other candidate SHALL be sent as one document: its title, two newlines, and its stored `body_excerpt`, bounded to 2,048 characters in all and cut at a code-point boundary, read by one statement inside the hybrid call's transaction and snapshot under the owner predicate, issued after the keyword snippet statements and only when such a candidate exists. A NULL excerpt SHALL send the title alone. The documents SHALL be allocated, scored, maximised per note and ordered exactly as for `semantic_search`, with fused order in place of vector order for the first and third passes, the semantic-arm distance for the second, and at most 50 documents per call and 3 per note. A keyword-only hybrid call SHALL still rerank, one excerpt document per note. With `rerank=false`, `hybrid_search` SHALL issue no additional statement and its output SHALL be unchanged.

#### Scenario: A keyword-only candidate is scored on its excerpt
- **WHEN** a fused candidate appears only in the keyword arm and has an excerpt
- **THEN** it SHALL be sent as one document, its title, two newlines and the excerpt's leading characters up to the 2,048-character bound, read in the same snapshot as the arms

#### Scenario: A both-arms note keeps its place on its best chunk
- **WHEN** a note is first in both arms, its cosine-best chunk scores −8.87, another fetched chunk of it scores +0.33, and every other candidate scores below +0.33
- **THEN** it SHALL be first on the reranked page with `rerank_score: 0.33`, and its `§` locator SHALL be the +0.33 chunk's

#### Scenario: Mixed candidates stay within the budget
- **WHEN** the 50 fused candidates hold 30 keyword-only notes and 20 semantic notes with 50 fetched chunks among them, and `limit=10`
- **THEN** at most 50 documents SHALL be sent, at most 3 per note, and each of the first 10 fused notes SHALL hold at least one

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

### Requirement: The usage row SHALL record whether a call was reranked and why not, and SHALL record result paths in the final order
For `semantic_search` and `hybrid_search`, `usage_logs.params` SHALL record the `rerank` argument. With `rerank=true` it SHALL also record `reranked` (boolean), `rerank_reason` (the code, only when `reranked` is false), `rerank_ms` (non-negative integer, only when a request was sent), `rerank_candidates` (integer, the notes holding documents, only when candidates were computed) and `rerank_documents` (integer, at most 50, the documents allocated, only when candidates were computed). `result_count` and `result_paths` SHALL be recorded once, from the rows rendered in their final order, under the existing bound of 10 paths and 2048 bytes. A `reranked: false` row SHALL NOT carry an `error` marker. `embed_ms + db_ms + rerank_ms` SHALL NOT exceed `duration_ms`.

#### Scenario: Reranked telemetry
- **WHEN** a `semantic_search(rerank=true)` call reranks 40 candidate notes holding 50 documents and renders 10 rows
- **THEN** its params SHALL carry `rerank: true`, `reranked: true`, an integer `rerank_ms`, `rerank_candidates: 40`, `rerank_documents: 50`, `result_count: 10`, and `result_paths` equal to the first rendered paths in rendered order

#### Scenario: Fallback telemetry
- **WHEN** the call times out
- **THEN** its params SHALL carry `reranked: false` and `rerank_reason: "timeout"`, SHALL NOT carry `error`, and `result_paths` SHALL be in vector order
