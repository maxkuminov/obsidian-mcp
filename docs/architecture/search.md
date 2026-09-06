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

## Stale vectors are annotated, never filtered (#200)

Both vector paths returned the **stored** `chunk_text[:500]` with no predicate
on `embedded_content_hash`. During the window a provider outage opens — the one
#201 used to hide entirely — an agent was handed superseded note text as a
current result with nothing marking it. Every other field on that row was
refreshed by the scan; only the chunk text is stale, and only the chunk text is
quotable as the note's content.

**The predicate is `embedded_content_hash IS DISTINCT FROM content_hash`, not
`!=`.** A note that was never embedded, or whose certification a move cleared,
holds `NULL`, and under `!=` that yields `NULL`, which a `WHERE` reads as false
— every never-embedded note would count as *fresh*, the exact inversion of what
the flag is for. `semantic_search` already hydrates the whole `NoteMetadata`
entity, so both hashes are in hand and the comparison is done in Python, where
`!= None` **is** that operator; do not "fix" it into an `is not None` guard.
`find_related_stmt` gained `content_hash`, `embedded_content_hash` and
`chunks_truncated` as projected columns of a table it already joins — scalar
columns, no predicate, so no plan moves, and the recall benchmark's EXPLAIN case
now asserts that they are projected and **not filtered on**, so a predicate
smuggled in later changes what the benchmark measures and the benchmark says so.

**Nothing about the query changes.** No predicate, no fourth `SET LOCAL`, no
change to the overfetch, the re-sort, the dedupe or exact-fallback eligibility.
The annotation is post-processing over rows the query already returned, which is
what keeps every claim in the section above true — and keeps the recall SLO's
baseline meaningful, since no note leaves any result set.

### Why filtering was refused

Removing stale rows was rejected in the issue and again here. It fails in three
ways at once:

- **The edit window.** A note edited a minute ago is stale by construction until
  the next pass commits its new hash, so a filter would hide every note edited in
  the last five minutes from every search.
- **The outage.** During a provider outage the whole vault is stale, so the
  filter empties the result set entirely — turning a degraded answer into no
  answer, at exactly the moment an operator has not yet noticed.
- **The exact fallback.** The owner predicate makes every vector query a
  *filtered* query, and both paths re-run the identical statement as an O(n)
  exact sequential scan on **any** zero-row result. A staleness filter would
  therefore convert an outage into a full scan of the embedding table on every
  single query.

### The preview is withheld; everything else is kept

Of the fields a vector result carries, `path`, `title` and `tags` come from
`notes_metadata`, which the **scan** refreshed — a row is stale precisely
because the scan already committed the new `content_hash` — so those fields
describe the note as it stands now. `similarity` is a retrieval score, not a
claim about content. `chunk` is the only field that is a verbatim quotation of
the note's text, the only one that is out of date, and the one an agent pastes
into an answer.

So a stale row's `chunk` is set to `None` in the service, not clipped in the
renderer: a caller cannot obtain the superseded text at all. The row keeps its
rank, its path and its title — the note is still found, still ranked, still
named — and the preview line becomes an explicit notice naming `read_note`,
which reads the file and is always correct. That notice contains **no text read
from the note**: not the stored chunk, and not the note's current leading text
either, which would be a different span from the one that matched presented
where the matching span goes — a fabricated excerpt, worse than none.

Two alternatives were considered and rejected for the same reason. *Flag it and
keep the preview*: the flag is metadata and the preview is content, and an agent
summarising three results into a paragraph quotes the previews and drops the
metadata. *Return the note's current first 500 characters*: see the fabricated
excerpt above.

Rendering follows `get_links`'s rules (`_degradation_suffix`,
`_degradation_footer` in `src/mcp_server/tools.py`). The header carries the stale
and truncated counts **always, including zero**, because an absent token is not
evidence of absence and an agent cannot otherwise distinguish "no stale rows"
from a build that does not report staleness. Per row, only a *true* marker is
rendered — `stale: false` on fifteen of fifteen rows is noise, not information.
A capped note (see the chunk cap in
[indexing and embeddings](indexing-and-embeddings.md)) carries
`embedding_truncated: true` on the same line, because a match against its head
reads as a match against the whole note.

### `find_related` states a stale source on every return path

The query vector is the mean of the **source's stored** chunk vectors, so a
stale source means every neighbour answers a question about content the note no
longer has — a fact no per-row flag can express. `source_stale` is therefore
computed from the source row every path below it has in hand, and the line
(`_stale_source_line`) is emitted on the ranked path **and on the true
zero-result path**.

The empty case is where it matters most, and a first draft put the line only
above a non-empty list, losing it exactly where it explains the most: a bare
`No related notes for 'X'` from a stale source is the reading an agent acts on —
*this note has no neighbours* — when the truth is that the vector searched with
describes content the note no longer has.

The two operational-failure branches keep their own messages and their own
markers: `related_source_not_found` never loaded a row at all, and
`related_source_not_embedded` is a source with *no* vectors, which is a
different fact with a different fix.

### The declared bound: this reports what the index has committed

`stale` is derived from `notes_metadata`, so it reports a note as stale only
once the scan has committed the note's new `content_hash`. Between an edit
landing on disk and the next scan reaching that note — up to
`INDEX_INTERVAL_SECONDS` plus the pass in flight — the row reads
`embedded_content_hash == content_hash` while the stored chunk is already
superseded, and the result is presented as fresh.

This is not closable from the read path: detecting it would mean hashing the
file on disk for every returned row, which puts a per-result filesystem read on
the hot path of every search and still races the writer. It is therefore
**declared** rather than quietly narrowed. The guarantee this signal makes is:

> No result presents text the index **knows** to be superseded.

Not "no result is ever out of date". The bound is stated in both tools'
docstrings, and the post-deploy exercise sets the state up explicitly — edit a
note, search *before* the pass and observe the row is **not** marked, then search
after the pass and observe that it is. Writing the test that way is what keeps
the residual from being re-described as a guarantee later.

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

