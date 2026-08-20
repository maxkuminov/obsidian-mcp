## Why

Four defects in the OAuth surface all reduce to the same thing: **the panel and
the token endpoint disagree about what a grant is**, and the disagreement
always resolves in favour of more access than the owner intended.

- **#64.** `oauth_tokens` had no identifier tying an access token to the refresh
  token minted beside it, so the panel could only offer per-row controls — and
  both were near no-ops. Revoking the access row left the refresh token to mint
  a fresh, identically-scoped pair on the client's next 401 retry (access
  tokens live one hour). Downgrading the access row silently reverted, because
  `_handle_refresh` copies the *refresh* token's scope. The revoked row then
  vanished from the page, so the operator was shown a blank space that read as
  success.
- **#67.** `update_oauth_token_scope` validated the submitted scope against the
  literal tuple `('read','readwrite')` and never clamped it against
  `OAuthClient.scope`. A client registered read-only via DCR could be raised to
  `readwrite` from the panel — permanently, since rotation re-minted from the
  token's own scope forever after.
- **#68.** A client binds to its first authorizing user and never rebinds, so a
  second user authorizing the same `client_id` got live tokens under a client
  they do not own: invisible in their panel (which filters by
  `OAuthClient.user_id`) and destroyable by the owner's Delete cascade.
- **#76 (OAuth half).** `oauth.html` printed "Active" from `revoked`/`expired`
  alone while `APIKeyMiddleware` additionally requires the owning
  `User.is_active`. A deactivated user's tokens were dead and badged green.
- **#65 (gaps 1–2).** The tests shipped with #62 built the template context by
  hand, so `oauth_page`'s `has_write` derivation was untested, and one
  assertion exercised only Python's `in` operator.

## What Changes

- **`oauth_tokens.grant_id`** (migration 014): opaque, indexed, NOT NULL. One
  value per consent event; both tokens minted from one `/authorize` approval
  share it and every rotation inherits it. Backfilled one id per distinct
  `(client_id, user_id)`.
- **Family-scoped writes.** `revoke_oauth_token`, `update_oauth_token_scope`
  and the RFC 7009 `/revoke` endpoint act on every non-revoked token in the
  family, in one transaction, under a transaction-scoped advisory lock that a
  concurrent `_handle_refresh` also takes — so a rotation cannot slip a new
  pair past a revocation's snapshot.
- **One scope definition.** New dependency-free `src/oauth/scope.py` holding
  `clamp_scope`, `client_can_write` and `token_has_write`. The OAuth routes,
  the ASGI auth middleware and the control panel all use it.
- **The registration is the cap, everywhere.** The panel refuses `readwrite`
  for a client not registered for it, `oauth.html` does not render the option,
  and `_handle_refresh` re-clamps on every rotation.
- **Cross-user client reuse is refused** in `authorize_post`.
- **The panel shows what is true.** Revoked and expired rows stay listed
  (dimmed, history capped per grant), one "Revoke access" control per grant
  instead of one per row, and an "Owner inactive" badge derived from the
  owner's `User.is_active`.

## Capabilities

### Modified Capabilities
- `oauth-authorization-integrity`: grant families, family-scoped revocation and
  scope changes, the registered-scope cap on every path, cross-user client
  refusal, and effective credential status in the panel.

## Impact

- `alembic/versions/014_oauth_grant_id.py` — new
- `src/oauth/scope.py`, `src/oauth/grants.py` — new
- `src/models/db.py` — `OAuthToken.grant_id`
- `src/oauth/routes.py` — stamp, inherit, re-clamp, cross-user refusal,
  family-scoped `/revoke`
- `src/control_panel/routes.py` — `oauth_page`, `revoke_oauth_token`,
  `update_oauth_token_scope`
- `src/control_panel/templates/oauth.html` — per-grant rendering
- `src/mcp_server/auth.py` — one-line swap to the shared helper
- `tests/` — `test_issue_64_grant_families.py`,
  `test_issue_67_panel_scope_clamp.py`,
  `test_issue_68_cross_user_client.py`, rewritten
  `test_oauth_panel_scope_display.py`, `_oauth_grant_fakes.py`, and seven new
  cases in `tests/integration/test_schema_check.py`

Carries a migration, so `make test-schema` is a required gate and
`make db-check` must report "No new upgrade operations detected" after deploy.
