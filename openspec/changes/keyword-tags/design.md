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
  NULL`, in id order, each UPDATE in its own savepoint:
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
- SQLSTATE 54000 on one row leaves it unchanged, counted and logged by id
  (its next edit or the rebuild writes it through the halving retreat);
  any other error fails the migration with nothing committed;
- one summary line: re-headed, already tagged, untagged, left at the limit.

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
took 0.75 s on PostgreSQL 16; 035's per-row savepoints add a round trip
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
so the existing gate applies: `make search-eval-snapshot`, then `make
search-eval` against a restored copy with the incumbent as `BASELINE`; the
keyword and hybrid arms are expected to move, the semantic arm not at all.

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
- **L7** A row whose re-head hits SQLSTATE 54000 in 035 keeps its untagged
  vector (logged by id) until its next edit or the rebuild.
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
