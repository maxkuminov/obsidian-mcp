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

**1. A pydantic result model, returned from the tool function.** `read_note` (server.py registration) returns `ReadNoteResult`; FastMCP derives `outputSchema` and emits `structuredContent` + JSON text fallback. JSON *is* the reversible escaping the issue's fallback alternative asked for — supplied by the protocol layer, not an invented scheme — and clients that ignore `structuredContent` still get an unambiguous, machine-parseable text block. Three SDK realities shape the implementation (verified in the pinned wheel; Codex audit): (a) the text block is rendered from the returned object and `structuredContent` from the validated dump — so every value must be made JSON-safe *when the model is built*, never inside a serializer, or the two renderings diverge; a parity test pins them equal. (b) Optional pydantic fields serialize as `null` by default; the model carries a serializer that omits absent fields from both renderings. (c) A returned result with `error` set is an MCP *success* (`isError=false`) — in-band errors are an application convention, so the live-client gate must confirm the agent-visible behavior, and `_tracked`'s admission-refusal path (which today returns a bare string before the tool body runs) must return a typed error result for this tool, or FastMCP output validation turns the refusal into a protocol error.
   - Alternative (rejected): keep the rendered string, escape each component, validate the composed envelope, refuse on forgery. Refusal-on-valid-notes is a permanent availability hole for notes that merely *contain* `---` shapes, the escaping must be specified and taught to agents, and the validation is a denylist over renderings — the class this issue documents as unclosable.

**2. Fields (final inventory).** `path` (always exact; bounded by admission-time path validation), `title`, `tags` (list), `frontmatter_yaml` (raw block text, authoritative, lossless-whenever-present), `frontmatter` (best-effort JSON view, decision 4), `heading` (section reads only: the matched heading line, no terminator), `content` (whole-note: the body with the frontmatter block stripped, as today; section: the **body only** — exactly the span `edit_note(section=…)` replaces), truncation block (`truncated`, `offset`, `next_offset`, `total_chars`), `outline` (truncated whole-note reads only: an **object** — `entries` [{ordinal, depth, text, size, exceeds_cap, duplicate}] plus omission count, full ordinal range, and a truncation-marker field; decision 6), `metadata_omissions` (server-controlled record of every dropped/elided metadata field with reason — omissions are never signalled by in-band markers, which note content could mimic), `notice` (server-authored guidance prose; still subject to the registered-tool-names requirement), `error` (in-band failures; when set, content-bearing fields are absent). Metadata-budget overflow drops fields in deterministic priority — JSON view, then `frontmatter_yaml`, then `tags`, then `heading` elision, then `title` elision — each recorded in `metadata_omissions`.
   - Dropping the heading line from a section's `content` removes the read-returns-heading/write-takes-body asymmetry instead of restating it; the heading stays available as its own field.

**3. Errors stay in-band.** Missing note, invalid offset/limit, unknown selector → `error` set, no raise, matching today's contract and the `_tracked` logging path.

**4. Frontmatter: raw block authoritative, JSON view best-effort.** Leaf `str()` coercion is neither total nor lossless: recursive aliases (`x: &X [*X]`) build cyclic objects that crash serialization; YAML's non-string keys collide onto JSON keys (`1:` vs `"1":`); dates round-trip as quoted strings. So the response carries `frontmatter_yaml` — the block's stored text, lossless and budgeted — plus a `frontmatter` JSON view built defensively (depth/size-bounded walk, cycle-safe, dates → ISO strings) and **omitted entirely** (with the omission stated) on construction failure or key collision. Mutation flows through `set_frontmatter` or the raw block, never a round trip of the lossy view; docstrings say so.

**5. Every field gets a budget, and the worst case is restated.** `content` keeps the response-size cap; the outline keeps its independent budget; the metadata fields (`title`, `tags`, `frontmatter_yaml` + view, `heading`) share a new metadata budget (≤ cap) — on breach fields are dropped whole (or, for `heading`/`title`, elided) in decision 2's priority order and recorded in `metadata_omissions`, never replaced by an in-band marker, because `read_note` goes through `read_file()` with no note-byte limit and a multi-megabyte frontmatter must not ride in beside a one-character body. `path` gets an explicit admission-time length limit (1,024 chars, the `file_path` column width) so its exact value is a fixed allocation. The architecture doc's worst case is restated for the new shape: (content cap + outline budget + metadata budget + fixed prose) × 2 for the structured/text duplication × up to 6 for JSON escaping of note-controlled text. The old "≈2×cap" figure is retired with the envelope.

**6. Outline degraded states are data.** The outline becomes an object: `entries` plus omission count, full ordinal range, and an explicit truncation-marker field, because the requirement's degraded states (omitted sections, cap-too-small) were not representable as a bare entry list. `size`/exceeds-cap keep the existing heading-plus-body measure — conservative in the safe direction for body-only reads.

**7. Sequencing: this change archives after `fence-grammar-150`.** Both changes modify the vault-write section-mode requirement; this change's vault-write delta is authored against the text as it stands after #150 archives, and must be re-verified at archive time.

**8. CRLF residual carried over.** Section `content` is LF-normalized by the read path exactly as today; the round-trip byte-identity claim stays scoped to LF-bodied notes and stays declared in both docstrings.

## Risks / Trade-offs

- [Agents mid-flight parse the old envelope] → the JSON text block fails their `---` split loudly rather than corrupting silently; docstrings and tool description updated in the same deploy. Accepted break, tracked as BREAKING.
- [`_tracked` or usage logging assumes `str` results] → audit `_tracked` for `len(result)`-style accounting; log the serialized length. Task item.
- [A YAML frontmatter value crashes pydantic serialization] → decision 4's leaf coercion; test with dates, nested maps, and anchors.
- [claude.ai client rendering of structuredContent differs] → the end-to-end gate (this project's substitute for the user-representative pass) exercises read_note → edit_note round trips against the live server before archive.

## Migration Plan

1. Ship code; no schema/data migration. Deploy via `make deploy`.
2. End-to-end: read a section with forged-envelope fixtures from issue #149 in a scratch vault area, round-trip through `edit_note(section=…)`, confirm byte-exact bodies and no clobber; delete fixtures.
3. Rollback: redeploy previous image; responses revert to the envelope string.
