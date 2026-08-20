## 1. Anchored validation

- [x] 1.1 Add `MutableTarget` (path, vault-relative path, final component, resolved root, parent fd, root fd) to `src/services/vault.py`, with `lstat`/`exists`/`is_file` through the parent fd, `parent_fd` (never creates) and `dir_fd` (creates on demand), and `close()` / context-manager semantics
- [x] 1.2 Add `open_mutable(rel, user_id)`: the existing lexical guard, parent resolution and containment check, then `vault_fs.open_root` + `vault_fs.open_dir_beneath` over the *resolved* parent path, then the leaf `lstat` through that descriptor
- [x] 1.3 Reduce `validate_mutable_path` to the single-shot form over `open_mutable`, keeping its signature, return type and every error message

## 2. Anchored write core

- [x] 2.1 `_temp_candidate` returns a bare name; `_create_temp_exclusively(dir_fd, name)` creates it with `O_CREAT|O_EXCL|O_NOFOLLOW` relative to the descriptor
- [x] 2.2 `_atomic_write_at(target, …)` replaces `_atomic_write`: stage → `fchmod` → `fsync` → `expected=` compare → `renameat` (overwrite) or `linkat` (no-clobber), all relative to `dir_fd`; `UnsupportedFilesystem` on `EPERM`/`EOPNOTSUPP`/`EXDEV` for the no-clobber publish
- [x] 2.3 Split the read primitive: `_read_fd_bytes(dir_fd, name, …)` for mutations, `_read_path_bytes(path, …)` for the symlink-following read path
- [x] 2.4 Re-point `write_file_at` / `write_bytes_at` / `read_bytes_at` at `MutableTarget`; `write_file` / `write_bytes` open and close their own target
- [x] 2.5 Replace `move_no_clobber` with `move_file_no_clobber(src_target, dst_target)` over `vault_fs.rename_noreplace`

## 3. Anchored soft delete

- [x] 3.1 Extract `vault_fs.soft_delete_at(src_dir_fd, name, root_fd, …)` from `soft_delete`, with `stamp=` and `label=`; `soft_delete` keeps its exact behaviour for `delete_file`
- [x] 3.2 Add `root_fd=` to `check_trash_support` / `_cached_probe` so the probe runs against the caller's anchored root
- [x] 3.3 `delete_note` probes trash support *after* the not-found check, unlinks through `dir_fd` for `permanent=True`, and soft-deletes through `soft_delete_at` with a UTC stamp

## 4. Tool call sites

- [x] 4.1 `create_note`, `edit_note`, `set_frontmatter`, `delete_note`, `write_file`: open one target, act through it, close it
- [x] 4.2 `move_note`: open the source, the destination and each rewrite source once; take `from_rel` / `to_rel` from the targets; publish with `move_file_no_clobber`; close everything in `finally`
- [x] 4.3 `_leaf_state_error` — absent / symlink / non-regular, re-checked through the parent descriptor before each tool acts

## 5. Tests and documentation

- [x] 5.1 `tests/test_anchored_note_writes.py`: the parent-rename race against every mutating tool, the vault-root variant, the trash anchoring, the `fsync`-before-publish ordering, staging location, crash-mid-write, and the two `UnsupportedFilesystem` refusals
- [x] 5.2 Update `tests/test_vault_mutation_safety.py` and `tests/test_symlink_mutation_guard.py` where they hooked the old path-based internals, keeping the property each pinned
- [x] 5.3 Confirm the new tests fail against the pre-change tree. Measured against `6252109` with the final test files: **19 of the 33** cases in `test_anchored_note_writes.py` fail, and **33 of 106** across that file plus `test_symlink_mutation_guard.py` (which includes all 11 swapped-leaf cases). The rest pin guarantees this change preserves rather than introduces, which is why they pass either way. Four of the swapped-leaf cases — `create_note`, `write_file` in both modes, and `move_note`'s destination — also fail against the *first* implementation of this change, which is the gap the review found.
- [x] 5.4 Module docstring in `src/services/vault.py` stating the remaining residual; `vault_fs` docstring updated to record the adoption
- [x] 5.5 CLAUDE.md: rewrite "The accepted residual, precisely", the `*_at` paragraph, the `delete_note` trash format and the "Follow-ups" note
- [x] 5.6 `openspec validate anchored-note-writes --strict` passes; full suite green

## 6. Review follow-ups

- [x] 6.1 `_leaf_state_error` grows an optional `missing`, so the *creating* tools can use it: `write_file` checks before writing (an `overwrite=True` publish replaces the link and would have reported "Wrote N bytes"), and `create_note` / `write_file` / `move_note`'s destination decode the no-clobber `EEXIST` through it — `link`/`renameat2` refuse a file, a directory and a symlink identically
- [x] 6.2 `move_note` releases a backlink source's descriptor on every path that plans no rewrite, and releases each planned one as soon as its write is published
- [x] 6.3 `config.max_move_rewrite_sources()` derives a descriptor budget from `RLIMIT_NOFILE`; the preflight aborts before any mutation rather than letting one move exhaust the process table mid-loop
- [x] 6.4 `tests/test_symlink_mutation_guard.py` parametrises the swapped-leaf case over all eleven applicable tool/mode combinations; the fd tests assert *peak*, not just post-call
- [x] 6.5 `src/mcp_server/server.py` and `README.md` state the new `.trash` name form and the `UnsupportedFilesystem` refusal
- [x] 6.6 Correct the claim that `write_file(overwrite=True)` is guarded by `expected=` (CLAUDE.md and the `vault` module docstring) — it is an unconditional replace
- [x] 6.7 Note on `validate_mutable_path` that it has no production caller and why it is kept
