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
- Vector search via a pgvector HNSW **expression** index over
  `(embedding::halfvec(D)) halfvec_cosine_ops` (`m=16, ef_construction=64`,
  *D* = `EMBEDDING_DIMENSIONS`, built only when *D* ≤ 2000; migration 027,
  #283). The query orders by that expression to pick candidates and re-ranks
  them by the full-precision `vector` distance — see "The `halfvec` index"
  below. `semantic_search` sets `hnsw.ef_search=80` per query and dedupes per
  note in Python after a 5x overfetch. See "Filtered vector search" below —
  the `SET LOCAL`s are load-bearing for *correctness*, not just speed.

## Filtered vector search — the SET LOCALs are correctness, not tuning

Both vector paths (`semantic_search` in `src/services/embeddings.py`,
`find_related_impl` in `src/mcp_server/tools.py`) issue three transaction-scoped
settings before the query, and all three matter:

- `hnsw.ef_search = 80` — recall@10 ≈ 98%.
- `random_page_cost = 1.1` — a planner correction, **not** a claim about the
  disk (D20, #283; the comment used to say "SSD"). The planner costs the join's
  heap side at `relpages` and does not model the detoast I/O of a TOASTed
  vector or tsvector, so at the default of 4 it overprices the index path and
  prefers a seq scan + sort, which is fine on a small table and degrades
  linearly. The working set is mostly cached, so the hint is right on an HDD
  too. `full_text_search` issues it for the same reason (above).
- `hnsw.iterative_scan = 'relaxed_order'` — **the recall fix.** With
  `random_page_cost` lowered, the planner picks HNSW → nested loop → filter.
  A non-iterative HNSW scan yields at most `ef_search` candidates; a `folder` /
  `tags` / `frontmatter` / `user_id` predicate then discards most of them and
  *nothing refills*. Measured: 45 of 120 folder-filtered probes returned zero
  rows, 100 returned short. `relaxed_order` keeps walking the graph until the
  overfetch is satisfied after filtering.

Consequences that are easy to undo by accident:

- **Re-sort before dedupe.** `relaxed_order` may emit rows slightly out of
  distance order across iterations, and since #283 the scan is ordered by the
  half-precision expression, so both paths select the **full-precision** cosine
  distance as a column and sort by it before per-note dedupe/truncation. This
  is presentation and ranking only — it cannot recover candidates the scan
  never returned.
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

## The `halfvec` index: half precision picks candidates, full precision ranks them (#283)

Migration 027 replaced 008's `ix_note_embeddings_embedding_hnsw`
(`vector_cosine_ops`, ~146 MB live) with
`ix_note_embeddings_embedding_halfvec_hnsw` over
`(embedding::halfvec(D)) halfvec_cosine_ops`. On a synthetic 17.5 k × 1024
corpus the two measured 137 MB and 46 MB; live after deploy (2026-09-23) the new
index is 45 MB, and on 30 real production queries its top-15 notes overlapped an
exact full-precision scan by a mean of 0.980 (minimum 0.800). The rules that make it safe:

- **One definition.** `src/services/vector_index.py` owns the name,
  `index_enabled(D)` (*D* ≤ 2000), `create_index_sql(D)`, `drop_index_sql()`
  (both names, `IF EXISTS`) and the two query expressions. Migration 027, both
  reset paths (panel and `scripts/reset_embeddings.py`), the pre-warm probe and
  the two vector queries all go through it. The DDL's expression text is
  compiled from the same builder the queries use, and
  `tests/test_perf_vector_index.py` pins that the ORDER BY's operand is the
  index expression verbatim — a planner matches an expression index only on the
  identical expression, and a mismatch is a silent sequential scan.
- **The dimension is configuration, never a literal.** An OpenAI deployment
  runs at 1536 or 3072. Above 2000 nothing is built — the condition under which
  the `vector` index existed — even though `halfvec` could index up to 4,000:
  turning those deployments' exact scan into an approximate one is a recall
  decision this change did not take. Above 2000 the queries order by the plain
  `vector` distance, as before. The query casts to the query vector's own
  length, which the startup dimension check makes equal to the setting.
- **Half precision only chooses the candidates.** Both queries order by
  `embedding::halfvec(D) <=> q::halfvec(D)` (the index expression), select the
  full-precision `embedding <=> q` as `distance`, re-sort the fetched rows by
  it **before** the per-note dedupe, and report `similarity = 1 − distance`.
  So the representative chunk, the result order and every reported number are
  full precision; what half precision can change is only membership at the
  overfetch boundary. The zero-row exact fallback re-runs the same statement as
  a sequential scan ordered by the `halfvec` expression, and the re-sort then
  restores full-precision order among what it fetched. `find_related`'s source
  vector (the mean of its chunks) is unchanged.
- **The recall gate.** It shipped only because
  `tests/integration/test_search_recall.py` met its SLO with the index and the
  casts in place: its fixture builds the index through `create_index_sql`, its
  EXPLAIN assertions name the new index, and its **exact baseline stays a
  full-precision `vector` sequential scan** (a `halfvec` baseline would share
  the precision loss it is meant to measure). Measured on 2026-09-22 (pgvector
  0.8.6, `pg16`): set recall **1.00 on all 60 filtered cases** (4 filter shapes
  × 5 queries × 3 rebuilds) and **1.00 for `find_related`** on all 9 (3 hubs ×
  3 rebuilds) — identical to the `vector` index on the same corpus before the
  change. The corpus is synthetic (L8); the live top-15 overlap check is
  informational.

## notes_metadata is vacuumed on a per-table threshold (#283, D17/D18)

Migration 027 sets `autovacuum_vacuum_scale_factor = 0.02` and
`autovacuum_vacuum_insert_scale_factor = 0.02` on `notes_metadata` (threshold
≈ 50 + 0.02 × 4,100 ≈ 132 dead tuples, against ~870 at the defaults). The table
had never been vacuumed, and that matters to keyword search specifically: the
GIN index's **metapage statistics are written only by VACUUM**
(`ginvacuumcleanup`), and `gincostestimate` reads them — without them it
assumes the whole index must be scanned, which is the same failure the keyword
benchmark's `VACUUM` note below describes. Vacuum also flushes the GIN pending
list. `fastupdate = off` was rejected: it makes every changed note's keyword
write slower and fixes none of the rest.

027 does **not** run `VACUUM` itself: it cannot run inside a transaction block,
and alembic runs the whole chain in one. Autovacuum visits the table within one
`autovacuum_naptime` of 027 committing, because the dead-tuple count already
exceeds the new threshold. `make db-vacuum-notes` is the deterministic
fallback: `VACUUM (ANALYZE) notes_metadata` in an autocommit session through
the application container. The reloptions are per table, so the shared
instance's other tenants are untouched.

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
the flag is for. `semantic_search` projects both hashes (it hydrated the whole
`NoteMetadata` entity until #280), so they are in hand and the comparison is done in Python, where
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

## Read paths project what they render (#280)

Six read statements select an explicit column list instead of whole entities:

| Path | Projected columns |
| --- | --- |
| `semantic_search` | `ne.note_id, ne.chunk_index, ne.chunk_text, nm.file_path, nm.title, nm.tags, nm.content_hash, nm.embedded_content_hash, nm.chunks_truncated, distance` (the `find_related_stmt` shape) |
| `keyword_search` | `nm.file_path, nm.title, nm.tags, rank` |
| `list_notes` | `file_path, file_size, modified_at` |
| `get_recent` / `find_orphans` | `file_path, title, tags, modified_at` |
| `get_neighborhood` hydration | `id, file_path, title, tags` |

None of them ships `notes_metadata.content_tsvector` (the largest TOASTed
column, which the planner does not cost), `note_embeddings.embedding` or
`notes_metadata.frontmatter`. `ts_rank_cd` and `@@` still read the tsvector
*server-side*; what is gone is shipping and hydrating it. **No predicate, no
`SET LOCAL`, no overfetch and no exact-fallback condition changed.** The
staleness fields (`content_hash`, `embedded_content_hash`) and
`chunks_truncated` are still selected wherever they are rendered. A new read
path should follow the same rule: select the columns it renders.

**`content_tsvector` is deferred with raiseload** (D5):
`mapped_column(TSVECTOR, deferred=True, deferred_raiseload=True)`. A
whole-entity `select(NoteMetadata)` (the path lookups in the graph tools, the
indexer's per-note loads) no longer carries it, and code that reads
`.content_tsvector` from a loaded entity raises `InvalidRequestError` instead
of lazy-loading. Under `AsyncSession` a lazy load is an implicit-IO error
anyway (`MissingGreenlet`); raising names the offending access in a test
instead. Every writer is SQL text or `insert().values`, which deferral does not
touch. A reader that genuinely needs the vector selects the column explicitly.
`frontmatter` was assessed and **left eager**: it is small on the median note,
filtered in SQL, and deferring it would turn any future entity reader on a
write path into a runtime error for little gain, because every hot path above
already projects it away.

**Similarity is `1 − distance`** (D7), as `find_related` already reported it.
The result order was always the database's distance order, and the old NumPy
recomputation ran *after* the sort, over vectors fetched only for that. It
differed from the distance the rows were sorted by only in float error
(pgvector accumulates in single precision, NumPy in double, ~1e-6). That error
could make the displayed similarity non-monotone against the displayed order.
It cannot now. This resolved L10 in [indexing and
embeddings](indexing-and-embeddings.md).

**Exact ties are deterministic.** `list_notes` and `get_recent` order by
`modified_at DESC, file_path ASC`. `find_orphans` orders by
`modified_at DESC NULLS LAST, file_path ASC`, so notes with no modification time
stay last. `semantic_search`'s in-Python re-sort keys on
`(distance, file_path, chunk_index)`. `keyword_search` already broke rank ties
on `file_path`. Before this, an exact tie was resolved by whatever row the sort
saw first.

**What "identical" means for this change.** The results are the same as the
pre-change implementation: the same result set, the same order, every field
other than `similarity` byte-equal, and `similarity` within 1e-5. There are
exactly three permitted exceptions, and all of them are confined to exact ties
of the sort key (`modified_at`, `rank` or distance):

1. **membership at a tied cutoff**: when more rows share the boundary key than
   fit under the limit, which of them are returned may differ;
2. **order among exact ties**: rows with an equal key may appear in a different
   relative order, even when all of them fit (L9 in the change);
3. **the representative chunk among exact distance ties**: when two chunks of
   one note are exactly equidistant, the kept `chunk_index` and its preview may
   differ (the lower index now wins).

`tests/integration/test_perf_projection_identity_pg.py` enforces this. It runs
the pre-change implementations, copied in as oracles, next to the production
ones on the recall benchmark's corpus plus keyword, link, tie and orphan
structure. It covers stale, truncated, filtered, unfiltered and exact-fallback
cases, and plants each of the three tie cases on purpose. Its comparator is
itself tested to reject every other difference. The offline half,
`tests/test_perf_projection.py`, compiles each statement and asserts the
projection and the raiseload mapping.

## The query length cap (`MAX_SEARCH_QUERY_CHARS`, #194)

`keyword_search` and `semantic_search` refuse a `query` longer than
`MAX_SEARCH_QUERY_CHARS` (8,192 — a module constant in `src/config.py` beside
`MAX_LIST_PATTERN_CHARS`, not an operator setting).

**It is enforced on the decorator, not in the search bodies.** `_tracked`
carries `arg_char_caps={"query": MAX_SEARCH_QUERY_CHARS}` on both tools, beside
the existing unencodable-argument screen (#149) — a generic argument screen
already lived there, so the mechanism generalises to any future argument. Being
a **pre-body** gate is the whole point: the refusal happens before the
embedding-provider call, before the `tsquery` parse, before any search or quota
statement, and before the value is interpolated into a server-authored string.
Enforcing it inside the bodies would have made it a post-body marker polluting
the latency percentiles. The refusal names the argument, its length, the limit
and the setting, and **never echoes the argument**.

**Provider cost is not the reason.** #194's own verification withdrew the
cost-amplification claim: Ollama truncates to the model context and OpenAI
rejects input over its token limit, so an over-long query was never a way to
spend the operator's money. The three real reasons are an unbounded argument
interpolated into a server-authored result string (the #149 discipline),
`tsquery` parsing on the single event loop (the #204 class), and an OpenAI
deployment turning an over-long query into a raw provider error where the
contract promises a typed refusal.

**A character cap cannot promise a token limit, so the provider's own limit is
handled separately, in the body.** 8,192 characters of a densely-tokenizing
script can still exceed what a provider accepts. When one rejects the input for
its own limit, `src/services/embeddings.py` raises
`refusals.ProviderInputTooLarge` (declared in `src/services/refusals.py`, which
imports nothing from the app, so the raiser and the handler share a
dependency-free contract) and `semantic_search_impl` translates it into the
**same caller-facing `argument_too_long` code** carrying the provider's stated
reason — one actionable failure mode for "the query was too large", whichever
limit applied. Its usage marker is deliberately different:
`provider_input_rejected`, classified **post-body** and *not* in
`pre_body_refusal_sql()`, because the body ran, resolved a vault and paid for a
network round trip, and enumerating it would drop the most expensive class of
call in the server out of the percentiles. The caller-facing code and the
operator-facing marker answer different questions and are permitted to differ;
see [rate limits](rate-limits.md) and [usage attribution](usage-attribution.md).

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

