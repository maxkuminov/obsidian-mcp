## ADDED Requirements

### Requirement: Every indexer pass SHALL detect overlapping or aliased active vault roots before it indexes any user
Each indexer iteration SHALL, before it indexes, backfills links for, or embeds any user, evaluate the identity and containment conditions across the roots of all active users holding an assignment, and SHALL publish the set of users standing in an overlap relation with at least one other user. The evaluation SHALL open one directory descriptor per root and take its `(st_dev, st_ino)` and its canonical real path from that descriptor, in one moment, rather than comparing assignment strings. The evaluation SHALL issue no write to the database.

Detecting only at assignment time would close the administrator-initiated case and nothing else. A root is a pathname into a filesystem that keeps moving: a symbolic link created after the assignment, a compose file edited to repoint a bind mount, or a `vault_path` written directly in psql all produce the same overlap with no panel request to intercept. The pass is where this belongs because it already resolves every active user's root and already opens each one, and because running it on the same cadence bounds the undetected window to one index interval.

The startup pass is an iteration, so startup is covered by this requirement and SHALL NOT be given a separate lifespan guard that could fall out of step with it.

#### Scenario: A root that becomes aliased after assignment is detected at the next pass

- **WHEN** two users hold non-overlapping assignments, both are indexing normally, and one user's assigned path is subsequently made a symbolic link to — or a bind mount of — the other user's root
- **THEN** the next indexer pass SHALL detect the overlap
- **AND** both users SHALL be in the published overlap set

#### Scenario: A root that becomes nested after assignment is detected at the next pass

- **WHEN** one user's assigned path is subsequently replaced by a symbolic link resolving to a directory inside another active user's root
- **THEN** the next pass SHALL detect the overlap through the canonical real paths, not through the unchanged assignment strings

#### Scenario: Detection precedes indexing

- **WHEN** a pass begins with an overlap already present
- **THEN** the detection SHALL complete before any note beneath either root is read

#### Scenario: A paused indexer does not un-quarantine

- **WHEN** an operator pauses the indexer from the panel while an overlap stands
- **THEN** the detection SHALL still run on each iteration and the overlap set SHALL still be published
- **AND** no index or embed work SHALL be performed, as a pause already requires

#### Scenario: Single-user mode has nothing to detect

- **WHEN** the server runs in single-user mode, where the root comes from settings and no `users` row carries an assignment
- **THEN** the published overlap set SHALL be empty and the pass SHALL behave exactly as it does today

### Requirement: A user in a detected overlap SHALL NOT be indexed, and unrelated users SHALL be
A pass SHALL skip the index, link-backfill and embed stages for every user in the published overlap set, and SHALL run all three stages normally for every active user that is not in it. The skip SHALL NOT delete, prune or otherwise mutate any `notes_metadata`, `note_embeddings` or `note_links` row belonging to a skipped user, and SHALL NOT write that user's provenance record.

Continuing to index an overlapping pair files one tenant's notes under the other tenant's `user_id`, which makes them answerable by `semantic_search`, `keyword_search` and every graph tool — a silently wrong search result delivered to an agent, which is the failure this server ranks highest. Refusing the pair is the narrowest control that stops it: the overlap is a property of two specific roots and says nothing about a third tenant's vault, so quarantining the deployment would convert a two-tenant misconfiguration into an outage for everyone.

Nothing is deleted for the same reason unassignment deletes nothing: preserving the rows is what makes a corrected assignment cheap, and the repair the operator's correction triggers — a discard or a re-derive from the provenance classification, plus the ordinary prune of rows whose files are no longer beneath the root — is machinery that already exists and is already reviewed. A blanket delete would be a second deletion path over index contents whose failure mode is worse than the one it addresses.

#### Scenario: Unrelated tenants keep indexing

- **WHEN** users A and B hold overlapping roots and user C holds an unrelated root
- **THEN** the pass SHALL index, backfill and embed C exactly as before
- **AND** SHALL perform none of those three stages for A or B

#### Scenario: No rows are destroyed by the refusal

- **WHEN** a pass skips a user because of a detected overlap
- **THEN** that user's `notes_metadata`, `note_embeddings` and `note_links` rows SHALL be unchanged
- **AND** the user's recorded vault provenance SHALL be unchanged

#### Scenario: A corrected assignment resumes indexing

- **WHEN** an administrator changes one of the two roots so that the overlap no longer holds
- **THEN** the next pass SHALL publish an overlap set that excludes both users
- **AND** both SHALL be indexed again, with the existing provenance classification deciding whether the previous rows are kept, re-derived or discarded

#### Scenario: A root that cannot be opened quarantines only its own user

- **WHEN** a pass cannot open a directory descriptor for one active user's assigned root
- **THEN** that user SHALL be treated as being in the overlap set, because their overlap status could not be established
- **AND** every other active user SHALL be indexed normally, so one unreadable root does not stop the deployment

### Requirement: A detected overlap SHALL be recorded durably for the affected users
A pass that skips a user because of a detected overlap SHALL log the fact at ERROR, naming both users and both roots, and SHALL record it in that user's `indexer_runs` row so the record survives a container restart. The recorded text SHALL identify the other user in the relation.

Two records because they answer different questions over different lifetimes. The log line reaches the in-process error ring buffer, which is 100 entries and process-lifetime: the line naming an overlap at deploy time is gone by the next restart while the misconfiguration persists, exactly as a link-truncation log line outlives nothing while the truncated rows persist. The run row is what an operator reads after a restart, and a pass that quietly did no work for a user would otherwise be indistinguishable from a pass that found nothing to do.

#### Scenario: The skip reaches the run row

- **WHEN** a pass skips a user for an overlap
- **THEN** an `indexer_runs` row SHALL be written for that user
- **AND** its error text SHALL state that the root overlaps another user's and name that user

#### Scenario: The skip reaches the error buffer

- **WHEN** the same pass runs
- **THEN** it SHALL log at ERROR, so the health page's recent-errors section shows it while the process lives

#### Scenario: A skip is not reported as a healthy pass

- **WHEN** every active user in a deployment is skipped for an overlap
- **THEN** the pass SHALL NOT be recorded as a clean run for those users
