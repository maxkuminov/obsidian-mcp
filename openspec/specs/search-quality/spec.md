# search-quality Specification

## Purpose
What the vector and keyword search paths must return and how they render it: filtered HNSW recall held against an exact baseline, the correctness `SET LOCAL`s, staleness and truncation annotations that never filter, explicit projections, keyword ranking and snippets, and (#355) the section locator that names the `read_note(section="#N")` a fresh vector row matched, one-line previews from the uncut chunk, and the `index_transition:` line that `semantic_search`, `find_related` and `hybrid_search` all carry while the caller's notes hold vectors of another chunking generation.
## Requirements
### Requirement: Filtered vector search recall against an exact baseline

`semantic_search` and `find_related` SHALL execute their HNSW queries with pgvector iterative scan enabled for the transaction (`hnsw.iterative_scan = relaxed_order`), so that when `folder`, `tags`, `frontmatter`, or user-scope filters discard index candidates the scan continues until the requested chunk overfetch (`max(5 × limit, 50)`) is satisfied, the index is exhausted, or pgvector's scan bounds (`hnsw.max_scan_tuples`, `hnsw.scan_mem_multiplier`) are reached. Rows returned by the query SHALL be re-ordered by cosine distance in the service before per-note dedupe and truncation, so the presented order is monotone; this re-sort orders returned candidates only and adds none.

The recall contract is a **benchmark SLO, not a per-query guarantee**, and its baseline is defined at the same candidate depth: for a filtered query, the notes returned SHALL achieve set-recall ≥ 0.9 against the notes produced by an *exact* filtered sequential scan that takes the same overfetch of nearest chunks and applies the same per-note dedupe and truncation. HNSW is approximate and the overfetch is fixed, so results MAY number fewer than `limit` after dedupe (one verbose note can own many of the nearest chunks) — that is a property of the overfetch, shared by the baseline, not a recall failure. Non-emptiness is guaranteed by construction rather than by the SLO: whenever a *filtered* HNSW query returns zero rows, the service SHALL re-run the same filtered query as an exact sequential scan (`SET LOCAL enable_indexscan = off` for that statement) and return its results, so a filtered search is empty only when no embedded note matches the filter. The recall SLO is measured over a fixed, versioned corpus and query set (see the integration test) with deterministic insertion order and index-build settings, ties at the cutoff counted as equivalent, and passing on each of three index rebuilds. Both vector paths SHALL use the same overfetch, `max(5 × limit, 50)` (`find_related` currently uses `limit × 5` and is brought in line).

#### Scenario: Filtered recall meets the baseline

- **WHEN** the fixed benchmark corpus (several hundred embedded chunks in folder `A/`, a few dozen in `B/`, plus tag/frontmatter/user variants) is queried with each benchmark query vector, `folder="B/"`, and the same query is executed as an exact filtered sequential scan at the same overfetch depth with the same dedupe
- **THEN** the HNSW plan SHALL be used (verified by `EXPLAIN`), and on each of three index rebuilds the returned `B/` notes SHALL cover at least 90% of the baseline notes (ties at the cutoff counted as equivalent) and SHALL be non-empty

#### Scenario: Zero-row filtered result falls back to exact scan

- **WHEN** a filtered HNSW query returns zero rows while at least one embedded note satisfies the filter
- **THEN** the service SHALL execute the exact filtered sequential query and return its results, and the usage log SHALL record `exact_fallback: true`

#### Scenario: Iterative scan is what provides the recall

- **WHEN** the same benchmark runs with `hnsw.iterative_scan = off` in an otherwise identical transaction
- **THEN** at least one benchmark query SHALL return an empty or shorter-than-baseline set (the fixture reproduces the failure being fixed, so the guard is meaningful)

#### Scenario: Results are ordered by distance

- **WHEN** iterative scan returns candidates across multiple scan iterations
- **THEN** the returned results SHALL be in non-decreasing cosine distance order after dedupe

#### Scenario: Both vector paths set the scan mode

- **WHEN** `semantic_search` or `find_related` executes its query
- **THEN** the transaction SHALL have issued `SET LOCAL hnsw.iterative_scan` before the vector query, in addition to the existing `hnsw.ef_search` and `random_page_cost` settings

#### Scenario: All filter shapes covered

- **WHEN** the benchmark is repeated with a `tags` filter, a `frontmatter` filter, a multi-user `user_id` scope, and via `find_related`
- **THEN** the same SLO SHALL hold

### Requirement: pgvector version guard for iterative scan

At startup (outside sandbox mode) the server SHALL read `pg_extension.extversion` for `vector` and SHALL exit non-zero with a message naming the minimum (`0.8.0`) if the installed pgvector does not support `hnsw.iterative_scan`. This prevents an older backend from accepting the setting as a placeholder GUC and silently running the non-iterative plan.

#### Scenario: Older pgvector refused

- **WHEN** the database reports pgvector `0.7.4`
- **THEN** startup SHALL fail with a message that names `hnsw.iterative_scan` and `0.8.0`

#### Scenario: Supported pgvector accepted

- **WHEN** the database reports pgvector `0.8.2`
- **THEN** startup SHALL proceed and a fresh pooled connection that issues the `SET LOCAL` and then the vector query SHALL show `hnsw.iterative_scan = relaxed_order` for the transaction

### Requirement: Keyword search planner setting and deterministic ordering

`full_text_search` SHALL issue `SET LOCAL random_page_cost = 1.1` in the same transaction as every statement it runs for one call — the conjunctive statement, the disjunctive fallback when one runs, and the page-bounded snippet statement (the same transaction-scoped setting the vector path uses, no global Postgres change) — and SHALL order each block's rows by rank descending with `file_path` ascending as a deterministic tie-break, so membership and order within a block are stable across plans, and SHALL never interleave the two blocks. Matching semantics of the conjunctive statement SHALL be unchanged: for any query and filters the set of rows tagged `match: "all"` SHALL equal the set returned without the planner setting.

#### Scenario: Setting is issued and results are unchanged

- **WHEN** `full_text_search` runs against a populated database with and without the planner setting for a matrix of rare and common terms combined with folder, tag, frontmatter, and user filters
- **THEN** the returned rows SHALL be identical in membership, tag and order in every case

#### Scenario: Index plan on a production-sized corpus

- **WHEN** `EXPLAIN (ANALYZE, BUFFERS)` is run for a rare-term query over a seeded, analysed keyword corpus of at least 3,000 notes with realistic tsvector sizes, once with the setting applied and once with the sequential baseline forced by `SET LOCAL enable_indexscan = off; SET LOCAL enable_bitmapscan = off` (leaving `enable_seqscan` on)
- **THEN** the plan with the setting SHALL use the tsvector index (bitmap or index scan) and SHALL read fewer buffers than the forced sequential plan for the same query (asserted); for a common-term query the plans and buffer counts are recorded, not asserted

#### Scenario: One transaction carries every statement of a call

- **WHEN** a call issues the conjunctive statement, a fallback and a snippet statement
- **THEN** all three SHALL execute on the connection that issued the `SET LOCAL`, before that transaction ends

### Requirement: Search caches are pre-warmed on the indexer tick

After each periodic indexer pass, while still holding the indexer pass lock, the server SHALL re-check the paused flag and then issue one short embedding request (only when the embedding provider is a local model provider such as Ollama) and one HNSW probe query using a deterministic non-zero unit vector of `EMBEDDING_DIMENSIONS`, so the embedding model stays resident and the index's hot pages stay cached between sparse searches. The whole pre-warm SHALL be bounded by a single wall-clock timeout of 15 seconds (`asyncio.wait_for`); on timeout or any ordinary exception it SHALL log at WARNING and return without raising, without changing the indexer's failure counter. `asyncio.CancelledError` SHALL be re-raised immediately so lifespan shutdown cancels the indexer task as before. The next tick begins `INDEX_INTERVAL_SECONDS` after the pre-warm completes or times out, so a tick is delayed by at most 15 seconds beyond the index pass. Because the pre-warm runs under the pass lock, the panel's reset-embeddings and legacy re-embed actions SHALL also acquire the pass lock before their destructive statements — setting the pause flag first, ending the request's own database transaction before waiting (so waiters never pin a pool connection), then acquiring a connection only after the lock is held — so a reset can never run concurrently with a probe or an index pass. The wait is bounded only by the current pass, not by the pre-warm timeout. The legacy re-embed action SHALL, in the same locked transaction that deletes `note_embeddings`, set `notes_metadata.embedded_content_hash = NULL`, so the subsequent reindex actually re-embeds (today it deletes the vectors and leaves the hashes, and nothing is re-embedded). The HNSW probe SHALL run only when an HNSW index exists on `note_embeddings.embedding` (deployments with `EMBEDDING_DIMENSIONS > 2000` have none); the embedding pre-warm is independent of that.

#### Scenario: Pre-warm runs after a tick

- **WHEN** a periodic indexer tick completes (with or without changes) and the indexer is not paused
- **THEN** one embedding request (local provider only) and one HNSW probe SHALL be issued under the pass lock and a log line SHALL record their timings

#### Scenario: Pre-warm failure or hang is contained

- **WHEN** the embedding provider or database raises, or the pre-warm exceeds 15 seconds
- **THEN** a WARNING SHALL be logged, the pre-warm SHALL be cancelled or abandoned, and the loop SHALL sleep for the normal interval as if the pre-warm had succeeded

#### Scenario: External cancellation propagates

- **WHEN** the indexer task is cancelled (lifespan shutdown) while the pre-warm is awaiting the embedding provider or the database
- **THEN** `CancelledError` SHALL propagate out of the pre-warm and the indexer loop SHALL exit promptly

#### Scenario: Reset waits for the lock without pinning the pool

- **WHEN** several reset-embeddings requests arrive while a tick (index pass or pre-warm) holds the pass lock
- **THEN** each SHALL set the pause flag, release its request transaction, wait for the lock without holding a pool connection, and only then acquire a connection and execute its destructive statements

#### Scenario: Legacy re-embed clears the embedded hashes

- **WHEN** the legacy re-embed action runs on a fully indexed vault
- **THEN** every `note_embeddings` row SHALL be deleted and every `notes_metadata.embedded_content_hash` SHALL be NULL in the same transaction, and the following reindex SHALL re-embed every note

#### Scenario: Skipped when paused, sandboxed, or remote provider

- **WHEN** the indexer becomes paused during the index pass, or `MCP_SANDBOX_MODE` is on, or the embedding provider is a remote API
- **THEN** the embedding pre-warm SHALL NOT be issued; the DB probe SHALL still run for a remote provider, and nothing SHALL run when paused or sandboxed

#### Scenario: Probe uses the index when one exists

- **WHEN** an HNSW index exists on `note_embeddings.embedding` and the probe query is explained
- **THEN** it SHALL use that index; when no HNSW index exists (dimensions above the pgvector index limit) the probe SHALL be skipped and logged

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

### Requirement: The owner predicate makes every vector query a filtered query

Because read-path owner scoping is total, `semantic_search` and `find_related` SHALL treat the owner predicate as a filter for exact-fallback eligibility: whenever the approximate vector query returns zero rows — under any combination of `folder`, `tags`, `frontmatter`, and owner scope, an ownerless (`user_id IS NULL`) scope included — the service SHALL re-run the identical statement as an exact sequential scan and return its results, recording `exact_fallback: true` in the usage log. Within those two tools there SHALL be no code path on which a vector query runs unfiltered or on which a zero-row approximate result is returned without the exact re-run. (The indexer's internal pre-warm probe issues an unfiltered nearest-neighbor statement by design; it returns nothing to any caller and is out of this requirement's scope.)

#### Scenario: Ownerless zero-row result on a mixed database falls back

- **WHEN** the database holds many named-user vectors and at least one matching NULL-owned vector, and an ownerless `semantic_search` HNSW query returns zero rows after the owner predicate discards every candidate
- **THEN** the service SHALL run the exact filtered scan and return the matching NULL-owned notes, and the usage log SHALL record `exact_fallback: true`

### Requirement: A stale vector result is annotated and never filtered out
`semantic_search` and `find_related` SHALL mark every returned note whose stored vectors predate its indexed content — `embedded_content_hash IS DISTINCT FROM content_hash` — as stale, and SHALL continue to return it. Neither tool SHALL add a staleness predicate to its vector query, and neither SHALL drop a row on account of staleness.

The comparison SHALL be `IS DISTINCT FROM`, so a note that has never been embedded, or whose certification was cleared by a move, is stale rather than falling through a NULL comparison as fresh.

**Filtering is refused, and the refusal is the requirement.** A hash-equality filter would remove every note edited since the last completed embed pass — a window of minutes in normal operation and the whole vault during a provider outage — and, because every vector query in these two tools is a filtered query whose zero-row result triggers an exact sequential re-run, it would convert an outage into an O(n) scan of the embedding table on every search. A slightly stale hit that says so is better for an agent than a missing note.

Each tool's result SHALL carry a count of stale rows **whenever it returns any rows at all, including when that count is zero**, so that a caller can distinguish "nothing here is stale" from a build that does not report staleness. Per-row marking SHALL identify which rows are stale.

`find_related` SHALL additionally state, once, when the **source** note is itself stale, because in that case the averaged query vector describes the source's previous content and every neighbour answers a superseded question — a fact no per-row marker can express. It SHALL state it on **every return path on which the source row was loaded, the empty one included**. "No related notes for this note" from a stale source is the reading a consumer acts on — that the note has no neighbours — when the truth is that the vector searched with describes content the note no longer has, so the empty result is where the statement matters most. The distinct "the source has not been embedded yet" refusal keeps its own message: a source with no vectors at all is a different fact with a different fix.

**The guarantee is scoped to what the index has committed, and the residual is declared rather than closed.** Staleness is derived from the metadata row, so a note reads as stale only once the scan has committed its new content hash. Between an edit landing on disk and the next scan reaching that note — bounded by the index interval plus the pass in flight — the row's two hashes still agree while the stored chunk text is already superseded, and the result is presented as fresh. Closing that would require hashing the file on disk for every returned row, putting a filesystem read on the hot path of every search and still racing the writer. The system SHALL therefore state the guarantee as *"no result presents text the index knows to be superseded"*, SHALL document the residual window where the tools are described, and SHALL NOT restate it as a stronger claim.

#### Scenario: A stale note is still returned, and is marked

- **WHEN** a note's content changes and a `semantic_search` whose nearest chunk belongs to that note runs before the next embed pass completes
- **THEN** the note SHALL appear in the results at the rank its stored vector earns
- **AND** it SHALL be marked stale, and the result SHALL report at least one stale row

#### Scenario: A provider outage does not empty the results

- **WHEN** the embedding provider has been unavailable long enough that every note in the vault has been edited since it was last embedded
- **THEN** `semantic_search` SHALL return its usual number of results
- **AND** every one of them SHALL be marked stale
- **AND** no exact sequential fallback SHALL be triggered by staleness

#### Scenario: The stale count is present when nothing is stale

- **WHEN** every returned note's stored vectors match its indexed content
- **THEN** the result SHALL still report its stale count, as zero

#### Scenario: A stale source is stated once

- **WHEN** `find_related` is called on a note whose own `embedded_content_hash` differs from its `content_hash`
- **THEN** the result SHALL state that the source note changed after it was embedded and that the neighbours were computed from its previous content
- **AND** the neighbours SHALL still be returned

#### Scenario: A stale source is stated on the empty result too

- **WHEN** `find_related` is called on a stale source that has vectors and the query returns no neighbour after the exact fallback
- **THEN** the result SHALL still state that the source changed after it was embedded
- **AND** it SHALL NOT present the empty result as a bare statement that the note has no related notes

#### Scenario: An unembedded source keeps its own refusal

- **WHEN** `find_related` is called on a note that has no vectors at all
- **THEN** the existing "not embedded yet" message and its usage marker SHALL be returned unchanged
- **AND** it SHALL NOT be replaced by the stale-source statement

#### Scenario: An edit the scan has not yet seen is not marked, and that bound is declared

- **WHEN** a note is edited on disk and a vector search returns it before the next index pass has committed its new content hash
- **THEN** the row SHALL NOT be marked stale, because the index does not yet know the note changed
- **AND** this SHALL be documented as the declared bound of the staleness signal rather than described as a case the signal covers

#### Scenario: No query predicate changes

- **WHEN** either tool executes its vector query
- **THEN** the statement SHALL carry the same owner, folder, tag and frontmatter predicates, the same overfetch, the same `SET LOCAL` settings and the same zero-row exact-fallback eligibility as before
- **AND** the staleness columns SHALL be read from the joined metadata row rather than filtered on

### Requirement: A stale row's chunk preview is withheld rather than shown
Where a vector-search row is stale, the tool SHALL withhold that row's chunk preview and SHALL replace it with an explicit notice naming `read_note` as the way to obtain the note's current text. Every other field of the row — path, title, tags, and the similarity or distance — SHALL be returned unchanged.

The distinction is what each field is. Path, title and tags are read from the metadata row, which the scan refreshed at the moment it committed the new content hash, so they describe the note as it stands. The similarity is a retrieval score, not an assertion about content. The chunk preview is the only field that is a **verbatim quotation of the note's text**, it is the only field that is out of date, and it is the field a consumer will reproduce in an answer. Withholding it turns a silently wrong result into a visibly degraded one whose remedy is one call away.

The tool SHALL NOT substitute the note's current leading text for the withheld preview: that text is a different span from the one that matched, presented where the matching span belongs, and it would read as an excerpt the search actually found.

#### Scenario: The preview is withheld and the row survives

- **WHEN** a stale note is returned by `semantic_search`
- **THEN** its chunk preview SHALL be absent from the result
- **AND** a notice in its place SHALL say that the note changed after it was embedded and that `read_note` returns the current content
- **AND** its path, title, tags and similarity SHALL be present and unchanged

#### Scenario: A fresh row keeps its preview

- **WHEN** a returned note's stored vectors match its indexed content
- **THEN** its chunk preview SHALL be returned exactly as before this change

#### Scenario: The substitute is not the note's current text

- **WHEN** a stale row's preview is withheld
- **THEN** the replacement SHALL NOT contain any text read from the note's current bytes

#### Scenario: Both vector tools behave the same way

- **WHEN** the same stale note is returned by `semantic_search` and by `find_related`
- **THEN** both SHALL withhold the preview and both SHALL mark the row stale

### Requirement: A note whose chunking was capped is marked in vector results
`semantic_search` and `find_related` SHALL mark a returned note whose embedding was truncated at the per-note chunk cap, so that a match drawn from the note's head is not read as a match against the whole note.

The marker SHALL be read from the note's durable `chunks_truncated` column and SHALL NOT be inferred from the number of chunk rows: a capped note holds exactly the cap and is indistinguishable by row count from a note that legitimately produces that many.

#### Scenario: A capped note's result says so

- **WHEN** a note whose `chunks_truncated` is true is returned by either vector tool
- **THEN** its row SHALL be marked as having a truncated embedding
- **AND** the marking SHALL state that the note's tail was not embedded and is therefore not reachable by semantic search

#### Scenario: An uncapped note is not marked

- **WHEN** a note whose `chunks_truncated` is false is returned
- **THEN** no truncation marking SHALL appear on its row

### Requirement: Read paths SHALL select only the columns they render
`semantic_search`, `keyword_search`, `list_notes`, `get_recent`, `find_orphans` and `get_neighborhood`'s metadata hydration SHALL each select an explicit list of columns, and that list SHALL contain neither `notes_metadata.content_tsvector`, nor `notes_metadata.body_excerpt`, nor `note_embeddings.embedding`. `content_tsvector` and `body_excerpt` SHALL be mapped as deferred attributes that raise when read from a loaded entity, so that no whole-entity load ships them and an accidental reader fails loudly instead of lazy-loading. `keyword_search`'s snippet statement SHALL read `body_excerpt` only inside `ts_headline`, projecting the headline and never the excerpt.

The change SHALL NOT alter any predicate, `SET LOCAL`, overfetch, or exact-fallback condition of the vector paths. It SHALL NOT alter any rendered field of those paths, including the staleness fields (`content_hash`, `embedded_content_hash`) and `chunks_truncated`, which SHALL remain selected wherever they are rendered today.

`list_notes` and `get_recent` SHALL order by `modified_at DESC, file_path ASC`. `find_orphans` SHALL keep its existing `modified_at DESC NULLS LAST` and add `file_path ASC` after it. Rows with exactly equal `modified_at` therefore have a deterministic order.

For the vector, listing and graph paths, results SHALL be identical to the pre-change implementation (the same result set, the same order, and every non-similarity field byte-equal) with exactly three permitted exceptions, all confined to exact ties. For `keyword_search` the identity claim is narrower, because the ranking change is intended: against the pre-change implementation on the same corpus, filters and limit, (a) the set of paths tagged `match: "all"` SHALL equal the pre-change result set whenever the limit cuts neither list, (b) among rows whose `matched_in` is `body` alone the relative order SHALL equal the pre-change order up to the same tie rules, and (c) `rank` values and the position of any row whose `matched_in` includes `title` are exempt — the new ranking contract is asserted by its own requirement above, not by the oracle. Since #391 the comparison is further restricted to rows with **no tag contribution**: a row has a tag contribution for a call when any lexeme of any configured config's `websearch_to_tsquery` parse of the query, positive or negated, is present in the row's tags part (`ts_filter(content_tsvector, '{b}')`). Membership in either block, order and rank of a row with a tag contribution are exempt from (a) and (b) — the tags contract is asserted by its own requirement above — and (a) and (b) SHALL hold over the rows with no tag contribution, so a query none of whose lexemes is any returned or pre-change row's tag keeps the whole pre-#391 guarantee. The `any` block has no pre-change counterpart and is outside the oracle. The three tie exceptions:
- **membership at a tied cutoff**: when more rows share the boundary sort key (`modified_at`, `rank` or distance) than fit under the limit, which of them are returned MAY differ;
- **the representative chunk among exact distance ties**: when two chunks of one note have exactly equal distance, the kept `chunk_index` and its preview MAY differ.
- **order among exact ties**: rows whose sort key (`modified_at`, `rank` or distance) is exactly equal MAY appear in a different relative order, even when all of them fit under the limit (the previous order among such rows was unspecified; the new `file_path ASC` tie-break makes it deterministic).

No other difference is permitted.

#### Scenario: No detoasted column is selected
- **WHEN** each of the six statements and the snippet statement is compiled
- **THEN** no SELECT list SHALL contain `content_tsvector`, `body_excerpt` or `embedding` as a projected column

#### Scenario: A deferred column read raises
- **WHEN** code loads a `NoteMetadata` entity and reads `content_tsvector` or `body_excerpt`
- **THEN** the access SHALL raise rather than issue a load

#### Scenario: Results match the previous implementation
- **WHEN** the same fixed corpus and query set are run through the previous and the new implementation, covering stale, truncated, filtered, unfiltered and exact-fallback cases
- **THEN** for the vector, listing and graph paths the result sets SHALL be equal, the order SHALL be equal, and every field other than `similarity` SHALL be byte-equal, except for the three permitted tie cases
- **AND** for `keyword_search`, over the rows with no tag contribution, the `all` paths SHALL equal the pre-change set restricted to those rows and the body-only rows SHALL keep their pre-change relative order, with rank values, title-matched rows' positions and every row with a tag contribution exempt

#### Scenario: The tag exemption is exactly the rows a query lexeme reaches through the tags

- **WHEN** the oracle corpus holds a note tagged `hoa` with `hoa` nowhere else, a note tagged `budget` whose body also contains `budget forecast`, and an untagged note whose body contains `budget forecast`, and the queries `hoa`, `budget forecast`, `forecast` and `forecast -hoa` are run through the pre-change and the new implementation
- **THEN** the tagged `hoa` note SHALL be exempt for `hoa` and for `forecast -hoa`, the `budget`-tagged note SHALL be exempt for `budget forecast` and SHALL NOT be exempt for `forecast`, and every non-exempt row SHALL satisfy (a) and (b)

#### Scenario: The permitted tie differences are exercised
- **WHEN** the corpus contains more notes with an identical `modified_at` than the requested limit, two notes with an identical `modified_at` that both fit under the limit, and one note with two chunks at exactly equal distance
- **THEN** the oracle SHALL accept a different membership at that cutoff, a different relative order of the two tied notes, and a different representative chunk for that note, and SHALL reject any other difference

#### Scenario: Orphans with no modification time stay last
- **WHEN** `find_orphans` returns notes some of which have a NULL `modified_at`
- **THEN** those notes SHALL be ordered after every note with a non-NULL `modified_at`, as before

### Requirement: Vector similarity SHALL be derived from the database's distance
`semantic_search` SHALL report `similarity = 1 − distance`, where the distance is the full-precision cosine distance the database returned for that row, as `find_related` already does. It SHALL NOT fetch stored vectors to recompute similarity. Its in-service re-sort SHALL key on `(distance, file_path, chunk_index)`, so the presented order is monotone in distance and deterministic among exact ties. The reported similarity SHALL differ from the previous NumPy recomputation by no more than 1e-5.

#### Scenario: Similarity is monotone in the presented order
- **WHEN** `semantic_search` returns results
- **THEN** their `similarity` values SHALL be non-increasing in the order presented

#### Scenario: No vector is fetched
- **WHEN** `semantic_search` executes
- **THEN** no statement it issues SHALL select `note_embeddings.embedding` as an output column

### Requirement: A reduced-precision vector index SHALL ship only if it meets the recall SLO, and SHALL change only candidate generation
The `note_embeddings` HNSW index SHALL be replaced by an expression index over `(embedding::halfvec(D)) halfvec_cosine_ops`, where *D* is the configured `EMBEDDING_DIMENSIONS`, **only if** the filtered-recall benchmark meets its SLO with the replacement in place, and SHALL otherwise be left as it is. The benchmark's exact baseline SHALL remain a full-precision `vector` sequential scan. The SLO is set-recall ≥ 0.9 on each of three index rebuilds, for every filter shape and for `find_related`. If the SLO is not met, the index SHALL NOT ship and the measured recall SHALL be recorded in the architecture note.

If the index ships:
- It SHALL be built only when *D* ≤ 2000. That is the condition under which an HNSW index exists today, so no deployment changes from an exact to an approximate scan.
- `semantic_search` and `find_related` SHALL order their index scan by the exact index expression.
- They SHALL also select the full-precision distance, re-sort the fetched candidates by it before per-note dedupe, and report similarity from it. So the reduced precision affects which candidates are fetched and nothing else.
- The pre-warm probe SHALL order by the same expression, so that it warms the index the search uses.
- The reset-embeddings paths SHALL drop and re-create it through the same definition.
- The index name, the build condition, the build statement and the query expression SHALL be defined in one module that all of these call.

#### Scenario: The gate decides
- **WHEN** the recall benchmark runs with the expression index and a full-precision exact baseline
- **THEN** the index SHALL ship only if recall ≥ 0.9 holds on each of three rebuilds for every filter shape and for `find_related`

#### Scenario: The index is used
- **WHEN** the index has shipped and a filtered `semantic_search` or `find_related` statement, or the pre-warm probe, is explained
- **THEN** the plan SHALL use the expression index

#### Scenario: Ranking among candidates is full precision
- **WHEN** two fetched candidates are ordered differently by the reduced-precision and the full-precision distance
- **THEN** the presented order SHALL follow the full-precision distance

#### Scenario: Above the HNSW dimension limit nothing changes
- **WHEN** `EMBEDDING_DIMENSIONS` is greater than 2000
- **THEN** no vector index SHALL be built, the queries SHALL use the plain `vector` distance, and the pre-warm probe SHALL be skipped as before

### Requirement: The keyword vector weights the title and aliases above the body, and ranking uses an explicit weight array

`notes_metadata.content_tsvector` SHALL be built, for every configured FTS config in sorted order (the order the stored keyword fingerprint lists them, so migration 031 and every writer build the same bytes whatever order `FTS_CONFIGS` names them in), as the title parts for every config, then the aliases parts for every config, then the tags parts for every config, then the content parts for every config, concatenated with `||`: each title part `setweight(to_tsvector(cfg, title), 'A')`, each aliases part `setweight(to_tsvector(cfg, aliases), 'A')`, each tags part `setweight(to_tsvector(cfg, tags), 'B')`, and each content part the pre-change `to_tsvector(cfg, content)` (implicit weight `D`, byte-identical fragment). `title` SHALL be the value `notes_metadata.title` holds for the row — the shared title rule (sanitised frontmatter `title`, else the filename stem, bounded to the column width) — `aliases` SHALL be the text the shared `note_aliases` rule derives from the note's frontmatter, and `tags` SHALL be the text the shared `tags_text` rule derives from the note's extracted tags (see `index-integrity`). No writer SHALL emit weight `C`. One builder in `src/services/fts.py` SHALL produce the fragment for every writer, every writer under `src/` SHALL pass it the tags bind, and no writer SHALL compose the expression by hand; only migration 031 SHALL call the builder with no tags part, so that it keeps building the head it always built.

`full_text_search` SHALL rank with `ts_rank_cd(RANK_WEIGHTS, content_tsvector, tsquery)` where `RANK_WEIGHTS` is the `RANK_WEIGHTS` setting — default `{0.02, 0.2, 0.4, 1.0}` (`D, C, B, A`), four finite numbers each in `[1e-6, 1]`, bound as `float4[]` and never interpolated — so that at the default one cover at weight `A` outweighs fifty covers of the same shape at weight `D`, one cover at weight `B` (a tag) ranks below one at `A` and above one at `D`, and SHALL use the same array in every ranking statement it issues. The relative order of two rows that match only in their bodies SHALL be the same as under PostgreSQL's default array.

Accepted limitation: at the default array one tag cover equals twenty body covers of the same shape, so a note repeating a word more than twenty times in its body can outrank a note only tagged with it.

#### Scenario: A note is found and ranked first by its own title

- **WHEN** a note is titled `Quarterly Review` and its body does not contain that phrase, a second note of about 3,000 words contains the adjacent phrase `quarterly review` thirty times in its body and is not titled with it, both are indexed, and `keyword_search("quarterly review")` runs
- **THEN** the titled note SHALL be the first result and the second note SHALL be present below it

#### Scenario: An alias finds the note

- **WHEN** a note's frontmatter declares `aliases: [Q3 retro, retrospective]` and no note body contains `retrospective`
- **THEN** `keyword_search("retrospective")` SHALL return that note, ranked by an `A` cover

#### Scenario: A word a note carries only as a tag finds it

- **WHEN** a note's frontmatter declares `tags: [hoa]`, neither its title, its aliases nor its body contains `hoa`, and `keyword_search("hoa")` runs
- **THEN** the note SHALL be returned tagged `match: "all"` with `matched_in: tags`

#### Scenario: A tag hit ranks between a title hit and a body hit

- **WHEN** one note is titled `HOA`, a second carries the tag `hoa` and has no `hoa` in its title, aliases or body, a third contains `hoa` three times in its body only, and `keyword_search("hoa")` runs
- **THEN** the titled note SHALL be first, the tagged note second and the body note third

#### Scenario: A query split between the tags and the body finds the tagged note

- **WHEN** a note carries the tag `HOA`, its body contains `the siding complaint went in` and does not contain `hoa`, and `keyword_search("HOA siding complaint")` runs
- **THEN** the note SHALL be returned tagged `match: "all"` with `matched_in: tags+body`

#### Scenario: A nested tag is found by its full form and by each segment

- **WHEN** a note's frontmatter declares `tags: [ctx/phone]` and its title, aliases and body contain no occurrence of `ctx` or `phone`
- **THEN** `keyword_search("phone")`, `keyword_search("ctx")` and `keyword_search("ctx/phone")` SHALL each return it with `matched_in: tags`
- **AND** when the same tag is only inline (`#ctx/phone` in the body), `phone` and `ctx` SHALL return it with `matched_in: tags` alone, while `ctx/phone` SHALL return it with `matched_in: tags,body`, because the inline tag's text is body text that PostgreSQL parses as the one token `ctx/phone`

#### Scenario: Body-only order is unchanged by the weight array

- **WHEN** two notes match a query only in their bodies and are ranked under `RANK_WEIGHTS` and under PostgreSQL's default array over the same vectors
- **THEN** their relative order SHALL be the same under both

#### Scenario: Every writer produces the same shape

- **WHEN** the incremental pass, the full rebuild and migration 035's re-head (over a row whose vector carries only weight-`D` lexemes) each write a vector for the same title, aliases, tags and body, under `FTS_CONFIGS=english` and under `FTS_CONFIGS=english,simple`
- **THEN** the three stored vectors SHALL be byte-equal

#### Scenario: An untagged note's vector is unchanged by the tags part

- **WHEN** a note has no tags, or only tags that produce no lexeme under any configured config
- **THEN** the vector the builder writes SHALL be byte-equal to `A(title) || A(aliases) || D(body)` for the same title, aliases and body

#### Scenario: Every builder call in the source passes the tags bind

- **WHEN** the calls of the head, full and re-head builders under `src/` and `alembic/` are inspected
- **THEN** every call under `src/` SHALL pass a tags bind, and the only call passing none SHALL be in `alembic/versions/031_*.py`, whose fragment SHALL be byte-identical to its pre-change text

### Requirement: Keyword search runs the conjunction first and fills the page from a disjunction derived from PostgreSQL's parse

`full_text_search` SHALL first run the conjunctive statement — `websearch_to_tsquery` per configured config, OR'd across configs, exactly as before — and SHALL return its rows tagged `match: "all"`. When that statement returns fewer rows than `limit`, the service SHALL obtain the text form of **each** config's `websearch_to_tsquery` parse from PostgreSQL and SHALL parse that text, never the caller's string: a parse that contains no `|` and no parenthesis and splits on `&` into units each of which is a single negated lexeme or a phrase chain of lexemes joined by `<->`/`<N>` is a plain conjunction. A config is *eligible* when its parse is a plain conjunction with at least two positive units. The fallback tsquery SHALL be built **per config** and OR'd across configs as the first statement's is: an eligible config contributes its positive units OR'd with every negated unit retained as an AND NOT, built from the verbatim lexeme tokens and cast with the `tsquery` input function (no re-stemming); an ineligible config contributes its own unchanged `websearch_to_tsquery` expression — so the combined fallback is implied by the combined conjunctive tsquery for every vector, by construction. When at least one config is eligible the service SHALL run the second statement: it SHALL exclude every row matching the conjunctive tsquery, SHALL apply the identical `folder`, `tags`, `frontmatter` and owner predicates, SHALL order by `rank DESC, file_path ASC` under the same `RANK_WEIGHTS`, SHALL be limited to `limit` minus the conjunctive row count, and its rows SHALL be appended after every conjunctive row tagged `match: "any"`. When no config is eligible there SHALL be no second statement. Every statement of one call SHALL run in the one transaction that issued `SET LOCAL random_page_cost = 1.1`, and that transaction SHALL be `REPEATABLE READ, READ ONLY`, set before its first snapshot-taking statement, so every statement of the call reads one snapshot and a concurrent index commit cannot drop a row from both blocks. The service SHALL record `fallback` (`ran`, `skipped` or `not_needed`) and `any_rows` in the call's telemetry.

#### Scenario: A three-term query that over-specifies still finds the target

- **WHEN** a note contains the terms `alpha` and `beta` but not `gamma`, no note contains all three, and `keyword_search("alpha beta gamma", limit=10)` runs
- **THEN** the result SHALL contain that note tagged `match: "any"`, and no row SHALL be tagged `match: "all"`

#### Scenario: Conjunctive rows come first and are never displaced

- **WHEN** three notes match all terms of a query, seven more match some, and `limit` is 5
- **THEN** the result SHALL be exactly the three `all` rows in `rank DESC, file_path ASC` order followed by the two best `any` rows in that order, and no `any` row SHALL precede an `all` row

#### Scenario: A full conjunctive page runs no second statement

- **WHEN** the conjunctive statement returns `limit` rows
- **THEN** no disjunctive statement SHALL be issued and telemetry SHALL record `fallback: "not_needed"`

#### Scenario: The user's operators decide the shape

- **WHEN** the query contains `OR`, a negated quoted phrase (`-"a b"`), or exactly one positive term
- **THEN** no disjunctive statement SHALL be issued and telemetry SHALL record `fallback: "skipped"`

#### Scenario: A negation is honoured in the fallback and a phrase stays a phrase

- **WHEN** the query is `alpha "beta gamma" -delta`, no note matches the conjunction, one note contains `alpha` and `delta`, and one contains the adjacent phrase `beta gamma`
- **THEN** the `any` block SHALL contain the phrase note and SHALL NOT contain the `delta` note

#### Scenario: The disjunction is a superset of the conjunction

- **WHEN** a generated corpus of queries (plain words, phrases, negations, stop words, quotes and backslashes in lexemes) is run against a seeded database under `FTS_CONFIGS=english` and under `FTS_CONFIGS=english,simple`
- **THEN** for every query every row matching the combined conjunctive tsquery SHALL match the combined fallback tsquery, and every eligible config's derived text SHALL cast as a `tsquery` without error

#### Scenario: Configs whose parses differ are combined per config

- **WHEN** `FTS_CONFIGS=english,simple`, a note's body is `runs fast`, and `keyword_search("the running", limit=10)` runs — `english` parses to the single unit `'run'` (ineligible) and `simple` to `'the' & 'running'` (eligible)
- **THEN** the note SHALL be returned tagged `match: "all"` (it matches `english`'s conjunctive parse), the fallback SHALL retain `english`'s unchanged query beside `simple`'s `('the' | 'running')`, and the row SHALL render `terms: 1/1`

#### Scenario: No caller text reaches a tsquery constructor

- **WHEN** the statements of one call are compiled
- **THEN** the caller's query string SHALL appear only as the bind of `websearch_to_tsquery`, and every disjunctive tsquery SHALL be built from PostgreSQL's parse output

#### Scenario: The disjunctive statement uses the keyword index

- **WHEN** `EXPLAIN (ANALYZE, BUFFERS)` is run for the disjunctive statement over the seeded keyword corpus with the planner setting applied
- **THEN** the plan SHALL use the tsvector GIN index (bitmap or index scan)

### Requirement: Each keyword result carries a snippet computed over the page only

Each of the first `KEYWORD_SNIPPET_ROWS` (default 20, a setting in `0..100`) rows of a `keyword_search` page SHALL carry `snippet`: `ts_headline(cfg, body_excerpt, q, opts)` where `body_excerpt` is the stored excerpt (see `index-integrity`), `q` is the tsquery that matched the row's block, and `opts` is `MaxFragments=2, MaxWords=18, MinWords=6` with U+0002/U+0003 as `StartSel`/`StopSel` rendered as `«`/`»`; rows beyond that count SHALL carry neither `snippet` nor `head`, and the header SHALL say so. The headline SHALL be computed by a statement whose predicate is those rows' ids (`id = ANY(:ids)`), never over the candidate set, so its cost is a function of `KEYWORD_SNIPPET_ROWS`, `KEYWORD_EXCERPT_CHARS` and the config count alone — never of `limit` — and that statement's time SHALL be recorded as `snippet_ms`. The integration suite SHALL measure the worst case on the throwaway PostgreSQL 16 container — twenty distinct 20,000-character excerpts with repeated matches, `FTS_CONFIGS=english,simple`, head-fallback rows included — and SHALL assert the snippet statement completes in under 300 ms; the measurement is recorded in the change's design. The same statement SHALL project `matched_in` from six flags `ts_filter(content_tsvector, W) @@ q` for `W` in `{a}` (title or aliases), `{b}` (tags), `{d}` (body), `{a,b}`, `{a,d}` and `{b,d}`: `matched_in` SHALL be every single part whose flag is true, in the order `title`, `tags`, `body`, rendered comma-joined; when no single part matches (the terms are split between parts), the first true pair in the order `title+tags`, `title+body`, `tags+body`; and when no pair matches, `title+tags+body` — so the rendered value is always one of `title`, `tags`, `body`, `title,tags`, `title,body`, `tags,body`, `title,tags,body`, `title+tags`, `title+body`, `tags+body`, `title+tags+body` — and the term count, rendered `terms: k/n` (the service field is `matched_terms`) — for each config whose parse is a plain conjunction of at most 32 positive units, the count of that config's positive units present in the row's vector over that count, rendered as the single pair with the greatest ratio (ties: the greater denominator, then config order). A headline SHALL be shown as `snippet` only when the row's body part matched `q`; a row whose body part did not match, or whose headline contains no highlight, SHALL instead render the excerpt's leading words labelled `head:`, taken from `ts_headline(fts_configs[0], body_excerpt, q, 'MaxFragments=0, MinWords=18, MaxWords=24')` with the U+0002/U+0003 sentinels as `StartSel`/`StopSel` and removed from the result, evaluated by a second statement over only such rows; a row whose stored excerpt is the empty string (a note with no body) SHALL render no second line at all — never a fabricated `head:` — SHALL keep its `matched_in`, and SHALL NOT be counted as a row without an excerpt, so the service SHALL distinguish a computed empty headline from a headline not computed (a row beyond `KEYWORD_SNIPPET_ROWS`) and from a NULL excerpt, and the renderer SHALL NOT decide between them by truthiness; a row whose excerpt is NULL SHALL render neither, and the header SHALL carry the count of such rows always, including zero, with a footer naming the rebuild command when the count is positive. The server SHALL NOT cut or mark inside a snippet, SHALL render each `snippet:` and `head:` on exactly one line (every run of whitespace, line breaks included, rendered as one space), and the header SHALL carry the `all` and `any` counts always, including zero.

Accepted limitations: a literal `«` or `»` in a note collides with the rendered highlight markers — the stored markers are the unforgeable U+0002/U+0003, stripped from excerpts at index time, and the visible rendering is a convenience the agent verifies with `read_note`; the 300 ms gate is measured at the defaults, and the cost scales roughly linearly with `KEYWORD_EXCERPT_CHARS` and `KEYWORD_SNIPPET_ROWS`, whose maxima are about 25× the measured work.

#### Scenario: A body match is shown highlighted

- **WHEN** a note's body contains `the budget forecast for Q3` within the stored excerpt and `keyword_search("budget forecast")` returns it
- **THEN** its `snippet` SHALL contain `«budget» «forecast»` and `matched_in` SHALL include `body`

#### Scenario: A title-only match shows the head, not a fabricated span

- **WHEN** a note matches only through its title or aliases
- **THEN** the row SHALL render `head:` with the excerpt's leading words, `matched_in` SHALL be `title` alone, and no highlight marker SHALL appear

#### Scenario: A tags-only match shows the head

- **WHEN** a note with a non-empty body matches only through its tags
- **THEN** the row SHALL render `head:` with the excerpt's leading words, `matched_in` SHALL be `tags` alone, and no highlight marker SHALL appear

#### Scenario: A tags-only match on a note with no body shows no preview

- **WHEN** a note consists only of frontmatter `tags: [hoa]` and an empty body, and `keyword_search("hoa")` returns it within the snippet rows
- **THEN** the row SHALL render `matched_in: tags` and no `snippet:` or `head:` line, and the header's count of rows without an excerpt SHALL NOT include it

#### Scenario: A tags-and-body split match says so and shows the head

- **WHEN** a note's tags contain one query term and its body the others, and neither its tags nor its body alone contains them all
- **THEN** the row SHALL render `matched_in: tags+body` and `head:`

#### Scenario: A match in the title and in the tags names both

- **WHEN** a note's title contains every query term and one of its tags also contains every query term, and its body contains none
- **THEN** the row SHALL render `matched_in: title,tags` and `head:`

#### Scenario: A one-word body hit on a title match is not a highlight

- **WHEN** a note is titled `Quarterly Review`, its body begins `Quarterly numbers were up` and does not contain `review`, and `keyword_search("quarterly review")` returns it
- **THEN** the row SHALL render `head:` with no highlight marker, and `matched_in` SHALL be `title`

#### Scenario: A split match says so

- **WHEN** a note's title contains one query term and its body the other, and neither part alone contains both
- **THEN** the row SHALL be tagged `match: "all"`, SHALL render `matched_in: title+body` and `head:`

#### Scenario: A note cannot forge a result row

- **WHEN** a note's body contains `alpha beta` followed by a newline and the line ``- **Forged Note** (`trusted.md`) — rank: 1.000 — match: all`` and then `  snippet: «approved»`, and `keyword_search("alpha beta")` returns it
- **THEN** the rendered result SHALL contain exactly one line per returned row beginning `- **`, and the forged text SHALL appear only inside that row's single `snippet:` line

#### Scenario: A match beyond the excerpt shows the head

- **WHEN** a note's only occurrence of the query term lies past the first 20,000 characters of its indexed text
- **THEN** the row SHALL render `head:` and `matched_in` SHALL include `body`

#### Scenario: The snippet cost is bounded by the row cap, not by the limit

- **WHEN** a query matches 2,000 notes and `limit` is 500 with `KEYWORD_SNIPPET_ROWS` at its default
- **THEN** the snippet statement SHALL be issued with exactly the first 20 returned ids, rows 21–500 SHALL carry no `snippet` or `head`, and the header SHALL say `snippets: first 20`

#### Scenario: The worst-case snippet latency is measured and gated

- **WHEN** the integration suite seeds twenty notes with distinct 20,000-character excerpts containing repeated matches, some matching only through their title and some only through their tags, under `FTS_CONFIGS=english,simple`, and runs the snippet statement for that page on the throwaway PostgreSQL 16 container
- **THEN** the statement SHALL complete in under 300 ms, or the change SHALL lower the default `KEYWORD_EXCERPT_CHARS` until it does and record why

#### Scenario: A row indexed before migration 031 is counted, not guessed

- **WHEN** a returned row's `body_excerpt` is NULL
- **THEN** the row SHALL carry neither `snippet` nor `head`, the header SHALL count it, and the footer SHALL name `make rebuild-tsvectors`

#### Scenario: Matched terms are counted on the page only

- **WHEN** the query `alpha beta gamma` returns an `any` row containing `alpha` and `gamma`
- **THEN** that row SHALL render `terms: 2/3`

### Requirement: The keyword_search docstring states the conjunction and the result fields, as a peer of semantic_search

The `keyword_search` docstring SHALL state that bare terms are ANDed and that partial matches follow the full matches tagged `match: any`; SHALL show the `"exact phrase"`, `a OR b` and `-excluded` forms; SHALL state that a note's tags are indexed below its title and aliases and above its body; SHALL describe `snippet`/`head`, `matched_in` (naming its parts `title`, `tags` and `body`) and `terms`; SHALL stay within the 4,000-character description bound; and SHALL keep the cross-reference to `semantic_search` in the same shape `semantic_search` uses for it, ranking neither tool above the other.

#### Scenario: The docstring names the semantics

- **WHEN** the registered tool description for `keyword_search` is read
- **THEN** it SHALL contain the AND default, the three operator forms, the five rendered result-field names `match`, `matched_in`, `terms`, `snippet:` and `head:`, and the statement that tags are indexed, and it SHALL be at most 4,000 characters

#### Scenario: The two tools remain symmetric peers

- **WHEN** the registered descriptions of `keyword_search` and `semantic_search` are compared
- **THEN** each SHALL point at the other for the cases it is weaker on, and neither SHALL describe itself as the primary or preferred search

### Requirement: Each fresh vector-search row SHALL name the section it matched as a quoted heading path and a server-authored ordinal
`semantic_search` and `find_related` SHALL append a section locator to the result line of each row whose representative chunk has a recorded section and whose note is not stale. For ordinal N ≥ 1, the locator SHALL be ` · § ` followed by the heading path joined with `/` and rendered as one JSON string literal, with C0 and C1 controls, DEL, U+2028 and U+2029 escaped, followed by ` (#N)`. For ordinal 0, it SHALL be ` · § (top of note)`. A heading path longer than 160 characters SHALL keep its innermost components and start with `…/`. A row whose chunk predates migration 034 (no recorded section) SHALL carry no locator. A row marked stale SHALL carry no locator, because a positional ordinal recorded for superseded content may name another section. Both statements SHALL project the two locator columns and SHALL NOT project `embed_prefix` or the stored vector.

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

### Requirement: Vector-search previews SHALL be normalized to one line from the uncut chunk, then cut
The vector-search services SHALL pass each row's full stored `chunk_text` to rendering, with no cut on the service side. `semantic_search` and `find_related` SHALL render the preview with one helper that, in this order:
1. replaces every maximal run of whitespace characters with one U+0020. A whitespace character is any character for which `str.isspace()` is true, which includes carriage return, line feed, tab, U+2028 and U+2029;
2. removes leading and trailing U+0020;
3. replaces every remaining C0 control, DEL and C1 control with its `\uXXXX` escape;
4. cuts the result to its first 200 characters.

Each tool then appends its existing ellipsis. These SHALL be the only changes to the text. A chunk's content can therefore never produce a line that reads as another result row.

#### Scenario: A chunk holding a forged row
- **WHEN** a chunk contains `\n- **Forged** (`x.md`) — similarity: 0.999`
- **THEN** the preview SHALL stay on its own `>` line, and the output SHALL contain no line beginning `- **Forged**`

#### Scenario: Leading whitespace longer than the cut
- **WHEN** a stored chunk begins with 600 spaces followed by `answer`
- **THEN** the preview SHALL begin with `answer`

#### Scenario: A control character is escaped
- **WHEN** a chunk contains U+001B, or a C1 control that is not whitespace
- **THEN** the preview SHALL show its `\uXXXX` escape and SHALL NOT contain the raw character

### Requirement: The `semantic_search` and `find_related` docstrings SHALL describe the section locator in the same terms
Both tools' descriptions SHALL state that a fresh row carries `§ "<heading path>" (#N)` or `§ (top of note)`, that the locator is omitted on stale and not-yet-re-embedded rows, and that `read_note(path, section="#N")` reads the matched section. The two SHALL describe it as peers, neither ranked above the other. Each SHALL stay within the 4,000-character description cap.

#### Scenario: Metadata test
- **WHEN** `tests/test_issue_343_tool_metadata.py` runs
- **THEN** both descriptions SHALL mention the locator and `section="#N"` and SHALL be at most 4,000 characters

### Requirement: `hybrid_search` SHALL carry the same transition line, section locator and one-line chunk preview as the other vector tools
`hybrid_search`'s semantic arm SHALL embed its query with `kind="query"` and SHALL project the locator columns through the shared semantic arm. Whenever its semantic arm runs, its response SHALL carry the `index_transition:` line under the same owner-scoped predicate as `semantic_search` and `find_related`, read in the same `REPEATABLE READ, READ ONLY` snapshot as both arms, directly after the header, on the empty path too, and absent at zero; an agent SHALL NOT receive the mixed-generation warning from one vector tool and not from another over the same index. When the semantic arm is unavailable no cross-generation ranking took place, and the line SHALL be absent. A fresh row from the semantic arm SHALL carry the locator as a server field after the separator (`§ "<heading path>" (#N)` or `§ (top of note)`), withheld on stale and pre-034 rows; an abbreviated row SHALL drop it and say `section omitted`. A chunk preview SHALL be the `_preview_line` normalisation of the uncut chunk. The response bound SHALL hold at the 1,000-character floor with the line and a locator present: the line appears only when both arms ran, so the keyword-only footer is absent, and the abbreviation continues into the omitted forms when needed.

#### Scenario: The same warning on all three tools
- **WHEN** the caller's scope has notes holding vectors of another generation and `hybrid_search` runs with both arms
- **THEN** its response SHALL carry the `index_transition:` line directly after the header, with the count `semantic_search` would report

#### Scenario: Keyword-only carries no transition line
- **WHEN** the semantic arm is unavailable
- **THEN** the response SHALL carry no `index_transition:` line

#### Scenario: The floor holds with the line and a locator
- **WHEN** the response cap is 1,000 characters and the worst-case header, row, transition line and locator are present
- **THEN** the response SHALL be at most 1,000 characters

### Requirement: `semantic_search` and `hybrid_search` SHALL state a `confidence` token computed by one rule from all-terms keyword evidence and the best similarity of the call's candidate set
Every **page** of `semantic_search` and `hybrid_search` — every response that carries a search header or zero-result line: rows, the zero-result line, `rerank=true` and `rerank=false`, reranked or falling back, and `hybrid_search`'s keyword-only path — SHALL carry exactly one header token `confidence: ok` or `confidence: low`. A refusal is not a page: `semantic_search`'s provider-input rejection (`_semantic_provider_refusal`, `provider_input_rejected`) and every pre-body refusal SHALL carry no `confidence` token and no hint, and their usage rows SHALL record neither `confidence` nor `top_similarity`. (`hybrid_search` has no such refusal: a provider rejection is its keyword-only page, which carries the token.)

The value SHALL be computed by one pure function shared by both tools, from three inputs: `keyword_all`, the number of `match: all` notes in the keyword arm (`match: any` rows are not evidence; `semantic_search` has no keyword arm and passes 0); `top_similarity`; and the threshold `T = SEARCH_LOW_CONFIDENCE_SIMILARITY`. The value SHALL be `low` exactly when `T > 0`, `keyword_all = 0`, and `top_similarity` is none or strictly below `T`; otherwise `ok`.

`top_similarity` SHALL be defined over the call's **actual candidate set**, with no new or changed statement: for `semantic_search`, the similarity of the first row of the ordered, deduplicated candidate list the call already fetched (the existing `max(5 × limit, 50)`-chunk overfetch), taken before reranking and before the response cap; for `hybrid_search`, the best similarity among its semantic arm's notes (the existing 50-chunk overfetch, independent of `limit`), computed by the hybrid service from those rows and carried on its result. It is none when that set is empty or the semantic arm did not run. Because `semantic_search`'s overfetch depends on `limit`, a different `limit` can change its candidate set and therefore, at the margin, its token. The `semantic_search`, `semantic_candidates`, `keyword_search` service return types SHALL be unchanged: the tool reads `top_similarity` from the first returned row. The token SHALL NOT reorder or alter any row and SHALL NOT change the candidate set. It is a header token like `arm_depth`, reserved in every page's base response budget, with no compaction to absorb it: on a capped page (`hybrid_search`, `semantic_search(rerank=true)`) a boundary row that fitted before this change MAY be omitted, and the existing response-cap footer SHALL then state the omitted count (with `more: true` on a `rerank=true` page). The `semantic_search(rerank=false)` page has no character budget, so the token never omits a row there. `keyword_search` and `find_related` SHALL NOT carry the token.

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
The setting `SEARCH_LOW_CONFIDENCE_SIMILARITY` SHALL be a float from 0.0 to 1.0 inclusive, default **0** (hard off: the token always reads `ok`), read once at startup; a value outside that range or not finite SHALL refuse startup. The token SHALL be present whatever the value; there SHALL be no setting that removes it. The setting SHALL NOT be a per-call argument and SHALL NOT change any ranking, so it SHALL NOT be part of `retrieval_fingerprint()`. The README settings table, `.env.example` and `docs/architecture/search.md` SHALL document it with its default, its range, that `ok` at 0 means "not judged", that it is model-dependent and must be re-chosen when the embedding model in the retrieval fingerprint changes, and the bge-m3 evidence so far (live, 2026-10-30: gibberish best similarity 0.36–0.49, real paraphrases 0.61–0.67), stated as a candidate value, not a default. `DEPLOYMENT.md` SHALL carry the calibration recipe: accrue post-change usage rows for at least 14 days **and** at least 200 token-bearing clicked-through pages **per tool**; run the harness's confidence tabulation (per tool, per query class) on a single-user (`USER_ID=`) export; pick the threshold at the observed noise ceiling; set it only if **both** tools' acceptance lines read PASS (false-low rate below 1 in 50 on clicked-through pages; `INSUFFICIENT_DATA` is not a pass); then re-run the tabulation after a further period. It SHALL state that only attachable output may leave `.search-eval/`. The reference deployment SHALL set a non-zero value only after both lines pass.

#### Scenario: Out of range
- **WHEN** the server starts with `SEARCH_LOW_CONFIDENCE_SIMILARITY=1.5` or `-0.1` or `nan`
- **THEN** settings validation SHALL fail and the server SHALL NOT start

#### Scenario: Documented
- **WHEN** README.md, `.env.example`, `DEPLOYMENT.md` and `docs/architecture/search.md` are searched for `SEARCH_LOW_CONFIDENCE_SIMILARITY`
- **THEN** README and `.env.example` SHALL name it with its default 0 and its range, `search.md` SHALL state that it is model-dependent and tuned by the harness, and `DEPLOYMENT.md` SHALL give the calibration recipe with its 1-in-50 acceptance line
