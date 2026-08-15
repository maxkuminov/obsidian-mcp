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
- `src/models/db.py` — ORM models (api_keys, usage_logs, notes_metadata, note_embeddings, note_links)
- `src/mcp_server/` — MCP server, tools, auth middleware
- `src/services/` — vault ops, search, embeddings, indexer
- `src/api/` — control panel REST endpoints
- `src/control_panel/` — Jinja2 templates + static assets
- `alembic/` — database migrations

## Infrastructure
- Container: `obsidian-mcp`, listens on `:8000`
- Traefik routes: hostname driven by `MCP_HOSTNAME` in `.env`
  - Panel routes: OAuth protected via `chain-oauth@file`
  - MCP routes (`/mcp/*`): API key auth at app level
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
| Deploy | `make deploy` (build, backup, migrate, deploy) |

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

## Key Decisions
- API keys use `omcp_` prefix, stored as SHA-256 hashes
- Panel/OAuth passwords: direct `bcrypt` (`$2b$`, cost 12); both hash and verify truncate the UTF-8 encoding at 72 bytes and reject NUL bytes — passlib's historical semantics — so existing hashes stay valid; don't "fix" the truncation.
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
- Vector search via pgvector HNSW index on `note_embeddings.embedding`
  (`vector_cosine_ops`, `m=16, ef_construction=64`); `semantic_search`
  sets `hnsw.ef_search=80` per query and dedupes per note in Python
  after a 5x overfetch
- Indexer runs on startup then every 5 minutes, hash-based change detection
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

## File-access tools (non-markdown)
Raw read/write/browse of arbitrary vault files, distinct peers to the note tools (note tools stay markdown-only). Pure byte transport — no server-side PDF/text extraction, no embedding or indexing of non-markdown files.
- `read_file(path, encoding="auto", offset=0, limit=None)` — `auto` resolves text-like MIME → text, image → inline MCP image content block (renders in-client), everything else → base64 string. `text` forces UTF-8 decode (errors on non-UTF-8); `base64` forces raw-bytes base64. Capped by `MAX_FILE_READ_BYTES` (default 10 MB), checked against on-disk size before reading. Text results are additionally bounded by `MAX_READ_RESPONSE_CHARS` and page via `offset`; base64 and image results are not windowed. Base64 reads are token-heavy — check size with `list_files` first.
- `write_file(path, content, encoding="base64", overwrite=False)` — `base64` decodes `content` to raw bytes; `text` writes UTF-8. No-clobber by default (`overwrite=True` to replace), auto-creates parent dirs, atomic via `vault.write_file`. Capped by `MAX_FILE_WRITE_BYTES` (default 25 MB) on decoded length.
- `list_files(folder=".", pattern="*", recursive=False, limit=200)` — `ls`-style: immediate children (subdirs + files) by default, each file with size + mtime; glob-filterable; capped at `limit` with a truncation note.

All three reuse `validate_path` (traversal guard) and a shared dot-dir guard (`is_hidden_path`) that rejects any path component starting with `.` — same visibility rule as the indexer, keeping `.obsidian`/`.git`/`.trash`/`.smart-env` out of reach. Vault helpers (`read_bytes`, `write_bytes`, `list_dir`, MIME classification) live in `src/services/vault.py`. MIME detection uses stdlib `mimetypes` plus a magic-byte sniff for PNG/JPEG/GIF/WebP. `read_file` is the first tool returning a non-`str` MCP content object.

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
