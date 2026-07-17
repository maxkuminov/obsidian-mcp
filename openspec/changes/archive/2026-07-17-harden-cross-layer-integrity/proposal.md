## Why

Several cross-layer invariants were only partially enforced: filesystem operations could lose concurrent edits, derived indexes could become permanently stale after partial failure, and OAuth/root-routing paths could bypass session or middleware security guarantees. These failures risk data loss, inconsistent search results, and credential minting from invalidated sessions.

## What Changes

- Make note creation, raw file writes, and note moves honor no-clobber semantics atomically under races.
- Reject note mutations in hidden vault directories and detect intervening external edits during read-modify-write operations.
- Preserve correct relative Markdown links and self-links when moving notes.
- Enforce bounded single-descriptor file reads.
- Make metadata, FTS, and link updates an atomic index snapshot and make link backfill per-user and restart-safe.
- Reject partial embedding batches and treat zero-chunk notes as completed without vectors.
- Revalidate active user and session version during both OAuth consent stages.
- Route root MCP fallback requests through the same middleware boundary as canonical `/mcp/` requests.

## Capabilities

### New Capabilities

- `index-integrity`: Atomic derived-index updates, restart-safe link backfill, and exact embedding completion semantics.
- `oauth-authorization-integrity`: OAuth consent honors user activation and session invalidation before credentials can be minted.
- `mcp-request-routing`: Root fallback and canonical MCP requests traverse equivalent application security middleware.

### Modified Capabilities

- `vault-write`: Strengthen mutation safety with atomic no-clobber, hidden-path denial, concurrent-edit conflicts, and correct link rewriting.
- `file-access`: Strengthen no-clobber writes and bounded reads against filesystem races.

## Impact

- Affected code: `src/services/indexer.py`, `src/services/embeddings.py`, `src/services/vault.py`, `src/mcp_server/tools.py`, `src/auth/session.py`, `src/oauth/routes.py`, and `src/main.py`.
- Existing APIs retain their signatures. Concurrent conflicting mutations now fail safely instead of silently overwriting newer data.
- No database migration or new dependency is required.
