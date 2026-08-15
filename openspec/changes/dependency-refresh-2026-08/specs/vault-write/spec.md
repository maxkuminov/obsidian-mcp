## ADDED Requirements

### Requirement: Note write tools bound the resulting note size

`create_note`, `edit_note`, `set_frontmatter`, and the link-rewriting path of `move_note(rewrite_links=True)` SHALL refuse to produce a note whose UTF-8 encoded content exceeds `MAX_NOTE_BYTES` (10 MiB). The check SHALL be applied to the content that would be written (after the edit, frontmatter mutation, or link rewrite is computed), SHALL return a tool-level error that names the limit, and SHALL NOT write that file. Under `edit_note(dry_run=True)` the same error SHALL be reported instead of a diff. For `move_note`, an over-cap rewrite of a source note SHALL leave that source byte-identical and SHALL be reported in the tool result; the move itself and the other rewrites proceed. Every note-writing path therefore has a tool-level size cap strictly below the MCP transport body limit, so a supported write is never rejected only by the transport.

#### Scenario: edit_note result over the cap

- **WHEN** `edit_note` is invoked in any mode such that the resulting note would exceed `MAX_NOTE_BYTES`
- **THEN** the tool SHALL return an error naming `MAX_NOTE_BYTES`
- **AND** the note on disk SHALL be unchanged

#### Scenario: set_frontmatter result over the cap

- **WHEN** `set_frontmatter` is invoked with updates whose serialized result would push the note over `MAX_NOTE_BYTES`
- **THEN** the tool SHALL return an error naming `MAX_NOTE_BYTES`
- **AND** the note on disk SHALL be unchanged

#### Scenario: move_note link rewrite over the cap

- **WHEN** `move_note(rewrite_links=True)` would expand a source note's links such that the rewritten note exceeds `MAX_NOTE_BYTES`
- **THEN** that source SHALL remain byte-identical, the tool result SHALL name the skipped source and the limit, and the `expected=` conflict guard SHALL still apply to the sources that are rewritten

#### Scenario: Result at the cap is accepted

- **WHEN** an `edit_note` or `set_frontmatter` call produces a note of exactly `MAX_NOTE_BYTES` bytes
- **THEN** the write SHALL succeed
