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
