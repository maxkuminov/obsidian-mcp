## Why

Issue #140 (found by #128's differential harness, deliberately deferred there):
`read_note(section=…)` and `edit_note(section=…)` disagree about where a
section's body *begins*, so the natural agent round trip — read a section,
change something, write the part after the heading line back — is not
byte-stable on ordinary LF notes.

Two observable failures, one root cause:

1. **Fenced-code duplication.** On `# A\n` + a fenced block + `# B`,
   `read_note(section="#1")` returns the code block, but
   `edit_note(section="#1")` computes an **empty** body span positioned after
   it. Writing the read body back leaves the original block in place and
   inserts a second copy. Verified: the round trip turns
   `'# A\n```\n## Hidden\ntext\n```\n# B\nb\n'` into a note carrying the fence
   twice.
2. **Blank-line accumulation.** A blank line after a heading is absorbed into
   the heading match, so it is inside what read returns and outside what write
   replaces. Each round trip re-emits it *and* keeps the original. Verified:
   `'# A\n\nbody\n\n# B\nb\n'` gains blank lines super-linearly — one round
   trip adds one, the next adds two, the next adds four.

Both are the read/write-parity family #128 hardened, and both are the product's
top failure class: an agent acting on `read_note`'s answer corrupts the note and
reports success. They were left out of #128 because any fix changes the bytes
`edit_note(section=…)` writes on common notes — the exact compat break #128's
zero-divergence envelope forbade — so the contract decision belongs here.

## What Changes

- **A section's body SHALL begin on the line immediately after the heading
  line.** This is the whole change, stated once and applied to both sides:
  `read_note(section=S)` returns the heading line, its terminator, and that
  body; `edit_note(section=S, content=B)` replaces exactly that body. The
  portion of a section response after its first line is therefore precisely the
  region a section write replaces, and the round trip is byte-identical on LF
  notes.
- **Mechanically:** `_ATX_HEADING_RE`'s trailing whitespace run narrows from
  `\s*` (which crosses line boundaries) to `[^\S\r\n]*` (horizontal only), so a
  heading's `line_end` is genuinely the end of its heading line in every
  dialect. `_section_body_span` keeps stepping over exactly one terminator.
  Nothing else in the resolver changes: heading *text*, depth, ordinals,
  `line_start`, and every selector form are unaffected, so no existing selector
  starts naming a different section.
- **BREAKING (declared, and the point of the change):** whitespace and fenced
  blocks that sit between a heading line and its first line of prose are now
  part of the body. Two consequences on notes that exist today:
  - `edit_note(section="Tasks", content="- x")` on `## Tasks\n\n- old\n`
    now writes `## Tasks\n- x\n`, not `## Tasks\n\n- x\n`. The blank separator
    is the caller's to send (`content="\n- x"`), and a caller that round-trips
    a read gets it back for free.
  - A fenced block directly under a heading is now *replaced* by a section
    write rather than being left behind with new content inserted after it.
    That is the fix, not a regression: leaving it was the duplication bug.
- Docstrings at both layers — the registered wrappers in `server.py` (what MCP
  clients see) and the `tools.py` impls — state the round-trip contract in the
  terms callers need: *the section response minus its first line is exactly
  what `edit_note(section=…)` takes*.
- `docs/architecture/vault-tools.md` is updated in the same change: its
  "the trailing run stays the original `\s*` … narrowing it would change the
  bytes `edit_note(section=…)` writes on ordinary LF notes" note is now the
  *history* of a constraint this change deliberately lifts, and must say so
  rather than continuing to forbid the fix.

## Capabilities

### New Capabilities

(none)

### Modified Capabilities

- `vault-write`: section mode's body-span definition, and the round-trip
  guarantee it now owes `note-read`.
- `note-read`: the section response's relationship to the section write span.

## Impact

- `src/services/vault.py` — `_ATX_HEADING_RE` (the trailing class only) and the
  comment block above it, which currently documents the opposite decision.
  `_scan_headings`, `_section_body_span`, `extract_section`, `replace_section`
  and `outline_sections` keep their signatures and their roles.
- `src/mcp_server/server.py` and `src/mcp_server/tools.py` — docstrings only.
- `docs/architecture/vault-tools.md` — the section-addressing section.
- **No schema change, no migration, no index effect.** `outline_sections`'
  reported `size` shifts by the bytes that moved from heading to body for
  sections that had a blank line or a leading fence; it is a display number in
  a truncation outline, and the `#N` ordinals it advertises are unchanged.
- Notes already in the vault are not rewritten. The change is to what a *future*
  section write does, and the direction is strictly toward preserving what the
  caller read.
