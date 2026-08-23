## ADDED Requirements

### Requirement: An administrator MUST NOT delete or deactivate their own account
The control panel's user-delete handler SHALL refuse a request whose target is the acting administrator's own account, for both the soft delete (which clears `is_active`) and the permanent delete, and SHALL refuse it unconditionally — the presence of other active administrators MUST NOT make it permissible. The refusal SHALL leave the target row and its `is_active` flag unchanged, and SHALL name that another administrator has to perform the removal.

This completes the promise the self-edit lock makes: an administrator cannot remove their own access through the panel by any route on the page. The permanent form is the more severe of the two, because the cascade on `users.id` also destroys the actor's API keys, OAuth clients, OAuth tokens and note index, leaving nothing the actor could sign back in with to undo it.

#### Scenario: Soft self-delete with other admins present

- **WHEN** an administrator submits the soft delete for their own account while other active administrators exist
- **THEN** the request SHALL be refused
- **AND** the account's `is_active` SHALL remain true

#### Scenario: Permanent self-delete with other admins present

- **WHEN** an administrator submits the permanent delete for their own account while other active administrators exist
- **THEN** the request SHALL be refused
- **AND** the account row SHALL still exist, together with its API keys, OAuth clients, OAuth tokens and note metadata

#### Scenario: Deleting another account is unaffected

- **WHEN** an administrator deletes a different account, soft or permanent
- **THEN** the delete SHALL proceed as before

#### Scenario: The last-admin guard still applies to other targets

- **WHEN** an administrator deletes the only remaining active administrator other than themselves
- **THEN** that request SHALL still be refused by the last-admin guard, with its own message

#### Scenario: Single-user mode has no account to refuse

- **WHEN** the panel runs in single-user mode, where the acting administrator is a sentinel with no `users` row
- **THEN** the handler SHALL behave as it did before this requirement, because no target can be the actor

#### Scenario: The self-view offers no enabled delete control

- **WHEN** an administrator opens the edit page for their own account
- **THEN** both delete controls SHALL be disabled and the page SHALL state that the account cannot delete itself

### Requirement: The self-delete refusal SHALL run inside the existing admin critical section
The refusal SHALL be evaluated after the shared admin advisory lock is taken and after the acting administrator's own privileges have been re-read inside that lock, and MUST NOT introduce a second lock key. One key is what makes a concurrent edit and a concurrent delete exclude each other; two keys would not.

#### Scenario: Ordering against a concurrent demotion

- **WHEN** the acting administrator is demoted or deactivated by another administrator while their delete request waits for the lock
- **THEN** the response SHALL be the actor-revoked refusal, not the self-delete refusal
- **AND** no flag SHALL be written

#### Scenario: One lock key for both handlers

- **WHEN** the user-edit handler and the user-delete handler are inspected
- **THEN** both SHALL take the same advisory-lock key
- **AND** neither SHALL commit between taking it and writing the flags
