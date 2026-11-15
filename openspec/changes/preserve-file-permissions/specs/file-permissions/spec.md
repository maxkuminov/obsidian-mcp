## ADDED Requirements

### Requirement: A publication that replaces an existing regular file SHALL keep that file's permission bits and POSIX access ACL

Every publication that replaces an existing regular file SHALL leave the published file with the replaced file's permission bits (`st_mode & 0o777`) and its POSIX access ACL (`system.posix_acl_access`), read from the replaced file itself through a descriptor. This covers `edit_note`, `set_frontmatter`, `complete_task`, `update_task`, `add_task`, `move_note`'s link rewrites in other notes, `write_file(overwrite=True)` over an existing file, and an upload or import published with an overwrite token.

- A replaced file with no access ACL (`ENODATA`) SHALL yield a published file with no access ACL, even when the staged inode inherited one from the directory's default ACL.
- The setuid, setgid and sticky bits SHALL NOT be carried to the published file.
- The published inode SHALL be owned by the server's uid. When only the owner uid differs from the replaced file's, the permission bits and ACL SHALL still be kept.
- When the staged inode's gid differs from the replaced file's, the server SHALL try to give the staged inode the replaced file's gid before writing any permission. It SHALL NOT try to change the owner uid.

#### Scenario: An edited note in a group-ACL folder keeps its group write access

- **WHEN** a note created by a plain `open(O_CREAT, 0o666)` in a directory whose default ACL is `u::rwx,g::rwx,g:<group>:rwx,m::rwx,o::r-x` (so its access ACL carries `mask::rw-`) is changed with `edit_note`
- **THEN** its `system.posix_acl_access` bytes after the edit SHALL equal the bytes before it, and its `st_mode & 0o777` SHALL be unchanged (0o664)

#### Scenario: A private note stays private after an edit

- **WHEN** a note with mode 0o600 and no access ACL, in a directory with no default ACL, is changed with `edit_note`
- **THEN** its `st_mode & 0o777` SHALL be 0o600 and reading `system.posix_acl_access` SHALL fail with `ENODATA`

#### Scenario: A file without an ACL does not gain one from the folder

- **WHEN** a note with mode 0o640 and no access ACL sits in a directory that has a default ACL, and it is changed with `set_frontmatter`
- **THEN** reading its `system.posix_acl_access` SHALL fail with `ENODATA` and its `st_mode & 0o777` SHALL be 0o640

#### Scenario: Special bits are not carried

- **WHEN** a raw file with mode 0o4755 is replaced with `write_file(overwrite=True)`
- **THEN** the published file's `st_mode & 0o7777` SHALL be 0o755

#### Scenario: A kept group is set before the permissions

- **WHEN** the replaced file's gid is a supplementary group of the server process that differs from the gid the staged inode was created with, and the file is changed with `edit_note`
- **THEN** the published file's `st_gid` SHALL equal the replaced file's, and its mode bits and ACL bytes SHALL equal the replaced file's

### Requirement: When the replaced file's group cannot be kept the publication SHALL get the create rule instead

When the staged inode cannot be given the replaced file's gid (`fchown` fails with `EPERM` or `EINVAL`), the server SHALL apply the create rule of this capability to the publication instead of the replaced file's bits and ACL, so that group-class permissions are never handed to a different group. This fallback SHALL NOT be a refusal.

#### Scenario: A group the server is not in

- **WHEN** `edit_note` replaces a note whose gid the server cannot set on the staged inode (the `fchown` call fails with `EPERM`)
- **THEN** the note SHALL be published, and its `st_mode & 0o777` and `system.posix_acl_access` bytes (or `ENODATA`) SHALL equal those of a control file created in the same directory with `open(O_CREAT, 0o666)`

### Requirement: A publication at a name that did not exist SHALL get exactly what an ordinary new file gets in that directory

A publication that creates a name SHALL leave the published file with the permission bits and POSIX access ACL that the kernel gives a file created by `open(O_CREAT, 0o666)` in the destination directory: derived from the directory's default ACL where it has one, with the umask not applied, and otherwise `0o666 & ~umask`. This covers `create_note`, `write_file` (by default, and with `overwrite=True` at a name that does not exist yet), a no-overwrite upload, and `import_from_url`.

- The rule SHALL hold whatever directory the bytes were staged in, including `.transfer-tmp`.
- The published file's gid SHALL be the one an ordinary create gets: the directory's gid when the directory has the setgid bit, otherwise the server's effective gid. When that gid cannot be set on a staged inode created elsewhere, the publication SHALL proceed with the owning group's permissions cleared (the `GROUP_OBJ` entry set to `---`, or the mode's group bits cleared when the computed ACL has no mask), never with them granted to another group.

#### Scenario: Group-writable default ACL

- **WHEN** `create_note` creates a note in a directory whose default ACL is `u::rwx,g::rwx,g:<group>:rwx,m::rwx,o::r-x`
- **THEN** the note's `st_mode & 0o777` SHALL be 0o664 and its `system.posix_acl_access` bytes SHALL equal those of a control file created in the same directory with `open(O_CREAT, 0o666)`, which carry `mask::rw-`

#### Scenario: Private default ACL

- **WHEN** `write_file` creates a file in a directory whose default ACL is `u::rwx,g::---,o::---`
- **THEN** the file's `st_mode & 0o777` SHALL be 0o600 and reading its `system.posix_acl_access` SHALL fail with `ENODATA`, exactly as for the control file

#### Scenario: No default ACL

- **WHEN** `create_note` creates a note in a directory with no default ACL
- **THEN** the note's `st_mode & 0o777` SHALL be `0o666 & ~umask` and reading its `system.posix_acl_access` SHALL fail with `ENODATA`

#### Scenario: An overwrite-mode write to a name that does not exist yet

- **WHEN** `write_file(overwrite=True)` writes a name that does not exist, in a directory whose default ACL grants a named group `rwx`
- **THEN** the file's mode bits and ACL bytes SHALL equal those of the control file

#### Scenario: Upload staged under a different default ACL

- **WHEN** a no-overwrite upload is published into a directory with default ACL A, while `.transfer-tmp` carries a different default ACL B
- **THEN** the published file's mode bits and ACL bytes SHALL equal those of a control file created in the destination directory, and SHALL NOT carry any entry of B that A lacks

### Requirement: A directory the server creates in the vault SHALL get what an ordinary mkdir gets, except the staging directory

Every vault directory the server creates (`create_note`, `write_file`, `move_note`, an upload or import into a new folder, the soft-delete tree under `.trash/`) SHALL be created with mode argument 0o777, so that the kernel applies the parent's default ACL or the umask exactly as for `mkdir(path, 0o777)`. `.transfer-tmp` SHALL be created with 0o700 and SHALL continue to be held at 0o700 on every open.

#### Scenario: A new folder under a group-writable default ACL

- **WHEN** `create_note` creates `New/x.md` and `New` does not exist, under a parent whose default ACL is `u::rwx,g::rwx,g:<group>:rwx,m::rwx,o::r-x`
- **THEN** `New`'s `st_mode & 0o777`, `system.posix_acl_access` bytes and `system.posix_acl_default` bytes SHALL equal those of a control directory created in the same parent with `os.mkdir(path, 0o777)`, which carry `mask::rwx`

#### Scenario: The staging directory stays owner-only

- **WHEN** the first upload creates `.transfer-tmp` under a vault root with a group-writable default ACL
- **THEN** `.transfer-tmp`'s `st_mode & 0o777` SHALL be 0o700

### Requirement: A filesystem without POSIX ACLs SHALL keep the mode-only behaviour

`ENODATA`, `EOPNOTSUPP` and `ENOTSUP` from reading `system.posix_acl_access` or `system.posix_acl_default`, or from removing `system.posix_acl_access` from a staged inode, SHALL mean "no POSIX ACL here" and SHALL NOT be failures. On such a filesystem an edit SHALL keep the replaced file's permission bits, and a create SHALL get `0o666 & ~umask`.

#### Scenario: ACL calls unsupported

- **WHEN** every xattr call on the vault fails with `EOPNOTSUPP`, and `create_note` creates a note and `edit_note` changes a note with mode 0o600
- **THEN** both calls SHALL succeed, the new note's `st_mode & 0o777` SHALL be `0o666 & ~umask`, and the edited note's SHALL be 0o600

### Requirement: Failing to apply the permission rule SHALL refuse the write and publish nothing

Any other failure while capturing or applying the permission rule (reading the replaced file's or directory's ACL, `fchown` other than `EPERM`/`EINVAL`, `fsetxattr`, `fremovexattr`, `fchmod`, an unparsable default ACL, or a required ACL write refused with `EOPNOTSUPP`/`ENOTSUP`) SHALL publish nothing and SHALL leave the destination's bytes, permission bits and ACL as they were. A note or file tool SHALL answer it as an `io_failure` refusal whose text says that the permissions could not be applied and that nothing was written. An upload SHALL treat it as a pre-publication failure: the claim is released and the token is `pending` again.

#### Scenario: An ACL write fails during an edit

- **WHEN** `edit_note` runs and setting `system.posix_acl_access` on the staged inode fails with `EIO`
- **THEN** the response SHALL end with `MCP-REFUSAL {"code":"io_failure"}`, its text SHALL say that nothing was written, and the note's bytes, `st_mode` and ACL bytes SHALL equal those before the call

#### Scenario: An ACL write fails during an upload

- **WHEN** an upload's permission step fails with `EIO`
- **THEN** nothing SHALL exist at, or change at, the bound path, no staged bytes SHALL remain, and the token SHALL be `pending`

### Requirement: Staged bytes SHALL stay private, and the published permissions SHALL be applied before publication and bound to the file the write verified

Staging inodes SHALL still be created with mode 0o600. The permission rule SHALL be applied to the staged inode before its payload flush, so the existing single flush covers it. Immediately before publication, after the `expected=` comparison where there is one, the server SHALL capture the destination's permissions again, through the same descriptor the comparison read the bytes from, or through a fresh `O_PATH | O_NOFOLLOW` descriptor where there is no comparison. When the result differs from what was applied, the server SHALL re-apply it and flush the staged inode once more before publishing. An ordinary write SHALL flush the staged file once, as before.

#### Scenario: Staged bytes are private while being written

- **WHEN** any write stages its payload
- **THEN** the staged inode SHALL be created with mode 0o600 before the first payload byte is written

#### Scenario: One flush for an ordinary write

- **WHEN** `edit_note` replaces a note whose permissions do not change during the call
- **THEN** exactly one `fsync` SHALL be issued on the staged file

#### Scenario: Permissions changed during the write are the ones published

- **WHEN** a note's mode is changed from 0o664 to 0o600 after the staged payload was written and before the `expected=` comparison, and the comparison then matches
- **THEN** the published note's `st_mode & 0o777` SHALL be 0o600, and the staged file SHALL have been flushed a second time before publication

#### Scenario: A file that appears during an overwrite-mode write

- **WHEN** `write_file(overwrite=True)` starts at an absent name, and a file with mode 0o600 appears at that name before publication
- **THEN** the published file's `st_mode & 0o777` SHALL be 0o600

#### Scenario: An overwrite token's target whose permissions changed after the mint

- **WHEN** an overwrite token is minted and the target's mode is changed afterwards
- **THEN** the upload SHALL be refused with HTTP 409 and the target's bytes and mode SHALL be unchanged
