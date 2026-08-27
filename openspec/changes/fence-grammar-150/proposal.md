## Why

`_FENCE_RE` in `src/services/links.py` recognises only a column-zero fence opener closed by a fence of exactly the same length. CommonMark allows up to three spaces of indentation on either fence and a closer **at least** as long as the opener, so an indented or longer-closed fence is not masked at all. A heading inside such a block stays visible to `_scan_headings`: it occupies a `#N` ordinal (shifting every later section's ordinal) and bounds the preceding section, so `edit_note(section="#1", content="new")` replaces only the opening fence line — silently deleting it and orphaning the block's contents into the body (issue #150, reproduced). That is a silent destructive write, the failure class this project treats as most expensive. The same gap affects every `mask_code` consumer: link extraction (`note_links`), inline tag extraction, and `move_note`'s link rewriting.

## What Changes

- Pin the shared code masker's fence grammar **explicitly** as a CommonMark subset, as a spec of its own (`code-masking`), instead of leaving it implementation-defined:
  - opener: up to 3 spaces of indentation, a run of ≥3 backticks or ≥3 tildes, then an info string; a backtick fence's info string may not contain a backtick (so a one-line ```` ```code``` ```` inline span never opens a block)
  - closer: up to 3 spaces of indentation, a run of the **same character at least as long as the opener**, nothing but whitespace after it
  - a backtick fence is not closed by a tilde fence or vice versa; a shorter run does not close; an inner "nested-looking" fence of a different char or shorter run is content
  - an **unterminated fence masks to end of note** (CommonMark: to the end of the containing block, which here is the document) — so a heading below an unclosed fence is never selectable
  - a fence "opened in one section and closed in another" is not a distinct case: masking runs over the whole note before heading scanning, so the block simply spans the boundary and both headings' visibility follows from the mask
  - documented divergences, kept deliberately: ATX headings remain column-zero only (stricter than CommonMark's 0–3 spaces — existing behavior); 4-space indented code blocks are not masked; inline-code masking keeps its current single-line single-backtick approximation; universal line terminators (LF/CRLF/lone CR) per the existing #146-era rule.
- **BREAKING (re-addressing):** on notes containing a newly-recognised fence shape, headings inside it stop being headings — `#N` ordinals shift, outlines change, and previously-selectable (wrongly selectable) sections disappear. Accepted, not mitigated: the previously-addressable heading was inside code, and writing to it destroyed the block. No refusal mode is added — the grammar is deterministic, so there is no ambiguity to refuse (issue #150's "refuse ambiguous notes" option is rejected in design.md).
- **Stale-index remediation:** `note_links` rows and `notes_metadata.tags` were extracted under the old masker and do not refresh on their own (extraction is `content_hash`-driven and the bytes on disk are unchanged). Ship a data migration that clears `content_hash` so the next indexer pass re-extracts links and tags for every note without re-embedding anything (`embedded_content_hash` still matches the recomputed hash).

## Capabilities

### New Capabilities
- `code-masking`: the shared fence/inline-code masking grammar (`mask_code`) that heading resolution, link extraction, tag extraction, and link rewriting all consume — exactly which byte spans count as code.

### Modified Capabilities
- `vault-write`: section addressing scenarios gain the newly-masked shapes — an indented opener, a longer closer, and an unterminated fence each hide their headings from `read_note(section=…)`/`edit_note(section=…)` symmetrically; the existing "shared masker" references now point at a pinned grammar rather than an unspecified one.
- `wikilink-graph`: "code blocks ignored" is re-anchored to the `code-masking` grammar, and the indexer requirement gains the re-extraction remediation (a masker-grammar change must force re-extraction; the hash-driven skip must not leave the graph stale).

## Impact

- `src/services/links.py` — `_FENCE_RE` rewrite (the whole change's code surface is essentially this regex plus its comment block).
- `src/services/vault.py` `_scan_headings` / section addressing — behavior shifts only via the mask; no code change expected.
- `extract_tags`, `extract_links`, `move_note` link rewriting — same.
- `alembic/` — one data migration (`UPDATE notes_metadata SET content_hash = NULL` or equivalent) to force re-extraction; no schema change, `alembic check` must stay clean.
- `docs/architecture/vault-tools.md` — the masker grammar and the re-addressing break must be recorded.
- Existing notes: ordinals shift only on notes containing a newly-recognised shape. This is the declared break; the truncation outline advertises ordinals per-response, and any response emitted after deploy is consistent with the new grammar.
