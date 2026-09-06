# Obsidian MCP Server
# Manages build, deploy, and database operations

# Optional host-specific overrides (gitignored). Set DEPLOY_DIR / DATA_DIR /
# REGISTRY here to deploy from a directory outside the repo.
-include Makefile.local

IMAGE_NAME := obsidian-mcp
IMAGE_TAG := latest
REGISTRY ?= localhost:5000
FULL_IMAGE := $(REGISTRY)/$(IMAGE_NAME):$(IMAGE_TAG)
DEPLOY_DIR ?= .
DATA_DIR ?= ./data
# Container running PostgreSQL, for db-backup / db-restore. The bundled
# compose stacks (docker-compose.simple.yml / .proxy.yml) name it
# `obsidian-mcp-postgres`; a shared host instance is usually just `postgres`.
# Override in Makefile.local to match the deployment.
DB_CONTAINER ?= postgres
# The application container, as named in docker-compose.yml.
CONTAINER ?= obsidian-mcp
COMPOSE_FILE := $(DEPLOY_DIR)/docker-compose.yml
ENV_FILE := $(DEPLOY_DIR)/.env
COMPOSE := docker compose --project-directory $(DEPLOY_DIR) -f $(COMPOSE_FILE)

GREEN := \033[0;32m
YELLOW := \033[0;33m
RED := \033[0;31m
NC := \033[0m

# The repo's virtualenv is where the dev tooling (pytest, pip-audit) actually
# lives, so `make audit` has to work from a shell that has not activated it —
# a gate nobody can run without remembering a preamble is a gate that stops
# being run. Overridable, and it falls back to `python3` when there is no venv.
PYTHON ?= $(if $(wildcard .venv/bin/python),.venv/bin/python,python3)
SCHEMA_TEST_CONTAINER ?= obsidian-mcp-schema-test
SCHEMA_TEST_PORT ?= 55438
SCHEMA_TEST_IMAGE ?= pgvector/pgvector:pg16

.PHONY: help init build build-cached push image deploy up down restart logs shell db-init db-migrate db-check db-backup db-restore status check-no-backups-mount clean reindex reset-embeddings rebuild-tsvectors audit trivy test-schema

help:
	@echo "$(GREEN)Obsidian MCP Server$(NC)"
	@echo ""
	@echo "$(YELLOW)Setup:$(NC)"
	@echo "  make init         - Initial setup (directories, .env, database)"
	@echo ""
	@echo "$(YELLOW)Build & Deploy:$(NC)"
	@echo "  make build        - Build Docker image (no cache)"
	@echo "  make build-cached - Build Docker image (with cache)"
	@echo "  make push         - Push image to local registry"
	@echo "  make image        - Build and push"
	@echo "  make deploy       - Full deploy (build, push, backup, recreate)"
	@echo ""
	@echo "$(YELLOW)Container Management:$(NC)"
	@echo "  make up           - Start container"
	@echo "  make down         - Stop container"
	@echo "  make restart      - Restart container"
	@echo "  make logs         - Tail container logs"
	@echo "  make shell        - Shell into container"
	@echo ""
	@echo "$(YELLOW)Database:$(NC)"
	@echo "  make db-init      - Create database, user, and extensions"
	@echo "  make db-migrate   - Run Alembic migrations"
	@echo "  make db-check     - Verify the schema matches the ORM models"
	@echo "  make test-schema  - Schema gate: migrations vs. models on a throwaway pgvector"
	@echo "  make db-backup    - Backup database"
	@echo "  make db-restore FILE=<path> - Restore from backup"
	@echo ""
	@echo "$(YELLOW)Operations:$(NC)"
	@echo "  make reindex      - Explain how to trigger a reindex (panel only)"
	@echo "  make reset-embeddings - Drop & recreate embedding column at configured dim"
	@echo "  make rebuild-tsvectors - Recompute keyword index for FTS_CONFIGS (no embeddings, no API calls)"
	@echo "  make status       - Show container and health status"
	@echo "  make clean        - Remove containers and images"
	@echo ""
	@echo "$(YELLOW)Security:$(NC)"
	@echo "  make audit        - Audit Python deps (pip-audit)"
	@echo "  make trivy        - Scan local image for HIGH/CRITICAL CVEs (SCAN_IMAGE=... to override)"

init:
	@echo "$(GREEN)Setting up Obsidian MCP...$(NC)"
	@sudo mkdir -p $(DATA_DIR)/backups
	@sudo chown -R $(shell id -u):$(shell id -g) $(DATA_DIR)
	# Owner-only: `.env` carries the DB password and SECRET_KEY, and the dumps
	# under backups/ carry every tenant's note text. Nothing but the deploying
	# user (and root) needs to read either, so no group/world bits (#187).
	@sudo chmod 750 $(DATA_DIR)
	@sudo chmod 700 $(DATA_DIR)/backups
	@if [ ! -f "$(ENV_FILE)" ]; then \
		echo "$(GREEN)Creating $(ENV_FILE) from template...$(NC)"; \
		cp .env.example $(ENV_FILE); \
		DB_PASS=$$(openssl rand -hex 16); \
		SECRET=$$(openssl rand -hex 32); \
		sed -i "s/CHANGE_ME/$$DB_PASS/" $(ENV_FILE); \
		sed -i "s/SECRET_KEY=.*/SECRET_KEY=$$SECRET/" $(ENV_FILE); \
		chmod 600 $(ENV_FILE); \
		echo "$(GREEN)$(ENV_FILE) created with random secrets$(NC)"; \
	else \
		echo "$(YELLOW)$(ENV_FILE) already exists$(NC)"; \
	fi
	@echo "$(GREEN)Setup complete. Next: make db-init && make deploy$(NC)"

build:
	@echo "$(GREEN)Building image (no cache)...$(NC)"
	docker build --no-cache --pull -f Dockerfile -t $(IMAGE_NAME):$(IMAGE_TAG) .
	@echo "$(GREEN)Built: $(IMAGE_NAME):$(IMAGE_TAG)$(NC)"

build-cached:
	@echo "$(GREEN)Building image (cached)...$(NC)"
	docker build -f Dockerfile -t $(IMAGE_NAME):$(IMAGE_TAG) .
	@echo "$(GREEN)Built: $(IMAGE_NAME):$(IMAGE_TAG)$(NC)"

push:
	@echo "$(GREEN)Pushing to registry...$(NC)"
	docker tag $(IMAGE_NAME):$(IMAGE_TAG) $(FULL_IMAGE)
	docker push $(FULL_IMAGE)
	@echo "$(GREEN)Pushed: $(FULL_IMAGE)$(NC)"

# Defaults to what `make build` produces (obsidian-mcp:latest). The bundled
# docker-compose.simple.yml / docker-compose.proxy.yml stacks build their own
# tag instead, so scan those with:
#     make trivy SCAN_IMAGE=obsidian-mcp:local
SCAN_IMAGE ?= $(IMAGE_NAME):$(IMAGE_TAG)

trivy:
	@echo "$(GREEN)Scanning $(SCAN_IMAGE) for HIGH/CRITICAL CVEs...$(NC)"
	@trivy image --severity HIGH,CRITICAL --exit-code 1 --ignore-unfixed --no-progress --scanners vuln $(SCAN_IMAGE)
	@echo "$(GREEN)No fixable HIGH/CRITICAL CVEs$(NC)"

image: build trivy push

deploy: image
	@echo "$(GREEN)Deploying Obsidian MCP...$(NC)"
	# A deploy runs `alembic upgrade head` against the live database, so the
	# backup is the only way back from a bad migration. If it cannot be taken,
	# stop here rather than migrate unprotected.
	@$(MAKE) db-backup || { \
		echo "$(RED)Deploy ABORTED: database backup failed — refusing to migrate without one.$(NC)"; \
		exit 1; \
	}
	# Migrate with the newly built image before replacing the live container.
	# Migrations are backward-compatible, avoiding a window where new code runs
	# against the old schema (and matching README's documented deploy behavior).
	$(COMPOSE) run --rm obsidian-mcp alembic upgrade head
	$(COMPOSE) up -d --force-recreate
	@$(MAKE) check-no-backups-mount
	@docker image prune -f
	@docker builder prune -f --filter until=168h
	@HOST=$$(grep -E '^MCP_HOSTNAME=' $(ENV_FILE) 2>/dev/null | cut -d= -f2); \
	echo "$(GREEN)Deployed! https://$${HOST:-localhost}$(NC)"

up:
	$(COMPOSE) up -d

down:
	$(COMPOSE) down

restart:
	$(COMPOSE) restart obsidian-mcp

logs:
	$(COMPOSE) logs -f --tail=100 obsidian-mcp

shell:
	$(COMPOSE) exec obsidian-mcp bash

db-init:
	@echo "$(GREEN)Initializing database...$(NC)"
	@bash docker/db-init.sh
	@echo "$(GREEN)Database ready$(NC)"

db-migrate:
	@echo "$(GREEN)Running migrations...$(NC)"
	$(COMPOSE) exec obsidian-mcp alembic upgrade head
	@echo "$(GREEN)Migrations complete$(NC)"

db-check:
	@echo "$(GREEN)Checking schema against the models...$(NC)"
	@$(COMPOSE) exec obsidian-mcp alembic check

# The pre-deploy gate for any change that carries a migration. `db-check` only
# runs `alembic check`, which cannot see a CHECK predicate; this stands up a
# disposable Postgres, migrates throwaway databases through fresh / drifted /
# impostor-constraint / violating-row / downgrade paths and asserts the catalog
# directly. Nothing it touches outlives the target: the container is removed
# even when the tests fail, and it never reads the deploy .env or the live DB.
#
# It also runs the two session cases that need a real engine and a real
# database, because this is the only job in CI that has one. A fake cannot show
# that twenty-five concurrent validations complete against fifteen leases, and
# it cannot show what a `Session.rollback()` does to an already-loaded ORM
# object.
#
# Start, run and teardown are **one shell** with a `trap` armed the instant the
# container exists, so the removal also happens on the paths a trailing `docker
# rm` line cannot cover: a Ctrl-C at the prompt, a SIGTERM, or a `make` that
# dies between recipe lines. Split across lines, an interrupt during the ~60s
# readiness wait leaves a Postgres listening with a known password until someone
# notices. The port is published on **127.0.0.1 only** for the same reason: this
# database is throwaway and its credentials are literally `test`, so it must not
# be reachable from the LAN even for the seconds it lives.
test-schema:
	@echo "$(GREEN)Schema gate: throwaway $(SCHEMA_TEST_IMAGE) on :$(SCHEMA_TEST_PORT)$(NC)"
	@docker rm -f $(SCHEMA_TEST_CONTAINER) >/dev/null 2>&1 || true; \
	docker run --rm -d --name $(SCHEMA_TEST_CONTAINER) \
		-e POSTGRES_PASSWORD=test -p 127.0.0.1:$(SCHEMA_TEST_PORT):5432 \
		$(SCHEMA_TEST_IMAGE) >/dev/null || exit 1; \
	trap 'docker rm -f $(SCHEMA_TEST_CONTAINER) >/dev/null 2>&1' EXIT INT TERM; \
	ready=0; \
	for i in $$(seq 1 60); do \
		if docker exec $(SCHEMA_TEST_CONTAINER) pg_isready -U postgres -q 2>/dev/null; then ready=1; break; fi; \
		sleep 1; \
	done; \
	if [ "$$ready" -eq 1 ]; then \
		OMCP_REQUIRE_SCHEMA_INTEGRATION=1 \
		PGVECTOR_TEST_ADMIN_URL=postgresql+asyncpg://postgres:test@127.0.0.1:$(SCHEMA_TEST_PORT)/postgres \
		$(PYTHON) -m pytest -q tests/integration/test_schema_check.py \
			tests/integration/test_issue_198_session_pool_capacity.py \
			tests/integration/test_issue_198_touch_failure_isolation.py; \
		status=$$?; \
	else \
		echo "$(RED)$(SCHEMA_TEST_CONTAINER) never became ready$(NC)"; status=1; \
	fi; \
	if [ $$status -eq 0 ]; then echo "$(GREEN)Schema gate passed$(NC)"; \
	else echo "$(RED)Schema gate FAILED — do not deploy$(NC)"; fi; \
	exit $$status

# The pre-migration safety net, so it must fail loudly. The previous recipe
# `|| true`d both pg_dump and gzip and printed the green line unconditionally,
# so a wrong container name, a dead database or a full disk produced a
# zero-byte file, a success message, and a `deploy` that went straight on to
# `alembic upgrade head` with nothing to roll back to. Three failure modes are
# now distinguished and all of them abort: a non-zero pg_dump, an empty dump
# with exit 0 (pg_dump can write nothing and still succeed when it is pointed
# at the wrong thing), and a failed gzip. The partial file is removed so a
# later `db-restore` cannot pick a truncated dump out of the backups directory.
#
# The dump is then **recorded** in `backups_log` (migration 021, #163) so the
# panel's health page can report backup age without the container being able to
# see the backups directory — it deliberately cannot, and mounting a host path
# into a public repo's compose file is the alternative that was rejected. The
# recording goes through `docker/record-backup.sh`, which uses the same
# `docker exec … psql` channel `pg_dump` just used and carries the three-branch
# guard the deploy ordering forces: `make deploy` backs up BEFORE it migrates,
# so on the deploy that ships 021 the table does not exist yet and the target
# must warn and still succeed. Its exit status is the target's: once the table
# exists, a backup that fails to record itself fails the backup.
# Dumps are created under `umask 077` so they are never readable by another
# local account (they hold every tenant's note text and every credential
# hash), and the directory is kept 0700. After a dump is verified (`gzip -t`)
# and recorded, dumps older than BACKUP_RETAIN_DAYS are pruned — but never
# below BACKUP_RETAIN_MIN most-recent files, and never the one just taken, so
# a long gap between deploys cannot leave the directory empty (#181).
BACKUP_RETAIN_DAYS ?= 30
BACKUP_RETAIN_MIN ?= 7

db-backup:
	@mkdir -p $(DATA_DIR)/backups
	@chmod 700 $(DATA_DIR)/backups
	@umask 077; TIMESTAMP=$$(date +%Y%m%d_%H%M%S); \
	BACKUP_FILE="$(DATA_DIR)/backups/backup_$$TIMESTAMP.sql"; \
	: "the database named here is mirrored as DB_NAME's default in docker/record-backup.sh; keep the two in step"; \
	if ! docker exec $(DB_CONTAINER) pg_dump -U postgres obsidian_mcp > $$BACKUP_FILE; then \
		rm -f $$BACKUP_FILE; \
		echo "$(RED)Backup FAILED: pg_dump against container '$(DB_CONTAINER)' returned non-zero$(NC)"; \
		echo "$(YELLOW)Set DB_CONTAINER in Makefile.local if the database container is named differently.$(NC)"; \
		exit 1; \
	fi; \
	if [ ! -s $$BACKUP_FILE ]; then \
		rm -f $$BACKUP_FILE; \
		echo "$(RED)Backup FAILED: pg_dump produced an empty dump$(NC)"; \
		exit 1; \
	fi; \
	if ! gzip $$BACKUP_FILE; then \
		rm -f $$BACKUP_FILE $$BACKUP_FILE.gz; \
		echo "$(RED)Backup FAILED: could not gzip $$BACKUP_FILE$(NC)"; \
		exit 1; \
	fi; \
	if ! gzip -t $$BACKUP_FILE.gz; then \
		rm -f $$BACKUP_FILE.gz; \
		echo "$(RED)Backup FAILED: $$BACKUP_FILE.gz did not verify$(NC)"; \
		exit 1; \
	fi; \
	chmod 600 $$BACKUP_FILE.gz; \
	echo "$(GREEN)Backup: $$BACKUP_FILE.gz$(NC)"; \
	: "prune BEFORE recording: the new dump is already verified above, and the recording must stay the recipe's last command so its exit status is the target's"; \
	PRUNED=0; \
	for OLD in $$(ls -1t $(DATA_DIR)/backups/backup_*.sql.gz 2>/dev/null | tail -n +$$(( $(BACKUP_RETAIN_MIN) + 1 ))); do \
		if [ "$$OLD" != "$$BACKUP_FILE.gz" ] && [ -n "$$(find "$$OLD" -mtime +$(BACKUP_RETAIN_DAYS) 2>/dev/null)" ]; then \
			rm -f "$$OLD"; PRUNED=$$((PRUNED + 1)); \
		fi; \
	done; \
	if [ $$PRUNED -gt 0 ]; then echo "$(YELLOW)Pruned $$PRUNED backup(s) older than $(BACKUP_RETAIN_DAYS) days (kept at least $(BACKUP_RETAIN_MIN))$(NC)"; fi; \
	DB_CONTAINER=$(DB_CONTAINER) bash docker/record-backup.sh $$BACKUP_FILE.gz

db-restore:
	@if [ -z "$(FILE)" ]; then echo "$(RED)Usage: make db-restore FILE=<path>$(NC)"; exit 1; fi
	@echo "$(YELLOW)WARNING: This will replace the obsidian_mcp database!$(NC)"
	@echo "Press Ctrl+C to cancel, waiting 5s..."
	@sleep 5
	@if echo "$(FILE)" | grep -q ".gz$$"; then \
		gunzip -c $(FILE) | docker exec -i $(DB_CONTAINER) psql -U postgres obsidian_mcp; \
	else \
		docker exec -i $(DB_CONTAINER) psql -U postgres obsidian_mcp < $(FILE); \
	fi
	@echo "$(GREEN)Restored from $(FILE)$(NC)"

# There is no headless trigger. The only on-demand reindex is
# `POST /admin/settings/reindex` (src/control_panel/routes.py), which sits on
# the panel router behind `require_admin_panel` *and* `verify_csrf`: it needs a
# signed panel session cookie plus a CSRF token that is only minted while
# rendering a panel page. curl cannot produce either, and the container
# publishes no host port anyway — the old `/api/reindex` recipe was a 404
# against a socket that was not listening.
reindex:
	@echo "$(YELLOW)No headless reindex trigger exists.$(NC)"
	@echo ""
	@echo "  The indexer already runs on startup and every"
	@echo "  INDEX_INTERVAL_SECONDS (default 300s), so a reindex normally"
	@echo "  needs no action at all."
	@echo ""
	@echo "  To force one now, use the control panel: \"Reindex Now\" on the"
	@echo "  dashboard, or Settings -> Indexer -> Reindex. Both POST"
	@echo "  /admin/settings/reindex, which requires an admin panel session"
	@echo "  and a CSRF token - neither of which curl can obtain."
	@echo ""
	@echo "  To restart the indexer instead: make restart"

# `run --rm`, not `exec` (#142): `exec` runs inside the LIVE container, whose
# environment was baked at creation — a changed .env (EMBEDDING_DIMENSIONS,
# FTS_CONFIGS) is invisible to it, so the reset recreated the column at the
# OLD dim and the rebuild indexed under the OLD configs. `run --rm` starts a
# fresh container that re-reads .env, and also works while the service is
# down — which matters here, because a dim-mismatched container exits at
# startup and a dead container cannot be exec'd into.
reset-embeddings:
	@echo "$(YELLOW)Resetting embeddings — column will be recreated at EMBEDDING_DIMENSIONS$(NC)"
	@echo "Press Ctrl+C to cancel, waiting 5s..."
	@sleep 5
	$(COMPOSE) run --rm obsidian-mcp python -m scripts.reset_embeddings
	@echo "$(GREEN)Done. The next indexer pass will re-embed all notes.$(NC)"

rebuild-tsvectors:
	@echo "$(YELLOW)Rebuilding keyword (FTS) index for the configured FTS_CONFIGS...$(NC)"
	$(COMPOSE) run --rm obsidian-mcp python -m scripts.rebuild_tsvectors
	@echo "$(GREEN)Done. Keyword search now reflects FTS_CONFIGS (embeddings untouched, no API calls).$(NC)"

# Invariant: the application container must not be able to see the backups
# directory (docs/architecture/control-panel.md, "Backup recency"). The mount
# once crept back into the compose file (#186); this refuses a deploy that
# reintroduces it under any host path.
check-no-backups-mount:
	@if docker inspect $(CONTAINER) --format '{{range .Mounts}}{{.Destination}}{{"\n"}}{{end}}' 2>/dev/null | grep -qx '/app/backups'; then \
		echo "$(RED)INVARIANT VIOLATED: $(CONTAINER) has a mount at /app/backups — the container must not see the backups directory (#186).$(NC)"; \
		exit 1; \
	fi

status:
	@echo "$(GREEN)Obsidian MCP Status:$(NC)"
	@$(COMPOSE) ps
	@$(MAKE) check-no-backups-mount
	@echo ""
	@echo "$(GREEN)Health:$(NC)"
	@HOST=$$(grep -E '^MCP_HOSTNAME=' $(ENV_FILE) 2>/dev/null | cut -d= -f2); \
	URL=$${HOST:+https://$$HOST/health}; \
	URL=$${URL:-http://localhost:8000/health}; \
	curl -s $$URL | python3 -m json.tool 2>/dev/null || echo "$(RED)Not responding$(NC)"

clean: down
	docker rmi $(IMAGE_NAME):$(IMAGE_TAG) $(FULL_IMAGE) 2>/dev/null || true
	@echo "$(GREEN)Cleaned. Data in $(DATA_DIR) preserved.$(NC)"

audit:
	$(PYTHON) -m pip_audit -r requirements.txt
