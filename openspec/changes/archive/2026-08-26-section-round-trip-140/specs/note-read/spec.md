## MODIFIED Requirements

### Requirement: Section selection in read_note

When `section` is supplied, `read_note` SHALL return only the named section — the matched ATX heading line together with the content up to the next heading of equal-or-shallower depth, or end of note. The selector SHALL accept the same forms as the write tools: exact heading text, the path-style `Parent/Child` form, and the `#N` ordinal form.

A section response SHALL be subject to the same response-size cap, and `offset` SHALL window within the selected section rather than within the whole note.

The **selected content** a section response carries — the note text within it,
as distinct from the title/path/tags envelope every `read_note` response
begins with — SHALL be exactly the heading line, its line terminator, and the
section's body as `edit_note(section=…)` defines it: nothing more and nothing
less. Apart from the heading line and its terminator — which a section read
returns and a section write deliberately preserves — there SHALL be no region
of a note that a section read returns but a section write to the same selector
cannot replace.

The parity claim is scoped to notes for which section-mode writing is
**admitted**. A note whose line-1 frontmatter is defective (unclosed fence,
YAML error, or non-mapping) stays readable by section — the read scans its raw
bytes — while every section write to it is refused by name, per the vault-write
requirement this change does not relax. On such a note the guarantee is the
refusal, not the round trip: it is the safe asymmetry, and widening parity to
cover it would mean scanning a broken block for headings on the write side,
which is the destructive behaviour #128 removed.

Whitespace, blank lines, and
fenced code blocks (as recognised by the shared code masker) between the
heading line and the next heading of equal-or-shallower depth are part of the
body on both sides.

This is a requirement on what the read and write agree about, and it SHALL be
verifiable directly against the shared section helpers, which operate on note
text rather than on a rendered response.

**This requirement deliberately does NOT define a textual procedure for
recovering the selected content from a rendered response, and no docstring
SHALL instruct a caller to perform one.** Every such procedure proposed for
this change was forgeable: the envelope interpolates the title, the path, the
tags and each frontmatter key and value, and a valid note can make any of them
emit a line that mimics the envelope's own `---` separator, so a caller
following the procedure writes `**Path:** …` into the note. Sanitising the
named fields does not close it — two successive audit rounds each found a
different field — and collapsing terminators to make them safe is itself lossy,
rendering the distinct paths `a\nb.md` and `a b.md` identically. Making the
selected content unambiguously recoverable requires structural framing of the
response and is tracked as its own change; until it lands, the round-trip
guarantee is stated over note text, not over response text.

#### Scenario: Reading one section of a large note

- **WHEN** `read_note` is invoked with a `section` that is within the cap, on a note that is not
- **THEN** the response SHALL contain that section's heading and body
- **AND** SHALL NOT contain content from other sections
- **AND** SHALL NOT be truncated

#### Scenario: A section read returns nothing a section write cannot replace

- **WHEN** a section is selected whose body begins with a blank line, or with
  a fenced code block, or both
- **THEN** every byte of the selected content after its heading line SHALL
  fall inside the span `edit_note(path, content, section=<sel>)` replaces
- **AND** passing that body back SHALL reproduce an LF-bodied note exactly

#### Scenario: No docstring prescribes a textual extraction

- **WHEN** the `read_note` or `edit_note` documentation describes how a
  section read relates to a section write
- **THEN** it SHALL describe the relationship — a section response carries the
  heading line and the body; `edit_note(section=…)` takes the body — without
  instructing the caller to split the response on a separator or to drop its
  first line
- **AND** the reason SHALL be recorded where a future author will find it:
  such a procedure is forgeable by note content, and following it corrupts the
  note

#### Scenario: A CRLF note's section round trip is not byte-identical

- **WHEN** a note's raw bytes are `# A\r\nold\r\n# B\r\nkeep\r\n` and its
  first section is read and written back unchanged
- **THEN** the round trip SHALL preserve the section's *content*, and the
  resulting note SHALL be `# A\r\nold\n# B\r\nkeep\r\n` — the selected body's
  terminators become LF because the read path normalises and the write path
  works on raw bytes
- **AND** this residual SHALL be declared in both docstrings rather than
  claimed as byte-identity, which holds only for LF-bodied notes

#### Scenario: Section larger than the cap

- **WHEN** the selected section itself exceeds `MAX_READ_RESPONSE_CHARS`
- **THEN** the response SHALL be truncated with a notice identifying the section and the continuing `offset`
- **AND** the continuation SHALL preserve the `section` selection
- **AND** the round-trip guarantee SHALL NOT apply to such a response, which
  is a window rather than a whole section

#### Scenario: Unknown section

- **WHEN** `read_note` is invoked with a `section` that matches no heading
- **THEN** the response SHALL list the headings that are present
- **AND** SHALL NOT return note content
