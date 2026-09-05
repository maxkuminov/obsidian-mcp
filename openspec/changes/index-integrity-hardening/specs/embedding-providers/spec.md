## ADDED Requirements

### Requirement: Stored settings fingerprints gate startup, and both fail closed
The system SHALL record, once per derived kind, a **fingerprint of the configuration those rows were generated under**, and SHALL compare it at startup against the fingerprint of the configuration the process is about to run with, refusing to start on a mismatch of either. Two fingerprints SHALL be kept: one for embeddings, covering the active provider, the active model, the dimension count, the chunk size, the chunk overlap and the per-note chunk cap; and one for keyword vectors, covering the configured text-search config list.

The fingerprints SHALL be **global**, one row each, and SHALL NOT be recorded per embedding row or per note. The settings they describe are global, a per-row copy would be one identical string per chunk, and its only use would be a lazy per-note re-embed — which is precisely the design that leaves vectors from two models coexisting in one index for the whole migration window. Cosine distance between two vector spaces is meaningless, so a partially migrated index answers wrongly for longer than a refused startup does. The remedy is the wipe-and-re-embed the reset workflow already performs.

The embedding fingerprint's `model` field SHALL be the model of the **active** provider, selected by the same branch that selects the provider itself, so that reading the inactive provider's model — the exact defect this guard exists to catch — cannot happen in one place while the provider is chosen in another.

**The per-note chunk cap SHALL be part of the embedding fingerprint.** It determines what a note's stored vector set *is*: at one cap a long note holds N chunks and its tail is absent, and at another it holds a different set. Lowering it leaves rows beyond the new bound; raising it leaves rows that are silently incomplete against the new policy and that nothing will ever re-select, because their content hash still matches. Including it makes a cap change a declared reset rather than a permanent, invisible under-embedding.

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

### Requirement: The keyword fingerprint is written only by an operation that rebuilt every retained row
The keyword-vector rebuild SHALL record the keyword fingerprint only as part of an operation that rebuilt **every `notes_metadata` row retained in the database**, and SHALL write the fingerprint and every rebuilt row in one transaction, so that a fingerprint can never certify a row that is still on the previous configuration.

The rebuild is per owner. A fingerprint written inside a per-owner rebuild claims something a per-owner rebuild cannot establish, and two ordinary shapes falsify it: a second owner's rebuild that raises after the first has already written the fingerprint, and an owner scope holding rows that the driver never visits at all — an inactive or unassigned user, or the ownerless scope in a database that also holds named users. In either case the stored fingerprint certifies rows still carrying the previous configuration, and a startup that refuses on that fingerprint would pass while keyword search is exactly as wrong as before.

The operation SHALL therefore determine its scopes from the **rows that exist** — every distinct owner value present in `notes_metadata`, the null owner included — rather than from the set of active users, SHALL rebuild each of them, and SHALL write the fingerprint only after all of them have succeeded. A scope that raises, or whose vault root cannot be pinned, SHALL roll the entire operation back, SHALL leave the stored fingerprint unchanged, and SHALL name the scope that stopped it.

The cost — a multi-tenant rebuild becomes all-or-nothing rather than per tenant — SHALL be accepted and documented. The rebuild touches no embeddings and makes no provider calls, and the alternative is a fingerprint that does not mean what it says.

Where a scope holds rows whose vault cannot be read, the operation cannot complete and the refusal at startup therefore persists. The remedy SHALL be stated in both the refusal and the operator documentation: assign or delete that scope's user, or restore the previous configuration, which clears the refusal with no rebuild at all.

#### Scenario: One failing scope writes no fingerprint

- **WHEN** two owner scopes hold rows and the second scope's rebuild raises
- **THEN** no fingerprint SHALL be recorded
- **AND** the first scope's rebuilt vectors SHALL be rolled back with it
- **AND** the error SHALL name the scope that failed

#### Scenario: A scope holding rows but not in the active set is rebuilt

- **WHEN** `notes_metadata` holds rows owned by an inactive user, and rows with a null owner, alongside active users' rows
- **THEN** the rebuild SHALL attempt every one of those scopes
- **AND** SHALL NOT write the fingerprint while any of them is unrebuilt

#### Scenario: A complete rebuild records the fingerprint atomically

- **WHEN** every scope holding rows is rebuilt successfully
- **THEN** the fingerprint and the rebuilt vectors SHALL be committed in one transaction
- **AND** the next startup SHALL proceed silently

#### Scenario: An unreadable scope is named and blocks the record

- **WHEN** a scope holds rows and its vault root cannot be pinned
- **THEN** the operation SHALL roll back, write no fingerprint, and name that scope
- **AND** the message SHALL state that assigning or deleting that user, or restoring the previous configuration, is the way forward

### Requirement: The embedding reset's ordering is specified and enforced by the pass
The embedding reset workflow SHALL be documented as running while the service is **not embedding**, and the system SHALL enforce that rather than rely on the documentation: an embed pass SHALL read the stored embedding fingerprint at the start of each user's embedding stage and SHALL embed nothing for any user when it differs from the fingerprint of the configuration the process itself is running, logging the refusal and recording nothing.

The hole this closes is created by the reset's own design. The reset runs as a one-off container so that it reads the edited configuration, which also means it runs happily while the previous container is still serving. In that ordering the reset wipes the column and records the **new** fingerprint, and the old container's next pass embeds the backlog with the **old** model and stamps the notes as embedded — old-model vectors under a fingerprint claiming the new model, permanently, with every later startup silent because the stored value already matches.

The documented ordering for any change to the embedding configuration SHALL be: edit the configuration, deploy — at which point the new image refuses at the fingerprint or the dimension guard and stays down, embedding nothing — run the reset while it is down, then start. The refusal is what creates the quiescent window, so the guard pays for its own runbook. This inverts the previous advice to reset before recreating the container, which was safe only while nothing depended on a stored claim.

The pass-level re-check is the backstop for an operator who does not follow that ordering: a live process running the previous configuration stops embedding within one pass of any reset. It SHALL NOT interfere with an in-process reset, which writes the very value the process then compares against.

#### Scenario: A live process stops embedding after an external reset

- **WHEN** the stored embedding fingerprint is changed by a one-off reset while a process running the previous configuration is serving
- **THEN** that process's next embed stage SHALL embed no note for any user
- **AND** it SHALL log the refusal naming both fingerprints

#### Scenario: The in-process reset does not block the pass

- **WHEN** the reset is performed by the running process itself
- **THEN** the following embed stage SHALL proceed normally

#### Scenario: The documented ordering leaves nothing embedding during the reset

- **WHEN** an operator changes the embedding model, deploys, and the new container refuses at the fingerprint guard
- **THEN** no process SHALL be embedding while the reset runs
- **AND** the restart after the reset SHALL start silently

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
