Refs #325, #326

## Why

The 2026 ASVS follow-up filed two medium findings against the OAuth token endpoint. Both are about a grant family outliving the evidence that it should die.

**#325 — ASVS V10.4.2: a replayed authorization code cannot revoke what the first exchange issued.** `_handle_auth_code` locks the code row with `used == False` in the WHERE clause, so a second presentation of a spent code matches nothing and is refused as `invalid_grant.unknown_code`. Even if it did match, `oauth_codes` records no lineage to the `grant_id` the first exchange minted, and `cleanup_expired_tokens` deletes every `used` code on the next five-minute tick. So the one signal RFC 6749 §4.1.2 defines for a stolen code — the legitimate client's exchange failing because somebody else redeemed first — is thrown away, and the thief keeps a `read` or `readwrite` family for its full life.

**#326 — ASVS V10.4.8 / V10.7.2: refresh grants have no absolute lifetime, and consent does not say how long access lasts.** Access tokens live one hour and each refresh token 30 days, but every rotation mints a fresh 30-day refresh token. A party that rotates at least once a month — the legitimate connector, or a thief who holds the current refresh token and avoids a reuse collision — keeps the grant for ever. The consent page states neither the one-hour access lifetime, the sliding 30-day refresh lifetime, nor that there is no end.

## What Changes

- **Spent codes keep their lineage.** `oauth_codes` gains `grant_id`, written in the same transaction that marks the code used and mints the family. The cleanup stops deleting used codes on sight: every code, spent or not, is retained until seven days past its `expires_at` — the retention token rows already get — so a spent code is kept for at least seven days after it was spent.
- **A replayed code revokes its family, but only after it proves it is the real code exchange.** The code row is looked up by hash alone (no `used` predicate, no caller `client_id`), locked, and then the client is authenticated, the `redirect_uri` compared and the PKCE verifier checked exactly as on a first exchange. Only a replay that passes all three revokes the linked family, under `lock_grant`, via `revoke_grant_family`. A replay that fails any of them refuses and revokes nothing — revocation keyed on a code hash alone would hand anyone who saw a code (proxy log, browser history, referrer) a way to end someone's grant. The response is byte-identical to the unknown-code refusal. One WARNING, `oauth_code_replay_detected`, on the path that actually killed live tokens.
- **Every grant family carries an absolute lifetime.** `oauth_tokens` gains `grant_issued_at`, set once at the code exchange and copied verbatim by every rotation — the same inheritance rule `grant_id` follows. The family's deadline is `grant_issued_at + OAUTH_GRANT_ABSOLUTE_LIFETIME_DAYS` (new setting, default **90**, range 1–365, not disable-able). Every minted access and refresh token is clamped to that deadline, `expires_in` reports the clamped value, a live refresh token presented at or past the deadline is refused (`invalid_grant`, "re-authorize", nothing revoked) while a rotated-away one is still reuse (family revoked, constant response), the MCP middleware refuses an access token whose family is past it, and the transfer subsystem — which re-validates the minting OAuth credential independently at mint, at redemption and inside the publish gate — treats the credential's effective expiry as `min(token expires_at, grant deadline)`, so a pending upload or download capability dies with its grant. The panel shows such a grant as expired.
- **Migration 029 starts the clock at migration time for every existing family.** Pre-existing token rows are backfilled with the migration's transaction timestamp, so no connector is logged out by the deploy; each existing family must re-authorize at most 90 days after it.
- **Consent discloses the effective lifetimes**: a one-hour access token, renewed by a refresh token valid for 30 days from its last renewal, renewal ending at the absolute cap counted from when the application first receives its tokens (the code exchange that follows approval — `grant_issued_at` — not the approval click itself; computed from the setting, never longer), and where to revoke. Under the nonce CSP: no inline style or handler.

## Capabilities

### New Capabilities

(none)

### Modified Capabilities

- `file-transfer`: an OAuth-minted capability's credential expiry is `min(token expires_at, grant deadline)` in the mint window, at redemption and in the locked pre-publication re-validation.
- `oauth-authorization-integrity`: authorization-code replay revokes the issued family after full revalidation; spent-code lineage and retention; absolute grant-family lifetime inherited across rotation and enforced at refresh, at the middleware and in the panel; consent lifetime disclosure; the setting. The client-expiry requirement's rationale is corrected (a used code is no longer deleted the instant it is spent) and the panel-status requirement gains the absolute deadline.
- `schema-integrity`: migration 029 owns `oauth_codes.grant_id` and `oauth_tokens.grant_issued_at` as one marked unit, backfills the latter with its own timestamp, and leaves `alembic check` clean.

## Impact

- `alembic/versions/029_oauth_grant_lifetime.py` (new); `src/models/db.py` (`OAuthCode.grant_id`, `OAuthToken.grant_issued_at`, marker constants).
- `src/oauth/routes.py` — `_handle_auth_code` (lookup, check order, replay branch, lineage write, clamp, `expires_in`), `_handle_refresh` (inherit, deadline check, clamp, `expires_in`), `authorize_get` (lifetime context).
- `src/oauth/grants.py` — deadline and clamp helpers, consent-lifetime text.
- `src/mcp_server/auth.py` — absolute-deadline check after the per-token expiry check.
- `src/control_panel/routes.py` — `_token_status` reads the deadline.
- `src/services/transfer.py` — `credential_expires_at` and `_credential_ok` read the grant deadline for an `OAuthToken`.
- `src/control_panel/templates/authorize.html` — lifetime block (CSP-clean).
- `src/services/indexer.py` — `cleanup_expired_tokens` code predicate.
- `src/services/security_events.py` — two new catalogue events.
- `src/config.py`, `.env.example`, `README.md` settings table.
- Docs: `docs/architecture/oauth-and-grants.md`, `docs/architecture/schema-and-migrations.md`, `docs/architecture/file-transfer.md`, `CLAUDE.md` key decisions.
- Tests: unit + real-Postgres (`tests/integration/`), schema gate cases for 029.
- Behaviour visible to users: every existing connector must re-authorize once, ~90 days after deploy, and every new one 90 days after its code exchange (seconds after consent). A client that retries a code exchange whose response it lost now loses that grant (RFC 6749 §4.1.2) and re-authorizes.
- Mandatory adversarial Codex pass (auth/token surface). Owner browser pass on the consent page with devtools open; zero CSP violations.
- Closes #325, #326.
