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

- No stored chunk's **collapsed length** (D1) is shorter than the effective
  minimum. The one exception is the single chunk of a note whose whole
  cleaned text is shorter (L7).
- No chunk's collapsed length exceeds one window. Its raw length exceeds one
  window only when the text's whitespace leaves no other way to meet the
  minimum (D7, L3).
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
where the module constant is 200.

**One length (Codex r2).** Every rule in this change uses one length: the
minimum, admissibility, tail balancing, `under_min` and the window bound. For
a text `t`, **`clen(t)`** is the code-point length of `t` after every maximal
run of **whitespace** is replaced by one space and the result is stripped of
spaces. Whitespace means exactly the six ASCII characters U+0009–U+000D and
U+0020. Python and SQL then agree: Python uses
`re.sub(r"[\t\n\v\f\r ]+", " ", t).strip(" ")`, and SQL uses
`length(btrim(regexp_replace(t, '[\t\n\v\f\r ]+', ' ', 'g')))`, as one
shared constant.

A **word start** is a non-whitespace character at index 0, or one preceded by
whitespace. A **line start** is a word start whose preceding whitespace run
contains `\n` or `\r`. A cut `c` for a window starting at `a` is
**admissible** when `clen(content[a:c]) ≥ m`. The stored `chunk_text` stays
the raw window, so reuse keys and previews are unaffected.

For a window starting at `a` whose limit `a + W` falls before the end of the
text, the cut is the first admissible one in this order:

1. the last line start in `(a + W − L, a + W]`;
2. the last word start in that range;
3. a hard cut at `a + W`;
4. **only if `clen(content[a:a + W]) < m`**, meaning the window is mostly
   whitespace: the first word start after `a + W` at which
   `clen(content[a:c]) ≥ m`, or failing that the first position at which it
   holds. This is the only way a window's raw length can exceed `W` outside
   tail balancing (L3).

Boundary preference therefore never picks a cut that leaves the preceding
window below `m`.

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

**Termination.** Every cut is admissible, so it lies beyond `a`, and with
overlap the next start is at least `a + 1`. The loop therefore advances. The cap still stops
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

**Length** is always D1's `clen`: whitespace runs (the six ASCII characters)
are collapsed to one space, and the result is stripped of spaces and counted
in code points. It is measured on the cleaned chunk text, never on the
prefixed input. For example, `A×1800 + space×390 + B×10` has `clen` 1,811,
not 2,200 (Codex r3).

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
when `clen(c_i) > 0`. It is **short** when it is non-empty and
`clen(c_i) < m`. A group's text is the whole-body-fence-cleaned
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

**Windowing a group.** D1 applies, plus **tail balancing** (Codex r2, which
replaces r1's backward re-cut). Suppose that, after the cut `c` of a window
starting at `a`, the rest of the text has `clen(content[c:end]) < m`. Then
the last two windows are re-decided over the span `[a, end)`. Let
`S = clen(content[a:end])`.

- **`S ≥ 2m + 1`: two windows, each at least `m`.** The cut is the
  admissible split nearest the collapsed midpoint. An admissible split is a
  word start `c'` with `clen(left) ≥ m`, `clen(right) ≥ m`, and both raw
  lengths at most `W`. Ties go to the earlier split. If no word start
  qualifies, the cut is a **midpoint hard cut**: the smallest `c'` with
  `clen(content[a:c']) = ⌊S/2⌋`. The left side is then exactly `⌊S/2⌋ ≥ m`.
  The right side is at least `⌈S/2⌉ − 1 ≥ m`, minus one if a collapsed space
  falls at the boundary, which is why the threshold is `2m + 1` and not `2m`.
- **`S ≤ 2m`: one window `[a, end)`.** Its collapsed length is at most
  `2m ≤ W`.
- **Raw bound.** Raw lengths at most `W` are kept whenever a qualifying cut
  exists. The midpoint hard cut and the one-window merge exceed `W` raw only
  through whitespace, and their collapsed length is always at most `W` (D7,
  L3).

**Overlap after tail balancing (Codex r3, normative).** With overlap, the
right window starts at `s = min(cut, max(cut − overlap × 4, end − W, a + 1))`.
It then moves forward to the first word start in `[s, cut]`. So it stays
within `W` raw whenever the split was in bound, it never passes the cut, and
coverage holds (`s ≤ cut`). Pinned: `CHUNK_SIZE=100` (W=400), overlap 99,
`A×599`:

- The first window hard-cuts at 400 and leaves `A×199`, so `S = 599`.
- The midpoint hard cut falls at 299, giving a left window `[0, 299)` (raw and
  `clen` 299).
- Unclamped, the right start would be 1, a raw length of 598. Clamped, it is
  `max(−97, 199, 1) = 199`, so the right window is `[199, 599)` (raw and
  `clen` 400).
- The provider input is `len(P) + 402`, which is at most `W + 302`.

**The two Codex inputs, pinned (task 2.6):**

- **W = 400, m = 200,** text `A×161 + " " + B×239` (raw 401). The first cut
  is a hard cut at 400 (no word start lies in `(360, 400]`), and it leaves
  `B`, so `clen = 1`. The span gives `S = 401 ≥ 401`. The only word start, 162,
  leaves the left side at 161 < m, so the cut is the midpoint hard cut at
  `clen = 200`, which is raw index 200. The output is two windows,
  `A×161 + " " + B×38` (raw 200, clen 200) and `B×201` (raw 201, clen 201).
- **Default W = 2048, m = 200,** text `A×1800 + space×390 + B×10` (raw
  2,200). The lookback `(1848, 2048]` holds only spaces, so the cut is a hard
  cut at 2,048, admissible with clen 1,800. The rest has clen 10, so the span
  gives `S = 1,811`. Word start 2,190 leaves the right side at 10, so the cut is
  the midpoint hard cut at `⌊1811/2⌋ = 905`. The output is two windows,
  `A×905` (raw 905, clen 905) and `A×895 + space×390 + B×10` (raw 1,295,
  clen 906).

**Property:** for every group, every window except the only chunk of a
whole-short note has `clen ≥ m` and `clen ≤ W`. Its raw length is at most
`W` unless step 4 of D1 or a tail-balancing case above had no in-bound
choice. Every non-heading cleaned character is in some window.

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

- **Amended by D9 (third commit).** Version 2 changes the embedded text, so
  a single compatible set no longer works. Each version's embedded-text
  formatter is frozen in `EMBEDDED_TEXT_FORMATTERS = {1: identity, 2:
  collapse_long_runs}`, the same pattern as the per-version extraction
  cleaners.
- **Reuse rule 2** now requires that the stamp is a key of the table (NULL
  never is), that the prefixes are equal, and that `fmt[stamp](stored_text)
  == fmt[2](new_text)`. In other words, the provider input the vector was
  built from equals the input that would now be sent. The fingerprint,
  non-NULL prefix and under-lock recheck rules are unchanged.
- **Rule for the table:** a frozen entry is never edited. A version that
  changes the embedded-text formatter adds an entry. A version that changes
  `build_embed_prefix` or the separator clears the table to its own entry.
  A golden test of every entry fails until a change is reviewed.
- **The effect.** A single-window section of at least `m` keeps an identical
  pair, and so reuses its vector unless its text holds a whitespace run longer
  than 8 characters (D9). That holds because the single-window branch
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

**The bound (amended after Codex r2):** every stored chunk has
`clen ≤ W`. Its raw length is also at most `W`, except where the text's
whitespace makes that incompatible with the minimum.

- **Ordinary text.** Every provider input is at most
  `EMBED_PREFIX_MAX_CHARS + 2 + W` characters. That is the maximum #355's
  version 1 already sends for a full window, so a provider that accepted
  version 1 accepts version 2.
- **Why ordinary text stays in bound.** D1's steps 1 to 3 cut at or before
  `a + W`. Tail balancing prefers in-bound splits. Merging joins *sections*
  into a group, which is then windowed, so it never appends text to an
  already-cut chunk.
- **The exception.** A window goes over `W` raw only through D1 step 4, or
  through tail balancing when **no valid split within `W` raw on both sides
  exists** (amended by the implementation review, round 1: the hard cut is
  clamped into `[end − W, a + W]` before any out-of-bound split is
  considered, and the `S ≤ 2m` merge splits at an in-bound valid cut rather
  than exceed `W`). In each case the excess is whitespace only, because
  collapsed content stays at most `W`.
- **The exception is unavoidable.** Take `A×100 + space×3000 + B×100`. Any
  window of at most `W` raw that holds the `A`s has `clen ≤ 101 < m`. So
  either the minimum or the raw bound must give way. This change gives up the
  raw bound, for whitespace only (L3), and reports how often that happens as
  `raw_over_window`.

The only chunks under `m` are therefore those of L7, a note whose whole text
is short, and L10's residue at `W ≤ 404`. Every split is validated on its two
actual slices, so stripping the boundary whitespace can no longer strand one
side below `m` (implementation review r1 M1).

A test builds the maximum case: a 300-character prefix, a group of
`W + m − 1` characters of prose, and tail balancing. It asserts:

- every provider input is at most `302 + W` characters;
- both of the last two windows have `clen ≥ m`;
- every character is covered.

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

**`chunk_lengths` and a single `under_min` (Codex r1 M4, r2).** Every count
uses D1's one length, `clen`. The SQL `clen` expression and the Python
`clen` function are one pair, kept together in `scripts/search_eval/db.py`
and `embeddings.py`, and a test checks that they agree. One read-only
`SELECT` under `legacy_chunked_notes`' owner predicate returns:

- `chunks`: the number of chunks.
- `p10`, `p50`, `p90`: percentiles of `clen(chunk_text)`.
- `whole_short`: chunks with `clen < m` whose note holds exactly one chunk
  (L7).
- `under_min`: chunks with `clen < m` whose note holds more than one chunk.
  There is no other exception.
- `raw_over_window`: chunks with `length(chunk_text) > W` (L3, observation
  only).

`under_min` SHALL be 0 for a conforming chunker. By D1's admissibility rule
and D3's tail balancing, every window except a whole-short note's only chunk
has `clen ≥ m`. `m` and `W` are the running configuration's.

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

## D9: Whitespace in the embedded input is bounded (third commit)

Round 2 left one gap. A chunk over `W` raw is over only by whitespace (L3),
but the provider input was `prefix + raw chunk`. The `A×100 + space×3000 +
B×100` case would therefore send more than 3,000 characters to the provider.
That costs tokens and latency, and on a token-limited endpoint it risks the
truncation M2 removed.

- **The rule.** The provider input is `embed_prefix + "\n\n" +
  embedded_text(chunk_text)`. `embedded_text` replaces every run of **more than
  8** whitespace characters with one space. Whitespace means the six ASCII
  characters of D1's length rule. Shorter runs and all other characters are
  kept. Stored `chunk_text` stays raw, so previews, containment and the locator
  are untouched. `embedded_input_digest` covers the inputs as sent, as before.
- **Why 8.** Paragraph breaks (`\n\n`, `\r\n\r\n`), two-level list
  indentation and table padding stay byte-identical. That keeps their
  structure in the embedding and keeps their v1 vectors reusable. A longer
  run carries no meaning.
- **Bound.** A chunk of at most `W` raw has embedded text of at most `W`, so
  #355's maximum (`W + 302`) holds exactly. An L3 chunk's embedded text is at
  most `clen + 7r + 2`, where `r` is the number of interior whitespace runs of
  2 to 8 characters in it and the 2 is its kept leading and trailing
  whitespace. Its input therefore exceeds #355's maximum by at most `7r + 2`.
  **Pinned:** for `A×100 + space×3000 + B×100` under prefix `P`, the embedded
  text is `A×100 + " " + B×100` and the input length is `len(P) + 203`.
- **Reuse cost.** Only chunks that contain a run longer than 8 miss version-1
  reuse (D6). Fenced code is masked before chunking, so these runs are rare.
  Two kinds remain: lists nested three levels deep with 4-space indentation, or
  five deep with 2-space indentation (a newline plus 8 or more spaces), and
  runs of blank lines. D8's `(S submitted, R reused)` line measures the actual
  share. Collapsing such a run also drops that list's deep indentation from the
  embedding, which is accepted.

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
5. **Reuse across versions through frozen per-version embedded-text
   formatters `{1, 2}`** (originally a compatible set; amended by D9), guarded
   by a golden formatter test (D6).
6. **Ship immediately after #355**, accepting a second transition (D5).
7. **The length distribution lives in the harness and the provider work in
   one log line.** Nothing new on the dashboard (D8).
8. **No ranking-time down-weighting.** It would hide the cause, keep
   degenerate vectors stored, and add a score transform that hybrid RRF, the
   reranker and the harness would all need to know about.
9. **The release bar** is D8's CI95 rule over four cells.
10. **No migration.**
11. **Whitespace runs longer than 8 characters are collapsed in the provider
    input only**, with per-version frozen embedded-text formatters for reuse
    (D9, D6).

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
| M4 | MAJOR | The `under_min` assertion contradicted L3. | Chunker kept simple, L3 kept. The predicate is exact SQL: stripped length below `m`, in a note with more than one chunk, with raw length below `W // 4`. It is made observable by storing the raw window. It equals 0 for a conforming chunker. `whole_short` and `whitespace_windows` are reported separately (D1, D8). Superseded in round 2. |
| M5 | MAJOR | The "noise band" was undefined. | The rule is now `deltas[<label>][overall, class:prose][recall@10, ndcg@10].ci95`. Accept when every upper bound is ≥ 0. Reject when any is below 0. `INSUFFICIENT_DATA`, a null CI or `CORPUS_LOSS` means re-measure (D8). |

Declined: none.

## Codex spec review, round 2 (2026-10-30)

FAIL on a3d4c36. B1, B2, M1, M3 and M5 were VERIFIED. M2 REGRESSED and M4
was NOT_FIXED. Both had one root cause: the tail rule and `under_min`
measured length in different ways. Codex reconfirmed that compatible-version
reuse is sound. Both findings were accepted.

| # | Status | Finding | Resolution |
|---|---|---|---|
| B1, B2, M1, M3, M5 | VERIFIED | — | — |
| M2 | REGRESSED | The backward re-cut could strand a prose window below `m`. At W=400, `A×161 + " " + B×239` gave windows of (162, 161) and (239, 239). | **One length.** `clen` (whitespace runs collapsed, then stripped) governs every rule. A cut must be admissible, meaning the preceding window has `clen ≥ m`. Normative tail balancing: for `S ≥ 2m + 1`, both windows are at least `m`, preferring the admissible word start nearest the midpoint and falling back to a midpoint hard cut. For `S ≤ 2m`, one window. Pinned output: (raw 200, clen 200) and (raw 201, clen 201) (D1, D3). |
| M4 | NOT_FIXED | The `W // 4` proxy hid the M2 window and flagged a conforming whitespace tail at the default W, `A×1800 + space×390 + B×10`. | `under_min` is now the single predicate `clen < m` in a note with more than one chunk, with no exception, and it must be 0. `whitespace_windows` and the old L3 are removed. The default-W case now yields (raw 905, clen 905) and (raw 1,295, clen 906), both pinned. The raw bound yields only to whitespace, and only when no in-bound choice meets the minimum. That is the new L3, reported as `raw_over_window` and proved unavoidable in D7 (D7, D8). |

Declined: none.

## Codex spec review, round 3 (2026-10-30)

FAIL on eed99c9, verification round. Six items were VERIFIED: the round-2
M2 and M4 fixes, the whitespace bound (D9), and the B1, B2 and M1 fixes they
touched. Two text-level MAJORs remained. Both are closed in this commit and
verified by the supervisor. There is no round 4.

| # | Status | Finding | Resolution |
|---|---|---|---|
| r3-1 | Stale definition | D2 still defined length as `len(text.strip())`, which contradicts `clen`. For `A×1800 + space×390 + B×10` that gives 2,200, not 1,811. | D2 now uses D1's `clen`, measured on the cleaned chunk text and never on the prefixed input. D3's non-empty test uses `clen > 0` (D2, D3). |
| r3-2 | REGRESSED | The overlap start after tail balancing could reach far before the cut. With `CHUNK_SIZE=100`, overlap 99 and `A×599`, the right window was 598 characters, breaking the provider-input bound. The clamp existed only in the design. | The clamp is now normative in index-integrity: `s = min(cut, max(cut − overlap × 4, end − W, a + 1))`, then a forward snap within `[s, cut]`. The high-overlap case is a scenario and a test. Its pinned output is `[0, 299)` and `[199, 599)`, and its input is at most `W + 302` (D3). |

Declined: none.

## Accepted limitations

- **L1** — When a short section merges into a following sibling, or a tail
  merges backward, the chunk takes the common parent's locator or
  `(top of note)`. The short section's own ordinal is not the locator of any
  chunk, but `read_note` at the reported ordinal still contains it.
- **L3** (replaces round 1's whitespace-window exception). The minimum is
  never waived. When whitespace makes the minimum incompatible with a raw
  length of at most `W`, a window may exceed `W` raw, by whitespace only. Its
  `clen` stays at most `W`. The provider never receives the long runs (D9).
  Such an input exceeds version 1's maximum by at most 7 characters per short
  whitespace run. These windows are counted as `raw_over_window`, and the
  proof that the exception is unavoidable is in D7.
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
- **L10** (implementation review r1) — At `W ≤ 404` (`CHUNK_SIZE ≤ 101`),
  where `2m + 2 > W`, a balanced span can have `S > W` and no valid split:
  single-character words around the midpoint, so every cut leaves one side
  below `m`. Example at `CHUNK_SIZE = 2` (W = 8, m = 4): a span `a b c d e`
  (`S = 9`) — every word-start split gives (3, 5) or (5, 3), and a cut on a
  space strips to the same. The pure midpoint cut stays, so one chunk is
  below `m` and `under_min` can be non-zero on such a deployment. Why the
  residue is that narrow: a valid split always exists when `S ≥ 2m + 2` (at
  the first position where the left side reaches `m` it holds `m` or `m + 1`,
  leaving the right at least `S − m − 2 ≥ m`), and with none the span is one
  window when `S ≤ W`. So the residue needs `W < S ≤ 2m + 1`, i.e. `W ≤ 2m`,
  which holds only at `W ≤ 400` (`CHUNK_SIZE ≤ 100`; the brief's "≤ 404" is
  a safe over-statement). Never at the default window.

## Implementation notes (2026-10-30)

Choices the spec left open, and the places where the implementation had to
read it one way. None changes a pinned output: all four pinned cases (W=400
`A×161 + " " + B×239`, default W `A×1800 + space×390 + B×10`, W=400 overlap
99 `A×599`, `A×100 + space×3000 + B×100`) reproduce exactly.

1. **`min_chars` is a parameter of `chunk_text_bounded` only**, default 0
   (task 2.1). `chunk_note_bounded` takes no minimum argument: it always
   passes `effective_min_chars(W)`. `chunk_text` and the zero-chunk probe keep
   0, which cannot change whether a text has a chunk.
2. **"Empty" is `clen == 0` everywhere** (a section's non-emptiness, a kept
   window), per D3 after r3-1, not `str.strip()`. The difference is Unicode
   whitespace other than the six ASCII characters: a note holding only NBSP
   now yields one chunk where version 1 yielded none. The zero-chunk
   invariant holds against the new `chunk_text_bounded`, as task 2.7 states.
3. **The midpoint hard cut** is the smallest position whose left side has
   `clen ≥ ⌊S/2⌋`. Exact equality is unreachable when the half falls on a
   collapsed space (including a word's first character also includes the
   space before it, so `clen` steps by two); the right side still keeps `m`
   because `S ≥ 2m + 1`.
4. **"Nearest the collapsed midpoint"** is measured as
   `|clen(left) − clen(right)|`, the earlier word start on a tie.
5. **D1 step 4 respects the window bound.** The first admissible word start
   past the limit is taken only when the window it ends has `clen ≤ W`;
   otherwise the first admissible position is. Without this, a mostly
   whitespace window followed by a long unbroken run (`A` + 3,000 spaces +
   5,000 `B` + ` C`) produced a chunk of `clen` 5,002, contradicting the
   Bounds clause.
6. **The whole-body groups record ordinal 0 for every window**, as the
   grouping and "No sections" clauses say ("at ordinal 0"), rather than the
   deepest containing section. Both satisfy parity; a later window of a
   whole-body group may lie inside one section's body and still reports 0.
7. **Task 2.7's raw-bound condition** ("no whitespace run longer than
   `W − 2m`") was not sufficient under the first implementation: a
   1,098-character unbroken run followed by a 1,428-space run gave a chunk of
   2,052 raw because the fallback was the pure midpoint although an in-bound
   cut at 628 existed. **Resolved by the review round 1 clamp rule** (tail
   balancing prefers any valid split within `W` raw on both sides), and task
   2.7 is reworded to that rule: a chunk exceeds `W` raw only when no valid
   in-bound split exists, and then by whitespace only. That case is pinned.
8. **`S` in the completion line is summed over the same certified notes as
   `X`** (`EMBEDDED` and `CERTIFIED_EMPTY`), so `R = X − S` is never
   negative. Provider work spent on a note that then failed or lost its
   certification race is not in `S`. The line's test is on PostgreSQL with a
   fake provider (a real pass), not an offline mock of `embed_vault`.
9. **The reuse lookup returns `(row id, vector, stored chunk_text)`**, and
   the under-lock re-check compares the stored row's own text. Under version
   2 two different raw texts can map to one embedded input (runs of 9 and 20
   spaces), so the new chunk's text is not the row's.
10. **`CLEN_SQL` lives in `embeddings.py` beside `clen`**, and
    `scripts/search_eval/db.UNDER_MIN_SQL` is built from it. DEPLOYMENT.md
    quotes that statement with `m = 200`; an offline test checks the doc
    holds it byte for byte, and a PostgreSQL test checks the SQL and Python
    `clen` agree (tabs, CR, VT, FF, NBSP, long runs).
11. **`chunk_lengths` is null for an owner with no chunks** as well as for a
    pre-034 copy; the percentiles are `percentile_disc`.
12. `_note_sections` keeps its output; it now delegates to
    `_sections_from_headings` so the chunker scans headings once.
    `_chunk_note` returns each chunk's raw span beside it (the property test
    reads it for coverage); `chunk_note_bounded` is its public wrapper.
13. #355's offline tests were updated, not deleted: their sections were
    padded to at least `m`, and the "byte-identical to the pre-change
    chunker" test now compares with `chunk_text_bounded(…, min_chars=m)`.

## Codex implementation review, round 1 (2026-10-31)

FAIL on ee2e152: 3 MAJOR + 1 MINOR, all accepted and fixed. No tenant-boundary
or destructive-write finding. The `openspec-verifier` pass on the same head
found 0 blockers and 1 MAJOR + 4 MINOR (the MAJOR the same root cause as M1),
all folded in below; its 430k-chunk property run was otherwise clean.

| # | Severity | Finding | Resolution |
|---|---|---|---|
| M1 | MAJOR | The midpoint fallback measured the right side as `S − left − 1`; stripping boundary whitespace stranded it: `A×199 + space×1700 + "x " + B×199` gave (raw 1900, clen 201), (raw 200, clen 199). The verifier reproduced it under a heading (`"# Notes\n" + A×199 + space×1900 + "a " + B×199`). | Every split is validated on its two **actual** slices (prefix sums, O(span)). With no valid split and `S ≤ W` the span is one window (always possible at the default window). Both inputs pinned: one chunk, `clen` 401. |
| M2 | MAJOR | The pure midpoint could exceed `W` raw although an in-bound split existed: `A×300 + space×1822 + B×175` gave a 2,059-raw chunk. | Tiered choice: valid word starts within `W` raw on both sides, then any valid position within `W` (the hard cut clamped into `[end − W, a + W]`), then out-of-bound word starts, then positions; nearest the midpoint within a tier. Pinned (249, 249), (2048, 227); implementation note 7's case pinned (628, 628), (2048, 621). |
| M3 | MAJOR | The unconditional `S ≤ 2m` merge exceeded the input bound: W=400, `space×8 + A×400 + space×8` under a 300-char prefix sent 718 > 702 + 14. | The merge splits at an in-bound valid cut when its raw length exceeds `W`. Pinned (208, 200), (208, 200), every input ≤ 702. |
| m1 | MINOR | The main reuse section of the indexing note still said byte-identical raw text and an equal chunking version. | Rewritten: equal prefix and `fmt[stored](stored) == fmt[current](new)`, any formatter-table version eligible. |

Verifier MINORs, folded: the Cut clause states the `clen ≤ W` condition on
the word start (note 5); the Locator clause says a whole-body group records 0
(note 6); D9's bound is `clen + 7r + 2`; "No sections" no longer claims
parity with the pre-#355 chunker for NBSP-only notes (one chunk; an NBSP test
added); note 7 resolved by the clamp rule (D7 and task 2.7 reworded). Lesser
items: DEPLOYMENT.md says its `under_min` statement is tenant-wide while the
report's is owner-scoped; the backstop's strict `= :cv` carries its
justifying comment (task 2.5); the task 1.2 test compares the fingerprint
literal byte for byte. The verifier's generator is now
`test_the_verifier_property` (12 size/overlap pairs × 25 notes). New
limitation **L10** records the small-window residue (`W ≤ 400`).

Declined: none.

## Codex implementation review, round 2 (2026-10-31)

PASS on d479ba5, a verification round. All four round-1 fixes (M1, M2, M3,
m1) VERIFIED, no new findings, and 1,375 randomized cases clean.

Declined: none.
