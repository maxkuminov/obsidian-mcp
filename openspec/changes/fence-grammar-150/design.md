## Context

`mask_code` (`src/services/links.py`) is the single shared masker: `_scan_headings` (section addressing for `read_note`/`edit_note`, outlines), `extract_links` (the `note_links` graph), `extract_tags` (inline tags), and `move_note`'s link rewriting all scan `mask_code(text)`. Its `_FENCE_RE` recognises only a column-zero opener closed by an equally-long fence. CommonMark permits 0–3 spaces of indentation on either fence and a closer at least as long as the opener; an unterminated fence runs to the end of the containing block. The gap is a reproduced silent destructive write (issue #150): a heading "hidden" inside an unrecognised fence is selectable, and a section write there deletes the opening fence and orphans the block.

Constraints inherited from #140/#146 (see `docs/architecture/vault-tools.md` and the comment blocks in `links.py`):
- universal terminators — LF, CRLF as a unit, lone CR — everywhere the masker and heading scanner look;
- masking is same-length space substitution so byte offsets survive;
- headings are column-zero only (deliberately stricter than CommonMark) — do not change this here;
- read and write sides share one resolver, so masking changes affect both symmetrically by construction.

## Goals / Non-Goals

**Goals:**
- Pin the fence grammar explicitly as a CommonMark subset and make `_FENCE_RE` (or a successor scanner) implement it.
- Close the reproduced destructive write: a heading inside any CommonMark-recognised fence is never selectable.
- Refresh the stale derived state (`note_links`, `notes_metadata.tags`) that the hash-driven indexer will not refresh on its own.

**Non-Goals:**
- 4-space indented code blocks (not masked; documented divergence — column-zero headings can't hide in them, and link extraction there matches current behavior).
- Inline-code fidelity (CommonMark's equal-backtick-run pairing); the single-line single-backtick approximation stays.
- Widening ATX heading recognition to CommonMark's 0–3 space indentation.
- Any change to section write semantics (#140's body-span contract is untouched).

## Decisions

**1. Regex vs. line scanner.** The current single `(?s)` regex cannot express "closer at least as long as the opener" cleanly with a backreference (`\1` matches the exact opener; a longer closer needs `\1[`~]*` with a same-char guarantee). Prefer rewriting fence masking as a small line-by-line scanner over the universal-terminator line split: find opener lines with `^ {0,3}(([`~])\2{2,})(info)$`, then scan forward for the first line matching `^ {0,3}(\2{N,})[ \t]*$` where N = opener run length and same char; mask opener line through closer line inclusive (or through end of text when unterminated). A scanner is auditable against the CommonMark clauses one-by-one, where a widened regex was exactly what hid this bug. Byte-offset preservation is kept by masking spans of the original text.
- Alternative (rejected): patch the regex incrementally — each of the four grammar clauses composes badly in one pattern (the #146 comment block already strains to justify the current one).

**2. Backtick info strings may not contain backticks (CommonMark).** Without this, a one-line ```` ```code``` ```` inline-ish span opens a "block" that swallows everything to the next fence line. Tilde-fence info strings are unrestricted, per CommonMark.

**3. Unterminated fence masks to end of note.** CommonMark closes the block at the end of the containing block; here that is the document. This also kills the current failure where an unterminated fence masks nothing.

**4. Closer line = 0–3 spaces, same-char run ≥ opener length, then horizontal whitespace only.** The old pattern's trailing `\s*` could swallow following blank lines into the mask; the scanner stops at the closer line's terminator. Blank lines are spaces-after-masking either way and heading/link/tag scanning cannot match inside them, so no consumer-visible behavior rides on this; do not contort the scanner to reproduce the old trailing-blank-line absorption.

**5. No refusal mode for "ambiguous" notes.** With the grammar pinned, every note parses deterministically — there is no ambiguity class to refuse. Refusing e.g. "any note whose masking would differ between old and new grammar" would permanently wall off legitimate notes over a one-time transition. Rejected.

**6. Re-addressing is accepted, not versioned.** Ordinals shift only on notes containing a newly-recognised shape, and only because a fake heading inside code stops occupying an ordinal. Selectors are advertised per-response (the truncation outline); nothing durable stores ordinals. The `#140` re-addressing concern was about *not bundling* this break — shipping it as its own change with its own record is precisely the mitigation.

**7. Stale-index remediation: clear `content_hash` in a data migration.** Link/tag extraction runs only when `content_hash` changes; the masker change alters derived rows without touching bytes. An alembic data migration sets `notes_metadata.content_hash = NULL` (column is nullable; if not, use an impossible sentinel), so the next indexer pass re-extracts links and tags for every note. `embedded_content_hash` still equals the recomputed hash, so **no re-embedding** occurs and no Ollama/OpenAI cost is incurred. Downgrade is a no-op (the hashes regenerate).
- Alternative (rejected): truncate `note_links` to trigger the empty-table backfill — refreshes links but not `notes_metadata.tags`, and deletes graph data ahead of its recomputation instead of letting upserts replace it.

## Risks / Trade-offs

- [Ordinal shift surprises a concurrent agent holding a pre-deploy outline] → outlines are short-lived tool responses; the shifted ordinals only occur on notes where the old ordinal targeted a heading inside code, where a write was already destructive. Accepted.
- [Scanner diverges from regex on an untested shape] → the spec's scenario list (indent 1–3, longer closer, `~~~`, mixed chars, nested-looking, unterminated, cross-section, CRLF/CR terminators, backtick-in-info) becomes the test matrix; tests pin exact masked/unmasked byte spans. Adversarial Codex pass is mandatory (section-addressing surface).
- [Indexer pass after `content_hash` reset is heavy] → it is the same work as the existing first-deploy backfill over ~2,600 notes, minus embedding; runs inside the normal index loop.
- [A note legitimately containing `#` lines inside a now-masked fence loses those "sections"] → that is the fix, not a regression; the sections were never real.

## Migration Plan

1. Deploy ships the code and the data migration together (`make deploy` runs alembic before recreate).
2. First indexer pass after startup re-extracts links/tags for all notes (content_hash NULL ≠ recomputed hash). Verify via dashboard/logs that the pass completes and `note_links` settles.
3. Rollback: redeploy the previous image; the next indexer pass re-extracts under the old grammar the same way. No schema change, nothing to downgrade.
