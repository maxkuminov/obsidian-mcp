## MODIFIED Requirements

### Requirement: Note mutations are anchored to the parent directory opened at validation

Every mutating note tool SHALL open the validated parent directory as a descriptor before it acts, and SHALL perform every subsequent filesystem operation of that call — temporary-file creation, the pre-publication read, publication, permanent deletion, and the soft-delete rename — relative to that descriptor. The applicable tools are `create_note`, `edit_note`, `move_note`, `delete_note`, `set_frontmatter`, and `write_file`. After validation, no pathname SHALL be resolved again by the kernel for that call.

The parent descriptor SHALL be obtained from the *resolved* parent path by a **single kernel-enforced beneath-root lookup** from an open vault-root descriptor, which refuses to follow a symbolic link at any component and refuses any resolution that would leave the root. It SHALL NOT be obtained by opening one component at a time: an ancestor renamed out of the vault between two such opens yields a parent descriptor outside the root, and every mutation anchored to it then lands outside the vault while the tool reports success for the path the caller named. Symbolic-link directory components that resolve inside the vault therefore remain permitted (they are resolved before the lookup); a component that is a symbolic link at lookup time SHALL be refused.

A missing parent directory SHALL NOT be created during validation. It SHALL be created on first use of the descriptor by a write, so a call refused for an unrelated reason leaves no directories behind, and reads SHALL NOT create it at all. Each missing directory SHALL be created through a descriptor obtained by a fresh beneath-root lookup of the prefix that already exists, and the descriptor the write then acts through SHALL come from a fresh beneath-root lookup of the whole parent path performed after the creation, not from the creation itself.

Directory creation keeps a bounded residual that SHALL be stated rather than claimed closed: there is no beneath-root form of directory creation, so a prefix renamed out of the vault between its lookup and the single creation issued through it yields an empty directory outside the root. No note, no note content and no file of any kind is created, read or modified outside the root by such a call — the descriptor the write acts through is one the kernel proved beneath the root — and the tool never reports success for a mutation that landed outside it.

When the kernel or the container cannot perform a beneath-root lookup, the mutation SHALL be refused with an error naming the unsupported capability, and SHALL NOT fall back to a per-component walk.

#### Scenario: An ancestor is renamed out of the vault while the parent is being resolved

- **WHEN** a mutating note tool is resolving the parent of `A/B/note.md` and another process renames `<vault>/A` to a directory outside the vault root during that resolution
- **THEN** the tool SHALL either anchor to a directory beneath the vault root or refuse
- **AND** SHALL NOT create, read or modify anything outside the vault root
- **AND** SHALL NOT report success for a mutation that landed outside the root

#### Scenario: A parent created on first use is re-looked-up before it is written through

- **WHEN** `create_note("New/Folder/x.md", …)` creates the missing parent directories and then writes
- **THEN** the descriptor the write acts through SHALL be the result of a beneath-root lookup performed after those directories were created
- **AND** each of those directories SHALL have been created through a descriptor obtained by a fresh beneath-root lookup of the prefix that already existed
- **AND** the note SHALL be created inside the vault root

#### Scenario: An ancestor is renamed out of the vault while missing parents are created

- **WHEN** `create_note("A/B/C/x.md", …)` is creating the missing directories and another process renames `<vault>/A` outside the root during that creation
- **THEN** the note SHALL be written beneath the vault root or the call SHALL be refused
- **AND** no note and no file content SHALL be created, read or modified outside the root
- **AND** the tool SHALL NOT report success for a write that landed outside the root

#### Scenario: The beneath-root lookup is unavailable

- **WHEN** a mutating note tool runs where the kernel or the container cannot perform a beneath-root lookup
- **THEN** the tool SHALL return an error naming the unsupported capability
- **AND** SHALL NOT walk the path one component at a time instead
- **AND** nothing SHALL be written

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

#### Scenario: A creating tool refused by the swapped leaf says why

- **WHEN** `create_note`, `write_file`, or the destination of `move_note` names a path that is absent at validation and holds a symbolic link by the time the tool publishes
- **THEN** the tool SHALL return an error identifying the path as a symbolic link, rather than a bare "already exists" message
- **AND** `write_file(overwrite=True)` SHALL NOT replace the link and report a successful write
- **AND** the link and the file it points at SHALL both be unchanged

#### Scenario: A move that would pin more descriptors than the process can spare

- **WHEN** `move_note(rewrite_links=True)` plans more link rewrites than the running process can hold open parent descriptors for
- **THEN** the move SHALL be aborted before any mutation: the note stays at its original path, no source is rewritten, and `notes_metadata`/`note_links` are unchanged
- **AND** the error SHALL name the limit and suggest moving without `rewrite_links`

#### Scenario: The vault root is substituted between resolution and anchoring

- **WHEN** the directory the configured vault root resolves to is renamed away and replaced by a link to another directory while a path is being validated
- **THEN** the operation SHALL be refused
- **AND** neither the original directory's copy of the note nor the substituted directory's copy SHALL be modified

#### Scenario: A backlink source needing no rewrite holds nothing open

- **WHEN** `move_note(rewrite_links=True)` considers a backlink source that is missing, unreadable, or contains no link this move rewrites
- **THEN** that source's descriptor SHALL be released immediately
- **AND** the descriptors held at any point SHALL be bounded by the number of rewrites actually planned, not by the number of sources considered

#### Scenario: A refused write creates no directories

- **WHEN** a note write names a path whose parent directory does not exist and the call is then refused before writing (for example because the resulting note would exceed the size cap)
- **THEN** no directory SHALL have been created

### Requirement: Atomic write invariant

The system SHALL perform all file writes from MCP write tools by staging the payload in the destination's own directory, flushing it to durable storage before publication, publishing it with an atomic same-directory rename (overwrite) or hard link (no-clobber) relative to the destination's directory descriptor, and flushing that directory once the publication has happened. The applicable tools are `create_note`, `edit_note`, `move_note`, `delete_note`, `set_frontmatter`, and `write_file`. Payload and directory durability are properties of the shared atomic-write helper, so **every** caller of it inherits them, including `write_file` in both its no-clobber and its `overwrite=True` mode; an implementation SHALL NOT satisfy this requirement for the note tools while omitting the flush for a raw-byte write that goes through the same helper. Direct writes that could leave the destination truncated on crash SHALL NOT be used. Where staging carries a name — the overwrite path, whose replacing rename has no by-descriptor form — that name SHALL be created with exclusive, non-symlink-following semantics so a pre-created name cannot be written through; the no-clobber path SHALL have no name at all.

The destination directory SHALL be flushed **after** the publishing rename or link, so that the directory entry the write created or replaced is durable and not only its contents. When the call created parent directories on its way to the destination, each created directory's parent SHALL be flushed as well, outward to the first directory that already existed — flushing only the immediate parent leaves the entry that names *it* unflushed, so a crash can lose the whole new folder and with it a note the tool reported written. A failure of the destination-directory flush, or of any of those ancestor flushes, SHALL be logged and SHALL NOT turn a write that already landed into a reported failure: the payload was already durable, the previous content survives either way, and a note tool that reports a false failure is retried — `edit_note(append=True)` retried after a write that landed appends the same block twice. This is deliberately the opposite failure direction from the transfer path, where the source bytes are gone and the ambiguity must be surfaced instead.

#### Scenario: Crash mid-write of an overwrite does not truncate the destination

- **WHEN** the server process is killed between the staging write and the
  publication of an **overwrite** write, whose staging carries a name
- **THEN** the destination file SHALL retain its prior content unchanged
- **AND** the orphaned `.tmp-*` file SHALL be discoverable for cleanup by
  the next reindex (it lives in a dot-prefixed name, so the indexer
  ignores it)

#### Scenario: Crash mid-write of a no-clobber write leaves nothing behind

- **WHEN** the server process is killed between the staging write and the
  publication of a **no-clobber** write, whose staging has no directory entry
- **THEN** nothing SHALL exist at the destination path
- **AND** no `.tmp-*` entry or any other directory entry SHALL be left in the
  destination directory for a sweep or a reindex to find, because the unnamed
  inode is reclaimed when the last descriptor closes

#### Scenario: Crash immediately after publication does not publish empty content

- **WHEN** the payload has been written to the temporary file and the system
  loses power immediately after the publishing rename
- **THEN** the destination SHALL hold either the full prior content or the full
  new content, because the payload was flushed to durable storage before the
  rename was issued

#### Scenario: The publishing rename is made durable

- **WHEN** a note write publishes its payload
- **THEN** the destination directory SHALL be flushed after the rename or link and before the tool returns

#### Scenario: A failed directory flush does not report a landed write as failed

- **WHEN** the destination directory's flush fails after the payload has been published
- **THEN** the tool SHALL report the write as successful
- **AND** the failure SHALL be logged
- **AND** the tool SHALL NOT return an error that would invite the caller to retry the write

#### Scenario: Successful write atomically replaces existing content

- **WHEN** `edit_note` is called with new content and succeeds
- **THEN** any reader observing the destination path SHALL see either the
  full prior content or the full new content, never a partial mix

#### Scenario: A no-clobber write exposes no staging name

- **WHEN** `create_note` or `write_file` (without `overwrite`) stages its payload
- **THEN** no directory entry for the staged content SHALL exist at any point before publication
- **AND** the staged content SHALL be published by descriptor, so that no name a third party could take over is consulted
- **AND** no cleanup of a staging name SHALL be required or performed

#### Scenario: The staging file of an overwrite is replaced before publication

- **WHEN** another process detaches an overwrite's staged temporary file from its name — by unlinking it or renaming a different file over it — after the payload has been flushed and before publication
- **THEN** the substituted file's contents SHALL NOT be published at the destination
- **AND** the destination SHALL hold either its prior content or the content this call staged, never a third party's
- **AND** the substituted file SHALL be left in place rather than unlinked by the cleanup

#### Scenario: The filesystem cannot stage without a name

- **WHEN** the vault filesystem does not support staging an unnamed file
- **THEN** a no-clobber write SHALL be refused with an error naming the unsupported capability
- **AND** SHALL NOT fall back to staging under a name

#### Scenario: Staging happens in the destination directory

- **WHEN** any note or file write stages its payload
- **THEN** the staged inode SHALL be allocated in the destination's own
  directory, so publication is a same-directory operation
- **AND** where that staging carries a name — the overwrite path only — the
  name SHALL be removed whether the write succeeds or fails, and only while it
  still refers to the inode this call staged

#### Scenario: A newly created folder is durable too

- **WHEN** `create_note("New/Folder/x.md", …)` creates `New` and `Folder` and
  then publishes the note
- **THEN** each created directory's parent SHALL be flushed as well, outward to
  the first directory that already existed
- **AND** a failure of any of those flushes SHALL be logged and SHALL NOT be
  reported to the caller as a failed write
