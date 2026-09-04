## ADDED Requirements

### Requirement: The link rebuild writes and releases per note

During a pass, the link rows derived from a changed note SHALL be inserted before the next note is processed, and the buffered body of a note SHALL be released once its derived rows are written, so that peak memory for the link rebuild is bounded by one note's derived rows (at most `MAX_LINKS_PER_NOTE`) rather than by the number of changed notes in the pass. A note whose link count exceeds the cap SHALL be recorded as a declared degradation — one ERROR log line naming the path, the cap and the count — and SHALL NOT be appended to the pass's `skips`, so a re-derive that meets such a note is still certifiable.

#### Scenario: Many changed link-heavy notes in one pass

- **WHEN** a pass processes N changed notes each carrying the maximum number of links
- **THEN** the number of link rows held in memory at any instant SHALL NOT exceed one note's worth plus one insert batch, and every note's rows SHALL be present in `note_links` when the pass commits

#### Scenario: An over-cap note during a re-derive

- **WHEN** a provenance re-derive processes a note with more than `MAX_LINKS_PER_NOTE` links
- **THEN** the re-derive SHALL complete and be recorded (the note is not a skip), the first `MAX_LINKS_PER_NOTE` links SHALL be persisted, and the ERROR line SHALL be emitted

### Requirement: The frozen v0 cleaner is linear and byte-identical

The frozen v0 cleaning function retained for `extraction_version` comparison SHALL run in time linear in the input length, including inputs with many unclosed fence openers, and SHALL produce output byte-identical to the original regex-based implementation for every input. Equivalence SHALL be established by a differential test that keeps the original regexes as an oracle and compares outputs over generated inputs covering unclosed openers, orphan closers, nested and adjacent mixed backtick/tilde fences, trailing whitespace on closer lines, blank-line runs, CRLF line endings and indented fences.

#### Scenario: Many unclosed openers

- **WHEN** the v0 cleaner is invoked on 160 KB of ```` ```x\n ```` repeated
- **THEN** it SHALL return within a bounded time (the regression test asserts under 0.5 s) and its output SHALL equal the oracle's

#### Scenario: Differential equivalence

- **WHEN** the differential test generates inputs from the covered classes
- **THEN** for every input the scanner's output SHALL equal the oracle regex pair's output byte for byte
