## Context

The server coordinates mutable vault files, PostgreSQL-derived indexes, browser sessions, and layered ASGI middleware. Several operations were locally correct but violated their end-to-end contract when interrupted or raced by another actor.

## Goals / Non-Goals

**Goals:**

- Fail safely under concurrent vault mutation and partial indexing failure.
- Preserve existing tool and HTTP interfaces while tightening their guarantees.
- Make authentication and request-routing paths share one authoritative validation boundary.
- Add deterministic regression coverage for every confirmed failure mode.

**Non-Goals:**

- Distributed locking across Obsidian and every external filesystem client.
- A schema migration solely to record link-backfill completion.
- Transactional atomicity for cross-filesystem moves.
- Redesigning OAuth token revocation or cookie policy.

## Decisions

### Use atomic filesystem primitives for no-clobber operations

Creation and non-overwrite writes use an atomic create/link operation rather than an `exists()` check followed by `os.replace()`. Moves use a no-replace path and a safe cross-filesystem fallback. This preserves the API contract even when another actor creates the destination concurrently.

### Use optimistic content comparison for read-modify-write tools

Edits, frontmatter changes, and backlink rewrites compare the content read with the content still present immediately before replacement. A mismatch produces a conflict rather than overwriting a newer external edit. This avoids server-wide locks that cannot coordinate with Obsidian itself.

### Commit one coherent derived-index snapshot

Metadata, deletion cleanup, FTS vectors, and link rows are committed together. A failed pass rolls back the metadata hash/path signal as well, ensuring the next scan retries. Link backfill is scoped per user and committed only after that user's complete pass.

### Validate provider cardinality before replacing embeddings

An embedding batch is successful only when it returns exactly one vector per requested chunk. Empty/fully-cleaned notes explicitly record their current content hash with zero vectors.

### Reuse browser-session validation in OAuth consent

OAuth consent GET and approval POST call the same database-backed session resolver used elsewhere, including active-user and `session_version` checks. Invalid sessions are cleared before any authorization code can be minted.

### Re-enter the application stack for root MCP fallback

The root fallback rewrites the ASGI path to the canonical MCP path and invokes the wrapped application instead of calling the terminal MCP handler directly. The rewritten path prevents recursion while retaining TrustedHost, CORS, proxy, session, security-header, and compression behavior.

## Risks / Trade-offs

- **A non-cooperating writer can race after the optimistic comparison** → Keep the comparison adjacent to atomic replacement and return explicit conflicts for detected changes.
- **Intermediate directory symlinks can still change during pathname traversal** → Reject final-component symlinks and retain resolved containment checks; fully descriptor-relative traversal remains future hardening.
- **A vault with zero links is backfilled on each startup** → Accept harmless extra scanning rather than introduce a migration-only completion marker.
- **Cross-filesystem moves cannot be one atomic rename** → Preserve no-clobber and source durability, and document the limitation.

## Migration Plan

No schema migration is required. Deploy normally after tests and strict specification validation. Rollback is a code rollback; no stored data format changes are introduced.

## Open Questions

- Whether a future migration should add explicit per-user link-backfill completion state.
- Whether Linux `openat2`/`renameat2` support should be used for full intermediate-symlink and no-replace guarantees where available.
