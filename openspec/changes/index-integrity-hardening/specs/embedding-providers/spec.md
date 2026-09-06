## ADDED Requirements

### Requirement: Stored settings fingerprints gate startup, and both fail closed
The system SHALL record, once per derived kind, a **fingerprint of the configuration those rows were generated under**, and SHALL compare it at startup against the fingerprint of the configuration the process is about to run with, refusing to start on a mismatch of either. Two fingerprints SHALL be kept: one for embeddings, covering the active provider, the active model, the dimension count, the chunk size, the chunk overlap and the per-note chunk cap; and one for keyword vectors, covering the configured text-search config list.

The fingerprints SHALL be **global**, one row each, and SHALL NOT be recorded per embedding row or per note. The settings they describe are global, a per-row copy would be one identical string per chunk, and its only use would be a lazy per-note re-embed — which is precisely the design that leaves vectors from two models coexisting in one index for the whole migration window. Cosine distance between two vector spaces is meaningless, so a partially migrated index answers wrongly for longer than a refused startup does. The remedy is the wipe-and-re-embed the reset workflow already performs.

The embedding fingerprint's `model` field SHALL be the model of the **active** provider, selected by the same branch that selects the provider itself, so that reading the inactive provider's model — the exact defect this guard exists to catch — cannot happen in one place while the provider is chosen in another.

**The per-note chunk cap SHALL be part of the embedding fingerprint.** It determines what a note's stored vector set *is*: at one cap a long note holds N chunks and its tail is absent, and at another it holds a different set. Lowering it leaves rows beyond the new bound; raising it leaves rows that are silently incomplete against the new policy and that nothing will ever re-select, because their content hash still matches. Including it makes a cap change a declared reset rather than a permanent, invisible under-embedding.

**Endpoint identity SHALL be excluded from the fingerprint** — neither the local provider's URL nor the hosted provider's base URL is part of it. Repointing at another host or proxy is an infrastructure change that usually serves the identical artifact, and including it would demand a full vault re-embed for it. The consequence SHALL be recorded as an accepted limitation rather than partially mitigated: **the fingerprint records the configuration, not the model artifact**, so replacing the weights behind a mutable model tag, or pointing at a host serving different weights under the same tag, mixes vector spaces undetected. No value available to the process distinguishes those cases, and a probe of the endpoint would have to trust the endpoint it is checking. The operator rule that stands in its place SHALL be documented beside the model keys: a change of model artifact requires the embedding reset, and nothing will detect it if that step is skipped.

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

- **WHEN** the provider is one of the two supported backends and the other backend's model setting carries a different value
- **THEN** the fingerprint SHALL name the active provider's model
- **AND** changing only the inactive backend's model setting SHALL NOT cause a mismatch

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
- **On the embedding path that acquisition SHALL live in the function that owns both statements.** The provider call and the certification are two statements of one function; no caller sits between them, so the lock and the fingerprint re-read SHALL be performed there rather than by the pass that invokes it. A mismatch SHALL be reported to the pass as its own outcome, distinct from a provider failure and from a successful embed: it SHALL NOT count as a note the pass embedded, SHALL NOT count as a failure — nothing went wrong with the provider — and SHALL count as an attempt, because a provider call was issued.
- **Every writer of a note's keyword vector SHALL take the same lock and make the same re-read**, including the incremental index pass. A rebuild can otherwise complete and record its fingerprint while an old-configuration pass writes one note's keyword vector under the previous configuration — and because a keyword vector is rewritten only when a note's content hash changes, that row then stays on the previous configuration indefinitely behind a fingerprint claiming otherwise. A refusal there SHALL abort that pass with nothing committed, as a floor failure already does.
- **The lock SHALL be transaction-scoped, never session-scoped**, so it is released by commit or rollback and a crashed pass cannot strand it in a pooled connection.
- **The lock SHALL be acquired before any row or table lock** in every transaction that takes it, so that one ordering holds everywhere and the new lock cannot close a cycle with the row locks the pass, the panel and the index-discard branch already contend for.
- **That ordering is a property of the transaction, not of the statement that needs the fingerprint.** A transaction that will write any configuration-dependent derived row SHALL acquire the lock and re-validate the fingerprint **before its first row-locking mutation**, and the implementation SHALL audit every mutation earlier in that transaction rather than reason backwards from the write that consumes the fingerprint. The index pass is one transaction that mutates note metadata — upserts, move updates, prunes, link rows, certification invalidation — long before it reaches its keyword-vector write; acquiring the lock at that write would leave the pass holding row locks while it waits for the lock, and the rebuild holding the lock while it waits for those rows, which is a deadlock and a direct violation of the ordering rule. The acquisition therefore belongs at the head of that transaction.

Holding the lock for the duration of a long pass is accepted: the maintenance operations then **wait** for an in-flight pass rather than interleaving with it, which is the required behaviour, and those operations SHALL NOT defeat it with a short lock timeout — **nor with any other timeout that applies to the acquisition**. The connection carries a statement timeout and the advisory-lock acquisition is a statement, so every path whose contract is "it waits" SHALL lift that timeout for the acquisition alone and restore the previous value once the lock is held. Lifting it beforehand SHALL NOT be treated as a violation of the ordering rule: a session-variable assignment takes no row or table lock and is invisible to the lock graph. The waiting side includes the incremental index pass whenever a maintenance operation holds the lock first; only the per-note embedding acquisition keeps the connection's timeout, because that transaction must not sit on a lock for minutes and its mismatch disposition is already to leave the note for a later pass.
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

#### Scenario: A crashed holder does not strand the lock

- **WHEN** a transaction holding the generation lock fails or its connection drops
- **THEN** the lock SHALL be released without operator action, and the next taker SHALL acquire it

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

### Requirement: A configuration knob that invalidates derived rows SHALL name its remedy where it is set
Every configuration key whose change silently invalidates stored derived rows SHALL carry, in the operator-facing configuration example and in the reference documentation, the command that repairs those rows. The embedding model keys — the local provider's model name and the hosted provider's model name — SHALL carry the embedding-reset pointer that the chunk-size, chunk-overlap and provider keys already carry, and the reference documentation's provider-switching section SHALL cover a model change within a single provider and SHALL state the deploy-then-reset ordering.

Documenting the remedy is not made redundant by the startup guard. The guard tells an operator that they have already made the change; the documentation tells them what the change costs before they make it, and it is the only signal for the operator editing the file on a host where the server is not running.

#### Scenario: The model keys carry the reset warning

- **WHEN** an operator reads the configuration example at the local provider's model key or the hosted provider's model key
- **THEN** each SHALL state that changing it after deploy requires the embedding reset workflow

#### Scenario: The reference documentation covers a same-provider model change and the ordering

- **WHEN** an operator reads the documentation section on changing embedding backends
- **THEN** it SHALL state that changing the model within one provider requires the same reset as switching providers
- **AND** it SHALL state that the deploy comes before the reset, and why

#### Scenario: The artifact rule is documented where the model is set

- **WHEN** an operator reads the configuration example or the reference documentation at either model key
- **THEN** it SHALL state that replacing the artifact behind an unchanged model name — re-pulling a mutable tag, or pointing at a host serving different weights — also requires the embedding reset
- **AND** it SHALL state that no startup check detects that case

## MODIFIED Requirements

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
