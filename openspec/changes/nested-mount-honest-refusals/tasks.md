# Tasks: nested-mount-honest-refusals

## 1. Primitive: EXDEV names the mount boundary (D1)

- [ ] 1.1 In `vault_fs.rename_noreplace`, split `EXDEV` out of the `EINVAL/ENOSYS/EOPNOTSUPP/EXDEV` fold and raise `MountBoundary` with text naming the mount layout; keep the other three on the existing message.
- [ ] 1.2 In `vault_fs.soft_delete_at`, add a `MountBoundary` catch **before** the existing `UnsupportedFilesystem` re-wrap around the trash rename, re-raising as `MountBoundary` with text naming the layout ("the file's directory and the vault root's `.trash/` are on different mounts") and `permanent=True` as the workaround.
- [ ] 1.3 In `vault_fs.probe_trash`, catch `MountBoundary` **before** `UnsupportedFilesystem` and re-raise as `MountBoundary` with accurate root/`.trash` mount-layout prose — the probe must not erase the subtype into "the vault filesystem cannot move files with a non-replacing rename" (Codex finding 3).
- [ ] 1.4 Audit every other re-wrap of `UnsupportedFilesystem` fed by `rename_noreplace` (`move_note` tool body, `delete_note`/`delete_file` surfaces, `_refuse_a_moved_directory`, `_verify_the_moved_inode` reporting) for subclass-before-class ordering; confirm rollback reporting treats `MountBoundary` as it treated `UnsupportedFilesystem`.

## 2. Best-effort preflights (D2)

- [ ] 2.1 Add the fail-open comparison helper to `vault_fs` (wraps `same_mount`, treats "cannot establish" — `UnsupportedFilesystem` from `mount_id_of` — as "proceed"); docstring states it must NOT be used by the transfer path, whose fail-closed `require_same_mount` is deliberate.
- [ ] 2.2 Soft delete: in `soft_delete_at`, after `trash_fd` opens and before the rename, preflight `src_dir_fd` vs `trash_fd`; definite mismatch → the task-1.2 `MountBoundary` refusal, before anything is renamed.
- [ ] 2.3 Move: in `vault.move_file_no_clobber`, forward path only (`confirmation is not None`), preflight before the rename **without creating anything** (Codex finding 4): compare `source.dir_fd` against the destination's never-creating parent descriptor when the parent exists, else against the destination's deepest existing ancestor (`vault_fs.deepest_existing_dir`) — never through `destination.dir_fd`'s creating path. Definite mismatch → `MountBoundary`. The rollback path (`permit`) never preflights.
- [ ] 2.4 Confirm `move_note` surfaces the refusal with no DB update and no mutation — verify against the session (task 5.3), don't infer it from the refusal text.

## 3. Vault-side named-fallback mappings (D3, #110)

- [ ] 3.1 `vault._link_staged_name`: `EXDEV` → `MountBoundary` naming the layout; `EOPNOTSUPP` keeps the filesystem-lacks-hard-links message; `EPERM` gets "hard-link publication denied" prose naming security policy (seccomp/LSM) alongside filesystem support (Codex finding 7).
- [ ] 3.2 `vault._atomic_write_at` fallback overwrite `os.replace`: catch `OSError` with `errno == EXDEV` → `MountBoundary`; all other errnos propagate unchanged.

## 4. Transfer discard published-state (D4, #115)

- [ ] 4.1 Hoist `state = {"published": False}` in `stream_to_vault` to **before** the staging block, so it exists from before `tmp_name` can (Codex finding 1 — otherwise any pre-drain failure hits `UnboundLocalError` in the modified cleanup, masking the real error and skipping the guarded discard).
- [ ] 4.2 In the outer `except BaseException` cleanup, replace `vault_fs.discard_temp(staging_fd, tmp_name, staged_st)` with `vault_fs.discard_staged_name(staging_fd, tmp_name, staged_st, published=state["published"])`. `discard_temp` itself is unchanged.

## 5. Tests (D5)

- [ ] 5.1 Real nested mount (`tests/_nested_mount_cases.py`): soft delete of a file on `M/` via `delete_note` and `delete_file` → `MountBoundary` text naming the layout and the workaround, file untouched, nothing in `.trash/`; `permanent=True` still works.
- [ ] 5.2 Real nested mount: `move_note("M/a.md", "a.md")` → mount-boundary refusal, source untouched, no destination entry; `move_note("M/a.md", "New/Sub/a.md")` with `New/Sub` absent → refusal **and neither `New/` nor `New/Sub/` exists afterwards** (finding 4); a same-side move still succeeds; `.trash` bind-mounted as its own mount → `probe_trash` fails `MountBoundary` with accurate prose (finding 3).
- [ ] 5.3 No-DB-mutation proof (finding 5): session/statement spy (or DB-backed case) asserting a refused cross-mount move executes and commits nothing against `notes_metadata`/`note_links`, including `rewrite_links=True` with ≥1 planned backlink whose source note is byte-identical afterwards.
- [ ] 5.4 Degraded-kernel policy (stubbed sibling module): `mount_id_of` raising ⇒ soft-delete and move preflights skip and same-mount operations succeed; transfer's `require_same_mount` still refuses — the two directions pinned apart.
- [ ] 5.5 Fault injection (#110): named-fallback note write with the publishing link / overwrite replace monkeypatched to raise `EXDEV` → `MountBoundary`; `EOPNOTSUPP` → filesystem hard-link message; `EPERM` → publication-denied message naming security policy.
- [ ] 5.6 #115 post-publish: fallback mode, successful overwrite publish, post-publication flush raising → `PostPublishFailure`, claim stranded, `caplog` holds **no** disappearance warning, and a spy asserts the outer discard ran with `published=True` (finding 6); cover the residual-matching-name shape (link publish) too.
- [ ] 5.7 #115 pre-publish: early fallback failures (over-cap body; failing `fstat`/`fchmod`/payload flush) → original exception propagates unmasked, guarded discard runs with `published=False`, staged name removed (finding 1); a pre-publication disappearance still warns.
- [ ] 5.8 Grep the test suite for assertions pinning the old lying strings ("cannot receive a non-replacing rename", "does not support hard links") on cross-mount cases and update them.

## 6. Verification and shipping

- [ ] 6.1 Full suite: `pytest tests/` plus the nested-mount runner (`pytest tests/test_nested_mount_publication.py`).
- [ ] 6.2 `openspec validate --strict` clean for this change.
- [ ] 6.3 PR (main is protected), verifier + adversarial Codex gates, deploy, exercise the affected tools live (delete/move on the production vault are same-mount — exercise success paths; the refusal paths are covered by the unshare harness), archive with `Closes #108 #109 #110 #115`.
