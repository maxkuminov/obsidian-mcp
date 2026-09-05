## ADDED Requirements

### Requirement: A signed-in user SHALL be able to change their own password

The control panel SHALL expose an account page and a password-change handler available to every signed-in user, administrator or not, requiring the current password, a new password and a confirmation of the new password. The handler SHALL verify the current password and hash the new one using the server's existing password functions, whose documented semantics — truncation of the UTF-8 encoding at 72 bytes and rejection of embedded NUL bytes — SHALL be preserved exactly, so that no stored hash becomes unverifiable.

The administrator-driven password reset SHALL remain available as the recovery path for a user who cannot sign in.

In single-user mode there is no account row and no local password, so the handler SHALL answer as if the route did not exist.

#### Scenario: A user changes their own password

- **WHEN** a signed-in non-administrator submits their correct current password with a valid new password and a matching confirmation
- **THEN** the password SHALL be changed
- **AND** the new password SHALL authenticate at the login form and the old one SHALL NOT

#### Scenario: The route is not administrator-only

- **WHEN** a signed-in user without the administrator role opens the account page
- **THEN** the page SHALL render with the password-change form

#### Scenario: Single-user mode has no account to change

- **WHEN** the password-change handler is reached with multi-user mode disabled
- **THEN** it SHALL answer not-found and change nothing

#### Scenario: The existing hashing semantics are unchanged

- **WHEN** a password longer than 72 UTF-8 bytes is set through this handler
- **THEN** hashing SHALL succeed under the same truncation the server has always applied
- **AND** the resulting hash SHALL verify with that password

### Requirement: A password change SHALL be refused without the correct current password, and the refusal SHALL be throttled

The handler SHALL refuse the change when the submitted current password does not verify against the stored hash, when the new password and its confirmation differ, when the new password is shorter than the server's minimum length, when it contains a NUL byte, or when it is identical to the current password. A refusal SHALL leave the stored hash, the account-wide session version and every session row untouched.

The handler SHALL be rate limited at the login handler's rate under a key composed of the client address **and** the authenticated session's user, so that repeated guessing of the current password is bounded per account as well as per address. Successful and refused attempts SHALL be counted against the same limit, so the limit cannot be drained by guessing.

The minimum password length SHALL be defined once and applied by every path that sets a password — this handler, bootstrap registration and the administrator reset — so that an administrator cannot set a password its owner is then forbidden from setting again. There SHALL be no composition rules. Existing stored passwords SHALL NOT be re-checked against the minimum at login, so raising it does not lock anyone out.

#### Scenario: Wrong current password is refused and changes nothing

- **WHEN** a signed-in user submits an incorrect current password with an otherwise valid new password
- **THEN** the change SHALL be refused
- **AND** the stored hash, the session version and every session row SHALL be unchanged

#### Scenario: Repeated wrong-password attempts are throttled

- **WHEN** the change handler is submitted with an incorrect current password more times than the limit allows within the window, from one browser session
- **THEN** the excess requests SHALL be rejected by the rate limiter
- **AND** the throttle SHALL apply even if the requests arrive from different client addresses under the same session

#### Scenario: Confirmation mismatch is refused

- **WHEN** the new password and its confirmation differ
- **THEN** the change SHALL be refused and nothing SHALL be written

#### Scenario: A too-short password is refused

- **WHEN** a new password shorter than the configured minimum is submitted
- **THEN** the change SHALL be refused and nothing SHALL be written

#### Scenario: A NUL byte is a form error, not a server error

- **WHEN** a new password containing a NUL byte is submitted here, at bootstrap registration, or at the administrator reset
- **THEN** the request SHALL be refused with a form-level message
- **AND** the server SHALL NOT answer with an unhandled error

#### Scenario: Reusing the current password is refused

- **WHEN** the new password verifies against the stored hash
- **THEN** the change SHALL be refused and nothing SHALL be written

#### Scenario: The minimum is shared by every setter

- **WHEN** bootstrap registration or the administrator reset is given a password shorter than the configured minimum
- **THEN** it SHALL be refused by the same rule as the self-service handler

### Requirement: A successful password change SHALL end the user's other sessions and keep the current one

On success the handler SHALL, in one transaction, write the new hash, increment the account-wide session version and revoke every session row belonging to that user — the one that made the request included — and SHALL then mint a fresh session for the requesting browser. The user-visible effect SHALL be that every other device is signed out and the browser that performed the change remains signed in under a new session identifier.

Rotating the identifier rather than retaining it is deliberate: the cookie that was live while the old password was live SHALL stop being a credential too.

If the re-issue fails after the change has committed, the user SHALL be signed out and able to sign in with the new password; the password change SHALL NOT be rolled back on account of a failure to re-issue a session.

#### Scenario: Other sessions are revoked but the current one survives

- **WHEN** a user with sessions on two browsers changes their password from the first
- **THEN** the second browser's next request SHALL be refused and redirected to login
- **AND** the first browser SHALL remain signed in without re-authenticating

#### Scenario: The pre-change cookie of the current browser is also dead

- **WHEN** the cookie the change was submitted with is replayed after the change
- **THEN** it SHALL be refused

#### Scenario: The account-wide version moves too

- **WHEN** a password change succeeds
- **THEN** the account's session version SHALL be greater than it was before

#### Scenario: A failed write revokes nothing

- **WHEN** the transaction that carries the new hash fails
- **THEN** the stored hash SHALL be unchanged and no session SHALL have been revoked

### Requirement: The password-change handler SHALL be CSRF-protected and SHALL report through the session, not the URL

The handler SHALL require a valid CSRF token like every other panel POST, and SHALL report both success and refusal through the panel's session-carried flash mechanism followed by a redirect, never through a query-string parameter. A message a link can carry is a message an attacker chooses.

Refusal messages SHALL NOT distinguish which of the credential checks failed in a way that discloses anything about the stored password beyond the fact that the submitted current password did not match.

#### Scenario: A POST without a CSRF token is rejected

- **WHEN** the password-change handler is posted without a valid CSRF token
- **THEN** the request SHALL be rejected and nothing SHALL be written

#### Scenario: The outcome is not in the URL

- **WHEN** a password change succeeds or is refused
- **THEN** the resulting redirect target SHALL carry no message parameter
- **AND** the message SHALL be shown once and not survive a reload
