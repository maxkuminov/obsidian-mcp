## MODIFIED Requirements

### Requirement: Tool calls are admitted only while the caller holds a vault assignment
Every MCP tool call that reaches its tool body SHALL have resolved the caller's
vault root once beforehand, and SHALL fail the call with a tool error when the
root cannot be resolved. A call refused by an earlier gate in the same shared
decorator — a per-principal rate bucket, or any other gate the decorator runs
before vault resolution — SHALL be refused without resolving the vault root.
That is sound because no tool body runs and the refusal reveals nothing about
the vault: it names no note path, title, tag, frontmatter value or chunk
excerpt, and its content depends only on the caller's own request rate. The
vault check MUST live in the shared tool decorator rather than in individual
tools, so that a tool served entirely from the database is covered without
opting in. Refusal MUST NOT depend on whether the process cache was previously
warmed, and MUST NOT delete the caller's `notes_metadata`, `note_embeddings`
or `note_links` rows.

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

#### Scenario: A rate-refused call is refused without resolving the vault
- **WHEN** a caller whose vault path has been cleared exceeds its per-principal
  rate bucket
- **THEN** the call SHALL be refused by the rate gate, no vault resolution SHALL
  be attempted, no tool body SHALL run, and the refusal SHALL name no note path,
  title, tag, frontmatter value or chunk excerpt

#### Scenario: An unassigned caller within its rate limit is still refused for the vault
- **WHEN** the same caller makes a call that its rate buckets admit
- **THEN** the vault gate SHALL refuse it with the unchanged no-vault tool error
  and the unchanged `no_vault_assigned` marker

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
