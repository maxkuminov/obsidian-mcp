## 1. Config

- [x] 1.1 Add `max_read_response_chars: int = Field(40_000, ge=1_000)` to `Settings` in `src/config.py` (env: `MAX_READ_RESPONSE_CHARS`), with a comment distinguishing it from the byte caps above it.
- [x] 1.2 Document the variable in `.env.example`, `README.md`, `DEPLOYMENT.md`, and `CLAUDE.md`.

## 2. Vault service — shared section addressing

- [x] 2.1 Extract heading resolution from `replace_section` into `_resolve_section_index(headings, heading) -> (idx, error)`, preserving the existing text and `Parent/Child` behavior.
- [x] 2.2 Extract span computation into `_section_body_span(text, headings, idx) -> (body_start, body_end)`.
- [x] 2.3 Rewrite `replace_section` on top of both helpers with no behavior change (the end-of-file-heading newline handling in particular).
- [x] 2.4 Add `#N` ordinal support to `_resolve_section_index`, checked ahead of text matching and skipped for selectors containing `/`; report an out-of-range ordinal with the valid range. (Ordering corrected in task 8.1 after review.)
- [x] 2.5 Add `_format_ordinal_choices` and name the resolving ordinals in both ambiguity errors.
- [x] 2.6 Add `extract_section(text, heading) -> (section_text, error)` returning the heading line plus its body.
- [x] 2.7 Add `outline_sections(text) -> list[dict]` returning `depth`, `text`, `size`, and 1-based `ordinal` per section.

## 3. read_note

- [x] 3.1 Add `_window(body, offset, limit) -> (chunk, next_offset)` in `src/mcp_server/tools.py`.
- [x] 3.2 Add `_outline_text(content, cap)` rendering ordinal, depth, title, size, an over-cap flag, and a duplicate-title marker.
- [x] 3.3 Extend `read_note_impl` with `section`, `offset`, `limit`; validate `offset >= 0` and `limit >= 1`; clamp `limit` to the configured cap.
- [x] 3.4 Return content unchanged when it fits and `offset == 0`; otherwise window it and append a truncation notice with range, total, and next offset.
- [x] 3.5 Include the outline only on whole-note truncation; suggest search when the note has no headings; report an offset past the end rather than returning empty.

## 4. read_file

- [x] 4.1 Add `_capped_text(text, path, offset, cap)` and apply it to both the forced-`text` and auto-detected-text branches.
- [x] 4.2 Extend `read_file_impl` with `offset` / `limit` and the same validation; leave base64 and image results unwindowed.

## 5. Registration & docs

- [x] 5.1 Update the `read_note` and `read_file` `@mcp.tool()` signatures and docstrings in `src/mcp_server/server.py`, documenting the cap, the selector forms, and that `limit` can only lower the cap.
- [x] 5.2 Update `README.md`: tool table, a Response size limits section, and the behavior-change note for existing callers.
- [x] 5.3 Update `DEPLOYMENT.md` and `.env.example` with `MAX_READ_RESPONSE_CHARS`.
- [x] 5.4 Update `CLAUDE.md` key decisions.
- [x] 5.5 Add both tracked params (`section`, `offset`, `limit`) to the `_tracked` decorator calls so `usage_logs` records them.

## 6. Tests

- [x] 6.1 Cap: oversized note truncates; small note returned verbatim with no notice; `limit` lowers but cannot raise the cap.
- [x] 6.2 Windowing: reported offset continues with no gap or overlap; final window offers no continuation; offset past the end is reported; negative offset and zero limit rejected.
- [x] 6.3 Sections: reading one section avoids the rest of the note; an over-cap section pages within itself and preserves the selection; an unknown section lists the headings present.
- [x] 6.4 Outline: emitted with ordinals and sizes on whole-note truncation; over-cap sections flagged; omitted on a section read; headingless note still truncates and suggests search.
- [x] 6.5 Ordinals: duplicate siblings addressable by ordinal; ambiguity error names the ordinals; out-of-range reported; a bare `#2` selects by position while a heading titled `#2` stays reachable via the path form and its own ordinal.
- [x] 6.6 `read_file`: text capped and pages; forced `text` encoding capped; small text unchanged.
- [x] 6.7 Vault helpers: `extract_section` returns heading + body, stops at equal depth but not deeper, disambiguates by path; `outline_sections` sizes match `extract_section` output.

## 7. Verify

- [x] 7.1 Run the full test suite (293 passed, 5 skipped).
- [x] 7.2 Run `openspec validate bound-read-response-size --strict`.
- [x] 7.3 Confirm `replace_section` regressions still pass, including the end-of-file-heading case (`test_issue_5_replace_section_eof_heading.py`).
- [x] 7.4 Smoke against the running server: whole-note read of a 2.9 MB note returns ~43K chars with an outline; a section read returns ~1.4K; ordinals resolve duplicate siblings.

## 8. Pre-merge review round (Codex adversarial pass)

Three findings, all reproduced before accepting.

- [x] 8.1 **Ordinal shadowing.** `#N` resolved only after exact-text matching, so a
  heading literally titled `#2` made ordinal 2 unreachable — breaking the guarantee the
  outline advertises, with no fallback for duplicate siblings. Ordinals now resolve first
  and always select by position; a selector containing `/` never takes the ordinal branch,
  keeping the literal heading reachable via `Parent/#2` and via its own ordinal.
- [x] 8.2 **Unbounded outline.** The outline was appended without a budget, so a
  92,000-char note with 1,000 headings produced a **106,842-char** outline against a
  500-char cap — recreating the context blowup this change exists to prevent. Outline now
  carries its own budget: titles elided at 80 chars, listing stops at the cap, tail reports
  omitted count and full ordinal range, always at least one entry. Now 631 chars for the
  same input.
- [x] 8.3 **`offset == len(body)` reported as "past the end."** Exactly-at-the-end is a
  completed read, not a caller error. Now distinguished from `offset > len` in both
  `read_note` and `read_file`.
- [x] 8.4 Update specs, archived deltas, design rationale, README, CLAUDE.md, and the
  `read_note` docstring to match; invert the test that codified the wrong ordinal priority.
- [x] 8.5 Re-run: 300 passed, 5 skipped; `openspec validate --specs --strict` 9 passed.

Confirmed NOT bugs by the same pass: `replace_section` remains equivalent to `main` for all
existing selectors (the derived `next_heading_line_start` cannot differ — a terminating
heading's `line_start` is necessarily `< len(text)`, otherwise `body_end == len(text)`);
no gaps or overlaps between consecutive windows; content exactly at the cap is returned
untruncated; Python slicing is code-point aligned so it cannot corrupt the UTF-8/JSON
payload (it can split a grapheme cluster, which is presentation only).

## 9. Second review round

- [x] 9.1 **Outline still leaked past its budget.** The per-entry check reserved nothing for
  the omitted-sections summary, and the always-emit-first-entry escape was unchecked — so a
  cap could still be exceeded (1,000 duplicate headings at cap=500 gave 634 chars; 10,000
  headings at cap=1 gave 251). Budget now reserves the worst-case summary up front, and a
  final hard truncation makes `len(outline) <= cap` hold unconditionally, including for a
  degenerate cap reachable via `limit=1`.
- [x] 9.2 **The outline tests were vacuous.** They allowed `cap + 400` and `4 x cap`, which is
  why 9.1 passed review the first time. Replaced with an exact `<= cap` assertion plus a
  30-combination sweep over section counts (1…10,000) and caps (1…5,000), duplicate-title and
  multibyte-title cases, and an end-to-end bound stated as the design states it (2 x cap plus
  a fixed notice allowance).
- [x] 9.3 **Archived `proposal.md` / `tasks.md` still described the pre-review behavior**
  (outline "lists every section", text-first ordinals) while the spec deltas and canonical
  specs described the corrected behavior. Reconciled.
- [x] 9.4 Re-verified both round-1 reproductions and swept 63 outline combinations: 0 violations.

## 10. Third review round

The cap itself was confirmed unbreakable this round — the reviewer could not produce
`len(_outline_text(...)) > cap` across caps 0/1/2, one section to 20,000, duplicate titles,
79/80/81-char boundary titles, Cyrillic/CJK/emoji/joined-grapheme titles, depth-6 headings,
and wide size formatting; and confirmed the hard slice cannot corrupt UTF-8 or the JSON
payload (Python slices code points; escaping happens after).

- [x] 10.1 **Over-reservation made valid outlines pathologically short.** The worst-case
  summary was reserved unconditionally, so `_outline_text("# A\nbody\n", 160)` returned a
  157-char "1 more section not shown" summary instead of the 22-char entry that fit. Added a
  fast path: if the complete listing fits, emit it and reserve nothing — a summary is only
  owed when something is actually omitted.
- [x] 10.2 **The sweep constrained length but not usefulness.** It would have accepted an
  implementation returning one arbitrary character. Added tests that a fitting listing is
  emitted whole with no summary, that every section and ordinal appears when the budget
  allows, and that a truncated outline still spends its budget on entries rather than
  degenerating to one entry plus a summary.
- [x] 10.3 **Spec said "At least one entry SHALL always be emitted"** while the
  implementation truncates to a marker at a degenerate cap. Reconciled honestly: at least one
  entry when one fits; otherwise a truncated marker, because the cap is the binding
  constraint and there is no output worth exceeding it for.
- [x] 10.4 **Contract-level inaccuracy in the docs.** `.env.example`, README and CLAUDE.md
  described the cap as bounding "what read_note returns", but it is a per-component budget:
  content window and outline are each bounded, so a truncated response can carry both and the
  worst case is ~2x cap plus fixed notice prose. Corrected in all three, matching what
  design.md already said and what the end-to-end test asserts.
- [x] 10.5 Removed a paragraph duplicated verbatim in CLAUDE.md.
