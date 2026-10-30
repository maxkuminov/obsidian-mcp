## ADDED Requirements

### Requirement: Every embedding call SHALL declare whether it embeds a query or a document
The shared embedding entry points and the provider protocol SHALL take a required keyword argument `kind`, either `"query"` or `"document"`, with no default. The indexer's chunk embedding SHALL pass `"document"`. `semantic_search`, the indexer's cache prewarm and the startup warm-up SHALL pass `"query"`. A test SHALL fail on any call to an embedding entry point under `src/` or `scripts/` that does not pass `kind`. Providers SHALL receive `kind`. A provider whose API has no input-type field SHALL ignore it.

#### Scenario: The indexer embeds documents
- **WHEN** `embed_note` sends chunks to the provider
- **THEN** the call SHALL carry `kind="document"`

#### Scenario: A search embeds a query
- **WHEN** `semantic_search` embeds the caller's query
- **THEN** the call SHALL carry `kind="query"`

#### Scenario: A call site without a kind fails the sweep
- **WHEN** a new call to `get_embedding` or `get_embeddings_batch` is added without `kind`
- **THEN** the call-site sweep test SHALL fail

### Requirement: `EMBEDDING_QUERY_INSTRUCTION` SHALL be prepended verbatim to query inputs only, and SHALL NOT be part of the stored embedding fingerprint
The system SHALL accept a setting `EMBEDDING_QUERY_INSTRUCTION`, default empty, of at most 1,000 characters, refused at load if it contains NUL or a C0 control other than line feed and tab. When it is non-empty, the shared embedding layer SHALL prepend it, unchanged and with no separator added, to the input of every `kind="query"` call. It SHALL NOT be applied to any `kind="document"` call. With the default empty value, every provider input SHALL be byte-identical to the input without the setting.

Because every stored vector is a document vector embedded without the instruction, the instruction SHALL NOT be a field of the stored, startup-compared embedding fingerprint. Changing it SHALL NOT refuse startup, SHALL NOT clear any certification and SHALL NOT re-embed anything.

#### Scenario: Default is unchanged
- **WHEN** the setting is empty
- **THEN** a query's provider input SHALL equal the query text

#### Scenario: Instructed query, plain documents
- **WHEN** the setting is `Instruct: find notes\nQuery: ` and a search and an index pass both run
- **THEN** the search's provider input SHALL be that string followed by the query, and no chunk's provider input SHALL contain it

#### Scenario: Changing the instruction needs no reset
- **WHEN** the instruction is changed between two starts and nothing else is
- **THEN** the second start SHALL proceed, the stored fingerprint SHALL be unchanged, and the embed backlog SHALL be unchanged

#### Scenario: A control character is refused
- **WHEN** the setting contains a NUL or a carriage return
- **THEN** settings load SHALL fail naming the setting

### Requirement: A retrieval fingerprint SHALL distinguish deployments by everything that shapes a query's result, without being stored or compared
The system SHALL provide `retrieval_fingerprint()`, a canonical-JSON rendering of the embedding fingerprint's fields plus `chunking_version` (the current `CHUNKING_VERSION`) and `query_instruction_sha256` (the first 16 hexadecimal digits of SHA-256 over the instruction's UTF-8, or null when the instruction is empty). It SHALL NOT be written to the index state store and SHALL NOT be compared at startup. Startup SHALL log it once. The log line SHALL carry the digest and never the instruction text. The search-evaluation harness SHALL record it for semantic arms.

#### Scenario: Two deployments that differ only in the instruction
- **WHEN** two processes share every embedding setting and differ only in `EMBEDDING_QUERY_INSTRUCTION`
- **THEN** their stored-fingerprint renderings SHALL be equal and their retrieval fingerprints SHALL differ

### Requirement: The chunking format SHALL be versioned per note and outside the stored embedding fingerprint, while model identity stays global and refusing
The system SHALL track the chunking format (the sectioning rule, the prefix and the embedded-input formatter) as the integer `CHUNKING_VERSION`, recorded per note in `notes_metadata.embedded_chunking_version`, and SHALL NOT add it to the stored embedding fingerprint. A change of chunking format SHALL re-select notes through the embed backlog, as the index-integrity requirement on chunking versions states. It SHALL NOT refuse startup or require the reset workflow. Any change to the input formatter that can alter a provider input for an unchanged `(embed_prefix, chunk_text)` pair SHALL bump `CHUNKING_VERSION`.

This is not the per-row model identity the fingerprint requirement forbids. That prohibition exists because vectors from two models in one column make distance meaningless. A chunking change leaves provider, model and dimensions unchanged, and those stay in the stored, refusing fingerprint, so legacy and re-chunked vectors share one space. A transition still changes ranking, and with it which notes fit inside a bounded page. That is why every vector-search response declares the transition while it lasts, as the index-integrity requirement on chunking versions states.

#### Scenario: A chunking bump does not refuse startup
- **WHEN** a build with a higher `CHUNKING_VERSION` starts over an index whose stored fingerprint matches its configuration
- **THEN** startup SHALL proceed silently and every note whose stamp `IS DISTINCT FROM` the running version, whether lower, higher or NULL, SHALL be in the embed backlog

#### Scenario: A model change still refuses
- **WHEN** the configured model differs from the stored fingerprint's, whatever the chunking version
- **THEN** startup SHALL refuse and name the reset workflow, as before
