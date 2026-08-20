## Why

Issue #54 established that a mutating note tool must act on the path *as
named*: an in-vault alias `alias.md -> important.md` had made `edit_note`
rewrite `important.md` and report success for `alias.md`. `validate_mutable_path`
fixed that by resolving the **parent** once and taking the final component as
named.

Issue #59 is the half that fix did not reach. `validate_mutable_path` returns a
`Path`, and a `Path` is a *pathname*: every syscall the write then makes — the
`mkdir`, the temp create, the `expected=` read, the `os.replace`, the `.trash`
link — hands that pathname back to the kernel, which walks it again from the
root. A concurrent process that renames the resolved parent directory and drops
a symlink at its name, or repoints the directory behind a symlinked vault root,
**between two of those syscalls** sends the write, the soft delete, or the move
to a directory nobody validated. The `expected=` compare-and-swap does not
catch it, because the decoy directory may hold a byte-identical copy of the
note. The vault is the owner's single source of truth and the caller is an
autonomous agent, so a destructive write reported as a success is the most
expensive failure this server has.

`src/services/vault_fs.py` already solved this shape for `/transfer/*` and
`delete_file` — walk from an open root descriptor, one `O_NOFOLLOW` component
at a time, and operate relative to the descriptor. This change brings the note
write path onto the same footing. It was recorded in CLAUDE.md as an accepted
limitation pending exactly this work.

## What Changes

- `open_mutable(rel, user_id)` replaces `validate_mutable_path` as the entry
  point for every mutating tool, returning a `MutableTarget` that carries an
  **open parent directory descriptor** alongside the resolved path, the
  vault-relative path and the final component. `validate_mutable_path` remains
  as the single-shot form for callers that only need the verdict.
- The parent is opened by walking the *resolved* parent path from an open root
  descriptor with `O_NOFOLLOW` per component. Resolution happens first, so
  in-vault symlinked directories keep working; the walk stays strict, so a
  component that became a link in the interval is refused rather than followed.
- Staging (`O_CREAT|O_EXCL|O_NOFOLLOW`), the `expected=` read, publication
  (`renameat` / `linkat`), the permanent unlink and the `.trash` rename all run
  **relative to that descriptor**. No pathname is resolved after validation.
- The payload is `fsync`ed before publication.
- `move_note` publishes with one `renameat2(RENAME_NOREPLACE)` instead of
  `link` + `unlink`, closing the case where the `unlink` removes a *different*
  inode than the one that was linked.
- `delete_note` soft-deletes through `vault_fs.soft_delete_at`, sharing the
  non-replacing trash rename with `delete_file`. Trash entries consequently
  gain the same `-<8 hex>` suffix, and a filesystem that cannot do a
  non-replacing rename is refused rather than silently degraded.
- A leaf that becomes a symlink between validation and the act is now named as
  such rather than reported as a missing note.
- `check_trash_support` accepts the caller's already-anchored root descriptor,
  so the probe cannot create `.trash` in a directory the root's pathname has
  been repointed at.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `vault-write`: mutation anchoring, `delete_note` trash naming and
  non-replacing rename, `move_note` publication primitive, durability of the
  atomic write.

## Impact

- `src/services/vault.py` — `MutableTarget`, `open_mutable`, `_atomic_write_at`,
  `_read_fd_bytes`, `move_file_no_clobber`; `move_no_clobber` removed.
- `src/services/vault_fs.py` — `soft_delete_at`, `check_trash_support(root_fd=)`.
- `src/mcp_server/tools.py` — `create_note`, `edit_note`, `move_note`,
  `delete_note`, `set_frontmatter`, `write_file` call sites; `_leaf_state_error`.
- `tests/test_anchored_note_writes.py` (new), plus updates to
  `tests/test_vault_mutation_safety.py` and
  `tests/test_symlink_mutation_guard.py` where they hooked the old internals.
- No database schema changes, no new dependencies, no configuration changes.
- Behaviour change visible to callers: `.trash` entry names for `delete_note`
  now carry a random suffix.
