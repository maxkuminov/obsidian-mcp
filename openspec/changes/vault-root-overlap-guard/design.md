## Context

Production runs `multi_user_mode=True` with two users, each holding a
`users.vault_path` under `/vaults/`. That column is the whole of tenancy: the
admission gate (`_vault_root` → `_tracked`) decides whether a credential may
call any tool at all, the indexer scans beneath it, `openat2(RESOLVE_BENEATH)`
confines every path lookup to it, and `transfer_tokens.vault_root` pins it at
mint. Two roots that name overlapping directories therefore do not produce a
*partial* leak — they make two tenants one tenant, in both directions, for every
tool the server has.

The only ongoing assignment path is admin-only, and the same admin can already
read any vault by reassigning it (README troubleshooting). This change is not
about an untrusted actor; it is about removing a false assurance — the panel
already claims to check collisions — and about making a misconfiguration that
*persists silently* fail loudly and closed instead.

### Where a vault root is assigned

| # | Site | Notes |
| --- | --- | --- |
| A1 | `src/control_panel/users.py::edit_user_submit` (~341-434) | The only ongoing path. Validated by `validate_vault_root_path`, checked by `_check_vault_path_unique`, written inside the `_lock_admin_guard` transaction. **This is where check 1 goes.** |
| A2 | `src/auth/routes.py::register_submit` (~229-300) | Bootstrap of the first admin, under `_BOOTSTRAP_LOCK_KEY`, guarded by `_users_table_empty`. Runs only when `users` holds **zero rows**, so there is no other assignment to overlap with and the check would be vacuous. Deliberately not added — a check that can never fire invites a future reader to believe this path is covered when the invariant, not the code, is what covers it. A comment says so at the site. |
| A3 | `src/control_panel/users.py::create_user` | Always writes `vault_path=None`. Nothing to check. |
| A4 | `settings.vault_path` (env `VAULT_PATH`, default `/obsidian`) | The single-user root, and the one non-`/vaults/` value `validate_vault_root_path` admits in multi-user mode (the legacy mount). Two users both assigned it is already caught by string equality. |
| A5 | Direct `UPDATE users SET vault_path = …` in psql | Outside the application entirely. This is one of the two reasons enforcement point 2 exists. |

### Where a vault root is read

| # | Site | Notes |
| --- | --- | --- |
| R1 | `src/services/vault.py::_vault_root` | The admission gate. Pure cache lookup by contract. |
| R2 | `src/services/vault.py::warm_user_vault_cache` (single and bulk forms) | Populates `_user_vault_cache`; the single form's return value is authoritative and evicts. |
| R3 | `src/mcp_server/auth.py::APIKeyMiddleware` (~245 API-key branch, ~401 OAuth branch) | Binds `current_vault_root` for the request. |
| R4 | `src/mcp_server/tools.py::_vault_admission_error` and `_vault_context` (~4129) | The gate's caller, and every disk-touching tool body. |
| R5 | `src/services/indexer.py` — `index_vault` (~1443), `link_backfill_pass` (~2283), `embed_vault` (~2475), `rebuild_tsvectors` (~2859) | Each resolves `_vault_root(user_id)` then `pinned_root(vault)`. |
| R6 | `src/services/indexer.py::observe_root_facts` / `classify_provenance` / `_recheck_assignment` (~1036) | Provenance: `canonical_vault_root`, `realpath`, `read_dir_handle`. |
| R7 | `src/services/vault.py::confirmed_publication` / `_confirm_vault_assignment` | Re-reads `users.vault_path` + `is_active` before each publication (#88). |
| R8 | `src/control_panel/routes.py::vault_page` | The panel vault browser; uses the `Path \| None` the warm returned. |
| R9 | `src/services/transfer.py` (~876, ~935 locked gate, ~1107) and `src/transfer/routes.py::_path_ok` / `open_root` | Compares `transfer_tokens.vault_root` against the owner's current assignment. |
| R10 | `src/control_panel/users.py::_list_available_vaults` | Scans `/vaults/*` for the dropdown — candidate roots, not assignments. |
| R11 | `src/services/indexer.py::_active_user_ids` | The pass's user list; bulk-warms the cache. |

### Where a pass over a vault root is *started*

This list is separate from R5 because a check installed in one loop is not
installed in the others, and every one of these reaches `index_vault` /
`embed_vault` / `rebuild_tsvectors` on its own:

| # | Entry point | Reaches |
| --- | --- | --- |
| E1 | `src/main.py::lifespan` | Creates the indexer task; the process's first opportunity to know anything. |
| E2 | `src/services/indexer.py::run_indexer_loop` — startup block | index → link backfill → embed, per user. |
| E3 | `src/services/indexer.py::run_indexer_loop` — periodic tick | `_index_pass_once` per user, or the single-user pass. |
| E4 | `src/control_panel/routes.py::_reindex_background` | **Reindex Now**, and the tails of *re-embed* and *reset embeddings*. Mirrors `run_indexer_loop` and shares `index_pass_lock` — and shares nothing else. |
| E5 | `scripts/rebuild_tsvectors.py` (`make rebuild-tsvectors`) | A **separate process** (`docker compose run --rm`), with its own `_active_user_ids()` loop, no indexer loop, and no lifespan. |

E4 and E5 are why detection cannot live inside `run_indexer_loop`.

## Goals / Non-Goals

**Goals.** Refuse an overlapping assignment at the moment it is made; detect an
overlap that appears *after* an assignment and fail the affected pair closed
before any pass reads a byte, at every entry point; be closed rather than
permissive before the first detection has completed; tell the operator,
durably and precisely, which accounts are affected and why.

**Non-Goals.** No general filesystem-layout policing (a vault that spans mounts
is fine for one tenant and stays fine); no `RESOLVE_NO_XDEV`; no migration; no
persisted quarantine; no attempt to *repair* an index that a previous
overlapping configuration already polluted beyond what the ordinary prune and
`classify_provenance` already do; no change to single-user mode.

## The three checks, and what each proves

Checks 1 and 2 are taken from **one opened directory descriptor per root**, in
one moment, the way `observe_root_facts` already binds a realpath to an
`fstat` — never from the `vault_path` string alone. Check 3 is best-effort and
reads the process's mount table.

| Check | Proves | Does **not** prove |
| --- | --- | --- |
| **1. Identity.** `os.fstat(fd_a)[st_dev, st_ino] == os.fstat(fd_b)[st_dev, st_ino]`, each `fd` from `os.open(root, O_RDONLY \| O_DIRECTORY \| O_CLOEXEC)`. | The two assignments name **one directory object** at this instant: same superblock, same inode. Every entry reachable under one is the same entry under the other. Catches a symlink alias, a same-filesystem bind mount of one directory to two pathnames, and a directory hard link where the kernel permits one. | **Nothing about containment.** Two distinct inodes nest all the time — that is check 2's job, and identity is blind to it. **Equal `st_dev` is not proof of one mount**: a bind mount of a subtree of the same filesystem reports the same `st_dev` while being a different mount, which is exactly why transfer publication uses `statx`'s `STATX_MNT_ID` and `_check_mount_identity_support` warns rather than comparing `st_dev` (`src/main.py:167`). **Unequal `st_dev` is not proof of unrelated directories**: a separate filesystem mounted *inside* another tenant's root gives `/vaults/a` and `/vaults/a/sub` different devices and total overlap. Different `st_dev` says "a `rename(2)` between these two would fail `EXDEV`" — it says nothing about who can read whom. |
| **2. Containment.** Component-wise prefix test over `os.path.realpath(root)` in **both** directions (`PurePosixPath.is_relative_to` / a `parts` prefix), never over the raw string. | One root's **canonical pathname lies inside** the other's, right now, in this container's namespace. `realpath` resolves every component, so an ancestor reached through a symlink (`/vaults/b -> /vaults/a/inner`) canonicalises to the containing form and is caught. Complement to check 1: identity answers "same object", containment answers "one inside the other", and neither implies the other. | It is a **pathname** fact about this mount namespace at this instant. It cannot see a future mount or symlink (which is why the checks re-run at every pass, not only at assignment). And it cannot see an overlap that neither canonical name expresses — which is check 3's job. Component-wise comparison is load-bearing: a raw string prefix test reports `/vaults/team` as an ancestor of `/vaults/team-2` and refuses an assignment that overlaps nothing. |
| **3. Mount grafting.** Parse `/proc/self/mountinfo`. For every entry whose **mount point** is strictly inside `realpath(A)`, treat it as an overlap of A with B when the entry's `major:minor` equals B's root device **and** the entry's **mount root** (the path *within that filesystem*) is equal to, or an ancestor of, or a descendant of, B's own filesystem-relative root path. B's filesystem-relative root is computed from B's own longest-matching mountinfo entry: `entry.root` joined with `realpath(B)` relative to `entry.mount_point`. | The kernel has **grafted B's directory (or a part of it, or a container of it) into A's tree** at a path that neither canonical name expresses — `mount --bind /vaults/b /vaults/a/inner`, which checks 1 and 2 both miss because the two root inodes stay distinct and both realpaths stay outside each other. This is the case that makes an unhandled residual a *permanent* cross-tenant destructive-write hole rather than a window. | It is the mount table of **this** process's namespace: a mount visible only in another namespace is invisible here, and by the same token cannot be traversed by this process either. It does not model **shadowing** — an entry later overmounted at the same mount point is still listed, so a stale entry can produce a refusing verdict for a graft that is no longer reachable. It is **Linux-and-`/proc`-only**: where `/proc/self/mountinfo` cannot be read or parsed, the check is skipped, logged **once**, and the reduced coverage is stated on the health page. |

**Check 3's failure posture is the `read_dir_handle` posture, verbatim:
best-effort, in the refusing direction only.** It can *add* an overlap; it can
never clear one; and its absence is not a degraded mode, not a quarantine and
not a startup refusal. That is what makes it safe to ship a check that some
kernels and some sandboxes cannot answer.

**Its availability is its own state, and must not be folded into
`vault_fs.mount_identity_available()`.** That flag records whether `statx`
reports `STATX_MNT_ID`, which is Linux **5.8**; reading and parsing
`/proc/self/mountinfo` needs neither that syscall extension nor that kernel.
The two are independent in both directions — a 5.6/5.7 kernel (this server's
`openat2` floor) has mountinfo and no `STATX_MNT_ID`, and a `/proc`-less or
hardened sandbox can have `STATX_MNT_ID` and no mountinfo. Reusing the flag
would therefore disable check 3 on kernels that can perform it, and — worse in
the other direction — would let a mountinfo failure change what `/health`
reports as `transfer_mount_check_available`, i.e. make a tenancy check's
coverage look like a transfer-write outage. So `vault_overlap` keeps
`_mountinfo_available: bool | None`, probes it on first use, logs once, and
leaves `transfer_mount_check_available`'s semantics exactly as they are. Check 3
is attempted independently of the transfer probe's verdict.

Equality of the two normalised assignment strings — today's whole check — is the
degenerate case of checks 1 and 2. The predicate therefore takes the two
**canonical assignment strings** alongside the descriptors, so that when a
collision is found the caller can still select the exact-duplicate wording
operators already know instead of describing an equal pair as a containment.

## Enforcement point 1 — assignment time

In `edit_user_submit`, after `_validate_vault_path` returns a normalised path
and while the `_lock_admin_guard` advisory lock is still held, in the same
transaction that will write `users.vault_path`:

0. **Only when the edit's *resulting* state is active and assigned.** The
   handler has already computed `new_active` and the normalised path by this
   point, so the condition is `normalized and new_active`. An edit whose result
   is inactive, or whose result clears the assignment, can create no overlap —
   the detector and the peer query both scope to active users holding an
   assignment — and refusing it would trap the operator in the one state they
   most need to leave: the panel's own remedy for a quarantined account is to
   deactivate or unassign it, and a guard that refuses *that* because the
   account still overlaps is a guard with no exit. Reactivating or reassigning
   runs the full check, which is where it belongs.
1. `SELECT id, username, vault_path FROM users WHERE is_active AND vault_path IS NOT NULL AND id != :target`.
2. Open the candidate root and each peer root; run checks 1, 2 and 3 for each pair.
3. First conflict wins; refuse with `_back_with_error`, naming the other user and the relation found ("… is inside the vault of user 'bob'", "… is the same directory as the vault of user 'bob'", "… has user 'bob''s vault mounted inside it"). Admin-facing, so naming is correct — the existing message already names the other user. An equal pair keeps today's wording.

The lock is the existing `_ADMIN_GUARD_LOCK_KEY`; **no second key is
introduced**, because `panel-user-administration` already pins "one lock key for
both handlers". Without the lock this is check-then-act: two admins assigning
`/vaults/team` and `/vaults/team/private` to two different users at the same
moment each read the other's *previous* row and both pass.

**A peer root that cannot be opened refuses the assignment**, naming that root
and saying the overlap could not be ruled out — not reporting an overlap that
was not observed. Identity is the check that catches precisely what string
equality already misses, and admitting on "we could not look" is the direction
this codebase treats as the expensive error. The precedent is in the same
function: `validate_vault_root_path` already refuses the *candidate* for exactly
the missing-mount case. Filesystem work under the advisory lock is bounded by
the number of active assigned users and by the same mounts that function already
stats.

## Enforcement point 2 — one detection, called from every entry point

### One orchestration

A single `detect_and_publish()` in a new `src/services/vault_overlap.py` is the
only thing that computes a snapshot, and **every** entry point calls it before
any pass reads a byte:

| Entry point | Call site |
| --- | --- |
| E1 lifespan | **Synchronously, before the app serves** — before the indexer task is created and before `yield`. |
| E2 startup pass | At the top of the startup block. |
| E3 periodic tick | At the top of each iteration, **before** the `_is_paused()` check. |
| E4 `_reindex_background` | At the top, before `index_pass_lock` is taken — Reindex Now, re-embed and reset-embeddings all land here. |
| E5 `scripts/rebuild_tsvectors.py` | At the top of the script's own `_active_user_ids()` loop. It is a separate process with no lifespan and no loop, so it publishes its own snapshot and consumes it. |

The rule the specs pin is not "call it in these five places" but "no code path
may begin a pass over a vault root without a snapshot published in this process
by this orchestration". A sixth entry point added later inherits the guard by
routing through the same helper, which is why the per-user stage skip lives in
the shared pass helpers rather than in each loop.

### One detection at a time, and a publication that cannot go backwards

Every entry point calls the detection *before* taking `index_pass_lock`, which
is correct — the check must not wait behind a pass it exists to gate — and it
means two detections can be in flight at once: a periodic tick (E3) and a
panel-triggered reindex (E4) overlap trivially, and E4 fires three times over
from Reindex Now, re-embed and reset-embeddings. The failure that buys is not
theoretical and it fails **open**: a detection that started before an overlap
appeared, stalled on a slow `open` of an NFS or FUSE root, and completed after a
newer detection had already published the quarantine, would publish its own
**empty** result over the newer one and re-admit both tenants until the next
entry point. Atomicity of the swap does not help — both writes are individually
atomic and the wrong one is last.

Two mechanisms, and they are not redundant:

- **One process-global `asyncio.Lock` around the whole operation.** Observation,
  the pairwise checks *and* the publication are inside the same critical
  section, so a second detection cannot begin until the first has published.
  Holding the lock only across the publication would leave exactly the
  interleaving above. Detection is cheap — N `open`s plus one mountinfo read —
  so a waiter costs a bounded stall, and a waiter is what we want: an entry
  point that finds a detection in flight is entitled to the answer it produces.
- **A monotonic sequence number, taken under the lock.** Each snapshot carries a
  sequence assigned when its detection began, and publication drops a snapshot
  whose sequence is not greater than the published one. The lock is the
  mechanism; the sequence is the *invariant*, and it is what survives a future
  caller — a test, a fixture, a sixth entry point — that publishes without
  taking the lock. A property enforced only by a lock somebody has to remember
  to take is a property that regresses silently.

The standalone rebuild process (E5) has its own event loop and its own lock, and
that is correct: it is a different process with a different snapshot, and it
consumes only what it published itself.

### Fail closed until the first snapshot

An asynchronously-published snapshot is not startup enforcement. Between the app
accepting connections and the first detection completing, a tool call would be
served against roots nobody has checked; and a first enumeration that *failed*
would leave the process permissive for ever.

So the published snapshot is a tri-state, and `_vault_root` refuses in two of
them:

- **Never published** → refuse every multi-user caller with a typed
  "not ready" refusal, distinct from both the overlap refusal and the
  no-assignment refusal.
- **Published, caller quarantined** → refuse with the caller's reason.
- **Published, caller absent** → admit, as today.

The lifespan runs `detect_and_publish()` **synchronously before serving**, so
the not-ready state is normally never observed. It remains reachable — a
detection that raised at startup, or a worker that somehow serves before it —
and in that state the correct answer is to refuse.

The cost of that fail-closed posture is low precisely because a *detector*
failure is not a per-root failure. A root that cannot be opened is a per-user
verdict (below), not a detector failure; a detector failure is the enumeration
query failing, which means the database is unavailable, which means the tools
are down anyway. So the server logs at ERROR, keeps serving the panel, and
retries at the next entry point rather than exiting — the same
partial-capability posture `_check_mount_identity_support` takes.

**Publication is atomic, monotonic, and never regresses on failure.** The
snapshot is one immutable object swapped in with a single assignment, so no
reader ever sees a half-built set; it carries the sequence number above, so an
older detection's result is dropped rather than published. A detection that
raises after a snapshot already exists
**retains the previous snapshot** and logs at ERROR; it does not clear it, and
clearing it back to "never published" is explicitly forbidden — a transient
database blip must not become a deployment-wide refusal, and a stale snapshot
of a condition that persists until an operator acts is the better of the two
errors.

Sandbox mode publishes an **empty** snapshot at startup without touching the
filesystem: it has no users, it skips the indexer, and the readiness invariant
must still hold or every registered tool would refuse for a reason the sandbox
cannot fix.

### Structured quarantine reasons, and the facts they preserve

The snapshot is not a set of ids; it is a mapping from user id to a reason, and
**each entry carries the facts as observed at detection time** — the subject's
username and canonical assignment, the peer's username and canonical assignment
for an overlap, and the moment the detection ran. Those are immutable in the
snapshot and are what every operator surface renders.

The alternative — record ids and let the panel resolve names at render time —
loses the display in exactly the situation it is needed. The operator's first
move on reading "vault root overlaps <peer>" is to *edit or delete* one of the
two accounts; the moment they do, and until the next detection publishes, a
render-time resolution shows a changed path, or a blank where a deleted peer
was, beside a condition that is still in force. Recording the facts also makes
the staleness honest: the surfaces label them "as at last check", so a name that
no longer exists is legible as a fact about the past rather than presented as
the current state.

| Reason | Meaning | Wording |
| --- | --- | --- |
| `overlap(peer_user_id, peer_username, peer_assignment, relation)` | This root and that user's root are the same directory, nested, or grafted. `relation` is one of `identical`, `contains`, `contained_by`, `mount_graft`. | Panel: "vault root overlaps <peer>'s (<relation>), as at last check". Run row and log: names both users and both roots, from the recorded facts. |
| `root_unexaminable(errno)` | The root could not be opened, so no overlap could be ruled out. **Not an overlap** — no peer is named, because none was observed. | Panel: "vault root could not be examined (<errno>) — not served". Run row and log: names the one user, the recorded assignment and the errno. |

Calling an unopenable root an "overlap" would send an operator looking for a
second account that does not exist. The two reasons are separately worded in the
panel, the log line, the `indexer_runs` row **and** the `usage_logs` marker
(`vault_root_overlap` / `vault_root_unexaminable`), for the reason this codebase
already keeps three vault markers apart: an operator investigating a
misconfiguration and an operator investigating a missing mount do different
things.

`root_unexaminable` quarantines **only that user**; the peers it could not be
compared against keep serving. Fail closed for the user whose status we cannot
establish, fail open for users we have no evidence against — one broken mount
must not take the deployment offline.

### Why not in `_vault_root`

`_vault_root` must stay a pure cache lookup. The architecture note gives the
reason: `APIKeyMiddleware` warms the cache on *every* authenticated MCP request,
and that warm is what makes a cache read correct — a database query in the gate
is a query on every tool call. Overlap detection would be worse than a query: it
needs every other user's assignment (a query), an `open` + `fstat` + `realpath`
per root, and a mount-table parse, on the hot path, per call.

**What the gate does instead is read the published snapshot** — one immutable
mapping lookup, ahead of the request's vault-root snapshot. No session, no
statement, no syscall. It is **refuse-only**: it can make the gate stricter and
can never admit a caller the existing logic would refuse, which is exactly why
it is safe to consult it *first*. Unlike an assignment — where a stale read must
never re-admit a revoked caller, which is why the immutable request snapshot
outranks the process-global cache — a quarantine has no direction in which
staleness admits anyone. The architecture note is updated to state this
exception in those terms, so it cannot be read as licence to add a query.

`VaultRootOverlap` and `VaultRootNotReady` both subclass `RuntimeError`, so every
existing `except RuntimeError` around `_vault_root` keeps failing closed
unchanged; `_vault_admission_error` adds branches ahead of the generic one to
select the message and marker.

The agent-facing message names **no other user and no other path** for any
reason — the caller is a tenant's agent. The operator-facing surfaces name
everything.

## Disposition for the affected tenants

**Quarantine = index refusal *and* tool refusal, for exactly the users the
snapshot names, with no rows deleted.**

Index refusal alone was considered and is insufficient:

- It stops *new* foreign rows entering the index. It does not remove the ones a
  previous pass already wrote, and those stay queryable by the outer tenant
  through `semantic_search` / `keyword_search` / the graph tools, which never
  touch the disk.
- It does nothing at all about writes. `edit_note`, `move_note`, `delete_note`
  and `write_file` resolve beneath the outer tenant's root, and the inner
  tenant's files *are* beneath it; `RESOLVE_BENEATH` agrees. The write path
  never consults the indexer. A destructive write that clobbers a note has
  actually happened on this server and is the expensive failure, so refusing to
  index while leaving the clobber reachable would aim the control at the cheaper
  of the two.

Tool refusal reaches all three consuming surfaces:

| Surface | How |
| --- | --- |
| Every MCP tool | `_vault_root` raises → `_tracked`'s admission gate refuses. Total by construction, exactly as #66 made it: nothing is exempt, and a tool added later inherits it. |
| Transfer redemption | A capability minted before the overlap carries a pinned `vault_root` that still matches, so every existing check passes; the public `/transfer/*` routes never call `_vault_root`. The redemption gate in `src/services/transfer.py` already re-reads the owner row and already refuses an inactive owner or a changed root — the snapshot test goes there. |
| Panel vault browser | `vault_page` renders the existing `vault_error` empty state. |

And the operator surfaces stop over-reporting liveness:

- **The users list shows the quarantined state instead of a note count.** The
  panel already refuses to render a number beside an unassigned account, for
  precisely this reason: a count reads as capacity the account has, when every
  tool call from it is refused before its body runs. A quarantined account is in
  the same position — assigned, indexed, and served by nothing — so it gets the
  same treatment with its own wording and its own reason.

**Nothing is deleted.** Preserving the rows is what makes a corrected assignment
cheap, and the alternative here is worse — the outer tenant's index may hold the
inner tenant's rows, and a blanket delete would be a second, unreviewed deletion
path over index contents. The operator's correction triggers the existing
repair: a changed assignment or realpath drives `classify_provenance` to
*discard* or *re-derive*, and rows for files no longer beneath the root are
removed by the ordinary prune.

**Unrelated tenants are untouched.** Every active user the snapshot does not
name is indexed and served normally in the same pass — the same isolation
`_index_pass_once` already gives a user with a broken vault.

**The refusal is recorded twice, and a pause does not suppress either.** At
ERROR, so the ops-health ring buffer catches it; and in the affected users'
`indexer_runs.error`, because the ring buffer is 100 entries and
process-lifetime while the misconfiguration persists across restarts — the same
argument that made `notes_metadata.links_truncated` a column rather than a log
line (#203). A **paused** iteration still publishes the snapshot, still logs at
ERROR and still writes the per-user run rows before returning: the pause exists
to stop index and embed work, and a quarantine that goes unrecorded for the
whole of a pause is a quarantine the operator cannot see. The row cadence is
unchanged — a running deployment already writes one row per user per tick.

## Single-user mode

`multi_user_mode = False` means `current_user_id` is None, `_vault_root(None)`
answers from `settings.vault_path`, `_active_user_ids()` returns `[]` and the
pass runs with `user_id=None`. There is exactly one root and no second
assignment, so `detect_and_publish()` publishes an empty snapshot and the gate's
lookup is never reached for `user_id is None` — including the readiness state.
**Neither the assignment check nor the quarantine changes any single-user
behaviour**, and the specs say so as scenarios rather than leaving it inferred.

The one multi-user case that touches `settings.vault_path` is the legacy
`/obsidian` mount, which `validate_vault_root_path` admits for one user. It is
an ordinary root to all three checks: if a second user is assigned a `/vaults/`
directory that is a bind mount of `/obsidian`, identity catches it — which is
precisely the case string equality could not see.

## Rejected alternatives

1. **Enforce in `_vault_root`.** Rejected: it is the admission gate on the hot
   path of every tool call and the architecture note forbids a database query
   there; detection needs a query, filesystem I/O *and* a mount-table parse. The
   refuse-only snapshot lookup is what is left after removing all three.
2. **A database column (`users.vault_root_quarantined`) written by the pass.**
   Rejected: a persisted quarantine is a second source of truth about a
   filesystem that keeps moving. It can outlive the condition or lag it, and it
   needs a migration to express a fact recomputed at every entry point anyway.
3. **Turn on `RESOLVE_NO_XDEV`, or check `STATX_MNT_ID` for every directory
   during the walk.** Rejected as the *general* answer: it would refuse a
   legitimate single-tenant vault that spans mounts, which the docs correctly
   treat as fine, converting a two-tenant misconfiguration into a one-tenant
   outage. Check 3 gets the same information from the mount table, once per
   detection rather than once per directory, and only ever concludes an overlap
   when a *second tenant's* filesystem-relative root is on the other end.
4. **Compare `st_dev` alone to decide "same filesystem, therefore related".**
   Rejected as unsound in both directions — see the table.
5. **Refuse only the *newer* assignment and leave the older tenant serving.**
   Rejected: "newer" is not a fact the system holds (`users` has no
   `vault_path_changed_at`), and the outer tenant is usually the older one and
   is precisely the one whose tools can clobber the inner tenant's files.
6. **Walk one root looking for the other's inode.** Rejected: a full-tree walk
   per pair per pass, on vaults of thousands of files, for what check 3 reads
   from one file.
7. **Detect at startup only, or in `run_indexer_loop` only.** Rejected: the
   former leaves a symlink created at 09:00 undetected until the next restart;
   the latter is the shape this revision removes, because Reindex Now (E4) and
   `make rebuild-tsvectors` (E5) both reach a pass without touching that loop —
   and E5 is a different process entirely.
8. **Publish the snapshot asynchronously and serve permissively until it
   lands.** Rejected: a tool call in that window is served against roots nobody
   has checked, and a failed first enumeration leaves the process permissive for
   the life of the container.

## Risks / Trade-offs

- **Filesystem work under the admin advisory lock.** Bounded by the number of
  active assigned users; the same handler already stats the candidate root
  before taking the lock, so a hung mount already hangs it. The alternative
  (observe outside the lock, decide inside) is check-then-act.
- **A false positive quarantines two live tenants.** Checks 1 and 2 are
  structural and deterministic — inode identity and a canonical path prefix —
  not heuristics with thresholds. Check 3 is the one that can be wrong, in the
  refusing direction, and only through a shadowed mount entry (L2). Production's
  two roots are siblings under `/vaults/`, so the expected live result is an
  empty snapshot, and the deploy check is exactly that.
- **`root_unexaminable` is a new refusal for an existing condition.** A mount
  blip today fails the disk-touching tools and leaves the database-backed ones
  answering; under this change it refuses all of them for up to one interval.
  Accepted (L3, owner decision 5): "we could not look" is not evidence of
  safety, and the refusal is loud, named and self-clearing.
- **Multi-worker.** The snapshot is process-global, like `_user_vault_cache` and
  the error ring buffer. Under a single-process uvicorn (today) that is the
  whole server; under workers, each converges at its next entry point, and each
  worker's lifespan publishes synchronously before it serves.
- **`scripts/rebuild_tsvectors.py` is a separate process** and gets its own
  snapshot. Its detection cost is one extra pass over the mount table and N
  opens, against a job that rewrites every keyword vector in the vault.

## Accepted limitations

| # | Limitation | Why it is accepted |
| --- | --- | --- |
| L1 | Check 3 sees only **this process's mount namespace**. A graft performed in another namespace is invisible. | It is also untraversable from here: a mount this process cannot see is a mount this process cannot walk into, so the containment it would create does not exist for this server. |
| L2 | Check 3 does not model **shadowing**. An entry later overmounted at the same mount point is still listed, so a stale graft can produce a refusing verdict for an overlap that is no longer reachable. | The refusing direction, admin-visible, named in the panel with both roots, and cleared by removing the stale mount. Modelling shadowing means reimplementing the kernel's mount-tree resolution from a text file. |
| L3 | Check 3 is **Linux-and-`/proc`-only**. Where `/proc/self/mountinfo` cannot be read or parsed it is skipped, logged once, and the health page states that graft coverage is unavailable. Its availability is tracked separately from `STATX_MNT_ID`, which is a different capability on a different kernel version. | Best-effort in the refusing direction only, exactly like `read_dir_handle`: it can add an overlap and can never clear one, so its absence degrades coverage and nothing else. It never quarantines anyone by being unavailable. |
| L4 | All three checks are **point-in-time**. Between two entry points a root can be aliased and un-aliased with no record. | The window is one index interval (300 s); the assignment-time check closes the administrator-initiated case entirely; and continuous detection needs an inotify/fanotify watch on paths the container may not be able to watch. |
| L5 | The guard **refuses the configuration; it does not un-index what a previous configuration indexed.** Rows the outer tenant's pass already wrote for the inner tenant's notes remain until a corrected assignment drives `classify_provenance` to discard/re-derive or the ordinary prune removes them. | They are unreachable while the quarantine stands (the admission gate is total), and a blanket delete is a second deletion path over index contents with a worse failure mode than the one it fixes. |
| L6 | The snapshot is **process-global**; under multiple uvicorn workers a worker converges only at its next entry point. | Same boundary as `_user_vault_cache`, `clear_user_vault_cache` and the ops-health ring buffer; single-process today, and each worker's lifespan publishes before it serves. |
| L7 | A **direct `UPDATE users SET vault_path`** in psql bypasses enforcement point 1 entirely. | Enforcement point 2 covers it at the next entry point. Nothing in the application can gate a statement issued outside it. |
| L8 | `root_unexaminable` **refuses the database-backed tools too**, which a missing mount does not do today. | Fail closed on "could not rule out an overlap"; owner decision 5, with the alternative recorded. |
| L9 | The panel's **`/vaults/*` dropdown is unchanged**; it still offers only top-level directories and does not pre-filter conflicting ones. | The refusal on submit is the enforcement; a filtered dropdown that silently omits a directory is less legible than a refusal that names the conflicting user. |

## Owner decisions

Defaults chosen and applied throughout the specs; each can be reversed without
touching the rest of the design.

1. **Disposition = index refusal *and* tool refusal (fail closed for the pair).**
   *Default taken.* Alternative: index refusal only. Rejected because the write
   path never consults the indexer, so index-only leaves the destructive
   cross-tenant write — the ranked failure — fully reachable.
2. **Three distinct markers and three exception types** —
   `vault_root_overlap`, `vault_root_unexaminable`, `vault_root_not_ready` —
   rather than reusing `no_vault_assigned`, and **all three join the shared
   pre-body-refusal predicate** in `usage_stats.py`. *Default taken.* Reusing
   `no_vault_assigned` tells the operator an administrator unassigned a user
   whose users page shows an assignment; and a marker the predicate does not
   enumerate is wrong in both directions at once — its `duration_ms` pollutes
   the tool's latency percentiles and the refusal is never counted.
3. **`_vault_root` reads the published snapshot directly (refuse-only, ahead of
   the request snapshot)** rather than the quarantine riding in the cache's value
   domain. *Default taken.* Keeps `warm_user_vault_cache`'s return type and the
   ContextVar type unchanged, and cannot be re-admitted by a stale bulk warm.
4. **A peer root that cannot be opened refuses the *assignment*.** *Default
   taken.* Alternative: fall back to containment alone for an unexaminable peer.
   Rejected because identity is the check that catches what string equality
   already misses.
5. **A root that cannot be opened during a *pass* quarantines that user only,
   under its own reason and its own marker.** *Default taken.* Deliberately
   asymmetric with decision 4: an assignment is a discrete administrator action
   that can be retried, while a pass runs unattended and must not turn one broken
   mount into a deployment-wide outage. Alternative: treat an unexaminable root
   as "not quarantined" and let the tool bodies fail on open — rejected because
   the database-backed tools would keep answering from rows whose provenance
   nobody could re-establish.
6. **One `detect_and_publish()`, called from all five entry points, with the
   per-user stage skip in the shared pass helpers** so a sixth entry point
   inherits the guard. *Default taken.*
7. **Fail closed until the first snapshot, with the lifespan publishing
   synchronously before serving; a later detector failure retains the previous
   snapshot and logs ERROR.** *Default taken.*
8. **Check 3 is best-effort in the refusing direction only, with its own
   availability state.** *Default taken.* Alternatives both rejected: making it
   a startup requirement is the whole-server-outage direction
   `_check_mount_identity_support` already refused; and gating it on
   `vault_fs.mount_identity_available()` conflates two independent
   capabilities — mountinfo needs no `STATX_MNT_ID` (5.8) and a 5.6/5.7 kernel
   has one without the other — which would both disable the check where it
   works and make its failure masquerade as a transfer-write outage on
   `/health`.
9. **The tool-facing refusal names no other user or path for any reason; the
   panel, the log and the run row name everything.** *Default taken.*
10. **`_check_vault_path_unique` is subsumed by the shared predicate**, with its
    exact-duplicate wording preserved by passing the canonical assignment
    strings alongside the descriptors. *Default taken.* Two functions answering
    the same question is how the two drift apart.
11. **The quarantined state replaces the note count on the users list**, with its
    own wording and reason, mirroring the existing unassigned treatment.
    *Default taken.*
12. **The whole detect-and-publish is serialized under one process-global
    `asyncio.Lock`, and publication is monotonic by a sequence number taken
    under that lock.** *Default taken.* The lock alone would be enough today;
    the sequence is what keeps the invariant true for a caller that publishes
    without it. Alternative: hold the lock only across the publication —
    rejected, it leaves precisely the stale-overwrite interleaving.
13. **The assignment-time check runs only when the edit's resulting state is
    active and assigned.** *Default taken.* Alternative: check unconditionally
    — rejected, it refuses the deactivation that is the operator's remedy.
14. **The snapshot records the observed usernames, canonical assignments and
    the detection time, and the surfaces render those rather than re-reading
    `users`.** *Default taken.* Alternative: resolve names at render time —
    rejected, it blanks the display exactly when the operator edits or deletes
    the peer the condition names.
15. **No migration, no persisted quarantine.** *Default taken.*
16. **The `/vaults/*` dropdown is left as it is** (limitation L9). *Default
    taken.*

## Review history

Round 1 (Codex, pre-code): FAIL — 2 BLOCKER, 5 MAJOR, 2 MINOR. All nine folded
in, none rejected.

| Finding | Where it landed |
| --- | --- |
| BLOCKER — detection only in `run_indexer_loop`; E4/E5 bypass it | The E1–E5 entry-point table, one `detect_and_publish()`, the stage skip in the shared pass helpers, tasks 3.2/3.6/6.6, and an `index-integrity` requirement per entry point |
| BLOCKER — async startup pass is not startup enforcement | Tri-state snapshot, typed not-ready refusal, synchronous lifespan detection, atomic publish, retain-on-failure; `mcp-request-routing` requirement + scenarios |
| MAJOR — bind-mount-inside is a permanent hole | Check 3 (mountinfo), its proof/limits row, L1–L3, owner decision 8 |
| MAJOR — an unopenable root is not an "overlap" | Structured reasons `overlap(peer, relation)` / `root_unexaminable(errno)`, separately worded in panel, log, run row and marker |
| MAJOR — the new marker must join the pre-body predicate | `panel-performance-views` MODIFIED, `usage_stats.py` added to slice 4 |
| MAJOR — the users list must show a quarantined state | `mcp-request-routing` MODIFIED, slice 2 owns `users.py` + `users.html` |
| MAJOR — slices not disjoint | Complete file ownership restated per slice; 1.6 moved to slice 2, `auth/routes.py` declared in slice 2, `usage_stats.py` in slice 4, `main.py` + `scripts/` in slice 3, `routes.py` wholly in slice 6 |
| MINOR — carry canonical assignment strings | Predicate signature takes them; exact-duplicate wording preserved |
| MINOR — a paused iteration must still log and record | Stated in the disposition and pinned as an `index-integrity` scenario with its own task |

Round 2 (Codex): FAIL — 2 BLOCKER, 1 MAJOR, 1 MINOR, round-1 items accepted as
resolved. All four folded in, none rejected.

| Finding | Where it landed |
| --- | --- |
| BLOCKER — a stalled older detection can publish an empty result over a newer quarantine | "One detection at a time, and a publication that cannot go backwards": one process-global `asyncio.Lock` around observation, checks *and* publication, plus a monotonic sequence taken under it; `index-integrity` requirement + concurrent stale-overwrite scenario; tasks 1.10, 1.11 and the concurrency test in 1.16 |
| BLOCKER — mountinfo availability must not reuse `vault_fs.mount_identity_available()` | Its own `_mountinfo_available` state, probed on first use, logged once, surfaced on the health page; `transfer_mount_check_available` untouched; the independence of `STATX_MNT_ID` (5.8) and `/proc/self/mountinfo` written out in the check-3 paragraph, L3 and owner decision 8 |
| MAJOR — the assignment check must gate on the *resulting* state | Step 0 of enforcement point 1 (`normalized and new_active`), owner decision 13, `panel-user-administration` requirement text + deactivation, unassignment and reactivation scenarios, tasks 2.1/2.2 |
| MINOR — the snapshot must carry the observed names and assignments | "Structured quarantine reasons, and the facts they preserve"; reason payloads carry peer username and canonical assignment plus a detection timestamp; surfaces render them labelled "as at last check" and re-read no `users` row; owner decision 14 |

## Open Questions

(none blocking — the thirteen decisions above are the choices, each with a
default applied)
