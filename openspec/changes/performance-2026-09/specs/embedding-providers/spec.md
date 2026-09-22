## MODIFIED Requirements

### Requirement: Provider-native batch embedding

The system SHALL embed multiple chunks per indexer pass using the provider's batch mechanism. For OpenAI, this SHALL be a batched request that sends up to 96 inputs in one HTTP call. For Ollama, this SHALL be a sequence of `/api/embed` requests, each carrying an `input` array of at most `OLLAMA_EMBED_BATCH_SIZE` consecutive chunks. The batch size SHALL default to 16 and SHALL be bounded to 1–256. Each Ollama request SHALL be bounded by a 30 s timeout, and there SHALL be no aggregate deadline across requests. Each response SHALL carry exactly as many vectors as the request carried inputs, in input order, or the batch SHALL fail. Setting the batch size to 1 reproduces the pre-batching request shape.

#### Scenario: OpenAI batch request
- **WHEN** the indexer calls `get_embeddings_batch` with N chunks (N ≤ 96) and provider is `openai`
- **THEN** the system issues a single POST to `/v1/embeddings` with all N inputs and returns the N resulting vectors in input order

#### Scenario: Large batch is split
- **WHEN** `get_embeddings_batch` is called with more than 96 chunks and provider is `openai`
- **THEN** the system splits the batch into sub-batches of at most 96 inputs each, calls the API sequentially, and concatenates results in input order

#### Scenario: Ollama fixed-size batches
- **WHEN** provider is `ollama`, `OLLAMA_EMBED_BATCH_SIZE` is 16, and `get_embeddings_batch` is called with 40 chunks
- **THEN** the system issues three sequential `/api/embed` requests carrying 16, 16 and 8 inputs, and returns the 40 vectors in input order

#### Scenario: A short Ollama response fails the batch
- **WHEN** an Ollama request carrying 16 inputs returns 15 vectors
- **THEN** the batch SHALL fail and no vector from it SHALL be used

#### Scenario: A hung Ollama request fails at the per-request bound
- **WHEN** an Ollama request does not answer
- **THEN** it SHALL fail after 30 s, with no longer deadline over the whole batch

## ADDED Requirements

### Requirement: Each provider SHALL use one pooled HTTP client built through the embedding transport factory
Each embedding provider SHALL send its requests through one shared, connection-pooling `httpx.AsyncClient` per event loop, instead of creating a client per call. That covers indexer batches, single embeddings and `semantic_search` query embeddings. The shared client SHALL be obtained only by calling the embedding transport factory (`embedding_http_client`), so it has the factory's properties:
- environment proxy and trust-store variables ignored;
- redirects not followed;
- the configured CA context.

No code path SHALL construct an `httpx` client for the embedding endpoint by any other means. The client SHALL be created lazily. It SHALL be rebuilt if the running event loop differs from the one it was created on. It SHALL be closed during application shutdown, after the indexer task has been cancelled. Per-request timeouts SHALL be passed on each request: 30 s for Ollama and 60 s for OpenAI.

#### Scenario: Connections are reused
- **WHEN** the indexer embeds several notes in one pass
- **THEN** one client instance SHALL serve every request of that pass

#### Scenario: The client comes from the factory
- **WHEN** the shared client is created
- **THEN** it SHALL have been returned by `embedding_http_client`, and the transport sweep test SHALL find no other client construction aimed at the embedding endpoint

#### Scenario: The client is closed at shutdown
- **WHEN** the application shuts down
- **THEN** the shared client SHALL be closed after the indexer task has stopped
