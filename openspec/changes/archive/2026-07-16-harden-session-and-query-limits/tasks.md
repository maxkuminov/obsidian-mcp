## 1. Query Resource Limits

- [x] 1.1 Clamp ordinary MCP query/list limits to 500
- [x] 1.2 Clamp semantic search limits to 50 before calculating overfetch
- [x] 1.3 Add regression tests for huge, zero, and negative limits

## 2. Session Invalidation

- [x] 2.1 Add `users.session_version` to the model and Alembic migration
- [x] 2.2 Store the version at login and bootstrap
- [x] 2.3 Reject and clear sessions with missing or stale versions
- [x] 2.4 Increment the version on password reset
- [x] 2.5 Add regression tests for valid and stale sessions

## 3. OAuth Loopback Consent

- [x] 3.1 Derive the OAuth state cookie's Secure flag from `BASE_URL`
- [x] 3.2 Add HTTPS and loopback HTTP cookie regression tests

## 4. Verification

- [x] 4.1 Run the focused regression tests
- [x] 4.2 Run the full non-hanging unit suite (234 passed, 5 skipped); isolate the pre-existing hanging `test_issue_22_indexer_cleanup_on_aenter_failure` regression
