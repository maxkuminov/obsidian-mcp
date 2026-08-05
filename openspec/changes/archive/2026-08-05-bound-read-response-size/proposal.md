## Why

`read_note` has no cap on how much content it returns. A tool result is model input, so a large note becomes a large prompt — and the caller finds out only when its provider rejects the request.

This is not hypothetical. On a production vault, `read_note` on an auto-generated "key documents" note (bulk full-text extracts of an entire folder of source documents) returned a **3,466,876-character** tool result — roughly 870K tokens — in a single turn. The downstream provider rejected the follow-up request with *"Your input exceeds the context window of this model"*, surfacing to end users as a recurring chat failure with no indication that an MCP tool caused it. The server's own `usage_logs` showed the pattern rather than the incident: **11 `read_note` responses over 1 MB in 30 days**, largest 2,993,611 chars, plus 2 such `read_file` responses.

`MAX_FILE_READ_BYTES` (10 MB) looks like it covers this and does not. It bounds what the **server reads into memory**; nothing bounds what the server **returns into the caller's context**. Those are different limits with different correct values, and having the first made the absence of the second easy to miss. `read_note` never had even the first — it goes through `read_file()` in the vault service, not `read_bytes()`.

Truncating alone would be a poor fix: a 2.9 MB note at any sane window is dozens of round trips. These notes are structured — one `##` section per source document — so the server can hand back a map and let the caller fetch the one section it needs.

## What Changes

- Add a configurable **`MAX_READ_RESPONSE_CHARS`** (default 40,000 ≈ 10K tokens) capping what a single `read_note` or `read_file` call returns to the caller.
- **`read_note(path, section=None, offset=0, limit=None)`.** Over-cap responses return the first window plus a `[TRUNCATED]` notice stating the range shown, the total size, and the exact `offset` that continues the read.
- **Heading outline on truncation.** When a whole-note read is truncated, the response lists every section with its `#N` ordinal, depth, title, and size, flagging those that are themselves over the cap. The caller jumps straight to the section it wants instead of paging.
- **`#N` ordinal section selectors** (1-based, document order), accepted anywhere `section=` is — so `edit_note` gains them too. The existing `Parent/Child` form cannot disambiguate duplicate **sibling** headings, because they share every ancestor; generated notes repeat source filenames routinely. Ordinals are resolved only after exact-text matching fails, so a heading literally titled `#2` still wins.
- **`read_file(path, encoding, offset, limit)`** — text results windowed by the same cap. Base64 and image results are unchanged.
- Section addressing is refactored out of `replace_section` into shared helpers (`_resolve_section_index`, `_section_body_span`, `extract_section`, `outline_sections`) so `read_note` and `edit_note` resolve headings identically.

Non-goals: no change to `MAX_FILE_READ_BYTES` / `MAX_FILE_WRITE_BYTES`; no caps on search, list, or graph tools (already result-capped); no server-side summarization of over-cap content; no change to indexing, embedding, or chunking.

## Capabilities

### New Capabilities
- `note-read`: Bounded reading of markdown note content via `read_note` — the response-size cap, section addressing (text, path-style, and ordinal), offset/limit windowing, and the heading outline returned when a note exceeds the cap.

### Modified Capabilities
- `file-access`: `read_file` gains `offset` and `limit`, and its text results are bounded by `MAX_READ_RESPONSE_CHARS` in addition to the existing `MAX_FILE_READ_BYTES` on-disk cap. The two caps are independent and serve different purposes.
- `vault-write`: the `section=` selector accepted by `edit_note` gains the `#N` ordinal form, and the ambiguous-heading error names the ordinals that resolve it.

## Impact

- **Code**: `src/services/vault.py` (section helpers extracted + `extract_section` / `outline_sections` added), `src/mcp_server/tools.py` (`read_note_impl`, `read_file_impl`, windowing + outline rendering), `src/mcp_server/server.py` (tool signatures + docstrings), `src/config.py` (new setting).
- **Config**: `MAX_READ_RESPONSE_CHARS` (default 40,000).
- **Behavior change for existing clients**: `read_note` on a note larger than 40,000 chars now returns a truncated response instead of the whole note. This is the point of the change, but it is a visible contract change for any caller that assumed whole-note reads. The truncation notice is self-describing so an agent can continue without prior knowledge; `limit` can only lower the cap, and operators can raise `MAX_READ_RESPONSE_CHARS` if their clients genuinely want larger reads.
- **No impact** to: database schema, migrations, the indexer, embeddings, search ranking, OAuth, or the control panel.
