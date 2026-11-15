## Context

Line references are at 85e8df5.

Both write paths stage into a new inode created 0600 and publish that inode. The publish is a `linkat` through `/proc/self/fd/<fd>` (no-clobber) or a `renameat` of a named staging file (overwrite).

- **Notes and raw files.** `vault._atomic_write_at` (`src/services/vault.py:2344`) stages *in the destination directory* (`target.dir_fd`):
  - an `O_TMPFILE` inode for no-clobber (`:2428`);
  - a `.tmp-<name>-…` name for overwrite (`:2425`);
  - a name for no-clobber on the `VAULT_ALLOW_NAMED_STAGING_FALLBACK` path (`:2432`).

  It then writes the payload, `os.fchmod(fd, vault_fs.default_file_mode())` (`:2452`), `fsync` (`:2453`), compares `expected=` through a descriptor that `_read_fd_bytes` opens and closes on its own (`:2460`, `:2589`), and publishes.
- **Uploads and imports.** `transfer._stream_locked` (`src/services/transfer.py:1808`) stages *in `.transfer-tmp`* (`:1858–1870`), not in the destination directory. It drains the body, `fchmod`s (`:1904`), flushes (`:1910`) and closes the staged descriptor in named mode before the gate (`:1911–1919`). Then, inside the gate, `_publish_into_current_parent` (`:1687`) re-resolves the destination parent (`:1752`) and calls `vault_fs.publish` (`:1773`).
- An overwrite token's binding is `vault_fs.fingerprint` (`src/services/vault_fs.py:1586`): device, inode, size, mtime_ns, ctime_ns, and sha256 where the file is small enough. `publish` re-checks it in `_require_committed_target` (`:1803`). Because `chmod` and `setxattr` update ctime, **any permission change after the mint already refuses the upload with 409**.

### Kernel facts this design relies on

All of these were measured on ext4 under Linux 6.8, with ACLs set through `os.setxattr` from a packed struct.

- **K1 (create).** A file created in a directory with a default ACL gets an access ACL equal to that default ACL. Within it, `USER_OBJ`, `MASK` (or `GROUP_OBJ` when there is no `MASK`) and `OTHER` are ANDed with the create mode's owner, group and other bits. Named entries are copied unchanged. The umask is not applied. A result equivalent to a plain mode is stored as mode only, with no xattr (`posix_acl_create`). With no default ACL, the mode is `mode & ~umask`. So `open(O_CREAT, 0o666)` and a 0600 staging inode in the same directory differ only in those three entries.
- **K2 (chmod).** `chmod` on an inode with an access ACL rewrites `USER_OBJ`, `MASK` (or `GROUP_OBJ`) and `OTHER` from the mode bits. That is the bug: `fchmod(0644)` sets `mask::r--`. Named entries and `GROUP_OBJ` under a mask are untouched.
- **K3 (setxattr).** Writing `system.posix_acl_access` updates the mode's permission bits from the ACL. An ACL equivalent to a mode is stored as no xattr, so a round trip of "absent" stays absent. The owner of an inode may write any valid ACL on it, including named entries for ids it is not, with no capability.
- **K4 (read).** Reading `system.*` xattrs has no VFS permission check, so a file the server cannot read still yields its ACL. On an `O_PATH` descriptor, `fgetxattr` fails with `EBADF` (measured on 6.8). The read has to go through `/proc/self/fd/<n>` (a path xattr call that follows the magic link). The no-clobber publication already depends on `/proc/self/fd` (`vault_fs.link_staged_inode`).
- **K5 (gid).** A new inode's gid is the directory's gid when the directory is setgid, otherwise the process's fsgid. `fchown(fd, -1, gid)` succeeds for the inode's owner only when the process is in `gid` (or holds `CAP_CHOWN`). It fails with `EPERM` otherwise, and with `EINVAL` for a gid not mapped in the process's user namespace. The reference pod runs uid/gid 1000 with all capabilities dropped.
- **K6 (mkdir).** `mkdir(name, 0o755)` under a default ACL `…,m::rwx,…` gives the new directory `mask::r-x` (measured). `mkdir(name, 0o777)` gives `mask::rwx`, identical to what `mkdir(1)` produces.

The create-rule formula in the issue checks out against K1 and K2: `fchmod(staged, 0o666 & (u<<6 | (mask if present else group_obj)<<3 | other))` on a staged inode that was created in the destination directory reproduces the control file's mode and ACL bytes exactly (measured for both a group-`rwx` and a private default ACL). It is **not** sufficient for uploads. Their staged inode inherited `.transfer-tmp`'s default ACL (the vault root's), not the destination's, and its gid follows `.transfer-tmp`. D2 therefore writes the computed ACL explicitly.

## Goals / Non-Goals

**Goals:**

- Edit keeps bits and access ACL; create equals an ordinary `open(O_CREAT, 0o666)`; directories equal an ordinary `mkdir(…, 0o777)`.
- A published file is never broader than its rule, including across a crash.
- Mode-only filesystems behave exactly as now.
- No extra `fsync` on an ordinary write.

**Non-goals:** see the proposal. Each excluded item is listed under the accepted limitations below.

## Decisions

### D1. Two rules, one plan

Every publication computes a **plan** `(gid, mode, acl_bytes | None)` before it writes any permission.

- **Edit plan:** the replaced file's `(st_gid, st_mode & 0o777, system.posix_acl_access or None)`, with the create plan as its fallback.
- **Create plan:**
  - `gid` is the directory's `st_gid` when the directory is setgid, otherwise `os.getegid()` (K5).
  - When the directory has a default ACL: parse it, AND `USER_OBJ`, `MASK` (or `GROUP_OBJ` with no `MASK`) and `OTHER` with 6/6/6 (K1 with mode 0o666), and keep named entries. `mode` is `u<<6 | (mask or group_obj)<<3 | other` of the result. `acl_bytes` is the packed result, or `None` when only the three base entries remain.
  - With no default ACL: `mode = default_file_mode()` and `acl_bytes = None`.

Which plan applies:

- **Edit plan:** a regular file stands at the name when the plan is captured, and the publication replaces it (the overwrite branch).
- **Create plan:** no-clobber publications, and an overwrite of an absent name or of a non-regular leaf (a symlink or FIFO replaced by `rename`; the "permissions of the replaced file" are not a regular file's permissions).

ACL encoding: little-endian `u32` version 2, then 8-byte entries `<HHI` (tag, perm, id). Tags are `USER_OBJ 0x01`, `USER 0x02`, `GROUP_OBJ 0x04`, `GROUP 0x08`, `MASK 0x10`, `OTHER 0x20`, and the base entries' id is `0xFFFFFFFF`. The parser refuses a wrong version, a length not `4 + 8n`, an unknown tag, a missing base entry, or entries out of the kernel's sort order. A refusal is a D6 failure. The kernel never returns such bytes.

### D2. Apply order: group first, then the ACL, then the mode; ACLs are always written explicitly

`apply(staged_fd, plan)`:

1. If `fstat(staged).st_gid != plan.gid`, call `fchown(staged, -1, plan.gid)`. On `EPERM` or `EINVAL`, an edit plan restarts with its create fallback. For a create plan, the plan is narrowed: `GROUP_OBJ` perm is set to 0, and when the ACL has no `MASK` (or there is no ACL) the mode's group bits are cleared. Nothing has been written yet, so the restart is clean.
2. If `plan.acl_bytes` is set, `fsetxattr(staged, "system.posix_acl_access", acl_bytes)`. Otherwise `fremovexattr` it, where `ENODATA`, `EOPNOTSUPP` and `ENOTSUP` mean there was nothing to remove.
3. `fchmod(staged, plan.mode)`. After step 2 this is consistent with the ACL (K2, K3) and also sets the mode on a filesystem without ACLs.

Writing the computed ACL explicitly (instead of relying on what the staged inode inherited) makes one code path correct for both writers. The upload's staged inode inherited the wrong directory's entries. A note's staged inode may have received an edit plan at the first capture and then need a create plan at the second (D3), and inheritance cannot undo an earlier explicit write. Measured: an `O_TMPFILE` staged in a directory with a private default ACL, given the computed ACL of a group-`rwx` directory and linked there, is byte-identical to the control file.

**The owner uid is never changed (decision on the issue's question).** `fchown` to another uid needs `CAP_CHOWN`. The reference pod drops every capability and runs `runAsNonRoot`, and the compose bundle's container is non-root too, so the call would be dead code on every documented deployment, and CI cannot exercise it unprivileged. A root deployment could make it succeed, but it would then hand the server-written bytes to another uid, which is a decision of its own. Keeping the bits and ACL when only the uid differs is right: the server could already replace the file (it holds write on the directory). The previous owner may lose owner-class access (L2).

### D3. Where the plan is captured, and how it is bound to the verified file

Two capture points, **P1** and **P2**. P1 is the one that normally takes effect. P2 is the binding.

- **P1: after the payload is written, before the payload `fsync`.**
  - In `_atomic_write_at`: capture the destination leaf (overwrite branch only) and the directory through `target.dir_fd`, plan, and apply. This replaces `vault.py:2452`.
  - In `_stream_locked`: after `_drain`, open the destination parent with `open_parent(…, create=False)`; the up-front walk already created it. Capture the leaf (overwrite token with a fingerprint only) and the directory, plan, apply to `staged_fd`, and close that parent. This replaces `transfer.py:1904`. A `FileNotFoundError` there means the parent vanished during the stream: P1 applies nothing, and P2 then counts as different.
  - The existing payload flush (`vault.py:2453`, `_flush_staged_payload`) then makes the permissions durable together with the bytes. `fsync` covers the inode's metadata and xattrs, so there is no additional flush on this path.
- **P2: at the publish point.**
  - In `_atomic_write_at`, `_read_fd_bytes` is replaced by a helper that keeps its descriptor. The `expected=` comparison reads the bytes, and the capture takes `fstat` and the access ACL **from that same descriptor**. With no `expected=`, P2 opens the leaf `O_PATH | O_NOFOLLOW` immediately before `_require_staged_name` / the publish (K4: no read permission needed, which matters for `write_file(overwrite=True)` over a file the server cannot read). The directory is re-read through `target.dir_fd` in both cases.
  - In `_publish_into_current_parent`, P2 runs after the two mount checks and before `vault_fs.publish`, through the gate's own `dst_fd`. For an overwrite token, the leaf captured at P2 must match the fingerprint's `(dev, inode, ctime_ns)`; otherwise the upload is refused with `vault_fs.Conflict("Target changed since the token was minted: …")`, which is the existing 409. `publish`'s own `_require_committed_target` then re-checks the same fingerprint.
  - If P2's computed plan differs from P1's (compared before any `fchown` fallback), apply P2's plan and **`fsync` the staged inode once more** before publishing. In the upload named mode, the staged descriptor was closed before the gate. The tmp name is reopened `O_RDONLY | O_NOFOLLOW | O_NONBLOCK` in the staging directory, its identity checked against `staged_st` (a mismatch is the existing `Conflict`), and the plan applied through that descriptor.

**What this binds.** In both writers, the permissions published are those of the file whose bytes the `expected=` comparison (or the fingerprint check) accepted, read through a descriptor on that very inode.

- **Swapped destination.** If the destination is swapped after P2 and before the rename, the rename replaces whatever is there, and the published file carries the verified file's permissions. This is the existing declared window (`_atomic_write_at` docstring, "What the overwrite window means"; D5 on the transfer path), and this change does not widen it.
- **Permissions changed after P2.** A change to the destination's permissions after P2 and before the rename is lost with the replaced inode (L8). That window is a few syscalls.
- **Permissions changed earlier.**
  - Between P1 and P2: captured at P2 and published (scenario "Permissions changed during the write are the ones published").
  - On an upload, at any time after the mint: refused by the ctime in the fingerprint.

**Why the second flush is needed, and why only there.** Without it, a P2 re-application could be lost in a crash after publication. The file would then show P1's permissions, which may be broader than the rule at the moment of publication (a destination chmod'ed from 0664 to 0600 during the write would come back 0664). The extra `fsync` runs only when the destination's permissions, gid, ACL, existence or directory default ACL changed during the call, so an ordinary write still flushes once. On the upload path it runs under the gate's locks, but only in that race.

**Durability, stated.** On every path, the permissions a published file carries were flushed to the staged inode before the `link`/`rename` that published it. A crash before publication leaves the previous file untouched. A crash after it leaves the new file with exactly the published permissions, or, if the directory entry itself was not yet durable, the previous file.

### D4. Ownership

The published inode is owned by the server's uid (K5). Its gid:

- Edit: the replaced file's, when `fchown` can set it. When it cannot, the create plan applies, and so does its gid.
- Create: the gid an ordinary create gets. For notes it is already right, because the staged inode was created in the destination directory. For uploads it may need the `fchown`. When that fails (destination setgid to a group the server is not in), the owning group's permissions are cleared (L13).

### D5. Mode-only filesystems

`ENODATA`, `EOPNOTSUPP` and `ENOTSUP` mean "no POSIX ACL":

- reading the leaf's access ACL gives `acl_bytes = None`, and the edit keeps the bits;
- reading the directory's default ACL selects `0o666 & ~umask`;
- removing the staged inode's access ACL is a no-op.

On such a filesystem `apply` is `fchmod` alone. That equals today's behaviour for creates and keeps the bits for edits.

### D6. Any other failure refuses, and publishes nothing

**Options considered:**

- **(a) Refuse.** Raise `vault_fs.PermissionsNotApplied`, an `OSError` subclass, before publication.
- **(b) Publish with a stated fallback.**

**Decision: (a).**

- **No useful fallback is provably never broader.** The staging permissions (owner `rw-`, group and other nothing, named entries masked out) are narrower for every principal but the server. They are still broader in the owner class than a replaced 0400 file, and they silently strip a group's access, which is the bug being fixed. Mode `0o000` is never broader, but it makes the server unable to read its own note.
- **The failing calls should not fail on a working host.** Each operates on an inode the server owns, or on a descriptor it opened: `fgetxattr` on `system.*` has no permission check (K4), and `fsetxattr`/`fchmod`/`fremovexattr` are allowed for the owner. What remains is `EIO`, `ENOSPC`/`EDQUOT` (an ext4 ACL that does not fit in the inode needs a block), `EROFS`, or an LSM or seccomp denial. Under any of those the payload write itself is in doubt.
- **A refusal on this path is the safe kind.** Nothing is published, the previous bytes and permissions stand, and a retry is harmless: `edit_note(append=True)` retried after a refusal appends once.
- **The excluded errnos keep every ACL-less filesystem working.** A host that cannot do ACLs at all is not refused (D5).

**Rendering:** no new refusal code. Because `PermissionsNotApplied` subclasses `OSError` and not `VaultFSError`, every write tool's existing `except OSError` answers `io_failure`. A `VaultFSError` would be mapped to `validation_failed` first (`src/mcp_server/tools.py:4715–4718`). The message names the step and the errno and says "nothing was written". An upload reaches the route's generic pre-publication branch: `transfer_prepublish_failure`, claim released, HTTP 500. One `logger.error` line names the step, errno and vault-relative path; no new security event.

`EPERM`/`EINVAL` from the gid `fchown` are not failures (D2 step 1). A required ACL *write* refused with `EOPNOTSUPP`/`ENOTSUP` is a failure: the plan has an ACL, so the directory reported one on the same filesystem, and publishing without it would drop named entries.

### D7. Directories

`vault_fs._create_descent` (`src/services/vault_fs.py:744`) calls `os.mkdir(…, 0o755)`. Under a group-`rwx` default ACL that gives `mask::r-x` (K6), so a folder the server creates is not group-writable. Under a umask looser than 022 it is narrower than `mkdir(1)`'s result. That is the same bug for folders, and in the issue's scenario a ticket filed into a new folder would hit it. The fix is the mode argument 0o777, so the kernel applies the parent's default ACL or the umask exactly as for any `mkdir`.

`open_dir_beneath` gains a `leaf_mode` keyword (default 0o777) used only by `open_staging_dir`, which passes 0o700. Its existing on-every-open `fchmod(0o700)` and verification stay.

Under umask 022 and no ACLs (the reference container) the result is the 0o755 it is today. A deployment running umask 0 now gets 0o777 folders, which is what `mkdir` gives there, matching how files already follow the umask. That goes in the upgrade note.

### D8. No setting

Nothing here needs one. There is no way to ask for the old behaviour, which was the bug. A filesystem without ACLs needs no switch (D5).

## Scope inventory

Every caller of `vault_fs.default_file_mode()` and every publish path, verified at 85e8df5.

| Path | Publication | Rule after this change |
| --- | --- | --- |
| `vault._atomic_write_at`, overwrite branch, regular file at the name (`edit_note`, `set_frontmatter`, `complete_task` / `update_task` / `add_task`, `move_note` link rewrites, `write_file(overwrite=True)` over a file, `vault.write_file` / `write_bytes` conveniences) | named staging + `renameat` (`vault.py:2470`) | **Edit** (create if the gid cannot be kept) |
| `vault._atomic_write_at`, overwrite branch, name absent or a non-regular leaf (`write_file(overwrite=True)` to a new name) | named staging + `renameat` | **Create** (edit if a file appears before P2) |
| `vault._atomic_write_at`, no-clobber, `O_TMPFILE` (`create_note`, `write_file` default) | `linkat` via `/proc/self/fd` (`vault.py:2492`) | **Create** |
| `vault._atomic_write_at`, no-clobber, named-staging fallback | named staging + `link` (`vault.py:2490`) | **Create** |
| `transfer._stream_locked` → `publish`, no-overwrite upload, `import_from_url`, overwrite token minted over an absent target (fingerprint `None` = expect absence) | unnamed: `linkat`; named: `link` from `.transfer-tmp` | **Create**, ACL computed from the destination directory (staged elsewhere) |
| `transfer._stream_locked` → `publish`, overwrite token with a fingerprint (`request_upload(overwrite=True)`, `import_from_url` overwrite) | transient or named name in `.transfer-tmp` + `renameat` | **Edit**, P2 bound by `(dev, inode, ctime_ns)` |
| `vault_fs.default_file_mode()` | only caller after the change: the create plan with no default ACL | n/a |
| `move_note`, the moved note | `renameat2(RENAME_NOREPLACE)` | **Unchanged**: rename keeps the inode, mode and ACL (as `mv`) |
| `delete_note` / `delete_file`, soft delete to `.trash/` | `renameat2` | **Unchanged**: rename keeps the inode |
| `delete_note(permanent=True)` / `delete_file(permanent=True)` | `unlinkat` | **Unchanged**: nothing published |
| `open_dir_beneath(create=True)`: parents for `create_note`, `write_file`, `move_note`, uploads (both descents), the `.trash/` tree | `mkdirat` (`vault_fs.py:744`) | **Directory rule**: mode 0o777, kernel applies default ACL / umask |
| `open_staging_dir`: `.transfer-tmp` | `mkdirat` + `fchmod(0o700)` on every open | **Unchanged**: created 0o700, held at 0o700 |
| `probe_publication`, `probe_trash` | transient files, unlinked by the probe | **Unchanged**: never left behind |
| `prune_stale_staging` | `unlinkat` | **Unchanged** |

No other writer creates files in the vault: the indexer, the panel and the change feed write only to the database. Search-eval exports are written outside the vault, at 0600 by design.

## Accepted limitations (session, pending owner)

- **L1.** Files that earlier releases narrowed (`mask::r--`) or widened (0600 → 0644) are not repaired. The upgrade note says how an operator can.
- **L2.** The owner uid is not kept. A file owned by another uid becomes the server's on edit, and its previous owner may lose owner-class access. No `fchown` of the uid is attempted (D2).
- **L3.** When the replaced file's group cannot be set on the new inode, the file gets the create rule, which can differ from what it had.
- **L4.** NFSv4 ACLs and richacl are not preserved. Only `system.posix_acl_access` is.
- **L5.** Other extended attributes (`user.*`, `security.*`, `trusted.*`) are lost on edit, as before.
- **L6.** Hard links are broken by the rename, as before.
- **L7.** setuid, setgid and sticky bits are never carried.
- **L8.** A permission change made after P2 and before the rename is lost with the replaced inode.
- **L9.** `move_note` keeps the moved file's mode and ACL even across folders with different default ACLs, as `mv` does.
- **L10.** In a user-namespaced container, ACL entries that name ids outside the namespace's map read back as the overflow id. They are written back as that id, or refused with `EINVAL` (→ `io_failure`) when the overflow id is itself unmapped. The documented deployments use no user namespace.
- **L11.** A directory's default ACL changed after P2 and before the publish is not seen.
- **L12.** The upload refusal for D6 is a generic HTTP 500 with the claim released, not a dedicated body.
- **L13.** An upload into a setgid directory whose group the server is not in gets the create rule with the owning group's permissions cleared, which is narrower than what an ordinary create by a member would get.

## Risks / Trade-offs

- **A host where ACL calls fail for another reason now refuses writes.** D6 explains why this is the right direction. The failure is loud (`io_failure`, an ERROR line) and names the step.
- **Up to about ten extra syscalls per write,** for the two captures and the apply, with no extra flush on the ordinary path.
- **One existing test pins the old policy:** `tests/test_transfer_service.py::test_overwrite_resets_the_incumbent_mode_to_server_policy`, whose docstring calls preserving the incumbent's bits "a product decision affecting both". That decision is now taken, and the test is inverted. `tests/test_vault_mutation_safety.py::test_written_note_keeps_the_umask_default_mode` stays valid as written (no default ACL, create rule).

## Test plan

New module `tests/test_preserve_file_permissions.py`, on the real filesystem under `tmp_path`.

**The probe.** A fixture writes a default ACL on a `tmp_path` directory with `os.setxattr` and a packed struct, with no dependency on `setfacl`. On `EOPNOTSUPP`/`ENOTSUP` it skips. When `OMCP_TEST_REQUIRE_POSIX_ACL=1` it fails instead. That variable is set in `.github/workflows/ci.yml`'s `tests` job env. It is not a `Settings` field, so `SETTINGS_ENV_KEYS` and `CONTROLLED_TEST_ENV` do not change and `test_conftest_env_hermeticity` passes unchanged. Only the test module reads it, through `os.environ`.

**Oracles:**

- Create: a control file made with `os.open(name, O_CREAT|O_WRONLY, 0o666, dir_fd=…)` in the same directory has the same `st_mode & 0o777` and the same `system.posix_acl_access` value (bytes, or `ENODATA`) as the published file.
- Edit: `(st_mode & 0o777, access ACL value)` before equals after.
- Directory: a control made with `os.mkdir(…, 0o777)` has the same mode, access ACL and default ACL.

**Cases, each in both directions:**

- Group-`rwx` default ACL with a named group entry, and private default ACL / 0600 file.
- The helpers directly.
- `create_note`, `edit_note`, `set_frontmatter`, `write_file` (create, overwrite, overwrite to an absent name), and `stream_to_vault` (create, overwrite token, overwrite token after a chmod giving 409).
- Directories, including `.transfer-tmp` staying 0700.
- The gid fallback (monkeypatched `fchown` → `EPERM`), and a supplementary-group keep where the process has one.
- `EOPNOTSUPP` everywhere (monkeypatched) giving mode-only.
- `EIO` on `fsetxattr` giving `io_failure` with nothing changed, and a released claim for an upload.
- The P2 race: a hook between P1 and the comparison chmods the destination; assert the published mode and two staged `fsync`s. The ordinary path asserts one `fsync`.
- The issue's own reproduction table, as a regression test.
