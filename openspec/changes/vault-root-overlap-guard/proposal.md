## Why

The vault-root uniqueness check is string equality. `_check_vault_path_unique`
(`src/control_panel/users.py:68-81`) rejects only an *identical* `vault_path`
among active users, and `validate_vault_root_path`
(`src/services/vault.py:266-299`) rejects only empty input, a `..` component, a
prefix outside `/vaults/`, and a non-directory. Three shapes get through:

- **Ancestor / descendant.** `/vaults/team` for user A and `/vaults/team/private`
  for user B are two different strings, so both are accepted. Every path lookup
  below a root is `openat2(RESOLVE_BENEATH | RESOLVE_NO_SYMLINKS)` and
  `RESOLVE_NO_XDEV` is deliberately omitted — B's root *is* beneath A's, so A's
  `read_note`, `edit_note`, `write_file` and `delete_note` reach B's files and
  the containment check agrees they are contained. The indexer additionally
  files B's notes under A's `user_id`, so `semantic_search` and `keyword_search`
  return B's content to A's agent.
- **Aliases.** A symlink or a bind mount that makes two different pathnames name
  one directory passes the same string comparison, because the strings differ.
- **Grafts.** `mount --bind /vaults/b /vaults/a/inner` puts B's whole vault
  inside A's tree while both root inodes stay distinct and both canonical
  pathnames stay outside each other — invisible to a path check *and* to an
  inode check.

All three require an administrator to hand-type a path or edit a mount — the
dropdown offers top-level `/vaults/*` only — so no untrusted actor crosses a
boundary. What makes it worth fixing anyway is that the exact-duplicate
rejection is a **false assurance that collisions are checked**, and the blast
radius under this product's framing is maximal: a cross-tenant destructive write
and a silently wrong search result, the two failures this server ranks highest,
both delivered to an agent that will act on them without a human seeing the
query. The ancestor/descendant case is documented nowhere, and
`docs/architecture/vault-tools.md` currently calls the `RESOLVE_NO_XDEV`
omission a containment non-issue — true for one tenant, false the moment two
tenants nest. There are zero tests referencing either function.

An assignment-time check alone is not enough: an assignment is validated once
and the filesystem keeps moving. A symlink created after the assignment, a bind
mount repointed by a compose edit, or a `vault_path` written directly in psql
produces the same overlap with no administrator action to intercept.

## What Changes

- **At assignment (`edit_user_submit`), inside the existing `_lock_admin_guard`
  transaction and only when the edit's *resulting* state is active and
  assigned** — deactivating or unassigning an account is the operator's remedy
  for a quarantine and must not be refused by the guard — **three checks against
  every *other* active assignment — the
  `(st_dev, st_ino)` identity of an opened directory descriptor (aliases), a
  component-wise realpath prefix test in both directions (ancestor/descendant),
  and a best-effort `/proc/self/mountinfo` scan for a mount of one tenant's
  filesystem-relative root grafted inside another's tree.** A conflict is
  refused, naming the conflicting user; an equal pair keeps today's wording.
- **One `detect_and_publish()`, called from every entry point that can begin a
  pass** — the lifespan, the indexer's startup block, each periodic tick, the
  panel's `_reindex_background` (Reindex Now, re-embed, reset embeddings) and
  the standalone `scripts/rebuild_tsvectors.py` process. Detection installed in
  the indexer loop alone would be bypassed by the last two, one of which is a
  different process entirely.
- **Fail closed until the first snapshot.** The lifespan runs the detection
  **synchronously before the app serves**. Until a snapshot has been published
  in this process, `_vault_root` refuses every multi-user caller with a typed
  not-ready refusal. Publication is atomic; a later detection that fails retains
  the previous snapshot and logs at ERROR rather than clearing it.
- **One detection at a time, and a publication that cannot go backwards.** The
  entry points call detection before taking the pass lock, so a periodic tick
  and a panel reindex overlap trivially — and a detection stalled on a slow root
  could otherwise publish its own *empty* result over a newer quarantine and
  re-admit both tenants. Observation, the checks and the publication all run
  inside **one process-global `asyncio.Lock`**, and each snapshot carries a
  **monotonic sequence** taken under that lock, so an older result is dropped
  rather than published.
- **Structured quarantine reasons that preserve what was observed.** The
  snapshot maps a user id to `overlap(peer, relation)` or
  `root_unexaminable(errno)` — an unopenable root is not an overlap and must not
  be reported as one — and each entry carries the usernames, canonical
  assignments and detection time **as observed**, so the panel can still name
  the pair after the operator edits or deletes one of them. The two are worded
  separately in the panel, the log line, the `indexer_runs` row and the
  `usage_logs` marker (`vault_root_overlap` / `vault_root_unexaminable`); both
  markers join the shared pre-body-refusal predicate so they are excluded from
  latency aggregates and counted as refusals.
- **Fail closed for the pair, not for the deployment.** A quarantined user is
  skipped by every pass stage and refused by every MCP tool, by transfer
  redemption and by the panel vault browser; the users list shows a
  quarantined-not-served state instead of a note count the tools will not serve.
  Unrelated tenants are untouched. No index rows are deleted — a corrected
  assignment must not cost a full re-embed.
- **Not in `_vault_root`.** The gate stays free of database work, filesystem
  work and mount-table parsing; it reads one immutable published snapshot,
  refuse-only.
- **Surfaced.** Logged at ERROR (so the ops-health ring buffer catches it),
  written to the affected users' `indexer_runs.error` (so it survives a restart,
  which the ring buffer does not — and a **paused** iteration still logs and
  still writes those rows), and rendered as an admin-only condition on the
  dashboard health strip and the health page naming every affected account, its
  reason and its root.
- **Documented.** `docs/architecture/vault-roots-and-tenancy.md` gains the guard
  and the refuse-only exception to "`_vault_root` is a pure cache lookup";
  `docs/architecture/vault-tools.md`'s `RESOLVE_NO_XDEV` paragraph is corrected;
  the README's existing "the validator does not resolve symlinks" paragraph
  gains the residual that survives.

No migration. The quarantine is derived from the filesystem at every entry
point; persisting it would create a second source of truth that can disagree
with the directory it describes.

## Capabilities

### New Capabilities

(none)

### Modified Capabilities

- `panel-user-administration`: the assignment-time overlap refusal.
- `index-integrity`: detection at every pass entry point, the published
  snapshot's lifecycle, and the index refusal.
- `mcp-request-routing`: the tool refusal for a quarantined caller and the
  not-ready refusal; the existing "the admission gate performs no database work"
  requirement widened to cover filesystem and mount-table work; the existing
  users-list requirement widened to the quarantined state.
- `panel-performance-views`: the two new markers join the shared
  pre-body-refusal predicate.
- `file-transfer`: a capability minted before the quarantine is refused at
  redemption.
- `panel-ops-health`: the operator surface, with the two reasons worded apart.

## Impact

- `src/services/vault_overlap.py` (new — the three checks, the snapshot, the
  orchestration), `src/services/vault.py` (the exception types, the gate's
  refuse-only lookup), `src/control_panel/users.py` + `users.html` (assignment
  check, quarantined state), `src/auth/routes.py` (a comment at the bootstrap
  path), `src/services/indexer.py` + `scripts/rebuild_tsvectors.py` +
  `src/main.py` (detection at every entry point, pass skip, run-row recording),
  `src/mcp_server/tools.py` + `src/services/usage_stats.py` (markers and the
  pre-body predicate), `src/services/transfer.py` (redemption gate),
  `src/control_panel/routes.py` + `dashboard.html` / `health.html` (surface,
  `vault_page`, the `_reindex_background` entry point).
- Mountinfo availability is tracked as **its own state** in the new module and
  is deliberately *not* folded into `src/services/vault_fs.py`'s
  `mount_identity_available()`: `STATX_MNT_ID` is Linux 5.8 while reading
  `/proc/self/mountinfo` needs neither that extension nor that kernel, so
  reusing the flag would disable the graft check on kernels that can perform it
  and would make its failure masquerade as a transfer-write outage on
  `/health`. `transfer_mount_check_available`'s semantics are untouched.
- Docs: `docs/architecture/vault-roots-and-tenancy.md`,
  `docs/architecture/vault-tools.md`,
  `docs/architecture/indexing-and-embeddings.md`,
  `docs/architecture/control-panel.md`,
  `docs/architecture/usage-attribution.md`, `README.md`, `CLAUDE.md`.
- No schema change, no migration, no `alembic check` movement.
- Production runs `multi_user_mode=True` with two users at sibling roots, so the
  guard is expected to be inert on the live deployment; the live check is that
  it stays inert and that a deliberately nested candidate is refused.

Closes #199
