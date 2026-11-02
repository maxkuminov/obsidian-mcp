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
