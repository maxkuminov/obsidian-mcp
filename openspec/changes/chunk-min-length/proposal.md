## Why

GitHub #390. On the live server (2026-10-30), a 4-word chunk, "in the Email
Triage log", scored 0.837 and ranked first for a paraphrase query. Another
row's preview was the fragment "lves".

A chunk that short is mostly its context prefix: up to 300 characters of
`Title > Heading` breadcrumb and tags, against about 25 characters of text.
Its vector sits near the note's topic, so it outranks real passages on any
query about that topic. Heading-aware chunking (#355) makes this more likely,
because a heading followed by one short line is now a chunk of its own.

The fragment has a second cause, and it is in the code today. The windowing
does not look for a word boundary. `chunk_text_bounded` cuts fixed
`CHUNK_SIZE × 4` (2,048) character windows, with the step equal to the window,
and strips each one. A window can therefore start or end mid-word, and the last
window of a section is whatever is left over, which can be four characters.

This ships right after #355, not months later. Any chunking change re-selects
every note for a gradual re-embed, and #355's transition is what makes that
cheap and safe. Doing it now means two transitions a few days apart, instead
of a second disruption later.

## What Changes

- **A minimum chunk length.** `CHUNK_MIN_CHARS = 200` is a constant in
  `embeddings.py`. It is not a setting, and changing it is a
  `CHUNKING_VERSION` bump. The effective minimum is `min(200, window ÷ 2)`, so
  every previously valid `CHUNK_SIZE` still works and no deploy refuses.
  - After sectioning, a short section is grouped with the following section
    into one contiguous span, including the heading line between them, so the
    merged text stays a substring of the note.
  - A short group at the end of the note joins the previous group. A sole
    short group in a note that is not itself short becomes one whole-body
    group.
  - Every length is the **collapsed length**: whitespace runs are collapsed
    to one space, then the text is stripped. A cut is only taken if the window
    before it reaches the minimum. Tail balancing splits the last two windows
    so that each reaches the minimum, or merges them into one window.
  - A note whose whole cleaned text is short keeps one chunk. It never has
    zero, and no other chunk is ever below the minimum.
- **No provider input grows for ordinary text.** No chunk's collapsed length
  exceeds one window. The raw length goes over one window only by whitespace,
  and only when the whitespace makes the minimum impossible otherwise
  (counted as `raw_over_window`).
- **The locator of a chunk is the deepest heading whose
  `read_note(section="#N")` contains it**, or 0. Containment parity with
  `read_note` holds by construction. An unmerged chunk keeps #355's locator
  exactly. The prefix uses the locator's heading path.
- **Word-boundary windows.** A cut moves back to the last line start, or the
  last word start, within `min(200, window ÷ 10)` characters. It cuts hard
  only when that range holds no whitespace.
  - The overlap snap never passes the previous cut, so every non-heading
    cleaned character is in some chunk.
  - Window text is stored raw (unstripped), as single-window chunks already
    are.
- **`CHUNKING_VERSION` 1 → 2.** The existing per-note backlog re-embeds every
  note in the background, under the budget. The `index_transition:` line and
  `/health` `rechunk: in_progress` return until the backlog drains. **This is
  a second whole-vault transition shortly after #355's.**
- **Reuse across format-compatible versions.** The formatter does not change,
  so the reuse lookup accepts a note stamped 1 or 2
  (`INPUT_COMPATIBLE_CHUNKING_VERSIONS`). The exact-pair key and the
  under-lock recheck are unchanged. Unchanged single-window chunks keep their
  vectors.
- **Observation.** The embed stage's completion log line gains
  `(S submitted, R reused)`, counts only. Semantic harness arms record
  `chunk_lengths`, including a deterministic `under_min` that must be 0.
- **No migration**, and no new setting.

There are no tool parameter, result-format or docstring changes. Previews
start on a word, and a merged chunk can contain an inner heading line.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `index-integrity`: the heading-aware chunking requirement changes, adding
  the minimum, grouping, tail rule, LCA locator, lookback, coverage, the window
  bound and raw window text. The reuse requirement now accepts
  format-compatible versions.
- `embedding-providers`: the chunking-format requirement now covers the
  minimum constant, grouping, lookback, version 2 and the compatible set.
- `search-evaluation`: semantic arms record `chunk_lengths`.

`search-quality` is unchanged. The locator rendering and containment hold
under the new rule, and preview wording is untouched.

## Impact

- **Code**:
  - `src/services/embeddings.py`: grouping, lookback, tail rule, raw windows,
    locator, `CHUNKING_VERSION = 2`, the compatible set, reuse.
  - `src/services/indexer.py`: one log line.
  - `scripts/search_eval/`: `chunk_lengths`.
- **Tests**: offline chunker tests with both live examples and Codex's
  hard-cut-plus-overlap and outline-heavy cases; a property test for
  containment, parity and coverage; the window bound; cross-version reuse,
  offline and on PostgreSQL; the log line; the harness predicate; updated
  version literals.
- **Docs, in the same PR**: README result-format line, DEPLOYMENT.md (upgrade
  note on the second transition; the recipe), `indexing-and-embeddings.md`,
  `search.md`, and a CLAUDE.md bullet. `.env.example` is untouched, because
  there is no new setting.
- **Not touched**: schema, the halfvec index, the stored fingerprint, the
  input formatter, the prefix rule, locator rendering, and panel templates.
- **Gates**: the adversarial Codex pass is mandatory, and the real-data
  `make search-eval` report must meet D8's CI95 bar.
