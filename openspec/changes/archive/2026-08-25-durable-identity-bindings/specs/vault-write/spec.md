## ADDED Requirements

### Requirement: A mutation confirms the caller's vault assignment immediately before each publishing operation
Every mutating note and file tool SHALL re-read the caller's vault assignment from the database immediately before **each** publishing operation it performs, and SHALL refuse when that assignment no longer equals the root the request bound at admission. The applicable tools are `create_note`, `edit_note`, `move_note`, `delete_note`, `set_frontmatter` and `write_file`. On refusal nothing further SHALL be written, published, renamed or unlinked, and no target directory SHALL be created.

The re-read SHALL be a fresh database read, not a lookup in the process vault cache or in the request's own bound snapshot. Both of those are the values being checked: the snapshot is bound once, at admission, and is deliberately immutable so the admission gate fails closed under a concurrent bulk cache warm, and the process cache is add-only from the indexer's side. Consulting either would compare a value with itself.

A confirmation SHALL cover exactly the publishing operation it precedes, and SHALL NOT be carried across an intervening `await`, database transaction, or subsequent publishing operation. Five of the six tools publish exactly once, so for them this is one confirmation per call. `move_note(rewrite_links=True)` publishes once for the move and once per planned link rewrite, with a metadata transaction of unbounded duration between the move and the first rewrite; a single confirmation reused across all of that would reintroduce, inside one call, the staleness this requirement exists to narrow. Each of those publications SHALL therefore carry its own confirmation.

The metadata transaction that follows the move SHALL NOT require a confirmation: it writes no vault bytes and it records a publication that has already occurred, so refusing it would leave the database describing a note that is no longer at that path.

The confirmation SHALL be enforced structurally rather than by convention, and the enforcement has three parts.

**One awaiting wrapper, and no retainable confirmation.** There SHALL be no public way to obtain a confirmation and hold it: the only entry point SHALL be an asynchronous confirmed-publication wrapper that awaits the assignment read and then invokes a **synchronous** publish callable before returning control to the event loop.

**The confirmation SHALL be leased to that callable's dynamic extent.** The wrapper SHALL activate it before the call and invalidate it in a `finally` on every exit path — normal return, exception, or a callable that retained the object — and `consume` SHALL refuse a confirmation that is not currently leased. Single consumption alone is insufficient and SHALL NOT be relied on: it bounds how many times a confirmation may be used and says nothing about *when*, so a callable that stores its confirmation and publishes with it after the wrapper has returned, and after a reassignment has committed, is otherwise obeyed.

**A successful publication SHALL have consumed exactly one confirmation.** A callable that returns normally without consuming the confirmation it was given SHALL be refused, because that is the shape of a publish path added outside the shared helpers.

Publish callables that would not have published by the time they return SHALL be refused rather than driven: coroutine functions, generator functions and async-generator functions, and likewise a *returned* coroutine, generator or async generator — a callable object whose `__call__` is a generator is none of the first three. The wrapper SHALL NOT invoke `close` or any other method on such a returned object: that is arbitrary code of a stranger's choosing, and the lease has already been revoked, so driving the object later cannot publish.

**The confirmation is intrinsically single-consumption and target-bound.** The consumed flag SHALL live on the confirmation itself rather than on the target it authorises, so one confirmation cannot be spent by two publications however it is attached, and the publish helper SHALL check the confirmation's user id and canonical assignment against the target's own before spending it. A confirmation taken for one user, or for one root, SHALL NOT authorise a publication into another's target.

**Every publish helper refuses an unauthorised publication.** The shared helpers SHALL refuse a mutation target for which no confirmation is presented, one already spent, or one taken for a different user or root, so a mutating tool added later cannot publish without confirming the assignment first, in the same way a tool added later cannot skip the admission gate. A refusal for a missing or unusable confirmation SHALL be distinguishable from a refusal for a changed assignment, because the first is a programming error and the second is an operational event.

**The rollback of a publication is covered by that publication's confirmation, through a narrowly scoped permit.** `move_note`'s inode verification may have to move the file straight back when what arrived at the destination is the source inode but is a directory or a symbolic link, and refusing that for want of a confirmation would strand the note somewhere nobody named. The forward move SHALL return a permit naming exactly the two targets it moved between, and that permit SHALL authorise exactly one reverse move between those same two targets — not a second forward move, not any other pair, and not itself twice. The permit SHALL NOT be a confirmation and SHALL NOT be usable as one. This is not a second confirmation and is not claimed to be: the rollback undoes the very publication the confirmation covered, synchronously, with no intervening `await`, so it lies inside that publication's window rather than opening a new one. Stamping the one confirmation onto both endpoints instead — so that either could spend it — SHALL NOT be done, because it makes a reusable token of a single-use fact.

**The permit SHALL be unforgeable and SHALL expire with the publication that issued it.** It SHALL be constructible only by a successful, confirmed forward move; a permit built by any other caller SHALL be refused rather than honoured, because it would otherwise authorise a rename for which no confirmation was ever taken. It SHALL be bound to the lease of the confirmation the forward move consumed and SHALL be refused once that lease has been revoked, and it SHALL additionally record the immutable `(user id, assignment, vault-relative path)` of each endpoint and refuse a rollback whose endpoints no longer carry them.

**Both ends of a move SHALL belong to one caller, one assignment and one pinned root directory.** A no-clobber move removes the source directory entry as surely as it creates the destination one, but only one confirmation is consumed for the pair. The publish helper SHALL therefore require, before consuming anything and on the rollback path too, that the two targets carry identical user ids and identical canonical assignments, and that their pinned vault-root descriptors name the same directory inode — a pathname comparison is insufficient, since two assignments may spell the same string while different directories were pinned. Requiring this is what makes confirming the destination sufficient for the source; without it a source validated for one user can be removed under another user's confirmation.

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

#### Scenario: The confirming read fails before the first publication

- **WHEN** the assignment re-read fails outright — the database is unreachable — before a call has published anything
- **THEN** the call SHALL fail rather than publish
- **AND** the failure SHALL NOT be reported as a changed vault assignment, because no administrator changed anything and the server cannot say whether one did

#### Scenario: The confirming read fails after the move has committed

- **WHEN** the assignment re-read fails outright before one of `move_note`'s link rewrites, after the move has already committed
- **THEN** the tool SHALL stop the remaining rewrites and report the partial outcome through the same mechanism a refusal uses: the completed move, the root it completed in, and every source left unrewritten
- **AND** it SHALL name the cause as a confirmation outage and SHALL NOT state that the vault assignment changed
- **AND** the move SHALL NOT be rolled back and the metadata rows SHALL remain consistent with where the note now is
- **AND** the call SHALL be recorded in `usage_logs` with an error marker distinct from both the changed-assignment marker and the missing-assignment marker

#### Scenario: A permanent delete is refused by a helper, not by a convention

- **WHEN** `delete_note(permanent=True)` reaches its unlink and the assignment has changed
- **THEN** the unlink SHALL be refused and the note SHALL remain at its path
- **AND** the refusal SHALL come from the same shared enforcement that covers the atomic write, the no-clobber move and the soft delete, rather than from a check written into the tool

#### Scenario: A soft delete confirms before it moves the note to trash

- **WHEN** `delete_note` runs and the assignment has changed
- **THEN** the note SHALL remain at its path and no `.trash` entry SHALL be created for it

#### Scenario: Every mutating tool inherits the confirmation

- **WHEN** any mutation target reaches a shared publish helper — including the permanent-unlink helper — without a confirmation for the operation about to be performed
- **THEN** the helper SHALL raise rather than publish
- **AND** the resulting error SHALL be distinguishable from the refusal a changed assignment produces

#### Scenario: A confirmation cannot be spent twice, on one target or on two

- **WHEN** a confirmation that has already authorised a publication is presented to a second one — through the same target or through a different target
- **THEN** the second publication SHALL be refused and nothing SHALL be written

#### Scenario: A confirmation is bound to the user and the root it was taken for

- **WHEN** a confirmation taken for one user id, or for one canonical assignment, is presented for a target validated for another
- **THEN** the publication SHALL be refused and nothing SHALL be written

#### Scenario: A confirmation cannot be held across a scheduling point

- **WHEN** the public interface for a confirmed publication is examined
- **THEN** there SHALL be no exported way to obtain a confirmation without publishing with it in the same synchronous step
- **AND** a publish callable that is asynchronous, or that returns an awaitable, SHALL be refused rather than awaited

#### Scenario: A callable that retains its confirmation publishes nothing later

- **WHEN** a publish callable stores the confirmation it was given, returns without consuming it, and the caller later presents that object to a publish helper — after an administrator's reassignment has committed
- **THEN** the wrapper SHALL refuse the callable's return, because nothing was consumed
- **AND** the retained confirmation SHALL authorise no publication, because its lease was revoked when the callable returned

#### Scenario: The lease is revoked when the callable raises

- **WHEN** a publish callable raises
- **THEN** its exception SHALL propagate unchanged
- **AND** the confirmation SHALL nonetheless be left unable to authorise a later publication

#### Scenario: A deferred publish callable is refused, not driven

- **WHEN** the publish callable is a coroutine function, a generator function or an async-generator function, or returns a coroutine, a generator or an async generator
- **THEN** it SHALL be refused
- **AND** the wrapper SHALL NOT call `close` or any other method on the returned object

#### Scenario: A move permit cannot be constructed by a caller

- **WHEN** any caller other than a successful confirmed forward move attempts to build a move permit
- **THEN** the attempt SHALL be refused, and no rename SHALL be authorised by it

#### Scenario: A move permit is inert once its publication has returned

- **WHEN** a permit issued inside a confirmed publication is presented after that publication has returned
- **THEN** the rollback SHALL be refused

#### Scenario: A move refuses endpoints that do not share a caller, an assignment and a root

- **WHEN** a no-clobber move is asked to move between two targets validated for different users, under different canonical assignments, or anchored to different vault-root directories
- **THEN** it SHALL be refused before any confirmation is consumed, and nothing SHALL be renamed

#### Scenario: A rollback is authorised by a permit, not by a spare confirmation

- **WHEN** the inode verification after a move has to put the file back
- **THEN** the reverse move SHALL be authorised by the permit the forward move returned, for exactly those two targets and exactly once
- **AND** the permit SHALL NOT authorise a second forward move, a move between any other pair of targets, or a second use of itself
- **AND** no confirmation SHALL be left unspent on either endpoint once the move stands

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
