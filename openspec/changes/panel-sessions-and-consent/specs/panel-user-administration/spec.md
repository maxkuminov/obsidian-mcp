## ADDED Requirements

### Requirement: Administrative actions that end an account's access SHALL end its live sessions

Every panel handler that ends or resets an account's ability to sign in SHALL revoke that account's live browser sessions in the same transaction as the write that makes the change true. This covers the administrator password reset, deactivation through the user edit form, and the soft delete that clears the active flag. A permanent delete SHALL remove the rows through the foreign key's cascade and SHALL require no handler code of its own.

The revocation SHALL be issued on the handler's own database session and SHALL NOT introduce a commit, because these handlers hold a transaction-scoped advisory lock and the check-then-act guard they implement depends on nothing committing between the lock and the protected write.

Incrementing the account-wide session version SHALL remain in place where it already is; it is a second, account-wide switch and not a substitute for revoking the rows.

#### Scenario: An administrator password reset ends the target's sessions

- **WHEN** an administrator resets another user's password
- **THEN** every live session row of that user SHALL carry a revocation time
- **AND** a request replaying one of that user's cookies SHALL be refused

#### Scenario: Deactivating a user ends their sessions

- **WHEN** an administrator clears a user's active flag through the edit form
- **THEN** every live session row of that user SHALL carry a revocation time

#### Scenario: A soft delete ends the sessions it deactivates

- **WHEN** an administrator soft-deletes a user
- **THEN** every live session row of that user SHALL carry a revocation time

#### Scenario: A permanent delete leaves no session rows behind

- **WHEN** an administrator permanently deletes a user
- **THEN** no session row SHALL remain for that user

#### Scenario: The actor's own sessions are untouched

- **WHEN** an administrator resets another user's password or deactivates them
- **THEN** the acting administrator's own session SHALL remain usable

#### Scenario: A refused administrative action revokes nothing

- **WHEN** an administrative action is refused by the last-administrator guard, the self-target guard, or the actor-still-privileged re-check
- **THEN** no session SHALL have been revoked
