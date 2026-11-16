## Context

Line references are at 85e8df5.

Both write paths stage into a new inode created 0600 and publish that inode. No-clobber publications use `linkat` through `/proc/self/fd/<fd>`. Overwrites use `renameat` of a named staging file.

- **Notes and raw files.** `vault._atomic_write_at` (`src/services/vault.py:2344`) stages *in the destination directory* (`target.dir_fd`):
  - `O_TMPFILE` for no-clobber (`:2428`);
  - a `.tmp-<name>-…` name for overwrite (`:2425`) and for the no-clobber `VAULT_ALLOW_NAMED_STAGING_FALLBACK` path (`:2432`).

  Then it writes the payload, `fchmod(fd, vault_fs.default_file_mode())` (`:2452`), `fsync` (`:2453`), compares `expected=` through a descriptor that `_read_fd_bytes` opens and closes on its own (`:2460`, `:2589`), and publishes.
- **Uploads and imports.** `transfer._stream_locked` (`src/services/transfer.py:1808`) stages *in `.transfer-tmp`* (`:1858–1870`). It drains the body, `fchmod`s (`:1904`), flushes (`:1910`), and in named mode closes the staged descriptor before the gate (`:1911–1919`). Inside the gate, `_publish_into_current_parent` (`:1687`) re-resolves the destination parent (`:1752`) and calls `vault_fs.publish` (`:1773`).
- **`.transfer-tmp` is never setgid while staging.** `open_staging_dir` compares `stat.S_IMODE` (which includes the setgid bit) with 0o700 and `fchmod`s to 0o700, then verifies the result (`src/services/vault_fs.py:1263–1284`). So a staged upload inode gets the process's filesystem gid.
- **An overwrite token's binding** is `vault_fs.fingerprint` (`:1586`): device, inode, size, mtime_ns, ctime_ns, and sha256 when small. `publish` re-checks it (`_require_committed_target`, `:1803`). `chmod` and `setxattr` update ctime, so a permission change after the mint already refuses the upload with 409.

### Kernel facts

All measured on ext4 under Linux 6.8, with ACLs set through `os.setxattr` from a packed struct.

- **K1, create.** A file created in a directory with a default ACL gets that ACL as its access ACL. `USER_OBJ`, `MASK` (or `GROUP_OBJ` when there is no mask) and `OTHER` are ANDed with the create mode. Named entries are copied unchanged. The umask is not applied. A result equivalent to a mode is stored without an xattr. With no default ACL, the mode is `mode & ~umask`. The gid is the directory's when it is setgid, otherwise the creator's filesystem gid.
- **K2, chmod.** `chmod` on an inode with an access ACL rewrites `USER_OBJ`, `MASK` (or `GROUP_OBJ`) and `OTHER` from the mode. This is the bug: `fchmod(0644)` sets `mask::r--`.
- **K3, setxattr.** Writing `system.posix_acl_access` sets the mode's permission bits from the ACL. An ACL equivalent to a mode is stored as no xattr. The inode's owner may write any valid ACL, including named entries for ids it is not. An ACL with a named entry must have a `MASK`.
- **K4, reading.** `system.*` xattr reads have no VFS permission check. `fgetxattr` on an `O_PATH` descriptor fails with `EBADF` (measured), so it goes through `/proc/self/fd/<n>`. Where `/proc` is unavailable, it goes through a second `O_RDONLY | O_NOFOLLOW | O_NONBLOCK` open of the name whose `(st_dev, st_ino)` must equal the `O_PATH` descriptor's; only a file the server cannot open for reading is then refused (D6).
- **K5, fchown.** `fchown(fd, -1, gid)` succeeds for the owner only when the process is in `gid`, or holds `CAP_CHOWN`. Otherwise it fails with `EPERM`, or with `EINVAL` when the gid is unmapped in a user namespace. The reference pod runs uid/gid 1000 with every capability dropped, and the compose image runs as a non-root `USER`.
- **K6, mkdir.** `mkdir(name, 0o755)` under a default ACL with `m::rwx` gives `mask::r-x`. `mkdir(name, 0o777)` gives `mask::rwx`, as `mkdir(1)` does.
- **K7, evaluation** (`acl(5)`, `posix_acl_permission`), for an unprivileged process:
  1. effective uid equals the owner: `USER_OBJ` decides;
  2. else a named `USER` entry for the uid decides, masked;
  3. else, among `GROUP_OBJ` (when the process is in the owning group) and the named `GROUP` entries for its groups (all masked when a mask exists), access is granted if any one matching entry holds every requested permission, and refused if entries matched but none did, with no fall-through;
  4. else `OTHER` decides.

  Without an ACL, the same order applies with owner, group and other bits.

The issue's create formula, `fchmod(staged, 0o666 & (u<<6 | (mask or group_obj)<<3 | other))`, is correct for a staged inode created in the destination directory, by K1 and K2. It is not correct for uploads: their inode inherited `.transfer-tmp`'s default ACL (the vault root's). D2 therefore always writes the computed ACL explicitly. Measured: an `O_TMPFILE` staged under a private default ACL, given a group-`rwx` directory's computed ACL and linked there, is byte-identical to the control file.

## Goals / Non-Goals

**Goals:**

- An edit keeps the replaced file's bits, access ACL and group. A create equals `open(O_CREAT, 0o666)`.
- Where the group cannot be set, access is translated, never broadened.
- Mode-only filesystems behave as now.
- One flush per write.
- The compare-to-rename window is not lengthened.

**Non-goals:** see the proposal and L1–L12.

## Decisions

### D1. Plans

Before writing any permission, every publication computes a plan `(wanted_gid, mode, acl | None)`. No plan has a fallback to another plan.

- **Edit plan.** Used when the overwrite branch finds a regular file at the name. It is the replaced file's `(st_gid, st_mode & 0o777, access ACL or None)`.
- **Create plan.** Used for no-clobber, for an absent name, and for a non-regular leaf.
  - `wanted_gid`: the directory's `st_gid` when the directory is setgid; otherwise `fstat(staged).st_gid`, which by K1 is what an ordinary create gets: for notes it was staged in that directory, for uploads `.transfer-tmp` is not setgid. Never `os.getegid()`.
  - Equality with an ordinary create, gid included, holds for writes staged in the destination directory (notes and raw files) and for an upload whose group is kept. An upload whose group is not kept keeps the staged gid and gets the translation. On a filesystem without ACLs, an upload into a setgid folder whose group the server is not in therefore gives that group only other-class access (L13).
  - From a default ACL: AND `USER_OBJ`, `MASK` (or `GROUP_OBJ`) and `OTHER` with 6, and keep named entries. `mode` follows from the result. `acl` is `None` when only the three base entries remain.
  - Without a default ACL: `mode = default_file_mode()` and `acl = None`.

ACL encoding: a little-endian `u32` version 2, then 8-byte `<HHI` entries (tag, perm, id). The tags are `USER_OBJ 0x01`, `USER 0x02`, `GROUP_OBJ 0x04`, `GROUP 0x08`, `MASK 0x10` and `OTHER 0x20`, and the base entries' id is `0xFFFFFFFF`. The parser refuses:

- a wrong version;
- a length not `4 + 8n`;
- an unknown tag;
- permission bits outside `rwx`;
- a missing base entry;
- named entries without a `MASK`;
- entries out of sort order, or repeated.

The kernel never returns the last four. A refusal is a D6 failure.

### D2. Apply: group, then translation if needed, then ACL, then mode

`apply(staged_fd, plan, acl_capable)`:

1. **Group.** If `fstat(staged).st_gid != plan.wanted_gid`, call `fchown(staged, -1, wanted_gid)`. If that fails with `EPERM` or `EINVAL`, the group is not kept, and the plan is translated (D3). Nothing has been written yet.
2. **ACL.** If `plan.acl` is set, `fsetxattr` it. Otherwise `fremovexattr`, where `ENODATA`, `EOPNOTSUPP` and `ENOTSUP` mean there was nothing to remove.
3. **Mode.** `fchmod(staged, plan.mode)`. This is consistent with the ACL by K2 and K3, and it is the whole of the step on a mode-only filesystem.

The ACL is always written explicitly, never left to inheritance, because the upload's staged inode inherited the wrong directory's entries.

### D3. Group translation, and the proof that it is never broader

Let `G` be the wanted gid and `G'` the staged inode's gid, which stays. Let `R` be the rule's ACL. When the rule has no ACL, `R` is the minimal ACL of its mode: `USER_OBJ = u`, `GROUP_OBJ = g`, `OTHER = o`.

**With POSIX ACLs.** The translated ACL `T` is `R` with these changes:

- `GROUP_OBJ` becomes `---`.
- A named `GROUP:G` entry is added with `R`'s `GROUP_OBJ` permissions. When `R` already has a `GROUP:G` entry, it is left exactly as it is: a union could grant a request neither entry granted alone, because K7 step 3 is any-single-entry.
- `MASK` is `R`'s mask when present. Otherwise it is the OR of `T`'s group-class entries. `R` without a mask is minimal (K3), so that OR is `R`'s `GROUP_OBJ` permissions, and the new entry's effective permission equals its own.
- Entries stay in sort order.
- `mode` becomes `u<<6 | mask<<3 | o`, numerically the rule's mode.

**Mode-only.** This applies only when the rule has no ACL, so it has no named entries: its source had no access ACL xattr, or the filesystem reported ACLs unsupported. It is taken when the filesystem has no POSIX ACLs, or when writing `T` for such a rule fails with `EOPNOTSUPP`/`ENOTSUP`. The mode becomes the rule's mode with the group bits cleared and `o' = o & g`. **When the rule has an ACL and writing `T` fails for any reason, that is the D6 refusal.** A mode cannot express a denying named entry (round 2, R2-1: `g:<g2>:---` with `o::rw-` would publish `rw-` to `<g2>` through other).

**Property.** Let `S` be the server's uid and `O` the replaced file's previous owner (for a create, `O = S`). Consider an unprivileged principal `p ∉ {S, O}`, with no `CAP_DAC_*`. Its access to the published file is a subset of its access under the rule:

- for an edit, the replaced file as it was;
- for a create, the file an ordinary create would have produced, with owner `S` and gid `G`.

Without translation, the bits, ACL and gid equal the rule's, so access is equal.

With translation it is equal, except for two groups of principals:

- **(a)** members of `G'` who match no other group entry;
- **(b)** when `R` already had a `GROUP:G` entry, members of `G` who were granted a request only by `GROUP_OBJ`.

Both get a subset.

*Proof, walking K7 for `p ∉ {S, O}`:*

- **Step 1.** `p` is the owner neither before (`O`) nor after (`S`). The step never applies.
- **Step 2.** Named `USER` entries are unchanged. The mask is unchanged when `R` had one. When `R` had none, `R` is minimal and has no named users. So step 2 decides identically.
- **Step 3.** Before, `p`'s matching set is `GROUP_OBJ(R)` if `p ∈ G`, plus `R`'s named groups for `p`. After, it is `GROUP_OBJ = ---` if `p ∈ G'`, plus `T`'s named groups for `p`, which are `R`'s plus `GROUP:G` when it was added. Masked effective values are unchanged (above).
  - `p ∈ G` (whether or not `p ∈ G'`), `GROUP:G` added: `GROUP_OBJ(R)`'s permission moves to `GROUP:G` with the same effective value, and a `---` entry, if matched, grants nothing and adds no match that changes the outcome, since `p` matches `GROUP:G` anyway. Equal.
  - `p ∈ G`, `GROUP:G` pre-existing: the set loses `GROUP_OBJ(R)`. Subset. This is (b).
  - `p ∉ G`, `p ∈ G'`: if `p` matched other group entries, the set gains only `---`. Equal. If not, `p` fell through to `OTHER` before and now matches `---` and is refused, which is the empty set. This is (a).
  - `p ∉ G ∪ G'`: the set is unchanged.
- **Step 4.** `OTHER` is unchanged and is reached by the same principals, except (a).

*Mode-only (rule without ACL only).* Before: owner `O` with `u`, group `G` with `g`, other `o`, and no named entries. After: owner `S` with `u`, group `G'` with `---`, other `o & g`. For `p ∉ {S, O}`:

- **Owner class:** `p` is in it neither before nor after.
- **Group class after** (`p ∈ G'`): `p` gets nothing, a subset of anything.
- **Otherwise,** `p` is in the other class after and gets `o & g`. That is a subset of `g` if `p ∈ G` (it had the group class) and a subset of `o` if not (it had the other class).

Subset for all; equality is not claimed. ∎

The issue's private-file case: the server's own 0600 file with gid `G`, edited under a permissive default ACL. Before the fix, that edit published 0644. A create-plan fallback would publish the folder's grants. Translation publishes `group::---`, `group:G:---`, `mask::---` and `other::---`, which grants nothing to anyone but the owner.

### D4. Ownership

The published inode belongs to the server's uid. The gid is the wanted one, or `G'` under translation. **There is no uid `fchown`, no refusal when the owner differs, and no named-user entry for the previous owner** (round 1, F2, declined):

- **Bits do not bound an inode's owner.** The owner can always `chmod`, so no choice of bits changes what the server can do with an inode it created.
- **The bytes are new.** They are the bytes the server wrote; the old bytes are gone with the old inode.
- **A refusal would break edits that work today on a plausible host**, for example notes created on disk by another service account.
- **The previous owner becomes an ordinary principal (K7 steps 2–4).** Where its owner-class permission was lower than its group or other class, it can end up with more. As the owner, it could already `chmod`.
- **A uid `fchown` would be dead code.** It works only with `CAP_CHOWN` (K5), which no documented deployment has.

### D5. Capture points, coherence, and the in-call conflict

- **Coherent leaf capture.** `fstat`, read the access ACL (through the comparison's descriptor, or `/proc/self/fd/<n>` of an `O_PATH | O_NOFOLLOW` one, or without `/proc` a readable descriptor of the same inode, K4), then `fstat` again. A different `(st_dev, st_ino, st_ctime_ns)` is the in-call conflict: `RuntimeError("File changed while editing: <name>")` in `vault`, `vault_fs.Conflict` on uploads. A directory capture reads `fstat` (setgid bit, gid, identity) and the default ACL, which are independent facts. It is not ctime-checked, because entry churn changes a directory's ctime.
- **Notes and raw files, P1.** After the payload is written and before the existing `fsync`, capture the leaf (overwrite branch only) and the directory through `target.dir_fd`, plan, and apply. This replaces `vault.py:2452`. The one `fsync` covers the permissions, since `fsync` flushes the inode's metadata and xattrs.
- **Notes and raw files, P2** (overwrite branch only). `_read_fd_bytes` is replaced by a helper that keeps its descriptor. The leaf is captured again (`fstat`, ACL, `fstat`) through that descriptor **before** the `expected=` comparison reads and compares the bytes. With no `expected=`, it is captured through a fresh `O_PATH | O_NOFOLLOW` descriptor immediately before `_require_staged_name`. Any difference from P1 is the in-call conflict, so nothing is published and the tools' existing `concurrent_write` handling and retries apply, with or without `expected=`. A difference is any of:
  - identity;
  - gid;
  - mode bits;
  - ACL bytes;
  - a regular file having appeared or vanished.

  There is no re-apply and no second `fsync`. **The compare-to-rename window is exactly today's**: nothing is added between the byte comparison and `_require_staged_name` / the rename. Without `expected=`, the `O_PATH` capture sits where the comparison would. A permission change after the P2 capture is lost with the replaced inode (L7). A content write after the comparison is overwritten, as today.
- **Uploads.** One capture, after `_drain` and before `_flush_staged_payload`, replacing `transfer.py:1904`. The destination parent is opened with `open_parent(…, create=False)`. A missing parent is a `Conflict` (409, claim released): the up-front walk created it, so it vanished during the stream.
  - For an overwrite token with a fingerprint, the leaf capture's `(st_dev, st_ino, st_ctime_ns)` must equal the fingerprint's, or `Conflict("Target changed since the token was minted: …")`. `publish`'s own fingerprint re-check inside the gate covers every later change. The existing mount-boundary checks (`require_same_mount`, `require_leaf_on_same_mount`) run before that comparison, so a destination or leaf bind-mounted after the mint is still refused as a `MountBoundary` (503, claim released), not as a 409 "Target changed".
  - For a create plan, the gate compares `fstat(dst_fd)`'s `(st_dev, st_ino)` with the captured directory's, and a mismatch is `Conflict`. This is an identity check, not a second capture.
  - The named-mode staged descriptor is still open at this point (it closes after the flush), so nothing is reopened.
- **Create plans** are captured once. A change to the directory's default ACL during the call is not seen (L10).
- **Staged-bytes verification** (named staging only: note and raw-file overwrites, and the named no-clobber fallback). From `apply_plan` on, a staging name in the destination directory carries the note's own permissions, so a principal they let write the note can also write the `.tmp-*`, and the staged-name check proves identity, not bytes. After the single `fsync` and **before** P2 and the comparison, the name must still be the staged regular inode, of the payload's size, holding exactly the payload: compared in bounded chunks against a `memoryview`, read back through the staging descriptor (opened read-write at creation, so on a local filesystem a kept mode without owner read does not make the read fail; where a network filesystem refuses that read, the comparison is skipped only if the applied permissions give the owner no read, otherwise the write is refused, L15). A difference is the in-call conflict. It sits before the comparison so the compare-to-rename window stays exactly today's; what remains after it is L14. Uploads need none of this: their named staging is in `.transfer-tmp` (0700) and their unnamed staging has no name.

**Durability.** Every published file's permissions were flushed with its payload before the `link`/`rename` that published it. A crash shows the old file, or the new file with its final permissions.

### D6. Any other failure refuses and publishes nothing

**Options:** (a) refuse, by raising `vault_fs.PermissionsNotApplied`, an `OSError` subclass, before publication; (b) publish with a fallback. **Decision: (a).** Codex ruled keep in round 1.

- **No useful fallback is provably never broader.** The staging permissions strip a group's access silently, which is the bug. Mode `0o000` locks the server out of its own note.
- **The failing calls act on an inode the server owns or a descriptor it opened.** What remains is `EIO`, `ENOSPC`/`EDQUOT` (an ACL needing an xattr block), `EROFS`, or an LSM or seccomp denial. Under any of these the write is in doubt anyway.
- **A refusal here is the safe kind.** Nothing is published, and a retry is harmless.
- **ACL-less filesystems are not refused.** `ENODATA`, `EOPNOTSUPP` and `ENOTSUP` on reads and on removal mean "no ACL" (D7).

**Rendering:** no new code. `PermissionsNotApplied` is not a `VaultFSError`, which the tools map to `validation_failed` first (`src/mcp_server/tools.py:4715–4718`). Every write tool's `except OSError` therefore answers `io_failure`, with a message naming the step and errno and saying "nothing was written". On an upload it reaches the route's generic pre-publication branch: `transfer_prepublish_failure`, claim released, HTTP 500. One ERROR log line is written, naming the step, the errno and the vault-relative path.

**Not failures:**

- `EPERM` and `EINVAL` from the gid `fchown` (D3).
- `EOPNOTSUPP` and `ENOTSUP` from writing a translated ACL **for a rule with no ACL**. These take the mode-only translation. For a rule with an ACL, they are a failure.

A required non-translated ACL write refused with `EOPNOTSUPP`/`ENOTSUP` **is** a failure: the directory reported an ACL on the same filesystem, and dropping it would remove named entries.

### D7. Mode-only filesystems

- Leaf read unsupported: the edit keeps the bits.
- Directory read unsupported: `0o666 & ~umask`.
- Removal is a no-op.
- `apply` is `fchown` (if needed) plus `fchmod`.
- A group not kept takes the mode-only translation (the rule has no ACL here by construction).

### D8. Directories, only under a default ACL

`vault_fs._create_descent` (`src/services/vault_fs.py:744`) uses `os.mkdir(…, 0o755)`. Under a group-`rwx` default ACL that gives `mask::r-x` (K6): a folder the server creates is not group-writable, which is the issue's bug for folders.

- **Parent has a default ACL:** read it through the prefix descriptor before each `mkdirat` and pass mode 0o777. The kernel ignores the umask there and applies the default ACL, exactly as `mkdir` does.
- **No default ACL, or it cannot be read for any reason:** 0o755, byte-identical to today, never broader, and the directory path never refuses.
- **`.transfer-tmp`:** unchanged. `open_staging_dir` calls `open_dir_beneath(follow_default_acl=False)`, so its `mkdirat` mode argument stays 0o755 even when the vault root has a default ACL, and the folder is never created group-writable before its `fchmod(0o700)`.

The non-ACL directory mode (0o755 versus `mkdir(1)`'s `0o777 & ~umask`) is deliberately left alone here. A separate issue will be filed. If a default ACL is removed between the read and the `mkdirat`, the new folder is `0o777 & ~umask` (L11).

### D9. No setting

There is no way to ask for the old behaviour, which was the bug. Filesystems without ACLs need no switch.

## Scope inventory

Verified at 85e8df5: every caller of `vault_fs.default_file_mode()` and every publish path.

| Path | Publication | Rule after this change |
| --- | --- | --- |
| `_atomic_write_at` overwrite over a regular file: `edit_note`, `set_frontmatter`, `complete_task` / `update_task` / `add_task`, `move_note` link rewrites, `write_file(overwrite=True)`, the `vault.write_file` / `write_bytes` conveniences | named staging + `renameat` (`vault.py:2470`) | **Edit**, group translation if needed. P1 vs P2 difference: conflict |
| `_atomic_write_at` overwrite of an absent name or a non-regular leaf | named staging + `renameat` | **Create**. A file appearing before P2: conflict |
| `_atomic_write_at` no-clobber, `O_TMPFILE` (`create_note`, `write_file` default) | `linkat` via `/proc/self/fd` (`:2492`) | **Create** (gid is always already right) |
| `_atomic_write_at` no-clobber, named-staging fallback | named staging + `link` (`:2490`) | **Create** |
| `stream_to_vault`: no-overwrite upload, `import_from_url`, overwrite token minted over an absent target | unnamed `linkat` or named `link` from `.transfer-tmp` | **Create**, ACL computed from the destination, translation for a setgid folder whose group the server is not in, gate checks directory identity |
| `stream_to_vault`: overwrite token with a fingerprint | transient or named name in `.transfer-tmp` + `renameat` | **Edit**, captured once from the fingerprinted leaf |
| `vault_fs.default_file_mode()` | after the change: only the create plan with no default ACL | n/a |
| `move_note`, the moved note | `renameat2(RENAME_NOREPLACE)` | **Unchanged**: rename keeps the inode (as `mv`) |
| soft delete to `.trash/` | `renameat2` | **Unchanged** |
| permanent delete | `unlinkat` | **Unchanged** |
| `open_dir_beneath(create=True)`: parents for writes, moves, both upload descents, the `.trash/` tree | `mkdirat` (`vault_fs.py:744`) | **0o777 under a default ACL**, else 0o755 as today |
| `open_staging_dir`: `.transfer-tmp` | `mkdirat` + `fchmod(0o700)` | **Unchanged**: mode argument 0o755 even under a default ACL (`follow_default_acl=False`) |
| `probe_publication`, `probe_trash`, `prune_stale_staging` | transient files / `unlinkat` | **Unchanged** |

No other writer creates vault files. The indexer, the panel and the change feed write only to the database.

## Accepted limitations (session, pending owner)

- **L1.** Files earlier releases narrowed or widened are not repaired. The upgrade note says how an operator can.
- **L2.** The owner uid is not kept. The new inode is the server's, and the previous owner becomes an ordinary principal: it can lose owner-class access, or gain where its owner class was lower than its group/other class. Reasoning in D4: bits do not bound an owner, the bytes are new, and refusing would break working edits.
- **L3.** Under group translation, members of the server's group who match no other entry lose access they had through "other". Where the rule already named the wanted group, its members lose what only the owning-group entry granted. Both are narrower (D3).
- **L4.** NFSv4 and richacl ACLs are not preserved. Only `system.posix_acl_access` is.
- **L5.** Other xattrs (`user.*`, `security.*`, `trusted.*`) are lost on edit, as before.
- **L6.** Hard links are broken by the rename, as before.
- **L7.** A permission change after the publish-point capture and before the rename is lost with the replaced inode.
- **L8.** `move_note` keeps a moved file's mode and ACL across folders with different default ACLs, as `mv` does.
- **L9.** In a user-namespaced container, ACL entries naming ids outside the map read back as the overflow id. They are written back as that id, or refused with `EINVAL` (`io_failure`). No documented deployment uses a user namespace.
- **L10.** A directory's default ACL changed during a call is not seen by that call's create plan.
- **L11.** A default ACL removed between the directory read and the `mkdirat` gives a folder of `0o777 & ~umask`.
- **L12.** The upload refusal for D6 is a generic HTTP 500 with the claim released.
- **L13.** On a filesystem without POSIX ACLs, an upload into a setgid folder whose group the server is not in is published with the server's gid and the mode-only translation, so the folder's group gets only other-class access. That is exactly what it gets today on that input, so nothing regresses and nothing is broadened. Notes and raw files are unaffected, because they stage in the destination directory and inherit its gid. The fix, staging uploads in the destination directory, is a redesign of transfer staging. Follow-up: #450.

- **L14.** From the staged-bytes verification to the rename (the publish-point capture and the bounded comparison read), a named staged copy can still be altered by a principal the published permissions let write the note; that principal can write the note itself at any time. Closing it fully means staging overwrites namelessly or in a private directory; follow-up: #451.
- **L15.** On a network filesystem whose server checks permissions on every read and gives the file's owner no override, the staged-bytes verification of an overwrite that keeps a mode without owner read (for example 0o200) can fail with `EACCES`. On such a filesystem the staged-bytes content comparison is skipped for that write only when the applied permissions give the owner no read (identity and size are still checked), so L14's interval begins at `apply_plan` there and nothing is refused; when they do give the owner read, a refused read-back refuses the write (`io_failure`, nothing published). It is reached by an unguarded `write_file(overwrite=True)` over such a file (the server cannot read it at all) and by a named no-clobber create under a default ACL that gives the owner write without read.

## Risks / Trade-offs

- **ACL calls failing for another reason now refuse writes** (D6). The refusal is loud and names the step.
- **Unguarded `write_file(overwrite=True)` can now answer `concurrent_write`** when the target's identity or permissions change during the call. Today it would silently overwrite with the wrong permissions.
- **About ten syscalls per write, with no extra flush.**
- **One test pins the old policy:** `tests/test_transfer_service.py::test_overwrite_resets_the_incumbent_mode_to_server_policy`. It is inverted. `tests/test_vault_mutation_safety.py::test_written_note_keeps_the_umask_default_mode` stays valid.

## Test plan

New module `tests/test_preserve_file_permissions.py`, on the real filesystem under `tmp_path`.

**Probe.** A fixture writes a default ACL with `os.setxattr` and a packed struct (no `setfacl`). On `EOPNOTSUPP`/`ENOTSUP` it skips, and with `OMCP_TEST_REQUIRE_POSIX_ACL=1` it fails instead. That variable is set in `.github/workflows/ci.yml`'s `tests` job env. It is not a `Settings` field: only the module reads it, through `os.environ`, so `SETTINGS_ENV_KEYS` and `CONTROLLED_TEST_ENV` are unchanged and `test_conftest_env_hermeticity` passes unchanged.

**Oracles:**

- Create: a control from `os.open(name, O_CREAT|O_WRONLY, 0o666, dir_fd=…)` in the same directory has the same `st_mode & 0o777` and access ACL value (bytes, or `ENODATA`), and, wherever the group is kept, the same `st_gid`. Translation cases instead assert the specified gid (the staged inode's) and effective access through the helper.
- Edit: `(st_mode & 0o777, st_gid, ACL value)` before equals after.
- Directory: a control from `os.mkdir(…, 0o777)` has the same mode, access ACL and default ACL. With no default ACL, 0o755 under umask 0.
- **Translation cases** use an effective-access helper. It computes read/write for a given `(uid, groups)` from owner, gid, mode and ACL by K7. Members of the old group, of the new owning group, of both and of neither are compared with before (edit) or with the control (create).

**Cases**, in both directions: group-`rwx` with a named group, and private / 0600.

- Helpers: codec round trip, malformed bytes refused, plans, translation on minimal / masked / pre-existing-named-entry ACLs.
- Tools: `create_note`, `edit_note`, `set_frontmatter`, `write_file` (create, overwrite, overwrite to an absent name).
- `stream_to_vault`: create, overwrite token, chmod after mint giving 409, setgid folder translation through a monkeypatched `fchown` returning `EPERM`, folder replaced before the gate giving 409.
- Directories, under a default ACL and without one (umask 0), and `.transfer-tmp`.
- Edit translation (monkeypatched `fchown` returning `EPERM`), including F1's private-file input. A supplementary-group keep where the process has one.
- Mode-only through monkeypatched `EOPNOTSUPP`, including the mode-only translation and L13's 0o604 upload.
- R2-1: a rule with a denying named entry whose translated ACL write fails with `EOPNOTSUPP` gives `io_failure`, nothing published.
- `EIO` on `fsetxattr` giving `io_failure` with nothing changed, and a released claim for an upload.
- A chmod between P1 and P2 giving the conflict, with nothing published. An ACL changed mid-capture (hook between `fstat` and the read) giving the conflict.
- Exactly one staged `fsync` per write.
- The issue's reproduction table.

## Review record

Codex spec review, round 1: FAIL, with 1 BLOCKER, 4 MAJOR and 3 MINOR findings. Dispositions:

- **F1 BLOCKER.** The edit's create-plan fallback can publish a private file with the folder's grants. **Accepted.** The fallback is removed and replaced by group translation (D3), with the proof. It does not refuse.
- **F2 MAJOR.** When the owner uid differs, the server gains owner-class access. **Declined as a refusal, documented** (D4, L2).
- **F3 MAJOR.** The P2 re-apply and second `fsync` lengthen the compare-to-rename window. **Accepted.** A P1/P2 difference is now the in-call conflict. Nothing is re-applied anywhere, uploads capture once, and the window is unchanged (D5).
- **F4 MAJOR.** Clearing the group on a setgid upload loses access. **Accepted** through group translation. The "cleared" clause and the old L13 are deleted.
- **F5 MAJOR.** Directories at 0o777 change umask-000 hosts. **Partly accepted.** 0o777 is used only under a default ACL; otherwise 0o755 as today (D8). The rest is follow-up #449.
- **F6 MINOR.** Mixed mode/ACL capture. **Accepted.** A capture is `fstat`, read, `fstat`, and a change is a conflict (D5).
- **F7 MINOR.** `getegid` is not the creating gid. **Accepted.** The creating gid is the setgid directory's, else the staged inode's (D1).
- **F8 MINOR.** The create oracle ignored the gid. **Accepted.** `st_gid` is compared, and translation is tested with an effective-access helper.
- **D6, the refusal policy, kept** as ruled.

Codex spec review, round 2: FAIL, with 1 BLOCKER, 1 MAJOR and 2 MINOR findings. Codex confirmed that the ACL translation itself is sound. Dispositions:

- **R2-1 BLOCKER.** The mode-only fallback after an unsupported translated-ACL write can drop a denying named entry. **Accepted.** Mode-only is allowed only for a rule without an ACL; otherwise the D6 refusal applies (D3, D6).
- **R2-2 MAJOR.** On a filesystem without ACLs, an upload into a foreign setgid folder lacks the folder's group access. **Declined as a fix.** Recorded as L13, the create requirement states the narrower result, and follow-up #450 is filed.
- **R2-3 MINOR.** The P2 metadata read sat between the comparison and the rename. **Accepted.** It is taken before the bytes are compared, and the window is exactly today's (D5).
- **R2-4 MINOR.** The create oracle's gid equality was unqualified. **Accepted.** It is qualified to the group-kept case, and translation cases assert the staged gid and effective access.

Implementation review round 1 (Codex): FAIL, 1 MAJOR (ACL capture required /proc). Accepted, fixed.

Implementation review round 2 (Codex): FAIL — 1 BLOCKER (named staged copy writable under its final permissions before publication): accepted as staged-bytes verification, residual recorded as L14; 1 MAJOR: symlink mapping accepted, FIFO open declined as pre-existing (follow-up #452); 1 MINOR (README) accepted.

Implementation review round 3 (Codex, verification of the round-2 fixes only): FAIL. The fixes were verified: the staged-bytes check runs on both named publication paths, before P2 and the comparison; a mismatch publishes nothing; `ELOOP` is mapped only at the comparison open; the added tests fail without the fixes. One finding, MAJOR by the reviewer's rubric: on NFS the verification read can be refused for a kept mode without owner read. **Accepted**: a refused read now skips the comparison instead of refusing the write (L15 reworded). The review budget ends here. Round 4 (Codex): the skip verified; 1 MAJOR — on a filesystem where a non-owner may change a file's ACL the skip could be induced: accepted, the skip is limited to a copy whose applied permissions give the owner no read; 1 MINOR (L15 wording) accepted. Round 5 (Codex, verification of that one change, at ff5fb3c): PASS, no findings.
