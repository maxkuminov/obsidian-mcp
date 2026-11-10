## ADDED Requirements

### Requirement: Migration 040 SHALL own the task-rule columns of `task_settings` as marked, reconcilable units with no data written

Migration 040 (`down_revision` `"039"`) SHALL add to `task_settings`:

- `task_rule` varchar(8) NULL;
- `task_tags` varchar(128)[] NOT NULL default empty;
- `global_filter` varchar(64) NULL;
- a CHECK `ck_task_settings_task_rule` admitting only `all`, `fields` and `filter` (and NULL);
- a CHECK `ck_task_settings_global_filter` admitting a `global_filter` only if it is NULL or not blank, and a `task_rule` of `filter` only with a non-NULL `global_filter`.

**Markers and mirroring.** Every added column and both CHECKs SHALL carry a `(040_task_rule)` ownership marker, and every one SHALL be mirrored in `src/models/db.py` so that `alembic check` is clean.

**Migration behaviour.** The migration:

- SHALL write no data;
- SHALL refuse by name a same-named object of a different shape or without the marker, and SHALL reconcile without change one of the exact shape carrying the marker;
- SHALL set `lock_timeout` and `statement_timeout` and `RESET` them;
- SHALL drop, in `downgrade()`, only marked objects, the CHECKs first.

**039's allow-list.** Migration 039's comparison of `task_settings` SHALL leave out exactly these five units, each only when it carries its exact 040 marker. Every other extra column or constraint, and any of these names with another comment, SHALL still be refused by 039.

**Previous image.**

- **Rolling deploy.** An already-running process of the previous image SHALL keep working against the 040 schema, because every new column is nullable or defaulted and both CHECKs admit the values it writes.
- **Restart.** A restarted previous image cannot start against 040, because its migration step does not know revision 040.
- **Rollback.** Rollback SHALL therefore be documented as roll-forward, or as `alembic downgrade 039` run with the 040-aware image before the previous image is started. That downgrade discards every saved task rule, task tag and global filter text.

#### Scenario: Fresh upgrade
- **WHEN** a database at 039 with `task_settings` rows is upgraded to 040
- **THEN** every existing row SHALL read `task_rule` NULL, `task_tags` empty and `global_filter` NULL, and no row SHALL have been updated

#### Scenario: Stamp-back re-runs cleanly through both revisions
- **WHEN** the schema gate stamps a head database back to 039 and upgrades, and separately stamps it back to 038 and upgrades
- **THEN** 040 and 039 SHALL each reconcile without error and write nothing

#### Scenario: Impostors are refused
- **WHEN** an unmarked `task_rule` column, a `task_tags` column of type varchar(64)[], or an unmarked `ck_task_settings_task_rule` or `ck_task_settings_global_filter` already exists
- **THEN** 040 SHALL fail naming the object and change nothing
- **AND** a re-run of 039 over a `task_settings` table holding an unmarked extra column SHALL fail naming it

#### Scenario: The rule values are constrained
- **WHEN** a row is written with `task_rule = 'any'`, with `task_rule = 'filter'` and `global_filter` NULL, or with `global_filter = '  '`
- **THEN** each write SHALL fail on the corresponding CHECK

#### Scenario: A running previous image still saves settings
- **WHEN** the previous image's settings save runs against the 040 schema
- **THEN** it SHALL succeed, and the new columns SHALL keep NULL, empty and NULL

#### Scenario: Downgrade round trip
- **WHEN** the schema gate upgrades to head, runs `alembic downgrade 039` with this change's code, and upgrades to head again
- **THEN** after the downgrade, `task_settings` SHALL have exactly 039's shape (039's reconcile accepts it), with every pre-existing row's 039 columns unchanged and the three new columns' values gone
- **AND** after the second upgrade, `alembic check` SHALL report "No new upgrade operations detected"

#### Scenario: The schema agrees with the model and runs as a non-superuser
- **WHEN** `make test-schema` runs, `make db-check` runs after deploy, and `test_nonsuperuser_migrations_pg.py` runs 040 as the database-owning role
- **THEN** each SHALL succeed, and both checks SHALL report "No new upgrade operations detected"
