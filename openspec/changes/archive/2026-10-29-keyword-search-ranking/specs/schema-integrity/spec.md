## ADDED Requirements

### Requirement: Migration 031 SHALL own the keyword excerpt column and SHALL re-head every keyword vector in SQL

Migration 031 SHALL add `notes_metadata.body_excerpt` (`text`, nullable, no server default, no backfill) carrying the migration's ownership marker as its column comment, mirrored byte-identically in `src/models/db.py` so that `alembic check` compares it; a same-named column of another type, NOT NULL, or with a default SHALL be refused by name, and a column of the exact shape carrying the marker SHALL be reconciled without change. `downgrade()` SHALL, under the index generation lock, set every non-NULL `content_tsvector` to `ts_filter(content_tsvector, '{d}')` — removing every weight-`A` lexeme so the previous image sees the pre-change matching semantics and its later D-only writes do not mix with headed rows — and only then drop a marked column.

Because an old-image process writing between 031's commit and its own replacement can preserve a stale head across a move, the compose `make deploy` SHALL stop the application container before `alembic upgrade head` whenever a migration is pending (`alembic current` differs from `alembic heads`), and `DEPLOYMENT.md`'s upgrade notes SHALL state the order stop → migrate → start for a hand-driven compose stack. The Kubernetes bundle needs no change: `strategy: Recreate` ends the old pod before the new pod's `migrate` initContainer runs, and the design records that.

In the same transaction, 031 SHALL re-head every `notes_metadata` row whose `content_tsvector` is not NULL, reading no file: it SHALL first acquire the index generation advisory lock (`pg_advisory_xact_lock` on the key `src/services/index_state.py` defines, mirrored as a literal and pinned equal by a test); it SHALL take the FTS configs from the stored `fts_fingerprint` row — its `configs` when present, `settings.fts_configs` when the key is absent, and SHALL refuse naming the key when the stored value cannot be read; and it SHALL then, per row whose vector carries no weight-`A` lexeme (`length(ts_filter(content_tsvector, '{a}')) = 0`), set `content_tsvector = head(title, aliases) || ts_filter(content_tsvector, '{d}')` through the shared `rehead` builder, with `aliases` from the shared rule over the stored frontmatter, each UPDATE in its own savepoint. A per-row SQLSTATE 54000 SHALL leave that row unchanged, be counted and logged by row id, and not fail the migration; any other error SHALL fail the migration with nothing committed. The re-head SHALL be byte-idempotent — a row already carrying a head is skipped, because a second application would shift the body's positions — and 031 SHALL raise `statement_timeout` for its own duration and pin and assert `search_path`.

#### Scenario: Fresh upgrade

- **WHEN** a database at 030 holding rows with unweighted vectors is upgraded to 031
- **THEN** `body_excerpt` SHALL exist nullable with the marker and read NULL on every row
- **AND** every row with a vector SHALL hold its title's lexemes at weight `A` and its former lexemes at weight `D`, equal to what the incremental writer produces for the same title, aliases and body

#### Scenario: Stamp-back re-runs cleanly and changes no vector twice

- **WHEN** the schema gate stamps the database back to 030 and upgrades again
- **THEN** 031 SHALL reconcile the marked column without error and every re-headed vector SHALL be byte-equal to its value after the first run

#### Scenario: The stored fingerprint decides the configs

- **WHEN** `indexer_state` records `fts_fingerprint` with `configs: ["simple"]` while the environment says `FTS_CONFIGS=english`
- **THEN** the head SHALL be built under `simple`, and when the key is absent it SHALL be built under the environment's configs

#### Scenario: An unreadable fingerprint refuses

- **WHEN** `fts_fingerprint` holds a value that is not a JSON object with a known version
- **THEN** 031 SHALL fail naming the key and change nothing

#### Scenario: A row at the size limit does not fail the migration

- **WHEN** one row's re-headed vector exceeds the 1 MiB tsvector limit
- **THEN** that row SHALL keep its previous vector, the migration SHALL log its id and complete, and every other row SHALL be re-headed

#### Scenario: An impostor column is refused

- **WHEN** `body_excerpt` already exists as NOT NULL, with a default, or with another type
- **THEN** the migration SHALL fail naming the column and the disagreement, and change nothing

#### Scenario: Downgrade restores D-only vectors and drops only what 031 created

- **WHEN** a downgrade runs against a database upgraded by 031 whose rows carry headed vectors
- **THEN** afterwards no `content_tsvector` SHALL contain a weight-`A` lexeme, every body lexeme SHALL still be present, and the marked column SHALL be dropped
- **AND** a downgrade against an unmarked same-named column SHALL leave that column in place naming it, after re-filtering the vectors

#### Scenario: A pending migration stops the compose app before it migrates

- **WHEN** `make deploy` runs on a compose host whose database is at 030 with image 031 built
- **THEN** it SHALL stop the application container, run `alembic upgrade head`, and only then recreate the container
- **AND** when the database is already at head it SHALL keep the previous order (migrate, then recreate)

#### Scenario: The schema agrees with the model afterwards

- **WHEN** `alembic check` runs after the migration has been applied
- **THEN** it SHALL report no new upgrade operations
