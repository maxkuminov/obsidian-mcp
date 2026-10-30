# Design: a minimum chunk length and word-boundary windows (#390)

## Context

#355 (archived as `2026-10-30-heading-chunks`) splits a note at its ATX
headings, cleans each section with the whole-body fence spans, and windows
each section with `chunk_text_bounded`. The provider input is
`embed_prefix + "\n\n" + chunk_text`. The prefix is up to 300 characters of
`Title > H1 > … > Hk`, plus an optional `tags:` line. The chunking format is
`CHUNKING_VERSION` (now 1), stamped per note. A bump re-selects every note
through the embed backlog, under the per-tenant budget. While a caller's
scope still holds vectors of another generation, every vector-search response
carries the `index_transition:` line, and `/health` reports
`indexer.rechunk: "in_progress"`. This change uses that transition as built
and changes none of it.

Observed live on 2026-10-30, after #354:

- A chunk of four words, "in the Email Triage log", scored 0.837 and ranked
  first for a paraphrase query.
- Another row's preview was "lves".

**What the windowing does today (read and checked).** There is no
word-boundary handling. `chunk_text_bounded(content, chunk_size=512,
overlap=0)` works like this:

- It sets `char_size = 512 × 4 = 2048` and `step = char_size − overlap × 4`.
- It slices `content[start:start + 2048]`, keeps the slice's `.strip()`, and
  advances by `step`.
- Nothing looks for whitespace.

So a window starts and ends wherever character 2,048·k falls, usually inside
a word. The last window of a section is whatever is left over: a section of
2,048·k + 4 characters ends in a 4-character chunk, and "lves" is one such
tail, or a mid-word start. Only a text that fits in one window is stored as
one chunk, and that chunk is not stripped.

**Why a tiny chunk scores high.** Its provider input is mostly prefix. For "in
the Email Triage log" (23 characters) under a breadcrumb of 40 to 300
characters, the vector is roughly the vector of the title and heading. Any
query about the note's topic lands close to it, closer than to a real passage.
The fix belongs at chunking time: do not store a chunk whose text is too small
to outweigh its own prefix.

## Goals / Non-Goals

**Goals**

- No stored chunk's stripped text is shorter than the effective minimum. The
  only exceptions are the single chunk of a note whose whole cleaned text is
  shorter (L7) and a whitespace-dominated window (L3).
- No chunk text is longer than one window, so no provider input is longer than
  #355 could already produce (D7).
- No window boundary falls inside a word, unless the lookback holds no
  whitespace.
- Every non-heading cleaned character of a section is in at least one chunk.
- Containment and ordinal parity with `read_note(section="#N")` survive,
  including for merged chunks.
- One more background transition on #355's machinery, made as cheap as
  correctness allows (D6).
- The before/after effect is measured with deterministic predicates and a
  fixed release bar (D8).

**Non-Goals**

- Down-weighting short chunks at ranking time (owner decision 8).
- A token-exact window. The 4-characters-per-token heuristic stays.
- Sentence segmentation.
- A setting for the minimum (D2).
- A dashboard widget for chunk lengths.
- Any change to the prefix, the input formatter, the locator rendering, the
  schema or the transition contract.

## D1: Word-boundary windows, raw window text, and coverage

`W = CHUNK_SIZE × 4` and `L = min(CHUNK_BOUNDARY_LOOKBACK_CHARS, W // 10)`,
where the module constant is 200. A **word start** is a non-whitespace
character at index 0 or preceded by a `str.isspace()` character. A **line
start** is a word start whose preceding whitespace run contains `\n` or `\r`.

For a window starting at `a` whose limit `a + W` falls before the end of the
text, the cut is chosen in this order:

1. the last line start in `(a + W − L, a + W]`;
2. otherwise, the last word start in that range;
3. otherwise, a hard cut at `a + W`.

The window is `content[a:cut]`, and the next window starts at `cut`. Because
the cut lands on the first character of a word, a window ends with the
whitespace before that word, and the next window starts on it.

**The stored text is the raw window. It is no longer stripped.** It is
`content[a:cut]` exactly, so `length(chunk_text)` is the window's raw length,
which D8's predicate needs. This matches the single-window branch, which has
always stored unstripped text, so those chunks stay byte-identical (which
matters for D6). A window is emitted only if its stripped text is not empty,
as today. Previews already trim (`_preview_line`).

**Overlap (Codex r1 B2).** With `CHUNK_OVERLAP > 0`, the tentative next start
is `s = max(cut − overlap × 4, a + 1)`. It moves forward to the first word
start in `[s, cut]` and **never beyond `cut`**. If that range holds no word
start, `s` stands. The next window's limit is measured from its own start.

**Coverage, stated normatively.** Every next start is at most the previous
cut, and the first window starts at 0. So every character of a group's text
lies in at least one window, and every non-whitespace character of a window
reaches the stored chunk because nothing is stripped. Codex's case shows what
the old snap did wrong. With `W = 400`, overlap 1, and the text `A×400 + B×20 +
" " + C×500`:

- The first window hard-cuts at 400, because the lookback holds only `A`.
- The tentative start is 396, and `[396, 400]` holds no word start, so the
  next window starts at 396.
- That window contains every `B`.

The old, unbounded snap skipped all twenty. This case is a scenario and a
test.

**Termination.** A window holds at least `W − L` characters, or, with overlap,
the next start is at least `a + 1`. So the loop advances. The cap still stops
one chunk past `MAX_CHUNKS_PER_NOTE`.

Why line start, then word start, and nothing smarter: markdown makes a line
break the strongest boundary that is cheap to find deterministically.
Sentence punctuation depends on the language and collides with
abbreviations, versions and paths.

## D2: The minimum is a constant, with an effective value (Codex r1 B1)

`CHUNK_MIN_CHARS = 200` is a **module constant** in `embeddings.py`. It is
not a setting, it adds no fingerprint field, and it has no cross-field
validator. The **effective minimum** is `m = min(CHUNK_MIN_CHARS, W // 2)`,
computed from the configured window. That way every previously valid
`CHUNK_SIZE`, `CHUNK_SIZE=1` included, keeps working, and the deploy never
refuses at startup. A later change to the constant changes the chunking format,
so it is a `CHUNKING_VERSION` bump, like the lookback constant (see
embedding-providers). `CHUNK_SIZE` itself stays in the refusing fingerprint,
so a change to it, and with it to `m`, is the existing declared reset.

**Length** is `len(text.strip())` in code points, measured on cleaned text.
It is never measured on the prefixed input.

**Why 200.**

- It is about 50 tokens, roughly two sentences or one substantive list item,
  and under 10% of the default window.
- The prefix is up to 300 characters (about 75 tokens), typically 30 to 120.
  At 200 characters of text, the chunk is at least 40% of its input under the
  longest prefix, and typically 65% to 85%. The live 23-character chunk was
  under 40% even under a short prefix.
- A larger value would fold more genuine one-paragraph sections into their
  neighbours and coarsen more locators (L1). The harness decides whether 200
  is right (D8). Another value is a code change plus a version bump, measured
  on a copy first.

## D3: Where short text goes: the grouping rule

The sections come from `_note_sections`, unchanged. Section *i* has an
own-text raw span `[s_i, e_i)` and cleaned text `c_i`. It is **non-empty**
when `c_i.strip()` is non-empty and **short** when it is non-empty with a
stripped length below `m`. A group's text is the whole-body-fence-cleaned
contiguous raw span `[s_first, e_last)`, so it includes the heading lines of
absorbed sections and of any empty sections between them.

Walk the non-empty sections in document order:

1. **Open** a group at the next non-empty section.
2. **Extend forward**: while the open group's text is short and another
   non-empty section follows, extend the group to it. Once the group reaches
   `m`, it closes.
3. **End of body, still short:**
   - If a closed group exists, the open group joins the last closed group
     (that group's span is extended to the end).
   - Otherwise this is the note's only group (Codex r1 M1). If the note's
     *whole* cleaned body, heading lines included, is also short, the group
     stands alone and is the note's single chunk. If not, the note is chunked
     as **one group of the whole cleaned body** at ordinal 0, which is the
     same shape as #355's headings-only fallback.

The outline-heavy case shows why the last rule is needed: sixty empty-bodied
headings, then a final heading whose body is `x`. That yields one chunk
containing the outline and the `x`, not a one-character chunk. It is a
scenario and a test.

Every group except a sole short group in a short note therefore reaches `m`.
A short section never stands alone while a neighbour exists. The forward
extension already covers the "short group with no previous group" case
whenever a later non-empty section exists.

**Windowing a group.** D1 applies, plus the **tail rule**. If the text left
after a cut would have a stripped length below `m`, the cut is recomputed
with the limit `p = n − m`, where `n = len(text.rstrip())`. The cut is the
last line start, else the last word start, in `(p − L, p]`, else a hard cut at
`p`. The final window therefore starts on a non-whitespace character and its
stripped length is at least `m`. The re-cut only moves a cut **back**, so no
window is ever longer than `W` (D7). At the default sizes the shortened
preceding window keeps at least `W − m − L` (about 1,650) characters.

**Why the span keeps heading lines.** A merged chunk must still be one
substring of the note. Concatenating the cleaned texts without the heading
lines would produce text found nowhere in the note, so containment would
fail. The inner heading also embeds the subsection's own name, which the
prefix (built from D4's locator path) no longer carries. A one-section group
is exactly #355's section text, with no heading line.

**Zero-chunk invariant** is unchanged. Merging moves text between chunks and
never drops it. #355's headings-only fallback is one group with locator 0,
and the tail rule applies to it.

**Direction: forward first, backward only at the end.** Neither of the
brief's sketches keeps parity:

- Under the *next* section's locator, the short text precedes heading N+1, so
  it is outside `read_note(#N+1)`.
- Under the *previous* chunk's locator, a short sibling falls outside
  `read_note(#N−1)`.

D4's locator rule is what guarantees parity. The direction is chosen for the
common shape: a heading, one short line, then subsections. Merging forward
joins the line with the text it introduces, and the locator is then the short
section's own heading.

## D4: The locator of a chunk is the deepest section that contains it

Define `body_span(N)` as the body `read_note(section="#N")` returns: from one
terminator past heading N's `line_end` to the next heading of depth ≤
depth(N), or the end of the body (`_section_body_span`, imported).
`body_span(0)` is the whole body. Each chunk has a **raw span**, the raw-body
range of its window, recovered through the cleaned-to-raw offset map that
fence removal implies.

> A chunk's `section_ordinal` is the deepest N whose `body_span(N)` contains
> the chunk's raw span, or 0 if no heading's body does. Its `heading_path` is
> N's path from `_note_sections` (`()` for 0), and its prefix is built from
> that path.

Consequences:

- **An unmerged chunk** gets section *k*, exactly as in #355.
- **A parent's short lead merged into its first child** gets *k*, the parent.
  The child's later windows keep the child's ordinal.
- **A short section merged into a following sibling** gets their common
  parent, or 0 (L1).
- **The whole-body fallbacks** get 0.

**Parity holds by construction.** The chunk's text is the fence-cleaned text
of a sub-span of `body_span(N)`, so it is a substring of section N's body with
the whole-body fence spans removed. That is index-integrity's containment
contract and search-quality's "follow-up resolves" scenario, both unchanged.
That is why `search-quality` is not modified. A property test (task 2.7)
asserts parity on generated notes.

## D5: `CHUNKING_VERSION` 2, riding #355's transition

Bumping the constant is the whole mechanism. Everything else is #355's,
unchanged:

- **Backlog.** Every note's stamp is now `IS DISTINCT FROM 2`, so the backlog
  re-selects every note. Version-only notes go after content-stale ones, held
  to the per-tenant budget in every mode.
- **Serving.** Old vectors are served until each note's new rows commit.
  Nothing is marked stale, and previews stay.
- **Marker and health.** `index_transition: N notes not yet re-chunked; …`
  reappears on every `semantic_search`, `find_related` and `hybrid_search`
  response until the caller's scope drains. `/health` reads `indexer.rechunk:
  "in_progress"` meanwhile, and `status` is not changed.
- **Fingerprint.** The stored fingerprint does not change, so the deploy does
  not refuse.
- **Backstop.** The full-hash backstop recomputes digests only for notes
  certified under the current version (`= :cv`). A v1 note is therefore not
  cleared: it waits in the backlog and stays fresh.
- **If #355's own transition has not drained**, a NULL-stamped note goes
  straight to 2, and a v1 note reuses what it can (D6).

**This is a second whole-vault transition within days of the first.** The
marker and the mixed ranking (#355's L1) return for its duration. It ships now
because the operator is already watching #355's drain. Waiting would mean a
fresh disruption later for the same fix.

## D6: Reuse across format-compatible chunking versions

#355's D3 rule 2 disables reuse unless the note's stamp equals
`CHUNKING_VERSION`. The rule exists for one case: a formatter change
(`embedding_input`, `build_embed_prefix`) that leaves a `(embed_prefix,
chunk_text)` pair byte-equal but changes the provider input. This change
edits neither function. So between versions 1 and 2, an equal pair is the
same provider input and the same vector. Codex round 1 confirmed this
reasoning ("the compatible-version reuse key itself is sound").

- `INPUT_COMPATIBLE_CHUNKING_VERSIONS = frozenset({1, 2})`. Reuse rule 2
  becomes "the stamp is in the set". NULL is never in it. The fingerprint,
  non-NULL prefix, exact pair and under-lock recheck rules are unchanged.
- **The set's rule:** it holds exactly the versions whose two formatter
  functions produce identical output. A version that changes either function
  resets the set to `{new}`. A golden test of both functions fails until the
  set is reviewed.
- **The effect.** A single-window section of at least `m` keeps an identical
  pair, and so reuses its vector. That holds because the single-window branch
  was and stays unstripped, and the locator, and with it the prefix, is the
  same. Merged chunks, windows of multi-window sections, and re-tailed sections
  go to the provider. The share is measured (D8), not guessed. The upper bound
  is #355's full re-embed.
- **Rollback** to a v1 build uses that build's strict `= 1`, so notes stamped
  2 are fully re-embedded there (L8).

## D7: No provider input grows beyond what #355 could produce (Codex r1 M2)

The first draft let a tail merge produce a window plus up to `m − 1`
characters. A provider that silently truncates at its token limit could then
drop the merged tail while the row was certified and previewed in full.
**That is not accepted.**

**The bound:** every stored chunk's text is at most `W` characters, so every
provider input is at most `EMBED_PREFIX_MAX_CHARS + 2 + W`. That is the
maximum #355's version 1 already sends for an unmerged full window, so a
deployment whose provider accepted version 1's inputs accepts every version 2
input. Three facts give the bound:

- Windows are cut at no more than `W` (D1).
- The tail rule only moves cuts back (D3).
- Merging joins *sections* into a group, which is then windowed. It never
  appends text to an already-cut chunk.

So a short neighbour can always be absorbed, because the group is re-windowed
under the bound. There is no case in which a tiny chunk must remain because no
neighbour can take it. The only short chunks are L7 (the whole note is short)
and L3 (whitespace-dominated windows). A test builds the maximum: a
300-character prefix, a group of exactly `W + m − 1` characters, and the tail
rule. It asserts that every provider input is at most `302 + W` characters,
that the final chunk is at least `m`, and that every character is covered.

Rejected: the brief's "merge backward if it fits, else leave the short section
as its own chunk". Under group windowing that fallback never triggers, and it
would bring back the case this change removes.

## D8: Measuring it

The search-path eval gate applies. The recipe is DEPLOYMENT.md's "Before and
after a re-chunk or re-embed", with these changes.

**Provider work (Codex r1 M3).** `EmbedNoteResult` already carries
`chunks_submitted` (sent to the provider) and `chunks_embedded` (stored). The
embed stage sums both and extends its existing completion line to
`Embedding complete…: N notes, X chunks (S submitted, R reused)`, where
`R = X − S`. These are counts only, with no path and no text. The panel,
dashboard and progress endpoint expose nothing new. Recipe step 4 sums S and R
over the re-embed container's log, and S is the provider work the transition
costs.

**Steps.**

1. **Drain the baseline at v1.** If production's #355 transition has not
   finished, the incumbent image first drains the restored copy at version 1.
   The baseline is then `make search-eval … ARMS="keyword semantic"`, run from
   the incumbent checkout.
2. **Before distribution.** Run the candidate checkout's harness with
   `ARMS="semantic"` on the drained copy, before re-embedding. Record its
   `chunk_lengths`. Its `legacy_chunked_notes` is non-zero, which is expected,
   and that run is not the candidate.
3. **Re-embed to v2** with the candidate image, budgets 0. Record
   `SELECT count(*) FROM note_embeddings`, S and R from the log lines, and
   the pass durations.
4. **Candidate run** as in #355's step 5, with a `runfile:` baseline. It
   requires `legacy_chunked_notes: 0` and `chunk_lengths.under_min: 0`.

**`chunk_lengths` and a deterministic `under_min` (Codex r1 M4).** I chose to
keep the chunker simple and leave L3 in place, with an exact SQL predicate
that matches the normative exceptions. Removing L3 would need whitespace-aware
window widths, which complicates both the chunker and D7's bound. Storing the
raw window (D1) makes the window's length observable. One read-only `SELECT`
under `legacy_chunked_notes`' owner predicate returns:

- `chunks`
- `p10`, `p50`, `p90` of `length(btrim(chunk_text))`
- `whole_short`: chunks of notes that hold exactly one chunk whose stripped
  length is below `m` (L7)
- `whitespace_windows`: chunks whose stripped length is below `m` and whose
  raw `length(chunk_text) >= W // 4` (L3)
- `under_min`: chunks whose stripped length is below `m`, whose note holds
  more than one chunk, and whose raw length is below `W // 4`

A conforming chunker gives `under_min = 0`:

- Any window of a multi-chunk note that is not the final window of its group
  is at least `W − L − m − L ≥ 0.3·W` characters raw.
- A final window has stripped length at least `m` by the tail rule.
- Every group reaches `m`, except a sole group in a short note.

`m` and `W` are the running configuration's. The SQL text lives in
`scripts/search_eval/db.py`, and both the recipe and a test use it.

**The release bar (Codex r1 M5).** From the candidate run's `report.json`,
for the candidate arm's label, read
`deltas[<label>][<slice>][<metric>]["ci95"]`, for `<slice>` in `overall` and
`class:prose` and `<metric>` in `recall@10` and `ndcg@10`:

- **Accept** when every one of the four has `ci95[1] >= 0`, that is, the
  interval contains zero or lies above it.
- **Reject** when any has `ci95[1] < 0`. The change then does not ship at
  200. Another value is re-measured on the copy, and the constant moves,
  before merge.
- **Re-measure, never accept**, when the report's `verdict` is
  `INSUFFICIENT_DATA`, when any of the four `ci95` is null, or when the
  verdict is `CORPUS_LOSS`, which invalidates the run.

## Rollout: what the operator sees

- **Deploy.** No migration, no new setting, and no refused startup for any
  previously valid configuration. The startup `Embedding retrieval:` line
  shows `chunking_version: 2`.
- **Transition.** The pending count jumps to roughly the note count, then
  falls with each pass. Wall-clock time is at most #355's estimate, and D6
  likely cuts it. D8 step 3 measures it. Each pass logs `(S submitted, R
  reused)`. `index_transition:` and `rechunk: in_progress` hold until the
  drain finishes.
- **Rollback.** Deploy the previous image. There is no schema to undo, and the
  notes stamped 2 are fully re-embedded (L8).

## Risks / Trade-offs

- **A second whole-vault transition, days after the first.** Provider load
  and mixed ranking repeat. Both are bounded by the budget and the marker, and
  reduced by D6. This is the main cost, and it is accepted for D5's reason.
- **Cross-version reuse is a new correctness surface.** A set that names a
  version with a different formatter would mismatch silently. The golden
  formatter test guards against that, and the adversarial pass targets it.
- **Unstripped window text** changes every multi-window chunk's bytes. Those
  chunks change anyway because of the lookback, and single-window chunks were
  already unstripped. Previews trim.
- **Precision on notes of many short meaningful sections**, such as a glossary
  of one-liners: groups hold several entries, so locators may be coarser. The
  harness measures this.
- **Merged text in previews** can show an inner heading line. `_preview_line`
  collapses it to one line, so it cannot forge a result row.

## Owner decisions (taken by default, 2026-10-30; B1 by the owner)

1. **`CHUNK_MIN_CHARS` is a constant, 200.** The effective minimum is
   `min(200, W // 2)`. There is no setting, fingerprint field or validator. A
   change to the constant is a version bump (D2, Codex r1 B1).
2. **Merge forward, and backward only at the end.** A sole short group in a
   non-short note becomes the whole-body group. The locator is the deepest
   containing section (D3, D4).
3. **A merged chunk's text is the contiguous span**, inner heading lines
   included (D3).
4. **Lookback** cuts at a line start, then a word start, within `min(200,
   W/10)`. The overlap snap never passes the cut. The stored chunk text is the
   raw window (D1).
5. **Reuse across `INPUT_COMPATIBLE_CHUNKING_VERSIONS = {1, 2}`**, guarded by
   a golden formatter test (D6).
6. **Ship immediately after #355**, accepting a second transition (D5).
7. **The length distribution lives in the harness and the provider work in
   one log line.** Nothing new on the dashboard (D8).
8. **No ranking-time down-weighting.** It would hide the cause, keep
   degenerate vectors stored, and add a score transform that hybrid RRF, the
   reranker and the harness would all need to know about.
9. **The release bar** is D8's CI95 rule over four cells.
10. **No migration.**

## Codex spec review, round 1 (2026-10-30)

FAIL on 02f3948, with 2 BLOCKER and 5 MAJOR. All seven were accepted and
folded in. Codex confirmed the compatible-version reuse key is sound.

| # | Severity | Finding | Resolution |
|---|---|---|---|
| B1 | BLOCKER | The new cross-field validator (`min × 2 ≤ CHUNK_SIZE × 4`) refused previously valid deployments, for example `CHUNK_SIZE=64` at default 200, so the upgrade went down. | Owner's choice: no setting, no fingerprint clause and no validator. `CHUNK_MIN_CHARS` is a constant of 200, and the effective minimum is `min(200, W // 2)`, so every valid `CHUNK_SIZE` keeps working. Changing the constant is a `CHUNKING_VERSION` bump. The embedding-providers ADDED requirement is dropped (D2). |
| B2 | BLOCKER | The overlap start-snap could pass the previous cut after a hard cut and skip text. Example: W=400, overlap 1, `A×400 + B×20 + " " + C×500` loses the Bs. | The snap is confined to `[s, cut]` and never passes the cut. Coverage is now normative ("every non-heading cleaned character of a section is in at least one chunk"). Codex's exact case is a scenario and a test (D1). |
| M1 | MAJOR | A sole short group surrounded by empty sections stood alone even though the note was not short (sixty empty headings, then `x`). | The no-previous-group case is defined. Forward extension covers any later non-empty section. With none, the group stands alone only if the whole cleaned note is short. Otherwise the note becomes one whole-body group at ordinal 0. The outline case is a scenario and a test (D3). |
| M2 | MAJOR | L2 accepted silent provider truncation of a window plus a merged tail. | Not accepted. The tail rule moves cuts back instead of extending windows, and merging re-windows groups, so no chunk exceeds `W` and no input exceeds version 1's maximum. No tiny-chunk fallback is needed. A maximum-prefix, maximum-tail test is added. L2 is removed (D7). |
| M3 | MAJOR | The savings measurement had no data source: `indexer_runs` has no submitted-chunk count. | The embed stage's completion line gains `(S submitted, R reused)` from the existing `chunks_submitted` / `chunks_embedded`. These are counts only, with nothing new on the panel. The recipe sums them. A task and a test are added (D8). |
| M4 | MAJOR | The `under_min` assertion contradicted L3. | Chunker kept simple, L3 kept. The predicate is exact SQL: stripped length below `m`, in a note with more than one chunk, with raw length below `W // 4`. It is made observable by storing the raw window. It equals 0 for a conforming chunker. `whole_short` and `whitespace_windows` are reported separately (D1, D8). |
| M5 | MAJOR | The "noise band" was undefined. | The rule is now `deltas[<label>][overall, class:prose][recall@10, ndcg@10].ci95`. Accept when every upper bound is ≥ 0. Reject when any is below 0. `INSUFFICIENT_DATA`, a null CI or `CORPUS_LOSS` means re-measure (D8). |

Declined: none.

## Accepted limitations

- **L1** — When a short section merges into a following sibling, or a tail
  merges backward, the chunk takes the common parent's locator or
  `(top of note)`. The short section's own ordinal is not the locator of any
  chunk, but `read_note` at the reported ordinal still contains it.
- **L3** — A window can hold little text and be mostly whitespace when the
  text has a whitespace run longer than about `W − m − L`. It is reported
  as `whitespace_windows` and excluded from `under_min` by its raw length.
  A third merge rule was not added for it.
- **L4** — A run with no whitespace in the lookback, such as base64, a long
  URL or CJK text without spaces, is still cut hard.
- **L5** — Lengths are counted in code points, not tokens. The rule is
  deterministic, not token-exact.
- **L6** — The second transition repeats #355's L1, mixed ranking declared by
  the marker, for its duration.
- **L7** — A note whose whole cleaned text is shorter than `m` keeps its one
  tiny chunk. A stub note stays near-degenerate. It is reported as
  `whole_short`.
- **L8** — Rolling back to a v1 build fully re-embeds the notes stamped 2.
- **L9** — The live "lves" row was not traced to its note. The fixtures
  reproduce both mechanisms, not the exact note.
