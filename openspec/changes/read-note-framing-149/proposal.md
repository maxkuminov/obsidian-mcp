## Why

Every `read_note` response is a single string: a `# <title>` / `**Path:**` / `**Tags:**` / `**Frontmatter:**` envelope, a `\n---\n` separator, then the selected content — and every envelope component is note-controlled and interpolated unsanitized. A note can forge the separator (two confirmed reproductions on different fields: a multiline YAML title, then a quoted frontmatter key), so any textual procedure for recovering the content from the response is forgeable, and the recovered "section body" can be a crafted string that, written back through `edit_note(section=…)`, clobbers the section (issue #149). Sanitizing fields one by one demonstrably does not close the class — two audit rounds each found a new field — and per-component invariants do not compose into an envelope invariant. The consumer is an agent; a wrong extraction becomes a destructive write with no human in the loop.

## What Changes

- **BREAKING:** `read_note` returns a **structured result** instead of a rendered string. The MCP tool declares an output schema and returns fields — no envelope to parse, so nothing note-controlled can forge a frame:
  - `path`, `title`, `tags`, `frontmatter` — metadata as data, not markdown;
  - whole-note reads: `content` — exactly the body `edit_note(path, content)` full-replace accepts (frontmatter block stripped, as today);
  - section reads: `heading` (the heading line) and `content` — the **body only**, exactly the span `edit_note(section=…)` replaces, ending the "read returns heading + body, write takes body only" asymmetry an agent had to compensate for;
  - truncation as data: `truncated`, `offset`, `next_offset`, `total_chars`, and — whole-note reads only — the section `outline` as an object (`entries` of `{ordinal, depth, text, size, exceeds_cap, duplicate}` plus omission count, ordinal range, and truncation marker), a `metadata_omissions` record for budget-dropped metadata fields, and a human-readable `notice` string carrying the existing guidance prose;
  - frontmatter twice: `frontmatter_yaml` (raw block text, authoritative) and a defensive best-effort JSON view.
- Error results (missing note, bad offset/limit, unresolvable selector) remain **in-band tool results**, not protocol errors, carried in the same structure (`error` field), preserving today's "return an error message, never raise" contract.
- The response-size cap (`MAX_READ_RESPONSE_CHARS`) still applies, measured against the `content` field; every other field keeps its existing independent budget (the outline's cap survives unchanged).
- Docstrings for `read_note` and `edit_note` are rewritten around the new round-trip: `content` from a section read is byte-exact input for `edit_note(section=…)`; `content` from a complete whole-note read is byte-exact input for full-replace.
- The rejected smaller alternative (keep the string; reversibly escape every component and validate the composed envelope) is recorded in design.md.

## Capabilities

### New Capabilities

(none)

### Modified Capabilities
- `note-read`: the `read_note` tool requirement and its response contract change from "a rendered string beginning with an envelope" to "a structured result whose fields separate note-controlled content from metadata"; the section-selection, size-cap, and outline requirements are restated against fields instead of rendered text.
- `vault-write`: the section-mode round-trip scenario ("stripped of its heading line…") and the section-mode docstring requirement are restated against the `content` field — left as-is they would instruct a caller to delete the body's first line. Authored against the post-`fence-grammar-150` text; this change archives second.

## Impact

- `src/mcp_server/tools.py` — `read_note_impl` returns a typed structure; `_outline_text` gains a structured sibling (or is replaced).
- `src/mcp_server/server.py` — `read_note` registration returns the structured type so MCP SDK 1.29 emits `structuredContent` + an `outputSchema`; the wire-visible text block becomes the SDK's JSON serialization (reversible by construction — this is the "reversible escaping" the issue demands, supplied by JSON rather than an invented scheme).
- Consumers: agents that parsed the old envelope must read fields instead; the JSON text fallback keeps non-structured clients functional.
- Known envelope consumers, each with a migration task: `tests/test_read_response_cap.py`; `tests/test_issue_128_edit_note_frontmatter.py` (splits on `\n---\n`); `tests/test_issue_140_section_round_trip.py`; `tests/test_issue_89_tool_names_in_copy.py` (guidance producers); the direct `read_note_impl` string consumers `tests/test_issue_128_section_mode_frontmatter.py`, `tests/test_issue_77_usage_attribution.py`, `tests/test_issue_88_root_confirmed_before_publish.py`, `tests/test_symlink_mutation_guard.py` (inventory to be re-verified by grep at implementation time); the `read_note`/`edit_note` docstrings in `server.py` and `tools.py`; `docs/architecture/vault-tools.md`'s worst-case-response accounting; the note-read registered-tool-guidance requirement's two producers (now the `notice` field).
- `docs/architecture/vault-tools.md` — the read→write round-trip contract is updated.
- Other read tools (`keyword_search`, `list_notes`, …) keep their string responses: none of them advertises a write-back round trip, so the forgery there has no destructive sink. Recorded as a non-goal.
