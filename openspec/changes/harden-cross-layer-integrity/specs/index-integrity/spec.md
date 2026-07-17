## ADDED Requirements

### Requirement: Derived index updates are retry-safe
The system SHALL commit note metadata, content hashes, FTS vectors, deletion cleanup, and outgoing link rows as one coherent transaction for an index pass. A failed or cancelled pass MUST NOT persist a metadata state that causes unfinished derived work to be skipped on retry.

#### Scenario: Failure after metadata update
- **WHEN** an index pass fails after preparing a new content hash but before FTS or links are complete
- **THEN** the transaction SHALL roll back
- **AND** the unchanged file SHALL be selected again by the next pass

### Requirement: Link backfill is per-user and restart-safe
Startup link backfill SHALL determine completion independently for each user scope and SHALL NOT treat links belonging to one user as proof that another user is complete. Partial backfill work MUST NOT be recorded as complete.

#### Scenario: Multiple users require migration backfill
- **WHEN** two users have indexed notes and neither user has link rows
- **THEN** startup SHALL backfill both users' notes

#### Scenario: Backfill is interrupted
- **WHEN** a user's link backfill fails before all notes are processed
- **THEN** its partial transaction SHALL roll back
- **AND** startup SHALL retry that user's complete backfill later

### Requirement: Single-user index work is NULL-owned scoped
When an index operation is invoked with no user identifier, the operation SHALL select, mutate, embed, rebuild, and resolve links only for metadata whose `user_id` is NULL. Rows owned by named users MUST remain unchanged, including after a deployment-mode transition leaves mixed scopes in the database.

#### Scenario: Mixed ownership survives single-user indexing
- **WHEN** NULL-owned and user-owned metadata coexist and a single-user index, embed, link, or FTS rebuild pass runs
- **THEN** only NULL-owned rows SHALL be selected or mutated
- **AND** user-owned metadata, vectors, and links SHALL remain unchanged

### Requirement: Embedding completion has exact cardinality
The system SHALL accept an embedding batch only when it contains exactly one vector for every requested chunk. It SHALL record an empty or fully-cleaned note as current with zero vectors.

#### Scenario: Provider returns too few vectors
- **WHEN** the provider returns fewer embeddings than requested chunks
- **THEN** the note SHALL NOT be marked current
- **AND** previously valid embeddings SHALL remain intact

#### Scenario: Note has no embeddable chunks
- **WHEN** cleaning and chunking produces zero chunks
- **THEN** the note's embedded content hash SHALL be marked current
- **AND** the note SHALL have zero embedding rows
