## MODIFIED Requirements

### Requirement: Section mode replaces the body under a named heading

When `section=<heading>` is supplied, `edit_note` SHALL locate the matching
ATX heading (1–6 `#` characters) and SHALL replace the lines between that
heading and the next heading of equal-or-shallower depth (or end of file)
with the supplied `content`. The matched heading line itself SHALL NOT be
removed or rewritten.

The selector SHALL accept three forms, resolved in this order:

1. **Exact heading text** (e.g. `Tasks`).
2. **Path-style chain** (e.g. `Parent/Child`), where the final part is the
   target heading and the preceding parts are ancestors in outermost-first
   order.
3. **Ordinal** (e.g. `#7`), selecting the Nth ATX heading in document order,
   1-based. The ordinal form SHALL be attempted only when no heading's text
   matches the selector exactly, so a heading literally titled `#7` continues
   to resolve to itself.

The ordinal form exists because the path-style form cannot separate duplicate
**sibling** headings — headings with identical text under the same parent share
every ancestor, so no chain distinguishes them.

The same selector grammar SHALL be used by every tool that accepts a `section`
argument, so a selector that names a section for reading names the same section
for writing.

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
- **THEN** that heading SHALL be selected by exact text match
- **AND** the second heading in the note SHALL NOT be selected
