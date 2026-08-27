## 1. Result model

- [ ] 1.1 Define `ReadNoteResult` (pydantic) in `src/mcp_server/tools.py` (or a sibling module): `path`, `title`, `tags`, `frontmatter`, `heading`, `content`, `truncated`, `offset`, `next_offset`, `total_chars`, `outline` (list of `{ordinal, depth, text, size, exceeds_cap, duplicate}`), `notice`, `error` — optional fields absent when inapplicable; frontmatter leaves coerced to JSON-safe values (str fallback for dates/anchors/etc.).
- [ ] 1.2 Rework `read_note_impl` to build the model for every path: whole-note, section (heading field + body-only content via the shared helpers — confirm `extract_section`'s heading-line output is split, not re-derived), windowed reads, offset-at-end/past-end, invalid offset/limit, missing note, unknown selector (error lists available headings). Keep every cap and budget: content window via `_window`, outline via the existing bounded builder rendered into structured entries with the same elision/omission/degradation rules, `notice` prose naming only registered tools.

## 2. Registration and tracking

- [ ] 2.1 Update the `read_note` registration in `src/mcp_server/server.py` to return `ReadNoteResult` so SDK 1.29 emits `outputSchema` + `structuredContent` + JSON text; rewrite the `read_note` docstring (field round trip, CRLF residual, no textual extraction) and touch `edit_note`'s counterpart wording.
- [ ] 2.2 Audit `_tracked` (and `_log_usage`) for str-result assumptions; log the serialized response size for structured results.

## 3. Tests

- [ ] 3.1 Rewrite/extend `read_note` tests against fields: both issue #149 forgeries (multiline YAML title, quoted frontmatter key) yield uncorrupted fields and byte-exact section bodies; section body round-trips through `edit_note(section=…)`; complete whole-note `content` round-trips through full replace; truncation fields, outline bounding/elision/omission/degradation, duplicate flags; frontmatter with dates/nested maps serializes; error cases in-band, never raising; unstructured text block equals the JSON serialization of `structuredContent` (via the SDK's conversion or an MCP-level test).

## 4. Docs

- [ ] 4.1 Update `docs/architecture/vault-tools.md`: the read→write round trip is now field-based; record the forgery history and why the envelope string died; note the per-component budget mapping for the new fields.

## 5. Gates

- [ ] 5.1 Full suite green; `openspec validate read-note-framing-149 --strict` clean.
