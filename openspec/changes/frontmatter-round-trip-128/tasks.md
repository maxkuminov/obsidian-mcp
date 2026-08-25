## 1. Diagnosing parser and pinned partition (D3)

- [ ] 1.1 `parse_frontmatter_diagnose(raw)` beside `parse_frontmatter` in `src/services/vault.py` returning `(fm, body, defect)` with an unambiguous has-valid-block signal. Predicates pinned to the read parser exactly: opening = first line exactly `---` (trailing CR ok); closing = `^---[ \t]*\r?$` (trailing spaces/tabs accepted). Partition: valid mapping; whitespace-only fenced YAML → valid `({}, body)`; `unclosed_fence` (bare unterminated `---` at EOF included); `("yaml_error", msg)`; `not_a_mapping` (list, scalar, or non-whitespace YAML loading to `None` — `null`, `~`, comment-only); absent (`----`, `--- x`, leading content, no fence) → `({}, raw)`. Body span computed by the parser, never `raw[:-len(body)]`. `parse_frontmatter` gains exactly ONE behavior change, shared with the diagnosing sibling: whitespace-only fenced YAML is recognized as a valid empty mapping `({}, body)` for every consumer (read, index, diagnose) — one partition, or the read-body round trip duplicates the block. Every other `parse_frontmatter` branch untouched; audit existing callers/tests for the empty-block case.
- [ ] 1.2 Unit tests: every partition branch; LF/CRLF; trailing-space and trailing-tab closers; `----`; `--- x`; `null`/`~`/comment-only; bare `---` at EOF; `---\n---\n` with empty and non-empty bodies; a metadata-only file with and without a trailing newline.

## 2. edit_note full-replace preservation + replace_frontmatter (D1, D2, D4)

- [ ] 2.1 Add `replace_frontmatter: bool = False` to `edit_note` in `src/mcp_server/tools.py` AND to the registered wrapper in `src/mcp_server/server.py`. Combined with `append`/`find`/`section` → the existing multi-mode error, no write.
- [ ] 2.1b Add `replace_frontmatter` to `edit_note`'s `_tracked` parameter allow-list so usage_logs records the destructive-intent flag; test that a flagged call's log row carries it.
- [ ] 2.2 Default full-replace on a note with a valid block: compose `<raw block slice><separator><content>` — separator is one `\n` only when the block slice does not end in a newline and `content` is non-empty. `content` is never classified. `replace_frontmatter=True` or no-valid-block → wholesale exact `content`.
- [ ] 2.3 Ordering: compose → size cap on composed result → `expected=` raw-bytes comparison → publish. `dry_run` diffs the composed result.
- [ ] 2.4 Docstrings at BOTH layers (server.py wrappers are what MCP clients see; the current edit_note text "overwrites the entire file" becomes false): edit_note states the preservation default, the flag, the escape hatches, AND that the round-trip guarantee covers only a complete unwindowed whole-note read — section responses belong to section mode, truncated reads must be completed first, and a `read_note(section=...)` result INCLUDES the heading line while `edit_note(section=...)` takes the body only (strip the heading or it duplicates); read_note states the same scoping. Test asserting the registered descriptions contain the key contract phrases.
- [ ] 2.5 Tests: body-only replace preserves block byte-identically; thematic-break body round-trips; body beginning with a complete mapping-shaped fenced block round-trips (the round-3 case); end-to-end read-content→default-edit round trip for a whitespace-only-empty-block note — byte-identical for LF; for CRLF assert the block (CRLF fences) survives byte-identically while the body arrives LF-normalized per the read path's pre-existing universal-newline translation (declared in the spec); metadata-only note without trailing newline gains exactly one separator (and none when content is empty); replace_frontmatter=True replaces wholesale; flag+mode conflict errors; absent/malformed existing block → wholesale; dry_run parity; composed-over-cap refused, exact-cap succeeds; concurrent frontmatter-only change with `expected=` conflicts; append/find modes regression (find still operates on the raw file).

## 3. Section mode: body-resolution or refusal (D5)

- [ ] 3.1 Valid block: resolution, replacement, and not-found/ambiguity listings over the frontmatter-stripped body; write reattaches the raw block byte-identically. Defective block (unclosed/yaml_error/not_a_mapping): refuse naming the defect and the `replace_frontmatter=True` repair. Absent: raw, as today.
- [ ] 3.2 Tests: YAML `#`-comment not selectable/countable; read/write ordinal parity on a note with fm comments; section edit never alters the block (trailing-whitespace closer included); comment-only/unclosed/yaml-error blocks refuse with no write; fence-line deletion is impossible (assert file bytes outside the section untouched).

## 4. set_frontmatter refusal + block removal (D6)

- [ ] 4.1 Diagnose first — BEFORE the empty-updates no-op check, so malformed input refuses even with `updates={}, remove=[]`; refuse unclosed/yaml_error/not_a_mapping naming the defect (with parser message) and the `edit_note(replace_frontmatter=True)` repair; `remove=` refuses identically. Empty fenced block = valid empty mapping.
- [ ] 4.2 Removing the last key removes the block entirely (normative; current serializer already does — add the test). Track *effective* mutations with type-sensitive structural equality (`True != 1`, `False != 0`, nested values compared by type and value — plain `==` conflates bool/int): a call that changed no key is a byte-identical no-op — an existing valid empty block is NOT dropped by a remove that removed nothing (test with a mapping-shaped fenced body prefix below the empty block).
- [ ] 4.3 Docstrings updated at both layers.
- [ ] 4.4 Tests: three defect classes refused with nothing written (incl. `null`/`~`/comment-only); empty-block update works (empty and non-empty bodies); last-key removal leaves exactly the prior body; absent-fence prepend unchanged.

## 5. Gates

- [ ] 5.1 `.venv/bin/python -m pytest -q tests/` green.
- [ ] 5.2 `openspec validate frontmatter-round-trip-128 --strict` clean.
- [ ] 5.3 CLAUDE.md: update the write-tools section (preservation default + flag, section-on-body-or-refuse, set_frontmatter refusal), brief, matching existing style.
