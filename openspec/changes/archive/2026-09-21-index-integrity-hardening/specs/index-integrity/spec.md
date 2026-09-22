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

### Requirement: The unverified ancillary passes do nothing for a user whose provenance is not settled
The one-shot link backfill and the keyword-vector rebuild SHALL each run, for a given user, only when that user's provenance is recorded and the classification for the assigned root at that moment is **same assignment**. For any other classification they SHALL skip that user, SHALL write no row for that user, and SHALL log the skip once, leaving the work to a later pass once the scan has settled the provenance.

The skip SHALL be **per user**, not global: a user whose provenance is unsettled SHALL NOT prevent these passes from running for every other user.

**One narrow exception: the operator-invoked keyword rebuild that records a global configuration fingerprint.** That operation asserts, in one stored row, that *every retained row in the database* was rebuilt under the current text-search configuration — a claim that cannot be established one user at a time. When it runs, a scope it must skip SHALL abort the whole operation: no fingerprint is recorded, every scope's rebuild is rolled back, and the skipped scope and its reason are named to the operator.

The exception SHALL be read narrowly, and its three limits are what keep it from swallowing the rule:

- It applies **only** to that operator-invoked, fingerprint-recording operation. The one-shot link backfill is untouched, and no pass on the periodic loop is covered.
- It does **not** weaken the per-user gate itself. The skipped user still gets nothing written — which is exactly what the rule above demands — and the gate is still computed by the same classification function.
- The rule's purpose is preserved. What "per user, not global" protects against is one tenant's unsettled provenance blocking another tenant's *ongoing* indexing; this operation is a one-shot an operator invoked, not the loop that keeps the index fresh, and it makes no claim about any user until it can make the claim about all of them.

**The maintenance operation SHALL be able to rebuild an inactive owner's scope.** Eligibility here is a question about *retained rows*, not about which users the periodic pass serves: an inactive user's rows are as present in the index, and as returnable by keyword search, as anyone's. The operation SHALL therefore resolve that owner's assigned vault path directly and read-only, within the operation, and pin it as any file-reading pass pins a root. It SHALL NOT widen the active-user root resolution or the active-user cache to do so. A scope with **no** assigned vault path, or one whose path cannot be pinned, remains a skip — and the provenance gate applies to an inactive owner exactly as it applies to an active one.

The classification SHALL be computed by the same function the scan uses, so that "settled" cannot come to mean two different things in two places.

**The embedding pass is deliberately not among them**, and the reason is stated in "The embedding pass is not gated on provenance, because it verifies every hash it certifies" below: it is the only one of the three that binds what it writes to the content the metadata row records, so it is safe by construction against the root mixing this gate exists to prevent, and gating it is the one gate whose cost is unbounded.

These two passes read vault files and write rows the provenance is a claim about — link rows and keyword vectors — with **no verification of any kind** that the bytes they read are the bytes the row they write against describes. They cannot assume the scan settled the provenance a moment ago: a user whose notes contain no links leaves the link backfill eligible on every startup, and a reassignment can commit between the scan and either of them. Allowing them to write under an unresolved provenance is what lets a link row extracted from one root be committed against a metadata row from another.

Verification is not merely unimplemented for the link backfill: a link row's *resolution* is a function of the whole set of notes under a root rather than of one file's bytes, so no per-file check could license it. The keyword-vector rebuild could in principle be verified the way the embedding pass is, and is still gated, because it records nothing that would let a later pass notice a vector built from foreign bytes — there is no keyword analogue of `embedded_content_hash`.

Skipping costs those two passes nothing even for a user whose provenance never settles, which is why it is the specified outcome rather than a per-file content check. The re-derive branch does both passes' work itself on every pass: it deletes and re-extracts every one of that user's link rows, and it rewrites every note's keyword vector, because it treats every note as changed. A delayed link backfill of a table the re-derive is filling anyway, and a delayed rebuild of vectors the re-derive is rewriting anyway, cost latency and write nothing wrong.

#### Scenario: An unsettled user is skipped by both gated passes

- **WHEN** a user has no recorded provenance, or the classification for their assigned root is anything other than same assignment, and the link backfill or the keyword-vector rebuild runs
- **THEN** that pass SHALL write no `note_links` or keyword-vector row for that user
- **AND** SHALL log the skip once

#### Scenario: The skip does not stop the pass for other users

- **WHEN** one user's provenance is unsettled and another user's is settled, and a gated pass runs
- **THEN** the settled user's work SHALL be performed in that same pass

#### Scenario: The fingerprint-recording rebuild is the exception

- **WHEN** the operator-invoked keyword rebuild that records the configuration fingerprint reaches a retained scope it must skip
- **THEN** it SHALL record no fingerprint, SHALL roll back every scope it had rebuilt, and SHALL name the skipped scope and its reason
- **AND** the skipped scope SHALL still have had no row written for it

#### Scenario: The exception does not reach the link backfill

- **WHEN** the one-shot link backfill encounters an unsettled user alongside settled ones
- **THEN** it SHALL skip that user and complete for the others, exactly as before

#### Scenario: An inactive but assigned owner is rebuilt

- **WHEN** the fingerprint-recording rebuild reaches a scope whose owner is inactive, has an assigned vault path, and whose provenance classification is same assignment
- **THEN** that scope SHALL be rebuilt
- **AND** the operation SHALL resolve and pin that owner's assigned path itself, without changing which users the periodic pass serves

#### Scenario: An unassigned owner remains a skip

- **WHEN** a retained scope's owner has no assigned vault path
- **THEN** it SHALL be a skip with its own named reason
- **AND** in the fingerprint-recording rebuild that skip SHALL abort the operation

#### Scenario: A reassignment between the scan and a later pass writes nothing

- **WHEN** the scan settles a user's provenance and the user is then reassigned to a different vault before the link backfill runs
- **THEN** the link backfill SHALL classify that user as reassigned rather than same assignment, and SHALL write no link row for them
- **AND** the next scan SHALL perform the reconciliation for that user

#### Scenario: A settled user proceeds unchanged

- **WHEN** a user's recorded provenance matches the assigned root and a gated pass runs
- **THEN** it SHALL do exactly the work it does today

## ADDED Requirements

### Requirement: A provider failure is excluded from the pass's embedded count and marks the run
An embed pass SHALL count into `notes_embedded` only the notes it actually certified, and SHALL route every per-note provider failure — a raised provider call and a returned-vector-count mismatch alike — through the pass's failure accumulator, so that the pass record carries a non-null `error` summarising them beside a truthful `notes_embedded`. The summary SHALL name the failure count, the attempted count, and the class and bounded message of the first failure.

**The exclusion-reconciliation sweep SHALL report into the same accumulator as the hash-mismatch backlog.** On a fully-indexed vault the backlog is empty and the sweep is the only stage making provider calls, so a sweep that swallowed its own failures would reproduce this defect in the one code path a backlog-only fix does not touch.

**`attempted` SHALL be incremented exactly once per note for which an embedding provider call is issued, at that call site and nowhere else.** It SHALL NOT be initialised from the size of the backlog the pass selected, and it SHALL NOT count rows the pass or the sweep decided about without calling the provider.

**"At that call site" means at the point of issuance, not at the point a result is read.** The certification runs after the provider call and can raise — the row moved under it — and a database error can escape anywhere between the two, so a pass that increments from a returned value counts nothing for a note whose call was made, whose provider time was spent, and whose outcome never came back as a result. A stage in which every note lost that race reported an attempted count of zero while consuming the whole stage. The same point SHALL debit the per-tenant chunk budget, and the note SHALL count as having reached a note boundary, so that a tenant losing that race on every note still becomes budget-exhaustible.

That single rule determines every case, and the cases SHALL NOT be enumerated as independent exceptions that could drift apart from it:

- a note whose cleaned content produces no chunks is certified without a provider call, so it counts into `notes_embedded` and **not** into `attempted`;
- a sweep row whose stored vectors already agree with the current configuration is decided without a call, so it is not an attempt — the sweep scans every certification-current row in the scope, so counting those would render three failures out of three calls as "3 of 16,700";
- a note skipped by an exclusion pattern, a note whose bytes no longer hash to its row, a note left behind by a pause or a budget stop, and a certification that matched no row all issue no call for that note, so none of them moves the denominator.

None of those SHALL be counted as failures either: each is a deliberate decision rather than something that went wrong.

**The failure summary SHALL NOT present a ratio it cannot support.** Failures that reach the accumulator without a provider call having been issued — a database error around the call, a rollback that itself failed — move the failure count and not the attempted count, so a pass that never reached the provider would otherwise report "1 of 0". "Of 0" asserts that no call was made, which makes the whole line read as a broken counter and costs the operator their trust in the number that reports a real outage. When the failures outnumber the calls, the summary SHALL state the failure count and the call count as two separate facts; otherwise it SHALL keep the ratio.

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

#### Scenario: A certification that raises after the provider call is still an attempt

- **WHEN** a note's provider call returns and its certification then raises because the row moved
- **THEN** that note SHALL be counted as an attempt and its submitted chunks SHALL be debited from the tenant's budget
- **AND** it SHALL NOT be counted as embedded and SHALL NOT be counted as a failure
- **AND** the note SHALL count as having reached a note boundary

#### Scenario: A repairing sweep counts the notes it certified

- **WHEN** a pass's backlog is empty and the reconciliation sweep re-embeds and certifies two notes whose exclusion pattern was removed, alongside rows that need no call
- **THEN** `notes_embedded` on that pass's record SHALL be 2
- **AND** the attempted count SHALL be 2

#### Scenario: A zero-chunk note is embedded but not attempted

- **WHEN** a pass selects 400 backlog rows of which 50 clean to zero chunks and the remaining 350 all embed successfully
- **THEN** `notes_embedded` SHALL be 400
- **AND** the attempted count SHALL be 350, because 50 of the notes issued no provider call

#### Scenario: One failing note among many does not suppress the rest

- **WHEN** one note's provider call fails and the remaining notes embed successfully
- **THEN** `notes_embedded` SHALL count only the successful notes
- **AND** the record's `error` SHALL name one failure out of the attempted count
- **AND** the pass SHALL continue to the end of the backlog

#### Scenario: A failure with no provider call is not rendered as a ratio

- **WHEN** a pass records a failure for a note whose provider call was never issued
- **THEN** the summary SHALL name the failure count and the number of provider calls issued as separate facts
- **AND** it SHALL NOT read as "of 0"

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

**A stored cursor value the pass cannot use SHALL be logged once and ignored, and the cycle SHALL begin at the first user in the deterministic order.** The value lives as text in a key/value table, so it can be non-numeric, negative, or larger than any live id through drift, a hand-edited row, or a downgrade. This disposition is deliberately the opposite of the settings fingerprints': a cursor is scheduling state whose worst consequence is an order, while a fingerprint is a claim about what the stored rows *are* and whose worst consequence is a permanently wrong answer. Failing closed on a stray character in a bookkeeping row would stop every tenant's indexing to protect nothing. An out-of-range numeric value needs no separate rule — "the smallest id strictly greater than N" selects nothing and wraps to the first — but it SHALL reach the same outcome rather than raising.

#### Scenario: A malformed cursor does not fail the pass

- **WHEN** the stored rotation cursor is not a valid user id — non-numeric, negative, or otherwise unusable
- **THEN** the pass SHALL log it once and begin the cycle at the first user in the deterministic order
- **AND** the pass SHALL complete normally and SHALL NOT raise

#### Scenario: An out-of-range cursor wraps

- **WHEN** the stored cursor is a number larger than every active user id
- **THEN** the cycle SHALL begin at the first user in the deterministic order

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

**The chunk budget SHALL be debited by the chunks a note *submitted* to the provider, never by the chunks it stored.** Every provider call debits what it sent, whatever came back: a call that raised and a call that returned the wrong number of vectors debit exactly as a successful one does. A budget debited by stored chunks is not debited at all when the provider fails, so a tenant whose notes all fail would consume the whole pass, every pass, without ever reaching its bound — the starvation this requirement exists to stop, reappearing inside it. The wall-clock budget does not cover that case, because an operator may disable it and keep only the chunk budget.

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

#### Scenario: A failing provider still debits the budget

- **WHEN** the wall-clock budget is disabled, a user's notes each submit chunks to the provider, and every one of those calls fails
- **THEN** the chunk budget SHALL be debited by the chunks each call submitted
- **AND** the user's embedding SHALL stop at a note boundary once the budget is exhausted, rather than continuing for the whole pass

#### Scenario: A certification race still debits the budget

- **WHEN** the wall-clock budget is disabled and every one of a user's notes has its certification raise after the provider call returned
- **THEN** the chunk budget SHALL be debited by the chunks each of those calls submitted
- **AND** the user's embedding SHALL stop at a note boundary once the budget is exhausted, rather than continuing for the whole pass

#### Scenario: A cardinality mismatch debits the budget

- **WHEN** a provider call returns the wrong number of vectors for a note
- **THEN** the chunks that call submitted SHALL be debited from the budget

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
