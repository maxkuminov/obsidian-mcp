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
- Jinja2 control panel. htmx and Chart.js are **vendored** under
  `src/control_panel/static/vendor/` and served from `/admin/static` — no CDN,
  no `integrity` to keep in sync (#130). There is no Tailwind; the styling is
  hand-written CSS in `base.html`. Google Fonts is the one remaining remote
  origin and ships no executable code. **No CSP on the panel**, deliberately:
  every template drives its controls with inline `onclick`/`onsubmit` and htmx
  attributes, so the only policy they survive is one carrying `unsafe-inline`
  for scripts — which permits exactly the injection a CSP is bought to stop.

## Project Layout
- `src/main.py` — FastAPI app, lifespan, MCP mount
- `src/config.py` — pydantic-settings
- `src/database.py` — async SQLAlchemy engine/session
- `src/models/db.py` — ORM models (api_keys, usage_logs, notes_metadata, note_embeddings, note_links, transfer_tokens, oauth_clients/codes/tokens)
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
  later revision would otherwise inherit them. 014 does the same for the same
  reason.
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

The same module also gates **014**'s backfill, which `alembic check` is blind to
in a different way: it sees the column, its NOT NULL and its index, and nothing
about *which rows got which grant*. So the 014 cases assert the grouping
directly — one family per `(client_id, user_id)`, never a family spanning two
users, never a user's rows split across two families — plus stamp-back
idempotence (which must not re-stamp existing `grant_id`s) and the downgrade.

## OAuth grant families

A `/authorize` approval is **one grant**; the token endpoint mints an
access/refresh pair from it and every rotation mints another pair.
`oauth_tokens.grant_id` (migration 014, NOT NULL, indexed) is what ties them
together, and it is the *only* way a family is ever resolved — the decision in
#64 was explicit that a second "find the family" path is how the bug comes
back. `src/oauth/grants.py` owns the primitives.

Why it exists: without it the panel could only offer per-row controls, and both
were near no-ops. Revoking the access row left the refresh token to mint a
fresh, identically-scoped pair on the client's next 401 retry (access tokens
live one hour). Downgrading the access row silently reverted, because
`_handle_refresh` copies the **refresh** token's scope. The revoked row then
vanished from the page, so the operator saw a blank space that read as success.

- **Invariant: one `grant_id` ⇒ one `(client_id, user_id)`.** Established at
  every write site and by 014's group key. Family writes therefore do **not**
  re-filter by `user_id` — under a broken invariant that predicate would turn a
  complete revocation into a partial one, which is the failure the whole thing
  exists to remove. `_assert_oauth_token_owner` still guards the token the
  operator names, and because a family cannot span users that covers it.
- **`lock_grant` is correctness, not tuning.** Under READ COMMITTED an
  `UPDATE … WHERE grant_id = :g` takes its snapshot at statement start, so rows
  a concurrent `_handle_refresh` *inserts* afterwards are invisible to it: the
  panel reports "revoked" and the client keeps the pair it just rotated into.
  Row locks cannot close that — the rows do not exist yet. Both sides take
  `pg_advisory_xact_lock` on the grant **before** touching any family row, so
  the order is total and nothing deadlocks; `_handle_refresh` does one unlocked
  `grant_id` lookup first because it cannot lock what it has not identified.
- **Revocation kills in-flight access tokens; rotation does not.** Letting the
  replaced access token run to its expiry is right for rotation and wrong for
  revocation — an hour of surviving write access after the operator clicked
  Revoke is exactly the defect.
- **Revocation takes effect at the next authenticated request; a request
  already in flight completes.** `APIKeyMiddleware` resolves the token once, at
  the start of the request, so a tool call authenticated microseconds before a
  revoke or downgrade commits still runs with the permission it was granted.
  Closing that would mean holding the grant lock across tool execution —
  arbitrary vault I/O, embedding calls, network fetches — trading a bounded,
  sub-second staleness for unbounded lock contention on every request. Accepted
  and documented, at the same optimistic level as `edit_note(expected=…)` and
  the transfer fingerprint check.
- **`/revoke` (RFC 7009) is family-scoped too**, which §2.1 explicitly permits.
  Anything narrower reproduces the near no-op for any client presenting its
  access token. It **authenticates the client** per its registered method and
  requires `client_id` to be **present and exactly equal** to the token's —
  without that, any holder of any token value ended a 30-day grant. Absence is
  not a match: a public client has no secret to check, so "omit `client_id`"
  would be a universal bypass proving nothing, and unlike `/token` there is no
  PKCE verifier binding the request to the initiating client. §2.2 governs the
  other direction: a foreign, unnamed or unknown token is answered 200 with
  nothing done, so the endpoint is not an oracle for who owns a token value.
  The only real error is naming the right client and failing to authenticate.
- **No token is minted without an owner, and the mint paths serialize with the
  multi-user bootstrap.** `register_submit` claims ownerless rows with
  `UPDATE ... WHERE user_id IS NULL`, whose snapshot is taken at statement
  start, so a mint committing afterwards left tokens belonging to nobody. Both
  token handlers take the *same* advisory key the bootstrap already held
  (`USER_BOOTSTRAP_LOCK_KEY` — a wire constant: changing it un-serializes the
  window during a rolling deploy) and, under `multi_user_mode`, refuse to mint
  a NULL-owner token at all. Lock order is bootstrap-then-grant on the only
  path that takes both; the panel takes the grant lock and never the bootstrap
  key, so there is no cycle.
- **The first-authorizer claim is `UPDATE ... WHERE user_id IS NULL
  RETURNING`**, not an ORM assignment on a row read from this transaction's
  snapshot — two users consenting to the same unbound client both saw NULL and
  the second write silently re-bound it. `_handle_refresh` and
  `src/mcp_server/auth.py` additionally refuse a grant whose owner is not the
  client's, so a legacy or race-created cross-user grant cannot rotate or
  authenticate.
- **The registered scope caps every path.** `src/oauth/scope.py` holds the one
  definition (`clamp_scope`, `client_can_write`, `token_has_write`); the OAuth
  routes, `src/mcp_server/auth.py` and the panel all use it. The panel refuses
  `readwrite` for a client not registered for it *and* clamps what it writes,
  and `_handle_refresh` re-clamps on every rotation — otherwise a scope raised
  above the registration survives forever (#67).
- **`authorize_post` refuses a client bound to a different user** (#68). Fixed
  at the source rather than by unioning the panel listing, which would hand the
  other user the owner's cascading Delete. Single-user mode cannot trigger it.
- **The panel lists revoked and expired rows, dimmed**, with one "Revoke
  access" control and one scope select *per grant*. Status also reads the
  owner's `User.is_active` ("Owner inactive") **and `has_vault_scope`** ("No
  vault scope"), both of which `APIKeyMiddleware` already enforces and the page
  used to badge green (#76). A no-vault-scope grant counts as dead: offering a
  Revoke and a scope select for a credential the middleware 401s is the same
  over-reporting of liveness, and that select could only ever write a scope the
  client is not registered for.
- **Live rows are queried unbounded; only history is capped.** One `LIMIT` over
  all of a client's tokens applied *before* grants were identified let a chatty
  grant's rotations push another grant's live refresh token off the page —
  a working credential with no control to revoke it. Losing the tail of the
  history costs a row nobody can act on; losing a live row costs a revocation.
  When the history query hits its cap the page says so rather than printing a
  total it did not count.
- **A grant's permission is `any(token_has_write(...))` over its live rows.**
  014's backfill can legitimately merge two pre-014 sessions of one client and
  user, one `read` and one `readwrite`; reading the newest row alone showed
  "read" while an older live access token still held write. Such a family is
  marked "mixed", and saving the select writes one clamped scope across all of
  it — which is what makes it uniform again. `offline_access` is read the same
  way, so a write cannot strip the marker from a sibling that carried it.
- **014 verifies a pre-existing `grant_id` column rather than patching it.**
  The backfill is a partition only because the migration created the column;
  on a column somebody else added, `WHERE grant_id IS NULL` becomes a patch
  that hands a NULL row beside a stamped sibling a *fresh* id — splitting one
  grant in two, so revoking either leaves the other alive. It therefore refuses
  a wrong type, any NULL row, any id spanning more than one
  `(client_id, user_id)`, and an index of its name that is not *exactly* its
  index. That last check reads `pg_index` — table, `indisvalid`,
  `indpred IS NULL`, `indexprs IS NULL`, and exactly one key column equal to
  `grant_id`'s attnum. "Which column is it on?" is not enough: a partial index
  covers a subset of rows, an expression index cannot serve an equality lookup,
  a multi-column index leads with the wrong key, and an INVALID leftover from a
  failed `CREATE INDEX CONCURRENTLY` serves nothing — `CREATE INDEX IF NOT
  EXISTS` keeps all of them, and autogenerate compares index *names*, so the
  check would look installed while staying dirty forever.
- **`cleanup_expired_tokens` retains on `expires_at`, never `created_at`.** Its
  revoked branch used to have no age condition at all, so the indexer deleted
  every revoked token within five minutes — the same blank space the listing
  exists to prevent, just delayed. Revocation time is not stored, but a token
  can only be revoked while it exists (`R <= expires_at`), so a 7-day window
  measured from `expires_at` *guarantees* a revoked row stays visible for at
  least 7 days after revocation. `created_at` inverts that: a refresh token
  minted 30 days ago and revoked a minute ago would be purged at once. Once
  age-gated the revoked branch is a strict subset of the expiry branch, which
  is why the predicate is a single comparison rather than an `or_`.

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
- **Each embed pass ends with an exclusion reconciliation sweep** (#127). The
  backlog selects on `embedded_content_hash IS NULL OR != content_hash`, so it
  is driven entirely by *content* changes and an `EMBEDDING_EXCLUDE_PATTERNS`
  edit reached nothing: adding a pattern left an excluded note answering
  `semantic_search` for ever, removing one left a now-included note stamped
  with zero vectors, hash-equal, never re-selected, silently absent. The sweep
  therefore reads the rows the backlog *cannot* see — certification-current
  (`embedded_content_hash IS NOT DISTINCT FROM content_hash`, owner-scoped) —
  and writes only where the config and the stored vectors disagree. **Every
  write goes through `certify_embedded`'s `id + content_hash + file_path`
  predicate, stamp before delete, per-note commit — never a delete by id**: a
  move changes `file_path` with an unchanged `content_hash`, so a decision
  about an excluded path would otherwise delete the vectors of a row that is
  now included and record it embedded with none. Convergence is defined for a
  *completed* sweep, with three declared exceptions — zero-chunk notes (already
  correct, and deliberately never rewritten, which is the one file read the
  sweep repeats per pass), bytes that no longer hash to the row (the backlog
  owns it next pass), and a failed provider call (left unstamped). A pause
  stops it between notes; the next pass runs a fresh, idempotent sweep.
- **Both move paths recompute the stem-derived `title`** (#127). It falls back
  to the filename stem, so a rename left `Alpha` on a note called `Beta.md`
  for ever — a move changes no content, so the scan never revisits the row.
  The indexer's id-preserving branch binds the title from the entry it already
  parsed for the new path; `move_note` reads the moved file through the
  destination target's descriptor after `_verify_the_moved_inode`, parses it
  with `parse_frontmatter` and derives through the same `_note_title`. **Not a
  SQL `CASE` over the stored JSONB**: that disagreed with `_note_title` on
  every falsy title (`false`, `0`, `[]`, `{}`, `""` all fall back to the stem)
  and trusted a copy that can be older than the file. The JSONB derivation
  survives only as the read/parse-failure fallback, declared best-effort.
- **The keyword vector attempts the full note and retreats per note** (#127).
  Both writers bound `content[:100000]`, so every term past that was invisible
  to `keyword_search` on a note the tool still reported. `write_tsvector_bounded`
  attempts the whole body, halving down to a floor of **exactly** 100,000
  characters — today's statement — with each attempt in its own savepoint and
  the `try` **outside** `async with session.begin_nested()`, so the error
  unwinds through the context manager's rollback and leaves the outer
  transaction usable. A floor failure propagates, exactly as before: the
  incremental pass aborts with nothing committed and retries next tick, and
  `rebuild_tsvectors` is now **atomic** — its every-500 intermediate commits
  are gone, so a floor failure rolls the whole rebuild back instead of leaving
  a keyword index half-built under two FTS configs that no periodic pass would
  repair. Verified against a real PostgreSQL
  (`tests/integration/test_tsvector_bounded_pg.py`); a mocked savepoint cannot
  show the driver's aborted-transaction state clearing.
- **The rebuild certifies what it writes, and the reason is that nothing else
  would ever repair it.** It snapshots the table once and then reads the vault
  note by note, while a keyword vector is rewritten again only when a note's
  `content_hash` changes — both move paths preserve `content_tsvector` and the
  scan skips an unchanged hash. So a row it steps over, or writes stale bytes
  into, stays on the *previous* `FTS_CONFIGS` for ever: `'running'` stored as
  the english stem `run`, never matching a `simple` query. Two shapes did
  exactly that — a note moved after the snapshot failed its old-path read and
  was a silent `continue`, and an UPDATE by `id` alone overwrote a concurrent
  pass's `tsvector(C2)` with `tsvector(C1)` while the hash stayed `C2`, so every
  later scan skipped it. The snapshot now retains owner, path and hash, the
  bytes are verified against that hash, and the UPDATE names all four and
  requires exactly one row. A zero-row update, a read failure or a hash mismatch
  is **never** routed through the halving retreat (that addresses size, not
  staleness): it re-reads the current owner-scoped row — gone → safely absent,
  moved or advanced → retried against the fresh values within
  `MAX_REBUILD_REREADS`, and still recording the same path and hash →
  `TsvectorRebuildAborted`, rolling the single transaction back rather than
  committing around it.
- **`OllamaProvider.embed_batch` has no aggregate deadline** (#127); the 30 s
  per-call `wait_for` is the only liveness bound. The old fixed 300 s
  whole-batch budget could fire only when every chunk was individually healthy
  — i.e. exactly on a note with more chunks than 300 s of normal latency
  covers, which then never certified and was re-selected every tick: a
  permanent 300 s burn under `index_pass_lock` that could never finish. A
  *proportional* replacement re-introduces the same boundary one size class up
  and was rejected. `OpenAIProvider` is untouched. The cost is a giant note
  holding the pass for 30 s × chunks once; the pause is honoured at the next
  note boundary, as always. `embed_note` still refuses to certify partial chunk
  coverage.
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
  actions take it too.** `reset_embeddings` and `trigger_reembed` take
  `_pause_indexer()` — a **depth counter** whose first holder sets
  `indexer_paused` and whose last one clears it, because a bare
  `indexer_paused = False` in each handler's `finally` had the first of two
  overlapping actions unpause the indexer underneath the second, and the
  progress endpoint then reported "not paused" about a pause still in force
  (#130) — then `await session.close()` on the request's own session
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
- **Zero-row exact fallback, on *every* zero-row result.** An empty result from
  an approximate filtered scan is ambiguous. Both paths re-run the identical
  statement after `SET LOCAL enable_indexscan = off` (pgvector's documented
  exact search) and use those rows, recording `exact_fallback: true` in
  `usage_logs.params`. This is what makes "empty only when nothing matches" a
  construction rather than a benchmark hope. Eligibility is **unconditional**
  since #127: it used to require a `folder`/`tags`/`frontmatter`/named-user
  predicate, on the reasoning that an unfiltered scan cannot lose candidates to
  a post-filter — and the owner mapping went total, so there is no unfiltered
  query left. The ownerless one (`user_id IS NULL` against a database whose
  vectors mostly belong to a named user) is exactly the shape where the HNSW
  window fills with candidates the predicate discards, and under the old
  condition it returned empty while NULL-owned matches sat in the table. Still
  O(n), still the rare path — it fires only on a genuinely empty result.
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

## Usage attribution is denormalised, because the credential can be deleted

`usage_logs` carries `actor_kind` (`api_key` | `oauth`), `actor_label` (the
key's name or the OAuth `client_name`) and `actor_ref` (the key's `omcp_`
prefix or the `client_id`) — migration 015, all nullable, all written at call
time. `/admin/usage` renders them and keeps its LEFT JOINs only as the
fallback for rows written before 015.

The join alone was the bug (#77). Both FK columns are allowed to lose their
target while the log row stays, and both do so on the operator's most urgent
path: `usage_logs.oauth_token_id` is `ON DELETE SET NULL` and
`oauth_tokens.client_id` is `ON DELETE CASCADE`, so deleting an OAuth client
unattributed every line it had produced; `usage_logs.key_id` has **no
`ON DELETE` at all**, so the panel `UPDATE usage_logs SET key_id = NULL` before
deleting a key, with the same effect. An operator who stops a suspect
credential and then opens the Usage page to see what it did was shown
"unknown" for exactly the rows they came to read.

- **The label is bound by `APIKeyMiddleware`, not looked up by `_log_usage`.**
  `current_actor` (a ContextVar beside `current_user_id` / `current_vault_root`
  in `src/auth/session.py`) is set from the credential row the middleware has
  already loaded, and it is read *before* the tool runs rather than seconds
  later when the credential may be gone. The API-key branch has the `APIKey`;
  the OAuth branch gets `client_name` from **the token lookup itself**, which
  `outerjoin`s `oauth_clients` and returns `(token, client_owner,
  client_name)` — one statement feeding the cross-user check and the label, so
  an ownerless OAuth request still issues exactly one query, as it did before.
  Do not add a second `oauth_clients` select; that is a round trip on the
  hottest path in the server. `_actor_columns()` returns `{}` when the
  ContextVar is unset, so a writer outside a request keeps the pre-015 row
  shape.
- **A dangling FK must not take the row down with it.** A tool call can outlive
  its own credential — the operator deletes the key or the client while a slow
  call is running — and the insert then names a row that is gone. `_log_usage`
  catches **only** `foreign_key_violation` (SQLSTATE 23503), rolls back and
  retries **once** with `key_id`/`oauth_token_id` cleared and `actor_*` kept;
  that is the same end state the panel's own key delete produces, so the reader
  already handles it. `user_id` is dropped only when it is the constraint that
  failed, because the panel scopes a non-admin's page by `user_id`. The error
  arrives wrapped twice and the layers carry different things: SQLAlchemy's
  `.orig` is the asyncpg *dialect's* error (SQLSTATE, no constraint name),
  whose `__cause__` is asyncpg's own (constraint name). `_error_chain` walks
  both and falls back to the message text — reading `orig.constraint_name`
  alone finds nothing and silently degrades every recovery to "assume it was
  `user_id`". An unresolvable name deliberately clears all three: losing the
  scoping beats losing the row. The broad `except` stays last: usage logging
  must never fail a call that already did its work.
- **It is a snapshot, not a view.** 015's backfill is guarded on
  `actor_kind IS NULL` and so is any re-run. Re-deriving the label from the
  credential's present state would rewrite history on every rename. A row
  carrying a label beside a NULL `actor_kind` is therefore an *error*, not
  something to fix up: the guard would relabel it from whatever credential it
  points at now, overwriting an attribution 015 did not write.
- **The three columns are one owned unit, and the COMMENT marker is what owns
  them.** 015 creates all three and stamps each with
  `denormalised actor, written at call time (015_usage_log_actor)`; on a
  re-run it completes only a set that is all present, exactly typed, nullable,
  default-free **and marked**, and refuses anything else (a partial set, a
  `NOT NULL` column, a foreign one) naming what it found. `downgrade()` drops
  only marked columns, all-or-nothing. Type and width are a coincidence anyone
  could reproduce; the marker is the only evidence that *this* scheme wrote the
  values, which is the whole basis for showing them to an operator as an audit
  trail. The same string is declared on the model columns
  (`UsageLog._ACTOR_COLUMN_MARKER`) so `alembic check` compares it — keep the
  two byte-identical or the check goes dirty.
- **Nothing is invented.** 015 labels a row from the credential its own FK
  points at, or leaves it NULL — no guess-by-`user_id`, because two of a
  user's keys are different actors. A NULL row renders
  "unknown (credential deleted)", which is a gap in the audit trail rather
  than a gap in the data, and says so.
- **The OAuth Delete was not weakened to protect the log.** Replacing it with
  a per-token revoke is a *worse* stop — per #64 a client whose row survives
  refreshes its way back — so the delete keeps all four cascades
  (`oauth_tokens`, `oauth_codes`, `transfer_tokens`, and the `SET NULL` on
  `usage_logs.oauth_token_id`) and the confirm text changed instead: it now
  states that the tokens are deleted, that transfer links minted under them
  stop working, and that the usage history stays attributed. **Do not
  interpolate `client_name` into that `confirm()`** — Jinja escapes an
  apostrophe to `&#39;`, the HTML parser restores it before the JS string is
  parsed, and the `onclick` throws, which submits the form *unconfirmed*.
- **`usage_logs.key_id` still has no `ON DELETE`, deliberately.** That is
  unchanged here and the panel still NULLs it by hand; the whole point is that
  the label survives it, which
  `tests/integration/test_schema_check.py::test_the_label_survives_the_panel_deleting_an_api_key`
  runs as the real two-statement sequence.
- **Transfer rows carry the actor from mint** (migration 017, the 015 register:
  marker-owned nullable columns on `transfer_tokens`, snapshot never re-derived,
  orphan-label refusal before any backfill). `mint_token` splices in the actor
  `APIKeyMiddleware` already bound — one shared reader,
  `src.auth.session.actor_columns`, so mint and `_log_usage` cannot drift in
  truncation — and `_log_row` copies it at redemption. The backfill labels
  `transfer_tokens` only, from the row's own FK; it writes nothing to
  `usage_logs`, because no usage row references the token that produced it.
  The honest gap is rows written between 015 and 017: they keep join-only
  attribution and render as unattributable when the joins miss. The label
  authorises nothing — redemption still resolves the credential row.
- **A transfer token names at most one minting credential**, and since 017 the
  database says so: `ck_transfer_tokens_one_credential`
  (`key_id IS NULL OR oauth_token_id IS NULL`), created and marked by 017 and
  resolved through `pg_constraint` — a same-named `CHECK (true)` would satisfy
  a lookup by name. Both NULL stays legal; that is the single-user and sandbox
  shape. It exists because nothing in a two-credential row records *which* of
  them minted it, so the API-key backfill would have labelled such a row purely
  by running first and the OAuth statement's `actor_kind IS NULL` guard would
  then have skipped the row it had just mislabelled — an invented attribution,
  rendered to an operator as an audit trail. 017 refuses such rows by id before
  either backfill, and `transfer.Identity.__post_init__` refuses the same state
  in the app. Unreachable today (`APIKeyMiddleware` clears both ContextVars and
  fills one branch), which is why it is asserted rather than assumed.
- **The deploy window is declared, not closed.** The deploy migrates and *then*
  recreates, so a mint served by the old code between 017's backfill and the
  recreate inserts a row with all three actor columns NULL. That is the tail of
  the same 015→017 gap: it renders through the join fallback like every pre-017
  row and degrades only if the credential is later deleted. Symmetric with
  016's treatment (NULL record ⇒ re-derive; NULL label ⇒ join fallback). No
  barrier and no quiesce: the window is seconds long and the fallback works.

## Publication confirms the vault root, and the residual is declared

`APIKeyMiddleware` binds `current_vault_root` once, at admission, and that
snapshot is immutable by design — it is what makes #66's gate fail closed under
a concurrent bulk cache warm. The cost is that it is *stale by design* for the
whole of a request: an administrator can reassign, the panel can report it
complete, and a write already in flight still publishes into the former root.
So every mutating tool re-reads `users.vault_path` / `is_active` immediately
before **each** publication and refuses on change (#88). The answer is
deliberately not a lock: holding the credential and user rows `FOR UPDATE`
across `move_note`'s link rewrites would put arbitrary vault I/O inside a lock
every authenticated request contends for. The transfer routes keep their
stronger locked gate; `import_from_url` and `request_upload` are the two
allow-listed exemptions.

- **The residual is stated, not implied.** The window shrinks from a whole
  request to staging, the durability flush and one publishing call. A
  reassignment committing inside *that* still lands in the former root and the
  tool reports success — the same optimistic level as `edit_note(expected=…)`
  and the transfer fingerprint check. `move_note(rewrite_links=True)` has
  several such windows, one per publication, and can be refused part way
  through; "one window per tool call" would be false for it and must not be
  claimed.
- **There is no retainable confirmation.** `vault._confirm_vault_assignment`
  is private and the only entry point is `vault.confirmed_publication(user_id,
  publish)`, which awaits the read and calls a **synchronous** `publish` before
  returning control — so no caller-visible `await` can sit between the two.
  Coroutine, generator *and* async-generator callbacks are refused, and so is a
  returned coroutine/generator/awaitable (a callable object whose `__call__` is
  a generator is none of the first three). Nothing is `close()`d on the way
  out: that is arbitrary code of a stranger's choosing, and the lease below has
  already made the object inert.
- **The confirmation is leased for the callback's dynamic extent, and that is
  the part that bounds *when*.** `_leased` activates it, and a `finally`
  revokes it on every exit — normal return, exception, or a callback that
  stashed the object. `consume` refuses an unleased confirmation, and
  `confirmed_publication` refuses a callback that returned without consuming
  one. Single-consumption alone was **not** enough and must not be relied on
  again: it bounds how many times a confirmation is used and says nothing about
  when, so `lambda c: saved.append(c)` followed by a reassignment and a later
  `write_file_at(..., confirmation=saved[0])` was obeyed.
- **`RootConfirmation` is also single-consumption and target-bound.** The spent
  flag lives on the confirmation, not on a slot in the target, so one object
  cannot be spent by two publications however it is attached; and `consume`
  checks the acting user id and the canonical assignment against
  `MutableTarget.user_id` / `.assignment`. Every publish helper
  (`_atomic_write_at`, `move_file_no_clobber`, `soft_delete_target`,
  `unlink_at`) takes one or refuses with `UnconfirmedPublication` — a
  programming error, deliberately not a `RuntimeError`, because the tool bodies
  catch `RuntimeError` around their publishes and would render it as a failed
  write.
- **A rollback rides the confirmation it undoes, through a `MovePermit` that
  cannot be forged.** The forward `move_file_no_clobber` issues it — nobody
  else can, `__init__` requires the module-private `_PERMIT_ISSUE` token — and
  it is bound to that confirmation's *lease*, so it is inert the moment
  `confirmed_publication` returns, plus the immutable
  `(user_id, assignment, rel)` of each end and object identity. One use,
  reverse direction only. Two earlier shapes were wrong: stamping the one
  confirmation onto both endpoints made a reusable token of a single-use fact,
  and a public `MovePermit(destination, source)` constructor authorised a
  rename with no confirmation at all.
- **Both ends of a move must be one caller, one assignment, one root inode.**
  `rename_noreplace` removes the source entry as surely as it creates the
  destination one, yet only the destination's confirmation is consumed, so
  `_require_one_vault` compares `user_id`, `assignment` and `fstat` of each
  pinned `root_fd` (a pathname comparison is not enough — two assignments can
  spell the same string over different directories) before anything is spent,
  on the forward move and on the rollback. Unreachable from `move_note`, which
  opens both ends with one `uid`; checked at the primitive because the next
  caller may not.
- **Three distinct error markers, because they say different things.**
  `no_vault_assigned` (admission: this credential had no vault this call),
  `vault_assignment_changed` (an administrator moved it — `VaultAssignmentChanged`),
  and `vault_confirmation_unavailable` (the read *failed*;
  `VaultConfirmationUnavailable`, not a `RuntimeError`, so no tool body renders
  it as a bad write). An outage recorded under the reassignment marker puts an
  administrator's name on an infrastructure incident. Before a call's first
  publication an outage propagates and the call fails; after `move_note`'s move
  has stood it is caught, the remaining rewrites stop, and the partial outcome
  is reported through the existing `failed_rewrite_sources` idiom — naming it
  an outage, never a reassignment.
- **`delete_file` holds no `MutableTarget`**, so its confirmation is consumed
  against the `(uid, root)` its own `_vault_context` resolved, and the whole
  delete — trash probe included — runs inside the confirmed step.

## The index records the vault it was scanned under

`users.indexed_vault_assignment` / `indexed_vault_realpath` /
`indexed_vault_handle` (migration 016, all nullable, marker-owned, **no
backfill**) record the root a pass actually scanned, so a reassignment stops
`semantic_search`/`keyword_search`/`list_notes`/the graph tools answering from
a vault the caller no longer has (#91). `classify_provenance` is the one
function that computes the verdict, over six rows: **indeterminate** (root
unpinnable, or its realpath no longer names the pinned inode) → nothing, and
the pass fails; **re-derive** (no record, a half-set record, exactly one fact
differing, or a handle contradicting an otherwise-matching pair); **keep**
(both agree); **discard** (both differ). A handle can refuse a keep, never
establish one, and never establish a discard. Ambiguity never resolves toward
keeping — silently wrong search results are the failure this product ranks
highest — and never toward discarding, which costs a full re-embed.

- **Not backfilling is the load-bearing decision.** Deriving the assignment
  from `users.vault_path` would assert that an assigned user's index was built
  under what it carries *now*, which is exactly the reassignment lag the record
  exists to detect. NULL means "provenance unknown", the only true statement at
  migration time, and such a user is repaired by re-deriving rather than
  discarding — so introducing the record costs no vault-wide re-embed. It is
  also what makes the deploy order safe with no cross-container coordination:
  the previous code cannot write these columns.
- **The whole pass runs beneath one pinned root descriptor**, so the facts
  observed, the files discovered and the bytes read come from one inode.
  `indexed_vault_realpath` stores `os.fsencode(realpath).hex()` — a pathname is
  arbitrary non-NUL bytes, and a surrogate escape would fail to encode inside
  the one transaction that must not roll back.
- **A discard is bound to the assignment that produced it.** The verdict is
  computed in an earlier transaction against a cached root, so the discard
  transaction takes the `users` row `SELECT … FOR UPDATE`, re-reads it, and
  requires present/active/assigned/*equal to `facts.assignment`* before
  deleting anything; the stamp beside it must affect exactly one row or the
  whole transaction rolls back. Without that, an administrator correcting a
  reassignment back destroys a complete, valid index and records provenance for
  a root nobody is assigned to. The re-derive's tail stamp takes the same lock
  and the same re-read, and is *withheld* on disagreement rather than fatal —
  it destroys nothing.
- **The two take that lock differently, and it is lock ordering, not tuning.**
  The discard has its own transaction and locks the parent *before* any child
  write — the panel's own user-delete direction — so it may wait. The tail
  stamp runs at the end of the pass's transaction, already holding
  `notes_metadata` row locks, while a permanent user delete locks `users` first
  and then cascades onto exactly those rows: waiting there is a real deadlock
  cycle, and Postgres would abort one side — possibly the operator's delete. So
  the tail asks `FOR UPDATE NOWAIT` **inside `session.begin_nested()`** and
  treats `55P03` as a withheld stamp (a state that branch already knows). The
  savepoint is required, not tidy: a failed statement poisons its transaction,
  so without one the pass would lose every repair along with the stamp.
  `_is_lock_not_available` walks `.orig` *and* `__cause__` — the SQLSTATE lives
  on asyncpg's own error, two layers down, exactly as `_log_usage`'s FK
  recovery has to walk.
- **A re-derive that skipped anything records nothing.** Any per-file skip —
  including both link-extraction skips, the missing buffered body and the
  missing index row — withholds the stamp, because the record's whole claim is
  that every surviving row was written by that pass.
- **`embed_vault` is deliberately ungated on provenance, because it verifies.**
  Gating it composed with the completeness rule into indefinite staleness: one
  permanently unreadable file withholds the record forever and would then
  freeze every other note's vectors at content it no longer has, while
  `semantic_search` kept returning them. Running ungated is sound only because
  the pass refuses bytes that do not hash to the selected row's `content_hash`
  — an embedding is a pure function of content, so which directory supplied the
  bytes is not a fact the vector depends on. **Removing the verification means
  re-gating the pass in the same change.**
- **Verifying the bytes is not enough on its own.** The ORM re-read that
  follows can see a hash another pass has committed (H2) while the vectors were
  built from H1; stamping H2 marked the row embedded for content it does not
  have, and H2 == H2 then blocked every later repair. `embed_note` therefore
  takes `certified_hash`/`certified_path` and stamps them with a conditional,
  row-locking `UPDATE … WHERE id AND file_path AND content_hash = H1` **before**
  it replaces a vector and **after** the provider call — so no row lock is held
  across a network request, and a row that moved matches nothing.
  `StaleCertification` rolls the note back and leaves it unmarked.
- **The exclusion branch certifies through the same predicate.** It reads no
  file, but it deletes a note's vectors and marks the row embedded, which is
  the same claim — and a move is exactly what it cannot see, because relocating
  a note changes `file_path` and not `content_hash`. Stamping by `id` alone let
  a decision about `Private/A.md` delete the vectors of a row that had become
  `Public/A.md` and record it as embedded with none: included, hash-equal, and
  therefore never selected again — silently and permanently absent from
  `semantic_search`. `certify_embedded` is shared by both paths, stamps before
  the delete (the conditional UPDATE is what takes the row lock), and takes
  `note_id` plus an explicit `expire_on` because the exclusion branch certifies
  from a plain result row no session maps.
- **A path change clears `embedded_content_hash`, at every statement that
  changes `file_path`.** The predicate above closes only *move-before-certify*;
  the mirror ordering is invisible to it, because when the move lands after a
  correct certification the stamp is already there and already true of the
  content. It is no longer true of the *decision*: the stamp says the row's
  current content has been dealt with and nothing about **how**, and the
  exclusion branch decides how by matching `EMBEDDING_EXCLUDE_PATTERNS` against
  the path. Carried across a move it freezes the old answer for ever — the pass
  selects on `embedded_content_hash != content_hash`, which a preserved stamp
  makes false. Out of an excluded folder: included, zero vectors, never
  selected again, silently missing from `semantic_search`. Into one: still
  searchable while excluded. So `move_note`'s metadata UPDATE and the indexer's
  **id-preserving** move detection both `SET embedded_content_hash = NULL`
  (the prune-and-insert path is unaffected — its replacement row starts null).
  NULL means *re-evaluate next pass*, not *not embedded*. **Do not "improve"
  this by consulting the exclusion config at move time**: the config can change
  before the next pass, so that is the same frozen answer in a new place, and
  it would give the move path a dependency on embedding configuration it has no
  other reason to know.

## The read path's owner predicate is total

`apply_note_filters(user_id=None)` used to append **no** owner predicate while
every write path maps `None` to `user_id IS NULL`. `MULTI_USER_MODE` can be
turned off after users exist, so a database holding named users' rows read by
an ownerless credential handed over every tenant's paths, titles, tags,
frontmatter and chunk excerpts (#127). `None` is now a scoping value — `IS
NULL` — and every index-backed tool is swept to it: `keyword_search`,
`semantic_search`, `list_notes`, `get_recent`, **`get_tags`**, `get_backlinks`,
`get_links`, `get_neighborhood`, `find_related`, `find_orphans`. A single-user
deployment sees no change; every row there is NULL-owned.

- **`note_links` carries no `user_id`, so ownership rides the endpoint rows —
  and *where* it rides decides two different things.** In a JOIN's ON clause a
  cross-owner target simply fails to resolve; as a WHERE on the joined row it
  would discard every *dangling* link too, which is what `get_links` exists to
  report. `_owner_predicate_for(entity, uid)` exists so an alias can carry it.
- **An edge admitted into the neighborhood BFS or the orphan calculus changes
  what the answer *is*.** It occupies a slot against `limit`, it can bridge two
  owned notes through a row the caller cannot see, and on the target side it
  silently strips an owned note's orphan status — so both endpoints must be
  inside the owned set at *traversal* time, never at hydration time. An edge
  counts for `find_orphans` only when its source is owned and its target is
  either owned or genuinely dangling (dangling still means "not an orphan",
  unchanged, and unrelated to ownership).
- **`get_links` classifies by what the scoped join resolved**, not by the raw
  `note_links.target_note_id`, and omits a row that names a target outside the
  owned set — that row is not dangling, and printing it would print the other
  owner's path. Unreachable in normal operation (link resolution is per user),
  which is why it is refused rather than assumed away.
- The owner predicate counts as a filter for the exact fallback — see "Filtered
  vector search".

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
- **The single-user form of that warm is authoritative — it evicts**, and it
  returns the root it read. It used to be a silent no-op for a NULL
  `vault_path`, so a previously cached root survived; the panel's
  `clear_user_vault_cache` only clears the worker that served the POST.
- **`_vault_root` prefers the request's own snapshot over the shared dict, and
  that is the part that fails closed.** `_user_vault_cache` is process-global
  and the indexer's bulk warm is add-only, so a bulk `SELECT` issued *before*
  the admin cleared `vault_path` can land *after* the per-request warm evicted
  the entry and put the revoked root back — mid-request, with a write tool in
  flight. Eviction cannot order a query that was already running. So the
  middleware binds `current_vault_root = (user_id, Path | None)` (a ContextVar
  beside `current_user_id` in `src/auth/session.py`) and the gate reads that;
  no other task can write this request's context. **Do not "simplify" the gate
  back to the dict** — the bulk warm's add-only behaviour is safe only because
  the snapshot outranks it. The snapshot is keyed by user id (another user's
  snapshot falls through to the dict) and is never consulted for
  `user_id is None`.
- **A cold cache refuses too**, with the same message — it is not permission to
  serve stale rows — and the refusal is written to `usage_logs` with
  `params["error"] = "no_vault_assigned"` and no other new field.
- Single-user and sandbox mode are untouched: `current_user_id` is None there
  and `_vault_root(None)` answers from `settings.vault_path`.
- **In multi-user mode, `user_id is None` is a refusal, not the global vault.**
  An ownerless credential — `api_keys.user_id` / `oauth_tokens.user_id` NULL —
  is the *single-user* shape, and it survives a configuration cycle: a key
  minted while multi-user was off keeps its NULL, and the bootstrap backfill in
  `src/auth/routes.py` only claims NULL rows while `users` is empty, so
  flipping the flag after users exist never adopts it. Every layer then treated
  that key as single-user and handed it `settings.vault_path` — an ownerless
  *readwrite* key could edit the whole vault. `APIKeyMiddleware` now 401s such
  a credential (`reason=ownerless_credential`, same body as any other rejected
  key, on both the API-key and OAuth branches) and `_vault_root(None)` raises
  when `settings.multi_user_mode`. Two layers on purpose: the middleware is the
  gate, `_vault_root` is the one that cannot be bypassed by a future caller.
- **The panel's vault browser uses what the warm returned, not a re-read of the
  dict.** `vault_page` warmed the cache and then called `_vault_root(user.id)`,
  which reopens the same window: a stale bulk warm landing in between served an
  unassigned user's vault. It now takes the `Path | None` from
  `warm_user_vault_cache` directly and renders the `vault_error` empty state on
  None. Any new caller that warms-then-resolves has the same bug — use the
  return value.

## Graph tools
- `get_backlinks(path, limit)` — notes that link TO `path` (resolved links only).
- `get_links(path)` — outgoing links from `path`, both resolved and dangling.
- `get_neighborhood(path, depth=1, limit=50)` — undirected BFS over the resolved-link graph; capped at `depth ≤ 5` and `limit ≤ 200`.
- `find_related(path, limit=10)` — semantic neighbors via averaged chunk embeddings; pgvector cosine distance, deduped per note.
- `find_orphans(folder, limit)` — notes with no incoming or outgoing resolved links; vault-hygiene tool.

Link extraction lives in `src/services/links.py`. The extractor strips fenced/inline code before regex matching for `[[wikilink]]`, `![[embed]]`, and `[md](path.md)` forms. Targets are resolved at index time and stored in `note_links`. On startup, if `note_links` is empty the indexer runs a one-shot backfill across all notes (logged with progress and surfaced on the dashboard).

## Write tools
- `create_note(path, content)` — create a new note (atomic write).
- `edit_note(path, content, append=False, find=None, section=None, replace_all=False, dry_run=False, replace_frontmatter=False)` — four mutually exclusive modes (full-replace, append, find/replace, section). `dry_run` returns a unified diff without writing; `replace_all` lifts the single-match guard for `find`. Section mode matches ATX headings only and supports `Parent/Child` path-style and `#N` ordinal disambiguation (see "Section addressing" below). Full-replace **preserves an existing valid frontmatter block** and section mode never touches one — see "Frontmatter is preserved unless the caller says otherwise" below.
- `move_note(from_path, to_path, rewrite_links=False)` — rename or relocate a note. Updates `notes_metadata.file_path` and `note_links.target_path` rows for the moved note. With `rewrite_links=True`, also rewrites `[[Old]]` / `[[Old|alias]]` / `[[Old#anchor]]` / `![[Old]]` / `[[folder/Old]]` forms in source notes.
- `delete_note(path, permanent=False)` — soft-delete to `.trash/<YYYYMMDD-HHMMSS>-<basename>-<8 hex>` by default (the same non-replacing `renameat2` `delete_file` uses; the hex suffix is what makes two same-second deletes distinct); `permanent=True` unlinks through the parent descriptor. The indexer skips dot-dirs, so search/embedding cleanup happens on the next reindex pass.
- `set_frontmatter(path, updates, remove=[])` — structured YAML frontmatter mutation. Round-trips via `yaml.safe_dump` (does not preserve YAML comments). Leaves the body byte-identical. **Refuses a malformed block by name** rather than prepending a second one; only an effective mutation writes.

All write tools route through `src/services/vault.py::_atomic_write_at`, which stages a tmp file in the destination's own directory, `fsync`s it, publishes it with a same-directory `renameat`/`linkat` **relative to the parent descriptor opened at validation**, and `fsync`s that directory afterwards — a crash mid-write cannot truncate the destination, and nothing that happens to the pathname meanwhile can redirect the write.

### Frontmatter is preserved unless the caller says otherwise

Issue #128. `read_note` strips the YAML block and full-replace wrote exactly
what it was given, so the natural agent read-modify-write — read a note, edit
the content portion, pass it back — **silently deleted the frontmatter**. In
the same family, `set_frontmatter` over a malformed block prepended a *second*
`---` block above the broken one and reported success, and `remove=` no-oped.
Destructive and silently-wrong writes, the class this product ranks highest.

- **`content` is the new body; a valid line-1 block is kept byte-identically
  ahead of it.** The separator is one `\n`, inserted **only** when the block
  slice does not end in a newline (a metadata-only note whose closing fence
  sits at EOF) and `content` is non-empty. The slice is the parser's own
  computed span — never `raw[:-len(body)]`, which is wrong for an empty body.
- **`content` is never classified, and that is the whole design.** Three audit
  rounds broke every attempt to infer intent from content shape: a line-1 `---`
  test breaks on bodies opening with a thematic break, a complete-valid-block
  test breaks on a stripped body that itself opens with a mapping-shaped fenced
  block — which is exactly what `read_note` returns for such a note. Intent is
  asked for instead: **`replace_frontmatter=True`** replaces the whole file
  (today's behaviour, now opt-in) and is the only way to drop or repair a
  block. It is in `edit_note`'s `_tracked` allow-list, because it is the
  difference an operator needs to see after a block goes missing. Combined with
  `append`/`find`/`section` it is the multi-mode error; with
  `operation="replace"` it is not, since both name full replacement.
- **A note with no valid block — absent *or* defective — is replaced wholesale
  by default.** There is nothing valid to preserve, and this keeps the repair
  path open without the flag.
- **Section mode resolves over the frontmatter-stripped body and reattaches the
  block**, restoring the read/write selector parity the spec already promised:
  a YAML `#` comment is never selectable and never counted by an ordinal, which
  it was on the write side. Over a **defective** block a section write is
  **refused by name** — resolving over raw bytes there lets a `#` line inside
  the broken block be selected and lets the replacement span swallow the
  closing fence. Reads are deliberately asymmetric: `read_note` still extracts
  from such a note, because a read destroys nothing.
- **`set_frontmatter` diagnoses before the empty-`updates`/`remove` no-op**, so
  a broken note is reported broken even for a call that would have changed
  nothing. Unclosed fence, YAML error and non-mapping (list, scalar, `null`,
  `~`, comment-only) each refuse naming the defect and the
  `edit_note(replace_frontmatter=True)` repair; `remove=` refuses identically.
- **Only an *effective* mutation reaches the serializer, compared
  type-sensitively.** Plain `==` conflates `True` with `1`, which would report
  a real type change as a no-op. The guard is also what stops a
  remove-of-nothing from dropping a valid **empty** block —
  `serialize_frontmatter({}, body)` emits no fences, so on a note whose body
  opens with a mapping-shaped fenced block that drop would promote the body
  prefix into active frontmatter. The guard compares the **final** mapping
  against an untouched deep copy, not the per-key bookkeeping: an update and a
  removal that cancel (`updates={"temp": 1}, remove=["temp"]`) record two steps
  and arrive back where they started, and the step-counting version waved that
  through into the serializer. Removing the last *actual* key does remove the
  block: no fences, no separator, exactly the prior body — and **if the body's
  own first lines are a mapping-shaped fenced block, that prefix becomes the
  note's active frontmatter.** Spec-mandated and caller-requested, unlike the
  accidental version above, but declared here because the outcome is the same
  shape.
- **Equality is type-sensitive, order-sensitive and signed-zero-sensitive.**
  `True == 1` and `-0.0 == 0.0` are both True in Python and both write
  different YAML, so either would report a real change as a no-op; floats go
  through `float.hex()`, which is exact for finite values and renders every NaN
  as `'nan'` (two NaNs are therefore the same value — YAML round-trips both to
  `.nan`). Mappings compare in order, because `safe_dump` runs with
  `sort_keys=False` and key order is part of the note's bytes.
- **One partition, shared by read and write.** `parse_frontmatter` and
  `parse_frontmatter_diagnose` both call `_partition_frontmatter`, so a block
  `read_note` strips can never be diagnosed differently by a tool about to
  write. `parse_frontmatter` gained **exactly one** behaviour change:
  whitespace-only fenced YAML is a valid empty mapping for *every* consumer.
  That has to be shared — leaving the read side treating `---\n---\n` as
  absent while the write side preserved it makes the read-body round trip
  *duplicate* the block. The predicate is whitespace, tested **before** the
  YAML call: PyYAML refuses a bare tab, so asking it would make `---\n \n---\n`
  valid and `---\n\t\n---\n` a parse error.
- **A line ends at LF, CRLF or a lone CR, in the partition and in the heading
  scan alike.** `read_file` applies universal-newline translation, so a
  classic-Mac note reaches the read parser as LF and its block is recognized;
  the write paths read raw bytes. While either predicate knew only `\n`, the
  same file was stripped on read and diagnosed *absent* on write — full-replace
  deleted the block and `set_frontmatter` prepended a second one — and `.`
  matching `\r` made the whole file one line, so `## A\rold\r## B` scanned as a
  single heading running to EOF. `(?m)^…$` cannot express either rule in
  Python; both use explicit `(?:\A|(?<=\n)|(?<=\r))` / `(?=\r|\n|\Z)`
  boundaries, and CRLF is always matched before the bare CR alternative so a
  terminator is never split down the middle. The two must move together: fixing
  the partition alone turns section mode's safe "no ATX headings" refusal on
  such a note into a write against a bogus heading.
- **Declared staleness.** Notes already indexed under the old empty-block
  partition (the block surfacing as literal body text, and `note_links.position`
  measured against it) do not self-heal on an ordinary pass — change detection
  hashes the raw bytes before parsing, so unchanged bytes skip the reparse. The
  artifact is cosmetic and vanishingly rare; it heals on the note's next
  hash-changing edit or under the explicit per-index rebuilds
  (`make rebuild-tsvectors`, reset/re-embed). A parser-revision invalidation
  mechanism is not worth building for it.
- **The round-trip guarantee is scoped, and both layers' docstrings say so:**
  it covers a complete, unwindowed whole-note read only (`section=None`,
  `offset=0`, no `[TRUNCATED]`). A truncated read must be paged to the end
  first, and a `read_note(section=…)` response **includes the heading line**
  while `edit_note(section=…)` takes the body only.

### Mutations act on the path as named — never through a symlink

`validate_path` returns `(vault / rel).resolve()`, which **follows symlinks**. Every mutating tool used to act on that resolved path, so an in-vault alias `alias.md -> important.md` made `edit_note("alias.md", …)` rewrite `important.md` and report success for `alias.md` — a destructive write on a path nobody named (#54).

`open_mutable(rel, user_id)` in `src/services/vault.py` is the guard (`validate_mutable_path` is its single-shot form, kept for callers that only need the verdict), and it is what `write_file` / `write_bytes` validate with, so every mutation entry point is covered:

- the **parent** is resolved and must stay inside the vault. Symlinked *directories* inside the vault (shared attachment folders — a common Obsidian setup) therefore keep working; an ancestor pointing out of the vault is still the traversal error.
- the **final component is taken as named** and `os.lstat`-ed. A symlink — dangling included — is refused with an error naming the link's canonical vault-relative target ("`outside the vault`" when it escapes), so the agent can operate on the real note.
- it returns `resolved_parent / name` as `path` (and its vault-relative form as `rel`): the real directory entry the indexer sees. `move_note` takes `from_rel` / `to_rel` from the targets, so `notes_metadata.file_path`, `note_links` and backlink discovery agree with the filesystem for notes under a symlinked folder.

Applies to `create_note`, `edit_note` (all modes — `dry_run` refuses too, rather than diffing a note the caller did not name), `set_frontmatter`, `move_note` (source and destination), `delete_note` and `write_file`. `delete_file` already refused, via the anchored `vault_fs` walk.

**Reads are deliberately unchanged.** `read_note`/`read_file`/`list_*`/graph tools still follow links — an alias reading as its target is what a user expects from an alias, and a read cannot destroy anything.

### Resolve once, open the parent, then act on the descriptor (#59)

Resolving the parent at validation only helps if the rest of the tool never asks the kernel to walk that pathname again — and a `Path` *is* a pathname. `validate_mutable_path` returning one left a live race: a process that renamed the resolved parent directory and dropped a symlink at its name, or repointed the directory behind a symlinked vault root, **between two syscalls of a single write** sent the write to a directory nobody validated. `expected=` cannot catch it, because the decoy may hold byte-identical bytes.

So `open_mutable(rel, user_id)` is now the entry point for every mutation. It runs the same guard and additionally hands back a **`MutableTarget`**: the resolved `path`, the vault-relative `rel`, the final component `name`, and an **open parent directory descriptor**. Staging, the `expected=` read, publication, the permanent unlink and the `.trash` rename all run relative to that descriptor, so no pathname is resolved after validation. A directory descriptor keeps naming the same directory however its pathname is later renamed or relinked.

- **The root descriptor is opened first and never reopened by name.** Resolving the root to a pathname and only then opening that pathname left the whole guard resting on a name: the resolved root could be renamed away and a symlink left at its name in between, and the descriptor everything else anchors to would be a directory containment never saw. Pinning first inverts it — the root is an inode from the start — and `_require_same_directory` then checks that `vault.resolve()` still names that inode, because `rel` and the containment check are computed against the pathname and must describe the directory we pinned.
- The parent is opened from that already-open root fd by **one** kernel-enforced beneath-root lookup — `openat2(RESOLVE_BENEATH | RESOLVE_NO_SYMLINKS | RESOLVE_NO_MAGICLINKS)` in `vault_fs.open_dir_beneath` (#87). Resolution of the *pathname* happens first, so in-vault symlinked folders keep working; the lookup itself stays strict, so a component that *became* a link in the interval is refused rather than followed. Relaxing it to follow in-vault links was the other option and was rejected — it re-derives containment per component, which is the check-then-act this change removes, and it would weaken the primitive `/transfer/*` depends on. Two flags it deliberately does not carry: `RESOLVE_NO_XDEV`, which would refuse lookups *through* a mount point beneath the root and so break every read and write that works across a nested mount while fixing none of the three that do not; and `RESOLVE_IN_ROOT`, which scopes `..` chroot-style rather than refusing it — `_split` stays in front and refuses `..`, absolute paths and NUL bytes lexically, because `RESOLVE_BENEATH` *scopes* `..` too (`A/../A` succeeds at the kernel) and nothing here is normalised on the caller's behalf.
- `MutableTarget.dir_fd` creates a missing parent on first use; `parent_fd` never does. Deferring creation out of validation keeps a refused write (an over-cap body) from littering the vault with empty folders, and stops a read helper from `mkdir`ing.
- `write_file_at` / `write_bytes_at` / `read_bytes_at` take the target, not a `Path`. The string-taking `read_bytes` / `write_file` / `write_bytes` are single-shot conveniences that open and close their own target; if you find yourself calling one *after* `open_mutable`, you have reintroduced the bug.
- `_atomic_write_at` stages with `O_CREAT|O_EXCL|O_NOFOLLOW` (a planted symlink on the temp name is an `EEXIST` we step around, never a decoy we truncate) and **`fsync`s the payload before publication** — without that, a crash just after the rename can publish a note whose data blocks never landed.
- **The destination directory is `fsync`ed *after* publication too, and so is the parent of every directory the call created** (#97). The payload flush makes the contents durable and says nothing about the entry that names them, so without this half a crash loses the write entirely; and flushing the note's own parent while the entry naming *that* parent is still only in the page cache loses the whole new folder, so `create_note("New/Folder/x.md")` flushes `New/Folder`, `New` and the root — outward to the first directory that already existed. `ensure_parent` records what it created (a component another writer won the race to is not ours to flush). **A failure of any of those flushes is logged and the write reported as the success it is** — see the asymmetry below; do not "fix" it into an error.
- **The no-clobber publish never exposes a staging name at all — on either write path** (#59 for notes, #92 item 1 for transfers). It stages into an `O_TMPFILE` inode — no directory entry, nothing to observe, replace or race — and publishes it with `linkat` through `/proc/self/fd/<fd>`. That also means there is nothing to clean up: a *named* staging file has to be unlinked, and an unlink is by name, so it can only be guarded by an identity check followed by the removal, which is check-then-act and could delete a substitute planted in between. `O_TMPFILE` removes the step rather than guarding it. **Do not add `O_EXCL`**: with `O_TMPFILE` it means "this file may never be linked into the filesystem", which makes the publish `ENOENT` — the opposite of its usual meaning. Two kernel details that look like blockers and are not: `linkat`'s `AT_EMPTY_PATH` form needs `CAP_DAC_READ_SEARCH` (an ordinary container has none) while the `/proc` magic link does not, and the "cannot link a zero-link inode" rule applies to an inode whose names were *removed*, not to one created `O_TMPFILE`. Verified on the deployment kernel at `CapEff=0`. `vault_fs.create_nameless_temp` / `vault_fs.link_staged_inode` are the one implementation of both halves — `vault.py` holds `_link_staged_inode` / `_proc_fd_available` as aliases, because a second copy is how the two paths drifted apart before. **This describes the mode the probe selects where `O_TMPFILE` works**; where it does not, `UnsupportedFilesystem` names `VAULT_ALLOW_NAMED_STAGING_FALLBACK` and only that flag buys the by-name form back (below).
- The **overwrite** publish is `renameat`, which is inherently by name, so its staged inode must acquire one. The two paths differ in *when*. A note write stages under a name for the whole call (`_create_temp_exclusively`, beside the destination), which is right for a write that completes in one call. A transfer stages unnamed and materialises a transient name **inside the publish gate**, immediately before the fingerprint check and the rename, in `.transfer-tmp` and never in the destination directory — the name then exists for two syscalls in a `0700` directory owned by this process, instead of for a multi-minute body plus an unbounded wait on the gate's row locks (D20). Both then run the same guards: an identity check immediately before the rename that the name still refers to the inode we wrote (`vault._require_staged_name`, `vault_fs.require_staged_name`), narrowing the substitution window to that one syscall; and a discard that unlinks **only** while the name still refers to our inode, otherwise leaving it in place and logging (`vault._discard_temp`, `vault_fs.discard_staged_name`). The failure direction is to leave litter, never to remove something we cannot prove is ours — answering a substitution by deleting the substitute is the same destructive-write class, just aimed elsewhere. The check narrows the window; it does not close it, and a substitution landing between the check and the rename is still published — declared, not a gap.
- **`VAULT_ALLOW_NAMED_STAGING_FALLBACK` is one flag for both write paths, default off** (#103, D27), **and both paths honour it.** Some servers refuse `O_TMPFILE` outright: TrueNAS SCALE's NFS export answers `EOPNOTSUPP` as root, on a second export, under NFSv4.1 and NFSv4.2, and still after a NAS upgrade, while named staging with a `link()` publish works on the same mount. It is deliberately one knob rather than two, because two would permit a deployment with a working `create_note` and a refusing upload — a state nobody chose and nobody can diagnose from either symptom alone. There is no `TRANSFER_*` variant and no per-path override.
  - **Both halves have landed** (the transfer path with the original change; the note path with #103's contributor PR, gate-hardened in #114 and archived in #116). Unset, both paths refuse on such a mount with a message naming the flag; set, the transfer path stages under a name in `.transfer-tmp` and the note path's no-clobber writes (`create_note`, `write_file(overwrite=False)`) stage under a name beside the destination. Note overwrites always staged under a name — a replacing rename has no by-descriptor form — so the flag never governed them. The archived `vault-write` spec states the fallback clauses; the interim wording that only the transfer path honoured the flag is gone with it.
  - **The probe selects the mode, once per root, and it never flips.** `probe_publication` exercises unnamed staging and by-descriptor publication alongside the hard link and the two flushes, and its cached per-root result *records the staging mode*; every publication on that root reads it back. A root that staged one upload without a name and the next one under a name would make the window each upload ran in unknowable after the fact. Flag off and unnamed staging unavailable → the probe raises, so **no token is minted and no body is streamed**. Flag on → the named mode is selected, but only after the primitives *it* needs have been established too; the flag buys back named staging and nothing else, so a root that cannot flush a directory is still refused.
  - **The fallback carries two guards the pre-change transfer path did not have.** Today's publish ran no staged-name identity check and unlinked the staging name unconditionally. The fallback inherits the transient name's guards rather than reverting past them — a name that lives for the whole streaming window needs them more, not less.
  - **The window it reopens is declared, and the two fallbacks are not equal.** A named staging file carries a directory entry for the whole streaming window, so the substitution the unnamed mode closes structurally is open again for that window, narrowed by the identity check. The transfer path stages in `.transfer-tmp` — `0700`, owner-checked, dot-prefixed, skipped by the indexer and refused by every tool's hidden-path guard — so no agent, no capability and no vault tool can reach a staged name and the residual adversary is a process running as this uid, which can rewrite the destination directly and needs no race. The note path stages beside the destination, in a directory the vault's own tools can write to. The transfer fallback's window is the **narrower** of the two; do not document them as equivalent.
  - **It announces itself once, on first exercise.** One `WARNING` per process the first time a call actually stages under a name — not when the flag is set, not when the probe selects the mode — plus `vault_named_staging_fallback_active` on `/health`, one field for both paths. That distinction is the whole value of the signal: it separates an operator who enabled the flag defensively from a mount that is taking the fallback. `/health` reads process state and **never re-probes**; a probe writes.
  - One consequence for the sweep: `.transfer-tmp/.tmp-*` older than 24 hours has nothing *new* to collect in the unnamed mode (the kernel frees an unnamed inode when the last descriptor closes), but an abandoned or killed upload in fallback mode leaves a staged file exactly as the pre-change path did. The sweep stays for both that and pre-change litter.
- No-clobber never degrades to a replacing rename: `EPERM`/`EOPNOTSUPP`/`EXDEV` raise `UnsupportedFilesystem`.
- **Moves that rewrite links are serialised process-wide** (`_MOVE_REWRITE_LOCK`, held across preflight *and* rewrites — exactly the span descriptors are pinned for). Two moves each inside their own budget can still exhaust the table between them, so the bound has to hold for the process, not per call. Plain moves pin two descriptors and are not serialised.
- **Anchoring costs descriptors, and `move_note(rewrite_links=True)` is the one place that matters.** Each *planned* rewrite pins one open parent fd from its preflight read until its post-move write — that single descriptor is what makes the two provably the same directory — and the preflight must finish before the move commits so an over-cap rewrite can still abort it. Sources that turn out to need no rewrite are released at once, and each planned one is released as soon as its write lands, so the peak tracks planned rewrites rather than backlink count. Beyond that the plan is bounded by `config.max_move_rewrite_sources()`, derived from `RLIMIT_NOFILE` (soft limit minus `MOVE_REWRITE_FD_RESERVE`) — **with no floor**, because a floor guarantees the exhaustion the cap exists to prevent on exactly the processes that cannot afford it. Exceeding the budget aborts the move *before any mutation*, and so does an actual `EMFILE`/`ENFILE` during the preflight: running out of descriptors says the plan is too big for this process, not that one source failed, and treating it as a per-source failure moved the note while silently dropping the rest of the rewrites. Same shape as `MAX_MOVE_REWRITE_BYTES`, same reason.
- `move_note` publishes with one `renameat2(RENAME_NOREPLACE)`, not `link` + `unlink`. The old shape could unlink a *different* inode than the one it linked, destroying a file that replaced the source in between. `delete_note` soft-deletes through `vault_fs.soft_delete_at`, sharing that primitive with `delete_file`. **Both parent directories are `fsync`ed after either rename lands** (#97) — the trash directory counts as one of them — as is the parent of a permanent unlink; every one of those flushes is logged and swallowed, never reported.
- **`move_note` identifies what it moved, before it moves it.** `renameat2` relocates whichever inode is at the source when it runs — the property that keeps a replacement from being destroyed — so the regular-file check that ran before the preflight does not bind the commit. `_pin_source_inode` takes an `O_PATH|O_NOFOLLOW` fd of the source first (it works for a link or a directory too, and has no side effects), and `_verify_the_moved_inode` compares it with an `lstat` of the destination through its parent fd. Three outcomes, and the last distinction is the point: our inode and a regular file → the move stands; **our** inode but a directory or a symlink → roll back with a second `RENAME_NOREPLACE` (as `soft_delete_at` does) and refuse; **not** our inode, or unidentifiable → report where things are and roll back *nothing*, because moving it away would relocate a third party's file on the strength of a name. The database is never updated on any refusal, and every post-rename failure becomes an explicit result — by then the file has been published somewhere and a traceback would leave the caller with no idea where. Unlike the soft delete, a **symlink** is refused too: a link is inert in `.trash` but not at a move destination.
- The leaf is re-`lstat`ed through the parent fd, and a leaf that became a symlink between validation and the act is **named as one** by every mutating tool. The read-modify-write tools (`edit_note`, `set_frontmatter`, `delete_note`, `move_note`'s source) and `write_file` check before acting, via `_leaf_state_error`; the creating tools (`create_note`, `write_file`, `move_note`'s destination) check on the no-clobber refusal, because `link`/`renameat2` reject a plain file, a directory and a symlink with the same `EEXIST`. Neither "not found" (which invites the agent to create it, over the link) nor a bare "already exists" nor a silent success is acceptable: `write_file(overwrite=True)` would otherwise replace the link and report "Wrote N bytes" for an alias the caller still believes in.

**The accepted residual, precisely.** The claim this is entitled to, in the words every artifact of #87 uses: **every below-root directory descriptor a call uses as a pathname anchor comes from a lookup the kernel proved beneath the vault root at the moment it resolved, and no directory descriptor retained from a creation descent is ever returned to a caller or used as a pathname anchor — so no operation is ever redirected into a directory that was never beneath the root.** Scope that exactly: it is about **directory** descriptors used as pathname anchors. A call's own staged payload descriptor is created by that call and published through by descriptor, and never anchors a pathname, so the broader "no descriptor whose containment the kernel did not check is ever acted through" is false and must not be written anywhere. Nor is "nothing outside the root is ever written" — that was the claim review rejected, and the two bullets below say why. What remains is substitution at the leaf, plus what a lookup structurally cannot promise:

- the leaf can be swapped for a symlink between the guard's `lstat` and the read or write. `O_NOFOLLOW` turns that into `ELOOP`, which the tools report; the link is never followed and nothing outside the named directory is touched.
- **an adversary who can write to the destination directory itself** can still win the `renameat` race on an overwrite publish. Say it plainly: that adversary can also just edit the note directly, so it is outside the threat #59 addresses — redirection through an *ancestor* or the *root*, where the attacker never had access to the destination at all. The no-clobber publish has no such window (nothing it stages is ever named), unless `VAULT_ALLOW_NAMED_STAGING_FALLBACK` is set, which is the operator's declared decision to take it back.
- **creating a missing directory has no beneath-root form** (#87, D22). The *lookup* window is gone: `vault_fs.open_dir_beneath` is one `openat2(RESOLVE_BENEATH | RESOLVE_NO_SYMLINKS | RESOLVE_NO_MAGICLINKS)`, so there is no interval between components for an ancestor rename to exploit. But `mkdirat` has no such form, and no syscall creates a directory *and* proves the path it created it under stayed beneath a root. So creation is still one component at a time — carrying **no** descriptor across a creation: each `mkdirat` goes through a fresh beneath-root lookup of the prefix that already exists, dropped at once, and the descriptor the caller finally gets comes from a fresh lookup of the whole path performed *after* the creation. What a race can still cost is at most one **empty** directory per component, **per creation descent**, in a place the winner already controls — never a file, never file content, never something a tool reports success about. The bound is per descent and one call can have more than one: an upload walks its destination twice with creation enabled (a cheap up-front walk so a bad path costs one syscall rather than a whole body, and the authoritative walk inside the publish gate), a note write once. **Do not try to clean it up** — an `rmdir` by a name the caller chose is the same delete-the-substitute hazard `_discard_temp` and `soft_delete` refuse.
- **a lookup proves containment when it resolves, not afterwards** (#87, D26). `RESOLVE_BENEATH` proves the path stayed beneath the root *during that resolution* and says nothing about the future — it cannot: the descriptor it returns keeps naming the same directory however that directory's pathname is later renamed, which is the entire reason #59 anchors to a descriptor rather than a name. So between the final lookup and the publish — the transfer gate's destination walk to its `linkat`/`renameat`, a note tool's `open_mutable` to its publish — a process that can rename a vault ancestor can move the resolved directory out of the vault, and the call publishes into it, wherever it now is, and reports success. Nothing was *redirected*: the bytes went to the directory the caller named, which somebody else moved. Excluding it would need an operation the kernel does not offer, and re-verifying by walking `..` upward from the descriptor is check-then-act one level up — the class of bug #59 exists to remove. **Retained, not introduced**: the per-component walk had this interval too, underneath the larger window it did not close.
- a **read-modify-write overwrite** (`edit_note`, `set_frontmatter`, and `move_note`'s link rewrites) is optimistic, not linearizable: `expected=` compares the current bytes immediately before the rename and a writer landing inside that window is still overwritten — the same guarantee level as the transfer fingerprint check, declared rather than implied.
- **`write_file(overwrite=True)` has no conflict detection at all** — it is an unconditional replace, and deliberately so: the raw byte tool takes whole-file content from the caller and has no prior read to compare against. Do not read `expected=` into it. `request_upload(overwrite=True)` is the path that *does* bind to the incumbent's fingerprint; use it when the caller cares that the file has not changed.
- The **no-clobber** publish (`create_note`, `write_file` by default) has no window at all (`link()` is kernel-atomic), and neither does the soft delete or `move_note`'s own publication (one `renameat2(RENAME_NOREPLACE)`).

All of these are properties of concurrent editing, of an attacker who already holds the destination directory, or of what the kernel offers — not of path resolution: the write always lands on the path the caller named, in the directory that was validated.

An in-vault `..` (`Folder/../note.md`) is also refused by `validate_mutable_path` — a mutating tool never resolves a component away — but with a message naming the normalised path rather than "Path traversal denied", which would be a lie the caller cannot act on. Reads still resolve `..`.

## File-access tools (non-markdown)
Raw read/write/browse of arbitrary vault files, distinct peers to the note tools (note tools stay markdown-only). Pure byte transport — no server-side PDF/text extraction, no embedding or indexing of non-markdown files.
- `read_file(path, encoding="auto", offset=0, limit=None)` — `auto` resolves text-like MIME → text, image → inline MCP image content block (renders in-client), everything else → base64 string. `text` forces UTF-8 decode (errors on non-UTF-8); `base64` forces raw-bytes base64. Capped by `MAX_FILE_READ_BYTES` (default 10 MB), checked against on-disk size before reading. Text results are additionally bounded by `MAX_READ_RESPONSE_CHARS` and page via `offset`; base64 and image results are not windowed. Base64 reads are token-heavy — check size with `list_files` first.
- `write_file(path, content, encoding="base64", overwrite=False)` — `base64` decodes `content` to raw bytes; `text` writes UTF-8. No-clobber by default (`overwrite=True` to replace), auto-creates parent dirs, atomic via `vault.write_file`. Capped by `MAX_FILE_WRITE_BYTES` (default 25 MB) on decoded length.
- `list_files(folder=".", pattern="*", recursive=False, limit=200)` — `ls`-style: immediate children (subdirs + files) by default, each file with size + mtime; glob-filterable; capped at `limit` with a truncation note.

- `delete_file(path, permanent=False)` — soft-deletes to `.trash/<YYYYMMDD-HHMMSS>-<basename>-<8 hex>` through the anchored helper; `permanent=True` unlinks. Refuses `.md` (pointing at `delete_note`), directories and symlinks. The `.md` refusal runs on the **canonical** final component, so `note.md/.`, `a//note.md` and `NOTE.MD` are refused too — the caller's string is not the path.

`write_file` additionally goes through `validate_mutable_path`, so it refuses a symlinked final component the way the note tools do (see "Mutations act on the path as named" above) — `overwrite=True` cannot clobber a file through an alias. `read_file` and `list_files` still follow links.

All four enforce the same traversal guard and the same dot-dir guard (`is_hidden_path`, rejecting any path component starting with `.` — the indexer's visibility rule, keeping `.obsidian`/`.git`/`.trash`/`.smart-env` out of reach), but **not through the same validator**: `read_file` and `list_files` use `validate_visible_path` (which resolves, so links are followed), `write_file` uses `validate_mutable_path` (parent resolved, symlinked leaf refused), and `delete_file` canonicalises lexically and resolves through `vault_fs`'s beneath-root lookup. Vault helpers (`read_bytes`, `write_bytes`, `list_dir`, MIME classification) live in `src/services/vault.py`. MIME detection uses stdlib `mimetypes` plus a magic-byte sniff for PNG/JPEG/GIF/WebP. `read_file` is the first tool returning a non-`str` MCP content object.

## File transfer

No MCP client can hand a tool the bytes of a file the user is looking at, and
the server cannot reach the user's disk. Five tools plus a public route family
close that gap. The whole design is one idea: **a capability pins everything it
may do at mint time, and the route acts on nothing else.**

**Tools** (`src/mcp_server/tools.py`, registered in `server.py`):
- `request_upload(path, overwrite=False, expires_in=None)` — readwrite. Mints a single-use upload capability; returns `upload_id` (opaque `public_id`, never the row id), a `…/transfer/upload#<token>` URL, `expires_at` and `max_bytes`.
- `check_upload(upload_id)` — `pending` | `uploading` | `unknown` | `revoked` | `completed{path,size,sha256,mime,completed_at}` | `expired`, scoped to the minting **principal** *and* user — the API key itself, or, for OAuth, the whole grant family behind the presented access token. Another principal's handle is `not found`. See "check_upload answers for the vault, not for the row" below.
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
- **The deadline is re-checked inside the locked gate, immediately before `publish`** (`_refuse_if_past_deadline` in `stream_to_vault`). `_drain` bounds the *body*; the gate runs afterwards and can wait unboundedly on `SELECT … FOR UPDATE`, so a body that finished a second inside the deadline could otherwise publish — an overwrite included — long after the capability expired, at a moment when `check_upload` was already reporting `unknown` for it. It raises the existing `Timeout` on purpose: the route maps that to **consume**, which is what the state machine says about a deadline overrun, and it is unambiguously pre-publication so the "`PostPublishFailure` is the only exception after the bytes land" contract is intact. So the deadline is enforced twice — at drain time and again inside the locked gate — but it is honoured **to within the publish latency** (`vault_fs.publish`'s own `open_parent` walk plus, for an overwrite, the incumbent's fingerprint re-hash, bounded by `MAX_FILE_WRITE_BYTES`), not to the syscall: a literal no-write-after-deadline guarantee would need a pre-mutation callback threaded inside `vault_fs.publish`, and that coupling was judged not worth it, because the write that lands late is the consented, fingerprint-verified one and so is not a destructive write on anything unintended.

### `check_upload` answers for the vault, not for the row

Three rules, each of which was violated by reading `state` and `expires_at` and nothing else:

- **A claimed token is answered before expiry, and never as "never used".** `claimed` past its TTL is reached by exactly one path — `PostPublishFailure` — and that path runs *after* the bytes are in the vault. The expiry branch used to fire first and say the link "was never used", about a file sitting at the path; with a ten-minute TTL that was the answer an agent was most likely to see (#75). Inside `min(expires_at, claimed_at + TRANSFER_MAX_UPLOAD_SECONDS)` the answer is `uploading` and it names that deadline; past it, `unknown` — the bytes may be there, go `list_files`/`read_file` the path before re-minting, the same thing `import_from_url` says for the same outcome. `consumed` is the one mid-flight end state that *is* provably empty (the timeout paths raise before `publish`), and it says so.
- **One deadline, and one clock.** The arithmetic lives once, in `transfer.upload_stream_deadline`, which returns an *absolute UTC instant*; `routes._upload_deadline` returns that instant unchanged and the route hands it to `stream_to_vault`, which measures it through `transfer._deadline_remaining` against `transfer.now_utc()` — the same function `check_upload` compares with. Both halves are load-bearing. A second copy of the arithmetic would drift. Converting to `time.monotonic()` at claim time (which is what the route used to do) keeps the arithmetic shared but splits the *clock*: a realtime step then moves the tool and not the route, and the tool reports a stream live that the route has already killed. The accepted trade-off is that a backward realtime step extends an upload — two surfaces that agree beats two that disagree, because the disagreement is what an agent relays to a human. `import_from_url` still passes a monotonic float: its fetch budget is private and no surface reports on it. **Nothing under `src/transfer/` may define its own "now".**
- **Liveness is re-checked, inside the open session.** `lookup_by_public_id` filters on public_id/direction/principal only, while `PUT /transfer/upload` also requires `resolve_identity_ok(need_write=True)` and `resolve_root_ok`. So after an OAuth scope downgrade or a vault reassignment the tool asserted "pending" about a link every redemption would 404 (#71). Both predicates now run for `pending`/`claimed` rows and produce a `revoked` answer naming the cause. **`completed` rows are deliberately not re-checked** — that transfer already happened, and a later revocation must not turn a true report of a landed file into a false "revoked". For a `claimed` row the dead reason is *appended* to the ambiguity, never substituted for it: revocation does not un-publish bytes.

Precision here is the design, not an exception to it — this side is authenticated and identity-scoped. None of the branches may put a token or any other secret into `usage_logs`; the `upload_id` shape check still runs before `_tracked` sees the argument.

### The handle belongs to the principal, not to the credential row

`lookup_by_public_id` scoped an OAuth-minted transfer to the exact
`oauth_tokens.id` that minted it. An access token lives one hour and rotation
mints a **new row** for the same user, the same client and the same consent, so
an hour later the agent's own `check_upload` answered "no upload link with id …
was minted by this identity" — the message reserved for a genuinely foreign
handle — about a `completed` upload whose sha256 `request_upload` had told it
to come back for (#74).

The scope is the stable principal instead. An API key *is* one; an OAuth access
token is one hour of one, and the principal behind it is the **grant family**
(`oauth_tokens.grant_id`, migration 014). The lookup is one statement with a
correlated `EXISTS` joining the *minting* token to the *presenting* token on
`grant_id` — no column on `transfer_tokens`, no migration, and nothing to keep
in sync: `grant_id` never changes after insert, and the row cascades away with
its minting token anyway. The `user_id` comparison stays on top of it.

What deliberately did **not** widen:

- **A different grant is still `not found`** — another client, or a second
  `/authorize` approval by the same user for the same client. Two consents are
  two things the operator can revoke independently.
- **Redemption stays bound to the minting credential row.** `resolve_identity_ok`
  and the publish gate still load the exact `oauth_tokens` row named on the
  token. That cannot make `check_upload` lie, because `plan_mint_window` clamps
  every capability's expiry to that credential's own (see below): the minting
  access token outlives every link it minted unless it is revoked, and a
  revocation *should* kill the link. Widening redemption to "any live token of
  the family" would bind an already-minted capability to credentials that did
  not exist when it was minted — strictly more than the operator agreed to, for
  no case that is currently wrong.
- **The API-key path is untouched.** A key does not rotate; a second key of the
  same user is a different principal.

The `EXISTS` compares `client_id` as well as `grant_id`. One `grant_id` belongs
to exactly one `(client_id, user_id)` by invariant, not by constraint, and this
predicate is the access control — so a family that somehow spanned two clients
still cannot leak between them.

**Accepted limitation — pre-014 families are approximate.** 014's backfill
groups pre-existing rows by `(client_id, user_id)`, which #64 accepted as the
best available guess (nothing in the old schema recorded which consent a row
came from). Two consents by the same user *for the same client* made before 014
therefore share one family, so a token from either can read `check_upload`
status — path, size, sha256, mime — for a handle minted by the other. Same
user, same client, read-only status on a handle that authorises nothing, and it
shrinks as those tokens age out; every grant issued after 014 is exact. Not
worth inventing a consent boundary the database never recorded.

### A link never outlives the credential that minted it

Redemption re-checks the credential (`_credential_ok`), so `transfer_tokens.expires_at` alone was never the effective lifetime: an OAuth access token lives one hour, and `expires_in=3600` on that path is therefore *always* divergent (#73). `transfer.plan_mint_window` computes `min(requested TTL, credential expiry)` and the row stores that, so the tool result, `/transfer/*/info` and both pages all show a deadline enforcement agrees with — clamping once instead of teaching three surfaces the same arithmetic. **`mint_token` calls it itself, in its own transaction, immediately before the INSERT, and takes no window parameter** — it *returns* the window so the tools can report a clamp. Do not add one back: a caller-supplied deadline is a caller-supplied security boundary, and a stale window (computed before a revocation, or by a path that forgot) is exactly the divergence this exists to remove. The same call re-validates the credential with `_credential_ok`, the redemption predicate, so a key revoked, downgraded, deactivated or reassigned between the tool's permission check and the INSERT mints nothing rather than a row whose only future is the 404. **That re-validation is an unlocked `SELECT`, and deliberately so:** a revocation committing between it and the INSERT yields a capability every redemption rejects and `check_upload` reports as `revoked` — fail-closed, the same optimistic level declared elsewhere here, and the locked re-check that actually matters is the publish gate.

Two details are load-bearing. A null `expires_at` means "never expires" for an `APIKey` and "already dead" for an `OAuthToken` (`_credential_ok` refuses the latter outright), so `credential_expires_at` maps them differently — getting that backwards mints links against dead tokens. And under `MIN_MINT_TTL_SECONDS` (30 s) of runway — or with no credential row behind the call at all — the mint is **refused** rather than shortened: an error tells the agent to re-authenticate, whereas a two-second link tells it to hand a human a URL that will 404. The threshold sits below the 60 s floor `expires_in` already clamps to, so it only ever fires because the credential is nearly spent.

### The consent page states the mode

`transfer_upload.html` shows Destination, **Mode**, Maximum size and Link expires. Mode is `overwrite` from the info payload — "Replaces the existing file at `<path>`" versus "Creates a new file" — and an overwrite link gets a destructive button label and destructive status copy. That page press is the consent step for the only session-less write path in the app; until this, a link that replaces a file rendered identically to one that creates one (#72). The flag was already on the wire; the fix is display plus the matching spec requirement. The page stays self-contained and nonce-guarded — no new asset, nothing path-specific rendered server-side, everything into the DOM through `textContent`.

### Uniform 404
Unknown, expired, consumed, claimed, revoked credential, downgraded permission, inactive user, reassigned vault root, **an ownerless identity while `MULTI_USER_MODE` is on** — one status, one body.

**`user_id IS NULL` is normal in single-user mode and is nobody in multi-user mode.** `_credential_ok` compares `cred.user_id == row.user_id`, and `None == None` passed; `resolve_root_ok` and `locked_rows_ok` then authorised `settings.vault_path` outright. So a capability minted by an ownerless key *before* an operator enabled multi-user mode stayed redeemable afterwards — able to replace a file in whatever vault that setting names — even once `APIKeyMiddleware` had started rejecting the same key. `_ownerless_in_multi_user` is consulted in all three predicates, not just the credential one: the two root checks are the other half of a defensive pair, so neither is the only thing standing between a stale capability and the global root. Single-user behaviour is unchanged. `_not_found()` is the only way for a bearer-protected endpoint to say no. Precise status comes from the *authenticated* side, via `check_upload`. The uniformity is of the *response*; the branches do different amounts of work and none of this is constant-time, so do not claim timing indistinguishability for it.

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
Every transfer write and `delete_file` resolves its directories with one `openat2(RESOLVE_BENEATH | RESOLVE_NO_SYMLINKS | RESOLVE_NO_MAGICLINKS)` from an open root descriptor (#87), so a symlink anywhere in the chain raises instead of being followed and the kernel proves containment for the whole path inside the single call. The read side inherits it too — `_fingerprint_of`, `_head_bytes` and the download route's `_open_bound_file` all reach it through `open_parent`. **There is no fallback to a per-component walk**, and a `lifespan` probe beside `_check_pgvector_version` refuses to start where the syscall is unavailable (kernel < 5.6, or a container seccomp profile that blocks it); the call site raises `UnsupportedFilesystem` regardless, which is what a `MCP_SANDBOX_MODE` process — the one configuration that skips the probe — hits. On that path the download route keeps its **uniform 404**: a distinguishable status there would report a server property to an unauthenticated bearer. Bytes stage in **`<root>/.transfer-tmp/`** — as an `O_TMPFILE` inode with **no directory entry** wherever the publication probe proves that works (#92 item 1), so nothing in that directory can be observed, replaced or raced while a body streams, and an abandoned upload leaves nothing for the sweep. The directory itself stays: `O_TMPFILE` takes a *directory* to choose the filesystem the inode is allocated on. The destination parent is re-walked *inside the gate* — a descriptor opened before a minutes-long stream keeps pointing at the same directory after a rename, and publishing through it would follow the move.

**`soft_delete` is one `renameat2(RENAME_NOREPLACE)` — it never unlinks anything and never pre-creates the destination.** `rename_noreplace()` in `vault_fs.py` reaches the syscall through `ctypes` (the glibc wrapper, present since 2.28; a `syscall()` fallback exists for older glibc). It buys two guarantees at once, and both are load-bearing:

- the trash name is *created or refused* — `EEXIST` means somebody else holds it, and the delete retries under a fresh random suffix rather than overwriting;
- whichever inode is at the source when the call runs is what moves, so a writer that replaced the source ends up in `.trash` intact rather than destroyed.

Two earlier shapes were each wrong at one end. `link`+`unlink` could unlink a *different* inode than it had copied. `O_EXCL` placeholder + `os.rename` fixed that end and not the other — **`os.rename` replaces** — so between reserving the placeholder and renaming onto it, anything that took that pathname over was silently destroyed, and the error path would have unlinked that file too while tidying up "our" placeholder. `RENAME_NOREPLACE` has no reservation window and nothing to clean up. **Do not "simplify" it back to `os.rename`**; a filesystem that cannot do the non-replacing form raises `UnsupportedFilesystem` (`probe_trash` exercises the same primitive at first use, so it fails at startup rather than at the first delete), and there is no safe fallback.

The `lstat` symlink/directory refusal still runs first. A **symlink** swapped in afterwards is deliberately not re-checked — it is moved into `.trash` intact, never followed — but a **directory** is: the moved name is `lstat`ed in the trash and, if it is one, put back with a second `RENAME_NOREPLACE` and the delete refused, because a subtree carries files nobody asked to delete. If the rollback loses the source name the directory stays in `.trash` and the error names that location.

**Publication is recorded the instant `publish()` returns, before any cleanup.** In `transfer.py`, `_publish_into_current_parent` invokes an `on_published` callback before it closes anything, and every close on the publish path goes through `_close_quietly`. The reason is narrow and expensive: a bare `os.close` raising `EIO` after the bytes landed discarded the `Published` outcome and surfaced as a generic `OSError` — which the upload route reads as "demonstrably pre-publication" and answers by **releasing the claim**, handing back a replayable token over a path that already holds the file, while `import_from_url` reported "could not write" for a file it had written. `PostPublishFailure` is the only exception `stream_to_vault` may raise once the bytes are in place; anything you add between `publish` and the return must preserve that.

**Both write paths flush, and they fail in opposite directions (#97).** Every publish makes two things durable: the staged payload before publication, and the destination directory — plus the parent of every directory the call created, outward to the first pre-existing one — after it. Without the pair a crash leaves the vault contradicting something the server already said: a transfer recorded `completed` whose file is absent or truncated at a path an agent was told to read back by `sha256`, or a `create_note` that reported success and left no note.

- **Transfer.** `stream_to_vault` flushes the staged payload immediately after `_drain` returns, beside the `os.fchmod`, **before `before_publish()`** and **off the event loop** (`asyncio.to_thread`): a flush of up to `MAX_FILE_WRITE_BYTES` must not run while the gate holds `SELECT … FOR UPDATE` on the token, the credential and the user, and unlike `_drain`'s per-chunk `_write_all` a single `fsync` waits on the whole body reaching the device. A failure there is pre-publication — the claim is released and the human may retry the same link. The directory flush lives in `_publish_into_current_parent`, **after `on_published` has recorded the publication**, which is the whole of its classification: `_stream_locked` then sees `state["published"]` true and converts it to `PostPublishFailure`, so the token strands `claimed` and `check_upload` answers `uploading`/`unknown` — "the bytes may be there, go look before re-minting". Flush *before* the callback and the same failure escapes as a bare `OSError`, which the route reads as "nothing was published" and answers by handing back a replayable token over a written path. `import_from_url` inherits all of it through the shared helper.
- **Note.** `vault._flush_publication` does the same two flushes after `_atomic_write_at` publishes, and **logs every failure rather than reporting one (D18)**. That is deliberately the opposite direction, and the reason is retry safety: a stranded upload capability costs a re-mint and the human is told to look at the path first, while a note tool that reports a false failure gets *retried* — and `edit_note(append=True)` retried after a write that actually landed appends the same block twice. On this path a false failure manufactures a destructive outcome; on the transfer path it merely wastes a link. The payload is durable either way, so what is unconfirmed is only a directory entry, and the previous content survives regardless. Every `_atomic_write_at` caller inherits both flushes — `write_file` in both its no-clobber and `overwrite=True` modes included, not just the note tools.
- **Every publication flushes, not only the staged-payload helper.** `move_note`'s `renameat2` writes *two* directory entries — the new name and the source's removal — so `move_file_no_clobber` flushes both parents (plus any chain the destination created), and the rollback in `_verify_the_moved_inode` gets it free by calling the same helper with the targets swapped. `soft_delete_at` flushes the source's parent and `.trash` once the directory refusal has had its say (flushing earlier would make an intermediate state durable and then undo it), and `_refuse_a_moved_directory` flushes both after a rollback that lands. `vault_fs.remove` flushes after the permanent unlink, and so does `delete_note(permanent=True)` — an entry that survives a crash resurrects a note the agent was told is gone. All of these go through `vault_fs.flush_dir_quietly`, which is D18's direction in a function: the rename already happened, and a tool that reported it as failed would be **retried** — against a source that is no longer there, so the retry either contradicts the vault or acts on whatever has since taken the name. `flush_dir_fd` is the raising form and has exactly one caller: the transfer path's post-publication flush.
- `probe_publication` exercises **both** flushes, not just the hard link. A filesystem that links happily and rejects a directory `fsync` would otherwise pass the probe, mint a token, take a whole body, publish it and only then strand the claim on the one failure the transfer path cannot undo. `EINVAL`/`ENOSYS`/`EOPNOTSUPP`/`EPERM`/`EACCES`/`EROFS` become `UnsupportedFilesystem`; `EIO` stays an `OSError`, because a sick device is not a filesystem to be swapped out.

**The probes are split by capability and run only where that capability is used.** `check_publication_support(root)` links root→root, allocates an unnamed inode and publishes it by descriptor, and flushes both a staged file and a directory descriptor (uploads, imports, `PUT /transfer/upload`) — **and returns the staging mode that root will use**, which is the only place that decision is made; `check_trash_support(root)` renames a temp file into `.trash` (only a `delete_file` soft delete — `permanent=True` probes nothing). Both cache per root; the tools and routes surface `UnsupportedFilesystem` as a stable error. **No read path probes at all** — a probe writes, and `request_download`/`check_upload`/the download route must not create files on a read-only capability. The first publication probe per root also prunes `.transfer-tmp/.tmp-*` files older than 24 h, which is where a crashed upload's staged bytes would otherwise sit forever.

**A cached probe result is bound to the root's *identity*, not to its pathname.** The key is the configured string, but the entry carries `(st_dev, st_ino, mount_id)` of the root that was actually probed, and every hit re-reads it and re-probes on a mismatch. A configured root is a *name*, and the root is the one path this module resolves by name — repointing a symlinked root from a filesystem where the fallback's primitives work to one that refuses a directory `fsync` would otherwise reuse the first verdict *and its staging mode* for a filesystem nothing ever probed: mint a token, stage a whole body under a name, publish it, and strand the claim on the first directory flush. The mount id is optional here and degrades to `None` on a kernel without `STATX_MNT_ID`; `same_mount` refuses in that case instead, because there a fallback to `st_dev` would answer the wrong question rather than a weaker one.

**Publication into a mount beneath the vault root is refused, and the two halves promise different things** (D23). `link(2)` and `rename(2)` both return `EXDEV` across a mount boundary, and an upload stages in a root-level `.transfer-tmp` and publishes out of it — so a destination on a nested mount cannot receive one. `vault_fs.same_mount` compares `statx`'s `STATX_MNT_ID`, **never `st_dev`**: a bind mount of a directory of the same filesystem beneath the root reports the same `st_dev` on both sides (measured: 66306) and different mount ids (653 vs 6036), so an `st_dev` preflight passes and the publish fails after the body has streamed. The ids are read and compared inside one call and never persisted — plain `STATX_MNT_ID` is reused once its mount is gone, and not spanning time is the only thing that makes it sufficient without `STATX_MNT_ID_UNIQUE`. `STATX_MNT_ID` is Linux **5.8** — a **transfer-write minimum, not the server floor**, which stays at `openat2`'s 5.6. The asymmetry is deliberate: `openat2` is a containment guard, and without it every write would anchor to a descriptor nobody checked, so `lifespan` `sys.exit(1)`s. `STATX_MNT_ID` guards one refusal on one feature — without it `request_upload`, `import_from_url` and `PUT /transfer/upload` refuse (the safe direction) while reads, search, the note tools, downloads, the panel and OAuth are unaffected. Killing all of that to defend a transfer-only check is the false-positive direction, so `_check_mount_identity_support` logs one WARNING naming exactly what is degraded and starts. `/health` reports it as `transfer_mount_check_available` (`null` = the probe never ran, e.g. sandbox mode). Both startup probes are read-only and create nothing.
  - **At mint or fetch start** (`request_upload`, `import_from_url`, through `_mint_preflight`) a boundary that is *already there* is refused before any body is read, staged or published — comparing the staging directory with the **deepest existing** destination ancestor, since a directory created beneath it lands on that ancestor's mount. This is the only half that spares the bytes.
  - **Inside the publish gate**, after the authoritative destination lookup and before the link or rename, a boundary established *afterwards* is refused. Still pre-publication — nothing is written and the claim is released — but by then the body has streamed in full. **Do not describe the pair as "refused before any body is streamed."**
  - The residual `EXDEV` mapping stays in front of both, because the preflight is check-then-act: `_link_no_clobber`, `link_staged_inode` and the overwrite `renameat` all raise `MountBoundary` (a subclass of `UnsupportedFilesystem`, so every surface answers it without a new branch) naming the boundary. Previously the no-clobber branch blamed hard-link support — false, the filesystem has them — and the overwrite branch let `EXDEV` escape as a bare `OSError` into the route's generic handler, giving a server error where the other mode gave a 503.
  - **The destination *file* is checked too, for an overwrite.** A bind mount on the target itself leaves the parent comparing equal and still refuses the rename — with `EBUSY`, not `EXDEV`. The leaf's mount id is read `O_PATH|O_NOFOLLOW` (so no symlink is followed and the file is never opened for I/O) at both points, and only for a publication that *replaces*: a no-clobber publish onto an existing name is `EEXIST` whatever the mount layout, and "target already exists" is the accurate refusal there. A leaf whose mount id cannot be read is **not** treated as a boundary — inventing one would refuse a publish that would have worked. `EBUSY` out of the rename is reclassified as a boundary **only** after a fresh leaf check establishes that cause, because `EBUSY` has other sources. In practice the fingerprint check usually gets there first: binding a *different* file over the target changes the inode, so only a self-bind reaches the rename.
  - **What is unaffected:** reads, note writes on the mount (they stage in the destination's own directory), permanent unlinks, and moves that stay on one side. **What still fails, with issues open:** the soft delete (`.trash` is opened beneath the *root*, so `EXDEV`) and `move_note` across the boundary. Neither is covered here — group 4 is transfer publication only, and it compares the staging directory with the destination parent, not the two parents of a move.
  - **The upload route says which refusal it is.** `MountBoundary` subclasses `UnsupportedFilesystem`, so its handler must come **first** or it is unreachable — and the generic body ("the filesystem does not support atomic no-clobber publication") is false for a mount boundary and flatly false for an `overwrite=True` link, which never uses that publish. The mount body is **path-free**: that route is unauthenticated beyond the bearer token, and precision about which path comes from the mint tools and `check_upload`. Unknown, expired and consumed tokens stay on the uniform 404.

**`vault` has adopted these helpers** (#59): `_atomic_write_at` stages and publishes against the parent descriptor `open_mutable` opened, `move_note` publishes with `rename_noreplace`, and `delete_note` soft-deletes through `soft_delete_at` (the anchored form of `soft_delete`, taking a parent fd the caller already holds plus a `stamp`/`label`). **Every successful publication flushes the complete ancestor chain from the destination parent up to the vault root** — a note write, a transfer publish, a move, and a soft delete (whose destination parent is `.trash`). Flushing a directory persists its *contents*; the entry that names it is a separate write, so without this a crash could durably remove `Folder/note.md` and lose the whole `.trash` directory with the only copy of the note inside it.

**It is the whole chain and not this call's creations, and that distinction is the fix.** Attributing the flush to whoever ran the `mkdir` looks precise and does not survive an *abort*: a call that creates `New/Folder` and then dies before publication flushes nothing — correctly, it published nothing — and the retry finds both directories there, records no creation, and would leave the entry naming `New` durable nowhere while reporting the file `completed`. The obligation outlives the call that incurred it and outlives the *process*, so no in-memory provenance can discharge it; a durable record would be a journal for something that costs one `fsync` per path component. Vault paths are two or three deep and a directory `fsync` is metadata-only, so the conservative rule is the cheap one. `vault_fs.publication_flush_dirs` is the one definition; `created` is still passed and is currently always a subset, kept so a caller that creates a directory *outside* the destination's chain is a covered case rather than a silent hole. **Do not "optimise" this back to the created list.** Direction stays D18's: raising on the transfer path (`PostPublishFailure`), swallowed-and-logged on the note path.

**A link rewrite is a publication and owes the same chain — which is what makes `move_note`'s descriptor budget load-bearing here.** `move_note(rewrite_links=True)` pins one target per backlink source from its preflight read until its post-move write, and the source count is unbounded, so each target used to hand back its root descriptor to hold one fd instead of two. But a target with no root cannot look its ancestors up: `_flush_target_dirs` caught the missing-root `RuntimeError`, logged it, and flushed only the leaf's parent — every backlink rewrite silently exempt from the chain rule. The fix is **one shared root descriptor for the whole rewrite phase** (`MutableTarget.share_root`), `dup`ed from a root the kernel already proved rather than opened from the root *pathname* — re-resolving that name is the substitution surface #59 closes, and a `dup` resolves nothing. `share_root` verifies the shared descriptor names the same inode the target's parent was proved beneath **before** swapping, which is why it must be called *instead of* `release_root` and never after it: a target whose root is already gone cannot prove which root it was validated against. A mismatch means the vault root was repointed mid-call (`VaultRootMismatch`) and aborts the whole move while that is still free — the preflight has not mutated anything. The budget arithmetic is explicit in `max_move_rewrite_sources()`: one fd per planned rewrite plus `MOVE_REWRITE_SHARED_ROOT_FDS` (1) for the phase. **Giving each target its own root is the trap** — same correctness, but two descriptors per source, which halves the cap (384 rather than 767 planned rewrites at a 1024 soft limit) to hold N duplicate descriptors of one directory. `release_root` survives only for a target that will not publish. `check_trash_support(root, root_fd=…)` takes the caller's anchored root so the probe cannot create `.trash` in a directory the root pathname has since been repointed at. `vault` keeps its own staging — a `.tmp-<name>-…` file in the *destination* directory rather than `.transfer-tmp/` — because a note write completes in one call: there is no minutes-long stream to survive, and staging beside the destination keeps the publish a same-directory rename.

**Follow-up:** `usage_logs.key_id` still has no `ON DELETE` — a usage-log row written by an upload blocks its key's delete. Pre-existing, not regressed here.

### Path canonicalisation — do not "simplify" this
`validate_visible_path` runs (it is the shared traversal and dot-dir guard, and it is what refuses a link pointing out of the vault) but its **return value is the resolved path, and resolving follows symlinks**. The vault-relative path a transfer acts on is normalised *lexically* in `tools._vault_context`. Taking it from the resolved result silently retargets the operation: `delete_file("Attachments/alias.png")` where `alias.png` links to `secret.png` resolved to `secret.png` and deleted **that**, reporting success for a path nobody named. Keeping the caller's own components means the anchored walk is what meets the symlink — and refuses it.

### SSRF policy for `import_from_url`
The host is folded to canonical ASCII **first** (NFKC, the alternative full stops `。．｡`, then `idna.encode(uts46=True)`) and every check runs on that form — checking before normalising let `svc.prod。internal` past a suffix check and then resolved it as `svc.prod.internal`. Then: https only (`IMPORT_ALLOW_HTTP` for http), no userinfo, no zone ids, no single-label or `.localhost`/`.local`/`.internal`/`.home.arpa` names, no ambiguous numeric hosts, scheme-paired ports (443/8443, 80/8080). Every resolved address must pass an **explicit deny list** — loopback, RFC 1918, ULA, link-local, CGNAT, `0.0.0.0/8`, `240/4`, `198.18/15`, `192.0.0.0/24`, documentation, multicast, unspecified, reserved, IPv4-mapped/compat, NAT64, 6to4, Teredo (embedded IPv4 extracted and re-checked) — *and then* `is_global`. `is_global` alone is not enough; it admits IPv6 multicast and the NAT64 prefix. The connection is pinned to the validated address with `Host` and SNI kept as the original name, a new client per hop, `trust_env=False`, `http2=False`, ≤ 5 manual redirects with every rule re-applied, and one 30 s deadline over the whole thing.

### Declared filesystem semantics
Case-sensitive, non-normalising filesystems (ext4/xfs — the production bind mount) on Linux. Hard links must work within the root, and `.trash` must accept a same-device `renameat2(RENAME_NOREPLACE)`; the probes refuse otherwise rather than degrading to an overwriting move. Since #59 this covers the **note** tools too — `create_note`/`write_file` no-clobber publication needs the hard link, and `move_note`/`delete_note` need `renameat2` — so a mount without them loses those tools with a named error, not silently. Case-insensitive or normalising mounts are out of scope, as is any platform without `renameat2` — the non-replacing move has no portable fallback.

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
