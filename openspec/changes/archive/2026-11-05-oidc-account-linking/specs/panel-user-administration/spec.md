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

### Requirement: OIDC link handlers SHALL re-check the acting administrator inside the guard
Each of `/admin/users/{id}/oidc/expect`, `/oidc/bind` and `/oidc/unlink` SHALL take the admin guard and then re-run the actor-privilege re-check before reading the target row; when the actor is no longer an active administrator the handler SHALL roll back, log the established actor-revoked event and answer with the established actor-revoked refusal, writing nothing.

#### Scenario: Actor demoted while waiting for the guard
- **WHEN** admin A's unlink request is held at the guard while admin B's demotion of A commits, and A's request then proceeds
- **THEN** A's request SHALL be refused with the actor-revoked message and the target's link SHALL be unchanged

### Requirement: With password login disabled, no panel mutation SHALL leave zero reachable administrators
While `LOCAL_LOGIN_ENABLED` is false, the OIDC unlink, the user edit that clears `is_admin` or `is_active`, the soft delete and the permanent delete SHALL each, under the admin guard, after the actor re-check and before any write, refuse with nothing written a change whose target is currently a reachable administrator (an active administrator bound under the configured issuer) and whose resulting state has no other reachable administrator. A change whose target is not currently a reachable administrator cannot reduce that set and SHALL NOT be refused by this rule, even when no reachable administrator exists. No other panel handler SHALL be able to reduce that set. While password login is enabled this requirement SHALL NOT apply and the existing last-active-administrator rule SHALL be unchanged.

#### Scenario: Unlinking the only reachable admin
- **WHEN** password login is disabled and admin A unlinks admin B, the only other admin, while A is not bound
- **THEN** the unlink SHALL be refused and B's link SHALL be unchanged

#### Scenario: Demoting or deactivating the only reachable admin
- **WHEN** password login is disabled and the edit would demote or deactivate the only bound active admin
- **THEN** the edit SHALL be refused and the row SHALL be unchanged, even though an unbound active admin exists

#### Scenario: Deleting the only reachable admin
- **WHEN** password login is disabled and a soft or permanent delete targets the only bound active admin
- **THEN** the delete SHALL be refused and the row SHALL be unchanged

#### Scenario: A target that is not a reachable admin is never refused by this rule
- **WHEN** password login is disabled, no reachable admin exists, and an admin deactivates or deletes an ordinary user, or demotes an admin who is not bound under the configured issuer
- **THEN** the change SHALL proceed

#### Scenario: Concurrent mutations keep one reachable admin
- **WHEN** password login is disabled, admins A and B are both bound, and "A unlinks B" and "B deactivates A" are issued with the first deterministically holding the guard
- **THEN** exactly one SHALL succeed, the other SHALL be refused by the actor re-check or the reachable-admin rule, and one reachable admin SHALL remain

#### Scenario: Password login enabled
- **WHEN** password login is enabled and an admin unlinks the only bound admin
- **THEN** the unlink SHALL proceed

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
