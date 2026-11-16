Refs #447 (regression of #95's fix). Ships as a hotfix release (1.5.1).

## Why

Every file the server writes is published with mode `0o666 & ~umask`, whatever the folder says and whatever the file had before. `vault._atomic_write_at` (notes and raw files) and `transfer.stream_to_vault` (uploads and imports) stage into a fresh inode created at 0600 and call `os.fchmod(fd, vault_fs.default_file_mode())` before publishing it by `linkat` or `rename`. Each write publishes a new inode, so an edit also throws away the old inode's mode and ACL.

Two consequences, both reproduced on a real filesystem:

1. **Group access is cut to read-only.** `chmod` on an inode that carries a POSIX ACL rewrites the ACL mask from the group bits. In a folder whose default ACL grants a group `rwx`, a plain `open(O_CREAT, 0o666)` file gets `mask::rw-`. A file the server creates or edits gets `mask::r--`, so the named group's effective access is `r--`. On a live vault this made an agent that works through a group ACL unable to write its result to a ticket filed through the MCP, and finished work was recorded as a failure.
2. **A private file becomes world-readable.** A 0600 note is 0644 after any edit. In a folder whose default ACL is `u::rwx,g::---,o::---`, an ordinary new file is 0600 and a server-created one is 0644.

`default_file_mode()` (added for #95, files landing 0600) assumed the umask alone decides a new file's mode. Under a default ACL the kernel ignores the umask.

The owner's decision for this change, in his words: "keep a note's existing permissions on edit". Everything else here is this change's design, pending the owner.

## What Changes

- **Edit rule.** A publication that replaces an existing regular file keeps the replaced file's permission bits (`0o777`), POSIX access ACL and gid. The bits and ACL are read from that file through a descriptor. setuid, setgid and sticky are never carried. The owner uid is never changed: the new inode belongs to the server.
- **Create rule.** A publication at a name that did not exist gets exactly what `open(O_CREAT, 0o666)` gets in that directory: the directory's default ACL where it has one (umask not applied), otherwise `0o666 & ~umask`, and the gid an ordinary create gets. This holds for uploads too, which stage in `.transfer-tmp` rather than in the destination directory.
- **Group translation.** Both rules use it when the server cannot give the new inode the wanted gid (a group it is not in). The owning-group entry becomes `---`, and a named entry for the wanted group carries its permissions. On a filesystem without ACLs, the group bits are cleared and the other bits are capped by them. For every principal other than the server and the file's previous owner, the result is never broader than the rule (proved in the design).
- **Directories under a default ACL.** These are made with mode argument 0o777, so the kernel applies the default ACL exactly as `mkdir` does. Today 0o755 gives `mask::r-x`, so the group cannot create files in a folder the server made. Without a default ACL, folders stay 0o755, byte-identical to today. `.transfer-tmp` stays 0o700.
- **Never broader, never silent.**
  - `ENODATA`, `EOPNOTSUPP` and `ENOTSUP` from the ACL calls mean "no POSIX ACL here": such filesystems keep the mode-only behaviour.
  - Any other failure to apply the rule refuses the write and publishes nothing: `io_failure` from the tools, a released claim for an upload.
- **Staging stays private; one flush.**
  - Staged inodes are still created 0600. The rule is captured once and applied before the existing payload flush.
  - A note overwrite captures the leaf again at the publish point, from the descriptor the `expected=` comparison reads. A difference there is the existing in-call conflict (`concurrent_write`), with nothing published.
  - An upload's capture is bound to the token's fingerprint.
- **No new setting, no migration, no new refusal code, no tool schema or description change.**

## Non-goals

- Repairing files that earlier releases already narrowed or widened.
- NFSv4 and richacl ACLs. Other extended attributes (`user.*`, security labels), which are lost on edit as before. Hard links, which a rename breaks as before.
- Changing the owner uid of a replaced file, or refusing when it differs (design D4).
- The directory mode outside a default ACL (left at 0o755; follow-up #449).
- Changing what `move_note` does to the moved file: a rename keeps its inode, mode and ACL, as `mv` does.

## Capabilities

### New Capabilities

- `file-permissions`: the edit rule, group translation, the create rule, directory creation under a default ACL, mode-only filesystems, the failure policy, and coherent capture with the publish-point conflict.

### Modified Capabilities

- `file-transfer`: the upload requirement's step (5) stops relaxing the staged file to the umask default and applies the `file-permissions` rule instead. The capture is bound to the fingerprint. Step (6) refuses when the gate's destination folder is not the one captured, and never captures or flushes a second time.

## Impact

- **Code:**
  - `src/services/vault_fs.py`: ACL parse/pack, capture, plan, apply; directory creation mode.
  - `src/services/vault.py`: `_atomic_write_at` and the `expected=` read.
  - `src/services/transfer.py`: `_stream_locked`, `_publish_into_current_parent`.
- **Tests:** a real-filesystem module with control-file oracles. A CI env var turns the no-ACL skip into a failure. One existing test that pins the old policy is inverted.
- **Behaviour:**
  - Edited files keep their mode and ACL.
  - New files match what any other program would create there.
  - Folders under a default ACL do too; other folders are unchanged.
  - Under umask 022 with no ACLs (the reference container), nothing visible changes for new files and folders.
  - An unguarded `write_file(overwrite=True)` whose target changes identity or permissions mid-call now answers `concurrent_write`.
  - A host where an ACL call fails for a reason other than "unsupported" now refuses writes instead of publishing with the wrong permissions.
- **Docs:** `docs/architecture/vault-tools.md`, `docs/architecture/file-transfer.md`, `CLAUDE.md`, `README.md` (1.5.1 upgrade note), release notes.
