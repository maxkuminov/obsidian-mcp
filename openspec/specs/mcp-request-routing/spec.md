# mcp-request-routing Specification

## Purpose
TBD - created by archiving change harden-cross-layer-integrity. Update Purpose after archive.
## Requirements
### Requirement: Root MCP fallback preserves application middleware
An MCP request accepted through the bearer-authenticated root-path fallback SHALL traverse the same trust, proxy, CORS, session, security-header, and compression middleware boundary as the canonical MCP path. Routing MUST NOT recursively invoke the root fallback.

#### Scenario: Forged host on root fallback
- **WHEN** a bearer-authenticated MCP request targets `/` with a Host value rejected by TrustedHost middleware
- **THEN** the root fallback request SHALL be rejected under the same policy as `/mcp/`

#### Scenario: Root fallback CORS preflight
- **WHEN** a root fallback request requires CORS handling
- **THEN** the response SHALL contain the same CORS policy applied to canonical application routes

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

### Requirement: Usage attribution survives deletion of the credential
Every `usage_logs` row written for an authenticated MCP tool call SHALL record the calling credential's identity denormalised onto the row itself — `actor_kind` (`api_key` or `oauth`), `actor_label` (the API key's name or the OAuth client's `client_name`) and `actor_ref` (the API key's `omcp_` prefix or the `client_id`) — captured at call time from the credential the request authenticated with. Those values MUST NOT be derived from a join at read time, MUST NOT be modified when the credential is later revoked, renamed or deleted, and MUST NOT be read for any authorization decision.

#### Scenario: An API-key call is labelled at write time
- **WHEN** a tool call authenticated with an API key is logged
- **THEN** the `usage_logs` row SHALL carry `actor_kind = 'api_key'`, the key's name as `actor_label`, and the key's `omcp_` prefix as `actor_ref`

#### Scenario: An OAuth call is labelled at write time
- **WHEN** a tool call authenticated with an OAuth access token is logged
- **THEN** the `usage_logs` row SHALL carry `actor_kind = 'oauth'`, the client's `client_name` as `actor_label`, and its `client_id` as `actor_ref`

#### Scenario: The label survives the panel deleting an API key
- **WHEN** the control panel NULLs `usage_logs.key_id` and then deletes the API key, as it must because that column has no `ON DELETE`
- **THEN** every affected row SHALL keep its `actor_kind`, `actor_label` and `actor_ref` unchanged
- **AND** the usage page SHALL still name that key as the actor

#### Scenario: The label is not overwritten by a later rename
- **WHEN** a credential is renamed after calls have been logged under it
- **THEN** the previously written rows SHALL continue to report the name the credential had at call time

#### Scenario: A call with no credential context is still recorded
- **WHEN** a usage row is written outside an authenticated MCP request
- **THEN** the row SHALL be written with the actor columns left unset rather than the write being refused

#### Scenario: The label does not leak between requests
- **WHEN** an authenticated request completes, by returning, by raising, or by being cancelled
- **THEN** the request-scoped actor SHALL be reset, so no later call can be logged under it

#### Scenario: A refused call is attributed too
- **WHEN** a tool call is refused before its body runs because the caller has no resolvable vault
- **THEN** the recorded refusal SHALL carry the actor of the credential that made it

#### Scenario: Capturing the label costs no extra query
- **WHEN** an OAuth request is authenticated
- **THEN** the client's name SHALL be obtained from the statement that resolves the token
- **AND** no additional query against the client table SHALL be issued on any path

### Requirement: A usage row is not lost when its credential is deleted mid-call
A usage log write whose credential row no longer exists SHALL still record the call, with its actor label intact and the dangling foreign keys cleared. The recovery MUST be limited to a foreign-key violation, MUST be attempted at most once, and MUST NOT propagate any failure to the tool call it describes.

#### Scenario: The key is deleted while a call is in flight
- **WHEN** an API key is deleted between the start of a tool call and the write of its usage row
- **THEN** the row SHALL be written with `key_id` NULL and its actor kind, label and reference unchanged

#### Scenario: The OAuth client is deleted while a call is in flight
- **WHEN** an OAuth client is deleted, cascading its tokens, between the start of a tool call and the write of its usage row
- **THEN** the row SHALL be written with `oauth_token_id` NULL and its actor kind, label and reference unchanged

#### Scenario: Only the violated owner column is dropped
- **WHEN** the violated foreign key is the credential's rather than the user's
- **THEN** `user_id` SHALL be preserved, so the row stays visible on its owner's scoped usage page

#### Scenario: An unidentifiable violation fails safe
- **WHEN** the violated constraint cannot be identified
- **THEN** every credential column SHALL be cleared so the row is still recorded

#### Scenario: Other failures are not retried
- **WHEN** the usage write fails for any reason other than a foreign-key violation
- **THEN** no retry SHALL be attempted
- **AND** the tool call SHALL NOT fail because of it

### Requirement: Existing usage rows are labelled from credentials that still resolve
The migration introducing the actor columns SHALL label every existing `usage_logs` row whose credential still resolves, using the same relationships the usage page joins through, and SHALL leave every other row's actor columns NULL. It MUST NOT infer a label from any weaker association such as `user_id`, MUST NOT overwrite a label that is already present, and MUST NOT make the columns `NOT NULL`.

#### Scenario: Resolvable credentials are backfilled
- **WHEN** the migration runs against a database holding usage rows for an existing API key and an existing OAuth grant
- **THEN** each row SHALL receive the actor kind, label and reference of its own credential

#### Scenario: Already-orphaned rows are left NULL
- **WHEN** a usage row's credential was deleted before the migration ran
- **THEN** its actor columns SHALL remain NULL and no label SHALL be inferred for it

#### Scenario: Re-running the migration changes no label
- **WHEN** the migration executes again against a database whose rows already carry actor labels, and a credential has been renamed in between
- **THEN** no existing label SHALL be changed

#### Scenario: A pre-existing column of another shape is refused
- **WHEN** one of the actor columns already exists with a different type, with a NOT NULL constraint, or with a server default
- **THEN** the migration SHALL fail, naming the column and what was found, and change nothing

### Requirement: The migration owns the actor columns as one marked unit
The migration SHALL treat the three actor columns as a single unit that it either creates in full or verifies in full. It SHALL mark each column it creates with an ownership marker recorded in the database, SHALL complete only a pre-existing set in which every column is present, exactly typed, nullable, free of a server default and carrying that marker, and SHALL refuse every other combination. Its downgrade SHALL remove only columns carrying the marker, and SHALL remove none of them if any is unmarked.

#### Scenario: A partially present set is refused
- **WHEN** some but not all of the actor columns already exist
- **THEN** the migration SHALL fail, naming which are present and which are absent, and change nothing

#### Scenario: An unmarked set is refused
- **WHEN** all three columns exist with the right types but without the ownership marker
- **THEN** the migration SHALL fail rather than adopt values it cannot attribute to itself

#### Scenario: A marked set is completed
- **WHEN** all three columns exist with the exact shape and the ownership marker
- **THEN** the migration SHALL proceed and backfill, leaving existing labels unchanged

#### Scenario: A label beside a missing kind is refused
- **WHEN** any row carries an actor label or reference while its actor kind is NULL
- **THEN** the migration SHALL fail, naming the rows, rather than relabel them from the credential they currently point at

#### Scenario: Downgrade leaves a column it did not create
- **WHEN** a downgrade runs and any actor column does not carry the ownership marker
- **THEN** no actor column SHALL be dropped
- **AND** the downgrade SHALL fail, naming the unmarked column

### Requirement: The usage page reports the recorded actor, and says when it has none
The control panel usage view SHALL render the actor recorded on the row in preference to any value resolved by join, SHALL fall back to the join only for rows written before the actor columns existed, and SHALL state explicitly when neither is available rather than reporting a bare "unknown".

#### Scenario: The recorded label wins over a stale join
- **WHEN** a row carries an actor label and its credential still exists under a different name
- **THEN** the page SHALL display the recorded label

#### Scenario: Pre-migration rows still resolve through the join
- **WHEN** a row has no recorded actor but its credential still exists
- **THEN** the page SHALL display the actor resolved by join

#### Scenario: An unattributable row says why
- **WHEN** a row has neither a recorded actor nor a resolvable credential
- **THEN** the page SHALL indicate that the credential was deleted, not merely that the actor is unknown

#### Scenario: A recorded kind without a label does not suppress the join
- **WHEN** a row carries an actor kind but no actor label, and its credential still resolves
- **THEN** the page SHALL display the actor resolved by join

#### Scenario: An unrecognised actor kind is not misreported
- **WHEN** a row carries an actor kind the panel does not recognise
- **THEN** the page SHALL display its label and reference without attributing it to any known credential type

#### Scenario: The label is rendered as text
- **WHEN** an actor label contains markup, as an OAuth client name taken from an unauthenticated registration may
- **THEN** the page SHALL escape it, so it cannot execute in the operator's session

#### Scenario: A non-admin sees only their own rows
- **WHEN** a non-admin opens the usage view
- **THEN** every statement it issues SHALL be filtered to that user's own rows

### Requirement: The users list MUST NOT report a note count the tools will not serve
The control panel's user list SHALL NOT render a note count for an account that holds no vault assignment. It SHALL render an explicit not-served state instead, stating that every MCP tool is refused for that account and that the index is kept for reassignment, so the operator reads the same fact the admission gate enforces.

A number rendered beside `(unassigned)` reads as capacity the account has, when in fact every tool call from that account is refused before its body runs. This is the same over-reporting of liveness as the revoked-key count the panel used to present as an unqualified total, and the same class as a control offered for a credential the middleware already rejects.

#### Scenario: Unassigned account

- **WHEN** the user list renders an account whose vault assignment is empty
- **THEN** the note column SHALL show a not-served state rather than a number
- **AND** SHALL state that the tools are refused and the index is retained for reassignment

#### Scenario: Assigned account

- **WHEN** the user list renders an account that holds a vault assignment
- **THEN** the note column SHALL show that account's note count as before

#### Scenario: The retained rows are not deleted to make the display true

- **WHEN** the display changes for an unassigned account
- **THEN** the account's `notes_metadata`, `note_embeddings` and `note_links` rows SHALL remain in the database, so a reassignment to the same directory still resumes without a full re-index

