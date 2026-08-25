# Tasks: nested-mount-honest-refusals

## 1. Primitive: EXDEV names the mount boundary (D1)

- [ ] 1.1 In `vault_fs.rename_noreplace`, split `EXDEV` out of the `EINVAL/ENOSYS/EOPNOTSUPP/EXDEV` fold and raise `MountBoundary` with text naming the mount layout; keep the other three on the existing message.
- [ ] 1.2 In `vault_fs.soft_delete_at`, add a `MountBoundary` catch **before** the existing `UnsupportedFilesystem` re-wrap around the trash rename, re-raising as `MountBoundary` with text naming the layout ("the file's directory and the vault root's `.trash/` are on different mounts") and `permanent=True` as the workaround.
- [ ] 1.3 Audit every other re-wrap of `UnsupportedFilesystem` fed by `rename_noreplace` (`move_note` tool body, `delete_note`/`delete_file` surfaces, `_refuse_a_moved_directory`, `_verify_the_moved_inode` reporting) for subclass-before-class ordering; confirm rollback reporting treats `MountBoundary` as it treated `UnsupportedFilesystem`.

## 2. Best-effort preflights (D2)

- [ ] 2.1 Add the fail-open comparison helper to `vault_fs` (wraps `same_mount`, treats "cannot establish" — `UnsupportedFilesystem` from `mount_id_of` — as "proceed"); docstring states it must NOT be used by the transfer path, whose fail-closed `require_same_mount` is deliberate.
- [ ] 2.2 Soft delete: in `soft_delete_at`, after `trash_fd` opens and before the rename, preflight `src_dir_fd` vs `trash_fd`; definite mismatch → the task-1.2 `MountBoundary` refusal, before anything is renamed.
- [ ] 2.3 Move: in `vault.move_file_no_clobber`, forward path only (`confirmation is not None`), preflight `source.dir_fd` vs `destination.dir_fd` before the rename; definite mismatch → `MountBoundary`. The rollback path (`permit`) never preflights.
- [ ] 2.4 Confirm `move_note` surfaces the refusal with no DB update and no mutation (the preflight fires before the rename, so the existing refusal plumbing should already do this — verify, don't assume).

## 3. Vault-side named-fallback mappings (D3, #110)

- [ ] 3.1 `vault._link_staged_name`: `EXDEV` → `MountBoundary` naming the layout; `EPERM`/`EOPNOTSUPP` keep the hard-link-support message.
- [ ] 3.2 `vault._atomic_write_at` fallback overwrite `os.replace`: catch `OSError` with `errno == EXDEV` → `MountBoundary`; all other errnos propagate unchanged.

## 4. Transfer discard published-state (D4, #115)

- [ ] 4.1 In `stream_to_vault`'s outer `except BaseException` cleanup (`src/services/transfer.py` ~1609), replace `vault_fs.discard_temp(staging_fd, tmp_name, staged_st)` with `vault_fs.discard_staged_name(staging_fd, tmp_name, staged_st, published=state["published"])`. `discard_temp` itself is unchanged.

## 5. Tests (D5)

- [ ] 5.1 Real nested mount (`tests/_nested_mount_cases.py`): soft delete of a file on `M/` via `delete_note` and `delete_file` → `MountBoundary` text naming the layout and the workaround, file untouched, nothing in `.trash/`; `permanent=True` still works.
- [ ] 5.2 Real nested mount: `move_note("M/a.md", "a.md")` → mount-boundary refusal, source untouched, no destination entry; a same-side move still succeeds.
- [ ] 5.3 Degraded-kernel policy (stubbed sibling module): `mount_id_of` raising ⇒ soft-delete and move preflights skip and same-mount operations succeed; transfer's `require_same_mount` still refuses — the two directions pinned apart.
- [ ] 5.4 Fault injection (#110): named-fallback note write with the publishing link / overwrite replace monkeypatched to raise `EXDEV` → `MountBoundary`; `EPERM`/`EOPNOTSUPP` → hard-link message.
- [ ] 5.5 #115: fallback mode, successful overwrite publish, post-publication flush raising → `PostPublishFailure`, claim stranded, and `caplog` holds **no** disappearance warning; pre-publication failure with the name absent still warns; no-clobber leftover source still discarded quietly.
- [ ] 5.6 Grep the test suite for assertions pinning the old lying strings ("cannot receive a non-replacing rename", "does not support hard links") on cross-mount cases and update them.

## 6. Verification and shipping

- [ ] 6.1 Full suite: `pytest tests/` plus the nested-mount runner (`pytest tests/test_nested_mount_publication.py`).
- [ ] 6.2 `openspec validate --strict` clean for this change.
- [ ] 6.3 PR (main is protected), verifier + adversarial Codex gates, deploy, exercise the affected tools live (delete/move on the production vault are same-mount — exercise success paths; the refusal paths are covered by the unshare harness), archive with `Closes #108 #109 #110 #115`.
