# Tasks — vault-root-overlap-guard (#199)

**File ownership is exhaustive and disjoint.** Every file any slice edits is
listed in exactly one slice heading; no slice may edit a file it does not own.
**Slice 1 must land on the shared branch first** — every other slice imports the
predicate, the exception types and the snapshot accessor it introduces. Slices
2–6 can then be fanned out to parallel worktrees.

Owned-file map, for the pre-flight check:

| Slice | Files owned |
| --- | --- |
| 1 | `src/services/vault_overlap.py` (new), `src/services/vault.py`, `tests/test_vault_root_overlap.py` |
| 2 | `src/control_panel/users.py`, `src/control_panel/templates/users.html`, `src/auth/routes.py`, `tests/test_users_vault_overlap.py` |
| 3 | `src/services/indexer.py`, `scripts/rebuild_tsvectors.py`, `src/main.py`, `tests/test_indexer_root_overlap.py` |
| 4 | `src/mcp_server/tools.py`, `src/services/usage_stats.py`, `tests/test_tools_overlap_refusal.py` |
| 5 | `src/services/transfer.py`, `tests/test_transfer_overlap_gate.py` |
| 6 | `src/control_panel/routes.py`, `src/control_panel/templates/dashboard.html`, `src/control_panel/templates/health.html`, `tests/test_panel_overlap_surface.py` |
| 7 | `docs/architecture/*.md`, `README.md`, `CLAUDE.md` |

## 1. The checks, the snapshot, and the gate — `src/services/vault_overlap.py`, `src/services/vault.py`, `tests/test_vault_root_overlap.py`

- [ ] 1.1 Root observation: open one root as `O_RDONLY | O_DIRECTORY | O_CLOEXEC` and return `(fd, st_dev, st_ino, realpath, canonical_assignment)` taken in one moment — the real path bound to the descriptor, the way `observe_root_facts` does it, never taken from the assignment string. Descriptors closed on every exit path. A failure returns the `errno`, not an exception.
- [ ] 1.2 **Check 1 — identity:** `(st_dev, st_ino)` equality of the two descriptors.
- [ ] 1.3 **Check 2 — containment:** component-wise ancestor test over the two canonical real paths, both directions. **No string prefix comparison anywhere** — `/vaults/team` must not be an ancestor of `/vaults/team-2`.
- [ ] 1.4 **Check 3 — mount grafting:** parse `/proc/self/mountinfo`; for every entry whose mount point is strictly inside `realpath(A)`, report an overlap when the entry's `major:minor` equals B's root device and the entry's mount root is equal to, an ancestor of, or a descendant of B's own filesystem-relative root (computed from B's longest-matching mountinfo entry as `entry.root` joined with `realpath(B)` relative to `entry.mount_point`). **Best-effort in the refusing direction only:** it can add an overlap, never clear one; unreadable `/proc/self/mountinfo` or a non-Linux platform skips it, logs **once**, and is reported through `vault_fs.mount_identity_available()` / `record_mount_identity_support` rather than a second notion of mount support. Its absence never quarantines anyone.
- [ ] 1.5 The predicate takes the two **canonical assignment strings** alongside the descriptors and returns the relation found — `identical`, `contains`, `contained_by`, `mount_graft`, or `None` — so a caller can select the exact-duplicate wording for an equal pair instead of describing it as a containment.
- [ ] 1.6 Reason types: `Overlap(peer_user_id, relation)` and `RootUnexaminable(errno)`. An unopenable root is **not** an overlap and names no peer.
- [ ] 1.7 `detect_and_publish(session_factory)`: enumerate active users holding an assignment, observe each root once, run the pairwise checks, build the reason mapping, and **publish it atomically** — one immutable object swapped in with a single assignment, so no reader sees a half-built snapshot. Issues no database write. Returns the published snapshot.
- [ ] 1.8 Snapshot lifecycle: tri-state (never published / published-empty / published-with-reasons). A detection that raises **after** a snapshot exists retains the previous snapshot and logs at ERROR; it MUST NOT clear it back to never-published. Sandbox mode publishes an empty snapshot without touching the filesystem.
- [ ] 1.9 `VaultRootOverlap(RuntimeError)`, `VaultRootUnexaminable(RuntimeError)` and `VaultRootNotReady(RuntimeError)`, plus the one wording for each agent-facing refusal (naming no other user, no other path, no note path). Subclassing `RuntimeError` is load-bearing: every existing `except RuntimeError` around `_vault_root` keeps failing closed unchanged.
- [ ] 1.10 `_vault_root` reads the published snapshot **before** the request snapshot and raises the matching type. The lookup is refuse-only: no session, no statement, no syscall, and it cannot admit a caller the rest of the gate would refuse. Never consulted for `user_id is None`.
- [ ] 1.11 Export the wording helper the assignment refusal needs, so slice 2 does not re-implement the relation-to-message mapping.
- [ ] 1.12 Tests: identity via symlink and via a second pathname for one directory; ancestor and descendant both directions; siblings accepted; `/vaults/team` vs `/vaults/team-2` accepted; exact-duplicate reports `identical`; a synthetic mountinfo fixture exercising the graft case, the same-device-different-root non-case and the unreadable-mountinfo skip (logged once, no quarantine); `RootUnexaminable` carries the errno and names no peer; `_vault_root` refuses on each of the three types and admits an absent caller; not-ready refuses in multi-user mode and is never reached for `user_id is None`; a failed re-detection retains the previous snapshot; descriptors closed on every path.

## 2. Assignment-time check and the users list — `src/control_panel/users.py`, `src/control_panel/templates/users.html`, `src/auth/routes.py`, `tests/test_users_vault_overlap.py`

- [ ] 2.1 Inside the existing `_lock_admin_guard` transaction in `edit_user_submit`, after `_validate_vault_path` and before the commit: select `id, username, vault_path` for every other active user holding an assignment, observe each root plus the candidate, and run all three slice-1 checks per pair. No second advisory-lock key.
- [ ] 2.2 **Subsume `_check_vault_path_unique`** into the shared predicate — this file owns that function — and keep its exact-duplicate message wording for the `identical` relation, selected from the canonical assignment strings (task 1.5). One implementation of "do these roots collide", not two.
- [ ] 2.3 Refusal wording per relation: same directory / inside / contains / has the other user's vault mounted inside it. Names the conflicting user in every case.
- [ ] 2.4 A peer root that cannot be opened refuses the assignment, naming that root and stating that the overlap could not be ruled out — not reporting an overlap that was not observed.
- [ ] 2.5 The users list renders a **quarantined — not served** state in place of the note count for an account the published snapshot names, stating the reason (overlap with a named account, or root unexaminable) and that the index is retained. Mirrors the existing unassigned treatment; the count is not deleted to make the display true.
- [ ] 2.6 `create_user` (always NULL) is untouched. In `src/auth/routes.py::register_submit`, add only a comment recording why the check would be vacuous there (`_users_table_empty` means zero rows, so no peer can exist) — no behaviour change.
- [ ] 2.7 Tests: descendant refused; ancestor refused; symlink alias refused; graft refused (synthetic mountinfo); two siblings accepted; string-prefix sibling accepted; identical path refused with the existing wording; inactive peer and NULL-`vault_path` peer are not conflicts; re-saving the target's own unchanged path proceeds; unopenable peer refuses naming that root; the check runs after the lock is taken and before the commit; the users list renders the quarantined state for each reason and the plain count for an unaffected assigned account.

## 3. Detection at every pass entry point — `src/services/indexer.py`, `scripts/rebuild_tsvectors.py`, `src/main.py`, `tests/test_indexer_root_overlap.py`

- [ ] 3.1 `src/main.py::lifespan` calls `detect_and_publish()` **synchronously, before the app serves** — before the indexer task is created and before `yield`. A raise is logged at ERROR and does not exit the process: the panel keeps serving and the readiness state refuses tool calls until a later entry point publishes.
- [ ] 3.2 `run_indexer_loop` calls it at the top of the startup block (E2) and at the top of each periodic iteration (E3), **before** the `_is_paused()` check.
- [ ] 3.3 `scripts/rebuild_tsvectors.py` calls it at the top of its own loop (E5) and consumes the snapshot it publishes — it is a separate process with no lifespan and no indexer loop.
- [ ] 3.4 The per-user stage skip lives in the **shared pass helpers**, not in each loop, so index, link backfill, embed and tsvector rebuild are all skipped for a quarantined user however the pass was started — and a sixth entry point added later inherits the guard by routing through the same helper.
- [ ] 3.5 No row is deleted, pruned or provenance-stamped for a skipped user. Every active user the snapshot does not name is indexed exactly as before.
- [ ] 3.6 Record the skip on the affected user's `indexer_runs` row (reason-specific wording, naming the peer for an overlap and the errno for an unexaminable root) and log at ERROR. A skipped user's pass is not recorded as a clean run.
- [ ] 3.7 A **paused** iteration still publishes the snapshot, still logs at ERROR and still writes the per-user run rows before returning; only index and embed work is suppressed.
- [ ] 3.8 Tests, **one per entry point** (E1 lifespan, E2 startup, E3 tick, E4 reindex — asserted from this slice against the shared helper, E5 script): each publishes a snapshot before any root is read, and each skips a quarantined user. Plus: an alias introduced after assignment is detected at the next entry point; a nested symlink is detected through the canonical paths and not the unchanged strings; a graft is detected via mountinfo; unrelated tenants still index; no rows deleted; a corrected overlap resumes indexing; an unexaminable root quarantines only its own user; single-user mode publishes an empty snapshot; a paused iteration logs and writes the run rows; a detection that raises after a snapshot exists retains it.

## 4. Tool-facing refusals and the pre-body predicate — `src/mcp_server/tools.py`, `src/services/usage_stats.py`, `tests/test_tools_overlap_refusal.py`

- [ ] 4.1 `_vault_admission_error` catches `VaultRootOverlap`, `VaultRootUnexaminable` and `VaultRootNotReady` ahead of the generic `RuntimeError` branch and returns the matching message; the generic branch is unchanged.
- [ ] 4.2 Three markers written to `usage_logs.params["error"]` — `vault_root_overlap`, `vault_root_unexaminable` and `vault_root_not_ready` — each with the comment explaining, as the existing markers do, what it says that the others do not. Three and not one: an overlap needs a mount or an assignment corrected, an unexaminable root needs a mount restored, and a not-ready process needs neither (it is the startup or detector-failure state, and a burst of it in the usage log is the signal that a detection is failing).
- [ ] 4.3 Add every new marker to `PRE_BODY_REFUSAL_ERROR_MARKERS` in `src/services/usage_stats.py`, so `PRE_BODY_REFUSAL_BINDS` and `pre_body_refusal_sql()` pick them up. Update that module's docstring enumeration — it deliberately lists the markers and nothing else, so an unlisted marker silently pollutes latency aggregates and is never counted as a refusal.
- [ ] 4.4 Every caller-facing message names no other user, no other vault path and no note path, for every reason.
- [ ] 4.5 Tests: a database-backed tool, a graph tool and a write tool are each refused for each reason; each marker is distinct from `no_vault_assigned`; the messages contain no other tenant's identifiers; a caller the snapshot does not name is admitted; single-user mode unaffected; the refusal deletes no rows. **Predicate tests:** a row carrying each new marker is excluded from the latency aggregates and counted as a refusal, and the bind set covers every enumerated marker (a test that fails when a marker is added to one list and not the other).

## 5. Transfer redemption gate — `src/services/transfer.py`, `tests/test_transfer_overlap_gate.py`

- [ ] 5.1 Add the snapshot test to the owner re-read in the redemption gate — the locked path and both unlocked root checks — so a capability minted before the quarantine is refused in the same place and manner as an inactive owner or a changed root. Both reasons refuse; the not-ready state refuses too.
- [ ] 5.2 Upload and download directions both refused; nothing published, nothing streamed.
- [ ] 5.3 Tests: upload capability refused; download capability refused for each reason; an unrelated owner's capability proceeds; a corrected quarantine restores redemption; single-user mode unaffected. Minting is already refused by slice 4 and is asserted there.

## 6. Operator surface and the `_reindex_background` entry point — `src/control_panel/routes.py`, `src/control_panel/templates/dashboard.html`, `src/control_panel/templates/health.html`, `tests/test_panel_overlap_surface.py`

- [ ] 6.1 `_health_strip` reads the published snapshot (never recomputes it, never opens a directory, never reads mountinfo) and resolves the usernames and roots it names; the strip and the health page render the condition for administrators only.
- [ ] 6.2 The two reasons are worded separately: an overlap names the peer account and the relation; an unexaminable root names the root and the errno and states that no peer was observed.
- [ ] 6.3 The condition clears on its own when a later snapshot no longer names the account; it is not a flash message and needs no dismissal. An empty snapshot renders nothing, and the never-published state renders its own "not yet checked" note rather than an all-clear.
- [ ] 6.4 `vault_page` renders the existing `vault_error` empty state for a named user.
- [ ] 6.5 Templates use theme tokens only — no color literals — so `colorscan` / the literal sweep stays clean.
- [ ] 6.6 **E4:** `_reindex_background` calls `detect_and_publish()` at its top, before `index_pass_lock` is taken, so Reindex Now, re-embed and reset-embeddings cannot start a pass against an unchecked snapshot. This file is owned here, so the call site lands in this slice; the helper it calls comes from slice 1 and the stage skip from slice 3.
- [ ] 6.7 Tests: the strip names every affected account, its reason and its root for an admin; a non-admin sees no other account's name or path; an empty snapshot renders nothing; the never-published state renders the not-yet-checked note; the handler opens no directory and reads no mountinfo; `vault_page` empty state; `_reindex_background` publishes before taking the pass lock.

## 7. Documentation — `docs/architecture/*.md`, `README.md`, `CLAUDE.md`

- [ ] 7.1 `docs/architecture/vault-roots-and-tenancy.md`: the three checks and what each does and does not prove; the two enforcement points and the five pass entry points; the readiness state and why the lifespan detection is synchronous; the refuse-only exception to "`_vault_root` is a pure cache lookup", written so a future reader cannot take it as licence to add a query; the disposition and why index refusal alone is not it.
- [ ] 7.2 `docs/architecture/vault-tools.md`: correct the `RESOLVE_NO_XDEV` paragraph — the omission is a containment non-issue for one tenant and is exactly what makes a nested or grafted second tenant reachable.
- [ ] 7.3 `docs/architecture/indexing-and-embeddings.md`: one detection called from every entry point (and why the loop alone was not enough), its position before the pause check, the paused-iteration recording rule, and the skip's two records with the ring buffer's lifetime as the reason for both.
- [ ] 7.4 `docs/architecture/control-panel.md`: the condition on the strip and the health page, the two reasons' separate wording, the quarantined state on the users list, and why the operator surfaces name everything while the tool refusal names nothing.
- [ ] 7.5 `docs/architecture/usage-attribution.md`: the new markers and their membership in the pre-body-refusal predicate.
- [ ] 7.6 `README.md`: extend the existing "the `vault_path` validator does not resolve symlinks" paragraph with what is now caught, and with limitations L1–L3 (namespace, shadowing, `/proc` availability).
- [ ] 7.7 `CLAUDE.md`: one line under Key decisions.

## 8. Verification

- [ ] 8.1 Full suite green: `OMCP_ALLOW_SKIP_TRANSFER_INTEGRATION=1 pytest -q` on the merged result, not per worktree.
- [ ] 8.2 `npx -y @fission-ai/openspec@1.3.1 validate --all --strict` clean.
- [ ] 8.3 No migration in this change: `make db-check` (`alembic check`) still reports "No new upgrade operations detected"; `make test-schema` not required and not run.
- [ ] 8.4 `make audit` clean (no dependency change expected).
- [ ] 8.5 Seam check before believing the fan-out is done: grep for a production caller of `detect_and_publish` at each of E1–E5 and for the stage skip in each pass helper. Fan-out ships green-but-unwired code, and this change's whole value is that no entry point is missed.
- [ ] 8.6 `openspec-verifier` subagent audits the implementation against these deltas; iterate to zero blocking gaps.
- [ ] 8.7 Adversarial Codex round 2 — mandatory: this change touches the admission gate and the write surface. Frame it as a defensive PASS/FAIL control review and tell it the expensive failures are a cross-tenant destructive write, a silently wrong search result, and a false positive that quarantines a healthy tenant. Attack in particular: the component-wise ancestor test against `/vaults/team-2`, a trailing slash, a repeated separator and a `.` component; the identity test against a reused inode after a directory is deleted and recreated; the mountinfo parser against octal-escaped path fields (`\040`, `\011`, `\134`), optional fields terminated by `-`, and a shadowed entry; the ordering between publication and the first served request; the refuse-only claim for the gate's ordering against the request snapshot; whether any entry point that can begin a pass was missed against the E1–E5 table; and whether any surface that resolves a vault root was missed against the read/assign enumeration in `design.md`.
- [ ] 8.8 `make deploy`.
- [ ] 8.9 Live check via the panel users page against production's two users: the dashboard strip shows **no** condition (the two roots are siblings, so the guard must be inert) and the users list shows both note counts as numbers; submitting a deliberately nested custom path for one user is refused naming the other user, and the form leaves `vault_path` unchanged; restore and confirm both original assignments still stand.
- [ ] 8.10 End-to-end exercise of the affected MCP tools against the live server, naming in the report which tools were actually called — at minimum one database-backed search, one graph tool and one write tool for an unaffected user, to confirm the gate did not become stricter for anyone. (No `user-representative` pass: there is no browser UI.)
- [ ] 8.11 `/openspec-archive-change`, then commit and push closing #199.
