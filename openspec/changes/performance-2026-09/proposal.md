## Why

The 2026-09-22 performance audit measured six sources of latency, which are filed as #278–#283. Each one is measured on the production host. Each one is paid on a path an agent waits on, or on the single event loop that every tenant shares (`--workers 1` is part of the contract):

- **#278**: the index pass walks, reads and SHA-256-hashes the whole vault **on the event loop**, inside a transaction that already holds the generation advisory lock (`src/services/indexer.py:1808` takes the lock; the walk runs at `:1854–1870`). On a cold page cache the walk took 177 s, and `/health` went unanswered for **235 s**. On a warm cache every 5-minute tick still freezes the loop for about 1 s. The embed path's `clean_for_embedding`, chunking and `parse_frontmatter` also run on the loop (`embeddings.py:1007`, `indexer.py:3296`, `:3616–3627`). A 9.2 MB note stalls every tenant for about 1 s.
- **#279**: every API-key `/mcp` POST pays up to **three fsync'd commits** on an HDD-backed Postgres, at about 40–50 ms each. The commits are the `api_keys.last_used_at` UPDATE (`auth.py:400–406`), the `usage_logs` INSERT (`tools.py:243`) and the quota upsert (`quotas.py:343`). For API-key clients, arrival→dispatch is p50 51 ms / p90 112 ms. For OAuth clients, which skip the write, it is p50 11 ms. Fast tools run in 1–8 ms, so the bookkeeping costs 10–50× the tool body.
- **#280**: read tools hydrate whole ORM rows. Every result detoasts `content_tsvector`, which averages 9.5 KB and reaches 883 KB. `semantic_search` also ships the 1024-float `embedding` of every overfetched chunk, only to recompute in NumPy a similarity the query already returned as a distance (`embeddings.py:1207`, `:1306`). Projected columns cut semantic search at limit 15 from 50–115 ms to 8–10 ms, and `list_notes` at 200 rows from 29–57 ms to 2.6–3.6 ms.
- **#281**: `OllamaProvider` opens a **new TCP connection per chunk** and embeds one chunk per request (`embeddings.py:539–590`). `OpenAIProvider` builds a client per call (`:614`). Every edit re-embeds **every** chunk of the note, even though append-only edits (the dominant agent write pattern) leave all earlier chunks byte-identical.
- **#282**: an idle tick does work proportional to the whole vault. It re-reads and re-hashes about 167 MB per tick because there is no stat shortcut. The exclusion reconciliation sweep runs in full every tick (`indexer.py:3526–3540`), with one `EXISTS` probe per certified row: that is 7.98 M lifetime scans of `ix_note_embeddings_note_id`, against 1,980 on the HNSW index. The sweep also re-reads, parses and chunks the ~56 zero-chunk notes on every tick.
- **#283, repo half**: `notes_metadata` has never been vacuumed. Its dead-tuple count sits below the 0.2 scale-factor threshold, so the GIN pending list is scanned linearly on every keyword query, and the GIN metapage statistics the planner relies on have never been written. The 146 MB HNSW index competes for a shared 2 GB `shared_buffers`. The `random_page_cost = 1.1` comment in `semantic_search` claims SSD storage that does not exist.

The consumer is an agent, and the vault is the owner's single source of truth. Three of these fixes therefore touch **search correctness**, not only speed: the stat shortcut, the exclusion-sweep gate and chunk-vector reuse. Each is designed so that a missed edit is **bounded, detectable, and declared**, never silent.

## What Changes

- **#279**
  - `last_used_at` is written at most once per 60 s per key, and asynchronously.
  - The `usage_logs` INSERT and the quota upsert commit with `SET LOCAL synchronous_commit = off`.
  - The `users` row (`is_active`, `vault_path`) is folded into the credential SELECT on both the API-key and OAuth paths. This removes a second transaction and two SELECTs while it keeps the fresh per-request read that #66 requires.
  - Gate order, `write_usage_row`'s return contract and the #193 coalescer requeue are unchanged.
  - Credential issuance, rotation, revocation, code exchange and transfer-token writes stay synchronous.
- **#280**
  - `NoteMetadata.content_tsvector` becomes `deferred` with raise-on-load.
  - `semantic_search`, `keyword_search`, `list_notes`, `get_recent`, `find_orphans` and `get_neighborhood` project only the columns they render.
  - `semantic_search` drops the `embedding` from its result set and reports `similarity = 1 − distance`.
  - Results are identical except for the ordering of exact ties, which is now deterministic. `find_related`'s overfetch is out of scope.
- **#278**
  - The scan's walk, read and hash run in a worker thread.
  - `clean_for_embedding`, the chunker and `parse_frontmatter` run in a worker thread on every embed path.
  - The walk moves **ahead of the generation lock and the pass transaction**. Every mutation decision is still made against the row state read *under* the lock (design D9, which writes down the constraints C1–C8 this must preserve).
  - Acceptance criterion: `/health` keeps answering during a cold full-hash pass.
- **#282**
  - A stat shortcut skips the read and hash when `(size, mtime_ns, ctime_ns, inode)` matches the row. It needs migration **026**.
  - The shortcut is bypassed by a re-derive, a stale extraction marker, a row with no recorded stat, a "racy" stat, and every full-hash backstop pass. Backstop passes run at process start and every `INDEX_FULL_HASH_INTERVAL_HOURS`, 24 h by default.
  - The exclusion sweep is gated on an in-process fingerprint of `EMBEDDING_EXCLUDE_PATTERNS` plus a clean-completion flag per scope. It also runs unconditionally on every backstop pass.
- **#281** (sequenced after PR #284 and S3)
  - Each provider gets one shared, pooled `httpx.AsyncClient`, built **through #284's `embedding_http_client` factory** and closed in the lifespan.
  - Ollama embeds through fixed-size `/api/embed` input arrays: `OLLAMA_EMBED_BATCH_SIZE`, default 16, with a 30 s timeout per request and still no aggregate deadline.
  - Stored vectors are reused for byte-identical chunk text, only while the embedding fingerprint is present and matches, and only if the reused rows still exist under the generation lock.
- **#283, repo half: migration 027**
  - `notes_metadata` gets autovacuum reloptions: `autovacuum_vacuum_scale_factor` and `autovacuum_vacuum_insert_scale_factor` = 0.02. This was chosen over `fastupdate = off`; see D17.
  - The one-time vacuum is **not** in the migration. Autovacuum performs it by itself once the reloptions lower the threshold below the current dead-tuple count, and `make db-vacuum-notes` is the explicit fallback.
  - A `halfvec` HNSW expression index at the **configured** dimension, with both vector paths casting to match and re-ranking candidates at full precision. **It is gated: it ships only if `tests/integration/test_search_recall.py` meets its SLO against a full-precision exact baseline.** Otherwise it is dropped from 027 and recorded here.
  - The `random_page_cost` rationale comment is corrected.
  - `alembic check` stays clean.

## Out of scope

- **Moving PGDATA/WAL to NVMe, or `chattr +C` on PGDATA.** Host operations on the shared Postgres; Max's call.
- **`pg_stat_statements` and `pg_prewarm`.** Both need `shared_preload_libraries` and therefore a restart of the shared Postgres. Host operations.
- **`usage_logs` retention.** An audit-log policy decision, not a performance fix.
- **A query-embedding cache** for `semantic_search`.
- **`find_related`'s overfetch underfill** (50 chunks collapsing to 7 distinct notes) and `DISTINCT ON (note_id)`. That change moves the recall baseline and needs its own benchmark decision.
- **`OpenAIProvider`'s batching**, which is already native (96 per request). It only gains the shared client.

## Capabilities

### New Capabilities

None. Every requirement modifies or extends an existing capability.

### Modified Capabilities

- `mcp-request-routing`: the per-request vault refresh is folded into the credential read (MODIFIED). Authentication bookkeeping is throttled and asynchronously committed, and the usage row commits asynchronously with an unchanged return contract (ADDED).
- `usage-quotas`: quota admission commits asynchronously, so a crash can only undercount (ADDED).
- `search-quality`: read paths project what they render; similarity is the database distance; a reduced-precision vector index ships only behind the recall SLO, and the pre-warm probes the index the search uses (ADDED).
- `index-integrity`: scan work is off the loop and ahead of the lock; the stat shortcut and its backstop (ADDED). The exclusion sweep is gated (MODIFIED). The per-request Ollama bound (MODIFIED). Chunk-vector reuse (ADDED).
- `embedding-providers`: Ollama batching (MODIFIED); one pooled client per provider, built through the transport factory (ADDED).
- `schema-integrity`: the head literal moves to `027`, with `026` in the chain (MODIFIED). 026 and 027 each own their units (ADDED).

## Impact

- **Code**
  - `src/mcp_server/auth.py`, `src/services/quotas.py`, `src/services/vault.py` (the warm helper only), `src/services/search.py`, `src/services/embeddings.py` (four disjoint regions), `src/services/indexer.py` (two disjoint regions), `src/services/index_state.py` (unchanged unless S3 needs a helper).
  - `src/mcp_server/tools.py` (four disjoint regions), `src/models/db.py` (three regions), `src/config.py` (two settings blocks), `src/main.py` (lifespan close), `src/control_panel/routes.py` (reindex flag and reset DDL), `scripts/reset_embeddings.py`, `alembic/env.py`, and a new `src/services/vector_index.py`.
- **Schema**
  - **026**: four nullable stat columns on `notes_metadata`, plus an all-or-none CHECK. Metadata-only.
  - **027**: `notes_metadata` reloptions and, if the recall gate passes, the `halfvec` expression index replacing `ix_note_embeddings_embedding_hnsw`.
  - Both migration numbers are reserved for this change.
- **Operators**
  - New settings: `INDEX_STAT_SHORTCUT`, `INDEX_FULL_HASH_INTERVAL_HOURS` and `OLLAMA_EMBED_BATCH_SIZE`.
  - New target: `make db-vacuum-notes`.
  - The first index pass after deploy is a full-hash pass that records every note's stat. If 027 carries the index, the pass rebuilds the vector index.
- **Dependencies**
  - S5 must not start before **PR #284 (`internal-transport-tls`)** merges.
  - S4 must not start before S2 and S3 merge: 027 chains from 026 and edits `semantic_search` after S2.
- **Docs**
  - `docs/architecture/{rate-limits,usage-attribution,vault-roots-and-tenancy,search,indexing-and-embeddings,schema-and-migrations}.md`.
  - The CLAUDE.md key-decisions list.

## Spec review

Not yet run. Task 0.2 is the Codex spec review. Because the stat shortcut, the sweep gate, chunk-vector reuse and the projection change are search-correctness surfaces, the implementation also goes through a mandatory adversarial pass (task 5.2).
