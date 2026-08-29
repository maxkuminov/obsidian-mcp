# Search: vector, keyword, and the settings that are correctness

> Deep rationale extracted from `CLAUDE.md`. Read before touching `semantic_search`, `keyword_search`, `find_related`, or any query planner hint. Owner scoping of the read path lives in `vault-roots-and-tenancy.md`.

## Search decisions

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

## Search result telemetry (#161)

The same holder carries what a search *returned*, which is what
`/admin/search-analytics` reads. Three keys, written by
`timing.record_results` / `timing.record_source_path` in the place where each
tool's result set is final — `full_text_search` (`src/services/search.py`),
`semantic_search` (`src/services/embeddings.py`), and `find_related_impl`
itself, after its dedupe and truncation to `limit`, so the telemetry names what
the caller was handed and not what the overfetch scanned.

- **`result_count`** — an int, always, and the **full** count. Not the number
  of paths that fit the budget below: the zero-result view reads this value,
  and a count clipped to the logging cap would report a search that found forty
  notes as having found ten.
- **`result_paths`** — at most the first `MAX_RESULT_PATHS` (10) paths, and at
  most `MAX_RESULT_PATHS_BYTES` (2048) bytes of UTF-8 JSON for the value,
  dropping paths **from the end** so what is logged stays a prefix of a ranked
  list. A path that does not fit is dropped whole, never cut: a truncated path
  names a note that does not exist, and it would land in the coverage ranking
  as a real retrieval.
- **`source_path`** (`find_related` only) — the grouping key for that tool's
  analytics, recorded before anything can return so that the two failure
  branches carry it too. The full path when it is at most
  `MAX_SOURCE_PATH_BYTES` (1024) bytes, otherwise its sha256 hex digest. The
  named `path` param cannot serve: `_truncate_params` cuts it at 200
  characters, so two distinct long paths would collapse onto one row.

**The budget is enforced at the record site, and it has to be.** `_tracked`
builds its logged params as `_truncate_params(named args)` and *then*
`update()`s the timing holder over the top — merged telemetry never meets the
generic 200-character truncation, so there is no backstop downstream. A
regression here does not fail a call; it writes a params blob orders of
magnitude larger than every other row in `usage_logs`.

**`find_related`'s two operational failures are marked** (`params.error`):
`related_source_not_found` and `related_source_not_embedded`. Both branches
used to return a plain string with no marker at all, which left them
indistinguishable *in the log* from a call that ran and found nothing — and
"the vault holds nothing near this note" is the one the analytics page exists
to count. Both are **post-body** markers; the classification rule and what that
implies live in [usage attribution](usage-attribution.md). A source that exists,
is embedded, and has no neighbours after the exact fallback stays a true
zero-result: `result_count` 0, no marker.

Reading side: `src/services/search_analytics.py` and
`/admin/search-analytics`. Its identity rule — every grouping and coverage join
keys on `(usage_logs.user_id, path)` with `IS NOT DISTINCT FROM` — is written
down there and in [usage attribution](usage-attribution.md).

