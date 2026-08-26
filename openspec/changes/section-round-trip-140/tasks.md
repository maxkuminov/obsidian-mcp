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
- [ ] 2.1b In `replace_section`, make **both** separator insertions conditional
      on a non-empty `new_body`. Unconditional insertion means an empty section
      (`# A\n# B\nb\n`) gains a blank line and an unterminated EOF heading
      (`# A`) gains a newline on every round trip — the regex fix alone does
      not make the headline property true. Do not touch the non-empty
      behaviour: `tests/test_issue_5_replace_section_eof_heading.py` pins it
      and must stay green unmodified.
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
      first line)` SHALL return `text` unchanged. Note the level: these are the
      *pure helpers*, where `extract_section` really does return raw section
      text. The tool-level property is different and is task 5.3 — do not
      conflate them.
- [ ] 3.2 Run that property over a corpus covering, at minimum: blank line(s)
      after a heading; a fenced block directly under a heading, including one
      containing `#`-prefixed lines and one using `~~~`; inline code on a
      heading line; a heading at EOF with and without a trailing newline; a
      heading line with trailing spaces, tabs, and a non-ASCII space; a note
      whose only content is headings; **empty sections** (two consecutive
      headings, and a heading at EOF with no body) in every dialect; nested
      depths where the next heading is deeper (body must run past it) and
      shallower (body must stop).
- [ ] 3.3 Run the same property over CRLF and lone-CR notes at the **helper**
      level, asserting the dialect's own terminators are preserved — the #128
      terminator rule must survive this change intact.
- [ ] 3.3b Pin the declared newline residual at the **tool** level as a test,
      not as prose: reading a CRLF note's section (which arrives LF-normalised)
      and writing it back yields `# A\r\nold\n# B\r\nkeep\r\n`. Assert that
      exact string, so the residual is visible and cannot drift silently. Add a
      **mixed**-ending case too — the normalisation is per-terminator, not
      per-note: `# A\r\none\r\ntwo\nthree\r# B\rkeep\r` yields
      `# A\r\none\ntwo\nthree\n# B\rkeep\r`.
- [ ] 3.3c Pin that the unmasked-fence shapes (an indented fence, and one
      closed by a longer run) produce **byte-identical** output before and
      after this change. This is the evidence for the "not this change's bug"
      claim; assert the explicit expected bytes.
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
- [ ] 4.3 Confirm `outline_sections` is **entirely** unchanged across the
      corpus — ordinals and `size` alike. `size` is `body_end - line_start` and
      this change moves neither endpoint, so any observed shift means the fix
      went further than intended. Assert explicit values.

## 5. The caller-visible contract

- [ ] 5.1 Update the `edit_note` docstring in `src/mcp_server/server.py` (the
      MCP-visible one) to state, in this order: (a) `content` is the body
      beginning on the line after the heading line; (b) a section write
      replaces the **whole body**, so omitted content — a fenced block included
      — is deleted; (c) a wanted blank separator belongs in `content`; (d)
      `read_note(section=…)` is the matching read, which carries the heading
      line and the body; (e) byte-identity holds for LF-bodied notes, and every
      non-LF terminator in the selected body comes back as LF.
      **Do not write a parsing recipe** — no "split on the separator", no "drop
      the first line". Such a procedure is forgeable by note content; the
      reason belongs in `vault-tools.md`, not in a docstring a future author
      will "helpfully" complete.
- [ ] 5.2 Make the same statements in `src/mcp_server/tools.py`'s `edit_note`
      docstring so the two layers do not diverge. Leave `read_note`'s
      docstrings alone beyond consistency — its response shape is not changing
      here, and the framing work is a separate filed change.
- [ ] 5.3 Add an end-to-end exercise against the running server, written so it
      does not depend on parsing a response: create a note with known bytes,
      call `read_note(path, section=…)` and assert the response contains the
      expected section, then call `edit_note(path, content=<the known body>,
      section=…)` and assert via `read_note(path)` that the whole note is
      byte-unchanged. Cover a section whose body starts with a blank line, one
      starting with a fenced block, and an empty section. Name in the report
      which tools were actually called.

## 6. Documentation

- [ ] 6.1 Update `docs/architecture/vault-tools.md`'s section-addressing
      material. The passage stating the trailing `\s*` must stay unnarrowed is
      now the *history* of a lifted constraint, not a live prohibition — it
      must record what changed, when, and why, and it must state the new
      body-start rule as the load-bearing invariant.
- [ ] 6.2 State the declared compat break there in the terms a future reader
      needs, and state the *whole* of it — not just the blank line. A section
      write replaces the entire body, so content the caller omits is deleted.
      Include the concrete fenced-block example: on
      `# A\n```\nimportant\n```\nold\n`,
      `edit_note(section="A", content="new")` now yields `# A\nnew\n`. A
      reader who learns only about the blank line will not predict that.
- [ ] 6.3 Record why this change documents **no** extraction procedure, with
      both reproductions (the multiline title and the quoted
      `"safe\n---\nforged"` key) and the reason sanitising fields does not fix
      it. This is the "do not undo it" note for the next author who notices the
      docstring stops short of telling the agent how to get the body, and
      helpfully adds a split rule. Link **#149**.

## 7. Residuals and follow-ups

- [ ] 7.1 Do NOT widen `_FENCE_RE` in this change. Confirm by test (3.3c) that
      the unmasked-fence shapes are byte-identical before and after, then link **#150**
      from `vault-tools.md`, which describes the gap (indented openers, longer
      closers), its destructive symptom, and why fixing it re-addresses `#N`
      ordinals and needs its own proposal.
- [ ] 7.2 Record the newline residual in `docs/architecture/vault-tools.md`
      beside the whole-note one #128 already declared, with the measured
      before/after string.
