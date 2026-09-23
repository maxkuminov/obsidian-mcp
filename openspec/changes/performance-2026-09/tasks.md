# Tasks — performance-2026-09 (#278–#283)

The work is five slices. Each is implemented by an independent Opus subagent in its own worktree, on a flat branch. Where a file is shared, each slice owns a **named region** of it. A subagent that needs to edit outside its region stops and reports instead. The supervisor closes the seams at merge (task 6).

| Slice | Branch | Issue | Sequencing | Owns (whole files) |
| --- | --- | --- | --- | --- |
| S1 — request-path commits | `perf-s1-commits` | #279 | none | `src/mcp_server/auth.py`, `src/services/quotas.py`, `tests/test_perf_auth_bookkeeping.py`, `tests/integration/test_perf_async_commit_pg.py` |
| S2 — read-path projection | `perf-s2-projection` | #280 | none | `src/services/search.py`, `tests/test_perf_projection.py`, `tests/integration/test_perf_projection_identity_pg.py` |
| S3 — scan off-loop + stat shortcut | `perf-s3-scan` | #278, #282 | none | `alembic/versions/026_note_stat_columns.py`, `tests/test_perf_scan_offload.py`, `tests/test_perf_stat_shortcut.py`, `tests/integration/test_perf_scan_pg.py` |
| S4 — database (027) | `perf-s4-db` | #283 | **after S2 and S3 merge** (027 chains from 026; S4 edits `semantic_search` after S2) | `alembic/versions/027_notes_vacuum_and_halfvec.py`, `src/services/vector_index.py`, `scripts/reset_embeddings.py`, `alembic/env.py`, `tests/test_perf_vector_index.py` |
| S5 — provider transport + reuse | `perf-s5-provider` | #281 | **after PR #284 merges and after S3 merges** | `tests/test_perf_provider_batching.py`, `tests/integration/test_perf_chunk_reuse_pg.py` |

**Shared files and their regions**

| File | S1 | S2 | S3 | S4 | S5 |
| --- | --- | --- | --- | --- | --- |
| `src/mcp_server/tools.py` | `_insert_usage` only | `list_notes_impl`, `get_recent_impl`, `find_orphans_impl`, `get_neighborhood_impl`'s hydration (`meta_stmt`) | `move_note_impl`'s `nm_update` `.values(...)` only | `find_related_stmt` and `find_related_impl`'s vector statement / re-sort | — |
| `src/services/embeddings.py` | — | `semantic_search` | `embed_note`'s `clean_for_embedding(...)` + `chunk_text_bounded(...)` statements only (wrapping them in `to_thread`) | `semantic_search` query expression, full-precision re-sort, and the `random_page_cost` comment (on top of S2) | `OllamaProvider`, `OpenAIProvider`, `get_provider`/client lifecycle, and all of `embed_note` **after** chunking: the post-chunking reuse lookup (before the provider call), the provider call and accounting, `_generation_matches`' reused-row check, and the delete/insert loop |
| `src/services/indexer.py` | — | — | `_index_vault_pinned` and its helpers, `_scan_vault` (new), the backlog read/parse (`_embed_vault_pinned` lines ~3240–3310), `_reconcile_exclusions`, `run_indexer_loop` (backstop scheduling) | the prewarm block: `_HNSW_INDEX_NAME`, `_hnsw_index_exists`, `probe_statement` | — |
| `src/models/db.py` | — | `NoteMetadata.content_tsvector` column line | the four `stat_*` columns (after `modified_at`) and the CHECK, **appended as the last element** of `NoteMetadata.__table_args__`, plus its marker constant | `NoteEmbedding.__table_args__` (drop the `vector` HNSW `Index` if the gate passes), plus a comment on the reloptions above `NoteMetadata.__table_args__` | — |
| `src/config.py` | — | — | `index_stat_shortcut`, `index_full_hash_interval_hours`, immediately after `index_interval_seconds` | — | `ollama_embed_batch_size`, immediately after `ollama_keep_alive` |
| `src/services/vault.py` | `apply_user_vault_row` (new, directly below `warm_user_vault_cache`) | — | — | — | — |
| `src/control_panel/routes.py` | — | — | `_reindex_background` / `trigger_reindex` (force a full-hash pass), plus **one line each** in `reset_embeddings` and `trigger_reembed` calling `indexer.clear_sweep_state()`, placed beside `invalidate_hnsw_index_cache()` and outside the DDL block | `reset_embeddings`' index DDL (through `vector_index`); S4 rebases onto S3's call line and does not move it | — |
| `src/main.py` | — | — | — | — | lifespan shutdown: close the provider client |
| `.env.example` | — | — | the two index settings | — | `OLLAMA_EMBED_BATCH_SIZE` |
| `Makefile` | — | — | — | `db-vacuum-notes` target | — |
| `tests/integration/test_schema_check.py` | — | — | head `025 → 026`, and 026's cases | head `026 → 027`, and 027's cases | — |
| `tests/integration/test_search_recall.py`, `test_prewarm_probe.py`, `test_pgvector_search.py`, `test_issue_206_generation_lock_pg.py` | — | — | — | index name and fixture DDL through `vector_index` | — |
| `docs/architecture/indexing-and-embeddings.md` | — | strike **L10** only | the scan, stat-shortcut, backstop and sweep-gate sections, and the `database.py` idle-in-transaction note | the prewarm bullet | the "Embedding providers" section and the D5 bullet |
| `docs/architecture/schema-and-migrations.md` | — | — | a "026" section after "024" | a "027" section after S3's "026" | — |
| `docs/architecture/search.md` | — | the projection/similarity section | — | the `halfvec` and reloptions sections | — |

**Base check, first thing in every brief.** Agent worktrees branch from `origin/main`, not from local HEAD. Each brief must confirm the following, and stop and report if any check fails:
- `openspec/changes/performance-2026-09/proposal.md` exists.
- The migration head matches the slice's merged prerequisites:
  - **S1 and S2:** `alembic/versions/` ends at `025_oauth_client_last_used.py`, or at 026/027 if S3/S4 have already merged. Neither slice cares which.
  - **S3:** it ends at `025`.
  - **S4:** it ends at `026_note_stat_columns.py`, and S2's projected `semantic_search` is present.
  - **S5:** `026_note_stat_columns.py` exists (S3 merged); the head may be `026` or `027`. `src/services/transport_security.py` defines `embedding_http_client` (#284 merged). `_scan_vault` exists in `indexer.py`.

**Integration tests.** Every slice that changes database behaviour runs `make test-integration`. A local `pytest tests` run skips `tests/integration/`, so it does not count as proof. `make test-integration` and `make test-schema` share a container and port: never run them concurrently.

## 0. Spec review before any code

- [ ] 0.1 Commit this proposal on `perf-proposal` and push. Codex reads only the committed tree. Open the coordination issue/PR now.
- [ ] 0.2 **Codex spec review, before implementation.** Frame it as a defensive PASS/FAIL review of a performance proposal whose failure modes are correctness failures. Tell Codex what "wrong" means here: the consumer is an agent, the vault is the owner's single source of truth, and the expensive failures are a destructive write and a **silently stale or wrong search result**. Ask specifically:
  - (a) Is D11's list of ways a file changes without changing `(size, mtime_ns, ctime_ns, ino)` complete for Linux local filesystems, and is the racy rule correct?
  - (b) Can D9's C4/C5 reconciliation prune, pair or certify a row on the strength of the pre-lock walk under any interleaving with `move_note`, another pass, a reset or a rebuild?
  - (c) Can any route leave a certification-current row disagreeing with the current exclusion patterns while D13's gate skips the sweep?
  - (d) Can D16 write a vector from another generation?
  - (e) Does D2's async commit reach any write whose loss could re-admit a caller or forget a revocation?

  Demand a machine-readable closing verdict block. Run it in the background, with both redirects. Fold the findings into the proposal before dispatching any slice.
- [ ] 0.3 `openspec validate performance-2026-09 --strict` passes.

## 1. Slice S1 — request-path commits (#279), branch `perf-s1-commits`

- [x] 1.1 `auth.py` API-key branch: replace the credential SELECT and the separate `User.is_active` SELECT with one statement that outer-joins `users` for `is_active` and `vault_path` (D3). Keep every refusal reason, body and order exactly as it is: invalid, ownerless, inactive user, expired.
- [x] 1.2 `auth.py` OAuth branch: add the same `users` outer join to the existing token statement. Remove the second `users` read.
- [x] 1.3 `vault.py`: add `apply_user_vault_row(user_id, is_active, vault_path) -> Path | None`, directly below `warm_user_vault_cache`. It must have exactly the single-user warm's write-or-evict semantics. Both auth branches bind its return value to `current_vault_root`. Leave `warm_user_vault_cache` and its other callers unchanged.
- [x] 1.4 `auth.py` `last_used_at` (D1):
  - Add the module constant `LAST_USED_AT_RESOLUTION_SECONDS = 60`.
  - Skip the statement entirely when the loaded value is within that window.
  - Otherwise issue `SET LOCAL synchronous_commit = off`, then the conditional UPDATE (`last_used_at IS NULL OR last_used_at < :cutoff`), then commit.
  - When the UPDATE is skipped, the transaction must still end before the response. It is read-only, so it costs no flush.
- [x] 1.5 `tools.py` `_insert_usage`: issue `SET LOCAL synchronous_commit = off` as the first statement of its transaction, before `session.add`. Change nothing in `write_usage_row` or `_write_usage_row_admitted`.
- [x] 1.6 `quotas.py` `admit`: issue `SET LOCAL synchronous_commit = off` before `ADMISSION_SQL`, and again before the prune, which runs in the next transaction. The docstring must state L1 in one paragraph. Keep the fail-closed behaviour and the event.
- [x] 1.7 `tests/integration/test_perf_async_commit_pg.py` (real Postgres). After each of the three writes, a fresh checkout of the **same pooled connection** reports `SHOW synchronous_commit` = `on`, so the setting did not leak. The quota's concurrency boundary test is repeated with async commit: exactly N of more than N concurrent calls are admitted. `test_issue_162_quotas_pg.py`, `test_usage_log_fk_recovery.py` and `test_issue_193_tool_exception_pg.py` pass unchanged.
- [x] 1.8 `tests/test_perf_auth_bookkeeping.py`, counting statements:
  - An API-key request whose `last_used_at` is fresh issues exactly **one** statement.
  - One whose `last_used_at` is stale issues one SELECT, one `SET LOCAL` and one UPDATE.
  - OAuth issues one statement.
  - Revocation still takes effect on the next request (#66), for each of: deactivating the user, clearing `vault_path`, revoking the key, deleting the `users` row, and revoking the OAuth token.
  - The OAuth code exchange, refresh rotation, revocation and transfer-token paths issue **no** `synchronous_commit` statement. The test sweeps them with a spy.
- [x] 1.9 Docs, in the same branch:
  - `rate-limits.md`: a paragraph under "Gate order" stating that the order is unchanged and that the quota commits asynchronously (L1).
  - `usage-attribution.md`: the meaning of `True` under async commit (L2).
  - `vault-roots-and-tenancy.md`: the warm is folded into the credential read, with the #66 argument from D3.
  - Validate with `make test-integration`.

## 2. Slice S2 — read-path projection (#280), branch `perf-s2-projection`

- [x] 2.1 `models/db.py`: `content_tsvector` gets `deferred=True, deferred_raiseload=True`. Leave `frontmatter` eager, as D5 decided.
- [x] 2.2 `search.py`: project `file_path, title, tags, rank`. Keep the `SET LOCAL`, the predicate and the ordering exactly as they are.
- [x] 2.3 `embeddings.py` `semantic_search`:
  - Project D6's columns, which is the `find_related_stmt` shape.
  - Re-sort by `(distance, file_path, chunk_index)`.
  - Set `similarity = 1 - float(distance)`.
  - Remove the NumPy recomputation and the `numpy` import if nothing else in the module uses it.
  - Keep the staleness, truncation and exact-fallback logic byte for byte.
- [x] 2.4 `tools.py`: project the columns D6 lists for `list_notes`, `get_recent` and `find_orphans`, and add `file_path ASC` after the existing `modified_at` ordering. `find_orphans` keeps `modified_at DESC NULLS LAST`. Project `get_neighborhood`'s hydration as `id, file_path, title, tags`. Rendering must be unchanged.
- [x] 2.5 `tests/test_perf_projection.py`: compile each statement and assert that `content_tsvector`, `embedding` and `frontmatter` are absent from its SELECT list. Also add a raiseload guard: loading `NoteMetadata` and touching `content_tsvector` raises.
- [x] 2.6 `tests/integration/test_perf_projection_identity_pg.py`. On the recall corpus plus a keyword corpus, run the **pre-change** implementations (copied into the test as oracles) and the new ones. They must produce the same result set, the same order, byte-equal non-similarity fields, and similarity within 1e-5, across stale, truncated, filtered, unfiltered and exact-fallback cases. The corpus must deliberately include all three permitted tie cases, and the oracle must accept them and reject every other difference. The three cases are: more notes with an identical `modified_at` (and identical `rank`) than the limit, where membership at the cutoff may differ; two notes with an identical `modified_at` that both fit under the limit, whose relative order may differ; and one note with two chunks at exactly equal distance, where the representative chunk may differ. It must also include orphans with NULL `modified_at`, which must stay last. `test_search_recall.py`, `test_keyword_plan.py` and `test_pgvector_search.py` must pass unchanged.
- [x] 2.7 Docs:
  - `search.md`: a "Read paths project what they render" section, covering D5–D7 and the definition of identical.
  - `indexing-and-embeddings.md`: strike L10 as resolved.
  - Validate with `make test-integration`.

## 3. Slice S3 — scan off the loop, stat shortcut, sweep gate (#278, #282), branch `perf-s3-scan`

- [x] 3.1 `alembic/versions/026_note_stat_columns.py` (`down_revision = "025"`). House shape:
  - A module-level `MARKER` is stamped as a column comment on each of the four columns. Mirror it in `models/db.py`.
  - Add `stat_size`, `stat_mtime_ns`, `stat_ctime_ns`, `stat_ino`, all `BIGINT NULL`, and the CHECK `ck_notes_metadata_stat_all_or_none`, resolved through `pg_constraint` and never by name.
  - It is metadata-only: no backfill, no default.
  - Pin `search_path`, and set and reset `lock_timeout`/`statement_timeout`, as 024 does.
  - `downgrade()` drops only marked columns.
  - Reconcile, don't adopt: a pre-existing column of the same name and a different shape is refused, and the refusal names it.
- [x] 3.2 `models/db.py`: add the four columns and the CHECK, appended last in `NoteMetadata.__table_args__`.
- [x] 3.3 `indexer.py`: add `_scan_vault(...)` (D8), which runs in `asyncio.to_thread`.
  - It performs the walk, the shortcut stat, the read, `fstat`-before-read, and the SHA-256.
  - Retain a body only where D8 says so.
  - A `threading.Event` checked between files provides stop-on-cancel.
  - Record the racy-stat decision (D10), with `STAT_RACY_WINDOW_NS = 2_000_000_000`, measured against `t_start = time.time_ns()` taken immediately **before** the pre-read `fstat`, not after the read. A future timestamp is also racy.
  - Convert `stat_ino` to signed 64-bit.
- [x] 3.4 `indexer.py` `_index_vault_pinned` restructure (D9):
  - The provenance reconcile runs first, as today.
  - The pre-walk snapshot is read in its own session and **committed**.
  - Then comes `_scan_vault`.
  - Then the locked transaction opens: `acquire_generation_lock_unbounded` and `_assert_fts_generation_current` first, then the locked re-read `L`, then per-path reconciliation (C4: re-process under the lock with `to_thread`) and deferral (C5: never prune or pair a row whose `L` differs from `S`; under a re-derive, append a skip).
  - Keep every existing mutation below the lock.
  - Add the unchanged-hash stat refresh as a conditional UPDATE (`id + file_path + content_hash`).
  - Carry the new path's stat on the id-preserving move.
  - Carry the stat on the upsert (insert values and `on_conflict` set).
  - Keep the single commit.
- [x] 3.5 `indexer.py` backstop (D12): add `_last_full_hash[scope]`, which is monotonic and in memory. It is set **only when a full-hash scan pass for that scope commits with an empty `skips` list** (every discovered file read and hashed). An aborted, refused or cancelled full-hash pass, or one that commits with any skipped path, leaves the scope due. Test: a committed full-hash pass containing one unreadable file leaves the scope due, and the next pass is a full-hash pass, so the next pass is again a full-hash pass. The first pass for a scope after process start is a full-hash pass, and so is every pass once `INDEX_FULL_HASH_INTERVAL_HOURS` have elapsed since the last successful one. `control_panel/routes.py`'s reindex forces one. A full-hash pass also forces the sweep (task 3.7).
- [x] 3.6 `indexer.py`: move the embed-path work to `to_thread`. That covers the backlog's `read_note_beneath` + `_content_hash` + `parse_frontmatter`, the sweep probe's read, hash, parse, `clean_for_embedding` and `chunk_text_bounded`, and, in `embeddings.py` `embed_note`, `clean_for_embedding` + `chunk_text_bounded`.
- [x] 3.7 `indexer.py` `_reconcile_exclusions` gate (D13):
  - Add `_swept[scope]` in memory.
  - Skip the sweep when it equals the current pattern fingerprint and the pass is not a backstop pass.
  - Set it only on a **clean** completion, meaning no pause, no budget stop, no exception, no provider failure, no read failure, no `StaleCertification` and **no hash-mismatch skip**. Only zero-chunk rows do not block it.
  - Clear it on a re-derive, and through `clear_sweep_state()` on the two in-process reset routes (`reset_embeddings`, `trigger_reembed`; see the region table).
- [x] 3.8 `tools.py` `move_note`: the `nm_update` `.values(...)` sets the four stat columns to NULL.
- [x] 3.9 `config.py` and `.env.example`: add `index_stat_shortcut: bool = True` and `index_full_hash_interval_hours: int = Field(24, ge=1)`, with the D11/perf-L3 guidance for network, FUSE and FAT mounts.
- [x] 3.10 `tests/test_perf_scan_offload.py`:
  - The scan's read and hash execute on a non-main thread, asserted inside the patched read.
  - `parse_frontmatter`, `clean_for_embedding` and `chunk_text_bounded` are dispatched through `to_thread` on all three embed paths.
  - **The /health acceptance criterion:** with the read patched to block for 2 s per file, a concurrent coroutine completes at least one iteration and `/health`, served by the test client, answers while the scan is in progress. This is a binary progress assertion, not a timing bound.
  - Cancellation sets the stop event and returns within one file.
- [x] 3.11 `tests/test_perf_stat_shortcut.py`:
  - Unchanged stat → no read. Changed stat with the same hash → stat refreshed, no upsert. Changed stat with a new hash → upsert.
  - Each of these reads: a NULL stat, a stale extraction marker, a re-derive, a backstop pass, and `INDEX_STAT_SHORTCUT=false`.
  - A racy stat, whether in the future or within 2 s before `t_start`, is recorded NULL.
  - **Slow-read, same-tick rewrite.** Freeze the file timestamps with a fake clock, so the rewrite shares the tick. Patch the read to rewrite already-read bytes at the same size mid-read and then block for more than 2 s. The row's stat must be recorded NULL, and the next pass must index the rewritten bytes.
  - A backstop pass that aborts before committing leaves the scope due: the next pass is again a full-hash pass.
  - A retargeted symlink is re-read.
  - Same-size in-place rewrite with a forced identical stat (monkeypatched `os.stat`) is missed until the backstop, then picked up. This documents perf-L3.
  - `move_note` NULLs the stat.
- [x] 3.12 `tests/integration/test_perf_scan_pg.py` (real Postgres):
  - A `move_note` committed between the snapshot and the lock does not prune the moved row (C5), and the next pass settles it.
  - Another process's committed upsert between the snapshot and the lock is re-decided under the lock (C4).
  - No transaction is open during the walk. Assert this via `pg_stat_activity` from a probe connection while the patched scan blocks.
  - The generation lock is the first lock the pass transaction takes. Extend `test_issue_206_lock_ordering_pg.py` so it still passes.
  - A reset running concurrently with a walk neither deadlocks nor lets old decisions land.
  - The sweep is skipped on a second clean pass, runs again after a provider failure, and runs on a backstop pass.
  - **A→B→A.** An excluded note (content A, certified with zero vectors) has its pattern removed, then a restart. The scan reads A. Before the sweep reaches the note it is saved as B, so the sweep skips it on a hash mismatch. It is restored to A before the next scan. The sweep must not record clean, the next pass must sweep again, and the note must end up embedded. `test_issue_127_exclusion_reconciliation_pg.py` passes with the gate in place: its assertions run on backstop or first passes.
- [x] 3.13 `tests/integration/test_schema_check.py`:
  - Raise `HEAD_REVISION` to `026`.
  - Add 026's marker, drift, downgrade, stamp-back and impostor cases (a same-named column of the wrong type is refused), plus a CHECK case resolved through `pg_constraint`.
  - Keep every earlier case.
- [x] 3.14 Docs:
  - `indexing-and-embeddings.md`: sections for D8, D9 (with C1–C8 verbatim), D10–D13, and perf-L3/perf-L4/perf-L7. Update the "Indexer runs on startup then every 5 minutes, hash-based change detection" bullet, and correct `database.py`'s idle-in-transaction comment. That comment's claim that the pass "holds one transaction … across the whole synchronous walk" stops being true. Coordinate with #284 if it has touched the file.
  - `schema-and-migrations.md`: a "026" section.
  - Validate with `make test-schema`, then `make test-integration`.

## 4. Slice S4 — database: migration 027 (#283), branch `perf-s4-db`, after S2 and S3 merge

- [x] 4.1 `src/services/vector_index.py` (D19). Contents:
  - `INDEX_NAME = "ix_note_embeddings_embedding_halfvec_hnsw"` and `LEGACY_INDEX_NAME = "ix_note_embeddings_embedding_hnsw"`.
  - `index_enabled(dim)`, true when `dim ≤ 2000`.
  - `create_index_sql(dim)`, with `m = 16, ef_construction = 64`.
  - `drop_index_sql()`, which drops both names `IF EXISTS`.
  - `order_expr(query_vec)`: the `halfvec(dim)` cast expression when enabled, else the plain `cosine_distance`.
  - `full_distance_expr(query_vec)`.
- [x] 4.2 **The recall gate, before 027 is written with the index.**
  - Adapt `tests/integration/test_search_recall.py`, `test_prewarm_probe.py` and `test_pgvector_search.py` to build and name the index through `vector_index`.
  - Keep the recall test's exact baseline a **full-precision `vector` sequential scan**.
  - Run `make test-integration`.
  - **Pass** (recall ≥ 0.9 on each of three rebuilds, every filter shape, and `find_related`): continue with 4.3–4.6 including the index.
  - **Fail**: drop the index and the query casts from this slice. Record the measured recall in design D19 and in `search.md` under "Rejected: halfvec index (recall)". Continue with the reloptions only.
- [x] 4.3 `alembic/versions/027_notes_vacuum_and_halfvec.py` (`down_revision = "026"`):
  - `ALTER TABLE notes_metadata SET (autovacuum_vacuum_scale_factor = 0.02, autovacuum_vacuum_insert_scale_factor = 0.02)`.
  - If the gate passed and `index_enabled(dim)`: under `SET LOCAL maintenance_work_mem = '512MB'` and a build-sized `statement_timeout`, `create_index_sql(dim)`, then drop the legacy index.
  - No VACUUM (D18).
  - `downgrade()` resets the reloptions, recreates the legacy `vector` index when `dim ≤ 2000`, and drops the new one.
  - Pin `search_path` as 024 does.
- [x] 4.4 Query side, if the gate passed.
  - `embeddings.py` `semantic_search` and `tools.py` `find_related_stmt`/`find_related_impl`:
    - order by `order_expr`;
    - also select `full_distance_expr`;
    - re-sort by full distance before the dedupe;
    - `similarity = 1 - full_distance`.
  - `indexer.py` prewarm: `probe_statement` orders by `order_expr(_probe_vector())`, and `_hnsw_index_exists` looks up `vector_index.INDEX_NAME`.
  - `control_panel/routes.py` `reset_embeddings` and `scripts/reset_embeddings.py`: drop and create through `vector_index`.
- [x] 4.5 `alembic/env.py`: add an `include_object` hook that excludes exactly the index named `vector_index.INDEX_NAME`, with `type_ == "index"`. Pass it to both `context.configure` calls. PR #284 also edits this file, in `run_async_migrations`; rebase onto whichever landed first.
- [x] 4.6 `models/db.py`: remove the legacy `Index` from `NoteEmbedding.__table_args__` if the gate passed. Add the reloptions comment above `NoteMetadata.__table_args__`, explaining that Alembic does not compare reloptions and the schema gate asserts them.
- [x] 4.7 `embeddings.py`: correct the `random_page_cost` comment (D20).
- [x] 4.8 `Makefile`: add a `db-vacuum-notes` target that runs `VACUUM (ANALYZE) notes_metadata` in an autocommit session through the application container, with a `make help` line.
- [x] 4.9 `tests/integration/test_schema_check.py`:
  - Raise `HEAD_REVISION` to `027` and keep `026` in the chain.
  - Add 027's cases: reloptions via `pg_class.reloptions`; the index via `pg_get_indexdef`, `indisvalid` and the opclass `halfvec_cosine_ops` at the configured dimension; the legacy index absent; downgrade restores the legacy index; stamp-back idempotence.
  - `alembic check` is clean at head.
- [x] 4.10 `tests/test_perf_vector_index.py`:
  - `include_object` excludes exactly one name.
  - `index_enabled` is false above 2000, and then the queries use the plain expression.
  - The query's order expression compiles to text identical to the index expression.
  - The full-precision re-sort precedes the dedupe.
- [x] 4.11 Docs:
  - `search.md`: the `halfvec` decision (or its rejection with the measured recall), the full-precision re-rank, D17, D18, and the corrected `random_page_cost` rationale.
  - `schema-and-migrations.md`: a "027" section, covering the `include_object` exclusion and why, and the catalogue verification.
  - `indexing-and-embeddings.md`: the prewarm bullet.
  - Validate with `make test-schema`, then `make test-integration`.

## 5. Slice S5 — provider transport, batching, chunk reuse (#281), branch `perf-s5-provider`, after #284 and S3 merge

- [ ] 5.1 `embeddings.py`: add one shared client per provider, **built only by calling `transport_security.embedding_http_client(timeout)`** (D14).
  - Create it lazily. Key it to the running loop, and rebuild it if the loop changed.
  - Pass per-request `timeout=` on each call: 30 s for Ollama, 60 s for OpenAI.
  - Add `close_provider_client()`.
  - `tests/test_internal_transport_http_clients.py`'s AST sweep must stay green, with no new exemption.
- [ ] 5.2 `main.py` lifespan: in shutdown, `await close_provider_client()` after the indexer task is cancelled and before the engine is disposed.
- [ ] 5.3 `OllamaProvider.embed_batch` (D15):
  - Split into consecutive slices of `settings.ollama_embed_batch_size`.
  - Send one `/api/embed` request per slice with an `input` array, under `asyncio.wait_for(..., 30.0)`.
  - Check cardinality per slice.
  - Add no aggregate deadline.
  - `embed_one` sends a one-element array.
  - Keep the input-limit translation.
- [ ] 5.4 `config.py` and `.env.example`: add `ollama_embed_batch_size: int = Field(16, ge=1, le=256)`, noting that `1` gives the pre-change request shape.
- [ ] 5.5 `embed_note` reuse (D16):
  - The lookup of `(id, chunk_text, embedding)` and of the stored fingerprint runs in a read-only transaction that is **committed before the provider call on every path**.
  - Reuse only when the fingerprint is present and equal.
  - Send only the new texts, and check cardinality over that subset. Certify only on full coverage of the requested list.
  - `on_provider_call(len(subset))`, and nothing at all when the subset is empty. A `GENERATION_MISMATCH` counts as an attempt only if a provider call was issued (the MODIFIED interlock requirement).
  - Under `_generation_matches`, verify that every reused row id still exists with the same text. If not, return `GENERATION_MISMATCH`.
  - Then certify, delete and reinsert all rows in order, as today.
- [ ] 5.6 `tests/test_perf_provider_batching.py`:
  - 40 chunks at batch 16 → three requests of 16, 16 and 8, in order.
  - A short response is refused.
  - A hung request fails at 30 s, and so does the next slice.
  - Exactly one client is built per loop.
  - The client is closed at shutdown.
  - The factory's `trust_env=False` and `follow_redirects=False` properties are observed on the shared instance.
- [ ] 5.7 `tests/integration/test_perf_chunk_reuse_pg.py` (real Postgres):
  - An append-only edit sends only the new tail chunk to a counting provider, and the result equals a from-scratch embed, apart from the reused vectors themselves.
  - With the fingerprint absent, nothing is reused.
  - With a fingerprint mismatch, nothing is reused and nothing is certified.
  - A reset committed during the provider call → `GENERATION_MISMATCH`, nothing written, and no deadlock. Reuse the `test_issue_206_generation_lock_pg.py` reset driver.
  - A metadata-only edit that yields identical chunks makes no provider call, certifies, and leaves `attempted` unchanged.
  - **All-reuse reset race.** Every chunk is reusable, and a reset deletes the rows between the lookup and the under-lock check. The outcome is `GENERATION_MISMATCH`, nothing is written, and `attempted` is unchanged.
  - The budget is debited by the subset only.
- [ ] 5.8 Docs, `indexing-and-embeddings.md`:
  - The "Embedding providers" section: the shared client through the factory, and batching.
  - Rewrite the #127 D5 bullet for per-request batches: why a fixed size keeps the bound constant.
  - A "Chunk-vector reuse" subsection, covering D16 and L6.
  - Validate with `make test-integration`.

## 6. Merge and combined gates (supervisor)

- [ ] 6.1 Merge in dependency order: S1 and S2 (any order), then S3, then S4, then S5 once #284 has merged. After each merge, resolve the region seams named above.
- [ ] 6.2 Check the seams by grepping for production callers of every new export: `apply_user_vault_row`, `_scan_vault`, `vector_index.*`, `close_provider_client`, the two index settings and the batch setting. None may be green-but-unwired.
- [ ] 6.3 On the merged tree, run once: `make test-schema`, then `make test-integration`, then the offline suite, then `make audit`. These are authoritative; the per-worktree runs are not.
- [ ] 6.4 Supervisor: update the CLAUDE.md key decisions with one bullet per surface: async bookkeeping commits (L1/L2), the stat shortcut and its backstop (perf-L3), and the `halfvec` index if it shipped.

## 7. Verification by non-authors

- [ ] 7.1 `openspec-verifier` against the merged tree. Iterate until there are zero blocking gaps.
- [ ] 7.2 **Adversarial Codex, mandatory.** The change touches search correctness and the embedding path. Give Codex the design's "Adversarial review focus" list verbatim, the requirements and the changed files. Tell it to attack, framed as a defensive PASS/FAIL control review. Two rounds by default. Triage the findings with the workflow's three questions. Record declined findings in the Accepted limitations.

## 8. Deploy

- [ ] 8.1 Deploy S1 and S2 together (no migration) with `make deploy`.
- [ ] 8.2 Deploy S3 (026) with `make deploy`, then `make db-check`, which must be clean.
- [ ] 8.3 Deploy S4 (027) with `make deploy`, then `make db-check`, which must be clean.
- [ ] 8.4 Deploy S5 with `make deploy`.

## 9. Live checks (end-to-end in place of `user-representative`; name the tools actually called)

- [ ] 9.1 After S1: API-key `read_note` arrival→dispatch p50 drops from ~51 ms toward the OAuth path's ~11 ms. `usage_logs` rows still land. Revoking a test key refuses its next call.
- [ ] 9.2 After S2: `semantic_search`, `keyword_search`, `list_notes`, `get_recent`, `find_orphans` and `get_neighborhood` return the same results as before the deploy for a fixed set of queries captured beforehand. `semantic_search` `db_ms` drops.
- [ ] 9.3 After S3:
  - Poll `/health` every 1 s through the first (full-hash) pass after the restart. The largest gap between answers must be < 5 s; it was 235 s.
  - The second pass reads only changed files, which the logs show.
  - Edit a note, and it is searchable after the next pass.
- [ ] 9.4 After S4:
  - `pg_stat_user_tables.last_autovacuum` or `last_vacuum` for `notes_metadata` is non-NULL within ~5 min; otherwise run `make db-vacuum-notes`.
  - The index is valid, and `semantic_search` EXPLAIN uses it.
  - The index size is about half of 146 MB.
- [ ] 9.5 After S4, informational: for 30 real queries, the top-15 notes with the `halfvec` path vs an exact full-precision scan have mean set overlap ≥ 0.9. Record the number in `search.md`. Below 0.9, the supervisor decides whether to roll forward with a migration that restores the legacy index.
- [ ] 9.6 After S5: an append to a large note triggers a single provider request, visible in the logs. A full tick's embed time for one changed note drops. No new connection per chunk, which the Ollama logs show.

## 10. Archive

- [ ] 10.1 `openspec archive performance-2026-09 -y`. Then commit and push, closing #278–#283 with `Closes #N`, or leaving #283 open for its host-ops half.
