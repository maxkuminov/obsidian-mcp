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
COMPOSE_FILE := $(DEPLOY_DIR)/docker-compose.yml
ENV_FILE := $(DEPLOY_DIR)/.env
COMPOSE := docker compose --project-directory $(DEPLOY_DIR) -f $(COMPOSE_FILE)

GREEN := \033[0;32m
YELLOW := \033[0;33m
RED := \033[0;31m
NC := \033[0m

PYTHON ?= python3
SCHEMA_TEST_CONTAINER ?= obsidian-mcp-schema-test
SCHEMA_TEST_PORT ?= 55438
SCHEMA_TEST_IMAGE ?= pgvector/pgvector:pg16

.PHONY: help init build build-cached push image deploy up down restart logs shell db-init db-migrate db-check db-backup db-restore status clean reindex reset-embeddings rebuild-tsvectors audit trivy test-schema

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
	@echo "  make reindex      - Trigger full vault reindex"
	@echo "  make reset-embeddings - Drop & recreate embedding column at configured dim"
	@echo "  make rebuild-tsvectors - Recompute keyword index for FTS_CONFIGS (no embeddings, no API calls)"
	@echo "  make status       - Show container and health status"
	@echo "  make clean        - Remove containers and images"
	@echo ""
	@echo "$(YELLOW)Security:$(NC)"
	@echo "  make audit        - Audit Python deps (pip-audit)"
	@echo "  make trivy        - Scan local image for HIGH/CRITICAL CVEs"

init:
	@echo "$(GREEN)Setting up Obsidian MCP...$(NC)"
	@sudo mkdir -p $(DATA_DIR)/backups
	@sudo chown -R $(shell id -u):$(shell id -g) $(DATA_DIR)
	@sudo chmod -R 775 $(DATA_DIR)
	@if [ ! -f "$(ENV_FILE)" ]; then \
		echo "$(GREEN)Creating $(ENV_FILE) from template...$(NC)"; \
		cp .env.example $(ENV_FILE); \
		DB_PASS=$$(openssl rand -hex 16); \
		SECRET=$$(openssl rand -hex 32); \
		sed -i "s/CHANGE_ME/$$DB_PASS/" $(ENV_FILE); \
		sed -i "s/SECRET_KEY=.*/SECRET_KEY=$$SECRET/" $(ENV_FILE); \
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

trivy:
	@echo "$(GREEN)Scanning $(IMAGE_NAME):$(IMAGE_TAG) for HIGH/CRITICAL CVEs...$(NC)"
	@trivy image --severity HIGH,CRITICAL --exit-code 1 --ignore-unfixed --no-progress --scanners vuln $(IMAGE_NAME):$(IMAGE_TAG)
	@echo "$(GREEN)No fixable HIGH/CRITICAL CVEs$(NC)"

image: build trivy push

deploy: image
	@echo "$(GREEN)Deploying Obsidian MCP...$(NC)"
	@$(MAKE) db-backup 2>/dev/null || true
	# Migrate with the newly built image before replacing the live container.
	# Migrations are backward-compatible, avoiding a window where new code runs
	# against the old schema (and matching README's documented deploy behavior).
	$(COMPOSE) run --rm obsidian-mcp alembic upgrade head
	$(COMPOSE) up -d --force-recreate
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
test-schema:
	@echo "$(GREEN)Schema gate: throwaway $(SCHEMA_TEST_IMAGE) on :$(SCHEMA_TEST_PORT)$(NC)"
	@docker rm -f $(SCHEMA_TEST_CONTAINER) >/dev/null 2>&1 || true
	@docker run --rm -d --name $(SCHEMA_TEST_CONTAINER) \
		-e POSTGRES_PASSWORD=test -p $(SCHEMA_TEST_PORT):5432 \
		$(SCHEMA_TEST_IMAGE) >/dev/null
	@ready=0; \
	for i in $$(seq 1 60); do \
		if docker exec $(SCHEMA_TEST_CONTAINER) pg_isready -U postgres -q 2>/dev/null; then ready=1; break; fi; \
		sleep 1; \
	done; \
	if [ "$$ready" -eq 1 ]; then \
		OMCP_REQUIRE_SCHEMA_INTEGRATION=1 \
		PGVECTOR_TEST_ADMIN_URL=postgresql+asyncpg://postgres:test@localhost:$(SCHEMA_TEST_PORT)/postgres \
		$(PYTHON) -m pytest -q tests/integration/test_schema_check.py; \
		status=$$?; \
	else \
		echo "$(RED)$(SCHEMA_TEST_CONTAINER) never became ready$(NC)"; status=1; \
	fi; \
	docker rm -f $(SCHEMA_TEST_CONTAINER) >/dev/null 2>&1 || true; \
	if [ $$status -eq 0 ]; then echo "$(GREEN)Schema gate passed$(NC)"; \
	else echo "$(RED)Schema gate FAILED — do not deploy$(NC)"; fi; \
	exit $$status

db-backup:
	@mkdir -p $(DATA_DIR)/backups 2>/dev/null || true
	@TIMESTAMP=$$(date +%Y%m%d_%H%M%S); \
	BACKUP_FILE="$(DATA_DIR)/backups/backup_$$TIMESTAMP.sql"; \
	docker exec postgres pg_dump -U postgres obsidian_mcp > $$BACKUP_FILE 2>/dev/null || true; \
	gzip $$BACKUP_FILE 2>/dev/null || true; \
	echo "$(GREEN)Backup: $$BACKUP_FILE.gz$(NC)"

db-restore:
	@if [ -z "$(FILE)" ]; then echo "$(RED)Usage: make db-restore FILE=<path>$(NC)"; exit 1; fi
	@echo "$(YELLOW)WARNING: This will replace the obsidian_mcp database!$(NC)"
	@echo "Press Ctrl+C to cancel, waiting 5s..."
	@sleep 5
	@if echo "$(FILE)" | grep -q ".gz$$"; then \
		gunzip -c $(FILE) | docker exec -i postgres psql -U postgres obsidian_mcp; \
	else \
		docker exec -i postgres psql -U postgres obsidian_mcp < $(FILE); \
	fi
	@echo "$(GREEN)Restored from $(FILE)$(NC)"

reindex:
	@echo "$(GREEN)Triggering reindex...$(NC)"
	@curl -s -X POST http://localhost:8000/api/reindex | python3 -m json.tool 2>/dev/null || echo "$(RED)Service not responding$(NC)"

reset-embeddings:
	@echo "$(YELLOW)Resetting embeddings — column will be recreated at EMBEDDING_DIMENSIONS$(NC)"
	@echo "Press Ctrl+C to cancel, waiting 5s..."
	@sleep 5
	$(COMPOSE) exec obsidian-mcp python -m scripts.reset_embeddings
	@echo "$(GREEN)Done. The next indexer pass will re-embed all notes.$(NC)"

rebuild-tsvectors:
	@echo "$(YELLOW)Rebuilding keyword (FTS) index for the configured FTS_CONFIGS...$(NC)"
	$(COMPOSE) exec obsidian-mcp python -m scripts.rebuild_tsvectors
	@echo "$(GREEN)Done. Keyword search now reflects FTS_CONFIGS (embeddings untouched, no API calls).$(NC)"

status:
	@echo "$(GREEN)Obsidian MCP Status:$(NC)"
	@$(COMPOSE) ps
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
	pip-audit -r requirements.txt
