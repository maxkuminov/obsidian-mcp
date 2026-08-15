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

- `delete_file(path, permanent=False)` — soft-deletes to `.trash/<YYYYMMDD-HHMMSS>-<basename>-<8 hex>` through the anchored helper; `permanent=True` unlinks. Refuses `.md` (pointing at `delete_note`), directories and symlinks. The `.md` refusal runs on the **canonical** final component, so `note.md/.`, `a//note.md` and `NOTE.MD` are refused too — the caller's string is not the path.

All four reuse `validate_path` (traversal guard) and a shared dot-dir guard (`is_hidden_path`) that rejects any path component starting with `.` — same visibility rule as the indexer, keeping `.obsidian`/`.git`/`.trash`/`.smart-env` out of reach. Vault helpers (`read_bytes`, `write_bytes`, `list_dir`, MIME classification) live in `src/services/vault.py`. MIME detection uses stdlib `mimetypes` plus a magic-byte sniff for PNG/JPEG/GIF/WebP. `read_file` is the first tool returning a non-`str` MCP content object.

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
