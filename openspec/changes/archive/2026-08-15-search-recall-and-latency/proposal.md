## Why

A read-only investigation of `usage_logs` (2026-08-15) found that `semantic_search`
latency is bimodal — warm ≈ 0.47 s, cold ≈ 17.5 s (Ollama re-loading bge-m3
after eviction, 14 s, plus HNSW index pages missing from a shared Postgres whose
`shared_buffers` is 128 MB, 3 s) — and that as usage grew sparser more calls pay
the cold price (p50 1.2 s → 4.8 s over five weeks). More seriously, **filtered
semantic search silently loses recall**: `SET LOCAL random_page_cost = 1.1`
forces an HNSW nested-loop plan, and when a `folder`/`tags`/`frontmatter`/
`user_id` filter is applied *after* the index scan, candidates are discarded
with no rescan — 45 of 120 folder-filtered probes returned zero rows and 100
returned fewer than the requested overfetch, while the same queries without the
planner override returned full result sets. `keyword_search` never uses its GIN
index (5 index scans lifetime vs 3,655 seq scans; each query detoasts all ~3,800
tsvectors, ~13,000 buffers). This is exactly the "silently wrong search results"
failure `CLAUDE.md` names as the expensive one: an agent acts on an empty
filtered result and concludes the note does not exist.

## What Changes

- **Filtered semantic recall (correctness):** `semantic_search` and
  `find_related` SHALL run their HNSW queries with pgvector iterative scan
  enabled (`hnsw.iterative_scan = relaxed_order`, pgvector ≥ 0.8; the live
  extension is 0.8.2) so that post-filter candidates are re-fetched until the
  requested overfetch is satisfied, the index is exhausted, or pgvector's scan
  bounds are hit. The contract is recall ≥ 0.9 against an exact filtered
  baseline and non-empty whenever the baseline is non-empty — HNSW stays
  approximate; the spec does not promise more than pgvector can. A startup
  guard requires pgvector ≥ 0.8.0 (the live extension is 0.8.2).
- **Keyword search plan:** `full_text_search` issues the same
  transaction-scoped `random_page_cost = 1.1` hint the vector path already
  uses, so on a production-sized corpus rare-term queries use the tsvector GIN
  index (verified: 1,146 buffers vs 13,086; 6–9 ms vs 26–37 ms warm; 267 vs
  3,926 disk reads cold). Matching semantics are unchanged; a deterministic
  `file_path` tie-break makes tied-rank ordering stable across plans.
- **Cache pre-warm on the indexer tick:** at the end of each periodic indexer
  pass, under the pass lock and bounded by a 15 s timeout, the server issues
  one short embedding request (local providers only) and one HNSW probe with a
  deterministic non-zero vector so the model stays resident and the index's
  hot pages stay cached. Cost ≈ 0.4 s + 6 ms per 5 minutes. Skipped when
  paused or in sandbox mode; failures/timeouts are logged, never raised.
- **Per-phase timing:** `semantic_search` records `embed_ms` and `db_ms`
  (`find_related`: `db_ms` only) in `usage_logs.params` via a call-scoped
  holder owned by the `_tracked` decorator, so the next regression can be
  attributed from data instead of probes and nothing leaks between calls.
- **Infra (outside this repo, for the operator):** raise `shared_buffers` on
  the shared Postgres from 128 MB to 2–4 GB (host has 62 GB; the container uses
  264 MB) — removes most of the 3 s cold-DB component. Recorded here; not a
  code change.
- Out of scope: chunk-size or embedding-model changes, hybrid search,
  reranking, `keep_alive` changes on the Ollama side.

## Capabilities

### New Capabilities

- `search-quality`: recall guarantees for filtered vector search, the keyword
  index-usage requirement, the cache pre-warm behaviour, and per-phase timing
  in usage logs.

### Modified Capabilities

_None — `note-filters` and `wikilink-graph` describe the tools' interfaces,
which do not change._

## Impact

- `src/services/embeddings.py` (`semantic_search`): add
  `SET LOCAL hnsw.iterative_scan = 'relaxed_order'`; wrap embed and DB phases
  in `time.monotonic()` marks and return them for logging.
- `src/mcp_server/tools.py` (`find_related_impl`, `semantic_search_impl`
  logging): same iterative-scan setting; timing fields into `_tracked` params.
- `src/services/search.py` (`full_text_search`): `SET LOCAL random_page_cost = 1.1`.
- `src/services/indexer.py` (periodic loop): pre-warm step after each tick.
- Tests: unit tests for the SET LOCAL statements being issued (fake session),
  the pre-warm hook (called after tick, skipped when paused/sandbox, swallows
  errors), and timing fields in the logged params; **integration test**
  (opt-in `TEST_DATABASE_URL`, throwaway pgvector container, reusing the
  harness added by `dependency-refresh-2026-08`) that seeds notes in two folders
  with vectors, asserts folder-filtered semantic search returns the expected
  rows (non-empty, correct order) and that `EXPLAIN` of the keyword query shows
  the GIN index.
- Adversarial-Codex trigger per `CLAUDE.md`: changes the search result path.
