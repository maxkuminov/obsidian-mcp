# search-quality Specification

## Purpose
TBD - created by archiving change search-recall-and-latency. Update Purpose after archive.
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

`full_text_search` SHALL issue `SET LOCAL random_page_cost = 1.1` in the same transaction as its query (the same transaction-scoped setting the vector path uses, no global Postgres change), and SHALL order results by rank descending with `file_path` ascending as a deterministic tie-break, so result membership and order are stable across plans. Matching semantics SHALL be unchanged: for any query and filters the returned set SHALL equal the set returned without the planner setting.

#### Scenario: Setting is issued and results are unchanged

- **WHEN** `full_text_search` runs against a populated database with and without the planner setting for a matrix of rare and common terms combined with folder, tag, frontmatter, and user filters
- **THEN** the returned rows SHALL be identical in membership and order in every case

#### Scenario: Index plan on a production-sized corpus

- **WHEN** `EXPLAIN (ANALYZE, BUFFERS)` is run for a rare-term query over a seeded, analysed keyword corpus of at least 3,000 notes with realistic tsvector sizes, once with the setting applied and once with the sequential baseline forced by `SET LOCAL enable_indexscan = off; SET LOCAL enable_bitmapscan = off` (leaving `enable_seqscan` on)
- **THEN** the plan with the setting SHALL use the tsvector index (bitmap or index scan) and SHALL read fewer buffers than the forced sequential plan for the same query (asserted); for a common-term query the plans and buffer counts are recorded, not asserted

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

`semantic_search` SHALL record `embed_ms` (time to obtain the query embedding) and `db_ms` (time in the vector query, including `SET LOCAL`s and fetch); `find_related` SHALL record `db_ms` only (it performs no embedding-provider call; its source-chunk fetch is included in `db_ms`). Values SHALL be non-negative integers stored in `usage_logs.params` alongside the existing whole-call `duration_ms`. Timing SHALL be scoped to the tool call: the `_tracked` decorator SHALL initialise the timing holder at call start and clear it in `finally`, so a value can never be attributed to a different call, and early returns or exceptions SHALL leave partial phases at their measured value or absent, never stale. The service functions' return types SHALL be unchanged; timing travels on a call-scoped holder, not in the return value.

#### Scenario: Timing fields present

- **WHEN** a `semantic_search` call completes
- **THEN** its usage log row's `params` SHALL contain integer `embed_ms` and `db_ms` such that `embed_ms + db_ms ≤ duration_ms`, and a boolean `exact_fallback`

#### Scenario: No cross-call leakage

- **WHEN** a `semantic_search` call is followed in the same task by a different tracked tool call
- **THEN** the second call's usage row SHALL NOT contain `embed_ms` or `db_ms`

#### Scenario: find_related timing

- **WHEN** a `find_related` call completes (including the "not embedded yet" early return)
- **THEN** its usage row SHALL contain `db_ms` and SHALL NOT contain `embed_ms`

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
`semantic_search`, `keyword_search`, `list_notes`, `get_recent`, `find_orphans` and `get_neighborhood`'s metadata hydration SHALL each select an explicit list of columns, and that list SHALL contain neither `notes_metadata.content_tsvector` nor `note_embeddings.embedding`. `content_tsvector` SHALL be mapped as a deferred attribute that raises when read from a loaded entity, so that no whole-entity load ships it and an accidental reader fails loudly instead of lazy-loading.

The change SHALL NOT alter any predicate, `SET LOCAL`, overfetch, or exact-fallback condition. It SHALL NOT alter any rendered field, including the staleness fields (`content_hash`, `embedded_content_hash`) and `chunks_truncated`, which SHALL remain selected wherever they are rendered today.

`list_notes` and `get_recent` SHALL order by `modified_at DESC, file_path ASC`. `find_orphans` SHALL keep its existing `modified_at DESC NULLS LAST` and add `file_path ASC` after it. Rows with exactly equal `modified_at` therefore have a deterministic order.

Results SHALL be identical to the pre-change implementation (the same result set, the same order, and every non-similarity field byte-equal) with exactly three permitted exceptions, both confined to exact ties:
- **membership at a tied cutoff**: when more rows share the boundary sort key (`modified_at`, `rank` or distance) than fit under the limit, which of them are returned MAY differ;
- **the representative chunk among exact distance ties**: when two chunks of one note have exactly equal distance, the kept `chunk_index` and its preview MAY differ.
- **order among exact ties**: rows whose sort key (`modified_at`, `rank` or distance) is exactly equal MAY appear in a different relative order, even when all of them fit under the limit (the previous order among such rows was unspecified; the new `file_path ASC` tie-break makes it deterministic).

No other difference is permitted.

#### Scenario: No detoasted column is selected
- **WHEN** each of the six statements is compiled
- **THEN** its SELECT list SHALL NOT contain `content_tsvector` or `embedding`

#### Scenario: A deferred column read raises
- **WHEN** code loads a `NoteMetadata` entity and reads `content_tsvector`
- **THEN** the access SHALL raise rather than issue a load

#### Scenario: Results match the previous implementation
- **WHEN** the same fixed corpus and query set are run through the previous and the new implementation, covering stale, truncated, filtered, unfiltered and exact-fallback cases
- **THEN** the result sets SHALL be equal, the order SHALL be equal, and every field other than `similarity` SHALL be byte-equal, except for the three permitted tie cases

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

