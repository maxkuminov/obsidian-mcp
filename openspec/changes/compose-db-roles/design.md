## Context

Today both Compose bundles run `pgvector/pgvector:pg16` with `POSTGRES_USER=obsidian_mcp`, `POSTGRES_DB=obsidian_mcp` and `POSTGRES_PASSWORD=${POSTGRES_PASSWORD:-changeme}`, mount `docker/db-init-simple.sql` (one line: `CREATE EXTENSION IF NOT EXISTS vector`) as an initdb script, and load the whole `.env` into the app container, whose `DATABASE_URL` the operator is told to keep in sync by hand. The image creates `POSTGRES_USER` as the bootstrap superuser (OID 10), so the app *is* the cluster superuser.

The Kubernetes bundle (`deploy/kubernetes/postgres/`) is the reference: bootstrap superuser `postgres` with `superuser-password`, an initdb shell script that creates role `obsidian_mcp` (LOGIN, not superuser) and database `obsidian_mcp OWNER obsidian_mcp` from `APP_DB_PASSWORD` via psql variables, then installs `vector` in that database as the superuser. Migration 001's `CREATE EXTENSION IF NOT EXISTS vector` is then a no-op.

Nothing in `alembic/versions/` creates an extension other than 001 (no-op once installed), alters a role, changes a server setting or touches another database. 013, 019, 023 and 027 need the database's TEMP privilege, which PUBLIC has by default. Every table is created by the connecting role, so the role that runs migrations owns the schema.

## Goals / Non-Goals

**Goals:** a non-superuser runtime role in both Compose bundles; the admin credential kept out of the app container; no usable default password, refused before `initdb` can run; a documented, manual, one-time path for existing installs that ends in exactly the fresh-install shape; an installation that skips the script fails loudly; migrations proven to need no superuser; configuration tests over both compose files.

**Non-Goals:** a separate migrator role distinct from the runtime role (the k3s bundle does not have one either); restricting the superuser to the local socket in `pg_hba.conf`; any change to the k3s bundle, the homelab `docker-compose.yml`, or `make db-init`; automatic migration of existing clusters.

## Decisions

### D1. Role layout mirrors Kubernetes exactly

| | Fresh Compose install (after) | Existing install after the script | Kubernetes |
| --- | --- | --- | --- |
| Bootstrap superuser | `postgres` | `postgres` (the old OID-10 role, renamed) | `postgres` |
| Runtime role | `obsidian_mcp`, NOSUPERUSER | `obsidian_mcp` (new), NOSUPERUSER | `obsidian_mcp`, NOSUPERUSER |
| Database owner | `obsidian_mcp` | `obsidian_mcp` | `obsidian_mcp` |
| `vector` extension owner | `postgres` | `postgres` | `postgres` |

The runtime role is created `LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS`. The runtime role keeps its name, so `DATABASE_URL`'s user, the healthcheck's target database and every doc example stay `obsidian_mcp`; the upgrade script exists to make the existing-install row identical to the fresh one rather than leaving two naming schemes in the wild.

The postgres service sets `POSTGRES_USER: postgres` explicitly (so a test can assert it differs from the app's user) and leaves `POSTGRES_DB` unset, because the init script creates `obsidian_mcp` with its owner, as Kubernetes does.

### D2. Where each secret lives

- `OBSIDIAN_DB_PASSWORD` (runtime) lives in `.env`. Compose interpolates it into two places: the app service's `environment: DATABASE_URL: postgresql+asyncpg://obsidian_mcp:${OBSIDIAN_DB_PASSWORD:?…}@postgres:5432/obsidian_mcp`, and the postgres service's `environment: OBSIDIAN_DB_PASSWORD`, which the init script consumes. `environment:` takes precedence over `env_file:`, so a stale `DATABASE_URL` left in `.env` is overridden. There is now one statement of the runtime password, not two kept in sync by hand.
- `POSTGRES_PASSWORD` (admin) lives in a new `postgres.env` (gitignored, `postgres.env.example` tracked, mode 600), loaded by **only** the postgres service through `env_file`. The app service loads `.env`, which must not contain it. Putting it in `.env` would inject the superuser password into the app container's environment through `env_file: .env` and undo the separation for the exact threat in #324 (a compromised app process).
- **The app service blanks `POSTGRES_PASSWORD` whatever `.env` holds** (Codex spec review, BLOCKER). An operator upgrading from the old docs has `POSTGRES_PASSWORD` in `.env` and may copy it into `postgres.env` without deleting it; the app would then still receive the live superuser password through `env_file: .env`. The app service therefore sets `environment: POSTGRES_PASSWORD: ""`. `environment:` takes precedence over `env_file:` (verified with `docker compose config`, Compose 2.40), so the rendered app environment carries an empty value regardless of `.env`. A configuration test renders both bundles with a sentinel admin password in `.env` and asserts it appears nowhere in the app service.
- The server warns once at startup if `POSTGRES_PASSWORD` is present and non-empty in its own environment (a non-bundle install that followed the old docs), naming `postgres.env`. A warning rather than a refusal, because the app cannot tell an admin password from a leftover. In the bundles the blanking above means the warning cannot fire; it exists for other deployments.

`env_file` entries are required by default in Compose, so a missing `postgres.env` makes `docker compose config` fail. Values in an `env_file` are not interpolated, so the `${…:?}` guard cannot cover the admin password; D3's wrapper does.

### D3. Validation happens before `initdb`, not in the init script

A failing `/docker-entrypoint-initdb.d` script leaves an initialised data directory behind (`PG_VERSION` exists), and the next start skips initialisation, so the volume comes up without the runtime role: a half-initialised state that is worse than a refusal. Validation therefore runs in `docker/postgres-entrypoint.sh`, set as the postgres service's `entrypoint` (with `command: ["postgres"]`), which checks and then `exec`s the image's `docker-entrypoint.sh "$@"`. It runs on every start, before any `initdb`.

Rules, applied to `POSTGRES_PASSWORD` and `OBSIDIAN_DB_PASSWORD`:

1. set and non-empty;
2. length ≥ 24;
3. not a shipped placeholder: `changeme`, `change_me`, `CHANGE_ME` and the literal values in `.env.example` / `postgres.env.example`, compared case-insensitively after trimming;
4. `OBSIDIAN_DB_PASSWORD` matches `^[A-Za-z0-9._~-]+$` (it is spliced into a URL unencoded, D2);
5. the two differ.

6. **init-completion marker** (Codex spec review, MINOR): if `$PGDATA/PG_VERSION` exists and `$PGDATA/obsidian-mcp-init.state` exists but does not read `complete`, initialisation started and did not finish. The wrapper refuses with the fresh-volume recovery instructions (the volume holds nothing yet: remove it with `docker compose down -v` or `docker volume rm`, fix the cause, start again). See D4 for who writes the marker.

On failure it prints one line per broken rule naming the variable and the file it belongs in (never the value) and exits 1. Layering: `${OBSIDIAN_DB_PASSWORD:?}` fails at `docker compose config` time; a missing `postgres.env` fails at config time; everything else fails when the postgres container starts, before `initdb`. The app container cannot start in the meantime, because it `depends_on` the postgres service being healthy.

### D4. The init script

`docker/db-init-compose.sh` replaces `docker/db-init-simple.sql`, mounted read-only at `/docker-entrypoint-initdb.d/10-obsidian-mcp.sh`. It is the Kubernetes script with the role attributes from D1: `CREATE ROLE obsidian_mcp LOGIN NOSUPERUSER … PASSWORD :'pw'` (psql variable quoting; the password is never interpolated by the shell into SQL), `CREATE DATABASE obsidian_mcp OWNER obsidian_mcp`, then `CREATE EXTENSION IF NOT EXISTS vector` in `obsidian_mcp` as the superuser. `set -euo pipefail` and `ON_ERROR_STOP=1`. Validation has already happened (D3), so this script cannot fail on input.

**Init-completion marker.** Its first action writes `started` to `$PGDATA/obsidian-mcp-init.state`; its last action replaces it with `complete` (write to a temporary file in the same directory, then `mv`, so a reader never sees a partial file). A failure anywhere in between (a `CREATE EXTENSION` that cannot find the library, a full disk) leaves `started` behind, and on the next start D3's wrapper refuses before the image skips initialisation, naming the fresh-volume recovery. The supervisor's triage asked for "PG_VERSION without the marker fails"; that literal rule would also refuse every volume initialised before this change, which carries no marker and must start so the operator can run D7's script. The marker therefore has two states and its **absence** means "not initialised by this script" (a pre-#324 volume), which the wrapper allows. A failure in the image's own initialisation before this script runs is not covered (L11).

**Healthcheck.** During initialisation the image runs a temporary server that listens on the Unix socket only. The current `pg_isready` healthcheck uses the socket and can report healthy before the init script has created the runtime role, which would start the app against a missing role. The healthcheck becomes `pg_isready -h 127.0.0.1 -U obsidian_mcp -d obsidian_mcp`; TCP is not open until initialisation has finished. (`pg_isready` does not authenticate, so the role name is cosmetic.)

### D5. The server refuses a superuser session and a placeholder password

- **Placeholder password.** A `Settings` validator parses `database_url`; if its password, trimmed and case-folded, is `changeme` or `change_me`, startup fails with a message that says how to generate one and where it is set. An empty or absent password is allowed (peer, trust or certificate authentication are legitimate outside the bundles). The default `database_url` becomes `postgresql+asyncpg://obsidian_mcp@postgres:5432/obsidian_mcp`, with no password, so an unset variable cannot authenticate with a known value.
- **Superuser session.** In the lifespan, after the database transport assertion and therefore skipped by `MCP_SANDBOX_MODE`, the server runs `SELECT rolsuper FROM pg_roles WHERE rolname = current_user` on the same engine the app uses. If true and `DATABASE_ALLOW_SUPERUSER` is false (the default), it logs one CRITICAL line and exits non-zero. The line names the connected role, says the role is a superuser, and points at `DATABASE_ALLOW_SUPERUSER` and DEPLOYMENT.md "Upgrading: split database roles". If true and the flag is set, it logs one WARNING and continues.

The check covers every deployment, not just the bundles, because the property (the long-lived process is not a superuser) is the ASVS requirement and the server is the only component that sees the real session. Kubernetes and the homelab compose already connect as a non-superuser and are unaffected. A third-party install pointed at a superuser gets a clear refusal and a one-line opt-out.

Not checked in `alembic/env.py`: migrations are a one-shot run by the operator; the integration harnesses and `make test-schema` legitimately migrate as `postgres`; and the long-lived process is guarded. `alembic/env.py` gains only the authentication hint in D6.

### D6. An existing install that skips the script fails loudly

On an initialised volume the image ignores `POSTGRES_USER`/`POSTGRES_PASSWORD` and never runs init scripts, so the new compose file starts the old cluster unchanged: bootstrap superuser `obsidian_mcp` with the old password. D3's wrapper still runs, so the operator must supply both new variables before the postgres container starts at all. Then one of two things happens:

- **`OBSIDIAN_DB_PASSWORD` differs from the old password** (the expected case: the docs say generate a new one). `alembic upgrade head` fails authentication. `alembic/env.py` catches the exception raised at connect, **whatever wraps it** (SQLAlchemy re-raises asyncpg's error as its own `DBAPIError` subclass; Codex spec review, MAJOR), walks the exception and its `orig` / `__cause__` / `__context__` chain for a SQLSTATE of `28P01` (invalid password) or `28000` (invalid authorization specification, e.g. no `pg_hba` entry or a missing role), and on a match prints one line to stderr before re-raising. Anything else re-raises untouched. A real-Postgres test connects with a wrong password through `alembic upgrade head` and asserts the line. The line is: "database authentication failed for role obsidian_mcp; a Compose install created before #324 must run docker/upgrade-split-db-roles.sql. See DEPLOYMENT.md 'Upgrading: split database roles'". The container exits and restarts; nothing serves.
- **It equals the old password.** Authentication succeeds as the superuser, `alembic upgrade head` runs (the same privilege it has always had), then D5's superuser refusal stops the server with the same pointer. Nothing serves.

**Why fail loudly rather than keep working.** "Keep working unchanged" would mean the new compose file silently serves as cluster superuser on every pre-existing volume, so the finding would stay open on exactly the installs that exist today, with nothing telling the operator. Compose cannot detect the volume's state at config time, so a compatibility mode would have to be an operator-set flag, which is `DATABASE_ALLOW_SUPERUSER` with extra steps. Failing loudly costs a restart loop with a clear message, and the operator who wants the old behaviour back for a night has an explicit opt-out (`DATABASE_ALLOW_SUPERUSER=true`, with `OBSIDIAN_DB_PASSWORD` set to the old password). That opt-out only works when the old password passes D3's wrapper (≥ 24 characters, URL-safe, not a placeholder, different from the new admin password), which an install on the old `changeme` default or any short password cannot meet; for those the rollback is the previous release with its own compose files and `.env` (verifier note, DEPLOYMENT.md "Rollback").

The postgres container itself starts normally on the old volume, which is what lets the operator run the script against it with `docker compose exec`.

### D7. The one-time upgrade script

`docker/upgrade-split-db-roles.sql`, run by the operator, never automatically:

```sh
docker compose -f <bundle> stop obsidian-mcp
docker compose -f <bundle> exec -T postgres sh -c \
  'psql -X -v ON_ERROR_STOP=1 -U obsidian_mcp -d obsidian_mcp \
        -v admin_pw="$POSTGRES_PASSWORD" -v app_pw="$OBSIDIAN_DB_PASSWORD"' \
  < docker/upgrade-split-db-roles.sql
docker compose -f <bundle> restart postgres
docker compose -f <bundle> up -d
```

The app is stopped before anything else (DEPLOYMENT.md makes it the first step, before the backup), and the postgres container is restarted after the conversion and before the app starts, so no session from before the conversion survives in any form (Codex review, MAJOR; see step 3 below).

The passwords come from the **new** postgres container's environment (D2/D3, already validated), so they never appear on the host command line. The docs put a `pg_dump` backup step first.

Constraints that shape the script:

- The bootstrap role (OID 10) cannot lose `SUPERUSER`, and neither the session user nor the current user can be renamed. The only way to reach D1's shape is to rename the OID-10 role from another superuser session.
- `REASSIGN OWNED BY` refuses the bootstrap role (its catalog objects are pinned), so ownership moves object by object.

Steps:

1. **Preflight** (as `obsidian_mcp`, through the container's local socket). The script requires `admin_pw` and `app_pw` to be set (checked client-side, so nothing is sent yet) and, once in a superuser session with logging turned off (below), non-empty. If `obsidian_mcp` exists with `rolsuper = false` and `postgres` exists, the cluster *looks* split: the script reconnects as `postgres` (`\c obsidian_mcp postgres`), drops `obsidian_mcp_split_tmp` **if it exists** (a previous run committed the conversion but stopped before its last step; Codex spec review, MINOR), then runs **the same self-check as the conversion** (step 2's last bullet) in a transaction that changes nothing. Only if it passes does it print "already split" and exit 0 (idempotent re-run, and the recovery path for a left-behind temporary superuser). A partly-converted cluster (database still owned by `postgres`, `obsidian_mcp` with `CREATEDB`/`CREATEROLE` or another attribute, user objects or default privileges still owned by OID 10) fails the check and exits non-zero with a message that lists every problem found; the script does not repair it (Codex review, MINOR: the name-level test alone printed "already split" for such a cluster).

   **No password in the server log** (verifier note). Every session that sends a password (the preflight's emptiness test, the temporary superuser's `ALTER ROLE … PASSWORD` / `CREATE ROLE … PASSWORD`) first runs `SET log_statement = 'none'`, `SET log_min_duration_statement = -1` and `SET log_min_error_statement = 'panic'`. Without the last one, the server's default `log_min_error_statement = error` writes the failing statement, password included, to the postgres container log when such a statement fails. All three are superuser-settable, and every such session is a superuser (OID 10 under either name, or the temporary role); the split path's emptiness test runs after reconnecting as `postgres`. Residual: L12. The documented command connects as `obsidian_mcp` in both states; local-socket `trust` (L5) lets the script reconnect as `postgres` whichever state it finds. The rename can proceed only if `obsidian_mcp` is the OID-10 superuser and no role `postgres` exists; anything else aborts with a message describing the state found. `DROP ROLE IF EXISTS obsidian_mcp_split_tmp`, then `CREATE ROLE obsidian_mcp_split_tmp LOGIN SUPERUSER`.
2. `\c obsidian_mcp obsidian_mcp_split_tmp`, then **one transaction**:
   - `ALTER ROLE obsidian_mcp RENAME TO postgres` and `ALTER ROLE postgres PASSWORD :'admin_pw'` (a rename clears an MD5 password; set it explicitly regardless);
   - `CREATE ROLE obsidian_mcp LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD :'app_pw'`;
   - `ALTER DATABASE obsidian_mcp OWNER TO obsidian_mcp`;
   - in a `DO` block, ownership of every **user object** owned by OID 10 moves to `obsidian_mcp`. A user object is one with `oid >= 16384` (`FirstNormalObjectId`: everything `initdb` created is below it, so no list of system schema names is needed) that is **not in the extension closure**: the objects with a `pg_depend` `deptype = 'e'` edge to any extension, plus, recursively, everything with an internal (`deptype = 'i'`) edge to one of those (an extension type's array type, for example). The categories, each with its own `ALTER … OWNER`: schemas; relations that are tables (incl. `alembic_version`), partitioned tables, views, materialized views, foreign tables and sequences not owned by a column (indexes, TOAST tables, row types and column-owned sequences follow their table, verified by the self-check); functions, procedures and aggregates; types that are enums, domains, ranges and standalone composites (array and multirange types follow their base type); collations, conversions, operators, operator classes and families, text-search configurations and dictionaries, extended statistics, and large objects. `public` (OID 2200) is left as it is when owned by `pg_database_owner` (PG ≥ 15); otherwise its owner is changed to `obsidian_mcp`. Object ACLs need no separate step: `ALTER … OWNER` rewrites the old owner's entries in the ACL to the new owner;
   - **default privileges** (`pg_default_acl`, Codex spec review, MAJOR): every row with `defaclrole = 10` in this database is moved to `obsidian_mcp`. For a database-wide row the desired ACL is the stored one with OID 10 replaced by `obsidian_mcp`; the script issues `ALTER DEFAULT PRIVILEGES FOR ROLE obsidian_mcp GRANT/REVOKE …` for the difference between that and `acldefault(<type>, obsidian_mcp)`, and the mirror image `FOR ROLE postgres` against `acldefault(<type>, 10)`, which makes the server delete the old row. A per-schema row only ever adds grants, so it is replayed as `GRANT`s `IN SCHEMA` for `obsidian_mcp` and `REVOKE`d for `postgres`. The fresh install has no default ACL rows, so after the script neither does the converted one unless the operator had added some, which are then `obsidian_mcp`'s;
   - **self-check**: raise (rolling everything back) unless `obsidian_mcp` is not a superuser and owns the database, OID 10 is named `postgres`, no user object (definition above, across every catalog in the category list plus foreign-data wrappers, foreign servers, event triggers and publications, which the script does not move) is still owned by OID 10, and no `pg_default_acl` row names OID 10. An object kind the script does not move therefore aborts the conversion with its catalog named, rather than being left with the superuser.
3. **Disconnect the old superuser identity** (Codex review, MAJOR). A session that authenticated as the old `obsidian_mcp` before the commit is still OID 10 after it, and OID 10 is the superuser now named `postgres`: it keeps superuser rights for as long as it stays connected. Still as the temporary superuser, after `COMMIT`, the script calls `pg_terminate_backend(pid, 5000)` for every other client backend whose `usesysid` is 10 (no legitimate one can exist yet: the admin password was set by the transaction just committed).
4. `\c obsidian_mcp postgres`, `DROP ROLE obsidian_mcp_split_tmp`, then **verify** that no client backend with `usesysid = 10` other than its own remains; if one does, it fails with a message that the conversion is committed and the postgres container must be restarted before the app starts. The documented procedure restarts the container anyway (above).

If step 2 fails, its transaction rolls back and only the temp role remains, which a re-run's preflight drops. The script uses local-socket `trust`, the image's default `pg_hba` for local connections (L5).

### D8. Migrations need no superuser, and a test proves it

Required of the migration role: ownership of the database (implying `CREATE` on `public` via `pg_database_owner`), and `TEMP` on the database (PUBLIC default), with `vector` pre-installed. A new integration test creates a fresh database whose `vector` extension was installed by the superuser and whose owner is a `NOSUPERUSER` role, then runs `alembic upgrade head` and `alembic check` **as that role**, asserting success, a clean check, `rolsuper = false` for the session, and that every table in `public` is owned by that role. Any future migration that needs superuser fails CI.

### D9. Configuration tests parse the compose files as data

`tests/test_compose_db_roles.py` loads both bundles with `yaml.safe_load` (no Docker needed) and, for each:

- postgres `environment.POSTGRES_USER` exists and differs from the user in the app's `environment.DATABASE_URL` (`obsidian_mcp`);
- no `:-` default appears on any variable containing `PASSWORD`, and the literal `changeme` appears nowhere in the file;
- the app's `DATABASE_URL` and postgres's `OBSIDIAN_DB_PASSWORD` both use `${OBSIDIAN_DB_PASSWORD:?`;
- postgres `env_file` names `postgres.env`; the app's `env_file` does not, and the app's `environment` sets `POSTGRES_PASSWORD` to the empty string (D2);
- postgres `entrypoint` is the validation wrapper and the init mount points at `docker/db-init-compose.sh`; both files exist;
- the healthcheck uses `-h 127.0.0.1`.

The two bundles' postgres services are also asserted identical, so they cannot drift. Shell-level tests run the wrapper (`bash docker/postgres-entrypoint.sh` with a stub `docker-entrypoint.sh` on `PATH`) over the D3 matrix and assert the exit status and that no password value is printed. If `docker` is on the host, an optional integration test runs `docker compose -f <bundle> config` with and without `OBSIDIAN_DB_PASSWORD` and asserts that the guard message appears.

A second compose test (needs `docker`, skipped without it) renders each bundle with `docker compose config` from a scratch project directory whose `.env` carries a sentinel `POSTGRES_PASSWORD` and asserts the sentinel appears nowhere in the app service's rendered environment, while `postgres.env`'s value reaches the postgres service.

The upgrade script gets a real-Postgres test: start `pgvector/pgvector:pg16` with `POSTGRES_USER=obsidian_mcp` (today's shape), migrate to head as that user, insert a row, then **seed every category the script promises** as the old superuser: a free sequence and an identity/serial column's sequence, a function, a procedure, an enum, a domain, a standalone composite type, a view, a materialized view, an extra schema with a table in it, and `ALTER DEFAULT PRIVILEGES` rows (database-wide and in-schema). Run the script with the documented `psql` invocation, then assert through the catalogs: the database owner; every non-`public` user schema; every table including `alembic_version`; every sequence (free and column-owned); every routine and type seeded; no user object of any catalog still owned by OID 10; `vector` and every member of its dependency closure (types, array types, functions, operators, operator classes and families) still owned by `postgres`; no `pg_default_acl` row for OID 10 and equivalent rows for `obsidian_mcp`; the row readable and `alembic check` clean as the new `obsidian_mcp` with the new password; `postgres` authenticating with the admin password; and a re-run printing "already split" and exiting 0. A recovery case creates `obsidian_mcp_split_tmp` on a split cluster and asserts a re-run removes it. Missing-input cases (each of `app_pw` and `admin_pw`, unset and empty) assert nothing changed. A TCP session opened with the old password before the conversion is asserted disconnected afterwards, with neither role name accepting the old password. A cluster in the fresh-install shape passes the re-run check (also on a real compose-initialised volume), and five partly-split shapes (database owned by `postgres`; `obsidian_mcp` with `CREATEDB CREATEROLE`; a table and a function owned by OID 10; an OID-10 database-wide default ACL; two defects at once) are each refused with every problem named and nothing changed. A separate case injects a failing init script and asserts the wrapper's refusal on the next start.

The acceptance criterion "the old password is rejected for `obsidian_mcp` after conversion" was **dropped** (Codex spec review, MINOR, declined by the supervisor): an operator may legitimately reuse the old strong password as the new runtime one (D6, L7), so the test cannot assert rejection and the script cannot know the old password. DEPLOYMENT.md says the new runtime password should differ from the old one (L10).

## Risks / Trade-offs

- **[Breaking upgrade]** Every existing Compose install must act. → Fail-loudly messages point at one doc section; the script is one command; the opt-out exists.
- **[Rename of OID 10]** Renaming the bootstrap superuser is unusual. → It is supported (`ALTER ROLE … RENAME` from another superuser); the transaction plus self-check makes it all-or-nothing; tested against the real image.
- **[Third-party installs connecting as a superuser]** are now refused. → `DATABASE_ALLOW_SUPERUSER=true`, documented in README Upgrading.

## Accepted limitations

- **L1** Password strength is checked by length, charset and a placeholder list, not by entropy. A 24-character dictionary phrase passes.
- **L2** The superuser check is point-in-time at startup. A role promoted to superuser while the server runs is noticed at the next restart.
- **L3** The admin password sits in `postgres.env` on the host and in the postgres container's environment, readable by anyone with Docker access (equivalent to root on the host), the same exposure as the Kubernetes Secret.
- **L4** The runtime role owns its database and can drop its own tables. Migrations need that, and there is no separate migrator role (as in Kubernetes).
- **L5** The upgrade script relies on local-socket `trust` in the image's default `pg_hba.conf`. An operator who hardened `pg_hba` runs the documented steps with explicit credentials.
- **L6** The bundled `pg_hba` still lets `postgres` log in over `mcp_internal` with the admin password. Superuser is not restricted to the socket; the app container never holds that password.
- **L7** An existing install whose new `OBSIDIAN_DB_PASSWORD` equals the old superuser password runs `alembic upgrade head` once more as superuser before the server refuses. That is no more privilege than it already had.
- **L8** The runtime password is restricted to URL-unreserved characters because Compose splices it into `DATABASE_URL` unencoded.
- **L9** The `POSTGRES_PASSWORD`-in-app-environment check (D2) is a warning; the app cannot distinguish a leftover from a live admin secret.
- **L10** Nothing proves the new runtime password differs from the pre-#324 superuser password. An operator who reuses it keeps a credential that was the superuser's; DEPLOYMENT.md says to generate a new one (declined Codex finding, see D9).
- **L11** The init-completion marker (D4) covers a failure inside `docker/db-init-compose.sh`. A failure in the image's own initialisation before that script runs leaves a volume with no marker, which the wrapper cannot tell from a pre-#324 volume; the app then fails authentication with the upgrade hint, and D7's preflight refuses the unrecognised shape with a message.
- **L12** The upgrade script turns statement and error-statement logging off for its own sessions, so a failing password statement is not written to the server log. A logging extension the operator added (`pgaudit`, `auto_explain` with statement text) is outside those settings and may still record the `ALTER ROLE … PASSWORD` / `CREATE ROLE … PASSWORD` text; and while the script runs, the statement text is visible in `pg_stat_activity` to other superuser sessions (the app is stopped, so there are none in the documented procedure).
- **L13** Old-identity sessions are terminated *after* the commit, so a pre-commit superuser session has the few milliseconds between `COMMIT` and its termination; the app is stopped first, so in the documented procedure there is no such session. A re-run on a split cluster ("already split") does not terminate sessions; an interrupted run's leftover sessions are cleared by the documented `restart postgres`, which the verify step's failure message also names.

## Owner decisions (2026-10-09)

- The startup superuser refusal (D5) applies to **every** deployment, not only the Compose bundles. The opt-out is `DATABASE_ALLOW_SUPERUSER=true`. Production (k3s, CloudNativePG, role `obsidian_mcp` with `rolsuper = false`) passes the check unchanged; the non-superuser migration test also runs the check's query as a non-superuser database owner.
- The admin password lives in a separate, gitignored `postgres.env`, loaded only by the postgres service (D2).
- Existing Compose installs get the documented one-time script (D7). Nothing converts a cluster automatically.
- Codex spec review: findings 1–5 accepted and folded into D2, D3, D4, D6, D7 and D9; finding 6 (old password rejected) declined, recorded as L10.
- Adversarial Codex review of the implementation: MAJOR (old-identity sessions keep superuser after the commit) accepted, D7 steps 3–4 and the restart in the procedure; MINOR (name-level "already split") accepted, D7 step 1 runs the full self-check. Verifier notes folded: logging off around passwords (L12), the rollback limitation (D6), the `.env.example` `DATABASE_URL` line commented out.

## Migration Plan

Fresh installs: copy `.env.example` and `postgres.env.example`, or run `make init`; `up -d`. Existing Compose installs: stop the app, back up, pull, create `postgres.env`, set `OBSIDIAN_DB_PASSWORD` and remove `POSTGRES_PASSWORD`/`DATABASE_URL` from `.env`, `up -d postgres`, run the script, `restart postgres`, `up -d`. Rollback: check out the previous release (its compose files build its image), restore its `.env` without `POSTGRES_PASSWORD`, and set `DATABASE_URL` to the new runtime credentials. The split cluster works with the old files, minus the superuser privilege. Before converting, the previous release with its unchanged `.env` is the rollback; the new files with `DATABASE_ALLOW_SUPERUSER=true` work only where the old password passes D3 (D6). k3s: nothing to do.
