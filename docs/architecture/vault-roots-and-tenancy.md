# Vault roots, tenancy, and the admission gate

> Deep rationale extracted from `CLAUDE.md`. Read before touching `APIKeyMiddleware`, `_vault_root`, the owner predicates, or anything that publishes into a vault.

## The vault assignment is the admission gate for every tool

`_tracked` in `src/mcp_server/tools.py` resolves `_vault_root(current_user_id)`
**once, before the tool body runs**, and fails the call with a tool error when
it raises. That is the whole enforcement of "this user has no vault", and it
lives in the shared decorator on purpose.

Per-tool checks were the bug (#66). The tools that leaked — `semantic_search`,
`keyword_search`, `list_notes`, `get_recent` and every graph tool — are exactly
the ones with no reason to call `_vault_root`: they are served from
`notes_metadata` / `note_embeddings` filtered by `user_id` alone. Unassigning
`users.vault_path` stopped only the disk-touching tools, while the indexer's
`_active_user_ids()` (which filters `vault_path IS NOT NULL`) meant the user's
rows were never pruned either. An unchanged API key kept returning paths,
titles, tags, frontmatter and chunk excerpts indefinitely, while the panel had
told the operator "vault tools error".

- **Nothing is exempt.** Every `_tracked` tool reads or writes vault content or
  vault metadata — `get_vault_guide` returns the vault's own `CLAUDE.md`,
  `check_upload` reports a published vault path and digest. Keep the exemption
  list at zero; a new tool inherits the gate by being registered.
- **The index rows are preserved.** Deleting `notes_metadata` on the NULL
  transition was the weaker fix: it forces a full re-embed on reassignment and
  leaves the credential itself unaddressed.
- **`_vault_root` must stay a pure cache lookup.** What makes that correct is
  `APIKeyMiddleware` calling `warm_user_vault_cache(session, user_id)` on
  *every* authenticated MCP request. Do not add a DB query to the gate.
- **The single-user form of that warm is authoritative — it evicts**, and it
  returns the root it read. It used to be a silent no-op for a NULL
  `vault_path`, so a previously cached root survived; the panel's
  `clear_user_vault_cache` only clears the worker that served the POST.
- **`_vault_root` prefers the request's own snapshot over the shared dict, and
  that is the part that fails closed.** `_user_vault_cache` is process-global
  and the indexer's bulk warm is add-only, so a bulk `SELECT` issued *before*
  the admin cleared `vault_path` can land *after* the per-request warm evicted
  the entry and put the revoked root back — mid-request, with a write tool in
  flight. Eviction cannot order a query that was already running. So the
  middleware binds `current_vault_root = (user_id, Path | None)` (a ContextVar
  beside `current_user_id` in `src/auth/session.py`) and the gate reads that;
  no other task can write this request's context. **Do not "simplify" the gate
  back to the dict** — the bulk warm's add-only behaviour is safe only because
  the snapshot outranks it. The snapshot is keyed by user id (another user's
  snapshot falls through to the dict) and is never consulted for
  `user_id is None`.
- **A cold cache refuses too**, with the same message — it is not permission to
  serve stale rows — and the refusal is written to `usage_logs` with
  `params["error"] = "no_vault_assigned"` and no other new field.
- Single-user and sandbox mode are untouched: `current_user_id` is None there
  and `_vault_root(None)` answers from `settings.vault_path`.
- **In multi-user mode, `user_id is None` is a refusal, not the global vault.**
  An ownerless credential — `api_keys.user_id` / `oauth_tokens.user_id` NULL —
  is the *single-user* shape, and it survives a configuration cycle: a key
  minted while multi-user was off keeps its NULL, and the bootstrap backfill in
  `src/auth/routes.py` only claims NULL rows while `users` is empty, so
  flipping the flag after users exist never adopts it. Every layer then treated
  that key as single-user and handed it `settings.vault_path` — an ownerless
  *readwrite* key could edit the whole vault. `APIKeyMiddleware` now 401s such
  a credential (`reason=ownerless_credential`, same body as any other rejected
  key, on both the API-key and OAuth branches) and `_vault_root(None)` raises
  when `settings.multi_user_mode`. Two layers on purpose: the middleware is the
  gate, `_vault_root` is the one that cannot be bypassed by a future caller.
- **The panel's vault browser uses what the warm returned, not a re-read of the
  dict.** `vault_page` warmed the cache and then called `_vault_root(user.id)`,
  which reopens the same window: a stale bulk warm landing in between served an
  unassigned user's vault. It now takes the `Path | None` from
  `warm_user_vault_cache` directly and renders the `vault_error` empty state on
  None. Any new caller that warms-then-resolves has the same bug — use the
  return value.

## The vault-root overlap guard (#199)

Two active users whose `users.vault_path` values name overlapping directories
are not a *partial* leak. `openat2(RESOLVE_BENEATH)` confines every lookup to
the caller's root and **agrees that the other tenant's files are beneath it**;
the indexer files one tenant's notes under the other's `user_id`, so
`semantic_search` and `keyword_search` answer with them; and the write tools
resolve beneath a root that physically contains the other tenant's notes.
Overlapping roots make two tenants one tenant, in both directions, for every
tool this server has.

The check that stood there was **string equality**. `_check_vault_path_unique`
rejected an identical `vault_path` among active users, and
`validate_vault_root_path` rejected an empty path, a `..` component, a prefix
outside `/vaults/` and a non-directory. Three shapes went straight through: an
**ancestor/descendant** pair (`/vaults/team` and `/vaults/team/private` are two
different strings), an **alias** (a symlink or a bind mount naming one
directory twice), and a **graft** (a bind mount putting one vault *inside* the
other's tree, with both root inodes distinct and both canonical names outside
each other). What made it worth fixing is not an untrusted actor — every shape
needs an administrator to hand-type a path or edit a mount, and the dropdown
offers top-level `/vaults/*` only — but that the exact-duplicate rejection was
a **false assurance that collisions were checked**, over the two failures this
product ranks highest: a cross-tenant destructive write and a silently wrong
search result, both delivered to an agent that acts on them without a human
seeing the query.

`src/services/vault_overlap.py` is the one place that decides "do these two
roots collide" and the one place that publishes the answer. Read it and this
section together before changing either.

### The two checks, and what each proves

Both are taken from **one opened directory descriptor per root, in one moment**
— the way `indexer.observe_root_facts` binds a realpath to an `fstat` — never
from the `vault_path` string, which is precisely the thing that can be a
symlink to somewhere else. `observe_root_blocking` opens the root
`O_RDONLY | O_DIRECTORY | O_CLOEXEC`, takes `(st_dev, st_ino)` from
`os.fstat(fd)` and the canonical path from `os.path.realpath`, requires
`os.stat(realpath)` to report the same pair as the descriptor, and **closes the
descriptor on every exit path**. A failure is a *verdict* carrying the `errno`,
never an exception.

| Check | Proves | Does **not** prove |
| --- | --- | --- |
| **1. Identity** — `roots_identical`: `(st_dev, st_ino)` equality of the two descriptors. | The two assignments name **one directory object** at this instant: same superblock, same inode, so every entry reachable under one is the same entry under the other. Catches a symlink alias, a same-filesystem bind mount of one directory to two pathnames, and a directory hard link where the kernel permits one — the aliases string equality cannot see, because the two strings differ. | **Nothing about containment.** Two distinct inodes nest all the time; that is check 2's job and identity is blind to it. **Equal `st_dev` is not proof of one mount** — a bind mount of a subtree of the same filesystem reports the same device while being a different mount, which is why transfer publication uses `statx`'s `STATX_MNT_ID` and `_check_mount_identity_support` warns rather than comparing `st_dev`. **Unequal `st_dev` is not proof of unrelated directories** — a separate filesystem mounted *inside* another tenant's root gives the two roots different devices and total overlap. Different `st_dev` says "a `rename(2)` between these would fail `EXDEV`"; it says nothing about who can read whom. **Do not "simplify" this to a device comparison.** |
| **2. Containment** — `contains_path`: a **component-wise** prefix test over the two canonical real paths, in **both** directions. | One root's **canonical pathname lies inside** the other's, right now, in this container's mount namespace. `realpath` resolves every component, so an ancestor reached through a symlink (`/vaults/b -> /vaults/a/inner`) canonicalises to the containing form and is caught. Complement to check 1: identity answers "same object", containment answers "one inside the other", and neither implies the other. | It is a **pathname** fact about this namespace at this instant. It cannot see a future mount or symlink — which is why the checks re-run at every pass entry point and not only at assignment — and it cannot see an overlap that **neither canonical name expresses**: a bind mount grafting one tenant's directory into the other's tree leaves both root inodes distinct and both real paths outside each other. That is limitation **L1** below. |

**Component-wise is load-bearing, not tidiness.** A raw string prefix test
reports `/vaults/team` as an ancestor of `/vaults/team-2` and would refuse an
assignment that overlaps nothing — quarantining two healthy tenants, which is
the false-positive direction this codebase treats as the expensive failure.
`contains_path` compares `PurePosixPath(...).parts` and is **strict**: a path
does not contain itself, because identity is check 1's job and has its own
relation.

**`relation_between` is the one predicate**, and it takes the two canonical
*assignment strings* alongside the observed facts (both ride on
`RootObservation`) so an exactly duplicated assignment reports `identical` even
when neither root could be opened, and the caller can select the
exact-duplicate wording operators already know instead of describing an equal
pair as a containment. Identity is reported in preference to containment
because an identical pair satisfies neither containment direction and would
otherwise fall through as "no relation". `_check_vault_path_unique` is gone,
**subsumed** by this predicate: two functions answering the same question is
how the two drift apart.

**The scope is the two roots themselves, and that is a deliberate narrowing.**
A `/proc/self/mountinfo` check was proposed, specified, and removed — three
review rounds each produced a *new* mount configuration it failed to cover,
which is the signal that it was a heuristic pretending to be a rule, and it
would have sat on the admission gate for two live tenants. What ships is the
two checks that are **structural and total over what they claim**. Everything
the mount table would have been needed for is L1/L2 below, with the destructive
consequence written out. **Do not reintroduce a mountinfo parser here without
reading that history.**

### Enforcement point 1 — assignment time

`_check_vault_root_conflict` in `src/control_panel/users.py` runs inside the
existing `_lock_admin_guard` transaction in `edit_user_submit`, after
`_validate_vault_path` and before the commit, under the existing
`_ADMIN_GUARD_LOCK_KEY` — **no second key**, because
`panel-user-administration` already pins one lock key for both handlers.
Without the lock this is check-then-act: two admins assigning `/vaults/team`
and `/vaults/team/private` at the same moment each read the other's *previous*
row and both writes land.

- **It runs only when the edit's *resulting* state is active and assigned**
  (`normalized and new_active`). An edit whose result is an inactive account,
  or one with no assignment, can create no overlap — the peer query and the
  detector both scope to active users holding an assignment — and refusing it
  would trap the operator in the one state they most need to leave.
  **Deactivating or unassigning is the panel's own remedy for a quarantined
  account, and a guard that refuses *that* because the account still overlaps
  is a guard with no exit.** Reactivating or reassigning is an edit whose
  result is active and assigned and runs the full check. Do not "harden" this
  into an unconditional check.
- The peer set is `SELECT id, username, vault_path` for every **other** active
  user holding an assignment; the candidate root and each peer root are
  observed and run through `relation_between`. First conflict wins.
- **A peer root that cannot be opened refuses the assignment**, naming that
  root and saying the overlap could not be *ruled out* — not reporting an
  overlap that was not observed (`peer_unexaminable_message`). Admitting on "we
  could not look" is the direction this codebase treats as the expensive error,
  and identity is precisely the check that catches what string equality already
  misses. The precedent is in the same function: `validate_vault_root_path`
  already refuses the *candidate* for the missing-mount case.
- Wording comes from `assignment_conflict_message`, so the handler does not
  re-implement the relation-to-message mapping. The `identical` wording is
  preserved **verbatim** from the string-equality check it subsumes — it is the
  message operators already know; `contains` and `contained_by` name the
  relation, because "already assigned" would be false for them and would send
  an operator looking for a duplicate string.
- **Every filesystem call on this path is off the loop and bounded**, and that
  is not tidiness. `validate_vault_root_path` is split in two:
  `validate_vault_root_lexical` decides the shape of the string (the `..`
  guard, the `/vaults/`-or-legacy rule) with no syscall, and the `async`
  `validate_vault_root_path` then observes the root through
  `vault_overlap.observe_root` — `O_DIRECTORY`, off the event loop, under
  `VAULT_ROOT_OBSERVE_TIMEOUT_SECONDS`. It used to be one function ending in a
  synchronous `Path(normalized).is_dir()`, called inline from
  `edit_user_submit` **after** the account guard was taken: a bind mount that
  stopped answering stalled every request the process was serving, from inside
  the one page an operator would use to move a vault *off* that mount, and held
  `_ADMIN_GUARD_LOCK_KEY` while it did. The signature is `async` on purpose —
  it is what stops a future caller reintroducing the blocking form by accident,
  because a sync call site now gets an unusable coroutine loudly instead of a
  stall nobody attributes to that line. The missing-mount wording is unchanged;
  a root that exists but could not be observed reports the cause instead, since
  sending an administrator to look for a directory that is present costs them
  the incident.
- **The `/vaults/*` dropdown is probed the same way.**
  `_list_available_vaults` runs `_list_available_vaults_blocking` through
  `asyncio.to_thread` under the same deadline, and expiry degrades the
  *dropdown*, not the page: it offers no candidates and the custom-path field
  beside it is the way through. The dropdown itself is otherwise unchanged and
  still does not pre-filter conflicting directories (limitation **L9**) — the
  refusal on submit is the enforcement, and a filtered dropdown that silently
  omits a directory is less legible than a refusal naming the conflicting user.
- **`create_user` and the bootstrap are untouched.** `create_user` always
  writes `vault_path=None`. `src/auth/routes.py::register_submit` runs only
  while `_users_table_empty` holds — zero rows — so the peer set is empty by
  invariant and a check there could never fire; it carries a comment saying so
  and no code. A check that can never fire invites a future reader to believe
  the path is covered by code when what covers it is the invariant.

An assignment-time check alone is not enough, which is why there is a second
enforcement point: an assignment is validated once and the filesystem keeps
moving. A symlink created after the assignment, a bind mount repointed by a
compose edit, or a `vault_path` written directly in psql produces the same
overlap with no administrator action to intercept.

### Enforcement point 2 — one detection, called from every entry point

`vault_overlap.detect_and_publish()` is the **only** thing that computes a
snapshot, and every code path that can begin a pass over a vault root calls it
before it reads a byte. A check installed in `run_indexer_loop` alone would be
bypassed by two of the five, one of which is a different process entirely.

| # | Entry point | Call site |
| --- | --- | --- |
| E1 | `src/main.py::lifespan` | `_publish_first_root_snapshot()` — **synchronously, before the app serves**: before `asyncio.create_task(run_indexer_loop())` and before `yield`, placed after the fingerprint guards so a misconfigured deployment still fails with their own messages. The sandbox branch calls it too. |
| E2 | `src/services/indexer.py::run_indexer_loop`, startup block | `detect_root_overlaps("startup")`, above `async with index_pass_lock`. |
| E3 | `run_indexer_loop`, periodic tick | `detect_root_overlaps("periodic")`, **before** the `_is_paused()` check. |
| E4 | `src/control_panel/routes.py::_reindex_background` | `detect_root_overlaps("panel on-demand")` at the top, before `index_pass_lock` — Reindex Now, re-embed and reset embeddings all land here. It mirrors `run_indexer_loop` and shares the pass lock and *nothing else*. |
| E5 | `scripts/rebuild_tsvectors.py` (`make rebuild-tsvectors`) | `detect_and_publish()` at the top of its own `_active_user_ids()` loop. A **separate process** — `docker compose run --rm`, no lifespan, no indexer loop — so it publishes its own snapshot and consumes only that. |

**The rule the specs pin is not "call it in these five places"** but *no code
path may begin a pass over a vault root without a snapshot published in this
process by this orchestration*. That is why the per-user skip lives in the
shared pass helpers rather than in each loop: `index_vault`,
`link_backfill_pass`, `embed_vault` and `rebuild_tsvectors` each call
`_refuse_quarantined_pass` **ahead of resolving the root**, so a sixth entry
point added later inherits the guard by routing through the same helper. A skip
re-implemented per loop is a skip one loop will be missing.

All five call the detection **before** taking `index_pass_lock`, deliberately:
the check must not queue behind the pass it exists to gate.

### Root observation is bounded, and the residual is stated

`os.open`, `os.fstat` and `os.path.realpath` on a vault root are not reliably
fast — these roots are bind mounts, and a network- or FUSE-backed one can block
in the kernel for minutes. Two things would then break at once: the lifespan's
**synchronous** first detection would hold the process before it serves, and
the detection lock would be held for the whole stall so every other entry point
queued behind it.

So `observe_root` dispatches each root's observation through
`asyncio.to_thread` under `VAULT_ROOT_OBSERVE_TIMEOUT_SECONDS`
(`src/config.py`, default **10**). **Expiry is a per-user verdict, not an
exception**: that user gets `RootUnexaminable(CAUSE_TIMEOUT)` — its own value,
distinct from an `errno`, because "the mount is hung" and "the directory is
gone" are different incidents an operator acts on differently — the detection
proceeds to the next root and still publishes. Treating a timeout as a
*detector* failure would retain the previous snapshot on every tick a slow
mount was slow, so an overlap appearing later would never be published.

**The deadline abandons the wait, not the syscall.** A Python thread blocked in
`open(2)` cannot be cancelled; it stays parked until the filesystem answers or
the process ends, so a pathological mount accumulates one thread per detection.
That is limitation **L4**: the bound that matters is on detection *latency*,
the blocked thread holds no lock and no pooled connection, the condition is
loud (the user is quarantined and named on the panel), and the alternative is a
server that will not start.

### The detection population is *active assigned users* — and E5 checks a wider one

`detect_and_publish` observes **active users holding an assignment**, and that
is deliberate in both directions. It is who the server serves: `_vault_root`
admits them, the periodic pass indexes them, and a quarantine is a refusal
aimed at exactly that. Widening it would be a mistake, not a hardening — an
inactive account is served by nothing and indexed by nothing, so quarantining
it refuses nothing while putting its name on the panel beside an active peer
that now looks implicated. **Do not widen the serving snapshot to inactive
users.**

The **all-scopes keyword rebuild** (`rebuild_tsvectors_all_scopes`, E5) opens a
strictly larger set, and that is equally deliberate. Since #206 it enumerates
scopes from the rows that exist (`SELECT DISTINCT user_id FROM notes_metadata`,
not `_active_user_ids()`), because the fingerprint it records asserts something
about *every retained row* — and an inactive owner's rows are as retained, and
as returnable by `keyword_search`, as anyone's. `_scope_vault_path` reads that
owner's `users.vault_path` directly for the purpose.

Those two facts are each correct and together they were a cross-tenant read:

> User B is **inactive**, retaining rows under `/vaults/team`. User A is active
> at `/vaults/team/private`. The snapshot names nobody — B is not observed, A
> overlaps no *active* peer — so the driver pins `/vaults/team`, walks A's
> notes through it, writes their keyword vectors under B's `user_id`, and
> records a fingerprint certifying that every retained row was rebuilt.

So the driver runs the same two checks over **its own read set**:
`survey_rebuild_roots` observes every retained scope's root, active or not,
together with every active assigned root as a peer it could collide with
(deduplicated by user, so a scope is never paired against itself, and skipped
entirely in single-user mode where `users.vault_path` is not the tenancy
source). A relation involving a root the driver would open aborts the whole
operation and names the pair. A root it would open that cannot be observed is
`RebuildSkip.ROOT_UNEXAMINABLE` — a non-completed outcome that aborts exactly
as an unsettled provenance does, because "we could not look" is not a completed
rebuild. A root it would **not** open and cannot observe does not abort it:
nothing was observed to relate that root to anything, which is L2's class, and
failing maintenance because one unrelated tenant's mount is down is the
false-positive direction this codebase treats as expensive.

**The survey is maintenance-only. It publishes nothing.** The verdict is
computed inside the command and discarded with it, the serving snapshot is
untouched, and the set of users the admission gate refuses is unchanged. Two
populations answering two questions — *whom does this server serve* and *whose
bytes will this command read* — and a single answer would be wrong for one of
them.

**It also runs before `acquire_generation_lock`, and it keeps the
descriptors.** `survey_rebuild_roots` calls
`vault_overlap.observe_root_blocking_retaining` through
`indexer._observe_root_retaining` — same bound, same per-root verdict, but the
open directory is handed back rather than closed — and `_rebuild_scope` reads
through *that descriptor*. Under the generation lock this command performs **no
pathname lookup at all**; its only filesystem call on a root is `fstat` of the
fd it is already holding.

Reopening the pathname was wrong twice over. `pinned_root` calls `os.open`
synchronously, so an unexamined root opened under the lock lets one hung NFS or
FUSE mount hold the index generation lock for as long as the kernel takes to
answer, with every pass in the process — in every container — queued behind it.
And it is a *second* lookup, so it can land on an inode nobody examined; a
descriptor is the only object that still names the same directory after a wait.

The assignment is still compared, because a scope reassigned between the survey
and the lock has a retained descriptor that is the *wrong directory to rebuild*
— right in identity, wrong in tenancy. Peer descriptors are closed immediately:
nothing reads a peer's directory. `RebuildRootSurvey.close()` is idempotent and
`rebuild_tsvectors_all_scopes` calls it from a `finally`, so success, abort and
exception all release them; a timed-out observation whose thread answers later
is closed by `_close_late_root_descriptor`, which is L4's descriptor half.

### Lock order: account guard → generation lock → row locks

The survey is check-then-act **across processes** on its own. It accepts a
nested pair when the conflicting user is *inactive* — correctly, since nothing
serves an inactive user — and an administrator may reactivate or reassign that
user in the panel while `make rebuild-tsvectors` is still running. The reads
that follow are then exactly the cross-tenant read the survey exists to
prevent, and nothing inside the driver's transaction can see it coming: the
edit is a different connection committing between the check and the read.

So `rebuild_tsvectors_all_scopes` takes **`ACCOUNT_GUARD_LOCK_KEY`**
(`oauth.grants.lock_account_guard`) as its first statement, before it
enumerates anything, and holds it to the commit. That is the same key
`users._lock_admin_guard`, the self-service password change and every session
mint take, so an assignment edit either waits for the rebuild or lands before
the survey and is seen by it.

| # | Lock | Who takes it |
| --- | --- | --- |
| 1 | `ACCOUNT_GUARD_LOCK_KEY` | `users._lock_admin_guard` (admin handlers), `routes.change_password`, `session.start_session` (mint) — each **alone**; and `indexer.rebuild_tsvectors_all_scopes`, which then takes 2. |
| 2 | `INDEX_GENERATION_LOCK_KEY` | `indexer._index_vault_pinned`, `embeddings._generation_matches`, `routes.reset_embeddings`, `routes.trigger_reembed`, `scripts/reset_embeddings.py` — each **alone**; and `indexer._rebuild_all_scopes_locked`, reached only from 1's holder. |
| 3 | row locks | inside each per-scope rebuild. |

**One direction everywhere.** The maintenance rebuild is the only holder of the
pair, and it takes them in that order; no path anywhere takes them in the
opposite one. If you add a path that needs both, **take the account guard
first**. The rule is written at both lock definitions
(`oauth/grants.py::lock_account_guard`, `index_state.py::acquire_generation_lock`)
as well as here.

**The cost, stated rather than discovered:** a running `make rebuild-tsvectors`
blocks panel account edits and session mints until it commits — including its
wait for the generation lock, which is a wait for an in-flight index pass. That
is accepted. It is an operator-run one-off, the alternative is a cross-tenant
read, and both locks are `pg_advisory_xact_lock`, so a crashed rebuild releases
them with no operator action.

### One detection at a time, and a publication that cannot go backwards

Because the entry points call detection before the pass lock, two detections
can be in flight at once — a periodic tick (E3) and a panel-triggered reindex
(E4) overlap trivially, and E4 fires from three buttons. The failure that buys
is not theoretical and it fails **open**: a detection that began before an
overlap appeared, stalled on a slow `open`, and completed *last* would publish
its own **empty** result over the newer quarantine and re-admit both tenants
until the next entry point. Atomicity of the swap does not help — both writes
are individually atomic and the wrong one is last.

Two mechanisms, and they are not redundant:

- **One process-global `asyncio.Lock` around the whole operation.**
  Observation, the pairwise checks *and* the publication are inside the same
  critical section. Holding it only across the publication leaves exactly the
  interleaving above. Detection is cheap — N bounded root observations — so a
  waiter costs a bounded stall, and a waiter is what we want: an entry point
  that finds a detection in flight is entitled to the answer it produces.
- **A monotonic sequence number, taken under that lock** when the detection
  *begins*; `publish` drops a snapshot whose sequence is not greater than the
  published one. The lock is the mechanism; the sequence is the **invariant**,
  and it is what survives a future caller — a test, a fixture, a sixth entry
  point — that publishes without taking the lock. A property enforced only by a
  lock somebody has to remember to take is a property that regresses silently.

E5 has its own event loop, its own lock and its own snapshot. That is correct:
it is a different process, and it consumes only what it published itself.

### Fail closed until the first snapshot

An asynchronously-published snapshot is not startup enforcement. Between the
app accepting connections and the first detection completing, a tool call would
be served against roots nobody had checked; and a first enumeration that
*failed* would leave the process permissive for the life of the container. So
the published snapshot is a **tri-state** and the gate refuses in two of them:

| State | `_vault_root` |
| --- | --- |
| **Never published** (`published_snapshot()` is None) | Refuse every multi-user caller with `VaultRootNotReady` — distinct from both the overlap refusal and the no-assignment refusal. |
| **Published, caller named** | Refuse with `VaultRootOverlap` or `VaultRootUnexaminable`, per the recorded reason. |
| **Published, caller absent** | Admit, exactly as before this guard existed. |

- E1 publishes **synchronously before serving**, so the not-ready state is
  normally never observed. It stays reachable — a detection that raised at
  startup, or a worker that somehow serves before it — and in that state the
  correct answer is to refuse.
- **The consequence, stated because it is a deployment hazard:** a build that
  carries the gate but *not* the entry points refuses every multi-user call for
  the life of the process. The readiness state is what makes a failed first
  detection closed rather than permissive, and it is exactly what makes E1–E5
  load-bearing rather than belt-and-braces. If you add a code path that serves
  before the lifespan has published, it will refuse — that is the design, not a
  bug to route around.
- A detector failure is cheap to fail closed on precisely because it is **not a
  per-root failure**: a root that cannot be opened is a per-user verdict, so
  the only way `detect_and_publish` raises is that the user enumeration failed,
  which means the database is unavailable and the tools are down anyway. The
  process logs at ERROR, keeps serving the panel and retries at the next entry
  point rather than exiting — the same partial-capability posture
  `_check_mount_identity_support` takes.
- **Publication is atomic, monotonic and never regresses on failure.** The
  snapshot is one immutable object swapped in with a single assignment, so no
  reader sees a half-built set. A detection that raises **after** a snapshot
  exists **retains the previous snapshot** and logs at ERROR; clearing it back
  to never-published is explicitly forbidden — a transient database blip must
  not become a deployment-wide refusal, and a stale snapshot of a condition
  that persists until an operator acts is the better of the two errors. A
  detection that raises **before** anything has been published propagates, and
  the never-published state refuses.
- **Sandbox mode publishes an empty snapshot** at startup without touching the
  filesystem or opening a session: it has no users and skips the indexer, and
  the readiness invariant must still hold or every registered tool would refuse
  for a reason the sandbox cannot fix.
- **No migration, no persisted quarantine.** The condition is derived from the
  filesystem at every entry point; a column would be a second source of truth
  about a filesystem that keeps moving, able to outlive the condition or lag
  it. `detect_and_publish` issues **no database write**.

### Two reasons, worded apart

The snapshot is not a set of ids. It maps a user id to a `QuarantineEntry`
carrying **the facts as observed at detection time** — the subject's username
and canonical assignment, the peer's for an overlap, and the detection
timestamp — immutable, and rendered by every operator surface, which re-reads
no `users` row.

| Reason | Meaning | Where it is worded |
| --- | --- | --- |
| `Overlap(peer_user_id, peer_username, peer_assignment, relation)`, `relation` in {`identical`, `contains`, `contained_by`} | This root and that user's root are the same directory, or nested. | Panel, log line, `indexer_runs.error` and the assignment refusal, all from `operator_text` / `assignment_conflict_message`; marker `vault_root_overlap`. |
| `RootUnexaminable(cause)`, `cause` an `errno`, `CAUSE_TIMEOUT` or `CAUSE_UNSTABLE` | The root could not be examined, so **no overlap could be ruled out**. Not an overlap, and it **names no peer**, because none was observed. | Same surfaces, own wording via `cause_text`; marker `vault_root_unexaminable`. |

- **Calling an unopenable root an "overlap" would send an operator looking for
  a second account that does not exist.** The two are worded separately in the
  panel, the log line, the `indexer_runs` row *and* the `usage_logs` marker,
  for the same reason this codebase already keeps three vault markers apart.
- **`CAUSE_UNSTABLE` is the third cause, and it is a "could not look" verdict
  too.** The root opened, but `os.stat(realpath)` did not report the inode the
  descriptor holds — the pathname moved between the two calls, so neither fact
  describes one directory. Reporting a relation computed from half of that
  would be a check over a directory nobody looked at.
- **Recording the facts rather than resolving names at render time is the
  load-bearing half.** The operator's first move on reading "vault root
  overlaps <peer>" is to *edit or delete* one of the two accounts; the moment
  they do, and until the next detection publishes, a render-time resolution
  shows a changed path — or a blank where the deleted peer was — beside a
  condition still in force. Recording them also makes the staleness honest: the
  surfaces label them "as at last check", so a name that no longer exists is
  legible as a fact about the past.
- `RootUnexaminable` quarantines **only that user**. The peers it could not be
  compared against keep serving: fail closed for the user whose status cannot
  be established, fail open for users against whom nothing was observed — one
  broken mount must not take the deployment offline. The cost is limitation
  **L2**, and it is accepted with its eyes open.

### Why detection is not in `_vault_root`, and what the gate reads instead

`_vault_root` must stay a pure cache lookup — `APIKeyMiddleware` warms the
cache on *every* authenticated MCP request, and that warm is what makes a cache
read correct. Detection is worse than a query: it needs every other user's
assignment (a query), an `open` + `fstat` + `realpath` per root dispatched to a
worker thread under a deadline, on the hot path, per call. It cannot live here.

**What the gate does instead is read the published snapshot.**
`_refuse_quarantined_root` is one attribute read plus a dict lookup: no
session, no statement, no syscall, no mount table. **This is the one exception
to "`_vault_root` is a pure cache lookup", and it is not licence to add a
query.**

- It is **refuse-only**: it can make the gate stricter and can never admit a
  caller the rest of the gate would refuse. That is exactly why it is safe to
  consult it *first*, ahead of the request's immutable vault-root snapshot.
  Unlike an assignment — where a stale read must never re-admit a revoked
  caller, which is why the request snapshot outranks the process-global cache —
  **a quarantine has no direction in which staleness admits anyone.**
- It is **never consulted for `user_id is None`**. Single-user mode has one
  root and no second assignment, so there is nothing to detect and nothing to
  be ready for; a pass and a tool call there behave exactly as they did before
  this guard existed. And `_detect` short-circuits to an empty snapshot on
  `not settings.multi_user_mode` **before the enumeration query and before any
  `open`** — not after, which is where the check used to sit. A single-user
  deployment whose `users` table still holds rows (a flag flipped back, a
  deployment that was multi-user once) would otherwise issue that query and
  then observe every root in it, once per pass entry point, to build a snapshot
  nothing in that mode reads. It still *publishes*: the never-published state
  is a refusal, and single-user mode must never sit in it.
- `VaultRootOverlap`, `VaultRootUnexaminable` and `VaultRootNotReady` all
  subclass **`RuntimeError`**, which is load-bearing: every existing
  `except RuntimeError` around `_vault_root` keeps failing closed unchanged,
  and a tool added later inherits the refusal by being registered.
  `_vault_admission_error` adds three branches **ahead of** the generic
  `RuntimeError` one to select the message and the marker; an ordering slip
  would silently file every quarantine under `no_vault_assigned`, which is why
  a test reads the source for that ordering.
- **The agent-facing wordings name no other user, no other vault path and no
  note path, for any reason** — the caller of a tool is a tenant's agent.
  `tools._QUARANTINE_REFUSALS` reads the message **from its table, not from the
  exception instance**, so a future `raise VaultRootOverlap(f"…{peer}")` added
  for a richer operator log cannot push a peer's name out to a tenant's agent.
  The refusal travels on `_MarkedRefusal`, a `str` subclass carrying the marker,
  so `_tracked` files it under the right value while every caller that treats a
  refusal as a plain string keeps working. The refusal is also emitted as the
  `tool_refused_vault_quarantined` security event, under the same permit
  discipline as `tool_refused_no_vault` and carrying only `user_id`, `tool` and
  the reason — see
  [security-event-logging.md](security-event-logging.md). The operator surfaces
  name everything.

### Disposition: index refusal *and* tool refusal, with nothing deleted

Index refusal alone was considered and is insufficient. It stops *new* foreign
rows entering the index and does nothing about the ones a previous pass already
wrote, which stay queryable by the outer tenant through `semantic_search` /
`keyword_search` / the graph tools — none of which touch the disk. And it does
nothing at all about writes: `edit_note`, `move_note`, `delete_note` and
`write_file` resolve beneath the outer tenant's root, the inner tenant's files
*are* beneath it, `RESOLVE_BENEATH` agrees, and the write path never consults
the indexer. **A destructive write that clobbers a note has actually happened
on this server**, so refusing to index while leaving the clobber reachable
would aim the control at the cheaper of the two failures.

| Surface | How it refuses |
| --- | --- |
| Every MCP tool | `_vault_root` raises → `_tracked`'s admission gate refuses before the body runs. Total by construction, exactly as #66 made it; a tool added later inherits it. |
| Every pass stage | `_refuse_quarantined_pass` in the four shared pass helpers, ahead of `_vault_root` and the pinned root, raising `indexer.VaultRootQuarantined` with the **operator**-facing wording. |
| Transfer redemption | `transfer.owner_quarantined` on all three owner re-reads — `resolve_root_ok` (the unlocked entry check, upload *and* download), `locked_rows_ok` (the locked publish path, before the link or rename, so nothing is published) and `_identity_publish_ok` (the `import_from_url` gate). |
| Panel vault browser | `vault_page` calls the gate's own `_refuse_quarantined_root` and renders the existing `vault_error` empty state. |
| Users list | The quarantined state replaces the note count — see [control-panel.md](control-panel.md). |

**A capability is a delayed write, which is why the transfer gate is not
optional.** A token minted before the quarantine pins a `vault_root` that is
still, byte for byte, the owner's current assignment, so every existing
predicate agrees and the redemption would proceed into a directory the server
has just determined is shared. The public `/transfer/*` routes carry no OAuth
chain and never call `_vault_root`, so the snapshot test goes into the
redemption gate that already re-reads the owner row and already refuses an
inactive owner or a changed root. **The response does not change**:
`_not_found()` stays byte-identical for every cause — the uniform 404 is the
anti-oracle the whole surface rests on — and only the server-side reason
differs, `owner_quarantined` or `root_unverified`, on the bounded
`transfer_refused` event. Nothing is logged from inside the predicate: an
unauthenticated caller replaying one dead capability drives that branch as fast
as it can open connections.

**Nothing is deleted.** Preserving the rows is what makes a corrected
assignment cheap — a full re-embed is the alternative — and a blanket delete
would be a second, unreviewed deletion path over index contents, over an index
that may hold the *other* tenant's rows. The operator's correction triggers the
existing repair: a changed assignment or realpath drives `classify_provenance`
to discard or re-derive, and rows for files no longer beneath the root go in
the ordinary prune. **Unrelated tenants are untouched** — every active user the
snapshot does not name is indexed and served normally in the same pass.

### Accepted limitations

Every residual is listed, so none is discovered later as a defect. **L1 and L2
are the two with a destructive consequence**, and both go to the follow-up
issue `vault-root-mount-graft-detection`; the owner decision on them is
**pending**.

- **L1 — a bind mount that grafts a peer's vault, or any mount nested inside
  it, to a path *inside* another tenant's root is not detected.**
  `mount --bind /vaults/b /vaults/a/inner` leaves both root inodes distinct and
  both canonical real paths outside each other, so neither check sees it.
  *The consequence, written out:* user A's `edit_note`, `move_note`,
  `delete_note` and `write_file` resolve beneath A's root, `RESOLVE_BENEATH`
  agrees the grafted path is contained, and **A can therefore read, overwrite
  and delete every note in B's vault**; A's index pass files B's notes under
  A's `user_id`, so `semantic_search` and `keyword_search` return B's content
  to A's agent. Both quarantine mechanisms stay silent, because neither ever
  names A or B. Accepted because a mount-table check was specified and removed
  after three rounds each produced a new configuration it failed to cover, the
  sound answer inside this namespace is a full-tree walk per pair per pass, and
  the condition requires an administrator to write a bind mount into the deploy
  configuration — a strictly higher bar than the hand-typed nested path #199
  was filed on.
- **L2 — a peer whose root aliases an *unexaminable* assignment cannot be
  related to it and stays served.** When A's root cannot be opened, A is
  quarantined under `root_unexaminable`; if B's root is an accessible alias of
  the same directory, B is compared against nothing that could establish the
  relation and **keeps serving, with full access to the shared vault** — the
  same read/overwrite/delete consequence as L1. Same class, same follow-up. The
  narrower alternative — quarantining every peer whenever any root is
  unexaminable — was rejected because it lets one broken mount take the whole
  deployment offline, the false-positive direction this codebase treats as the
  expensive failure.
- **L3 — both checks are point-in-time.** Between two entry points a root can
  be aliased and un-aliased with no record. The window is one index interval
  (300 s); the assignment-time check closes the administrator-initiated case
  entirely; continuous detection needs an inotify/fanotify watch on paths the
  container may not be able to watch.
- **L4 — the observation deadline abandons the wait, not the syscall.** A
  thread blocked opening a hung mount stays parked, so a pathological mount
  accumulates one thread per detection. It holds no lock and no pooled
  connection, and without the deadline the synchronous first detection would
  not return and the panel would be unavailable during exactly the incident an
  operator opens it for.
- **L5 — the guard refuses the configuration; it does not un-index what a
  previous configuration indexed.** Rows the outer tenant's pass already wrote
  for the inner tenant's notes remain until a corrected assignment drives
  `classify_provenance` to discard/re-derive or the ordinary prune removes
  them. They are unreachable while the quarantine stands, because the admission
  gate is total.
- **L6 — the snapshot is process-global.** Under multiple uvicorn workers a
  worker converges only at its next entry point. Same boundary as
  `_user_vault_cache`, `clear_user_vault_cache` and the ops-health ring buffer;
  single-process today, and each worker's lifespan publishes before it serves.
- **L7 — a direct `UPDATE users SET vault_path` in psql bypasses enforcement
  point 1 entirely.** Enforcement point 2 covers it at the next entry point.
  Nothing in the application can gate a statement issued outside it.
- **L8 — `root_unexaminable` refuses the database-backed tools too**, which a
  missing mount does not do today: a mount blip that used to fail only the
  disk-touching tools now refuses all of them for up to one interval. Fail
  closed on "could not rule out an overlap"; the refusal is loud, named and
  self-clearing.
- **L9 — the panel's `/vaults/*` dropdown is unchanged.** It still offers only
  top-level directories and does not pre-filter conflicting ones; the refusal
  on submit is the enforcement. A filtered dropdown that silently omits a
  directory is less legible than a refusal that names the conflicting user.

## The read path's owner predicate is total

`apply_note_filters(user_id=None)` used to append **no** owner predicate while
every write path maps `None` to `user_id IS NULL`. `MULTI_USER_MODE` can be
turned off after users exist, so a database holding named users' rows read by
an ownerless credential handed over every tenant's paths, titles, tags,
frontmatter and chunk excerpts (#127). `None` is now a scoping value — `IS
NULL` — and every index-backed tool is swept to it: `keyword_search`,
`semantic_search`, `list_notes`, `get_recent`, **`get_tags`**, `get_backlinks`,
`get_links`, `get_neighborhood`, `find_related`, `find_orphans`. A single-user
deployment sees no change; every row there is NULL-owned.

- **`note_links` carries no `user_id`, so ownership rides the endpoint rows —
  and *where* it rides decides two different things.** In a JOIN's ON clause a
  cross-owner target simply fails to resolve; as a WHERE on the joined row it
  would discard every *dangling* link too, which is what `get_links` exists to
  report. `_owner_predicate_for(entity, uid)` exists so an alias can carry it.
- **An edge admitted into the neighborhood BFS or the orphan calculus changes
  what the answer *is*.** It occupies a slot against `limit`, it can bridge two
  owned notes through a row the caller cannot see, and on the target side it
  silently strips an owned note's orphan status — so both endpoints must be
  inside the owned set at *traversal* time, never at hydration time. An edge
  counts for `find_orphans` only when its source is owned and its target is
  either owned or genuinely dangling (dangling still means "not an orphan",
  unchanged, and unrelated to ownership).
- **`get_links` classifies by what the scoped join resolved**, not by the raw
  `note_links.target_note_id`, and omits a row that names a target outside the
  owned set — that row is not dangling, and printing it would print the other
  owner's path. Unreachable in normal operation (link resolution is per user),
  which is why it is refused rather than assumed away.
- The owner predicate counts as a filter for the exact fallback — see "Filtered
  vector search" in [search.md](search.md).

## Publication confirms the vault root, and the residual is declared

`APIKeyMiddleware` binds `current_vault_root` once, at admission, and that
snapshot is immutable by design — it is what makes #66's gate fail closed under
a concurrent bulk cache warm. The cost is that it is *stale by design* for the
whole of a request: an administrator can reassign, the panel can report it
complete, and a write already in flight still publishes into the former root.
So every mutating tool re-reads `users.vault_path` / `is_active` immediately
before **each** publication and refuses on change (#88). The answer is
deliberately not a lock: holding the credential and user rows `FOR UPDATE`
across `move_note`'s link rewrites would put arbitrary vault I/O inside a lock
every authenticated request contends for. The transfer routes keep their
stronger locked gate; `import_from_url` and `request_upload` are the two
allow-listed exemptions.

- **The residual is stated, not implied.** The window shrinks from a whole
  request to staging, the durability flush and one publishing call. A
  reassignment committing inside *that* still lands in the former root and the
  tool reports success — the same optimistic level as `edit_note(expected=…)`
  and the transfer fingerprint check. `move_note(rewrite_links=True)` has
  several such windows, one per publication, and can be refused part way
  through; "one window per tool call" would be false for it and must not be
  claimed.
- **There is no retainable confirmation.** `vault._confirm_vault_assignment`
  is private and the only entry point is `vault.confirmed_publication(user_id,
  publish)`, which awaits the read and calls a **synchronous** `publish` before
  returning control — so no caller-visible `await` can sit between the two.
  Coroutine, generator *and* async-generator callbacks are refused, and so is a
  returned coroutine/generator/awaitable (a callable object whose `__call__` is
  a generator is none of the first three). Nothing is `close()`d on the way
  out: that is arbitrary code of a stranger's choosing, and the lease below has
  already made the object inert.
- **The confirmation is leased for the callback's dynamic extent, and that is
  the part that bounds *when*.** `_leased` activates it, and a `finally`
  revokes it on every exit — normal return, exception, or a callback that
  stashed the object. `consume` refuses an unleased confirmation, and
  `confirmed_publication` refuses a callback that returned without consuming
  one. Single-consumption alone was **not** enough and must not be relied on
  again: it bounds how many times a confirmation is used and says nothing about
  when, so `lambda c: saved.append(c)` followed by a reassignment and a later
  `write_file_at(..., confirmation=saved[0])` was obeyed.
- **`RootConfirmation` is also single-consumption and target-bound.** The spent
  flag lives on the confirmation, not on a slot in the target, so one object
  cannot be spent by two publications however it is attached; and `consume`
  checks the acting user id and the canonical assignment against
  `MutableTarget.user_id` / `.assignment`. Every publish helper
  (`_atomic_write_at`, `move_file_no_clobber`, `soft_delete_target`,
  `unlink_at`) takes one or refuses with `UnconfirmedPublication` — a
  programming error, deliberately not a `RuntimeError`, because the tool bodies
  catch `RuntimeError` around their publishes and would render it as a failed
  write.
- **A rollback rides the confirmation it undoes, through a `MovePermit` that
  cannot be forged.** The forward `move_file_no_clobber` issues it — nobody
  else can, `__init__` requires the module-private `_PERMIT_ISSUE` token — and
  it is bound to that confirmation's *lease*, so it is inert the moment
  `confirmed_publication` returns, plus the immutable
  `(user_id, assignment, rel)` of each end and object identity. One use,
  reverse direction only. Two earlier shapes were wrong: stamping the one
  confirmation onto both endpoints made a reusable token of a single-use fact,
  and a public `MovePermit(destination, source)` constructor authorised a
  rename with no confirmation at all.
- **Both ends of a move must be one caller, one assignment, one root inode.**
  `rename_noreplace` removes the source entry as surely as it creates the
  destination one, yet only the destination's confirmation is consumed, so
  `_require_one_vault` compares `user_id`, `assignment` and `fstat` of each
  pinned `root_fd` (a pathname comparison is not enough — two assignments can
  spell the same string over different directories) before anything is spent,
  on the forward move and on the rollback. Unreachable from `move_note`, which
  opens both ends with one `uid`; checked at the primitive because the next
  caller may not.
- **Three distinct error markers, because they say different things.**
  `no_vault_assigned` (admission: this credential had no vault this call),
  `vault_assignment_changed` (an administrator moved it — `VaultAssignmentChanged`),
  and `vault_confirmation_unavailable` (the read *failed*;
  `VaultConfirmationUnavailable`, not a `RuntimeError`, so no tool body renders
  it as a bad write). An outage recorded under the reassignment marker puts an
  administrator's name on an infrastructure incident. Before a call's first
  publication an outage propagates and the call fails; after `move_note`'s move
  has stood it is caught, the remaining rewrites stop, and the partial outcome
  is reported through the existing `failed_rewrite_sources` idiom — naming it
  an outage, never a reassignment.
- **`delete_file` holds no `MutableTarget`**, so its confirmation is consumed
  against the `(uid, root)` its own `_vault_context` resolved, and the whole
  delete — trash probe included — runs inside the confirmed step.

## The index records the vault it was scanned under

`users.indexed_vault_assignment` / `indexed_vault_realpath` /
`indexed_vault_handle` (migration 016, all nullable, marker-owned, **no
backfill**) record the root a pass actually scanned, so a reassignment stops
`semantic_search`/`keyword_search`/`list_notes`/the graph tools answering from
a vault the caller no longer has (#91). `classify_provenance` is the one
function that computes the verdict, over six rows: **indeterminate** (root
unpinnable, or its realpath no longer names the pinned inode) → nothing, and
the pass fails; **re-derive** (no record, a half-set record, exactly one fact
differing, or a handle contradicting an otherwise-matching pair); **keep**
(both agree); **discard** (both differ). A handle can refuse a keep, never
establish one, and never establish a discard. Ambiguity never resolves toward
keeping — silently wrong search results are the failure this product ranks
highest — and never toward discarding, which costs a full re-embed.

- **Not backfilling is the load-bearing decision.** Deriving the assignment
  from `users.vault_path` would assert that an assigned user's index was built
  under what it carries *now*, which is exactly the reassignment lag the record
  exists to detect. NULL means "provenance unknown", the only true statement at
  migration time, and such a user is repaired by re-deriving rather than
  discarding — so introducing the record costs no vault-wide re-embed. It is
  also what makes the deploy order safe with no cross-container coordination:
  the previous code cannot write these columns.
- **The whole pass runs beneath one pinned root descriptor**, so the facts
  observed, the files discovered and the bytes read come from one inode.
  `indexed_vault_realpath` stores `os.fsencode(realpath).hex()` — a pathname is
  arbitrary non-NUL bytes, and a surrogate escape would fail to encode inside
  the one transaction that must not roll back.
- **A discard is bound to the assignment that produced it.** The verdict is
  computed in an earlier transaction against a cached root, so the discard
  transaction takes the `users` row `SELECT … FOR UPDATE`, re-reads it, and
  requires present/active/assigned/*equal to `facts.assignment`* before
  deleting anything; the stamp beside it must affect exactly one row or the
  whole transaction rolls back. Without that, an administrator correcting a
  reassignment back destroys a complete, valid index and records provenance for
  a root nobody is assigned to. The re-derive's tail stamp takes the same lock
  and the same re-read, and is *withheld* on disagreement rather than fatal —
  it destroys nothing.
- **The two take that lock differently, and it is lock ordering, not tuning.**
  The discard has its own transaction and locks the parent *before* any child
  write — the panel's own user-delete direction — so it may wait. The tail
  stamp runs at the end of the pass's transaction, already holding
  `notes_metadata` row locks, while a permanent user delete locks `users` first
  and then cascades onto exactly those rows: waiting there is a real deadlock
  cycle, and Postgres would abort one side — possibly the operator's delete. So
  the tail asks `FOR UPDATE NOWAIT` **inside `session.begin_nested()`** and
  treats `55P03` as a withheld stamp (a state that branch already knows). The
  savepoint is required, not tidy: a failed statement poisons its transaction,
  so without one the pass would lose every repair along with the stamp.
  `_is_lock_not_available` walks `.orig` *and* `__cause__` — the SQLSTATE lives
  on asyncpg's own error, two layers down, exactly as `_log_usage`'s FK
  recovery has to walk.
- **A re-derive that skipped anything records nothing.** Any per-file skip —
  including both link-extraction skips, the missing buffered body and the
  missing index row — withholds the stamp, because the record's whole claim is
  that every surviving row was written by that pass.
- **`embed_vault` is deliberately ungated on provenance, because it verifies.**
  Gating it composed with the completeness rule into indefinite staleness: one
  permanently unreadable file withholds the record forever and would then
  freeze every other note's vectors at content it no longer has, while
  `semantic_search` kept returning them. Running ungated is sound only because
  the pass refuses bytes that do not hash to the selected row's `content_hash`
  — an embedding is a pure function of content, so which directory supplied the
  bytes is not a fact the vector depends on. **Removing the verification means
  re-gating the pass in the same change.**
- **Verifying the bytes is not enough on its own.** The ORM re-read that
  follows can see a hash another pass has committed (H2) while the vectors were
  built from H1; stamping H2 marked the row embedded for content it does not
  have, and H2 == H2 then blocked every later repair. `embed_note` therefore
  takes `certified_hash`/`certified_path` and stamps them with a conditional,
  row-locking `UPDATE … WHERE id AND file_path AND content_hash = H1` **before**
  it replaces a vector and **after** the provider call — so no row lock is held
  across a network request, and a row that moved matches nothing.
  `StaleCertification` rolls the note back and leaves it unmarked.
- **The exclusion branch certifies through the same predicate.** It reads no
  file, but it deletes a note's vectors and marks the row embedded, which is
  the same claim — and a move is exactly what it cannot see, because relocating
  a note changes `file_path` and not `content_hash`. Stamping by `id` alone let
  a decision about `Private/A.md` delete the vectors of a row that had become
  `Public/A.md` and record it as embedded with none: included, hash-equal, and
  therefore never selected again — silently and permanently absent from
  `semantic_search`. `certify_embedded` is shared by both paths, stamps before
  the delete (the conditional UPDATE is what takes the row lock), and takes
  `note_id` plus an explicit `expire_on` because the exclusion branch certifies
  from a plain result row no session maps.
- **A path change clears `embedded_content_hash`, at every statement that
  changes `file_path`.** The predicate above closes only *move-before-certify*;
  the mirror ordering is invisible to it, because when the move lands after a
  correct certification the stamp is already there and already true of the
  content. It is no longer true of the *decision*: the stamp says the row's
  current content has been dealt with and nothing about **how**, and the
  exclusion branch decides how by matching `EMBEDDING_EXCLUDE_PATTERNS` against
  the path. Carried across a move it freezes the old answer for ever — the pass
  selects on `embedded_content_hash != content_hash`, which a preserved stamp
  makes false. Out of an excluded folder: included, zero vectors, never
  selected again, silently missing from `semantic_search`. Into one: still
  searchable while excluded. So `move_note`'s metadata UPDATE and the indexer's
  **id-preserving** move detection both `SET embedded_content_hash = NULL`
  (the prune-and-insert path is unaffected — its replacement row starts null).
  NULL means *re-evaluate next pass*, not *not embedded*. **Do not "improve"
  this by consulting the exclusion config at move time**: the config can change
  before the next pass, so that is the same frozen answer in a new place, and
  it would give the move path a dependency on embedding configuration it has no
  other reason to know.

