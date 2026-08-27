## ADDED Requirements

### Requirement: One shared fence recognizer defines what counts as fenced code

The system SHALL provide a single shared fence recognizer whose recognised spans feed every consumer that must ignore or remove fenced code: the code masker (`mask_code`) consumed by ATX heading resolution for section addressing and outlines, wikilink/markdown-link extraction, inline tag extraction, and `move_note` link rewriting — and the embedding cleaner (`clean_for_embedding`), which SHALL remove fenced blocks per the same grammar (while continuing to preserve inline code). No consumer SHALL carry a private fence grammar.

#### Scenario: Masking preserves length and positions

- **WHEN** `mask_code` masks any fenced or inline code span
- **THEN** the substitution SHALL have exactly the same code-point length as the text it replaces, so character positions reported against masked text are valid against the original — including for non-ASCII content

#### Scenario: Read and write sides agree by construction

- **WHEN** a heading is hidden inside masked code
- **THEN** it SHALL be invisible to `read_note(section=…)`, `edit_note(section=…)`, and the truncation outline alike, because all three scan the same masked text

#### Scenario: The embedding cleaner uses the shared grammar

- **WHEN** a note contains a fenced block recognised by the shared grammar (an indented or longer-closed fence included)
- **THEN** `clean_for_embedding` SHALL remove it before chunking, so newly recognised code does not dominate the note's vectors

### Requirement: Fenced code is recognised per an explicit CommonMark subset

The fence recognizer SHALL implement the following grammar, a CommonMark subset applied flat (container blocks — lists and blockquotes — are not parsed; the documented divergences are listed below). An opening fence is a line consisting of 0–3 spaces of indentation, a run of at least three backticks or at least three tildes, and an optional info string; a backtick fence's info string SHALL NOT contain a backtick. A closing fence is a line consisting of 0–3 spaces of indentation, a run of the same fence character at least as long as the opening run, followed by nothing except U+0020 SPACE and U+0009 TAB characters. A recognised block's masked span SHALL run from the first character of the opening fence line through the last character of the closing fence line, **excluding the closing line's terminator**, so the line boundary before any following heading survives masking; line terminators inside the span are masked like any other character. Fence lines SHALL be delimited by LF, CRLF as a unit, or a lone CR, consistently with the universal-newline rule the read path applies.

Unterminated openers split by indentation: a **column-zero** opener with no closer SHALL be recognised, its span running to the end of the note (at the top level of a document, CommonMark closes the block at end of input); an **indented** (1–3 space) opener with no closer SHALL NOT be recognised as a fence at all, because flat scanning cannot know the enclosing container's extent, and fabricating an end-of-note extent would let one stray line swallow every later section. The safety consequence for writes on such notes is specified in `vault-write`.

#### Scenario: Indented fences are recognised

- **WHEN** a note contains an opening or closing fence indented by one to three spaces (e.g. `# A\n   ```\n# Hidden\ntext\n   ```\n# B\n`)
- **THEN** the block SHALL be masked and `# Hidden` SHALL NOT be a heading

#### Scenario: A longer closer closes the block

- **WHEN** a block opened with ``` ``` ``` is closed by a run of four or more backticks (e.g. `# A\n```\n# Hidden\n````\n# B\n`)
- **THEN** the block SHALL be masked through the longer closing fence line

#### Scenario: A heading immediately after the closer survives, in every dialect

- **WHEN** a masked block's closing fence line is immediately followed by an ATX heading, with the note terminated by LF, CRLF, or lone CR
- **THEN** the closing line's terminator SHALL NOT be masked, and the following heading SHALL keep its name and ordinal

#### Scenario: Tilde fences are recognised

- **WHEN** a note contains a block fenced by `~~~` (closed by three or more tildes)
- **THEN** the block SHALL be masked under the same rules as backtick fences

#### Scenario: A shorter run or the other fence character does not close

- **WHEN** a block opened with four backticks is followed by a three-backtick line, or a backtick block is followed by a tilde line (or vice versa)
- **THEN** those lines SHALL be content of the still-open block, not closers

#### Scenario: An unterminated column-zero fence masks to end of note

- **WHEN** a column-zero opening fence is never closed
- **THEN** everything from the opening fence line to the end of the note SHALL be masked, so no heading, link, or tag below it is recognised

#### Scenario: An unterminated indented opener is not a fence

- **WHEN** an opening fence indented by one to three spaces has no closing fence anywhere below it (e.g. `# A\n- item\n  ```\n  code\n\n# B\nkeep\n`)
- **THEN** the recognizer SHALL NOT treat it as opening a block — `# B` SHALL remain a heading — and SHALL surface the unmatched opener (character position included) to callers that need to refuse

#### Scenario: A closer followed by non-space/tab whitespace does not close

- **WHEN** a would-be closing fence line carries a character other than U+0020 or U+0009 after its run (e.g. an NBSP)
- **THEN** that line SHALL NOT close the block

#### Scenario: A backtick info string containing a backtick is not an opener

- **WHEN** a line is ```` ```code``` ```` or otherwise carries a backtick in a backtick fence's info string
- **THEN** that line SHALL NOT open a fenced block (a tilde fence's info string is unrestricted)

#### Scenario: A fence spanning a heading boundary masks across it

- **WHEN** a fence opens under one heading and closes under text that would otherwise be a later heading's section
- **THEN** the whole span SHALL be masked, and any `#`-prefixed line inside it SHALL NOT be a heading, so the block cannot be split by section addressing

#### Scenario: Four-space indentation is not a fence

- **WHEN** a fence-looking run is indented by four or more spaces
- **THEN** it SHALL NOT open or close a fenced block (indented code blocks are a documented divergence: they are not masked)

### Requirement: Fence state does not cross the frontmatter boundary

When a note begins with a valid line-1 frontmatter block (as recognised by the shared frontmatter partition), the fence recognizer SHALL treat that block as opaque: no line inside it opens or closes a fence, and fence scanning begins at the first line after the block. On a note whose frontmatter is absent or defective, the whole raw text is scanned.

#### Scenario: A fence-shaped YAML scalar does not swallow the body

- **WHEN** a note is `---\nliteral: |\n   ```\n---\n#real\n[[Old]]\n`
- **THEN** the indented fence-shaped line inside the valid frontmatter block SHALL NOT open a block, `#real` SHALL be extracted as a tag, and `[[Old]]` SHALL be extracted (and rewritten by `move_note(rewrite_links=True)`) normally

### Requirement: Documented divergences from CommonMark

The following divergences SHALL hold and be documented where the grammar is recorded: container blocks are not parsed, so a matched fence's extent is computed flat even when its opener sits inside a list item; 4+-space indented code blocks are not masked; ATX headings remain column-zero only; inline code masking is a single-line backtick-delimited approximation of CommonMark's equal-length-run pairing.

#### Scenario: Inline code cannot span lines

- **WHEN** a backtick run is not closed before the next line terminator
- **THEN** no inline mask SHALL be applied across that terminator
