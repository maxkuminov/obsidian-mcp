# vault-write — delta for nested-mount-honest-refusals

## ADDED Requirements

### Requirement: A cross-mount rename names the mount boundary as its cause

The non-replacing rename primitive SHALL map `EXDEV` to a mount-boundary error (`MountBoundary`, the existing subclass of `UnsupportedFilesystem`) whose text names the mount layout as the cause, distinct from the `EINVAL`/`ENOSYS`/`EOPNOTSUPP` cases that genuinely mean the kernel or filesystem cannot perform a non-replacing rename. `EXDEV` from `renameat2` means the two names are on different mounts and nothing else; folding it into a "renameat2 is not available" message blames a filesystem that renames fine, and an agent acting on that text is sent to change filesystems when the fix is the mount layout. Every caller of the primitive — the soft delete, `move_note`'s publication and its rollbacks — SHALL surface that cause rather than re-wrapping it into filesystem-support prose; a caller that wraps rename failures in its own message SHALL handle the mount-boundary subclass before the generic class, or its wrapper is a lie and the subclass handler unreachable.

#### Scenario: A rename across a nested mount is refused with the mount-boundary cause

- **WHEN** a `renameat2(RENAME_NOREPLACE)` issued by a vault primitive returns `EXDEV` because its two names sit on different mounts beneath the vault root
- **THEN** the raised error SHALL be the mount-boundary type and its text SHALL name the mount layout as the cause
- **AND** the text SHALL NOT state or imply that the filesystem lacks non-replacing-rename support

#### Scenario: The genuinely unsupported cases keep their message

- **WHEN** the same primitive fails with `EINVAL`, `ENOSYS` or `EOPNOTSUPP`
- **THEN** the error SHALL remain the generic unsupported-filesystem refusal stating that a non-replacing rename is unavailable and there is no safe fallback

### Requirement: A soft delete across a mount boundary is refused with an accurate cause and an actionable workaround

The soft delete SHALL refuse to move a file into `.trash/` across a mount boundary with a mount-boundary error naming the layout — the file's directory and the vault root's `.trash` are on different mounts, which the rename cannot cross — and naming `permanent=True` as the workaround; it SHALL NOT blame `.trash/`'s ability to receive a non-replacing rename for a cross-mount failure. Where the kernel can answer the mount question (`STATX_MNT_ID`), a best-effort preflight comparing the source parent with the opened `.trash` descriptor SHALL raise that refusal before the rename is attempted; where it cannot answer, the preflight SHALL be skipped — never failed closed, because a kernel between the 5.6 floor and 5.8 serves same-mount soft deletes correctly today — and the rename's own `EXDEV` mapping is the backstop that still names the true cause. Both mount ids SHALL be read inside a single comparison immediately before use and never persisted. The behavior SHALL live in the shared soft-delete primitive, so `delete_note` (specified here) and `delete_file` (whose own requirement in `file-transfer` states the same refusal) cannot drift apart; soft-deleting into a per-mount trash is out of scope and the operation still fails on such a layout — only the reported cause changes.

#### Scenario: Soft delete of a file on a nested mount

- **WHEN** `delete_note(path)` or `delete_file(path)` soft-deletes a file whose directory is on a different mount than the vault root's `.trash/` (e.g. a directory of the same filesystem bind-mounted beneath the root)
- **THEN** the tool SHALL refuse with a mount-boundary error naming the mount layout as the cause and `permanent=True` as the workaround
- **AND** the file SHALL be untouched and nothing SHALL be created in `.trash/`
- **AND** the error SHALL NOT claim that `.trash/` cannot receive a non-replacing rename from the vault

#### Scenario: A kernel that cannot answer the mount question keeps its soft delete

- **WHEN** the same soft delete runs where `STATX_MNT_ID` is unavailable and the source and `.trash/` share a mount
- **THEN** the preflight SHALL be skipped and the soft delete SHALL proceed and succeed
- **AND** a cross-mount attempt on such a kernel SHALL still be refused by the rename's `EXDEV` mapping, with the same mount-boundary cause

### Requirement: `move_note` refuses a cross-mount move naming the mount boundary

`move_note` SHALL refuse a move whose source and destination parents sit on different mounts with a mount-boundary error naming the layout, and SHALL NOT attribute the failure to missing filesystem support for the non-replacing rename. Where the kernel can answer the mount question, a best-effort preflight SHALL refuse before the rename and before the database is touched, and the preflight itself SHALL create nothing: when the destination parent does not exist yet, the comparison SHALL run against the destination's deepest **existing** ancestor — a directory created beneath it lands on that ancestor's mount, the same reasoning the transfer mint preflight already uses — never by materializing the destination parent to compare against it. Where the kernel cannot answer, the preflight SHALL be skipped and the rename's `EXDEV` mapping is the backstop; on that path missing destination parent directories may have been created before the rename refuses, and what such a refusal can leave behind SHALL be at most empty directories — the same bounded residual already declared for creation descents — never a moved note, a lost note, or a database change. The preflight SHALL run only on the forward move: a rollback SHALL always attempt its rename, because refusing a rollback on a preflight strands the note at the destination, and a forward rename that landed proves both parents share a mount. A refused move SHALL update no database row — `notes_metadata` and `note_links` SHALL be untouched, including under `rewrite_links=True` with planned rewrites, and the refusal SHALL come before any source note is rewritten. Moves that stay on one side of a boundary SHALL be unaffected, and a copying fallback SHALL NOT be introduced — it breaks the guarantee that whichever inode is at the source when the call runs is what moves.

#### Scenario: A move across a nested-mount boundary

- **WHEN** `move_note("M/a.md", "a.md")` runs where `M/` is a mount beneath the vault root
- **THEN** the tool SHALL refuse with a mount-boundary error naming the mount layout as the cause
- **AND** the source note SHALL be untouched, nothing SHALL exist at the destination, and no database statement SHALL be executed or committed for the move — verified against the session, not inferred from the refusal text — including under `rewrite_links=True` with at least one planned backlink rewrite, whose source notes SHALL be byte-identical afterwards
- **AND** the error SHALL NOT claim that `renameat2(RENAME_NOREPLACE)` is unavailable

#### Scenario: A cross-mount move to a missing destination folder creates nothing

- **WHEN** `move_note("M/a.md", "New/Sub/a.md")` runs where `M/` is a mount beneath the vault root and `New/Sub/` does not exist, on a kernel that can answer the mount question
- **THEN** the preflight SHALL refuse against the deepest existing destination ancestor's mount
- **AND** neither `New/` nor `New/Sub/` SHALL exist after the refusal

#### Scenario: A move on one side of the boundary still works

- **WHEN** a note is moved between two directories on the same mount, in a vault that also contains a nested mount elsewhere
- **THEN** the move SHALL proceed exactly as before this change

#### Scenario: A degraded kernel keeps its moves

- **WHEN** `move_note` runs where `STATX_MNT_ID` is unavailable and both parents share a mount
- **THEN** the preflight SHALL be skipped and the move SHALL succeed

### Requirement: The note path's named-fallback publish maps `EXDEV` to the mount boundary

The note path's named-staging fallback SHALL map `EXDEV` from its publishing link to a mount-boundary error naming the mount layout; and its overwrite publish (`os.replace`) SHALL map `EXDEV` to the same mount-boundary error rather than letting it escape as a bare `OSError`. Of the remaining link errnos, only `EOPNOTSUPP` SHALL be described as the filesystem not supporting hard links; `EPERM` SHALL be described as hard-link publication being denied — pointing at permissions or security policy (seccomp/LSM) as well as filesystem support — because a security policy returns `EPERM` for `link` on filesystems whose hard links work fine, and a message that diagnoses the filesystem there repeats the defect class this change removes. The note path stages beside its destination, so the `EXDEV` cases require an exotic layout to fire — but a message that is wrong whenever it fires is wrong, and the transfer path's equivalent branches already carry the accurate mapping; the two write paths SHALL use the same vocabulary.

#### Scenario: The fallback's no-clobber link fails `EXDEV`

- **WHEN** the named-fallback publish's hard link fails with `EXDEV`
- **THEN** the raised error SHALL be the mount-boundary type naming the mount layout
- **AND** an `EOPNOTSUPP` failure of the same link SHALL keep the message stating the filesystem does not support hard links
- **AND** an `EPERM` failure SHALL be reported as hard-link publication denied, naming security policy alongside filesystem support as possible causes

#### Scenario: The fallback's overwrite rename fails `EXDEV`

- **WHEN** the named-fallback overwrite publish's replacing rename fails with `EXDEV`
- **THEN** the raised error SHALL be the mount-boundary type naming the mount layout, not a bare `OSError`
