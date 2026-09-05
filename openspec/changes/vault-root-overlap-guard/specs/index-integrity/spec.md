## ADDED Requirements

### Requirement: No pass over a vault root SHALL begin without a quarantine snapshot published by the shared detection
Every code path that can begin an index, link-backfill, embed or tsvector-rebuild pass over a vault root SHALL first call one shared detection routine, and that routine SHALL be the only thing in the process that computes and publishes a quarantine snapshot. The routine SHALL evaluate the identity, containment and mount-grafting conditions across the roots of all active users holding an assignment, taking each root's device, inode and canonical real path from **one opened directory descriptor** rather than from the assignment string, and SHALL issue no database write.

Installing detection in the periodic loop alone leaves it installed in one of five places. The panel's on-demand reindex — reached by **Reindex Now**, by *re-embed* and by *reset embeddings* — mirrors the loop and shares only `index_pass_lock` with it, and the standalone tsvector rebuild (`make rebuild-tsvectors`) is a **separate process** with its own user enumeration, no loop and no lifespan. Both reach a pass, and neither would consult a check placed in the loop. The requirement is therefore stated over the property — no pass begins unchecked — and the per-user stage skip SHALL live in the shared pass helpers rather than in each caller's loop, so a sixth entry point added later inherits the guard by routing through the same helper instead of by remembering to add a call.

The startup entry point SHALL run the detection **synchronously before the application serves its first request**, and SHALL NOT rely on the asynchronous startup pass for it: between accepting connections and that pass completing, a tool call would otherwise be served against roots nothing had checked.

#### Scenario: The scheduled loop publishes before it indexes

- **WHEN** a periodic iteration begins
- **THEN** a snapshot SHALL be published before any note beneath any root is read

#### Scenario: The startup path publishes before the application serves

- **WHEN** the application starts
- **THEN** the first snapshot SHALL be published before the first request is served and before the indexer task is created

#### Scenario: The panel's on-demand reindex publishes

- **WHEN** an administrator triggers Reindex Now, re-embed, or reset embeddings
- **THEN** the resulting pass SHALL publish a snapshot before it takes the pass lock
- **AND** SHALL skip a quarantined user exactly as a scheduled pass does

#### Scenario: The standalone tsvector rebuild publishes

- **WHEN** the standalone rebuild process is run
- **THEN** it SHALL publish its own snapshot before rebuilding any keyword vector
- **AND** SHALL rebuild nothing for a quarantined user

#### Scenario: The skip is enforced in the shared pass helper

- **WHEN** the pass helpers are inspected
- **THEN** the per-user skip SHALL be enforced inside them, so that every caller inherits it
- **AND** no caller SHALL be relied upon to re-implement it

#### Scenario: Single-user mode has nothing to detect

- **WHEN** the server runs in single-user mode, where the root comes from settings and no `users` row carries an assignment
- **THEN** the published snapshot SHALL be empty and every pass SHALL behave exactly as it does today

### Requirement: The published snapshot SHALL be atomic, tri-state, and SHALL NOT regress on a failed detection
Publication SHALL replace the snapshot with one immutable value in a single assignment, so no reader observes a partially built snapshot. The snapshot SHALL be tri-state — never published, published and empty, or published with quarantine reasons — and a detection that raises **after** a snapshot has been published SHALL retain the previous snapshot and log at ERROR. It MUST NOT clear the snapshot back to the never-published state.

The three states answer three different questions and collapsing any two is a defect. "Never published" means nothing has been checked and the correct response is to refuse; "published and empty" means everything was checked and nothing overlaps; and a failed re-detection means the last complete answer is the best available one. Clearing on failure would turn a transient database blip into a deployment-wide refusal, and treating a failure as an all-clear would serve overlapping roots on the strength of a query that never returned.

A detection failure is not a per-root failure: a root that cannot be opened is a per-user verdict, so the only way the routine itself fails is that the user enumeration failed — which means the database is unavailable and the tools are unusable regardless. The routine SHALL therefore log and let the process keep serving the panel rather than exiting, and SHALL retry at the next entry point.

#### Scenario: A failed re-detection keeps the last snapshot

- **WHEN** a snapshot has been published and a later detection raises
- **THEN** the previously published snapshot SHALL remain in force
- **AND** the failure SHALL be logged at ERROR

#### Scenario: A failed first detection does not become an all-clear

- **WHEN** the first detection of the process raises
- **THEN** the snapshot SHALL remain in the never-published state
- **AND** the process SHALL keep serving the panel rather than exiting

#### Scenario: No reader sees a partial snapshot

- **WHEN** a detection is publishing while a request reads the snapshot
- **THEN** the reader SHALL observe either the previous snapshot or the new one in full

#### Scenario: Sandbox mode is ready without touching the filesystem

- **WHEN** the server starts in sandbox mode, where there are no users and the indexer is skipped
- **THEN** an empty snapshot SHALL be published without opening any root

### Requirement: The snapshot SHALL record why each user is quarantined, and an unexaminable root SHALL NOT be reported as an overlap
The snapshot SHALL map each quarantined user to a structured reason: an **overlap** carrying the peer user and the relation found (identical, contains, contained by, or mount graft), or a **root unexaminable** carrying the error number and naming no peer. Each reason SHALL be worded separately wherever it is surfaced — the control panel, the log line, the `indexer_runs` row and the usage-log marker.

A root that could not be opened is not an overlap. Reporting it as one sends an operator looking for a second account that does not exist, and reporting it under the overlap marker makes the two indistinguishable in the usage log. The user is quarantined because their status could not be established, which is a different fact requiring a different fix.

An unexaminable root SHALL quarantine **only that user**. The peers it could not be compared against SHALL keep being indexed and served — fail closed for the user whose status is unknown, fail open for users against whom nothing was observed, so that one broken mount does not take the deployment offline.

#### Scenario: An overlap names the peer and the relation

- **WHEN** two users' roots are detected as overlapping
- **THEN** each user's reason SHALL name the other user and the relation found

#### Scenario: An unexaminable root names no peer

- **WHEN** a user's assigned root cannot be opened
- **THEN** that user's reason SHALL record that the root could not be examined, with the error number
- **AND** SHALL NOT name any peer user or claim an overlap was observed

#### Scenario: An unexaminable root quarantines only its own user

- **WHEN** one active user's root cannot be opened and two other users hold unrelated, examinable roots
- **THEN** only the first user SHALL be quarantined
- **AND** the other two SHALL be indexed normally

### Requirement: A quarantined user SHALL NOT be indexed, and unrelated users SHALL be
A pass SHALL skip the index, link-backfill, embed and tsvector-rebuild stages for every user the published snapshot names, and SHALL run all of them normally for every active user it does not name. The skip SHALL NOT delete, prune or otherwise mutate any `notes_metadata`, `note_embeddings` or `note_links` row belonging to a skipped user, and SHALL NOT write that user's provenance record.

Continuing to index an overlapping pair files one tenant's notes under the other tenant's `user_id`, which makes them answerable by `semantic_search`, `keyword_search` and every graph tool — a silently wrong search result delivered to an agent, which is the failure this server ranks highest. Refusing the named users is the narrowest control that stops it: the condition is a property of specific roots and says nothing about a third tenant's vault, so quarantining the deployment would convert a two-tenant misconfiguration into an outage for everyone.

Nothing is deleted for the same reason unassignment deletes nothing: preserving the rows is what makes a corrected assignment cheap, and the repair the operator's correction triggers — a discard or a re-derive from the provenance classification, plus the ordinary prune of rows whose files are no longer beneath the root — is machinery that already exists and is already reviewed. A blanket delete would be a second, unreviewed deletion path over index contents.

#### Scenario: Unrelated tenants keep indexing

- **WHEN** users A and B hold overlapping roots and user C holds an unrelated root
- **THEN** the pass SHALL index, backfill and embed C exactly as before
- **AND** SHALL perform none of those stages for A or B

#### Scenario: No rows are destroyed by the refusal

- **WHEN** a pass skips a user named by the snapshot
- **THEN** that user's `notes_metadata`, `note_embeddings` and `note_links` rows SHALL be unchanged
- **AND** the user's recorded vault provenance SHALL be unchanged

#### Scenario: A root that becomes aliased after assignment is detected at the next entry point

- **WHEN** two users hold non-overlapping assignments, both are indexing normally, and one user's assigned path is subsequently made a symbolic link to — or a bind mount of — the other user's root
- **THEN** the next detection SHALL name both users
- **AND** neither SHALL be indexed until the condition is corrected

#### Scenario: A root that becomes nested after assignment is detected

- **WHEN** one user's assigned path is subsequently replaced by a symbolic link resolving to a directory inside another active user's root
- **THEN** the next detection SHALL find it through the canonical real paths, not through the unchanged assignment strings

#### Scenario: A vault grafted into another by a bind mount is detected

- **WHEN** one user's vault directory is bind-mounted to a path inside another active user's root, leaving both root inodes distinct and both canonical real paths outside each other
- **THEN** the mount-grafting condition SHALL name both users
- **AND** where the mount table cannot be read, the condition SHALL be skipped and logged once rather than reported as an overlap

#### Scenario: A corrected condition resumes indexing

- **WHEN** an administrator changes one of the two roots so that no condition holds
- **THEN** the next detection SHALL publish a snapshot naming neither user
- **AND** both SHALL be indexed again, with the existing provenance classification deciding whether the previous rows are kept, re-derived or discarded

### Requirement: A quarantine SHALL be recorded durably for each affected user, and a pause SHALL NOT suppress the record
A pass that skips a user because the snapshot names them SHALL log the fact at ERROR with the reason-specific wording, and SHALL record it in that user's `indexer_runs` row so the record survives a container restart. An iteration that finds the indexer **paused** SHALL still publish the snapshot, still emit the ERROR log and still write those per-user run rows before returning; the pause suppresses index and embed work only.

Two records because they answer different questions over different lifetimes. The log line reaches the in-process error ring buffer, which is 100 entries and process-lifetime: the line naming a quarantine at deploy time is gone by the next restart while the misconfiguration persists. The run row is what an operator reads after a restart, and a pass that quietly did no work for a user would otherwise be indistinguishable from a pass that found nothing to do. A pause is entered precisely when an operator is doing something destructive and watching the panel, which is the worst moment for a quarantine to become invisible; and the row cadence is unchanged, because a running deployment already writes one row per user per iteration.

#### Scenario: The skip reaches the run row

- **WHEN** a pass skips a user for a quarantine
- **THEN** an `indexer_runs` row SHALL be written for that user
- **AND** its error text SHALL carry the reason-specific wording, naming the peer for an overlap and the error number for an unexaminable root

#### Scenario: The skip reaches the error buffer

- **WHEN** the same pass runs
- **THEN** it SHALL log at ERROR, so the health page's recent-errors section shows it while the process lives

#### Scenario: A paused iteration still records

- **WHEN** an iteration begins while the indexer is paused and the snapshot names at least one user
- **THEN** the snapshot SHALL still be published, the ERROR SHALL still be logged, and the per-user run rows SHALL still be written before the iteration returns
- **AND** no index or embed work SHALL be performed

#### Scenario: A skip is not reported as a healthy pass

- **WHEN** every active user in a deployment is skipped for a quarantine
- **THEN** the pass SHALL NOT be recorded as a clean run for those users
