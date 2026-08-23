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
- **THEN** this requirement SHALL NOT refuse it
- **AND** the delete SHALL proceed as before, subject only to the last-active-administrator guard below

#### Scenario: Single-user mode has no account to refuse

- **WHEN** the panel runs in single-user mode, where the acting administrator is a sentinel with no `users` row
- **THEN** the handler SHALL behave as it did before this requirement, because no target can be the actor

#### Scenario: The self-view offers no enabled delete control

- **WHEN** an administrator opens the edit page for their own account
- **THEN** both delete controls SHALL be disabled and the page SHALL state that the account cannot delete itself

### Requirement: A delete MUST NOT take the last active administrator away
The user-delete handler SHALL refuse a delete, soft or permanent, exactly when the target is itself an active administrator and no *other* active administrator exists — equivalently, when the operation would take the count of active administrators from one to zero — and SHALL NOT refuse it in any other case.

The predicate has two conjuncts and both are load-bearing. The target must currently be an active administrator, and the count taken must be of active administrators *other than* the target: zero refuses, one or more proceeds. A target who is not an active administrator — an ordinary account, or an inactive or demoted one — does not change how many active administrators the table holds, so such a delete proceeds even when that number is already zero. Restating the guard as "the state it would leave behind contains no active administrator" is broader than that and refuses in exactly that case, which is a false positive on ordinary account cleanup in a deployment that has no active administrator row at all — the shape single-user mode presents. Refusing more broadly is also what would forbid the removal the self-delete refusal above directs the operator to perform. This requirement pins the existing guard rather than changing it; the implementation is expected to leave it untouched.

An acting administrator that is a `users` row can no longer reach this guard. A self-target is refused by the requirement above, and for any other target the actor is itself an active administrator — re-read as such inside the same lock — so the count is at least one. The reachable case is the single-user sentinel, which is not a `users` row and is therefore never counted. Nothing has to be restricted for that path to exist: the users router is mounted unconditionally, and the sidebar link is gated on `is_admin` alone, which `_panel_context` sets true for the sentinel — so the page and both delete forms are reachable in single-user mode by following the visible link. A sentinel actor deleting the only active administrator in the table would otherwise leave a database no multi-user deployment could be switched back on with.

#### Scenario: Deleting a non-administrator is not refused for want of an administrator

- **WHEN** the table holds no active administrator and the target is an active account that is not an administrator
- **THEN** this guard SHALL NOT refuse the delete
- **AND** the delete SHALL proceed, because the target is not an active administrator and the count of active administrators is unchanged by it

#### Scenario: One of two active administrators deletes the other

- **WHEN** two active administrators exist and one deletes the other, soft or permanent
- **THEN** the delete SHALL proceed, because the acting administrator remains an active administrator afterwards

#### Scenario: The sole active administrator is the target

- **WHEN** the acting administrator holds no `users` row — the single-user sentinel — and the target is the only active administrator in the table
- **THEN** the request SHALL be refused with the last-admin message
- **AND** the target's `is_active` SHALL remain true and the row SHALL still exist

#### Scenario: A self-target never reaches this guard

- **WHEN** an administrator targets their own account, whether or not they are the only active administrator
- **THEN** the self-delete refusal SHALL answer first
- **AND** the response SHALL carry the self-delete message rather than the last-admin message

### Requirement: The self-delete refusal SHALL run inside the existing admin critical section
The refusal SHALL be evaluated after the shared admin advisory lock is taken, after the acting administrator's own privileges have been re-read inside that lock, before the active-administrator count is taken and before any row is written, and MUST NOT introduce a second lock key. One key is what makes a concurrent edit and a concurrent delete exclude each other; two keys would not.

Placing it after the actor re-check keeps the diagnostics in the right order — an actor demoted while queued for the lock is told that, not told they cannot delete themselves — and placing it before the count is what makes the previous requirement's ordering scenario hold.

#### Scenario: Ordering against a concurrent demotion

- **WHEN** the acting administrator is demoted or deactivated by another administrator while their delete request waits for the lock
- **THEN** the response SHALL be the actor-revoked refusal, not the self-delete refusal
- **AND** no flag SHALL be written

#### Scenario: One lock key for both handlers

- **WHEN** the user-edit handler and the user-delete handler are inspected
- **THEN** both SHALL take the same advisory-lock key
- **AND** neither SHALL commit between taking it and writing the flags
