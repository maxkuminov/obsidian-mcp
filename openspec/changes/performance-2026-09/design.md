## Context

Six issues from the 2026-09-22 performance audit (#278–#283). Every file:line claim in the issues was re-verified against `f2ec082` before it was written here; where the line drifted, the verified line is given.

| Claim | Verified at | Note |
| --- | --- | --- |
| Generation lock taken before the walk | `indexer.py:1808–1809`, walk `:1854–1870` | `acquire_generation_lock_unbounded` + `_assert_fts_generation_current` at the head of `_index_vault_pinned`'s one transaction; `read_note_at` + `_content_hash` on the loop, no `to_thread` |
| `read_note_at` stats before it reads | `indexer.py:1525–1548` | `os.fstat(fd)` then `handle.read()` on the same descriptor; follows a leaf symlink |
| Embed-path cleaning on the loop | `embeddings.py:1007–1013` (`embed_note`), `indexer.py:3262/3286/3296` (backlog), `:3601–3627` (sweep probe) | link extraction and `extract_tags` are already `to_thread` |
| `last_used_at` UPDATE + COMMIT per request | `auth.py:400–406` | preceded by the credential SELECT and a separate `users.is_active` SELECT (`:368–370`); `warm_user_vault_cache` then re-reads `users` in a *second* transaction (`:441–444`, `vault.py:128`). OAuth reads `users` twice (`:508–510`, `:606–609`) |
| Usage INSERT + COMMIT awaited | `tools.py:243–253` (`_insert_usage`), reached from `_write_usage_row_admitted` `:351/:379` | |
| Quota upsert commits | `quotas.py:343–347` (`admit`) | |
| Whole-entity loads | `embeddings.py:1207`, `search.py:40`, `tools.py:2133, 2202, 3475, 3777`; path lookups `:3189, 3262, 3399, 3587`; indexer `:3300, :3634` | `find_related_stmt` (`tools.py:3548`) already projects |
| NumPy similarity | `embeddings.py:1306–1308` | order is already DB-distance order; only the *value* is recomputed |
| Per-chunk client | `embeddings.py:539–590`, `:614` | cardinality check `embeddings.py:1091` |
| Sweep runs every tick | `indexer.py:3461` (call), `:3526–3540` (EXISTS query) | |
| `random_page_cost` "SSD" comment | `embeddings.py:1182` | `search.py`'s own comment is correct (detoast-cost rationale) |

Two facts found while verifying shape the design and are not in the issues:

1. **The embedding column's dimension is configuration, not a constant.** `Vector(settings.embedding_dimensions)` (`db.py:505`); migration 008, `scripts/reset_embeddings.py:80–137` and `control_panel/routes.py:2817–2880` all create `ix_note_embeddings_embedding_hnsw` only when the dimension is ≤ 2000 and skip it above. The prewarm probe (`indexer.py:5178–5243`) and three integration tests name that index. Any index change must go through all of them, and the `halfvec(1024)` literal in #283 is wrong for an OpenAI deployment (1536 or 3072).
2. **Alembic 1.19 on SQLAlchemy 2 *does* compare expression indexes on PostgreSQL** (`alembic/ddl/postgresql.py`: `_skip_functional_indexes` only runs when `not sqla_2`). An expression index present in the database but absent from the models is reported as a drop, so `alembic check` would go dirty. Table reloptions are not compared at all.

## Goals / Non-Goals

**Goals**
- No fsync on the hot path of an admitted `/mcp` call, while revocation stays fresh per request and every audit/quota contract is preserved except crash durability, which is declared.
- Read tools ship only what they render; results do not change except where sort keys are exactly tied (D6).
- The event loop never runs whole-vault filesystem work or large-note cleaning; `/health` answers throughout a cold pass.
- An idle tick does work proportional to what changed, with every shortcut bounded by a periodic full re-verification.
- Embedding cost proportional to changed chunks, over pooled connections.
- `notes_metadata` actually vacuumed; a smaller vector index only if it demonstrably keeps recall.

**Non-Goals** — see the proposal's Out of scope. Also: no change to the embed backlog's selection predicate, `certify_embedded`, the generation-lock key or its ordering rule, `_tracked`'s gate order, or any tool's output format.

## Decisions

### #279 — the request path

**D1. `last_used_at` is written at most once per 60 s per key.** The loaded row carries `last_used_at`; when it is non-NULL and newer than `now − 60 s`, no statement is issued. Otherwise the UPDATE is conditional — `WHERE id = :id AND (last_used_at IS NULL OR last_used_at < :cutoff)` — so two concurrent requests that both saw an old value serialise for microseconds on the row and the second re-evaluates its predicate (EvalPlanQual) to a no-op instead of waiting for a flush. The 60 s is a module constant (`LAST_USED_AT_RESOLUTION_SECONDS`), not a setting: nothing but the panel's display reads the column (`api/routes.py:216`, `control_panel/routes.py:1042`, `keys.html:108`), and a knob for display resolution is noise. Rejected: a Python-only check (concurrent duplicates still take the row lock in turn); an SQL-only predicate (every request would still issue a statement and open a write transaction).

**D2. Three writes commit asynchronously; everything that grants or revokes stays synchronous.** `SET LOCAL synchronous_commit = off` is issued inside the same transaction, before the first write statement, for: the `last_used_at` UPDATE (D1), `_insert_usage` (both the initial insert and the FK-cleared retry, because both go through it), and `quotas.admit`'s admission statement *and* its prune. Semantics, stated precisely because "committed" is load-bearing in three contracts:

- An asynchronously committed transaction is **visible to every other session at commit**, exactly as a synchronous one; only its WAL flush is deferred (by up to `3 × wal_writer_delay`, ~600 ms at defaults). So the quota's concurrency boundary (`quota_counters`' conditional increment under row lock) is still exact, and `write_usage_row` still returns `True` only after a commit that other sessions can read.
- What changes is **durability across a PostgreSQL server crash or host power loss** — not an application crash or restart, which lose nothing committed. Such a crash can lose at most the last ~600 ms of these three kinds of write. It cannot corrupt, and it cannot lose a later *synchronous* transaction while keeping an earlier async one (WAL is flushed in order).
- `SET LOCAL` dies with the transaction, so a pooled connection cannot carry `synchronous_commit = off` into the next checkout. That is asserted against a real server (task 1.7), not assumed.

Owner decision taken by default and recorded as an accepted limitation (L1): the quota part means a crash can lose admitted increments — **an undercount in the caller's favour**, never an overcount, never a refusal that should not have happened.

**Stays synchronous** (no `SET LOCAL`, and a test pins that the statement is absent): OAuth code issue/exchange, refresh rotation, revocation, grant-family revocation, DCR, API-key create/revoke/delete, panel login/session mint/revoke, transfer-token mint/redeem/publish, `users` edits, every indexer and maintenance write, `indexer_runs`. The rule: a write whose loss after a crash could **re-admit** something or **forget a revocation** is synchronous; only bookkeeping whose loss undercounts is async.

**D3. The `users` row rides the credential SELECT, and the refresh semantics are unchanged.** The API-key lookup becomes one statement: `APIKey` outer-joined to `users` on `users.id = api_keys.user_id`, selecting `users.is_active` and `users.vault_path`. The OAuth statement gains the same outer join beside its existing `oauth_clients` join. A new helper in `src/services/vault.py`, `apply_user_vault_row(user_id, is_active, vault_path) -> Path | None`, performs **exactly** `warm_user_vault_cache`'s single-user write-or-evict (evict when inactive, absent or unassigned; write and return the root otherwise), and the middleware binds its return value to `current_vault_root` as today. `warm_user_vault_cache` keeps its signature and its other callers (the indexer's bulk warm, the panel). Why this preserves #66: the requirement is a *fresh per-request read* whose answer is bound to the request and outranks the process-global dict. The joined columns are read in the same request, from the same snapshot as the credential — marginally *earlier* than today's separate warm (by one statement), which no revocation contract distinguishes; the per-request binding and the bulk-warm-cannot-re-admit property are untouched. The inactive-user refusal keeps its reason code and its body: a missing `users` row outer-joins to NULL, and `is_active is not True` refuses, as `scalar_one_or_none() is not True` does today. Net: the API-key path goes from two transactions / three SELECTs / one sync commit to **one transaction / one SELECT / at most one async UPDATE**; OAuth from three statements to one.

**D4. Gate order and every return contract are unchanged.** The rate buckets stay first and the quota admission last pre-body (`rate-limits.md` "Gate order"); a rate-refused call consumes nothing durable. `write_usage_row`'s return value, the writer lease, the FK retry and the #193 coalescer requeue are untouched — D2 changes only what happens inside `_insert_usage`'s transaction.

### #280 — read paths ship what they render

**D5. `content_tsvector` is `deferred(raiseload=True)`; `frontmatter` is assessed and left alone.** `mapped_column(TSVECTOR, nullable=True, deferred=True, deferred_raiseload=True)`. Raise-on-load, not lazy-load: under `AsyncSession` a lazy load is an implicit-IO error anyway, and raising names the offending access in a test instead of surfacing as `MissingGreenlet` in production. A repository grep finds **no** entity read of `.content_tsvector` (every writer is SQL text or `insert().values`, both unaffected by deferral). `frontmatter` is **not** deferred: it is filtered in SQL (`apply_note_filters`), read by `move_note`'s title fallback through an explicit column, and small on the median note; every hot read path in D6 projects it away anyway, so deferring it would buy only the whole-entity lookups (path lookups in graph tools, the indexer's per-note `select(NoteMetadata)`), at the cost of turning any future entity reader into a runtime error on a write path.

**D6. Explicit projection on six paths, and exact ties become deterministic.**

| Path | Projected columns |
| --- | --- |
| `semantic_search` | `ne.note_id, ne.chunk_index, ne.chunk_text, nm.file_path, nm.title, nm.tags, nm.content_hash, nm.embedded_content_hash, nm.chunks_truncated, distance` — the `find_related_stmt` shape |
| `keyword_search` | `nm.file_path, nm.title, nm.tags, rank` |
| `list_notes` | `file_path, file_size, modified_at` |
| `get_recent` / `find_orphans` | `file_path, title, tags, modified_at` |
| `get_neighborhood` hydration | `id, file_path, title, tags` |

No predicate, `SET LOCAL`, overfetch or exact-fallback condition changes. `ts_rank_cd` still detoasts the tsvector *server-side* for matching rows; what is removed is shipping and hydrating it. Tie-breaks: `list_notes` and `get_recent` gain `file_path ASC` after `modified_at DESC`; `find_orphans` keeps its existing `modified_at DESC NULLS LAST` and gains `file_path ASC` after it (today an exact `modified_at` tie is resolved by whatever the sort saw first). `semantic_search`'s in-Python re-sort keys on `(distance, file_path, chunk_index)` instead of `distance` alone. These change output **only** where sort keys are exactly equal, and in exactly two visible ways, both permitted (Codex spec review r1):
- **membership at a tied LIMIT cutoff**: when more rows share the boundary `modified_at` (or `rank`) than fit under the limit, which of them survive can change;
- **the representative chunk among exact distance ties**: when two chunks of one note have exactly equal distance, the kept `chunk_index` and its preview can change.

The identity oracle in task 2.6 covers both cases explicitly and asserts them as permitted differences, not failures.

**D7. `similarity = 1 − distance`, as `find_related` already does.** The result order was always the database's distance order (the NumPy value was computed *after* the sort), so membership and order are unchanged; the reported value differs from the NumPy recomputation by float error (pgvector accumulates in single precision, NumPy in double) — ~1e-6, visible at most as a last-digit flip in the 3-decimal rendering. What this *fixes*: the displayed similarity can no longer be non-monotone against the displayed order. `L10` in `indexing-and-embeddings.md` is resolved by this and is struck through in the same change. "Identical" for the whole slice is therefore defined as: same result set and same order, except (a) membership at a cutoff where the sort key is exactly tied and (b) the representative chunk among exact distance ties within one note, and (c) the relative order of rows whose sort key is exactly equal, even when all fit under the limit (Codex r2); every other non-similarity field byte-equal; similarity within 1e-5.

### #278 — off the loop, and out from under the lock

**D8. The scan runs in one worker-thread call; the embed paths offload per note.** A synchronous `_scan_vault(root_fd, snapshot, *, force_read, re_derive, stop) -> ScanResult` runs the whole `discover_markdown_files_at` walk, the stat, the read and the hash in `asyncio.to_thread`. It returns, per discovered path, `(rel, stat, hash, body | None)` plus the `skips` list; a body is retained only where the caller will need it (hash differs from the snapshot, path absent from it, `re_derive`, or stale extraction marker) — the same set `path_to_content` holds today. The pinned `root_fd` and the per-directory descriptors are plain integers usable from the thread; the walk's one-descriptor-per-depth property is unchanged because the generator is drained inside the thread. `parse_frontmatter` for each retained body, `extract_tags` and the grammar check are dispatched per changed note (the latter two already are). On the embed paths — the backlog (`indexer.py:3262–3296`), the sweep probe (`:3601–3627`) and `embed_note`'s `clean_for_embedding` + `chunk_text_bounded` (`embeddings.py:1007–1013`) — the read, hash, parse, clean and chunk each go through `to_thread`. **Cancellation:** a thread cannot be cancelled, so `_scan_vault` checks a `threading.Event` between files; the awaiting coroutine sets it on `CancelledError` and re-raises, so lifespan shutdown waits for at most one file, not a 177 s walk. The honest bound from the existing extraction note applies: a thread yields the GIL between bytecodes and I/O, and SHA-256 and `read()` release it, so the loop is free during the I/O-bound walk; a single pathological `re` step is still one step.

**D9. The walk moves ahead of the generation lock. The constraints, and how each is kept.** Read from `indexer.py:1733–2310` and `indexing-and-embeddings.md` "The index generation lock (#206)":

- **C1 — advisory before any row or table lock** (D7c3). The pass transaction's first lock-taking statement stays `acquire_generation_lock_unbounded`. Unchanged: the walk takes no database lock at all.
- **C2 — no table lock held across an advisory wait.** A plain SELECT holds `AccessShareLock` until transaction end ("Discovery SELECTs end their transaction before provider work"). So the pre-walk snapshot — `file_path, content_hash, extraction_version` and the four stat columns, owner-scoped — is read in **its own session and committed before the walk starts**. It is never held open across the walk or the lock wait.
- **C3 — one commit for one filesystem snapshot.** Every mutation (move UPDATE, upsert, grammar invalidation, prune, link rebuild, tsvector write, stat refresh, provenance stamp) stays in the single locked transaction and commits together, so a failure cannot pair a new hash with stale derived rows. Unchanged.
- **C4 — decisions are made against the locked state.** Under the lock the pass re-reads the same owner-scoped rows (`L`). For every walked path whose `L` row equals its snapshot row (`S`) in presence, hash, extraction version and stat, the walk's result stands. For any path where they differ — another process's pass, `move_note`, or a reset committed in between — the path is **re-processed under the lock** (read + hash via `to_thread`, decided against `L`). `existing`, move detection and `deleted_paths` are computed from `L`, never from `S`.
- **C5 — a row that changed since the snapshot is never pruned by an older walk.** A path in `L` that the walk did not see and whose `L` row differs from `S` (including "absent from `S`" — e.g. a `move_note` that landed mid-walk) is **deferred**: not pruned, not paired as a move, left for the next pass. Under a re-derive a deferral is appended to `skips`, so A.7a withholds the stamp exactly as for any other unprocessed path. Today's pass has the same race at a narrower width (the walk and `move_note` already interleave); this rule makes the wider window no worse.
- **C6 — provenance and the FTS fingerprint.** `_reconcile_provenance` still runs first in its own committed session and decides `re_derive` before the walk; `_assert_fts_generation_current` still runs immediately after the lock. Unchanged.
- **C7 — `index_pass_lock` covers the whole pass**, walk included, so no two passes in one process interleave. Unchanged.
- **C8 — the quarantine snapshot is consulted before the pass** (`_refuse_quarantined_pass`). Unchanged.

Safety argument: the walk is filesystem-only and was never protected by the database lock (a file can change under a locked walk today); the lock protects *rows*, and every row decision is still taken under it against rows read under it. What moves is only the time at which bytes are read, which C4/C5 reconcile. What this buys: the lock and the open transaction are held for the mutation phase (seconds) instead of the walk (up to minutes), so `make reset-embeddings` / `make rebuild-tsvectors` wait less (L5b shrinks), and no transaction sits idle holding back the vacuum horizon for the walk's duration. If review finds C4/C5 insufficient, the fallback is D8 alone (walk offloaded but still under the lock) — the loop-freeze fix does not depend on D9.

### #282 — the stat shortcut and its backstop

**D10. What is recorded, when, and when it is trusted.** Migration 026 adds `stat_size BIGINT`, `stat_mtime_ns BIGINT`, `stat_ctime_ns BIGINT`, `stat_ino BIGINT` (inode reinterpreted as signed 64-bit, `ino − 2**64` when `ino ≥ 2**63`), all nullable, plus a CHECK that they are all NULL or all non-NULL. `file_size`/`modified_at` are **not** reused: `modified_at` has microsecond precision and both keep their display meaning.

- **Source.** The recorded tuple is always the `os.fstat` of the descriptor whose bytes were hashed, taken **before** the read (as `read_note_at` does today). A write that lands after the fstat changes the stat, so the next pass re-reads — the failure direction is a wasted read, never a stale row. The shortcut comparison uses `os.stat(name, dir_fd=parent_fd)` — following a leaf symlink, as the read does — so a retargeted `.md` symlink presents its new target's inode.
- **Racy stats are not recorded, and recency is measured at read start** (Codex spec review r1, MAJOR). Let `t_start` be the wall-clock instant (`time.time_ns()`) taken immediately **before** the pre-read `fstat`. If `max(mtime_ns, ctime_ns) ≥ t_start − STAT_RACY_WINDOW_NS` (2 s), or either timestamp is later than `t_start` (a future timestamp), the row's stat columns are written NULL, so the next pass hashes it (git's racy-clean rule). Measuring at *hash completion* was wrong: a slow read (cold HDD, scheduling delay) could take more than 2 s, so a timestamp that was fresh when the read began looked old by the time it ended, even though a writer had rewritten already-read bytes within the same timestamp tick. Measured at read start, the argument is closed: any write that lands after `t_start` either gets a timestamp in a later tick (a changed stat, so the file is re-read) or shares the recorded timestamp's tick, which by the rule is within 2 s of `t_start` and therefore never trusted. The kernel stamps files from the coarse realtime clock, which lags `time.time_ns()` by at most one tick, and the 2 s window absorbs that.
- **Eligibility.** A path skips its read iff: `INDEX_STAT_SHORTCUT` is on, the pass is not a backstop pass, not `re_derive`, the row's extraction marker is current, the row's stat is non-NULL, and all four fields equal. Otherwise it is read and hashed exactly as today.
- **Refresh.** When a read's hash equals the row's but the stat differs or is NULL (a `touch`, a no-op save, the first pass after 026), the pass writes the new stat with a conditional UPDATE (`WHERE id AND file_path AND content_hash`) inside the locked transaction; without it such a file would be re-read every tick. New and changed rows get the stat in their upsert; the id-preserving move gets the new path's stat. `move_note` writes NULL to the stat columns (belt and braces: carrying them would be sound, since the stat names an inode state, but one extra read per move buys not having to argue it).

**D11. Every way a file can change without changing those four fields, and what bounds it.** The expensive failure is a silently stale row; this is the complete list we know of, each with its bound.

| # | Mechanism | Plausible here? | Bound |
| --- | --- | --- | --- |
| 1 | Same-size in-place rewrite that shares the recorded stat's timestamp tick, landing during or after our read (including a slow read that outlasts the window) | Rare (editors and this server write by rename → new inode; appends change size) | **Closed on local filesystems** by the racy rule measured at read start (D10): a stat whose timestamp is within 2 s of `t_start` is never recorded, and any later write outside that tick changes the stat. Remote/FUSE clocks: row 6 |
| 2 | `mmap` writes: Linux updates mtime/ctime at the first write fault after writeback, so later same-page writes can leave them unchanged until the next writeback cycle | Unusual for notes | Backstop |
| 3 | Restore with preserved mtime (`rsync -a`, `cp -p`, `tar x`, restic/borg) | Yes | **Not a hole**: userspace cannot set ctime — `utimensat` sets ctime to *now* — and these tools usually create a new inode. Detected on the next pass |
| 4 | btrfs/zstd specifics: transparent compression, CoW, reflinks, dedupe (`FIDEDUPERANGE`), defrag | This host | **Not a hole**: `st_size` is the logical size; CoW and dedupe change extents, not content; a content write updates mtime/ctime as on any fs. Inode numbers are per-subvolume, but comparison is per path |
| 5 | Subvolume/snapshot rollback or replacement — of the vault root, or of a child subvolume inside it | Possible | **Partially covered, qualified after Codex r1.** Provenance re-derive runs **only for multi-user scopes** (`_reconcile_provenance` is called only when `user_id is not None`, `indexer.py:1738–1741`) and observes **only the pinned root's** identity. It does not cover single-user mode, and it does not cover a child subvolume swapped inside an unchanged root. There the protection is that btrfs snapshots preserve inode numbers *and* ctime, so a restored file presents the `(ino, ctime_ns)` of the state it had when snapshotted. That matches a recorded row only if the row was recorded from that same state, i.e. the same bytes. A mismatch is re-read. The residual (a content state reached twice with identical `ino`, `size`, `mtime_ns` and `ctime_ns`, or a different subvolume presenting a colliding inode at the same path) falls to the backstop |
| 6 | Network / FUSE filesystems: attribute caching (NFS `actimeo`, CIFS, FUSE `attr_timeout`) serving a stale stat; synthetic or unstable inode numbers; server clock skew defeating the racy rule | Not on this host; possible for another operator | `INDEX_STAT_SHORTCUT=false` (documented for such mounts); backstop |
| 7 | FAT/exFAT: 2 s mtime granularity, no true ctime, synthesised inodes | Not on this host | Racy window is 2 s for this reason; `INDEX_STAT_SHORTCUT=false` recommended; backstop |
| 8 | System clock set backwards so a later same-size write reproduces an earlier `(mtime_ns, ctime_ns)` exactly | Needs root and a coincidence at ns resolution | Backstop |
| 9 | Offline modification (disk image edited, fsck/debugfs) while the server is down | Operator action | The first pass after process start is a full-hash pass |
| 10 | Row changed by another writer between the snapshot and the lock | Yes (deploy overlap, `move_note`) | D9 C4/C5 re-decide or defer under the lock |

**D12. The backstop: a full-hash pass at process start and every `INDEX_FULL_HASH_INTERVAL_HOURS` (default 24, `ge=1`).** Tracked per scope in process memory (`_last_full_hash[scope]`, monotonic clock); a restart resets it, which is exactly why the first pass after a start is a full-hash pass. The panel's "Reindex" action also forces one. A full-hash pass is today's pass (every file read and hashed, off the loop now), so its cost is the known cost, once a day.

**The clock advances only on success, so incomplete work cannot postpone the backstop** (Codex r1). `_last_full_hash[scope]` is set only when a full-hash scan pass for that scope **commits with complete verification** — every discovered file's bytes were read and hashed, and no other path was left unprocessed (Codex r2: the existing scan catches read/parse failures and still commits, `indexer.py:1861-1868,1903-1906`, so commit alone would let a skipped file hide for another interval). **Files that are not valid UTF-8 do not block** (verifier, wave 1): their bytes were read in full, such a file is never indexed from them, and a full-hash pass every tick could not change that, so counting it would keep the scope off the shortcut for as long as the file exists. It is still a skip for A.7a and is still logged. Every other skip source blocks: walk failures, read errors (the scan's and C4's re-read), a missing buffered body, a parse failure, the keyword-vector and link-rebuild skips, and a C5 deferral under re-derive. A full-hash pass that aborts, is refused (quarantine, generation mismatch), is cancelled, or commits with any blocking skip leaves the scope *due*, so every following pass for it stays a full-hash pass until one succeeds. **A forced pass** (the panel's Reindex) removes the scope's timestamp before any refusal or filesystem work (Codex r1, wave 1), so a forced pass that fails cannot fall back on a recent earlier success. A file that persistently cannot be read keeps its scope on full-hash passes every tick; it is visible as a logged warning. A due backstop is never deferred by another interval. The interval is when the work becomes *due*; completion within it cannot be guaranteed while files fail to read or passes are paused, and those states are visible (skips are logged, and the scope stays due).

**Two bounds, stated separately:**
- **Detection.** An edit the stat shortcut misses (D11 classes 2, 6, 7, 8 and the residuals of 5) is detected by the **next successful full-hash pass** of its scope. That pass starts at most `INDEX_FULL_HASH_INTERVAL_HOURS` after the previous successful one; the edit is detected earlier if the file's stat changes again. Detection commits the new `content_hash`, which immediately makes the keyword index current. It also makes `semantic_search` mark the note `stale: true` and withhold its preview, the existing #200 behaviour.
- **Semantic convergence.** Re-embedding then follows the ordinary backlog. It is subject to the same per-tenant chunk and time budgets, provider availability and pause flag as any other edit, so it can take further passes. This change does not alter that bound.

Both bounds are recorded in accepted limitation perf-L3.

**D13. The exclusion sweep is gated on an in-process pattern fingerprint plus clean completion.** The issue offered two mechanisms: a persisted fingerprint of `EMBEDDING_EXCLUDE_PATTERNS` plus a "last sweep completed" flag, or a persisted zero-chunk marker. This design uses the first, **held in process memory rather than persisted**. The rule:

> For each scope, the sweep runs on a pass iff: the pass is a backstop pass, **or** `swept[scope]` is not equal to `sha256(json(EMBEDDING_EXCLUDE_PATTERNS))`. `swept[scope]` is set only by a **clean** completed sweep — one that visited every selected row with no pause, no budget stop, no exception, no provider failure, no read failure, no `StaleCertification`, **and no hash-mismatch skip**. It is cleared for a scope on a re-derive and by the in-process reset paths.

*Why this preserves #127's convergence definition.* #127 defines convergence for a completed sweep: afterwards, every certification-current row has vectors iff the configuration includes it, with three declared exceptions: zero chunks, bytes that no longer hash, and a failed provider call. Only a **clean** completion is recorded here, so a sweep with a failed provider call never records, and the next pass sweeps again (today's retry behaviour). **A hash-mismatch skip also blocks the record** (Codex spec review r1, MAJOR). The first draft argued that such a row belongs to the backlog. That is false for an A→B→A sequence. The scan reads A; an editor saves B before the sweep reaches the row, so the sweep sees a mismatch and skips it; undo restores A before the next scan. The row's content hash and its certification now match again, so the backlog never selects it, and a gate that had recorded completion would leave a now-included note absent from search until the backstop. So a sweep with any hash-mismatch skip is not clean, and the next pass sweeps again. This is today's per-tick behaviour, confined to the rare pass where a note changed mid-sweep. No separate pending-reconciliation state is kept. Once the invariant is established under patterns *P*, it stays true while *P* is unchanged, because every other route to a certification-current row applies the current *P* or clears the stamp:
- the backlog's exclusion and embed branches both read the current patterns;
- both move paths NULL the stamp (`indexer.py:1979–2030`, `tools.py:5227–5236`), which hands the row to the backlog;
- a reset NULLs every stamp.

The persisted variant was rejected. `EMBEDDING_EXCLUDE_PATTERNS` is read from the environment at startup, and nothing mutates it at runtime (a repository grep finds no writer). So a pattern change always means a restart, and every restart already forces a backstop sweep. Persistence would only buy skipping that one startup sweep, about 0.2 s. It would also cost a cross-process false agreement: a one-off container, or a deploy overlap with a different *P*, could leave a stored fingerprint matching one process's *P* while the other process certified rows under its own. The persisted variant would also need an `indexer_state` key, and `ck_indexer_state_key` would have to be rewritten. The zero-chunk marker was rejected for three reasons:
- It removes only the zero-chunk re-reads, and leaves the per-row `EXISTS` probe that accounts for the 7.98 M index scans.
- It adds a column whose invalidation lifecycle spans every content change, every cleaner-version bump and every chunk-size change.
- A stale marker is exactly a silently absent note.

The residual is perf-L4: a second process running with different patterns could leave disagreeing rows. The next backstop sweep bounds this at 24 h.

### #281 — the provider's transport and its work

**D14. One pooled client per provider, built through #284's factory.** PR #284 introduces `embedding_http_client(timeout) -> httpx.AsyncClient` in `src/services/transport_security.py` (`trust_env=False`, `follow_redirects=False`, `verify=` the CA `SSLContext` parsed once at settings construction, else certifi), and `tests/test_internal_transport_http_clients.py` carries an AST sweep that fails on any `httpx` client built outside it. The shared client **is built by calling that factory** — never `httpx.AsyncClient(...)` directly — so all three properties hold for the pooled client by construction and the sweep stays green. Lifecycle: created lazily on first use and **keyed to the running event loop** (a client bound to a closed loop is discarded and rebuilt — this is what keeps pytest's per-test loops and the one-off maintenance scripts working); `aclose()`d in the lifespan's shutdown, after the indexer task is cancelled and before the engine is disposed. Per-request timeouts are passed on each `post(..., timeout=...)` (Ollama 30 s, OpenAI 60 s, unchanged values); the client-level timeout is the provider's. Pool limits stay httpx's defaults. **Sequencing: S5 does not start until #284 merges**; the brief's first check is that `embedding_http_client` exists in the base.

**D15. Ollama embeds through fixed-size `/api/embed` batches.** `embed_batch` splits the chunk list into consecutive slices of `OLLAMA_EMBED_BATCH_SIZE` (default 16, `ge=1`, `le=256`) and sends each as one `/api/embed` request with an `input` array, awaited under `asyncio.wait_for(..., 30.0)` with the 30 s HTTP timeout. **There is still no aggregate deadline (#127 D5).** A *fixed* batch size keeps the per-request bound constant, independent of the note's size. A per-note or proportional batch would reintroduce the size class that D5 removed, because the time to answer one request would then grow with the note. Each response must carry exactly `len(slice)` vectors, or the batch raises. `embed_note`'s existing whole-note cardinality check (`embeddings.py:1091`) is kept unchanged above it. `embed_one` (the query path) sends a one-element array. `OllamaProvider` and `OpenAIProvider` are both adapted to the shared client, and OpenAI's 96-input batching is otherwise unchanged. This goes into the architecture note as the D5 rationale for batching.

**D16. A stored vector is reused for byte-identical chunk text, only while it provably belongs to the current generation.** In `embed_note`, after chunking:

1. **Look up.** In a read-only transaction, load this note's `(id, chunk_text, embedding)` rows and the stored embedding fingerprint, then **commit before any provider I/O**. This follows the "Discovery SELECTs end their transaction" rule: holding `AccessShareLock` on `note_embeddings` across the provider call would close the reset deadlock that #206 documents.
2. **Decide eligibility.** Reuse is eligible only when the stored fingerprint is **present** and equals `embedding_fingerprint()`. `ABSENT` disables reuse, because nothing has been claimed about those rows.
3. **Embed the rest.** Send only the chunks whose exact text has no stored vector. The cardinality check applies to that subset. `on_provider_call` fires with the subset's size, and does not fire at all when the subset is empty, because no provider call was made and the `attempted` rule stands.
4. **Certify.** Under the generation lock (`_generation_matches`, unchanged), re-read `note_embeddings` for the note and require every reused row's `id` to still exist with the same `chunk_text`. A reset between step 1 and step 4 deletes every row, so this fails. It is the one interleaving the fingerprint cannot see: a reset under an unchanged fingerprint after a model-artifact change (L1). On failure the attempt returns `GENERATION_MISMATCH`: nothing is certified, nothing is written, and the note is retried next pass. **Accounting (Codex r1):** the existing interlock requirement says a mismatch "SHALL count as an attempt, because a provider call was issued". An all-reuse note reaches the mismatch *without* a provider call, so this change modifies that requirement: a mismatch counts as an attempt **if and only if** a provider call was issued. The existing cardinality requirement is likewise restated. The provider's answer must carry exactly one vector per chunk *sent*, and certification requires full coverage of the *requested* chunk list, whether by reused or fresh vectors.
5. **Replace.** `certify_embedded`, then delete and re-insert **all** of the note's rows (reused and new vectors together) in document order, exactly as today. No partial row surgery.

The vector for a chunk is assumed to be a function of (model, text) alone. That holds for both providers up to numeric noise between batched and single requests; recorded as L6.

### #283 — the database

**D17. Autovacuum reloptions, not `fastupdate = off`.** Setting `fastupdate = off` makes every tsvector update insert straight into the GIN tree, so the pending list disappears. But it makes every changed note's keyword write slower, and it does nothing about the rest of what "never vacuumed" means:
- 565 dead tuples;
- no visibility map;
- **GIN metapage statistics are written only by VACUUM** (`ginvacuumcleanup`), and the planner's GIN cost estimate relies on them (`search.md`).

The reloptions fix all of that and the pending list with it:
- `ALTER TABLE notes_metadata SET (autovacuum_vacuum_scale_factor = 0.02, autovacuum_vacuum_insert_scale_factor = 0.02)`.
- Threshold: 50 + 0.02 × ~4,100 ≈ 132 dead tuples.
- Vacuum flushes the pending list, well before `gin_pending_list_limit` would.

The setting is per table, so the shared instance's other tenants are untouched.

**D18. The one-time VACUUM is not in the migration.** `VACUUM` cannot run inside a transaction block, and alembic runs this chain in one transaction (`env.py`: `context.begin_transaction()`). Two ways to get it done were rejected:
- `autocommit_block()` would commit the rest of the chain mid-migration and break the schema gate's stamp-back idempotence cases.
- A `docker exec … psql` step inside `make db-migrate` would couple migration to a tool the image does not guarantee.

What happens instead:
- **Autovacuum does it by itself.** The table's current dead-tuple count (565) is already above the new threshold (~132), so the autovacuum launcher visits `notes_metadata` within one `autovacuum_naptime` (60 s default) of 027 committing.
- **`make db-vacuum-notes` is the deterministic fallback.** It runs `VACUUM (ANALYZE) notes_metadata` in its own autocommit session through the application container.
- Live check 7.4 confirms `last_autovacuum` or `last_vacuum` is non-NULL.

**D19. The `halfvec` expression index: dimension, gate, re-rank, one DDL owner, prewarm, and `alembic check`.**

- **Dimension.** The expression is `(embedding::halfvec(D)) halfvec_cosine_ops`, where *D* is `settings.embedding_dimensions`. It is not the literal 1024. It is built only when *D* ≤ 2000, which is the condition under which `ix_note_embeddings_embedding_hnsw` exists today, and it replaces that index. `halfvec` could index up to 4,000 dimensions, but building it for 2000 < *D* ≤ 4000 would turn today's exact scan into an approximate one for those deployments. That is a recall decision this change does not take. Above 2000 nothing is built, and the queries keep the plain `vector` expression.
- **Query.** When the index mode is on (*D* ≤ 2000), `semantic_search` and `find_related_stmt`:
  - order by `embedding::halfvec(D) <=> q::halfvec(D)`, the exact index expression;
  - also select the full-precision `embedding <=> q`;
  - re-sort the fetched candidates by the full-precision distance before the per-note dedupe;
  - report `similarity = 1 −` the full-precision distance.

  So `halfvec` affects only **which candidates** the index yields. The ranking among them and every reported number stay full precision, and the difference reduces to membership at the overfetch boundary, which the recall gate measures.
- **Other paths.** The zero-row exact fallback (`enable_indexscan = off`) re-runs the same statement, so its ordering uses the `halfvec` expression over a sequential scan; the full-precision re-sort then restores exact order. The `find_related` source-vector average is unchanged.
- **Gate.** Before 027 carries the index, `tests/integration/test_search_recall.py` must pass with these changes:
  - its fixture builds the index through the production DDL helper (below) instead of its own `CREATE INDEX`;
  - **its exact baseline stays a full-precision `vector` sequential scan**, because a `halfvec` baseline would hide exactly the precision loss being measured;
  - its EXPLAIN assertions name the new index.

  The SLO is recall ≥ 0.9 on each of three rebuilds, for every filter shape and for `find_related`. If it is not met, the index part is removed from 027, the query casts are not merged, and the outcome is recorded in this design and in `search.md`. Recall is also spot-checked on the live vault (live check 7.5), but that check is informational, because the SLO is the gate.
- **One DDL owner.** New `src/services/vector_index.py` holds the index name `ix_note_embeddings_embedding_halfvec_hnsw`, `index_enabled(dim)`, `create_index_sql(dim)` (with `m = 16, ef_construction = 64`, as today), and the SQLAlchemy distance-expression builders. It is called by 027, `scripts/reset_embeddings.py`, the panel's reset route, the prewarm and the recall test, so the five places that create or use the index cannot drift. Both reset paths drop the new index (and the legacy name, `IF EXISTS`) before `ALTER COLUMN TYPE`, as they do now.
- **Prewarm.** `probe_statement()` orders by the same helper-built expression, so the probe warms the index the search actually uses. `_hnsw_index_exists` looks up the helper's name, and `invalidate_hnsw_index_cache` is unchanged.
- **`alembic check`.** The expression index is **not** declared on the model. Declaring it would put an operator class inside an index expression, and Alembic's expression comparison strips `::type` casts by regex and then either reports spurious drift or skips with a warning. Instead, `alembic/env.py` passes `include_object`, which excludes exactly the object named by `vector_index.INDEX_NAME` when `type_ == "index"`. The exclusion names one index, and a test pins that it names exactly one. The schema gate then verifies the index **through the catalogue**: `pg_get_indexdef`, `indisvalid` and the operator class, which is the house rule for indexes (019/021/024). `ix_note_embeddings_embedding_hnsw` is removed from `NoteEmbedding.__table_args__` in the same change that drops it. The reloptions are invisible to Alembic and are asserted by the schema gate via `pg_class.reloptions`.
- **Build cost.** 027 creates the index non-concurrently under `SET LOCAL maintenance_work_mem = '512MB'` and a `statement_timeout` sized for the build. That takes about 17.5 k × 1024 dimensions in tens of seconds, during which writes to `note_embeddings` wait. It runs in the deploy's migrate step, where waiting is acceptable. `downgrade()` recreates the `vector` index when *D* ≤ 2000 and drops the `halfvec` index.

**D20. The `random_page_cost` comment is corrected.** It says the planner hint is needed because the planner costs the heap at `relpages` and does not model the detoast I/O of a TOASTed vector/tsvector. That is the rationale `search.py` already states, and the hint remains correct on an HDD because the working set is mostly cached. It no longer says "SSD".

**D21. Migration numbers and sequencing.** 026 belongs to S3 (`down_revision = "025"`) and 027 to S4 (`down_revision = "026"`). Because the chain is 025 → 026 → 027 and the schema gate carries a single head literal, **S4 must run after S3 as well as after S2**. This goes beyond the brief, which sequenced S4 only after S2. S3 raises the gate's head to `026`; S4 raises it to `027`.

## Spec review history

Round 1 (Codex, against `75e5d58`) returned **REJECT**, with 2 MAJOR and 5 MINOR findings and `d9_recommendation = keep`. All seven were accepted and folded in:

| # | Sev | Finding | Disposition |
| --- | --- | --- | --- |
| 1 | MAJOR | A hash-mismatch skip counted as a clean sweep; an A→B→A edit leaves an included note absent until the backstop | A hash-mismatch skip now blocks clean completion (D13, index-integrity delta); A→B→A test added (task 3.12) |
| 2 | MAJOR | Racy recency measured at hash completion misses a same-tick rewrite during a slow read | Recency is measured against `t_start`, taken before the pre-read fstat, and future timestamps stay untrusted (D10). D11 row 1 corrected; slow-read test added (task 3.11) |
| 3 | MINOR | The staleness bound conflated detection with semantic convergence, and did not say when the backstop clock advances | Detection and convergence bounds are stated separately. The clock advances only on a committed full-hash pass (D12, perf-L3, delta) |
| 4 | MINOR | The root-rollback claim overstated provenance coverage (multi-user and root only) | D11 row 5 qualified with code evidence; the residual goes to the backstop |
| 5 | MINOR | "Identical" contradicted the tie-breaks at cutoffs and the chunk choice; `find_orphans`' `NULLS LAST` was dropped | Both differences explicitly permitted and covered by the oracle; `NULLS LAST` kept (D6, D7, delta, task 2.6) |
| 6 | MINOR | The all-reuse mismatch contradicted "a mismatch counts as an attempt"; the cardinality wording was full-note | The interlock requirement is MODIFIED (an attempt only if a provider call was issued), and the cardinality requirement is MODIFIED (per chunks sent, plus full coverage) |
| 7 | MINOR | Slice ownership gaps; S5's base check contradicted its dependencies | S5 owns the post-chunking region; S3 owns reset-state invalidation lines; base checks are per slice (tasks.md) |

## Risks / Trade-offs

- **Stat shortcut misses an edit** → bounded by D10's racy rule and D12's backstop; every class enumerated in D11. Adversarial focus.
- **Walk-before-lock mis-reconciles** → C4/C5 re-decide or defer under the lock; fallback is D8 alone.
- **Chunk reuse attaches a wrong vector** → gated on a present, matching fingerprint and on the reused rows surviving under the lock (D16).
- **Async commit loses audit rows on a server crash** → ≤ ~600 ms, declared (L1, L2).
- **`halfvec` loses recall** → gated on the SLO before shipping; full-precision re-rank confines the effect to candidate membership.
- **Deferred tsvector breaks an unknown reader** → raise-on-load makes it fail loudly in tests; grep found none.
- **Seam conflicts** across four slices in `embeddings.py`/`tools.py`/`indexer.py`/`models/db.py` → regions named in tasks.md; supervisor merges.

## Migration Plan

1. S1 and S2 can deploy independently; neither carries a migration.
2. S3 carries 026. The first pass after deploy is a full-hash pass that records every note's stat. Because the stat is recorded on the unchanged-hash refresh path, the pass writes about 4 k UPDATEs once.
3. S4 carries 027: reloptions, and the index if the gate passed. The deploy's migrate step builds the index. Autovacuum then visits `notes_metadata` within about a minute.
4. S5 (after #284) carries no migration.
5. Rollback is roll-forward, per house rule. 026's `downgrade()` drops its four columns and CHECK. 027's restores the `vector` index and resets the reloptions. Every setting has an off switch: `INDEX_STAT_SHORTCUT=false`, and `OLLAMA_EMBED_BATCH_SIZE=1` for the pre-batching request shape.

## Accepted limitations

- **L1** — **Quota durability.** A PostgreSQL server crash or host power loss can lose admitted quota increments committed in the last ~600 ms. That is an undercount in the caller's favour and never an overcount. Owner decision taken by default (#279).
- **L2** — **Audit durability.** The same crash can lose `usage_logs` rows and `last_used_at` stamps from the same window. `write_usage_row` returning `True` means "committed and visible", not "durable across a server crash". An application crash or restart loses nothing committed.
- **perf-L3** — **Stat-shortcut staleness.** An edit that changes none of `(size, mtime_ns, ctime_ns, inode)` is not *detected* until the next **successful** full-hash pass of its scope. Such a pass starts at most `INDEX_FULL_HASH_INTERVAL_HOURS` (24 h) after the previous successful one, and a failed or incomplete one does not restart the clock. The edit is detected earlier if the file's stat changes again. Once detected, keyword search is current and vector results mark the note stale. Semantic re-embedding then converges under the existing budgets and provider availability, which can take further passes. Every known class is listed in D11. Network, FUSE and FAT mounts should set `INDEX_STAT_SHORTCUT=false`.
- **perf-L4** — **Exclusion sweep skipped between backstops.** A second process running with different `EMBEDDING_EXCLUDE_PATTERNS` (a deploy overlap or a one-off container) could certify rows that disagree with this process's patterns without triggering a sweep here. Bounded by the 24 h backstop sweep.
- **L5** — **`last_used_at` resolution.** The panel's "last used" is accurate to 60 s.
- **L6** — **Reused vectors.** A reused vector is the provider's answer from when that chunk was first embedded. Batched and single requests can differ by numeric noise, so a note mixing reused and fresh vectors is not bit-identical to a from-scratch embed.
- **perf-L7** — **Deferred rows.** C5 defers a row changed mid-walk to the next pass. That note's row, and its search presence, can lag by one extra pass.
- **L8** — **Recall gate on synthetic data.** The `halfvec` SLO is measured on the fixed synthetic corpus. The live spot check (7.5) is informational.
- **L9** — **Exact-tie ordering.** Among rows whose sort key is exactly equal, result order can differ from before this change. It is now deterministic.

## Adversarial review focus

These are search-correctness surfaces. Per CLAUDE.md, a change to the chunking/embedding path or to search is a mandatory adversarial-pass trigger. Codex should be told to *attack* each surface below.

1. **Stat shortcut (D10–D12).** Find any sequence of filesystem operations in which the recorded stat matches while the bytes differ, beyond D11's list. Check that the recorded stat always comes from before the read, on the same descriptor. Check that the racy rule cannot be bypassed by a future timestamp. Check that the backstop really runs at startup and when the interval elapses, for every scope. Check that the shortcut is never taken under `re_derive`, a stale marker, a NULL stat, or a full-hash pass. Check that the unchanged-hash stat refresh cannot record a stat for bytes other than the ones hashed.
2. **Walk before lock (D9).** Try to construct an interleaving with `move_note`, another container's pass, a reset or a rebuild in which a row is pruned, paired as a move, or left certified on the strength of the pre-lock walk. Check that C2 holds: no snapshot transaction may be open across the walk or the lock wait. Check that a deferral under re-derive withholds the stamp.
3. **Exclusion-sweep gate (D13).** Find a route by which a certification-current row comes to disagree with the current patterns without the sweep's clean-completion flag being cleared or the backstop running. Check that a sweep with any provider failure, read failure, `StaleCertification` or hash-mismatch skip never records completion (the A→B→A case).
4. **Chunk-vector reuse (D16).** Check whether a vector from a previous model or fingerprint can be written under the current one. Check whether the lookup's transaction is committed before provider I/O on every path, including exceptions. Check whether an empty provider subset is handled without breaking `attempted`, the budget or cardinality. Check the under-lock existence check against a reset that runs mid-call.
5. **Projection and similarity (D5–D7, D19).** Check that every field a result renders is still selected. Check that no path reads the deferred `content_tsvector` from an entity. Check that the staleness and truncation markers are unchanged. Check that `halfvec` changes candidate membership only, and that the full-precision re-sort precedes the dedupe.
6. **Request path (D1–D3).** Check whether any revocation — key revoke, user deactivate, vault unassign, OAuth revoke — can be missed or delayed by the folded read. Check whether `synchronous_commit = off` can leak to another checkout of the pooled connection, or reach a write that must stay synchronous.

## Open Questions

- None blocking. The `halfvec` go/no-go is decided by the gate at S4 time and is recorded here and in `search.md` when it is.
