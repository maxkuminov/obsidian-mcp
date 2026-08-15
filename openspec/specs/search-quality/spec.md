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

