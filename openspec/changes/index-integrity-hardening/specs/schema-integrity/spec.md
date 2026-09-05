## ADDED Requirements

### Requirement: Migration 023 owns the indexer state table and the chunk-truncation column as two marked units
Migration 023 SHALL create the `indexer_state` table — `key` as the primary key, a non-null `value`, and a non-null `updated_at` defaulting to the transaction timestamp — stamped with 023's ownership marker as its table comment, and SHALL add `notes_metadata.chunks_truncated` as `BOOLEAN NOT NULL DEFAULT FALSE` stamped with 023's column marker as its column comment. It SHALL write no row into `indexer_state` and SHALL backfill no value into the column. `downgrade()` SHALL drop each unit only if it carries the marker, all-or-nothing per unit. After migrating to head, `alembic check` SHALL report no pending operations, and the same marker strings SHALL be declared on the ORM table and column so the check compares them.

**`indexer_state` SHALL carry a CHECK constraint restricting `key` to the closed set of keys the application uses**, resolved through the catalogue rather than by name — a same-named `CHECK (true)` satisfies a lookup by name while enforcing nothing — with its definition compared against the server's own rendering of the canonical predicate, required to be validated, and required to carry 023's marker as its constraint comment.

The constraint is not tidiness. A key read from this table that does not exist reads as **absent**, and absent is the state that makes the startup fingerprint check *adopt* rather than refuse. A single mistyped key therefore silently disables the guard whose entire purpose is to prevent a permanent, undetectable corruption of the vector space. The weaker version of this argument already justifies `ck_indexer_runs_trigger`, where a typo produces only a mislabelled row. Adding a key becomes a migration, which is correct: every key in this table has a startup or a scheduling consequence.

**023 SHALL write no fingerprint.** Deriving one from the current settings would assert that the stored embedding and keyword rows were produced by the configuration the environment carries at migration time — exactly the claim the fingerprint exists to test, and exactly the reassignment-lag mistake migration 016 refuses to make with vault provenance. An absent fingerprint means "unknown", which is the only true statement available at migration time, and the startup adoption rule handles it.

**The column's server default is load-bearing in two ways.** It makes the addition a catalogue-only `ADD COLUMN` on a table that carries a `tsvector` and two GIN indexes, rather than a full rewrite; and `false` is the *correct* value for every pre-existing row rather than a placeholder, because every row that exists when 023 runs was embedded by a chunker that had no cap and could not truncate.

**A pre-existing object of either name SHALL be refused, not adopted.** 023 SHALL require a pre-existing `indexer_state` to be exactly its own — the three columns with its types and nullability, the primary key, the marked and validated CHECK, and the table marker — and a pre-existing `chunks_truncated` to be exactly its own — boolean, `NOT NULL`, defaulting to `false`, carrying the column marker. Anything else SHALL make the migration fail naming what it found, with nothing changed. Adopting a same-named column of unknown provenance would let the vector tools read another writer's boolean as "this note's embedding is complete".

Because 023 writes no row and backfills nothing, a stamp-back re-run reconciles the existing objects and writes nothing, so it cannot erase a fingerprint the application has since recorded or a marker the indexer has since set.

The deploy migrates before the container is recreated, so the previous build serves briefly against the new objects. It neither reads nor writes either of them and its upserts omit the column, so the default supplies `false` — which is the correct value for anything the uncapped chunker produced — and `indexer_state` stays empty until the new build's first startup adopts.

#### Scenario: Fresh database

- **WHEN** an empty database is migrated to head
- **THEN** `indexer_state` SHALL exist with its three columns, its primary key, its validated and marked CHECK on `key`, and 023's table marker as its comment
- **AND** `notes_metadata.chunks_truncated` SHALL exist as `boolean NOT NULL` defaulting to `false` and carrying 023's column marker
- **AND** `alembic check` SHALL report no new upgrade operations

#### Scenario: The migration writes nothing

- **WHEN** 023 runs on a database holding notes, embeddings and keyword vectors
- **THEN** `indexer_state` SHALL hold no rows
- **AND** every `notes_metadata` row's `chunks_truncated` SHALL be `false`
- **AND** no `notes_metadata`, `note_embeddings` or `note_links` row SHALL be otherwise modified or deleted

#### Scenario: A disallowed key is rejected

- **WHEN** an insert or update sets `indexer_state.key` to a value outside the closed set
- **THEN** the database SHALL reject it

#### Scenario: An impostor constraint is refused

- **WHEN** `indexer_state` already exists carrying a constraint of the expected name whose definition is not the canonical predicate, or which is not validated, or which lacks 023's marker
- **THEN** the migration SHALL fail naming what it found and SHALL leave the schema unchanged

#### Scenario: A foreign object of either name is refused

- **WHEN** a table named `indexer_state` or a column named `notes_metadata.chunks_truncated` already exists that is not exactly 023's — a different type or nullability, a different default, or lacking the marker — and 023 runs
- **THEN** the migration SHALL fail naming what it found
- **AND** SHALL NOT adopt it and SHALL leave the schema unchanged

#### Scenario: Re-running the migration preserves recorded state

- **WHEN** the database is stamped back to 022 and upgraded again after the application has recorded fingerprints and the indexer has set the truncation marker on a note
- **THEN** every recorded `indexer_state` row SHALL be left unchanged
- **AND** the note's `chunks_truncated` SHALL still be true

#### Scenario: The model and the migration agree on the markers

- **WHEN** `alembic check` runs at head
- **THEN** it SHALL report no operation for the table or the column, which is only true while the ORM's declared comments are byte-identical to the migration's markers

#### Scenario: Downgrade drops only marked work

- **WHEN** a database at 023 is downgraded to 022
- **THEN** a marked `indexer_state` SHALL be dropped and a marked `chunks_truncated` SHALL be dropped
- **AND** an object of either name that lacks the marker SHALL be left in place instead
- **AND** no other table, column or constraint SHALL be altered

### Requirement: The schema gate covers migration 023 before deploy
`make test-schema` SHALL exercise migration 023 on a throwaway pgvector container before the deploy that carries it, asserting `alembic check` clean at head and asserting the catalogue directly for both units — the table's columns, its primary key, its CHECK definition, validation and marker, the table comment, and the column's type, nullability, default and comment — across the fresh, stamp-back, impostor-object, impostor-constraint and downgrade paths, and asserting that a disallowed key is actually rejected on insert.

Asserting the catalogue directly is required because `alembic check` does not compare CHECK constraint predicates at all, and a `CHECK (true)` of the right name would satisfy every name-based lookup while enforcing nothing — which for this constraint means a mistyped key silently disabling the fingerprint guard.

#### Scenario: The gate runs before the deploy

- **WHEN** the change carrying migration 023 is prepared for deploy
- **THEN** `make test-schema` SHALL pass, including its 023 cases
- **AND** a run in which the integration module skipped SHALL be treated as a failure, not as a pass

#### Scenario: A disallowed key is rejected on insert

- **WHEN** the gate inserts a row into `indexer_state` with a key outside the closed set
- **THEN** the insert SHALL be rejected by the database
