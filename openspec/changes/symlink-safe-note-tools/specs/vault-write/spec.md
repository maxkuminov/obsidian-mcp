## ADDED Requirements

### Requirement: Mutating note tools act on the named path and refuse symlinked final components

`create_note`, `edit_note`, `set_frontmatter`, `move_note` (source and destination), and `delete_note` SHALL operate on the directory entry named by the path — the resolved parent directory (which MUST be inside the vault) joined with the final component as named — and SHALL refuse with an error naming the link's canonical vault-relative target when that final component is a symbolic link (including a dangling one). Symbolic-link directory components that resolve inside the vault SHALL remain permitted; the tools' database updates (`notes_metadata.file_path`, `note_links`, backlink discovery for `rewrite_links`) SHALL use the resolved vault-relative path, matching what the indexer stores for files under such directories. Read tools are unchanged and MAY follow links.

#### Scenario: Alias note is not retargeted

- **WHEN** `alias.md` is a symlink to `important.md` and any mutating note tool is invoked on `alias.md`
- **THEN** the tool SHALL return an error naming `important.md`
- **AND** `important.md` and the link SHALL be byte-identical afterwards

#### Scenario: Symlinked folder inside the vault still works

- **WHEN** `Shared/` is a symlink to `Real/` inside the vault and `create_note("Shared/new.md", …)` is invoked
- **THEN** the note SHALL be created in `Real/`

#### Scenario: Move through a symlinked folder keeps the index consistent

- **WHEN** `Real/A.md` is indexed, `Shared -> Real`, and `move_note("Shared/A.md", "Shared/B.md", rewrite_links=True)` is invoked
- **THEN** the file SHALL move to `Real/B.md`, `notes_metadata.file_path` and `note_links` SHALL be updated for `Real/A.md` → `Real/B.md`, and backlinks to `A` SHALL be rewritten

#### Scenario: Multi-user vault root

- **WHEN** the same alias case occurs under a per-user vault root
- **THEN** the refusal and the canonical target SHALL be computed relative to that user's root

#### Scenario: Dangling link at a destination

- **WHEN** `create_note` or `move_note` targets a path whose final component is a dangling symlink
- **THEN** the tool SHALL return an error and SHALL NOT write

#### Scenario: Escaping link still rejected

- **WHEN** a path component links outside the vault root
- **THEN** the existing traversal error SHALL be returned
