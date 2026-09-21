# panel-user-administration Specification

## Purpose
TBD - created by archiving change truthful-surfaces. Update Purpose after archive.
## Requirements
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

### Requirement: A vault-root assignment SHALL be refused when it overlaps an active assignment
The control panel's user-edit handler SHALL refuse a `vault_path` assignment whose root overlaps the root of any *other* active user holding an assignment, and SHALL name the conflicting user in the refusal. The check SHALL be evaluated only when the edit's **resulting** state is both active and assigned; an edit whose result is an inactive account, or an account with no assignment, SHALL NOT be refused by it. Two independent conditions each constitute an overlap and both SHALL be evaluated: **identity**, where an opened directory descriptor for each root reports the same `(st_dev, st_ino)`; and **containment**, where the canonical real path of one root is an ancestor of the canonical real path of the other, tested in both directions and compared on whole path components rather than as a string prefix. A refusal SHALL leave `users.vault_path` unchanged.

Equality of the two normalised assignment strings — the whole of the check that exists today — is the degenerate case of both conditions, and its existing wording SHALL be preserved for that case. The check SHALL therefore be given the canonical assignment strings alongside the descriptors, so an equal pair can still be described as a duplicate rather than as a containment. It is kept as a message, not as a second implementation: two functions answering "do these roots collide" is how the two answers drift apart.

The two conditions are complementary and neither implies the other. Identity proves the two assignments name one directory object, which catches a symlink alias or a same-filesystem bind mount of one directory under two pathnames; it proves nothing about nesting, because two distinct inodes nest freely. Containment proves one root's canonical pathname lies inside the other's, which catches `/vaults/team` against `/vaults/team/private` and an ancestor reached through a symlinked component; it proves nothing about aliasing.

Their scope is the two roots themselves. An overlap that **neither canonical name expresses** — a bind mount grafting one tenant's vault, or a mount nested inside it, to a path inside the other's tree — is out of scope for this requirement and is a recorded residual with its destructive consequence stated in the change's design; it is NOT to be approximated by a mount-table comparison, which three review rounds showed to be a heuristic that needs widening for each new mount configuration.

Device numbers SHALL NOT be used to infer a mount relation in either direction: equal `st_dev` does not prove one mount, and unequal `st_dev` does not prove unrelated directories — a filesystem mounted inside another tenant's root gives different devices and total overlap.

Component-wise comparison is load-bearing. A raw string prefix test reports `/vaults/team` as an ancestor of `/vaults/team-2`, which refuses an assignment that overlaps nothing.

Gating on the resulting state, not the current one, is what keeps the guard escapable. An inactive or unassigned account is outside the set the check compares against and can create no overlap — the peer query and the detection both scope to active users holding an assignment — so refusing such an edit protects nothing. It also removes the operator's remedy: deactivating or unassigning the account is exactly how a quarantined overlap is resolved from the panel, and a check that refuses that edit because the account still overlaps leaves the condition with no exit through the interface that reports it. Reactivating or reassigning is an edit whose result is active and assigned, and runs the full check.

#### Scenario: A descendant of another user's root is refused

- **WHEN** an administrator assigns `/vaults/team/private` to one user while another active user holds `/vaults/team`
- **THEN** the assignment SHALL be refused
- **AND** the refusal SHALL name the user holding `/vaults/team`
- **AND** the target's `vault_path` SHALL be unchanged

#### Scenario: An ancestor of another user's root is refused

- **WHEN** an administrator assigns `/vaults/team` to one user while another active user holds `/vaults/team/private`
- **THEN** the assignment SHALL be refused by the same condition, tested in the other direction
- **AND** the refusal SHALL name the user holding `/vaults/team/private`

#### Scenario: A symlink alias of another user's root is refused

- **WHEN** an administrator assigns a path that is a symbolic link to — or a bind mount of — the directory another active user is assigned, so that the two path strings differ and an opened descriptor for each reports the same `(st_dev, st_ino)`
- **THEN** the assignment SHALL be refused
- **AND** the refusal SHALL name the other user

#### Scenario: Two sibling directories are accepted

- **WHEN** an administrator assigns `/vaults/bob` to one user while another active user holds `/vaults/alice`, and the two are distinct directories neither of which contains the other
- **THEN** the assignment SHALL be accepted
- **AND** `users.vault_path` SHALL be written

#### Scenario: A sibling sharing a string prefix is accepted

- **WHEN** an administrator assigns `/vaults/team-2` to one user while another active user holds `/vaults/team`
- **THEN** the assignment SHALL be accepted, because `team-2` is not a path component of `/vaults/team`

#### Scenario: An identical path is still refused with the existing wording

- **WHEN** an administrator assigns a path exactly equal to another active user's assignment
- **THEN** the assignment SHALL be refused
- **AND** the message SHALL state that the path is already assigned to that user, as it does today, rather than describing the pair as a containment

#### Scenario: An inactive or unassigned user is not a conflict

- **WHEN** an administrator assigns a root that is identical to, contains, or is contained by the `vault_path` of a user who is inactive, or of a user whose `vault_path` is NULL
- **THEN** the assignment SHALL be accepted, because only active users holding an assignment can read or write a vault

#### Scenario: The target's own current assignment is not a conflict with itself

- **WHEN** an administrator re-saves a user's edit form without changing that user's `vault_path`
- **THEN** the check SHALL exclude the target's own row and the save SHALL proceed

#### Scenario: A peer root that cannot be opened refuses the assignment

- **WHEN** the check cannot open a directory descriptor for another active user's assigned root — the directory is missing, or unreadable
- **THEN** the assignment SHALL be refused, naming the root that could not be examined
- **AND** the refusal SHALL state that the overlap could not be ruled out, rather than reporting an overlap that was not observed or naming a peer relation that was not established

#### Scenario: Single-user mode is unaffected

- **WHEN** the panel runs in single-user mode, where the vault root comes from settings and no `users` row carries an assignment
- **THEN** this check SHALL have nothing to compare and SHALL refuse nothing

### Requirement: The overlap check SHALL run inside the existing admin critical section
The overlap check SHALL be evaluated after the shared admin advisory lock is taken and before the transaction that writes `users.vault_path` commits, and it MUST NOT introduce a second advisory-lock key.

Outside the lock the check is check-then-act. Two administrators assigning `/vaults/team` and `/vaults/team/private` to two different users at the same moment each read the other's *previous* row, each observe no conflict, and both writes land — producing exactly the overlap the check exists to prevent. One key is what makes the user-edit handler exclude a concurrent user-edit and a concurrent delete; a second key would serialize nothing against the first.

#### Scenario: Two concurrent overlapping assignments

- **WHEN** two administrators concurrently assign two roots that overlap each other to two different users
- **THEN** at most one of the two assignments SHALL be written
- **AND** the other SHALL be refused by this check, naming the user the first assignment landed on

#### Scenario: One lock key

- **WHEN** the user-edit handler's overlap check is inspected
- **THEN** it SHALL take no advisory lock of its own and SHALL run under the key the handler already takes

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

