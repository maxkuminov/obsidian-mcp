# Design: keyword ranking, OR fallback and snippets (#344)

## Context

What the code holds today, established by reading it (not the issue):

- **The body is not stored.** `notes_metadata` carries `title` (the shared
  `note_title` rule: sanitised frontmatter `title`, else the filename stem,
  bounded to 512), `tags`, `frontmatter` (JSONB, sanitised by
  `_sanitize_frontmatter`), `content_hash` and `content_tsvector` — never
  the text. `note_embeddings.chunk_text` holds *cleaned* chunk text at
  embed time (stale under #200's rules, code-masked, capped per note), so
  it is not the text the keyword vector was built from. The rebuild
  (`_rebuild_tsvectors_pinned`) re-reads every file for the same reason.
- **Aliases are not parsed anywhere.** The only alias handling in `src/`
  is YAML anchors. The stored JSONB carries whatever the note's
  frontmatter said under `aliases` (list, scalar, anything). The tag rule
  in `vault.extract_tags` (list → `str()` each; scalar string → comma
  split; NUL removed; byte-bounded) is the model.
- **The content slice.** Both writers go through `write_tsvector_bounded`:
  the full body first, halving per savepoint to a floor of exactly 100,000
  characters (#127). The text a vector was built from is therefore
  `content[:used]` for a `used` the helper knows.
- **Pre-change vectors are all weight D.** `to_tsvector` assigns `D` to
  every lexeme; nothing in the schema ever called `setweight`. `tsvector ||
  tsvector` is associative and shifts the right operand's positions by the
  left's maximum, so `head || stored` equals what a fresh build of
  `head || body` writes — which is what lets migration 031 re-head rows in
  SQL with no file access.
- **Both move paths preserve `content_tsvector`** (indexer id-preserving
  move UPDATE; `move_note`'s `update(NoteMetadata).values(...)`), and the
  scan skips an unchanged hash. #127 D3 fixed the stem-derived title on
  move for exactly this reason; putting the title *inside* the vector
  reopens it unless both paths rewrite the head.
- **The keyword fingerprint** (`indexer_state.fts_fingerprint`,
  `{"configs":[…],"v":1}`) names the configs the stored vectors were built
  under; startup fails closed on a membership difference and the rebuild
  is the only other writer. `FINGERPRINT_VERSION` bumps every fingerprint
  (embedding too) and would demand a reset instruction.
- **The rebuild on k8s** is a documented one-off Job (`python -m
  scripts.rebuild_tsvectors`, `docs/deployment-kubernetes.md` "Index
  maintenance") run with the Deployment scaled to 0 for the RWO vault
  volume. The manifest lives only in the doc; the bundle has
  `vault-init-job.example.yaml` as the precedent for a checked-in example.
  k8s deploys with `strategy: Recreate` and a `migrate` initContainer, so
  there is no old/new overlap there; compose's `make deploy` migrates
  **before** `--force-recreate`, so the old container may run one more
  tick after 031 commits.
- `test_perf_projection_identity_pg.py` runs the pre-#280
  `_old_full_text_search` oracle beside production and requires identical
  rows; the corpus builds its vectors through `index_tsvector_sql`.

The expensive failure here is a *silently wrong* result handed to an
agent. Every decision below is ranked by that first, agent ergonomics
second, cost third.

## Goals / Non-goals

Goals: a note whose title or alias is the query ranks first; a query that
over-specifies still returns the closest notes, visibly marked as partial
matches; a result row says why it matched and shows the matching text
without a `read_note`; every stored vector is weighted at deploy without an
operator step and without a startup refusal; the GIN plan the #283 work
established is untouched.

Non-goals: hybrid fusion (#354); a title-only search mode; changing
`ts_rank_cd` normalisation (scores stay uninterpretable, L1); per-user FTS
configs; self-healing the excerpt from the periodic pass (D5, offered as an
owner decision).

## D1 — The vector: `A(title) || A(aliases) || D(body)`, one builder

`index_tsvector_sql(content_bind="content", title_bind="title",
aliases_bind="aliases")` returns, per configured config in order:

```
setweight(to_tsvector(CAST(:fts_cfg_i AS regconfig), :title),   'A')
  || setweight(to_tsvector(CAST(:fts_cfg_i AS regconfig), :aliases), 'A')   (all configs first)
  || to_tsvector(CAST(:fts_cfg_i AS regconfig), :content)                   (all configs, as today)
```

Concatenation order is **title parts for every config, then aliases parts
for every config, then content parts for every config** — the content part
is byte-identical to today's fragment, so the existing multi-config
reasoning (`||` over lexeme sets, order-insensitive for matching) stands.
The body stays implicit `D`; a test pins `ts_filter(to_tsvector(x),
'{d}') = to_tsvector(x)`, which is what D4's `rehead` relies on.

Two sibling helpers share the head fragment: `head_tsvector_sql(title_bind,
aliases_bind)` (the A parts alone) and `rehead_tsvector_sql(title_bind,
aliases_bind)` = `head || ts_filter(content_tsvector, '{d}')`, which
replaces a vector's head from stored columns without the body. What
`rehead` preserves, measured (see "Measured on PostgreSQL 16" below): the
body's lexemes, their weights and their **relative** positions — `@@`,
`<->`/`<N>` and `ts_rank_cd` give identical answers before and after. What
it does not preserve: the body's **absolute** positions. `ts_filter` keeps
the positions the previous concatenation assigned, so each application
shifts the body by the previous head's length (`Alpha(1A) bodi:2 text:3`
re-headed with `Beta` gives `beta:1A bodi:3 text:4`, where a fresh build
gives `bodi:2 text:3`). Over a weight-D-only vector — every pre-change row —
the first application is byte-exact; repeated applications drift (L11).
That is why migration 031 skips rows that already carry a weight-A part
(D5) and why the move paths, which may re-head the same row many times, are
specified on matching and ranking rather than on bytes (D6).

**Title** is `notes_metadata.title` — the value the writer already binds
(the incremental pass computes it from the parsed entry; the rebuild
recomputes it from the file it reads and hashes; `move_note` and the
indexer's move path have the new one in hand by #127 D3). Not the raw
frontmatter: the shared rule is what every surface shows, and a title that
stems to nothing (`The`) yields an empty head, which is correct.

**Aliases** are `note_aliases(frontmatter) -> list[str]`, a new rule in
`src/services/fts.py` beside the builders (it imports nothing from the
app, so the migration and the request path can both use it):

- keys `aliases` then `alias` (Obsidian's documented key and the legacy
  spelling), both read, order preserved, duplicates dropped;
- a list → each scalar item rendered by the title rule's scalar rendering
  (`str()`, a non-finite number as its YAML token, a date as ISO), nested
  lists and mappings dropped; a scalar string → comma-split and stripped
  exactly as `extract_tags` treats a scalar `tags` string; another scalar →
  rendered the same way;
- U+0000, U+0002 and U+0003 removed (D4's sentinels); empty strings
  dropped;
- the joined text (`"\n"` between aliases) is cut at
  `FTS_ALIASES_MAX_CHARS = 2048` characters — the head must stay small
  against the 1 MiB tsvector limit and the halving retreat only ever
  shrinks the **body** bind.

The rule is total over the sanitised JSONB **and** over a freshly parsed
mapping, and a test pins that both inputs yield the same list for the
same note (the rebuild and `move_note` parse; the migration reads JSONB).

Alternatives rejected: indexing aliases into `tags` (changes a rendered
field and the tag filter's semantics); a separate `aliases` column (the
vector is the only consumer; the JSONB already holds the data).

## D2 — Rank: `ts_rank_cd` with an explicit weight array

`ts_rank_cd(RANK_WEIGHTS, content_tsvector, query)` with
`RANK_WEIGHTS = '{0.02, 0.2, 0.4, 1.0}'` (`D, C, B, A`), one constant in
`fts.py`, used by both blocks of D3.

Why not PostgreSQL's default `{0.1, 0.2, 0.4, 1.0}`: `calc_rank_cd` sums
one term per cover, `len(cover) / Σ(1/weight)`, so an adjacent two-word
cover scores `2/(1+1) = 1.0` at `A` and `2/(10+10) = 0.1` at default `D`.
Measured on PostgreSQL 16: a title `Quarterly Review` alone ranks `1.0`
for `quarterly review`; a body repeating `quarterly review filler words
here` thirty times ranks **3.725** under the default array (the titled
note loses, which is the issue) and **0.745** under `{0.02, …}` (it wins;
the crossover is near forty adjacent repetitions, because overlapping
covers add a little beyond the `30 × 0.02` estimate). For a one-word
query, sixty body occurrences rank `1.2` against the title's `1.0`, so
the crossover there is fifty. Relative order *among* body-only matches is
unchanged — the scale is uniform there — so the array only decides
title-versus-body competition, which is the bug. `B`/`C` are never written
and keep their defaults. The number is a heuristic and recorded as L8;
#356's eval set is where it gets measured.

Normalisation flags were rejected: flag 1 (`1 + log(length)`) still lets
the 10 k-word note win in the issue's shape, flag 2 (`/length`) makes
every short note beat every long one. Both change scores for body-only
matches, which this change promises not to re-rank.

## D3 — Conjunction first, disjunction to fill; the fallback is derived from PostgreSQL's parse

The AND statement is today's, plus `NoteMetadata.id` in the projection
(for D4) and D2's weights: `combined_tsquery(query)` (websearch per config,
OR'd), `@@`, `apply_note_filters`, `ORDER BY rank DESC, file_path ASC
LIMIT :limit`. Its plan shape is unchanged; `test_keyword_plan.py` still
asserts the GIN bitmap scan.

If it returns `n < limit` rows, the service asks PostgreSQL for the text
form of **each config's** websearch parse (`SELECT
CAST(websearch_to_tsquery(CAST(:cfg AS regconfig), :q) AS text)` — a
constant-time statement with no table access), and
`fts.parse_conjunction(text)` reads PostgreSQL's own output grammar:

- tokens: a quoted lexeme (`'…'`, embedded quotes and backslashes doubled
  — the tokenizer only needs to find the closing quote and passes the
  token through verbatim), `&`, `|`, `!`, `<->`, `<N>`, `(`, `)`;
- the parse is a **plain conjunction** iff it contains no `|` and no
  parenthesis, and splits on `&` into units, each of which is either a
  negation (`!` followed by exactly one lexeme) or a phrase chain (one
  lexeme, or lexemes joined by `<->`/`<N>`);
- anything else — a `|` (the user wrote `OR`), a parenthesis (a negated
  phrase, `-"a b"`), an unknown token — means **no fallback**: the AND rows
  stand alone and telemetry records `fallback: "skipped"`.

With a plain conjunction holding `k ≥ 2` positive units, the fallback text
for that config is `(u1 | u2 | … | uk) & !n1 & … & !nm` built from the
verbatim unit tokens, and the fallback tsquery is `CAST(:fallback_i AS
tsquery)` per config, OR'd with `||` like `combined_tsquery`. The
`tsquery` input function takes quoted lexemes **verbatim** — no stemming,
no stop-word pass — so the fallback's stems are exactly the AND parse's,
including stop-word gaps (`<2>`) inside a quoted phrase. Nothing from the
caller's string reaches `to_tsquery`; the only caller-derived input is the
same `:q` bind the AND statement already uses. `k = 1` means the OR equals
the AND and the fallback is skipped (`fallback: "not_needed"` when `n =
limit`).

The fallback statement is the AND statement with the fallback tsquery in
both `@@` and `ts_rank_cd`, `AND NOT (content_tsvector @@ <and tsquery>)`,
the same filters, the same ORDER BY, `LIMIT :limit - n`. Because the AND
block was not full, every AND-matching row is in it, so the predicate
excludes exactly those rows. `(a & b & !n) ⇒ ((a | b) & !n)` for every
vector, so the union is a superset of the AND set — asserted on Postgres
over a generated query corpus as the safety property of the parser.

Both statements and D4's run in one transaction after `SET LOCAL
random_page_cost = 1.1`; rows are assembled `all` block then `any` block,
each in its own `rank DESC, file_path ASC`, never interleaved — an `any`
row's rank is against a different query and is not comparable (L1).

Alternatives rejected: parsing the user's string in Python (reimplements
websearch's quoting and operator rules and will diverge from the stemmer);
`querytree()` (drops negations, which the fallback must keep);
`to_tsquery(cfg, 'a | b')` on lexemes (re-stems a stem; Snowball is not
idempotent on every input).

## D4 — The snippet source is a stored excerpt of the indexed text

Three candidates for `ts_headline`'s source:

- **Read the file on the result path.** Rejected: a per-result filesystem
  read on the hot path, through the owner's root and the anchored
  descriptors, is what `search.md` declined for staleness detection; and
  the file's *current* text is not what the vector matched, so a highlight
  can be absent for a true match or present for a superseded one.
- **`note_embeddings.chunk_text`.** Rejected: embed-time, provider-gated,
  code-masked, capped per note and stale under #200's rules — precisely
  stale for the just-written note the keyword arm exists to find.
- **A stored excerpt written with the vector** (chosen).
  `notes_metadata.body_excerpt text NULL`: the first
  `KEYWORD_EXCERPT_CHARS = 20_000` characters of `content[:used]` — the
  same bind the vector's successful attempt used, so the excerpt is a
  prefix of exactly the text that was indexed (`write_tsvector_bounded`
  binds `excerpt = content[:min(length, CAP)]` per attempt). Written by
  the incremental pass and the rebuild in the same UPDATE as the vector;
  preserved by both move paths; NULL means "not written by this build".
  Stored NUL-free (already) and with U+0002/U+0003 removed, so those two
  bytes can serve as headline sentinels (L7). Mapped
  `deferred=True, deferred_raiseload=True` like the vector: no read path
  ships it (`#280`'s rule), the headline is computed server-side.

Storage: ≤ 20 KB per note, ~12 MB on the reference vault, TOASTed beside
a 36 MB tsvector TOAST table that already holds the same words. Write
amplification: one more TOAST value per changed note per pass, the same
cardinality as the vector.

Why 20,000: it covers the median note many times over and most long notes
whole, and it bounds `ts_headline`'s parse to `limit × 20 KB` per search
(default page: ≤ 400 KB; `limit=500`: ≤ 10 MB, the caller's own cost,
timed separately). A match beyond the excerpt, or a match only in the
head (title/aliases are not in the excerpt), yields no highlight and the
row shows the excerpt's leading words as `head:` with `matched_in` saying
where it matched (L3). Nothing fabricated: `head:` is text the index holds
for that note, labelled as not the matching span.

**The snippet statement** is a third statement, over the page only:
`WHERE id = ANY(:ids)` for the block's ids, projecting per row
`ts_headline(CAST(:cfg_i AS regconfig), body_excerpt, <block tsquery>,
:opts)` for each configured config (the first headline containing the
sentinel wins), `ts_filter(v, '{a}') @@ q` as `matched_title`,
`ts_filter(v, '{d}') @@ q` as `matched_body`, and `matched_terms` = `Σ (v
@@ CAST(:unit_j AS tsquery))::int` over the plain conjunction's positive
units when D3 parsed one and `k ≤ 32`, else omitted (L6). `:opts` is
`MaxFragments=2, MaxWords=18, MinWords=6, StartSel=\x02, StopSel=\x03,
FragmentDelimiter=" … "`. When no config's headline carries the sentinel,
the row's `head:` text is a second call, `ts_headline(cfg_0, body_excerpt,
q, 'MaxFragments=0, MinWords=18, MaxWords=24')` — in fragment mode a
no-cover headline is only `MinWords` (six) words, measured, while
`MaxFragments=0` with no cover returns the document's first eighteen
words — evaluated behind a `CASE` so a highlighted row pays for one parse
per config and a `head:` row for one more. One statement per non-empty
block (the query differs). Its cost is a function of the page, never of
the candidate set, and it is timed as `snippet_ms` in `usage_logs.params`
beside `db_ms`.

Rendering (`search_notes_impl`): header `Found N results for 'q' — all
terms: a, any term: b — without excerpt: e` (counts always, including
zero, #200's rule); per row `- **title** (`path`) [tags] — rank: r —
match: all|any — matched_in: title,body — terms: k/n` and a second line
`snippet: …` (sentinels rendered `«` `»`) or `head: …`; a row with NULL
excerpt has no second line, and a footer names `make rebuild-tsvectors`
when `e > 0`. The snippet is a note-controlled field: never cut by the
server (ts_headline bounds it), never marked inside.

## D5 — Migration 031: the column, and an in-SQL re-head of every row

031 (revises 030) does two things in one transaction:

1. `notes_metadata.body_excerpt text NULL`, no default, marked comment
   mirrored in `src/models/db.py`; reconcile-or-refuse on a re-run (030's
   shape); marked downgrade.
2. Re-head every row holding a vector, **without reading any file**:
   - takes `pg_advisory_xact_lock(INDEX_GENERATION_LOCK_KEY)` first — the
     key literal is mirrored from `index_state.py` and a test pins
     equality — so a compose host's still-running old container cannot
     commit a vector between the migration's read and write (its pass
     holds the same lock);
   - reads the configs from the stored `fts_fingerprint`: present →
     its `configs`; absent → `settings.fts_configs` (what startup's
     adoption would bless, L8 of #206); unreadable → refuse, naming the
     key (the guard's own rule: a value this build cannot read certifies
     nothing, and overwriting rows under a guessed config is the mixed
     stemmer startup exists to prevent);
   - per row `(id, title, frontmatter)` with `content_tsvector IS NOT
     NULL`: `UPDATE … SET content_tsvector = rehead(:title, :aliases)
     WHERE id = :id AND length(ts_filter(content_tsvector, '{a}')) = 0`,
     aliases from `note_aliases(frontmatter)`, each in a savepoint.
     SQLSTATE 54000 (the 1 MiB limit on a vector already at the edge)
     leaves that row unchanged, is counted and logged by id (L10); any
     other error aborts the migration. **Idempotent because of the
     predicate, not because of `ts_filter`**: a pre-change vector has no
     weight-A lexeme (`to_tsvector` emits only `D`), so the first
     application is byte-exact, and a row that already carries a head is
     skipped on a re-run; a row whose head stems to nothing (title `The`)
     carries no A part either, and re-heading it with an empty head shifts
     nothing. Without the predicate a stamp-back re-run would shift every
     body's positions by its head's length (D1).
   - `statement_timeout` raised to 15 min for the migration (027's
     device), `search_path` pinned and asserted.

Why in the migration and not "leave the column, require the rebuild":
with deploy-on-merge, a build whose ranking is wrong until an operator
scales to zero and runs a Job is the issue's bug shipped under a new
name; the head needs only stored columns, and a few thousand small
UPDATEs take seconds. Why no fingerprint change: weights are not
configuration, every writer after 031 produces the new shape, and a
`FINGERPRINT_VERSION` bump would also invalidate the embedding
fingerprint. Why no startup refusal: an unweighted row is a ranking
defect, not the false-positive class that justified failing closed on
`FTS_CONFIGS`.

**The excerpt is not backfilled** by 031 (no source without files). It is
populated by the next content change of each note or by one run of the
existing rebuild — `make rebuild-tsvectors` on compose; on k8s the Job the
docs already describe, which this change checks in as
`deploy/kubernetes/base/rebuild-tsvectors-job.example.yaml` so the command
is `kubectl apply -f` of a known file (plus the scale-to-zero step the doc
prescribes for the RWO volume). Startup logs one WARNING with the count of
`body_excerpt IS NULL AND content_tsvector IS NOT NULL` rows and names the
command; no refusal, no `/health` change (L4). **Owner decision offered:**
let the full-hash pass (startup, every 24 h, panel Reindex — it reads
every file anyway) write the excerpt for rows lacking one. Declined here
because it changes the indexer's skip rules (C1–C8) in the most delicate
module for a one-time upgrade step; recorded as a follow-up if the
operator step proves annoying.

**The compose overlap window.** 031 commits before `--force-recreate`; the
old container's next tick may write an unweighted vector for a note
changed in that window. The next edit, or the rebuild, repairs it (L4).
k8s has no such window.

## D6 — Both move paths re-head the vector

The indexer's id-preserving move UPDATE and `move_note`'s metadata UPDATE
each add `content_tsvector = rehead(:title, :aliases)` to the statement
that already rewrites `title` (#127 D3): the indexer binds the parsed
entry's title and `note_aliases(entry frontmatter)`; `move_note` binds the
title and aliases it derived from the moved file, with the same declared
best-effort fallback to the stored JSONB when that read or parse failed.
`body_excerpt` is untouched by both — the body did not move. A
`set_frontmatter` or `edit_note` that changes aliases changes the hash and
the pass rewrites the whole vector; no third writer is needed.

The move paths are specified on what matters — the new title matches at
`A`, the old one does not, the body's lexemes, weights and relative
positions are unchanged — and **not** on byte equality of the body part,
because each re-head shifts the body's absolute positions by the previous
head's length (D1, L11). Matching, phrase search and `ts_rank_cd` are
invariant under that shift (measured); positions saturate at 16383 only
after thousands of consecutive renames of one note with no edit between,
and any edit rebuilds the vector from scratch.

## D7 — The rebuild and the fingerprint

`_rebuild_tsvectors_pinned` binds title (recomputed from the bytes it
verified, which equals the stored title because the hash matched) and
`note_aliases` from the parsed frontmatter, and writes the excerpt in the
same certified UPDATE. `rebuild_tsvectors_all_scopes` and the fingerprint
write are unchanged: the fingerprint still certifies configs only.

## D8 — Telemetry and the identity oracle

`usage_logs.params` gains `any_rows` (int), `fallback`
(`"ran" | "skipped" | "not_needed"`) and `snippet_ms`; JSONB, no migration.
`timing.record_results` runs over the final assembled list.

`test_perf_projection_identity_pg.py`'s keyword case compares the oracle
against the rows tagged `match: "all"`, with the comparator ignoring the
new fields — the identity requirement's keyword clause is re-scoped to the
`all` block, since the `any` block has no pre-change counterpart.

## D9 — The docstring, symmetric with `semantic_search`

States that bare terms are ANDed and partial matches come after the full
ones marked `match: any`; shows `"exact phrase"`, `a OR b`, `-excluded`;
describes `snippet`/`head`, `matched_in`, `matched_terms`; keeps the
"use the other tool for…" cross-reference in the same shape
`semantic_search` uses, with neither ranked above the other.

## Risks / Trade-offs

- D2's weight array changes the **rank values** of every result, not
  only their order; any consumer treating `rank` as a stable number was
  already wrong (L1), and the analytics page reads paths, not ranks.
- D3 adds up to two cheap statements on under-filled pages; a full AND
  page costs exactly what it does today plus D4.
- D4 adds a TOAST value per note and `ts_headline` parsing bounded by the
  page; `snippet_ms` makes the cost visible before it is tuned.
- D5 rewrites every `notes_metadata` row once (new tuple versions; 027's
  vacuum thresholds handle it) and holds the generation lock for seconds.
- D6 touches `move_note`, a write tool; the adversarial pass is mandatory
  for that reason alone.

## Accepted limitations

- **L1** `ts_rank_cd` normalisation unchanged: ranks are uninterpretable
  and an `any` row's rank is against a different query.
- **L2** Phrase adjacency across the head/body boundary and across configs
  (`title's last word <-> body's first word` matches a quoted phrase); the
  cross-config case pre-exists.
- **L3** A match beyond the 20,000-character excerpt, or only in the
  title/aliases, shows `head:` (leading words) instead of a highlight.
- **L4** Rows indexed before 031 carry no excerpt until edited or rebuilt;
  on compose, notes changed in the migrate-to-recreate window may keep an
  unweighted vector until their next edit or a rebuild. A startup WARNING
  counts the former; nothing counts the latter.
- **L5** No fallback for a query with `OR`, a negated phrase, or a single
  positive unit; a `-term` is honoured in the fallback.
- **L6** `matched_terms` omitted above 32 positive units or when the
  parse is not a plain conjunction.
- **L7** U+0002 and U+0003 are removed from the stored excerpt and from
  alias text.
- **L8** The weight array is a heuristic: a note with about forty adjacent
  occurrences of a two-word title phrase (fifty of a one-word title) can
  still outrank the titled note. #356 is where it gets measured.
- **L9** No hybrid fusion (#354), no title-only mode.
- **L10** A row whose re-head hits SQLSTATE 54000 in 031 stays unweighted
  (logged by id) until its next edit or the rebuild.
- **L11** Each move re-head shifts the body's absolute tsvector positions
  by the previous head's length; matching, phrases and `ts_rank_cd` are
  unaffected, and an edit rebuilds the vector. Positions saturate at
  16383 only after thousands of consecutive renames with no edit between.

## Measured on PostgreSQL 16 (pgvector/pgvector:pg16, 2026-10-28)

Probed while writing this design; the implementation's tests pin each.

- `ts_filter(to_tsvector(x), '{d}') = to_tsvector(x)` is true: a
  pre-change vector is entirely weight `D`.
- `head || ts_filter(head' || body, '{d}')` ≠ `head || body` in bytes:
  positions `bodi:3 text:4` versus `bodi:2 text:3`; `@@ 'bodi' <-> 'text'`
  and `ts_rank_cd` are equal on both (0.02 and 0.02). Hence 031's skip
  predicate and D6's wording.
- `ts_filter(A(Alpha) || D(body), '{a}') @@ 'alpha'` is true and the `{d}`
  side is false: `matched_in` is computable from the vector alone.
- `ts_rank_cd('{0.02,0.2,0.4,1.0}', …)`: title-only `1.0`; thirty adjacent
  body repetitions `0.745` (default array: `3.725`); sixty one-word body
  occurrences `1.2`.
- `websearch_to_tsquery` text forms: `alpha beta gamma` → `'alpha' & 'beta'
  & 'gamma'`; `alpha "beta gamma" -delta` → `'alpha' & 'beta' <-> 'gamma' &
  !'delta'`; `alpha OR beta` → `'alpha' | 'beta'`; `alpha -"beta gamma"` →
  `'alpha' & !( 'beta' <-> 'gamma' )` (parenthesised, so skipped);
  `alpha beta OR gamma delta` → `'alpha' & 'beta' | 'gamma' & 'delta'`
  (skipped); `"foo the bar"` → `'foo' <2> 'bar'`; `nothing-here` →
  `'nothing-her' <-> 'noth'` (one phrase unit); `2026-10-28` → `'2026' <->
  '-10' <-> '-28'`; `foo:* bar:A (x) & | ! <-> baz` → `'foo' & 'bar' & 'x'
  & 'baz'` (websearch strips every operator, so the output never carries
  `:*` or weights); stop words only → an empty tsquery.
- `$$('alpha' | 'beta' <-> 'gamma') & !'delta'$$::tsquery` round-trips with
  the lexemes verbatim; `$$'running'$$::tsquery` stays `'running'` (no
  stemming through the input function).
- `to_tsvector('alpha beta') @@ ('alpha' | 'beta' | 'gamma')` is true where
  the conjunction is false: the superset direction.
- `ts_headline(…, 'MaxFragments=2, MaxWords=18, MinWords=6, …')` with no
  cover returns six words; with `MaxFragments=0, MinWords=18, MaxWords=24`
  and no cover it returns the first eighteen words; two covers far apart
  yield two fragments joined by the delimiter; U+0002/U+0003 pass through
  as `StartSel`/`StopSel` bytes.

## Open owner decisions

1. **`RANK_WEIGHTS = {0.02, 0.2, 0.4, 1.0}`** (one title cover ≈ fifty
   body covers) versus PostgreSQL's default (≈ ten). The scenario in the
   spec uses thirty adjacent body occurrences; the default fails it.
2. **`KEYWORD_EXCERPT_CHARS = 20,000`** and the storage it implies (~12 MB
   on the reference vault, ≤ 20 KB per note).
3. **Excerpt backfill is an operator step** (`make rebuild-tsvectors` /
   the k8s Job) rather than self-healing in the full-hash pass.
4. **A checked-in k8s Job example** under `deploy/kubernetes/base/`.
5. **`alias` (singular) read beside `aliases`**, and a scalar alias string
   comma-split like a scalar `tags` string.
