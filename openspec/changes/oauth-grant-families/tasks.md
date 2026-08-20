## 1. Shared scope vocabulary (#67, #65 gap 1)

- [x] 1.1 Add `src/oauth/scope.py` with `scope_set`, `client_can_write`, `token_has_write`, `clamp_scope` and `VALID_SCOPES` — dependency-free so every layer can import it without a cycle
- [x] 1.2 Point `src/oauth/routes.py` at it (`_clamp_scope` is now the shared helper; `authorize_get`'s `client_can_write` comes from it)
- [x] 1.3 Swap `src/mcp_server/auth.py`'s private membership check for `token_has_write`
- [x] 1.4 Point `oauth_page`'s `has_write` at `token_has_write` and its per-client `can_write` at `client_can_write`

## 2. Grant families (#64)

- [x] 2.1 Add `OAuthToken.grant_id` (`String(64)`, NOT NULL, `index=True`) to `src/models/db.py`
- [x] 2.2 Write `alembic/versions/014_oauth_grant_id.py`: add nullable, backfill one id per `(client_id, user_id)` with `IS NOT DISTINCT FROM` for NULL users, verify no NULL remains, `SET NOT NULL`, create `ix_oauth_tokens_grant_id`; guard the backfill on `grant_id IS NULL` so a re-run cannot re-stamp; `SET LOCAL` and `RESET` the timeouts
- [x] 2.3 Add `src/oauth/grants.py`: `new_grant_id`, `grant_lock_key`, `lock_grant`, `revoke_grant_family`, `set_grant_family_scope`
- [x] 2.4 Stamp a fresh `grant_id` on both tokens in `_handle_auth_code`
- [x] 2.5 Inherit `old_token.grant_id` in `_handle_refresh`, and take the family lock before reading the family
- [x] 2.6 Make `revoke_oauth_token` revoke the whole family (access tokens included)
- [x] 2.7 Make `update_oauth_token_scope` write the whole family
- [x] 2.8 Make the RFC 7009 `/revoke` endpoint family-scoped

## 3. The registration is the cap (#67)

- [x] 3.1 `update_oauth_token_scope` refuses `readwrite` when the client is not registered for it, and clamps whatever it does write
- [x] 3.2 `oauth.html` renders the `readwrite` option only when `client.can_write`
- [x] 3.3 `_handle_refresh` re-clamps the rotated scope against the client's current registration and returns the clamped value in the token response
- [x] 3.4 `_handle_auth_code` clamps the code's scope against the client row it already loads, so the exchange is not the one unclamped write path

## 4. Cross-user client reuse (#68)

- [x] 4.1 Add `_client_belongs_to_another_user` and refuse in `authorize_post` before any code is minted; leave `authorize_get` alone
- [x] 4.2 Re-check in `_handle_auth_code` before the code is marked used, closing the race where two users consent to the same *unbound* client concurrently and only one claim wins

## 5. Panel display (#64 history, #76 OAuth half)

- [x] 5.1 `oauth_page` groups tokens into grants, includes revoked/expired rows, caps history per grant and bounds the per-client scan
- [x] 5.2 Derive effective status including the owner's `User.is_active`; add the "Owner inactive" badge
- [x] 5.3 Rewrite `oauth.html` for one block per grant: one "Revoke access" control, one scope select, dimmed history rows, "+ N earlier" line
- [x] 5.4 Age-gate the revoked branch of `cleanup_expired_tokens` on `expires_at` (it had no age condition at all, so the indexer deleted every revocation's evidence within 5 minutes); document why `expires_at` and not `created_at`

## 6. Tests

- [x] 6.1 Rewrite `tests/test_oauth_panel_scope_display.py` to exercise the real `oauth_page` (#65 gap 1) and replace the tautological assertion with calls into `token_has_write` (#65 gap 2)
- [x] 6.2 `tests/_oauth_grant_fakes.py` — statement-interpreting fake session so family writes are observable
- [x] 6.3 `tests/test_issue_64_grant_families.py` — stamping, inheritance, revocation that survives a refresh, downgrade that does not revert, family isolation, lock ordering, RFC endpoint
- [x] 6.4 `tests/test_issue_67_panel_scope_clamp.py` — panel refusal, option gating, rotation re-clamp, helper unit tests
- [x] 6.5 `tests/test_issue_68_cross_user_client.py` — refusal, no rebinding, owner/unbound/single-user paths, deny path
- [x] 6.6 Seven new cases in `tests/integration/test_schema_check.py` for 014's backfill, NOT NULL, index name, idempotence and downgrade; update the head-revision assertions to `014`
- [x] 6.7 `tests/test_issue_64_token_cleanup_retention.py` — structural comparison of the emitted WHERE clause plus a small real evaluator, so the retention window is pinned by behaviour and not by reading one bind parameter

## 8. Adversarial round 1 (Codex FAIL: 1 BLOCKER, 6 MAJOR, 2 MINOR)

- [x] 8.1 BLOCKER — bootstrap vs mint race: both token handlers take the bootstrap advisory lock (key shared from `src/oauth/grants.py`; value unchanged for rolling deploys) and refuse to mint a NULL-owner token under `multi_user_mode`
- [x] 8.2 MAJOR — `/revoke` authenticates the client per its registered method and requires a submitted `client_id` to match the token's; a foreign or unknown token stays a uniform 200 (RFC 7009 §2.2)
- [x] 8.3 MAJOR — the first-authorizer claim is `UPDATE ... WHERE user_id IS NULL RETURNING`, with a fresh-snapshot re-read and a refusal when the winner is another user
- [x] 8.4 MAJOR — `_handle_refresh` rejects a grant whose owner is not the client's; `src/mcp_server/auth.py` rejects the same mismatch for access tokens
- [x] 8.5 MAJOR — `oauth_page` reads live rows unbounded and caps only history, so no live grant can be pushed off the page
- [x] 8.6 MAJOR — a mixed-scope family reports `any(token_has_write(...))`, is marked "mixed", and a scope write makes it uniform across the family
- [x] 8.7 MAJOR — 014 verifies a pre-existing `grant_id` column instead of patching it: refuses a wrong type, a squatting index name, any NULL row, or any id spanning two owners
- [x] 8.8 MINOR — `clamp_scope` fails closed on an empty intersection (section 3 above)
- [x] 8.9 MINOR — fakes match the literal `pg_advisory_xact_lock` and honour LIMIT/OFFSET; `tests/integration/test_oauth_grants_pg.py` adds real-Postgres coverage of revoke-vs-refresh ordering (deterministically gated, not a `gather` and a hope), concurrent first consent, and client-authenticated `/revoke`

## 9. Adversarial round 2 (Codex: 1 BLOCKER, 2 MAJOR, 1 MINOR)

- [x] 9.0 Rebase onto `origin/main` (three merged slices; `src/mcp_server/auth.py` and `src/services/vault.py` overlapped). The round-2 BLOCKER — an ownerless OAuth token accepted in multi-user mode — is what main's `ownerless_credential` check rejects; pinned in this branch's suite too, end to end through the middleware
- [x] 9.1 MAJOR — `/revoke` requires `client_id` present **and** equal; absence was a universal bypass because a public client authenticates trivially
- [x] 9.2 MAJOR — `_token_status` includes `has_vault_scope`, rendering a distinct "No vault scope" state with no scope or revoke control
- [x] 9.3 MINOR — 014's index check reads `pg_index` (table, `indisvalid`, `indpred`, `indexprs`, exact key attnums); partial / expression / multi-column / wrong-table / INVALID impostors all refused
- [x] 9.4 MINOR — the fake matches only the exact normalized lock statement and interprets both `expires_at` directions (and the history disjunction as a disjunction); `tests/test_oauth_grant_fakes_fidelity.py` pins both, verified by mutation

## 7. Gates

- [x] 7.1 `pytest --ignore=tests/integration` green (1213 passed, 5 skipped, post-rebase)
- [x] 7.2 `make test-schema` green (34 passed) — includes `alembic check` clean on every path
- [x] 7.3 `openspec validate oauth-grant-families --strict`
- [x] 7.4 Adversarial Codex rounds 1 and 2 addressed (sections 8 and 9); re-run is orchestrator's
- [ ] 7.5 Deploy + `make db-check` (orchestrator-run)
