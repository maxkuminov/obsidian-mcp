## ADDED Requirements

### Requirement: A vault-root assignment SHALL be refused when it overlaps an active assignment
The control panel's user-edit handler SHALL refuse a `vault_path` assignment whose root overlaps the root of any *other* active user holding an assignment, and SHALL name the conflicting user in the refusal. The check SHALL be evaluated only when the edit's **resulting** state is both active and assigned; an edit whose result is an inactive account, or an account with no assignment, SHALL NOT be refused by it. Three independent conditions each constitute an overlap and all three SHALL be evaluated: **identity**, where an opened directory descriptor for each root reports the same `(st_dev, st_ino)`; **containment**, where the canonical real path of one root is an ancestor of the canonical real path of the other, tested in both directions and compared on whole path components rather than as a string prefix; and **mount grafting**, where the process's mount table reports a mount point strictly inside one root whose device and filesystem-relative root place the other tenant's directory inside it. A refusal SHALL leave `users.vault_path` unchanged.

Equality of the two normalised assignment strings — the whole of the check that exists today — is the degenerate case of the first two conditions, and its existing wording SHALL be preserved for that case. The check SHALL therefore be given the canonical assignment strings alongside the descriptors, so an equal pair can still be described as a duplicate rather than as a containment. It is kept as a message, not as a second implementation: two functions answering "do these roots collide" is how the two answers drift apart.

The three conditions are complementary and none implies another. Identity proves the two assignments name one directory object, which catches a symlink alias or a same-filesystem bind mount of one directory under two pathnames; it proves nothing about nesting, because two distinct inodes nest freely. Containment proves one root's canonical pathname lies inside the other's, which catches `/vaults/team` against `/vaults/team/private` and an ancestor reached through a symlinked component; it proves nothing about aliasing. Mount grafting proves the kernel has attached one tenant's directory inside the other's tree at a path neither canonical name expresses — `mount --bind /vaults/b /vaults/a/inner`, which the first two conditions both miss because the root inodes stay distinct and both real paths stay outside each other.

Device numbers SHALL NOT be used to infer a mount relation in either direction: equal `st_dev` does not prove one mount, and unequal `st_dev` does not prove unrelated directories — a filesystem mounted inside another tenant's root gives different devices and total overlap.

Component-wise comparison is load-bearing. A raw string prefix test reports `/vaults/team` as an ancestor of `/vaults/team-2`, which refuses an assignment that overlaps nothing.

Gating on the resulting state, not the current one, is what keeps the guard escapable. An inactive or unassigned account is outside the set the check compares against and can create no overlap — the peer query and the detection both scope to active users holding an assignment — so refusing such an edit protects nothing. It also removes the operator's remedy: deactivating or unassigning the account is exactly how a quarantined overlap is resolved from the panel, and a check that refuses that edit because the account still overlaps leaves the condition with no exit through the interface that reports it. Reactivating or reassigning is an edit whose result is active and assigned, and runs the full check.

The mount-grafting condition SHALL be best-effort in the refusing direction only: where the mount table cannot be read, it SHALL be skipped and SHALL NOT by itself refuse an assignment, and the other two conditions SHALL still decide. Its availability SHALL be determined independently of whether this kernel can report a descriptor's mount identity — those are different capabilities on different kernel versions, and conflating them would disable this condition where it works.

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

#### Scenario: Another user's vault grafted inside the candidate is refused

- **WHEN** an administrator assigns a root inside which the mount table reports another active user's vault directory bind-mounted, while the two root inodes differ and neither canonical real path is inside the other
- **THEN** the assignment SHALL be refused
- **AND** the refusal SHALL name the other user and state that their vault is mounted inside the candidate root

#### Scenario: An unreadable mount table does not refuse on its own

- **WHEN** the process cannot read its mount table and a candidate root is neither identical to nor nested with any other active user's root
- **THEN** the mount-grafting condition SHALL be skipped
- **AND** the assignment SHALL be accepted on the strength of the other two conditions

#### Scenario: Deactivating an account whose root overlaps another is not refused

- **WHEN** an administrator submits an edit for an account whose assigned root overlaps another active user's, and the edit's resulting state is inactive
- **THEN** the overlap check SHALL NOT refuse it
- **AND** the account SHALL be deactivated

#### Scenario: Clearing an overlapping assignment is not refused

- **WHEN** an administrator submits an edit that clears the `vault_path` of an account whose root overlaps another active user's
- **THEN** the overlap check SHALL NOT refuse it
- **AND** the assignment SHALL be cleared

#### Scenario: Reactivating an account whose root still overlaps is refused

- **WHEN** an administrator submits an edit whose resulting state is active and assigned to a root that overlaps another active user's
- **THEN** the overlap check SHALL refuse it, naming the conflicting user
- **AND** the account SHALL remain as it was

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
