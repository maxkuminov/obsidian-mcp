# api-auth-hardening Specification

## Purpose
TBD - created by archiving change security-hardening. Update Purpose after archive.
## Requirements
### Requirement: REST API application-level auth
All `/api/*` endpoints SHALL require an authenticated session via the same `require_user_panel` dependency used by panel routes. Admin-only endpoints (`POST /api/keys`, `DELETE /api/keys/{id}`) SHALL additionally require `require_admin_panel`. Unauthenticated requests MUST receive HTTP 302 (redirect to login) in multi-user mode or rely on Traefik SSO in single-user mode.

#### Scenario: Unauthenticated API request in multi-user mode
- **WHEN** a request to `GET /api/keys` has no valid session cookie and multi-user mode is enabled
- **THEN** the server responds with HTTP 302 redirecting to `/admin/auth/login`

#### Scenario: Authenticated non-admin API request
- **WHEN** a non-admin user with a valid session requests `GET /api/keys`
- **THEN** the server returns only the user's own keys (scoped by user_id)

#### Scenario: Key creation stamped with user_id
- **WHEN** a user creates a key via `POST /api/keys`
- **THEN** the created key has `user_id` set to the authenticated user's id

### Requirement: Timing-safe hash comparison in OAuth
All hash comparisons in the OAuth token endpoint (`client_secret_hash` verification) SHALL use `secrets.compare_digest` instead of `!=` to prevent timing side-channel attacks.

#### Scenario: Client secret verified timing-safely
- **WHEN** the token endpoint compares a client's secret hash
- **THEN** `secrets.compare_digest` is used for the comparison

#### Scenario: Invalid client secret rejected
- **WHEN** an invalid client secret is provided to the token endpoint
- **THEN** the request is rejected with HTTP 401 and the comparison took constant time regardless of which bytes differed

### Requirement: Login rate limiting
The `POST /admin/auth/login` endpoint SHALL be rate-limited to 5 attempts per minute per IP address using slowapi.

#### Scenario: Rate limit enforced on login
- **WHEN** a client submits more than 5 login attempts within one minute from the same IP
- **THEN** subsequent attempts receive HTTP 429

#### Scenario: Normal login not affected
- **WHEN** a client submits fewer than 5 login attempts within one minute
- **THEN** login attempts proceed normally

### Requirement: Session fixation prevention
On successful login, the system SHALL clear the existing session data before populating it with the authenticated user's information. This prevents session fixation attacks where an attacker pre-sets a session cookie.

#### Scenario: Session rotated on login
- **WHEN** a user successfully authenticates via `POST /admin/auth/login`
- **THEN** the session is cleared before `user_id`, `is_admin`, and `username` are written

### Requirement: Secret key validation in all modes
The application SHALL reject `secret_key="changeme"` at startup regardless of whether multi-user mode is enabled. The error message MUST suggest using `secrets.token_hex(32)` to generate a strong key.

#### Scenario: Changeme rejected in single-user mode
- **WHEN** the application starts with `SECRET_KEY=changeme` and `MULTI_USER_MODE=false`
- **THEN** startup fails with a ValueError explaining that SECRET_KEY must be changed

#### Scenario: Strong key accepted
- **WHEN** the application starts with a random SECRET_KEY value
- **THEN** startup proceeds normally

### Requirement: OpenAI key masking
The settings page SHALL display only the last 4 characters of the OpenAI API key, prefixed with `***...`. Keys shorter than 8 characters SHALL display as `***`.

#### Scenario: Long key masked
- **WHEN** the settings page renders with an OpenAI API key of 51 characters
- **THEN** the displayed value is `***...` followed by the last 4 characters

#### Scenario: Short key fully masked
- **WHEN** the settings page renders with an OpenAI API key shorter than 8 characters
- **THEN** the displayed value is `***`

