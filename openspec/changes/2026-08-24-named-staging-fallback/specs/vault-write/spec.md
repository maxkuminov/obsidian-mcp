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

#### Scenario: A no-clobber write exposes no staging name

- **WHEN** `create_note` or `write_file` (without `overwrite`) stages its payload
  on a filesystem that supports `O_TMPFILE`, or on any filesystem when
  `vault_allow_named_staging_fallback` is not set
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
  unsupported capability and the `VAULT_ALLOW_NAMED_STAGING_FALLBACK`
  setting that would opt into the fallback
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
  time the fallback is actually exercised (not merely when the setting is
  enabled)
- **AND** `/health` SHALL report `vault_named_staging_fallback_active: true`
  once the fallback has been exercised in that process

#### Scenario: Staging happens in the destination directory

- **WHEN** any note or file write stages its payload
- **THEN** the temporary file SHALL be created in the destination's own
  directory, so publication is a same-directory operation
- **AND** the temporary file SHALL be removed whether the write succeeds or
  fails
