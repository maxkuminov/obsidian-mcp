# Native OIDC browser login (#414)

## Why
Existing accounts need centralized browser authentication while retaining their vaults and MCP credentials.

## What Changes
Add optional native OIDC for existing accounts without altering local MCP authorization. Disabled by default; no automatic linking, provisioning, vault assignment or role changes.

## Design
Use pinned Authlib's async OAuth/OIDC client with code + PKCE S256 and verified signed ID-token claims. One configured HTTPS issuer, confidential client, HTTPS callback derived from BASE_URL. Discovery issuer must exactly match configuration and all endpoints must be HTTPS on the issuer origin. Require a configured group in the ID token and an explicit subject-to-existing-positive-user-ID configuration map. Existing users' active flag, session version, admin role and vault path remain authoritative. The issuer namespaces the subject map.

Use single-worker bounded in-memory, single-use login transactions (five-minute expiry, capacity 1024), bound to a random browser session nonce; store PKCE verifier and OIDC nonce server-side, never provider tokens in cookies. Consume transaction before exchange, prevent callback replay even if a signed cookie is restored. Do not hold DB connection during network calls. Safe relative next survives MCP authorize redirects. Existing start_session performs final account/version validation. Public endpoints rate limited; HTTP clients have bounded timeout, TLS verification, no environment proxies and no redirects. ID tokens require RS256, issuer/audience/expiry/nonce/subject. Never log tokens or provider exception strings.

Local login retains default behavior; when explicitly disabled deny its POST and hide form. Enabling OIDC disables public bootstrap registration even when local login remains allowed. Disable OIDC only after restoring local login configuration. Existing MCP tokens/API keys/session revocation remain unchanged. No provider logout/backchannel logout, automatic offboarding, email linking, or new users.

## Accepted limitations and rollout
Login transactions are in memory and lost on restart; one worker is existing app contract. Existing app sessions/MCP credentials survive provider group removal or disablement until app expiry/revocation; operator must revoke locally. Native OIDC is not active until client/group/subject mappings configured, account linkage and MCP consent tested, and proxy Google gate removed. Keep rollback config and strong local recovery password. Max manual browser/CSP pass remains required before production activation.

## Spec review resolutions
Independent review a69f882: accepted activation invariant, strict audience/azp/algorithm/claim types, group array typing, transient credential handling, usable login-method invariant, bounded transaction admission and audit requirements. Intended issuer is Keycloak; Google is retired, not a direct provider. Unknown mapped IDs fail callback closed rather than making a typo prevent all app startup. Exact group is in signed ID token; configuration never infers roles or creates accounts. Additional protocol and rollout details are in design.md. Callback query logging is suppressed inside the app and must also be disabled at its external proxy before activation.
