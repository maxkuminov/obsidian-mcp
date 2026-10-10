# database-least-privilege Specification

## Purpose
TBD - created by archiving change compose-db-roles. Update Purpose after archive.
## Requirements
### Requirement: Both Compose bundles SHALL separate the bootstrap superuser from a non-superuser runtime role

In `docker-compose.simple.yml` and `docker-compose.proxy.yml`, the postgres service SHALL set `POSTGRES_USER` to `postgres`, distinct from the application's database user `obsidian_mcp`. A privileged initialisation script SHALL create `obsidian_mcp` as `LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS`, create database `obsidian_mcp` owned by it, and install the `vector` extension in that database as the superuser. The application service's `DATABASE_URL` SHALL name `obsidian_mcp` and SHALL be set by the compose file's `environment`, so a `DATABASE_URL` in `.env` cannot override it. The postgres services of the two bundles SHALL be identical.

#### Scenario: Fresh volume gets role separation

- **WHEN** either bundle is brought up against an empty `pg_data` volume with valid passwords
- **THEN** `obsidian_mcp` SHALL exist with `rolsuper`, `rolcreatedb`, `rolcreaterole`, `rolreplication` and `rolbypassrls` all false, database `obsidian_mcp` SHALL be owned by `obsidian_mcp`, extension `vector` SHALL be installed in it and owned by `postgres`, and the application's session SHALL report `current_user = 'obsidian_mcp'`

#### Scenario: Compose files encode the separation

- **WHEN** the configuration test parses both compose files as YAML
- **THEN** in each, the postgres `POSTGRES_USER` SHALL differ from the user in the application's `DATABASE_URL`, the init mount SHALL reference the role-creating script and that script SHALL exist, and the two postgres service definitions SHALL be equal

#### Scenario: Healthcheck does not report the init-phase server as ready

- **WHEN** the configuration test reads the postgres healthcheck in each bundle
- **THEN** the command SHALL invoke `pg_isready` over TCP (`-h 127.0.0.1`), not over the Unix socket

### Requirement: The administrative database password MUST NOT reach the application container

The superuser password SHALL be supplied only through `postgres.env`, which SHALL be listed in the postgres service's `env_file` and in no other service's. The application service's `environment` SHALL set `POSTGRES_PASSWORD` to the empty string, so that a value in `.env` cannot reach the application container through `env_file`. `postgres.env` SHALL be gitignored and a `postgres.env.example` SHALL be tracked. At startup the server SHALL log one WARNING naming `postgres.env` when `POSTGRES_PASSWORD` is present in its own environment.

#### Scenario: Only the postgres service loads the admin file

- **WHEN** the configuration test parses both compose files
- **THEN** the postgres service's `env_file` SHALL include `postgres.env`, no other service's `env_file` SHALL include it, and the application service's `environment` SHALL map `POSTGRES_PASSWORD` to the empty string

#### Scenario: A stale admin password in .env does not reach the app

- **WHEN** `docker compose config` renders either bundle from a project directory whose `.env` sets `POSTGRES_PASSWORD` to a sentinel value and whose `postgres.env` sets a different one
- **THEN** the sentinel SHALL NOT appear anywhere in the application service's rendered environment, and the postgres service's rendered `POSTGRES_PASSWORD` SHALL be the `postgres.env` value

#### Scenario: A leftover admin password in the app environment is flagged

- **WHEN** the server starts with a non-empty `POSTGRES_PASSWORD` in its process environment
- **THEN** it SHALL log exactly one WARNING naming `postgres.env` and not containing the value, and SHALL continue starting

### Requirement: The Compose bundles MUST NOT start PostgreSQL with a missing, placeholder or weak password

Neither bundle SHALL contain a default value (`:-`) for any password variable or the literal `changeme`. `OBSIDIAN_DB_PASSWORD` SHALL be interpolated with `${OBSIDIAN_DB_PASSWORD:?…}` wherever it is used. The postgres service's entrypoint SHALL be a wrapper that, before invoking the image's `docker-entrypoint.sh`, exits non-zero unless `POSTGRES_PASSWORD` and `OBSIDIAN_DB_PASSWORD` are each set, at least 24 characters long, and not a shipped placeholder (compared case-insensitively after trimming), `OBSIDIAN_DB_PASSWORD` contains only `A-Z a-z 0-9 . _ ~ -`, and the two differ. Its error output SHALL name each failing variable and SHALL NOT contain any password value. The initialisation script SHALL record `started` in `$PGDATA/obsidian-mcp-init.state` before its first statement and replace it atomically with `complete` after its last; the wrapper SHALL exit non-zero, with instructions to recreate the volume, when `$PGDATA/PG_VERSION` exists and the marker exists without reading `complete`. A data directory with no marker SHALL be allowed to start.

#### Scenario: Unset runtime password fails at configuration time

- **WHEN** `docker compose -f <bundle> config` runs with `OBSIDIAN_DB_PASSWORD` unset
- **THEN** it SHALL exit non-zero with the `:?` message naming `OBSIDIAN_DB_PASSWORD`

#### Scenario: Missing admin file fails at configuration time

- **WHEN** `docker compose -f <bundle> config` runs with no `postgres.env` present
- **THEN** it SHALL exit non-zero

#### Scenario: Weak values are refused before initdb

- **WHEN** the wrapper runs with a stub `docker-entrypoint.sh`, for each of: `POSTGRES_PASSWORD` empty; `OBSIDIAN_DB_PASSWORD` of 23 characters; `POSTGRES_PASSWORD=CHANGE_ME` padded with spaces; `OBSIDIAN_DB_PASSWORD` containing `@`; both passwords equal
- **THEN** it SHALL exit non-zero, SHALL NOT invoke the stub, SHALL name the failing variable, and its output SHALL NOT contain the supplied value

#### Scenario: Valid values reach the image entrypoint unchanged

- **WHEN** the wrapper runs with two distinct 32-character hex passwords and arguments `postgres`
- **THEN** it SHALL `exec` the stub with exactly the arguments `postgres`

#### Scenario: A half-initialised volume is refused on the next start

- **WHEN** the initialisation script fails after recording `started` and the postgres container is started again on the same volume
- **THEN** the wrapper SHALL exit non-zero before invoking `docker-entrypoint.sh`, and its output SHALL name the volume as half-initialised and say how to recreate it

#### Scenario: A pre-existing volume without a marker still starts

- **WHEN** the wrapper runs over a data directory that has `PG_VERSION` and no marker
- **THEN** it SHALL `exec` the image entrypoint

#### Scenario: No usable fallback remains in the files

- **WHEN** the configuration test scans both compose files' text
- **THEN** no `${…PASSWORD…:-…}` form and no occurrence of `changeme` (case-insensitive) SHALL be found

### Requirement: The server MUST refuse to run as a PostgreSQL superuser unless explicitly allowed

During startup, after the database transport assertion and not in `MCP_SANDBOX_MODE`, the server SHALL query `rolsuper` for `current_user`. If it is true and `DATABASE_ALLOW_SUPERUSER` is false (the default), the server SHALL log one CRITICAL line that names the role and points at `DATABASE_ALLOW_SUPERUSER` and the DEPLOYMENT.md upgrade section, and SHALL exit before serving any request. If it is true and the setting is true, the server SHALL log one WARNING and continue.

#### Scenario: Superuser session is refused by default

- **WHEN** the server starts connected as a role with `rolsuper = true` and `DATABASE_ALLOW_SUPERUSER` unset
- **THEN** startup SHALL fail, the log SHALL contain one CRITICAL line naming the role and `DATABASE_ALLOW_SUPERUSER`, and no request SHALL be served

#### Scenario: Explicit opt-out allows it with a warning

- **WHEN** the server starts connected as a superuser with `DATABASE_ALLOW_SUPERUSER=true`
- **THEN** startup SHALL complete and one WARNING naming the role SHALL be logged

#### Scenario: Non-superuser session starts silently

- **WHEN** the server starts connected as a role with `rolsuper = false`
- **THEN** startup SHALL complete and no superuser-related log line SHALL be emitted

#### Scenario: Sandbox mode skips the check

- **WHEN** the server starts with `MCP_SANDBOX_MODE=true`
- **THEN** no `rolsuper` query SHALL be issued

### Requirement: The server MUST reject a placeholder database password

`Settings` SHALL reject a `DATABASE_URL` whose password, trimmed and case-folded, is `changeme` or `change_me`, with an error naming `DATABASE_URL` and the generation command but not the URL. An absent or empty password SHALL be accepted. The default `database_url` SHALL contain no password.

#### Scenario: Shipped placeholders are refused

- **WHEN** `Settings` is constructed with `DATABASE_URL` passwords `CHANGE_ME`, `changeme` and ` Change_Me ` (percent-encoded spaces)
- **THEN** each SHALL raise a validation error naming `DATABASE_URL` whose text does not contain the URL

#### Scenario: Passwordless and real passwords are accepted

- **WHEN** `Settings` is constructed with a URL without a password, and with a 32-character hex password
- **THEN** both SHALL validate

#### Scenario: Default carries no credential

- **WHEN** `Settings` is constructed with `DATABASE_URL` unset
- **THEN** the parsed `database_url` SHALL have no password

### Requirement: Existing Compose clusters SHALL be converted only by an operator-run script that reaches the fresh-install shape atomically

`docker/upgrade-split-db-roles.sql` SHALL, when run as documented against a cluster whose OID-10 superuser is `obsidian_mcp`, rename that role to `postgres` with the supplied admin password, create a non-superuser `obsidian_mcp` with the supplied runtime password, and transfer to it ownership of database `obsidian_mcp` and of every user object (OID at or above 16384, outside the dependency closure of an installed extension) and every default-privilege entry the old role held in that database, in one transaction that ends in a self-check and rolls back entirely if the check fails. It SHALL refuse to start when either password variable is unset or empty or the cluster is in neither the pre-split nor the split shape. After the commit it SHALL terminate every other client session whose role is OID 10, and SHALL exit non-zero, naming a restart of the postgres container, if any such session remains. On a cluster whose role names show the split shape it SHALL remove a temporary role left by an interrupted earlier run and then run the same self-check as the conversion (which also refuses an `obsidian_mcp` that is a direct or nested member of a role with `SUPERUSER`, `CREATEDB`, `CREATEROLE`, `REPLICATION` or `BYPASSRLS`, or of `pg_execute_server_program`, `pg_read_server_files` or `pg_write_server_files`, and an installed extension not owned by OID 10), exiting 0 reporting "already split" only if that check passes and otherwise exiting non-zero with a message naming each failed condition, changing no role or owner. It SHALL turn off statement and error-statement logging in every session that sends a password, and SHALL leave no temporary role behind on success. No compose file, entrypoint or server code SHALL run it or alter an existing cluster's roles.

#### Scenario: Pre-split cluster is converted

- **WHEN** a `pgvector/pgvector:pg16` volume initialised with `POSTGRES_USER=obsidian_mcp`, migrated to head and holding a row, is upgraded by the documented command
- **THEN** OID 10 SHALL be named `postgres` and authenticate with the admin password, `obsidian_mcp` SHALL be a non-superuser, the new runtime password SHALL authenticate, the row SHALL be readable, `alembic check` as `obsidian_mcp` SHALL be clean, and no role named `obsidian_mcp_split_tmp` SHALL exist

#### Scenario: Every promised object category changes owner

- **WHEN** the pre-split database additionally holds, created by the old superuser, a free sequence, a column-owned sequence, a function, a procedure, an enum, a domain, a standalone composite type, a view, a materialized view, an extra schema with a table, and database-wide and in-schema default privileges, and is upgraded by the documented command
- **THEN** the catalogs SHALL show `obsidian_mcp` owning the database, every user schema, every table including `alembic_version`, every sequence, and every seeded routine and type; no user object in any catalog SHALL remain owned by OID 10; the `vector` extension and every member of its dependency closure SHALL remain owned by `postgres`; and no `pg_default_acl` row SHALL name OID 10 while equivalent rows SHALL exist for `obsidian_mcp`

#### Scenario: A left-behind temporary role is removed

- **WHEN** the script runs against a split cluster on which `obsidian_mcp_split_tmp` exists
- **THEN** it SHALL remove that role, print "already split" and exit 0

#### Scenario: Re-run is a no-op

- **WHEN** the script runs a second time against the converted cluster
- **THEN** it SHALL print "already split", exit 0, and change no role or ownership

#### Scenario: Missing input aborts before any change

- **WHEN** the script runs without `app_pw` set, or without `admin_pw` set, or with either passed but empty
- **THEN** it SHALL exit non-zero naming the missing variable, and every role and owner SHALL be unchanged

#### Scenario: A session of the old superuser identity does not survive

- **WHEN** a TCP session authenticated as the pre-split `obsidian_mcp` with the old password is open while the documented command converts the cluster
- **THEN** the script SHALL exit 0, that session SHALL be disconnected, no other client session with role OID 10 SHALL remain, and neither `obsidian_mcp` nor `postgres` SHALL accept the old password

#### Scenario: A partly split cluster is refused

- **WHEN** the script runs against a cluster whose bootstrap superuser is `postgres` and whose `obsidian_mcp` is not a superuser, but where database `obsidian_mcp` is owned by `postgres`, or `obsidian_mcp` has `CREATEDB` or `CREATEROLE`, or `obsidian_mcp` is a member, directly or through another role, of a role with `SUPERUSER`, `CREATEDB`, `CREATEROLE`, `REPLICATION` or `BYPASSRLS`, or of `pg_execute_server_program`, `pg_read_server_files` or `pg_write_server_files`, or an installed extension is owned by a role other than OID 10, or a user object or a default-privilege entry is owned by OID 10
- **THEN** it SHALL exit non-zero without printing "already split", its message SHALL name each such condition, and every role and owner SHALL be unchanged apart from the removal of a left-behind temporary role

#### Scenario: A fresh install passes the re-run check

- **WHEN** the script runs against a volume initialised by the bundle's postgres service
- **THEN** it SHALL print "already split" and exit 0

#### Scenario: Nothing runs it automatically

- **WHEN** the configuration test inspects both compose files and the postgres wrapper
- **THEN** none SHALL reference `upgrade-split-db-roles`

### Requirement: An unconverted existing install MUST fail loudly rather than serve

On a pre-split volume brought up with the new compose file, the application SHALL NOT serve. If authentication as `obsidian_mcp` fails (SQLSTATE `28P01` or `28000` anywhere in the raised exception's `orig` / `__cause__` / `__context__` chain), `alembic/env.py` SHALL print one line naming `docker/upgrade-split-db-roles.sql` and the DEPLOYMENT.md section before re-raising. If authentication succeeds because the role is still the superuser, the superuser refusal SHALL stop the server.

#### Scenario: New password on old volume

- **WHEN** `alembic upgrade head` connects to a real PostgreSQL server with a wrong password, so that SQLAlchemy raises its own connection exception wrapping asyncpg's
- **THEN** stderr SHALL contain exactly one line naming `docker/upgrade-split-db-roles.sql`, and the process SHALL exit non-zero

#### Scenario: Other connection failures get no hint

- **WHEN** migrations fail to connect for a reason whose SQLSTATE chain contains neither `28P01` nor `28000`
- **THEN** the hint SHALL NOT be printed and the original exception SHALL propagate

#### Scenario: Old password reused on old volume

- **WHEN** the server starts connected as the still-superuser `obsidian_mcp`
- **THEN** the superuser refusal SHALL stop it before serving

### Requirement: Application migrations MUST run to head without superuser privilege

`alembic upgrade head` SHALL succeed, and `alembic check` SHALL then be clean, when run as a `NOSUPERUSER` role that owns the target database, where `vector` was installed beforehand by a superuser and the role holds only the default PUBLIC privileges otherwise.

#### Scenario: Non-superuser owner migrates a fresh database

- **WHEN** the integration test creates a `NOSUPERUSER` role, a database it owns with `vector` installed by `postgres`, and runs `alembic upgrade head` then `alembic check` as that role
- **THEN** both SHALL succeed, the session SHALL report `rolsuper = false`, every table in `public` SHALL be owned by that role, and the server's startup superuser check SHALL pass for that session

