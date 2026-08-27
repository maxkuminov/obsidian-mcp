## 1. Shared fence recognizer

- [ ] 1.1 In `src/services/links.py`, replace `_FENCE_RE` with a line scanner implementing the code-masking grammar: opener `0–3 spaces + ([`~])×3+ + info` (backtick info string may not contain a backtick; tilde info unrestricted), closer `0–3 spaces + same char × ≥opener + only U+0020/U+0009`, span = opener line start → closing line end **excluding the closing line's terminator**, unterminated column-zero opener → end of note, unterminated indented opener → not a fence but reported (position included); universal terminators (LF/CRLF/lone CR); a valid line-1 frontmatter block (shared partition helper) is skipped before scanning. Expose spans + unmatched-indented-opener report; `mask_code` masks spans with same-code-point-length spaces (internal terminators masked, closing terminator untouched). Keep inline-code masking unchanged. Rewrite the comment block: grammar, divergences (flat containers, column-zero headings, no indented code blocks, single-line inline), and the closing-terminator rule's reason.
- [ ] 1.2 Unit tests pinning exact masked spans and reports for: 1–3 space indented opener/closer, 4-space non-fence, longer closer, shorter-run and wrong-char non-closers, `~~~`, backtick-in-info non-opener, NBSP-suffixed non-closer, unterminated column-zero → EOF, unterminated indented → no mask + report, fence spanning a section boundary, heading immediately after the closer (LF, CRLF, lone CR), fence-shaped YAML scalar inside valid frontmatter not opening a block, defective-frontmatter note scanned raw, non-ASCII content (code-point length preserved), and same-length invariant on every case.

## 2. Consumers

- [ ] 2.1 `clean_for_embedding` (`src/services/embeddings.py`): delete `_FENCE_BACKTICK_RE`/`_FENCE_TILDE_RE`; remove the shared recognizer's spans instead; inline code still preserved. Tests: indented and longer-closed fences removed before chunking; prose retained.
- [ ] 2.2 Section addressing: `edit_note(section=…)` refuses, without writing and naming the unmatched indented opener's position, any note the recognizer reports one for; reads and outline unaffected. Tests: the list-item shape from the spec is refused on write and readable by section; matched indented fences write normally.
- [ ] 2.3 Section-addressing behavior tests through `extract_section`/`replace_section`/`outline_sections`: issue #150's reproductions now give `#1` = whole section A (block included), `#2` = `# B`; no selector reaches `# Hidden`; unterminated column-zero fence hides everything below; outline ordinals round-trip through the resolver.
- [ ] 2.4 Integration tests for the raw-text consumers: `extract_tags` and `move_note(rewrite_links=True)` on the frontmatter-scalar note (tag extracted, link rewritten) and on notes with newly-recognised fences (tag/link inside them ignored).

## 3. Re-derivation remediation

- [ ] 3.1 Schema: `notes_metadata.extraction_version SMALLINT NOT NULL SERVER DEFAULT 0` in `src/models/db.py` + alembic migration (additive, server default; downgrade drops it). `make test-schema` green; `alembic check` clean.
- [ ] 3.2 Indexer: `CURRENT_EXTRACTION_VERSION = 1`; re-derive when hash changed OR marker stale — re-extract links/tags; compare recognised fence spans (new recognizer vs frozen legacy regexes, copied verbatim and marked for removal) and clear `embedded_content_hash` only when they differ; stamp marker in the same transaction (retry-safety per index-integrity: a failed pass must not leave a stamped marker with unfinished work).
- [ ] 3.3 Integration tests (throwaway pgvector container, per `make test-schema` harness or existing test DB fixtures): stale-marker pass refreshes links+tags for all notes; embedding invalidation only for span-diff notes (assert embed-call count); external rename between migration and pass keeps identity via true `content_hash` (no cascade delete); tsvectors intact.

## 4. Docs

- [ ] 4.1 Update `docs/architecture/vault-tools.md` (grammar, closing-terminator rule, refusal, re-addressing break) and `docs/architecture/indexing-and-embeddings.md` (shared recognizer in the embed path, extraction_version mechanism, legacy comparator's removal condition); point both at the `code-masking` capability.

## 5. Gates

- [ ] 5.1 Full test suite green; `openspec validate fence-grammar-150 --strict` clean; `make db-check` clean post-deploy.
