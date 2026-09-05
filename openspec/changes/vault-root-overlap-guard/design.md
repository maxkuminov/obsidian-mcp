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
| A2 | `src/auth/routes.py::register_submit` (~229-300) | Bootstrap of the first admin, under `_BOOTSTRAP_LOCK_KEY`, guarded by `_users_table_empty`. Runs only when `users` holds **zero rows**, so there is no other assignment to overlap with and the check would be vacuous. Deliberately not added — a check that can never fire invites a future reader to believe this path is covered when the invariant, not the code, is what covers it. |
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

R5, R6 and R11 are enforcement point 2's home. R1/R4, R8 and R9 are the three
surfaces the quarantine has to reach.

## Goals / Non-Goals

**Goals.** Refuse an overlapping assignment at the moment it is made; detect an
overlap that appears *after* an assignment and fail the affected pair closed;
tell the operator, durably, which two users and which two roots.

**Non-Goals.** No general filesystem-layout policing (a vault that spans mounts
is fine for one tenant and stays fine); no `RESOLVE_NO_XDEV`; no migration; no
persisted quarantine; no attempt to *repair* an index that a previous
overlapping configuration already polluted beyond what the ordinary prune and
`classify_provenance` already do; no change to single-user mode.

## The two checks, and what each proves

Both are taken from **one opened directory descriptor per root**, in one moment,
the way `observe_root_facts` already binds a realpath to an `fstat` — never from
the `vault_path` string alone.

| Check | Proves | Does **not** prove |
| --- | --- | --- |
| **Identity.** `os.fstat(fd_a)[st_dev, st_ino] == os.fstat(fd_b)[st_dev, st_ino]`, where each `fd` is `os.open(root, O_RDONLY \| O_DIRECTORY \| O_CLOEXEC)`. | The two assignments name **one directory object** at this instant: same superblock, same inode. Every entry reachable under one is the same entry under the other. This is what catches a symlink alias, a same-filesystem bind mount of one directory to two pathnames, and a directory hard link where the kernel permits one. | **Nothing about containment.** Two distinct inodes nest all the time — that is the ancestor/descendant case, and identity is blind to it. **Equal `st_dev` is not proof of one mount**: a bind mount of a subtree of the same filesystem reports the same `st_dev` while being a different mount, which is exactly why transfer publication uses `statx`'s `STATX_MNT_ID` and `_check_mount_identity_support` warns rather than comparing `st_dev` (`src/main.py:167`). **Unequal `st_dev` is not proof of unrelated directories**: a separate filesystem mounted *inside* another tenant's root gives `/vaults/a` and `/vaults/a/sub` different devices and total overlap. Different `st_dev` says "a `rename(2)` between these two would fail `EXDEV`" — it says nothing about who can read whom. |
| **Containment.** Component-wise prefix test over `os.path.realpath(root)` in **both** directions: `realpath(a)` is an ancestor of `realpath(b)`, or the reverse. Compared on path components (`PurePosixPath.is_relative_to` / a `parts` prefix), never on the raw string — `/vaults/team` is not an ancestor of `/vaults/team-2`, and a string prefix test would say it is. | One root's **canonical pathname lies inside** the other's, right now, in this container's namespace. `realpath` resolves every component, so an ancestor reached through a symlink (`/vaults/b -> /vaults/a/inner`) canonicalises to the containing form and is caught. This is the complement to identity: identity answers "same object", containment answers "one inside the other", and neither implies the other. | It is a **pathname** fact about **this** mount namespace at **this** instant. It cannot see a future mount or symlink (which is why it is re-run every pass, not only at assignment). And it cannot see an overlap that neither canonical name expresses: `mount --bind /vaults/b /vaults/a/inner` leaves `realpath(/vaults/b) == "/vaults/b"` and the two root inodes distinct, while A's tree now contains B's entire vault. See **Accepted limitations**. |

Equality of the two normalised assignment strings — today's whole check — is the
degenerate case of both: it implies identity (the same pathname opens the same
inode) and it is the zero-length case of containment. It is kept only so its
wording, which operators have seen, survives.

## Enforcement point 1 — assignment time

In `edit_user_submit`, after `_validate_vault_path` returns a normalised path
and while the `_lock_admin_guard` advisory lock is still held, in the same
transaction that will write `users.vault_path`:

1. `SELECT id, username, vault_path FROM users WHERE is_active AND vault_path IS NOT NULL AND id != :target` — the same predicate `_check_vault_path_unique` uses today, plus the columns needed to name the conflict.
2. Open the candidate root and each peer root; run identity and containment for each pair.
3. First conflict wins; refuse with `_back_with_error`, naming the other user and which relation was found ("… is inside the vault of user 'bob'", "… is the same directory as the vault of user 'bob'"). Admin-facing, so naming is correct — the existing message already names the other user.

The lock is the existing `_ADMIN_GUARD_LOCK_KEY`; **no second key is
introduced**, because `panel-user-administration` already pins "one lock key for
both handlers" and two keys would not make an edit and a concurrent edit exclude
each other. Without the lock this is check-then-act: two admins assigning
`/vaults/team` and `/vaults/team/private` to two different users at the same
moment each read the other's *old* row and both pass.

**A peer root that cannot be opened refuses the assignment**, naming that root.
An unopenable peer makes the identity check unavailable, and identity is the
check that catches precisely what string equality already misses; admitting on
"we could not look" is the direction this codebase treats as the expensive
error. The precedent is already in the same function: `validate_vault_root_path`
refuses the *candidate* for exactly the missing-mount case, so an admin whose
compose mounts are not applied already cannot save. Filesystem work under the
advisory lock is bounded by the number of active assigned users — the count the
users page already renders — and by the same mounts that function already stats.

## Enforcement point 2 — every pass, and why not `_vault_root`

`_vault_root` must stay a pure cache lookup. The architecture note gives the
reason: `APIKeyMiddleware` warms the cache on *every* authenticated MCP request,
and that warm is what makes a cache read correct — a database query in the gate
is a query on every tool call. Overlap detection would be worse than a query:
it needs *every other user's* assignment (a query) **and** an `open` + `fstat` +
`realpath` per root (filesystem I/O), on the hot path, per call.

So detection runs where a pass already holds a session and already opens roots:
at the top of every indexer iteration, before `_active_user_ids()`'s list is
consumed, in both the startup block and the periodic tick of
`run_indexer_loop`. Startup is covered by virtue of the startup pass, so there
is one detector and one call shape rather than a second lifespan guard that
would have to be kept in step with it.

- It runs **before** the `_is_paused()` check. A pause exists to stop heavy and
  destructive passes; it must not silently un-quarantine anyone, and the
  detector writes nothing to the database.
- It is skipped in sandbox mode along with the indexer, which is correct:
  sandbox mode has no users and no auth.
- It produces `_overlapping_user_ids: frozenset[int]`, a process-global,
  replaced wholesale each pass.

**`_vault_root` consults that set directly, before the request snapshot, and
raises `VaultRootOverlap` (a `RuntimeError` subclass).** This is not a
relaxation of the "pure cache lookup" rule and the architecture note is updated
to say so precisely: the test is a frozenset membership check — no session, no
statement, no syscall — and it is **refuse-only**. It can make the gate stricter
and can never admit a caller the existing logic would refuse, which is why it is
safe to consult it *ahead* of the per-request snapshot: unlike an assignment,
which must be read from the immutable snapshot so a concurrent bulk warm cannot
re-admit a revoked user, a quarantine has no direction in which staleness
admits. Keeping it out of the cache's value domain also leaves
`warm_user_vault_cache`'s return type and the `current_vault_root` ContextVar
type untouched.

`VaultRootOverlap` subclasses `RuntimeError` so every existing
`except RuntimeError` around `_vault_root` keeps failing closed unchanged;
`_vault_admission_error` adds one branch ahead of the generic one to select a
distinct message and a distinct `usage_logs.params["error"]` marker,
`vault_root_overlap`. That follows the codebase's own rule that markers must not
be conflated — `no_vault_assigned`, `vault_assignment_changed` and
`vault_confirmation_unavailable` are already three markers because they name
three different things an operator would do three different things about.
Recording a quarantine as `no_vault_assigned` would tell the operator an
administrator unassigned the user while the users page shows a vault path
assigned: a contradiction, and one that points the investigation at the wrong
person.

The agent-facing message names **no other user and no other path** — the caller
is a tenant's agent. It says the vault is unavailable pending an operator fix.
The operator-facing surfaces name both users and both roots.

## Disposition for the affected tenants

**Quarantine = index refusal *and* tool refusal, for exactly the users in an
overlap relation, with no rows deleted.**

Index refusal alone was considered and is insufficient:

- It stops *new* foreign rows entering the index. It does not remove the ones a
  previous pass already wrote, and those stay queryable by the outer tenant
  through `semantic_search` / `keyword_search` / the graph tools, which never
  touch the disk.
- It does nothing at all about writes. `edit_note`, `move_note`, `delete_note`
  and `write_file` resolve beneath the outer tenant's root, and the inner
  tenant's files *are* beneath it; `RESOLVE_BENEATH` agrees. The write path
  never consults the indexer. Under this product's framing — a destructive write
  that clobbers a note has actually happened, and it is the expensive failure —
  refusing to index while leaving the clobber reachable would be a control aimed
  at the cheaper of the two failures.

So tool refusal is not belt-and-braces; it is the only control that addresses
the ranked failure. It reaches all three consuming surfaces:

| Surface | How |
| --- | --- |
| Every MCP tool | `_vault_root` raises → `_tracked`'s admission gate refuses. Total by construction, exactly as #66 made it: nothing is exempt, and a tool added later inherits it. |
| Transfer redemption | A capability minted before the overlap appeared carries a pinned `vault_root` and is redeemed on the public `/transfer/*` routes, which never call `_vault_root`. The redemption gate in `src/services/transfer.py` already re-reads the owner row (unlocked at ~876/~1107, locked at ~935) and already refuses an inactive owner or a changed root — the membership test goes there, at a point that already fails closed. |
| Panel vault browser | `vault_page` renders the existing `vault_error` empty state for a quarantined user, instead of browsing a tree that contains another tenant's notes. |

**Nothing is deleted.** The same reasoning as #66's: preserving the index rows
is what makes a corrected assignment cheap, and the alternative here is worse —
the outer tenant's index may hold the inner tenant's rows, and a blanket delete
would be a second, unreviewed deletion path over index contents. Once the
operator corrects the assignment, the existing machinery does the repair: a
changed assignment or realpath drives `classify_provenance` to *discard* or
*re-derive*, and rows for files no longer beneath the root are removed by the
ordinary scan prune.

**Unrelated tenants are untouched.** The quarantine set is exactly the users
standing in an overlap relation with at least one other user. Every other active
user is indexed and served normally in the same pass — the same isolation
`_index_pass_once` already gives a user with a broken vault.

**The refusal is recorded twice, on purpose.** At ERROR, so the ops-health ring
buffer catches it and the health page shows it; and in the affected users'
`indexer_runs.error`, because the ring buffer is 100 entries and
process-lifetime while the misconfiguration persists across restarts — the same
argument that made `notes_metadata.links_truncated` a column rather than a log
line (#203).

## Single-user mode

`multi_user_mode = False` means `current_user_id` is None, `_vault_root(None)`
answers from `settings.vault_path`, `_active_user_ids()` returns `[]` and the
pass runs with `user_id=None`. There is exactly one root and no second
assignment, so the detector has nothing to compare and produces an empty set;
the gate's membership test is never reached for `user_id is None`. Sandbox mode
is the same. **Neither the assignment check nor the quarantine changes any
single-user behaviour**, and the specs say so as a scenario rather than leaving
it to be inferred.

The one multi-user case that touches `settings.vault_path` is the legacy
`/obsidian` mount, which `validate_vault_root_path` admits for one user. It is
an ordinary root to both checks: if a second user is assigned a `/vaults/`
directory that is a bind mount of `/obsidian`, identity catches it — which is
precisely the case string equality could not see.

## Rejected alternatives

1. **Enforce in `_vault_root`.** Rejected: it is the admission gate on the hot
   path of every tool call and the architecture note forbids a database query
   there; overlap detection needs a query *and* filesystem I/O. The refuse-only
   frozenset test is what is left after removing both.
2. **A database column (`users.vault_root_quarantined`) written by the pass.**
   Rejected: a persisted quarantine is a second source of truth about a
   filesystem that keeps moving. It can outlive the condition (an operator fixes
   the mount, the column still refuses) or lag it, and it needs a migration to
   express a fact that is recomputed every five minutes anyway.
3. **Turn on `RESOLVE_NO_XDEV`, or check `STATX_MNT_ID` per directory during the
   walk.** Rejected: it would catch the bind-mount-inside-a-root residual, and
   it would also refuse a legitimate single-tenant vault that spans mounts —
   which the docs currently, correctly, treat as fine. It converts a two-tenant
   misconfiguration into a one-tenant outage, the false-positive direction this
   codebase treats as the expensive failure (`_check_mount_identity_support`
   reasons the same way).
4. **Compare `st_dev` alone to decide "same filesystem, therefore related".**
   Rejected as unsound in both directions — see the table above. `st_dev` is not
   a mount identity and is not a containment relation.
5. **Refuse only the *newer* assignment and leave the older tenant serving.**
   Rejected: "newer" is not a fact the system holds (`users` has no
   `vault_path_changed_at`), and the outer tenant is usually the older one and
   is precisely the one whose tools can clobber the inner tenant's files.
6. **Walk one root looking for the other's inode.** Rejected: it is a full-tree
   walk per pair per pass, on vaults of thousands of files, to catch a residual
   that a bind mount can still hide from it.
7. **Detect at startup only.** Rejected: a symlink created at 09:00 would go
   undetected until the next container restart. The pass already runs every five
   minutes and already opens every root.

## Risks / Trade-offs

- **Filesystem work under the admin advisory lock.** Bounded by the number of
  active assigned users; the same handler already stats the candidate root
  before taking the lock, so a hung mount already hangs it. No new class of
  risk, and the alternative (observe outside the lock, decide inside) is
  check-then-act.
- **A false positive quarantines two live tenants.** Both checks are structural
  and deterministic — inode identity and a canonical path prefix — not
  heuristics with thresholds, so a false positive requires the two roots to
  genuinely be the same directory or genuinely nested. Production's two roots
  are siblings under `/vaults/`, so the expected live result is an empty set;
  the deploy check is exactly that.
- **A pass that cannot open a root** is already the `indeterminate` provenance
  verdict ("nothing at all, and the pass fails") for that user. The detector
  inherits it: a root it cannot open makes that user's overlap status unknown,
  and unknown quarantines that user only — it does not quarantine the peers it
  could not compare against, because that would let one broken mount take every
  tenant down.
- **Multi-worker.** The quarantine set is process-global, like
  `_user_vault_cache` and the error ring buffer. Under a single-process uvicorn
  (today) that is the whole server; under workers, each converges within one
  index interval. Recorded below rather than solved, consistent with the
  existing note on `clear_user_vault_cache`.

## Accepted limitations

| # | Limitation | Why it is accepted |
| --- | --- | --- |
| L1 | A **bind mount of one tenant's root to a path inside another tenant's tree** (`mount --bind /vaults/b /vaults/a/inner`) is caught by neither check: both root inodes stay distinct and both canonical pathnames stay outside each other. | Catching it needs either a full-tree walk per pair per pass or `RESOLVE_NO_XDEV` / a per-directory mount-id check, and the latter breaks a legitimate single-tenant vault that spans mounts (rejected alternative 3). Documented beside the README's existing symlink paragraph so the residual is stated, not implied. |
| L2 | Both checks are **point-in-time**. Between two passes, a root can be aliased and un-aliased with no record. | The window is one index interval (300 s), and the assignment-time check closes the administrator-initiated case entirely. Continuous detection would need an inotify/fanotify watch on paths the container may not be able to watch. |
| L3 | The guard **refuses the configuration; it does not un-index what a previous configuration indexed.** Rows the outer tenant's pass already wrote for the inner tenant's notes remain in `notes_metadata` until a corrected assignment drives `classify_provenance` to discard/re-derive or the ordinary prune removes them. | They are unreachable while the quarantine stands (the admission gate is total), and a blanket delete is a second deletion path over index contents with a worse failure mode than the one it fixes. |
| L4 | The quarantine set is **process-global**, so under multiple uvicorn workers a worker converges only at its next pass. | Same boundary as `_user_vault_cache`, `clear_user_vault_cache` and the ops-health ring buffer; single-process today. |
| L5 | A **direct `UPDATE users SET vault_path`** in psql bypasses enforcement point 1 entirely. | Enforcement point 2 is what covers it, at the next pass. Nothing in the application can gate a statement issued outside it. |
| L6 | An overlap involving a user whose root **cannot be opened** is reported as indeterminate and quarantines that user alone; the peers it could not be compared against keep serving. | Fail-closed for the user we cannot clear, fail-open for users we have no evidence against — the alternative lets one missing mount take the whole deployment offline. |
| L7 | The panel's **`/vaults/*` dropdown is unchanged**; it still offers only top-level directories and does not pre-filter ones that would conflict. | The refusal on submit is the enforcement; a filtered dropdown that silently omits a directory is less legible than a refusal that names the conflicting user. |

## Owner decisions

Defaults chosen and applied throughout the specs; each can be reversed without
touching the rest of the design.

1. **Disposition = index refusal *and* tool refusal (fail closed for the pair).**
   *Default taken.* Alternative: index refusal only. Rejected because the write
   path never consults the indexer, so index-only leaves the destructive
   cross-tenant write — the ranked failure — fully reachable.
2. **A distinct refusal marker `vault_root_overlap` and a distinct
   `VaultRootOverlap` exception, rather than reusing `no_vault_assigned`.**
   *Default taken.* Costs one exception class and one branch in
   `_vault_admission_error`. Alternative: reuse the existing marker (cheaper,
   zero new types) — rejected because it tells the operator an administrator
   unassigned a user whose users page shows an assignment.
3. **`_vault_root` consults the quarantine set directly (refuse-only,
   ahead of the request snapshot)** rather than the quarantine riding in the
   cache's value domain. *Default taken.* Keeps `warm_user_vault_cache`'s return
   type and the ContextVar type unchanged, and cannot be re-admitted by a stale
   bulk warm.
4. **A peer root that cannot be opened refuses the *assignment*.** *Default
   taken.* Alternative: fall back to the containment test alone for an
   unexaminable peer. Rejected because identity is the check that catches what
   string equality already misses.
5. **A root that cannot be opened during a *pass* quarantines that user only,
   not its peers.** *Default taken.* Deliberately asymmetric with decision 4:
   an assignment is a discrete administrator action that can simply be retried,
   while a pass runs unattended every five minutes and must not turn one broken
   mount into a deployment-wide outage.
6. **Detection runs at the top of each indexer iteration, before the pause
   check, and startup is covered by the startup pass** — no separate lifespan
   guard. *Default taken.*
7. **The tool-facing refusal names no other user or path; the panel and the
   assignment refusal do.** *Default taken.* The tool caller is a tenant's
   agent; the panel reader is an administrator.
8. **`_check_vault_path_unique` is subsumed rather than kept beside the new
   check**, with its exact-duplicate wording preserved as the equality case.
   *Default taken.* Two functions answering the same question is how the two
   drift apart.
9. **No migration, no persisted quarantine.** *Default taken.*
10. **The `/vaults/*` dropdown is left as it is** (limitation L7). *Default
    taken.*

## Open Questions

(none blocking — the ten decisions above are the choices, each with a default
applied)
