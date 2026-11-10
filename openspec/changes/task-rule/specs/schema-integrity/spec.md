## ADDED Requirements

### Requirement: Migration 040 SHALL own the task-rule columns of `task_settings` as marked, reconcilable units with no data written

Migration 040 (`down_revision` `"039"`) SHALL add to `task_settings`:

- `task_rule` varchar(8) NULL;
- `task_tags` varchar(128)[] NOT NULL default empty;
- `global_filter` varchar(64) NULL;
- a CHECK `ck_task_settings_task_rule` admitting only `all`, `fields` and `filter` (and NULL).

**Markers and mirroring.** Every added column and the CHECK SHALL carry a `(040_task_rule)` ownership marker, and every one SHALL be mirrored in `src/models/db.py` so that `alembic check` is clean.

**Migration behaviour.** The migration:

- SHALL write no data;
- SHALL refuse by name a same-named object of a different shape or without the marker, and SHALL reconcile without change one of the exact shape carrying the marker;
- SHALL set `lock_timeout` and `statement_timeout` and `RESET` them;
- SHALL drop, in `downgrade()`, only marked objects.

**039's allow-list.** Migration 039's comparison of `task_settings` SHALL leave out exactly these four units, each only when it carries its exact 040 marker. Every other extra column or constraint, and any of these names with another comment, SHALL still be refused by 039.

**Rolling deploy.** The previous image SHALL keep working against the 040 schema, because every new column is nullable or defaulted.

#### Scenario: Fresh upgrade
- **WHEN** a database at 039 with `task_settings` rows is upgraded to 040
- **THEN** every existing row SHALL read `task_rule` NULL, `task_tags` empty and `global_filter` NULL, and no row SHALL have been updated

#### Scenario: Stamp-back re-runs cleanly through both revisions
- **WHEN** the schema gate stamps a head database back to 039 and upgrades, and separately stamps it back to 038 and upgrades
- **THEN** 040 and 039 SHALL each reconcile without error and write nothing

#### Scenario: Impostors are refused
- **WHEN** an unmarked `task_rule` column, a `task_tags` column of type varchar(64)[], or an unmarked `ck_task_settings_task_rule` already exists
- **THEN** 040 SHALL fail naming the object and change nothing
- **AND** a re-run of 039 over a `task_settings` table holding an unmarked extra column SHALL fail naming it

#### Scenario: The rule value is constrained
- **WHEN** a row is written with `task_rule = 'any'`
- **THEN** the write SHALL fail on `ck_task_settings_task_rule`

#### Scenario: The previous image still saves settings
- **WHEN** the previous image's settings save runs against the 040 schema
- **THEN** it SHALL succeed, and the new columns SHALL keep NULL, empty and NULL

#### Scenario: The schema agrees with the model and runs as a non-superuser
- **WHEN** `make test-schema` runs, `make db-check` runs after deploy, and `test_nonsuperuser_migrations_pg.py` runs 040 as the database-owning role
- **THEN** each SHALL succeed, and both checks SHALL report "No new upgrade operations detected"

#### Scenario: Downgrade drops only what 040 owns
- **WHEN** `alembic downgrade 039` runs
- **THEN** the three columns and the CHECK SHALL be gone, and every other `task_settings` column and row SHALL be unchanged
