## 1. Index and Embedding Integrity

- [x] 1.1 Commit metadata, FTS, deletion cleanup, and link refresh atomically.
- [x] 1.2 Make link backfill per-user and rollback incomplete backfills.
- [x] 1.3 Validate exact embedding response cardinality before replacing vectors.
- [x] 1.4 Mark zero-chunk notes current with zero embeddings.
- [x] 1.5 Dispose the database engine during application shutdown.

## 2. Vault Mutation Safety

- [x] 2.1 Implement race-safe no-clobber creation, raw writes, and moves.
- [x] 2.2 Add optimistic conflict detection to note edits, frontmatter updates, and backlink rewrites.
- [x] 2.3 Deny hidden paths across note mutation APIs.
- [x] 2.4 Preserve source-relative Markdown links and rewrite moved self-links.
- [x] 2.5 Enforce bounded single-descriptor file reads.

## 3. Authentication and Routing Integrity

- [x] 3.1 Revalidate active user and session version on OAuth consent GET and approval POST.
- [x] 3.2 Route root MCP fallback through the canonical application middleware stack.

## 4. Verification

- [x] 4.1 Run focused regression suites for all three tracks.
- [x] 4.2 Run the complete automated test suite.
- [x] 4.3 Run strict OpenSpec validation.
- [ ] 4.4 Run a fresh OpenSpec implementation verifier and resolve drift.
- [ ] 4.5 Run a fresh adversarial verifier and resolve actionable findings.
