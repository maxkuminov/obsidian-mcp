## ADDED Requirements

### Requirement: Note write tools bound the resulting note size

`create_note`, `edit_note`, and `set_frontmatter` SHALL refuse to produce a note whose UTF-8 encoded content exceeds `MAX_NOTE_BYTES` (10 MiB). The check SHALL be applied to the content that would be written (after the edit or frontmatter mutation is computed), SHALL return a tool-level error that names the limit, and SHALL NOT write any file. Under `edit_note(dry_run=True)` the same error SHALL be reported instead of a diff. Every write tool therefore has a tool-level size cap strictly below the MCP transport body limit, so a supported write is never rejected only by the transport.

#### Scenario: edit_note result over the cap

- **WHEN** `edit_note` is invoked in any mode such that the resulting note would exceed `MAX_NOTE_BYTES`
- **THEN** the tool SHALL return an error naming `MAX_NOTE_BYTES`
- **AND** the note on disk SHALL be unchanged

#### Scenario: set_frontmatter result over the cap

- **WHEN** `set_frontmatter` is invoked with updates whose serialized result would push the note over `MAX_NOTE_BYTES`
- **THEN** the tool SHALL return an error naming `MAX_NOTE_BYTES`
- **AND** the note on disk SHALL be unchanged

#### Scenario: Result at the cap is accepted

- **WHEN** an `edit_note` or `set_frontmatter` call produces a note of exactly `MAX_NOTE_BYTES` bytes
- **THEN** the write SHALL succeed
