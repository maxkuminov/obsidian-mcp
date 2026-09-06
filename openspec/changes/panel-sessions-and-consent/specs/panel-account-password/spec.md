## ADDED Requirements

### Requirement: A signed-in user SHALL be able to change their own password

The control panel SHALL expose an account page and a password-change handler available to every signed-in user, administrator or not, requiring the current password, a new password and a confirmation of the new password. The handler SHALL verify the current password and hash the new one using the server's existing password functions, whose documented semantics — truncation of the UTF-8 encoding at 72 bytes and rejection of embedded NUL bytes — SHALL be preserved exactly, so that no stored hash becomes unverifiable.

The administrator-driven password reset SHALL remain available as the recovery path for a user who cannot sign in.

In single-user mode there is no account row and no local password, so **both** the account page and the password-change handler SHALL answer as if the route did not exist. A page whose only content is a form that cannot exist there is not a page, and the sidebar entry is already gated on multi-user mode; one rule for both methods is also one thing to verify.

#### Scenario: A user changes their own password

- **WHEN** a signed-in non-administrator submits their correct current password with a valid new password and a matching confirmation
- **THEN** the password SHALL be changed
- **AND** the new password SHALL authenticate at the login form and the old one SHALL NOT

#### Scenario: The route is not administrator-only

- **WHEN** a signed-in user without the administrator role opens the account page
- **THEN** the page SHALL render with the password-change form

#### Scenario: Single-user mode has no account page and no account to change

- **WHEN** either the account page or the password-change handler is reached with multi-user mode disabled
- **THEN** it SHALL answer not-found and change nothing

#### Scenario: The existing hashing semantics are unchanged

- **WHEN** a password longer than 72 UTF-8 bytes is set through this handler
- **THEN** hashing SHALL succeed under the same truncation the server has always applied
- **AND** the resulting hash SHALL verify with that password

### Requirement: The password change SHALL verify and write against a locked, freshly read account row

The handler SHALL take the same account-guard advisory lock the administrative user-management handlers take — the same constant, because two different keys do not exclude each other — and SHALL then re-read the acting user's row `FOR UPDATE` before it verifies the submitted current password and before it writes anything. The re-read SHALL force the loaded object's attributes to be replaced by the values read under the lock; an object already in the request's identity map otherwise returns its pre-lock attribute values, and the re-read would prove nothing.

Verification SHALL use the hash read under the lock, never the hash loaded by the request's authentication dependency. Without this, an administrator's password reset or deactivation that commits between the dependency's read and this handler's write is silently overwritten: the user's change would be verified against a hash the administrator has already replaced, and would restore access the administrator had just removed.

If the row is gone, or its active flag is not exactly true when read under the lock, the handler SHALL refuse, write nothing, revoke nothing, and end the acting session. Nothing SHALL commit between the lock being taken and the protected write, so the lock's critical section stays atomic.

#### Scenario: A concurrent administrative reset is not overwritten

- **WHEN** an administrator's password reset commits after the change handler's request began but before it takes the lock
- **THEN** the change SHALL be refused because the submitted current password no longer verifies against the stored hash
- **AND** the administrator's newly set hash SHALL remain in place
- **AND** no session SHALL be minted for the acting browser

#### Scenario: A concurrent deactivation is not overwritten

- **WHEN** an administrator deactivates the account after the change handler's request began but before it takes the lock
- **THEN** the change SHALL be refused
- **AND** the stored hash SHALL be unchanged and the account SHALL remain inactive
- **AND** no session SHALL be minted for the acting browser

#### Scenario: The re-read is authoritative

- **WHEN** the acting user's row is modified and committed by another transaction before the lock is taken
- **THEN** the verification SHALL use the values read under the lock, not those loaded earlier in the request

#### Scenario: A deleted account cannot change its password

- **WHEN** the acting user's row no longer exists when read under the lock
- **THEN** the change SHALL be refused and nothing SHALL be written

### Requirement: A password change SHALL be refused without the correct current password, and refusals SHALL be throttled per account and per address

The handler SHALL refuse the change when the submitted current password does not verify against the hash read under the lock, when the new password and its confirmation differ, when the new password is shorter than the server's minimum length, when it contains a NUL byte, or when it is identical to the current password. A refusal SHALL leave the stored hash, the account-wide session version and every session row untouched.

The handler SHALL carry **two independent** rate limits at the login handler's rate: one keyed on the authenticated session's account and one keyed on the client address. Neither subsumes the other — an account-only key lets one address walk many accounts, and a key that mixes the address in gives an attacker a fresh allowance for every address they rotate through, which is why a single composite key does not bound guessing against one account. Successful and refused attempts SHALL count against the same limits, so the allowance cannot be drained by guessing.

The minimum password length SHALL be defined once and applied by **every** path that sets a password — this handler, bootstrap registration, the administrator reset, and administrative user creation — so that an administrator cannot set a password its owner is then forbidden from setting again. There SHALL be no composition rules. Existing stored passwords SHALL NOT be re-checked against the minimum at login, so raising it does not lock anyone out.

#### Scenario: Wrong current password is refused and changes nothing

- **WHEN** a signed-in user submits an incorrect current password with an otherwise valid new password
- **THEN** the change SHALL be refused
- **AND** the stored hash, the session version and every session row SHALL be unchanged

#### Scenario: Guessing against one account is bounded across addresses

- **WHEN** more attempts than the limit allows are made against one account within the window from a series of different client addresses
- **THEN** the excess requests SHALL be rejected by the account-keyed limit

#### Scenario: Guessing from one address is bounded across accounts

- **WHEN** more attempts than the limit allows are made from one client address within the window against a series of different accounts
- **THEN** the excess requests SHALL be rejected by the address-keyed limit

#### Scenario: Successes count against the limit

- **WHEN** attempts within the window include successful changes
- **THEN** those successes SHALL count against the same limits as the refusals

#### Scenario: Confirmation mismatch is refused

- **WHEN** the new password and its confirmation differ
- **THEN** the change SHALL be refused and nothing SHALL be written

#### Scenario: A too-short password is refused

- **WHEN** a new password shorter than the configured minimum is submitted
- **THEN** the change SHALL be refused and nothing SHALL be written

#### Scenario: A NUL byte is a form error, not a server error

- **WHEN** a new password containing a NUL byte is submitted here, at bootstrap registration, at the administrator reset, or at administrative user creation
- **THEN** the request SHALL be refused with a form-level message
- **AND** the server SHALL NOT answer with an unhandled error

#### Scenario: Reusing the current password is refused

- **WHEN** the new password verifies against the stored hash
- **THEN** the change SHALL be refused and nothing SHALL be written

#### Scenario: The minimum is shared by every setter

- **WHEN** bootstrap registration, the administrator reset, or administrative user creation is given a password shorter than the configured minimum
- **THEN** it SHALL be refused by the same rule as the self-service handler

### Requirement: A successful password change SHALL end the user's other sessions and keep the current one

On success the handler SHALL, in the transaction that holds the account guard, write the new hash, increment the account-wide session version and revoke every session row belonging to that user — the one that made the request included — and SHALL then, in a second transaction, mint a fresh session for the requesting browser. The user-visible effect SHALL be that every other device is signed out and the browser that performed the change remains signed in under a new session identifier.

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

#### Scenario: A failed re-issue does not undo the change

- **WHEN** the mint that follows a committed change fails
- **THEN** the new password SHALL still be in force
- **AND** the acting browser SHALL be signed out rather than left holding a cookie with no row

### Requirement: The password-change handler SHALL be CSRF-protected and SHALL report through the session, not the URL

The handler SHALL require a valid CSRF token like every other panel POST, and SHALL report both success and refusal through the panel's session-carried flash mechanism followed by a redirect, never through a query-string parameter. A message a link can carry is a message an attacker chooses.

Refusal messages SHALL NOT distinguish which of the credential checks failed in a way that discloses anything about the stored password beyond the fact that the submitted current password did not match.

A request rejected by either rate limit is **exempt** from this rule: it never reaches the handler, and the application-wide rate-limit error handler's JSON response SHALL stand unchanged. Producing a flash for it would require either a second counter inside the handler — which would diverge from the limiter that already decided — or replacing a process-wide error handler for one route. A rate-limit rejection also carries no message any caller chose.

#### Scenario: A rate-limited attempt answers with the limiter's own response

- **WHEN** a sixth attempt within the window is rejected by either limit
- **THEN** the response SHALL be the application's rate-limit response rather than a flash and a redirect
- **AND** nothing SHALL be written

#### Scenario: A POST without a CSRF token is rejected

- **WHEN** the password-change handler is posted without a valid CSRF token
- **THEN** the request SHALL be rejected and nothing SHALL be written

#### Scenario: The outcome is not in the URL

- **WHEN** a password change succeeds or is refused
- **THEN** the resulting redirect target SHALL carry no message parameter
- **AND** the message SHALL be shown once and not survive a reload
