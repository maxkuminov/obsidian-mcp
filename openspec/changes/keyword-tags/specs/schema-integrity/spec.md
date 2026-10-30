## ADDED Requirements

### Requirement: Migration 035 SHALL add the tags part to every tagged keyword vector in SQL or commit nothing, and SHALL leave `alembic check` clean

Migration 035 (`down_revision "034"`) SHALL add, alter and drop no schema object. In one transaction it SHALL raise `lock_timeout` to 10 s and `statement_timeout` to 15 min for its own duration, pin and assert `search_path` and the qualified `notes_metadata` table as 031 does, acquire the index generation advisory lock (`pg_advisory_xact_lock` on the key `src/services/index_state.py` defines, mirrored as a literal and pinned equal by a test) before reading any row, and take the FTS configs from the stored `fts_fingerprint` row — its `configs` when present, `settings.fts_configs` when the key is absent, and a refusal naming the key, with nothing changed, when the stored value cannot be read. Before its first UPDATE, 035 SHALL run a read-only size preflight over every row the rewrite would change (the same two predicates): for each, it SHALL estimate the post-035 vector's data size as `S(ts_filter(content_tsvector, '{d}')) + S(head(title, aliases, tags))`, the head built by the shared builder under those configs, where `S(v)` is `Σ over unnest(v) of (octet_length(lexeme) rounded up to even) + (2 + 2 × cardinality(positions) when positions is non-empty, else 0)` — PostgreSQL's own accounting of a tsvector's string area, an upper bound for a concatenation because merged lexemes share storage — and when any estimate exceeds 1,048,511 bytes (the 1,048,575-byte limit less a 64-byte alignment margin) it SHALL abort with nothing written, naming the count and the row ids (ids only, never paths or text), so that a Kubernetes `migrate` initContainer fails in seconds rather than after rewriting rows. The rewrite's all-or-nothing rule remains the backstop for any row the estimate passes. It SHALL then, reading no file, for each `notes_metadata` row whose `content_tsvector` is not NULL, in id order, set `content_tsvector = head(title, aliases, tags) || ts_filter(content_tsvector, '{d}')` through the shared `rehead` builder, with `title` from the stored column, `aliases` from the shared aliases rule over the stored frontmatter and `tags` from the shared `tags_text` rule over the stored `tags`, where the UPDATE's own predicate requires both that the row's vector carries no weight-`B` lexeme (`length(ts_filter(content_tsvector, '{b}')) = 0`) and that the row's tags text produces at least one lexeme under those configs. 035 SHALL be all-or-nothing: any error on any row, SQLSTATE 54000 (the 1 MiB tsvector limit) included, SHALL fail the whole migration with nothing committed, and the error SHALL name the row's `notes_metadata.id` and the SQLSTATE (never the path or any note text); no row SHALL be left untagged by a migration that succeeded. 035 SHALL log one summary line with the counts re-headed, already tagged and without tag lexemes. `DEPLOYMENT.md`'s 035 upgrade note SHALL give the operator a read-only SQL statement to run against the live database before merging, reporting only the count of tagged rows, the count and ids of rows flagged and the largest estimate, where that statement SHALL never pass a row 035's preflight would abort on (a false flag is acceptable, a false pass is not): it SHALL flag a candidate row whose `S(content_tsvector) + S(tags part)` exceeds the margin, and SHALL flag every candidate row whose vector carries no weight-`A` lexeme (a D-only row 031 left at the limit), because the head 035 rebuilds for such a row from title and aliases is not in its stored vector and the aliases rule is not expressible in that statement; the note SHALL say that a flagged D-only row must be shortened, or rewritten through the halving retreat by an edit or `make rebuild-tsvectors` on the current image, before upgrading; SHALL state that an abort under Kubernetes `Recreate` is downtime until the image digest is reverted; and SHALL state the remedy for a preflight abort or a 54000 failure: the database is still at 034, so serve the previous image, shorten the named note's body or tags so that its vector is smaller, let that image re-index it, and deploy again. `downgrade()` SHALL, under the index generation lock, set every vector that carries a weight-`B` lexeme to `ts_filter(content_tsvector, '{a,d}')`. Because an old-image process writing between 035's commit and its own replacement would write untagged vectors that 035 never revisits, the compose `make deploy` SHALL keep stopping the application container before `alembic upgrade head` whenever a migration is pending, and `DEPLOYMENT.md`'s 035 upgrade note SHALL state the order stop → migrate → start.

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

#### Scenario: The preflight aborts on a near-limit row before writing anything

- **WHEN** a 034 database holds a tagged row whose vector's data size is within 64 bytes of the 1,048,575-byte limit, and other tagged rows of ordinary size, and it is upgraded to 035
- **THEN** 035 SHALL fail before its first UPDATE, naming one row and that row's id, `alembic_version` SHALL still read `034`, every vector SHALL be byte-equal to its value before the attempt, and `alembic check` SHALL report no new upgrade operations afterwards

#### Scenario: The operator statement reports a D-only row the stored-vector estimate would pass

- **WHEN** a 034 database holds a tagged row with no weight-`A` lexeme whose `S(content_tsvector) + S(tags part)` is within the margin but whose `S(ts_filter(content_tsvector, '{d}')) + S(head(title, aliases, tags))` exceeds it, and the `DEPLOYMENT.md` statement is run against it
- **THEN** the statement SHALL report that row's id among the flagged rows, and upgrading that database to 035 SHALL abort in the preflight naming the same id

#### Scenario: The preflight passes an ordinary database

- **WHEN** every tagged row's estimate is within the margin
- **THEN** the preflight SHALL write nothing and the rewrite SHALL proceed

#### Scenario: A row at the size limit fails the whole migration

- **WHEN** one tagged row's re-headed vector exceeds the 1 MiB tsvector limit although its estimate passed the preflight (the test bypasses the preflight)
- **THEN** 035 SHALL fail naming that row's id and SQLSTATE 54000, `alembic_version` SHALL still read `034`, and every vector SHALL be byte-equal to its value before the attempt

#### Scenario: Any other row error fails the whole migration

- **WHEN** an UPDATE of one row fails with any other error
- **THEN** 035 SHALL fail with nothing committed and `alembic_version` SHALL still read `034`

#### Scenario: Downgrade strips the tags part

- **WHEN** a downgrade to 034 runs against a database upgraded by 035
- **THEN** no `content_tsvector` SHALL contain a weight-`B` lexeme, every weight-`A` and weight-`D` lexeme SHALL still be present, and a lexeme that was in both the tags and the body SHALL keep its body positions

#### Scenario: The schema agrees with the model after the round trip

- **WHEN** `alembic check` runs after upgrade to 035, after downgrade to 034, and after upgrading again
- **THEN** it SHALL report no new upgrade operations each time
