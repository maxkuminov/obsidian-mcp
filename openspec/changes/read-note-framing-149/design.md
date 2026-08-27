## Context

`read_note` renders one string: an envelope (`# <title>`, `**Path:**`, optional `**Tags:**`/`**Frontmatter:**`), `\n---\n`, then content. Every envelope component is note-controlled; two reproduced forgeries (multiline YAML title, quoted frontmatter key) emit a fake separator, so no textual recovery procedure is safe — and the note-read spec (#140's delta) already refuses to document one, deferring the real fix to this change. The pinned MCP SDK (1.29.0, FastMCP) supports structured tool output: a typed return annotation generates an `outputSchema`, and results are returned as `structuredContent` plus a JSON-serialized text block (verified in `mcp/server/fastmcp/utilities/func_metadata.py` in the pinned wheel).

## Goals / Non-Goals

**Goals:**
- Make the note-controlled content and the server-controlled metadata structurally inseparable-by-forgery: fields, not a parseable frame.
- Give `edit_note` byte-exact inputs from `read_note` outputs in both modes (section body → section write; complete whole-note content → full replace).
- Preserve the caps, windowing, outline budgets, and "errors are in-band results, never raises" behavior exactly.

**Non-Goals:**
- Restructuring any other tool's response (`keyword_search`, `list_notes`, `read_file`, …): none advertises a write-back round trip, so forgery there has no destructive sink. (`read_file` returns raw bytes/base64 text without an envelope claim to recover from.)
- Changing section resolution, caps, or write semantics.
- SDK upgrade (2.0 stays deferred; 1.29 suffices).

## Decisions

**1. A pydantic result model, returned from the tool function.** `read_note` (server.py registration) returns `ReadNoteResult`; FastMCP derives `outputSchema` and emits `structuredContent` + JSON text fallback. JSON *is* the reversible escaping the issue's fallback alternative asked for — supplied by the protocol layer, not an invented scheme — and clients that ignore `structuredContent` still get an unambiguous, machine-parseable text block.
   - Alternative (rejected): keep the rendered string, escape each component, validate the composed envelope, refuse on forgery. Refusal-on-valid-notes is a permanent availability hole for notes that merely *contain* `---` shapes, the escaping must be specified and taught to agents, and the validation is a denylist over renderings — the class this issue documents as unclosable.

**2. Fields.** `path`, `title`, `tags` (list), `frontmatter` (object), `heading` (section reads only: the matched heading line, no terminator), `content` (whole-note: the body with the frontmatter block stripped, as today; section: the **body only** — exactly the span `edit_note(section=…)` replaces), truncation block (`truncated`, `offset`, `next_offset` nullable, `total_chars`), `outline` (truncated whole-note reads only: list of `{ordinal, depth, text, size, exceeds_cap, duplicate}`), `notice` (server-authored guidance prose; still subject to the registered-tool-names requirement), `error` (in-band failures; when set, content-bearing fields are absent).
   - Dropping the heading line from a section's `content` removes the read-returns-heading/write-takes-body asymmetry instead of restating it; the heading stays available as its own field.

**3. Errors stay in-band.** Missing note, invalid offset/limit, unknown selector → `error` set, no raise, matching today's contract and the `_tracked` logging path.

**4. Frontmatter must serialize.** YAML admits values JSON does not (dates, arbitrary tags). The model coerces non-JSON-native frontmatter values via `str()` at the leaf, so no valid note can make the response fail to serialize. Depth/size already bounded by note-size caps.

**5. Caps measured against `content` chars, unchanged.** The outline keeps its independent budget; the per-component-budget doctrine in `docs/architecture/vault-tools.md` ("if you add a third, give it one") is satisfied by pointing each field at its existing budget; `notice` is fixed prose plus bounded interpolations.

**6. CRLF residual carried over.** Section `content` is LF-normalized by the read path exactly as today; the round-trip byte-identity claim stays scoped to LF-bodied notes and stays declared in both docstrings.

## Risks / Trade-offs

- [Agents mid-flight parse the old envelope] → the JSON text block fails their `---` split loudly rather than corrupting silently; docstrings and tool description updated in the same deploy. Accepted break, tracked as BREAKING.
- [`_tracked` or usage logging assumes `str` results] → audit `_tracked` for `len(result)`-style accounting; log the serialized length. Task item.
- [A YAML frontmatter value crashes pydantic serialization] → decision 4's leaf coercion; test with dates, nested maps, and anchors.
- [claude.ai client rendering of structuredContent differs] → the end-to-end gate (this project's substitute for the user-representative pass) exercises read_note → edit_note round trips against the live server before archive.

## Migration Plan

1. Ship code; no schema/data migration. Deploy via `make deploy`.
2. End-to-end: read a section with forged-envelope fixtures from issue #149 in a scratch vault area, round-trip through `edit_note(section=…)`, confirm byte-exact bodies and no clobber; delete fixtures.
3. Rollback: redeploy previous image; responses revert to the envelope string.
