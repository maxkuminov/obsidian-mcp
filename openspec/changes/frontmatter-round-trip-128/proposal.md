## Why

Issue #128 (found by #122, adversarially verified): the natural agent read-modify-write — reading a note, editing the *note-content portion* of `read_note`'s response, and passing it back through `edit_note` full-replace — silently deletes the note's YAML frontmatter, because that content portion has the block stripped while full-replace writes exactly what it is given. Separately, `set_frontmatter` on a note whose frontmatter is malformed (unclosed fence, YAML parse error, non-dict YAML) prepends a *second* `---` block above the broken one and reports success, and `remove=` silently no-ops. Both are destructive or silently-wrong write outcomes — the product's top failure class — and both are tool-contract questions, hence a spec change.

## What Changes

- **`edit_note` full-replace preserves an existing valid frontmatter block unconditionally by default.** `content` is always the new *body*; the block is kept byte-identical (with a specified separator newline when the block ends at EOF without one). No shape of `content` — a leading `---`, even a complete mapping-shaped fenced block — changes that: three audit rounds established that destructive intent cannot be inferred from content shape. A new explicit `replace_frontmatter: bool = False` parameter selects wholesale replacement (today's behavior, now opt-in) and is the escape hatch for replacing, dropping, or repairing a block. A note with no valid block (absent or malformed) is still replaced wholesale by default — the repair path. **BREAKING** only for callers that relied on body-only full-replace deleting frontmatter (that outcome is the bug).
- **Section mode cannot touch a frontmatter block.** Over a valid block it resolves and replaces against the frontmatter-stripped body (the same text `read_note` scans — restoring the read/write selector parity the base spec already promises) and reattaches the block byte-identically; over a *defective* block (unclosed fence, YAML error, non-mapping) it refuses by name rather than scanning raw bytes where a YAML `#` comment can be selected as a heading and a replacement can delete the closing fence.
- **`set_frontmatter` refuses malformed frontmatter** instead of prepending a second block: an unclosed line-1 fence, a fenced block that fails YAML parsing, or one whose YAML is not a mapping (`null`/`~`/comment-only included) produce an error naming the defect; nothing is written; `remove=` refuses identically. The whitespace-only empty block is a *valid* empty mapping. Removing the last key removes the block entirely (normative). The genuinely-absent cases keep today's prepend behavior.
- Docstrings at both layers — the registered wrappers in `server.py` (what MCP clients see) and the `tools.py` impls — state the round-trip contract.

## Capabilities

### New Capabilities

(none)

### Modified Capabilities

- `vault-write`: the full-replace mode's frontmatter-preservation rule; `set_frontmatter`'s malformed-frontmatter refusal.

## Impact

- `src/mcp_server/tools.py` (`edit_note` full-replace + section paths, new `replace_frontmatter` parameter, `set_frontmatter`) and `src/mcp_server/server.py` (registered wrapper signatures/docstrings — the MCP-visible contract)
- `src/services/vault.py`: `parse_frontmatter_diagnose` beside `parse_frontmatter`, which itself gains exactly one behavior change — whitespace-only fenced YAML becomes a valid empty mapping for every consumer (read, index, diagnose), so `read_note` stops rendering such a block as body text; section helpers gain the body-scoped path
- No schema change. `read_note`'s response shape is unchanged. Notes already indexed under the old empty-block partition stay stale in the index until their next hash-changing edit or an explicit per-index rebuild (ordinary reindex skips unchanged hashes) — declared, accepted (cosmetic, vanishingly rare)
