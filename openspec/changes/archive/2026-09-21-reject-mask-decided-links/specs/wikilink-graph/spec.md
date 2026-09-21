## ADDED Requirements

### Requirement: Code masking must not invent link targets

The extractor SHALL omit a candidate when code masking changes any character
in its deciding wikilink target or markdown href span, before resolution and
before counting it toward the per-note link cap.

#### Scenario: Inline code inside a target
- **WHEN** a note contains [[`x`Old]], ![[`x`Old]] or [t](`x`Old.md)
- **THEN** extraction SHALL emit no link for that candidate
- **AND** the candidate SHALL neither consume the cap nor set truncation

#### Scenario: Code outside the deciding span
- **WHEN** an otherwise valid link has inline code only in its label, alias or anchor
- **THEN** its unchanged target SHALL still be extracted

#### Scenario: Existing indexed notes are repaired
- **WHEN** a version-2 note with unchanged bytes is scanned by this build
- **THEN** its links SHALL be re-derived and its extraction version stamped 3 in the existing atomic pass
- **AND** its embedding certification SHALL remain unchanged solely on account of this link-only version bump

#### Scenario: Ordinary links retain their behavior
- **WHEN** extraction processes unchanged targets with whitespace, Unicode, percent encoding or balanced parentheses supported by the existing grammar
- **THEN** their resolution inputs and bounded document ordering SHALL remain unchanged
