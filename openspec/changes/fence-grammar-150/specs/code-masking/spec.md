## ADDED Requirements

### Requirement: One shared masker defines what counts as code

The system SHALL provide a single shared code masker (`mask_code`) that replaces fenced and inline code with whitespace of identical byte length, and every scanner that must ignore code — ATX heading resolution for section addressing and outlines, wikilink/markdown-link extraction, inline tag extraction, and `move_note` link rewriting — SHALL consume this masker and no private variant.

#### Scenario: Masking preserves byte offsets

- **WHEN** `mask_code` masks any fenced or inline code span
- **THEN** the substitution SHALL be exactly as long as the bytes it replaces, so positions reported against masked text are valid against the original

#### Scenario: Read and write sides agree by construction

- **WHEN** a heading is hidden inside masked code
- **THEN** it SHALL be invisible to `read_note(section=…)`, `edit_note(section=…)`, and the truncation outline alike, because all three scan the same masked text

### Requirement: Fenced code is recognised per an explicit CommonMark subset

The masker SHALL recognise a fenced code block exactly when CommonMark's fenced-code rules do, restricted as follows. An opening fence is a line consisting of 0–3 spaces of indentation, a run of at least three backticks or at least three tildes, and an optional info string; a backtick fence's info string SHALL NOT contain a backtick. A closing fence is a line consisting of 0–3 spaces of indentation, a run of the same fence character at least as long as the opening run, and nothing but horizontal whitespace after it. The masked span SHALL run from the first byte of the opening fence line through the terminator of the closing fence line. Fence lines SHALL be delimited by LF, CRLF, or a lone CR, consistently with the universal-newline rule the read path applies.

#### Scenario: Indented fences are recognised

- **WHEN** a note contains an opening or closing fence indented by one to three spaces (e.g. `# A\n   ```\n# Hidden\ntext\n   ```\n# B\n`)
- **THEN** the block SHALL be masked and `# Hidden` SHALL NOT be a heading

#### Scenario: A longer closer closes the block

- **WHEN** a block opened with ``` ``` ``` is closed by a run of four or more backticks (e.g. `# A\n```\n# Hidden\n````\n# B\n`)
- **THEN** the block SHALL be masked through the longer closing fence line

#### Scenario: Tilde fences are recognised

- **WHEN** a note contains a block fenced by `~~~` (closed by three or more tildes)
- **THEN** the block SHALL be masked under the same rules as backtick fences

#### Scenario: A shorter run or the other fence character does not close

- **WHEN** a block opened with four backticks is followed by a three-backtick line, or a backtick block is followed by a tilde line (or vice versa)
- **THEN** those lines SHALL be content of the still-open block, not closers

#### Scenario: An unterminated fence masks to end of note

- **WHEN** an opening fence is never closed
- **THEN** everything from the opening fence line to the end of the note SHALL be masked, so no heading, link, or tag below it is recognised

#### Scenario: A backtick info string containing a backtick is not an opener

- **WHEN** a line is ```` ```code``` ```` or otherwise carries a backtick in a backtick fence's info string
- **THEN** that line SHALL NOT open a fenced block

#### Scenario: A fence spanning a heading boundary masks across it

- **WHEN** a fence opens under one heading and closes under text that would otherwise be a later heading's section
- **THEN** the whole span SHALL be masked, and any `#`-prefixed line inside it SHALL NOT be a heading, so the block cannot be split by section addressing

#### Scenario: Four-space indentation is not a fence

- **WHEN** a fence-looking run is indented by four or more spaces
- **THEN** it SHALL NOT open or close a fenced block (indented code blocks are a documented divergence: they are not masked)

### Requirement: Inline code masking is a documented single-line approximation

The masker SHALL mask inline code as backtick-delimited runs that do not span a line terminator. This is a documented divergence from CommonMark's equal-length-run pairing rule.

#### Scenario: Inline code cannot span lines

- **WHEN** a backtick run is not closed before the next line terminator
- **THEN** no inline mask SHALL be applied across that terminator
