# oauth-authorization-integrity Specification

## Purpose
TBD - created by archiving change harden-cross-layer-integrity. Update Purpose after archive.
## Requirements
### Requirement: OAuth consent revalidates the browser session
Both OAuth authorization display and approval SHALL resolve the session identity from the database and require an active user whose `session_version` matches the signed session. Missing, deleted, inactive, or version-mismatched identities MUST NOT mint an authorization code and SHALL have their session cleared.

#### Scenario: Password reset invalidates consent session
- **WHEN** a user's password reset increments `session_version` after the browser cookie was issued
- **THEN** an OAuth authorization GET or approval POST using that cookie SHALL require authentication again
- **AND** no authorization code SHALL be minted

#### Scenario: User is deactivated before approval
- **WHEN** an authenticated user is deactivated before submitting OAuth approval
- **THEN** approval SHALL be rejected as unauthenticated
- **AND** no authorization code SHALL be minted

