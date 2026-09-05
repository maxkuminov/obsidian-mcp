## MODIFIED Requirements

### Requirement: Embedding completion has exact cardinality
The system SHALL accept an embedding batch only when it contains exactly one vector for every **requested** chunk, where the requested chunks are the chunks the bounded chunker produced for that note. It SHALL record an empty or fully-cleaned note as current with zero vectors. A batch that returns the wrong number of vectors, and a provider call that raises, SHALL each be a **distinct outcome** from that zero-chunk certification: neither certifies, neither counts as a note the pass embedded, and both count as failures of the pass.

The three used to be one value. `embed_note` returned `0` for a note that cleaned to zero chunks *and was certified*, for a provider exception it swallowed, and for a cardinality mismatch — and the caller incremented its embedded count after all three. A total provider outage therefore produced a pass record reading `notes_embedded = N, error = NULL`, which is the record a healthy pass writes, with a positive count.

Each failing outcome SHALL carry a **bounded, structured description of what went wrong** — the exception class and a message truncated at the source, the number of chunks requested, and for a cardinality mismatch the number of vectors received — because the pass's own record of the failure is built from it and there is no exception left for the caller to inspect. The message SHALL be truncated where it is captured rather than where the run record is written: the run record's total error budget is shared with the pass's stage labels, and one untruncated provider message can evict them.

#### Scenario: Provider returns too few vectors

- **WHEN** the provider returns fewer embeddings than requested chunks
- **THEN** the note SHALL NOT be marked current
- **AND** previously valid embeddings SHALL remain intact
- **AND** the outcome SHALL be reported to the pass as a failure, not as an embedded note
- **AND** the failure description SHALL name both the requested chunk count and the received vector count

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

- **WHEN** a note produces more chunks than the per-note chunk cap and the provider returns one vector for each of the first N chunks
- **THEN** the batch SHALL be accepted, because the requested chunks are the capped list
- **AND** a batch returning fewer vectors than that capped list SHALL still be refused

### Requirement: A many-chunk note completes, and certifies only on full coverage

The Ollama (local, sequential) embedding batch SHALL have no aggregate deadline: its per-chunk timeout (30 s per provider call) is the liveness bound, so a note cannot be structurally unable to finish while every chunk is individually healthy. A note SHALL be certified only when every one of its **requested** chunks produced a vector; no partial coverage of the requested chunks may ever be stamped complete.

**"Requested chunks" is the bounded chunk list**, not every chunk the note's text could yield. The chunker caps a note at `MAX_CHUNKS_PER_NOTE` in document order, and a capped note is certified on full coverage of that capped list. Certification is what makes the cap safe: a capped note that were left uncertified would be re-selected by the backlog on every pass for ever and would re-perform every provider call it already made, which is the never-finishing note the removal of the aggregate deadline exists to prevent. The degradation is declared on the row and in every result that names the note; it is not expressed by withholding the stamp.

**No aggregate deadline SHALL be reintroduced in place of the cap.** The cap bounds a *count*, deterministically, and says so on the row; a time budget fires on a note whose chunks are all individually healthy, certifies nothing, and repeats every tick.

#### Scenario: A giant note eventually embeds and stops being retried

- **WHEN** a note produces more chunks than the former fixed 300 s deadline allowed at normal provider latency, and fewer than the chunk cap
- **THEN** the embed pass SHALL process all of its chunks, certify it, and not select it again while its content is unchanged

#### Scenario: A hung provider still fails fast

- **WHEN** the Ollama embedding provider stops responding mid-batch (the OpenAI provider keeps its own pre-existing contract: per-request HTTP timeout with bounded retries)
- **THEN** the in-flight chunk call SHALL time out at the per-chunk timeout and the note SHALL remain uncertified

#### Scenario: Partial coverage is never certified

- **WHEN** the provider returns fewer vectors than the requested chunks for a note
- **THEN** the note SHALL NOT be certified and its previous vectors SHALL remain in place

#### Scenario: A capped note is certified on full coverage of its capped list

- **WHEN** a note yields more chunks than the cap and every one of the first N chunks produces a vector
- **THEN** the note SHALL be certified
- **AND** it SHALL NOT be selected by the backlog again while its content is unchanged

#### Scenario: A capped note with one chunk missing is not certified

- **WHEN** a note is capped at N chunks and the provider returns N-1 vectors
- **THEN** the note SHALL NOT be certified and its previous vectors SHALL remain in place

## ADDED Requirements

### Requirement: A provider failure is excluded from the pass's embedded count and marks the run
An embed pass SHALL count into `notes_embedded` only the notes it actually certified, and SHALL route every per-note provider failure — a raised provider call and a returned-vector-count mismatch alike — through the pass's failure accumulator, so that the pass record carries a non-null `error` summarising them beside a truthful `notes_embedded`. The summary SHALL name the failure count, the attempted count, and the class and bounded message of the first failure.

**The exclusion-reconciliation sweep SHALL report into the same accumulator as the hash-mismatch backlog.** On a fully-indexed vault the backlog is empty and the sweep is the only stage making provider calls, so a sweep that swallowed its own failures would reproduce this defect in the one code path a backlog-only fix does not touch.

**`attempted` SHALL be the number of backlog rows the pass selected plus the number of reconciliation rows the sweep actually attempted to embed** — the included-and-vectorless rows whose bytes verified and which reached the provider. It SHALL NOT count rows the sweep merely considered: the sweep scans every certification-current row in the scope and decides about almost all of them without a provider call, so counting those would render three failures out of three attempts as "3 of 16,700".

A note skipped by an exclusion pattern, a note skipped because its bytes no longer hash to its row, a note whose cleaned content produces no chunks, a certification that matched no row, and a note left behind by a pause or a budget stop SHALL NOT be counted as failures or as attempts: each is a deliberate decision rather than something that went wrong.

The in-process "last run" heartbeat SHALL remain unaffected by these swallowed per-note failures. The heartbeat answers "is this process's loop alive" and the run record answers "did the work succeed"; a provider outage leaves the first green and the second failed, and collapsing them would change the heartbeat's meaning.

#### Scenario: A total provider outage marks the run as failed

- **WHEN** every note in a pass's backlog fails at the embedding provider
- **THEN** the pass's record SHALL carry a non-null `error` naming the failure count, the attempted count and the first failure's class and message
- **AND** `notes_embedded` on that record SHALL be zero
- **AND** the record SHALL be distinguishable from the record a pass with an empty backlog writes

#### Scenario: A reconciliation-only outage marks the run as failed

- **WHEN** a pass's backlog is empty, the reconciliation sweep attempts to re-embed notes whose exclusion pattern was removed, and every one of those provider calls fails
- **THEN** the pass's record SHALL carry a non-null `error`
- **AND** its attempted count SHALL be the number of notes the sweep actually sent to the provider, not the number of rows it scanned

#### Scenario: One failing note among many does not suppress the rest

- **WHEN** one note's provider call fails and the remaining notes embed successfully
- **THEN** `notes_embedded` SHALL count only the successful notes
- **AND** the record's `error` SHALL name one failure out of the attempted count
- **AND** the pass SHALL continue to the end of the backlog

#### Scenario: Deliberate decisions are not failures

- **WHEN** a pass skips a note because its path matches an exclusion pattern, skips a note whose bytes no longer hash to its row, and stops at a note boundary because the pause flag was set
- **THEN** none of the three SHALL increment the failure count or the attempted count
- **AND** a pass in which nothing else went wrong SHALL record a null `error`

#### Scenario: The heartbeat stays green through the outage

- **WHEN** a periodic pass completes with every note failing at the provider
- **THEN** the in-process last-run heartbeat SHALL record the pass as ok
- **AND** the pass record SHALL record it as failed

### Requirement: Chunking is capped per note, and a capped note is certified and marked
The chunker SHALL produce at most `MAX_CHUNKS_PER_NOTE` chunks for one note, keeping the first N in document order. A note that hits the cap SHALL be a **declared degradation**, not a skip and not a refusal: its first N chunks SHALL be embedded, the note SHALL be certified through the ordinary conditional certification, a durable marker (`notes_metadata.chunks_truncated`) SHALL be set on the row and cleared when a later embed of that note fits under the cap, and one ERROR line SHALL name the path and the cap.

**The ERROR line SHALL be emitted only after the certifying transaction has committed.** Logging it before the commit records a permanent truncation in a bounded, process-lifetime error buffer for a write that may then roll back on a failed certification, sending an operator after a note that was never stored that way.

The ERROR line SHALL NOT name the note's true chunk count, which could only be obtained by the unbounded chunking the cap exists to prevent.

The marker SHALL be a column rather than only a log line, for the reason the link-truncation marker is one: the error buffer is bounded and process-lifetime while the truncated vector set persists, and the vector tools would otherwise answer from a note's head as though it were the whole note.

The cap SHALL bound the emptiness probe the exclusion-reconciliation sweep performs as well, so that "this note produces no chunks" means the same thing in both places.

**The configured chunk overlap SHALL be strictly less than the configured chunk size, validated at startup.** The chunker's step is the chunk size minus the overlap, floored at one character to prevent a non-terminating loop; at equality that floor takes effect and the step becomes one character, so a few kilobytes of ordinary prose produce thousands of chunks and every ordinary note in the vault is silently truncated at the cap while the truncation ERROR fires thousands of times. The floor turns a hang into a quiet catastrophe and is not a substitute for rejecting the configuration. The refusal SHALL name both configured values.

#### Scenario: A capped note keeps its first N chunks and is marked

- **WHEN** a note whose cleaned content would produce more than `MAX_CHUNKS_PER_NOTE` chunks is embedded
- **THEN** exactly `MAX_CHUNKS_PER_NOTE` embedding rows SHALL be written for it, holding the first N chunks in document order
- **AND** `chunks_truncated` SHALL be true on its row and one ERROR line SHALL name the path and the cap
- **AND** the note SHALL be certified, so the next pass does not select it again while its content is unchanged

#### Scenario: A truncation that does not commit is not logged

- **WHEN** a capped note's certification matches no row and its transaction is rolled back
- **THEN** no truncation ERROR line SHALL have been emitted for that attempt

#### Scenario: The marker is cleared when the note fits

- **WHEN** a capped note is edited down to fewer than `MAX_CHUNKS_PER_NOTE` chunks and embedded again
- **THEN** `chunks_truncated` SHALL be false on its row after that pass

#### Scenario: A capped note is not a skip and does not withhold a re-derive's record

- **WHEN** a re-deriving pass processes a note whose chunking was capped and every other discovered file without a skip
- **THEN** the pass SHALL record the provenance of the directory it scanned
- **AND** the capped note SHALL NOT appear in the pass's skip list

#### Scenario: A note under the cap is unaffected

- **WHEN** a note produces fewer chunks than the cap
- **THEN** every chunk SHALL be embedded, `chunks_truncated` SHALL be false, and no ERROR line SHALL be logged for it

#### Scenario: An overlap equal to the chunk size is refused at startup

- **WHEN** the configured chunk overlap equals or exceeds the configured chunk size
- **THEN** the server SHALL refuse to start with an error naming both values
- **AND** it SHALL NOT start with a chunker whose step has collapsed to the floor

### Requirement: The embed pass rotates tenants from a cursor that survives a restart
The multi-tenant index pass SHALL iterate active users in a deterministic order and SHALL begin each cycle at the user following the one recorded in a **persisted** rotation cursor, wrapping around. The cursor SHALL be advanced to a user's id after that user's per-user pass finishes, whether it succeeded or failed, and SHALL be stored in the database rather than in process memory.

The cursor SHALL record a **user id**, never a positional offset: the active-user list changes when a user is added, deactivated or deleted, so an offset points somewhere else on the next cycle, whereas "resume after this id" is well defined whether or not that user still exists.

Persisting it is the requirement, not an implementation detail. Rotating a list the pass re-fetches every cycle with state that resets on restart is a no-op in exactly the case that matters: a deploy or a crash truncates a pass, and the tenants at the tail — the ones the truncated pass never reached — are the ones an in-memory cursor would send to the tail again.

Operator-triggered reindex paths SHALL NOT consume or advance the cursor, so that a panel action cannot move the periodic pass's rotation.

The cursor is scheduling instrumentation: a failure to write it SHALL be logged and swallowed and SHALL NOT fail the pass.

#### Scenario: A truncated pass resumes where it stopped

- **WHEN** a pass serving users in the order A, B, C finishes A and B and the process restarts before C
- **THEN** the first pass after the restart SHALL begin at C

#### Scenario: The cursor survives the tenant it names

- **WHEN** the cursor records a user who is then deleted
- **THEN** the next cycle SHALL begin at the smallest active user id greater than the recorded one, wrapping if none is greater
- **AND** the pass SHALL NOT fail on the missing user

#### Scenario: A complete cycle wraps

- **WHEN** a pass serves every active user without interruption
- **THEN** the cursor SHALL name the last user served and the next cycle SHALL begin at the first user in the deterministic order

#### Scenario: A manual reindex does not move the rotation

- **WHEN** an operator triggers a reindex from the control panel
- **THEN** the rotation cursor SHALL be unchanged by that request

#### Scenario: Recording the cursor cannot fail a pass

- **WHEN** the cursor write raises
- **THEN** the failure SHALL be logged and swallowed, and the pass's own outcome SHALL be unaffected

### Requirement: A per-tenant embed budget is checked only at a note boundary
The embed pass SHALL bound the work it performs for one user in one pass by a configurable chunk budget and a configurable wall-clock budget, and SHALL evaluate that bound **only between notes**, at the same points the pause flag is already checked. It SHALL NOT abandon a note that has already begun embedding.

Mid-note preemption is forbidden because a note is certified only on full coverage of its requested chunks: a note abandoned between chunks is left uncertified, is re-selected by the backlog on the next pass, and re-performs every provider call it already made — a burn that repeats for as long as the note stays over budget and that no pass can ever finish. Bounding at the note boundary means the overrun is at most one note, which the per-note chunk cap has already bounded.

**The bound this provides SHALL be stated as the budget plus one note's embedding time**, and one note's embedding time is bounded by the chunk cap multiplied by the provider's per-call timeout, not by any aggregate deadline. That arithmetic worst case is hours on a provider answering every call at the edge of its timeout, and it is an **accepted limitation**: the alternative is an aggregate deadline, which is the construct that produced a note the pass could never finish and which SHALL NOT be reintroduced.

**This requirement's fairness claim covers the embedding stage only.** The scan and the one-shot link backfill run before it in each user's sequence and are deliberately not budgeted: each is a single transaction over a walk of the vault, so stopping one part-way means either committing a partial derive — which the re-derive completeness rule forbids — or discarding the pass's work, and neither is a cheap bound. This scoping SHALL be documented rather than left implied, so that "one tenant cannot starve another" is not read as a claim about the whole pass.

The pass SHALL process **at least one note** for a user before the budget can stop it, so that a user whose first note alone exceeds the budget still advances by one note per pass rather than never advancing.

The budget SHALL be enforced only when the pass is serving more than one active user scope. With a single scope there is no other tenant for the budget to be fair to, and stopping there would only spread an initial index across several passes.

A budget stop SHALL NOT be recorded as a failure and SHALL NOT be written into the pass record's `error`: it is a deliberate decision of the same class as a pause, and recording it as an error would make a healthy server report the outage signal. It SHALL be logged once per user per pass, and the remaining backlog SHALL remain visible as the pending count the panel reports.

Both the hash-mismatch backlog and the exclusion-reconciliation sweep SHALL draw on the same per-user budget, since both call the embedding provider. A sweep stopped by the budget SHALL behave exactly as a sweep stopped by the pause flag: it stops between notes, already-repaired rows stay repaired, and the next unpaused pass runs a fresh sweep.

#### Scenario: One tenant's backlog does not consume the whole embed stage

- **WHEN** two users are active, the first has a backlog far exceeding the chunk budget, and a pass runs
- **THEN** the first user's embedding SHALL stop at a note boundary once the budget is exhausted
- **AND** the second user's notes SHALL be indexed and embedded in that same pass

#### Scenario: A note in flight is never abandoned

- **WHEN** the budget is exhausted partway through a note's chunks
- **THEN** the pass SHALL finish that note's chunks and certify it
- **AND** the stop SHALL take effect before the next note begins

#### Scenario: A single oversized note still makes progress

- **WHEN** a user's first note alone consumes more than the entire chunk budget
- **THEN** that note SHALL be embedded and certified in that pass
- **AND** the user SHALL advance by one note per pass rather than being blocked indefinitely

#### Scenario: A budget stop is not an error

- **WHEN** a pass stops a user at the budget and nothing else goes wrong
- **THEN** the pass record's `error` SHALL be null and its failure count SHALL be zero
- **AND** one warning SHALL be logged naming the user and the budget

#### Scenario: A single-scope deployment is unbudgeted

- **WHEN** the pass serves exactly one scope — single-user mode, or a multi-user deployment with one active user — and that scope's backlog exceeds the budget
- **THEN** the pass SHALL embed the whole backlog without stopping at the budget

#### Scenario: A budget-stopped sweep converges on a later pass

- **WHEN** the exclusion-reconciliation sweep is stopped by the budget partway through
- **THEN** already-repaired rows SHALL stay repaired
- **AND** the next unbudgeted-or-unexhausted pass SHALL run a fresh sweep that completes the remainder

#### Scenario: The scan is not budgeted

- **WHEN** a user's scan and link backfill run before that user's embed stage in the same pass
- **THEN** neither SHALL be stopped by the embed budget
- **AND** the delay they can impose on a later tenant SHALL be recorded as a declared residual rather than as a bound this requirement provides
