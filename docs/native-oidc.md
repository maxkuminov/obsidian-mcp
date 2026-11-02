# Native browser SSO

Optional native OIDC signs users into the control panel and the existing MCP consent flow. It does not replace this server's MCP OAuth issuer, client registrations, grants, access/refresh tokens or API keys. It never creates users, changes IDs, roles or vault assignments, or links accounts by email.

The supported profile is **Keycloak**: HTTPS issuer and discovery endpoints on that issuer's origin, authorization code with PKCE S256, confidential client using `client_secret_basic`, RS256 ID tokens, and a signed `groups` array. Direct Google OIDC is unsupported. Native SSO is disabled by default; existing deployments keep their login behavior.

## Configure

1. Back up the database. Create/link the existing local user accounts and record their numeric `users.id` values, current admin flags and vault assignments. Keep a strong local administrator recovery password.
2. Create a confidential Keycloak client with standard code flow enabled, implicit/direct-access flows disabled, PKCE S256 required, and the exact redirect URI `https://notes.example.com/admin/auth/oidc/callback`. No wildcard redirects. Use RS256 and a singleton audience equal to the client ID. Add a Group Membership mapper to the **ID token** named `groups`; configure full paths consistently. The required group must grant this particular application, not the whole household.
3. Copy each account's immutable Keycloak subject (`sub`) into the explicit map to its existing numeric application user ID. Treat the issuer and map together as an identity binding; re-review every binding when changing realms/issuers or recreating users. Unknown mappings, inactive/missing users and missing group membership deny login.
4. Configure the settings below through the deployment's secret/config mechanism. Never commit real credentials. The application must reach the issuer over verified HTTPS; add required cluster egress. It ignores environment proxy variables for these requests. Endpoints must share the issuer origin, without redirects. Use one worker as already required by the server.

```dotenv
MULTI_USER_MODE=true
BASE_URL=https://notes.example.com
OIDC_ENABLED=true
OIDC_ISSUER=https://identity.example.com/realms/household
OIDC_CLIENT_ID=obsidian-mcp
# Supply OIDC_CLIENT_SECRET through your secret mechanism.
OIDC_REQUIRED_GROUP=/apps/obsidian-mcp
OIDC_SUBJECT_USER_IDS={"immutable-subject-from-keycloak":1}
LOCAL_LOGIN_ENABLED=true
```

`OIDC_ENABLED=true` refuses single-user/sandbox mode and incomplete configuration. `BASE_URL` must be an HTTPS origin. `OIDC_SUBJECT_USER_IDS` accepts positive integers, not names or emails. Public bootstrap is closed while OIDC is enabled, including an empty database. Provision local accounts before activation; do not turn multi-user mode off to recover access.

## Activate and verify

Keep `LOCAL_LOGIN_ENABLED=true` during migration. While the old proxy gate exists it may still cause a second login: remove it only when native authentication is ready. The public callback/start/login paths must be reachable for the browser flow to work. Before removing the gate, prove direct backend anonymous `/admin/*` redirects to login and `/api/*` refuses access; keep transport, CSRF and application authentication protections on all paths.

Suppress query strings on `/admin/auth/oidc/callback` in proxy/ingress access logs and tracing. The application filters that callback's Uvicorn access log and emits only bounded security-event metadata. Do not enable HTTP protocol debug logging with provider credentials. Provider JSON responses are limited to 128 KiB; compressed responses are refused, and the client requests identity encoding.

Max's manual browser check is required before production activation: sign in with each intended role, verify the correct existing vault and administrator controls, complete one existing MCP client's authorization/consent round trip, verify existing API keys and refresh tokens still work, test logout and denied group/unmapped identities, and inspect devtools for zero CSP violations. Confirm wrong/unmapped/disabled identities fail. Confirm the recovery login works independently of Keycloak. Once those checks pass, set `LOCAL_LOGIN_ENABLED=false`; this hides the password form and refuses its POST. Remove the old Google gate and retire its credentials as part of that deployment's controlled cutover.

## Recovery and offboarding

For an issuer outage, restore `LOCAL_LOGIN_ENABLED=true` through your deployment configuration and restart, then use the strong local admin password. This is an operator configuration change, not a public bypass URL. You can then disable OIDC; multi-user configuration with both login methods disabled refuses startup. Keep multi-user mode enabled so existing vault boundaries remain enforced.

Provider group removal, password resets and account disablement **do not revoke existing application sessions or MCP credentials**. Deactivate the local account and use the application's existing session/key/grant revocation procedures for immediate offboarding. The default app session lifetime still applies; choose `SESSION_MAX_AGE` to suit your deployment. Re-linking a subject is not a revocation mechanism.

Logout is local: it revokes the app session, but an existing Keycloak browser session can sign in again. No provider logout, backchannel logout, token refresh or provider-token storage is implemented. Provider tokens are discarded after callback validation. Login transactions expire after five minutes, are consumed once, and disappear on restart; retry login after a restart. One outstanding login per browser is supported, so starting again cancels its previous pending attempt. Starts are limited to 5/minute/address and 60/minute/process, callbacks 10/minute/address; the 1024-entry transaction store refuses newcomers rather than evicting live attempts.

Protocol implementation references: [Authlib HTTP clients](https://docs.authlib.org/en/stable/oauth2/client/http/index.html), [joserfc JWT verification](https://jose.authlib.org/en/guide/jwt/).
