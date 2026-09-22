## MODIFIED Requirements

### Requirement: The schema gate covers both migrations of this wave before deploy

The schema gate SHALL exercise every migration whose behaviour it asserts, including both migrations of the current wave, on a throwaway database in the same run, and SHALL assert `alembic check` clean at the resulting head. The gate SHALL carry a single literal naming the current head revision, and SHALL assert that a migrated database reads exactly that literal.

The current head literal SHALL be **`027`**, with **`026` present in the applied chain**. The requirement keeps its original heading so that this block modifies the existing requirement rather than adding a second one; the current wave is not limited to the 016/017 pair for which it was first written.

`026` and `027` are the two migrations of `performance-2026-09`:
- `026`'s recorded predecessor SHALL be `025`;
- `027`'s recorded predecessor SHALL be `026`;
- migration to head SHALL apply `025`, then `026`, then `027`.

The sibling-ordering guarantee this requirement already carried is preserved rather than replaced: raising the head extends the chain it asserts; it does not narrow it to the newest pair. Raising the asserted head is a required part of adding a migration. A later migration added without updating the gate SHALL fail the head assertion rather than silently widening what "head" means. A second requirement naming a different head SHALL NOT be introduced alongside this one, because two requirements disagreeing about the head is exactly the contradiction the single literal exists to prevent.

The gate module carrying this literal is the one `make test-schema` invokes. A new migration's marker, drift, downgrade and stamp-back cases SHALL live in that module, not in a separate module the gate does not run.

The historical checks SHALL be kept, not replaced. The cases covering 013, 014, 016, 017, 022, 023, 024 and 025 SHALL continue to execute alongside the 026 and 027 cases; raising the head SHALL NOT remove the earlier reconciliations' coverage.

Idempotence SHALL be exercised by stamping the revision back and upgrading again, not by a second `upgrade head`. The latter is a no-op at the alembic level and proves nothing about the migration body.

#### Scenario: The gate asserts the current head and 026 is in the chain

- **WHEN** a throwaway database is migrated to head
- **THEN** `alembic_version` SHALL read the single head revision the gate module names
- **AND** `026` SHALL be one of the revisions that ran to reach it
- **AND** `alembic check` SHALL report no new upgrade operations

#### Scenario: The earlier waves' cases still run

- **WHEN** the gate runs at the new head
- **THEN** the cases covering 013, 014, 016, 017, 022, 023, 024 and 025 SHALL all execute and pass
- **AND** none SHALL have been removed in the course of raising the head

#### Scenario: The new migrations' cases run inside the gate

- **WHEN** `make test-schema` runs
- **THEN** 026's and 027's marker, drift, downgrade and stamp-back cases SHALL be among the tests it executes

#### Scenario: Head at 027

- **WHEN** a throwaway database is migrated to head
- **THEN** `alembic_version` SHALL read `027`
- **AND** `alembic check` SHALL report no new upgrade operations

#### Scenario: The ordering against the sibling migration holds

- **WHEN** the merged migration history is inspected
- **THEN** `026`'s recorded predecessor SHALL be `025`
- **AND** `027`'s recorded predecessor SHALL be `026`
- **AND** migrating to head SHALL apply `025`, then `026`, then `027`

#### Scenario: Idempotence is exercised by stamping back

- **WHEN** the gate tests that any covered migration can re-run
- **THEN** it SHALL stamp the database to the preceding revision and upgrade again, so the migration body genuinely re-executes

## ADDED Requirements

### Requirement: Migration 026 SHALL own the note stat columns as one marked unit
Migration 026 SHALL add `stat_size`, `stat_mtime_ns`, `stat_ctime_ns` and `stat_ino` to `notes_metadata`, each `BIGINT NULL` with no default and no backfill, together with a CHECK constraint requiring the four to be all NULL or all non-NULL.

Each column SHALL carry the migration's marker as its comment, and the marker SHALL be mirrored byte for byte in the model, so that `alembic check` compares it. The CHECK SHALL be resolved through `pg_constraint` by its definition, not by name.

Where a same-named column already exists with a different type or nullability, or a same-named constraint with a different definition, 026 SHALL refuse and name what disagreed rather than adopt it. `downgrade()` SHALL drop only the columns and constraint that carry 026's marker.

#### Scenario: Fresh upgrade
- **WHEN** a database at 025 is upgraded to 026
- **THEN** the four columns SHALL exist, nullable, with 026's marker, every existing row SHALL read NULL in all four, and the all-or-none CHECK SHALL be in force

#### Scenario: A half-recorded stat is refused by the database
- **WHEN** a row is written with some but not all of the four columns non-NULL
- **THEN** the CHECK SHALL reject it

#### Scenario: An impostor column is refused
- **WHEN** `notes_metadata` already has a `stat_ino` column of type `integer`
- **THEN** 026 SHALL fail and name the column and its disagreement

#### Scenario: Stamp-back re-runs cleanly
- **WHEN** the gate stamps the database back to 025 and upgrades again
- **THEN** 026 SHALL reconcile the existing marked unit without error and without altering row data

### Requirement: Migration 027 SHALL own the notes_metadata vacuum settings and the reduced-precision vector index, and SHALL leave `alembic check` clean
Migration 027 SHALL set `autovacuum_vacuum_scale_factor = 0.02` and `autovacuum_vacuum_insert_scale_factor = 0.02` on `notes_metadata`. It SHALL NOT run `VACUUM`, which cannot run inside the migration's transaction. The first vacuum after deploy SHALL be left to autovacuum, with `make db-vacuum-notes` as an explicit fallback that runs outside a transaction.

If the reduced-precision index passed its recall gate, 027 SHALL also:
- build it from the shared vector-index definition, at the configured embedding dimension, and only when that dimension is at most 2000;
- drop `ix_note_embeddings_embedding_hnsw`.

`downgrade()` SHALL then restore that legacy index when the dimension is at most 2000, and drop the new one. In both cases `downgrade()` SHALL reset the two reloptions.

`alembic check` SHALL report no new upgrade operations at head:
- The legacy index's declaration SHALL be removed from the model in the same change that drops it.
- The expression index SHALL be excluded from autogenerate comparison by an `include_object` hook that excludes exactly that one index name.
- Because Alembic compares neither the excluded index nor table reloptions, the schema gate SHALL verify both through the catalogue: the index's definition, validity and operator class through `pg_index`/`pg_get_indexdef`, and the reloptions through `pg_class.reloptions`.

#### Scenario: Reloptions are in force
- **WHEN** a database is migrated to 027
- **THEN** `pg_class.reloptions` for `notes_metadata` SHALL contain both scale factors at 0.02

#### Scenario: The index is verified through the catalogue
- **WHEN** the index shipped and a database is migrated to 027 with the default dimension
- **THEN** exactly one valid HNSW index on `note_embeddings` SHALL exist, its definition SHALL use `halfvec(1024)` with `halfvec_cosine_ops`, and `ix_note_embeddings_embedding_hnsw` SHALL be absent

#### Scenario: The exclusion is exactly one name
- **WHEN** the `include_object` hook is evaluated against every reflected object
- **THEN** it SHALL return false for exactly one index name and true for every other object

#### Scenario: Downgrade restores the previous index
- **WHEN** the index shipped and a database at 027 is downgraded to 026
- **THEN** `ix_note_embeddings_embedding_hnsw` SHALL exist and be valid, the expression index SHALL be absent, and the reloptions SHALL be reset
