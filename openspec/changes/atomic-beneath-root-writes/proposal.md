## Why

Three defects, one code path. All of them live in `src/services/vault_fs.py`
and the publish that runs on top of it, which is why they are one change and
why they have to be applied in a fixed order rather than fanned out.

### 1. The anchored walk is not an atomic beneath-root lookup (#87)

`vault_fs.open_dir_beneath` opens one component at a time with `O_NOFOLLOW`
from an open root descriptor. Each open is individually safe; the *sequence*
is not. Between opening ancestor `A` and opening its child `B`, another
process on the same filesystem can rename `<vault>/A` to a location outside
the vault, and the descriptor the walk goes on to return — with every
mutation anchored to it — is then outside the root. Nothing later in the call
can notice: the descriptor is valid, the writes succeed, the tool reports
success for the path the caller named.

Every caller inherits it. The transfer publish re-walks inside the gate
(`_publish_into_current_parent`), `delete_file` walks through `_open_parent`,
and since #59 every note mutation walks through `open_mutable` and
`MutableTarget.ensure_parent`. #59 closed redirection through an *ancestor* or
the *root* for everything that happens after validation; this is redirection
during validation itself, and it is the one window that survived. It is
recorded today as design note D15 of `anchored-note-writes` and as the last
bullet of CLAUDE.md's "The accepted residual, precisely" — prose this change
removes rather than leaves standing.

**The decision is `openat2(2)` with `RESOLVE_BENEATH | RESOLVE_NO_SYMLINKS |
RESOLVE_NO_MAGICLINKS`**, reached through `ctypes` exactly as
`rename_noreplace` already reaches `renameat2`. The kernel then enforces
containment for the whole path inside one call, so there is no interval
between components to race. The three flags map onto semantics the module
already has rather than adding new ones: `RESOLVE_NO_SYMLINKS` is what the
per-component `O_NOFOLLOW` was approximating, `RESOLVE_BENEATH` is what
`_split`'s refusal of `..` and of absolute paths was approximating, and
`RESOLVE_NO_MAGICLINKS` closes the `/proc` door that neither of them covered.

**On unavailability the answer is both halves, not either.** `openat2` needs
Linux 5.6, and an older Docker default seccomp profile blocks it (`EPERM`, or
`ENOSYS` where the profile's default action returns that). So: a **startup
probe** that `sys.exit(1)`s with a message naming the syscall, the kernel
version and the seccomp profile, in the shape `_check_pgvector_version`
already uses; **and** a call site that raises `UnsupportedFilesystem` if it
ever sees the errno anyway. Two layers for the same reason the ownerless-key
refusal has two: the probe is the gate, the call site is the thing a future
caller cannot bypass.

There is **no silent fallback to the per-component walk**. A containment
guard that degrades quietly is precisely the failure mode being removed — the
operator would see a working server and never learn that the property
CLAUDE.md advertises had stopped holding, and the degradation would be
invisible in every test, because tests run on a kernel that has the syscall.

### 2. Nothing on the transfer path ever fsyncs (#97)

There is no `fsync` or `fdatasync` anywhere in `src/services/transfer.py`,
`src/services/vault_fs.py` or `src/transfer/routes.py`. The note path treats
this as load-bearing — `vault._atomic_write_at` flushes the payload before
publication precisely so a crash just after the rename cannot publish a note
whose data blocks never landed — and the transfer path has the same exposure
in a worse form. An upload is the one write whose source bytes are gone
afterwards: a note the agent rewrote can be reconstructed from the
conversation, a file a human handed to a single-use capability cannot. After a
crash the directory entry can be durable while the contents are not, so the
vault holds a truncated or zero-length file at a path `check_upload` has
already reported `completed`, carrying a `sha256` the bytes no longer match.
That is a false statement made by the one surface an agent consults instead of
looking.

**The decision is payload *and* parent directory, on *both* paths.** The
payload flush goes in `stream_to_vault` immediately after `_drain` returns,
beside the `os.fchmod` #95 added, and therefore **before** `before_publish()`
— a flush of up to `MAX_FILE_WRITE_BYTES` (25 MB) must not run while the
gate's `SELECT … FOR UPDATE` locks are held. The directory flush is the half
neither path has today: `_atomic_write_at` flushes the payload and not the
directory, so a rename's durability is unguaranteed there too, and the issue
asks for that to be decided once for both rather than half-fixed twice.
`import_from_url` shares `stream_to_vault` and is covered by the same change.

**What happens when the directory flush fails is the load-bearing part.** It
runs *after* publication, and `stream_to_vault`'s contract is that
`PostPublishFailure` is the only exception it raises once the bytes are in
place — the upload route reads anything else as demonstrably pre-publication
and answers by releasing the claim, handing back a replayable token over a
path that already holds the file. So the flush runs after `on_published` has
recorded the publication and before `gate.complete()` commits, which puts it
inside the existing `state["published"]` handler that converts any exception
to `PostPublishFailure`. The token stays `claimed`, forever, and `check_upload`
answers `uploading` and then `unknown` — "the bytes may already be in the
vault, go `list_files`/`read_file` the path before re-minting". That is exactly
the honest answer for "the file is there and we cannot promise it survives a
crash", and the vocabulary for it already exists. Logging the failure and
committing `completed` was the alternative and is rejected: `completed{sha256}`
is a claim about the vault, and #97 is about not making claims the bytes do not
support.

The note path takes the opposite failure direction, deliberately — see D18.

### 3. The transfer path still publishes by staging name (#92, item 1)

Uploads stage in `<root>/.transfer-tmp/` and publish from that staging *name*.
The note path no longer does: PR #84 stages an unnamed `O_TMPFILE` inode and
publishes it by descriptor through a `/proc/self/fd/<fd>` `linkat`, so there
is nothing to observe, nothing to substitute and nothing to clean up. The
transfer path should be on the same primitive.

**Be honest about the threat model: this is not a live exploit.**
`.transfer-tmp/` is a dot-directory the indexer skips and every tool's
`is_hidden_path` guard refuses, it is held at `0700`, and `open_staging_dir`
refuses it outright when it is owned by another uid. No agent, no capability
and no vault tool can reach a staged name. The residual adversary is a process
on the host running as the same uid — which can rewrite the destination
directly and needs no race. So the *why* here is convergence on the stronger
primitive next door and the deletion of a whole class of cleanup code, not the
closing of an open hole. It earns its place in this change because it is the
same part of `vault_fs` the other two items rewrite, and doing it separately
means disturbing the publish path a third time.

Two things had to be worked out rather than assumed, and both changed the
shape of the requirement — see D19 and D20. In short: the pruning of stale
staged files stays (it has pre-change litter to collect and a rolling deploy
to survive) but stops having anything new to collect, because an unnamed inode
is reclaimed by the kernel when the last descriptor closes rather than sitting
on disk for 24 hours; and the **overwrite** publish cannot move to `O_TMPFILE`
wholly, because `renameat` has no by-descriptor form. The overwrite path
therefore materialises a name from the staged inode **inside the publish
gate**, immediately before the fingerprint check and the rename — a window of
two syscalls in a `0700` directory, instead of a name that exists for the
whole multi-minute stream.

## What Changes

- `vault_fs` gains `openat2` through `ctypes` and obtains every below-root
  directory descriptor with a single `RESOLVE_BENEATH | RESOLVE_NO_SYMLINKS |
  RESOLVE_NO_MAGICLINKS` lookup. `open_dir_beneath` keeps its name, signature
  and error vocabulary; the per-component descent stops being how the
  authoritative descriptor is produced.
- Directory *creation* keeps a per-component `mkdir` walk (`openat2` cannot
  create intermediate directories), but no descriptor that walk produces is
  ever written through: the caller's descriptor always comes from a fresh
  single beneath-root lookup performed **after** the creation completes. That
  is what covers `MutableTarget.ensure_parent`, the one deferred-creation site.
- A read-only startup probe in `lifespan`, beside `_check_pgvector_version`,
  refuses to start when `openat2` is unavailable; the call site raises
  `UnsupportedFilesystem` on the same errnos.
- `stream_to_vault` flushes the staged payload after `_drain` and before the
  publish gate, off the event loop, and flushes the destination directory
  after publication — the latter classified as post-publication, so it can
  only ever surface as `PostPublishFailure`.
- `vault._atomic_write_at` flushes the destination directory after
  publication; a failure there is logged and the write reported as the success
  it is (D18).
- Transfer staging becomes `O_TMPFILE` in `.transfer-tmp`. The no-clobber
  publish becomes the by-descriptor `linkat`; the overwrite publish
  materialises a transient staging name inside the gate. `_link_staged_inode`
  and the `/proc` availability check move from `vault.py` into `vault_fs.py`
  so both paths share one implementation rather than drifting.
- `probe_publication` additionally exercises unnamed staging and
  by-descriptor publication, so a filesystem or container that cannot do them
  is refused at the probe rather than at the first upload.
- CLAUDE.md's "The accepted residual, precisely" loses its
  non-atomic-walk bullet, and the surrounding sections stop deferring to a
  follow-up that has landed.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `file-transfer`: the beneath-root lookup primitive shared by the transfer
  publish and `delete_file`; durability of staged bytes and of the
  publication; unnamed staging and by-descriptor publication; what the
  publication probe covers.
- `vault-write`: how a note mutation's parent descriptor is obtained;
  durability of the publishing rename or link.

## Impact

- `src/services/vault_fs.py` — `openat2` binding and errno mapping,
  `open_dir_beneath`, `_open_parent`, `open_staging_dir`, unnamed staging,
  `publish`, `probe_publication`, `prune_stale_staging` commentary;
  `_link_staged_inode` and `_proc_fd_available` adopted from `vault.py`.
- `src/services/transfer.py` — `_stream_locked` (payload flush, unnamed
  staging, discard path), `_publish_into_current_parent` (directory flush
  after `on_published`).
- `src/services/vault.py` — `_atomic_write_at` (directory flush),
  `_link_staged_inode` / `_proc_fd_available` delegate to `vault_fs`.
- `src/main.py` — the `openat2` startup probe in `lifespan`.
- `tests/` — new cases for the ancestor-rename race, the unavailability
  refusals, the flush ordering and its two failure directions, and the
  staging-name absence; existing tests that hook the per-component walk or
  the staging name are updated.
- No database schema changes, no new dependencies, no configuration changes.
- Behaviour change visible to operators: a kernel older than 5.6, or a
  container seccomp profile that blocks `openat2`, now stops the server at
  startup instead of running with a weaker guarantee.

## Design notes

Numbering continues from `anchored-note-writes`, whose **D15** is the residual
this change closes.

### D16. Why `RESOLVE_BENEATH`, and why not the two nearby candidates

`RESOLVE_IN_ROOT` was rejected. It scopes `..` and absolute paths to the
directory descriptor, chroot-style, rather than refusing them — so
`a/../../b` would be silently accepted as something the caller did not write.
`_split` refuses both outright today and the module's stated posture is that
nothing is normalised on our behalf; `RESOLVE_BENEATH` matches that posture
exactly, and `_split` stays as the cheap first refusal with a message naming
the offending path.

`RESOLVE_NO_XDEV` is deliberately **not** set. It would refuse a mount point
inside the vault, which is a supported deployment (an attachments volume
bind-mounted under the vault root), and it buys nothing: containment is what
`RESOLVE_BENEATH` enforces, and a mount point beneath the root is still
beneath the root.

The userspace alternative — walk as today, then verify afterwards that the
final descriptor is beneath the root — was rejected because there is no atomic
form of that verification. Walking `..` upward from the descriptor races with
exactly the rename being defended against, and comparing inode numbers proves
where the descriptor is *now*, not where it was when an intermediate open
happened. It is check-then-act one level up, which is the class of bug #59 and
this change exist to remove.

### D17. `EXDEV` from `openat2` means containment was violated

`rename_noreplace` maps `EXDEV` to `UnsupportedFilesystem` — there it means
"these two names are on different devices". `openat2` returns `EXDEV` when the
resolution would escape the beneath-root, which is the opposite kind of event:
an attack was blocked, or a path was wrong. Mapping it to
`UnsupportedFilesystem` would tell an operator to change filesystems in
response to a containment refusal, so it maps to `UnsafePath` instead. Two
`ctypes` callers in one module with two meanings for one errno is the sort of
thing that gets "simplified" into a shared helper later; it is recorded here so
it does not.

`EAGAIN` is the other errno with no analogue in the old walk. The kernel
returns it when path resolution raced a concurrent rename and it cannot decide
the answer — userspace is expected to retry. It must not be treated as a
refusal (a legitimate write would then fail whenever anything else renamed a
directory) and it must not be retried forever (an adversary renaming in a loop
would hold the request open). Bounded retry, then refuse.

### D18. The two paths take opposite directions on a failed directory flush

On the **transfer** path a failed directory flush raises `PostPublishFailure`:
the token strands, the response is an error, and `check_upload` reports the
ambiguity. On the **note** path the same failure is logged and the write is
reported as the success it is.

The asymmetry is deliberate and the reason is retry safety. A stranded upload
capability costs a re-mint, and the human re-minting is told to look at the
path first; the source bytes are gone, so the ambiguity has to be surfaced or
it is lost. A note tool that tells an agent "the write failed" gets *retried*,
and `edit_note(append=True)` retried after a write that actually landed appends
the same block twice — a false failure on the note path manufactures a
destructive outcome, where on the transfer path it merely wastes a link. The
payload is flushed in both cases, so what is unconfirmed is only the durability
of the directory entry, and on the note path the previous content survives
either way.

### D19. `O_TMPFILE` does not retire the staging directory, and barely changes the prune

Three things about the long-lived staging that look like blockers and are not:

- **The directory is still needed.** `O_TMPFILE` takes a *directory* to choose
  the filesystem the inode is allocated on, so `.transfer-tmp` and every
  guarantee `open_staging_dir` enforces about it — existence, `0700`, owner —
  stay exactly as they are. What goes away is its *contents*.
- **The `0700` stops being the only thing protecting staged bytes**, which is
  what its docstring currently claims it is. An inode with no directory entry
  cannot be opened by name at all, so the "a peer alters the bytes between the
  digest and the publish" window closes structurally rather than by
  permissions. The mode enforcement stays: it is now defence in depth, and it
  still governs the transient overwrite name (D20).
- **The prune stays and stops having work.** `prune_stale_staging` removes
  `.transfer-tmp/.tmp-*` older than 24 hours — the litter of a crashed upload.
  An unnamed inode is freed by the kernel when the last descriptor closes,
  which happens in `_stream_locked`'s `finally` for an abandoned upload and at
  process death for a crash, so no *new* litter is produced. It must
  nonetheless stay: the live vault has pre-change files, a rolling deploy runs
  both versions at once, and the prune is the only thing that collects them.
  Removing it is a separate decision for a later release, once no pre-change
  staging file can exist.

### D20. The overwrite publish keeps a name, but only inside the gate

`renameat` has no by-descriptor form (`RENAME_EXCHANGE` does not help — it
still names the source), so an overwrite publish cannot consume an unnamed
inode. The choice is therefore not "name or no name" but *when* the name
exists.

Rejected: keep the overwrite path staging under a name for the whole stream,
as `vault._atomic_write_at` does. That is right for a note write, which
completes in one call, and wrong here: the name would exist for the entire
multi-minute body plus an unbounded wait on the gate's row locks.

Adopted: stage unnamed, and materialise a name from the staged inode *inside*
the gate, immediately before the fingerprint check and the rename, by linking
`/proc/self/fd/<fd>` into the staging directory under a fresh no-clobber name
(`EEXIST` → retry under another). The name then exists for two syscalls, in a
`0700` directory owned by this process, instead of for minutes. The identity
check `vault._require_staged_name` performs before its rename applies here
unchanged, and so does the cleanup posture: unlink the transient name only
while it still refers to our inode, otherwise leave it and log — answering a
substitution by deleting the substitute is the same destructive-write class
aimed at a different file.

The transient name goes in the **staging** directory, not beside the
destination. That is one respect in which the transfer path ends up stronger
than the note path, which stages beside the destination in a directory the
vault's own tools can write to.

So the requirement is scoped honestly: the no-clobber publish gets "no
directory entry exists at any point"; the overwrite publish gets "no directory
entry exists outside the publish gate's own rename sequence, and never outside
`.transfer-tmp`".

### D21. Why the `openat2` probe is a startup guard and not a per-root probe

`probe_publication` and `probe_trash` are lazy and cached per vault root
because they test *filesystem and mount* properties — whether hard links work
in this root, whether `.trash` is reachable by a same-device
`renameat2(RENAME_NOREPLACE)` — which genuinely differ per root in multi-user
mode, and because they **write**, which is why no read path may call them.

`openat2` availability is not that kind of property. It is a property of the
kernel and of this container's seccomp profile: one answer for the whole
process, identical for every root, knowable before a single request arrives,
and answerable with a **read-only** probe — one `openat2` of `"."` relative to
a directory descriptor the process already holds, which creates nothing. A
server that cannot enforce containment should not accept its first write, so
the probe belongs in `lifespan` next to the guards that already refuse to
start — `_check_embedding_dim`, `_check_pgvector_version` — and it is skipped
under `MCP_SANDBOX_MODE` for the same reason they are.

The call-site refusal is not redundant with it. The probe answers for the
process at startup; the call site is what a future caller — a new `vault_fs`
entry point, a test harness, a sandbox build — cannot get around.
