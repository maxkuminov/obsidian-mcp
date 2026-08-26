## MODIFIED Requirements

### Requirement: Section selection in read_note

When `section` is supplied, `read_note` SHALL return only the named section — the matched ATX heading line together with the content up to the next heading of equal-or-shallower depth, or end of note. The selector SHALL accept the same forms as the write tools: exact heading text, the path-style `Parent/Child` form, and the `#N` ordinal form.

A section response SHALL be subject to the same response-size cap, and `offset` SHALL window within the selected section rather than within the whole note.

A section response SHALL carry the same title/path/tags envelope as every
other `read_note` response, terminated by a `\n---\n` separator line, followed
by the **selected-content portion**. The selected-content portion of an
untruncated section response SHALL be exactly the heading line, its line
terminator, and the section's body as `edit_note(section=…)` defines it —
nothing more and nothing less. There SHALL be no region of a note that a
section read returns but a section write to the same selector cannot replace:
whitespace, blank lines, and fenced code blocks (as recognised by the shared
code masker) between the heading line and the next heading of
equal-or-shallower depth are part of the body on both sides.

The round trip SHALL therefore be specified over the selected-content portion,
never over the response as a whole. The extraction is: take the text following
the response's first `\n---\n` separator, then drop its first line. Passing
that remainder back as `edit_note(path, content=<remainder>,
section=<same selector>)` SHALL leave a note whose body newlines are LF
byte-identical.

That extraction is only safe if the first `\n---\n` in a response is always the
envelope's own separator, so the envelope SHALL be framed unambiguously — see
the requirement below. A body that itself contains a `\n---\n` line is not a
problem, because the envelope's separator precedes it.

A specification or docstring that instructs a caller to strip the response's
own first line SHALL be treated as a defect: the response's first line is
`# <title>`, and following that instruction writes the envelope's `**Path:**`
line into the note.

#### Scenario: Reading one section of a large note

- **WHEN** `read_note` is invoked with a `section` that is within the cap, on a note that is not
- **THEN** the response SHALL contain that section's heading and body
- **AND** SHALL NOT contain content from other sections
- **AND** SHALL NOT be truncated

#### Scenario: A section read returns nothing a section write cannot replace

- **WHEN** `read_note(path, section=<sel>)` returns an untruncated section
  whose body begins with a blank line, or with a fenced code block, or both
- **THEN** every byte of the selected-content portion after its first line
  SHALL fall inside the span `edit_note(path, content, section=<sel>)` replaces
- **AND** writing that remainder back SHALL reproduce an LF-bodied note exactly

#### Scenario: The response envelope is not part of the section

- **WHEN** a note `n.md` contains `# A\nold\n` and the client calls
  `read_note(path="n.md", section="#1")`
- **THEN** the response SHALL begin with the title/path envelope and a
  `\n---\n` separator, and the selected content SHALL begin only after it
- **AND** a caller that drops the response's own first line and writes the
  remainder back SHALL corrupt the note — so the documented extraction rule
  SHALL be the separator-based one, and both `read_note`'s and `edit_note`'s
  docstrings SHALL state it

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

## ADDED Requirements

### Requirement: The response envelope SHALL be unambiguously terminated

No dynamic component interpolated into a successful `read_note` response's envelope SHALL contain a line terminator. This is stated as an invariant over *every* component rather than as a list of fields, and SHALL be enforced at a single rendering choke point that every component passes through — the title, the path, each tag, **each frontmatter key**, and each frontmatter value alike, together with any component added later. Enumerating today's fields is what let two successive audit rounds each find a different field that could forge the separator; the invariant is what closes the class.

A component SHALL be rendered by stringifying it and replacing every LF, CRLF, or lone CR in the **resulting string** with a single space. Applying the rule to the rendered string rather than recursively to a composite's leaves is deterministic and needs no normalization order: Python's `str()` of a list or dict escapes interior newlines, so only a bare string component can carry a literal terminator into the envelope.

Consequently the first `\n---\n` in a successful, enveloped response is always the envelope's own separator, which is what makes the section round-trip extraction safe to document.

The guarantee is scoped to **successful responses that carry an envelope**. Error and end-of-content responses — a missing note, an out-of-range `offset`, an unknown section — carry no envelope and no selected content, so no extraction contract applies to them; they are outside this requirement and SHALL NOT be read as promising anything about their `---` lines.

The note **body** SHALL NOT be sanitized — only the envelope's own components.

#### Scenario: A multiline title cannot forge the separator

- **WHEN** a note's frontmatter is `title: |-` with a multiline scalar whose
  lines include markup and a bare `---`, and the client calls
  `read_note(path, section=<sel>)`
- **THEN** the envelope SHALL render that title on one line, and the first
  `\n---\n` in the response SHALL be the envelope separator
- **AND** the documented extraction SHALL yield the section, so writing it
  back leaves the note unchanged — rather than yielding a remainder beginning
  `**Path:**` and clobbering the section

#### Scenario: A multiline frontmatter KEY cannot forge the separator

- **WHEN** a note's frontmatter contains the valid quoted key
  `"safe\n---\nforged": value` and the client calls
  `read_note(path, section="#1")`
- **THEN** the rendered key SHALL occupy one line, so the response contains no
  `---` line before the envelope separator
- **AND** the documented extraction SHALL yield the section rather than
  `\n---\n# A\nold\n`, which written back would clobber the section

#### Scenario: Composite frontmatter values are rendered deterministically

- **WHEN** a frontmatter value is a list or a dict whose string leaves contain
  newlines, for example `items: ["a\nb"]`
- **THEN** the component SHALL be stringified first and the terminators
  collapsed in the resulting string, yielding one line
- **AND** the rule SHALL NOT depend on recursing into the composite's leaves

#### Scenario: A body containing a separator line is unaffected

- **WHEN** the selected section's body itself contains a `\n---\n` line (a
  thematic break)
- **THEN** the extraction SHALL still select the whole section, because the
  envelope's separator occurs first
- **AND** the body's own `---` line SHALL be returned and written back
  unchanged

#### Scenario: The note body is never sanitized

- **WHEN** a successful enveloped `read_note` response is produced
- **THEN** only envelope components SHALL have line terminators collapsed
- **AND** the note content portion SHALL be byte-exact apart from the read
  path's pre-existing universal-newline translation
