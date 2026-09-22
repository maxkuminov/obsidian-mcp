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
