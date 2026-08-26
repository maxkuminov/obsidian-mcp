# Tasks — section read/write parity (#140)

## 1. Pin the defect before changing anything

- [ ] 1.1 Add `tests/test_issue_140_section_round_trip.py` exercising the pure
      helpers in `src/services/vault.py` (`_scan_headings`,
      `_section_body_span`, `extract_section`, `replace_section`,
      `outline_sections`) — no DB, no vault, no async.
- [ ] 1.2 Write the two reported reproductions as failing tests first:
      the fenced-block duplication on
      `'# A\n```\n## Hidden\ntext\n```\n# B\nb\n'`, and blank-line
      accumulation over three round trips on `'# A\n\nbody\n\n# B\nb\n'`.
      Confirm both fail on the unmodified tree, then proceed.

## 2. The fix

- [ ] 2.1 In `src/services/vault.py`, narrow `_ATX_HEADING_RE`'s trailing
      whitespace run from `\s*` to `[^\S\r\n]*`. Change nothing else in the
      pattern — the separator class `[^\S\r\n]+` and the text capture
      `([^\r\n]+?)` stay exactly as they are.
- [ ] 2.2 Rewrite the comment block above the regex. It currently documents
      the opposite decision ("The TRAILING run stays the original `\s*`,
      unnarrowed and still allowed to cross line boundaries … narrowing it
      would change the bytes `edit_note(section=…)` writes"). It must now say
      why that constraint was lifted, what it cost, and what it bought — so a
      future reader does not "restore" the bug.
- [ ] 2.3 Update `_scan_headings`' and `_section_body_span`'s docstrings so
      `line_end` is described accurately (it is now genuinely the end of the
      heading line) and the body-start rule is stated once.

## 3. Prove the contract, not just the two bugs

- [ ] 3.1 Add the differential property test: for every ordinal `#N` in a
      note, `replace_section(text, "#N", extract_section(text, "#N") minus its
      first line)` SHALL return `text` unchanged.
- [ ] 3.2 Run that property over a corpus covering, at minimum: blank line(s)
      after a heading; a fenced block directly under a heading, including one
      containing `#`-prefixed lines and one using `~~~`; inline code on a
      heading line; a heading at EOF with and without a trailing newline; a
      heading line with trailing spaces, tabs, and a non-ASCII space; a note
      whose only content is headings; nested depths where the next heading is
      deeper (body must run past it) and shallower (body must stop).
- [ ] 3.3 Run the same property over CRLF and lone-CR notes, asserting the
      dialect's own terminators are preserved — the #128 terminator rule must
      survive this change intact.
- [ ] 3.4 Assert the **non**-regression that makes this safe: over the whole
      corpus, `_scan_headings` reports identical `depth`, trimmed `text`,
      `line_start`, and document order before and after the fix. Pin it by
      asserting against explicit expected values, not by comparing two regexes.

## 4. Guard what must not change

- [ ] 4.1 Re-run the existing section suites unchanged and keep them green:
      `tests/test_read_response_cap.py`,
      `tests/test_issue_5_replace_section_eof_heading.py`,
      `tests/test_issue_128_section_mode_frontmatter.py`,
      `tests/test_issue_128_edit_note_frontmatter.py`,
      `tests/test_issue_14_extract_tags_code_blocks.py`.
      If one fails, decide explicitly whether it pinned the bug (update it and
      say so in the commit) or caught a real regression (fix the code). Do not
      silently edit an assertion.
- [ ] 4.2 Verify section mode over a valid frontmatter block still resolves
      against the stripped body and reattaches the block byte-identically,
      and that a defective block still refuses by name.
- [ ] 4.3 Confirm `outline_sections`' `#N` ordinals are unchanged across the
      corpus; the `size` field may shift and the test SHALL assert the new
      values explicitly rather than being loosened.

## 5. The caller-visible contract

- [ ] 5.1 Update the `edit_note` docstring in `src/mcp_server/server.py` (the
      MCP-visible one) to state that in section mode `content` is the body
      beginning on the line after the heading line — the text a
      `read_note(section=…)` response carries below its heading line — and
      that a wanted blank separator belongs in `content`.
- [ ] 5.2 Make the same statement in `src/mcp_server/tools.py`'s `edit_note`
      docstring so the two layers do not diverge.
- [ ] 5.3 Add an end-to-end exercise against the running server: `read_note`
      a section, write the body back with `edit_note`, read the whole note and
      assert it is unchanged. Name in the report which tools were actually
      called.

## 6. Documentation

- [ ] 6.1 Update `docs/architecture/vault-tools.md`'s section-addressing
      material. The passage stating the trailing `\s*` must stay unnarrowed is
      now the *history* of a lifted constraint, not a live prohibition — it
      must record what changed, when, and why, and it must state the new
      body-start rule as the load-bearing invariant.
- [ ] 6.2 State the declared compat break there in the terms a future reader
      needs: a section write no longer preserves a blank line the caller did
      not send.
