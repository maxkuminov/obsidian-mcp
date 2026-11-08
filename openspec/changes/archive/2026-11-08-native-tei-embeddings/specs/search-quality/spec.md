## MODIFIED Requirements

### Requirement: Search caches are pre-warmed on the indexer tick

After each periodic indexer pass, while still holding the indexer pass lock, the server SHALL re-check the paused flag and then issue one short embedding request (only when the embedding provider is `ollama`, whose model can be unloaded between calls; not for `openai`, and not for `tei`, whose server holds its one model loaded for its whole lifetime) and one HNSW probe query using a deterministic non-zero unit vector of `EMBEDDING_DIMENSIONS`, so the embedding model stays resident and the index's hot pages stay cached between sparse searches. The whole pre-warm SHALL be bounded by a single wall-clock timeout of 15 seconds (`asyncio.wait_for`); on timeout or any ordinary exception it SHALL log at WARNING and return without raising, without changing the indexer's failure counter. `asyncio.CancelledError` SHALL be re-raised immediately so lifespan shutdown cancels the indexer task as before. The next tick begins `INDEX_INTERVAL_SECONDS` after the pre-warm completes or times out, so a tick is delayed by at most 15 seconds beyond the index pass. Because the pre-warm runs under the pass lock, the panel's reset-embeddings and legacy re-embed actions SHALL also acquire the pass lock before their destructive statements — setting the pause flag first, ending the request's own database transaction before waiting (so waiters never pin a pool connection), then acquiring a connection only after the lock is held — so a reset can never run concurrently with a probe or an index pass. The wait is bounded only by the current pass, not by the pre-warm timeout. The legacy re-embed action SHALL, in the same locked transaction that deletes `note_embeddings`, set `notes_metadata.embedded_content_hash = NULL`, so the subsequent reindex actually re-embeds (today it deletes the vectors and leaves the hashes, and nothing is re-embedded). The HNSW probe SHALL run only when an HNSW index exists on `note_embeddings.embedding` (deployments with `EMBEDDING_DIMENSIONS > 2000` have none); the embedding pre-warm is independent of that.

#### Scenario: Pre-warm runs after a tick

- **WHEN** a periodic indexer tick completes (with or without changes) and the indexer is not paused
- **THEN** one embedding request (`ollama` only) and one HNSW probe SHALL be issued under the pass lock and a log line SHALL record their timings

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

#### Scenario: TEI is not pre-warmed
- **WHEN** a periodic indexer tick completes with `EMBEDDING_PROVIDER=tei`
- **THEN** no embedding request SHALL be issued by the pre-warm, and the HNSW probe SHALL still run
