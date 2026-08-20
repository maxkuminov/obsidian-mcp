# oauth-authorization-integrity Specification

## Purpose
TBD - created by archiving change harden-cross-layer-integrity. Update Purpose after archive.
## Requirements
### Requirement: OAuth consent revalidates the browser session
Both OAuth authorization display and approval SHALL resolve the session identity from the database and require an active user whose `session_version` matches the signed session. Missing, deleted, inactive, or version-mismatched identities MUST NOT mint an authorization code and SHALL have their session cleared.

#### Scenario: Password reset invalidates consent session
- **WHEN** a user's password reset increments `session_version` after the browser cookie was issued
- **THEN** an OAuth authorization GET or approval POST using that cookie SHALL require authentication again
- **AND** no authorization code SHALL be minted

#### Scenario: User is deactivated before approval
- **WHEN** an authenticated user is deactivated before submitting OAuth approval
- **THEN** approval SHALL be rejected as unauthenticated
- **AND** no authorization code SHALL be minted

### Requirement: Consent preselects the least privilege
The OAuth consent screen SHALL render the read-only access option as the preselected (`checked`) option on every request, and SHALL NOT render any write-capable option preselected. The preselection MUST NOT depend on the `scope` query parameter, on the client's registered scope, or on any other client-supplied input. Granting write access MUST therefore require an affirmative selection by the user in addition to approving the request. The screen MUST also prevent the browser from restoring a previously selected access level in place of the markup default, so that the preselection holds on a repeat visit to the same authorization URL.

#### Scenario: Write-capable client requests readwrite
- **WHEN** a client registered for `readwrite` starts an authorization request with `scope=readwrite`
- **THEN** the consent screen renders the read-only option preselected
- **AND** the read + write option is rendered but not preselected
- **AND** approving without changing the selection submits the read-only scope

#### Scenario: Read request
- **WHEN** an authorization request asks for `scope=read`
- **THEN** the consent screen renders the read-only option preselected

#### Scenario: Scope parameter omitted
- **WHEN** an authorization request omits the `scope` query parameter entirely
- **THEN** the request is treated as a read request
- **AND** the consent screen renders the read-only option preselected

#### Scenario: Browser state restore cannot revive an earlier write selection
- **WHEN** the consent screen is rendered
- **THEN** the form and every access-level control opt out of browser autofill/state restoration
- **AND** revisiting the same authorization URL after previously selecting read + write renders the read-only option preselected

#### Scenario: No option other than read-only is preselected
- **WHEN** the consent screen is rendered for any valid authorization request
- **THEN** exactly one access-level option carries `checked`
- **AND** that option is the read-only option

### Requirement: Consent discloses the requested access level
The OAuth consent screen SHALL name the access level the client requested, so that a user whose grant will be narrower than the request sees the difference rather than receiving a silent downgrade. When the client requested write access it is not registered to hold, the screen SHALL state that write access is not available to that client. When the client is registered for write access, the screen SHALL state that read-only is preselected and that write is granted only if the user selects it.

#### Scenario: Readwrite request from a write-capable client is named
- **WHEN** a client registered for `readwrite` requests `scope=readwrite`
- **THEN** the consent screen states that the client is requesting read + write access
- **AND** it states that read only is preselected and read + write is granted only if selected

#### Scenario: The preselect explanation is only shown where there is a choice
- **WHEN** the client is not registered for `readwrite`, so no read + write option is offered
- **THEN** the consent screen does not state that read + write is granted only if selected

#### Scenario: Read request is named
- **WHEN** a client requests `scope=read`
- **THEN** the consent screen states that the client is requesting read only access

#### Scenario: Read-only client requesting write is told write is unavailable
- **WHEN** a client registered only for `read` requests `scope=readwrite`
- **THEN** the consent screen states that the client is requesting read + write access
- **AND** it states that read + write is not available to that client
- **AND** no read + write option is offered

#### Scenario: Write-capable client is not told write is unavailable
- **WHEN** a client registered for `readwrite` requests `scope=readwrite`
- **THEN** the consent screen does not state that write access is unavailable

### Requirement: Consent renders client-supplied text as text
The OAuth consent screen SHALL render every client-supplied value it displays — the client name in particular — as escaped text, never as markup. Client registration is unauthenticated, so a client-supplied string that reached the page as markup could contribute a form control, including a preselected one, that an unchanged approval would submit. A `scope` query parameter that is not a known scope token SHALL be rejected with `invalid_scope` before any consent screen is rendered.

#### Scenario: Hostile client name is escaped
- **WHEN** a registered client's name contains markup, quotes, or a complete `<input ... checked>` element
- **THEN** the consent screen renders that name as text
- **AND** the page still carries exactly one checked control, the read-only option
- **AND** no additional access-level control appears

#### Scenario: Malformed scope is rejected before rendering
- **WHEN** an authorization request carries a `scope` value containing a token outside the known scopes
- **THEN** the server responds `invalid_scope` with HTTP 400
- **AND** no consent screen, form, or access-level control is produced

### Requirement: Consent marks the selected access level visibly
The OAuth consent screen SHALL visually distinguish the currently selected access level beyond the native radio indicator. The styling that provides this MUST degrade gracefully where the browser does not support it, leaving the native radio indicator as the fallback signal, and MUST NOT be grouped with styling the screen depends on for legibility.

#### Scenario: Selected option is highlighted
- **WHEN** an access-level option is selected on the consent screen
- **THEN** that option's container is styled distinctly from the unselected options

#### Scenario: Unsupported browser keeps a working screen
- **WHEN** the browser cannot parse the selected-option selector
- **THEN** only the highlight rules are dropped
- **AND** every other consent-screen rule, including the native radio indicator styling, still applies

### Requirement: Every OAuth token belongs to exactly one grant family
Every `oauth_tokens` row SHALL carry a non-null `grant_id` identifying the consent event it descends from. Both tokens minted from a single `/authorize` approval MUST share one `grant_id`, every token pair produced by a later rotation MUST inherit the `grant_id` of the refresh token it rotated, and a `grant_id` MUST belong to exactly one `(client_id, user_id)` pair.

#### Scenario: One consent produces one family
- **WHEN** an authorization code is exchanged at the token endpoint
- **THEN** the issued access token and refresh token SHALL carry the same, freshly generated `grant_id`

#### Scenario: Rotation stays inside its family
- **WHEN** a refresh token is exchanged for a new token pair
- **THEN** both new tokens SHALL carry the `grant_id` of the refresh token that was presented
- **AND** no new `grant_id` SHALL be generated

#### Scenario: Pre-existing tokens are backfilled without merging users
- **WHEN** migration 014 runs against a database whose tokens predate `grant_id`
- **THEN** every row SHALL receive a `grant_id`, one per distinct `(client_id, user_id)` group, with NULL `user_id` treated as a single group
- **AND** no `grant_id` SHALL be shared by rows belonging to two different users
- **AND** all rows sharing a `(client_id, user_id)` pair SHALL share one `grant_id`

#### Scenario: Re-running the migration preserves existing families
- **WHEN** migration 014 executes again against a database whose rows already carry a `grant_id`
- **THEN** no existing `grant_id` value SHALL be changed

### Requirement: Revoking an OAuth grant revokes every live token in its family
Panel revocation and the RFC 7009 revocation endpoint SHALL mark every non-revoked token sharing the target's `grant_id` as revoked, in a single transaction, including access tokens that have not yet expired. Revocation MUST NOT be satisfiable by a subsequent refresh.

#### Scenario: Revoking through the access token kills the refresh token
- **WHEN** an operator revokes a grant from the control panel using its access token row
- **THEN** the paired refresh token SHALL also be marked revoked
- **AND** a later refresh presenting that refresh token SHALL be rejected with `invalid_grant`

#### Scenario: Revocation covers tokens minted by an earlier rotation
- **WHEN** a grant has rotated one or more times and older non-revoked tokens are still within their lifetime
- **THEN** revoking the grant SHALL mark every one of those tokens revoked

#### Scenario: The RFC 7009 endpoint is grant-scoped
- **WHEN** a client presents any token of a grant to `POST /revoke`
- **THEN** every non-revoked token in that grant SHALL be revoked
- **AND** the endpoint SHALL still respond HTTP 200

#### Scenario: A different grant is untouched
- **WHEN** a client has two grants and one of them is revoked
- **THEN** no token belonging to the other grant SHALL be modified

#### Scenario: Revocation is not lost to a concurrent rotation
- **WHEN** a revocation and a refresh of the same grant execute concurrently
- **THEN** both SHALL serialize on a transaction-scoped lock keyed by that `grant_id`, taken before either reads or writes any token row of the family
- **AND** the outcome SHALL be either a rejected refresh or a revocation that also covers the newly minted pair

### Requirement: A scope change applies to every live token in the grant family
A scope change made from the control panel SHALL be written to every non-revoked token sharing the target's `grant_id` in one transaction, and MUST NOT be reverted by a subsequent rotation. Revoked tokens MUST keep the scope they carried when they were revoked.

#### Scenario: Downgrading the access row also downgrades the refresh row
- **WHEN** an operator changes a grant's scope to `read` using its access token row
- **THEN** the paired refresh token's scope SHALL also become `read`

#### Scenario: A downgrade survives the next refresh
- **WHEN** the client refreshes after the grant was downgraded to `read`
- **THEN** the newly minted tokens SHALL NOT carry `readwrite`
- **AND** the token response's `scope` SHALL NOT contain `readwrite`

#### Scenario: Revoked rows keep their historical scope
- **WHEN** a grant containing a revoked token has its scope changed
- **THEN** the revoked token's scope SHALL be left unchanged

### Requirement: No path may grant a scope above the client's registration
`OAuthClient.scope` SHALL cap every grant that client can hold, on every path that writes a token scope: consent approval, authorization-code exchange, panel scope changes, and refresh-token rotation. A request for a scope the client is not registered for MUST NOT result in that scope being written.

#### Scenario: The token endpoint clamps the code's scope
- **WHEN** an authorization code carrying `readwrite` is exchanged and its client is not registered for `readwrite`
- **THEN** the issued tokens SHALL NOT carry `readwrite`
- **AND** the token response's `scope` SHALL NOT contain `readwrite`

#### Scenario: The panel refuses to raise a read-only client
- **WHEN** an operator submits `readwrite` for a grant whose client is registered `read`
- **THEN** no token scope SHALL be modified
- **AND** the request SHALL not be committed

#### Scenario: The panel does not offer an unavailable option
- **WHEN** the OAuth page renders a client registered without `readwrite`
- **THEN** the scope control SHALL NOT contain a `readwrite` option

#### Scenario: Rotation re-clamps against the current registration
- **WHEN** a token carrying `readwrite` is rotated and its client's registered scope no longer includes `readwrite`
- **THEN** the newly minted tokens SHALL NOT carry `readwrite`

#### Scenario: A legitimate write grant is preserved
- **WHEN** a `readwrite` token is rotated and its client is registered for `readwrite`
- **THEN** the newly minted tokens SHALL still carry `readwrite`

### Requirement: An OAuth client bound to one user MUST NOT mint grants for another
In multi-user mode, `authorize_post` SHALL reject an approval when the client is already bound to a different user than the authenticated session identity, and MUST NOT mint an authorization code. The token endpoint SHALL re-check the same condition before exchanging a code. Single-user mode SHALL be unaffected.

#### Scenario: A code for a client another user has since claimed is not exchangeable
- **WHEN** an authorization code stamped with one user is presented and its client is now bound to a different user
- **THEN** the exchange SHALL be rejected with `invalid_grant`
- **AND** no token SHALL be minted
- **AND** the code SHALL NOT be marked used

#### Scenario: A second user cannot authorize someone else's client
- **WHEN** an authenticated user approves consent for a client whose `user_id` is a different user
- **THEN** the request SHALL be rejected with HTTP 403 and `error: access_denied`
- **AND** no authorization code SHALL be minted
- **AND** the client's existing `user_id` SHALL be left unchanged

#### Scenario: The first authorizer still claims an unbound client
- **WHEN** an authenticated user approves consent for a client whose `user_id` is null
- **THEN** the client SHALL be bound to that user and the authorization code SHALL be minted

#### Scenario: Single-user mode is unaffected
- **WHEN** the server runs in single-user mode and a consent approval is submitted
- **THEN** the approval SHALL proceed regardless of any `user_id` stored on the client

### Requirement: The OAuth panel SHALL show the status the middleware enforces
The OAuth page SHALL derive each grant's status from revocation, expiry and the owning user's `is_active`, using the same scope-membership helper the authentication middleware uses, and SHALL list revoked and expired tokens rather than omitting them. The page MUST present one revocation control and one scope control per grant, never one per token row.

#### Scenario: A deactivated owner's grant is not shown as active
- **WHEN** a token's owning user has `is_active = false`
- **THEN** the grant SHALL be badged "Owner inactive" and SHALL NOT be badged "Active"

#### Scenario: Revoked tokens remain visible
- **WHEN** a grant has been revoked
- **THEN** its token rows SHALL still be listed with a "Revoked" status
- **AND** no revocation or scope control SHALL be offered for that grant

#### Scenario: Periodic cleanup does not erase a revocation
- **WHEN** the periodic token cleanup runs after a grant has been revoked
- **THEN** a revoked token SHALL only be deleted once its `expires_at` is more than the retention window in the past
- **AND** a token revoked while still within its lifetime SHALL therefore remain listed for at least that window after it was revoked

#### Scenario: One grant, one set of controls
- **WHEN** a grant's access and refresh tokens are both live
- **THEN** the page SHALL render exactly one revocation control and exactly one scope control for that grant

#### Scenario: The displayed permission is membership-based
- **WHEN** a token's scope is `offline_access readwrite`
- **THEN** the scope control SHALL show `readwrite` as the selected value
- **AND** that value SHALL be derived by the route from the token's scope, not supplied to the template independently

### Requirement: Deleting an OAuth client preserves its usage attribution
Deleting an OAuth client from the control panel SHALL leave every `usage_logs` row that client produced attributed to it by name. The delete MUST remain a delete — the cascades that remove the client's tokens, authorization codes and any transfer capabilities minted under those tokens are the point of it and MUST be preserved — and MUST NOT be replaced by marking tokens revoked, which a surviving client row can defeat by refresh.

#### Scenario: History stays readable after the client is deleted
- **WHEN** an operator deletes an OAuth client and then opens the usage view
- **THEN** every row that client produced SHALL still name the client
- **AND** no such row SHALL render as an unknown actor

#### Scenario: The stop is still a real stop
- **WHEN** an OAuth client is deleted
- **THEN** its access and refresh tokens SHALL be removed
- **AND** any outstanding transfer capabilities minted under those tokens SHALL be removed
- **AND** the client SHALL NOT be able to obtain new tokens

### Requirement: The delete confirmation states what the delete actually does
The control panel's OAuth client Delete control SHALL describe the operation it performs — that the client's tokens are deleted, that outstanding transfer links minted under them stop working, and that usage history keeps its attribution — and SHALL NOT describe it as revoking the client's tokens.

#### Scenario: The confirmation no longer promises a revocation
- **WHEN** the OAuth clients page is rendered
- **THEN** the Delete control's confirmation SHALL NOT describe the action as revoking the client's tokens

#### Scenario: The confirmation names the consequences
- **WHEN** the OAuth clients page is rendered
- **THEN** the Delete control's confirmation SHALL state that the tokens are deleted, that transfer links minted under them stop working, and that usage history remains attributed to the client

