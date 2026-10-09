Slices A, B and C touch disjoint files and can run in parallel worktrees; D depends on A and C; E is last.

## 1. Slice A: Compose bundles and postgres-side scripts (D1–D4)

- [x] 1.1 `docker/postgres-entrypoint.sh`: D3 validation (five rules plus the init-marker check, per-variable messages, never echo values), then `exec docker-entrypoint.sh "$@"`
- [x] 1.2 `docker/db-init-compose.sh` (replaces `docker/db-init-simple.sql`; delete the old file): role with D1 attributes via psql `:'pw'`, `CREATE DATABASE obsidian_mcp OWNER obsidian_mcp`, `CREATE EXTENSION IF NOT EXISTS vector` as superuser; `started` / `complete` marker in `$PGDATA/obsidian-mcp-init.state` (D4)
- [x] 1.3 `docker-compose.simple.yml` and `docker-compose.proxy.yml`: postgres `POSTGRES_USER: postgres`, no `POSTGRES_DB`, `env_file: postgres.env`, `OBSIDIAN_DB_PASSWORD: ${OBSIDIAN_DB_PASSWORD:?…}`, `entrypoint` wrapper + `command: ["postgres"]`, init mount at `/docker-entrypoint-initdb.d/10-obsidian-mcp.sh`, healthcheck `pg_isready -h 127.0.0.1 -U obsidian_mcp -d obsidian_mcp`; app `environment: DATABASE_URL` built from `${OBSIDIAN_DB_PASSWORD:?…}` and `POSTGRES_PASSWORD: ""` (D2, overrides `.env`); header comments updated; identical postgres blocks
- [x] 1.4 `postgres.env.example` (tracked), `postgres.env` in `.gitignore`
- [x] 1.5 `tests/test_compose_db_roles.py`: D9 YAML assertions for both bundles plus wrapper shell matrix (stub entrypoint on `PATH`); optional `docker compose config` cases skipped without `docker`, including the sentinel-admin-password render (D2). The half-initialised-volume refusal with an injected init failure (D4) is a real-container case in `tests/integration/test_compose_postgres_init_pg.py`, not in this offline file

## 2. Slice B: Server-side guards (D5, D6 hint)

- [x] 2.1 `src/config.py`: passwordless default `database_url`; placeholder-password validator (after the existing transport validator, message without URL); `database_allow_superuser: bool = False`
- [x] 2.2 `src/main.py` lifespan: `rolsuper` assertion after the transport assertion, CRITICAL + exit / WARNING per spec; one WARNING when `POSTGRES_PASSWORD` is in `os.environ`
- [x] 2.3 `alembic/env.py`: catch the (SQLAlchemy-wrapped) connect exception, walk `orig` / `__cause__` / `__context__` for SQLSTATE `28P01` / `28000`, print the one-line pointer, re-raise; verified against a real wrong-password connection (integration test)
- [x] 2.4 Unit tests: config placeholder matrix, default URL, setting parse; lifespan superuser refuse / opt-out / silent / sandbox (stub engine, see `tests/_lifespan_stubs.py`); env.py hint; existing config/hermetic-env tests updated for the new default
- [x] 2.5 Integration harnesses that run the lifespan as `postgres` set `DATABASE_ALLOW_SUPERUSER=true` (grep `tests/integration/` and CI env); `make test-schema` unaffected (alembic only). Result: none needed. No integration module enters the real lifespan (they drive `src.main.app` through `httpx.ASGITransport`, which does not run it), offline lifespan tests stub the check through `tests/_lifespan_stubs.py`, and CI's env is unchanged

## 3. Slice C: Upgrade script (D7)

- [x] 3.1 `docker/upgrade-split-db-roles.sql`: preflight (`\if` on inputs and cluster shape, "already split" exit 0, temp role), `\c` as temp, already-split path drops a left-behind temp role; single transaction (rename, admin password, new role, database owner, ownership `DO` block over every user-object catalog skipping the extension closure, `pg_default_acl` move, self-check), `\c` as `postgres`, drop temp role
- [x] 3.2 `tests/integration/test_upgrade_split_db_roles_pg.py`: throwaway `pgvector/pgvector:pg16` with `POSTGRES_USER=obsidian_mcp`, migrate to head, insert a row, run the documented command, seed every promised category (D9), assert the spec scenarios through the catalogs (shape, every category, extension closure, default ACLs, passwords, row, `alembic check`, no temp role, re-run no-op, left-behind temp role removed, missing-input abort). Uses its own container name and port, not `SCHEMA_TEST_*`

## 4. Slice D: Migrations without superuser (D8), after A and C merge

- [x] 4.1 `tests/integration/test_nonsuperuser_migrations_pg.py`: NOSUPERUSER owner role, `vector` pre-installed by `postgres`, `alembic upgrade head` + `alembic check` as that role, ownership assertions, and the startup superuser check passes for that session; runs in CI's `tests` job
- [x] 4.2 End-to-end on a scratch host: `docker compose -f docker-compose.simple.yml up` on a fresh volume (role attributes, app healthy), and the existing-volume path (refusal message, script, healthy); record the commands and outcomes in the PR. Done 2026-10-09 with `docker-compose.proxy.yml` (container names, image tag and host port rewritten so nothing collides with the host's live containers; Caddy's 80/443 bindings rule out the simple bundle on that host, and the two postgres services are asserted identical): fresh volume healthy, `obsidian_mcp` all attributes false, app session `obsidian_mcp`, `POSTGRES_PASSWORD` empty in the app container despite a sentinel in `.env`; pre-#324 volume with a new password logs the alembic hint, with the old password logs the superuser refusal, the documented script converts it (row kept, `alembic check` clean in the app, health 200), re-run reports already split

## 5. Docs and setup

- [x] 5.1 `.env.example`: `OBSIDIAN_DB_PASSWORD=` with generation hint; `DATABASE_URL` comment saying the bundles set it themselves; `DATABASE_ALLOW_SUPERUSER` documented (commented out)
- [x] 5.2 `Makefile` `init`: generate `OBSIDIAN_DB_PASSWORD` (hex, ≥ 24 chars) into `.env` and create `postgres.env` (mode 600) with its own generated `POSTGRES_PASSWORD`, without overwriting existing files
- [x] 5.3 `DEPLOYMENT.md`: Step 2/3 for both bundles (two files, two passwords, which container sees which); new "Upgrading: split database roles" section (backup, steps from design Migration Plan, the exact script command, the two failure messages, rollback, `DATABASE_ALLOW_SUPERUSER`); fix the `psql -U obsidian_mcp` example; note that k3s is unaffected
- [x] 5.4 `README.md`: Quick start mentions `postgres.env`; Upgrading gains a **Breaking** bullet linking the DEPLOYMENT.md section and `DATABASE_ALLOW_SUPERUSER`
- [x] 5.5 `docs/architecture/schema-and-migrations.md`: role model (bootstrap vs runtime, migrations need ownership + TEMP only, the non-superuser migration test as the gate, the startup refusal); `CLAUDE.md` key-decisions bullet

## 6. Gates

- [ ] 6.1 Offline suite, `make test-integration`, `make test-schema`, `make audit` green
- [ ] 6.2 `openspec-verifier`; adversarial Codex (credential and privilege boundary; existing-install data path → mandatory). Triage per the workflow budget; record declined findings under Accepted limitations
- [ ] 6.3 `openspec validate compose-db-roles --strict`; `/openspec-archive-change compose-db-roles -y` as the last commit of the feature branch; PR `Closes #324`
- [ ] 6.4 Post-merge: no production deploy action (k3s unaffected); confirm the image build only

## 7. Review fixes (adversarial Codex + openspec-verifier)

- [x] 7.1 Codex MAJOR: after the commit the script terminates every other OID-10 client backend and fails unless none remains; DEPLOYMENT.md stops the app first and restarts postgres after the conversion (D7 steps 3–4). Integration: a held old-password TCP session is disconnected and cannot reconnect
- [x] 7.2 Codex MINOR: the already-split path runs the full self-check and refuses a partly split cluster naming each problem (D7 step 1). Integration: five malformed-split shapes, plus the fresh compose volume passing
- [x] 7.3 Verifier: k8s doc default `DATABASE_URL` and troubleshooting rows; `.env.example` `DATABASE_URL` commented out with `make init` uncommenting it (offline test runs the Makefile's seds); rollback limitation in DEPLOYMENT.md and D6; `make init`'s `postgres.env` noted; logging off around passwords (L12); missing-`admin_pw` test; this file's 1.5 wording
