## 1. Masker rewrite

- [ ] 1.1 Replace `_FENCE_RE`-based fence masking in `src/services/links.py` with a line-scanner implementing the code-masking grammar: opener `^ {0,3}([`~])\1{2,}<info>` (backtick info string may not contain a backtick), closer `^ {0,3}<same char>{>=opener}[ \t]*$`, unterminated → mask to end of note, universal terminators (LF/CRLF/lone CR), same-length space substitution. Keep inline-code masking unchanged. Update the comment block to state the grammar and its documented divergences (column-zero headings, no indented code blocks, single-line inline approximation).
- [ ] 1.2 Unit tests pinning exact masked spans for: 1–3 space indented opener/closer, 4-space non-fence, longer closer, shorter-run and wrong-char non-closers, `~~~` fences, backtick-in-info non-opener (```` ```code``` ````), tilde info string with backticks allowed, unterminated fence to EOF, fence spanning a section boundary, CRLF and lone-CR notes, and byte-length preservation on every case.

## 2. Section addressing behavior

- [ ] 2.1 Tests through `extract_section`/`replace_section`/`outline_sections` for the vault-write delta scenarios: issue #150's two reproductions now resolve `#1` = whole section A (block included), `#2` = `# B`; no selector reaches `# Hidden`; unterminated-fence note exposes no headings below the fence; outline ordinals round-trip through the resolver.

## 3. Re-extraction remediation

- [ ] 3.1 Alembic data migration clearing `notes_metadata.content_hash` (NULL if nullable, else sentinel) — no schema ops; downgrade is a no-op; `alembic check` stays clean; `make test-schema` passes.
- [ ] 3.2 Verify by inspection (and test if cheap) that a NULL/sentinel `content_hash` routes the indexer through link+tag re-extraction without re-embedding when `embedded_content_hash` matches the recomputed hash.

## 4. Docs

- [ ] 4.1 Update `docs/architecture/vault-tools.md` (and the `#140`-era masker commentary it carries) with the pinned grammar, the declared re-addressing break, and the remediation mechanism; note the `code-masking` capability as the source of truth.

## 5. Gates

- [ ] 5.1 Full test suite green; `openspec validate fence-grammar-150 --strict` clean.
