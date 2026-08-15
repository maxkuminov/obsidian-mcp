## ADDED Requirements

### Requirement: Mutating note tools act on the named path and refuse symlinked final components

`create_note`, `edit_note`, `set_frontmatter`, `move_note` (source and destination), and `delete_note` SHALL operate on the vault path exactly as named (lexically normalised, containment-checked on its resolved form) and SHALL refuse with an error naming the link target when the final component of that path is a symbolic link (including a dangling one). Symbolic-link directory components that resolve inside the vault SHALL remain permitted. Read tools are unchanged and MAY follow links.

#### Scenario: Alias note is not retargeted

- **WHEN** `alias.md` is a symlink to `important.md` and any mutating note tool is invoked on `alias.md`
- **THEN** the tool SHALL return an error naming `important.md`
- **AND** `important.md` and the link SHALL be byte-identical afterwards

#### Scenario: Symlinked folder inside the vault still works

- **WHEN** `Shared/` is a symlink to a directory inside the vault and `create_note("Shared/new.md", …)` is invoked
- **THEN** the note SHALL be created in the linked directory

#### Scenario: Dangling link at a destination

- **WHEN** `create_note` or `move_note` targets a path whose final component is a dangling symlink
- **THEN** the tool SHALL return an error and SHALL NOT write

#### Scenario: Escaping link still rejected

- **WHEN** a path component links outside the vault root
- **THEN** the existing traversal error SHALL be returned
