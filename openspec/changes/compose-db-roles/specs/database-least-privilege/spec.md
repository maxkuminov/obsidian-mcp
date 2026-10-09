## ADDED Requirements

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

The superuser password SHALL be supplied only through `postgres.env`, which SHALL be listed in the postgres service's `env_file` and in no other service's. The application service's `environment` SHALL NOT contain `POSTGRES_PASSWORD`. `postgres.env` SHALL be gitignored and a `postgres.env.example` SHALL be tracked. At startup the server SHALL log one WARNING naming `postgres.env` when `POSTGRES_PASSWORD` is present in its own environment.

#### Scenario: Only the postgres service loads the admin file

- **WHEN** the configuration test parses both compose files
- **THEN** the postgres service's `env_file` SHALL include `postgres.env`, no other service's `env_file` SHALL include it, and no other service's `environment` SHALL contain the key `POSTGRES_PASSWORD`

#### Scenario: A leftover admin password in the app environment is flagged

- **WHEN** the server starts with `POSTGRES_PASSWORD` set in its process environment
- **THEN** it SHALL log exactly one WARNING naming `postgres.env` and not containing the value, and SHALL continue starting

### Requirement: The Compose bundles MUST NOT start PostgreSQL with a missing, placeholder or weak password

Neither bundle SHALL contain a default value (`:-`) for any password variable or the literal `changeme`. `OBSIDIAN_DB_PASSWORD` SHALL be interpolated with `${OBSIDIAN_DB_PASSWORD:?…}` wherever it is used. The postgres service's entrypoint SHALL be a wrapper that, before invoking the image's `docker-entrypoint.sh`, exits non-zero unless `POSTGRES_PASSWORD` and `OBSIDIAN_DB_PASSWORD` are each set, at least 24 characters long, and not a shipped placeholder (compared case-insensitively after trimming), `OBSIDIAN_DB_PASSWORD` contains only `A-Z a-z 0-9 . _ ~ -`, and the two differ. Its error output SHALL name each failing variable and SHALL NOT contain any password value.

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

`docker/upgrade-split-db-roles.sql` SHALL, when run as documented against a cluster whose OID-10 superuser is `obsidian_mcp`, rename that role to `postgres` with the supplied admin password, create a non-superuser `obsidian_mcp` with the supplied runtime password, and transfer to it ownership of database `obsidian_mcp` and every non-extension object in its non-system schemas, in one transaction that ends in a self-check and rolls back entirely if the check fails. It SHALL refuse to start when either password variable is unset or the cluster is in neither the pre-split nor the split shape, SHALL exit 0 reporting "already split" on a split cluster, and SHALL leave no temporary role behind on success. No compose file, entrypoint or server code SHALL run it or alter an existing cluster's roles.

#### Scenario: Pre-split cluster is converted

- **WHEN** a `pgvector/pgvector:pg16` volume initialised with `POSTGRES_USER=obsidian_mcp`, migrated to head and holding a row, is upgraded by the documented command
- **THEN** OID 10 SHALL be named `postgres` and authenticate with the admin password, `obsidian_mcp` SHALL be a non-superuser owning the database and every table, the old password SHALL be rejected for `obsidian_mcp`, the new one SHALL authenticate, the row SHALL be readable, `alembic check` as `obsidian_mcp` SHALL be clean, and no role named `obsidian_mcp_split_tmp` SHALL exist

#### Scenario: Re-run is a no-op

- **WHEN** the script runs a second time against the converted cluster
- **THEN** it SHALL print "already split", exit 0, and change no role or ownership

#### Scenario: Missing input aborts before any change

- **WHEN** the script runs without `app_pw` set
- **THEN** it SHALL exit non-zero and every role and owner SHALL be unchanged

#### Scenario: Nothing runs it automatically

- **WHEN** the configuration test inspects both compose files and the postgres wrapper
- **THEN** none SHALL reference `upgrade-split-db-roles`

### Requirement: An unconverted existing install MUST fail loudly rather than serve

On a pre-split volume brought up with the new compose file, the application SHALL NOT serve. If authentication as `obsidian_mcp` fails, `alembic/env.py` SHALL print one line naming `docker/upgrade-split-db-roles.sql` and the DEPLOYMENT.md section before re-raising. If authentication succeeds because the role is still the superuser, the superuser refusal SHALL stop the server.

#### Scenario: New password on old volume

- **WHEN** migrations connect and asyncpg raises `InvalidPasswordError`
- **THEN** stderr SHALL contain one line naming `docker/upgrade-split-db-roles.sql`, and the process SHALL exit non-zero

#### Scenario: Old password reused on old volume

- **WHEN** the server starts connected as the still-superuser `obsidian_mcp`
- **THEN** the superuser refusal SHALL stop it before serving

### Requirement: Application migrations MUST run to head without superuser privilege

`alembic upgrade head` SHALL succeed, and `alembic check` SHALL then be clean, when run as a `NOSUPERUSER` role that owns the target database, where `vector` was installed beforehand by a superuser and the role holds only the default PUBLIC privileges otherwise.

#### Scenario: Non-superuser owner migrates a fresh database

- **WHEN** the integration test creates a `NOSUPERUSER` role, a database it owns with `vector` installed by `postgres`, and runs `alembic upgrade head` then `alembic check` as that role
- **THEN** both SHALL succeed, the session SHALL report `rolsuper = false`, and every table in `public` SHALL be owned by that role
