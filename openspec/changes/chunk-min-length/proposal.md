## Why

GitHub #390. On the live server (2026-10-30) a 4-word chunk, "in the Email
Triage log", scored 0.837 and ranked first for a paraphrase query, and another
row's preview was the fragment "lves". A chunk that short is mostly its context
prefix: up to 300 characters of `Title > Heading` breadcrumb and tags against
about 25 characters of text. Its vector is close to the note's topic, so it
outranks real passages on any query about that topic. Heading-aware chunking
(#355) makes this more likely, because a heading followed by one short line is
now a chunk of its own.

The fragment has a second cause, and it is in the code today. The windowing
does not look for a word boundary. `chunk_text_bounded` cuts fixed
`CHUNK_SIZE × 4` (2,048) character windows with step = window, then strips
each one. So a window can start or end mid-word, and the last window of a
section is whatever is left over, which can be four characters long.

This ships right after #355, not months later, because any chunking change
re-selects every note for a gradual re-embed. #355's transition is the
machinery that makes this cheap and safe. Doing it now means two transitions a
few days apart, rather than one now and another once the vault has settled.

## What Changes

- **A minimum chunk length, `CHUNK_MIN_CHARS`** (setting, default 200, range
  0–1,000, and at most half the window). The limit applies after sectioning,
  to each section's cleaned, stripped text. A short section is joined with the
  following section into one contiguous span. The join includes the heading
  line between them, so the merged text stays a substring of the note. A
  short section at the end of the note is joined to the previous span instead.
  When a section's last window would be shorter than the minimum, it is
  appended to the window before it. A note whose whole cleaned text is shorter
  than the minimum still gets one chunk, so a note has zero chunks only when it
  has zero today. `0` turns the merge off.
- **The locator of a merged chunk is the deepest heading whose
  `read_note(section="#N")` contains the chunk's whole span**: the lowest
  common ancestor of the sections the chunk came from, or 0 (top of note) if
  there is none. Containment parity with `read_note` holds by construction. In
  the common shape (a heading, one short line, then subsections), the locator
  is the short section's own heading. An unmerged chunk keeps exactly #355's
  locator. The prefix uses the locator's heading path.
- **Word-boundary windows.** A window that ends before its span ends moves its
  cut back to the last line break, or failing that the last whitespace, within
  a lookback of `min(200, window ÷ 10)` characters. A run with no whitespace
  (base64, a long URL) is still cut hard. With overlap enabled, the next
  window's start moves forward to the first whitespace inside the same
  lookback.
- **`CHUNKING_VERSION` 1 → 2.** The existing per-note backlog re-selects every
  note and re-embeds it in the background, under the embed budget. The
  `index_transition:` line comes back on every vector-search response, and
  `/health` reports `indexer.rechunk: "in_progress"` until the backlog drains.
  **This is a second whole-vault transition shortly after #355's.**
- **Cheaper second transition: reuse across format-compatible versions.** A
  stored vector is a function of the model and the exact provider input.
  `embedding_input` and `build_embed_prefix` do not change between versions 1
  and 2. So the reuse lookup accepts a note stamped with any version in
  `INPUT_COMPATIBLE_CHUNKING_VERSIONS = {1, 2}`, with the exact
  `(embed_prefix, chunk_text)` key and the under-lock recheck unchanged. A
  single-window section of at least 200 characters keeps its vector, and only
  merged or re-windowed chunks go to the provider.
- **`CHUNK_MIN_CHARS` joins the stored embedding fingerprint only when it
  differs from 200.** At the default, the deploy does not refuse. A
  non-default value refuses startup and names the reset, the same treatment
  as `CHUNK_SIZE`.
- **Harness.** Semantic arms record `chunk_lengths` for the provenance owner:
  chunk count, count under `CHUNK_MIN_CHARS`, and p10/p50/p90 of the stored
  `chunk_text` length. This is the before/after evidence for #390, alongside
  the retrieval metrics. No dashboard change.
- **No migration.** `embedded_chunking_version` is a `smallint` with
  `CHECK (… >= 1)`, so version 2 fits.

No tool parameter, result format or docstring changes. Previews still come
from `chunk_text`. They now start and end on whitespace, and merged chunks
contain an inner heading line.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `index-integrity`: the heading-aware chunking requirement (the minimum-length
  merge, the LCA locator, word-boundary windows; "byte-identical to the
  pre-change chunker" is retired) and the reuse requirement (format-compatible
  versions).
- `embedding-providers`: the chunking-format requirement (the merge and the
  window boundary are part of the format, plus the compatible-version set) and
  a new requirement that puts a non-default `CHUNK_MIN_CHARS` in the stored
  fingerprint.
- `search-evaluation`: semantic arms also record `chunk_lengths`.

`search-quality` is unchanged. The locator rendering and the containment
scenario already hold under the LCA rule, and the preview wording is
untouched.

## Impact

- **Code**: `src/services/embeddings.py` (`chunk_note_bounded` grouping,
  `chunk_text_bounded` lookback and tail merge, `CHUNKING_VERSION = 2`,
  `INPUT_COMPATIBLE_CHUNKING_VERSIONS`, reuse lookup and recheck);
  `src/services/index_state.py` (conditional fingerprint key);
  `src/config.py` (one setting plus a validator); `scripts/search_eval/`
  (`chunk_lengths`).
- **Tests**: offline chunker tests, including both live examples as fixtures,
  a property test of containment and ordinal parity over generated notes, the
  zero-chunk invariant, the cap, and LF/CRLF/CR line endings. Also fingerprint
  and settings tests, cross-version reuse tests (offline and PostgreSQL),
  updated version literals, and a harness test.
- **Docs, in the same PR**: README (setting, reset note), `.env.example`,
  DEPLOYMENT.md (upgrade note: the second transition; the recipe's literal `1`
  becomes `2`), `docs/architecture/indexing-and-embeddings.md`, `search.md`,
  and the CLAUDE.md key-decision bullet.
- **Not touched**: the schema, the halfvec index, the input formatter, the
  prefix rule, the locator rendering, panel templates (so no browser pass), and
  the stored fingerprint at the default setting.
- **Gates**: chunking and search path, so the adversarial Codex pass is
  mandatory, and the real-data `make search-eval` report is attached if it is
  attachable.
