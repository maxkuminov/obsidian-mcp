## ADDED Requirements

### Requirement: An administrator SHALL manage a user's OIDC link from the user edit page
While OIDC is enabled, the user edit page SHALL show the user's link status (not linked, pending with its email and expiry, expired, linked with its date, or linked under a previous issuer) and SHALL offer three CSRF-protected POST actions under the admin guard: set an expected email (`/admin/users/{id}/oidc/expect`), bind a subject directly under the configured issuer (`/admin/users/{id}/oidc/bind`), and unlink (`/admin/users/{id}/oidc/unlink`). Expect and bind SHALL be refused on a bound row, so a bound user is never re-pointed without an explicit unlink. A value already used by another user SHALL be refused with a flash and nothing written.

#### Scenario: Set an expectation
- **WHEN** an admin submits `Max@Example.com` for an unlinked user
- **THEN** the row SHALL hold `max@example.com`, the configured issuer and the current time as its expectation, and the page SHALL show it pending with its expiry

#### Scenario: Bind a subject directly
- **WHEN** an admin submits a subject for an unlinked or pending user
- **THEN** the row SHALL be bound to `(configured issuer, subject)` with its expectation cleared

#### Scenario: Re-pointing a bound user is refused
- **WHEN** an admin submits an expectation or a subject for a bound user
- **THEN** the request SHALL be refused with a flash and the binding SHALL be unchanged

#### Scenario: Duplicate email or subject
- **WHEN** the submitted email is pending for, or the subject is bound to, another user
- **THEN** the request SHALL be refused with "already used by another account" and nothing written

#### Scenario: Controls follow the CSP rules
- **WHEN** the card is rendered
- **THEN** it SHALL contain no inline event handler, no `style=` attribute and no `javascript:` URL, and the unlink control SHALL be a `type="button"` with a `data-confirm` attribute handled by `panel.js`

### Requirement: Unlinking an OIDC identity SHALL end the user's live sessions
The unlink action SHALL clear all six OIDC columns and revoke the user's live browser sessions in the same transaction, under the admin guard, without committing between the lock and the writes. An administrator SHALL NOT unlink their own account while `LOCAL_LOGIN_ENABLED` is false.

#### Scenario: Unlink revokes
- **WHEN** an admin unlinks another user who has two live sessions
- **THEN** the columns SHALL be NULL and both session rows SHALL carry a revocation time

#### Scenario: Self-unlink with password login disabled
- **WHEN** an admin unlinks their own account while password login is disabled
- **THEN** the request SHALL be refused with a flash and nothing written

### Requirement: The users list and account page SHALL show OIDC link status read-only
While OIDC is enabled, the users list SHALL show each user's link state (`linked`, `pending` or none) and the account page SHALL show whether the signed-in user is linked, without any self-service link control.

#### Scenario: Account page
- **WHEN** a linked non-admin user opens `/admin/account` with OIDC enabled
- **THEN** the page SHALL state the account is linked to single sign-on and SHALL offer no link or unlink control
