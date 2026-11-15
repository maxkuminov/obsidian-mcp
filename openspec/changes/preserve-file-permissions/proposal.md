Refs #447 (regression of #95's fix). Ships as a hotfix release (1.5.1).

## Why

Every file the server writes is published with mode `0o666 & ~umask`, whatever the folder says and whatever the file had before. `vault._atomic_write_at` (notes and raw files) and `transfer.stream_to_vault` (uploads and imports) stage into a fresh inode created at 0600 and call `os.fchmod(fd, vault_fs.default_file_mode())` before publishing it by `linkat` or `rename`. Each write publishes a new inode, so an edit also throws away the old inode's mode and ACL.

Two consequences, both reproduced on a real filesystem:

1. **Group access is cut to read-only.** `chmod` on an inode that carries a POSIX ACL rewrites the ACL mask from the group bits. In a folder whose default ACL grants a group `rwx`, a plain `open(O_CREAT, 0o666)` file gets `mask::rw-`. A file the server creates or edits gets `mask::r--`, so the named group's effective access is `r--`. On a live vault this made an agent that works through a group ACL unable to write its result to a ticket filed through the MCP, and finished work was recorded as a failure.
2. **A private file becomes world-readable.** A 0600 note is 0644 after any edit. In a folder whose default ACL is `u::rwx,g::---,o::---`, an ordinary new file is 0600 and a server-created one is 0644.

`default_file_mode()` (added for #95, files landing 0600) assumed the umask alone decides a new file's mode. Under a default ACL the kernel ignores the umask.

The owner's decision for this change, in his words: "keep a note's existing permissions on edit". Everything else here is this change's design, pending the owner.

## What Changes

- **Edit rule.** A publication that replaces an existing regular file leaves the published file with the replaced file's permission bits (`0o777`) and POSIX access ACL, read from that file through a descriptor. setuid, setgid and sticky are never carried. The server tries to keep the replaced file's gid on the staged inode. When it cannot, the publication gets the create rule instead, so group-class permissions are never handed to another group. The owner uid is never changed: the new inode belongs to the server.
- **Create rule.** A publication at a name that did not exist gets exactly what `open(O_CREAT, 0o666)` gets in that directory: the directory's default ACL where it has one (umask not applied), otherwise `0o666 & ~umask`. This holds for uploads too, which stage in `.transfer-tmp` rather than in the destination directory.
- **Directories** the server creates in the vault are made with mode argument 0o777, so the kernel applies the parent's default ACL or the umask exactly as `mkdir` does. Today they are made 0o755, which under a group-`rwx` default ACL gives `mask::r-x`: the group cannot create files in a folder the server made. `.transfer-tmp` stays 0o700.
- **Never broader, never silent.** `ENODATA`, `EOPNOTSUPP` and `ENOTSUP` from the ACL calls mean "no POSIX ACL here": such filesystems keep today's mode-only behaviour. Any other failure to apply the rule refuses the write and publishes nothing: `io_failure` from the tools, a released claim for an upload.
- **Staging stays private.** Staged inodes are still created 0600. The rule is applied before the existing payload flush, and captured again at the publish point from the descriptor the `expected=` comparison reads. It is re-applied and flushed a second time only when the destination changed in between.
- **No new setting, no migration, no new refusal code, no tool schema or description change.**

## Non-goals

- Repairing files that earlier releases already narrowed or widened.
- NFSv4 and richacl ACLs. Other extended attributes (`user.*`, security labels), which are lost on edit as before. Hard links, which a rename breaks as before.
- Changing the owner uid of a replaced file (works only for a root deployment, see design D4).
- Changing what `move_note` does to the moved file: a rename keeps its inode, mode and ACL, as `mv` does.

## Capabilities

### New Capabilities

- `file-permissions`: the edit rule, the group fallback, the create rule, directory creation, mode-only filesystems, the failure policy, and private staging with the publish-point binding.

### Modified Capabilities

- `file-transfer`: the upload requirement's step (5) stops relaxing the staged file to the umask default and applies the `file-permissions` rule instead. Step (6) re-captures inside the publish gate and refuses an overwrite whose target is not the file the fingerprint names.

## Impact

- **Code:**
  - `src/services/vault_fs.py`: ACL parse/pack, capture, plan, apply; directory creation mode.
  - `src/services/vault.py`: `_atomic_write_at` and the `expected=` read.
  - `src/services/transfer.py`: `_stream_locked`, `_publish_into_current_parent`.
- **Tests:** a real-filesystem module with control-file oracles. A CI env var turns the no-ACL skip into a failure. One existing test that pins the old policy is inverted.
- **Behaviour:**
  - Edited files keep their mode and ACL.
  - New files and folders match what any other program would create there.
  - Under umask 022 with no ACLs (the reference container), nothing visible changes for new files and folders.
  - A host where an ACL call fails for a reason other than "unsupported" now refuses writes instead of publishing with the wrong permissions.
- **Docs:** `docs/architecture/vault-tools.md`, `docs/architecture/file-transfer.md`, `CLAUDE.md`, `README.md` (1.5.1 upgrade note), release notes.
