# Design: nested-mount-honest-refusals

## Context

Group 4 of `atomic-beneath-root-writes` (D23) gave the **transfer** publication path an accurate mount-boundary vocabulary: `vault_fs.MountBoundary` (a subclass of `UnsupportedFilesystem`), the `same_mount`/`mount_id_of` primitives, a mint-time and in-gate preflight, and residual `EXDEV` mappings in `vault_fs._link_no_clobber` and `vault_fs.publish`'s overwrite branch. It deliberately left four adjacent spots behind, now issues:

- `vault_fs.rename_noreplace` still folds `EXDEV` into the generic "renameat2(RENAME_NOREPLACE) is not available" `UnsupportedFilesystem`, so the soft delete (#108) and `move_note` (#109) blame the filesystem for what the mount layout refuses, and `soft_delete_at` re-wraps that into "`.trash/` cannot receive a non-replacing rename" — a message that sends an operator chasing filesystem support that is present.
- The vault-side named-fallback publish branches (#110): `vault._link_staged_name` maps `EXDEV` into "the vault filesystem does not support hard links", and the fallback overwrite `os.replace` in `_atomic_write_at` has no `EXDEV` handling at all.
- `stream_to_vault`'s outer cleanup discards a named-fallback staging name via `vault_fs.discard_temp`, which hardcodes `published=False` into `discard_staged_name` — so a publish that succeeded and then failed a post-publication flush (`PostPublishFailure`, claim stranded, all correct) logs a false "staging name disappeared before its write was published" warning (#115). The `vault.py` twin (`_discard_temp`) already threads the real flag.

Constraint inherited from D23: `mount_id_of` **raises** `UnsupportedFilesystem` where `STATX_MNT_ID` is unavailable (Linux < 5.8, or a `statx`-less container); it never degrades to `st_dev`. The server floor is `openat2`'s 5.6, so every new preflight must survive a kernel that cannot answer the mount question.

## Goals / Non-Goals

**Goals:**

- Every `EXDEV` a vault-side publish, soft delete, or move surfaces names the mount boundary as the cause, in `MountBoundary` vocabulary, on every kernel (this is the backstop and it needs no `statx`).
- Soft delete and `move_note` refuse a *pre-existing* cross-mount layout early with the same accurate message, where the kernel can answer the mount question (best-effort preflight).
- A named-fallback upload that published successfully never logs a staging-name "disappeared" warning about the name its own publish consumed.

**Non-Goals:**

- Making cross-mount soft delete or move *work* (per-mount `.trash`, copy-based moves). Copy-and-unlink is the `link`+`unlink` shape `soft_delete`'s docstring exists to refuse, and a copying move breaks "whichever inode is at the source when the call runs is what moves". #108's per-mount-trash option stays open as a possible future change; this one only stops the error from lying.
- Any change to the transfer path's mount checks — group 4's mint-time/in-gate preflights and mappings are untouched (only the #115 discard flag beside them changes).
- Kernel-floor changes. Nothing new is required; degraded kernels keep exactly the operations they have today, with better residual messages.

## Decisions

### D1 — Split `EXDEV` out of `rename_noreplace`'s errno fold (one site fixes #108 and #109's backstop)

`rename_noreplace` maps `EINVAL/ENOSYS/EOPNOTSUPP/EXDEV` to one `UnsupportedFilesystem`. `EXDEV` is the odd one out: the other three mean "this kernel or filesystem cannot do a non-replacing rename", while `EXDEV` from `renameat2` means, definitionally, "the two names are on different mounts" — the filesystem is fine. Raise `MountBoundary` for `EXDEV` with text naming the mount boundary, keep the other three as they are.

This is the *primitive*, so every caller inherits the accurate cause at once: `soft_delete_at`/`_rename_into_trash`, `move_file_no_clobber` (forward and rollback), `_refuse_a_moved_directory`'s rollback, and `vault_fs.publish` is unaffected (its overwrite branch already maps its own `EXDEV`). `MountBoundary` subclasses `UnsupportedFilesystem`, so no existing handler misses it — but two callers *re-wrap* `UnsupportedFilesystem` with their own (now-lying) prose and must let `MountBoundary` pass through or re-wrap it accurately:

- `soft_delete_at` catches `UnsupportedFilesystem` around the trash rename and re-raises as "`.trash/` cannot receive a non-replacing rename … (see probe_trash)". Add a `MountBoundary` catch **before** it (same ordering rule as the upload route: the subclass handler must come first or it is unreachable) that re-raises `MountBoundary` naming the actual shape: the file lives on a different mount than the vault root's `.trash`, the rename cannot cross the boundary, and `permanent=True` is the workaround.
- Audit the other `except UnsupportedFilesystem` re-wrap sites the rename feeds (`move_note`'s tool body, `delete_note`/`delete_file` surfaces) for the same must-not-swallow-the-subclass ordering; where they merely propagate the message, nothing changes.

### D2 — Best-effort preflights at the two primitives, forward direction only

- **Soft delete (#108):** in `soft_delete_at`, after `trash_fd` is opened and before the rename, compare `src_dir_fd` with `trash_fd`. Mismatch → the same `MountBoundary` text as D1's re-wrap. This covers `delete_note` and `delete_file` in one place.
- **Move (#109):** in `move_file_no_clobber`, on the **forward** path only (i.e. when `confirmation` is presented), compare `source.dir_fd` with `destination.dir_fd` before the rename. Mismatch → `MountBoundary`. The check runs after the confirmation/permit bookkeeping so a refusal cannot leave a spent-but-unused confirmation ambiguity — the confirmation is spent by `_require_confirmation` exactly as on any refused publish, which is the existing behavior for a rename that fails. The **rollback** path (`permit is not None`) never preflights: a rollback must attempt the rename regardless — refusing it strands the note at the destination on the strength of a preflight, and if the forward rename landed, both parents are on one mount anyway, so the check could only ever misfire there.

**Best-effort means: skip on "cannot answer", refuse only on a definite mismatch.** `same_mount` raises `UnsupportedFilesystem` (via `mount_id_of`) where `STATX_MNT_ID` is unavailable. The existing `require_same_mount` treats that as fatal, which is right for the transfer path — a late `EXDEV` there costs a fully-streamed body, so "cannot check" must not mint. Here the failure it would prevent costs nothing: the rename fails immediately, and D1's mapping names the cause. Failing *closed* on a degraded kernel would instead remove soft delete and `move_note` from kernels 5.6–5.8 that serve them fine today. So these preflights wrap the comparison and treat "cannot establish" as "proceed to the rename". A small `vault_fs` helper (`cross_mount_definitely(fd_a, fd_b) -> bool`, or equivalent) holds the try/except so the policy is written once; it must **not** be reused by the transfer path, whose fail-closed direction is deliberate — a docstring on the helper says so.

`same_mount`'s no-time-spanning rule is respected by construction: both ids are read inside one call, immediately before the rename, and never stored. The preflight is check-then-act like every other preflight in this file — a bind mount appearing between it and the rename is caught by D1's mapping, which is why the backstop, not the preflight, is the correctness layer.

### D3 — Vault-side named-fallback mappings (#110)

- `_link_staged_name`: split `EXDEV` out of the `(EPERM, EOPNOTSUPP, EXDEV)` tuple → `MountBoundary` naming the boundary; `EPERM`/`EOPNOTSUPP` keep the hard-link-support message, which is true for them.
- The fallback overwrite `os.replace` in `_atomic_write_at`: catch `OSError` with `errno == EXDEV` → `MountBoundary`; everything else propagates as before.

**Reachability, honestly:** the note path stages *beside the destination* — same directory, same `dir_fd` on both sides of the link/replace — so a genuine `EXDEV` needs an exotic layout (e.g. a mount pinned on the leaf name itself typically yields `EBUSY`/`EEXIST`, not `EXDEV`; overlay/network filesystems have their own quirks). These mappings are defensive accuracy for a message that is wrong *whenever* it fires, not a claim that the fire is common. Consequently they are tested by fault injection (monkeypatch `os.link`/`os.replace` to raise `EXDEV`) rather than by a real bind mount, which cannot produce a same-directory `EXDEV`.

### D4 — #115: thread the published state into the outer discard, at the call site

In `stream_to_vault`'s outer `except BaseException` cleanup, replace `vault_fs.discard_temp(staging_fd, tmp_name, staged_st)` with `vault_fs.discard_staged_name(staging_fd, tmp_name, staged_st, published=state["published"])`.

- `discard_staged_name` already implements both directions ("an absent name is ordinary only after a publish that consumes it"); the bug is purely that this caller reaches it through `discard_temp`, whose `published=False` is baked in. `discard_temp` keeps its name, contract and docstring — it *is* the abandon path, and its other callers are genuinely pre-publication.
- The alternative in the issue — clearing `tmp_name` inside `on_published` — is rejected: `tmp_name = None` in the happy path is assignment to a local after the call returns; doing it from the callback means mutating shared state from inside `_publish_into_current_parent`, and it would also skip the *quiet* discard the no-clobber link publish still owes (its staging name legitimately survives publication and should be unlinked, inode-guarded, not left as litter).
- Semantics after the fix, per `discard_staged_name`'s existing contract: `published=True` + absent name → silent no-op (the overwrite rename consumed it); `published=True` + present matching name → quiet unlink (the link publish's leftover source); `published=False` branches unchanged. No behavior change for any pre-publication failure.

### D5 — Tests

- **Real nested mount** (`tests/_nested_mount_cases.py`, run via `unshare` from `tests/test_nested_mount_publication.py`): soft delete of `M/x.md` (both `delete_note` and `delete_file`) refuses with `MountBoundary` text naming the mount layout and never `.trash`-blames the filesystem; `move_note("M/a.md", "a.md")` likewise, and the vault is untouched afterwards (source still present, no DB update — assert via the tool result since sandbox mode has no DB); a move that stays on one side of the boundary still works.
- **Degraded-kernel policy** (sibling stub-based module, where mount ids are already stubbed): with `mount_id_of` raising `UnsupportedFilesystem`, the soft-delete and move preflights *skip* and the operation proceeds (same-mount case succeeds); the transfer path's `require_same_mount` still refuses — pinning the two directions apart.
- **Fault injection** (#110): monkeypatch the link/replace to raise `EXDEV` inside a named-fallback note write; assert `MountBoundary` with mount-layout text, and that `EPERM`/`EOPNOTSUPP` still yield the hard-link message.
- **#115**: fallback mode, successful overwrite publish, `os.fsync` (or the flush helper) raising `EIO` on the destination directory → `PostPublishFailure` propagates, claim strands, and **no** "disappeared" warning is logged (assert via `caplog`); the inverse case (pre-publication failure, name genuinely taken/absent) still warns.

## Risks / Trade-offs

- **[Preflight refuses a would-have-worked rename]** — only possible if `same_mount` answers a definite mismatch for two directories the kernel would happily rename between; `EXDEV` across mounts is unconditional for `rename(2)`, so a definite mount-id mismatch is a definite `EXDEV`. The remaining risk is a *stale* answer, which `same_mount`'s single-call rule already excludes.
- **[Error-text consumers]** — agents or tests keying on the old "`.trash/` cannot receive…" / "does not support hard links" strings for cross-mount layouts will see new text. That is the point; `MountBoundary` subclassing `UnsupportedFilesystem` keeps every typed handler working. Grep tests for the old strings and update the ones that pinned the lying message.
- **[Rollback paths]** — D1 changes the exception *type* a failed rollback rename can raise. Audit `_refuse_a_moved_directory` and `_verify_the_moved_inode`'s failure reporting to confirm they treat `MountBoundary` as they treated `UnsupportedFilesystem` (they catch broadly and report location; expected no change, must be verified, and the forward preflight makes a cross-mount rollback near-unreachable anyway).
- **[Scope creep temptation]** — `probe_trash`/`probe_publication` stay per-root and deliberately do not learn about interior mounts (a probe is per root, the boundary is per pair); the preflights are the per-pair answer. Do not add mount enumeration.

## Migration Plan

No migrations, no config, no API changes. Ship as one deploy; `make test-schema` not required (no migration). Rollback is a plain revert.

## Open Questions

None — the one policy fork (fail-open preflight here vs fail-closed on the transfer path) is decided above with rationale.
