## MODIFIED Requirements

### Requirement: Configurable embedding provider selection

The system SHALL support selecting the embedding backend via the `EMBEDDING_PROVIDER` environment variable, with valid values `ollama`, `openai` and `tei`. The default value SHALL be `ollama` so existing deployments continue working without configuration changes.

#### Scenario: Default provider is Ollama
- **WHEN** the server starts with no `EMBEDDING_PROVIDER` set
- **THEN** the active provider is `ollama` and embeddings are generated against the configured Ollama URL using the configured model

#### Scenario: OpenAI provider selected
- **WHEN** `EMBEDDING_PROVIDER=openai` and `OPENAI_API_KEY` is set
- **THEN** all calls to `get_embedding` and `get_embeddings_batch` route to OpenAI's `/v1/embeddings` endpoint using the configured `OPENAI_EMBEDDING_MODEL`

#### Scenario: Invalid provider value
- **WHEN** `EMBEDDING_PROVIDER` is set to a value other than `ollama`, `openai` or `tei`
- **THEN** the server fails to start with a configuration error naming the invalid value and listing valid options

#### Scenario: TEI provider selected
- **WHEN** `EMBEDDING_PROVIDER=tei`
- **THEN** all calls to `get_embedding` and `get_embeddings_batch` route to `POST /embed` on the configured `TEI_URL`
- **AND** the active embedding model SHALL be `TEI_EMBEDDING_MODEL`, read by the same branch that selects the provider

#### Scenario: TEI requires no API key
- **WHEN** `EMBEDDING_PROVIDER=tei` and `OPENAI_API_KEY` is unset
- **THEN** settings construction SHALL succeed
- **AND** no `Authorization` header SHALL be sent to `TEI_URL`

### Requirement: Provider-native batch embedding

The system SHALL embed multiple chunks per indexer pass using the provider's batch mechanism. For OpenAI, this SHALL be a batched request that sends up to 96 inputs in one HTTP call. For Ollama, this SHALL be a sequence of `/api/embed` requests, each carrying an `input` array of at most `OLLAMA_EMBED_BATCH_SIZE` consecutive chunks. For TEI, this SHALL be a sequence of `/embed` requests, each carrying at most `TEI_EMBED_BATCH_SIZE` consecutive chunks, under the TEI request contract requirement. Each of the two batch-size settings SHALL default to 16 and SHALL be bounded to 1–256. Each Ollama request SHALL be bounded by a 30 s timeout, and there SHALL be no aggregate deadline across requests. Each response SHALL carry exactly as many vectors as the request carried inputs, in input order, or the batch SHALL fail. Setting the batch size to 1 reproduces the pre-batching request shape.

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

### Requirement: Retry on transient OpenAI errors

When the OpenAI provider encounters an HTTP 429 or 5xx response or an `httpx.HTTPError` raised while sending the request, the system SHALL retry the request with exponential backoff of 1 s then 2 s, up to 3 attempts in total, before propagating the error. A retry after a 429 or 5xx SHALL be logged at WARNING level; a retry after a transport error SHALL NOT log. When the attempts are exhausted by transport errors, the exception from the last attempt SHALL propagate unchanged; when they are exhausted by a status, the `httpx.HTTPStatusError` for that response SHALL propagate. Any other non-200 status SHALL NOT be retried. A 200 response whose body is not JSON SHALL propagate `json.JSONDecodeError` after one request, with no retry.

#### Scenario: Rate limit retry
- **WHEN** OpenAI returns 429 on the first attempt and 200 on retry
- **THEN** the embedding call returns the successful result and logs the retry at WARNING level

#### Scenario: Persistent failure
- **WHEN** OpenAI returns 5xx on three consecutive attempts
- **THEN** the call raises an error that the indexer logs and skips, leaving `embedded_content_hash` unchanged so the chunk is retried on the next indexer pass

#### Scenario: A transport error is retried
- **WHEN** the first attempt raises `httpx.ConnectError` and the second is answered 200 with a valid body
- **THEN** the call SHALL return the vectors after one 1 s backoff, having made two requests

#### Scenario: Three transport errors exhaust the attempts
- **WHEN** all three attempts raise an `httpx.HTTPError`
- **THEN** the backoffs SHALL be 1 s then 2 s, three requests SHALL have been made, and the exception object from the third attempt SHALL propagate unchanged

#### Scenario: A non-retryable status is sent once
- **WHEN** OpenAI answers 400 with no input-limit code or phrase
- **THEN** exactly one request SHALL be made and `httpx.HTTPStatusError` SHALL propagate

#### Scenario: A non-JSON 200 is not retried
- **WHEN** OpenAI answers 200 with an HTML body
- **THEN** exactly one request SHALL be made, with no backoff, and `json.JSONDecodeError` SHALL propagate

### Requirement: Configurable embedding dimensions

The system SHALL store note embeddings in a pgvector column whose width matches `EMBEDDING_DIMENSIONS`. The default value SHALL be 1024 to preserve existing 1024-dimensional bge-m3 embeddings. When the OpenAI provider is selected, the system SHALL pass the configured dimension count to the API via the `dimensions` request parameter (supported by `text-embedding-3-*` models).

#### Scenario: Default dimension preserved
- **WHEN** `EMBEDDING_DIMENSIONS` is unset and the system runs migrations
- **THEN** `note_embeddings.embedding` is created as `Vector(1024)`

#### Scenario: OpenAI dimension override
- **WHEN** `EMBEDDING_PROVIDER=openai`, `EMBEDDING_DIMENSIONS=512`, and the column has been created with width 512
- **THEN** OpenAI requests include `"dimensions": 512` and stored vectors have length 512

#### Scenario: TEI sends no dimension parameter
- **WHEN** `EMBEDDING_PROVIDER=tei`
- **THEN** the `/embed` request SHALL NOT carry a dimension field, the model's own output width SHALL apply, and a width other than `EMBEDDING_DIMENSIONS` SHALL fail under the TEI response validation requirement

### Requirement: Visible provider status on dashboard

The control-panel settings/dashboard view SHALL display the active embedding provider name, model, and configured dimension count. When the provider is `openai`, the masked key prefix (e.g. `sk-...abc1`) SHALL be shown but the full key SHALL never be rendered.

#### Scenario: Ollama active
- **WHEN** the dashboard loads with provider=ollama
- **THEN** the page shows "Provider: Ollama", the configured model name, and the Ollama URL

#### Scenario: OpenAI active
- **WHEN** the dashboard loads with provider=openai and a key prefixed `sk-proj-1234...wxyz`
- **THEN** the page shows "Provider: OpenAI", the configured model, the dimension, and a masked key like `sk-proj-...wxyz`; the full key is not present in HTML or JS sources

#### Scenario: TEI active
- **WHEN** the dashboard loads with provider=tei
- **THEN** the page shows the provider name `tei`, `TEI_EMBEDDING_MODEL`, the dimension and `TEI_URL`
- **AND** the reachability indicator SHALL reflect a `GET /health` against `TEI_URL` made through the embedding transport factory

### Requirement: Stored settings fingerprints gate startup, and both fail closed
The system SHALL record, once per derived kind, a **fingerprint of the configuration those rows were generated under**, and SHALL compare it at startup against the fingerprint of the configuration the process is about to run with, refusing to start on a mismatch of either. Two fingerprints SHALL be kept: one for embeddings, covering the active provider, the active model, the dimension count, the chunk size, the chunk overlap and the per-note chunk cap; and one for keyword vectors, covering the configured text-search config list.

The fingerprints SHALL be **global**, one row each, and SHALL NOT be recorded per embedding row or per note. The settings they describe are global, a per-row copy would be one identical string per chunk, and its only use would be a lazy per-note re-embed — which is precisely the design that leaves vectors from two models coexisting in one index for the whole migration window. Cosine distance between two vector spaces is meaningless, so a partially migrated index answers wrongly for longer than a refused startup does. The remedy is the wipe-and-re-embed the reset workflow already performs.

The embedding fingerprint's `model` field SHALL be the model of the **active** provider, selected by the same branch that selects the provider itself, so that reading the inactive provider's model — the exact defect this guard exists to catch — cannot happen in one place while the provider is chosen in another.

**The per-note chunk cap SHALL be part of the embedding fingerprint.** It determines what a note's stored vector set *is*: at one cap a long note holds N chunks and its tail is absent, and at another it holds a different set. Lowering it leaves rows beyond the new bound; raising it leaves rows that are silently incomplete against the new policy and that nothing will ever re-select, because their content hash still matches. Including it makes a cap change a declared reset rather than a permanent, invisible under-embedding.

**Endpoint identity SHALL be excluded from the fingerprint** — none of `OLLAMA_URL`, `OPENAI_BASE_URL` and `TEI_URL` is part of it. Repointing at another host or proxy is an infrastructure change that usually serves the identical artifact, and including it would demand a full vault re-embed for it. The consequence SHALL be recorded as an accepted limitation rather than partially mitigated: **the fingerprint records the configuration, not the model artifact**, so replacing the weights behind a mutable model tag, or pointing at a host serving different weights under the same tag, mixes vector spaces undetected. No value available to the process distinguishes those cases, and a probe of the endpoint would have to trust the endpoint it is checking. The TEI server identity check does not change this: it compares the model name the server reports with the configured one, and compares no artifact digest. The operator rule that stands in its place SHALL be documented beside the model keys: a change of model artifact requires the embedding reset, and nothing will detect it if that step is skipped.

The keyword fingerprint's config list SHALL be compared **order-insensitively**, because a note's stored vector is the concatenation of one vector per config and a query is the disjunction of one query per config; reordering the list changes neither, so refusing on it would be a false alarm.

Comparison SHALL be by byte equality of a canonical rendering that admits exactly one spelling per configuration, and the rendering SHALL be parseable so that a mismatch can be reported field by field rather than as two opaque strings. The rendering SHALL carry a format version.

The comparison SHALL resolve as follows:

- **No fingerprint stored** — the system SHALL adopt the current one, record it, log a warning that it was assumed rather than verified, and start. Refusing here would take every existing deployment down on upgrade over a configuration nobody changed. The one-time consequence — that a configuration changed in the same deploy that introduces the fingerprint is blessed rather than caught — SHALL be documented in the change's deploy instructions.
- **Stored equals current** — the system SHALL start, silently.
- **Stored differs** — the system SHALL log at critical level naming both fingerprints and the fields that differ, point at the workflow that repairs that kind of row, and **exit non-zero**. This applies to embeddings and to keyword vectors alike.
- **Stored is unparseable, or carries a format version this build does not recognise** — the system SHALL treat it as a mismatch and refuse, with a message saying the stored value could not be interpreted, and SHALL write nothing. Overwriting it would convert a claim this build cannot read into a confident false one; the same rule already governs an extraction version whose frozen cleaner this build does not have.
- **The state store has not been migrated yet** — the system SHALL return without deciding, deferring to migrations, exactly as the dimension check defers when the embedding column is absent.

**Keyword vectors fail closed for the same reason embeddings do.** A stale stemmer is not merely incomplete: under an English config the token `running` is stored as the lexeme `run`, so a query under a `simple` config for `run` **matches a note that does not contain the word `run`** — a false positive indistinguishable from a real hit, handed to an agent that acts on it without a human ever seeing the query, which is this product's second-named expensive failure. Changing the config list is an operator action, it is rare, the refusal names the one command that repairs it, and reverting the configuration clears the refusal immediately with no rebuild at all. There is therefore always an exit, which is what distinguishes this refusal from an outage.

The existing validation of config *names* against the installed text-search configurations SHALL run **before** the fingerprint comparison, so a misspelled name still fails with its own message listing what is installed rather than as an opaque fingerprint difference.

**Only the maintenance workflows SHALL write a fingerprint after the initial adoption.** Startup SHALL NOT rewrite a fingerprint it has just refused on; a guard that clears its own refusal is not a guard.

The dimension check SHALL remain in place alongside the embedding fingerprint. It reads the live column width from the database catalogue — a physical fact about the table — while the fingerprint records the configuration the stored rows were produced under. A dump restored into a differently configured deployment trips the first; a same-dimension model swap trips only the second.

#### Scenario: A same-dimension model swap refuses startup

- **WHEN** the stored embedding fingerprint names one model and the configured provider names a different model of the same dimension
- **THEN** startup SHALL log both fingerprints and the differing field, name the embedding reset command, and exit non-zero
- **AND** no embedding row SHALL be modified by the check

#### Scenario: A chunk-size, overlap or chunk-cap change refuses startup

- **WHEN** the configured chunk size, chunk overlap or per-note chunk cap differs from the stored fingerprint's
- **THEN** startup SHALL exit non-zero with the same message shape and the same reset pointer

#### Scenario: An FTS config change refuses startup

- **WHEN** the configured text-search config list differs in membership from the stored keyword fingerprint's
- **THEN** startup SHALL log both lists and the differing entries, name the keyword-rebuild command, and exit non-zero

#### Scenario: A refusing startup writes nothing

- **WHEN** a process refuses on either fingerprint mismatch and is restarted without the repair having been run
- **THEN** the stored fingerprint SHALL be unchanged and the second start SHALL refuse identically

#### Scenario: Reverting the configuration clears the refusal

- **WHEN** an operator restores the configuration named by the stored fingerprint
- **THEN** the next start SHALL proceed, with no rebuild and no reset performed

#### Scenario: Reordering the FTS config list does not refuse

- **WHEN** the configured config list holds the same names as the stored fingerprint's in a different order
- **THEN** startup SHALL treat the fingerprints as equal and SHALL proceed silently

#### Scenario: A misspelled config name fails on its own message

- **WHEN** the configured config list names a text-search configuration the database does not have
- **THEN** startup SHALL fail with the message that lists the installed configurations
- **AND** SHALL NOT report the failure as a fingerprint difference

#### Scenario: An absent fingerprint is adopted, not refused

- **WHEN** a deployment whose state store holds no fingerprint starts
- **THEN** the system SHALL record the current fingerprints, log that they were assumed and not verified, and start
- **AND** the following start with an unchanged configuration SHALL be silent

#### Scenario: An unreadable stored fingerprint refuses and is not overwritten

- **WHEN** the stored value is not parseable, or carries a format version this build does not recognise
- **THEN** startup SHALL refuse, naming the stored value as uninterpretable
- **AND** the stored value SHALL be left exactly as it was

#### Scenario: An unmigrated state store defers

- **WHEN** the process starts against a database where the state store does not yet exist
- **THEN** the check SHALL return without deciding and without failing, leaving the table to migrations

#### Scenario: Sandbox mode skips both checks

- **WHEN** the server starts in sandbox mode
- **THEN** neither fingerprint SHALL be read, compared or written

#### Scenario: The active provider's model is the one fingerprinted

- **WHEN** the provider is any of `ollama`, `openai` and `tei`, and the model settings of the two inactive providers carry different values
- **THEN** the fingerprint SHALL name the active provider's model and no other
- **AND** changing either inactive provider's model setting SHALL NOT change the fingerprint or cause a mismatch

#### Scenario: A changed endpoint alone does not refuse

- **WHEN** only the provider's URL or base URL changes, with every other fingerprinted setting unchanged
- **THEN** startup SHALL proceed, because endpoint identity is not fingerprinted
- **AND** the limitation that a differently-weighted model behind the same name is undetected SHALL be recorded as accepted, with the operator rule documented beside the model keys

### Requirement: A configuration knob that invalidates derived rows SHALL name its remedy where it is set
Every configuration key whose change silently invalidates stored derived rows SHALL carry, in the operator-facing configuration example and in the reference documentation, the command that repairs those rows. The embedding model keys — `EMBEDDING_MODEL`, `OPENAI_EMBEDDING_MODEL` and `TEI_EMBEDDING_MODEL` — SHALL carry the embedding-reset pointer that the chunk-size, chunk-overlap and provider keys already carry, and the reference documentation's provider-switching section SHALL cover a model change within a single provider and SHALL state the deploy-then-reset ordering.

Documenting the remedy is not made redundant by the startup guard. The guard tells an operator that they have already made the change; the documentation tells them what the change costs before they make it, and it is the only signal for the operator editing the file on a host where the server is not running.

#### Scenario: The model keys carry the reset warning

- **WHEN** an operator reads the configuration example at any of the three model keys
- **THEN** each SHALL state that changing it after deploy requires the embedding reset workflow

#### Scenario: The reference documentation covers a same-provider model change and the ordering

- **WHEN** an operator reads the documentation section on changing embedding backends
- **THEN** it SHALL state that changing the model within one provider requires the same reset as switching providers
- **AND** it SHALL state that the deploy comes before the reset, and why

#### Scenario: The artifact rule is documented where the model is set

- **WHEN** an operator reads the configuration example or the reference documentation at any of the three model keys
- **THEN** it SHALL state that replacing the artifact behind an unchanged model name — re-pulling a mutable tag, or pointing at a host serving different weights — also requires the embedding reset
- **AND** it SHALL state that no startup check detects that case

### Requirement: Each provider SHALL use one pooled HTTP client built through the embedding transport factory
Each embedding provider SHALL send its requests through one shared, connection-pooling `httpx.AsyncClient` per event loop, instead of creating a client per call. That covers indexer batches, single embeddings and `semantic_search` query embeddings. The shared client SHALL be obtained only by calling the embedding transport factory (`embedding_http_client`), so it has the factory's properties:
- environment proxy and trust-store variables ignored;
- redirects not followed;
- the configured CA context.

No code path SHALL construct an `httpx` client for the embedding endpoint by any other means. The client SHALL be created lazily. It SHALL be rebuilt if the running event loop differs from the one it was created on. It SHALL be closed during application shutdown, after the indexer task has been cancelled. Per-request timeouts SHALL be passed on each request: 30 s for Ollama, 60 s for OpenAI, and 60 s for each TEI `POST /embed` attempt. The 60 s TEI timeout SHALL NOT apply to `GET /info`, which SHALL be bounded by the 5 s wall-clock timeout of the TEI identity check.

#### Scenario: Connections are reused
- **WHEN** the indexer embeds several notes in one pass
- **THEN** one client instance SHALL serve every request of that pass

#### Scenario: The client comes from the factory
- **WHEN** the shared client is created
- **THEN** it SHALL have been returned by `embedding_http_client`, and the transport sweep test SHALL find no other client construction aimed at the embedding endpoint

#### Scenario: The client is closed at shutdown
- **WHEN** the application shuts down
- **THEN** the shared client SHALL be closed after the indexer task has stopped

## ADDED Requirements

### Requirement: The TEI provider SHALL embed through native `/embed` requests with an exact request and response cardinality
With `EMBEDDING_PROVIDER=tei`, every embedding SHALL be one or more `POST {TEI_URL}/embed` requests whose JSON body is exactly `{"inputs": [...], "truncate": true}`. A batch SHALL be split into consecutive slices of at most `TEI_EMBED_BATCH_SIZE` inputs, sent sequentially. `TEI_EMBED_BATCH_SIZE` SHALL default to 16 and be bounded to 1–256. Each 200 response SHALL carry exactly one vector per input, in input order, or the batch SHALL fail and no vector of it SHALL be used. The request SHALL NOT carry a model field. The query/document `kind` SHALL be accepted and not sent.

#### Scenario: Single input
- **WHEN** `get_embedding("hello", kind="query")` runs under `tei` with an empty `EMBEDDING_QUERY_INSTRUCTION`
- **THEN** exactly one `POST /embed` SHALL be sent with the body `{"inputs": ["hello"], "truncate": true}`

#### Scenario: Fixed-size slices
- **WHEN** `TEI_EMBED_BATCH_SIZE` is 16 and `get_embeddings_batch` is called with 40 chunks
- **THEN** three sequential requests carrying 16, 16 and 8 inputs SHALL be sent, and the 40 vectors SHALL be returned in input order

#### Scenario: A short response fails the batch
- **WHEN** a request carrying 16 inputs is answered with 15 vectors
- **THEN** the batch SHALL fail and no vector from it SHALL be used

### Requirement: The TEI provider SHALL retry transient failures within one overall deadline per slice
A TEI slice SHALL be attempted at most 3 times, retrying on HTTP 429, HTTP 5xx and transport errors with exponential backoff of 1 s then 2 s. Each attempt SHALL carry the 60 s per-request timeout. The whole slice, attempts and backoff together, SHALL be bounded by one overall deadline of 90 s, and a single-input call SHALL be bounded the same way. Any other non-200 status SHALL NOT be retried. There SHALL be no deadline across slices. A slice that reaches the deadline SHALL fail with a provider failure whose message is non-empty and names the deadline.

#### Scenario: A transient 503 is retried
- **WHEN** `/embed` answers 503 and then 200 with a valid body
- **THEN** the call SHALL return the vectors after one backoff

#### Scenario: Persistent 5xx exhausts the attempts
- **WHEN** `/embed` answers 503 on three consecutive attempts
- **THEN** the call SHALL raise, the indexer SHALL record `PROVIDER_FAILED`, and the note SHALL remain uncertified with its previous vectors intact

#### Scenario: A hung server cannot hold a slice past the deadline
- **WHEN** every attempt on a slice hangs until its 60 s timeout
- **THEN** the slice SHALL fail no later than the 90 s deadline, and SHALL NOT take the roughly 183 s that three timed-out attempts and their backoff would

#### Scenario: A batch-limit refusal is not retried
- **WHEN** `/embed` answers 413 with `batch size 17 > maximum allowed batch size 16`
- **THEN** exactly one request SHALL be made and the call SHALL raise a provider failure

#### Scenario: A healthy many-slice note is not cut off
- **WHEN** a note needs more slices than 90 s of normal latency covers and every slice is answered within the deadline
- **THEN** every slice SHALL complete and the note SHALL be certified

### Requirement: Every TEI response MUST be validated before any vector of it is used
A 200 response body from `/embed` MUST parse as JSON and MUST be a list in which every row is a list of exactly `EMBEDDING_DIMENSIONS` finite numbers; a JSON boolean SHALL NOT count as a number. A response that does not satisfy this SHALL be a provider failure raised from the provider, carrying no input text, before any vector of the response is returned to its caller, so that it follows the path of any other provider exception and never reaches the database as a vector.

#### Scenario: A row of the wrong width
- **WHEN** `EMBEDDING_DIMENSIONS` is 1024 and one row of a 200 response has 768 elements
- **THEN** the provider SHALL raise, and no vector of that response SHALL be used

#### Scenario: A non-numeric or non-finite element
- **WHEN** a row contains a string, a null, a JSON boolean, a nested list, `NaN` or `Infinity`
- **THEN** the provider SHALL raise, and no vector of that response SHALL be used

#### Scenario: A non-JSON 200
- **WHEN** a proxy answers `/embed` with status 200 and an HTML body
- **THEN** the provider SHALL raise a provider failure and SHALL NOT retry it

#### Scenario: A JSON object instead of a list
- **WHEN** `/embed` answers 200 with `{"error": "x"}`
- **THEN** the provider SHALL raise a provider failure

#### Scenario: The indexer records it as a provider failure
- **WHEN** a malformed 200 is received while embedding a note
- **THEN** `embed_note` SHALL return `PROVIDER_FAILED`, nothing SHALL be written to `note_embeddings`, and no pgvector error SHALL occur

#### Scenario: Hybrid search degrades
- **WHEN** a malformed 200 is received for the query embedding of `hybrid_search`
- **THEN** the call SHALL return the keyword arm marked `semantic unavailable: provider_error`

### Requirement: The server MUST compare the TEI server's reported identity with its configuration at startup
With `EMBEDDING_PROVIDER=tei`, outside `MCP_SANDBOX_MODE`, the lifespan MUST make one `GET {TEI_URL}/info` request after the stored-fingerprint guards, with the complete exchange, including reading the response body, bounded by an overall `asyncio` wall-clock timeout of 5 s. The server SHALL exit non-zero, with one CRITICAL log line naming the setting and both values, when the response is a 200 JSON object whose `model_id` differs from `TEI_EMBEDDING_MODEL` by exact string comparison, or whose integer `max_client_batch_size` is less than `TEI_EMBED_BATCH_SIZE`. When the check cannot be concluded, because of a transport error, a timeout including that wall-clock timeout, any non-200 status, a body that is not a JSON object, or a `model_id` that is absent or not a non-empty string, the server SHALL start, SHALL log one CRITICAL line stating that embedding is withheld until the server is verified, and SHALL NOT exit. When `model_id` matches and `max_client_batch_size` is absent or not an integer, the check SHALL pass with one WARNING. The server-reported value SHALL be bounded to 200 characters and escaped in the log. Under any other provider no request to `/info` SHALL be made.

#### Scenario: A matching server
- **WHEN** `/info` reports `model_id` equal to `TEI_EMBEDDING_MODEL` and `max_client_batch_size` 96 with `TEI_EMBED_BATCH_SIZE` 16
- **THEN** startup SHALL proceed and the server SHALL be marked verified

#### Scenario: A different model refuses startup
- **WHEN** `TEI_EMBEDDING_MODEL` is `BAAI/bge-m3` and `/info` reports `model_id` `intfloat/multilingual-e5-small`
- **THEN** the server SHALL log one CRITICAL line naming `TEI_EMBEDDING_MODEL` and both values, and exit non-zero

#### Scenario: A batch size above the server's limit refuses startup
- **WHEN** `TEI_EMBED_BATCH_SIZE` is 128 and `/info` reports `max_client_batch_size` 96
- **THEN** the server SHALL log one CRITICAL line naming `TEI_EMBED_BATCH_SIZE` and both values, and exit non-zero

#### Scenario: An unreachable TEI does not stop the server
- **WHEN** `GET /info` fails with a connection error at startup
- **THEN** the server SHALL start and serve, with one CRITICAL line and no non-zero exit, and keyword search and the read and write tools SHALL be unaffected

#### Scenario: A hung or trickling /info cannot hold startup
- **WHEN** `GET /info` never answers, or sends its response slowly enough that no single read reaches the HTTP client's timeout
- **THEN** the exchange SHALL be abandoned at the 5 s wall-clock bound, and startup SHALL continue in the inconclusive state with one CRITICAL line and no non-zero exit

#### Scenario: A server that answers without a usable identity does not stop the server
- **WHEN** `/info` answers 401, 404, a 3xx, or a 200 without a `model_id`
- **THEN** the server SHALL start and the check SHALL be treated as not concluded

#### Scenario: A missing batch limit is not a refusal
- **WHEN** `model_id` matches and the response has no `max_client_batch_size`
- **THEN** the check SHALL pass with one WARNING

#### Scenario: Sandbox mode skips the check
- **WHEN** `MCP_SANDBOX_MODE=true` and `EMBEDDING_PROVIDER=tei`
- **THEN** no request to `/info` SHALL be made

#### Scenario: Other providers are not probed
- **WHEN** `EMBEDDING_PROVIDER` is `ollama` or `openai`
- **THEN** no request to `/info` SHALL be made and startup behaviour SHALL be unchanged

### Requirement: No TEI vector SHALL be requested until the TEI server has been verified
`TeiProvider` SHALL send no `/embed` request until a `/info` check in this process has concluded that the server matches. While unverified, each embedding call SHALL run the same check under the same 5 s wall-clock timeout, at most once per 30 s; between checks the call SHALL raise a provider failure without any HTTP request, carrying the last reason. A mismatch found after startup SHALL fail each call the same way, SHALL log one CRITICAL line when the verdict changes, and SHALL NOT exit the process. Once verified, a process SHALL NOT check again. This gate SHALL apply on every path that uses the provider, including maintenance scripts that do not run the lifespan.

#### Scenario: TEI comes up after the application
- **WHEN** `/info` failed at startup and later reports a matching model
- **THEN** the first embedding call after the 30 s check interval SHALL verify the server and proceed to `/embed`

#### Scenario: A wrong server that comes up late writes nothing
- **WHEN** `/info` failed at startup and later reports a different `model_id`
- **THEN** every embedding call SHALL raise a provider failure, no `/embed` request SHALL be sent, no vector SHALL be stored, and the notes SHALL remain uncertified

#### Scenario: Recovery without restart
- **WHEN** a late mismatch is corrected on the TEI server
- **THEN** the next check SHALL verify the server and embedding SHALL proceed without restarting the application

#### Scenario: An unverified server does not cost a probe per note
- **WHEN** 100 notes are embedded within 30 s while TEI is unreachable
- **THEN** at most one `/info` request SHALL be made in that interval and each note SHALL record `PROVIDER_FAILED`

#### Scenario: Verification is not repeated
- **WHEN** the server has been verified at startup and embedding calls follow
- **THEN** no further `/info` request SHALL be made by this process

### Requirement: The TEI provider SHALL NOT raise input-limit refusals, and other providers' input-limit classification SHALL NOT change
`TeiProvider` SHALL NOT raise `ProviderInputTooLarge`, because it sends `truncate: true`. A TEI response on a non-retryable status SHALL be a provider failure. The alternative `less than \d+ tokens` SHALL NOT be in the shared input-limit phrase list, so that the classification of Ollama and OpenAI-compatible responses is the classification on `main` before the TEI provider.

#### Scenario: A gateway's unrelated token message stays a provider error
- **WHEN** an OpenAI-compatible gateway answers 400 with `{"error": {"message": "max_tokens must be less than 4096 tokens"}}`
- **THEN** the `openai` provider SHALL NOT raise `ProviderInputTooLarge`, and SHALL raise the ordinary provider error

#### Scenario: The same message from Ollama
- **WHEN** Ollama answers 400 with `{"error": "max_tokens must be less than 4096 tokens"}`
- **THEN** the `ollama` provider SHALL NOT raise `ProviderInputTooLarge`

#### Scenario: Existing input-limit detection is unchanged
- **WHEN** an OpenAI-compatible provider answers 400 with `context_length_exceeded`
- **THEN** the `openai` provider SHALL raise `ProviderInputTooLarge` as before

#### Scenario: A TEI token-limit message is a provider failure
- **WHEN** `/embed` answers 413 with `` Input validation error: `inputs` must have less than 2048 tokens. Given: 4003 ``
- **THEN** exactly one request SHALL be made and a provider failure, not `ProviderInputTooLarge`, SHALL be raised

### Requirement: An over-limit chunk under TEI SHALL be truncated and certified as an accepted limitation
The TEI provider SHALL always send `truncate: true`, so that the server embeds an input longer than the model's limit from its leading tokens. Such a note SHALL be certified as embedded on the same terms as any other note, and no marker SHALL be recorded for the truncation. The operator documentation SHALL state this limitation and that it matches Ollama's truncation to the model context.

#### Scenario: An over-limit chunk is certified
- **WHEN** a chunk's provider input tokenizes to more than the model's limit and TEI returns a valid vector for it
- **THEN** the vector SHALL be stored and the note SHALL be certified as embedded, and the text beyond the limit SHALL NOT be represented in that vector

#### Scenario: An over-limit query is searched, not refused
- **WHEN** a `semantic_search` query under `MAX_SEARCH_QUERY_CHARS` exceeds the model's token limit under `tei`
- **THEN** the call SHALL return results for the truncated query and SHALL NOT return `argument_too_long`
