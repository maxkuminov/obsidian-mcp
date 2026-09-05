## ADDED Requirements

### Requirement: A browser session SHALL be a server-side row, not a signed cookie alone

Every authenticated browser session in multi-user mode SHALL be represented by a row in a server-side session registry, and a signed session cookie SHALL NOT by itself be sufficient to authenticate a request. The cookie SHALL carry a session identifier generated from a cryptographically secure random source of at least 256 bits; the registry SHALL store only a one-way hash of that identifier, never the identifier itself, so that a database dump contains no usable session credential.

Each row SHALL record the owning user, the time it was created, the time it was last seen, the time it expires, and whether and when it was revoked. It MAY record a hash of the creating request's user-agent for forensic purposes only; a user-agent value SHALL NOT be used as an authorization input, because it is replayed by any party holding the cookie and changes on ordinary browser updates.

Sessions SHALL be minted at exactly three points — a successful login, bootstrap administrator registration, and the re-issue that follows a self-service password change — and by a single implementation shared by all three, so that no future handler can create a cookie with no row behind it.

Single-user mode SHALL be unaffected: no session row is created, read, or required, because that mode resolves identity from a sentinel before any session is consulted.

#### Scenario: Login creates a row

- **WHEN** a user signs in successfully in multi-user mode
- **THEN** a session row SHALL exist for that user with a future expiry and no revocation time
- **AND** the session cookie SHALL carry an identifier that resolves to that row

#### Scenario: The identifier is not stored

- **WHEN** the session registry is read directly
- **THEN** no column SHALL contain the identifier carried by the cookie
- **AND** the stored value SHALL be a one-way hash of it

#### Scenario: Bootstrap registration creates a row

- **WHEN** the first administrator is created through bootstrap registration and signed in
- **THEN** a session row SHALL exist for that administrator

#### Scenario: Single-user mode creates no rows

- **WHEN** the panel is used with multi-user mode disabled
- **THEN** no session row SHALL be created or required

### Requirement: Session validation SHALL consult the registry on every request that resolves a browser identity

Every request that resolves a browser identity SHALL require the cookie to carry both a user reference and a session identifier, and SHALL resolve that identifier against the registry. The request SHALL be refused when the row is absent, when it carries a revocation time, when its expiry has passed, when the referenced user does not exist or is not active, or when the account-wide session version does not match the cookie. All of these checks SHALL be applied together; passing any subset SHALL NOT authenticate the request.

A cookie that carries a user reference but no session identifier SHALL be refused rather than accepted for compatibility, so that cookies issued before the registry existed stop being credentials at the moment the registry ships.

Every refusal SHALL clear the session cookie, so a rejected cookie cannot be replayed against a different route.

There SHALL be exactly one implementation of this validation, and every entry point that resolves a browser identity SHALL use it — including the login page's own "already signed in" short-circuit, which SHALL NOT read the cookie's user reference directly. A revoked cookie reaching a raw read there sends the visitor into a redirect loop between the login page and the panel.

#### Scenario: A replayed cookie after logout is refused

- **WHEN** a session cookie captured before logout is replayed against a panel route after that session has been logged out
- **THEN** the request SHALL be refused with a redirect to the login page
- **AND** no user identity SHALL be resolved from it

#### Scenario: A pre-registry cookie is refused

- **WHEN** a correctly signed session cookie carrying a user reference but no session identifier is presented
- **THEN** the request SHALL be refused and the cookie SHALL be cleared

#### Scenario: An expired row is refused while the cookie signature is still valid

- **WHEN** a cookie whose signature has not yet aged out is presented and its session row has passed its expiry
- **THEN** the request SHALL be refused

#### Scenario: A revoked row is refused

- **WHEN** a cookie is presented whose session row carries a revocation time
- **THEN** the request SHALL be refused

#### Scenario: An inactive user is refused even with a live row

- **WHEN** a cookie is presented whose session row is live but whose user has been deactivated
- **THEN** the request SHALL be refused

#### Scenario: The login page uses the same validation

- **WHEN** a revoked or expired cookie is presented to the login page
- **THEN** the login form SHALL be rendered
- **AND** the visitor SHALL NOT be redirected to the panel

#### Scenario: A valid session still works

- **WHEN** a cookie whose row is live, unrevoked and unexpired is presented by an active user whose session version matches
- **THEN** the request SHALL be authenticated as that user

### Requirement: Logout SHALL revoke the session server-side

The panel's logout handler SHALL record a revocation time on the session row named by the presenting cookie before clearing the cookie, so that the cookie ceases to be a credential for every subsequent request rather than merely disappearing from the browser that submitted the logout.

If the revocation cannot be written, the handler SHALL still clear the cookie and redirect to the login page, and SHALL record an error-level event. Refusing the sign-out would leave the user signed in and the cookie live, which is worse than the state the handler is trying to leave.

Logout SHALL revoke only the presenting session. Ending a user's other sessions is the business of the account-level events specified below.

#### Scenario: Logout revokes the presenting session

- **WHEN** a signed-in user submits the logout form
- **THEN** that session's row SHALL carry a revocation time
- **AND** a subsequent request replaying the same cookie SHALL be refused

#### Scenario: Other sessions survive a logout

- **WHEN** a user with two live sessions logs out of one of them
- **THEN** the other session SHALL remain usable

#### Scenario: A failing revocation still signs the browser out

- **WHEN** the revocation write fails during logout
- **THEN** the cookie SHALL still be cleared and the response SHALL still redirect to the login page
- **AND** an error-level event SHALL be recorded

### Requirement: Session revocation SHALL be written in the caller's transaction, never committed by the helper

The helpers that revoke one session or all of a user's sessions SHALL issue their write on the database session they are given and SHALL NOT commit; the calling handler SHALL commit as part of its own transaction. Handlers that hold the administrative critical section take a transaction-scoped advisory lock and must not commit between taking it and writing the flags it protects, so a helper that committed would silently break the last-administrator guard from inside.

Revoking all of a user's sessions SHALL affect only rows that are not already revoked, so a re-revocation does not rewrite historical revocation times.

#### Scenario: The revocation and the write it accompanies are atomic

- **WHEN** an account-level write that revokes sessions fails and its transaction is rolled back
- **THEN** no session SHALL have been revoked

#### Scenario: The administrative lock is not broken

- **WHEN** a handler holding the administrative advisory lock revokes a target's sessions
- **THEN** no commit SHALL occur between the lock being taken and the protected flags being written

#### Scenario: Already-revoked rows keep their original revocation time

- **WHEN** a user's sessions are revoked twice
- **THEN** the rows revoked by the first call SHALL retain their original revocation time

### Requirement: Session last-seen tracking SHALL be throttled and SHALL NOT be able to fail a request

A validated request SHALL update its session's last-seen time only when the stored value is older than a configured interval, and SHALL perform that update outside the request handler's own transaction so that it is neither lost to a handler rollback nor able to interfere with a handler's critical section. A failure to record it SHALL be logged and swallowed: the value is telemetry and nothing authorizes on it.

#### Scenario: A fresh session is not written on every request

- **WHEN** several authenticated requests are made within the throttle interval
- **THEN** at most one last-seen update SHALL be issued

#### Scenario: A stale session is touched

- **WHEN** an authenticated request is made after the throttle interval has passed
- **THEN** the session's last-seen time SHALL be updated

#### Scenario: A failing touch does not fail the page

- **WHEN** the last-seen update raises
- **THEN** the request SHALL still be served
- **AND** the failure SHALL be logged

### Requirement: Expired session rows SHALL be purged on the same retention rule as OAuth credentials

The periodic cleanup job SHALL delete session rows whose expiry is more than the configured retention window (seven days) in the past, and SHALL NOT delete a revoked row any earlier than that. Because a session can only be revoked while it exists, a window measured from the expiry guarantees that a revoked row remains readable for at least the full window after the revocation — which is what keeps a deliberate revocation from becoming an absence indistinguishable from a row that never existed.

#### Scenario: A long-expired row is removed

- **WHEN** the cleanup job runs and a session row expired more than the retention window ago
- **THEN** that row SHALL be deleted

#### Scenario: A recently revoked row is retained

- **WHEN** the cleanup job runs and a session was revoked moments ago but has not yet passed its expiry plus the retention window
- **THEN** that row SHALL still exist

#### Scenario: A live session is never purged

- **WHEN** the cleanup job runs while a session is live
- **THEN** that session's row SHALL be untouched

### Requirement: No session identifier, password or password hash SHALL appear in any log record

Records emitted by the session and password paths SHALL NOT contain a session identifier, a password, or a password hash, in any field, message or traceback. Where a record must identify a session it SHALL use a short truncated digest of the identifier in the same form already used for presented bearer credentials, never the identifier itself.

The event names SHALL match the server's security-event catalogue. When the shared emitter is available the records SHALL be emitted through it; otherwise the same names SHALL be emitted through a module logger with the identifying fields rendered into the message text, because the deployed log format emits the message alone.

#### Scenario: A refused replay records no credential

- **WHEN** a revoked cookie is replayed and the refusal is recorded
- **THEN** the record SHALL NOT contain the session identifier carried by that cookie
- **AND** it SHALL NOT contain any substring of it

#### Scenario: A password change records no password

- **WHEN** a password change succeeds or is refused and the outcome is recorded
- **THEN** no record SHALL contain the submitted current password, the new password, or any password hash
