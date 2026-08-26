## MODIFIED Requirements

### Requirement: Section mode replaces the body under a named heading

When `section=<heading>` is supplied, `edit_note` SHALL locate the matching
ATX heading (1–6 `#` characters) and SHALL replace that heading's **body** with
the supplied `content`. The matched heading line itself SHALL NOT be removed or
rewritten.

A section's body SHALL begin at the first byte of the line immediately
following the heading line, and SHALL end immediately before the next heading of
equal-or-shallower depth, or at end of file. At most one line terminator (LF,
CRLF as a unit, or a lone CR) separates the heading line from the body; a
heading at end of file with no trailing terminator has an empty body. No
whitespace, blank line, or fenced code block — as recognised by the shared code
masker — between the heading line and the next heading of equal-or-shallower
depth SHALL be excluded from the body: a section has no third region between
its heading line and its body.

A separator newline SHALL be inserted around the replacement only when the
replacement body is **non-empty**: one before it when the retained prefix does
not already end in a terminator (an end-of-file heading), and one after it when
a following heading would otherwise be glued to it. Replacing an empty body
with an empty body SHALL leave the note byte-identical — an unconditional
separator makes a section with no body grow a blank line on every round trip,
and makes an unterminated end-of-file heading grow a newline.

Because a section write replaces the whole body, content the caller does not
resend is **deleted**, fenced code blocks included. This is the contract, not
an accident; it SHALL be stated in the caller-visible documentation in those
terms.

The selector SHALL accept three forms:

1. **Ordinal** (e.g. `#7`) — a selector consisting solely of `#` followed by
   digits SHALL select the Nth ATX heading in document order, 1-based. A bare
   ordinal SHALL always select by position, even when some heading's literal
   text is the same string. Ordinals are advertised to callers as the reliable
   selector, so note content MUST NOT be able to shadow one.
2. **Path-style chain** (e.g. `Parent/Child`), where the final part is the
   target heading and the preceding parts are ancestors in outermost-first
   order. A selector containing `/` SHALL NOT be interpreted as an ordinal.
3. **Exact heading text** (e.g. `Tasks`).

A heading whose literal text is `#N` SHALL remain addressable by the path-style
form (`Parent/#N`) and by its own ordinal.

The ordinal form exists because the path-style form cannot separate duplicate
**sibling** headings — headings with identical text under the same parent share
every ancestor, so no chain distinguishes them.

The same selector grammar SHALL be used by every tool that accepts a `section`
argument, so a selector that names a section for reading names the same section
for writing.

Narrowing the body's start to the line after the heading line SHALL NOT change
which heading any selector resolves to. Heading depth, trimmed heading text,
document order, and therefore every `#N` ordinal SHALL be unchanged by this
requirement.

When the note carries a valid line-1 frontmatter block, heading resolution
(all three selector forms), body replacement, and the not-found/ambiguity
listings SHALL operate on the frontmatter-stripped body — the same text
`read_note` scans — and the write SHALL reattach the raw block
byte-identically. A line inside the block (a YAML `#` comment included)
SHALL never be selectable as a heading and SHALL never be counted by an
ordinal. When the note's block is **defective** (unclosed fence, YAML
error, or non-mapping — comment-only YAML included), section-mode writes
SHALL be refused with an error naming the defect and pointing at
`edit_note(replace_frontmatter=True)` as the repair — never resolved over
the raw bytes, where a `#` line inside the broken block could be selected
and a replacement could delete the closing fence. A note with no fence at
all resolves over its raw content, which is identical to what `read_note`
scans there.

#### Scenario: A section round trip is byte-identical

- **WHEN** the **selected-content portion** of an untruncated
  `read_note(path, section=<sel>)` response — the text after the response's
  `\n---\n` envelope separator — is stripped of its first line (the heading
  line and its terminator) and the remainder is passed back as
  `edit_note(path, content=<remainder>, section=<sel>)`, on a note whose body
  newlines are LF
- **THEN** the resulting file SHALL be byte-identical to the original
- **AND** this SHALL hold for every `#N` ordinal in the note, including
  sections whose body begins with a blank line, sections whose body begins
  with a fenced code block, sections with an empty body, and the final
  section of the note

#### Scenario: An empty section survives a round trip

- **WHEN** a note is `# A\n# B\nb\n` (the first section has no body) and
  the client calls `edit_note(path, "", section="#1")`
- **THEN** the resulting note SHALL be `# A\n# B\nb\n`, unchanged — no
  separator newline SHALL be inserted for an empty body
- **AND** on the note `# A` (a heading at end of file with no trailing
  terminator), the same call SHALL leave the note as `# A`

#### Scenario: A non-empty body is still separated from what surrounds it

- **WHEN** the client calls `edit_note(path, "- item", section="Notes")` on
  a note whose `# Notes` heading is the last line and carries no trailing
  newline
- **THEN** the heading SHALL NOT be glued to the content: the result SHALL
  be `# Notes\n- item`
- **AND** when a following heading exists, a terminator SHALL be ensured
  between the new body and that heading

#### Scenario: A fenced block under a heading is replaced, not duplicated

- **WHEN** a note is `# A\n` followed by a fenced code block recognised by
  the shared masker (whose content may itself contain `#`-prefixed lines)
  followed by `# B\nb\n`, and the client calls
  `edit_note(path, "new", section="#1")`
- **THEN** the fenced block SHALL be replaced by `new`
- **AND** the resulting note SHALL contain the fence exactly zero further
  times — it SHALL NOT be retained with `new` inserted after it
- **AND** the caller-visible documentation SHALL state that this is a
  deletion: a `content` that does not resend the block loses it

#### Scenario: A fence the masker does not recognise is out of scope, not covered

- **WHEN** a fence is indented, or is closed by a run longer than its
  opener — shapes `_FENCE_RE` does not currently mask — so that a heading
  inside it is visible to the scanner
- **THEN** this requirement's fenced-code guarantees SHALL NOT be read as
  covering it, and the behaviour SHALL be identical to the behaviour before
  this change (verified, not assumed)
- **AND** the gap SHALL be recorded as a declared residual with its own
  tracking issue, because widening the masker re-addresses `#N` ordinals on
  existing notes and is a larger compat break than this change

#### Scenario: A blank line after a heading belongs to the body

- **WHEN** a note is `# A\n\nold\n# B\nb\n` and the client calls
  `edit_note(path, "new", section="#1")`
- **THEN** the resulting note SHALL be `# A\nnew\n# B\nb\n` — the blank
  line is part of the replaced body, and a caller that wants it back
  SHALL include it in `content`
- **AND** repeating the read-strip-write round trip on the original note
  any number of times SHALL NOT change the file

#### Scenario: Trailing spaces on a heading line stay on the heading line

- **WHEN** a heading line carries trailing horizontal whitespace (spaces,
  tabs, or a non-ASCII space) before its terminator
- **THEN** that whitespace SHALL remain part of the heading line and SHALL
  NOT become part of the body
- **AND** the heading's trimmed text, and therefore its addressability by
  the exact-text and path-style selectors, SHALL be unchanged

#### Scenario: A YAML comment in frontmatter is not a heading

- **WHEN** a note is `---\n# Tasks\nstatus: draft\n---\n# Body\nkeep\n`
  and the client calls `edit_note(path, "x", section="Tasks")`
- **THEN** the selector SHALL NOT match the `# Tasks` line inside the
  frontmatter block; it resolves against the body's headings only (here
  reporting `Tasks` not found and listing `Body`)
- **AND** the frontmatter block SHALL be untouched by any section edit

#### Scenario: Ordinals agree between read_note and edit_note

- **WHEN** a note's frontmatter block contains `#`-prefixed comment lines
  and its body contains ATX headings
- **THEN** `edit_note(section="#N")` SHALL select the same heading that
  `read_note(section="#N")` extracts

#### Scenario: A defective block refuses section writes by name

- **WHEN** a note is `---\n# Tasks\n---\n# Body\nkeep\n` (comment-only
  YAML — a non-mapping) or carries an unclosed or YAML-erroring block,
  and the client calls `edit_note(path, "x", section=<anything>)`
- **THEN** the call SHALL be refused with an error naming the defect and
  the `replace_frontmatter=True` repair path
- **AND** SHALL NOT modify the file

#### Scenario: Replace section under a level-2 heading

- **WHEN** the note contains `## Tasks\nA\nB\n## Notes\nC` and the client
  calls `edit_note(path, content="X\nY", section="Tasks")`
- **THEN** the resulting note SHALL be `## Tasks\nX\nY\n## Notes\nC`

#### Scenario: Section heading not found

- **WHEN** no ATX heading in the note has trimmed text equal to
  `<heading>`, and the selector is not a valid ordinal
- **THEN** the response SHALL list the headings that ARE present in the
  note (with their depth) and instruct the caller to disambiguate
- **AND** SHALL NOT modify the file

#### Scenario: Multiple matching headings disambiguated by occurrence

- **WHEN** more than one heading in the note matches `<heading>` exactly
- **THEN** the response SHALL state the number of matches and instruct
  the caller to use the more-specific path-style form
  `Parent Heading/Child Heading` to disambiguate
- **AND** the response SHALL name the `#N` ordinals that identify each match
- **AND** SHALL NOT modify the file until the call is reissued
  unambiguously

#### Scenario: Path-style heading disambiguation

- **WHEN** the client calls `edit_note(path, content, section="Tasks/Today")`
  and the note contains `## Tasks` followed by `### Today`
- **THEN** the system SHALL replace the body under `### Today` (bounded
  by the next heading of depth ≤ 3) with `content`

#### Scenario: Duplicate sibling headings resolved by ordinal

- **WHEN** a note contains two headings with identical text under the same
  parent, and the client supplies the `#N` ordinal of one of them
- **THEN** that specific section SHALL be selected
- **AND** the other identically-titled section SHALL be unaffected

#### Scenario: Ordinal out of range

- **WHEN** the supplied ordinal is below 1 or exceeds the number of ATX
  headings in the note
- **THEN** the response SHALL report the valid ordinal range
- **AND** SHALL NOT modify the file

#### Scenario: A heading literally named like an ordinal

- **WHEN** a note contains a heading whose text is exactly `#2` and the
  client supplies `section="#2"`
- **THEN** the second heading in the note SHALL be selected, because a bare
  ordinal always selects by position
- **AND** the heading titled `#2` SHALL remain reachable via the path-style
  form and via its own ordinal

## ADDED Requirements

### Requirement: Section-mode docstrings state the round-trip contract

Both layers' `edit_note` docstrings SHALL state that in section mode `content` is the section's **body only** — the registered wrapper in `server.py` (what an MCP client sees) and the implementation in `tools.py` alike. The body is the text a
`read_note(section=…)` response carries *below* its heading line, beginning on
the line immediately after it. This is the contract a caller can only discover
by reading it, and getting it wrong is how a section round trip corrupts a note.

The docstrings SHALL state that any blank line the caller wants between the
heading and its content belongs in `content`, and SHALL point at
`read_note(section=…)` as the source of a round-trippable body — together with
the exact extraction: **the text after the response's `\n---\n` envelope
separator, minus its first line.** They SHALL NOT tell a caller to strip the
response's own first line, which is the envelope's `# <title>` and would write
`**Path:**` into the note.

They SHALL further state (a) that a section write **replaces the whole body**,
so content omitted from `content` — a fenced code block included — is deleted,
and (b) that byte-identity holds only for notes whose body newlines are LF.
Every non-LF terminator inside the **selected body** comes back as LF, whether
the note uses one dialect throughout or mixes them, because the read path
normalises and the write path rewrites raw bytes; terminators outside the
selected body are untouched, so a round trip can leave a note with more mixed
endings than it started with.

#### Scenario: An MCP client can learn the contract from introspection

- **WHEN** an MCP client introspects `edit_note`
- **THEN** the documentation for `section` SHALL say that `content` replaces
  the body beginning on the line after the heading line
- **AND** SHALL name `read_note(section=…)` as the matching read
- **AND** the same statement SHALL appear in the `tools.py` implementation's
  docstring, so the two layers do not diverge
