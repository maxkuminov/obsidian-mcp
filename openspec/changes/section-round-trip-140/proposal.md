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

  **The guarantee is stated over note text, not over rendered response text,
  and that boundary is deliberate.** Every textual procedure for recovering a
  section from a `read_note` response proved forgeable: the envelope
  interpolates the title, the path, the tags and each frontmatter key and
  value, and a valid note can make any of them emit a line that mimics the
  envelope's own `---` separator. Reproduced twice, on different fields — a
  multiline YAML title, and the valid quoted key `"safe\n---\nforged"` — each
  time yielding a remainder beginning `**Path:** `n.md`` that
  clobbers the section when written back. Sanitising the named fields does not
  close it (two audit rounds, two different fields), and collapsing terminators
  to make them safe is lossy: the distinct paths `a\nb.md` and `a b.md` would
  render identically, trading a destructive write for a silently wrong read.

  So this change does **not** document an extraction procedure, and forbids the
  docstrings from prescribing one. Making the selected content unambiguously
  recoverable needs structural framing of the response — separate metadata and
  section-body fields — which changes `read_note`'s response shape and belongs
  in its own proposal — filed as **#149**. Until it lands, the docstrings state the
  relationship (a section response carries the heading line and the body;
  `edit_note(section=…)` takes the body) without a parsing recipe.

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
    `# A\n```\nimportant\n```\nold\n`,
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
- `src/mcp_server/server.py` and `src/mcp_server/tools.py` — docstrings only.
  `read_note`'s response shape is unchanged here; the envelope framing is a
  separate, filed change.
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
  larger compat break than this one and belongs in its own change; filed as
  **#150**. The spec here says "fenced code block **as recognised by the
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
