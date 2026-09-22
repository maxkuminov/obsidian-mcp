## ADDED Requirements

### Requirement: Whole-vault filesystem work and large-note cleaning SHALL run off the event loop
The index pass's walk, per-file stat, read and content hash SHALL execute in a worker thread and SHALL NOT execute on the event loop. So SHALL `parse_frontmatter`, `clean_for_embedding` and the bounded chunker, on the embed backlog, on the exclusion sweep's probe, and inside `embed_note`.

The scan thread SHALL check a stop signal between files. When the awaiting coroutine is cancelled, the signal SHALL be set and the cancellation re-raised, so shutdown waits for at most one file.

Acceptance criterion: while a full-hash pass is in progress, the application SHALL keep serving `/health` and other requests.

#### Scenario: The scan does not run on the loop
- **WHEN** an index pass reads and hashes a note
- **THEN** the read and hash SHALL execute on a thread other than the event loop's

#### Scenario: /health answers during a pass
- **WHEN** an index pass is in progress with a read that blocks for seconds per file
- **THEN** a concurrent `/health` request SHALL be answered and a concurrent coroutine SHALL make progress before the pass completes

#### Scenario: Embed-path cleaning is offloaded
- **WHEN** the embed backlog, the exclusion sweep's probe or `embed_note` cleans and chunks a note
- **THEN** `parse_frontmatter`, `clean_for_embedding` and the chunker SHALL be dispatched to a worker thread

#### Scenario: Cancellation stops the walk promptly
- **WHEN** the indexer task is cancelled during the walk
- **THEN** the scan SHALL stop before its next file and the cancellation SHALL propagate

### Requirement: The scan's walk SHALL precede the generation lock, and every mutation SHALL be decided against rows read under the lock
The index pass SHALL:
1. read an owner-scoped snapshot of its rows in a separate transaction that is **committed before the walk begins**;
2. perform the walk;
3. only then open the mutating transaction, whose first lock-taking statement SHALL remain the generation advisory lock, followed by the FTS fingerprint assertion.

No transaction SHALL be open during the walk. No table or row lock SHALL be held while waiting for the generation lock.

Under the lock, the pass SHALL re-read the same rows. It SHALL then apply these rules:
- **Unchanged rows.** For each walked path whose locked row equals its snapshot row in presence, content hash, extraction version and recorded stat, the walk's result stands.
- **Changed rows.** Each walked path whose row differs SHALL be re-read and re-decided under the lock.
- **The locked state decides.** Move detection, deletion and the change decisions SHALL be computed from the locked rows, never from the snapshot.
- **Deferral.** A row that the walk did not see, and whose locked state differs from the snapshot (including a row absent from the snapshot), SHALL NOT be pruned or paired as a move by this pass. It SHALL be left for the next pass.
- **Re-derive.** Under a re-derive, such a deferral SHALL count as a skip, and the re-derive stamp SHALL be withheld.

All mutations SHALL still commit in the pass's single transaction.

#### Scenario: A move landing mid-walk is not pruned
- **WHEN** `move_note` commits a row's new path after the snapshot and before the lock, and the walk saw neither the old nor the new path
- **THEN** the pass SHALL neither delete that row nor pair it as a move, and a later pass SHALL settle it

#### Scenario: A row another process changed is re-decided under the lock
- **WHEN** another process commits a new content hash for a walked path between the snapshot and the lock
- **THEN** this pass SHALL re-read that file under the lock and decide against the locked row

#### Scenario: No transaction spans the walk
- **WHEN** the walk is in progress
- **THEN** the pass SHALL hold no open database transaction

#### Scenario: The advisory lock stays first
- **WHEN** the mutating transaction begins
- **THEN** its first lock-taking statement SHALL be the generation advisory lock

### Requirement: An unchanged file SHALL be skipped only when its recorded stat matches, and the stat SHALL describe the bytes that were hashed
`notes_metadata` SHALL record, per row, the `(size, mtime_ns, ctime_ns, inode)` of the file whose bytes produced `content_hash`. The tuple SHALL be taken from the file descriptor that was read, before the read. The four fields SHALL be all NULL or all non-NULL.

Racy recency SHALL be measured against the wall-clock instant taken immediately before that pre-read `fstat` (`t_start`), not against the end of the read or hash. A stat whose modification or change time is at or after `t_start − 2 s`, including any time later than `t_start`, SHALL be recorded as NULL.

An index pass SHALL skip reading and hashing a discovered file only if all of the following hold:
- `INDEX_STAT_SHORTCUT` is enabled;
- the pass is not a full-hash pass and not a re-derive;
- the row's extraction marker is current;
- the row's recorded stat is non-NULL;
- the file's current stat, following a leaf symlink as the read does, equals the recorded stat in all four fields.

In every other case the file SHALL be read and hashed exactly as before.

When a file is re-read and its hash is unchanged but its stat differs from the row's, or the row's stat is NULL, the pass SHALL update the row's stat with a conditional write that names the row's id, path and content hash. `move_note` SHALL set the moved row's stat to NULL.

#### Scenario: An unchanged file is not read
- **WHEN** a file's current stat equals its row's recorded stat and no bypass condition applies
- **THEN** the pass SHALL not open the file and SHALL treat it as unchanged

#### Scenario: A touched file is re-read and its stat refreshed
- **WHEN** a file's modification time changes but its bytes do not
- **THEN** the pass SHALL read and hash it, find the hash unchanged, write nothing but the new stat, and skip it on the following pass

#### Scenario: Every bypass reads
- **WHEN** the row's stat is NULL, the extraction marker is stale, the pass is a re-derive or full-hash pass, or the shortcut is disabled
- **THEN** the file SHALL be read and hashed

#### Scenario: A racy stat is not trusted
- **WHEN** a file's modification or change time is within 2 seconds before the instant the pass took before its pre-read `fstat`, or later than that instant
- **THEN** its row's stat SHALL be recorded as NULL, so the next pass reads it

#### Scenario: A slow read does not launder a fresh timestamp
- **WHEN** a file's timestamp is fresh at read start, a writer rewrites already-read bytes in the same timestamp tick without changing the size, and the read then takes longer than 2 seconds to complete
- **THEN** the row's stat SHALL still be recorded as NULL, and the next pass SHALL read the file and index the rewritten bytes

#### Scenario: A retargeted symlink is re-read
- **WHEN** a discovered `.md` symlink is repointed at a different file
- **THEN** the stat comparison SHALL see the new target's inode and the pass SHALL read it

### Requirement: A full-hash pass SHALL bound every edit the stat shortcut can miss
For each scope, a full-hash pass SHALL run:
- on the first pass after process start;
- whenever `INDEX_FULL_HASH_INTERVAL_HOURS` (default 24, at least 1) have elapsed since that scope's last **successful** full-hash pass (the interval is when the pass becomes due, not a completion guarantee);
- whenever an operator triggers a reindex from the panel.

A full-hash pass is successful only when its scan transaction commits and every discovered file was read and hashed (no skipped path). A full-hash pass that aborts, is refused, is cancelled, or commits with any skipped path SHALL leave the scope due, and every following pass for that scope SHALL be a full-hash pass until one commits. Incomplete verification SHALL NOT postpone outstanding backstop work by another interval.

A full-hash pass SHALL read and hash every discovered file regardless of recorded stats. It SHALL also run the exclusion reconciliation sweep regardless of that sweep's gate.

The architecture note SHALL list every known way a file's content can change while its `(size, mtime_ns, ctime_ns, inode)` does not, and the bound on each. The consequence SHALL be declared as an accepted limitation, as two separate bounds:
- **Detection.** Such an edit SHALL be detected (its new `content_hash` committed, making keyword search current and vector results mark the note stale) no later than the scope's next successful full-hash pass.
- **Semantic convergence.** Re-embedding follows under the existing per-tenant budgets, provider availability and pause flag, and MAY take further passes.

#### Scenario: The first pass after a restart hashes everything
- **WHEN** the process starts and runs its first pass for a scope
- **THEN** every discovered file in that scope SHALL be read and hashed

#### Scenario: A missed edit is caught by the backstop
- **WHEN** a file's bytes change while all four recorded stat fields stay equal
- **THEN** the change SHALL be detected and its new hash committed no later than the next successful full-hash pass

#### Scenario: A failed backstop is retried, not postponed
- **WHEN** a scope's full-hash pass aborts before committing
- **THEN** that scope's next pass SHALL again be a full-hash pass

#### Scenario: The interval is honoured
- **WHEN** `INDEX_FULL_HASH_INTERVAL_HOURS` have elapsed since a scope's last full-hash pass
- **THEN** that scope's next pass SHALL be a full-hash pass

### Requirement: An unchanged chunk SHALL reuse its stored vector only when that vector provably belongs to the current generation
`embed_note` SHALL reuse a stored vector of the note for a chunk whose text is byte-identical to the stored chunk's text, and send only the remaining chunks to the provider, only if all of the following hold, and SHALL otherwise embed every chunk as before:
- **Lookup transaction.** The lookup of stored `(id, chunk_text, embedding)` rows and of the stored embedding fingerprint runs in a read-only transaction that is committed before any provider call.
- **Fingerprint.** The stored embedding fingerprint is present and equal to the current one. An absent fingerprint SHALL disable reuse.
- **Rows survive.** Under the generation lock, after the provider call and before certification, every reused row still exists with the same chunk text. Otherwise the attempt SHALL certify nothing, write nothing and report `GENERATION_MISMATCH`.

The cardinality check SHALL apply to the chunks actually sent. The note SHALL be certified only when every requested chunk has a vector, whether reused or fresh. After certification, all of the note's rows SHALL be deleted and re-inserted in document order.

A note whose chunks are all reusable SHALL make no provider call. It SHALL NOT be counted as a provider attempt, even when the under-lock check fails and it reports `GENERATION_MISMATCH`, and it SHALL debit no chunk budget.

#### Scenario: An append re-embeds only the new tail
- **WHEN** a note's content is extended so that every earlier chunk's text is unchanged
- **THEN** only the new chunks SHALL be sent to the provider and the note SHALL be certified with every chunk present

#### Scenario: No fingerprint, no reuse
- **WHEN** the stored embedding fingerprint is absent or differs from the current one
- **THEN** no stored vector SHALL be reused

#### Scenario: A reset during the provider call defeats reuse
- **WHEN** a reset deletes the note's stored vectors while the provider call for the remaining chunks is in flight
- **THEN** the attempt SHALL certify nothing and write nothing, and SHALL not deadlock with the reset

#### Scenario: Nothing to send
- **WHEN** every chunk of a changed note has a byte-identical stored chunk
- **THEN** no provider call SHALL be made, the note SHALL be certified, and the pass's attempt count and chunk budget SHALL be unchanged

#### Scenario: An all-reuse note loses a reset race without an attempt
- **WHEN** every chunk of a note is reusable and a reset deletes the stored rows between the lookup and the under-lock check
- **THEN** the outcome SHALL be `GENERATION_MISMATCH`, nothing SHALL be written, and the pass's attempt count SHALL be unchanged

## MODIFIED Requirements

### Requirement: Exclusion-pattern changes reconcile on the next completed embed pass

After processing the hash-mismatch backlog, an embed pass SHALL run a reconciliation sweep for a scope when the sweep is due. The sweep covers rows whose certification is current (`embedded_content_hash IS NOT DISTINCT FROM content_hash`, owner-scoped) and checks them against the *current* `EMBEDDING_EXCLUDE_PATTERNS`:
- a row whose path matches a pattern and still has vectors SHALL have them removed;
- a row whose path matches no pattern and has none SHALL be re-embedded from its verified bytes.

Every reconciliation write SHALL go through the certified predicate (`id + content_hash + file_path`, stamp before delete), with a per-note commit. A row that fails certification SHALL be rolled back and left for a later pass, never patched by id.

Convergence is defined for a **completed** sweep: one that visited every selected row without pause or error. After it, every certification-current row satisfies "vectors exist iff the current configuration includes it", with three defined exceptions:
- a row whose cleaned content produces zero chunks is correct with zero vectors and SHALL NOT be rewritten;
- a row whose on-disk bytes no longer hash to its `content_hash` SHALL be skipped (the backlog owns it next pass);
- a row whose provider call fails SHALL be left unstamped where the stamp would have been new, and retried on a later pass.

A sweep interrupted by the pause flag SHALL stop between notes, and a later pass SHALL run a fresh sweep from the start. Per-note commits make re-visiting already-repaired rows a no-op.

**When the sweep is due.** The sweep is due for a scope on a pass if either of these holds:
- the pass is a full-hash pass (process start, every `INDEX_FULL_HASH_INTERVAL_HOURS`, or an operator reindex);
- the scope has no record of a **clean** completed sweep under a fingerprint equal to the SHA-256 of the current `EMBEDDING_EXCLUDE_PATTERNS`.

A clean completion is one with:
- no pause and no budget stop;
- no exception, no `StaleCertification` and no read failure;
- no provider failure;
- **no row skipped because its bytes no longer hash to its `content_hash`.**

Rows left alone for zero chunks do not prevent a clean completion. A hash-mismatch skip does, because the backlog is not guaranteed to select that row later: an edit that is undone before the next scan restores a matching hash and certification. The record SHALL be held in process memory, set only by a clean completion, and cleared for a scope on a re-derive and by the in-process reset paths. A sweep that is not clean SHALL leave the scope due, so the next pass sweeps again.

#### Scenario: Adding a pattern removes existing vectors

- **WHEN** a note was embedded, its certification is current, and the operator then adds a pattern matching its path and restarts
- **THEN** the next completed embed pass SHALL certify the row (`id + content_hash + file_path`) and delete its vectors
- **AND** the note SHALL stop appearing in semantic search after that pass

#### Scenario: Removing a pattern restores vectors

- **WHEN** a note was stamped by the exclusion branch (certified, zero vectors) and the operator then removes the pattern that excluded it and restarts
- **THEN** the next completed embed pass SHALL re-read the note's bytes beneath the pass's pinned root, verify they hash to the row's `content_hash`, and embed it through the certified path
- **AND** the note SHALL appear in semantic search after that pass

#### Scenario: A concurrent move defeats the reconciliation write, not the vault

- **WHEN** the reconciliation decides about a row and the row's `file_path` changes before the certifying UPDATE commits
- **THEN** the certification SHALL match no row, the note's reconciliation SHALL be rolled back, and no vector SHALL be deleted or written on the strength of the stale decision

#### Scenario: A genuinely empty note is not rewritten every pass

- **WHEN** an included note's cleaned content produces zero chunks and its certification is current
- **THEN** the reconciliation SHALL write nothing for it

#### Scenario: Bytes that no longer match the row are left to the backlog

- **WHEN** the reconciliation reads a row's bytes and they do not hash to the row's `content_hash`
- **THEN** the reconciliation SHALL write nothing for that row
- **AND** the ordinary backlog SHALL select the row on a later pass once the scan has upserted its new hash

#### Scenario: A pause stops the sweep between notes and the next pass converges

- **WHEN** the pause flag is set while a reconciliation sweep is mid-way
- **THEN** the sweep SHALL stop before the next note, already-repaired rows SHALL stay repaired (per-note commits), and the next unpaused pass SHALL run a fresh sweep that completes the remainder

#### Scenario: A clean sweep is not repeated every tick

- **WHEN** a scope's sweep completed cleanly under the current patterns and the next pass is not a full-hash pass
- **THEN** that pass SHALL not run the sweep for the scope

#### Scenario: A sweep with a provider failure runs again

- **WHEN** a sweep's re-embed of an included note fails at the provider
- **THEN** the sweep SHALL not be recorded as clean and the next pass SHALL sweep that scope again

#### Scenario: An edit undone between scan and sweep does not hide a note
- **WHEN** an excluded note's pattern is removed and the process restarts, the scan reads content A, the note is saved as B before the sweep reaches it (so the sweep skips it on a hash mismatch), and the note is restored to A before the next scan
- **THEN** that sweep SHALL NOT be recorded as clean, and the next pass SHALL sweep the scope again and embed the note

#### Scenario: The backstop sweeps regardless

- **WHEN** a pass is a full-hash pass
- **THEN** the sweep SHALL run for every scope the pass serves, whatever the in-memory record says

### Requirement: A many-chunk note completes, and certifies only on full coverage

The Ollama embedding batch SHALL have no aggregate deadline. Its liveness bound is the per-request timeout: 30 s per provider request, where each request carries at most `OLLAMA_EMBED_BATCH_SIZE` inputs, a fixed size independent of the note. So a note cannot be structurally unable to finish while every request is individually healthy. A note SHALL be certified only when every one of its **requested** chunks has a vector, whether produced by the provider or reused under the chunk-reuse requirement. No partial coverage of the requested chunks may ever be stamped complete.

**"Requested chunks" is the bounded chunk list**, not every chunk the note's text could yield. The chunker caps a note at `MAX_CHUNKS_PER_NOTE` in document order, and a capped note is certified on full coverage of that capped list. Certification is what makes the cap safe. A capped note left uncertified would be re-selected by the backlog on every pass for ever, and would re-perform every provider call it already made: exactly the never-finishing note that removing the aggregate deadline exists to prevent. The degradation is declared on the row and in every result that names the note; it is not expressed by withholding the stamp.

**No aggregate deadline SHALL be reintroduced in place of the cap, and the batch size SHALL NOT be made proportional to the note.** The cap bounds a *count*, deterministically, and says so on the row. A time budget fires on a note whose chunks are all individually healthy, certifies nothing, and repeats every tick. A proportional batch size would reintroduce the same boundary one size class up.

#### Scenario: A giant note eventually embeds and stops being retried

- **WHEN** a note produces more chunks than the former fixed 300 s deadline allowed at normal provider latency, and fewer than the chunk cap
- **THEN** the embed pass SHALL process all of its chunks, certify it, and not select it again while its content is unchanged

#### Scenario: A hung provider still fails fast

- **WHEN** the Ollama embedding provider stops responding mid-batch (the OpenAI provider keeps its own pre-existing contract: per-request HTTP timeout with bounded retries)
- **THEN** the in-flight request SHALL time out at the per-request timeout and the note SHALL remain uncertified

#### Scenario: Partial coverage is never certified

- **WHEN** the provider returns fewer vectors than the chunks it was sent for a note
- **THEN** the note SHALL NOT be certified and its previous vectors SHALL remain in place

#### Scenario: A capped note is certified on full coverage of its capped list

- **WHEN** a note yields more chunks than the cap and every one of the first N chunks has a vector
- **THEN** the note SHALL be certified
- **AND** it SHALL NOT be selected by the backlog again while its content is unchanged

#### Scenario: A capped note with one chunk missing is not certified

- **WHEN** a note is capped at N chunks and only N-1 of them have a vector
- **THEN** the note SHALL NOT be certified and its previous vectors SHALL remain in place

### Requirement: Embedding completion has exact cardinality
The system SHALL accept a provider's answer only when it contains exactly one vector for every chunk **sent** to the provider, and SHALL certify a note only when every **requested** chunk has a vector — returned by the provider for this note or reused under the chunk-reuse requirement — where the requested chunks are the chunks the bounded chunker produced for that note. It SHALL record an empty or fully-cleaned note as current with zero vectors. A provider answer that returns the wrong number of vectors, and a provider call that raises, SHALL each be a **distinct outcome** from that zero-chunk certification: neither certifies, neither counts as a note the pass embedded, and both count as failures of the pass.

The three used to be one value. `embed_note` returned `0` for a note that cleaned to zero chunks *and was certified*, for a provider exception it swallowed, and for a cardinality mismatch — and the caller incremented its embedded count after all three. A total provider outage therefore produced a pass record reading `notes_embedded = N, error = NULL`, which is the record a healthy pass writes, with a positive count.

Each failing outcome SHALL carry a **bounded, structured description of what went wrong** — the exception class and a message truncated at the source, the number of chunks sent, and for a cardinality mismatch the number of vectors received — because the pass's own record of the failure is built from it and there is no exception left for the caller to inspect. The message SHALL be truncated where it is captured rather than where the run record is written: the run record's total error budget is shared with the pass's stage labels, and one untruncated provider message can evict them.

#### Scenario: Provider returns too few vectors

- **WHEN** the provider returns fewer embeddings than the chunks it was sent
- **THEN** the note SHALL NOT be marked current
- **AND** previously valid embeddings SHALL remain intact
- **AND** the outcome SHALL be reported to the pass as a failure, not as an embedded note
- **AND** the failure description SHALL name both the sent chunk count and the received vector count

#### Scenario: Note has no embeddable chunks

- **WHEN** cleaning and chunking produces zero chunks
- **THEN** the note's embedded content hash SHALL be marked current
- **AND** the note SHALL have zero embedding rows
- **AND** the outcome SHALL be reported to the pass as a note it embedded, not as a failure

#### Scenario: A provider failure is not a zero-chunk certification

- **WHEN** the embedding provider raises for a note whose cleaned content produces at least one chunk
- **THEN** the outcome reported to the pass SHALL be distinguishable from the zero-chunk certification above
- **AND** the note SHALL NOT be certified, so a later pass selects it again
- **AND** the failure description SHALL carry the exception's class name and a bounded message

#### Scenario: A long provider message cannot crowd out the pass record

- **WHEN** the provider raises with a message longer than the per-failure bound
- **THEN** the captured message SHALL be truncated to that bound before it reaches the pass record

#### Scenario: Cardinality is exact over the capped chunk list

- **WHEN** a note produces more chunks than the per-note chunk cap and every one of the first N chunks has a vector
- **THEN** the note SHALL be certified, because the requested chunks are the capped list
- **AND** a provider answer returning fewer vectors than the chunks sent SHALL still be refused

#### Scenario: Reused and fresh vectors together cover the note

- **WHEN** some of a note's requested chunks reuse stored vectors and the provider returns exactly one vector for each remaining chunk sent
- **THEN** the note SHALL be certified with one row per requested chunk, in document order
