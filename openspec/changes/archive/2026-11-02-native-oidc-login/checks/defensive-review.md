PASS. No plausible security-boundary or first-login failures found in `b5d5c0d..HEAD`.

Confirmed:

- PKCE S256, nonce, signed RS256 ID token, exact issuer, singleton audience, optional `azp`, expiry/issued-at, browser-bound single-use state, and exact typed group controls.
- Explicit subject-to-existing-user mapping only; no provisioning, email linking, role/vault mutation, or single-user/sandbox bypass.
- Callback delegates to the guarded `start_session` mint with session-version validation.
- Provider credentials remain transient and callback query logging is redacted.
- Local MCP OAuth issuer/tokens/grants, API keys, user IDs, and vault ownership remain unchanged.
- OIDC disables bootstrap; the local-password switch hides and refuses password login while preserving a recovery configuration.
- Keycloak-specific flow is compatible with the actual Authlib/JOSE client path. Direct Google remains unsupported.
- Documented upstream logout/offboarding limitations match implementation.

Validation: broader authentication/session regression set passed, `612 passed in 43.71s`. This was not a live Keycloak/browser test.

```json
{"verdict":"PASS","findings":[]}
```