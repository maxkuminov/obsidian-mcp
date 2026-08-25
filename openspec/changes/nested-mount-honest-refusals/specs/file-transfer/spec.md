# file-transfer — delta for nested-mount-honest-refusals

## MODIFIED Requirements

### Requirement: `delete_file` tool

`delete_file(path, permanent=False)` SHALL require a `readwrite` identity, validate the path with the vault guards, refuse markdown files (pointing to `delete_note`), directories, and symlinks, and by default move the file through anchored operations to `.trash/<YYYYMMDD-HHMMSS>-<basename>-<8 hex>`; with `permanent=True` it SHALL unlink the file. The markdown refusal SHALL be applied case-insensitively to the **canonical** final path component — the one the filesystem will open — not to the caller's raw string. The soft delete SHALL be a single `renameat2` call carrying `RENAME_NOREPLACE`, so that it never unlinks anything, never pre-creates or reserves the destination name, never clobbers an existing trash entry whoever created it, and moves a file that replaced the source concurrently into the trash rather than destroying it. A destination name already taken (`EEXIST`) SHALL be retried with a fresh random suffix, bounded; a kernel or filesystem that cannot perform a non-replacing rename (`EINVAL`, `ENOSYS`, `EOPNOTSUPP`) SHALL raise the unsupported-filesystem error rather than fall back to a replacing `rename`. `EXDEV` SHALL NOT be classified with those, and SHALL be classified before it is named (the rename primitive's rule): a definite mount mismatch is surfaced as the mount-boundary error naming the mount layout as the cause and `permanent=True` as the workaround; a definite same-mount `EXDEV` names a security policy or filesystem-internal boundary instead; an unreadable mount identity is presented as ambiguous between them — never as missing non-replacing-rename support and never as `.trash/` being unable to receive a rename. Where the kernel can answer the mount question (`STATX_MNT_ID`), a best-effort preflight comparing the source parent with the opened `.trash` descriptor SHALL raise that same refusal before the rename is attempted; where it cannot answer, the preflight SHALL be skipped — never failed closed — and the rename's `EXDEV` mapping is the backstop. `probe_trash` SHALL exercise that same primitive, so an environment without `RENAME_NOREPLACE` is caught at first use rather than at the first delete — and when the probe's own rename fails `EXDEV` because `.trash/` sits on a different mount than the vault root, the probe SHALL preserve the mount-boundary cause in type and prose rather than re-wrapping it as generic filesystem inability.

#### Scenario: Markdown is refused however the path is spelled

- **WHEN** `delete_file` is called with `note.md/.`, `note.md/`, `a//note.md`, or `NOTE.MD`
- **THEN** each SHALL be refused with the pointer to `delete_note` and nothing SHALL be deleted

#### Scenario: The source is replaced while it is being trashed

- **WHEN** a different file replaces the source name after the `lstat` refusal has passed but before the move completes
- **THEN** that replacement SHALL end up in `.trash/` intact and no file SHALL be unlinked

#### Scenario: The trash destination name is taken by someone else

- **WHEN** the chosen `.trash/<YYYYMMDD-HHMMSS>-<basename>-<8 hex>` name is created by another writer before the move runs
- **THEN** the move SHALL fail `EEXIST` and retry under a different random suffix, and the other writer's file SHALL be left byte-for-byte intact

#### Scenario: The filesystem cannot do a non-replacing rename

- **WHEN** `renameat2(RENAME_NOREPLACE)` is unavailable or refused (`EINVAL`, `ENOSYS`, `EOPNOTSUPP`)
- **THEN** the soft delete SHALL fail with the unsupported-filesystem error, SHALL NOT fall back to a replacing rename, and the source file SHALL remain in place

#### Scenario: The source is on a different mount than `.trash/`

- **WHEN** `delete_file` soft-deletes a file whose directory is on a different mount than the vault root's `.trash/` (e.g. a directory of the same filesystem bind-mounted beneath the root)
- **THEN** the tool SHALL refuse with the mount-boundary error naming the mount layout as the cause and `permanent=True` as the workaround
- **AND** the file SHALL be untouched and nothing SHALL be created in `.trash/`
- **AND** the error SHALL NOT claim the filesystem cannot perform a non-replacing rename or that `.trash/` cannot receive one

#### Scenario: `.trash/` itself is a separate mount

- **WHEN** the vault root's `.trash/` directory is itself a mount distinct from the root's and `probe_trash` runs
- **THEN** the probe SHALL fail with the mount-boundary error, in prose naming the root/`.trash` mount layout
- **AND** the failure SHALL NOT be reported as the filesystem being unable to move files with a non-replacing rename

#### Scenario: A kernel that cannot answer the mount question keeps its soft delete

- **WHEN** a soft delete runs where `STATX_MNT_ID` is unavailable and the source and `.trash/` share a mount
- **THEN** the preflight SHALL be skipped and the soft delete SHALL proceed and succeed
- **AND** a cross-mount attempt on such a kernel SHALL still be refused by the rename's `EXDEV` classification, presented as ambiguous between a mount boundary and a policy or filesystem-internal boundary — the identity that would prove the mount claim is exactly what such a kernel cannot read

#### Scenario: Soft delete

- **WHEN** `delete_file("Attachments/shot.png")` is called
- **THEN** the file SHALL no longer exist at the path and SHALL exist under `.trash/` with the timestamped name

#### Scenario: Permanent delete

- **WHEN** `delete_file(path, permanent=True)` is called
- **THEN** the file SHALL be removed and nothing SHALL be written to `.trash/`

#### Scenario: Permanent delete still works across a mount boundary

- **WHEN** `delete_file(path, permanent=True)` targets a file on a mount beneath the vault root
- **THEN** the file SHALL be unlinked exactly as on a single-mount vault — an unlink crosses no mount boundary

#### Scenario: Markdown, symlink, directory refused

- **WHEN** `delete_file` targets `note.md`, a symlink, or a directory
- **THEN** the tool SHALL return an error and nothing SHALL change

#### Scenario: Concurrent soft-deletes with the same basename

- **WHEN** two files with the same basename are soft-deleted within the same second
- **THEN** both SHALL exist in `.trash/` under distinct names

## ADDED Requirements

### Requirement: The fallback's staging discard distinguishes a published name from a disappeared one

The named-staging fallback's discard SHALL be told whether the publication landed, and SHALL treat an absent staging name after a successful publish as the ordinary consumed case — silent, because the overwrite publish is a rename that consumes the name — reserving the "staging name disappeared before its write was published" warning for a name that vanished while the write had genuinely not published. Every discard call site on the transfer path SHALL pass the publication's actual outcome, including the outer cleanup reached when a failure *after* publication (a post-publication directory flush failing, correctly classified as a post-publish failure with the claim stranded) unwinds the stream: hardcoding "not published" there makes the warning false in exactly the doubly-degraded corner where an operator most needs to trust it, and a false disappearance warning trains an operator to ignore the true one, which is the substitution signal. The published-state record SHALL be initialized before the staging name can exist, so that a failure at any point after staging — the body drain, the identity `fstat`, the mode change, the payload flush — finds it present and false rather than absent: the cleanup consulting a record that is not yet bound would replace the original failure with a name error and skip the guarded discard entirely. The inode-guarded unlink direction is unchanged: a present name still referring to the staged inode is removed quietly, a substituted or unidentifiable name is left in place and logged, published or not.

#### Scenario: A fallback upload publishes and a post-publication flush fails

- **WHEN** a named-fallback upload's overwrite publish lands and a subsequent post-publication directory flush raises
- **THEN** the failure SHALL remain classified post-publish and the claim SHALL strand exactly as specified elsewhere
- **AND** the outer cleanup's discard SHALL be invoked with the published outcome true and SHALL log no warning about the staging name having disappeared, whether the name was consumed by the publish or a matching residual name remains to be removed quietly

#### Scenario: A failure after the staged identity was recorded cleans up exactly as before

- **WHEN** a named-fallback upload fails after its staging name exists and its identity `fstat` has succeeded, but before publication — an over-cap body, a disconnect, a failing `fchmod` or payload flush
- **THEN** the original failure SHALL propagate unmasked and the claim handling SHALL be the pre-publication behavior specified elsewhere
- **AND** the discard SHALL run with the published outcome false, removing a name that still refers to the staged inode and warning if the name has disappeared

#### Scenario: A failing identity `fstat` leaves the name in place

- **WHEN** a named-fallback upload's identity `fstat` itself fails, so no staged identity was ever recorded
- **THEN** the original failure SHALL propagate unmasked with the published outcome false
- **AND** the discard SHALL be invoked with no recorded identity and SHALL remove nothing — the name is left in place with the cannot-confirm warning, because with no identity nothing can prove the name still refers to the staged inode, and unlinking it could destroy a concurrent substitute (the destructive-write class the guard exists to refuse)

#### Scenario: A pre-publication disappearance still warns

- **WHEN** a named-fallback upload fails before publication and its staging name is found absent at discard
- **THEN** the discard SHALL log the disappearance warning exactly as it does today
