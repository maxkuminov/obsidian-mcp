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
`RANK_WEIGHTS = '{0.02, 0.2, 0.4, 1.0}'` (`D, C, B, A`) — the default of
the `RANK_WEIGHTS` setting (D4's settings paragraph), bound by
`fts.rank_expression` and used by both blocks of D3.

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

**Per config, because the parses differ per config** (Codex r1 #3: under
`english,simple`, `the running` parses to `'run'` and to `'the' &
'running'`). For each configured config `i`:

- its parse is a plain conjunction with `k_i ≥ 2` positive units →
  *eligible*: the per-config fallback form is `(u1 | … | uk) & !n1 & … &
  !nm` built from the verbatim unit tokens and cast `CAST(:fallback_i AS
  tsquery)`;
- otherwise (an `OR`, a parenthesised negation, a single unit, an empty
  parse) → *ineligible*: the per-config form is that config's **own AND
  query**, `websearch_to_tsquery(CAST(:cfg_i AS regconfig), :q)`, retained
  unchanged.

The combined fallback is the OR (`||`) over configs of those per-config
forms — the same shape `combined_tsquery` has. Because each per-config form
is implied by that config's AND parse (`(a & b & !n) ⇒ ((a | b) & !n)`, and
an ineligible config's form *is* its AND parse), the combined fallback is a
superset of the combined AND query **by construction**, whatever mix of
eligible and ineligible configs a query produces. A `runs fast` document
under `english,simple` matches the combined AND through `english`'s `'run'`
and matches the combined fallback through the retained `english` form. The
fallback runs only when at least one config is eligible; when none is, the
fallback is skipped (`fallback: "skipped"`). The `tsquery` input function
takes quoted lexemes **verbatim** — no stemming, no stop-word pass — so an
eligible form's stems are exactly that config's AND parse, including
stop-word gaps (`<2>`) inside a quoted phrase. Nothing from the caller's
string reaches `to_tsquery`; the only caller-derived input is the same `:q`
bind the AND statement already uses.

The fallback statement is the AND statement with the fallback tsquery in
both `@@` and `ts_rank_cd`, `AND NOT (content_tsvector @@ <and tsquery>)`,
the same filters, the same ORDER BY, `LIMIT :limit - n`. Because the AND
block was not full, every AND-matching row is in it, so the predicate
excludes exactly those rows. The superset property is asserted on Postgres
over a generated query corpus under `english` and under `english,simple`,
with the `the running` / `runs fast` case pinned by name.

**`matched_terms` is per config, rendered as one pair.** For each config
whose parse is a plain conjunction (any `k_i ≥ 1`, at most
`MATCHED_TERMS_MAX_UNITS` = 32 units), the statement counts the config's
positive units present in the row's vector, `m_i`, and the row renders the
pair `m/k` of the config with the greatest `m_i / k_i`, ties broken by the
greater `k_i`, then by config order. Under `english,simple` the `runs
fast` row for `the running` therefore renders `1/1` (english: `'run'`
present), not `0/2`. A row whose configs all have a non-conjunctive parse
carries no `matched_terms`.

Both statements and D4's run in one transaction after `SET LOCAL
random_page_cost = 1.1`, and that transaction opens with `SET TRANSACTION
ISOLATION LEVEL REPEATABLE READ, READ ONLY` (Codex implementation round 1,
C-1): the fallback's `NOT (v @@ <and query>)` excludes exactly the
conjunctive block only when both statements read the same snapshot — at
READ COMMITTED an index commit between them could drop a note from both
blocks for that call. `SET TRANSACTION` is legal before the first
snapshot-taking statement; `SET LOCAL` takes none, and `search_notes_impl`
hands the service a session with nothing run. A read-only REPEATABLE READ
transaction raises no serialization failure. Rows are assembled `all` block then `any` block,
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
whole. It does **not** bound the cost by itself (Codex r1 #5: twenty
distinct 20,000-character excerpts cost ≈ 452 ms under one config and ≈
891 ms under `english,simple` on PG16, and `limit` may be 500), so the
cost is bounded by rows, not by `limit`: snippets are computed for the
first **`KEYWORD_SNIPPET_ROWS`** (default 20) rows of the assembled page
regardless of `limit`; rows beyond it carry no `snippet`/`head` field and
the header says `snippets: first N`. The implementation ships only if the
measured gate passes: on the throwaway PG16 container, a page of twenty
distinct 20,000-character excerpts with repeated matches, under
`english,simple`, with head-fallback rows included, completes the snippet
statement in **under 300 ms** (asserted in the integration suite, the
measurement recorded here); if it does not, `KEYWORD_EXCERPT_CHARS`'s
default is lowered until it does and the reason recorded. **Measured
2026-10-29** on the throwaway `pgvector/pgvector:pg16` container
(`test_keyword_ranking_pg.py::test_the_worst_case_snippet_latency_is_under_the_gate`):
**112 ms best of three (147, 130, 112 ms)** for the headline statement plus
the head statement over 20 rows × 20,000 characters, `english,simple`, 5
head rows — so the 20,000 default stands. Codex's ≈ 452 / 891 ms were for a
`limit`-bound design with a fragment-mode headline on every row; the row
cap and the two-statement split (no second parse for highlighted rows) are
what changed. A match beyond
the excerpt, or a match only in the head (title/aliases are not in the
excerpt), yields no highlight and the row shows the excerpt's leading
words as `head:` with `matched_in` saying where it matched (L3). Nothing
fabricated: `head:` is text the index holds for that note, labelled as
not the matching span.

`RANK_WEIGHTS`, `KEYWORD_EXCERPT_CHARS` and `KEYWORD_SNIPPET_ROWS` are
**settings** (`src/config.py`, validated at boot: four finite floats in
`[1e-6, 1]` rendered as a bound `float4[]`, never interpolated — the floor
because a positive value below float4's range binds as zero and silently
zeroes that weight's covers, the ceiling because PostgreSQL refuses a
weight above 1 (Codex implementation round 1, C-3); `1_000 ≤ excerpt ≤
100_000`; `0 ≤ rows ≤ 100`), documented in README's Configuration table and
`.env.example` per the public-docs rule. Changing `KEYWORD_EXCERPT_CHARS`
takes effect for rows written afterwards; the rebuild rewrites the rest.
**The 300 ms gate is measured at the defaults** (20,000 × 20); the snippet
cost scales roughly linearly with both settings, so their maxima (100,000 ×
100) are about 25× the measured work. The bounds are kept as an operator's
choice and the README rows say so (verifier V-N3).

**The snippet statement** is a third statement, over the page only:
`WHERE id = ANY(:ids)` for the block's ids, projecting per row
`ts_headline(CAST(:cfg_i AS regconfig), body_excerpt, <block tsquery>,
:opts)` for each configured config (the first headline containing the
sentinel wins), `ts_filter(v, '{a}') @@ q` as `matched_title`,
`ts_filter(v, '{d}') @@ q` as `matched_body`, and `matched_terms` = `Σ (v
@@ CAST(:unit_j AS tsquery))::int` over the plain conjunction's positive
units when D3 parsed one and `k ≤ 32`, else omitted (L6). `:opts` is
`MaxFragments=2, MaxWords=18, MinWords=6, StartSel=\x02, StopSel=\x03,
FragmentDelimiter=" … "`. A headline is accepted as the row's `snippet`
only when `matched_body` is true: with no full cover `ts_headline` still
marks single query words, so a title-only or split match would otherwise
show a body "highlight" that is not the match (verifier V-B1). For every
row without a snippet and with an excerpt, the `head:` text comes from a
**second statement** over those ids only — not a `CASE` inside the first —
`ts_headline(fts_configs[0], body_excerpt, q, 'MaxFragments=0,
MinWords=18, MaxWords=24, StartSel=\x02, StopSel=\x03')`, with the
sentinels stripped from the result (PostgreSQL still marks query words in
the leading words, and its default `<b>…</b>` would be indistinguishable
from a note that contains `<b>` literally, V-B2). In fragment mode a
no-cover headline is only `MinWords` (six) words, measured, while
`MaxFragments=0` with no cover returns the document's first eighteen
words. A highlighted row pays one parse per config and a `head:` row one
more. One statement pair per non-empty block (the query differs). The
parse-text statement of D3 runs whenever `KEYWORD_SNIPPET_ROWS > 0`, even
on a full conjunctive page, because `matched_terms` is computed from it.
The pair's cost is a function of the page, never of the candidate set, and it is timed as `snippet_ms` in `usage_logs.params`
beside `db_ms`.

Rendering (`search_notes_impl`): header `Found N results for 'q' — all
terms: a, any term: b — without excerpt: e` (counts always, including
zero, #200's rule), followed by `— snippets: first N` when the page is
longer than `KEYWORD_SNIPPET_ROWS`, or `— snippets: off` when it is 0; per
row `- **title** (`path`) [tags] — rank: r — match: all|any — matched_in:
title,body — terms: k/n` and a second line `snippet: …` (sentinels
rendered `«` `»`) or `head: …`. `matched_in` is `title+body` for a split
match — neither part alone matches the block's query, the whole vector
does (one term in the title, another in the body; V-N5). A row with NULL
excerpt has no second line, and a footer names `make rebuild-tsvectors`
when `e > 0`. The snippet is a note-controlled field: never cut by the
server (ts_headline bounds it), never marked inside, and **rendered as one
line** — every whitespace run, line breaks included, becomes one space —
because `ts_headline` keeps the excerpt's newlines and markdown and a note
could otherwise print a line that reads as another result row (Codex
implementation round 1, C-2). The service dict carries `snippet`, `head`,
`matched_in`, `matched_terms` and `has_excerpt` (whether the row has a
stored excerpt; the header's `without excerpt` count reads it). The field
is rendered `terms:`; the service key stays `matched_terms`.

`move_note` enters the keyword-vector writer's two opening statements
(generation lock, fingerprint re-read; D6) through one module-level seam,
`tools._enter_keyword_generation`, so the offline move tests that script a
fake session by statement order can stand it down; the real behaviour is
pinned on PostgreSQL.

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

**Downgrade restores the old semantics, not only the schema** (Codex r1
#4). `downgrade()` takes the generation lock, sets `content_tsvector =
ts_filter(content_tsvector, '{d}')` on every row holding a vector (strips
every weight-`A` lexeme, so the previous image sees no title-only matches
and later D-only writes do not mix with headed rows), and only then drops
a marked `body_excerpt`. The schema gate asserts a downgraded database has
no A-weighted lexeme.

**The old-image overlap is closed, not accepted** (Codex r1 #1). The
failing input was: 031 re-heads `Alpha.md`; before `--force-recreate` the
old container serves `move_note(Alpha.md → Beta.md)`, which rewrites
`title` and `file_path` but preserves the headed vector — so the new
image serves `alpha` as an A-weighted false positive and misses `beta`,
and neither the unchanged-hash scan nor 031's skip predicate ever repairs
it. The control: **the compose `make deploy` stops the application
container before migrating whenever a migration is pending** (`alembic
current` ≠ `heads`), then migrates, then recreates; with no pending
migration it keeps today's order. `DEPLOYMENT.md`'s upgrade notes state
the same order (stop app → migrate → start) for anyone driving compose by
hand. k8s already has no overlap: `strategy: Recreate` terminates the old
pod before the new pod's `migrate` initContainer runs. The scenario stays
in the spec as an accepted-and-closed limitation whose control is the
ordering rule (L4), and a rebuild-versus-move race test covers the other
writer pairing (D6).

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

**Both move paths are tsvector writers, so both hold the generation lock
and revalidate the fingerprint** (Codex r1 #1). The indexer's move UPDATE
already runs inside the pass transaction, which takes
`acquire_generation_lock_unbounded` at its head and calls
`_assert_fts_generation_current` before any row lock. `move_note`'s
metadata transaction gains the same two steps as its **first** statements
— `acquire_generation_lock(session)` (the 60 s bounded form: a request
must not sit on a lock for minutes), then `_assert_fts_generation_current`
— before its `UPDATE notes_metadata` takes a row lock, keeping the
documented order *generation lock → row locks*. A lock wait past the
statement timeout, or a fingerprint mismatch (a rebuild recorded a
different `FTS_CONFIGS` since this process started), aborts the metadata
transaction: the move on disk stands, the result reports the partial
outcome exactly as a database failure does today, and the next pass's
id-preserving move detection — itself under the lock and the fingerprint
check — rewrites path, title and head. Nothing is written under a
configuration the store does not certify. The rebuild holds the lock for
its whole transaction, so a `move_note` racing it waits and re-heads
afterwards, and a move that commits first makes the rebuild's certified
UPDATE miss and re-read (the race test in tasks §5).

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

`test_perf_projection_identity_pg.py`'s keyword case can no longer claim
byte identity: `rank` is a pre-change field and D2 scales every body-only
rank, and title-versus-body order changes on purpose (Codex r1 #2). The
identity requirement's keyword clause is therefore rewritten to what is
actually invariant, and the new contract is asserted separately:

- **conjunctive membership**: the set of paths tagged `match: "all"`
  equals the oracle's set (the `@@` predicate and the filters are
  unchanged), for every query, filter and limit, where the limit cuts
  neither list;
- **body-only relative order**: among rows whose `matched_in` is `body`
  alone, the relative order equals the oracle's (same tie rules as before,
  `file_path` tie-break);
- **exempt**: rank values, and the position of any row whose `matched_in`
  includes `title` (the intended change);
- **asserted separately** (`test_keyword_ranking_pg.py`): the title case,
  the alias case, body-only order equal under `RANK_WEIGHTS` and under the
  default array.

The oracle's corpus still builds its vectors through `index_tsvector_sql`
(with the corpus titles, so title rows exist to be exempted).
Membership is checked on every `all` row; body-only order only within the
first `KEYWORD_SNIPPET_ROWS` rows, because `matched_in` exists only there
(V-N9, accepted).

## D9 — The docstring, symmetric with `semantic_search`

States that bare terms are ANDed and partial matches come after the full
ones marked `match: any`; shows `"exact phrase"`, `a OR b`, `-excluded`;
describes `snippet`/`head`, `matched_in`, `terms` (the rendered name of
the service's `matched_terms`); keeps the
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
- **L4** Rows indexed before 031 carry no excerpt until edited or rebuilt
  (a startup WARNING counts them). The old-image overlap on compose — an
  old container moving a note after 031 re-headed it, leaving the old
  title at weight `A` for ever — is **closed** by the ordering rule: `make
  deploy` stops the app before a pending migration, `DEPLOYMENT.md` says
  so for hand-driven compose, and k8s `Recreate`s. An operator who
  migrates a live compose stack by hand against that note reopens it and
  repairs it with `make rebuild-tsvectors`.
- **L5** No fallback when no config's parse is a plain conjunction of at
  least two positive units (`OR`, a negated phrase, a single word); an
  ineligible config keeps its AND form inside a fallback another config
  made eligible; a `-term` is honoured in the fallback.
- **L6** `matched_terms` omitted when no config's parse is a plain
  conjunction of at most 32 positive units; rendered per the best config.
- **L12** Snippets are computed for the first `KEYWORD_SNIPPET_ROWS` (20)
  rows of the page; rows beyond carry no `snippet`/`head` field.
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
- **L13** `make deploy`'s stop-before-a-pending-migration step has no
  automated test; it fails safe — an empty or failing `alembic current`
  differs from `alembic heads`, so the app is stopped (V-N8).
- **L14** A literal `«` or `»` in a note collides with the rendered
  highlight markers. The stored markers are the unforgeable U+0002/U+0003
  (stripped from excerpts at index time); the visible rendering is a
  convenience the agent verifies with `read_note` (C-2).
- **L15** The builders sort the configs (V-N4), so under an `FTS_CONFIGS`
  not in sorted order a vector written by a pre-#344 build has its body
  parts in the old order until the note is rewritten or rebuilt; matching
  and rank are unaffected, only byte equality with a fresh write is.

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

## Owner decisions (taken 2026-10-29, after Codex round 1)

1. **`RANK_WEIGHTS = {0.02, 0.2, 0.4, 1.0}`** (one title cover ≈ fifty
   body covers), a setting with that default.
2. **`KEYWORD_EXCERPT_CHARS = 20,000`** (a setting) and the storage it
   implies (~12 MB on the reference vault, ≤ 20 KB per note), with
   **`KEYWORD_SNIPPET_ROWS = 20`** bounding the per-call parse cost and a
   measured 300 ms gate.
3. **Excerpt backfill is an operator step** (`make rebuild-tsvectors` /
   the k8s Job) rather than self-healing in the full-hash pass.
4. **A checked-in k8s Job example** under `deploy/kubernetes/base/`.
5. **`alias` (singular) read beside `aliases`**, and a scalar alias string
   comma-split like a scalar `tags` string.

## Codex spec review, round 1 (FAIL, six findings, all folded)

1. BLOCKER — old-image overlap + unlocked move writer → D5 (stop before a
   pending migration; k8s `Recreate` stated), D6 (both move paths under the
   generation lock with the fingerprint re-read; rebuild race test), L4.
2. MAJOR — identity oracle contradicted the ranking change → D8 rewritten.
3. MAJOR — multi-config fallback and `matched_terms` undefined → D3
   per-config construction, superset by construction, the `the running` /
   `runs fast` case pinned.
4. MAJOR — downgrade left headed vectors → D5 downgrade re-filters under
   the lock.
5. MAJOR — snippet cost was a row-count assertion → D4
   `KEYWORD_SNIPPET_ROWS` and the measured 300 ms gate.
6. MINOR — settings and `DEPLOYMENT.md` omitted → D4 settings, tasks §7.

## Implementation review, round 1 (verifier + Codex), folded

Verifier: V-B1 (headline accepted only for a body match), V-B2 (head
sentinels), V-N1 (raiseload test), V-N2 (the `to_tsvector(` guard), V-N4
(sorted builders), V-N5 (`title+body`) fixed; V-N3, V-N6–V-N9 accepted
above and in the deltas. Codex: C-1 fixed by the REPEATABLE READ READ ONLY
transaction (D3) — the service's session has no statement run before it,
so the isolation can be set; C-2 fixed by one-line rendering, its
guillemet half accepted (L14); C-3 fixed by the `[1e-6, 1]` finite bound
(D4) — the triage's `[1e-6, 1e6]` ceiling was not taken because
PostgreSQL refuses any weight above 1 ("weight out of range"), so the
existing `≤ 1` stays, and zero stays refused because `ts_rank_cd`
divides by each weight.
