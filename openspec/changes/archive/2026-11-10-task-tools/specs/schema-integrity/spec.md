## ADDED Requirements

### Requirement: Migration 039 SHALL own the task-id columns, the task-refresh columns and `task_settings` as marked, reconcilable units with no backfill

Migration 039 (`down_revision` `"038"`) SHALL add the following to `note_tasks`, each nullable unless stated:

- `task_key` char(12);
- `key_ordinal` integer;
- `key_count` integer;
- `description_key` char(16);
- `text_fold` text;
- `tag_prefixes` varchar(128)[];
- `recurrence` varchar(256);
- `recurrence_cut` boolean NOT NULL default false;
- `created` date;
- `cancelled` date;
- `context` text;
- `context_cut` boolean NOT NULL default false.

It SHALL also add:

- a CHECK `ck_note_tasks_key_shape` requiring `task_key`, `key_ordinal` and `key_count` to be all NULL, or all set with `1 ≤ key_ordinal ≤ key_count`;
- a GIN index on `tag_prefixes`;
- a partial index on `description_key` where `state = 'open'`.

It SHALL add `notes_metadata.tasks_source_hash` varchar(64) NULL and `notes_metadata.tasks_refreshed_at` timestamptz NULL.

It SHALL create `task_settings` with these columns:

- `id` integer primary key;
- `user_id` integer NULL, a foreign key to `users.id` `ON DELETE CASCADE`, unique with NULLs not distinct;
- `timezone` varchar(64) NULL;
- `excluded_folders` varchar(1024)[] NOT NULL default empty;
- `excluded_note_tags` varchar(128)[] NOT NULL default empty;
- `set_done_date`, `set_cancelled_date`, `set_created_date` and `next_recurrence_below`, each boolean NULL;
- `updated_at` timestamptz NOT NULL default now().

**Markers and mirroring.** Every added column, the CHECK, both indexes and the table SHALL carry a `(039_task_tools)` ownership marker, and every one SHALL be mirrored in `src/models/db.py` so that `alembic check` is clean.

**Migration behaviour.** The migration:

- SHALL write no data;
- SHALL refuse by name a same-named object of a different shape, and SHALL reconcile without change one of the exact shape carrying the marker;
- SHALL set `lock_timeout` and `statement_timeout` and `RESET` them;
- SHALL drop, in `downgrade()`, only marked objects.

**Classification.** `task_settings` SHALL be classified as durable for backups. `note_tasks` stays derived.

#### Scenario: Fresh upgrade
- **WHEN** a database at 038 is upgraded to 039
- **THEN** every existing `note_tasks` row SHALL read NULL in the new nullable columns and false in the two new booleans, and `task_settings` SHALL exist empty with its marker

#### Scenario: Stamp-back re-runs cleanly
- **WHEN** the schema gate stamps the database back to 038 and upgrades again
- **THEN** 039 SHALL reconcile every marked object without error and write nothing

#### Scenario: An impostor is refused
- **WHEN** an unmarked `task_settings` table, or a `task_key` column of another type, already exists
- **THEN** the migration SHALL fail naming the object and change nothing

#### Scenario: The key shape is enforced
- **WHEN** a row is inserted with `task_key` set and `key_count` NULL, or with `key_ordinal` 3 and `key_count` 2
- **THEN** the insert SHALL fail on `ck_note_tasks_key_shape`

#### Scenario: The schema agrees with the model and runs as a non-superuser
- **WHEN** `make test-schema` runs, `make db-check` runs after deploy, and `test_nonsuperuser_migrations_pg.py` runs 039 as the database-owning role
- **THEN** each SHALL succeed, and both checks SHALL report "No new upgrade operations detected"

#### Scenario: Settings survive a backup and are not treated as derived
- **WHEN** `make db-backup` runs
- **THEN** `task_settings` rows SHALL be dumped with their data, and `tests/test_backup_derived_tables.py` SHALL list `task_settings` among the durable tables
