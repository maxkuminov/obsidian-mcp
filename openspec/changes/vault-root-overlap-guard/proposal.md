## Why

The vault-root uniqueness check is string equality. `_check_vault_path_unique`
(`src/control_panel/users.py:68-81`) rejects only an *identical* `vault_path`
among active users, and `validate_vault_root_path`
(`src/services/vault.py:266-299`) rejects only empty input, a `..` component, a
prefix outside `/vaults/`, and a non-directory. Two shapes get through:

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

Both require an administrator to hand-type a path into the custom-path field —
the dropdown offers top-level `/vaults/*` only — so no untrusted actor crosses a
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
and the filesystem keeps moving. A symlink created after the assignment, or a
bind mount repointed by a compose edit, produces the same overlap with no
administrator action to intercept.

## What Changes

- **At assignment (`edit_user_submit`), inside the existing `_lock_admin_guard`
  transaction:** two checks against every *other* active assignment — the
  `(st_dev, st_ino)` identity of an opened directory descriptor (aliases) and a
  component-wise realpath prefix test in both directions (ancestor/descendant).
  A conflict is refused, naming the conflicting user. `_check_vault_path_unique`
  is subsumed: exact-string equality is the degenerate case of both checks and
  its message wording is preserved.
- **At every indexer pass (which includes the startup pass):** the same two
  checks across all active assignments, before any user is indexed. Every user
  in a detected overlap relation is **quarantined**: the pass indexes none of
  them (index, link backfill and embed all skipped), and their MCP tool calls,
  transfer redemptions and panel vault browser are refused through the existing
  "this user has no vault" paths. Unrelated tenants are untouched.
- **Fail closed for the pair, not for the deployment.** Quarantine refuses
  reads *and* writes for exactly the users in the overlap, because index refusal
  alone leaves the outer tenant's write tools able to clobber the inner tenant's
  notes and leaves already-indexed foreign rows queryable. No index rows are
  deleted — a corrected assignment must not cost a full re-embed.
- **Not in `_vault_root`.** The gate stays free of database and filesystem work;
  it consults one process-global, refuse-only set. Detection happens where a
  pass already opens roots and already holds a session.
- **Surfaced.** The overlap is logged at ERROR (so the ops-health ring buffer
  catches it), written to the affected users' `indexer_runs.error` (so it
  survives a restart, which the ring buffer does not), and rendered as an
  admin-only banner on the dashboard health strip and the health page naming
  both users and both roots.
- **Documented.** `docs/architecture/vault-roots-and-tenancy.md` gains the guard
  and the refuse-only exception to "`_vault_root` is a pure cache lookup";
  `docs/architecture/vault-tools.md`'s `RESOLVE_NO_XDEV` paragraph is corrected;
  the README's existing "the validator does not resolve symlinks" paragraph
  gains the residual that survives.

No migration. The quarantine is derived from the filesystem every pass;
persisting it would create a second source of truth that can disagree with the
directory it describes.

## Capabilities

### New Capabilities

(none)

### Modified Capabilities

- `panel-user-administration`: the assignment-time overlap refusal.
- `index-integrity`: per-pass overlap detection and the index refusal.
- `mcp-request-routing`: the tool refusal for a quarantined caller, and the
  existing "the admission gate performs no database work" requirement widened to
  cover filesystem work.
- `file-transfer`: a capability minted before the overlap is refused at
  redemption.
- `panel-ops-health`: the operator surface for a detected overlap.

## Impact

- `src/control_panel/users.py` (assignment check), `src/services/vault.py`
  (the quarantine set, `_vault_root`'s refuse-only test, the shared root-pair
  predicate), `src/services/indexer.py` (per-pass detection, pass skip, run-row
  recording), `src/services/transfer.py` (redemption gate),
  `src/control_panel/routes.py` + `dashboard.html` / `health.html` (surface),
  `src/mcp_server/tools.py` (distinct refusal marker).
- Docs: `docs/architecture/vault-roots-and-tenancy.md`,
  `docs/architecture/vault-tools.md`, `docs/architecture/control-panel.md`,
  `README.md`, `CLAUDE.md` (one line under Key decisions).
- No schema change, no migration, no `alembic check` movement.
- Production runs `multi_user_mode=True` with two users at sibling roots, so the
  guard is expected to be inert on the live deployment; the live check is that
  it stays inert and that a deliberately nested candidate is refused.

Closes #199
