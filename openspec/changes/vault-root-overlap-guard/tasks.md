# Tasks — vault-root-overlap-guard (#199)

Slices 2–6 are disjoint in their file scopes and can be fanned out to parallel
worktrees. **Slice 1 must land on the shared branch first** — every other slice
imports the predicate, the exception and the quarantine set it introduces. Each
slice owns its own test module so the test files do not collide either; no slice
may edit a file another slice owns.

## 1. Shared predicate, quarantine set, and the gate — `src/services/vault.py`, `tests/test_vault_root_overlap.py`

- [ ] 1.1 A pure predicate over two opened roots: `roots_overlap(fd_a, realpath_a, fd_b, realpath_b) -> str | None` returning the relation found (`"identical"`, `"a_contains_b"`, `"b_contains_a"`) or `None`. Identity by `(st_dev, st_ino)` of the two `fstat`s; containment by component-wise ancestor test over the two canonical real paths, both directions. **No string prefix comparison anywhere** — `/vaults/team` must not be an ancestor of `/vaults/team-2`.
- [ ] 1.2 A helper that opens one root and returns `(fd, st_dev, st_ino, realpath)` in one moment, or reports that it could not be examined — same discipline as `observe_root_facts`: the real path is bound to the descriptor, never taken from the assignment string. Descriptors are closed on every exit path.
- [ ] 1.3 `VaultRootOverlap(RuntimeError)` plus the one wording for the agent-facing refusal. Subclassing `RuntimeError` is load-bearing: every existing `except RuntimeError` around `_vault_root` keeps failing closed unchanged.
- [ ] 1.4 The process-global `_overlapping_user_ids: frozenset[int]`, its setter (replaces wholesale) and its reader. No database column, no persistence.
- [ ] 1.5 `_vault_root` consults the set **before** the request snapshot and raises `VaultRootOverlap`; the test is refuse-only and performs no session, no statement and no syscall. Never consulted for `user_id is None`.
- [ ] 1.6 Fold the exact-duplicate case into the shared predicate and keep `_check_vault_path_unique`'s existing message wording for it; do not leave two implementations of "do these roots collide".
- [ ] 1.7 Tests: identity via a symlink and via a same-directory second pathname; ancestor and descendant in both directions; siblings accepted; `/vaults/team` vs `/vaults/team-2` accepted; the exact-duplicate case; `_vault_root` refuses a quarantined id and cannot admit an unassigned one; single-user (`user_id is None`) untouched; descriptors closed.

## 2. Assignment-time check — `src/control_panel/users.py`, `tests/test_users_vault_overlap.py`

- [ ] 2.1 Inside the existing `_lock_admin_guard` transaction in `edit_user_submit`, after `_validate_vault_path` and before the commit: select `id, username, vault_path` for every other active user holding an assignment, open each root plus the candidate, and run the slice-1 predicate for each pair. No second advisory-lock key.
- [ ] 2.2 Refusal wording: names the conflicting user and the relation found; the exact-duplicate case keeps today's message.
- [ ] 2.3 A peer root that cannot be opened refuses the assignment, naming that root and saying the overlap could not be ruled out — not reporting an overlap that was not observed.
- [ ] 2.4 Leave `create_user` (always NULL) and `src/auth/routes.py::register_submit` (runs only when `users` is empty) untouched; record in a comment at the latter why the check would be vacuous there.
- [ ] 2.5 Tests: descendant refused; ancestor refused; symlink alias refused; two siblings accepted; string-prefix sibling accepted; identical path refused with the existing wording; inactive peer and NULL-`vault_path` peer are not conflicts; re-saving the target's own unchanged path proceeds; unopenable peer refuses; the check runs after the lock is taken and before the commit.

## 3. Per-pass detection, skip and recording — `src/services/indexer.py`, `tests/test_indexer_root_overlap.py`

- [ ] 3.1 A detector that reads every active assigned user's root, opens each once, runs the slice-1 predicate pairwise, and publishes the overlap set. Issues no database write. A root it cannot open puts **that user only** into the set.
- [ ] 3.2 Call it at the top of each iteration of `run_indexer_loop` — the startup block and the periodic tick — **before** the `_is_paused()` check and before `_active_user_ids()`'s list is consumed. No separate lifespan guard.
- [ ] 3.3 Skip index, link backfill and embed for every user in the set; run all three normally for every user not in it. No row is deleted, pruned or provenance-stamped for a skipped user.
- [ ] 3.4 Record the skip on the affected user's `indexer_runs` row (naming the other user) and log at ERROR (naming both users and both roots) so the ring buffer catches it. A skipped user's pass is not recorded as a clean run.
- [ ] 3.5 Tests: an alias introduced after assignment is detected at the next pass; a nested symlink is detected through the canonical paths and not the unchanged strings; unrelated tenants still index; no rows deleted; a corrected overlap resumes indexing at the next pass; an unopenable root quarantines only its own user; single-user mode publishes an empty set; a paused indexer still publishes the set.

## 4. Tool-facing refusal and its marker — `src/mcp_server/tools.py`, `tests/test_tools_overlap_refusal.py`

- [ ] 4.1 `_vault_admission_error` catches `VaultRootOverlap` ahead of the generic `RuntimeError` branch and returns the distinct message; the generic branch is unchanged.
- [ ] 4.2 `_VAULT_OVERLAP_MARKER = "vault_root_overlap"` written to `usage_logs.params["error"]`, with the comment explaining — as the two existing markers do — what it says that the others do not.
- [ ] 4.3 The caller-facing message names no other user, no other vault path and no note path.
- [ ] 4.4 Tests: a database-backed tool, a graph tool and a write tool are each refused; the marker is distinct from `no_vault_assigned`; the message contains no other tenant's identifiers; a caller not in the set is admitted; single-user mode unaffected; the refusal deletes no rows.

## 5. Transfer redemption gate — `src/services/transfer.py`, `tests/test_transfer_overlap_gate.py`

- [ ] 5.1 Add the membership test to the owner re-read in the redemption gate — the locked path and both unlocked root checks — so a capability minted before the overlap is refused in the same place and manner as an inactive owner or a changed root.
- [ ] 5.2 Upload and download directions both refused; nothing published, nothing streamed.
- [ ] 5.3 Tests: upload capability refused; download capability refused; an unrelated owner's capability proceeds; a corrected overlap restores redemption; single-user mode unaffected. Minting is already refused by slice 4 and is asserted there.

## 6. Operator surface — `src/control_panel/routes.py`, `src/control_panel/templates/dashboard.html`, `src/control_panel/templates/health.html`, `tests/test_panel_overlap_surface.py`

- [ ] 6.1 `_health_strip` reads the published overlap set (never recomputes it, never opens a directory) and resolves the usernames and roots it names; the strip and the health page render the condition for administrators only.
- [ ] 6.2 The condition clears on its own when a pass publishes an empty set; it is not a flash message and needs no dismissal. Absent condition renders nothing and is not an error state.
- [ ] 6.3 `vault_page` renders the existing `vault_error` empty state for a user in the set.
- [ ] 6.4 Templates use theme tokens only — no color literals — so `colorscan` / the literal sweep stays clean.
- [ ] 6.5 Tests: strip names both users and both roots for an admin; a non-admin sees no other user's name or path; empty set renders nothing; the handler opens no directory; `vault_page` empty state.

## 7. Documentation — `docs/architecture/*`, `README.md`, `CLAUDE.md`

- [ ] 7.1 `docs/architecture/vault-roots-and-tenancy.md`: the two checks and what each does and does not prove; the two enforcement points; the refuse-only exception to "`_vault_root` is a pure cache lookup", written so a future reader cannot take it as licence to add a query; the disposition and why index refusal alone is not it.
- [ ] 7.2 `docs/architecture/vault-tools.md`: correct the `RESOLVE_NO_XDEV` paragraph — the omission is a containment non-issue for one tenant and is exactly what makes a nested second tenant reachable.
- [ ] 7.3 `docs/architecture/indexing-and-embeddings.md`: the per-pass detector, its position before the pause check, and the skip's two records (log line and run row) with the ring buffer's lifetime as the reason for both.
- [ ] 7.4 `docs/architecture/control-panel.md`: the overlap condition on the strip and the health page, and why it names both users there and nowhere else.
- [ ] 7.5 `README.md`: extend the existing "the `vault_path` validator does not resolve symlinks" paragraph with what is now caught and with limitation L1 (a bind mount of one root to a path inside another tenant's tree).
- [ ] 7.6 `CLAUDE.md`: one line under Key decisions.

## 8. Verification

- [ ] 8.1 Full suite green: `OMCP_ALLOW_SKIP_TRANSFER_INTEGRATION=1 pytest -q` on the merged result, not per worktree.
- [ ] 8.2 `npx -y @fission-ai/openspec@1.3.1 validate --all --strict` clean.
- [ ] 8.3 No migration in this change: `make db-check` (`alembic check`) still reports "No new upgrade operations detected"; `make test-schema` not required and not run.
- [ ] 8.4 `make audit` clean (no dependency change expected).
- [ ] 8.5 `openspec-verifier` subagent audits the implementation against these deltas; iterate to zero blocking gaps.
- [ ] 8.6 Adversarial Codex pass — mandatory: this change touches the admission gate and the write surface. Frame it as a defensive PASS/FAIL control review and tell it the expensive failures are a cross-tenant destructive write, a silently wrong search result, and a false positive that quarantines a healthy tenant. Attack in particular: the component-wise ancestor test against `/vaults/team-2`, a trailing slash, a repeated separator and a `.`-component; the identity test against a reused inode after a directory is deleted and recreated; the check-then-act window between the detector and the pass; the refuse-only claim for the gate's ordering against the request snapshot; and whether any surface that resolves a vault root was missed (the read/assign enumeration in `design.md` is the list to check it against).
- [ ] 8.7 `make deploy`.
- [ ] 8.8 Live check via the panel users page against production's two users: the dashboard strip shows **no** overlap condition (the two roots are siblings, so the guard must be inert); submitting a deliberately nested custom path for one user is refused naming the other user, and the form leaves `vault_path` unchanged; restore and confirm the users page still shows both original assignments.
- [ ] 8.9 End-to-end exercise of the affected MCP tools against the live server, naming in the report which tools were actually called — at minimum one database-backed search, one graph tool and one write tool for an unaffected user, to confirm the gate did not become stricter for anyone. (No `user-representative` pass: there is no browser UI.)
- [ ] 8.10 `/openspec-archive-change`, then commit and push closing #199.
