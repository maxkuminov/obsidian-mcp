# Tasks

**These three groups are sequential, not parallel.** Group 1 changes how every
parent descriptor in the process is obtained, so groups 2 and 3 are written
against its result; group 3 rewrites the staging and publish that group 2 adds
flushes to. Do not fan them out into worktrees — the file scopes overlap
almost completely (`vault_fs.py`, `transfer.py`) and the merge would be the
whole diff. Land 1, verify, then 2, verify, then 3.

Order and dependency: **#87 → #97 → #92-item-1**.

## 1. #87 — the beneath-root lookup becomes one kernel-enforced call

*Scope: `src/services/vault_fs.py`, `src/main.py`, `src/services/vault.py`
(docstring only), tests.*

- [ ] 1.1 Bind `openat2(2)` through `ctypes` in `vault_fs`, following
  `_resolve_renameat2` / `_renameat2_raw` exactly: the glibc symbol when it
  exists, the raw `syscall()` with a per-architecture number table as the
  pre-2.28 fallback, an architecture that is not in the table treated as "no
  `openat2`" rather than guessed at, one cached resolution per process, and a
  single `_openat2_raw` that returns an errno rather than raising so every
  mapping branch is drivable from a test
- [ ] 1.2 Define `struct open_how` (`flags`, `mode`, `resolve` — three
  `__u64`s) as a `ctypes.Structure` and pass `sizeof` as the `size` argument;
  map `E2BIG` (unknown structure size) to the same unavailability answer as
  `ENOSYS`
- [ ] 1.3 Rewrite `open_dir_beneath` to obtain the returned descriptor from a
  single `openat2(root_fd, rel_dir, RESOLVE_BENEATH | RESOLVE_NO_SYMLINKS |
  RESOLVE_NO_MAGICLINKS, O_RDONLY|O_DIRECTORY|O_CLOEXEC)`, keeping its name,
  signature, the `_split` pre-check and every existing error type. Do **not**
  set `RESOLVE_NO_XDEV` (D16)
- [ ] 1.4 Errno mapping, with a test per branch: `ELOOP` → the existing
  `UnsafePath` traversal message; `EXDEV` → `UnsafePath` naming the containment
  violation, **not** `UnsupportedFilesystem` (D17); `ENOENT` →
  `FileNotFoundError`; `ENOTDIR` → `UnsafePath`; `ENOSYS`/`EPERM`/`E2BIG` →
  `UnsupportedFilesystem` naming `openat2`, the 5.6 kernel and the seccomp
  profile; `EAGAIN` → bounded retry, then refuse
- [ ] 1.5 Keep the per-component `mkdir` walk for `create=True`, and make the
  descriptor the caller receives come from a *fresh* single beneath-root
  lookup performed after creation finishes. No descriptor the creation walk
  produces may be returned or written through
- [ ] 1.6 Confirm every caller inherits it without its own change:
  `_open_parent` / `open_parent` (transfer publish, `delete_file`),
  `open_staging_dir`, `probe_trash`'s trash open, and
  `MutableTarget.ensure_parent` (the deferred-creation site). Grep for direct
  `os.open(..., dir_fd=...)` descents that bypass the helper
- [ ] 1.7 `_check_openat2_support()` in `src/main.py`, called from `lifespan`
  beside `_check_pgvector_version`, skipped under `MCP_SANDBOX_MODE`:
  read-only (`openat2` of `"."` relative to a descriptor the process already
  holds — it must create nothing), `logging.critical` + `sys.exit(1)` naming
  the syscall, the kernel requirement and the container seccomp profile as the
  two causes
- [ ] 1.8 Tests: an ancestor renamed out of the vault mid-lookup is refused
  rather than yielding an outside descriptor; a symlinked component still gets
  the traversal error; `..` and absolute paths still refused by `_split` with
  their own messages; a mount point beneath the root still resolves; the
  deferred-creation path re-opens; `EAGAIN` retried then refused; each
  unavailability errno produces the refusal and **no** per-component fallback;
  the startup probe exits with the named message and creates nothing
- [ ] 1.9 Update the `vault_fs` module docstring and `vault.py`'s "remaining
  residual" paragraph; **rewrite CLAUDE.md's "The accepted residual,
  precisely"** so the non-atomic-walk bullet is gone and the list describes
  what is actually left

## 2. #97 — staged bytes and the publication are made durable

*Scope: `src/services/transfer.py`, `src/services/vault.py`, tests.*

Depends on group 1: the destination descriptor that gets flushed is the one
group 1 now produces.

- [ ] 2.1 `stream_to_vault` / `_stream_locked`: flush the staged payload
  immediately after `_drain` returns, beside the `os.fchmod` from #95, before
  the descriptor closes and before `before_publish()` is entered — never
  inside the gate, where it would hold `SELECT … FOR UPDATE` locks across up
  to 25 MB of I/O
- [ ] 2.2 Run that flush off the event loop (`asyncio.to_thread`). Unlike
  `_drain`'s per-chunk `_write_all`, a single `fsync` can wait on the whole
  body reaching the device, and `TRANSFER_MAX_CONCURRENT_UPLOADS` of them
  inline would stall every other request in the process
- [ ] 2.3 A failed payload flush stays pre-publication: nothing published, the
  staged bytes discarded, the claim released, `PostPublishFailure` **not**
  raised
- [ ] 2.4 `_publish_into_current_parent`: flush the destination directory
  after `on_published` has recorded the publication and before the function
  returns — the ordering is what guarantees a failure is classified
  post-publication and converted to `PostPublishFailure` by
  `_stream_locked`'s existing handler, rather than escaping as a bare `OSError`
  the route reads as "nothing was published"
- [ ] 2.5 When the call created directories on the way to the destination,
  flush each created directory's parent as well, outward to the first
  directory that already existed — otherwise a crash can lose the new folder
  and take a `completed` upload with it
- [ ] 2.6 `vault._atomic_write_at`: flush `target.dir_fd` after publication.
  A failure there is **logged and swallowed**, and the write reported as the
  success it is (D18) — the opposite direction from the transfer path, for the
  `edit_note(append=True)`-retry reason
- [ ] 2.7 Confirm `import_from_url` is covered by 2.1–2.5 through the shared
  `stream_to_vault`, and that its gate's `complete()` no-op is unaffected
- [ ] 2.8 Tests: the payload flush happens before the gate is entered and
  after the body is fully drained; it does not block the event loop; a failing
  payload flush releases the claim and leaves nothing at the path; a failing
  directory flush leaves the file at the path, the token `claimed` (never
  `pending`), the response a post-publication failure, and `check_upload`
  answering `uploading`/`unknown` rather than `completed`; the note path's
  directory flush failure does not turn a landed write into a reported failure
- [ ] 2.9 CLAUDE.md: state the durability contract for both paths and the
  asymmetry in the failure direction, in the transfer and anchored-write
  sections

## 3. #92 item 1 — transfer staging has no directory entry

*Scope: `src/services/vault_fs.py`, `src/services/transfer.py`,
`src/services/vault.py`, tests. **Item 1 only** — the actor label on
`transfer_tokens` (item 2) and the scope echo (item 3) are not in this
change.*

Depends on group 2: the payload flush moves onto the unnamed descriptor.

- [ ] 3.1 Move `_link_staged_inode` and `_proc_fd_available` from `vault.py`
  into `vault_fs.py` and have `vault.py` call them, so the two publish paths
  share one implementation. Keep every kernel note in the docstring:
  `AT_EMPTY_PATH` needs `CAP_DAC_READ_SEARCH` and the `/proc` magic link does
  not; the zero-link-inode rule is about *deleted* inodes; **`O_EXCL` must not
  be set** with `O_TMPFILE` — it forbids linking and makes the publish `ENOENT`
- [ ] 3.2 Add unnamed staging to `vault_fs` (`O_TMPFILE|O_RDWR` relative to the
  staging descriptor) and use it from `_stream_locked` in place of
  `create_temp`; `UnsupportedFilesystem` when the filesystem refuses it, never
  a fallback to a named staging file
- [ ] 3.3 `publish` takes the staged descriptor rather than a staging name.
  No-clobber → `linkat` through `/proc/self/fd/<fd>` into the destination
  descriptor. Overwrite → materialise a transient name **in the staging
  directory** from that same descriptor, no-clobber with a bounded `EEXIST`
  retry, immediately before the fingerprint check and the `renameat`; verify
  the name still refers to the staged inode before the rename; on cleanup
  unlink it only while it still does, otherwise leave it and log
- [ ] 3.4 Rework `Published.temp_removed`, `discard_temp` and
  `_stream_locked`'s `except` branch for a staging file that usually has no
  name: closing the descriptor is the discard, and the only unlink left is the
  overwrite path's transient name
- [ ] 3.5 `probe_publication` additionally exercises unnamed staging and the
  by-descriptor publication, so a filesystem or container that cannot do them
  is refused at the probe rather than at the first upload
- [ ] 3.6 Leave `prune_stale_staging` in place and update its docstring: it
  has pre-change litter to collect and a rolling deploy to survive, and it no
  longer has anything new to collect (D19). Leave `open_staging_dir`'s `0700`
  and owner check in place and record that they are now defence in depth plus
  the guard on the transient overwrite name
- [ ] 3.7 Tests: `.transfer-tmp` holds no entry for the staged bytes at any
  point while a body streams; a killed upload leaves nothing to sweep; the
  overwrite path's transient name exists only inside the gate and only in
  `.transfer-tmp`; a substituted transient name refuses the publish and leaves
  the substitute in place; `O_TMPFILE` and `/proc` unavailability each refuse
  at the probe with a named error and never fall back
- [ ] 3.8 CLAUDE.md: extend "The no-clobber publish never exposes a staging
  name at all" to cover the transfer path, and record the overwrite path's
  in-gate window

## 4. Verification

- [ ] 4.1 `openspec validate atomic-beneath-root-writes --strict` passes
- [ ] 4.2 Full test suite green; `make audit` clean
- [ ] 4.3 Confirm the new tests fail against the pre-change tree, per group,
  and record which ones do not and why (they pin guarantees this change
  preserves rather than introduces)
- [ ] 4.4 Adversarial Codex pass — this change is squarely in the destructive-
  write class: framing must say that a false "nothing was published" hands
  back a replayable capability over an existing file, and that a containment
  guard which degrades silently is worse than one that refuses
- [ ] 4.5 End-to-end exercise against the live server, naming the tools
  actually called: `request_upload` + a real `PUT /transfer/upload` (both
  no-clobber and overwrite), `check_upload`, `import_from_url`, `create_note`,
  `edit_note(append=True)`, `move_note`, `delete_note`, `write_file`,
  `delete_file`
- [ ] 4.6 Deploy on a host whose kernel and seccomp profile allow `openat2`,
  and confirm the startup probe logs nothing on the happy path
