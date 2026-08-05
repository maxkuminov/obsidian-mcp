## 1. Config

- [x] 1.1 Add `max_read_response_chars: int = Field(40_000, ge=1_000)` to `Settings` in `src/config.py` (env: `MAX_READ_RESPONSE_CHARS`), with a comment distinguishing it from the byte caps above it.
- [x] 1.2 Document the variable in `.env.example`, `README.md`, `DEPLOYMENT.md`, and `CLAUDE.md`.

## 2. Vault service — shared section addressing

- [x] 2.1 Extract heading resolution from `replace_section` into `_resolve_section_index(headings, heading) -> (idx, error)`, preserving the existing text and `Parent/Child` behavior.
- [x] 2.2 Extract span computation into `_section_body_span(text, headings, idx) -> (body_start, body_end)`.
- [x] 2.3 Rewrite `replace_section` on top of both helpers with no behavior change (the end-of-file-heading newline handling in particular).
- [x] 2.4 Add `#N` ordinal support to `_resolve_section_index`, attempted only after exact-text matching fails; report an out-of-range ordinal with the valid range.
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
- [x] 6.5 Ordinals: duplicate siblings addressable by ordinal; ambiguity error names the ordinals; out-of-range reported; a heading literally titled `#2` wins over the ordinal.
- [x] 6.6 `read_file`: text capped and pages; forced `text` encoding capped; small text unchanged.
- [x] 6.7 Vault helpers: `extract_section` returns heading + body, stops at equal depth but not deeper, disambiguates by path; `outline_sections` sizes match `extract_section` output.

## 7. Verify

- [x] 7.1 Run the full test suite (293 passed, 5 skipped).
- [x] 7.2 Run `openspec validate bound-read-response-size --strict`.
- [x] 7.3 Confirm `replace_section` regressions still pass, including the end-of-file-heading case (`test_issue_5_replace_section_eof_heading.py`).
- [x] 7.4 Smoke against the running server: whole-note read of a 2.9 MB note returns ~43K chars with an outline; a section read returns ~1.4K; ordinals resolve duplicate siblings.
