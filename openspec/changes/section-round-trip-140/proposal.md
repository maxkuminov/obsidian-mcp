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
  the *selected-content portion* of a `read_note(section=S)` response is the
  heading line, its terminator, and that body; `edit_note(section=S, content=B)`
  replaces exactly that body.

  **The extraction rule is stated explicitly, because the response is not raw
  section text.** `read_note` prefixes every response with a title/path/tags
  envelope terminated by a `\n---\n` separator line. "Strip the first line of
  the response" — the obvious formulation, and the one the first draft of this
  spec used — would write `**Path:** \u0060…\u0060` into the note. The rule is:
  take the text after the response's first `\n---\n` separator, then drop its
  first line. Both docstrings SHALL say this in those terms.
- **The envelope SHALL be framed unambiguously, so that rule is safe to
  follow — stated as an invariant, not a list of fields.** No dynamic component
  of a successful response's envelope may contain a line terminator, enforced at
  a single rendering choke point that title, path, tags, frontmatter **keys**
  and frontmatter values all pass through. The enumeration is what failed twice:
  audit round 2 found a multiline *title* could forge the separator, round 3
  found a multiline frontmatter *key* could do it through the
  `**Frontmatter:**` block. Both are valid YAML. Patching the named field would
  have invited a third.

  Concretely, before this change: the valid key `"safe\n---\nforged": value` renders as two
  lines inside the `**Frontmatter:**` block, and the documented extraction then
  yields `\n---\n# A\nold\n` — which written back clobbers the section. Both
  reproductions are in the tasks as end-to-end cases, alongside a guard test
  that names no field at all: no line before the envelope separator may be
  exactly `---`.

  A component is rendered by stringifying it and collapsing terminators in the
  *resulting* string, which is deterministic and needs no rule about recursing
  into composites — `str()` escapes newlines inside a list or dict, so only a
  bare string component can carry one through. This also repairs the existing
  display bug where such a title breaks the response layout on an ordinary
  whole-note read. The note body is never sanitized, and error responses — which
  carry no envelope and no selected content — are out of scope.
- **Mechanically:** `_ATX_HEADING_RE`'s trailing whitespace run narrows from
  `\s*` (which crosses line boundaries) to `[^\S\r\n]*` (horizontal only), so a
  heading's `line_end` is genuinely the end of its heading line in every
  dialect. `_section_body_span` keeps stepping over exactly one terminator.
  Nothing else in the resolver changes: heading *text*, depth, ordinals,
  `line_start`, and every selector form are unaffected, so no existing selector
  starts naming a different section.
- **`replace_section` SHALL insert a separator newline only for a non-empty
  replacement body.** Today it appends one before the following heading, and
  prepends one after an unterminated EOF heading, unconditionally — so an
  *empty* section is not round-trip stable even with the regex fixed
  (`# A\n# B\nb\n` gains a blank line; `# A` gains a newline). Verified against
  the current helpers. Without this the headline property is false for the
  commonest degenerate note.
- **BREAKING (declared, and the point of the change) — and it is destructive,
  not cosmetic.** Everything between a heading line and the next heading of
  equal-or-shallower depth is now the body, so a section write replaces all of
  it. Consequences on notes that exist today:
  - `edit_note(section="Tasks", content="- x")` on `## Tasks\n\n- old\n`
    now writes `## Tasks\n- x\n`, not `## Tasks\n\n- x\n`. The blank separator
    is the caller's to send (`content="\n- x"`).
  - **A fenced block directly under a heading is now deleted** by a section
    write whose `content` does not resend it. On
    `# A\n\u0060\u0060\u0060\nimportant\n\u0060\u0060\u0060\nold\n`,
    `edit_note(section="A", content="new")` previously kept the block and
    replaced only `old`; it now yields `# A\nnew\n`. That is the intended
    contract — leaving the block behind *was* the duplication bug — but the
    blast radius is content loss for a caller that does not round-trip, and
    both the docstrings and `vault-tools.md` must say so in those words.
- Docstrings at both layers — the registered wrappers in `server.py` (what MCP
  clients see) and the `tools.py` impls — state the round-trip contract in the
  terms callers need: *take the text after the section response's `\n---\n`
  envelope separator, drop its first line, and that is exactly what
  `edit_note(section=…)` takes*. They SHALL NOT say "the response minus its
  first line" — the response's first line is the envelope's `# <title>`.
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
  comment block above it, which currently documents the opposite decision; plus
  `replace_section`'s two separator insertions, which become conditional on a
  non-empty body. `_scan_headings`, `_section_body_span`, `extract_section`,
  `replace_section` and `outline_sections` keep their signatures and their
  roles.
- `src/mcp_server/tools.py` — `read_note_impl`'s envelope construction, so no
  interpolated field can emit a bare `\n---\n` line; plus `edit_note`'s and
  `read_note`'s docstrings. `src/mcp_server/server.py` — the registered
  wrappers' docstrings, which must carry the same statements.
- `docs/architecture/vault-tools.md` — the section-addressing section.
- **No schema change, no migration, no index effect.** `outline_sections`'
  reported `size` is `body_end - line_start`; this change moves neither
  endpoint, so both the sizes and the `#N` ordinals in a truncation outline are
  **unchanged**. Verified across the corpus.
- **Declared residual (pre-existing, unchanged here): the code masker's fence
  grammar is narrower than CommonMark.** `_FENCE_RE` recognises only a
  column-zero opener closed by a fence of exactly the same length, so an
  indented fence, or one closed by a longer run, is not masked — a heading
  inside such a block is selectable, and a section write there already deletes
  the opening fence and orphans the contents. Measured before and after this
  change: the bytes written are **identical**, so this change neither creates
  nor worsens it. Fixing the masker shifts which lines count as headings and
  therefore re-addresses every `#N` ordinal on affected notes, which is a
  larger compat break than this one and belongs in its own change; filed
  separately. The spec here says "fenced code block **as recognised by the
  shared masker**" rather than claiming CommonMark coverage it does not have.
- **Declared residual: newline dialect.** `read_note` applies universal-newline
  translation; `edit_note` reads and rewrites raw bytes. A section round trip
  on a CRLF note therefore rewrites *that section's* terminators as LF while
  the retained heading line and the rest of the note keep CRLF — measured:
  `# A\r\nold\r\n# B\r\nkeep\r\n` becomes `# A\r\nold\n# B\r\nkeep\r\n`.
  This is the section-mode instance of the whole-note residual #128 already
  declared; the byte-identity guarantee is stated for LF-bodied notes only, and
  the docstrings say so.
- Notes already in the vault are not rewritten. The change is to what a *future*
  section write does, and the direction is strictly toward preserving what the
  caller read.
