# Tasks

**Groups 1 to 3 are sequential, not parallel.** Group 1 changes how every
parent descriptor in the process is obtained, so groups 2 and 3 are written
against its result; group 3 rewrites the staging and publish that group 2 adds
flushes to. Do not fan them out into worktrees — the file scopes overlap
almost completely (`vault_fs.py`, `transfer.py`) and the merge would be the
whole diff. Land 1, verify, then 2, verify, then 3. **Group 4 is independent**
— it adds a check rather than changing how anything is resolved, staged or
published — but it touches the same two files, so run it after 3 rather than
beside it.

Order and dependency: **#87 → #97 → #92-item-1**, then group 4, which is
independent of all three and which nothing in them depends on. No task depends
on a later one: 2.5a puts the probe's flush coverage in the same group as the
flushes it guards rather than in group 3, 3.5 keeps those checks rather than
introducing them, and 3.0 settles the named-staging fallback flag before any
task in group 3 reads it.

## 1. #87 — the beneath-root lookup becomes one kernel-enforced call

*Scope: `src/services/vault_fs.py`, `src/main.py`, `src/services/vault.py`
(docstring only), tests. `src/mcp_server/tools.py` and
`src/transfer/routes.py` are read for 1.6 and not changed.*

- [x] 1.1 Bind `openat2(2)` through `ctypes` in `vault_fs`, following
  `_renameat2_raw`'s *shape* but not its resolution order: **glibc exports no
  `openat2` wrapper at any version** (D24), so the raw `syscall()` with a
  per-architecture number table is the implementation, not a fallback. It is
  `__NR_openat2 == 437` on every architecture that has it, an architecture
  absent from the table is treated as "no `openat2`" rather than guessed at,
  the resolution is cached once per process, and a single `_openat2_raw`
  returns an errno rather than raising so every mapping branch is drivable
  from a test. Do **not** carry `_resolve_renameat2`'s `pragma: no cover`
  markers across — the branches they cover are unreachable there and are the
  normal path here
- [x] 1.2 Define `struct open_how` (`flags`, `mode`, `resolve` — three
  `__u64`s) as a `ctypes.Structure` and pass `sizeof` as the `size` argument.
  Map **both** `EINVAL` (a `size` smaller than any version the kernel knows,
  or an unrecognised flag/`resolve` bit) and `E2BIG` (nonzero extension data
  past the size this kernel knows) to the unavailability answer, naming the
  ABI mismatch. Neither is reachable from a correct binding, which is exactly
  why neither may escape as a generic `OSError` — see D24 for the measured
  behaviour, and note the first draft had these two the wrong way round
- [x] 1.3 Rewrite `open_dir_beneath` to obtain the returned descriptor from a
  single `openat2(root_fd, rel_dir, RESOLVE_BENEATH | RESOLVE_NO_SYMLINKS |
  RESOLVE_NO_MAGICLINKS, O_RDONLY|O_DIRECTORY|O_CLOEXEC)`, keeping its name,
  signature, the `_split` pre-check and every existing error type. Do **not**
  set `RESOLVE_NO_XDEV` (D16)
- [x] 1.4 Errno mapping, with a test per branch: `ELOOP` → `UnsafePath`
  naming the **requested vault-relative path**, with any component
  identification explicitly best-effort (D25); `EXDEV` → `UnsafePath` naming
  the containment violation, **not** `UnsupportedFilesystem` (D17); `ENOENT` →
  `FileNotFoundError`; `ENOTDIR` → `UnsafePath`; `ENOSYS`/`EPERM` →
  `UnsupportedFilesystem` naming `openat2`, the 5.6 kernel and the seccomp
  profile; `EINVAL`/`E2BIG` → `UnsupportedFilesystem` naming the `open_how`
  ABI mismatch; `EAGAIN` **and `EINTR`** → bounded retry, then refuse — the
  `os.open` walk retried `EINTR` transparently and a raw syscall does not, so
  omitting it turns a signal into a false failure of a write (D17)
- [x] 1.5 Rewrite the `create=True` descent so **no directory descriptor is
  carried across a creation**: for each missing component, re-acquire the
  already-existing prefix with a fresh `openat2` from the root, issue the
  one `mkdirat` through it, drop it. The directory descriptor the caller
  receives then comes from a *fresh* single beneath-root lookup of the whole
  parent performed after creation finishes; no directory descriptor the
  creation produced may be returned to a caller or used as a pathname
  anchor. Record the residual this leaves in the docstring — at most one
  empty directory per component **per creation descent**, if a prefix is
  renamed out inside a one-syscall window; never a file, never something the
  tool reports success about; and an upload performs two such descents while
  a note write performs one — and do **not** try to clean it up: an `rmdir`
  by name is the delete-the-substitute hazard `_discard_temp` already
  refuses (D22)
- [x] 1.6 Confirm every caller inherits it without its own change:
  `_open_parent` / `open_parent` (transfer publish, `delete_file`),
  `open_staging_dir`, `probe_trash`'s trash open,
  `MutableTarget.ensure_parent` (the deferred-creation site), **and the
  read-side callers the first draft omitted** — `tools._fingerprint_of` and
  `tools._head_bytes` (mint-time fingerprint and MIME sniff) and
  `routes._open_bound_file` (`GET|HEAD /transfer/download/file`). Grep for
  direct `os.open(..., dir_fd=...)` descents that bypass the helper. Record
  the error each of those three surfaces produces: the two tool-side readers
  raise to an authenticated caller, the download route keeps its uniform 404
  (`except (FileNotFoundError, VaultFSError, OSError)`) and must **not** grow
  a distinguishable status — see D21
- [x] 1.7 `_check_openat2_support()` in `src/main.py`, called from `lifespan`
  beside `_check_pgvector_version`, skipped under `MCP_SANDBOX_MODE`:
  read-only (`openat2` of `"."` relative to a descriptor the process already
  holds — it must create nothing), `logging.critical` + `sys.exit(1)` naming
  the syscall, the kernel requirement and the container seccomp profile as the
  two causes
- [x] 1.8 Tests: an ancestor renamed out of the vault mid-lookup is refused
  rather than yielding an outside descriptor; a symlinked component still gets
  the traversal error, naming the requested path; `..` and absolute paths still
  refused by `_split` with their own messages *before* the syscall (the kernel
  would accept `A/../A` under `RESOLVE_BENEATH` — D17); a mount point beneath
  the root still resolves; the deferred-creation path re-acquires the prefix
  per component and re-opens the parent afterwards; `EAGAIN` retried then
  refused; **one `EINTR`-then-success sequence completes the write**; each
  unavailability errno (`ENOSYS`, `EPERM`, `EINVAL`, `E2BIG`) produces the
  refusal and **no** per-component fallback; the startup probe exits with the
  named message and creates nothing; with the syscall unavailable and the
  startup probe skipped, a download redemption still answers the uniform 404
- [x] 1.9 Update the `vault_fs` module docstring and `vault.py`'s "remaining
  residual" paragraph; **rewrite CLAUDE.md's "The accepted residual,
  precisely"** so the non-atomic-walk bullet is *replaced* — the lookup window
  is gone, and the creation-side residual (D22) takes its place, stated as
  precisely as the bullets around it. The proposal's claim and that section
  must end up telling the same story in the same words: **Every below-root
  directory descriptor a call uses as a pathname anchor comes from a lookup
  the kernel proved beneath the vault root at the moment it resolved, and no
  directory descriptor retained from a creation descent is ever returned to
  a caller or used as a pathname anchor — so no operation is ever redirected
  into a directory that was never beneath the root.**
  Scope it to *directory* descriptors used as pathname anchors and keep it
  there: a transfer's own staged payload descriptor is created by the call
  and is written, flushed and published through by descriptor, so the
  broader "no descriptor whose containment the kernel did not check is ever
  acted through" is false and must not be written anywhere. Then: creation
  keeps a bounded empty-directory residual, at most one per component per
  creation descent (D22); and a lookup proves containment only at the
  instant it resolves, so a rename landing before the publish carries the
  call with it (D26). Do **not** write "nothing outside the root is ever
  written" unqualified — it is false, and it was the claim review rejected

## 2. #97 — staged bytes and the publication are made durable

*Scope: `src/services/transfer.py`, `src/services/vault.py`,
`src/services/vault_fs.py` (2.5a only), tests.*

Depends on group 1: the destination descriptor that gets flushed is the one
group 1 now produces.

- [x] 2.1 `stream_to_vault` / `_stream_locked`: flush the staged payload
  immediately after `_drain` returns, beside the `os.fchmod` from #95, before
  the descriptor closes and before `before_publish()` is entered — never
  inside the gate, where it would hold `SELECT … FOR UPDATE` locks across up
  to 25 MB of I/O
- [x] 2.2 Run that flush off the event loop (`asyncio.to_thread`). Unlike
  `_drain`'s per-chunk `_write_all`, a single `fsync` can wait on the whole
  body reaching the device, and `TRANSFER_MAX_CONCURRENT_UPLOADS` of them
  inline would stall every other request in the process
- [x] 2.3 A failed payload flush stays pre-publication: nothing published, the
  staged bytes discarded, the claim released, `PostPublishFailure` **not**
  raised
- [x] 2.4 `_publish_into_current_parent`: flush the destination directory
  after `on_published` has recorded the publication and before the function
  returns — the ordering is what guarantees a failure is classified
  post-publication and converted to `PostPublishFailure` by
  `_stream_locked`'s existing handler, rather than escaping as a bare `OSError`
  the route reads as "nothing was published"
- [x] 2.5 When the call created directories on the way to the destination,
  flush each created directory's parent as well, outward to the first
  directory that already existed — otherwise a crash can lose the new folder
  and take a `completed` upload with it
- [x] 2.5a Extend the **existing** `probe_publication` — in this group, not the
  next — so it `fsync`s the temp file it already creates and `fsync`s a
  directory descriptor. Without this, a tree that has landed groups 1 and 2 on
  a filesystem that hard-links happily and rejects a directory `fsync` passes
  the unchanged probe, mints a token, streams a whole body, publishes, and only
  then strands the claim on the flush 2.4 just introduced — the failure has to
  be detectable from the moment the flush exists, and group 2 is a shippable
  head of the tree. Group 3 rewrites this probe and **keeps** both checks (3.5)
- [x] 2.6 `vault._atomic_write_at`: flush `target.dir_fd` after publication.
  A failure there is **logged and swallowed**, and the write reported as the
  success it is (D18) — the opposite direction from the transfer path, for the
  `edit_note(append=True)`-retry reason
- [x] 2.6a Flush the newly created ancestors too. `MutableTarget.ensure_parent`
  can create a whole chain, and flushing only the immediate parent leaves the
  entry that names *it* unflushed — a crash then loses the folder and the note
  the tool reported written. Have `ensure_parent` record which directories it
  created and flush each created directory's parent outward to the first
  pre-existing one, under the same logged-and-swallowed policy as 2.6
- [x] 2.6b Confirm the durability is inherited by **every** `_atomic_write_at`
  caller, not just the note tools: `write_file_at`, `write_bytes_at`, and so
  `write_file` in both its no-clobber and `overwrite=True` modes. The
  requirement now names `write_file` explicitly because an implementation
  could otherwise satisfy the tool list literally and skip it
- [x] 2.6c The rename publications get the same treatment, for the same
  reason #97 exists: deciding durability once for both paths rather than
  fixing half of one twice. `move_note`'s `renameat2` writes two directory
  entries, so `move_file_no_clobber` flushes both parents (and the rollback in
  `_verify_the_moved_inode` inherits it by calling the same helper with the
  targets swapped); `soft_delete_at` flushes the source's parent and `.trash`,
  and `_refuse_a_moved_directory` flushes both after a rollback that lands;
  the permanent unlink flushes the parent it removed the entry from, in
  `vault_fs.remove` and in `delete_note(permanent=True)`. All D18 direction
  via `vault_fs.flush_dir_quietly` — a move or delete reported as failed after
  its rename landed gets retried against a source that is no longer there
- [x] 2.7 Confirm `import_from_url` is covered by 2.1–2.5 through the shared
  `stream_to_vault`, and that its gate's `complete()` no-op is unaffected
- [x] 2.8 Tests: the payload flush happens before the gate is entered and
  after the body is fully drained; it does not block the event loop; a failing
  payload flush releases the claim and leaves nothing at the path; a failing
  directory flush leaves the file at the path, the token `claimed` (never
  `pending`), the response a post-publication failure, and `check_upload`
  answering `uploading`/`unknown` rather than `completed`; the note path's
  directory flush failure does not turn a landed write into a reported failure;
  `create_note("New/Folder/x.md")` flushes `Folder`, `New` and the root entry
  that names `New`, and a failure of any of those is logged rather than
  reported; `write_file` gets the same flushes as `create_note` in both
  overwrite modes
- [x] 2.9 CLAUDE.md: state the durability contract for both paths and the
  asymmetry in the failure direction, in the transfer and anchored-write
  sections

## 3. #92 item 1 — transfer staging has no directory entry

*Scope: `src/services/vault_fs.py`, `src/services/transfer.py`,
`src/services/vault.py`, `src/main.py` (the `/health` field), `src/config.py`
(**only** in the pre-PR case of 3.0), tests. **Item 1 only** — the actor label
on `transfer_tokens` (item 2) and the scope echo (item 3) are not in this
change.*

Depends on group 2: the payload flush moves onto the unnamed descriptor.

**Ordering note — the named-staging fallback flag is shared with a contributor
PR that is in flight (#103).** `O_TMPFILE` is refused server-side by TrueNAS
SCALE's NFS, so an accepted contributor change adds
`VAULT_ALLOW_NAMED_STAGING_FALLBACK` (env, default `false`) for the *note*
path. This group honours the same flag for the transfer path (D27), which
means the two changes touch one setting and either may land first. 3.0 is the
task that resolves that, and every later task in this group reads the flag
through whatever 3.0 established — no task here depends on the contributor PR
having landed, and none of them introduces a second knob. Coordinate on #103,
not in a merge.

- [x] 3.0 Settle the fallback flag before anything reads it. If the
  contributor PR (#103) has landed, consume the existing
  `Settings.vault_allow_named_staging_fallback` and add **nothing** to
  `src/config.py`. If it has not, introduce that field here under exactly that
  name, exactly that environment variable (`VAULT_ALLOW_NAMED_STAGING_FALLBACK`)
  and exactly that default (`false`), and say so on #103 so the PR rebases onto
  it. Either way there is one setting and one knob for both write paths — no
  `TRANSFER_*` variant, no per-path override, no "transfers only" escape
  (D27). Nothing else in this group defines or renames it
- [x] 3.1 Move `_link_staged_inode` and `_proc_fd_available` from `vault.py`
  into `vault_fs.py` and have `vault.py` call them, so the two publish paths
  share one implementation. Keep every kernel note in the docstring:
  `AT_EMPTY_PATH` needs `CAP_DAC_READ_SEARCH` and the `/proc` magic link does
  not; the zero-link-inode rule is about *deleted* inodes; **`O_EXCL` must not
  be set** with `O_TMPFILE` — it forbids linking and makes the publish `ENOENT`
- [x] 3.2 Add unnamed staging to `vault_fs` (`O_TMPFILE|O_RDWR` relative to the
  staging descriptor) and use it from `_stream_locked` in place of
  `create_temp` on every root whose probe selected the unnamed mode.
  `UnsupportedFilesystem` when the filesystem refuses it **and the flag from
  3.0 is off**, with the error naming that flag the way the note path's
  refusal does — never an unflagged fallback to a named staging file
- [x] 3.2a The fallback branch (D27): where the probe selected named staging,
  `_stream_locked` keeps the **pre-change** `create_temp` path in
  `.transfer-tmp` exactly as it is — exclusive, `O_NOFOLLOW`, through the
  staging descriptor the beneath-root lookup returned. Nothing outside the
  staging mode changes: the payload flush, the directory flush, the publish
  gate and its lock order, the size caps and the token state machine are the
  same code on both branches. Do not grow a second streaming path; branch at
  staging and at publication only
- [x] 3.3 `publish` takes the staged descriptor rather than a staging name in
  the unnamed mode. No-clobber → `linkat` through `/proc/self/fd/<fd>` into
  the destination descriptor. Overwrite → materialise a transient name **in
  the staging directory** from that same descriptor, no-clobber with a bounded
  `EEXIST` retry, immediately before the fingerprint check and the `renameat`;
  verify the name still refers to the staged inode before the rename; on
  cleanup unlink it only while it still does, otherwise leave it and log
- [x] 3.3a In the fallback mode `publish` keeps the pre-change by-name form —
  `create_temp`'s `.tmp-*` in `.transfer-tmp`, `_link_no_clobber` for
  no-clobber (still `EEXIST` on an existing destination, **never** degraded to
  a replacing rename) and `os.replace` for overwrite — but with two guards the
  pre-change path does not have, because 3.3 introduces them for the transient
  overwrite name and a name that lives for minutes needs them more, not less:
  verify the staged name still refers to this call's inode immediately before
  the publish, and replace `_unlink_quietly`'s unconditional unlink with the
  inode-guarded discard (unlink only while it still refers to our inode,
  otherwise leave it and log). Do **not** carry the unconditional unlink into
  the fallback. The staged name then exists for the whole streaming window
  rather than two syscalls — that is the declared residual (D27), not a bug to
  patch here
- [x] 3.4 Rework `Published.temp_removed`, `discard_temp` and
  `_stream_locked`'s `except` branch for a staging file that usually has no
  name: in the unnamed mode closing the descriptor is the discard, and the
  only unlink left is the overwrite path's transient name. In the fallback
  mode the pre-change discard rules apply unchanged — unlink the staged name
  only while it still refers to the inode this call staged, otherwise leave it
  in place and log
- [x] 3.5 `probe_publication` additionally exercises unnamed staging and the
  by-descriptor publication, **records which staging mode the root will use**
  in its cached per-root result — unnamed, or the fallback where unnamed is
  unavailable and 3.0's flag is on — so the mode is decided once and never per
  call, and **keeps the staged-file flush and the directory flush that 2.5a
  added** — they must survive the conversion, because
  a filesystem that does `O_TMPFILE` and `linkat` but refuses a directory
  `fsync` would otherwise pass the rewritten probe, take a token and a whole
  body, publish, and only then strand the claim as a post-publication failure.
  Also state in the docstring what the probe *cannot* answer for: it links
  root→root and is cached per root, so it cannot see a destination on a
  different filesystem or mount (D23)
- [x] 3.5a The probe's two unnamed-staging outcomes. Flag off → raise
  `UnsupportedFilesystem` naming the missing capability **and** the flag, so
  no token is minted and no body is streamed. Flag on → select the fallback
  mode after establishing the primitives *it* needs (exclusive
  non-symlink-following creation in `.transfer-tmp`, the hard link within the
  root, the staged-file flush, the directory flush); a root that fails any of
  those is still refused rather than accepting a body it cannot publish
- [x] 3.5b Make the fallback observable the way the note path's is: one
  `WARNING` per process, logged the first time a call **actually stages under
  a name** — not when the flag is set, not when the probe selects the mode —
  and the same `/health` field the note path's fallback exposes, under the
  same name and meaning, reporting inactive until that first exercise. One
  field for both paths; `/health` reads process state and SHALL NOT re-probe
  (a probe writes). If 3.0 found the contributor PR already landed, wire the
  transfer path into its existing warning-once helper and its existing field
  rather than adding a parallel pair
- [x] 3.6 Leave `prune_stale_staging` in place and update its docstring: it
  has pre-change litter to collect and a rolling deploy to survive, it no
  longer has anything new to collect **in the unnamed mode** (D19), and in the
  fallback mode an abandoned or killed upload leaves a staged file exactly as
  the pre-change path did, so the sweep keeps a live purpose there (D27). Leave `open_staging_dir`'s `0700`
  and owner check in place and record that they are now defence in depth plus
  the guard on the transient overwrite name
- [x] 3.7 Tests, unnamed mode: `.transfer-tmp` holds no entry for the staged
  bytes at any point while a body streams; a killed upload leaves nothing to
  sweep; the overwrite path's transient name exists only inside the gate and
  only in `.transfer-tmp`; a transient name substituted **before** the
  identity check refuses the publish and leaves the substitute in place (the
  interval after that check is the declared residual — D20 — and is not
  asserted); directory-`fsync` unavailability refuses at the probe with a
  named error and never falls back
- [x] 3.7a Tests, mode selection and the fallback (D27), with `O_TMPFILE` and
  `/proc` unavailability simulated at the probe:
  - the probe drives the mode: a root that supports unnamed staging stages
    without a name, a root that does not (flag on) stages under one, and the
    recorded mode is used by every subsequent upload on that root rather than
    re-decided per call
  - flag **off** on such a root: the refusal is the unsupported-filesystem
    error, its message names `VAULT_ALLOW_NAMED_STAGING_FALLBACK`, no token is
    minted, nothing is staged and nothing is published
  - flag **on**: a no-clobber upload over an existing destination still fails
    on `EEXIST` through the named `link()` publish, the existing file is
    unchanged, and the claim is released; the publish never degrades to a
    replacing rename
  - flag on: the identity check precedes the publish in both publish modes, a
    substitution observable at it refuses and leaves the substitute in place,
    and no cleanup path unlinks a name that no longer refers to this call's
    inode
  - the warning fires **once per process, on first exercise** — asserted by
    setting the flag and probing with no upload (silent), then serving two
    uploads (exactly one warning)
  - `/health` reports the fallback inactive until that first exercise and
    active after it, in one field shared with the note path, and calling
    `/health` creates no file in the vault
  - an abandoned fallback upload leaves a `.tmp-*` file that the existing
    24-hour sweep collects
- [x] 3.8 CLAUDE.md: extend "The no-clobber publish never exposes a staging
  name at all" to cover the transfer path, **scoped to the mode the probe
  selects where `O_TMPFILE` works**, and record the overwrite path's in-gate
  window. Add the fallback beside it (D27): one flag for both write paths,
  default off, refusal naming the flag when it is off, warning once per
  process on first exercise, `/health` field, and the reopened window stated
  in the same register as the overwrite residual — including why the transfer
  fallback's window is *narrower* than the note path's (`.transfer-tmp` is
  `0700`, owner-checked and unreachable by any agent, capability or vault
  tool; the note path stages beside the destination). Do not describe the two
  fallbacks as equivalent
- [ ] 3.9 At archive time, reconcile with the contributor PR rather than
  overwriting it: if #103's change landed a delta to
  `openspec/specs/vault-write/spec.md` for the note path's fallback, make sure
  this change's `vault-write` delta — whose "The filesystem cannot stage
  without a name" scenario still reads as an unconditional refusal — is
  updated to match it before archiving, so promoting these deltas does not
  silently revert the contributor's requirement. Spec-only reconciliation; no
  code change belongs to this task.

  **State as of group 3 landing:** the contributor PR had **not** landed, so
  3.0 introduced `Settings.vault_allow_named_staging_fallback`
  (`VAULT_ALLOW_NAMED_STAGING_FALLBACK`, default `false`) here and the PR
  rebases onto it. The `vault-write` delta's "The filesystem cannot stage
  without a name" scenario is therefore **still an unconditional refusal**, and
  that is correct for the note path *today* — nothing in group 3 changed how
  `vault._atomic_write_at` stages. It stops being correct the moment #103's
  note-path fallback lands. Nothing is invented here on the contributor's
  behalf: the reconciliation is exactly the check this task describes, and it
  must be done before archiving. The `file-transfer` delta already carries the
  flag's full requirement, so a reader of the promoted specs would otherwise
  find one path refusing unconditionally and the other honouring a flag both
  are meant to share

## 4. D23 — transfer publication refuses a destination on another mount

*Scope: `src/services/vault_fs.py` (a `statx` mount-id helper),
`src/services/transfer.py`, `src/mcp_server/tools.py`, tests.*

Depends on nothing in groups 1–3 and nothing in groups 1–3 depends on it: it
adds a check, it does not change how any descriptor is obtained, staged or
published. It is placed last because it is the one item here that is not #87,
#97 or #92-item-1, and because the descriptors it compares should already be
the ones group 1 produces.

**Landed in group 3's commit**, not after it: the adversarial pass called the
absent preflight a MAJOR against the wave rather than a pending task, and the
same commit had to carry the `MountBoundary` errno mapping (5.0 item 3) that
group 3's own `publish` branch introduced the second half of. The ordering the
group header describes was still honoured — nothing here changed how anything
is resolved, staged or published, and every group-3 assertion was green before
these tasks were started.

- [x] 4.1 Bind `statx(2)` in `vault_fs` through the **glibc wrapper**. Unlike
  `openat2` this one exists: checked in the running container, `statx` resolves
  through `ctypes.CDLL(None)` and `openat2` raises `AttributeError`, so D24's
  raw-syscall reasoning does **not** carry over. Expose `mount_id_of(fd)`
  reading `STATX_MNT_ID` with `AT_EMPTY_PATH`, raising `UnsupportedFilesystem`
  when the returned `stx_mask` does not carry the bit. `STATX_MNT_ID` is Linux
  **5.8**, above `openat2`'s 5.6, so this raises the change's kernel floor —
  say so in the docstring and in the `openat2` startup probe's message. Do
  **not** require `STATX_MNT_ID_UNIQUE` (D23)
- [x] 4.2 `same_mount(fd_a, fd_b)` reads both ids and compares them **inside
  the one call**. Never persist an id and compare it against a later reading: a
  mount id can be reused once its mount is gone, and the only thing that makes
  plain `STATX_MNT_ID` sufficient here is that no comparison ever spans time
  (D23)
- [x] 4.3 Compare **mount identity, never `st_dev`.** Measured on the
  deployment kernel: a bind mount of an ext4 directory beneath the vault root
  gives `.transfer-tmp` and the destination the same `st_dev` (66306) and
  different mount ids (653 vs 6036), while `link` and `rename` across it both
  return `EXDEV`. An `st_dev` preflight passes and the publish fails after the
  body has streamed — that was the first draft and review caught it
- [x] 4.4 Run the check at mint: in `request_upload` before the row is inserted
  and a URL is handed out, and in `import_from_url` before the fetch begins.
  Where the destination's parent does not exist yet, check the **deepest
  existing ancestor** — a directory created beneath it is created on that
  ancestor's mount. Refuse with an error naming the mount boundary and the
  destination path, never one blaming hard-link support
- [x] 4.5 Run it again inside the publish gate, in
  `_publish_into_current_parent` after the authoritative destination lookup and
  **before** `publish`, so a mount established after the mint is refused rather
  than published into. Raising before `publish` is what keeps the refusal
  unambiguously pre-publication: `_stream_locked` then classifies it correctly,
  the claim is released, and the human may retry the same link. Note what this
  one does **not** save: by the time the gate runs the body has already
  streamed in full, so this half is pre-*publication*, not pre-*body*. Only
  4.4's check spares the bytes, and only where the boundary already existed at
  mint — do not describe the pair as "refused before any body is streamed"
- [x] 4.6 Update the docstring 3.5 added: the destination-mount case the probe
  cannot see is now covered by this preflight, and what remains uncovered is a
  capability difference between two directories on the *same* mount
- [x] 4.7 Tests, in a mount namespace with a same-filesystem bind mount beneath
  the root: the mint refuses with the named error and inserts no token;
  `import_from_url` refuses before it opens a connection; a mount established
  between mint and redemption is refused in the gate **after the body has been
  streamed in full** — assert that ordering rather than a pre-body refusal —
  with the destination keeping its prior content and the claim released to
  `pending`; a vault with no nested mount behaves exactly as before; a `statx`
  stubbed to return no mount-id bit refuses rather than falling back to
  `st_dev` or to the errno
- [x] 4.8 CLAUDE.md: record the envelope in the transfer section — publication
  into a mount beneath the vault root is refused before any body where the
  boundary already exists at mint or fetch start, and pre-publication inside
  the gate (after the body, before the link or rename) where it appears
  afterwards; reads, note writes, permanent deletes and same-side moves across
  one are unaffected; the soft delete and the cross-boundary move are known
  failures with issues open (5.0)

## 5. Verification

- [ ] 5.0 File the D23 follow-ups as **three separate issues** before this
  change is archived. Group 4 handles transfer publication; these are the
  nested-mount failures it deliberately does not. Each issue must stand on its
  own for someone reading it cold — the reproduction, the reason the existing
  probe misses it, and the options with their costs:
  1. **Soft delete fails across a nested mount.** `delete_note` and
     `delete_file(permanent=False)` move the file into `.trash` with one
     `renameat2(RENAME_NOREPLACE)`, and `.trash` is opened beneath the *root*
     descriptor (`vault_fs.soft_delete_at` → `open_dir_beneath(root_fd,
     ".trash")`). A file on a mount beneath the vault root therefore fails
     `EXDEV`, surfaced as `UnsupportedFilesystem` — "`.trash/` cannot receive a
     non-replacing rename from the vault" — which names the wrong cause.
     `probe_trash` cannot catch it: it creates its temp at the root and renames
     into the root's `.trash`, root→root, so it passes on a vault where every
     such delete fails. Reproduced on kernel 6.8 with an ext4 directory
     bind-mounted beneath the root. Options: a per-mount `.trash`, or an early
     refusal whose message names the mount boundary. A copy-and-unlink fallback
     is **not** an option — it is exactly the `link`+`unlink` shape
     `soft_delete`'s docstring exists to refuse, because it can unlink a
     different inode than it copied. Workaround today: `permanent=True`
  2. **`move_note` fails across a nested-mount boundary.** `move_note`
     publishes with one `renameat2(RENAME_NOREPLACE)` through
     `vault_fs.rename_noreplace`, issued against the *source* parent
     descriptor and the *destination* parent descriptor. When those two
     parents sit on different mounts the kernel returns `EXDEV`, which
     `rename_noreplace` maps to `UnsupportedFilesystem` — a message blaming
     a filesystem that renames perfectly well. Reproduction, on kernel 6.8
     with ext4: `mount --bind <vault>/M <vault>/M` so that `M` is a mount of
     the same filesystem as the vault root (same `st_dev`, different
     `STATX_MNT_ID`), create `M/a.md`, then call `move_note("M/a.md",
     "a.md")` — `EXDEV`. Moves that stay on one side of the boundary work.
     **No existing probe can catch it, because there is no move probe at
     all**: `probe_publication` links root→root and `probe_trash` renames
     root→root/`.trash`, so both exercise the root against itself and
     neither touches the source-parent/destination-parent pair a move
     actually names — run against the vault above, both pass while every
     cross-boundary move fails. Group 4's mount-identity check does not
     cover it either: that one is transfer publication only, and it compares
     the *staging* directory with the destination parent, not the two
     parents of a move. Options: refuse early with a message naming the
     mount boundary and both paths — group 4's `same_mount(fd_a, fd_b)`
     already performs exactly this comparison and would need only the move's
     two parent descriptors — or stage a copy, which carries the same
     objection as (1) and additionally breaks `move_note`'s "whichever inode
     is at the source when the call runs is what moves" guarantee.
     Workaround today: read the note, write it at the destination, delete
     the source
  3. **Destination-mount errno mapping is inconsistent between the two publish
     modes.** `vault_fs._link_no_clobber` maps `EXDEV` to
     `UnsupportedFilesystem` reading "the vault filesystem does not support
     hard links" — false: the filesystem does, the mount boundary does not.
     `vault_fs.publish`'s overwrite branch calls a bare `os.replace` with no
     `EXDEV` mapping at all, so it escapes as a plain `OSError`, is classified
     pre-publication (correctly — nothing was published) and reaches the upload
     route's generic `except Exception`, giving the person a server error where
     the other mode gives a 503. Group 4's preflight makes this rare rather
     than gone, since the preflight is check-then-act, so both branches should
     name the mount boundary
- [ ] 5.1 `openspec validate atomic-beneath-root-writes --strict` passes
- [ ] 5.2 Full test suite green; `make audit` clean
- [ ] 5.3 Confirm the new tests fail against the pre-change tree, per group,
  and record which ones do not and why (they pin guarantees this change
  preserves rather than introduces)
- [ ] 5.4 Adversarial Codex pass — this change is squarely in the destructive-
  write class: framing must say that a false "nothing was published" hands
  back a replayable capability over an existing file, and that a containment
  guard which degrades silently is worse than one that refuses. Point it at
  D20, D22, D23 and **D26** explicitly: each is a *declared* residual, and the
  question to ask of them is whether the declaration is honest and bounded, not
  whether the residual exists. D26 in particular is the one review already
  overturned once — the change must not claim anywhere that nothing is ever
  written outside the root, only that nothing is ever *redirected* into a
  directory that was never beneath it. Two scoping limits of that claim are
  load-bearing and should be handed to the reviewer as things to attack: it
  is about **directory** descriptors used as pathname anchors, not about a
  call's staged payload descriptor (which the call creates and then writes,
  flushes and publishes through by descriptor); and the mount check is
  pre-body only where the boundary already existed at mint or fetch start,
  pre-publication-but-post-body inside the gate
- [ ] 5.5 End-to-end exercise against the live server, naming the tools
  actually called: `request_upload` + a real `PUT /transfer/upload` (both
  no-clobber and overwrite), `check_upload`, `import_from_url`, `create_note`,
  `edit_note(append=True)`, `move_note`, `delete_note`, `write_file`,
  `delete_file`
- [ ] 5.6 Deploy on a host whose kernel and seccomp profile allow `openat2`,
  and confirm the startup probe logs nothing on the happy path. Confirm the
  mount preflight is a no-op there: the production vault at `/obsidian` is a
  single mount throughout (measured — root and every child report the same
  `STATX_MNT_ID`), and `statx` is not blocked by the container's seccomp
  profile (measured in the running container). Both were checked before this
  change was written; re-check on the host that actually runs it
