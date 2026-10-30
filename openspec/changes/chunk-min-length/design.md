# Design: a minimum chunk length and word-boundary windows (#390)

## Context

#355 (archived as `2026-10-30-heading-chunks`) splits a note at its ATX
headings, cleans each section with the whole-body fence spans, and windows
each section with `chunk_text_bounded`. The provider input is
`embed_prefix + "\n\n" + chunk_text`, where the prefix is up to 300
characters of `Title > H1 > … > Hk` plus an optional `tags:` line. The
chunking format is `CHUNKING_VERSION` (now 1), stamped per note. A bump
re-selects every note through the embed backlog, under the per-tenant budget.
While any vectors of another generation remain in the caller's scope, every
vector-search response carries the `index_transition:` line, and `/health`
reports `indexer.rechunk: "in_progress"`. This change uses that transition as
it was built and changes none of it.

Observed live on 2026-10-30, after #354:

- A chunk of four words, "in the Email Triage log", scored 0.837 and ranked
  first for a paraphrase query.
- Another row's preview was "lves".

**What the windowing does today.** I read the code and checked this. The
windowing never looks for a word boundary.
`chunk_text_bounded(content, chunk_size=512, overlap=0)` sets
`char_size = 512 × 4 = 2048` and `step = char_size − overlap × 4` (2,048).
It slices `content[start:start + 2048]`, keeps the slice's `.strip()`, and
advances by `step`. Nothing looks for whitespace. Two defects follow:

1. A window's start and end fall wherever character 2,048·k falls, which is
   usually inside a word.
2. The last window of a section holds whatever is left. A section of
   2,048·k + 4 characters ends in a 4-character chunk. "lves" is either that
   tail, the end of "solves" or "themselves", or a mid-word start whose
   window is mostly whitespace.

Only a text that fits in one window is stored as one unstripped chunk. That
branch is unaffected.

**Why a tiny chunk scores high.** Its provider input is mostly prefix. For "in
the Email Triage log" (23 characters) under a breadcrumb of 40 to 300
characters, the vector is roughly the vector of the title and heading. Any
query about the note's topic lands close to it, closer than to a real
passage, which spends tokens on specifics. The fix belongs at chunking time:
do not store a chunk whose text is too small to outweigh its own prefix.

## Goals / Non-Goals

**Goals**

- No stored chunk is shorter than `CHUNK_MIN_CHARS` of stripped text, except
  the single chunk of a note whose whole cleaned text is shorter (and L3).
- No window boundary falls inside a word, unless the text has no whitespace
  in the lookback.
- Containment and ordinal parity with `read_note(section="#N")` survive,
  merged chunks included.
- One more background transition on #355's machinery, made as cheap as
  correctness allows (D6).
- The harness measures the length distribution before and after.

**Non-Goals**

- Down-weighting short chunks at ranking time. The issue's alternative was
  rejected (owner decision 8).
- A token-exact window. The 4-characters-per-token heuristic stays.
- Sentence segmentation. It is language-dependent, and whitespace is not.
- A dashboard widget for chunk lengths.
- Any change to the prefix, the input formatter, the locator rendering, the
  schema, or the transition contract.

## D1: Word-boundary windows (the lookback)

`chunk_text_bounded` keeps its signature and gains `min_chars` (D2). For a
window `[a, a + W)` with `a + W < len(content)` (not the last window), let
`L = min(CHUNK_BOUNDARY_LOOKBACK_CHARS, W // 10)`, where the module constant
is 200, and let `R = content[a + W − L : a + W]`.

- If `R` contains a line break (`\n` or `\r`), the cut is just after the last
  one.
- Otherwise, if `R` contains a `str.isspace()` character, the cut is just
  after the last one.
- Otherwise the cut stays at `a + W` (a hard cut).

The window is `content[a:cut]`, and the next window starts at `cut`.

With `CHUNK_OVERLAP > 0`, the next start is `max(cut − overlap × 4, a + 1)`.
It then moves forward to just after the first whitespace character in
`[start, start + L)`, if there is one. The `a + 1` floor is #10's termination
guard, so the loop still terminates.

`L ≤ W / 10`, so a cut never moves a window more than 10% short. Every window
has at least `W − L ≥ 1` characters, and the step stays positive.

**Why line break first, then whitespace, and nothing smarter.** The text is
markdown, so a line break is the strongest boundary that is cheap to find
deterministically. Sentence punctuation differs between languages and
collides with abbreviations, version numbers and paths. Whitespace is
universal. The constant is not a setting. It is part of the chunking format,
so changing it is a `CHUNKING_VERSION` bump (embedding-providers).

## D2: The minimum, `CHUNK_MIN_CHARS`, and why 200

**Length** is `len(text.strip())` in code points, measured on cleaned text
(fences removed). It is never measured on the prefixed input.

**200 is the default.** The numbers:

- The window is 512 tokens, about 2,048 characters. 200 characters is about
  50 tokens, roughly two sentences or one substantive list item, and under
  10% of a window.
- The prefix is up to 300 characters (about 75 tokens). It is typically 30 to
  120 characters, a title plus one or two headings. At 200 characters of
  text, the chunk is at least 40% of its input even under the longest prefix,
  and typically 65% to 85%. The live 23-character chunk was under 40% even
  under a short prefix.
- The worst-case merged chunk is a full window plus a tail of fewer than 200
  characters (D3), about 2,247 characters, roughly 560 tokens. With the prefix
  that is about 640 tokens. bge-m3 accepts 8,192, `text-embedding-3-*`
  accepts 8,191, and Qwen3-Embedding accepts 32K, so nothing is truncated by
  the provider (L2 covers a provider with a hard 512-token limit).
- A larger minimum (400 to 500) would fold more genuine one-paragraph
  sections into their neighbours and coarsen more locators (L1). The harness
  decides whether 200 is right. The setting exists so the operator can
  measure another value on a copy.

**The setting.** `chunk_min_chars: int = Field(200, ge=0, le=1000)`, plus a
model validator that refuses `chunk_min_chars × 2 > chunk_size × 4`, so a
minimum is at most half a window. That bounds a merged chunk at 1.5 windows
and keeps merging local. The refusal names both values. `0` disables both
merge rules (D3), but the lookback (D1) stays. The fingerprint treatment is
in D7.

## D3: Where short text goes: the grouping rule

The sections come from `_note_sections`, which is unchanged. Section *i* has
an own-text raw span `[s_i, e_i)` and a cleaned text `c_i`. It is
**non-empty** when `len(c_i.strip()) > 0` and **short** when it is non-empty
and `len(c_i.strip()) < CHUNK_MIN_CHARS`.

**Groups.** Walk the non-empty sections in document order and build groups:

1. Open a group at the first non-empty section.
2. While the open group's text is short, extend it to the next non-empty
   section. The group's text is the whole-body-fence-cleaned raw span
   `[s_first, e_last)`, so it includes the heading lines of the absorbed
   sections and of any empty sections between them. When the group reaches
   the minimum, close it.
3. At the end of the note, an open group that is still short joins the last
   closed group: that group's span is extended to the end. If no group was
   closed, the open group stands alone. This is the "whole note is short"
   case: one chunk, never zero.

Each group's text is windowed by `chunk_text_bounded(text, W, overlap,
min_chars)`, with D1's lookback and one more rule. **Tail rule:** if the text
left after a window's cut has a stripped length below `min_chars`, the window
runs to the end of the text instead, and it is the last window. The per-note
cap still counts windows across groups in document order and stops one past
`MAX_CHUNKS_PER_NOTE`, so `chunks_truncated` keeps its meaning.

**Why the span keeps the heading lines.** A merged chunk must still be one
substring of the note. Concatenating the cleaned texts with the heading lines
dropped would produce text that occurs nowhere in the note, and containment
could not hold. Including the inner heading also embeds the subsection's own
name, which the prefix (the locator's path, D4) no longer carries. A group of
one section is exactly #355's section text, with no heading line in it.

**The zero-chunk invariant** is unchanged. Merging moves text between chunks
and never drops it. The headings-only fallback (whole-body chunking as
section 0) is one group. The tail rule applies to it, and its locator is 0.

**Direction: forward first, backward only at the end.** The brief offered two
sketches: merge into the next section's first chunk under the next section's
locator, or merge into the previous chunk. **Neither keeps parity.**

- Under the next section's locator, the short text precedes heading N+1, so it
  is never inside `read_note(section="#N+1")`.
- Under the previous chunk's locator, the short text lies inside
  `read_note(#N−1)` only when it is a descendant of N−1. A sibling falls
  outside.

So the direction does not decide parity. D4's locator rule does. The
direction is chosen for the common shape. A heading, one short line ("see the
Email Triage log"), then subsections: forward merging joins the line with
the text it introduces, and the locator is the short section's own heading
(D4), which is as precise as it can be. Backward merging would attach the
line to an unrelated sibling above it. At the end of the note there is no
next section, so the tail goes backward.

## D4: The locator of a chunk is the deepest section that contains it

Define `body_span(N)` as the body that `read_note(section="#N")` returns. It
runs from one terminator past heading N's `line_end` up to the next heading
of depth ≤ depth(N), or the end of the body (`_section_body_span`, imported).
`body_span(0)` is the whole body. Every chunk has a **raw span**: the raw-body
range its stripped text came from, recovered through the cleaned-to-raw offset
map that fence removal implies.

> A chunk's `section_ordinal` is the deepest N such that its raw span lies
> within `body_span(N)`, or 0 if no heading's body contains it. Its
> `heading_path` is N's path from `_note_sections` (`()` for 0), and its
> prefix is built from that path.

Consequences:

- **An unmerged chunk** lies in section *k*'s own text, and no child body
  starts before the child heading, so N = *k*. The locator is identical to
  #355's.
- **A parent's short lead merged into its first child**: the span starts in
  *k*'s own text and continues into the child, all inside `body_span(k)`. So
  N = *k*. The child's later windows lie in the child alone and keep the
  child's ordinal.
- **A short section merged into a following sibling**: neither sibling's body
  contains the other, so N is their common parent, or 0. The short section's
  own ordinal is not the locator of any chunk (L1).
- **The tail merged backward** follows the same rule.

**Parity holds by construction.** The chunk's text is the fence-cleaned
text of a sub-span of `body_span(N)`, so it is a substring of section N's
body with the whole-body fence spans removed. That is exactly the
containment contract in index-integrity and in search-quality's "follow-up
resolves" scenario. Both stay true without edits, which is why
`search-quality` is not modified. The property test (task 2.6) asserts it on
generated notes.

## D5: `CHUNKING_VERSION` 2: riding #355's transition

Bumping the constant is the whole mechanism. Everything else is #355's,
unchanged:

- Every note's stamp is now `IS DISTINCT FROM 2`, so the backlog re-selects
  every note. Version-only notes go after content-stale ones, held to the
  per-tenant budget in every mode (`EMBED_TIME_BUDGET_SECONDS_PER_USER` 300 s
  per tick).
- Old vectors are served until each note's new rows commit. No note is
  marked stale, and previews stay. Old rows keep their #355 locators.
- The `index_transition: N notes not yet re-chunked; …` line reappears on
  every `semantic_search`, `find_related` and `hybrid_search` response until
  the caller's scope drains. `/health` reads `indexer.rechunk:
  "in_progress"` and never changes `status`.
- The stored fingerprint is unchanged at the default (D7), so the deploy does
  not refuse.
- The full-hash backstop recomputes the input digest only for notes certified
  under the current version (`= :cv`). A v1-stamped note is not cleared. It
  waits in the backlog and stays fresh, so the transition does not mark the
  vault stale.
- **If #355's own transition has not drained when this deploys**, a
  NULL-stamped note goes straight to 2, and a v1 note reuses what it can
  (D6). Nothing is done twice for a NULL note.

**Plainly: this is a second whole-vault transition within days of the
first.** The marker and the mixed ranking (#355's L1) come back for its
duration. This is why it ships now. The vault was just re-embedded, and the
operator is already watching the drain. Waiting would mean a fresh
disruption later for the same fix.

## D6: Reuse across format-compatible chunking versions

#355's D3 rule 2 disables reuse unless the note's stamp equals
`CHUNKING_VERSION`. It exists for one case: a change to the input formatter
(`embedding_input`, `build_embed_prefix`) that leaves a `(embed_prefix,
chunk_text)` pair byte-equal but changes the provider input. In that case
the stored pair no longer proves the vector. This change edits neither
function. Between versions 1 and 2, an equal pair is the same provider input,
and so the same vector (a vector is a function of the model and the input,
and the model is in the refusing fingerprint).

- `INPUT_COMPATIBLE_CHUNKING_VERSIONS: frozenset[int] = frozenset({1, 2})`
  sits beside `CHUNKING_VERSION` in `embeddings.py`. Reuse rule 2 becomes:
  the note's `embedded_chunking_version` is **in** that set. NULL is never in
  it, so pre-034 rows still never reuse. The other rules are unchanged:
  fingerprint `MATCH`, non-NULL prefix, exact pair, under-lock recheck by row
  id and pair.
- **The rule for the set:** it holds exactly the versions whose
  `embedding_input` and `build_embed_prefix` produce identical output. A
  version that changes either function resets the set to `{new}`. A golden
  test pins both functions' output on fixed fixtures and fails with the
  message "formatter changed: reset INPUT_COMPATIBLE_CHUNKING_VERSIONS". A
  formatter change cannot then keep a stale set without someone editing that
  golden file, which is a reviewed act.
- **The effect.** A single-window section of at least `CHUNK_MIN_CHARS` is
  byte-identical between the versions, and so is its prefix (same locator),
  so it reuses its vector. Merged chunks, every window of a multi-window
  section (the lookback moves the first cut, and with it all later ones), and
  re-tailed sections go to the provider. Their exact share is measured on the
  copy (recipe step 4) and is not guessed here. The upper bound is #355's
  full re-embed.
- **Rollback** to a v1 build uses that build's strict rule (`= 1`). Notes
  stamped 2 are re-selected there and fully re-embedded. That is
  conservative and correct (L8).

The `CHUNKING_VERSION` docstring and the embedding-providers requirement
gain the set.

## D7: `CHUNK_MIN_CHARS` and the stored fingerprint

`CHUNK_SIZE` and `CHUNK_OVERLAP` are in the stored, refusing fingerprint. A
change to either is a declared reset. `CHUNK_MIN_CHARS` shapes the stored
vector set in the same way, so it belongs with them, but adding a key
unconditionally would change every deployment's fingerprint string. Production
deploys on merge, so the new pod would refuse, crash-loop and need a reset:
#355's Context fact 1, the outcome #355 was designed to avoid.

**Rule:** `embedding_fingerprint()` includes `"chunk_min_chars": n` **only
when `n != 200`** (`CHUNK_MIN_CHARS_DEFAULT`, one constant shared with the
setting's default). `v` stays 1.

- At the default, the fingerprint string is byte-identical to today's, and the
  deploy proceeds.
- An operator who sets another value gets a refused startup naming the
  differing field and the reset workflow, exactly as for `CHUNK_SIZE`.
- Setting the value back to 200 over a non-default index refuses too, which
  is correct, because those vectors were built under the other value.

The README, `.env.example` and the reset list name it beside `CHUNK_SIZE`
(embedding-providers' "name its remedy" requirement). The retrieval
fingerprint inherits the field.

Rejected:

- A per-note stamp of the value. That needs a migration, plus a second backlog
  predicate on every #355 surface, for a knob nobody should turn in
  production.
- A module constant with no setting. It would leave the harness unable to
  measure 300 against 200 on a copy without a code change.

## D8: Measuring it

The search-path eval gate applies. The recipe is DEPLOYMENT.md's "Before and
after a re-chunk or re-embed", with these changes:

- **Baseline copy at version 1, drained.** If production's #355 transition
  has not finished, the restored copy is first drained at version 1 by the
  incumbent image (the same one-off container, recipe step 4 of #355). The
  baseline is then `make search-eval … ARMS="keyword semantic"` from the
  incumbent checkout.
- **Before distribution.** Run the candidate checkout's harness with
  `ARMS="semantic"` on the same drained copy, before re-embedding. Its
  `chunk_lengths` is the before measurement. Its `legacy_chunked_notes` is
  nonzero, as expected, and that run is not the candidate.
- **Re-embed to version 2** with the candidate image, budgets 0. Record
  `SELECT count(*) FROM note_embeddings`, the provider-call count (the sum of
  the run rows' embedded chunks), and the pass durations. That measures D6's
  saving and is the production estimate.
- **Candidate run** as in #355's step 5 (`runfile:` baseline). It requires
  `legacy_chunked_notes: 0`, and its `chunk_lengths.under_min` must be
  exactly the number of notes whose whole text is shorter than the minimum
  (the SQL is in the recipe).
- **The predicate literal** in the recipe's drain check becomes
  `IS DISTINCT FROM 2`.

**`chunk_lengths`**: one read-only `SELECT` under the same owner predicate as
`legacy_chunked_notes`. It returns `{chunks, under_min, p10, p50, p90}`:
`length(btrim(chunk_text))` over the provenance owner's `note_embeddings`
joined to `notes_metadata`, with `under_min` against the running
`CHUNK_MIN_CHARS`, `percentile_disc`, and null on an empty set or a pre-034
copy. It holds no text and no path. Fixture arms do not gain it, so the
golden file is unchanged.

**Release bar (owner decision 9).** Candidate recall@10 and nDCG@10 on the
prose class are not below baseline by more than the report's noise band.
`under_min` is the short-note residue only. If the metrics regress, the
change does not ship at 200. The operator re-measures at another value on
the copy, and the default moves before merge.

## Rollout: what the operator sees

- **Deploy.** No migration. No refused startup at the default. The startup
  `Embedding retrieval:` line shows `chunking_version: 2`.
- **Transition.** The dashboard pending count jumps to about the note count
  and falls each pass. Wall-clock time is at most #355's (1.5 to 4 hours by
  that estimate) and likely less because of D6. Recipe step 4 replaces the
  guess with a number. `index_transition:` is on every vector response, and
  `/health` reads `rechunk: in_progress` until the backlog drains.
- **Rollback.** No schema to undo. Deploy the previous image. Its strict
  reuse rule fully re-embeds the notes already stamped 2, and the transition
  runs again in reverse.

## Risks / Trade-offs

- **A second whole-vault transition, days after the first.** The
  provider-load and mixed-ranking costs repeat. They are bounded by the
  budget and the marker, and reduced by D6. This is the main cost of the
  change, and it is accepted for the reason in D5.
- **Cross-version reuse is a new correctness surface.** If the set ever
  names a version with a different formatter, the vectors silently
  mismatch. The golden formatter test guards it, and the adversarial pass
  targets it.
- **The conditional fingerprint key.** A bug here either refuses every deploy
  (the key emitted at the default) or misses a non-default change.
  Byte-equality tests against today's fingerprint string at the default, and a
  refusal test at 300, guard both.
- **Precision on notes of many short meaningful sections** (a glossary of
  one-liners). Groups of about 200 characters hold several entries, so a
  query for one entry matches the group, and the locator may be coarser. The
  harness measures it.
- **Merged text in previews.** A preview can now show an inner heading
  line, collapsed to one line by `_preview_line`, so no line can be forged.

## Owner decisions (taken by default, 2026-10-30)

1. **`CHUNK_MIN_CHARS` is a setting**, default 200, range 0–1,000, at most
   half a window, in the stored fingerprint only when it is not 200 (D2, D7).
2. **Merge forward. A short section at the end of the note merges backward.**
   The locator is the deepest section whose `read_note` body contains the
   chunk, not the next or previous section's (D3, D4).
3. **A merged chunk's text is the contiguous span, inner heading lines
   included** (D3).
4. **Lookback is a constant**, line break then whitespace, `min(200, W/10)`.
   No sentence detection (D1).
5. **Reuse across `INPUT_COMPATIBLE_CHUNKING_VERSIONS = {1, 2}`**, guarded by
   a golden formatter test (D6).
6. **Ship immediately after #355**, accepting a second transition (D5).
7. **The length distribution goes in the harness**, not the dashboard (D8).
8. **No ranking-time down-weighting.** It would hide the cause, keep
   degenerate vectors stored, and add a score transform that hybrid RRF, the
   reranker and the harness would all have to know about.
9. **The release bar** is no regression beyond noise on the real-data report
   (D8).
10. **No migration.**

## Accepted limitations

- **L1** — A short section merged into a following sibling (or a backward
  tail) takes their common parent's locator, or `(top of note)`. Its own
  ordinal is not the locator of any chunk. `read_note` at the reported
  ordinal still contains it.
- **L2** — A merged chunk can reach about 1.1 windows (about 560 tokens) of
  text, plus the prefix. Every supported provider accepts that. A provider
  that silently truncates at 512 tokens would lose up to about 50 tokens of
  the tail.
- **L3** — A window inside a group can still be short when the text holds a
  whitespace run longer than `W − CHUNK_MIN_CHARS`, so the window is mostly
  blank. Accepted rather than adding a third merge rule.
- **L4** — A run with no whitespace in the lookback (base64, a long URL, CJK
  text without spaces) is still cut hard, possibly mid-character-sequence.
- **L5** — Lengths are code points, not tokens. 200 CJK characters carry more
  tokens than 200 Latin characters. The rule is deterministic, not
  token-exact.
- **L6** — The second transition repeats #355's L1 (mixed ranking, declared
  by the marker) for its duration.
- **L7** — A note whose whole text is shorter than the minimum keeps its one
  tiny chunk. A stub note stays near-degenerate. Ranking-time handling is out
  of scope (owner decision 8).
- **L8** — Rolling back to a v1 build fully re-embeds the notes stamped 2,
  because the old build's reuse rule is strict.
- **L9** — The live "lves" row has not been traced to its note. The fixture
  reproduces both mechanisms (a 4-character tail, a mid-word start), not the
  exact note.
