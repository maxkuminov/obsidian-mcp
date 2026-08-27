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

- **WHEN** the `content` field of a **complete, unwindowed** section read for
  `<sel>` (`offset=0`, `truncated` false) is passed back unchanged as
  `edit_note(path, content=<content>, section=<sel>)`, on a note whose body
  newlines are LF, whose line-1 frontmatter is absent or valid, and which
  contains no unmatched indented fence opener — the notes for which a section
  write is admitted at all
- **THEN** the resulting file SHALL be byte-identical to the original
- **AND** this SHALL hold for every `#N` ordinal in the note, including
  sections whose body begins with a blank line, sections whose body begins
  with a fenced code block, sections with an empty body, and the final
  section of the note
- **AND** the guarantee SHALL be verified against the shared section helpers
  AND against the structured response itself: the section read's `content`
  field **is** the body, so no recovery procedure exists to get wrong
- **AND** it SHALL NOT extend to a windowed or truncated section response
  (`truncated` true) — writing such a window back replaces the whole body with
  the fragment and deletes the remainder, exactly as the note-read requirement
  already warns
- **AND** it SHALL NOT be read as weakening the refusal on a defective
  frontmatter block: such a note remains readable by section and refused for
  section writes, and the refusal takes precedence over the round trip
- **AND** the same precedence SHALL hold for the unmatched-indented-fence-opener
  refusal this change introduces: such a note remains readable by section, its
  selectors resolve for reads under the not-a-fence interpretation, and every
  section write to it is refused by name — selector parity between read and
  write is a claim about resolution on admitted writes, not a promise that
  every readable section is writable

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

#### Scenario: Indented and longer-closed fences are covered by the masker

- **WHEN** a fence is indented by one to three spaces, or is closed by a
  run at least as long as its opener — shapes the `code-masking`
  capability's grammar recognises
- **THEN** this requirement's fenced-code guarantees SHALL cover it: a
  heading inside it is not selectable and does not bound a section
- **AND** the re-addressing this widening causes on notes containing such
  shapes is the declared break of the fence-grammar change (issue #150),
  superseding the residual this scenario previously declared out of scope

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

### Requirement: Section-mode docstrings state the round-trip contract

Both layers' `edit_note` docstrings SHALL state that in section mode `content` is the section's **body only** — the registered wrapper in `server.py` (what an MCP client sees) and the implementation in `tools.py` alike. The body is exactly what a `read_note(section=…)` response's `content` field carries, beginning on the line immediately after the heading line.

The docstrings SHALL state that any blank line the caller wants between the
heading and its content belongs in `content`, and SHALL name
`read_note(section=…)` as the matching read: a section response carries the
heading line in its `heading` field and the body in its `content` field, and
`edit_note(section=…)` takes exactly that `content`.

They SHALL NOT prescribe a textual procedure for recovering the body from a
rendered response — no "split on the separator", no "drop the first line".
The structured `content` field is the recovery; the reason such procedures
were banned (any rendered envelope interpolates note-controlled values and can
be forged into an instruction that writes `**Path:** …` into the note) SHALL
stay recorded where a future author will find it.

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
- **AND** SHALL say that the whole body is replaced, so omitted content — a
  fenced code block included — is deleted
- **AND** SHALL name `read_note(section=…)` as the matching read and its
  `content` field as the exact body to pass back
- **AND** the same statements SHALL appear in the `tools.py` implementation's
  docstring, so the two layers do not diverge

### Requirement: `edit_note` supports four mutually exclusive modes

The `edit_note` tool SHALL expose exactly four edit modes, selected by the
combination of parameters supplied: full-replace (default), append,
find/replace, and section. The four modes SHALL be mutually exclusive —
supplying parameters that select more than one mode SHALL return an
actionable error and SHALL NOT mutate the file.

#### Scenario: Full-replace mode (default) preserves a valid frontmatter block

- **WHEN** the client calls `edit_note(path, content)` with neither
  `append`, `find`, nor `section` set and without
  `replace_frontmatter=True`, on a note that carries a valid line-1
  frontmatter block
- **THEN** the written file SHALL be the existing raw block
  byte-identical, a separator (one `\n` inserted only when the block's
  bytes do not already end in a newline and `content` is non-empty),
  then `content` as the entire body
- **AND** no property of `content`'s shape — a leading `---`, an
  unclosed fence, or a complete mapping-shaped fenced block (exactly
  what `read_note` returns for a note whose body begins with one) —
  SHALL change this: `content` is always the body

#### Scenario: Any stripped body round-trips unchanged

- **WHEN** the note-content portion of a **complete, unwindowed
  whole-note** `read_note` response (`section=None`, `offset=0`, no
  truncation notice) is passed back via `edit_note(path, body)` —
  including a body whose first line is a thematic break `---`, a body
  that itself begins with a complete mapping-shaped fenced block, and
  the body of a note whose frontmatter is the whitespace-only empty
  block (which the shared parser SHALL strip on read exactly as it
  preserves it on write)
- **THEN** the resulting file SHALL be identical to the original for a
  note whose body newlines are LF
- **AND** for a CRLF-bodied note the frontmatter block SHALL still be
  preserved byte-identically (CRLF fences included); the *body* comes
  back through `read_note`'s pre-existing universal-newline translation
  as LF, and the round trip preserves content, not the body's original
  newline bytes — a pre-existing property of the read path, declared,
  not a regression introduced here

#### Scenario: The round-trip guarantee does not extend to windows or sections

- **WHEN** a `read_note` response was a selected section
  (`section=<sel>`), a paged window (`offset>0`, or `truncated` true in
  the structured result), or otherwise less than the whole body, and its
  `content` is passed to default full-replace
- **THEN** full-replace still preserves the frontmatter block but
  replaces the ENTIRE body with the partial text — so both layers'
  docstrings SHALL state that section responses belong to section mode
  and truncated reads must be completed before a full-replace write

#### Scenario: replace_frontmatter selects wholesale replacement

- **WHEN** the client calls
  `edit_note(path, content, replace_frontmatter=True)` in full-replace
  mode
- **THEN** the entire note, frontmatter included, SHALL be overwritten
  with exactly `content`
- **AND** `replace_frontmatter=True` combined with `append`, `find`, or
  `section` SHALL return the multi-mode error and SHALL NOT modify the
  file

#### Scenario: A note without a valid block is replaced wholesale

- **WHEN** the existing note has no line-1 fence, or its block is
  defective (unclosed fence — a bare unterminated `---` at EOF included
  — YAML error, or non-mapping), and the client calls
  `edit_note(path, content)` in full-replace mode
- **THEN** the entire note SHALL be overwritten with `content` (there is
  no valid block to preserve; this is the repair path and needs no flag)

#### Scenario: A metadata-only note gains a correct separator

- **WHEN** the existing note is exactly a valid frontmatter block whose
  closing fence ends at EOF without a trailing newline, and the client
  calls `edit_note(path, "Body\n")` in default full-replace mode
- **THEN** the written file SHALL contain the block, a single inserted
  newline, then `Body\n` — the closing fence SHALL remain a recognized
  fence

#### Scenario: A trailing-whitespace closing fence is one block everywhere

- **WHEN** a note's closing fence carries trailing spaces or tabs
  (which `parse_frontmatter` accepts today)
- **THEN** default full-replace SHALL treat the note as carrying a valid
  block and preserve it byte-identically, trailing whitespace included

#### Scenario: Fence lines end at LF, CRLF or a lone CR

- **WHEN** a note's fence lines are terminated by LF, by CRLF, or by a
  lone CR (classic-Mac line endings)
- **THEN** the shared parser SHALL recognize the block in every case,
  consistent with the universal-newline translation the read path applies
  before it parses the same file — so a note whose block `read_note`
  strips is never diagnosed as having no frontmatter by a tool that is
  about to write
- **AND** default full-replace SHALL preserve that block
  byte-identically, its own terminators included, and SHALL insert the
  separator newline only when the block's bytes end in no line terminator
  at all
- **AND** `set_frontmatter` SHALL update such a block rather than
  prepending a second one above it, and SHALL refuse it by name when it
  is defective
- **AND** the fenced/inline code masking that heading resolution runs on
  SHALL use the same terminator rule, so a heading inside a code block is
  hidden from `edit_note(section=…)` exactly as it is from
  `read_note(section=…)` — otherwise a selector resolves inside code on
  the write side only, and the replacement deletes the closing fence
- **AND** widening the terminator rule SHALL NOT narrow which characters
  separate a heading's `#` marker from its text: every whitespace
  character except the three terminators still separates them, so no
  heading present on an existing note loses its name or its `#N` ordinal

#### Scenario: The composed result meets the cap and conflict checks

- **WHEN** preservation composes a result whose byte size exceeds the
  note-size cap, or `expected=` was supplied and any part of the raw
  file — the frontmatter block included — changed since it was read
- **THEN** the call SHALL be refused without writing (the cap applies to
  the composed result; `expected=` compares the complete raw bytes, so a
  concurrent frontmatter-only change conflicts)
- **AND** `dry_run=True` SHALL diff the composed result

#### Scenario: Append mode

- **WHEN** the client calls `edit_note(path, content, append=True)`
  without `find` or `section`
- **THEN** the new file content SHALL be the prior content followed by a
  single `\n` separator and `content`

#### Scenario: Find/replace mode

- **WHEN** the client calls `edit_note(path, content, find=<text>)`
  without `append=True` or `section`
- **THEN** the system SHALL replace occurrences of `find` in the prior
  content with `content` per the `replace_all` rules below

#### Scenario: Section mode

- **WHEN** the client calls `edit_note(path, content, section=<heading>)`
  without `append=True` or `find`
- **THEN** the system SHALL replace the body under the named ATX heading
  per the section-mode rules below

#### Scenario: Multiple modes set is rejected

- **WHEN** the client supplies more than one of `append=True`,
  `find=...`, or `section=...` in the same call
- **THEN** the system SHALL return an error naming the conflicting
  parameters
- **AND** SHALL NOT modify the file
