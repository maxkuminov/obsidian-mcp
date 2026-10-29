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
  seq-scan. Since #344 the vector is `A(title) || A(aliases) || D(body)`,
  ranking is `ts_rank_cd(RANK_WEIGHTS, …)`, an under-filled page is filled
  from a disjunction derived from PostgreSQL's own parse, and each row
  carries a page-bounded snippet — see the section below.
- Vector search via a pgvector HNSW **expression** index over
  `(embedding::halfvec(D)) halfvec_cosine_ops` (`m=16, ef_construction=64`,
  *D* = `EMBEDDING_DIMENSIONS`, built only when *D* ≤ 2000; migration 027,
  #283). The query orders by that expression to pick candidates and re-ranks
  them by the full-precision `vector` distance — see "The `halfvec` index"
  below. `semantic_search` sets `hnsw.ef_search=80` per query and dedupes per
  note in Python after a 5x overfetch. See "Filtered vector search" below —
  the `SET LOCAL`s are load-bearing for *correctness*, not just speed.

## Keyword ranking, the disjunctive fill and the snippet (#344)

`keyword_search("<exact two-word title of an existing note>")` returned ten
rows without that note, live, on 2026-10-28. Three causes, three changes.

**The vector weights the title and aliases above the body.** Every writer
builds `content_tsvector` through `fts.index_tsvector_sql` as *title parts for
every config, then aliases parts, then the content parts* (configs in sorted
order — the fingerprint's order, so 031 and the writers agree byte for byte) — `setweight(…,
'A')` on the first two, the pre-031 `to_tsvector(cfg, body)` fragment byte for
byte on the last, so a pre-031 vector is exactly the body part of a post-031
one. Ranking is `ts_rank_cd(RANK_WEIGHTS, vector, query)` with the array bound
as `float4[]` (a setting, default `{0.02, 0.2, 0.4, 1.0}` for `D, C, B, A`,
never interpolated; each entry finite and in `[1e-6, 1]` — a smaller positive
value binds as float4 zero, and PostgreSQL refuses one above 1). `calc_rank_cd` scores one term per cover,
`len(cover) / Σ(1/weight)`: an adjacent two-word title cover is `2/(1+1) =
1.0`, a body cover `2/(50+50) = 0.02`. Measured: the issue's shape — a title
`Quarterly Review` against a hub repeating the phrase thirty times — scores
`1.0` against `3.725` under PostgreSQL's default array (the hub wins) and
`1.0` against `0.745` under ours (the title wins; the crossover is near forty
adjacent repetitions, fifty for a one-word title — L8). The array moves only
title-versus-body competition: body-only order is identical under any uniform
`D`, which the identity suite pins. Normalisation flags were rejected (flag 1
still loses the issue's case; flag 2 makes every short note beat every long
one) and would re-rank body-only matches.

**Conjunction first, disjunction to fill — derived from PostgreSQL's parse.**
The conjunctive statement is the pre-#344 one plus `id` in the projection and
the weights in the rank (the GIN plan `test_keyword_plan.py` pins is
unchanged); its rows are `match: all`. When it returns fewer than `limit`
rows, the service asks PostgreSQL for `websearch_to_tsquery(cfg, :q)::text`
per config — a no-table statement — and `fts.parse_conjunction` reads that
output grammar, **never the caller's string**: a *plain conjunction* has no
`|` and no parenthesis and splits on `&` into units that are a negated lexeme
or a `<->`/`<N>` phrase chain. Per config: a plain conjunction with ≥ 2
positive units is *eligible* and contributes `(u1 | … | uk) & !n1 …` built
from the verbatim tokens and cast with the `tsquery` input function (which
does not re-stem — `'running'::tsquery` stays `'running'`, measured); an
ineligible config (an `OR`, a negated phrase `!( 'a' <-> 'b' )`, a single
word, stop words only) contributes its **own unchanged AND expression**. The
OR over configs of those forms is implied by the OR over configs of the AND
parses, so the fallback is a superset of the conjunction by construction,
whatever mix of eligible and ineligible configs a query yields — the case
that forced this (Codex r1 #3) is `english,simple` on `the running`: english
parses to `'run'`, simple to `'the' & 'running'`, and `runs fast` matches the
conjunction only through english. The fallback statement is the conjunctive
one with the fallback tsquery, `AND NOT (v @@ <and query>)` (the conjunctive
block was not full, so that excludes exactly its rows), the same filters,
`rank DESC, file_path ASC`, `LIMIT limit − n`; its rows are `match: any`,
appended after every `all` row and never interleaved (their rank is against
a different query — L1). **Every statement of the call reads one
snapshot**: the transaction opens with `SET TRANSACTION ISOLATION LEVEL
REPEATABLE READ, READ ONLY` before the `SET LOCAL`, because the fallback's
`NOT (v @@ <and query>)` excludes exactly the conjunctive block only when
both statements see the same rows — at READ COMMITTED an index commit
between them could drop a note from both blocks for that call (Codex C-1).
`SET TRANSACTION` is legal only before the first snapshot-taking statement;
`SET LOCAL` takes none, and `search_notes_impl` hands over a session with
nothing run. Read-only REPEATABLE READ raises no serialization failure. The
superset property is asserted over a generated
query corpus under `english` and `english,simple`. Telemetry: `fallback`
(`ran | skipped | not_needed`) and `any_rows` in `usage_logs.params`.

`matched_terms` is per config: for each config whose parse is a plain
conjunction of ≤ 32 units, the count of its positive units present in the
vector over that count; the row renders the pair with the greatest ratio
(ties: the greater total, then config order) — `1/1` for the `runs fast` row
above, not `0/2`.

**The snippet comes from a stored excerpt, over the page only.** The body is
not stored anywhere usable (`chunk_text` is embed-time, masked, capped and
stale under #200; a per-result file read is what this document already
declined), so migration 031 adds `notes_metadata.body_excerpt`: the first
`KEYWORD_EXCERPT_CHARS` (20,000) characters of exactly the prefix the vector's
successful attempt bound, written in the same UPDATE, preserved by moves,
sentinel-free, deferred with raiseload, **never projected** — `ts_headline`
reads it server-side. The snippet statement names the ids of the first
`KEYWORD_SNIPPET_ROWS` (20) rows of the assembled page, whatever `limit` is,
and projects one headline per config (`MaxFragments=2, MaxWords=18,
MinWords=6`, U+0002/U+0003 as `StartSel`/`StopSel`, rendered `«»`),
`matched_in` from `ts_filter(v, '{a}') @@ q` / `ts_filter(v, '{d}') @@ q`, and
the per-config `matched_terms` counts (the parse statement therefore runs
whenever snippets are on, even on a full page). A headline counts as the
row's snippet **only when the body part itself matched** — with no full
cover `ts_headline` still marks single query words, so a title-only match
whose body holds one of the words would otherwise show a highlight that is
not the match. A second statement supplies the `head:` text
(`ts_headline(fts_configs[0], …, 'MaxFragments=0, MinWords=18,
MaxWords=24')` with the same sentinels, stripped from the result — never
PostgreSQL's default `<b>`, which a note may contain literally; in fragment
mode a no-cover headline is only `MinWords` words, measured) for every other
row with an excerpt: a title/alias-only match, a split match (rendered
`matched_in: title+body`), or a match beyond the excerpt (L3). Nothing fabricated: `head:` is text the index holds for that
note, labelled as not the matching span. Rows beyond the cap carry no
`snippet`/`head` key and the header says `snippets: first N`; a row with a
NULL excerpt (indexed before 031) carries neither, the header counts such
rows always, including zero, and the footer names `make rebuild-tsvectors`.
`snippet:` and `head:` are **rendered on one line** — every whitespace run,
line breaks included, becomes one space — because `ts_headline` keeps the
excerpt's newlines and markdown, and a note body could otherwise print a
line that reads as another server-authored result row (Codex C-2). A
literal `«`/`»` in a note still collides with the rendered markers
(accepted: the stored markers are U+0002/U+0003, stripped from excerpts).
Cost is a function of the row cap, the excerpt cap and the config count —
never of `limit` — recorded as `snippet_ms`; the integration suite gates the
worst case (twenty distinct 20,000-character excerpts, `english,simple`,
head rows included) under 300 ms on the throwaway container, after Codex
measured ≈ 452 ms / 891 ms for a `limit`-bound design. That gate is at the
defaults; the cost grows roughly linearly with both settings, whose maxima
are about 25× the measured work.

**What is invariant, and what the oracle checks.** The projection identity
suite can no longer claim byte identity for `keyword_search` — `rank` is a
pre-change field and every body-only rank scales, and title-versus-body order
changes on purpose — so its keyword case asserts what is: conjunctive
membership equals the oracle's (`@@` and the filters did not change), body-only
relative order equals the oracle's up to ties, rank values and title-matched
rows' positions are exempt, and the new contract is asserted by its own
tests.

**031 and the move paths** are described in [schema and
migrations](schema-and-migrations.md#031-notes_metadatabody_excerpt-and-the-in-sql-re-head-344)
and [indexing and embeddings](indexing-and-embeddings.md): the migration
re-heads every vector in SQL under the generation lock with the stored
fingerprint's configs and skips rows already headed (`rehead` keeps relative
positions, not absolute ones); its downgrade strips every `A` lexeme; both
move paths re-head under the lock with the fingerprint re-read; and `make
deploy` stops the app before a pending migration so an old-image move cannot
leave a stale title at weight `A`.

Accepted limitations (the change's design, L1–L12): ranks stay
uninterpretable and an `any` row's rank is against a different query;
phrase adjacency across the head/body boundary; `head:` for a match beyond
the excerpt or in the title only; pre-031 rows have no excerpt until edited
or rebuilt; no fallback when no config is eligible; `matched_terms` omitted
past 32 units; U+0002/U+0003 removed from excerpts; the weight array is a
heuristic (#356 measures it); no hybrid fusion (#354); a 54000 row stays
unweighted after 031 until written again; repeated re-heads shift body
positions; snippets for the first `KEYWORD_SNIPPET_ROWS` rows only.

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
| `keyword_search` | `nm.id, nm.file_path, nm.title, nm.tags, rank` (both ranking blocks); the snippet statement projects `id`, one `ts_headline` per config, the two `matched_in` flags, `has_excerpt` and the `matched_terms` counts — `body_excerpt` is read inside `ts_headline` and never projected |
| `list_notes` | `file_path, file_size, modified_at` |
| `get_recent` / `find_orphans` | `file_path, title, tags, modified_at` |
| `get_neighborhood` hydration | `id, file_path, title, tags` |

None of them ships `notes_metadata.content_tsvector` (the largest TOASTed
column, which the planner does not cost), `notes_metadata.body_excerpt`,
`note_embeddings.embedding` or `notes_metadata.frontmatter`. `ts_rank_cd`,
`@@` and `ts_headline` still read the tsvector and the excerpt *server-side*;
what is gone is shipping and hydrating them. **No predicate, no `SET LOCAL`,
no overfetch and no exact-fallback condition of the vector paths changed.**
The staleness fields (`content_hash`, `embedded_content_hash`) and
`chunks_truncated` are still selected wherever they are rendered. A new read
path should follow the same rule: select the columns it renders.

**`content_tsvector` and `body_excerpt` are deferred with raiseload** (D5,
#344): `mapped_column(…, deferred=True, deferred_raiseload=True)`. A
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

**What "identical" means for this change.** For the vector, listing and graph
paths the results are the same as the pre-change implementation: the same
result set, the same order, every field other than `similarity` byte-equal,
and `similarity` within 1e-5. (`keyword_search`'s clause was narrowed by #344
— see "What is invariant" above.) There are exactly three permitted
exceptions, and all of them are confined to exact ties of the sort key
(`modified_at`, `rank` or distance):

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
`semantic_search`, `db_ms` + `exact_fallback` for `find_related` (it makes
no embedding call), and `fallback` + `any_rows` + `snippet_ms` for
`keyword_search` (#344). A single whole-call `duration_ms` could not separate the
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

