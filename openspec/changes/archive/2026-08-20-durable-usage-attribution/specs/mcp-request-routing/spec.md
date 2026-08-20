## ADDED Requirements

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
