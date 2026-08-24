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
bullet of CLAUDE.md's "The accepted residual, precisely".

**This change closes the lookup and narrows — but does not remove — the
creation side.** The claim it is entitled to, in the words every artifact of
this change uses: **Every below-root directory descriptor a call uses as a
pathname anchor comes from a lookup the kernel proved beneath the vault root
at the moment it resolved, and no directory descriptor retained from a
creation descent is ever returned to a caller or used as a pathname anchor —
so no operation is ever redirected into a directory that was never beneath
the root.**

This is a claim about **directory** descriptors used as pathname anchors: a
call's own staged payload descriptor is created by that call, is written,
flushed and published through by descriptor, and never anchors a pathname
lookup.

Two things this change does not close, and both are stated rather than
implied. Creating a missing directory has no beneath-root form, so it keeps
a bounded residual: at most one empty directory per component **per creation
descent**, in a place the renaming process already controls — and an upload
has two such descents (D22). And a lookup proves containment *at the instant
it resolves*, not afterwards: a rename landing between the final lookup and
the publish carries the whole call into wherever it moved the directory,
because a descriptor keeps naming its directory across a rename. That second
one is inherent to descriptor anchoring — it is the property #59 relies on —
so it is *retained*, not introduced here, and it is recorded as such (D26).
That
CLAUDE.md bullet is therefore *rewritten*, not deleted.

**The decision is `openat2(2)` with `RESOLVE_BENEATH | RESOLVE_NO_SYMLINKS |
RESOLVE_NO_MAGICLINKS`**, reached through `ctypes` as `rename_noreplace`
reaches `renameat2` — but not by the same route inside it, because glibc
exports no `openat2` wrapper at all (D24). The kernel then enforces
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

**The decision is payload *and* parent directory, on *both* paths.** Parent
directory means every directory the call created, not just the immediate one:
`MutableTarget.ensure_parent` and the transfer publish's `create=True` walk
both build whole chains, and flushing only the leaf's parent leaves the entry
that names *it* unflushed — a crash then loses the folder and with it a note
`create_note` reported written or a file `check_upload` reported `completed`.
The payload flush goes in `stream_to_vault` immediately after `_drain` returns,
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

**And it cannot be unconditional, because there are real mounts that refuse
`O_TMPFILE`.** #103 reports it, verified rather than guessed: TrueNAS SCALE's
NFS server rejects the operation outright (`EOPNOTSUPP`) — as root, on a second
export, under NFSv4.1 and NFSv4.2, and still after a NAS upgrade — while the
named staging the overwrite path uses works on the same mount. The note path
already refuses there, and an accepted contributor change adds an opt-in
`VAULT_ALLOW_NAMED_STAGING_FALLBACK` for it. If this item shipped without
honouring the same flag, a vault whose *transfers work today* would gain a new
refusal from a change whose stated purpose is convergence on a stronger
primitive next door — the one deployment outcome that cannot be justified by
"this is not a live exploit". So the probe selects the mode, the flag is the
single operator decision for both write paths, and the fallback's reopened
window is declared in the same register as the overwrite window above. D27
records the decision, the threat comparison and the rejected alternative.

## What Changes

- `vault_fs` gains `openat2` through `ctypes` and obtains every below-root
  directory descriptor with a single `RESOLVE_BENEATH | RESOLVE_NO_SYMLINKS |
  RESOLVE_NO_MAGICLINKS` lookup. `open_dir_beneath` keeps its name, signature
  and error vocabulary; the per-component descent stops being how the
  authoritative descriptor is produced.
  - Directory *creation* still happens one component at a time (`openat2`
  cannot create intermediate directories), but no directory descriptor is
  carried across a creation: each `mkdir` is issued through a fresh
  beneath-root lookup of the prefix that already exists, and the caller's
  descriptor always comes from a fresh single beneath-root lookup of the
  whole parent performed **after** the creation completes. That is what
  covers `MutableTarget.ensure_parent`, the one deferred-creation site. The
  residual it leaves — at most one empty directory per component **per
  creation descent**, outside the root, never a file — is stated rather than
  claimed closed (D22).
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
- Transfer staging becomes `O_TMPFILE` in `.transfer-tmp` **wherever the
  publication probe proves it works**. The no-clobber publish becomes the
  by-descriptor `linkat`; the overwrite publish materialises a transient
  staging name inside the gate. `_link_staged_inode` and the `/proc`
  availability check move from `vault.py` into `vault_fs.py` so both paths
  share one implementation rather than drifting.
  - Where the probe shows unnamed staging unavailable, the transfer path
  refuses with an error naming `VAULT_ALLOW_NAMED_STAGING_FALLBACK`, or —
  where that flag is set — keeps today's **named** `.transfer-tmp` staging
  unchanged, with the same once-per-process warning on first exercise and the
  same `/health` field the note path's fallback uses. One flag governs both
  write paths; the probe records which mode a root uses and the mode never
  flips per call (D27).
- `probe_publication` additionally exercises unnamed staging, by-descriptor
  publication, a payload flush and a directory flush, so a filesystem or
  container that cannot do them is refused at the probe rather than after a
  body has been streamed — and its cached per-root result records the staging
  mode that root will use, which is what keeps the mode from being re-decided
  per call (D27). What the probe cannot answer for — a destination
  whose filesystem or mount differs from the root's — is stated rather than
  implied (D23).
  - Transfer publication gains a **mount-identity check**: the destination
  parent must be on the same mount as the staging directory, checked with
  `statx`'s `STATX_MNT_ID` (never `st_dev`, which a same-filesystem bind
  mount defeats) at mint or fetch start and again inside the publish gate. A
  boundary that is already there at mint or fetch start is refused **before
  any body is read, staged or published**; one established afterwards is
  caught only by the in-gate check, which is still pre-publication — nothing
  is written and the claim is released — but runs after the body may already
  have streamed in full. That is the whole of what this change does about
  nested mounts; the soft delete and the cross-boundary `move_note` are
  enumerated honestly and filed as their own issues (D23).
- CLAUDE.md's "The accepted residual, precisely" has its non-atomic-walk
  bullet **rewritten**: the lookup window is gone, the creation-side residual
  (D22) takes its place, and the surrounding sections stop deferring to a
  follow-up that has landed.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `file-transfer`: the beneath-root lookup primitive shared by the transfer
  publish and `delete_file`; durability of staged bytes and of the
  publication; unnamed staging and by-descriptor publication, and the
  flag-gated named-staging fallback where a mount cannot do them; what the
  publication probe covers and which staging mode it records; refusal of a destination on another mount —
  before any body where the boundary is already present at mint or fetch
  start, and pre-publication inside the gate where it appears afterwards.
- `vault-write`: how a note mutation's parent descriptor is obtained;
  durability of the publishing rename or link.

## Impact

- `src/services/vault_fs.py` — `openat2` binding and errno mapping,
  `open_dir_beneath`, `_open_parent`, `open_staging_dir`, unnamed staging,
  `publish`, `probe_publication`, `prune_stale_staging` commentary;
  `_link_staged_inode` and `_proc_fd_available` adopted from `vault.py`.
- `src/services/transfer.py` — `_stream_locked` (payload flush, unnamed
  staging, discard path), `_publish_into_current_parent` (directory flush
  after `on_published`, mount-identity re-check before the publish),
  `plan_mint_window`'s callers / the mint path for the same preflight.
- `src/main.py` — the `/health` field reporting whether the named-staging
  fallback has been exercised in this process (shared with the note path's
  fallback, not a second field).
- `src/config.py` — **only if this change ships before the contributor PR that
  defines it**: `Settings.vault_allow_named_staging_fallback`
  (`VAULT_ALLOW_NAMED_STAGING_FALLBACK`, default `false`), introduced under
  exactly that name and default so the two never diverge (D27).
- `src/services/vault.py` — `_atomic_write_at` (destination-directory flush,
  plus a flush of every directory `MutableTarget.ensure_parent` created,
  outward to the first pre-existing one — inherited by every caller of the
  helper, `write_file` included), `MutableTarget` (recording which directories
  it created), `_link_staged_inode` / `_proc_fd_available` delegate to
  `vault_fs`.
- `src/main.py` — the `openat2` startup probe in `lifespan`.
- `src/mcp_server/tools.py` — `request_upload` and `import_from_url` run the
  mount-identity preflight before a link is handed out or a fetch begins. The
  read-side callers are **unchanged** and enumerated only: `_fingerprint_of`,
  `_head_bytes` and `routes._open_bound_file` reach the layer through
  `open_parent` and inherit the new lookup.
- `src/transfer/routes.py` — **no change**; the download route's uniform 404 is
  deliberately left alone (D21), and the upload route's existing
  unsupported-filesystem 503 already carries a mount refusal that reaches it.
- `tests/` — new cases for the ancestor-rename race, the unavailability
  refusals, the flush ordering and its two failure directions, and the
  staging-name absence; existing tests that hook the per-component walk or
  the staging name are updated.
- No database schema changes and no new dependencies. The only configuration
  is the named-staging fallback flag, which this change **consumes** rather
  than invents — see D27 for what happens if it ships first.
- Behaviour change visible to operators: a kernel older than 5.6, or a
  container seccomp profile that blocks `openat2`, now stops the server at
  startup instead of running with a weaker guarantee.

## Design notes

Numbering continues from `anchored-note-writes`, whose **D15** is the residual
this change closes on the lookup side and narrows, without closing, on the
creation side (D22).

### D16. Why `RESOLVE_BENEATH`, and why not the two nearby candidates

`RESOLVE_IN_ROOT` was rejected. It scopes `..` and absolute paths to the
directory descriptor, chroot-style, rather than refusing them — so
`a/../../b` would be silently accepted as something the caller did not write.
`_split` refuses both outright today and the module's stated posture is that
nothing is normalised on our behalf; `RESOLVE_BENEATH` matches that posture
exactly, and `_split` stays as the cheap first refusal with a message naming
the offending path.

`RESOLVE_NO_XDEV` is deliberately **not** set, and the reason is narrower than
"nested mounts are supported". It buys nothing for containment — that is what
`RESOLVE_BENEATH` enforces, and a mount point beneath the root is still beneath
the root — while setting it would refuse *lookups* through a mount point, and
lookups are what reads, `delete_file`, the note tools and the transfer path all
share. Note *writes* stage beside their destination and so work across a
nested mount today; the transfer publish does not and never has, and neither
does a soft delete or a move that crosses the boundary — for reasons that have
nothing to do with this flag, enumerated operation by operation in D23. Setting
`RESOLVE_NO_XDEV` would break every path that works, including all the reads,
and would fix none of the three that do not.

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

Measured on a 6.8 kernel, with `RESOLVE_BENEATH | RESOLVE_NO_SYMLINKS |
RESOLVE_NO_MAGICLINKS`: a symlinked component gives `ELOOP`, a missing one
`ENOENT`, and **both `..` and an absolute path give `EXDEV`** — so `EXDEV` is
the ordinary answer for "this path leaves the root", not an exotic one. Worth
recording alongside it: `A/../A` *succeeds*, because `RESOLVE_BENEATH` scopes
`..` rather than forbidding it. That is exactly why `_split` stays in front and
keeps refusing `..` outright — the module's posture is that nothing is
normalised on our behalf, and the kernel's is that normalisation is fine as
long as it stays beneath.

`EAGAIN` is the other errno with no analogue in the old walk. The kernel
returns it when path resolution raced a concurrent rename and it cannot decide
the answer — userspace is expected to retry. It must not be treated as a
refusal (a legitimate write would then fail whenever anything else renamed a
directory) and it must not be retried forever (an adversary renaming in a loop
would hold the request open). Bounded retry, then refuse. **`EINTR` joins it in
that class for a different reason**: the walk being replaced went through
`os.open`, which retries `EINTR` transparently under PEP 475, and a raw
`ctypes` syscall does not — so without an explicit retry a signal delivered
without `SA_RESTART` would turn into a false failure of `create_note`,
`delete_file`, a transfer or a download. Same bounded-retry shape, different
meaning: `EAGAIN` is a contested path, `EINTR` is nothing at all.

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
  process death for a crash, so no *new* litter is produced **in the unnamed
  mode**. The named-staging fallback (D27) produces it exactly as the
  pre-change path did, which is a second reason the prune stays. It must
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
`.transfer-tmp`". Both are scoped further by D27 — they describe the mode the
probe selects where `O_TMPFILE` works, and the flagged fallback is the one
declared departure from them.

And the refusal is scoped honestly too. The identity check **narrows** the
substitution window to the single `renameat`; it does not close it, exactly as
`vault._require_staged_name` does not. A substitution observable before the
check is refused; one landing between the check and the rename can still be
published. That is an accepted residual, not a gap in the implementation: an
actor who can create a name in a `0700` directory owned by this process can
also edit the destination file directly, which is the same reasoning that puts
the note path's overwrite window outside the threat model. The spec says
"refused" only for the interval where refusal is achievable, and records the
other interval as a residual — a scenario promising detection across an
interval the design admits it cannot cover is a requirement no implementation
can pass.

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

The call-site refusal is not redundant with it, and the sandbox skip is
precisely why. The probe answers for the process at startup; the call site is
what a future caller — a new `vault_fs` entry point, a test harness, a sandbox
build — cannot get around. `MCP_SANDBOX_MODE` is therefore the one
configuration in which a call site can be reached with the syscall
unavailable, and what each surface answers there is its existing contract, not
a new one: a tool returns the unsupported-filesystem error, `PUT
/transfer/upload` returns its 503, and the download route returns the **uniform
404** it returns for every other refusal. That last one is deliberate and must
not be "fixed" into a distinguishable error: `/transfer/*`'s read side answering
one status for unknown, expired, consumed, deleted and replaced is what keeps
it from being an oracle, and an "unsupported filesystem" 503 there would report
a server property to an unauthenticated bearer. Precision on that path comes
from `check_upload` and the mint tools, which are authenticated.

### D22. The creation descent cannot be made beneath-root, and what that leaves

`openat2` resolves; it does not create. `mkdirat` has no `RESOLVE_BENEATH`
form, and there is no syscall that creates a directory *and* proves the path it
created it under stayed beneath a root. So a missing `A/B/C` cannot be
conjured atomically, and the honest question is not whether a residual remains
but how small it can be made and what it can cost.

Made small: no directory descriptor is carried across a creation. For each
missing component the prefix that already exists is re-acquired with a fresh
`openat2` from the root, the single `mkdirat` is issued through *that*
descriptor, and it is dropped. The window is then one syscall per component
rather than the whole descent, and — the part that matters — the descriptor
the caller finally receives comes from a fresh beneath-root lookup of the
complete parent, so no directory descriptor the creation produced is ever
returned to a caller or used as a pathname anchor. Re-acquiring is cheap:
paths are two or three components deep and the lookups are
`O_RDONLY|O_DIRECTORY` opens.

What it can cost: if a prefix is renamed out of the vault in that one-syscall
window, an **empty directory** is created outside the root. No file, no file
content, no note, and nothing the tool then reports success about — the write
goes through the post-creation lookup, which either resolves beneath the root
or refuses. The process that wins the race must already hold rename rights on
the prefix's parent *and* write access wherever it moved it, so the directory
lands somewhere it already controls. Detecting the escape afterwards is
possible (the post-creation lookup either resolves to the inode we created or
does not) but removing it is not, and *cleaning up* is worse than leaving it:
an `rmdir` by name, of a name the caller chose, is the same
delete-the-substitute hazard `_discard_temp` and `soft_delete` already refuse.

**The bound is per creation descent, and one call can have more than one.** An
upload walks its destination twice with creation enabled: a cheap up-front walk
in `_stream_locked`, so that a `..`, a symlinked ancestor or a non-directory
costs one syscall rather than a whole body, and the authoritative walk inside
the publish gate. A coordinated race can therefore leave one escaped empty
directory for each. A note write performs one descent.

Collapsing the up-front walk to a non-creating one would halve that, and was
rejected. The `create=True` up front is also what makes a `mkdir` that
cannot succeed — a read-only mount, a permission, a full disk — fail before
25 MB has been streamed rather than after, which is the same "refuse before
any body moves" principle D23's mint-time check is built on. Trading it for
one fewer empty directory, in a location the winner of the race already
controls, is a bad trade. The honest answer is to state the bound per
descent, which is what the requirement now does.

Which is why D15 is recorded as narrowed rather than closed. The claim this
change is entitled to make is: **Every below-root directory descriptor a
call uses as a pathname anchor comes from a lookup the kernel proved beneath
the vault root at the moment it resolved, and no directory descriptor
retained from a creation descent is ever returned to a caller or used as a
pathname anchor — so no operation is ever redirected into a directory that
was never beneath the root.**

This is a claim about **directory** descriptors used as pathname anchors: a
call's own staged payload descriptor is created by that call, is written,
flushed and published through by descriptor, and never anchors a pathname
lookup.

It is *not* entitled to "nothing outside the root is ever created"
(D22's empty directory), and it is *not* entitled to an unqualified "nothing
outside the root is ever written" (D26's post-lookup rename interval).

### D23. Nested mounts: what works, what does not, and what is refused early

D16 declines `RESOLVE_NO_XDEV`, which keeps *lookups* working through a mount
point beneath the vault root. That is worth keeping and it is exactly all it
means. It does not follow that operations *across* such a boundary work, and
the first draft of this note asserted that deletes did. They do not.

Measured on the deployment kernel (6.8.0-138), with an ext4 directory
bind-mounted at `<vault>/M` — the same filesystem, so `st_dev` is identical on
both sides (66306):

| Operation | Across / on a nested mount | Why |
| --- | --- | --- |
| Lookup and read | works | `O_NOFOLLOW` constrains symlinks, not mount traversal, and `RESOLVE_BENEATH` treats a mount point beneath the root as beneath the root |
| Note write on the mount (`create_note`, `edit_note`, `set_frontmatter`, `write_file`) | works | `vault._atomic_write_at` stages in the **destination's own** directory and publishes with a same-directory `linkat`/`renameat` |
| Permanent unlink (`delete_note`/`delete_file` with `permanent=True`) | works | `unlinkat` through the parent descriptor crosses nothing |
| `move_note` **within** the mount | works | both parent descriptors are on the same mount |
| Soft delete (`delete_note`, `delete_file(permanent=False)`) | **`EXDEV`** | `.trash` is opened beneath the *root* descriptor and the move is one `renameat2` into it |
| `move_note` **across** the boundary | **`EXDEV`** | same `rename_noreplace`, two mounts |
| Transfer publication into the mount (upload and `import_from_url`) | **`EXDEV`** | staging is a root-level `.transfer-tmp`, publication is `link`/`replace` out of it |

Every row was executed, not reasoned about, in a mount namespace with that bind
mount in place.

Two of the failures have probes that cannot see them. `probe_trash` creates its
temp file at the root and renames it into the root's `.trash` — root→root, so
it passes on a vault whose every soft delete would fail. `probe_publication`
links root→root for the same reason.

**The current transfer error differs by publish mode, and neither is good.**
No-clobber goes through `_link_no_clobber`, which maps `EXDEV` to
`UnsupportedFilesystem` reading "the vault filesystem does not support hard
links" — a false statement about a filesystem that supports them perfectly
well, just not across that boundary. Overwrite (with a fingerprint) goes
through a bare `os.replace`, whose `EXDEV` has no mapping at all: it propagates
as a plain `OSError`, `_stream_locked` classifies it as pre-publication —
correctly, nothing was published — and the upload route's generic
`except Exception` releases the claim and re-raises, so the person gets a
server error rather than the 503 the other mode produces.

None of this is introduced here. The staging directory, the link publish and
the root-level probes all predate this change, and moving staging to
`O_TMPFILE` neither introduces the problem nor worsens it: an unnamed inode
allocated in `.transfer-tmp` lives on exactly the filesystem — and the mount —
a named one did. It is recorded here because this change is what says "the
lookup supports nested mounts" out loud, and that sentence sitting next to an
unqualified publication requirement would read as a promise the code does not
keep.

**In scope: transfer publication refuses a destination on another mount — at
mint (`request_upload`, or the start of `import_from_url`) and again inside the
publish gate.** The two halves refuse at different moments and the difference
is worth stating rather than rounding off. A boundary that already exists at
mint or fetch start costs nothing: no body is read, staged or published, and no
link is handed to a person that could only ever fail at redemption. A mount
established *after* the mint can only be caught by the in-gate check, which
runs once the body has streamed — before the link or rename, so nothing is
published and the claim is released to `pending`, but the person has already
sent the bytes. That is the residual price of a check that cannot precede a
boundary that does not yet exist, and it still covers the one operation whose
failure is both late and expensive: by the time a publish fails on `EXDEV`, the
human's copy of the bytes is the only one left and it has already been sent.

**The check compares mount identity, not `st_dev`, and that distinction is
the whole point.** A bind mount of an ext4 directory beneath the vault root has
the *same* `st_dev` as `.transfer-tmp`, so an `st_dev` comparison passes and
`linkat` still returns `EXDEV` after the body has streamed — which is what the
first draft of this note proposed. Measured: `.transfer-tmp` and a
bind-mounted `<vault>/M` both reported `st_dev` 66306 while `statx`'s
`STATX_MNT_ID` reported 653 and 6036, and both `link` and `rename` across the
boundary returned `EXDEV`.

`STATX_MNT_ID` is Linux **5.8** — above `openat2`'s 5.6, so this change's
kernel floor becomes 5.8 rather than 5.6 — and unlike `openat2` it is reachable
through the ordinary glibc wrapper: checked inside the running container, glibc
exposes `statx` and does not expose `openat2`, and the returned `stx_mask`
carries the mount-id bit. `STATX_MNT_ID_UNIQUE` (Linux 6.8) exists and is
deliberately *not* required. A mount id can be reused once its mount is gone,
which would matter if one were recorded at mint and compared at publish — so
neither is. Each of the two checks reads **both** sides and compares them
within the same call; nothing is persisted, and reuse cannot mislead a
comparison of two ids read microseconds apart. A kernel or container that
cannot report a mount id refuses the publication rather than falling back to
`st_dev` or letting the errno decide after the body has moved, for the same
reason there is no fallback to a per-component walk.

Where the destination parent does not exist yet — the ordinary case for an
upload into a new folder — the check runs against the deepest existing ancestor,
because a directory created beneath it is created on that ancestor's mount. A
mount established beneath the root *after* the mint is what the in-gate check
is for.

**Out of scope, filed as their own issues: the soft delete and the
cross-boundary `move_note`.** Both are real, both predate this change, and both
have none of the transfer's urgency — they fail *before* anything is written,
with a message that at least names `.trash` and the rename, and the operator
has `permanent=True` or a two-step move to fall back on. Fixing them means a
per-mount `.trash`, or a copy-and-unlink fallback — and a copy-and-unlink soft
delete is precisely the `link`+`unlink` shape `soft_delete`'s docstring exists
to refuse. That is a decision of its own. Filed with them: making the
destination-mount errno mapping consistent, so an `EXDEV` that reaches the
publish anyway names the mount boundary in both modes rather than blaming hard
links in one and escaping as a bare `OSError` in the other.

**Deliberately not done: nested mounts are not declared unsupported wholesale,
and there is no boot-refusing probe for them.** Most of the vault works across
one — every read, every note write, every permanent delete, every move that
stays on one side — and refusing to start would remove working functionality to
prevent three named failures, two of which are already clean refusals.

### D24. glibc has no `openat2` wrapper, so the raw syscall is the normal path

`_resolve_renameat2` prefers the glibc symbol and keeps a `syscall()` number
table for glibc < 2.28 only — every branch of it marked `pragma: no cover`,
because the deployment image has glibc 2.36 and always takes the wrapper.
Copying that shape for `openat2` would be wrong in a way that hides itself:
**glibc exports no `openat2` wrapper at any version.** Checked on glibc 2.39,
where `renameat2`, `statx`, `close_range` and `getrandom` all resolve through
`ctypes.CDLL(None)` and `openat2` raises `AttributeError`.

So for `openat2` the raw `syscall()` path is not a fallback, it is the
implementation, and everything the `renameat2` table treats as unreachable
becomes load-bearing: the per-architecture number must be right, an
architecture missing from the table means "no `openat2`" (which now means the
server refuses to start, not that a rarely-taken branch is skipped), and the
branch needs real test coverage rather than a `pragma`. The number itself is
the same everywhere — `__NR_openat2 == 437` on x86_64 and on the asm-generic
table `aarch64` uses — because the syscall postdates the unified-numbering
convention; that uniformity is a convenience, not a licence to guess for an
architecture that is not listed.

The errno contract needs the same care, and two of its members were wrong in
the first draft. Measured against a 6.8 kernel: a `size` smaller than any
version the kernel knows gives **`EINVAL`**, not `E2BIG`; `E2BIG` is what a
*larger* structure with nonzero bytes past the kernel's known size gives (a
larger structure that is zero-padded simply succeeds); and an unrecognised
`resolve` bit also gives `EINVAL`. Neither can happen from a correct binding
that passes `sizeof(struct open_how)` with known flags — which is exactly why
both must map to a refusal that names the ABI mismatch rather than to a
generic `OSError`: they are what a binding bug looks like, and a containment
lookup that never ran must never be mistaken for one that passed.

### D25. What an `ELOOP` can honestly name

The walk being replaced opened one component at a time, so when a component was
a symlink it knew *which* one and said so: "Refusing to traverse a symlink or
non-directory at `'B'` in `'A/B/note.md'`". One `openat2` gives up that
information — the kernel reports `ELOOP` for the resolution, not for a
component — and a diagnostic walk issued afterwards is not a substitute: by
then the link may be gone, or a different component may have become one, so it
can report no link at all or the wrong one, authoritatively, about a state the
kernel never saw.

The requirement therefore asks for the **requested vault-relative path** and
permits component identification only as explicitly best-effort. Nothing
load-bearing is lost. CLAUDE.md's promise that a refused symlink is named with
its canonical vault-relative target is about the **leaf** — the `os.lstat` of
the final component that `open_mutable` performs through the parent
descriptor, so the agent can operate on the real note — and that check is not
this one, is not a path resolution, and is untouched here.

### D26. A lookup proves containment when it resolves, not afterwards

`openat2` with `RESOLVE_BENEATH` proves that the path it resolved stayed
beneath the root *during that resolution*. It says nothing about the future,
and it cannot: the descriptor it returns keeps naming the same directory
however that directory's pathname is later renamed. Linux preserves open
descriptors across rename, and this design **depends** on that — it is the
entire reason #59 anchors to a descriptor, so that a mutation lands in the
directory that was validated rather than in a substitute left at its name.

The consequence has to be said out loud rather than left implied by an
unqualified "nothing lands outside the root". Between the final lookup and the
publish there is an interval — the transfer gate's destination walk to its
`linkat`/`renameat`, a note tool's `open_mutable` to its publish — in which a
process that can rename a vault ancestor can move the resolved directory out of
the vault. The call then publishes into it, wherever it now is, and reports
success. Nothing was *redirected*: the bytes went to the directory the caller
named, which somebody else moved.

So the first draft's claim — "no file is ever created, read or modified outside
the root" — was false, and narrowing it is the fix rather than redesigning.
Excluding renames through publication would need something the kernel does not
offer: there is no "publish only if this descriptor is still beneath that root"
operation, and re-verifying by walking `..` upward from the descriptor is the
check-then-act one level up that D16 already rejects for the same reason. The
adversary it would defend against must already hold rename rights on a vault
ancestor, at which point the vault's contents are theirs to move regardless —
the same boundary that puts D20's overwrite window outside the threat model.

What the requirements therefore say is what a lookup actually proves, in the
same words everywhere: **Every below-root directory descriptor a call uses
as a pathname anchor comes from a lookup the kernel proved beneath the vault
root at the moment it resolved, and no directory descriptor retained from a
creation descent is ever returned to a caller or used as a pathname anchor —
so no operation is ever redirected into a directory that was never beneath
the root.** And they record the post-lookup rename as a retained residual
beside D20's and D22's. It is retained, not introduced: the per-component
walk this change replaces had this interval too, underneath the larger
window it did close.

### D27. `O_TMPFILE` is not universal, so the transfer path honours the note path's fallback flag

D19 and D20 assume every deployment can allocate an unnamed inode. #103 shows
one that cannot, with the verification done rather than asserted: on TrueNAS
SCALE 25.10.5 and 25.10.6, over NFSv4.1 and NFSv4.2, `O_TMPFILE` returns
`EOPNOTSUPP` — as root, on a second unrelated export, and on a client kernel
whose local ext4 does it fine. It is the NFS server, it is standing behaviour,
and named staging with a `link()` publish works on the same mount. So the note
path's no-clobber writes (`create_note`, `write_file` without `overwrite`)
already refuse there today, and an accepted contributor change adds an opt-in
`VAULT_ALLOW_NAMED_STAGING_FALLBACK` (env, default `false`) that lets an
operator take named staging back on that path.

**Item 3 honours the same flag.** Where the publication probe proves unnamed
staging and by-descriptor publication work on a root, transfers use them —
that is the whole of item 3 and it is unchanged. Where the probe shows them
unavailable and the flag is on, transfers keep today's named `.transfer-tmp`
staging — the same exclusive, non-symlink-following `.tmp-*` creation through
the staging descriptor, the same `link()` no-clobber publish, the same
replacing rename for an overwrite — with two guards the pre-change path does
not have. Today's transfer publish runs no staged-name identity check and its
`finally` unlinks the staging name unconditionally; item 3 introduces the
guarded form for the transient overwrite name, and the fallback inherits it
rather than reverting past it: verify the name still refers to this call's
inode immediately before the publish, and unlink it only while it still does,
otherwise leave it and log. A name that lives for minutes needs those more
than one that lives for two syscalls, and unlinking a substitute is the
destructive write every other cleanup here refuses. Where the flag is off, the
refusal is `UnsupportedFilesystem` and it *names the flag*, which is the note
path's refusal shape. The probe is what selects the mode, once,
cached per root, so the mode cannot flip between two uploads on one vault —
otherwise the window each upload ran in would be unknowable after the fact.

**The rejected alternative is hard-refusing on such a mount.** It is the
stronger primitive, and the argument for it is real: the fallback reopens the
very window `O_TMPFILE` staging exists to close. It was rejected because of
what it costs and what it buys. It costs a deployment that has *working
transfers today* — this item's own "Why" says out loud that the staging-name
window is not a live exploit, that `.transfer-tmp` is unreachable by any agent,
capability or vault tool, and that the residual adversary is a same-uid process
that can rewrite the destination directly. Converting a working deployment into
a refusal, in a change whose stated purpose is convergence on a stronger
primitive next door, to defend a window the change itself describes as not
exploitable, is a trade nobody would make deliberately. And it buys nothing an
operator is not entitled to decide: the window is narrower than one this design
already hands the operator on the overwrite path (D20), behind an explicit,
default-off switch, announced once per process the first time it is actually
taken, and visible in `/health`.

**The two fallbacks are not equal, and saying so is part of the honesty.** The
note path stages beside the destination, in an ordinary vault directory the
vault's own tools can write to. The transfer path stages in `.transfer-tmp` —
`0700`, owner-checked, dot-prefixed, skipped by the indexer and refused by
every tool's hidden-path guard. Reaching a transfer staging name therefore
requires a process running as this uid, which needs no race to rewrite the
destination anyway; reaching a note staging name requires only the destination
directory, which is the boundary #59 already places outside its threat model
for the overwrite path. The transfer fallback's window is the narrower of the
two. It is still a window, it is still declared, and the requirement scopes
"no directory entry at any point" to the unnamed mode rather than pretending
the fallback satisfies it.

**One flag, not two.** The failure is one filesystem property met on two paths
for one reason, so it gets one operator decision. Two knobs would permit a
deployment with a working `create_note` and a refusing upload — a state nobody
chose, and one that cannot be diagnosed from either symptom on its own. The
flag's *definition* belongs to the note path's contributor change: the
`Settings` field, the environment variable, the default. This change consumes
it. **Ordering, because the two are in flight at once:** if the contributor PR
lands first, group 3 reads the existing field and adds nothing to `config.py`;
if group 3 lands first, it introduces the field under *exactly* that name and
default and the contributor PR rebases onto it. Either way there is one
setting, and the coordination happens on #103 rather than in a merge. The
`/health` field is shared for the same reason, and reports the fallback as
active only once a call has actually staged under a name — the same
first-exercise rule, so it distinguishes an operator who enabled the flag
defensively from a mount that is taking the path.

One consequence for D19: the sweep of `.transfer-tmp/.tmp-*` older than 24
hours stops having new work only in the unnamed mode. In fallback mode an
abandoned or killed upload leaves a staged file exactly as the pre-change path
did, so the prune keeps a live purpose beyond collecting pre-change litter.
