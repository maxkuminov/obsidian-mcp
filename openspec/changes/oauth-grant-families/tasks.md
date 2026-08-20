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

## 6. Tests

- [x] 6.1 Rewrite `tests/test_oauth_panel_scope_display.py` to exercise the real `oauth_page` (#65 gap 1) and replace the tautological assertion with calls into `token_has_write` (#65 gap 2)
- [x] 6.2 `tests/_oauth_grant_fakes.py` — statement-interpreting fake session so family writes are observable
- [x] 6.3 `tests/test_issue_64_grant_families.py` — stamping, inheritance, revocation that survives a refresh, downgrade that does not revert, family isolation, lock ordering, RFC endpoint
- [x] 6.4 `tests/test_issue_67_panel_scope_clamp.py` — panel refusal, option gating, rotation re-clamp, helper unit tests
- [x] 6.5 `tests/test_issue_68_cross_user_client.py` — refusal, no rebinding, owner/unbound/single-user paths, deny path
- [x] 6.6 Seven new cases in `tests/integration/test_schema_check.py` for 014's backfill, NOT NULL, index name, idempotence and downgrade; update the head-revision assertions to `014`

## 7. Gates

- [x] 7.1 `pytest --ignore=tests/integration` green (995 passed, 5 skipped; baseline 940/5)
- [x] 7.2 `make test-schema` green (20 passed) — includes `alembic check` clean on every path
- [x] 7.3 `openspec validate oauth-grant-families --strict`
- [ ] 7.4 Adversarial Codex pass (orchestrator-run)
- [ ] 7.5 Deploy + `make db-check` (orchestrator-run)
