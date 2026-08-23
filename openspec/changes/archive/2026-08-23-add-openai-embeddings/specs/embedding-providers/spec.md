## ADDED Requirements

### Requirement: Configurable embedding provider selection

The system SHALL support selecting the embedding backend via the `EMBEDDING_PROVIDER` environment variable, with valid values `ollama` and `openai`. The default value SHALL be `ollama` so existing deployments continue working without configuration changes.

#### Scenario: Default provider is Ollama
- **WHEN** the server starts with no `EMBEDDING_PROVIDER` set
- **THEN** the active provider is `ollama` and embeddings are generated against the configured Ollama URL using the configured model

#### Scenario: OpenAI provider selected
- **WHEN** `EMBEDDING_PROVIDER=openai` and `OPENAI_API_KEY` is set
- **THEN** all calls to `get_embedding` and `get_embeddings_batch` route to OpenAI's `/v1/embeddings` endpoint using the configured `OPENAI_EMBEDDING_MODEL`

#### Scenario: Invalid provider value
- **WHEN** `EMBEDDING_PROVIDER` is set to a value other than `ollama` or `openai`
- **THEN** the server fails to start with a configuration error naming the invalid value and listing valid options

### Requirement: OpenAI provider configuration validation

When `EMBEDDING_PROVIDER=openai`, the system SHALL validate at startup that `OPENAI_API_KEY` is set and non-empty. The system SHALL also accept an optional `OPENAI_BASE_URL` to support Azure OpenAI and OpenAI-compatible proxies, defaulting to `https://api.openai.com/v1`.

#### Scenario: Missing API key
- **WHEN** `EMBEDDING_PROVIDER=openai` and `OPENAI_API_KEY` is unset or empty
- **THEN** the server fails to start with an error stating the API key is required

#### Scenario: Custom base URL
- **WHEN** `OPENAI_BASE_URL=https://my-azure.openai.azure.com/openai/deployments/my-deploy` is set
- **THEN** embedding requests POST to that base URL instead of the public OpenAI endpoint

### Requirement: Provider-native batch embedding

The system SHALL embed multiple chunks per indexer pass using the provider's most efficient mechanism. For Ollama this MAY be a serial loop; for OpenAI this SHALL be a batched request that sends multiple inputs in one HTTP call.

#### Scenario: OpenAI batch request
- **WHEN** the indexer calls `get_embeddings_batch` with N chunks (N ≤ 96) and provider is `openai`
- **THEN** the system issues a single POST to `/v1/embeddings` with all N inputs and returns the N resulting vectors in input order

#### Scenario: Large batch is split
- **WHEN** `get_embeddings_batch` is called with more than 96 chunks and provider is `openai`
- **THEN** the system splits the batch into sub-batches of at most 96 inputs each, calls the API sequentially, and concatenates results in input order

#### Scenario: Ollama serial behavior preserved
- **WHEN** provider is `ollama` and `get_embeddings_batch` is called with N chunks
- **THEN** the system issues N sequential single-input requests, matching pre-change behavior

### Requirement: Retry on transient OpenAI errors

When the OpenAI provider encounters an HTTP 429 or 5xx response, the system SHALL retry the request with exponential backoff up to 3 attempts before propagating the error.

#### Scenario: Rate limit retry
- **WHEN** OpenAI returns 429 on the first attempt and 200 on retry
- **THEN** the embedding call returns the successful result and logs the retry at WARNING level

#### Scenario: Persistent failure
- **WHEN** OpenAI returns 5xx on three consecutive attempts
- **THEN** the call raises an error that the indexer logs and skips, leaving `embedded_content_hash` unchanged so the chunk is retried on the next indexer pass

### Requirement: Configurable embedding dimensions

The system SHALL store note embeddings in a pgvector column whose width matches `EMBEDDING_DIMENSIONS`. The default value SHALL be 1024 to preserve existing 1024-dimensional bge-m3 embeddings. When the OpenAI provider is selected, the system SHALL pass the configured dimension count to the API via the `dimensions` request parameter (supported by `text-embedding-3-*` models).

#### Scenario: Default dimension preserved
- **WHEN** `EMBEDDING_DIMENSIONS` is unset and the system runs migrations
- **THEN** `note_embeddings.embedding` is created as `Vector(1024)`

#### Scenario: OpenAI dimension override
- **WHEN** `EMBEDDING_PROVIDER=openai`, `EMBEDDING_DIMENSIONS=512`, and the column has been created with width 512
- **THEN** OpenAI requests include `"dimensions": 512` and stored vectors have length 512

### Requirement: Dimension-mismatch startup check

The system SHALL compare the configured `EMBEDDING_DIMENSIONS` against the actual width of the `note_embeddings.embedding` column at startup. On mismatch, the system SHALL refuse to start with an error explaining how to run the reset workflow.

#### Scenario: Matching dimensions
- **WHEN** the configured dim equals the column's stored dim
- **THEN** the server starts normally and indexer/search proceed

#### Scenario: Mismatch detected
- **WHEN** the configured dim is 1536 and the column is `Vector(1024)`
- **THEN** the server logs an error including both values and a pointer to `make reset-embeddings`, then exits non-zero

### Requirement: Reset embeddings workflow

The system SHALL provide an operator-triggered reset workflow that drops and recreates the `note_embeddings.embedding` column at the currently configured dimension and clears `embedded_content_hash` on all notes so they are re-embedded on the next indexer pass.

#### Scenario: Make target reset
- **WHEN** an operator runs `make reset-embeddings`
- **THEN** an alembic migration drops and recreates the column at `EMBEDDING_DIMENSIONS`, sets `embedded_content_hash = NULL` for every row in `notes_metadata`, and the next indexer pass re-embeds the vault

#### Scenario: Control-panel reset
- **WHEN** an operator clicks the "Reset embeddings" button in the control panel and confirms the modal
- **THEN** the indexer is paused, the same SQL effect is applied via a one-shot endpoint, and the indexer resumes; the dashboard shows progress on re-embedding

### Requirement: Visible provider status on dashboard

The control-panel settings/dashboard view SHALL display the active embedding provider name, model, and configured dimension count. When the provider is `openai`, the masked key prefix (e.g. `sk-...abc1`) SHALL be shown but the full key SHALL never be rendered.

#### Scenario: Ollama active
- **WHEN** the dashboard loads with provider=ollama
- **THEN** the page shows "Provider: Ollama", the configured model name, and the Ollama URL

#### Scenario: OpenAI active
- **WHEN** the dashboard loads with provider=openai and a key prefixed `sk-proj-1234...wxyz`
- **THEN** the page shows "Provider: OpenAI", the configured model, the dimension, and a masked key like `sk-proj-...wxyz`; the full key is not present in HTML or JS sources
