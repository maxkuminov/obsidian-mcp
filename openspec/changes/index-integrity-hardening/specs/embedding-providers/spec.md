## ADDED Requirements

### Requirement: Stored settings fingerprints gate startup
The system SHALL record, once per derived kind, a **fingerprint of the configuration those rows were generated under**, and SHALL compare it at startup against the fingerprint of the configuration the process is about to run with. Two fingerprints SHALL be kept: one for embeddings, covering the active provider, the active model, the dimension count, the chunk size and the chunk overlap; and one for keyword vectors, covering the configured text-search config list.

The fingerprints SHALL be **global**, one row each, and SHALL NOT be recorded per embedding row or per note. The settings they describe are global, a per-row copy would be one identical string per chunk, and its only use would be a lazy per-note re-embed — which is precisely the design that leaves vectors from two models coexisting in one index for the whole migration window. Cosine distance between two vector spaces is meaningless, so a partially migrated index answers wrongly for longer than a refused startup does. The remedy is the wipe-and-re-embed the reset workflow already performs.

The embedding fingerprint's `model` field SHALL be the model of the **active** provider, selected by the same branch that selects the provider itself, so that reading the inactive provider's model — the exact defect this guard exists to catch — cannot happen in one place while the provider is chosen in another.

The keyword fingerprint's config list SHALL be compared **order-insensitively**, because a note's stored vector is the concatenation of one vector per config and a query is the disjunction of one query per config; reordering the list changes neither, so warning about it would be a false alarm.

Comparison SHALL be by byte equality of a canonical rendering that admits exactly one spelling per configuration, and the rendering SHALL be parseable so that a mismatch can be reported field by field rather than as two opaque strings.

The comparison SHALL resolve as follows:

- **No fingerprint stored** — the system SHALL adopt the current one, record it, log a warning that it was assumed rather than verified, and start. Refusing here would take every existing deployment down on upgrade over a configuration nobody changed. The one-time consequence — that a configuration changed in the same deploy that introduces the fingerprint is blessed rather than caught — SHALL be documented in the change's deploy instructions.
- **Stored equals current** — the system SHALL start, silently.
- **Stored differs, embeddings** — the system SHALL log at critical level naming both fingerprints and the fields that differ, point at the embedding reset workflow, and **exit non-zero**.
- **Stored differs, keyword vectors** — the system SHALL log a loud warning naming both fingerprints, the fields that differ, and the keyword-rebuild command, and **SHALL start**.
- **The state store has not been migrated yet** — the system SHALL return without deciding, deferring to migrations, exactly as the dimension check defers when the embedding column is absent.

**Embeddings fail closed and keyword vectors do not, and the asymmetry is the point.** Two vectors from different models in one column are not less accurate — the distance between them carries no meaning, the approximate index has already been built over both, and nothing detects or repairs it. A stale stemmer is incomplete rather than wrong: every lexeme stored is a real lexeme of the real note, the note self-heals at its next content change, and a full repair is a cheap rebuild that makes no provider calls. Refusing to serve for that would turn a configuration edit into an outage.

**Only the maintenance workflows SHALL write a fingerprint after the initial adoption.** Startup SHALL NOT rewrite a fingerprint it has just warned about; a guard that fires once and then silences itself while the index stays stale is worse than no guard.

The dimension check SHALL remain in place alongside the embedding fingerprint. It reads the live column width from the database catalogue — a physical fact about the table — while the fingerprint records the configuration the stored rows were produced under. A dump restored into a differently configured deployment trips the first; a same-dimension model swap trips only the second.

#### Scenario: A same-dimension model swap refuses startup

- **WHEN** the stored embedding fingerprint names one model and the configured provider names a different model of the same dimension
- **THEN** startup SHALL log both fingerprints and the differing field, name the embedding reset command, and exit non-zero
- **AND** no embedding row SHALL be modified by the check

#### Scenario: A chunk-size change refuses startup

- **WHEN** the configured chunk size or chunk overlap differs from the stored fingerprint's
- **THEN** startup SHALL exit non-zero with the same message shape and the same reset pointer

#### Scenario: An FTS config change only warns

- **WHEN** the configured text-search config list differs from the stored keyword fingerprint's
- **THEN** startup SHALL log a warning naming both lists and the keyword-rebuild command
- **AND** the server SHALL start and SHALL serve keyword search

#### Scenario: The FTS warning does not silence itself

- **WHEN** a process starts, warns about a keyword fingerprint mismatch, and is restarted without the rebuild having been run
- **THEN** the second start SHALL warn again
- **AND** the stored keyword fingerprint SHALL be unchanged by either start

#### Scenario: Reordering the FTS config list does not warn

- **WHEN** the configured config list holds the same names as the stored fingerprint's in a different order
- **THEN** startup SHALL treat the fingerprints as equal and SHALL NOT warn

#### Scenario: An absent fingerprint is adopted, not refused

- **WHEN** a deployment whose state store holds no fingerprint starts
- **THEN** the system SHALL record the current fingerprints, log that they were assumed and not verified, and start
- **AND** the following start with an unchanged configuration SHALL be silent

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

### Requirement: A configuration knob that invalidates derived rows SHALL name its remedy where it is set
Every configuration key whose change silently invalidates stored derived rows SHALL carry, in the operator-facing configuration example and in the reference documentation, the command that repairs those rows. The embedding model keys — the local provider's model name and the hosted provider's model name — SHALL carry the embedding-reset pointer that the chunk-size, chunk-overlap and provider keys already carry, and the reference documentation's provider-switching section SHALL cover a model change within a single provider.

Documenting the remedy is not made redundant by the startup guard. The guard tells an operator that they have already made the change; the documentation tells them what the change costs before they make it, and it is the only signal for the operator editing the file on a host where the server is not running.

#### Scenario: The model keys carry the reset warning

- **WHEN** an operator reads the configuration example at the local provider's model key or the hosted provider's model key
- **THEN** each SHALL state that changing it after deploy requires the embedding reset workflow

#### Scenario: The reference documentation covers a same-provider model change

- **WHEN** an operator reads the documentation section on changing embedding backends
- **THEN** it SHALL state that changing the model within one provider requires the same reset as switching providers

## MODIFIED Requirements

### Requirement: Reset embeddings workflow

The system SHALL provide an operator-triggered reset workflow that drops and recreates the `note_embeddings.embedding` column at the currently configured dimension and clears `embedded_content_hash` on all notes so they are re-embedded on the next indexer pass. The workflow SHALL, in the same transaction, record the current embedding settings fingerprint, so that the configuration the reset commits to is the configuration the next startup verifies against.

The keyword-vector rebuild SHALL record the current keyword fingerprint in the same single transaction it rebuilds under, so that a rebuild that rolls back does not leave a claim that it succeeded.

These two workflows SHALL be the only writers of their respective fingerprints after the initial adoption. That is what closes the loop: a configuration change refuses startup or warns, the operator runs the repair, the repair records the new configuration, and the next startup is silent because the stored rows really were produced under it.

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

#### Scenario: A rolled-back rebuild records nothing
- **WHEN** the keyword-vector rebuild aborts and its transaction rolls back
- **THEN** the stored keyword fingerprint SHALL be unchanged
- **AND** the next startup SHALL warn about the same mismatch
