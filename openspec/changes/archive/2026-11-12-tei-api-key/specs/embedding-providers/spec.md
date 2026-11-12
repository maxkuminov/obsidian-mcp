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

#### Scenario: TEI without an API key
- **WHEN** `EMBEDDING_PROVIDER=tei`, `OPENAI_API_KEY` is unset and `TEI_API_KEY` is unset, empty or whitespace-only
- **THEN** settings construction SHALL succeed
- **AND** no `Authorization` header SHALL be sent to `TEI_URL`

## ADDED Requirements

### Requirement: `TEI_API_KEY` SHALL be sent as a bearer token on every request to `TEI_URL`, and on no other request
With `EMBEDDING_PROVIDER=tei` and `TEI_API_KEY` set, every request the server makes to `TEI_URL` SHALL carry the header `Authorization: Bearer <key>`, where `<key>` is the value after surrounding whitespace is stripped. These requests are `POST /embed` (every attempt, including retries), `GET /info` (the identity check, from the lifespan and from the embedding gate) and `GET /health` (the control panel's reachability probe). The header SHALL be passed per request, and SHALL NOT be set as a default header of any client or inside `embedding_http_client`. The header SHALL NOT be sent by the Ollama or OpenAI providers, by the reranker, or on any request whose URL is not built from `TEI_URL`. When `TEI_API_KEY` is unset, empty or whitespace-only, the requests to `TEI_URL` SHALL be the same as before this change: the same methods, paths, bodies and header set, with no `Authorization` header. `TEI_API_KEY` SHALL NOT be part of the stored embedding fingerprint or the retrieval fingerprint, and setting, changing or removing it SHALL NOT require an embedding reset.

#### Scenario: All three call sites carry the header
- **WHEN** `TEI_API_KEY` is `k-123`, and the lifespan check, one `embed_batch` call of 20 inputs with `TEI_EMBED_BATCH_SIZE` 16, and one panel settings page load run against a mock TEI
- **THEN** the mock SHALL receive `GET /info`, two `POST /embed` and `GET /health`, each with exactly `Authorization: Bearer k-123`

#### Scenario: Retries carry the header
- **WHEN** `/embed` answers 503 and then 200
- **THEN** both attempts SHALL carry the header

#### Scenario: Unset is byte-identical
- **WHEN** `TEI_API_KEY` is unset, `""` or `"   "`
- **THEN** the header set of each request to `/info`, `/embed` and `/health` SHALL equal the header set captured with the pre-change code under the same mock, and SHALL contain no `Authorization`

#### Scenario: Surrounding whitespace is stripped
- **WHEN** `TEI_API_KEY` is `k-123` followed by a newline
- **THEN** the header SHALL be `Authorization: Bearer k-123`

#### Scenario: Other providers never send it
- **WHEN** `TEI_API_KEY` is set and `EMBEDDING_PROVIDER` is `ollama` or `openai`, or a rerank request is made
- **THEN** no request SHALL carry a header containing the key; the OpenAI provider's header SHALL still be built from `OPENAI_API_KEY` only

#### Scenario: The setting is read in three files only
- **WHEN** the source tree under `src/` is searched for `tei_api_key`
- **THEN** it SHALL occur only in `src/config.py`, `src/services/embeddings.py` and `src/services/transport_security.py`, and `src/services/rerank.py` SHALL NOT reference the header helper

#### Scenario: A redirect does not forward the header
- **WHEN** `/embed`, `/info` or `/health` answers 307 with `Location` naming a second listener
- **THEN** the second listener SHALL receive no request

#### Scenario: A key change does not refuse startup
- **WHEN** only `TEI_API_KEY` changes between two starts
- **THEN** the embedding fingerprint and the retrieval fingerprint SHALL be unchanged and startup SHALL proceed

### Requirement: `TEI_API_KEY` SHALL never be disclosed by the server
The value of `TEI_API_KEY` SHALL NOT appear in any log record (message, arguments or formatted exception), in the `/health` response, in any control-panel page (the `/admin/health` run history and error ring included), in the persisted `indexer_runs.error` column, in any exception message or stored embed-failure message, in any security event, in `repr` or `str` of the settings object, in a settings `ValidationError`, or in either embedding fingerprint. The setting SHALL be held as a `SecretStr`, and `Settings` SHALL keep `hide_input_in_errors` enabled.

#### Scenario: A sentinel value never surfaces
- **WHEN** `TEI_API_KEY` is a sentinel and the test runs startup (transport line, plaintext warning, identity check answering 200 match, 200 mismatch, 401 and 403), `embed_note` with `/embed` answering 200, 401, 403 and three 503s, the panel settings page, the app's `/health`, both fingerprints, and every security event emitted along the way, with logs captured at DEBUG
- **THEN** the sentinel SHALL NOT occur in any captured log record, response body, exception string, stored failure message, event or fingerprint
- **AND** the mock TEI SHALL have received the sentinel in the `Authorization` header, as the positive control

#### Scenario: The persisted pass record and the health page do not carry it
- **WHEN** `TEI_API_KEY` is a sentinel and an index pass runs against a verified TEI whose `/embed` answers 401
- **THEN** the persisted `indexer_runs.error` value of that pass SHALL NOT contain the sentinel, and SHALL name `TEI_API_KEY`
- **AND** the rendered `/admin/health` page (run history and the error ring) SHALL NOT contain the sentinel

#### Scenario: The settings representation masks it
- **WHEN** `repr(settings)` and `str(settings.tei_api_key)` are taken with the sentinel set
- **THEN** neither SHALL contain the sentinel

#### Scenario: A validation error does not echo it
- **WHEN** settings construction fails on a malformed `TEI_API_KEY` that contains the sentinel
- **THEN** the `ValidationError` text SHALL NOT contain the sentinel

### Requirement: A malformed `TEI_API_KEY` SHALL refuse startup without showing the value
With `EMBEDDING_PROVIDER=tei`, outside `MCP_SANDBOX_MODE`, settings construction SHALL refuse a `TEI_API_KEY` whose value, after surrounding whitespace is stripped, is non-empty and contains any character outside visible ASCII (U+0021 to U+007E), with a message naming `TEI_API_KEY` and not containing the value. An empty or whitespace-only value SHALL be treated as unset. Under any other provider, or under `MCP_SANDBOX_MODE`, the value SHALL NOT be checked and SHALL NOT be sent. The check SHALL do no I/O, so that settings construction in the migration init container, which imports `Settings`, succeeds whenever the key is absent.

#### Scenario: An inner space refuses startup
- **WHEN** `EMBEDDING_PROVIDER=tei` and `TEI_API_KEY` is `abc def`
- **THEN** settings construction SHALL fail with a message naming `TEI_API_KEY`, and the message SHALL NOT contain `abc` or `def`

#### Scenario: A control or non-ASCII character refuses startup
- **WHEN** `TEI_API_KEY` contains a carriage return, a tab between two visible characters, or the character U+00E9 (Latin small letter e with acute)
- **THEN** settings construction SHALL fail naming `TEI_API_KEY`

#### Scenario: Another provider ignores it
- **WHEN** `EMBEDDING_PROVIDER=ollama` and `TEI_API_KEY` is `abc def`
- **THEN** settings construction SHALL succeed and no request SHALL carry it

#### Scenario: An absent key never refuses
- **WHEN** `EMBEDDING_PROVIDER=tei`, `TEI_API_KEY` is absent, and settings are constructed as the migration init container constructs them
- **THEN** settings construction SHALL succeed

### Requirement: A 401 or 403 from TEI SHALL name `TEI_API_KEY` as the likely cause and SHALL NOT be treated as a model mismatch
A `GET /info` answered 401 or 403 SHALL be an inconclusive identity check, never a mismatch. It SHALL NOT exit the process, and embedding SHALL stay withheld under the existing re-check rule. The reason SHALL be `GET /info answered HTTP <status>; TEI_API_KEY is not set` when the key is unset, and `GET /info answered HTTP <status>; the server rejected TEI_API_KEY` when it is set. The identity check SHALL log one CRITICAL line carrying that reason when an auth reason first appears or changes, even if an inconclusive verdict of another kind was logged before. A repeat of the same auth reason SHALL NOT log again. Every other inconclusive reason SHALL keep its existing wording and once-per-verdict logging. A `POST /embed` answered 401 or 403 SHALL be raised after one request, with no retry, as a provider failure whose message is `TEI /embed answered HTTP <status>; TEI_API_KEY is not set` or `TEI /embed answered HTTP <status>; the server rejected TEI_API_KEY`, carrying no URL, header or response body. It SHALL be recorded by `embed_note` as `PROVIDER_FAILED`, leaving previous vectors intact. One ERROR line with that message SHALL be logged on the first such failure, and SHALL NOT be logged again until a `POST /embed` has been answered 200. An `/embed` auth failure SHALL NOT clear the verified state. The OpenAI provider's handling of 401 and 403 SHALL NOT change.

#### Scenario: /info 401 with no key
- **WHEN** `TEI_API_KEY` is unset and `/info` answers 401 at startup
- **THEN** the server SHALL start, SHALL NOT exit, and SHALL log one CRITICAL line containing `GET /info answered HTTP 401; TEI_API_KEY is not set`

#### Scenario: /info 403 with a rejected key
- **WHEN** `TEI_API_KEY` is set and `/info` answers 403
- **THEN** the check SHALL be inconclusive with the reason `GET /info answered HTTP 403; the server rejected TEI_API_KEY`, and no `/embed` request SHALL be sent

#### Scenario: The auth line is not swallowed by an earlier inconclusive line
- **WHEN** `/info` first fails with a connection error, and at the next check answers 401
- **THEN** two CRITICAL lines SHALL have been logged, the second naming `TEI_API_KEY`; a third check answering 401 again SHALL log nothing

#### Scenario: Recovery after the key is fixed
- **WHEN** `/info` answered 401 and, after a restart with the right key, answers 200 with a matching `model_id`
- **THEN** the server SHALL be verified and embedding SHALL proceed

#### Scenario: /embed 401 mid-run
- **WHEN** the server is verified and `/embed` answers 401 for every note of a 50-note pass
- **THEN** each note SHALL record `PROVIDER_FAILED` with the message `TEI /embed answered HTTP 401; the server rejected TEI_API_KEY`, exactly one request SHALL be made per slice, and no `/info` request SHALL be made
- **AND** exactly one provider ERROR line naming `TEI_API_KEY` SHALL be logged for the pass, beside the indexer's existing pass-summary ERROR line and its existing per-note WARNING lines (`Failed to embed <path>: …`), which this change SHALL NOT alter

#### Scenario: The ERROR line re-arms after success
- **WHEN** `/embed` answers 401, then 200, then 401
- **THEN** two provider ERROR lines naming `TEI_API_KEY` SHALL have been logged

### Requirement: A key on a plaintext non-loopback TEI hop SHALL be admitted with one startup warning
When `EMBEDDING_PROVIDER=tei`, `TEI_API_KEY` is set, and `TEI_URL` is `http` to a non-loopback host (admitted by `EMBEDDING_ALLOW_PLAINTEXT=true`), the server SHALL start and SHALL log, once at startup after the embedding transport line, one WARNING: `TEI_API_KEY is sent in clear over http to a non-loopback host (admitted by EMBEDDING_ALLOW_PLAINTEXT); use an https TEI_URL to protect it`. With an `https` URL, a loopback `http` URL, or the key unset, no such line SHALL be logged. The embedding transport line and the `internal_transport_plaintext` event SHALL be unchanged.

#### Scenario: Plaintext remote hop with a key
- **WHEN** `TEI_URL=http://tei:80`, `EMBEDDING_ALLOW_PLAINTEXT=true` and `TEI_API_KEY` is set
- **THEN** startup SHALL proceed and the WARNING SHALL be logged exactly once, without the key

#### Scenario: Loopback or TLS
- **WHEN** `TEI_URL` is `http://127.0.0.1:8080` or `https://tei.internal.example`, with `TEI_API_KEY` set
- **THEN** no plaintext-key WARNING SHALL be logged

#### Scenario: Unset key logs nothing new
- **WHEN** `TEI_URL=http://tei:80`, `EMBEDDING_ALLOW_PLAINTEXT=true` and `TEI_API_KEY` is unset
- **THEN** the startup log lines SHALL be the same as before this change
