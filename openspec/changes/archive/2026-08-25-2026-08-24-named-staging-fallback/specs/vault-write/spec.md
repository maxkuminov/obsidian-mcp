## MODIFIED Requirements

### Requirement: Atomic write invariant

The system SHALL perform all file writes from MCP write tools by staging the payload in the destination's own directory, flushing it to durable storage before publication, publishing it with an atomic same-directory rename (overwrite) or hard link (no-clobber) relative to the destination's directory descriptor, and flushing that directory once the publication has happened. The applicable tools are `create_note`, `edit_note`, `move_note`, `delete_note`, `set_frontmatter`, and `write_file`. Payload and directory durability are properties of the shared atomic-write helper, so **every** caller of it inherits them, including `write_file` in both its no-clobber and its `overwrite=True` mode; an implementation SHALL NOT satisfy this requirement for the note tools while omitting the flush for a raw-byte write that goes through the same helper. Direct writes that could leave the destination truncated on crash SHALL NOT be used. Where staging carries a name — the overwrite path, whose replacing rename has no by-descriptor form — that name SHALL be created with exclusive, non-symlink-following semantics so a pre-created name cannot be written through; the no-clobber path SHALL have no name at all, unless `vault_allow_named_staging_fallback` is set and the filesystem cannot stage an unnamed inode, which is the one opted-in case where it too stages under an exclusively created name.

The destination directory SHALL be flushed **after** the publishing rename or link, so that the directory entry the write created or replaced is durable and not only its contents. Flushing only the immediate parent leaves the entry that names *it* unflushed, so a crash can lose the whole new folder and with it a note the tool reported written. The directories to flush SHALL be the **complete ancestor chain** from the destination parent up to the vault root, innermost first — not only the directories the publishing call itself created. Per-call creation provenance is insufficient and SHALL NOT be relied on: a call that creates a directory and then aborts before publication flushes nothing, correctly, because it published nothing; the call that later succeeds finds that directory already present, records no creation of it, and would leave the entry naming it made durable by nobody. The obligation outlives the call that incurred it, and outlives the process, so no in-memory record of "who created what" can discharge it. The chain is bounded by path depth and a directory flush is metadata-only, so the conservative rule is also the cheap one. A failure of the destination-directory flush, or of any of those ancestor flushes, SHALL be logged and SHALL NOT turn a write that already landed into a reported failure: the payload was already durable, the previous content survives either way, and a note tool that reports a false failure is retried — `edit_note(append=True)` retried after a write that landed appends the same block twice. This is deliberately the opposite failure direction from the transfer path, where the source bytes are gone and the ambiguity must be surfaced instead.

**Durability is a property of every publication, not only of the staged-payload helper — and of every *caller* of it, not only the ones a tool name makes obvious.** A note tool publishes in three ways and all three write a directory entry that a crash can lose, so the requirement names them rather than scoping itself to the shared atomic-write helper: the staged-payload `rename`/`link` above; `move_note`'s `renameat2`, which writes **two** entries — the destination's new one and the source's removal — so **both** parent directories SHALL be flushed after it lands, and so SHALL both parents of a rollback rename that puts a source back; and the soft delete's `renameat2` into the trash, which SHALL flush the source's parent **and** the trash directory. A permanent delete's `unlink` SHALL flush the parent directory it removed the entry from. The soft delete's flushes belong to the shared primitive the note and file tools both reach it through, so `delete_file`'s soft delete SHALL get them too. In each case a crash that makes only one of the two entries durable leaves the vault holding the note twice or not at all, or holding an entry for a note the tool reported deleted.

**A link rewrite is one of those callers and SHALL NOT be exempt.** `move_note(rewrite_links=True)` publishes a rewritten body into every backlink source, through the same staged-payload helper, and each of those publications owes the same complete ancestor chain — a rewrite target may sit under directories an external writer created and never made durable, which is exactly the case the chain rule exists for. An implementation SHALL therefore keep a usable vault-root anchor available to every target it publishes through: without one the ancestor lookup is impossible and the flush silently degrades to the leaf's parent alone, which is a *quiet* exemption from this requirement rather than a visible one.

That anchor SHALL be reconciled with the descriptor budget rather than allowed to defeat it. The rewrite phase holds one target per planned rewrite and the number of backlink sources is unbounded, so giving each target its own root descriptor doubles the per-source cost and halves how large a move the process can afford. Since every rewrite target resolves the same vault root, a **single shared root descriptor** for the phase SHALL be permitted, and it SHALL be adopted by a target only after verifying that it names the same root inode that target's parent was proved beneath — a mismatch means the vault root pathname was repointed mid-call, which SHALL abort the whole move before any mutation rather than being reported as one source failing to rewrite. Whatever descriptors the phase retains SHALL be charged against the documented budget.

Every one of these flushes SHALL take the same failure direction as the write path's: **logged, and never turned into a reported failure**, for the same reason expressed for the operation at hand. The rename or the unlink has already happened; a tool that reports it as failed is retried, and a retried move or delete finds the source gone and either contradicts the vault or acts on whatever has since taken the name. Nothing is lost by absorbing the failure except a warning.

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
  on a filesystem that supports staging an unnamed inode, or on any filesystem
  when `vault_allow_named_staging_fallback` is not set
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
  and `vault_allow_named_staging_fallback` is not set (the default)
- **THEN** a no-clobber write SHALL be refused with an error naming the
  unsupported capability and the `VAULT_ALLOW_NAMED_STAGING_FALLBACK` setting
  that would opt into the fallback
- **AND** SHALL NOT fall back to staging under a name

#### Scenario: Named-staging fallback, opted in

- **WHEN** the vault filesystem does not support staging an unnamed file
  and `vault_allow_named_staging_fallback` is set
- **THEN** the no-clobber write SHALL stage a named temporary file, created
  `O_CREAT|O_EXCL|O_NOFOLLOW` through the same parent directory descriptor
  (`MutableTarget.dir_fd`, opened at validation) every other mutating write
  uses — no pathname SHALL be re-resolved to obtain it
- **AND** the write SHALL publish by hard-linking the staged file to the
  destination name, so an existing destination is still refused (`EEXIST`)
  rather than replaced
- **AND** this reopens the named-staging substitution window unnamed-inode
  staging exists to close: a directory entry for the staged content exists,
  observable and replaceable, between staging and publication
- **AND** a `WARNING` SHALL be logged exactly once per process, the first
  time the fallback is actually exercised — that is, once a staging name has
  been created, and not when the setting is enabled, not when a probe selects
  the mode, and not when the creation of that name failed every attempt
- **AND** that warning SHALL state where the exercising path stages, and
  SHALL NOT attribute one path's staging location to the other: a note write
  stages beside its destination in an ordinary vault directory, a transfer
  stages in its own hidden staging directory, and the note path's window is
  the wider of the two
- **AND** `/health` SHALL report `vault_named_staging_fallback_active: true`
  once the fallback has been exercised in that process, and SHALL NOT report
  it for a process whose only attempt to stage under a name failed

#### Scenario: A staging name is not removed unless it is provably ours

- **WHEN** a write that staged under a name reaches its cleanup without an
  identity for what it staged — the `fstat` of the staged descriptor failed
  after the exclusive creation, or the name was already gone when the
  publication looked for it
- **THEN** the cleanup SHALL NOT unlink the name
- **AND** the file at that name SHALL be left in place and the fact logged,
  because a no-clobber write that published nothing must not destroy a file
  that took the name over — the same destructive-write class as unlinking an
  identified substitute
- **AND** this SHALL hold for both write paths, since they clean up through
  the same primitive

#### Scenario: A staging name disappears before publication

- **WHEN** a write's staging name is absent at cleanup time and the write did
  **not** publish
- **THEN** the cleanup SHALL report the disappearance rather than treating it
  as the ordinary consumed-by-publication case
- **AND** an absent staging name SHALL be quiet only when the write published,
  because that is the `renameat` that consumed it

#### Scenario: Staging happens in the destination directory

- **WHEN** any note or file write stages its payload
- **THEN** the staged inode SHALL be allocated in the destination's own
  directory, so publication is a same-directory operation
- **AND** where that staging carries a name — the overwrite path, and the
  no-clobber path under the opted-in named-staging fallback — the name SHALL
  be removed whether the write succeeds or fails, and only while it still
  refers to the inode this call staged

#### Scenario: A move's rename is made durable at both ends

- **WHEN** `move_note` publishes its `renameat2` from one directory to another
- **THEN** the destination's parent directory SHALL be flushed after the rename, and so SHALL the source's
- **AND** every directory above each end, up to the vault root, SHALL be flushed as well — not only the ones this call created
- **AND** a failure of any of those flushes SHALL be logged and SHALL NOT turn a move that already landed into a reported failure

#### Scenario: A move that is rolled back is made durable too

- **WHEN** a move is refused after its rename landed — the destination held the caller's inode but is a directory or a symbolic link — and the tool renames it back
- **THEN** both parent directories SHALL be flushed after the rollback rename lands
- **AND** the refusal SHALL still be reported to the caller, unchanged by whether those flushes succeeded

#### Scenario: A soft delete's rename is made durable

- **WHEN** a note or a file is soft-deleted by renaming it into the trash directory
- **THEN** the source's parent directory SHALL be flushed after the rename, and so SHALL the trash directory
- **AND** a failure of either flush SHALL be logged and the delete SHALL still be reported as the success it is
- **AND** the same SHALL hold for the rollback rename that puts back a directory the soft delete refuses to take

#### Scenario: A permanent delete's unlink is made durable

- **WHEN** a note or a file is deleted permanently
- **THEN** the parent directory the entry was removed from SHALL be flushed after the unlink
- **AND** a failure of that flush SHALL be logged and SHALL NOT be reported as a failed delete, because the file is already unlinked and a retry would act on whatever now holds the name

#### Scenario: A backlink rewrite is made durable to the same depth as any other write

- **WHEN** `move_note(rewrite_links=True)` rewrites a backlink in a source note that lives several directories deep, and an outer directory on that source's path was created by another writer and never flushed
- **THEN** the rewrite's publication SHALL flush every directory above that source's parent, up to the vault root, innermost first — exactly as a `create_note` into the same path would
- **AND** the descriptor arrangement that makes the lookup possible SHALL NOT pin one root descriptor per rewritten source

#### Scenario: The vault root is repointed while rewrites are being planned

- **WHEN** a rewrite target cannot be shown to have been validated against the same vault root the move is anchored to
- **THEN** the move SHALL be aborted before its rename runs, so nothing is moved, rewritten or reindexed
- **AND** it SHALL NOT be reported as a single source failing to rewrite, because the root itself moved and every remaining target is equally suspect

#### Scenario: A newly created folder is durable too

- **WHEN** `create_note("New/Folder/x.md", …)` creates `New` and `Folder` and
  then publishes the note
- **THEN** every directory above the destination parent, up to the vault root,
  SHALL be flushed as well — not only the ones this call created, because a
  directory an aborted attempt created and never flushed is indistinguishable
  from one this call found
- **AND** a failure of any of those flushes SHALL be logged and SHALL NOT be
  reported to the caller as a failed write
