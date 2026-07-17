## ADDED Requirements

### Requirement: No-clobber mutations are race-safe
`create_note` and `move_note` SHALL atomically fail if another actor creates the destination at any time before the operation commits. They MUST NOT implement no-clobber solely as an existence check followed by a replacing rename.

#### Scenario: Destination appears during create
- **WHEN** another actor creates the destination after validation but before `create_note` commits
- **THEN** `create_note` SHALL report that the destination exists
- **AND** SHALL leave the other actor's content unchanged

#### Scenario: Destination appears during move
- **WHEN** another actor creates the destination after validation but before `move_note` commits
- **THEN** `move_note` SHALL fail without replacing the destination
- **AND** the source note SHALL remain available

### Requirement: Note read-modify-write operations detect conflicts
`edit_note`, `set_frontmatter`, and backlink body rewrites SHALL reject a mutation when the on-disk content differs from the content on which the new result was computed.

#### Scenario: External edit occurs concurrently
- **WHEN** Obsidian changes a note after the server reads it but before the server replaces it
- **THEN** the server SHALL return a conflict
- **AND** SHALL NOT overwrite the newer external content

### Requirement: Note tools reject hidden vault paths
All note mutation tools SHALL reject a source or destination containing a dot-prefixed path component, including `.obsidian` and `.trash`, except for the server's internal soft-delete destination handling.

#### Scenario: Hidden configuration mutation attempted
- **WHEN** a note mutation targets `.obsidian/plugins/example.md`
- **THEN** the operation SHALL return a validation error
- **AND** SHALL NOT modify the hidden file

### Requirement: Link rewrites preserve source-relative meaning
When `move_note(rewrite_links=true)` rewrites Markdown links, each resulting href SHALL resolve from the source note to the moved target. A moved note that links to itself SHALL be rewritten at its new path.

#### Scenario: Markdown source and target are in different folders
- **WHEN** `Folder/source.md` links to a target moved to `Archive/target.md`
- **THEN** the rewritten Markdown href SHALL resolve from `Folder/source.md` to `Archive/target.md`

#### Scenario: Moved note contains a self-link
- **WHEN** the moved note itself links to its old path and rewriting is enabled
- **THEN** its body at the destination SHALL link to the new path

