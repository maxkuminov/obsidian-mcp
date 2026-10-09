## Why

Refs #324 (ASVS V13.2.2 / V13.2.3, medium).

Both bundled Compose deployments (`docker-compose.simple.yml`, `docker-compose.proxy.yml`) set `POSTGRES_USER=obsidian_mcp` and the app connects as that same identity. The official PostgreSQL image creates `POSTGRES_USER` as the **bootstrap cluster superuser**, so the application process holds cluster administration: a compromised app, or a peer on `mcp_internal` holding the app's credential, can read every database, run `COPY ... PROGRAM` (a shell inside the postgres container) and alter roles. Both files also fall back to `${POSTGRES_PASSWORD:-changeme}`, and `Settings.database_url` defaults to the same `changeme` credential, so an operator who omits the variables gets a working stack with a guessable superuser password.

The Kubernetes bundle already does it right (`deploy/kubernetes/postgres/configmap-initdb.yaml`): a distinct bootstrap superuser creates a non-superuser role that owns the app database, and installs pgvector in the privileged init phase. Migration 001's `CREATE EXTENSION IF NOT EXISTS vector` is then a no-op that needs no privilege. This change gives the Compose bundles the same shape.

Production runs on the homelab k3s cluster with the Kubernetes bundle and is **unaffected**. The maintainer's homelab `docker-compose.yml` uses an external shared Postgres where `make db-init` already creates a non-superuser role, and is also unaffected.

## What Changes

- **Two identities in both bundles.** The postgres service's bootstrap superuser is `postgres`, with password `POSTGRES_PASSWORD` read from a new `postgres.env` file that only the postgres service loads. A privileged init script creates `obsidian_mcp` as a `LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS` role, creates database `obsidian_mcp` owned by it, and installs `vector` as the superuser. The app container receives only the runtime credential: compose builds its `DATABASE_URL` from `OBSIDIAN_DB_PASSWORD`, and the admin password never enters the app container: the app service also sets `POSTGRES_PASSWORD` to the empty string, so a value left in `.env` from the old docs is overridden.
- **No usable default credential.** The `changeme` fallbacks are removed. `OBSIDIAN_DB_PASSWORD` is interpolated with `${VAR:?message}`, so `docker compose config` and `up` fail before anything starts. `postgres.env` is a required `env_file`, so a missing file also fails at config time. A wrapper entrypoint on the postgres service validates both passwords (present, at least 24 characters, URL-safe characters for the runtime one, not a shipped placeholder, not equal to each other) **before** `docker-entrypoint.sh` runs `initdb`, so a weak value never initialises a volume. The init script writes a two-state completion marker into the data directory, and the wrapper refuses a volume whose initialisation started and never finished, with fresh-volume recovery instructions.
- **The server refuses a superuser session and a placeholder password.** At startup (after the transport assertion, skipped in `MCP_SANDBOX_MODE`) the server reads `rolsuper` for `current_user` and exits with a message naming the upgrade procedure if the role is a superuser, unless `DATABASE_ALLOW_SUPERUSER=true`. `Settings` rejects a `DATABASE_URL` whose password is a shipped placeholder (`changeme` / `CHANGE_ME`, ignoring case and surrounding whitespace, mirroring the `SECRET_KEY` rule), and the default `database_url` carries no password.
- **Existing Compose installs get a documented one-time script; nothing automatic.** `docker/upgrade-split-db-roles.sql` converts a cluster initialised before this change (bootstrap superuser named `obsidian_mcp`) into the fresh-install shape: the bootstrap role is renamed to `postgres`, a new non-superuser `obsidian_mcp` is created and given ownership of the database and every non-extension object in it, default privileges are moved too, and a final self-check aborts the transaction if any user object or default-privilege row is still the superuser's. A re-run on a split cluster removes a left-behind temporary role and reports "already split". An existing install that takes the new compose file without running the script **fails loudly** (design D6) with a message pointing at the upgrade note.
- **Migrations proven to run without superuser.** A real-Postgres test runs `alembic upgrade head` and `alembic check` as a non-superuser owner role over a database where only the superuser installed `vector`.
- **Configuration tests** parse both compose variants and assert role separation, required password inputs, the admin secret's absence from the app service, and the init wiring.
- Docs: `DEPLOYMENT.md` (setup and "Upgrading: split database roles"), README Quick start and Upgrading, `.env.example` and a new `postgres.env.example`, `docs/architecture/schema-and-migrations.md` (role model), a `CLAUDE.md` key-decisions bullet; `make init` fills the new variables.

**BREAKING** for existing Compose installs (one-time script) and for any other deployment that connects as a superuser (set `DATABASE_ALLOW_SUPERUSER=true`, or better, switch to a non-superuser role).

## Capabilities

### New Capabilities
- `database-least-privilege`: the bundled deployments' role separation and credential inputs, the server's refusal of a superuser session and of placeholder database passwords, the existing-install upgrade script, and the guarantee that migrations run without superuser.

### Modified Capabilities
<!-- none -->

## Impact

- `docker-compose.simple.yml`, `docker-compose.proxy.yml`: postgres env, entrypoint, healthcheck and init mount; the app's `environment: DATABASE_URL`.
- New: `docker/postgres-entrypoint.sh` (validation wrapper), `docker/db-init-compose.sh` (replaces `docker/db-init-simple.sql`), `docker/upgrade-split-db-roles.sql`, `postgres.env.example`; `.gitignore` gains `postgres.env`.
- `src/config.py` (default URL, placeholder-password validator, `database_allow_superuser`), `src/main.py` lifespan (superuser assertion), `alembic/env.py` (authentication-failure hint).
- Tests: `tests/test_compose_db_roles.py` (new), config tests, a lifespan unit test, `tests/integration/test_nonsuperuser_migrations_pg.py` (new). Integration harnesses that drive the lifespan as `postgres` set `DATABASE_ALLOW_SUPERUSER=true`.
- `Makefile` (`init`), `.env.example`, `DEPLOYMENT.md`, `README.md`, `docs/architecture/schema-and-migrations.md`, `CLAUDE.md`.
- Not touched: `docker-compose.yml` (homelab reference, external Postgres), `deploy/kubernetes/**`, every migration.
