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
- Jinja2 + htmx + Tailwind CDN control panel

## Project Layout
- `src/main.py` — FastAPI app, lifespan, MCP mount
- `src/config.py` — pydantic-settings
- `src/database.py` — async SQLAlchemy engine/session
- `src/models/db.py` — ORM models (api_keys, usage_logs, notes_metadata, note_embeddings, note_links, transfer_tokens)
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
- `make logs` — tail container logs
- `make status` — check health

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
and, worse, hides a real missing constraint in it — see "Schema drift is not
only what `alembic check` sees" below.

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

## Schema drift is not only what `alembic check` sees

`alembic check` autogenerates against the live database and reports what it
would emit. It compares tables, columns, nullability and indexes — it does
**not** compare CHECK constraint predicates. So it is the cheap gate, not the
whole gate.

Issue #53 found both kinds at once. The models declare nine columns NOT NULL
that their migrations left nullable (`api_keys.is_active/created_at`,
`notes_metadata.indexed_at`, `oauth_clients.created_at`,
`oauth_codes.used/created_at`, `oauth_tokens.revoked/created_at`,
`usage_logs.created_at`) — `alembic check` sees that, and a freshly migrated
001→012 database has it too, so it was a migration bug. Separately, the CHECK
`ck_oauth_clients_auth_method_secret` that migration 010 creates was **absent
on the live database** while `alembic_version` read 012 — `alembic check`
cannot see that at all, and neither can a lookup by constraint name, which a
same-named `CHECK (true)` would satisfy while enforcing nothing.

Migration `013_schema_reconciliation` fixes both and enforces the *complete*
010 shape rather than just the missing piece, because how the live database
lost the constraint is unknown. Its rules, all load-bearing:

- The constraint is resolved through `pg_constraint` — `conrelid`, `contype`,
  `convalidated`, and `pg_get_constraintdef` compared against the canonical
  predicate — never by name. The canonical rendering is derived at runtime from
  an empty scratch table (`CREATE TEMP TABLE … (LIKE oauth_clients)`) so it is
  the server's own normalization, not a hand-written string pinned to one
  Postgres major.
- The server default is compared **exactly** — `pg_get_expr` on `pg_attrdef`
  against a canonical default derived the same way, by setting it on a scratch
  `TEMP` table and reading it back. A substring test would accept
  `'not_client_secret_post'`, and autogenerate does not compare server defaults
  at all, so this comparison is the only thing that sees that drift.
- **Rows are never mutated to satisfy a constraint, and nothing the constraint
  reads is written before the constraint has been verified.** The offender
  query runs over the raw rows *first*, and a NULL `token_endpoint_auth_method`
  counts as an offender. **Do not "simplify" this by backfilling the NULL to
  `client_secret_post` before the check** — that was the first draft and review
  caught it. A CHECK passes when its predicate is NULL, so such a row really
  can exist on a drifted database; backfilling it manufactures a passing row,
  i.e. invents an auth method for a client and lets it authenticate with
  whatever secret it happens to carry. Only after the check passes does 013
  touch the column's type/default/NOT NULL — declarative fixes that write no
  row value, and `SET NOT NULL` then needs no backfill because a NULL would
  already have raised. A violating row makes the migration raise, naming the
  `client_id`s; the transaction rolls back and the deploy aborts before the
  container is recreated. If the column is *absent*, 013 refuses rather than
  adding it — 010 always adds it, so its absence means guessing an auth method
  for every existing client.
- `LOCK TABLE oauth_clients IN SHARE ROW EXCLUSIVE MODE` is held from before
  the offender check to the end, so no insert can land between the check and
  `ADD CONSTRAINT`. `lock_timeout=10s` / `statement_timeout=60s` make a
  blocked migration fail fast instead of stalling the deploy; they are **per
  statement / per lock acquisition**, not a budget for the transaction. 013
  `RESET`s both at the end of `upgrade()`: `SET LOCAL` lasts for the
  transaction, and `alembic/env.py` runs *every* pending revision in one, so a
  future 014 would otherwise inherit them.
- **Lock order is child-first.** The five other tables are backfilled and set
  NOT NULL before `oauth_clients` is locked, matching the app's own direction
  (`src/oauth/routes.py` locks `oauth_codes` `FOR UPDATE`, then reads
  `oauth_clients`, then inserts `oauth_tokens`), so a concurrent OAuth request
  queues behind the migration instead of closing a wait cycle with it. That is
  an ordering guarantee, not an absence of contention: the residual behaviour
  is rollback-and-retry — past `lock_timeout` the whole transaction aborts and
  the deploy fails before the container is recreated. Re-run when quiet.
- Downgrade drops the constraint **only if it carries 013's COMMENT marker**.
  A fresh 001→013 database got its constraint from 010, so downgrading to 012
  must keep it. NOT NULL is never relaxed on downgrade — that would re-create
  the drift the migration exists to remove.
- The scratch `TEMP` tables mean the migration role needs the **TEMP
  privilege** on the database. The deploy's owner role has it; a locked-down
  migration role may not.

`tests/integration/test_schema_check.py` is the gate, and **`make test-schema`
is how you run it** — throwaway `pgvector/pgvector:pg16` up, module, container
removed pass or fail. Run it before any deploy that carries a migration; it
never reads the deploy `.env` or touches the live database. The target sets
`OMCP_REQUIRE_SCHEMA_INTEGRATION=1`, which turns the module's opt-in
`PGVECTOR_TEST_ADMIN_URL` skip into a hard failure — a gate that silently skips
because nobody exported the URL is not a gate. (Plain `pytest tests/` still
skips it, which is what you want on a machine with no Postgres.) It asserts
`alembic check` clean *and* the catalog directly *and* that the two forbidden
inserts are actually rejected, across fresh / drifted / impostor-constraint /
violating-row / NULL-method / missing-column / wrong-default / wrong-type /
`client_secret_hash NOT NULL` / nullable-method / stamp-back / downgrade paths.
Idempotence is exercised by `alembic stamp 012` then `upgrade head`, not by a
second `upgrade head` — the latter is a no-op at the alembic level and proves
nothing about the migration body.

## Key Decisions
- API keys use `omcp_` prefix, stored as SHA-256 hashes
- Panel/OAuth passwords: direct `bcrypt` (`$2b$`, cost 12); both hash and verify truncate the UTF-8 encoding at 72 bytes and reject NUL bytes — passlib's historical semantics — so existing hashes stay valid; don't "fix" the truncation.
- **OAuth consent preselects "Read only" unconditionally — never bind `checked` to the requested scope.** On an HTML form the checked radio *is* the submitted value, so the preselect is the default grant, not a display detail. `/register` is unauthenticated and a DCR client that omits `scope` is registered `read readwrite offline_access`, so a requested-scope preselect lets one unchanged **Approve** click hand vault-wide write to any self-registered client (#63; #62 introduced exactly that and it was reverted). What #62 was right about is kept as *prose*: `authorize_get` passes `requested_write` (from the validated scope alone, ungated by the registered scope — display only) and `write_unavailable`, and the request box names the level asked for and says when the client cannot hold it. `_clamp_scope` in `authorize_post` is the enforcement boundary and is unrelated to any of this. The markup default is not enough on its own: Firefox restores a control's dynamic checked state across page loads in preference to it, so the form **and** every `name="scope"` radio carry `autocomplete="off"` — without them a user who once picked write gets that radio re-checked on a repeat visit to the same `/authorize` URL (clients reuse `state`/PKCE) and one Approve re-grants write after a revocation. The `.scope-option:has(:checked)` highlight lives in standalone rule blocks: a selector list is dropped wholesale when any part fails to parse, so grouping it would take the native-radio fallback down with it.
- Vault mounted read-write at /obsidian in container
- Read responses are capped in characters (`MAX_READ_RESPONSE_CHARS`, default
  40,000) independently of the byte caps on disk I/O — see "Three kinds of size
  cap" below. Tool output is model input; the server must bound it.
- Embeddings: pluggable provider, `EmbeddingProvider` Protocol with two
  implementations (Ollama, OpenAI). Single `EMBEDDING_PROVIDER` env var
  picks the backend; `get_provider()` is a cached singleton. Default is
  Ollama bge-m3 at 1024 dim, 512 token chunks, no overlap.
- Full-text search via PostgreSQL tsvector. The text-search config(s) are
  configurable via `FTS_CONFIGS` (default `["english"]`; e.g. `["simple"]` or
  `["english","norwegian"]`). Index- and query-time configs are kept in sync
  through `src/services/fts.py` (`index_tsvector_sql` / `combined_tsquery`).
  A note is indexed under every config (tsvectors `||`-concatenated) and a
  query matches if any config hits (tsqueries OR'd). Startup validates the
  config names against `pg_ts_config`. Changing `FTS_CONFIGS` requires `make
  rebuild-tsvectors` — keyword index only, no embeddings, no API calls.
  `full_text_search` also issues `SET LOCAL random_page_cost = 1.1` (the
  planner costs the heap at `relpages` and does not model detoast I/O, so it
  seq-scanned and detoasted every tsvector: 13,086 buffers vs 1,146) and
  orders by `rank DESC, file_path ASC`. The tie-break is not cosmetic — a
  plan change would otherwise change *which* tied rows survive the LIMIT.
  Index usage is the expected plan for rare terms on a production-sized
  corpus, not a guarantee; a tiny table or a very common term may legitimately
  seq-scan.
- Vector search via pgvector HNSW index on `note_embeddings.embedding`
  (`vector_cosine_ops`, `m=16, ef_construction=64`); `semantic_search`
  sets `hnsw.ef_search=80` per query and dedupes per note in Python
  after a 5x overfetch. See "Filtered vector search" below — the
  `SET LOCAL`s are load-bearing for *correctness*, not just speed.
- Indexer runs on startup then every 5 minutes, hash-based change detection.
  Each periodic tick ends with `prewarm_search_caches()` **inside**
  `index_pass_lock`: one `get_embedding("warmup")` (Ollama only — a remote API
  has no warm state) and one HNSW probe with a deterministic non-zero unit
  vector, the whole thing under a single 15 s `asyncio.wait_for`. It exists
  because `semantic_search` is bimodal (≈0.47 s warm, ≈17.5 s cold: 14 s of
  Ollama reloading bge-m3, 3 s of HNSW pages missing from a 128 MB shared
  `shared_buffers`) and the median gap between calls has grown to ~28 min.
  It logs and swallows ordinary failures (the indexer's `consecutive_failures`
  must not react to it) but **re-raises `CancelledError`** so lifespan shutdown
  still stops the loop.
- **The dashboard's "Last run" is an in-process heartbeat, not
  `max(notes_metadata.indexed_at)`.** `indexer.last_index_run_at` /
  `last_index_run_ok` are stamped at the end of the startup pass and of every
  periodic tick (`False` in the `except Exception` branch; `CancelledError` is
  a `BaseException` so shutdown is not recorded as a failure). `indexed_at`
  only moves for notes a pass actually upserted or moved, so a pass over an
  unchanged vault writes it nowhere and a healthy indexer looked stalled for
  days on an idle vault — an invitation to reach for the Danger zone and
  re-embed the whole vault for nothing (#78). `max(indexed_at)` is still shown,
  under its own label "Last change detected". No migration and no per-tick
  write: it answers "is *this process's* loop alive", which is a property of
  the process, and it resets to `None` on restart until the startup pass lands.
- **Because the pre-warm holds `index_pass_lock`, the panel's destructive
  actions take it too.** `reset_embeddings` and `trigger_reembed` set
  `indexer_paused`, then `await session.close()` on the request's own session
  **before** waiting for the lock — a waiter that keeps its pooled connection
  deadlocks against a lock holder that needs one — and only then open a fresh
  session inside the lock (`_pass_lock_without_a_connection`). `trigger_reembed`
  also NULLs `notes_metadata.embedded_content_hash` in the same transaction as
  the `DELETE`: `embed_vault` selects on hash mismatch, so deleting vectors
  alone meant the reindex it spawns re-embedded nothing.
- **Every panel handler that can change `users.is_admin` / `users.is_active`
  takes `_lock_admin_guard(session)` before counting the remaining admins**
  (`src/control_panel/users.py` — `edit_user_submit` and `delete_user`, one
  shared `pg_advisory_xact_lock` key). The last-admin guard is a count
  followed by a write; without the lock two admins demoting each other
  concurrently both read "one other admin remains", both pass, and the panel
  is left with zero admins and no way back in through the UI. The lock is
  transaction-scoped, so **never commit between taking it and writing the
  flags** — that is what makes the check-then-act atomic. A new handler that
  flips either flag must take it too, and use the *same* constant: two keys
  do not exclude each other. **Immediately after the lock, both handlers
  re-read the acting admin's own `is_admin`/`is_active`
  (`_actor_still_privileged`) and refuse unless both are exactly True** —
  `require_admin_panel` authorised the request before the lock was requested,
  and the wait for that lock is precisely the window in which another admin's
  demotion of *this* actor commits; serializing the writes is no use if the
  loser of the race then performs the mutation anyway.
- Wikilink graph extracted from note bodies into `note_links`; resolved at index time with same-folder-first preference
- `MCP_SANDBOX_MODE=true` is a registry-eval-only switch: lifespan skips `_check_embedding_dim` and the indexer, and `APIKeyMiddleware` bypasses auth on `/mcp/*`. Lets Glama's sandbox build the image and validate MCP introspection without external deps. Never enable in production — tools register but cannot run.

## Embedding providers
- `EMBEDDING_PROVIDER=ollama` (default) — uses `OLLAMA_URL` and
  `EMBEDDING_MODEL`; serial single-input HTTP per chunk.
- `EMBEDDING_PROVIDER=openai` — requires `OPENAI_API_KEY` (validated at
  startup). Uses `OPENAI_BASE_URL` (default `https://api.openai.com/v1`)
  and `OPENAI_EMBEDDING_MODEL` (default `text-embedding-3-small`). Native
  batching: up to 96 inputs per `/v1/embeddings` POST, with sub-batching
  for larger lists. Retries 429/5xx with exponential backoff, max 3
  attempts.
- `EMBEDDING_DIMENSIONS` (default 1024) controls both the pgvector column
  width and the `dimensions` param on OpenAI requests.
- Reset workflow: `make reset-embeddings` (or **Settings → Danger zone →
  Reset embeddings** in the panel) drops & recreates `note_embeddings.embedding`
  at the configured dim and clears every `embedded_content_hash`. The next
  indexer pass re-embeds the vault.
- Dimension-mismatch guard: lifespan startup queries `pg_attribute` for
  the live column dim and `sys.exit(1)`s if it disagrees with
  `EMBEDDING_DIMENSIONS`, with a log message pointing to
  `make reset-embeddings`.
- pgvector version guard (`_check_pgvector_version`, next to the dim guard in
  `src/main.py`): reads `pg_extension.extversion` for `vector` and
  `sys.exit(1)`s below **0.8.0**, naming `hnsw.iterative_scan`. Skipped in
  sandbox mode and when the extension is not installed yet (alembic's job).
  See "Filtered vector search" for why an older backend fails *silently*
  without it.

## Filtered vector search — the SET LOCALs are correctness, not tuning

Both vector paths (`semantic_search` in `src/services/embeddings.py`,
`find_related_impl` in `src/mcp_server/tools.py`) issue three transaction-scoped
settings before the query, and all three matter:

- `hnsw.ef_search = 80` — recall@10 ≈ 98%.
- `random_page_cost = 1.1` — SSD costing; without it the planner prefers a
  seq scan + sort, which is fine on a small table and degrades linearly.
- `hnsw.iterative_scan = 'relaxed_order'` — **the recall fix.** With
  `random_page_cost` lowered, the planner picks HNSW → nested loop → filter.
  A non-iterative HNSW scan yields at most `ef_search` candidates; a `folder` /
  `tags` / `frontmatter` / `user_id` predicate then discards most of them and
  *nothing refills*. Measured: 45 of 120 folder-filtered probes returned zero
  rows, 100 returned short. `relaxed_order` keeps walking the graph until the
  overfetch is satisfied after filtering.

Consequences that are easy to undo by accident:

- **Re-sort before dedupe.** `relaxed_order` may emit rows slightly out of
  distance order across iterations, so both paths select the cosine distance as
  a column and sort by it before per-note dedupe/truncation. This is
  presentation only — it cannot recover candidates the scan never returned.
- **Zero-row exact fallback.** An empty result from an approximate *filtered*
  scan is ambiguous. Both paths re-run the identical statement after
  `SET LOCAL enable_indexscan = off` (pgvector's documented exact search) and
  use those rows, recording `exact_fallback: true` in `usage_logs.params`. This
  is what makes "empty only when nothing matches" a construction rather than a
  benchmark hope. It is O(n), which is acceptable only because it is the rare
  path — do not make it unconditional.
- **The recall contract is a benchmark SLO**, not a per-query guarantee: set
  recall ≥ 0.9 against an *exact filtered sequential scan taken at the same
  overfetch with the same dedupe*. HNSW is approximate and the overfetch is
  fixed at `max(5 × limit, 50)` for both paths, so a verbose note can still
  crowd out others after dedupe — the baseline shares that property.
- Recall is bounded by `hnsw.max_scan_tuples` (20,000) and
  `hnsw.scan_mem_multiplier` (1). At ~16.7k chunks the vault is under the cap;
  those are the next knobs, not `ef_search`.

## Search benchmarks (opt-in integration)

`tests/integration/test_search_recall.py` and `test_keyword_plan.py` run only
when `PGVECTOR_TEST_ADMIN_URL` names a throwaway Postgres **server** (the
harness creates and drops its own database per module — see
`tests/integration/_harness.py`):

```sh
docker run --rm -d --name pgvector-search-test -e POSTGRES_PASSWORD=test \
    -p 55433:5432 pgvector/pgvector:pg16
PGVECTOR_TEST_ADMIN_URL=postgresql+asyncpg://postgres:test@localhost:55433/postgres \
    pytest -q tests/integration/
docker rm -f pgvector-search-test
```

Two things about these fixtures are load-bearing and non-obvious:

- **The filtered slice must be a large fraction of the corpus.** A filter
  matching a few percent makes the planner estimate a tiny join and pick a seq
  scan + sort — the HNSW nested-loop plan the recall bug lives in never
  appears, and every assertion passes against a plan production does not use.
- **The keyword corpus needs `VACUUM`, not just `ANALYZE`.** A GIN index's cost
  estimate comes from its metapage stats, which only VACUUM writes. Without it
  `gincostestimate` assumes the whole index must be scanned (cost 621 vs 4.15
  here) and the planner hint looks broken. Production gets this from
  autovacuum; a freshly-seeded test database does not.

Recorded numbers on that corpus: rare-term keyword query 228 buffers with the
hint vs 29,071 sequential; common-term 57,799 either way (seq scan is the right
plan there, so it is recorded, not asserted).

## Per-phase search timing

`usage_logs.params` carries `embed_ms` + `db_ms` + `exact_fallback` for
`semantic_search`, and `db_ms` + `exact_fallback` for `find_related` (it makes
no embedding call). A single whole-call `duration_ms` could not separate the
two independent cold paths — provider eviction and HNSW page cache — so the
last regression had to be diagnosed with hand-run probes against the live DB.

**`usage_logs.tool` must hold the name the tool is registered under.**
`_tracked`'s first argument is that name, and FastMCP takes it from the
function name in `server.py` — so `search_notes_impl` is logged as
`keyword_search`, not `search_notes`, which named a tool no client is ever
offered and made `WHERE tool = 'keyword_search'` return nothing (#78). Rows
written before that fix keep the old spelling, which is why `_usage_detail` in
`src/control_panel/routes.py` still lists it alongside the current one.

The holder is a `ContextVar` in `src/services/timing.py`, **owned by
`_tracked`**: fresh dict at call start, cleared in `finally`. The ContextVar
lives in a service module only to avoid an import cycle (`tools` imports
`semantic_search`); nothing but `_tracked` calls `begin()`/`clear()`. Service
return types are unchanged — a direct call outside a tracked tool finds no
holder and records nothing. No migration: `params` is JSONB.

## The vault assignment is the admission gate for every tool

`_tracked` in `src/mcp_server/tools.py` resolves `_vault_root(current_user_id)`
**once, before the tool body runs**, and fails the call with a tool error when
it raises. That is the whole enforcement of "this user has no vault", and it
lives in the shared decorator on purpose.

Per-tool checks were the bug (#66). The tools that leaked — `semantic_search`,
`keyword_search`, `list_notes`, `get_recent` and every graph tool — are exactly
the ones with no reason to call `_vault_root`: they are served from
`notes_metadata` / `note_embeddings` filtered by `user_id` alone. Unassigning
`users.vault_path` stopped only the disk-touching tools, while the indexer's
`_active_user_ids()` (which filters `vault_path IS NOT NULL`) meant the user's
rows were never pruned either. An unchanged API key kept returning paths,
titles, tags, frontmatter and chunk excerpts indefinitely, while the panel had
told the operator "vault tools error".

- **Nothing is exempt.** Every `_tracked` tool reads or writes vault content or
  vault metadata — `get_vault_guide` returns the vault's own `CLAUDE.md`,
  `check_upload` reports a published vault path and digest. Keep the exemption
  list at zero; a new tool inherits the gate by being registered.
- **The index rows are preserved.** Deleting `notes_metadata` on the NULL
  transition was the weaker fix: it forces a full re-embed on reassignment and
  leaves the credential itself unaddressed.
- **`_vault_root` must stay a pure cache lookup.** What makes that correct is
  `APIKeyMiddleware` calling `warm_user_vault_cache(session, user_id)` on
  *every* authenticated MCP request. Do not add a DB query to the gate.
- **The single-user form of that warm is authoritative — it evicts.** It used
  to be a silent no-op for a NULL `vault_path`, so a previously cached root
  survived; the panel's `clear_user_vault_cache` only clears the worker that
  served the POST. Eviction is what makes a mid-session unassignment visible
  from the next call in every process. The bulk form stays add-only (it would
  otherwise drop a just-warmed entry for a user created after its query).
- **A cold cache refuses too**, with the same message — it is not permission to
  serve stale rows — and the refusal is written to `usage_logs` with
  `params["error"] = "no_vault_assigned"` and no other new field.
- Single-user and sandbox mode are untouched: `current_user_id` is None there
  and `_vault_root(None)` answers from `settings.vault_path`.

## Graph tools
- `get_backlinks(path, limit)` — notes that link TO `path` (resolved links only).
- `get_links(path)` — outgoing links from `path`, both resolved and dangling.
- `get_neighborhood(path, depth=1, limit=50)` — undirected BFS over the resolved-link graph; capped at `depth ≤ 5` and `limit ≤ 200`.
- `find_related(path, limit=10)` — semantic neighbors via averaged chunk embeddings; pgvector cosine distance, deduped per note.
- `find_orphans(folder, limit)` — notes with no incoming or outgoing resolved links; vault-hygiene tool.

Link extraction lives in `src/services/links.py`. The extractor strips fenced/inline code before regex matching for `[[wikilink]]`, `![[embed]]`, and `[md](path.md)` forms. Targets are resolved at index time and stored in `note_links`. On startup, if `note_links` is empty the indexer runs a one-shot backfill across all notes (logged with progress and surfaced on the dashboard).

## Write tools
- `create_note(path, content)` — create a new note (atomic write).
- `edit_note(path, content, append=False, find=None, section=None, replace_all=False, dry_run=False)` — four mutually exclusive modes (full-replace, append, find/replace, section). `dry_run` returns a unified diff without writing; `replace_all` lifts the single-match guard for `find`. Section mode matches ATX headings only and supports `Parent/Child` path-style and `#N` ordinal disambiguation (see "Section addressing" below).
- `move_note(from_path, to_path, rewrite_links=False)` — rename or relocate a note. Updates `notes_metadata.file_path` and `note_links.target_path` rows for the moved note. With `rewrite_links=True`, also rewrites `[[Old]]` / `[[Old|alias]]` / `[[Old#anchor]]` / `![[Old]]` / `[[folder/Old]]` forms in source notes.
- `delete_note(path, permanent=False)` — soft-delete to `.trash/<YYYYMMDD-HHMMSS>-<basename>` by default; `permanent=True` does a hard `os.unlink`. The indexer skips dot-dirs, so search/embedding cleanup happens on the next reindex pass.
- `set_frontmatter(path, updates, remove=[])` — structured YAML frontmatter mutation. Round-trips via `yaml.safe_dump` (does not preserve YAML comments). Leaves the body byte-identical.

All write tools route through `src/services/vault.py::write_file`, which writes to a tmp file in the same directory and `os.replace()`s it into place — a crash mid-write cannot truncate the destination.

### Mutations act on the path as named — never through a symlink

`validate_path` returns `(vault / rel).resolve()`, which **follows symlinks**. Every mutating tool used to act on that resolved path, so an in-vault alias `alias.md -> important.md` made `edit_note("alias.md", …)` rewrite `important.md` and report success for `alias.md` — a destructive write on a path nobody named (#54).

`validate_mutable_path(rel, user_id)` in `src/services/vault.py` is the guard, and it is what `write_file` / `write_bytes` validate with, so every mutation entry point is covered:

- the **parent** is resolved and must stay inside the vault. Symlinked *directories* inside the vault (shared attachment folders — a common Obsidian setup) therefore keep working; an ancestor pointing out of the vault is still the traversal error.
- the **final component is taken as named** and `os.lstat`-ed. A symlink — dangling included — is refused with an error naming the link's canonical vault-relative target ("`outside the vault`" when it escapes), so the agent can operate on the real note.
- it returns `resolved_parent / name`: the real directory entry the indexer sees. `move_note` derives `from_rel` / `to_rel` from it, so `notes_metadata.file_path`, `note_links` and backlink discovery agree with the filesystem for notes under a symlinked folder.

Applies to `create_note`, `edit_note` (all modes — `dry_run` refuses too, rather than diffing a note the caller did not name), `set_frontmatter`, `move_note` (source and destination), `delete_note` and `write_file`. `delete_file` already refused, via the anchored `vault_fs` walk.

**Reads are deliberately unchanged.** `read_note`/`read_file`/`list_*`/graph tools still follow links — an alias reading as its target is what a user expects from an alias, and a read cannot destroy anything.

**Resolve once, then act on the `Path` — never re-pass the caller's string.** Resolving the parent at validation only helps if the rest of the tool uses the returned `Path`. A tool that keeps the `Path` for the guard and then calls `read_bytes(path_str)` / `write_file(path_str)` resolves the string again, per call: an ancestor symlink repointed between the read and the write redirects the *write* to a note nobody named, and `expected=` cannot catch it because the decoy may hold byte-identical bytes. So every read-modify-write inside one tool call goes through the `*_at` helpers in `vault.py` — `read_bytes_at`, `write_file_at`, `write_bytes_at` — which take an already-validated absolute `Path` and never touch the vault root again. The string-taking `read_bytes` / `write_file` / `write_bytes` are single-shot conveniences; if you find yourself calling one *after* `validate_mutable_path`, you have reintroduced the bug.

The residual TOCTOU (leaf swapped for a link between the `lstat` and the read/write) is the same optimistic level as every other note write here — `O_NOFOLLOW` turns it into an `OSError`, which the write tools report as a tool error. Staging is covered too: `_atomic_write` creates its `.tmp-…` file with `O_CREAT|O_EXCL|O_NOFOLLOW` and retries the name a couple of times, so a planted symlink on the temp name is an `EEXIST` we step around rather than a decoy we truncate.

**The accepted residual, precisely.** A *static* alias — a symlink sitting in the vault, the #54 vector — cannot redirect any mutation: the parent is resolved once and the leaf is taken as named. What remains is a live race: a concurrent process that renames the resolved parent directory (putting a symlink at its name) or repoints a symlinked vault root *between two syscalls of a single write or soft-delete* can still send that operation somewhere else. That needs write access to the vault's directories at exactly the wrong microsecond, and it closes with the descriptor-anchored migration to the `vault_fs` helpers tracked in issue #59, not before.

An in-vault `..` (`Folder/../note.md`) is also refused by `validate_mutable_path` — a mutating tool never resolves a component away — but with a message naming the normalised path rather than "Path traversal denied", which would be a lie the caller cannot act on. Reads still resolve `..`.

## File-access tools (non-markdown)
Raw read/write/browse of arbitrary vault files, distinct peers to the note tools (note tools stay markdown-only). Pure byte transport — no server-side PDF/text extraction, no embedding or indexing of non-markdown files.
- `read_file(path, encoding="auto", offset=0, limit=None)` — `auto` resolves text-like MIME → text, image → inline MCP image content block (renders in-client), everything else → base64 string. `text` forces UTF-8 decode (errors on non-UTF-8); `base64` forces raw-bytes base64. Capped by `MAX_FILE_READ_BYTES` (default 10 MB), checked against on-disk size before reading. Text results are additionally bounded by `MAX_READ_RESPONSE_CHARS` and page via `offset`; base64 and image results are not windowed. Base64 reads are token-heavy — check size with `list_files` first.
- `write_file(path, content, encoding="base64", overwrite=False)` — `base64` decodes `content` to raw bytes; `text` writes UTF-8. No-clobber by default (`overwrite=True` to replace), auto-creates parent dirs, atomic via `vault.write_file`. Capped by `MAX_FILE_WRITE_BYTES` (default 25 MB) on decoded length.
- `list_files(folder=".", pattern="*", recursive=False, limit=200)` — `ls`-style: immediate children (subdirs + files) by default, each file with size + mtime; glob-filterable; capped at `limit` with a truncation note.

- `delete_file(path, permanent=False)` — soft-deletes to `.trash/<YYYYMMDD-HHMMSS>-<basename>-<8 hex>` through the anchored helper; `permanent=True` unlinks. Refuses `.md` (pointing at `delete_note`), directories and symlinks. The `.md` refusal runs on the **canonical** final component, so `note.md/.`, `a//note.md` and `NOTE.MD` are refused too — the caller's string is not the path.

`write_file` additionally goes through `validate_mutable_path`, so it refuses a symlinked final component the way the note tools do (see "Mutations act on the path as named" above) — `overwrite=True` cannot clobber a file through an alias. `read_file` and `list_files` still follow links.

All four enforce the same traversal guard and the same dot-dir guard (`is_hidden_path`, rejecting any path component starting with `.` — the indexer's visibility rule, keeping `.obsidian`/`.git`/`.trash`/`.smart-env` out of reach), but **not through the same validator**: `read_file` and `list_files` use `validate_visible_path` (which resolves, so links are followed), `write_file` uses `validate_mutable_path` (parent resolved, symlinked leaf refused), and `delete_file` canonicalises lexically and walks with `O_NOFOLLOW` via `vault_fs`. Vault helpers (`read_bytes`, `write_bytes`, `list_dir`, MIME classification) live in `src/services/vault.py`. MIME detection uses stdlib `mimetypes` plus a magic-byte sniff for PNG/JPEG/GIF/WebP. `read_file` is the first tool returning a non-`str` MCP content object.

## File transfer

No MCP client can hand a tool the bytes of a file the user is looking at, and
the server cannot reach the user's disk. Five tools plus a public route family
close that gap. The whole design is one idea: **a capability pins everything it
may do at mint time, and the route acts on nothing else.**

**Tools** (`src/mcp_server/tools.py`, registered in `server.py`):
- `request_upload(path, overwrite=False, expires_in=None)` — readwrite. Mints a single-use upload capability; returns `upload_id` (opaque `public_id`, never the row id), a `…/transfer/upload#<token>` URL, `expires_at` and `max_bytes`.
- `check_upload(upload_id)` — `pending` | `uploading` | `completed{path,size,sha256,mime,completed_at}` | `expired`, scoped to the exact minting credential *and* user. Another identity's handle is `not found`.
- `request_download(path, expires_in=None)` — read is enough. Multi-use within its TTL; bound to the file's exact bytes at mint.
- `import_from_url(url, path, overwrite=False)` — readwrite. Server-side fetch under the SSRF policy, straight through the same capped, anchored publish.
- `delete_file(path, permanent=False)` — readwrite. See "File-access tools".

**Routes** (`src/transfer/routes.py`, mounted in `src/main.py`): `GET|HEAD /transfer/{upload,download}` (static pages), `GET|HEAD /transfer/{upload,download}/info`, `PUT /transfer/upload`, `GET|HEAD /transfer/download/file`. Rate-limited 30/min for pages and info, 10/min for the two that move bytes. Any other method is a 405.

### Fragment, not query string
The token travels in the URL **fragment**, which browsers never send, so Traefik and Uvicorn access logs see only `/transfer/upload`. It is redeemed *only* from `Authorization: Bearer`; a token in the path or query is ignored, so pasting a link into a URL bar cannot be replayed out of a log. Two operator constraints follow: **Traefik header logging must stay at its default `drop`**, and no APM may capture request headers. `_tracked` allow-lists log `upload_id`, `path`, `expires_in`, `overwrite` and — for imports — the URL *host* only. `check_upload` additionally validates the `upload_id` **shape** (22 URL-safe characters, exactly what `token_urlsafe(16)` produces) before `_tracked` sees it: an agent that pastes the whole `…#<token>` URL or the token itself would otherwise write a live capability into `usage_logs`, so anything off-shape is logged as `<invalid>` and answered `not found` without a lookup.

### Token state machine
`pending → claimed → completed`, with `consumed` as the dead end.
- **`claim_upload` is one committed conditional `UPDATE … RETURNING`, run before a single body byte is read.** Zero rows → the uniform 404. That is what makes single-use linearizable and what stops an unknown token from streaming gigabytes to disk.
- Handled pre-publication failures (413, 409, disconnect, dead identity) **release** the claim: nothing was published, so the human may retry the same link.
- Deadline or idle timeout **consumes** it: the request died mid-stream and a retry should mint afresh.
- A failure *after* publication — `PostPublishFailure` — leaves it **`claimed`**, forever. Never release there: from that state we cannot prove nothing landed, and a replayable token over an already-written path is the worse failure.

### Uniform 404
Unknown, expired, consumed, claimed, revoked credential, downgraded permission, inactive user, reassigned vault root — one status, one body. `_not_found()` is the only way for a bearer-protected endpoint to say no. Precise status comes from the *authenticated* side, via `check_upload`. The uniformity is of the *response*; the branches do different amounts of work and none of this is constant-time, so do not claim timing indistinguishability for it.

### Fingerprint binding — and its two honest limits
An overwrite upload and every download record `{dev, inode, size, mtime_ns, ctime_ns, sha256}` of the target at mint. At publish (or before a download's first byte) the incumbent is `fstat`ed and, when the mint recorded a hash, re-hashed **from the descriptor**. Mismatch → 409 / 404.
- **Optimistic, not linearizable.** `stat` → `replace` is check-then-act; a writer landing in that window is still overwritten. Same guarantee level as `edit_note(expected=…)`, declared rather than implied. The no-clobber path (`overwrite=False`) *is* kernel-linearizable — it is `link()`.
- **Metadata-only above `MAX_FILE_WRITE_BYTES`.** Hashing multi-GB media at mint is not acceptable tool latency, so `sha256` is null there and only the metadata part binds.
- **A null `expected_fingerprint` on an overwrite token is the expected-*absence* sentinel** — the target must still be absent. It never means "skip the check".

### The publish gate
`before_publish()` yields a `GateHandle` (`ok`, `session`, `complete`). Its transaction takes `SELECT … FOR UPDATE` on token → credential → user *in that fixed order* and **holds those locks across the filesystem publish**; `stream_to_vault` calls `gate.complete(result, published=…)` the instant the bytes are in place, so completion and the `usage_logs` row commit with the locks still held. A revocation, downgrade, reassignment or cascade delete needs the same rows, so it either waits for the publisher or beats it — there is no interleaving that publishes under a revoked identity.

**`import_from_url` goes through a gate too** — `transfer.lock_identity_for_publish(session, identity, vault_root=…)`, which locks the *caller's own* credential and user rows (credential → user, the same relative order) and re-checks the database's current root against the root captured when the tool started. It has no token, but it holds a network stream open for up to 30 s, which is ample time for the key to be revoked or repointed; without the gate the bytes would land under whatever the identity looked like when the tool began. Its `complete()` is a no-op: there is no token row to finish, and `_tracked` writes the usage log after the tool returns.

**`stream_to_vault` raises `PostPublishFailure` and nothing else after the bytes land.** That is the contract the upload route leans on to decide between releasing the claim and stranding it: any *other* exception — `ENOSPC` while staging, a database error opening the gate — is demonstrably pre-publication, so the claim is released and the human may retry the same link.

### Anchored filesystem (`src/services/vault_fs.py`)
Every transfer write and `delete_file` walks one component at a time with `O_NOFOLLOW` from an open root descriptor, so a symlink anywhere in the chain raises instead of being followed. Bytes stage in **`<root>/.transfer-tmp/`** and the destination parent is re-walked *inside the gate* — a descriptor opened before a minutes-long stream keeps pointing at the same directory after a rename, and publishing through it would follow the move.

**`soft_delete` is one `renameat2(RENAME_NOREPLACE)` — it never unlinks anything and never pre-creates the destination.** `rename_noreplace()` in `vault_fs.py` reaches the syscall through `ctypes` (the glibc wrapper, present since 2.28; a `syscall()` fallback exists for older glibc). It buys two guarantees at once, and both are load-bearing:

- the trash name is *created or refused* — `EEXIST` means somebody else holds it, and the delete retries under a fresh random suffix rather than overwriting;
- whichever inode is at the source when the call runs is what moves, so a writer that replaced the source ends up in `.trash` intact rather than destroyed.

Two earlier shapes were each wrong at one end. `link`+`unlink` could unlink a *different* inode than it had copied. `O_EXCL` placeholder + `os.rename` fixed that end and not the other — **`os.rename` replaces** — so between reserving the placeholder and renaming onto it, anything that took that pathname over was silently destroyed, and the error path would have unlinked that file too while tidying up "our" placeholder. `RENAME_NOREPLACE` has no reservation window and nothing to clean up. **Do not "simplify" it back to `os.rename`**; a filesystem that cannot do the non-replacing form raises `UnsupportedFilesystem` (`probe_trash` exercises the same primitive at first use, so it fails at startup rather than at the first delete), and there is no safe fallback.

The `lstat` symlink/directory refusal still runs first. A **symlink** swapped in afterwards is deliberately not re-checked — it is moved into `.trash` intact, never followed — but a **directory** is: the moved name is `lstat`ed in the trash and, if it is one, put back with a second `RENAME_NOREPLACE` and the delete refused, because a subtree carries files nobody asked to delete. If the rollback loses the source name the directory stays in `.trash` and the error names that location.

**Publication is recorded the instant `publish()` returns, before any cleanup.** In `transfer.py`, `_publish_into_current_parent` invokes an `on_published` callback before it closes anything, and every close on the publish path goes through `_close_quietly`. The reason is narrow and expensive: a bare `os.close` raising `EIO` after the bytes landed discarded the `Published` outcome and surfaced as a generic `OSError` — which the upload route reads as "demonstrably pre-publication" and answers by **releasing the claim**, handing back a replayable token over a path that already holds the file, while `import_from_url` reported "could not write" for a file it had written. `PostPublishFailure` is the only exception `stream_to_vault` may raise once the bytes are in place; anything you add between `publish` and the return must preserve that.

**The probes are split by capability and run only where that capability is used.** `check_publication_support(root)` links root→root (uploads, imports, `PUT /transfer/upload`); `check_trash_support(root)` renames a temp file into `.trash` (only a `delete_file` soft delete — `permanent=True` probes nothing). Both cache per root; the tools and routes surface `UnsupportedFilesystem` as a stable error. **No read path probes at all** — a probe writes, and `request_download`/`check_upload`/the download route must not create files on a read-only capability. The first publication probe per root also prunes `.transfer-tmp/.tmp-*` files older than 24 h, which is where a crashed upload's staged bytes would otherwise sit forever.

**Follow-ups:** `vault._atomic_write` should adopt this helper (it is written to make that easy), and `usage_logs.key_id` still has no `ON DELETE` — a usage-log row written by an upload blocks its key's delete, exactly as before this change. Both are pre-existing, neither is regressed here.

### Path canonicalisation — do not "simplify" this
`validate_visible_path` runs (it is the shared traversal and dot-dir guard, and it is what refuses a link pointing out of the vault) but its **return value is the resolved path, and resolving follows symlinks**. The vault-relative path a transfer acts on is normalised *lexically* in `tools._vault_context`. Taking it from the resolved result silently retargets the operation: `delete_file("Attachments/alias.png")` where `alias.png` links to `secret.png` resolved to `secret.png` and deleted **that**, reporting success for a path nobody named. Keeping the caller's own components means the anchored walk is what meets the symlink — and refuses it.

### SSRF policy for `import_from_url`
The host is folded to canonical ASCII **first** (NFKC, the alternative full stops `。．｡`, then `idna.encode(uts46=True)`) and every check runs on that form — checking before normalising let `svc.prod。internal` past a suffix check and then resolved it as `svc.prod.internal`. Then: https only (`IMPORT_ALLOW_HTTP` for http), no userinfo, no zone ids, no single-label or `.localhost`/`.local`/`.internal`/`.home.arpa` names, no ambiguous numeric hosts, scheme-paired ports (443/8443, 80/8080). Every resolved address must pass an **explicit deny list** — loopback, RFC 1918, ULA, link-local, CGNAT, `0.0.0.0/8`, `240/4`, `198.18/15`, `192.0.0.0/24`, documentation, multicast, unspecified, reserved, IPv4-mapped/compat, NAT64, 6to4, Teredo (embedded IPv4 extracted and re-checked) — *and then* `is_global`. `is_global` alone is not enough; it admits IPv6 multicast and the NAT64 prefix. The connection is pinned to the validated address with `Host` and SNI kept as the original name, a new client per hop, `trust_env=False`, `http2=False`, ≤ 5 manual redirects with every rule re-applied, and one 30 s deadline over the whole thing.

### Declared filesystem semantics
Case-sensitive, non-normalising filesystems (ext4/xfs — the production bind mount) on Linux. Hard links must work within the root, and `.trash` must accept a same-device `renameat2(RENAME_NOREPLACE)`; the probes refuse otherwise rather than degrading to an overwriting move. Case-insensitive or normalising mounts are out of scope, as is any platform without `renameat2` — the soft delete has no portable fallback.

## Three kinds of size cap — don't confuse them

There are **byte** caps, a **character** cap, and a **transport** cap, and they protect different things:

- `MAX_FILE_READ_BYTES` / `MAX_FILE_WRITE_BYTES` bound what the **server** reads into or writes out of memory. They refuse the operation.
- `MAX_READ_RESPONSE_CHARS` (default 40,000 ≈ 10K tokens) bounds what `read_note` / `read_file` **return to the caller**, whose context the result consumes. It truncates rather than refusing.
- The MCP streamable-HTTP **request body limit** bounds what the transport accepts at all, before any tool runs. It is derived, not configured: `max(2 × MAX_FILE_WRITE_BYTES, 6 × MAX_NOTE_BYTES) + 1 MiB` (61 MiB with the defaults), passed to `FastMCP(max_request_body_size=)` from `Settings.mcp_max_request_body_bytes`. The SDK's own default is 4 MiB, which would silently reject writes far below our documented 25 MB cap. The formula guarantees — for a *canonical* envelope, i.e. JSON-RPC framing plus non-content arguments under 1 MiB — that a base64 `write_file` at the cap (base64 is `4·⌈n/3⌉ ≤ 2n + 2`) and any note write up to `MAX_NOTE_BYTES` (JSON escaping expands a byte at most 6×) always reach the tool, which then decides. Unsupported shapes are bounded by the transport with a bare HTTP 413 and no tool error: text-mode `write_file` whose escaping exceeds the limit (send base64 — always safe), an envelope over 1 MiB, and arguments that are large but discarded.

**Every note write tool caps its own result.** `create_note`, `edit_note` (all modes, `dry_run` included), and `set_frontmatter` refuse a result over `MAX_NOTE_BYTES` with a tool-level error and no write, via `_note_size_error()` in `tools.py`. That is what keeps the tool, not the transport, in charge of every supported write — the transport limit sits deliberately above every tool cap. `MAX_NOTE_BYTES` lives in `src/config.py` (not `tools.py`) because the transport formula needs it.

**Precisely: it is a per-component budget, not a single ceiling on the whole response.** The content window is bounded by the cap, and the heading outline is *independently* bounded by the cap. A truncated whole-note read can therefore carry both, so the worst-case response is about `2 × cap` plus the fixed notice prose — not `cap`. Every component must have a budget; if you add a third, give it one, and update the worst case here and in the end-to-end test.

Satisfying the byte caps says nothing about the response. A 3 MB note is well inside the 10 MB read cap and will still exhaust a context window — that is exactly how this bit us: `read_note` had no cap at all and returned a 3.4 M-char tool result, which the caller's inference provider rejected as "input exceeds the context window". `read_note` goes through `read_file()` in the vault service, not `read_bytes()`, so it never even had the byte cap.

**If you add a tool that returns file or note content, it needs the character cap too.** `_window()` and `_capped_text()` in `tools.py` are the shared helpers.

Over-cap reads return the first window, a `[TRUNCATED]` notice with the exact continuing `offset`, and — for a whole-note read — a section outline. `limit` may lower the cap for one call but never raise it; raising is an operator decision via the env var.

## Section addressing

`read_note(section=…)` and `edit_note(section=…)` share one resolver in `src/services/vault.py`, so a selector names the same section for both. Three forms:

1. Ordinal — `"#7"`, the 7th ATX heading in document order, 1-based. Checked **first**.
2. Path-style chain — `"Parent/Child"`, ancestors outermost-first. A selector containing `/` never takes the ordinal branch.
3. Exact heading text — `"Tasks"`.

The ordinal exists because **path-style cannot disambiguate duplicate siblings**: two `## Report.xlsx` under the same parent share every ancestor, so no chain separates them. Bulk-extraction notes are full of these.

**A bare `#N` always wins over a heading literally titled `#N` — don't "fix" this by preferring text.** The outline we emit on truncation advertises ordinals as the reliable selector; if note content could shadow one, the section we just told the caller to fetch by `#2` would be unreachable by `#2`. Text-first was the original implementation and pre-merge review caught it. The literal heading loses nothing: it stays reachable by the path form (`Parent/#2`) and by its own ordinal, so under ordinal-first every section is addressable, which is not true the other way round.

Ambiguity stays an error that names the resolving ordinals; it never silently picks the first match (that is how an agent edits the wrong section and reports success).

Helpers: `_resolve_section_index` (selector → index), `_section_body_span` (index → body span), `extract_section` (heading line **plus** body, for reads), `replace_section` (body only, for writes), `outline_sections` (depth/text/size/ordinal per section).
