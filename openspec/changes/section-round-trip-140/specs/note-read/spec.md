## MODIFIED Requirements

### Requirement: Section selection in read_note

When `section` is supplied, `read_note` SHALL return only the named section — the matched ATX heading line together with the content up to the next heading of equal-or-shallower depth, or end of note. The selector SHALL accept the same forms as the write tools: exact heading text, the path-style `Parent/Child` form, and the `#N` ordinal form.

A section response SHALL be subject to the same response-size cap, and `offset` SHALL window within the selected section rather than within the whole note.

An untruncated section response SHALL be exactly the heading line, its line
terminator, and the section's body as `edit_note(section=…)` defines it —
nothing more and nothing less. There SHALL be no region of a note that a
section read returns but a section write to the same selector cannot replace:
whitespace, blank lines, and fenced code blocks between the heading line and
the next heading of equal-or-shallower depth are part of the body on both
sides. Consequently, stripping the first line from an untruncated section
response and passing the remainder back as `edit_note(path, content=<remainder>,
section=<same selector>)` SHALL leave an LF-bodied note byte-identical.

#### Scenario: Reading one section of a large note

- **WHEN** `read_note` is invoked with a `section` that is within the cap, on a note that is not
- **THEN** the response SHALL contain that section's heading and body
- **AND** SHALL NOT contain content from other sections
- **AND** SHALL NOT be truncated

#### Scenario: A section read returns nothing a section write cannot replace

- **WHEN** `read_note(path, section=<sel>)` returns an untruncated section
  whose body begins with a blank line, or with a fenced code block, or both
- **THEN** every byte of the response after its first line SHALL fall inside
  the span `edit_note(path, content, section=<sel>)` replaces
- **AND** writing that remainder back SHALL reproduce the note exactly

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
