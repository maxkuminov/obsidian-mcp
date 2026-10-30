## ADDED Requirements

### Requirement: Migration 035 SHALL add the tags part to every tagged keyword vector in SQL, and SHALL leave `alembic check` clean

Migration 035 (`down_revision "034"`) SHALL add, alter and drop no schema object. In one transaction it SHALL raise `lock_timeout` to 10 s and `statement_timeout` to 15 min for its own duration, pin and assert `search_path` and the qualified `notes_metadata` table as 031 does, acquire the index generation advisory lock (`pg_advisory_xact_lock` on the key `src/services/index_state.py` defines, mirrored as a literal and pinned equal by a test) before reading any row, and take the FTS configs from the stored `fts_fingerprint` row — its `configs` when present, `settings.fts_configs` when the key is absent, and a refusal naming the key, with nothing changed, when the stored value cannot be read. It SHALL then, reading no file, for each `notes_metadata` row whose `content_tsvector` is not NULL, in id order and each UPDATE in its own savepoint, set `content_tsvector = head(title, aliases, tags) || ts_filter(content_tsvector, '{d}')` through the shared `rehead` builder, with `title` from the stored column, `aliases` from the shared aliases rule over the stored frontmatter and `tags` from the shared `tags_text` rule over the stored `tags`, where the UPDATE's own predicate requires both that the row's vector carries no weight-`B` lexeme (`length(ts_filter(content_tsvector, '{b}')) = 0`) and that the row's tags text produces at least one lexeme under those configs. A per-row SQLSTATE 54000 SHALL leave that row unchanged, be counted and logged by row id, and not fail the migration; any other error SHALL fail the migration with nothing committed. 035 SHALL log one summary line with the counts re-headed, already tagged, without tag lexemes, and left at the size limit. `downgrade()` SHALL, under the index generation lock, set every vector that carries a weight-`B` lexeme to `ts_filter(content_tsvector, '{a,d}')`. Because an old-image process writing between 035's commit and its own replacement would write untagged vectors that 035 never revisits, the compose `make deploy` SHALL keep stopping the application container before `alembic upgrade head` whenever a migration is pending, and `DEPLOYMENT.md`'s 035 upgrade note SHALL state the order stop → migrate → start.

#### Scenario: Upgrade adds the tags part to tagged rows only

- **WHEN** a database at 034 holds a row headed by 031 whose `tags` are `["hoa"]`, an untagged row, and a row whose only tag is `the`, and it is upgraded to 035
- **THEN** the tagged row SHALL hold `hoa` at weight `B`, its title and aliases lexemes at weight `A`, and the same body lexemes, weights and relative positions as before
- **AND** the untagged row and the stop-word-tagged row SHALL be byte-equal to their values before the upgrade

#### Scenario: A D-only row is re-headed byte-exactly

- **WHEN** a row 031 left without a head (at the size limit then, within it now) carries tags and is upgraded to 035
- **THEN** its vector SHALL be byte-equal to what the incremental writer produces for the same title, aliases, tags and body

#### Scenario: Stamp-back re-runs change no vector twice

- **WHEN** the schema gate stamps the database back to 034 and upgrades again
- **THEN** every vector SHALL be byte-equal to its value after the first run

#### Scenario: The stored fingerprint decides the configs

- **WHEN** `indexer_state` records `fts_fingerprint` with `configs: ["simple"]` while the environment says `FTS_CONFIGS=english`
- **THEN** the tags part SHALL be built under `simple`, and when the key is absent it SHALL be built under the environment's configs

#### Scenario: An unreadable fingerprint refuses

- **WHEN** `fts_fingerprint` holds a value that is not a JSON object with a known version
- **THEN** 035 SHALL fail naming the key and change nothing

#### Scenario: A row at the size limit does not fail the migration

- **WHEN** one row's re-headed vector exceeds the 1 MiB tsvector limit
- **THEN** that row SHALL keep its previous vector, the migration SHALL log its id and complete, and every other tagged row SHALL be re-headed

#### Scenario: Downgrade strips the tags part

- **WHEN** a downgrade to 034 runs against a database upgraded by 035
- **THEN** no `content_tsvector` SHALL contain a weight-`B` lexeme, every weight-`A` and weight-`D` lexeme SHALL still be present, and a lexeme that was in both the tags and the body SHALL keep its body positions

#### Scenario: The schema agrees with the model after the round trip

- **WHEN** `alembic check` runs after upgrade to 035, after downgrade to 034, and after upgrading again
- **THEN** it SHALL report no new upgrade operations each time
