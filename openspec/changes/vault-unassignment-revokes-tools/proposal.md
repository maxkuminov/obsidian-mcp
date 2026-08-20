## Why

The control panel offers `(unassigned — vault tools error)` for a user's vault
path. Selecting it NULLs `users.vault_path`, clears the in-process vault cache
and flashes success — but the promise only held for the disk-touching tools.

Everything served from the database kept working. `semantic_search`,
`keyword_search`, `list_notes`, `get_recent` and every graph tool
(`get_backlinks`, `get_links`, `get_neighborhood`, `find_orphans`,
`find_related`) query `notes_metadata` / `note_embeddings` filtered by
`user_id` alone and never call `_vault_root`. Nothing prunes those rows either:
the indexer's `_active_user_ids()` filters `User.vault_path.isnot(None)`, so
`index_vault(user_id=…)` — the only thing that deletes stale `notes_metadata`
rows — never runs for that user again. The rows are frozen in place
permanently and this never self-heals.

The user's API key still authenticates (`src/mcp_server/auth.py` checks only
key/user activity and expiry — there is no vault requirement), so the result is
an **indefinite, fully queryable mirror of the content the user last held** —
file paths, titles, tags, frontmatter and 200-char chunk excerpts, up to 50
hits per call, unbounded calls — reachable with an unchanged credential, while
the operator was told "vault tools error". `read_note` on any returned path
correctly errors, which is exactly what makes the leak look like it isn't
happening. (Issue #66, severity high.)

## What Changes

- **One admission gate for every MCP tool.** `_tracked` in
  `src/mcp_server/tools.py` — the decorator every tool impl shares — resolves
  the caller's vault root once, *before* the tool body runs, and fails the
  whole call with a tool error when it cannot be resolved. Nothing is exempt:
  every `_tracked` tool reads or writes vault content or vault metadata.
- **The refusal is logged** like any other tool error, with an
  `error: "no_vault_assigned"` marker in `usage_logs.params` and the same
  allow-listed params as a successful call. No new field carries user content.
- **`warm_user_vault_cache(session, user_id)` becomes authoritative.** It was
  a silent no-op when the user had no usable row, leaving a previously cached
  root in place; it now evicts. That warm runs on every authenticated MCP
  request, so a mid-session unassignment is refused from the next call in
  *every* worker process, not only the one that served the panel request.
- **Single-user mode is untouched.** `current_user_id` is None there and
  `_vault_root(None)` answers from `settings.vault_path` without consulting
  the cache.
- **The index rows are preserved** — deliberately. A reassignment of the same
  path must not have to re-embed the vault from scratch.
- **The option label is corrected** to state what the code now does:
  `(unassigned — every MCP tool refuses; index kept for reassignment)`.

## Capabilities

### Modified Capabilities
- `mcp-request-routing`: an authenticated MCP tool call is admitted only while
  the caller has a resolvable vault root.

## Impact

- `src/mcp_server/tools.py` — `_tracked` gate, `_vault_admission_error()`,
  `_NO_VAULT_MESSAGE`, `_NO_VAULT_MARKER`
- `src/services/vault.py` — `warm_user_vault_cache` evicts; `_vault_root`
  docstring records that it is now the admission gate and must stay a cache
  lookup
- `src/control_panel/templates/user_edit.html` — option label
- `tests/test_issue_66_vault_unassignment_revokes_tools.py` — new
- No database schema changes, no new dependencies, no migration
