# Obsidian MCP Server

Self-hosted MCP server exposing an Obsidian vault (~2,577 markdown files) via semantic search, full-text search, and agentic exploration.

## Stack
- Python 3.12 / FastAPI / uvicorn
- SQLAlchemy async + asyncpg + pgvector
- MCP Python SDK (Streamable HTTP)
- PostgreSQL 16 with pgvector (shared instance — see `.env`)
- Embedding provider abstraction (`src/services/embeddings.py`):
  - `OllamaProvider` — bge-m3 by default, set `OLLAMA_URL` in `.env`
  - `OpenAIProvider` — `text-embedding-3-{small,large}` over httpx, supports
    Azure OpenAI / OpenAI-compatible base URLs
- Jinja2 control panel; htmx and Chart.js vendored under
  `src/control_panel/static/vendor/`, no CDN, hand-written CSS, **no CSP** —
  see [control panel](docs/architecture/control-panel.md) for why.

## Project Layout
- `src/main.py` — FastAPI app, lifespan, MCP mount
- `src/config.py` — pydantic-settings
- `src/database.py` — async SQLAlchemy engine/session
- `src/models/db.py` — ORM models (api_keys, usage_logs, notes_metadata, note_embeddings, note_links, indexer_runs, transfer_tokens, oauth_clients/codes/tokens)
- `src/mcp_server/` — MCP server, tools, auth middleware
- `src/services/` — vault ops, search, embeddings, indexer, transfer, anchored FS
- `src/transfer/` — public `/transfer/*` capability-redemption routes
- `src/api/` — control panel REST endpoints
- `src/control_panel/` — Jinja2 templates + static assets
- `alembic/` — database migrations

## Infrastructure
- Container: `obsidian-mcp`, listens on `:8000`
- Traefik routes: hostname driven by `MCP_HOSTNAME` in `.env`
  - Panel routes: OAuth protected via `chain-oauth@file`
  - MCP routes (`/mcp/*`): API key auth at app level
  - Transfer routes (`/transfer/*`): same router as `/mcp` (no OAuth chain);
    capability-token auth at app level
- Registry: `localhost:5000` (or change in `Makefile`)
- Deploy: `make deploy` (build → scan → push → backup → migrate → recreate)

## Public repo — host paths live outside the tree
This repo is published on GitHub. Anything host-specific (paths, secrets,
hostnames) must stay out of tracked files. The mechanism:
- `Makefile.local` (gitignored) overrides `DEPLOY_DIR` and `DATA_DIR`. On
  the production host both point at `/storage/docker/data/obsidian-mcp/`,
  which holds the real `docker-compose.yml`, `.env`, and `backups/`.
- The compose project that owns the running container is rooted at
  `$(DEPLOY_DIR)`, not the repo. Always invoke `make` from the repo so
  `Makefile.local` loads — `cd /storage/docker/data/obsidian-mcp && docker
  compose ...` works but skips the build/push pipeline.
- The repo's `docker-compose.yml` and the deploy-dir copy are kept
  identical; if you change one, copy it over.

## Commands
- `make init` — first-time setup
- `make deploy` — full build, backup, migration, and deploy
- `make db-init` — create database + pgvector extension
- `make db-migrate` — run alembic migrations
- `make db-check` — `alembic check`: schema vs. ORM models (must be clean)
- `make test-schema` — schema gate: migrations vs. models on a throwaway
  pgvector container (run before any deploy that carries a migration)
- `make audit` — pip-audit dependency scan
- `make reindex` / `make reset-embeddings` / `make rebuild-tsvectors` — index
  maintenance (see [indexing and embeddings](docs/architecture/indexing-and-embeddings.md))
- `make db-backup` / `make db-restore` — database dump and restore
- `make logs` — tail container logs
- `make status` — check health

`make help` lists the rest. Deployment detail lives in `DEPLOYMENT.md`, the
user-facing documentation in `README.md`.

## Engineering workflow

The standard flow — supervisor main thread, **Opus** subagents, OpenSpec, the
two Codex audit gates, `openspec-verifier`, and the `user-representative`
browser pass — is defined once in `~/.claude/rules/engineering-workflow.md` and
applies here. Read it; don't restate it. This section records only what is
specific to this MCP server.

**Local gates**

| Gate | Command |
| --- | --- |
| Dependency audit | `make audit` (pip-audit) |
| Migrations | `make db-migrate` (alembic) |
| Schema drift | `make db-check` (`docker exec obsidian-mcp alembic check`) |
| Schema gate (any change carrying a migration) | `make test-schema` |
| Deploy | `make deploy` (build, backup, migrate, deploy) |

**`alembic check` must be clean** — "No new upgrade operations detected." Run
it after any migration and after any deploy that ran one (`make db-check`, or
`docker exec obsidian-mcp alembic check` directly). A dirty check means the
database and the models disagree, which makes the next autogenerate emit noise
and, worse, hides a real missing constraint in it — see
[schema and migrations](docs/architecture/schema-and-migrations.md).

**Codex framing for this product.** The consumer here is an **agent**, not a
person, and the vault is Max's single source of truth for every project. The
expensive failures are **destructive writes** (an edit or append that clobbers
a note — this has actually happened) and **silently wrong search results**,
which an agent will act on without a human ever seeing the query. Treat any
change to the write tools, section addressing, or the chunking/embedding path
as a mandatory adversarial-pass trigger.

**No `user-representative` pass** — there is no browser UI. Substitute an
end-to-end exercise of the affected MCP tools against the live server, and say
in the report which tools were actually called.

## Key decisions

The load-bearing rationale — the "this was a bug once, do not undo it"
material — lives in [`docs/architecture/`](docs/architecture/), indexed at the
bottom of this file. **Read the relevant note before changing that area, and
update it in the same change.** What stays here is the short list:

- API keys use `omcp_` prefix, stored as SHA-256 hashes
- Panel/OAuth passwords: direct `bcrypt` (`$2b$`, cost 12); both hash and verify truncate the UTF-8 encoding at 72 bytes and reject NUL bytes — passlib's historical semantics — so existing hashes stay valid; don't "fix" the truncation.
- Vault mounted read-write at /obsidian in container
- Read responses are capped in characters (`MAX_READ_RESPONSE_CHARS`, default
  40,000) independently of the byte caps on disk I/O — see "Three kinds of size cap"
  in [vault tools](docs/architecture/vault-tools.md). Tool output is model
  input; the server must bound it.
- Two active users' vault roots may not overlap. `src/services/vault_overlap.py`
  compares each pair by inode identity `(st_dev, st_ino)` and by a
  **component-wise** canonical-containment test, at assignment time and before
  every pass (five entry points, `detect_and_publish`); an affected pair is
  refused by every MCP tool, pass stage and transfer redemption, and nothing is
  deleted. **Until a snapshot is published, `_vault_root` refuses every
  multi-user caller** — the lifespan publishes synchronously before serving.
  A bind-mount *graft* is still undetected (L1/L2, owner decision pending) and
  the consequence is cross-tenant read/overwrite/delete — see
  [vault roots and tenancy](docs/architecture/vault-roots-and-tenancy.md).
- **`/mcp` rate control is in-process, and `--workers 1` is part of the
  contract.** Two per-principal token buckets in `_tracked` (general 120/min
  burst 30; write 60/min burst 15 on the eight vault-mutating tools *and*
  `PUT /transfer/upload`, charged to the principal that **minted** the
  capability) plus a per-address budget on failed `/mcp` authentication. The
  principal is `("api_key", id)` or `("oauth", grant_id)` — the **grant**, so a
  refresh does not mint a fresh allowance. State is in the worker's memory and
  deliberately not persisted; a second uvicorn worker multiplies every rate by
  the worker count. See [rate limits](docs/architecture/rate-limits.md).
- **The buckets are the first gates; the daily quota stays the last pre-body
  gate.** Nothing durable is consumed by a call that does not run — a
  rate-refused call spends no `quota_counters` slot. Every refusal raised
  inside `_tracked` ends with one parseable line,
  `MCP-REFUSAL {"code":…,"retry_after_seconds":…}`, carried identically into a
  structured tool's error field; the transport 429s (failed-auth budget,
  transfer redemption) are deliberately outside that contract. `rate_limited`
  rows are coalesced — a row stands for `1 + suppressed` refusals.
- **New API keys get `DEFAULT_DAILY_REQUEST_LIMIT` (5,000); existing keys are
  grandfathered** — applied in application code, never as a column default, and
  an explicit `null` (or a blank panel field) still means unlimited. OAuth
  grants and pre-existing NULL-limit keys therefore have **velocity bounds
  only**, owner-accepted. **Concurrency is not bounded at all** — deferred to
  `mcp-concurrency-slots`, which ships in shadow mode first.
- Wikilink graph extracted from note bodies into `note_links`; resolved at index time with same-folder-first preference
- `MCP_SANDBOX_MODE=true` is a registry-eval-only switch: lifespan skips `_check_embedding_dim` and the indexer, and `APIKeyMiddleware` bypasses auth on `/mcp/*`. Lets Glama's sandbox build the image and validate MCP introspection without external deps. Never enable in production — tools register but cannot run.

## MCP tools

25 tools, all registered in `src/mcp_server/server.py` and all wrapped by
`_tracked`, which resolves the caller's vault root *before* the tool body runs
— that decorator is the whole enforcement of "this user has no vault"
([vault roots and tenancy](docs/architecture/vault-roots-and-tenancy.md)).

**Search and read** — `keyword_search`, `semantic_search`, `read_note`,
`list_notes`, `get_tags`, `get_recent`, `get_vault_guide`.

**Graph**
- `get_backlinks(path, limit)` — notes that link TO `path` (resolved links only).
- `get_links(path)` — outgoing links from `path`, both resolved and dangling.
- `get_neighborhood(path, depth=1, limit=50)` — undirected BFS over the resolved-link graph; capped at `depth ≤ 5` and `limit ≤ 200`.
- `find_related(path, limit=10)` — semantic neighbors via averaged chunk embeddings; pgvector cosine distance, deduped per note.
- `find_orphans(folder, limit)` — notes with no incoming or outgoing resolved links; vault-hygiene tool.

Link extraction lives in `src/services/links.py`. The extractor strips fenced/inline code before regex matching for `[[wikilink]]`, `![[embed]]`, and `[md](path.md)` forms. Targets are resolved at index time and stored in `note_links`. On startup, if `note_links` is empty the indexer runs a one-shot backfill across all notes (logged with progress and surfaced on the dashboard).

**Write** — `create_note`, `edit_note`, `move_note`, `delete_note`,
`set_frontmatter`. Markdown only, all publishing through
`vault._atomic_write_at` against a descriptor opened at validation. The
destructive-write surface: read
[vault tools](docs/architecture/vault-tools.md) first.

**Raw files (non-markdown)** — `read_file`, `write_file`, `list_files`,
`delete_file`. Pure byte transport, same traversal and dot-dir guards, no
indexing.

**Transfer** — `request_upload`, `check_upload`, `request_download`,
`import_from_url`, plus the public `/transfer/*` routes. A capability pins
everything it may do at mint time; see
[file transfer](docs/architecture/file-transfer.md).

## Architecture notes

One file per area, each holding the decisions that must not be silently
reversed. They are the extracted "why" of this codebase — verbatim, not
summaries.

| Note | Read it before you touch |
| --- | --- |
| [schema-and-migrations.md](docs/architecture/schema-and-migrations.md) | any alembic migration; `alembic check` is the cheap gate, not the whole gate |
| [oauth-and-grants.md](docs/architecture/oauth-and-grants.md) | `src/oauth/`, the consent page, anything minting/rotating/revoking a token |
| [vault-roots-and-tenancy.md](docs/architecture/vault-roots-and-tenancy.md) | `APIKeyMiddleware`, `_vault_root`, owner predicates, publication confirmation, the vault-root overlap guard (`src/services/vault_overlap.py`, the snapshot, the five pass entry points) |
| [vault-tools.md](docs/architecture/vault-tools.md) | any note or file tool: frontmatter, symlinks, anchored writes, section addressing, size caps |
| [file-transfer.md](docs/architecture/file-transfer.md) | `src/transfer/`, `src/services/vault_fs.py`, the publish gate, SSRF policy, the write-bucket charge at redemption |
| [search.md](docs/architecture/search.md) | `semantic_search` / `keyword_search` / `find_related` and every `SET LOCAL` they issue |
| [indexing-and-embeddings.md](docs/architecture/indexing-and-embeddings.md) | the indexer loop, the embed pass, tsvector writers, provider abstraction |
| [security-event-logging.md](docs/architecture/security-event-logging.md) | `src/logging_setup.py`, `src/services/security_events.py`, and any call site that logs a refusal: the field allow-list, the event catalogue, the suppressor |
| [rate-limits.md](docs/architecture/rate-limits.md) | `src/services/rate_limits.py`, `src/services/refusals.py`, the failed-auth budget in `APIKeyMiddleware`, the gate order in `_tracked`, the worker count, and every `MCP_RATE_LIMIT_*` / `MCP_AUTH_FAILURE_*` / `DEFAULT_DAILY_REQUEST_LIMIT` setting |
| [usage-attribution.md](docs/architecture/usage-attribution.md) | `usage_logs`, `_log_usage`, actor columns |
| [control-panel.md](docs/architecture/control-panel.md) | panel templates, flash messages, admin guards, the Danger zone |

Formal requirements live in `openspec/specs/`; the change history and its
reasoning in `openspec/changes/archive/`.
