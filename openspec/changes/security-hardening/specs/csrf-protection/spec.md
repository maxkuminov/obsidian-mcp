## ADDED Requirements

### Requirement: CSRF token generation
The system SHALL generate a signed CSRF token for each session using `itsdangerous.URLSafeTimedSerializer` with the application's `secret_key`. The token MUST be included in every HTML form as a hidden input named `csrf_token`. Tokens MUST expire after 1 hour.

#### Scenario: Token embedded in panel form
- **WHEN** a logged-in user loads any panel page containing a POST form
- **THEN** every `<form method="POST">` on that page contains a hidden input `<input type="hidden" name="csrf_token" value="...">` with a valid signed token

#### Scenario: Token embedded in auth forms
- **WHEN** a user loads the login or registration form
- **THEN** the form contains a hidden `csrf_token` input

### Requirement: CSRF token validation on POST
The system SHALL validate the `csrf_token` form field on every POST request to panel and auth routes. If the token is missing, invalid, or expired, the request MUST be rejected with HTTP 403.

#### Scenario: Valid CSRF token accepted
- **WHEN** a POST request includes a valid, unexpired `csrf_token` matching the session
- **THEN** the request proceeds normally

#### Scenario: Missing CSRF token rejected
- **WHEN** a POST request to a panel route omits the `csrf_token` field
- **THEN** the server responds with HTTP 403

#### Scenario: Expired CSRF token rejected
- **WHEN** a POST request includes a `csrf_token` that was generated more than 1 hour ago
- **THEN** the server responds with HTTP 403

#### Scenario: Forged CSRF token rejected
- **WHEN** a POST request includes a `csrf_token` signed with a different secret key
- **THEN** the server responds with HTTP 403

### Requirement: CSRF scope limited to multi-user mode
In single-user mode (where `SessionMiddleware` is not mounted), CSRF token generation and validation SHALL be skipped. Forms SHALL still render without the hidden input. Panel security in single-user mode is delegated to the Traefik SSO middleware.

#### Scenario: Single-user mode skips CSRF
- **WHEN** the application runs in single-user mode and a POST form is submitted without a csrf_token
- **THEN** the request proceeds normally (no 403)

### Requirement: OAuth authorize form excluded from CSRF
The `/authorize` POST endpoint already has its own CSRF protection via a signed `oauth_state` cookie. It SHALL NOT use the panel CSRF token mechanism (it runs outside the panel session context).

#### Scenario: OAuth authorize uses its own CSRF
- **WHEN** a user submits the OAuth authorization form
- **THEN** the existing `oauth_state` cookie-based CSRF check is used, not the panel CSRF token

### Requirement: API key display via session flash
When a new API key is created via the panel form, the raw key MUST be stored in the session (not in a URL query parameter) and displayed once on the next page load. The session entry MUST be cleared after reading.

#### Scenario: New key shown via session flash
- **WHEN** admin creates a new API key via the panel
- **THEN** the raw key is stored in `request.session["flash_new_key"]`, the redirect URL contains no key material, and the keys page displays the key from the session

#### Scenario: Key not visible on page refresh
- **WHEN** admin refreshes the keys page after seeing the new key
- **THEN** the key is no longer displayed (session entry was cleared on first read)
