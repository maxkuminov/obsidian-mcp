## ADDED Requirements

### Requirement: Tool calls are admitted only while the caller holds a vault assignment
Every MCP tool call SHALL resolve the caller's vault root once, before the tool
body runs, and SHALL fail the call with a tool error when the root cannot be
resolved. The check MUST live in the shared tool decorator rather than in
individual tools, so that a tool served entirely from the database is covered
without opting in. Refusal MUST NOT depend on whether the process cache was
previously warmed, and MUST NOT delete the caller's `notes_metadata`,
`note_embeddings` or `note_links` rows.

#### Scenario: Database-backed search after unassignment
- **WHEN** an administrator clears a multi-user account's vault path and the
  account's unchanged, still-active API key calls `semantic_search`,
  `keyword_search`, `list_notes` or `get_recent`
- **THEN** the call SHALL be refused with a tool error naming no note path,
  title, tag, frontmatter value or chunk excerpt

#### Scenario: Graph tools after unassignment
- **WHEN** the same credential calls `get_backlinks`, `get_links`,
  `get_neighborhood`, `find_orphans` or `find_related`
- **THEN** the call SHALL be refused with the same tool error

#### Scenario: No exemptions for vault content or vault metadata
- **WHEN** the same credential calls any registered tool, including
  `get_vault_guide` (which returns the vault's own `CLAUDE.md`) and
  `check_upload` (which reports a published vault path, size and digest)
- **THEN** the call SHALL be refused

#### Scenario: Cold process cache is a refusal, not an error response
- **WHEN** a freshly started worker process that has never cached this user's
  vault root receives a tool call from that user
- **THEN** the call SHALL be refused with the same tool error rather than
  raising an unhandled exception

#### Scenario: Operator-facing label matches the enforcement
- **WHEN** an administrator opens the vault-path selector on the user edit page
- **THEN** the unassigned option SHALL state that every MCP tool refuses and
  that the index is kept for reassignment

#### Scenario: Every registered tool is covered
- **WHEN** the set of tools registered on the MCP server is enumerated
- **THEN** each one SHALL delegate to an implementation carrying the shared
  admission gate, so a tool added later inherits it by being registered

#### Scenario: The index survives the refusal
- **WHEN** the account's vault path is assigned again to the same directory
- **THEN** the previously indexed rows SHALL still be present, so tool calls
  resume without a full re-index

### Requirement: A refused tool call is recorded in the usage log
A tool call refused for a missing vault assignment SHALL be written to
`usage_logs` like any other tool error, carrying an error marker and the same
allow-listed parameters as a successful call, and no additional field.

#### Scenario: Refusal is auditable
- **WHEN** a tool call is refused for a missing vault assignment
- **THEN** a `usage_logs` row SHALL be written for that tool with an error
  marker in `params` and the tool's normal allow-listed parameters

#### Scenario: Refusal adds no new logged field
- **WHEN** that row is written
- **THEN** `params` SHALL contain no parameter outside the tool's existing
  allow-list plus the error marker

### Requirement: Single-user mode is unaffected by the admission gate
In single-user mode the vault root SHALL continue to come from configuration
and every tool SHALL continue to run, regardless of the multi-user vault-path
cache.

#### Scenario: Single-user call with an empty multi-user cache
- **WHEN** a tool is called with no current user id (single-user mode, or the
  registry-evaluation sandbox mode that bypasses authentication) and the
  multi-user vault-path cache is empty
- **THEN** the tool body SHALL run against the configured vault path and the
  usage log SHALL carry no error marker

### Requirement: Refreshing a user's cached vault root removes a revoked assignment
Refreshing the cached vault root for a single user SHALL write the user's
current assignment or remove the cached entry when the user has no usable
assignment, so that a revocation takes effect on the next authenticated tool
call in every worker process without depending on an explicit cache-clear call.

#### Scenario: Mid-session unassignment in a process that did not serve the panel request
- **WHEN** a user's vault path is cleared and a worker process that still holds
  the previous value refreshes that user's cached root while authenticating the
  next tool call
- **THEN** the cached entry SHALL be removed and the tool call SHALL be refused

#### Scenario: Deactivated user
- **WHEN** the refresh finds the user inactive or absent
- **THEN** the cached entry SHALL be removed

### Requirement: A concurrent cache refresh cannot re-admit a revoked caller
The vault root that admits a tool call SHALL be the value read while
authenticating that request, and a concurrent or stale refresh of the shared
process-level cache SHALL NOT be able to override it. The snapshot SHALL be
scoped to the request and to the user it was read for, and SHALL NOT be
consulted in single-user mode.

#### Scenario: Stale bulk refresh lands after the revocation
- **WHEN** a bulk cache refresh whose database snapshot predates the
  revocation completes *after* the request's own refresh observed the cleared
  assignment, and the request then calls a tool
- **THEN** the call SHALL be refused, even though the shared cache once again
  holds the previous vault root

#### Scenario: Snapshot does not answer for another user
- **WHEN** a vault root is resolved for a user other than the one the request
  authenticated as
- **THEN** the request snapshot SHALL be ignored and the shared cache
  consulted instead

#### Scenario: Snapshot does not outlive the request
- **WHEN** the authenticated request completes
- **THEN** the snapshot SHALL be cleared, leaving later work in that process
  with no request-scoped vault root

### Requirement: An ownerless credential is refused in multi-user mode
A credential that is not bound to a user SHALL be rejected at authentication whenever multi-user mode is enabled, and resolving a vault root with no user SHALL raise rather than falling back to the globally configured vault path.
Single-user mode SHALL be unaffected, and the panel bootstrap flow — which
claims unbound rows for the first administrator — SHALL keep working.

#### Scenario: Key minted before multi-user was enabled
- **WHEN** multi-user mode is enabled and a still-active API key whose owner is
  unset is presented to the MCP endpoint
- **THEN** the request SHALL be rejected as unauthenticated, with the same
  response body as any other rejected key

#### Scenario: OAuth token with no owner
- **WHEN** the same situation arises for an OAuth access token
- **THEN** the request SHALL be rejected as unauthenticated

#### Scenario: No fallback to the configured vault path
- **WHEN** a vault root is resolved with no user while multi-user mode is
  enabled
- **THEN** resolution SHALL fail, so no caller can reach the globally
  configured vault by having no owner

#### Scenario: Single-user mode still serves an unbound credential
- **WHEN** multi-user mode is disabled and a credential with no owner is
  presented
- **THEN** the request SHALL authenticate and resolve the configured vault path
  as before

### Requirement: The panel vault browser uses the root it just read
The control panel's vault browser SHALL browse the vault root returned by the
refresh it performs for the signed-in user, not a subsequent re-read of the
shared process cache, and SHALL render its empty state when that refresh
reports no assignment.

#### Scenario: Stale refresh lands between the read and the browse
- **WHEN** a bulk cache refresh predating the revocation repopulates the shared
  cache after the page's own refresh observed the cleared assignment
- **THEN** the page SHALL render the no-vault empty state and list no folders
  or notes

### Requirement: The admission gate performs no database work
Resolving the caller's vault root for admission SHALL NOT issue a database
statement, so the check costs nothing on the hot path.

#### Scenario: Assigned caller invokes a tool
- **WHEN** an assigned caller's tool call passes the admission gate
- **THEN** the gate SHALL have opened no database session
