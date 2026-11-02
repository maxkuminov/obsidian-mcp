## ADDED Requirements
### Requirement: Optional existing-account OIDC
The server SHALL keep OIDC disabled by default and require HTTPS issuer/callback, confidential client credentials, a required group and explicit immutable subject-to-existing-user mappings when enabled.
#### Scenario: Unmapped or unentitled identity
- **WHEN** a valid ID token has an unknown subject or lacks the exact required group
- **THEN** login is denied without provisioning, changing roles or assigning vaults
### Requirement: Browser login transaction
The server SHALL bind a short-lived single-use state, nonce and PKCE S256 transaction to the initiating browser and validate signed ID-token issuer, audience, expiry, nonce and subject before starting the existing revocable app session.
#### Scenario: Replayed or cross-browser callback
- **WHEN** callback state is expired, consumed or bound to another browser
- **THEN** login is denied and no app session is created
#### Scenario: Existing account disabled concurrently
- **WHEN** the account is disabled or its session version changes during login
- **THEN** the existing session mint guard refuses login
### Requirement: Preserve MCP identity and safe rollout
The server SHALL preserve user IDs, vault ownership, local MCP issuer, grants, tokens and API keys, disable bootstrap while OIDC is enabled, and enforce an explicit local password-login switch.
#### Scenario: MCP authorization after SSO
- **WHEN** an authorized existing user signs in from the local authorize flow
- **THEN** the safe local next path resumes that same flow as the same user
#### Scenario: Password login disabled
- **WHEN** local login is disabled
- **THEN** its form is hidden and password POST is refused
### Requirement: Safe activation and provider profile
The server SHALL reject startup with OIDC enabled unless multi-user mode is enabled and sandbox mode is disabled, reject multi-user configuration with both login methods disabled, and require positive integer mapping values. Missing mapped database users SHALL fail login closed. The supported provider profile SHALL be Keycloak with same-origin HTTPS discovery, authorization, token and JWKS endpoints; direct Google login is unsupported.
#### Scenario: Single-user exposure
- **WHEN** OIDC is enabled in single-user mode
- **THEN** startup fails before routes serve
### Requirement: Strict protocol and entitlement validation
The server SHALL require exact discovery issuer, RS256 signature validation against issuer JWKS, nonempty string iss/sub/nonce, integer exp/iat, aud equal to the client ID or its singleton array, azp equal to client ID if present, and zero clock skew. Discovery endpoints SHALL use HTTPS on the issuer origin without credentials or fragment; HTTP requests SHALL disable redirects/environment proxies and use bounded time and response size. The groups claim SHALL be an array of at most 256 strings of at most 512 characters; admission requires exact case-sensitive element equality.
#### Scenario: Wrong audience or malformed groups
- **WHEN** a signed token includes an extra audience, foreign azp, scalar groups or case-variant group
- **THEN** login is denied
### Requirement: Ephemeral provider credentials
The server SHALL request only openid and profile scopes, never offline_access, and keep provider codes/tokens only transiently during callback. They SHALL NOT enter cookies, app sessions, databases, caches, events, exceptions or application access logs. Callback tokens SHALL be discarded before app-session mint; deployments SHALL suppress callback query logging at their external proxy.
#### Scenario: Callback audit
- **WHEN** a callback succeeds or fails
- **THEN** application logs contain no callback query, provider credential or subject
### Requirement: Bounded transactions and audit
The server SHALL atomically consume transactions once, expire them after five minutes, clean expired entries before admission and refuse new starts with Retry-After when all 1024 slots are live. Per-address start/callback limits and a process-global start budget SHALL bound public requests. Catalogued bounded events SHALL record start/success/refusal, with success only after app-session commit and closed refusal reasons, without state/nonce/verifier/client-secret/token/subject/raw-groups or exception strings.
#### Scenario: Full store and repeated callback
- **WHEN** capacity is full or state was already consumed
- **THEN** new admission fails without evicting a live transaction and callback cannot mint a session
