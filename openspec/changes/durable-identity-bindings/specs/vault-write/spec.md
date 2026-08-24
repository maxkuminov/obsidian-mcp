## ADDED Requirements

### Requirement: A mutation confirms the caller's vault assignment immediately before each publishing operation
Every mutating note and file tool SHALL re-read the caller's vault assignment from the database immediately before **each** publishing operation it performs, and SHALL refuse when that assignment no longer equals the root the request bound at admission. The applicable tools are `create_note`, `edit_note`, `move_note`, `delete_note`, `set_frontmatter` and `write_file`. On refusal nothing further SHALL be written, published, renamed or unlinked, and no target directory SHALL be created.

The re-read SHALL be a fresh database read, not a lookup in the process vault cache or in the request's own bound snapshot. Both of those are the values being checked: the snapshot is bound once, at admission, and is deliberately immutable so the admission gate fails closed under a concurrent bulk cache warm, and the process cache is add-only from the indexer's side. Consulting either would compare a value with itself.

A confirmation SHALL cover exactly the publishing operation it precedes, and SHALL NOT be carried across an intervening `await`, database transaction, or subsequent publishing operation. Five of the six tools publish exactly once, so for them this is one confirmation per call. `move_note(rewrite_links=True)` publishes once for the move and once per planned link rewrite, with a metadata transaction of unbounded duration between the move and the first rewrite; a single confirmation reused across all of that would reintroduce, inside one call, the staleness this requirement exists to narrow. Each of those publications SHALL therefore carry its own confirmation.

The metadata transaction that follows the move SHALL NOT require a confirmation: it writes no vault bytes and it records a publication that has already occurred, so refusing it would leave the database describing a note that is no longer at that path.

The confirmation SHALL be enforced structurally rather than by convention: the shared publish helpers SHALL refuse a mutation target that carries no confirmation for the operation about to be performed, so a mutating tool added later cannot publish without one, in the same way a tool added later cannot skip the admission gate. A refusal for a missing confirmation SHALL be distinguishable from a refusal for a changed assignment, because the first is a programming error and the second is an operational event.

**Every destructive operation on a mutation target SHALL go through such a helper, the permanent unlink included.** A tool that reaches a bare unlink syscall on a target's parent descriptor is outside the enforcement, and while any such call site remains the structural claim is false rather than merely incomplete. `delete_note(permanent=True)` is that call site today and SHALL be routed through a permanent-unlink helper on the same seam as the atomic write, the no-clobber move and the soft delete.

`user_id is None` outside multi-user mode has no user row to re-read; those calls SHALL be unaffected and SHALL issue no such query.

#### Scenario: Reassignment between admission and publication

- **WHEN** a request is admitted with one vault root bound, an administrator commits a reassignment to a different root, and the request's `create_note`, `edit_note`, `set_frontmatter` or `write_file` then reaches its publish
- **THEN** the call SHALL be refused with a tool error naming that the vault assignment changed while the call was in flight
- **AND** no file SHALL be created or modified in the former root
- **AND** no file SHALL be created or modified in the newly assigned root

#### Scenario: Unassignment between admission and publication

- **WHEN** the caller's vault assignment is cleared in that window
- **THEN** the call SHALL be refused and nothing SHALL be written

#### Scenario: The caller is deactivated between admission and publication

- **WHEN** the caller's account is deactivated, or its user row removed, in that window
- **THEN** the call SHALL be refused and nothing SHALL be written

#### Scenario: The confirmation is a fresh read, not a cache hit

- **WHEN** the database's assignment has changed but the process vault cache and the request's bound snapshot both still hold the previous root
- **THEN** the call SHALL still be refused

#### Scenario: An unchanged assignment publishes as before

- **WHEN** the assignment is unchanged between admission and publication
- **THEN** the mutation SHALL complete exactly as it does today
- **AND** a tool that publishes once SHALL have issued exactly one assignment re-read
- **AND** a tool that publishes more than once SHALL have issued exactly one re-read per publishing operation

#### Scenario: A move that rewrites links confirms before it commits

- **WHEN** `move_note(rewrite_links=True)` runs and the assignment has already changed when it reaches the operation that commits the move
- **THEN** the note SHALL NOT be moved, no source SHALL be rewritten, and `notes_metadata` and `note_links` SHALL be unchanged

#### Scenario: A move that rewrites links confirms again before every rewrite

- **WHEN** `move_note(rewrite_links=True)` commits the move under a valid confirmation and the assignment changes during the metadata transaction that follows
- **THEN** the first link rewrite SHALL be refused by its own confirmation
- **AND** no further source SHALL be rewritten, because every remaining rewrite would write into a vault the caller no longer holds

#### Scenario: A refusal part way through a move reports the partial outcome

- **WHEN** a link rewrite is refused after the move has already committed
- **THEN** the tool SHALL report that the move completed in the previous root, that the vault assignment changed while the call was in flight, and which sources were left unrewritten
- **AND** SHALL NOT report the move as a clean success
- **AND** the move itself SHALL NOT be rolled back, and the metadata rows recording it SHALL remain consistent with where the note now is

#### Scenario: A permanent delete is refused by a helper, not by a convention

- **WHEN** `delete_note(permanent=True)` reaches its unlink and the assignment has changed
- **THEN** the unlink SHALL be refused and the note SHALL remain at its path
- **AND** the refusal SHALL come from the same shared enforcement that covers the atomic write, the no-clobber move and the soft delete, rather than from a check written into the tool

#### Scenario: A soft delete confirms before it moves the note to trash

- **WHEN** `delete_note` runs and the assignment has changed
- **THEN** the note SHALL remain at its path and no `.trash` entry SHALL be created for it

#### Scenario: Every mutating tool inherits the confirmation

- **WHEN** any mutation target reaches a shared publish helper — including the permanent-unlink helper — without a confirmation recorded for the operation about to be performed
- **THEN** the helper SHALL raise rather than publish
- **AND** the resulting error SHALL be distinguishable from the refusal a changed assignment produces

#### Scenario: The refusal is auditable

- **WHEN** a mutation is refused because the assignment changed
- **THEN** the call SHALL be recorded in `usage_logs` with the same allow-listed parameters as a successful call, carrying an error marker that names a changed vault assignment, and no additional field
- **AND** that marker SHALL be distinct from the marker a missing vault assignment produces at admission

#### Scenario: Single-user mode is unaffected

- **WHEN** a mutating tool runs in single-user mode
- **THEN** it SHALL issue no assignment re-read and SHALL behave exactly as before

#### Scenario: The read path gains no query

- **WHEN** a tool that does not mutate the vault is called — a search, a read, a listing or a graph tool
- **THEN** it SHALL issue no assignment re-read, and the admission gate SHALL remain a pure cache lookup performing no database work

### Requirement: The pre-publish confirmation is optimistic, and its residual is declared
The confirmation SHALL be documented as narrowing the window rather than closing it. A reassignment that commits after the confirming read and before the publishing operation completes — including one that commits while that operation is running — SHALL still take effect in the former root, and the tool MAY report success. The system SHALL NOT claim that a vault reassignment is linearizable with an in-flight mutation.

What changes is the size of the window: from the whole tool body — a read, a diff, a section resolve, a payload of up to the note size cap — down to staging, the durability flush and one publishing call. This is the same guarantee level the system declares for `edit_note(expected=…)` and for the transfer fingerprint check, and it is stated rather than implied.

That bound holds for **every** publishing operation, including each of `move_note`'s link rewrites, because each carries its own confirmation. The consequence is that a multi-publication tool has several such windows rather than one, and can be refused part way through; the partial outcome is specified and reported rather than swallowed. A claim that the residual is bounded by "one publishing call per tool call" would be false for `move_note` and SHALL NOT be made.

Closing the window would mean holding the credential and user rows locked across arbitrary vault I/O, which is the shape the transfer routes use and which those routes can afford because their publish is a bounded byte stream against an already-open session. Adopting it for the note tools would put a note read, a link-rewrite plan and an unbounded number of file writes inside a lock every authenticated request contends for, and it is rejected for that reason rather than overlooked.

#### Scenario: A reassignment inside the publish window is not prevented

- **WHEN** the confirming read succeeds and the reassignment commits before the publishing operation completes
- **THEN** the mutation MAY take effect in the former root and the tool MAY report success
- **AND** this SHALL be recorded as a declared residual rather than specified as prevented

#### Scenario: The narrowed window is what is claimed

- **WHEN** the guarantee is stated to an operator or in the design record
- **THEN** it SHALL be stated as a re-read immediately before publication, bounded by the publish operation, and SHALL NOT be stated as a lock held across the publish

#### Scenario: No row locks are held across vault I/O

- **WHEN** a mutating note tool performs the confirmation
- **THEN** it SHALL NOT hold a `SELECT … FOR UPDATE` on the credential or user row across the note read, the link-rewrite plan, the staging write or the publish
