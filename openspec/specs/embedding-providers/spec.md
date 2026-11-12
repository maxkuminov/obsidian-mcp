# embedding-providers Specification

## Purpose
How the server turns text into vectors: a pluggable provider (Ollama, an OpenAI-compatible endpoint, or a native Text Embeddings Inference server) selected by configuration, its transport and batching, the stored embedding fingerprint that refuses a startup whose model, dimensions or chunking parameters differ from the stored vectors', and (#355, #367) the query/document `kind` every embedding call declares, the query-only `EMBEDDING_QUERY_INSTRUCTION` that stays outside the stored fingerprint, the logged but never-compared retrieval fingerprint, and the per-note chunking version that re-selects notes instead of refusing startup.
## Requirements
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

### Requirement: OpenAI provider configuration validation

When `EMBEDDING_PROVIDER=openai`, the system SHALL validate at startup that `OPENAI_API_KEY` is set and non-empty. The system SHALL also accept an optional `OPENAI_BASE_URL` to support Azure OpenAI and OpenAI-compatible proxies, defaulting to `https://api.openai.com/v1`.

#### Scenario: Missing API key
- **WHEN** `EMBEDDING_PROVIDER=openai` and `OPENAI_API_KEY` is unset or empty
- **THEN** the server fails to start with an error stating the API key is required

#### Scenario: Custom base URL
- **WHEN** `OPENAI_BASE_URL=https://my-azure.openai.azure.com/openai/deployments/my-deploy` is set
- **THEN** embedding requests POST to that base URL instead of the public OpenAI endpoint

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

### Requirement: Dimension-mismatch startup check

The system SHALL compare the configured `EMBEDDING_DIMENSIONS` against the actual width of the `note_embeddings.embedding` column at startup. On mismatch, the system SHALL refuse to start with an error explaining how to run the reset workflow.

#### Scenario: Matching dimensions
- **WHEN** the configured dim equals the column's stored dim
- **THEN** the server starts normally and indexer/search proceed

#### Scenario: Mismatch detected
- **WHEN** the configured dim is 1536 and the column is `Vector(1024)`
- **THEN** the server logs an error including both values and a pointer to `make reset-embeddings`, then exits non-zero

### Requirement: Reset embeddings workflow

The system SHALL provide an operator-triggered reset workflow that drops and recreates the `note_embeddings.embedding` column at the currently configured dimension and clears `embedded_content_hash` on all notes so they are re-embedded on the next indexer pass. The workflow SHALL, in the same transaction, record the current embedding settings fingerprint, so that the configuration the reset commits to is the configuration the next startup verifies against.

**Every reset path SHALL create the approximate vector index only where the configured dimension admits one.** The vector extension refuses to build that index above its dimension limit, so a reset path that creates it unconditionally fails outright on a deployment configured above the limit — leaving the operator with a wiped column, no index, and an aborted transaction, while the equivalent path elsewhere in the system already skips it. Both the command-line reset and the control-panel reset SHALL apply the same condition, and a deployment above the limit SHALL complete its reset with no approximate index and a log line saying so, exactly as the pre-warm probe already reports.

These maintenance workflows SHALL be the only writers of their respective fingerprints after the initial adoption. That is what closes the loop: a configuration change refuses startup, the operator runs the repair, the repair records the new configuration, and the next startup is silent because the stored rows really were produced under it.

#### Scenario: Make target reset
- **WHEN** an operator runs `make reset-embeddings`
- **THEN** an alembic migration drops and recreates the column at `EMBEDDING_DIMENSIONS`, sets `embedded_content_hash = NULL` for every row in `notes_metadata`, records the current embedding fingerprint, and the next indexer pass re-embeds the vault

#### Scenario: Control-panel reset
- **WHEN** an operator clicks the "Reset embeddings" button in the control panel and confirms the modal
- **THEN** the indexer is paused, the same SQL effect is applied via a one-shot endpoint including the fingerprint record, and the indexer resumes; the dashboard shows progress on re-embedding

#### Scenario: A reset clears a refusing startup
- **WHEN** a startup has refused on an embedding fingerprint mismatch and the operator runs the reset with the new configuration in place
- **THEN** the stored fingerprint SHALL become the new configuration's
- **AND** the next startup SHALL proceed without refusing

#### Scenario: A dimension above the index limit resets without an index
- **WHEN** `EMBEDDING_DIMENSIONS` is configured above the vector extension's index limit and either reset path runs
- **THEN** the column SHALL be recreated at that dimension, the hashes cleared and the fingerprint recorded
- **AND** no approximate vector index SHALL be created, and the omission SHALL be logged
- **AND** the reset SHALL NOT fail

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

### Requirement: The keyword fingerprint is written only by an operation that rebuilt every retained row
The keyword-vector rebuild SHALL record the keyword fingerprint only as part of an operation that rebuilt **every `notes_metadata` row retained in the database**, and SHALL write the fingerprint and every rebuilt row in one transaction, so that a fingerprint can never certify a row that is still on the previous configuration.

The rebuild is per owner. A fingerprint written inside a per-owner rebuild claims something a per-owner rebuild cannot establish, and two ordinary shapes falsify it: a second owner's rebuild that raises after the first has already written the fingerprint, and an owner scope holding rows that the driver never visits at all — an inactive or unassigned user, or the ownerless scope in a database that also holds named users. In either case the stored fingerprint certifies rows still carrying the previous configuration, and a startup that refuses on that fingerprint would pass while keyword search is exactly as wrong as before.

The operation SHALL therefore determine its scopes from the **rows that exist** — every distinct owner value present in `notes_metadata`, the null owner included — rather than from the set of active users, SHALL rebuild each of them, and SHALL write the fingerprint only after every one of them reported a **completed** rebuild.

**An inactive owner with an assigned vault SHALL be rebuilt, not skipped.** The active-user set answers "whom does the periodic pass serve"; this operation answers "which rows exist", and an inactive owner's rows are as retained, and as returnable by keyword search, as anyone's. The operation SHALL resolve that owner's assigned vault path directly and read-only within itself and pin it as any file-reading pass pins a root, without widening the active-user root resolution or the active-user cache. Only an owner with **no** assigned path, or one whose path cannot be pinned, is a skip — and the provenance gate applies to an inactive owner exactly as to an active one.

**The per-scope rebuild SHALL return a typed outcome distinguishing "completed" from every kind of skip, and SHALL NOT report both as a row count.** It returns a count today, and `0` means both "this scope had nothing to do" and "this scope was skipped because its provenance is not settled" — the gate that already forbids the keyword rebuild from writing for a user whose index provenance is unresolved. A driver reading `0` as success would record a fingerprint certifying a scope the rebuild deliberately declined to touch, which is precisely the false claim this requirement exists to remove. Any retained scope whose outcome is not "completed" — provenance unsettled, root unpinnable, or any other named skip — SHALL abort the driver, SHALL prevent the fingerprint write, and SHALL be named with its reason.

**A retained scope with a null owner SHALL abort the operation while multi-user mode is enabled.** The ownerless scope has no vault root to pin in that mode, and substituting the process-wide configured vault path would read one tenant's notes under an unowned scope — a tenancy violation performed to satisfy a bookkeeping row. Nor may such rows be quietly excluded from the coverage proof: they are retained rows that keyword search can return. The operation SHALL therefore name them and their count and write nothing, and the operator's prerequisite — deleting or reassigning them — SHALL be stated in the refusal and in the deploy checklist rather than first discovered as a refusing startup. In single-user mode the ownerless scope is the only scope and rebuilds normally.

The cost — a multi-tenant rebuild becomes all-or-nothing rather than per tenant — SHALL be accepted and documented. The rebuild touches no embeddings and makes no provider calls, and the alternative is a fingerprint that does not mean what it says.

Where a scope cannot be rebuilt, the operation cannot complete and the refusal at startup therefore persists. Three remedies SHALL be stated in both the refusal and the operator documentation: settle the scope (assign or delete its user, or let an in-progress re-derive complete), delete or reassign ownerless rows, or restore the previous configuration, which clears the refusal immediately with no rebuild at all.

#### Scenario: One failing scope writes no fingerprint

- **WHEN** two owner scopes hold rows and the second scope's rebuild raises
- **THEN** no fingerprint SHALL be recorded
- **AND** the first scope's rebuilt vectors SHALL be rolled back with it
- **AND** the error SHALL name the scope that failed

#### Scenario: A scope holding rows but not in the active set is rebuilt

- **WHEN** `notes_metadata` holds rows owned by an inactive user who has an assigned vault path and settled provenance
- **THEN** the rebuild SHALL rebuild that scope, resolving and pinning that owner's assigned path within the operation
- **AND** it SHALL NOT be reported as a skip, and SHALL NOT block the fingerprint

#### Scenario: The active-user machinery is unchanged

- **WHEN** the rebuild resolves an inactive owner's root
- **THEN** the set of users the periodic index pass serves SHALL be unchanged
- **AND** the active-user root resolution and cache SHALL be unchanged

#### Scenario: A complete rebuild records the fingerprint atomically

- **WHEN** every scope holding rows is rebuilt successfully
- **THEN** the fingerprint and the rebuilt vectors SHALL be committed in one transaction
- **AND** the next startup SHALL proceed silently

#### Scenario: An unreadable scope is named and blocks the record

- **WHEN** a scope holds rows and its vault root cannot be pinned
- **THEN** the operation SHALL roll back, write no fingerprint, and name that scope
- **AND** the message SHALL state that assigning or deleting that user, or restoring the previous configuration, is the way forward

#### Scenario: A skipped scope is not read as a completed one

- **WHEN** a retained scope's rebuild is skipped because that user's index provenance is not settled
- **THEN** the driver SHALL treat it as not completed, roll back, and write no fingerprint
- **AND** it SHALL name the scope and the reason, rather than reading the skip's zero row count as success

#### Scenario: Ownerless rows abort the rebuild under multi-user mode

- **WHEN** multi-user mode is enabled and `notes_metadata` retains rows with a null owner
- **THEN** the operation SHALL abort, naming those rows and their count, and SHALL write no fingerprint
- **AND** it SHALL NOT rebuild them against the process-wide configured vault path
- **AND** it SHALL NOT exclude them from the coverage proof

#### Scenario: Ownerless rows are the normal case in single-user mode

- **WHEN** multi-user mode is disabled and every retained row has a null owner
- **THEN** that scope SHALL be rebuilt normally and the fingerprint SHALL be recorded

### Requirement: A generation lock makes the fingerprint an interlock, not merely a startup check
The system SHALL hold a single, transaction-scoped, database-level advisory lock — one fixed key, the **index generation lock** — across every mutation whose correctness depends on the configuration the stored derived rows were built under, so that a configuration change and a derived-row write cannot interleave.

**A check at the head of a pass is not sufficient, and SHALL NOT be relied on as the enforcement.** A pass that reads the fingerprint, then issues an embedding provider call taking seconds to minutes, then certifies, has separated the check from the act by a network round trip: a reset that commits in that window leaves the pass certifying previous-configuration vectors under the new fingerprint, permanently, with every later startup silent because the stored value already matches. The reset is designed to run as a one-off container reading the edited configuration, so it can and does run while a previous container is still serving.

The lock's rules:

- **Every maintenance operation that changes the generation SHALL take the lock before it mutates anything** — before the embedding wipe, before the keyword rebuild reads its first row, and before either records a fingerprint.
- **Every transaction that writes a configuration-dependent derived row SHALL take the lock, re-read the corresponding fingerprint under it, and refuse on a mismatch.** For the embedding path the lock SHALL be acquired **after** the provider call and **before** the certification — the window the existing certification requirement already reserves, so that no lock of any kind is held across a network request. On a mismatch the transaction SHALL certify nothing, insert nothing and delete nothing, leaving the row for a later pass, which is the disposition a failed certification already has.
- **On the embedding path that acquisition SHALL live in the function that owns both statements.** The provider call and the certification are two statements of one function; no caller sits between them, so the lock and the fingerprint re-read SHALL be performed there rather than by the pass that invokes it. A mismatch SHALL be reported to the pass as its own outcome, distinct from a provider failure and from a successful embed: it SHALL NOT count as a note the pass embedded, SHALL NOT count as a failure — nothing went wrong with the provider — and SHALL count as an attempt **if and only if a provider call was issued** for that note. A note whose every chunk reused a stored vector reaches the certification without a provider call; its mismatch SHALL NOT count as an attempt. When stored vectors are reused, the check under the lock SHALL also require every reused row to still exist with its chunk text, and a failure of that check SHALL have the mismatch disposition.
- **Every writer of a note's keyword vector SHALL take the same lock and make the same re-read**, including the incremental index pass. A rebuild can otherwise complete and record its fingerprint while an old-configuration pass writes one note's keyword vector under the previous configuration — and because a keyword vector is rewritten only when a note's content hash changes, that row then stays on the previous configuration indefinitely behind a fingerprint claiming otherwise. A refusal there SHALL abort that pass with nothing committed, as a floor failure already does.
- **The lock SHALL be transaction-scoped, never session-scoped**, so it is released by commit or rollback and a crashed pass cannot strand it in a pooled connection.
- **The lock SHALL be acquired before any row or table lock** in every transaction that takes it, so that one ordering holds everywhere and the new lock cannot close a cycle with the row locks the pass, the panel and the index-discard branch already contend for. The embedding backlog and reconciliation discovery transactions, including their per-note ORM lookups, SHALL end before provider I/O; plain SELECT table locks SHALL NOT be retained into the later generation-lock acquisition. The verified hash/path snapshots and after-provider fingerprint recheck SHALL be preserved.
- **That ordering is a property of the transaction, not of the statement that needs the fingerprint.** A transaction that will write any configuration-dependent derived row SHALL acquire the lock and re-validate the fingerprint **before its first row-locking mutation**, and the implementation SHALL audit every mutation earlier in that transaction rather than reason backwards from the write that consumes the fingerprint. The index pass is one transaction that mutates note metadata — upserts, move updates, prunes, link rows, certification invalidation — long before it reaches its keyword-vector write; acquiring the lock at that write would leave the pass holding row locks while it waits for the lock, and the rebuild holding the lock while it waits for those rows, which is a deadlock and a direct violation of the ordering rule. The acquisition therefore belongs at the head of that transaction.

Holding the lock for the duration of a long pass is accepted: the maintenance operations then **wait** for an in-flight pass rather than interleaving with it, which is the required behaviour, and those operations SHALL NOT defeat it with a short lock timeout — **nor with any other timeout that applies to the acquisition**. The connection carries a statement timeout and the advisory-lock acquisition is a statement, so every path whose contract is "it waits" SHALL lift that timeout for the acquisition alone and restore the previous value once the lock is held. Lifting it beforehand SHALL NOT be treated as a violation of the ordering rule: a session-variable assignment takes no row or table lock and is invisible to the lock graph. The waiting side includes the incremental index pass whenever a maintenance operation holds the lock first; only the per-note embedding acquisition keeps the connection's timeout, because that transaction must not sit on a lock for minutes and its mismatch disposition is already to leave the note for a later pass.
- **An absent state table has the same disposition as an absent fingerprint.** A transaction that would re-read a fingerprint SHALL first establish that the state table exists without raising when it does not, and SHALL proceed past this guard when it is absent. Other required schema objects remain prerequisites; this does not guarantee operation on an otherwise unmigrated schema.
- **The key SHALL be a single declared constant**, defined in one place and not derived at runtime from a value that could differ between builds.
- **Any keyword-vector writer retained outside the interlock SHALL be private and SHALL have no production caller**, and that SHALL be enforced by a test rather than by a comment. The single-scope keyword rebuild kept for the tests that hold its per-scope contract writes `content_tsvector` without taking the lock or re-reading the fingerprint; exported under a plausible public name beside the operational driver, it reads like the per-user version of it, and one row written through it under a superseded configuration keeps that vector indefinitely behind a fingerprint claiming otherwise.

**The exclusion branch is exempt**, and the exemption is by argument rather than omission: it issues no provider call, writes no vector, and stamps a row to record that an *excluded* note has been dealt with — a claim true under any configuration, because the correct vector set for an excluded note is the empty one. It has nothing a generation change can invalidate.

A per-pass fingerprint re-read MAY additionally be performed as a cheap early exit, so that a process running the previous configuration abandons the stage instead of grinding through a backlog whose every certification the lock will refuse. It is an optimisation and SHALL NOT be described as the guarantee.

The documented ordering for any change to the embedding configuration SHALL still be: edit the configuration, deploy — at which point the new image refuses at the fingerprint or the dimension guard and stays down, embedding nothing — run the reset while it is down, then start. This inverts the previous advice to reset before recreating the container, which was safe only while nothing depended on a stored claim. The lock is what makes an operator who does not follow that ordering lose time rather than correctness.

#### Scenario: A reset committing during a provider call cannot be overwritten

- **WHEN** an embed pass reads a matching fingerprint, issues its provider call, a one-off reset commits a wipe and a new fingerprint while that call is in flight, and the pass then reaches its certification
- **THEN** the certification SHALL be refused under the generation lock
- **AND** no embedding row SHALL be inserted or deleted for that note, and its `embedded_content_hash` SHALL be left unchanged
- **AND** a later pass running the new configuration SHALL embed that note

#### Scenario: An old-configuration keyword write cannot land after a rebuild

- **WHEN** the keyword rebuild completes every retained scope and records its fingerprint, and a process running the previous config then attempts to write one note's keyword vector in its incremental pass
- **THEN** that write SHALL be refused under the generation lock
- **AND** that pass SHALL abort with nothing committed
- **AND** the rebuilt row SHALL NOT be overwritten with a vector built under the previous configuration

#### Scenario: The single-scope rebuild is private and uncalled

- **WHEN** the tree is searched for callers of the single-scope keyword rebuild under the application and script trees
- **THEN** there SHALL be none, and the function SHALL NOT be exported under a public name
- **AND** a test SHALL fail if either changes

#### Scenario: The rebuild takes the lock before it reads

- **WHEN** the keyword rebuild driver runs
- **THEN** it SHALL hold the generation lock before it reads the first row it intends to rebuild
- **AND** no keyword-vector write by any process SHALL commit between that read and the fingerprint record

#### Scenario: The index pass and the rebuild do not deadlock

- **WHEN** an index pass has begun its transaction and mutated note metadata rows, and the keyword rebuild driver starts concurrently on another connection
- **THEN** neither transaction SHALL be aborted by the database as a deadlock victim
- **AND** the pass SHALL have acquired the generation lock before its first row-locking mutation, so the rebuild waits for the pass rather than the two waiting on each other

#### Scenario: A maintenance operation waits for an in-flight pass

- **WHEN** a reset or a rebuild starts while an index pass holds the generation lock
- **THEN** it SHALL wait for the pass to commit or roll back rather than failing fast or proceeding alongside it
- **AND** the wait SHALL complete even when the pass holds the lock for longer than the connection's statement timeout

#### Scenario: The index pass waits for an in-flight maintenance operation

- **WHEN** an incremental index pass starts while a rebuild holds the generation lock for longer than the connection's statement timeout
- **THEN** the pass SHALL wait for the rebuild to commit or roll back rather than being cancelled

#### Scenario: No lock is held across a provider call

- **WHEN** an embed pass embeds a note
- **THEN** the generation lock SHALL be acquired after the provider call returns and before the certification
- **AND** it SHALL NOT be held while the provider call is in flight

#### Scenario: The exclusion branch does not take the lock

- **WHEN** the pass certifies a note whose path matches an exclusion pattern and deletes its vectors
- **THEN** it SHALL proceed without the generation lock, because it writes no vector and its claim is configuration-independent

#### Scenario: The in-process reset does not deadlock against its own pass

- **WHEN** the reset is performed by the running process itself
- **THEN** it SHALL acquire the generation lock and complete
- **AND** the following embed stage SHALL proceed normally against the fingerprint that reset recorded

#### Scenario: An absent state table does not abort certification

- **WHEN** a note is embedded with every required schema object present except the indexer state table
- **THEN** the note SHALL be embedded and certified normally
- **AND** no read of the state table's contents SHALL be issued, and the pass SHALL NOT fail

#### Scenario: A crashed holder does not strand the lock

- **WHEN** a transaction holding the generation lock fails or its connection drops
- **THEN** the lock SHALL be released without operator action, and the next taker SHALL acquire it

#### Scenario: A mismatch without a provider call is not an attempt

- **WHEN** every chunk of a note reuses a stored vector and a reset deletes those rows before the certification's check under the lock
- **THEN** the outcome SHALL be a generation mismatch, nothing SHALL be certified, inserted or deleted, and the pass's attempt count SHALL be unchanged

### Requirement: A fingerprint write that fails aborts the maintenance operation
A failure to record a fingerprint SHALL roll back the maintenance operation that was recording it and SHALL surface to the operator who invoked it. It SHALL NOT be logged and swallowed.

The swallow-on-failure rule the indexer follows covers **instrumentation** — the pass history and the rotation cursor — where a lost write costs an operator a view. A fingerprint is not instrumentation: it is the claim a later startup refuses on. A reset that wiped the vectors and then swallowed a failed fingerprint write leaves a stored value naming the previous configuration over rows about to be built under the new one; a rebuild that rebuilt everything and lost its write refuses at every subsequent startup over a database that is actually correct.

#### Scenario: A failed fingerprint write rolls back the reset

- **WHEN** the embedding reset's fingerprint record fails
- **THEN** the whole reset SHALL roll back, leaving the column and the stored fingerprint as they were
- **AND** the failure SHALL surface to the operator rather than being logged and swallowed

#### Scenario: A failed fingerprint write rolls back the rebuild

- **WHEN** the keyword rebuild's fingerprint record fails after every scope has been rebuilt
- **THEN** the whole rebuild SHALL roll back and the stored fingerprint SHALL be unchanged

#### Scenario: Reset DDL can complete during an embedding provider call

- **WHEN** either the backlog or reconciliation sweep reaches a provider call and a concurrent reset takes the generation lock, drops and recreates the embedding index/column, and records a different fingerprint
- **THEN** discovery table locks SHALL NOT block the reset's DDL or close a deadlock cycle with certification
- **AND** the old provider output SHALL be refused without certification or vector writes after the reset commits

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
The system SHALL track the chunking format as the integer `CHUNKING_VERSION`, record it per note in `notes_metadata.embedded_chunking_version`, and SHALL NOT add it to the stored embedding fingerprint. The chunking format covers:
- the sectioning rule;
- the minimum-length constant `CHUNK_MIN_CHARS` and the effective-minimum rule;
- the grouping and tail rules;
- the window-boundary lookback and its constant;
- the locator rule;
- whether window text is stored raw;
- the prefix, the embedded-text formatter (version 2 collapses whitespace runs longer than 8 characters) and the embedded-input formatter.

The current version SHALL be 2.

A change of chunking format SHALL re-select notes through the embed backlog, as the index-integrity requirement on chunking versions states. It SHALL NOT refuse startup or require the reset workflow. A change to any element of the chunking format SHALL bump `CHUNKING_VERSION`. The embedded-text formatter of each version SHALL be frozen in a per-version table (`EMBEDDED_TEXT_FORMATTERS`), in the same way as the per-version extraction cleaners. Vector reuse SHALL compare the stored row's text under its stamped version's formatter with the new text under the current formatter, so a version that changes the embedded-text formatter adds an entry and never edits one. Version 2 adds the collapse of whitespace runs longer than 8 characters, giving the entries `{1, 2}`. A version that changes the prefix builder SHALL clear the table to its own entry. `CHUNK_MIN_CHARS` SHALL NOT be a configuration setting and SHALL NOT be part of the stored fingerprint. Its effective value depends only on the chunk size, which is already in the fingerprint.

This is not the per-row model identity that the fingerprint requirement forbids. That prohibition exists because vectors from two models in one column make distance meaningless. A chunking change leaves provider, model and dimensions unchanged, and those stay in the stored, refusing fingerprint, so legacy and re-chunked vectors share one space. A transition still changes ranking, and with it which notes fit inside a bounded page. That is why every vector-search response declares the transition while it lasts, as the index-integrity requirement on chunking versions states.

#### Scenario: A chunking bump does not refuse startup
- **WHEN** a build with a different `CHUNKING_VERSION` starts over an index whose stored fingerprint matches its configuration
- **THEN** startup SHALL proceed silently, and every note whose stamp `IS DISTINCT FROM` the running version, whether lower, higher or NULL, SHALL be in the embed backlog

#### Scenario: A model change still refuses
- **WHEN** the configured model differs from the stored fingerprint's, whatever the chunking version
- **THEN** startup SHALL refuse and name the reset workflow, as before

#### Scenario: The version-2 deploy over a version-1 index
- **WHEN** the version-2 build starts over an index built and certified by the version-1 build, with any previously valid configuration
- **THEN** the computed embedding fingerprint SHALL equal the stored one byte for byte, and startup SHALL proceed
- **AND** every note SHALL be in the backlog, and a stored vector SHALL be reused when its prefix and its version-1 embedded text equal a new chunk's prefix and version-2 embedded text

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

