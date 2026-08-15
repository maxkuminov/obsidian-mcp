## Context

Findings from the 2026-08-15 read-only investigation (probes against the live
DB in `BEGIN…ROLLBACK`, Ollama timing, `usage_logs` analysis):

| semantic_search component | warm | cold |
|---|---|---|
| query embedding (Ollama bge-m3, GPU) | 0.39–0.44 s | 14.3 s (model reload) |
| pgvector HNSW query (ef_search 80, LIMIT 75, join) | 5–7 ms | 3.2 s (910 blocks read) |
| Python dedupe / `np.dot` / formatting | ~50 ms | same |

Observed p50 by gap since previous call: <60 s → 466 ms; <5 min → 633 ms;
<1 h → 3.1 s; >1 d → 5.2 s. Median inter-call gap rose from 135 s (Apr) to
1,676 s (Aug). Step change on 2026-05-01 (512-token chunks → 16.7k chunks,
132 MB HNSW index > 128 MB `shared_buffers`, shared with a 2.3 GB tenant).

Recall bug: with `random_page_cost = 1.1` the planner picks HNSW index scan →
nested loop → filter. pgvector's non-iterative scan returns at most
`ef_search` candidates; filtering afterwards drops most of them and nothing
refills. 120 folder-filtered probes: 45 empty, 100 short. Without the override
(seq scan + sort): 0 empty, 0 short — but that plan degrades linearly with
vault size, which is why the override was added. pgvector ≥ 0.8 solves this
with `hnsw.iterative_scan` (live extension: 0.8.2).

Keyword: `full_text_search` plans as Seq Scan over `notes_metadata`, detoasting
every tsvector (36 MB TOAST) — 13,086 buffers/query — because the planner costs
the heap at `relpages = 481` and does not model detoast I/O. Setting
`random_page_cost = 1.1` flips it to a GIN bitmap scan (verified 11× fewer
buffers).

Ruled out: indexer overlap (ticks 1–2 s, slow calls uniformly distributed
across the 300 s cycle), stale statistics, bloat, pool exhaustion, event-loop
blocking, response size.

## Goals / Non-Goals

**Goals:**
- Filtered semantic search returns the same notes an unfiltered search would,
  restricted to the filter — never an empty or truncated set because of the
  plan.
- Warm-path latency stays warm between sparse calls without operator action.
- Keyword search uses its index.
- Future latency regressions are attributable from `usage_logs` alone.

**Non-Goals:**
- Changing chunking, embedding model, `ef_search`, or overfetch factors.
- Hybrid search or reranking (explicitly rejected in `IMPROVEMENTS.md`).
- Changing Ollama `keep_alive` or Postgres `shared_buffers` from this repo
  (operator actions; documented as recommendations).

## Decisions

1. **`hnsw.iterative_scan = 'relaxed_order'`, transaction-scoped, in both
   vector query paths — with an honest contract.** `relaxed_order` lets
   pgvector keep scanning the graph until the `LIMIT` is satisfied after
   filtering, bounded by `hnsw.max_scan_tuples` (20,000) and
   `hnsw.scan_mem_multiplier` (1). HNSW stays approximate, so the spec's
   guarantee is a **benchmark SLO** — recall ≥ 0.9 against an exact filtered
   sequential baseline *at the same overfetch depth with the same dedupe*,
   measured on a fixed corpus/query set across three index rebuilds with
   cutoff ties treated as equivalent — plus non-empty-when-baseline-non-empty
   even when scan bounds are reached. It is not "exactly the filtered subset of
   the unfiltered window" (unattainable) and not a per-query guarantee (ANN is
   nondeterministic across builds). The fixed 5× chunk overfetch means one
   verbose note can crowd out others after dedupe; that is a property the
   baseline shares, and changing the overfetch is a non-goal here (but
   `find_related` moves from `limit × 5` to the same `max(5 × limit, 50)` so
   both paths share one contract). **Zero-row safety net:** when a filtered
   HNSW query returns no rows, the service re-runs the same filtered statement
   with `SET LOCAL enable_indexscan = off` (pgvector's documented exact-search
   switch) and returns that — O(n) but only on the rare empty path, and it
   turns "non-empty when a match exists" into a construction, not a benchmark
   claim. Logged as `exact_fallback: true` in the usage params. `relaxed_order` may emit results
   slightly out of distance order across iterations, so the Python dedupe step
   **re-sorts by distance** before truncating — a presentation guarantee only;
   it cannot recover candidates the scan did not return. `strict_order` was
   considered and rejected: costlier, and the re-sort gives monotone output.
   The whole index is 16.7k tuples today, under the tuple cap; the code
   comment names the cap as the next knob. Keep `random_page_cost = 1.1` (it
   is what makes the planner choose the index at all). **A startup guard**
   (`pg_extension.extversion` for `vector` ≥ 0.8.0, `sys.exit(1)` otherwise,
   skipped in sandbox mode, mirroring the embedding-dim guard) prevents an
   older backend from accepting the GUC as a placeholder and silently running
   the old plan.
   *Alternative:* drop the planner override and accept seq scan + sort —
   correct today but O(n) and already the reason the override exists.
   *Alternative:* raise `ef_search` to hundreds — reduces but does not fix the
   miss, and slows the unfiltered path.

2. **`SET LOCAL random_page_cost = 1.1` in `full_text_search` too, plus a
   deterministic tie-break.** Same transaction-scoped mechanism the vector
   path uses; no global Postgres change (the DB is shared with other tenants).
   The requirement is that the setting is issued and that *matching semantics
   are unchanged*; index usage is the expected plan on a production-sized
   corpus for rare terms (verified 11× fewer buffers) but is not a universal
   SHALL — the planner may legitimately seq-scan a tiny table or a very common
   term. `ORDER BY rank DESC, file_path ASC` makes tied-rank membership stable
   across plans.

3. **Pre-warm inside the existing indexer loop, at the end of each tick,
   *inside* the `index_pass_lock` block, bounded by one `asyncio.wait_for(...,
   15)`.** Holding the lock serialises it with the panel's reindex (which takes
   the lock today) **and with reset-embeddings, which this change makes take
   the lock too** (`src/control_panel/routes.py::reset_embeddings` and the
   legacy `trigger_reembed` currently only set `indexer_paused` / delete
   rows). Protocol: set the pause flag; **end the request's own session
   transaction** (`await session.commit()`/close) so a waiter never pins a
   pool connection while blocked on the lock (fifteen concurrent resets
   otherwise exhaust the pool while the lock holder waits for a connection);
   `async with index_pass_lock:` then open a fresh session for the destructive
   statements. The wait is bounded by the current pass, not by 15 s — the
   danger-zone page already warns it can take a while. While in there, fix
   the pre-existing legacy-reembed bug Codex found: `trigger_reembed` deletes
   `note_embeddings` but never NULLs `notes_metadata.embedded_content_hash`,
   so the reindex it spawns embeds nothing — clear the hashes in the same
   transaction. The pre-warm re-checks
   `_is_paused()` immediately before running. It does one
   `get_embedding("warmup")` (≈ 0.4 s; keeps bge-m3 resident, defeats eviction
   after Ollama restarts) when the provider is local (`settings.embedding_provider
   == "ollama"`), and one HNSW probe `SELECT 1 FROM note_embeddings ORDER BY
   embedding <=> :vec LIMIT 1` using a **deterministic non-zero unit vector**
   of `EMBEDDING_DIMENSIONS` (a zero vector has undefined cosine direction and
   would not traverse the graph) bound through pgvector's typed expression so
   the plan uses the HNSW index; the probe is skipped (and logged) when no
   HNSW index exists (`EMBEDDING_DIMENSIONS > 2000` deployments), detected
   once via `pg_indexes`. Timeout or any ordinary `Exception` →
   WARNING, no re-raise, failure counter untouched; **`asyncio.CancelledError`
   is re-raised immediately** so lifespan shutdown still cancels the loop. Cadence: the loop's sleep
   starts after the pre-warm returns, so a tick is delayed by at most 15 s.
   *Alternative:* a separate `asyncio` task on its own interval — more moving
   parts, and it would race the lock holders.

4. **Per-phase timing via a call-scoped holder owned by `_tracked`.** The
   decorator sets a `ContextVar[dict]` to a fresh dict at call start and
   resets it in `finally`; the services write `embed_ms`/`db_ms` into the
   current holder if one exists (a direct service call outside a tracked tool
   finds none and records nothing). Service return types are unchanged (no
   list→tuple contract change for existing callers). `find_related` performs
   no embedding-provider call, so it records `db_ms` only. Early returns and
   exceptions leave measured phases as measured; nothing can be consumed by a
   later call in the same task because the holder is reset per call. No schema
   migration — `params` is JSONB.

5. **Integration test reuses the throwaway-pgvector harness introduced by
   `dependency-refresh-2026-08`** (`TEST_DATABASE_URL`, skip when unset) — that
   change lands first; if this one is implemented before it merges, the
   harness is added here identically. The fixture must actually reproduce the
   bug: several hundred `A/` chunks and a few dozen `B/` chunks, with `A/`
   dominating the nearest neighbours of the query vector; assert via `EXPLAIN`
   that the HNSW index is used (a 45-row table would seq-scan and pass
   vacuously), assert that with `hnsw.iterative_scan = off` the filtered result
   is empty/short, and that with `relaxed_order` recall against the exact
   sequential baseline is ≥ 0.9 and non-empty. Cover folder, tags,
   frontmatter, user scope, and `find_related`.

## Risks / Trade-offs

- [`relaxed_order` returns slightly out-of-order rows] → Python re-sort by
  distance before truncation (presentation only); test asserts monotone
  distances in the output.
- [Older pgvector accepts the GUC as a placeholder and silently runs the old
  plan] → startup version guard (≥ 0.8.0), fresh-connection integration check.
- [Pre-warm hangs] → single 15 s `wait_for`; runs under the pass lock so it
  cannot overlap reset/reindex; re-checks pause first.
- [Timing leaks between calls] → holder is created and reset by `_tracked` per
  call; test: same-task sequential calls.
- [Iterative scan hits `max_scan_tuples` on a very selective filter over a much
  larger vault] → returns what it found (still ≥ non-iterative behaviour);
  comment names the GUC to raise; not reachable at today's 16.7k chunks.
- [Planner hint in keyword path picks a worse plan for some query shapes] →
  hint is transaction-scoped and identical to the vector path's; integration
  test asserts index usage for a representative query; revert is one line.
- [Pre-warm masks a broken embedding provider by "warming" it every 5 min] →
  it is logged at WARNING on failure and never raised; the health of the
  provider is the indexer's business, unchanged.
- [OpenAI-provider deployments would pay per tick] → pre-warm embed skipped
  for non-Ollama providers by design.
- [Timing side channel leaks across concurrent calls] → ContextVar is
  per-task; each tool call runs in its own task under the SDK.

## Migration Plan

1. Branch; implement; unit tests green; run integration test against a
   throwaway pgvector container.
2. Pre-deploy: capture live results for fixed queries — one unfiltered, one
   `folder=`-filtered that is known to be short/empty today, one
   `keyword_search` — via the live tools.
3. `make deploy`; `make status`.
4. Post-deploy: re-issue the queries via the live tools; the filtered one must
   now return rows consistent with the unfiltered one; check `usage_logs` rows
   carry `embed_ms`/`db_ms`; watch one indexer tick in `make logs` for the
   pre-warm line. Record which tools were called.
5. Rollback: previous image; no data change.

## Open Questions

- Whether to expose `embed_ms`/`db_ms` on the panel dashboard — deferred to the
  observability roadmap item (#9).
- Whether to raise `hnsw.max_scan_tuples` proactively — no; revisit when the
  vault passes ~20k chunks.
