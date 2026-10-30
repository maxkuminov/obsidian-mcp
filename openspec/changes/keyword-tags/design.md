## Context

#344 (archived as `2026-10-29-keyword-search-ranking`, migration 031) gave
the keyword vector a weighted head: `A(title) || A(aliases) || D(body)` per
FTS config, one builder in `src/services/fts.py`, ranked with
`ts_rank_cd(RANK_WEIGHTS, …)` at `{0.02, 0.2, 0.4, 1.0}`. Tags were left
out. They are extracted on every pass by `vault.extract_tags(body,
frontmatter)` — frontmatter `tags` (list, or comma-split scalar) plus
inline `#tags` outside code (`INLINE_TAG_RE`), NUL removed, tags over
`MAX_TAG_BYTES` (1,024) dropped, returned **sorted and de-duplicated, each
in its full form** (`ctx/phone` is stored as one tag; no parent is added)
— and stored in `notes_metadata.tags` (`varchar[]`, GIN) for the `tags`
filter and for display. No `tsvector` holds them, so `keyword_search` and
`hybrid_search`'s keyword arm cannot match a word a note carries only as a
tag (#391).

Every number below was measured on `pgvector/pgvector:pg16` while writing
this design (2026-10-30); the implementation's tests pin each.

## Goals / Non-goals

Goals: a word that is one of a note's tags finds that note through the
keyword arm; a tag hit ranks above a body hit and below a title hit;
`matched_in` says when the match came from the tags; the stored vectors are
right the moment 035 commits, with no operator step; every writer produces
the same shape.

Non-goals: a tag-only search mode (the `tags` filter already exists); a
change to the tag extractor; a change to `RANK_WEIGHTS`, the fingerprint or
the embedding path; any change to the search-evaluation harness.

## D1 — The vector: `A(title) || A(aliases) || B(tags) || D(body)`

Per configured config, in the builders' sorted config order (#344 V-N4):
title parts for every config, aliases parts for every config, **tags parts
for every config**, content parts for every config. A tags part is
`setweight(to_tsvector(cfg, :tags), 'B')`; the others are unchanged.
`head_tsvector_sql`, `index_tsvector_sql` and `rehead_tsvector_sql` gain a
required keyword argument `tags_bind`; the head is everything before the
body, so `rehead` stays `head || ts_filter(content_tsvector, '{d}')` with
the tags inside the head. A new `tags_tsvector_sql(tags_bind, configs)`
returns the tags parts alone, for 035's skip predicate (D5) — so 035 never
composes `to_tsvector(` itself and the existing AST guard
(`test_to_tsvector_is_composed_only_by_the_builders_and_031`) holds
unchanged.

**Why the tags sit between the head and the body, not at the tail.** A tail
placement (`… || D(body) || B(tags)`) would make 035 byte-exact (`old ||
B(tags)` equals a fresh build), which is attractive, and was rejected for
two measured reasons: (1) tsvector positions saturate at 16,383, so on a
long note every tag lexeme lands at position 16,383 and multi-word tag
phrases stop matching as phrases; (2) a cover that joins a tag with body
words (the issue's own query: `HOA` in the tags, `siding complaint` in the
body) spans the whole body at the tail and scores near zero, whereas in
the middle the tags are adjacent to the body's start. The middle placement
is also the order the owner named.

**Weight `C` stays unused.** No writer emits it; `ts_filter(v, '{c}')` is
empty on every row and the spec says so, so a future use has to be a
deliberate change.

**`tags_bind=None`.** Migration 031 imports `rehead_tsvector_sql` and must
keep building the A-only head it always built (an upgrade from 030 runs 031
then 035 in one alembic run). Its one call site is edited to pass
`tags_bind=None` explicitly — the only edit to a historical migration, a
behaviour- and byte-preserving one — and `None` omits the tags parts. A
new AST test pins that every call of the three builders under `src/`
passes a non-`None` `tags_bind`, and that only `alembic/versions/031_*.py`
passes `None`; a unit test pins that 031's fragment is byte-identical to
the pre-change text.

Measured: `A(t) || B(empty) || D(body) = A(t) || D(body)` is **true** —
an empty tags part shifts nothing. So an untagged note's vector is
byte-equal under the old and the new builder, which is what lets 035 skip
untagged rows (D5).

## D2 — The tags text: one shared rule, full form plus segments

`fts.tags_text(tags: list[str] | None) -> str`, used by every writer and by
035, over the list `extract_tags` returns (writers) or the stored
`notes_metadata.tags` (035, the `move_note` fallback):

- for each tag in list order, one line holding the tag, followed — when it
  contains `/` — by each non-empty `/`-segment, space-separated
  (`ctx/phone` → `ctx/phone ctx phone`);
- lines joined with `\n`; U+0000, U+0002 and U+0003 removed (the excerpt
  rule, for symmetry with aliases — tags never reach the excerpt);
- cut at `FTS_TAGS_MAX_CHARS` (2,048) characters **at a whole-line
  boundary** (a line that would cross the bound and every later one is
  dropped), a module constant like `FTS_ALIASES_MAX_CHARS`, not a setting.
  The bound keeps the head small against the 1 MiB tsvector limit, since
  the halving retreat in `write_tsvector_bounded` only shrinks the body.

**Why the segments.** Measured: `to_tsvector('english', 'ctx/phone')` is
`'ctx/phone':1` — the parser's `file` token type, one lexeme — and `@@
websearch_to_tsquery('english', 'phone')` is **false**. The full form alone
would make `keyword_search("phone")` miss a `#ctx/phone` note, which is the
issue's bug for every nested tag. Segments alone would lose the exact form:
`websearch_to_tsquery('english', 'ctx/phone')` is `'ctx/phone'`, which only
the full form matches. Both, then. This also matches Obsidian, where
searching a parent tag finds its children. Hyphens need nothing:
`project-x` already yields `project-x`, `project` and `x`. A leading `#`
in a frontmatter value is a blank to the parser (`'#hoa'` → `'hoa'`).

**Why from the extracted list, not from frontmatter again.** The list is
already the one tag rule (#308's normalisations included) and is what the
`tags` filter, `get_tags` and every result row show; a second derivation
would let the vector and the displayed tags disagree.

## D3 — Rank: the default array already has `B = 0.4`

`ts_rank_cd`'s weight array is `{D, C, B, A}`; `B` is the **third** slot,
`0.4` at the default, between `A = 1.0` and `D = 0.02`. `RANK_WEIGHTS`
and its validation are unchanged. Measured with the default array,
single-word query `hoa`:

| Vector | `ts_rank_cd` |
| --- | --- |
| `hoa` in the title | 1.0 |
| `hoa` in the tags only | 0.4 |
| `hoa` three times in the body only | 0.06 |

The issue's query against `A(Neighborhood board) || B(hoa) || D(the siding
complaint went in)`: the whole vector matches the conjunction, no part
alone does, `ts_filter(v, '{b,d}')` does, rank 0.0146 — `match: all`,
`matched_in: tags+body`. The arithmetic limit: one tag cover equals twenty
body covers of the same shape (L2).

## D4 — `matched_in` and the snippet

The snippet statement (page ids only, #344 D4) projects six flags instead
of two: `ts_filter(v, W) @@ q` for `W` in `{a}`, `{b}`, `{d}`, `{a,b}`,
`{a,d}`, `{b,d}`. `matched_in` is:

1. every single part whose flag is true, in the order `title` (`{a}`:
   title or aliases), `tags` (`{b}`), `body` (`{d}`) — rendered
   comma-joined (`title,tags`, `tags,body`, …);
2. when none is, the first true pair in the order `title+tags`
   (`{a,b}`), `title+body` (`{a,d}`), `tags+body` (`{b,d}`);
3. when none is, `title+tags+body`.

The complete set of rendered values is therefore eleven: `title`, `tags`,
`body`, `title,tags`, `title,body`, `tags,body`, `title,tags,body`,
`title+tags`, `title+body`, `tags+body`, `title+tags+body`. Every value
pre-#391 keeps its meaning for a note with no tag hit (`title+body` is
still the title/body split). The cost is four more `@@` per page row, over
`KEYWORD_SNIPPET_ROWS` rows — inside the 300 ms gate's margin, re-measured
by the existing latency test with tagged rows added.

The snippet rule is unchanged: a headline is shown only when the body part
matched; otherwise `head:`. A tags-only match therefore renders `head:`
with `matched_in: tags`, and a `tags+body` split renders `head:` — tags are
not in `body_excerpt`, so nothing in the excerpt is the match.

## D5 — Migration 035: re-head the tagged rows in SQL

035 (`down_revision "034"`) adds no schema object. In one transaction, on
031's model:

- `lock_timeout 10s`, `statement_timeout 15min`, `search_path` pinned and
  the qualified table asserted;
- `pg_advisory_xact_lock(INDEX_GENERATION_LOCK_KEY)` before the first row
  is read (the literal mirrored and pinned by a test, as 031's);
- the configs from the stored `fts_fingerprint` (present → its `configs`;
  absent → `settings.fts_configs`; unreadable → refuse naming the key,
  nothing changed);
- per row `(id, title, frontmatter, tags)` with `content_tsvector IS NOT
  NULL`, in id order:
  `content_tsvector = rehead(:title, :aliases, :tags)` where `aliases =
  aliases_text(frontmatter)` and `tags = tags_text(tags)`, **guarded by
  two predicates in the UPDATE's own `WHERE`**:
  - `length(ts_filter(content_tsvector, '{b}')) = 0` — skip a row that
    already carries a tags part. `rehead` is not position-idempotent
    (each application shifts the body by the previous head's length), so
    this is what makes a stamp-back re-run change nothing;
  - `length(<tags_tsvector_sql>) > 0` — skip a row whose tags produce no
    lexeme (no tags, or only stop words). Such a row is already byte-equal
    to what the new builder writes (D1), and re-heading it would only
    shift its body.
- **all-or-nothing** (Codex r1 B1): any error on any row — SQLSTATE 54000,
  the 1 MiB tsvector limit, included — fails the whole migration with
  nothing committed, naming the row's id and the SQLSTATE (never the path
  or any text). A migration that succeeded leaves no tagged row untagged;
- one summary line: re-headed, already tagged, without tag lexemes.

**The size preflight (owner decision after round 1).** Before the first
UPDATE, under the same lock and configs, 035 reads every row the rewrite
would change (the two predicates) and estimates its post-035 data size as
`S(ts_filter(v, '{d}')) + S(head(title, aliases, tags))`, the head built by
the shared builder (per-row or batched read-only statements; the head is at
most a few KiB, so building it cannot itself hit the limit). `S(v)` is
PostgreSQL's own accounting of the string area, summed over `unnest(v)`:
the lexeme's bytes rounded up to even, plus `2 + 2 × npos` when it has
positions. It is an upper bound for a concatenation (a lexeme in two parts
is stored once). Measured on PG16: a stored vector with `S = 1,048,174`
failed at the twelfth added 32-character tag word with PostgreSQL reporting
1,048,610 against the formula's 1,048,606 — within four bytes — so the
abort threshold is 1,048,575 − 64 = **1,048,511**. Any row over it aborts
the migration with nothing written, naming the count and the ids. A false
abort is possible only for a row within 64 bytes of the limit. The
rewrite's all-or-nothing rule stays as the backstop.

The operator runs the SQL equivalent against the live 034 database before
merging (`DEPLOYMENT.md`, read-only, counts and ids only). It approximates
`tags_text` in SQL (full forms plus `/` replaced by spaces, no cut — an
over-estimate) and takes the existing head from the vector itself, which
the re-head replaces with an equal `A` part. That holds only for a row
that *has* an `A` part: a D-only row 031 left at the limit (#344 L10)
gains its whole title-and-aliases head in 035, which the stored vector
does not contain and the aliases rule cannot be computed in one portable
statement (Codex r2). The statement therefore **flags every candidate row
with no `A` lexeme** (`d_only`) in addition to the size check — a false
flag (a title that stems to nothing, `The`) is acceptable, a false pass is
not. A flagged D-only row is fixed before upgrading by shortening the note,
or by letting the current image write it through the halving retreat (an
edit, or `make rebuild-tsvectors`), after which it has an `A` part and the
size check applies to it. One config shown; with several,
add one `to_tsvector` per config to `b`. Prototyped on PG16:

```sql
WITH tagged AS (
  SELECT id, content_tsvector AS v,
         setweight(to_tsvector('english',
           array_to_string(tags, E'\n') || E'\n' ||
           replace(array_to_string(tags, E'\n'), '/', ' ')), 'B') AS b
  FROM notes_metadata
  WHERE content_tsvector IS NOT NULL AND cardinality(tags) > 0
    AND length(ts_filter(content_tsvector, '{b}')) = 0
), est AS (
  SELECT id,
    (SELECT coalesce(sum((octet_length(lexeme) + 1) / 2 * 2
       + CASE WHEN cardinality(positions) > 0 THEN 2 + 2 * cardinality(positions) ELSE 0 END), 0)
     FROM unnest(v))
  + (SELECT coalesce(sum((octet_length(lexeme) + 1) / 2 * 2
       + CASE WHEN cardinality(positions) > 0 THEN 2 + 2 * cardinality(positions) ELSE 0 END), 0)
     FROM unnest(b)) AS bytes
    , length(ts_filter(v, '{a}')) = 0 AS d_only
  FROM tagged WHERE length(b) > 0
)
SELECT count(*) AS tagged_rows,
       count(*) FILTER (WHERE bytes > 1048511 OR d_only) AS flagged,
       array_agg(id ORDER BY id) FILTER (WHERE bytes > 1048511 OR d_only) AS ids,
       count(*) FILTER (WHERE d_only) AS d_only,
       max(bytes) AS largest
FROM est;
```

**A 54000 failure and its remedy.** The tags text is at most 2,048
characters, so only a vector already within a few KiB of 1 MiB can cross
the limit. Nothing is committed, so the database is still at 034. On
compose the app was stopped before migrating; start the previous image. On
Kubernetes the new pod's `migrate` initContainer fails and, under
`Recreate`, the old pod is already gone: revert the image digest so the
previous image serves (an outage until then — the price of never shipping
a silently untaggable note). Then shorten the named note's body or tags
(the stored vector is reused by the re-head, so only an edit shrinks it),
let the previous image re-index it, and deploy again. Lowering a tag cap
would not help: one tag is enough to cross the limit for such a row, and
the cap is a constant (OD4).


A row 031 left at the size limit (no `A` part, #344 L10) that has tags is
re-headed here with both its title and its tags — the first application
over a D-only vector, byte-exact. A tagged row with a head is **not**
byte-exact after 035: its body keeps the positions the old concatenation
gave it, shifted again by the old head's length (measured: `side:7`
fresh, `side:9` re-headed for a two-lexeme head). Matching, phrase search
within each part and every single-part cover score are invariant; a cover
spanning the tags/body boundary scores marginally lower until the note is
next written (L1). The operator may run `make rebuild-tsvectors` to make
every row byte-exact; it is not required.

**Downgrade** takes the generation lock and sets `content_tsvector =
ts_filter(content_tsvector, '{a,d}')` on every row carrying a `B` lexeme,
so the previous image sees the pre-035 semantics and its later A+D writes
do not mix with tagged rows. Measured: a lexeme in both tags and body
(`'hoa':1B,2`) keeps only its `D` position.

**Why in SQL, not a rebuild or an extraction-version bump.** As in #344
D5: with deploy-on-merge, ranking that is wrong until an operator scales
to zero and runs a Job is the bug shipped under a new name; the stored
columns are sufficient. An extraction-version bump would make the startup
pass rewrite every vector from the files, byte-exact, but it re-derives
links and tasks for every note, and tags would stay unsearchable on every
row the pass has not reached yet. Declined.

**The old-image overlap is closed by the existing ordering rule.** An
old-image process that writes a vector after 035 commits writes it
without tags, and 035 never runs again. Compose `make deploy` already
stops the app before a pending migration (`alembic current` ≠ `heads`,
#344 D5) and k8s `Recreate`s, so nothing writes between 035 and the new
image. `DEPLOYMENT.md`'s 035 upgrade note says so for a hand-driven stack.

**Cost.** 031 re-headed every row of the reference deployment in a few
seconds. 035 rewrites only tagged rows of the 4,265 notes. A set-based
probe over 4,265 synthetic rows with 1,800-word bodies and three tags each
took 0.75 s on PostgreSQL 16; 035's per-row UPDATEs add a round trip
each, so seconds, holding the generation lock (no pass can run, which the
deploy order guarantees anyway). Every rewritten row is a new tuple
version; 027's vacuum thresholds handle it.

## D6 — The writers

Every writer of `content_tsvector` binds `tags = tags_text(…)`:

- **Incremental pass** (`indexer.py`, the tsvector UPDATE after the
  upsert): from the entry's `tags` — the same list the upsert writes to
  `notes_metadata.tags` in the same transaction.
- **Indexer move-pair UPDATE**: from the entry parsed for the new path
  (`e["tags"]`, already bound to the `tags` column in that statement).
- **`move_note`**: from the destination read it already performs for the
  title and aliases, via `extract_tags(body, frontmatter)`; when that read
  or parse fails, from the row's stored `notes_metadata.tags`, selected in
  the same fallback statement as the frontmatter (best-effort, self-heals
  at the next content change, as the title/aliases fallback does).
- **Full rebuild** (`_rebuild_tsvectors_pinned`): from the verified bytes,
  `extract_tags(content, frontmatter)` — the hash matched, so it equals
  the stored list at the current extraction version.
- **035**: from the stored `notes_metadata.tags` (D5).

**A tag edit alone updates the vector.** Tags are derived from the note's
bytes, so adding or removing a frontmatter or inline tag changes
`content_hash`; the note enters `to_upsert`, and the tsvector UPDATE for
every `to_upsert` path runs in the same pass transaction, after the
upsert, from the same entry. #355's rule (the upsert clears
`embedded_content_hash` when title or tags change) is independent and
unchanged: it governs re-embedding, not the keyword vector, which is
rewritten for every upserted row regardless. No extraction-version bump:
the extractor is unchanged, and rows whose stored tags predate the current
extractor version get both their tags and their vector at the next
re-upsert (L6).

**Writer equality.** For the same title, aliases, tags and body, the
incremental pass, the full rebuild, and 035 over a D-only row produce
byte-equal vectors (the existing `test_every_writer_produces_the_same_shape`,
extended with tags). 035 over a headed row and both move paths are
specified on matching and ranking, not bytes (D5, #344 L11).

## D7 — `hybrid_search`, the harness, the docstrings

`hybrid_search` calls `keyword_arm_rows` and `keyword_snippets`; it gains
the new `matched_in` values in its `keyword_rank` provenance and nothing
else. A tags-only keyword row's preview is the keyword `head` under the
existing `preview_kind` rule.

The search-evaluation harness is unchanged. This is a search-path change,
so the existing gate applies — and because 035 rewrites the vectors in
place, the incumbent keyword and hybrid arms cannot be run after the copy
is migrated (Codex r1 M4). The recipe is the runfile workflow:

1. restore a `BACKUP_FULL=1` copy at 034 and `make search-eval-snapshot`;
2. at the incumbent commit (`origin/main` before the merge) run the
   harness's `run` over that copy for `keyword`, `semantic` and the hybrid
   arm, and keep that generation's complete manifest and staged arm files;
3. migrate the copy to 035, and at the candidate commit run the same arms
   with the same qrels, snapshot and selection;
4. `score` the candidate generation with the step-2 files as
   `runfile:<path>` arms (same qrels, snapshot and selection hashes) and the
   incumbent keyword runfile as `BASELINE` (then the hybrid one).

Keyword and hybrid are expected to move, semantic not at all;
`CORPUS_LOSS` invalidates, and `report.json` is attached only when
`attachable: true`.

**The response bounds (Codex r1 M2).** The hybrid and reranker bounds were
proved with `matched_in: title+body` as the longest value; `title+tags+body`
is five characters longer. At `rerank=false` the abbreviated structure
becomes ≤ 345 and the floor sum 990 ≤ 1,000, so the 40–45 cut is still
guaranteed. At `rerank=true` the structure becomes ≤ 367 and the cut-form
sum 431 + 367 + 25 + 25 + 75 + 75 + 5 = 1,003 > 1,000: that one worst case
continues to the labelled omitted form (≤ 984), as the reranker design
already records for the transition-line case. `HYBRID_ROW_STRUCTURE_MAX`
and `HYBRID_RERANK_ROW_STRUCTURE_MAX` move to 345 and 367; the semantic
`rerank=true` bounds carry no `matched_in` and do not move.

**An empty body (Codex r1 m1).** A note with no body stores an empty
excerpt; its headline is empty. Such a row renders no second line at all
(never a fabricated `head:`), keeps `matched_in` (`tags` for the tags-only
case) and is not counted as missing an excerpt. The renderer must tell a
computed empty headline from one not computed and from a NULL excerpt
without relying on truthiness.

The `keyword_search` docstring (≤ 4,000 characters, #343) says that tags
are indexed between title and body and lists `tags` among `matched_in`'s
parts; the server instructions are untouched (they do not gloss
`matched_in`). `render_keyword_results`' docstring follows.

## Risks / Trade-offs

- Rank **values** change for every tagged note; ranks were never stable
  across versions (#344 L1).
- A query word that is also a common tag (`project`, `meeting`) now lifts
  every note carrying it to `B`: a broad tag can crowd body matches out of
  a page. That is what a tag is for, and the `all`/`any` split and
  `matched_in` make it visible; measured by the eval gate.
- 035 touches every tagged row once and holds the generation lock for
  seconds (D5).
- `move_note` is a write tool; its metadata statement changes. The
  adversarial pass is mandatory for that and for the search-path change.

## Accepted limitations

- **L1** 035 is not byte-exact on a tagged row that already had a head:
  body positions shift by the old head's length. Matching and single-part
  scores are invariant; a tags/body-spanning cover scores marginally lower
  until the next write or an optional `make rebuild-tsvectors`.
- **L2** One tag cover equals twenty body covers of the same shape at the
  default array (`0.4 = 20 × 0.02`): a note repeating a word twenty-one
  times in its body outranks a note merely tagged with it. #344 L8's
  class; the eval gate measures it.
- **L3** Phrase adjacency crosses part boundaries: the last alias word and
  the first tag, the last tag and the first body word, and consecutive
  tag lines are adjacent positions. Extends #344 L2.
- **L4** Tags beyond 2,048 characters of tags text (whole lines) are in the
  `tags` column and filter but not in the vector.
- **L5** A parent segment matches every child: `keyword_search("ctx")`
  finds every `ctx/*` note at weight `B`. Intended (Obsidian semantics).
- **L6** 035 uses the stored `tags`; a row last upserted under an older
  extraction version keeps the tags that version extracted until its next
  re-upsert, which rewrites both.
- **L7** *Withdrawn* (Codex r1 B1): 035 is all-or-nothing; a 54000 row
  fails the migration instead of staying untagged.
- **L8** A tag that stems to nothing (`#the`) contributes no lexeme; tags
  are stemmed by each configured config like the body (`#running` matches
  `run` under `english`).
- **L9** `matched_in` names the first minimal split in a fixed order, not
  every combination that would also match.
- **L10** An operator who migrates a live compose stack by hand against
  the upgrade note can let the old image write untagged vectors after 035;
  `make rebuild-tsvectors` repairs them.

## Owner decisions (taken by default 2026-10-30; Max can overrule)

- **OD1** Tags at weight `B` (between title/aliases `A` and body `D`),
  placed between the head and the body (D1). The issue's "weighted like
  title and aliases" is read as "in the head", not "at `A`".
- **OD2** Nested tags: full form plus every `/`-segment (D2).
- **OD3** `matched_in` gains `tags` and the eleven-value set of D4; a
  tags-only match renders `head:`.
- **OD4** `RANK_WEIGHTS` default unchanged (`B` is already `0.4`); no new
  setting; `FTS_TAGS_MAX_CHARS` is a constant.
- **OD5** 035 re-heads in SQL; no fingerprint change, no extraction-version
  bump, no required rebuild (an optional one restores byte-exactness).

## Open questions for Max

- Should the upgrade note *recommend* `make rebuild-tsvectors` after 035
  (byte-exact vectors, L1) or merely mention it? Default: mention.
- Is weight `B` right for tags, or should a deliberate label tie the title
  (`A`)? The issue's wording suggests the latter; the brief chose `B`.
- *(Answered 2026-10-30: add the preflight — D5, "The size preflight".)*

## Codex spec review, round 1 (2026-10-30)

Verdict on 4bbb51f: FAIL — 1 BLOCKER, 4 MAJOR, 1 MINOR, all accepted and
folded.

1. **BLOCKER — 035 could skip a 54000 row and commit**, leaving a tagged
   note unsearchable by its tag for ever. → 035 is all-or-nothing; the
   error names the row id and SQLSTATE; L7 withdrawn; D5 states the remedy
   and the k8s outage it implies; the schema delta and the `DEPLOYMENT.md`
   task follow.
2. **MAJOR — the result-identity requirement contradicted the change.** →
   MODIFIED in `search-quality`: rows with a *tag contribution* (any
   lexeme, positive or negated, of any config's parse present in
   `ts_filter(v, '{b}')`) are exempt; the old guarantee holds over every
   other row, with an oracle scenario that pins the definition.
3. **MAJOR — hybrid/reranker bounds assumed `title+body` was the longest
   `matched_in`.** → MODIFIED `hybrid-search` (structure 345, sum 990,
   cut form kept) and `reranker` (structure 367, cut-form sum 1,003, the
   omitted form in that one case, ≤ 984); minimum-cap scenarios for
   `rerank=false` and `rerank=true`.
4. **MAJOR — `move_note`'s stored-tags fallback untested.** → index-integrity
   scenario and integration test: destination read forced to fail after
   the rename, two tenants with the same path, owner-scoped stored tags,
   the `B` lexeme and the tag search surviving the move and the following
   stat-only pass.
5. **MAJOR — the eval recipe had no incumbent after 035.** → the runfile
   workflow in D7: incumbent generation on the 034 copy first, retained;
   then migrate, run the candidate, score against the same hashes with the
   incumbent runfile as `BASELINE`.
6. **MINOR — tags-only match on an empty body.** → no preview line, never
   a fabricated `head:`, `matched_in: tags`, not counted as a missing
   excerpt; the renderer distinguishes an empty headline from none.

## Codex spec review, round 2 (2026-10-30)

Verdict on 83b7c75: all six round-1 fixes VERIFIED; one new MAJOR, folded.

1. **MAJOR — the operator's pre-merge SQL could pass a D-only row that
   035's preflight then aborts on.** It estimated `S(v) + S(B)`, assuming
   `v` already carries its `A` head; a row 031 left D-only gains the head
   in 035. → The statement flags every candidate with no `A` lexeme
   (`d_only`) besides the size check; the schema-integrity requirement
   forbids a false pass; a scenario and a schema-gate test seed a D-only row
   that passes `S(v) + S(B)` but exceeds `S(D) + S(A + B)`, and assert the
   statement reports it and 035 aborts naming the same id; the upgrade note
   says a flagged D-only row must be shortened or rewritten first.


## Codex spec review, round 3 (2026-10-30)

Codex spec review, round 3 (2026-10-30): PASS — the round-2 fix VERIFIED,
no findings. The `d_only` flag in the operator statement catches the D-only
row that passed `S(v) + S(B)`, and every round-1 fix stands.

## Implementation notes (2026-10-30)

Choices made while implementing, and where the code meets the spec at an
angle. Nothing here changes a requirement.

1. **`head` is tri-state in the service.** `None` = no head computed (a
   snippet was shown, the row is beyond the cap, or the excerpt is NULL); a
   string = computed, the **empty string** for an empty excerpt (a note with
   no body). `render_keyword_results` decides by `is None` / `!= ""`, never
   truthiness; `has_excerpt` (`body_excerpt IS NOT NULL`) still drives the
   no-excerpt count, so an empty-body row is not counted.
2. **`matched_in` is a pure function** (`search.matched_in_from_flags`) over
   the six labels of `search.MATCHED_IN_FLAGS`; a missing flag reads as false.
   A table test over every consistent assignment (three terms over three
   parts) reaches all eleven values.
3. **`move_note`'s head falls back as one unit.** The destination read sets
   title, frontmatter and tags together or not at all; on any failure the
   one owner-predicated statement selects `frontmatter, tags` and the title
   is derived from that frontmatter (previously title and aliases already
   fell back together).
4. **035's error text carries no note data.** `RowFailure` and the preflight
   refusal are raised `from None`, because SQLAlchemy's wrapped error prints
   the bound parameters (title, aliases and tags text) into the deploy log.
   The SQLSTATE is read by 031's chain walk before the original is dropped.
5. **035's preflight skips rows with an empty tags text without a
   statement** and counts them, with rows whose tags text yields no lexeme,
   as "without tag lexemes"; the summary line is `N re-headed, N already
   tagged, N without tag lexemes`. `preflight` and `rewrite` take a bind so
   the gate can call `rewrite` directly (the "preflight bypassed" scenario)
   without an environment switch in the migration.
6. **The rebuild extracts tags off the loop** (`asyncio.to_thread`), like
   the incremental pass, since `extract_tags` scans the whole body.
7. **`tags_text` drops a tag that is empty after sentinel removal**
   (unspecified; an empty line contributes nothing).
8. **Spec tension — nested inline tag.** The search-quality scenario says an
   inline `#ctx/phone` is found by `phone`, `ctx` and `ctx/phone`, each with
   `matched_in: tags`. For `phone` and `ctx` that holds. For `ctx/phone` the
   inline tag's own text is also body text, which PostgreSQL parses as the
   `file` token `ctx/phone`, so the row honestly renders `tags,body`. The
   integration test pins that, and pins `tags` for all three queries when
   the same tag is in frontmatter.
9. **Spec tension — `alembic check` after a downgrade.** `alembic check`
   refuses at a non-head revision ("Target database is not up to date"), so
   the gate asserts it after the upgrade and after the re-upgrade (031's
   test does the same) and, after the preflight abort, after the remedy and
   the upgrade.
10. **The identity-oracle exemption** is exercised in
    `test_keyword_ranking_pg.py` (the "pre-change corpus" is the same rows
    with the tags part stripped inside a rolled-back transaction, the
    pre-#344 statement over it); the projection-identity corpus now binds
    its tags, and none of its queries reaches a tag.

Measurements (gate container, PostgreSQL 16, local):

- **`S` against PostgreSQL**: a concatenation crossing the limit, `S` =
  1,049,880, PostgreSQL reported 1,049,880 (exact for positional lexemes).
- **035 cost**: 4,265 synthetic rows, 1,800-word bodies, three tags each —
  candidates 0.3 s, preflight 9.7 s, rewrite 26.7 s; the alembic run 28–41 s
  end to end. The design's "seconds" was a set-based probe; per-row UPDATEs
  over TOASTed vectors cost ≈ 6 ms each. `DEPLOYMENT.md` and README say
  "tens of seconds"; under `Recreate` it is added to the deploy gap.
- **Snippet gate** with five title-only and five tags-only head rows of
  twenty, `english,simple`: 128 ms best of three (161, 147, 128), under 300.
- **Hybrid bounds**: worst abbreviated structure 337 at `rerank=false`
  (pinned 345) and 365 at `rerank=true` (pinned 367).
