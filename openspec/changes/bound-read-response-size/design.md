## Context

Two size limits already exist (`MAX_FILE_READ_BYTES`, `MAX_FILE_WRITE_BYTES`) and both protect the **server**: they stop a pathological file from being read into or written out of process memory. Nothing protected the **caller**. Because an MCP tool result is fed back into the model's next request, the server is effectively writing into a context window it cannot see and whose size it does not know.

The failure that motivated this is instructive about where the cost lands. The server did its job — it read a 2.9 MB note quickly and returned it. The rejection happened one hop downstream at the inference provider, wrapped as an HTTP 502 `provider_unavailable`, so it read like an upstream outage rather than a payload the server had produced.

## Goals / Non-Goals

**Goals**
- No single read can, by itself, exhaust a caller's context.
- A caller that hits the cap can make progress without out-of-band knowledge — the response says what to call next.
- Reaching one section of a large structured note costs one call, not N.
- `read_note` and `edit_note` agree on what `section="X"` means.

**Non-Goals**
- Summarizing or compressing over-cap content server-side. Lossy, surprising, and expensive.
- Guessing the caller's real context window. It is not knowable from here; a conservative fixed default that operators can raise is honest and predictable.
- Making the vault's oversized notes smaller. That belongs to whatever generates them.

## Decisions

### Cap in characters, not tokens

Tokenization is model-specific and would drag a tokenizer dependency into a server that is deliberately model-agnostic. Characters are exact, cheap, and stable. 40,000 chars ≈ 10K tokens across common tokenizers — small enough to be safe alongside a system prompt, conversation history, and other tool results; large enough that ordinary notes are unaffected. In the vault that triggered this, notes over the cap are a rounding error by count and dominate by bytes.

### `limit` may lower the cap but never raise it

`limit` is a caller convenience for taking less. If it could raise the ceiling, the protection would be advisory — and the caller asking for more is exactly the one that cannot judge the consequence. Raising the ceiling is an operator decision via `MAX_READ_RESPONSE_CHARS`, where it is set once by someone who knows the deployment.

### Truncation returns an outline, not just a prefix

A prefix plus "call again with offset=40000" is technically sufficient and practically useless at 2.9 MB — 72 round trips to find one document. These notes carry one `##` section per source document, so the server already knows the map. Returning it converts a linear scan into a direct lookup, and the sizes in the outline let the caller predict which sections will still need paging.

The outline is emitted only for whole-note reads. A truncated **section** read does not re-list the note's other sections: the caller has already chosen, and repeating the map on every window would waste the context this change exists to protect.

### `#N` ordinals, resolved after text matching

The existing selector grammar is heading text, or `Parent/Child` to disambiguate. That covers headings distinguished by ancestry and cannot express *"the second `## Report.xlsx` under this same parent"* — duplicate siblings share every ancestor, so no chain separates them. Generated notes hit this constantly; the note behind this change contains its entire document set twice, making roughly half its sections unreachable by name.

Resolution order is deliberate: exact text match first, ordinal only if that finds nothing. A note with a heading literally titled `#2` keeps working, and no existing selector changes meaning. The cost is that such a note cannot be addressed by ordinal 2 — an acceptable trade against silently breaking a real heading.

Ambiguity remains an **error** that names the resolving ordinals rather than a silent pick of the first match. Selecting arbitrarily is how an agent edits the wrong section and reports success.

### Shared resolver for read and write

`replace_section` already contained heading resolution and span computation inline. Rather than copy that logic into the read path — where it would drift and where `read_note` and `edit_note` could disagree about which section `"X"` names — it is extracted into `_resolve_section_index` and `_section_body_span`, with `extract_section` and `outline_sections` built on top. `replace_section` keeps its exact prior behavior, including the end-of-file-heading newline handling, which has its own regression test.

`extract_section` returns the heading line **plus** its body, while `replace_section` operates on the body only. A read wants the heading for orientation; a write must not rewrite it.

## Risks / Trade-offs

- **Contract change for existing callers.** Whole-note reads over 40,000 chars now truncate. Mitigated by a self-describing notice, an operator-tunable cap, and the fact that the prior behavior's failure mode was a hard provider rejection rather than a good outcome. Called out explicitly in the proposal's Impact and in the README.
- **Character cap is a proxy for token cost.** Dense CJK text tokenizes worse per character than English prose, so the effective token cost varies. The default is conservative enough to absorb this; a tighter bound would require a tokenizer and model knowledge the server does not have.
- **Offsets are character positions into the note body, not stable identifiers.** If a note changes between windowed reads, offsets shift. Acceptable: this is the same weakness as any paged read, and section addressing avoids it for the structured notes that most need paging.
- **Ordinals shift when a note gains or loses headings.** They are for navigating within one response, not for storing. The outline that produces them is returned in the same response that uses them, so the window for skew is one call.

## Open Questions

None blocking. Worth revisiting if the default cap proves wrong in practice: `usage_logs.response_size` already records every tool response, so the distribution needed to retune it is being collected without extra instrumentation.
