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

### Requirement: A refused tool call is recorded in the usage log

A tool call refused for one of the enumerated refusal causes SHALL be written to `usage_logs` like any other tool error, carrying that cause's marker and the same allow-listed parameters as a successful call, and no field outside that allow-list, the marker, and — for a call whose body raised — the exception's class name. The enumerated causes are the three decided before the body runs (a missing vault assignment, an unencodable argument, an exhausted quota), the write refused for a read-only credential (marked `permission_denied`), a body that raised (marked `tool_exception`), and the post-body markers already in the register. Other in-band refusals a tool body returns as a message — a create over an existing path, a path or size validation, a write conflict — are **not** marked by this requirement and remain ordinary rows. Each marker SHALL be classified as pre-body or post-body when it is introduced, and two branches on opposite sides of that line SHALL NOT share a marker value.

#### Scenario: Refusal is auditable

- **WHEN** a tool call is refused for a missing vault assignment
- **THEN** a `usage_logs` row SHALL be written for that tool with an error marker in `params` and the tool's normal allow-listed parameters

#### Scenario: Refusal adds no new logged field

- **WHEN** that row is written
- **THEN** `params` SHALL contain no parameter outside the tool's existing allow-list plus the error marker

#### Scenario: A write refused for permission is auditable

- **WHEN** a read-only credential calls a write tool
- **THEN** a `usage_logs` row SHALL be written carrying the `permission_denied` marker, so that the row is distinguishable from a successful write by the same tool

#### Scenario: A raising body is auditable

- **WHEN** a tool body raises an exception
- **THEN** a `usage_logs` row SHALL be written carrying the `tool_exception` marker, the exception's class name, and the duration measured up to the raise

#### Scenario: An unenumerated in-band refusal is an ordinary row

- **WHEN** `create_note` refuses because a note already exists at the path
- **THEN** the row SHALL be written as an ordinary call with no error marker, as it is today

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
Refreshing the cached vault root for a single user SHALL write the user's current assignment, or remove the cached entry when the user has no usable assignment, so that a revocation takes effect on the next authenticated tool call in every worker process without depending on an explicit cache-clear call.

The refresh SHALL be a fresh read of `users.is_active` and `users.vault_path`, made on every authenticated request and bound to that request. Two ways of making it satisfy this requirement:
- the dedicated `warm_user_vault_cache` query;
- the same two columns read in the credential lookup statement itself (an outer join from the API key or OAuth token to its user), applied through the same write-or-evict rule.

Folding the read into the credential statement SHALL NOT weaken either property: the value is still read per request, and it is still bound to the request so that it outranks the process-global cache. The inactive-user refusal SHALL keep its reason code and response body, and a missing `users` row SHALL be refused as inactive.

#### Scenario: Mid-session unassignment in a process that did not serve the panel request
- **WHEN** a user's vault path is cleared and a worker process that still holds
  the previous value refreshes that user's cached root while authenticating the
  next tool call
- **THEN** the cached entry SHALL be removed and the tool call SHALL be refused

#### Scenario: Deactivated user
- **WHEN** the refresh finds the user inactive or absent
- **THEN** the cached entry SHALL be removed

#### Scenario: The folded read still revokes on the next request
- **WHEN** an API key's or OAuth token's user is deactivated, unassigned or deleted between two requests, and the second request's credential lookup reads the user's columns in the same statement as the credential
- **THEN** the second request SHALL be refused exactly as it would have been by the separate refresh query, with the same reason code and body

#### Scenario: One statement authenticates an API key
- **WHEN** an API-key request authenticates and the key's `last_used_at` is within the write resolution
- **THEN** authentication SHALL issue exactly one database statement, which returns the key and its user's `is_active` and `vault_path`

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
Resolving the caller's vault root for admission SHALL NOT issue a database statement and SHALL NOT perform filesystem I/O, so the check costs nothing on the hot path. The quarantine and readiness tests the gate performs SHALL be lookups into a snapshot already published by the shared detection, and they SHALL be able only to refuse — they MUST NOT be capable of admitting a caller the rest of the gate would refuse.

The gate runs on every tool call and the per-request cache warm is what makes a cache read correct there; a query would be a query per call, and detection needs a query for every other user's assignment plus an `open`, `fstat` and `realpath` per root — the latter dispatched to a worker thread under a deadline, which is not something a per-call gate can do. All of it belongs in the detection, which already does it once per pass. Because the tests can only refuse, it is safe to consult them ahead of the request's immutable vault-root snapshot: unlike an assignment — where a stale read must never re-admit a revoked caller, which is why the snapshot outranks the process-global cache — a quarantine has no direction in which staleness admits anyone.

#### Scenario: Assigned caller invokes a tool

- **WHEN** an assigned caller's tool call passes the admission gate
- **THEN** the gate SHALL have opened no database session

#### Scenario: The quarantine test opens nothing

- **WHEN** a tool call is refused for a quarantine or for readiness
- **THEN** the gate SHALL have opened no database session and made no filesystem call

#### Scenario: The quarantine test cannot admit

- **WHEN** a caller has no vault assignment and is also absent from the quarantine snapshot
- **THEN** the call SHALL still be refused for having no assignment

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
The control panel's user list SHALL NOT render a note count for an account that holds no vault assignment, nor for an account the published quarantine snapshot names. It SHALL render an explicit not-served state instead, stating for an unassigned account that every MCP tool is refused and the index is kept for reassignment, and stating for a quarantined account which reason applies — an overlap with a named account, or a root that could not be examined — so the operator reads the same fact the admission gate enforces.

A number rendered beside `(unassigned)` reads as capacity the account has, when in fact every tool call from that account is refused before its body runs. This is the same over-reporting of liveness as the revoked-key count the panel used to present as an unqualified total. A quarantined account is in exactly that position and worse: it is assigned, it is indexed, its row count is real, and nothing will serve it. Rendering the count unqualified beside a healthy-looking assignment is the most misleading of the three states, because the operator has no other cue that the account is dark.

#### Scenario: Unassigned account

- **WHEN** the user list renders an account whose vault assignment is empty
- **THEN** the note column SHALL show a not-served state rather than a number
- **AND** SHALL state that the tools are refused and the index is retained for reassignment

#### Scenario: Quarantined account

- **WHEN** the user list renders an account the quarantine snapshot names
- **THEN** the note column SHALL show a not-served state rather than a number
- **AND** SHALL state the reason, naming the conflicting account for an overlap and stating that the root could not be examined for the other reason

#### Scenario: Assigned account

- **WHEN** the user list renders an account that holds a vault assignment and is not named by the snapshot
- **THEN** the note column SHALL show that account's note count as before

#### Scenario: The retained rows are not deleted to make the display true

- **WHEN** the display changes for an unassigned or quarantined account
- **THEN** the account's `notes_metadata`, `note_embeddings` and `note_links` rows SHALL remain in the database, so a corrected assignment still resumes without a full re-index

### Requirement: Terminal tool-body outcomes are typed
The server SHALL identify every returned in-body refusal or partial completion
using a closed typed result, and SHALL classify only the terminal result without
parsing response prose or note content.

#### Scenario: Existing note creation is refused
- **WHEN** create_note returns because the destination already exists
- **THEN** the result SHALL keep its existing explanation and end with an authoritative MCP-REFUSAL line naming the applicable closed code
- **AND** its single usage row SHALL carry a post-body error marker and refused disposition

#### Scenario: Intermediate errors do not poison success
- **WHEN** a helper constructs an outcome that is discarded before the tool returns successfully
- **THEN** the returned success SHALL have no body refusal marker from that helper

#### Scenario: Note content forges a sentinel
- **WHEN** a successful read or search returns note content containing MCP-REFUSAL or error-looking prose
- **THEN** it SHALL remain a successful result and SHALL not acquire a refusal usage marker

#### Scenario: Structured errors stay bounded and parseable
- **WHEN** read_note returns a body error whose explanation exceeds its error budget
- **THEN** its public schema SHALL remain unchanged and its error SHALL fit MAX_READ_RESPONSE_CHARS with the complete authoritative final sentinel intact
- **AND** internal typed metadata SHALL not appear in serialized results

#### Scenario: Partial publication is stated honestly
- **WHEN** a move or import returns after some publication committed or rollback cannot be verified
- **THEN** its typed disposition SHALL be partial, with the existing explanation preserved
- **AND** it SHALL not claim nothing_written or successful completion of work that failed

#### Scenario: Successful empty and status results remain successes
- **WHEN** a tool returns an empty search/list/graph result, a no-op edit, or check_upload reports a valid status lookup including expired, revoked or unknown
- **THEN** its response and success classification SHALL remain unchanged

#### Scenario: Existing contracts retain precedence
- **WHEN** a body refusal uses existing precondition, permission, provider or publication rules
- **THEN** its permission and validation order, caller code, usage marker identity, quota accounting and publication boundaries SHALL remain unchanged
- **AND** a later body exception SHALL be recorded as tool_exception rather than as a returned refusal

### Requirement: Concurrency ships as zero-wait shadow observation
The server SHALL default concurrency control to shadow mode, evaluate the same zero-wait capacity predicate as enforcement against observed occupancy, and leave actual admission, results and outcomes unchanged by these concurrency ceilings.

Shadow SHALL never wait on any stage, whatever wait durations are configured. Configured positive waits SHALL be accepted in shadow mode, SHALL NOT be applied, and SHALL be reported in the shadow metadata as `configured_wait_ms` so that no reader can mistake a zero-wait observation for the result of a wait. Changing between `shadow`, `queue` and `enforce` SHALL require changing no setting other than `MCP_CONCURRENCY_MODE`.

#### Scenario: An overloaded call still runs in shadow
- **WHEN** observed concurrent work exceeds a configured ceiling in shadow mode
- **THEN** the call SHALL execute subject to existing non-concurrency gates without a concurrency wait or refusal
- **AND** an actual tool usage row SHALL carry bounded namespaced shadow metadata without changing its actual error, disposition or request count

#### Scenario: A positive wait is not applied in shadow
- **WHEN** shadow mode is configured with `MCP_CONCURRENCY_WAIT_SECONDS=5` and `MCP_CONCURRENCY_TRANSPORT_WAIT_SECONDS=2`
- **THEN** startup SHALL succeed
- **AND** a pressured call SHALL be admitted without awaiting any concurrency primitive
- **AND** its shadow metadata SHALL carry `basis: observed_occupancy_zero_wait` and `configured_wait_ms` of `{"transport": 2000, "tool": 5000}`

#### Scenario: A mode change is one line
- **WHEN** the default `.env.example` concurrency block is loaded once with mode `shadow`, once with `queue` and once with `enforce`
- **THEN** all three SHALL pass configuration validation with no other edit

### Requirement: Request and authentication occupancy have separate lifetimes
The middleware SHALL control global and fingerprint request occupancy before DB lookup and control auth-session occupancy only while its own session is open.

In `queue` and `enforce` modes, the request stage and the auth stage SHALL share one monotonic deadline per request of `MCP_CONCURRENCY_TRANSPORT_WAIT_SECONDS` (default 2, maximum 5). Waiters SHALL be bounded by `MCP_CONCURRENCY_REQUEST_WAITERS`, `MCP_CONCURRENCY_FINGERPRINT_WAITERS` and `MCP_CONCURRENCY_AUTH_WAITERS`. A request waiting at either stage SHALL hold no database connection and no auth permit, and SHALL be granted in eligible-FIFO order when capacity frees. In `enforce` mode, expiry of the deadline or a full waiter bound SHALL produce the transport refusal. In `queue` mode it SHALL produce an overrun admission instead.

Transport waiting SHALL be disconnect-aware.

From the first wait until admission ends, one watcher SHALL be the only caller of ASGI `receive`. It SHALL keep calling `receive` after a body message with `more_body: false`, and SHALL stop only when admission ends, when a disconnect arrives, or when the replay budget is exhausted.

On `http.disconnect` the middleware SHALL release every waiter count, registry reference and lease held for that request, and SHALL run no credential query.

Every message the watcher consumes SHALL be kept and replayed to the downstream application in order and unchanged, before the application's own calls are delegated to `receive`. At handoff the watcher SHALL end without cancelling an in-flight `receive`; a message whose `receive` had already completed SHALL be included in the replay, and a still-pending `receive` SHALL pass to the replay wrapper, which delivers its result exactly once after the replayed messages.

Consumed messages SHALL count against a process-wide replay budget, `MCP_CONCURRENCY_REPLAY_BUDGET_BYTES` (default 32 MiB). When the budget is exhausted, the watcher SHALL stop consuming, SHALL NOT discard anything already consumed, and the request SHALL remain bounded by the transport deadline.

After the auth permit is granted and before its session opens, the middleware SHALL skip authentication for a request whose client has disconnected.

#### Scenario: Authentication capacity is full in enforce mode
- **WHEN** a new request cannot acquire request or auth capacity before the transport deadline, or the relevant waiter bound is full
- **THEN** it SHALL receive a transport refusal without a new credential query or per-request usage INSERT

#### Scenario: A short auth burst waits instead of failing
- **WHEN** enforce mode has auth ceiling 2 and six requests arrive together, each of whose auth sessions completes in 10 ms
- **THEN** all six SHALL authenticate within the transport deadline and none SHALL receive a 429
- **AND** no more than two auth sessions SHALL be open at any instant

#### Scenario: One deadline covers both transport stages
- **WHEN** a request waits 1.5 s for the request envelope under a 2 s transport deadline and then finds the auth ceiling full
- **THEN** it SHALL wait at most the remaining 0.5 s for the auth permit before the deadline outcome for its mode applies

#### Scenario: A real disconnect frees a transport waiter
- **WHEN** `receive` delivers `http.disconnect` while a request waits for the envelope or for the auth permit, and the request task is not cancelled
- **THEN** every waiter count, registry reference and lease captured for that request SHALL be released exactly once and promptly
- **AND** no credential query and no usage row SHALL be issued for that request

#### Scenario: A disconnect after a complete body is still seen
- **WHEN** a waiting request's complete body arrives in one message with `more_body: false`, and the client then disconnects while the request still waits for the envelope or for the auth permit
- **THEN** the waiter SHALL be released promptly, with no credential query and no usage row

#### Scenario: Body messages read while waiting are preserved
- **WHEN** a request's body arrives as one or several `http.request` messages while it waits, and it is then admitted
- **THEN** the downstream application SHALL receive exactly those messages, in order, with byte-identical bodies and `more_body` flags, and SHALL then read any later messages from the real `receive`

#### Scenario: An oversized message is kept losslessly
- **WHEN** a waiting request receives one body message larger than the whole replay budget, or fragments whose total crosses the budget
- **THEN** the message that crossed the budget SHALL be kept, the watcher SHALL stop consuming, and the downstream application SHALL receive the full body byte-exact
- **AND** the request SHALL be released no later than the transport deadline

#### Scenario: A cancelled transport waiter leaks nothing
- **WHEN** the request task is cancelled while it waits at either transport stage
- **THEN** every waiter count, registry reference and lease captured for that request SHALL be released exactly once

#### Scenario: A stream outlives authentication
- **WHEN** an authenticated SSE or other request remains open after auth completes
- **THEN** its request/fingerprint lease SHALL remain held and its auth permit SHALL be released
- **AND** completion, exceptions and cancellation SHALL release each lease exactly once

#### Scenario: Authentication sends a refusal slowly
- **WHEN** the transport stalls while sending an invalid-credential response
- **THEN** the middleware SHALL already have exited its auth DB session and released that auth permit

### Requirement: Tool admission acquires the entire class lattice atomically
Every registered tool SHALL have exactly one explicit class out of embedding, vector, write, scan or light, and SHALL acquire class, tenant, principal and global dimensions together.

The closed mapping SHALL be:
- `semantic_search` → embedding;
- `find_related` → vector;
- the eight write-class tools → write;
- `keyword_search`, `list_notes`, `get_tags`, `get_neighborhood`, `find_orphans` and `list_files` → scan;
- `read_note`, `read_file`, `get_recent`, `get_vault_guide`, `get_backlinks`, `get_links`, `request_upload`, `check_upload` and `request_download` → light.

Each class ceiling SHALL be an independent ceiling no greater than the global `tools` ceiling. Class ceilings SHALL NOT be required to sum to at most `tools`.

#### Scenario: Every registered tool is classified
- **WHEN** the registered tool set is compared with the class mapping
- **THEN** every tool SHALL map to exactly one of the five classes, no class SHALL be named `other`, and an unmapped registration SHALL fail the registry test

#### Scenario: Class ceilings may overlap the global ceiling
- **WHEN** settings configure tools 6 with light 4, scan 2, write 1, embedding 1 and vector 1
- **THEN** validation SHALL accept them
- **AND** no more than six tools SHALL be admitted at once whatever mix of classes arrives

#### Scenario: A busy class cannot occupy another class's capacity while waiting
- **WHEN** a class is full and another eligible class has capacity
- **THEN** the waiting call SHALL hold no partial permits and the eligible call SHALL be able to proceed

#### Scenario: Rotating credentials does not reset tenant capacity
- **WHEN** calls share a user across API keys or OAuth grants, or an OAuth grant refreshes its token
- **THEN** tenant limits SHALL remain shared and grant-level principal limits SHALL remain stable across refresh

#### Scenario: Positive waiting has one deadline
- **WHEN** an enforced call waits on several saturated dimensions
- **THEN** it SHALL have one monotonic bounded deadline and bounded waiter registration
- **AND** timeout, zero-wait miss or waiter overflow SHALL return the typed slot_timeout result

#### Scenario: A parallel read batch waits rather than failing
- **WHEN** enforce mode runs with default settings and one principal issues eight concurrent `read_note` calls, each taking 10 ms
- **THEN** all eight SHALL complete successfully, and no `slot_timeout` SHALL be returned or logged

#### Scenario: Cancellation races with a grant
- **WHEN** a queued call is cancelled before or immediately after a grant
- **THEN** no active permit or queued entry SHALL be leaked or released twice

### Requirement: Configured MCP pool demand includes refusal logging
The server SHALL validate auth, per-class tool and usage-writer connection demand against its single defined 15-connection pool capacity with explicit headroom.

Tool demand SHALL be the maximum of Σ multiplier(class) × admitted(class) over every admissible mix, where each class is capped by its ceiling and the total is capped by `tools`. Per-class multipliers SHALL be defined once in `pool_budget.py` as write 2 and every other class 1. The validated sum SHALL be `auth + tool_demand + writers + 4 ≤ 15`.

A real-PostgreSQL test SHALL invoke every registered tool through the tracking decorator and assert that its per-task checkout peak is at most its class multiplier.

#### Scenario: Configuration exceeds the pool budget
- **WHEN** auth + tool demand + writers + headroom exceeds pool capacity, for example auth 2, tools 8 with write 3, writers 1
- **THEN** startup SHALL refuse and name every term of the sum

#### Scenario: Defaults leave a spare connection
- **WHEN** the default settings are validated
- **THEN** the computed demand SHALL be 2 + 7 + 1 + 4 = 14

#### Scenario: A tool overlaps more sessions than its class allows
- **WHEN** a registered tool's measured per-task checkout peak exceeds its class multiplier
- **THEN** the per-tool checkout test SHALL fail and name the tool, the class and the measured peak

#### Scenario: Refusal logging is flooded
- **WHEN** enforced usage writers or their bounded waiting registry are full
- **THEN** no more than the writer ceiling SHALL check out logging connections
- **AND** coalesced refused-row counts SHALL survive unsuccessful writes or cancellation
- **AND** a completed tool result SHALL not fail because its usage row could not be written

#### Scenario: Shared consumers use the headroom
- **WHEN** non-MCP components use the shared pool
- **THEN** operational documentation SHALL state that arithmetic headroom is not reserved capacity or a universal availability guarantee

### Requirement: Overflow identities cannot gain a fresh allowance
Bounded keyed registries SHALL keep overflow ownership stable while any active
lease or pending waiter still belongs to the overflow epoch.

#### Scenario: Dedicated capacity opens while an overflow identity is active
- **WHEN** an identity is active in overflow and another dedicated entry drains
- **THEN** a new request from that overflow identity SHALL remain subject to the same shared allowance
- **AND** identities without a dedicated entry SHALL remain in overflow until the epoch drains

### Requirement: A tool call whose body raises MUST NOT be lost, and its audit write MUST NOT mask the failure

The tracking decorator SHALL record a tool body's exception before re-raising it, and the record SHALL consist of one ERROR log entry carrying exception information and one best-effort `usage_logs` row whose insertion reports success or failure to the handler; a failed or interrupted insertion SHALL be logged and discarded rather than raised, so the caller always receives the original exception. The decorator SHALL guard only the tool body's invocation, so that a failure of an admission gate before it, or of the parameter and logging work after a body has completed, is never recorded as a tool exception. It SHALL catch `Exception` for the body, so that a cancellation propagates without being recorded as a tool failure and without writing a row.

#### Scenario: The row is best effort, the exception is not

- **WHEN** a tool body raises and the audit insert also fails
- **THEN** the caller SHALL receive the tool body's original exception and the failed audit write SHALL appear only as a warning record

#### Scenario: A completed write is never reported as failed

- **WHEN** a write tool completes and publishes, and the usage-logging work that follows then raises
- **THEN** no row SHALL carry the `tool_exception` marker for that call and no exception record SHALL be emitted for it

#### Scenario: Cancellation writes nothing

- **WHEN** a tool call is cancelled while its body is running
- **THEN** no `usage_logs` row SHALL be written for that call and no exception record SHALL be emitted for it

#### Scenario: The refusal count on a raising tool is not inflated

- **WHEN** a tool body raises after doing real work
- **THEN** the written row SHALL NOT match the pre-body refusal predicate, so the call's duration SHALL remain in the latency aggregates

### Requirement: A caller the quarantine snapshot names SHALL be refused by the admission gate
Every MCP tool call by a user the published quarantine snapshot names SHALL be refused by the shared admission gate, through the same mechanism as a caller with no vault assignment: the root resolution SHALL raise and the decorator SHALL fail the call before the tool body runs. The refusal SHALL apply to every registered tool with no exemptions, SHALL NOT delete the caller's index rows, and SHALL be recorded in `usage_logs` under a marker distinct from the no-assignment marker and distinct per reason — one marker for an overlap, another for a root that could not be examined.

Refusing to *index* a quarantined pair is not sufficient and must not be mistaken for the whole control. The database-backed tools answer from `notes_metadata` and `note_embeddings` and never touch the disk, so rows a previous pass already wrote for the other tenant's notes stay queryable; and the write tools resolve beneath the caller's root, which physically contains the other tenant's files, so `edit_note`, `move_note`, `delete_note` and `write_file` reach them and the beneath-root containment check agrees they are contained. The write path consults no indexer. A cross-tenant destructive write is the failure this product ranks highest, and the admission gate is the only control that is total over it.

The markers are distinct because the markers already distinguish things an operator would act on differently. Recording a quarantine as "no vault assigned" tells an operator that an administrator unassigned a user whose users page plainly shows an assignment; recording an unexaminable root as an overlap sends them looking for a second account that does not exist.

The refusal message the caller receives SHALL name no other user, no other vault path and no note path, for any reason. The caller is a tenant's agent; the operator-facing surfaces are where the affected accounts, reasons and roots are named.

#### Scenario: Database-backed tools are refused

- **WHEN** a user the snapshot names calls `semantic_search`, `keyword_search`, `list_notes`, `get_recent`, `get_tags` or any graph tool with an unchanged, still-active credential
- **THEN** the call SHALL be refused with a tool error naming no note path, title, tag, frontmatter value or chunk excerpt

#### Scenario: Write tools are refused

- **WHEN** the same caller calls `create_note`, `edit_note`, `move_note`, `delete_note`, `set_frontmatter`, `write_file` or `delete_file`
- **THEN** the call SHALL be refused before any path is resolved beneath the root and before any byte is written

#### Scenario: The refusal names no other tenant

- **WHEN** any tool call is refused for a quarantine, under either reason
- **THEN** the message SHALL NOT contain another user's username, another user's vault path, or any note path

#### Scenario: Each reason carries its own marker

- **WHEN** one call is refused for an overlap and another for a root that could not be examined
- **THEN** the two `usage_logs` rows SHALL carry different error markers
- **AND** both SHALL differ from the marker used for a caller with no vault assignment

#### Scenario: The index survives the refusal

- **WHEN** the quarantine is corrected and the caller's assignment is unchanged
- **THEN** the caller's previously indexed rows SHALL still be present

#### Scenario: Unrelated callers are unaffected

- **WHEN** a user the snapshot does not name calls any tool
- **THEN** the call SHALL be admitted exactly as before

#### Scenario: Single-user mode is unaffected

- **WHEN** the server runs in single-user mode, where the caller has no user id and the root comes from settings
- **THEN** no quarantine test SHALL apply and admission SHALL behave exactly as it does today

#### Scenario: The panel vault browser refuses the same user

- **WHEN** a user the snapshot names opens the panel's vault browser
- **THEN** the page SHALL render the existing unavailable-vault empty state rather than listing a directory tree that may contain another tenant's notes

### Requirement: A tool call SHALL be refused until a quarantine snapshot has been published in this process
Until the shared detection has published a snapshot in the serving process, the admission gate SHALL refuse every multi-user tool call with a refusal typed distinctly from both the overlap refusal and the no-assignment refusal. The startup path SHALL publish synchronously before the application serves, so this state is normally never observed; it SHALL remain reachable and SHALL fail closed when it is.

Publishing asynchronously and serving permissively in the meantime has two failure modes and both are silent. A tool call between the first accepted connection and the first published snapshot is served against roots nothing has checked — the whole window the guard exists to close, reopened once per restart. And a first detection that *raised* would leave the process permissive for the life of the container, because nothing would ever revisit the decision.

Failing closed is cheap here precisely because a detection failure is not a per-root failure. A root that cannot be opened is a per-user verdict; the routine itself fails only when the user enumeration fails, which means the database is unavailable and the tools cannot serve anyway. Single-user mode and sandbox mode SHALL NOT be affected: the former never consults the snapshot, and the latter publishes an empty one at startup without touching the filesystem.

#### Scenario: A call before the first snapshot is refused

- **WHEN** a multi-user tool call reaches the gate in a process where no snapshot has been published
- **THEN** the call SHALL be refused
- **AND** the refusal SHALL be typed distinctly from the overlap and no-assignment refusals

#### Scenario: The startup publication precedes serving

- **WHEN** the application starts normally
- **THEN** the snapshot SHALL be published before the first request is served, so an ordinary caller never observes the not-ready refusal

#### Scenario: A failed first detection keeps the gate closed

- **WHEN** the first detection raises and a tool call arrives
- **THEN** the call SHALL be refused rather than admitted on the strength of a detection that did not complete

#### Scenario: Single-user mode is never gated on readiness

- **WHEN** the server runs in single-user mode
- **THEN** the readiness state SHALL not be consulted and every tool call SHALL be admitted as it is today

### Requirement: Per-request authentication bookkeeping SHALL be throttled and SHALL NOT wait on a disk flush
`api_keys.last_used_at` SHALL be written at most once per 60 seconds per key: an authenticated request whose loaded `last_used_at` is newer than that SHALL issue no write. When a write is due, it SHALL be a conditional update that re-checks the age in its own predicate, so concurrent requests that both saw a stale value result in at most one effective write. It SHALL be committed with `SET LOCAL synchronous_commit = off` in the same transaction.

A write whose loss after a database crash could re-admit a caller or forget a revocation SHALL remain synchronously committed. That covers credential and OAuth issuance, rotation and revocation, authorization-code exchange, client registration, panel session mint and revoke, user edits, and transfer-token mint, redemption and publication. Only bookkeeping whose loss undercounts SHALL use asynchronous commit.

`SET LOCAL` SHALL be the only form used, so the setting ends with its transaction and SHALL NOT be observable on a later checkout of the same pooled connection.

#### Scenario: A fresh stamp issues no write
- **WHEN** a key whose `last_used_at` is 10 seconds old authenticates
- **THEN** no UPDATE of `api_keys` SHALL be issued

#### Scenario: A stale stamp is written asynchronously, once
- **WHEN** two requests for a key whose `last_used_at` is two minutes old authenticate concurrently
- **THEN** each request that issues the UPDATE SHALL have issued `SET LOCAL synchronous_commit = off` in that transaction first
- **AND** the stored value SHALL advance once, not be re-written by the second request

#### Scenario: The setting does not leak through the pool
- **WHEN** a connection that committed an asynchronous bookkeeping write is checked out again
- **THEN** `SHOW synchronous_commit` on it SHALL report the server default

#### Scenario: Grant and revoke paths stay synchronous
- **WHEN** an OAuth code is exchanged, a refresh token rotated or revoked, or a transfer token minted or redeemed
- **THEN** no `synchronous_commit` setting SHALL be issued in that transaction

### Requirement: The usage row SHALL be committed asynchronously, and its write contract SHALL be otherwise unchanged
`_insert_usage` SHALL issue `SET LOCAL synchronous_commit = off` as the first statement of its transaction, for both the initial insert and the foreign-key-cleared retry. Nothing else about the usage write SHALL change:
- `write_usage_row` SHALL return `True` only after the insert's transaction has committed and is visible to other sessions, and `False` otherwise;
- the writer-concurrency lease, the single FK retry, and the refusal coalescer's requeue of an unconfirmed row (#193) SHALL behave exactly as before.

`True` SHALL be documented as meaning "committed and visible", not "durable across a database server crash". A crash of the PostgreSQL server or of the host may lose rows committed within the preceding ~600 ms. An application crash or restart SHALL lose no committed row.

#### Scenario: A successful write still reports True
- **WHEN** a tool call completes and its usage row commits
- **THEN** `write_usage_row` SHALL return `True` and a second session SHALL be able to read the row immediately

#### Scenario: A failed write still requeues
- **WHEN** the usage insert fails for a reason other than a foreign-key violation
- **THEN** `write_usage_row` SHALL return `False` and a coalesced caller SHALL requeue its unconfirmed weight, as before

#### Scenario: The FK retry is also asynchronous
- **WHEN** the initial insert fails on a dangling credential and is retried with the credential columns cleared
- **THEN** the retry's transaction SHALL also begin with `SET LOCAL synchronous_commit = off`

### Requirement: Tool calls with undeclared arguments are refused
Every MCP tool call whose arguments include a name the tool does not declare SHALL fail with a tool error that names each undeclared argument, and no part of the tool body SHALL run. Every tool's published input schema MUST set `additionalProperties` to `false`. The rule MUST be applied to all registered tools in one place, after the last registration, rather than per tool, and MUST be disableable only by setting `MCP_REJECT_UNKNOWN_ARGUMENTS=false`, which restores the SDK's ignore behaviour and unmodified schemas.

#### Scenario: Misspelled filter is refused
- **WHEN** a caller invokes `keyword_search` with `{"query": "x", "folders": "Projects/"}`
- **THEN** the call SHALL return a tool error naming `folders`
- **AND** no search SHALL be executed

#### Scenario: Argument the tool lacks is refused
- **WHEN** a caller invokes `semantic_search` with `{"query": "x", "user_id": 2}`
- **THEN** the call SHALL return a tool error naming `user_id`

#### Scenario: Declared arguments still validate
- **WHEN** a caller invokes any tool with only arguments it declares, each carrying a value its declared type accepts (including `null` only where the type permits it)
- **THEN** argument validation SHALL succeed exactly as before this change
- **AND** a declared argument with an invalid value SHALL be refused exactly as before this change

#### Scenario: Published schemas forbid extras
- **WHEN** a client lists tools
- **THEN** every tool's `inputSchema` SHALL contain `"additionalProperties": false`

#### Scenario: Rollback flag
- **WHEN** the server starts with `MCP_REJECT_UNKNOWN_ARGUMENTS=false`
- **THEN** undeclared arguments SHALL be ignored and the published schemas SHALL NOT contain `additionalProperties`

### Requirement: Queue mode waits like enforcement and never refuses for capacity
The server SHALL accept `MCP_CONCURRENCY_MODE=queue`. In that mode every stage (request, auth, tool and writer) SHALL wait with enforcement's deadlines, waiter bounds and eligible-FIFO order, and SHALL admit the work with an `overrun` mark wherever enforcement would have refused for capacity.

A queue-mode overrun SHALL change only the concurrency outcome. The call SHALL then proceed through the remaining non-concurrency gates, whose outcomes and classification SHALL stay authoritative. An overrun SHALL NOT return a refusal and SHALL NOT write a `slot_timeout` row. Quota SHALL be consumed only if the call passes the daily-quota gate. The `concurrency_queue` object SHALL annotate a row and SHALL NOT alter its executed or pre-body classification. Shutdown refusal SHALL remain a refusal in every mode.

#### Scenario: A deadline expiry admits instead of refusing
- **WHEN** a tool call in queue mode waits past its tool deadline and then passes the daily-quota gate
- **THEN** the call SHALL execute and return its normal result
- **AND** its usage row SHALL carry `concurrency_queue` with `overrun: true`, `code: slot_timeout` and the waited milliseconds

#### Scenario: A waiter overflow admits instead of refusing
- **WHEN** a request arrives in queue mode while the fingerprint waiter bound is full
- **THEN** the request SHALL proceed immediately, with no 429
- **AND** the request SHALL be counted once in the `transport_overrun` windowed counter

#### Scenario: An overrun followed by a quota refusal stays a quota refusal
- **WHEN** a queue-mode call overruns its tool deadline and the daily-quota gate then refuses it
- **THEN** the caller SHALL receive the existing `over_quota` refusal, and the body SHALL NOT run
- **AND** no quota SHALL be consumed, and the row SHALL be classified as a pre-body refusal by the existing predicate even though it also carries `concurrency_queue.overrun: true`

#### Scenario: An ordinary wait is measured and sets no code
- **WHEN** a queue-mode call waits 40 ms for a light slot and is then granted
- **THEN** its row SHALL record `queue_ms` of about 40, with `concurrency_queue.overrun` false and `concurrency_queue.code` null

#### Scenario: A writer overrun keeps the row
- **WHEN** a usage writer in queue mode exceeds its writer wait
- **THEN** the row SHALL still be written, and the writer overrun SHALL be counted

### Requirement: Concurrency metadata orders observations by stage and derives its code by mode
The `concurrency_shadow` and `concurrency_queue` objects SHALL list observations in pipeline-stage order (request, auth, tool, writer), SHALL keep at most four observations, and SHALL carry `schema: 2`. They SHALL NOT contain a credential, a fingerprint or a principal identity.

In `concurrency_shadow`, every observation is a zero-wait capacity miss. Its `code` SHALL be the code of the earliest-stage observation, and truncation SHALL always keep that observation.

In `concurrency_queue`, `code` SHALL be the code of the earliest-stage observation marked `overrun: true`, and SHALL be `null` when no observation overran. Truncation SHALL always keep that overrun observation.

The stage codes SHALL be `request_concurrency_limited`, `auth_concurrency_limited`, `slot_timeout` and `writer_concurrency_limited`.

#### Scenario: A later writer observation does not mask tool pressure
- **WHEN** a shadow call observes tool pressure and its usage writer then observes writer pressure
- **THEN** the row's `concurrency_shadow.code` SHALL be `slot_timeout` and its first observation SHALL be the tool stage

#### Scenario: A transport observation outranks the tool in shadow
- **WHEN** a shadow call observes fingerprint pressure at the request stage and class pressure at the tool stage
- **THEN** `code` SHALL be `request_concurrency_limited`

#### Scenario: An earlier wait does not set the queue code
- **WHEN** a queue-mode request waits 40 ms for the auth permit without overrunning, then overruns its tool deadline
- **THEN** `concurrency_queue.code` SHALL be `slot_timeout`, and the auth wait SHALL be listed first with `overrun: false`

#### Scenario: Truncation keeps the deciding observation
- **WHEN** a call carries five distinct observations whose deciding observation (the earliest in shadow, the earliest overrun in queue) would fall outside the first four
- **THEN** exactly four SHALL be stored and the deciding observation SHALL be among them

### Requirement: Concurrency configuration is validated as a coherent hierarchy
Startup SHALL refuse any concurrency configuration that breaks a hierarchy or coherence rule, and the error SHALL name the settings involved.

The rules SHALL be:
- `fingerprint ≤ requests`;
- `principal ≤ tenant ≤ tools`;
- every class ceiling ≤ `tools`;
- `principal_waiters ≤ tenant_waiters ≤ waiters`;
- `fingerprint_waiters ≤ request_waiters`;
- `fingerprint ≥ principal + principal_waiters`;
- the pool budget.

`MCP_CONCURRENCY_OTHER` SHALL be ignored with one WARNING when it is set to `1`, and SHALL be refused at startup, with a message naming `MCP_CONCURRENCY_LIGHT` and `MCP_CONCURRENCY_SCAN`, when it is set to any other value.

#### Scenario: The transport envelope cannot refuse what the tool stage would queue
- **WHEN** settings configure principal 3, principal waiters 16 and fingerprint 4
- **THEN** startup SHALL refuse and name `MCP_CONCURRENCY_FINGERPRINT`, `MCP_CONCURRENCY_PRINCIPAL` and `MCP_CONCURRENCY_PRINCIPAL_WAITERS`

#### Scenario: A pinned legacy default is tolerated
- **WHEN** the environment carries `MCP_CONCURRENCY_OTHER=1` with otherwise valid settings
- **THEN** startup SHALL succeed and log one WARNING naming the replacement settings

#### Scenario: A tuned legacy value is refused
- **WHEN** the environment carries `MCP_CONCURRENCY_OTHER=3`
- **THEN** startup SHALL refuse with a message naming `MCP_CONCURRENCY_LIGHT` and `MCP_CONCURRENCY_SCAN`

### Requirement: Every tracked usage row carries concurrency provenance
Whenever the concurrency mode is not `off`, every `usage_logs` row written by the tracking decorator or by its refusal, coalescer or failure paths SHALL carry `params.concurrency` with `v: 2`, the current `mode`, and an `epoch`. The `epoch` SHALL be the first 12 hex digits of the SHA-256 of the canonical JSON of every `mcp_concurrency_*` setting except `mode`, together with the class-mapping version. Startup SHALL log the effective concurrency settings and the epoch once at INFO.

#### Scenario: An unpressured call is still marked
- **WHEN** a tool call runs in queue mode with no wait and no overrun
- **THEN** its row SHALL carry `params.concurrency` with `v: 2`, `mode: queue` and the current epoch, and SHALL carry no `concurrency_queue` object

#### Scenario: Refusal and coalesced rows are marked
- **WHEN** a `rate_limited` refusal row, a coalesced `slot_timeout` row, an `over_quota` row or a `tool_exception` row is written
- **THEN** each SHALL carry the same `params.concurrency` provenance

#### Scenario: A mode change keeps the epoch and a limit change moves it
- **WHEN** only `MCP_CONCURRENCY_MODE` changes between two boots
- **THEN** the epoch SHALL be identical; when any other `MCP_CONCURRENCY_*` value changes, the epoch SHALL differ

### Requirement: Transport, writer and pool outcomes are recorded in durable event-time counters with a run watermark
The server SHALL record, in a `concurrency_counters` table keyed by event-time minute bucket, epoch, mode and a metric from a closed set:
- request totals;
- per-request transport outcomes;
- writer overruns and refusals;
- pool checkout timeouts;
- the pool checkout high-water mark;
- the maximum transport wait.

Each observation SHALL be attributed to the minute in which it occurred, and that attribution SHALL be preserved when a failed flush is retried.

Each process run SHALL have a `run_id` and a `concurrency_runs` row carrying its epoch, mode, start time, completed-interval watermark (`completed_through`), clean-shutdown flag and lossy flag. The flush SHALL run every 60 s, whatever the request volume, as one bounded transaction that drains the accumulator at time t, upserts the drained buckets and sets `completed_through` to t rounded down to the minute. The shutdown flush, before the engine is disposed, SHALL set `completed_through` to t exactly and mark the run cleanly shut down. Data older than 35 days SHALL be pruned.

Each request SHALL contribute at most once to each transport metric: its worst transport outcome, recorded when the request completes. Tool-stage figures SHALL come only from usage rows, and transport, writer and pool figures only from these counters.

#### Scenario: Pressure at two transport stages counts once
- **WHEN** one shadow request observes pressure at both the request stage and the auth stage
- **THEN** `transport_pressured` SHALL increase by exactly 1 for that request

#### Scenario: A request that never reaches a tool is still counted
- **WHEN** an `initialize` request overruns the transport deadline in queue mode
- **THEN** `requests` and `transport_overrun` SHALL each increase by 1, and no usage row SHALL be written for it

#### Scenario: A flood does not amplify into writes
- **WHEN** 10,000 requests arrive within one flush interval
- **THEN** the flush SHALL issue one bounded transaction for that interval, not one statement per request

#### Scenario: An incident keeps its event-time minute
- **WHEN** a pool checkout times out at 12:00:50 and the next flush runs at 12:01:10
- **THEN** the timeout SHALL be stored in bucket 12:00, not 12:01

#### Scenario: A failed flush keeps attribution
- **WHEN** a flush that drained a 12:00 incident fails and the next flush at 12:02 succeeds
- **THEN** the incident SHALL be stored in bucket 12:00

#### Scenario: The watermark advances with no traffic
- **WHEN** a flush interval passes with no traffic
- **THEN** the run's `completed_through` SHALL still advance

### Requirement: Pool checkout timeouts are counted at the shared acquisition boundary
The database engine SHALL count every connection-pool checkout timeout, `sqlalchemy.exc.TimeoutError` raised by pool checkout, from every consumer of the engine, and SHALL track the checked-out high-water mark. It SHALL re-raise the original exception unchanged and SHALL NOT count other exceptions named `TimeoutError`.

#### Scenario: Timeouts outside tool bodies are counted
- **WHEN** a pool checkout times out during MCP authentication, during the quota gate, in a usage writer, in a panel request, or in OAuth `/token`
- **THEN** `pool_checkout_timeout` SHALL increase by 1 for each, and each caller SHALL see the same exception as before

#### Scenario: An unrelated timeout is not a pool timeout
- **WHEN** an embedding-provider call raises `TimeoutError` or `asyncio.TimeoutError`
- **THEN** `pool_checkout_timeout` SHALL be unchanged

### Requirement: A concurrency refusal consumes no durable quota and refunds no rate token
A tool call refused at the slot gate SHALL consume no daily-quota slot. Rate-bucket tokens it spent at the earlier bucket gates SHALL remain spent and SHALL NOT be refunded. The guarantee that nothing durable is consumed by a refused call SHALL be stated as covering the durable daily quota only.

#### Scenario: A write refused for capacity
- **WHEN** a write-class call passes both token buckets and is then refused with `slot_timeout` in enforce mode
- **THEN** its daily-quota counter SHALL be unchanged
- **AND** its general-bucket and write-bucket tokens SHALL remain spent, refilling only at their configured rates

### Requirement: A readiness evaluator applies fixed numeric criteria for each mode change
The server SHALL provide one pure evaluator. For a target mode of `queue` or `enforce`, it SHALL return PASS, FAIL or INSUFFICIENT_DATA for each fixed criterion, together with the numbers behind each verdict. It SHALL use only usage rows carrying `params.concurrency.v = 2`, together with `concurrency_counters` and `concurrency_runs` rows, and SHALL exclude every other row unconditionally.

A window SHALL qualify only when all of these hold:
- All of its rows, counters and runs carry the source mode (`shadow` for target `queue`, `queue` for target `enforce`) and a single epoch.
- Its end is no later than the durable watermark, the latest `completed_through` of the qualifying runs rounded down to a whole minute. The default end SHALL be that rounded watermark.
- It is covered:
  - every instant lies within some non-lossy run's `[started_at, completed_through]`, or in a gap that follows a run with a clean-shutdown flush;
  - a gap after a run that ended without a clean-shutdown flush SHALL be uncovered, however short it is.
- Its boundaries are whole minutes, with no exception: the start is rounded up and the end rounded down, including an end that falls at a clean shutdown. A minute bucket shared by two runs SHALL count only toward windows that contain that whole minute.

A non-qualifying or uncovered window SHALL yield INSUFFICIENT_DATA for every criterion.

For target `queue`, the window SHALL be at least 3 days and 300 executed tool calls, and the criteria SHALL be:
- **Q1:** tool-pressured executed calls ≤ 10 % of executed calls (rows);
- **Q2:** `transport_pressured` ≤ 5 % of `requests` (counters);
- **Q3:** zero `pool_checkout_timeout` (counters).

For target `enforce`, the window SHALL be at least 7 days and 1,000 executed tool calls, and each criterion SHALL hold over the whole window and over its last 72 hours:
- **E1:** distinct calls with a tool-stage overrun ≤ max(1, 0.1 % of executed calls) (rows);
- **E2:** zero `transport_overrun` (counters);
- **E3:** zero `writer_overrun` (counters);
- **E4:** tool `queue_ms` p99 ≤ 500 ms (rows);
- **E5:** maximum tool `queue_ms` over every v2 row carrying one — a call later refused pre-body (such as by the daily quota) included — ≤ 50 % of the tool deadline (rows), and maximum `transport_wait_max_ms` ≤ 50 % of the transport deadline (counters);
- **E6:** zero `pool_checkout_timeout`, and maximum `pool_high_water` ≤ 13 (counters).

The same evaluator SHALL back both the panel verdict and `make concurrency-report`.

#### Scenario: A thin window is not a pass
- **WHEN** the enforce evaluation covers 5 days, or fewer than 1,000 executed calls
- **THEN** every enforce criterion SHALL report INSUFFICIENT_DATA and the overall verdict SHALL not be PASS

#### Scenario: E1 boundary
- **WHEN** a qualifying window holds 2,000 executed calls and 2 calls with a tool overrun
- **THEN** E1 SHALL PASS; with 3 such calls E1 SHALL FAIL

#### Scenario: Recent regression fails even when the window average passes
- **WHEN** a 7-day qualifying window meets E4 overall but its last 72 hours have a tool `queue_ms` p99 of 800 ms
- **THEN** E4 SHALL FAIL

#### Scenario: Legacy rows are ignored, including unpressured ones
- **WHEN** the window contains pre-change rows carrying `queue_ms: 0` but no `params.concurrency`
- **THEN** the evaluator SHALL exclude them from every count and denominator

#### Scenario: A mixed-mode window does not qualify
- **WHEN** a 7-day window contains queue-mode rows and, on day 2, shadow-mode rows or rows from a different epoch
- **THEN** the enforce criteria SHALL report INSUFFICIENT_DATA, and the evaluator SHALL report the start of the latest qualifying sub-window

#### Scenario: A quick restart after a hard kill is not a pass
- **WHEN** a run flushes at 12:00, records a transport overrun at 12:00:20, is killed without a shutdown flush at 12:00:40, and a new run with the same settings starts at 12:01, a gap under 180 s
- **THEN** every enforce criterion over a window containing 12:00–12:01, including E2, E3 and E6, SHALL report INSUFFICIENT_DATA rather than PASS

#### Scenario: A clean recreate stays covered
- **WHEN** a run shuts down with its shutdown flush and a new run with the same epoch and mode starts 40 s later
- **THEN** the interval between them SHALL count as covered

#### Scenario: An unflushed tail is not certified
- **WHEN** a pool checkout times out after the latest `completed_through`, and a report is requested with an explicit end after that watermark
- **THEN** the criteria SHALL report INSUFFICIENT_DATA; with the default end, the evaluation SHALL stop at the watermark and report that end

#### Scenario: Two runs in one minute do not leak into the earlier window
- **WHEN** run A, in queue mode, shuts down cleanly at 12:00:20; run B, with the same epoch and mode, starts at 12:00:40 and records a pool checkout timeout at 12:00:50; and an evaluation is requested with an end at run A's shutdown
- **THEN** the evaluation end SHALL be rounded down to 12:00, and bucket 12:00 and run B's timeout SHALL NOT count toward that window
- **AND** a window ending at 12:01 or later SHALL include the timeout and fail Q3 or E6

#### Scenario: Incidents at the window boundaries
- **WHEN** a pool timeout occurs at 11:59:50, just before a window starting at 12:00, and another at 12:59:50, inside a window ending at 13:00 whose watermark is 13:00
- **THEN** the first SHALL be outside the window and the second SHALL make Q3 FAIL

#### Scenario: No double counting across sources
- **WHEN** 300 executed calls include 12 requests that were auth-pressured
- **THEN** Q2 SHALL use `transport_pressured` over `requests` from the counters only, and those 12 requests SHALL be counted once

#### Scenario: A pool timeout blocks the flip
- **WHEN** a qualifying window holds one `pool_checkout_timeout`, from any consumer
- **THEN** Q3 and E6 SHALL FAIL

