## ADDED Requirements

### Requirement: Migration 034 SHALL add the chunk-locator, prefix and chunking-version columns as marked, reconcilable units with no backfill
Migration 034 (revising 033) SHALL add `note_embeddings.heading_path text[]`, `note_embeddings.section_ordinal integer`, `note_embeddings.embed_prefix text` `notes_metadata.embedded_chunking_version smallint` and `notes_metadata.embedded_input_digest varchar(64)`, all nullable, with no default, no index and no backfill. It SHALL add `ck_note_embeddings_heading_meta`, which requires the three row columns to be all NULL, or all non-NULL with `section_ordinal >= 0` and `(section_ordinal = 0) = (cardinality(heading_path) = 0)`. It SHALL add `ck_notes_metadata_embedded_chunking_version`, which requires NULL or `>= 1`. Both CHECKs SHALL be added `NOT VALID` and then validated. Each column SHALL carry a marker comment byte-identical to the ORM model's. A re-run against a database already carrying a same-named column or constraint SHALL verify its exact shape and marker, and SHALL refuse anything else by name before changing anything. The migration SHALL pin and assert `search_path` and SHALL set and reset `lock_timeout` and `statement_timeout`, in both `upgrade()` and `downgrade()`, so the downgrade's wait on the generation lock is bounded. It SHALL need only ownership of the tables.

`downgrade()` SHALL, in one transaction holding the index generation advisory lock, clear `embedded_content_hash` for every note holding a row with a non-NULL `embed_prefix`, delete those rows, and then drop only the marked constraints and columns.

#### Scenario: Upgrade leaves existing rows untouched
- **WHEN** 034 runs over a database holding pre-034 embedding rows
- **THEN** every existing row SHALL read NULL in the three new row columns and every note NULL in `embedded_chunking_version` and `embedded_input_digest`, and no vector SHALL be modified

#### Scenario: Clean drift check, including a round trip
- **WHEN** `make test-schema` runs migrations to head, then `downgrade 033` and `upgrade head` again
- **THEN** `alembic check` SHALL report no new upgrade operations after both upgrades

#### Scenario: CHECKs asserted through the catalogue
- **WHEN** the schema gate reads `pg_constraint`
- **THEN** both CHECK definitions SHALL match the design and be validated

#### Scenario: An impostor is refused
- **WHEN** a same-named column of another type, or without the marker, already exists before 034 runs
- **THEN** the migration SHALL fail naming it and SHALL change nothing

#### Scenario: Downgrade removes heading-era vectors
- **WHEN** `downgrade 033` runs over a database holding both pre-034 rows and rows with a prefix
- **THEN** the prefixed rows SHALL be deleted, their notes SHALL have `embedded_content_hash` NULL, the pre-034 rows SHALL remain, and the marked columns SHALL be gone

#### Scenario: Non-superuser migration
- **WHEN** `tests/integration/test_nonsuperuser_migrations_pg.py` runs to head as a non-superuser owner
- **THEN** it SHALL pass
