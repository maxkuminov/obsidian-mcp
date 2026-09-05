## ADDED Requirements

### Requirement: A caller whose vault root overlaps another tenant's SHALL be refused by the admission gate
Every MCP tool call by a user in the published overlap set SHALL be refused by the shared admission gate, for the same reason and through the same mechanism as a caller with no vault assignment: the root resolution SHALL raise and the decorator SHALL fail the call before the tool body runs. The refusal SHALL apply to every registered tool with no exemptions, SHALL NOT delete the caller's index rows, and SHALL be recorded in `usage_logs` under a marker distinct from the no-assignment marker.

Refusing to *index* an overlapping pair is not sufficient and must not be mistaken for the whole control. The database-backed tools answer from `notes_metadata` and `note_embeddings` and never touch the disk, so rows a previous pass already wrote for the other tenant's notes stay queryable; and the write tools resolve beneath the caller's root, which physically contains the other tenant's files, so `edit_note`, `move_note`, `delete_note` and `write_file` reach them and the beneath-root containment check agrees they are contained. The write path consults no indexer. A cross-tenant destructive write is the failure this product ranks highest, and the admission gate is the only control that is total over it.

The marker is distinct because the markers already distinguish things an operator would act on differently. Recording an overlap quarantine as "no vault assigned" tells an operator that an administrator unassigned a user whose users page plainly shows an assignment — a contradiction that sends the investigation at a person rather than at a mount.

The refusal message the caller receives SHALL name no other user and no other path. The caller is a tenant's agent; the operator-facing surfaces are where the conflicting user and root are named.

#### Scenario: Database-backed tools are refused

- **WHEN** a user in the overlap set calls `semantic_search`, `keyword_search`, `list_notes`, `get_recent`, `get_tags` or any graph tool with an unchanged, still-active credential
- **THEN** the call SHALL be refused with a tool error naming no note path, title, tag, frontmatter value or chunk excerpt

#### Scenario: Write tools are refused

- **WHEN** the same caller calls `create_note`, `edit_note`, `move_note`, `delete_note`, `set_frontmatter`, `write_file` or `delete_file`
- **THEN** the call SHALL be refused before any path is resolved beneath the root and before any byte is written

#### Scenario: The refusal names no other tenant

- **WHEN** any tool call is refused for an overlap
- **THEN** the message SHALL NOT contain another user's username, another user's vault path, or any note path

#### Scenario: The refusal is recorded under its own marker

- **WHEN** a tool call is refused for an overlap
- **THEN** the `usage_logs` row SHALL carry an error marker distinct from the one used for a caller with no vault assignment

#### Scenario: The index survives the refusal

- **WHEN** the overlap is corrected and the caller's assignment is unchanged
- **THEN** the caller's previously indexed rows SHALL still be present

#### Scenario: Unrelated callers are unaffected

- **WHEN** a user who is not in the overlap set calls any tool
- **THEN** the call SHALL be admitted exactly as before

#### Scenario: Single-user mode is unaffected

- **WHEN** the server runs in single-user mode, where the caller has no user id and the root comes from settings
- **THEN** no overlap test SHALL apply and admission SHALL behave exactly as it does today

#### Scenario: The panel vault browser refuses the same user

- **WHEN** a user in the overlap set opens the panel's vault browser
- **THEN** the page SHALL render the existing unavailable-vault empty state rather than listing a directory tree that contains another tenant's notes

## MODIFIED Requirements

### Requirement: The admission gate performs no database work
Resolving the caller's vault root for admission SHALL NOT issue a database statement and SHALL NOT perform filesystem I/O, so the check costs nothing on the hot path. The overlap test the gate performs SHALL be a membership test against a set already computed by the indexer pass, and it SHALL be able only to refuse — it MUST NOT be capable of admitting a caller the rest of the gate would refuse.

The gate runs on every tool call and the per-request cache warm is what makes a cache read correct there; a query would be a query per call, and overlap detection needs both a query for every other user's assignment and an `open`, `fstat` and `realpath` per root. Both belong in the pass, which already does them. Because the test can only refuse, it is safe to consult it ahead of the request's immutable vault-root snapshot: unlike an assignment — where a stale read must never re-admit a revoked caller, which is why the snapshot outranks the process-global cache — a quarantine has no direction in which staleness admits anyone.

#### Scenario: Assigned caller invokes a tool

- **WHEN** an assigned caller's tool call passes the admission gate
- **THEN** the gate SHALL have opened no database session

#### Scenario: The overlap test opens nothing

- **WHEN** a tool call is refused for an overlap
- **THEN** the gate SHALL have opened no database session and made no filesystem call

#### Scenario: The overlap test cannot admit

- **WHEN** a caller has no vault assignment and is also absent from the overlap set
- **THEN** the call SHALL still be refused for having no assignment
