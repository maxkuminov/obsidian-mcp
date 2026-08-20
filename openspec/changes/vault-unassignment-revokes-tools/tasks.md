## 1. Admission gate

- [x] 1.1 Add `_NO_VAULT_MESSAGE`, `_NO_VAULT_MARKER` and
      `_vault_admission_error()` to `src/mcp_server/tools.py`, resolving
      `_vault_root(current_user_id.get())` and returning the refusal string on
      `RuntimeError` (with an operator-facing `logger.warning`)
- [x] 1.2 Call the gate from `_tracked`'s wrapper *before* awaiting the tool
      body; on refusal use the message as the result and skip the body
- [x] 1.3 Confirm single-user / sandbox mode passes the gate (`current_user_id`
      is None → `settings.vault_path`, cache untouched)
- [x] 1.4 Enumerate exemptions: none. `get_vault_guide` reads the vault's
      `CLAUDE.md`; `check_upload` reports a vault path/size/digest; every other
      `_tracked` tool reads or writes vault content or metadata

## 2. Usage logging

- [x] 2.1 Log the refusal through the existing single `_log_usage` call site
      with `params["error"] = _NO_VAULT_MARKER`
- [x] 2.2 Keep the tool's normal allow-listed params and add no other field

## 3. Cache semantics

- [x] 3.1 Make the single-user form of `warm_user_vault_cache` evict when the
      query returns no row, so a revocation lands in every worker process
- [x] 3.2 Leave the bulk form add-only (documented in design.md) and keep
      `_vault_root` a pure cache lookup — no DB query per tool call
- [x] 3.3 Record in `_vault_root`'s docstring that it is now the admission gate
- [x] 3.4 Add `current_vault_root` / `UNSET_VAULT_ROOT` to `src/auth/session.py`
      and bind the per-request root in `APIKeyMiddleware` (both the API-key and
      OAuth branches), resetting it in the same `finally` as the other
      ContextVars
- [x] 3.5 Make `_vault_root` prefer the request snapshot over the shared dict,
      keyed by user id, and never consult it for `user_id is None`

## 4. Panel copy

- [x] 4.1 Change the `user_edit.html` option label to
      `(unassigned — every MCP tool refuses; index kept for reassignment)`

## 5. Tests

- [x] 5.1 DB-only tools (`semantic_search`, `keyword_search`, `list_notes`,
      `get_recent`, `get_tags`) refuse for a user with no cached root
- [x] 5.2 Graph tools (`get_backlinks`, `get_links`, `get_neighborhood`,
      `find_orphans`, `find_related`) refuse
- [x] 5.3 Disk-touching tools and `get_vault_guide` refuse through the same gate
- [x] 5.4 The refusal echoes neither the query nor any path
- [x] 5.5 Single-user mode still runs the tool body with an empty cache, and an
      assigned user still passes the gate
- [x] 5.6 The refusal is logged with the error marker and no extra field
- [x] 5.7 `warm_user_vault_cache` evicts a stale entry when the row is gone
- [x] 5.8 A cold cache refuses rather than raising
- [x] 5.9 Full suite green; new tests verified to fail against the pre-change
      code
- [x] 5.10 The refusal matrix enumerates every tool registered on the MCP
      server by introspecting `mcp._tool_manager`, not a hand list
- [x] 5.11 Structural test: every registered tool delegates to a
      `_tracked`-wrapped impl (via the `__tracked_tool__` marker)
- [x] 5.12 Deterministic ordered-race test: a stale bulk warm landing after the
      per-request eviction still leaves the call refused — with a negative
      control proving the bulk warm really does repopulate the shared dict
- [x] 5.13 End-to-end test through `APIKeyMiddleware`: the key authenticates,
      the snapshot binds as `(user_id, None)`, the tool refuses, and the
      snapshot does not outlive the request
- [x] 5.14 Assert the *rendered* option text from `user_edit.html`

## 6. Documentation

- [x] 6.1 Record the gate in `CLAUDE.md` next to the multi-user vault behaviour
- [x] 6.2 `openspec validate vault-unassignment-revokes-tools --strict` passes
