## ADDED Requirements

### Requirement: Note mutations are anchored to the parent directory opened at validation

Every mutating note tool SHALL open the validated parent directory as a descriptor before it acts, and SHALL perform every subsequent filesystem operation of that call — temporary-file creation, the pre-publication read, publication, permanent deletion, and the soft-delete rename — relative to that descriptor. The applicable tools are `create_note`, `edit_note`, `move_note`, `delete_note`, `set_frontmatter`, and `write_file`. After validation, no pathname SHALL be resolved again by the kernel for that call.

The parent descriptor SHALL be obtained by walking the *resolved* parent path from an open vault-root descriptor, one component at a time, refusing to follow a symbolic link at any component. Symbolic-link directory components that resolve inside the vault therefore remain permitted (they are resolved before the walk); a component that is a symbolic link at walk time SHALL be refused.

A missing parent directory SHALL NOT be created during validation. It SHALL be created on first use of the descriptor by a write, so a call refused for an unrelated reason leaves no directories behind, and reads SHALL NOT create it at all.

#### Scenario: The validated parent is renamed and a symlink left at its name

- **WHEN** a mutating note tool has validated `Folder/note.md`, and before the tool publishes, another process renames `Folder` away and creates a symbolic link named `Folder` pointing at a different in-vault directory that holds a byte-identical `note.md`
- **THEN** the mutation SHALL take effect in the directory that was validated, under its new name
- **AND** the note in the directory the link now points at SHALL be unchanged
- **AND** the tool SHALL report success for the path the caller named

#### Scenario: The directory behind a symlinked vault root is renamed mid-call

- **WHEN** the configured vault root is a symbolic link, a mutating note tool has validated a path under it, and the directory the link points at is renamed away and replaced by a link to a different directory before the tool publishes
- **THEN** the mutation SHALL take effect in the directory that was validated
- **AND** the substituted directory SHALL be unchanged

#### Scenario: A soft delete's trash destination is anchored to the same root

- **WHEN** the vault root's target is substituted between validation and the soft delete of a note
- **THEN** the note SHALL be moved into the `.trash` directory of the root that was validated
- **AND** no `.trash` directory SHALL be created in the substituted directory

#### Scenario: A leaf that becomes a symbolic link after validation is named as one

- **WHEN** the final component of a validated path is replaced by a symbolic link before the tool reads or writes it
- **THEN** the tool SHALL return an error identifying the path as a symbolic link
- **AND** SHALL NOT report the note as missing
- **AND** SHALL NOT follow the link or modify anything

#### Scenario: A refused write creates no directories

- **WHEN** a note write names a path whose parent directory does not exist and the call is then refused before writing (for example because the resulting note would exceed the size cap)
- **THEN** no directory SHALL have been created

## MODIFIED Requirements

### Requirement: Atomic write invariant

The system SHALL perform all file writes from MCP write tools via a temporary file created in the same directory as the destination, whose contents are flushed to durable storage before publication, followed by an atomic same-directory rename (overwrite) or hard link (no-clobber) relative to the destination's directory descriptor. The applicable tools are `create_note`, `edit_note`, `move_note`, `delete_note`, and `set_frontmatter`. Direct writes that could leave the destination truncated on crash SHALL NOT be used, and the temporary file SHALL be created with exclusive, non-symlink-following semantics so a pre-created name cannot be written through.

#### Scenario: Crash mid-write does not truncate the destination

- **WHEN** the server process is killed between the tmp-file write and the
  publication
- **THEN** the destination file SHALL retain its prior content unchanged
- **AND** the orphaned `.tmp-*` file SHALL be discoverable for cleanup by
  the next reindex (it lives in a dot-prefixed name, so the indexer
  ignores it)

#### Scenario: Crash immediately after publication does not publish empty content

- **WHEN** the payload has been written to the temporary file and the system
  loses power immediately after the publishing rename
- **THEN** the destination SHALL hold either the full prior content or the full
  new content, because the payload was flushed to durable storage before the
  rename was issued

#### Scenario: Successful write atomically replaces existing content

- **WHEN** `edit_note` is called with new content and succeeds
- **THEN** any reader observing the destination path SHALL see either the
  full prior content or the full new content, never a partial mix

#### Scenario: Staging happens in the destination directory

- **WHEN** any note or file write stages its payload
- **THEN** the temporary file SHALL be created in the destination's own
  directory, so publication is a same-directory operation
- **AND** the temporary file SHALL be removed whether the write succeeds or
  fails

### Requirement: No-clobber mutations are race-safe

`create_note`, `write_file` (without `overwrite`) and `move_note` SHALL atomically fail if another actor creates the destination at any time before the operation commits. They MUST NOT implement no-clobber as an existence check followed by a replacing rename. `create_note` and `write_file` SHALL publish by hard-linking the staged temporary file, and `move_note` SHALL publish with a non-replacing rename, so that the destination is either created by the operation or the operation fails. When the vault filesystem cannot provide the required primitive, the tool SHALL refuse with an error naming the unsupported capability and SHALL NOT fall back to an operation that can replace an existing file.

#### Scenario: Destination appears during create
- **WHEN** another actor creates the destination after validation but before `create_note` commits
- **THEN** `create_note` SHALL report that the destination exists
- **AND** SHALL leave the other actor's content unchanged

#### Scenario: Destination appears during move
- **WHEN** another actor creates the destination after validation but before `move_note` commits
- **THEN** `move_note` SHALL fail without replacing the destination
- **AND** the source note SHALL remain available

#### Scenario: The source is replaced during a move

- **WHEN** another actor replaces the file at `from_path` with a different file after validation but before `move_note` commits
- **THEN** whichever file is at `from_path` when the move executes SHALL be relocated intact
- **AND** no file SHALL be unlinked that was not the one moved

#### Scenario: Hard links unavailable

- **WHEN** the vault filesystem refuses a hard link inside the vault root and a no-clobber note or file write is attempted
- **THEN** the tool SHALL return an error naming hard links as the unsupported capability
- **AND** any existing file at the destination SHALL be unchanged

### Requirement: `delete_note` soft-deletes to `.trash/` by default

The MCP server SHALL expose a tool `delete_note(path: str, permanent: bool = False) -> str`. With `permanent=False` (default), the tool SHALL move the note into `.trash/` inside the vault root under a name of the form `<YYYYMMDD-HHMMSS>-<original-basename>-<random suffix>`, creating `.trash/` if needed, using a single non-replacing rename so that an existing or concurrently created trash entry is never overwritten. With `permanent=True`, the tool SHALL unlink the file directly. In both cases the operation SHALL run relative to the parent directory descriptor opened at validation and the trash directory SHALL be resolved from the same vault-root descriptor. In both cases the response SHALL identify what happened and where the file went (or that it was permanently deleted). When the vault filesystem cannot perform a non-replacing rename into `.trash/`, the soft delete SHALL be refused with an error that names the limitation and points at `permanent=True`.

#### Scenario: Soft-delete moves the file under `.trash/`

- **WHEN** the client calls `delete_note(path="Cards/Old.md")`
- **THEN** the file SHALL be moved to a path under `.trash/` whose name begins
  with a timestamp and contains the original basename
- **AND** the response SHALL include the trash path

#### Scenario: Soft-delete is invisible to search

- **WHEN** a soft-deleted note has been moved into `.trash/` and the
  next reindex pass completes
- **THEN** the row in `notes_metadata` for that note SHALL be removed
- **AND** the dependent `note_embeddings` and `note_links` rows SHALL be
  cleaned up via existing FK cascades

#### Scenario: Permanent delete removes the file outright

- **WHEN** the client calls `delete_note(path="Cards/Old.md", permanent=True)`
- **THEN** the file SHALL be unlinked relative to the validated parent
  directory
- **AND** the response SHALL state that the file was permanently deleted

#### Scenario: Trash collisions are disambiguated

- **WHEN** the same note path is soft-deleted twice within the same second
- **THEN** each delete SHALL produce a distinct `.trash/` entry
- **AND** neither entry SHALL have overwritten the other

#### Scenario: Missing note returns an actionable error

- **WHEN** the client calls `delete_note` on a non-existent path
- **THEN** the response SHALL state that the note does not exist
- **AND** SHALL NOT create a `.trash/` directory

#### Scenario: Non-replacing rename unavailable

- **WHEN** the vault filesystem or kernel cannot perform a non-replacing rename into `.trash/`
- **THEN** `delete_note(permanent=False)` SHALL return an error naming the limitation
- **AND** the note SHALL remain at its original path

### Requirement: Mutating note tools act on the named path and refuse symlinked final components

`create_note`, `edit_note`, `set_frontmatter`, `move_note` (source and destination), `delete_note` and `write_file` SHALL operate on the directory entry named by the path — the resolved parent directory (which MUST be inside the vault) joined with the final component as named — and SHALL refuse with an error naming the link's canonical vault-relative target when that final component is a symbolic link (including a dangling one). Symbolic-link directory components that resolve inside the vault SHALL remain permitted; the tools' database updates (`notes_metadata.file_path`, `note_links`, backlink discovery for `rewrite_links`) SHALL use the resolved vault-relative path, matching what the indexer stores for files under such directories. Read tools are unchanged and MAY follow links.

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

#### Scenario: An ancestor repointed mid-mutation cannot redirect the write

- **WHEN** a tool reads a note through a symlinked ancestor directory and that ancestor is repointed at a different directory before the write is published
- **THEN** the write SHALL land in the directory that was validated at the start of the call
- **AND** the directory the link now points at SHALL be unchanged, even when it holds a byte-identical copy of the note

#### Scenario: Soft delete through a symlinked folder

- **WHEN** `Shared -> Real` and `delete_note("Shared/note.md")` is invoked
- **THEN** `Real/note.md` SHALL be moved into `.trash/` and the response SHALL include the trash path

#### Scenario: Multi-user vault root

- **WHEN** the same alias case occurs under a per-user vault root
- **THEN** the refusal and the canonical target SHALL be computed relative to that user's root

#### Scenario: Dangling link at a destination

- **WHEN** `create_note` or `move_note` targets a path whose final component is a dangling symlink
- **THEN** the tool SHALL return an error and SHALL NOT write

#### Scenario: Escaping link still rejected

- **WHEN** a path component links outside the vault root
- **THEN** the existing traversal error SHALL be returned
