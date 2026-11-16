# file-permissions Specification

## Purpose
The permissions of every file and folder the server publishes in a vault (#447): an edit keeps the replaced file's permission bits, POSIX access ACL and group; a create gets what an ordinary new file gets in its folder; a group the server cannot set is carried by a named ACL entry; and a rule that cannot be applied refuses the write instead of publishing under unknown permissions.

## Requirements
### Requirement: A publication that replaces an existing regular file SHALL keep that file's permission bits, POSIX access ACL and group

Every publication that replaces an existing regular file SHALL leave the published file with the replaced file's permission bits (`st_mode & 0o777`) and POSIX access ACL (`system.posix_acl_access`), read from the replaced file itself through a descriptor. This covers `edit_note`, `set_frontmatter`, `complete_task`, `update_task`, `add_task`, `move_note`'s link rewrites in other notes, `write_file(overwrite=True)` over an existing file, and an upload or import published with an overwrite token.

- A replaced file with no access ACL (`ENODATA`) SHALL yield a published file with no access ACL, even when the staged inode inherited one from the directory's default ACL.
- The setuid, setgid and sticky bits SHALL NOT be carried.
- The published inode SHALL be owned by the server's uid. When only the owner uid differs from the replaced file's, the permission bits and ACL SHALL still be kept, and the server SHALL NOT try to change the owner uid.
- The wanted gid is the replaced file's. When the staged inode's gid differs from it, the server SHALL `fchown(fd, -1, wanted)` before writing any permission. When that fails with `EPERM` or `EINVAL`, the group translation requirement SHALL apply. An edit SHALL NOT fall back to the create rule.

#### Scenario: An edited note in a group-ACL folder keeps its group write access

- **WHEN** a note created by a plain `open(O_CREAT, 0o666)` in a directory whose default ACL is `u::rwx,g::rwx,g:<group>:rwx,m::rwx,o::r-x` (so its access ACL carries `mask::rw-`) is changed with `edit_note`
- **THEN** its `system.posix_acl_access` bytes, `st_gid` and `st_mode & 0o777` (0o664) after the edit SHALL equal those before it

#### Scenario: A private note stays private after an edit

- **WHEN** a note with mode 0o600 and no access ACL, in a directory with no default ACL, is changed with `edit_note`
- **THEN** its `st_mode & 0o777` SHALL be 0o600 and reading `system.posix_acl_access` SHALL fail with `ENODATA`

#### Scenario: A file without an ACL does not gain one from the folder

- **WHEN** a note with mode 0o640 and no access ACL sits in a directory that has a default ACL, and it is changed with `set_frontmatter`
- **THEN** reading its `system.posix_acl_access` SHALL fail with `ENODATA` and its `st_mode & 0o777` SHALL be 0o640

#### Scenario: Special bits are not carried

- **WHEN** a raw file with mode 0o4755 is replaced with `write_file(overwrite=True)`
- **THEN** the published file's `st_mode & 0o7777` SHALL be 0o755

#### Scenario: A group the server holds is kept

- **WHEN** the replaced file's gid is a supplementary group of the server process that differs from the gid the staged inode was created with, and the file is changed with `edit_note`
- **THEN** the published file's `st_gid`, mode bits and ACL bytes SHALL equal the replaced file's

### Requirement: When the wanted group cannot be set the publication SHALL translate the owning group's permissions to a named group entry

"Group not kept" SHALL mean that the staged inode's gid differs from the wanted gid and `fchown(fd, -1, wanted)` failed with `EPERM` or `EINVAL`. The publication SHALL then proceed, never broader than its rule. It SHALL NOT refuse and SHALL NOT use another rule.

**On a filesystem with POSIX ACLs**, the published access ACL SHALL be the rule's ACL — or, when the rule has none, the minimal ACL of its mode bits — with these changes:

- `GROUP_OBJ` set to `---`.
- A named `GROUP` entry for the wanted gid carrying the permissions `GROUP_OBJ` had. When the rule's ACL already has a named entry for that gid, that entry SHALL be left exactly as it is, with no union.
- `MASK`: the rule's mask when it has one. Otherwise, the OR of the new ACL's group-class entries.
- Entries in the kernel's sort order.

**Mode-only translation**, permitted only when the rule has no ACL (its source had no access ACL xattr, or the filesystem reported ACLs unsupported, so there are no named entries): when the filesystem has no POSIX ACLs, or writing the translated ACL is refused with `EOPNOTSUPP`/`ENOTSUP`, the published mode SHALL be the rule's mode with the group bits cleared and the other bits ANDed with the rule's group bits. When the rule has an ACL and writing the translated ACL fails for any reason, the write SHALL be refused and nothing published, per the failure requirement.

For every unprivileged principal other than the server's uid and the replaced file's previous owner, the access the published file grants SHALL be a subset of the access its rule grants.

#### Scenario: An edit of a note whose group the server is not in

- **WHEN** `edit_note` replaces a note with mode 0o664, no ACL and gid `<g>`, on a filesystem with POSIX ACLs, and `fchown` to `<g>` fails with `EPERM`
- **THEN** the note SHALL be published with the staged inode's gid; its access ACL SHALL hold `group::---`, `group:<g>:rw-` and `mask::rw-`; its `st_mode & 0o777` SHALL be 0o664; and the effective read/write access the effective-access helper computes for a member of `<g>` only, for a member of the server's group only and for a user in neither group SHALL be no more than before the edit, equal for the member of `<g>`

#### Scenario: A private file stays private when its group cannot be kept

- **WHEN** `edit_note` replaces the server's own note with mode 0o600 and gid `<g>`, the server cannot set `<g>`, and the directory's default ACL grants a named group and other `rw-`
- **THEN** the published note SHALL grant no access to any principal other than its owner, as computed by the effective-access helper, and SHALL NOT carry an entry from the directory's default ACL

#### Scenario: Group translation on a filesystem without ACLs

- **WHEN** every xattr call fails with `EOPNOTSUPP`, and `edit_note` replaces a note with mode 0o664 whose gid the server cannot set
- **THEN** the published note's `st_mode & 0o777` SHALL be 0o604

#### Scenario: An upload into a setgid folder whose group the server is not in

- **WHEN** a no-overwrite upload is published into a setgid directory with gid `<g>` and default ACL `u::rwx,g::rwx,o::r-x`, the server is not in `<g>`, and `fchown` to `<g>` fails with `EPERM`
- **THEN** the published file SHALL hold `group::---`, `group:<g>:rw-` and `mask::rw-`, and the effective access the helper computes for a member of `<g>` SHALL equal what a control file created in that directory with `open(O_CREAT, 0o666)` grants that member

### Requirement: A publication at a name that did not exist SHALL get exactly what an ordinary new file gets in that directory

A publication that creates a name SHALL leave the published file with the permission bits, POSIX access ACL and gid the kernel gives a file created by `open(O_CREAT, 0o666)` in the destination directory. This covers `create_note`, `write_file` (by default, and with `overwrite=True` at a name that does not exist), a no-overwrite upload, and `import_from_url`.

- The bits and ACL come from the directory's default ACL where it has one, with the umask not applied. Otherwise they are `0o666 & ~umask`.
- The rule SHALL hold whatever directory the bytes were staged in, including `.transfer-tmp`.
- The wanted gid SHALL be the directory's gid when the directory has the setgid bit, and otherwise the gid observed on the staged inode the process just created. It SHALL NOT be derived from the process's effective gid.
- When the wanted gid cannot be set, the group translation requirement SHALL apply.
- Equality with the control file, `st_gid` included, SHALL hold for every write staged in the destination directory (notes and raw files), and for an upload wherever the wanted gid is kept. For an upload whose group is not kept, the published `st_gid` SHALL be the staged inode's, and on a filesystem with POSIX ACLs the translation SHALL give the wanted group the access the control file gives it. **On a filesystem without POSIX ACLs, an upload into a setgid directory whose group the server cannot set SHALL be published with the server's gid and the mode-only translation, so the directory's group gets only the other-class access** — narrower than an ordinary create, and the same as before this change.

#### Scenario: Group-writable default ACL

- **WHEN** `create_note` creates a note in a directory whose default ACL is `u::rwx,g::rwx,g:<group>:rwx,m::rwx,o::r-x`
- **THEN** the note's `st_mode & 0o777` (0o664), `st_gid` and `system.posix_acl_access` bytes SHALL equal those of a control file created in the same directory with `open(O_CREAT, 0o666)`, which carry `mask::rw-`

#### Scenario: Private default ACL

- **WHEN** `write_file` creates a file in a directory whose default ACL is `u::rwx,g::---,o::---`
- **THEN** the file's `st_mode & 0o777` SHALL be 0o600, its `st_gid` SHALL equal the control file's, and reading its `system.posix_acl_access` SHALL fail with `ENODATA`, exactly as for the control file

#### Scenario: No default ACL

- **WHEN** `create_note` creates a note in a directory with no default ACL
- **THEN** the note's `st_mode & 0o777` SHALL be `0o666 & ~umask` and reading its `system.posix_acl_access` SHALL fail with `ENODATA`

#### Scenario: An overwrite-mode write to a name that does not exist yet

- **WHEN** `write_file(overwrite=True)` writes a name that does not exist, in a directory whose default ACL grants a named group `rwx`
- **THEN** the file's mode bits, `st_gid` and ACL bytes SHALL equal those of the control file

#### Scenario: Upload into a foreign setgid folder on a filesystem without ACLs

- **WHEN** every xattr call fails with `EOPNOTSUPP`, the umask is 0o002, and a no-overwrite upload is published into a setgid directory with gid `<g>` that the server cannot set
- **THEN** the published file's `st_gid` SHALL be the staged inode's and its `st_mode & 0o777` SHALL be 0o604

#### Scenario: Upload staged under a different default ACL

- **WHEN** a no-overwrite upload is published into a directory with default ACL A, while `.transfer-tmp` carries a different default ACL B
- **THEN** the published file's mode bits, `st_gid` and ACL bytes SHALL equal those of a control file created in the destination directory, and SHALL NOT carry any entry of B that A lacks

### Requirement: A directory the server creates under a default ACL SHALL get what an ordinary mkdir gets there

When the parent directory has a default ACL, a vault directory the server creates (for `create_note`, `write_file`, `move_note`, an upload or import into a new folder, or the soft-delete tree under `.trash/`) SHALL be created with mode argument 0o777, so the kernel applies the default ACL exactly as for `mkdir(path, 0o777)`. When the parent has no default ACL, or its default ACL cannot be read for any reason, the mode argument SHALL stay 0o755, as before this change, and the directory path SHALL NOT refuse. `.transfer-tmp` SHALL be unchanged: its `mkdirat` mode argument SHALL stay 0o755 even when its parent has a default ACL, so it is never created group-writable, and it SHALL be set to and held at 0o700 on every open.

#### Scenario: A new folder under a group-writable default ACL

- **WHEN** `create_note` creates `New/x.md` and `New` does not exist, under a parent whose default ACL is `u::rwx,g::rwx,g:<group>:rwx,m::rwx,o::r-x`
- **THEN** `New`'s `st_mode & 0o777`, `system.posix_acl_access` bytes and `system.posix_acl_default` bytes SHALL equal those of a control directory created in the same parent with `os.mkdir(path, 0o777)`, which carry `mask::rwx`

#### Scenario: A new folder with no default ACL is unchanged

- **WHEN** `create_note` creates `New/x.md` under a parent with no default ACL, with the process umask set to 0o000
- **THEN** `New`'s `st_mode & 0o777` SHALL be 0o755

#### Scenario: The staging directory stays owner-only

- **WHEN** the first upload creates `.transfer-tmp` under a vault root with a group-writable default ACL
- **THEN** `.transfer-tmp`'s `st_mode & 0o777` SHALL be 0o700

### Requirement: A filesystem without POSIX ACLs SHALL keep the mode-only behaviour

`ENODATA`, `EOPNOTSUPP` and `ENOTSUP` from reading `system.posix_acl_access` or `system.posix_acl_default`, or from removing `system.posix_acl_access` from a staged inode, SHALL mean "no POSIX ACL here" and SHALL NOT be failures. On such a filesystem:

- an edit SHALL keep the replaced file's permission bits;
- a create SHALL get `0o666 & ~umask`;
- a group not kept SHALL take the mode-only translation.

#### Scenario: ACL calls unsupported

- **WHEN** every xattr call on the vault fails with `EOPNOTSUPP`, and `create_note` creates a note and `edit_note` changes a note with mode 0o600 whose gid the server holds
- **THEN** both calls SHALL succeed, the new note's `st_mode & 0o777` SHALL be `0o666 & ~umask`, and the edited note's SHALL be 0o600

### Requirement: Failing to apply the permission rule SHALL refuse the write and publish nothing

Any other failure while capturing or applying the permission rule SHALL publish nothing and SHALL leave the destination's bytes, permission bits and ACL as they were. This covers:

- reading the replaced file's or the directory's ACL;
- `fchown` failing with anything other than `EPERM` or `EINVAL`;
- `fsetxattr`, `fremovexattr` or `fchmod`;
- an unparsable ACL;
- a required ACL write refused with `EOPNOTSUPP` or `ENOTSUP`, including a translated ACL for a rule that has an ACL.

A note or file tool SHALL answer it as an `io_failure` refusal whose text says that the permissions could not be applied and that nothing was written. An upload SHALL treat it as a pre-publication failure: the claim is released and the token is `pending` again.

#### Scenario: An ACL write fails during an edit

- **WHEN** `edit_note` runs and setting `system.posix_acl_access` on the staged inode fails with `EIO`
- **THEN** the response SHALL end with `MCP-REFUSAL {"code":"io_failure"}`, its text SHALL say that nothing was written, and the note's bytes, `st_mode` and ACL bytes SHALL equal those before the call

#### Scenario: A translated ACL refused as unsupported when the rule has an ACL

- **WHEN** `edit_note` replaces a note whose access ACL is `u::rw-,g::rw-,g:<g2>:---,m::rw-,o::rw-`, the server cannot set the note's gid, and writing the translated ACL fails with `EOPNOTSUPP`
- **THEN** the response SHALL be the `io_failure` refusal, nothing SHALL be published, and the note's bytes, `st_mode` and ACL bytes SHALL be unchanged

#### Scenario: An ACL write fails during an upload

- **WHEN** an upload's permission step fails with `EIO`
- **THEN** nothing SHALL exist at, or change at, the bound path, no staged bytes SHALL remain, and the token SHALL be `pending`

### Requirement: Permissions SHALL be captured coherently, applied to the private staged inode before its single flush, and a change before publication SHALL be an in-call conflict

Staging inodes SHALL still be created with mode 0o600. Every write SHALL capture its permission rule once and apply it to the staged inode before the payload flush, so that the one existing flush covers it.

**Leaf captures.** A capture of a leaf SHALL be `fstat`, then a read of the access ACL, then `fstat` again. A changed `(st_dev, st_ino, st_ctime_ns)` between the two SHALL be an in-call conflict.

**Note and raw-file overwrites.** In this branch the server SHALL capture the leaf again immediately before publication: through the descriptor the `expected=` comparison reads the bytes from, taken **before** the bytes are read and compared, or through a fresh `O_PATH | O_NOFOLLOW` descriptor where there is no comparison. Nothing SHALL be added between the byte comparison and the rename. Any difference from the first capture SHALL publish nothing and SHALL raise the existing in-call conflict, `File changed while editing: <name>`, with or without `expected=`. A difference is any of:

- identity;
- gid;
- mode bits;
- ACL bytes;
- a regular file having appeared or vanished.

No write SHALL re-apply permissions or flush the staged file a second time.

**Named staged copies.** Where a write stages under a name in the destination directory (a note or raw-file overwrite, and the named no-clobber fallback), the server SHALL verify after the flush, and before the publish-point capture and the `expected=` comparison, that the staging name is still the staged regular inode and, where the filesystem lets the server read the staged copy back or the applied permissions give the owner read, holds exactly the payload (a read-back refused despite owner read SHALL refuse the write); any difference SHALL publish nothing and SHALL raise the in-call conflict.

**Uploads.** An upload's edit plan SHALL be captured once, from a leaf whose `(st_dev, st_ino, st_ctime_ns)` equals the token's fingerprint. Otherwise the upload SHALL be refused with HTTP 409.

**Create plans** (both writers) SHALL be captured once.

#### Scenario: Staged bytes are private while being written

- **WHEN** any write stages its payload
- **THEN** the staged inode SHALL be created with mode 0o600 before the first payload byte is written

#### Scenario: One flush per write

- **WHEN** `edit_note`, `create_note` or an upload publishes
- **THEN** exactly one `fsync` SHALL be issued on the staged file

#### Scenario: Permissions changed during an edit

- **WHEN** a note's mode is changed from 0o664 to 0o600 after the staged payload was written and before the `expected=` comparison, and the bytes still match
- **THEN** nothing SHALL be published, the response SHALL be the `concurrent_write` refusal carried by `File changed while editing: <name>`, and the note's bytes and mode SHALL be those the chmod left

#### Scenario: A file that appears during an overwrite-mode write

- **WHEN** `write_file(overwrite=True)` starts at an absent name, and a file appears at that name before publication
- **THEN** nothing SHALL be published by this call, the response SHALL carry `File changed while editing: <name>`, and the file that appeared SHALL be untouched

#### Scenario: An ACL changed in the middle of a capture

- **WHEN** a leaf's access ACL is changed between the capture's first `fstat` and its ACL read
- **THEN** the write SHALL publish nothing and SHALL report the in-call conflict

#### Scenario: An overwrite token's target whose permissions changed after the mint

- **WHEN** an overwrite token is minted and the target's mode is changed afterwards
- **THEN** the upload SHALL be refused with HTTP 409 and the target's bytes and mode SHALL be unchanged

#### Scenario: A staged copy altered before publication

- **WHEN** the bytes at a write's staging name are changed after the staged flush and before the publish-point capture
- **THEN** the write SHALL be refused with the in-call conflict, the destination's bytes, mode and ACL SHALL be unchanged, and no staging name SHALL remain

