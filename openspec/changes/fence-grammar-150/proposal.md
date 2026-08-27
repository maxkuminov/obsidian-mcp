## Why

`_FENCE_RE` in `src/services/links.py` recognises only a column-zero fence opener closed by a fence of exactly the same length. CommonMark allows up to three spaces of indentation on either fence and a closer **at least** as long as the opener, so an indented or longer-closed fence is not masked at all. A heading inside such a block stays visible to `_scan_headings`: it occupies a `#N` ordinal (shifting every later section's ordinal) and bounds the preceding section, so `edit_note(section="#1", content="new")` replaces only the opening fence line — silently deleting it and orphaning the block's contents into the body (issue #150, reproduced). That is a silent destructive write, the failure class this project treats as most expensive. The same gap affects every consumer of the shared masker — link extraction (`note_links`), inline tag extraction, `move_note` link rewriting — **and** `clean_for_embedding` in `src/services/embeddings.py`, which carries its own private (LF-only, column-zero, exact-closer) fence grammar, so semantic search embeds code the masker would hide.

## What Changes

- Pin the shared fence grammar **explicitly** as a CommonMark subset, as a spec of its own (`code-masking`), and make every fence consumer — `mask_code` and `clean_for_embedding` alike — share one recognizer:
  - opener: up to 3 spaces of indentation, a run of ≥3 backticks or ≥3 tildes, then an info string; a backtick fence's info string may not contain a backtick (so a one-line ```` ```code``` ```` inline span never opens a block)
  - closer: up to 3 spaces of indentation, a run of the **same character at least as long as the opener**, then nothing but U+0020/U+0009
  - the masked span excludes the closing line's terminator, so the line boundary before a following heading survives
  - a backtick fence is not closed by a tilde fence or vice versa; a shorter run does not close; an inner "nested-looking" fence of a different char or shorter run is content
  - an unterminated **column-zero** fence masks to end of note (CommonMark's top-level container is the document); an unterminated **indented** opener is *not* a fence — flat scanning cannot know its enclosing container's extent (a list item under CommonMark), and fabricating end-of-note extent would let one stray line swallow every later section
  - fence state never crosses a valid line-1 frontmatter block: the block is opaque to fence scanning, so a fence-shaped YAML scalar cannot suppress body extraction
  - documented divergences, kept deliberately: container blocks are not parsed (matched fences are computed flat); ATX headings remain column-zero only; 4+-space indented code blocks are not masked; inline-code masking keeps its single-line approximation; universal line terminators (LF/CRLF/lone CR).
- **New refusal:** `edit_note(section=…)` refuses, by name, a note containing an unmatched indented fence opener — the one shape whose extent the flat grammar genuinely cannot determine. Reads keep working; the guarantee on such a note is the refusal, not the round trip (same doctrine as defective frontmatter).
- **BREAKING (re-addressing):** on notes containing a newly-recognised fence shape, headings inside it stop being headings — `#N` ordinals shift, outlines change, and previously-(wrongly-)selectable sections disappear. Accepted: the previously-addressable heading was inside code, and writing to it destroyed the block.
- **Stale-index remediation:** derived state (`note_links`, `notes_metadata.tags`, embeddings) was computed under the old grammars and does not refresh on its own (derivation is `content_hash`-gated and the bytes on disk are unchanged). Ship a per-note **extraction-version marker** (new column + migration): a stale marker forces link/tag re-extraction, and embedding invalidation **scoped to notes whose recognised fence spans actually changed**. `content_hash` is never nulled or overwritten — it stays the true hash, so move detection keeps working during the remediation window.

## Capabilities

### New Capabilities
- `code-masking`: the shared fence-recognition grammar and code masker that heading resolution, link extraction, tag extraction, link rewriting, and embedding cleaning all consume — exactly which spans count as code, and the frontmatter boundary rule.

### Modified Capabilities
- `vault-write`: the section-mode requirement's "indented/longer-closed fences are out of scope" residual scenario (which named this issue) is replaced by coverage; new requirements pin the newly-masked shapes' section behavior and the unmatched-indented-opener write refusal.
- `wikilink-graph`: link/tag extraction is re-anchored to the `code-masking` grammar, including the frontmatter-boundary behavior.
- `index-integrity`: a masker grammar change must force re-derivation of links/tags/embeddings via a versioned marker without corrupting `content_hash`-based note identity or move detection.

## Impact

- `src/services/links.py` — fence recognizer rewrite (line scanner exposing spans; `mask_code` consumes it).
- `src/services/embeddings.py` — `clean_for_embedding` consumes the shared recognizer; its private regexes are deleted.
- `src/services/vault.py` — section addressing picks up the mask change; section-mode write refusal for unmatched indented openers.
- `src/services/indexer.py` — extraction-version gating; scoped embedding invalidation during the re-derivation pass.
- `src/models/db.py` + `alembic/` — new `notes_metadata` extraction-version column (schema migration; `make test-schema` and `alembic check` gates apply).
- `docs/architecture/vault-tools.md` and `docs/architecture/indexing-and-embeddings.md` — grammar, refusal, remediation mechanism recorded.
- Existing notes: ordinals shift only on notes containing a newly-recognised shape; declared break, tracked here.
