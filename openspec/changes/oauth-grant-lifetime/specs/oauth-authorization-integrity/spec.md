## ADDED Requirements

### Requirement: A spent authorization code SHALL record the grant family its exchange issued
When the token endpoint exchanges an authorization code it SHALL record, on the code row and in the same transaction that marks the code used and inserts the tokens, the `grant_id` of the family it mints. A code spent before this record existed carries none and is treated as having no lineage.

#### Scenario: The first exchange records its lineage
- **WHEN** an authorization code is exchanged successfully
- **THEN** the code row SHALL be marked used and SHALL carry the `grant_id` shared by the issued access and refresh tokens

#### Scenario: A refused exchange records nothing
- **WHEN** an exchange is refused before minting (for example by a failed PKCE check)
- **THEN** the code row SHALL remain unused and SHALL carry no `grant_id`

### Requirement: A replayed authorization code SHALL revoke the grant family its first exchange issued, and only after full revalidation
The token endpoint SHALL resolve an authorization code by its hash alone — never narrowed by the code's used flag or by any client identifier the caller supplied — and, when the resolved code is already spent, SHALL revoke every still-live token in the grant family recorded on it, provided the request first passes every check a first exchange must pass: the caller's client identifier, where supplied, equals the code's; the client exists and authenticates by its registered method; the redirect URI equals the code's; and the PKCE verifier is well-formed and matches the code's challenge.

A replay that fails any of those checks SHALL be refused and SHALL revoke nothing, because revocation keyed on a code value alone would let anyone who observed a code end another party's grant. The used-flag decision SHALL be taken after those checks and before the code's expiry is checked, so that a replay arriving after the code's ten-minute lifetime still revokes the family. A spent code with no recorded lineage SHALL be refused with nothing revoked.

The revocation SHALL be performed while holding the grant-family lock, so no rotation can insert a token into the family between the decision and the write. The response SHALL be identical in error code, HTTP status, headers and body to the response for a code that names no row, and every database operation on the replay path — the revocation, its commit and any rollback — SHALL be guarded so a failure still produces that response. Response timing is outside this requirement.

The server SHALL emit one WARNING-level event `oauth_code_replay_detected` carrying the client, grant, owner and number of tokens revoked, only when live tokens were revoked and only after the commit; a replay that revokes nothing SHALL produce the ordinary token-refusal record; a failure while revoking SHALL be recorded with the exception's class name only. No record SHALL carry a code, verifier, challenge or any hash.

#### Scenario: A valid replay revokes the issued family
- **WHEN** a code is exchanged successfully and the same code is presented again with the same client, redirect URI and PKCE verifier
- **THEN** the second request SHALL be rejected with `invalid_grant`
- **AND** every token of the family the first exchange issued SHALL be revoked, including its unexpired access token
- **AND** one `oauth_code_replay_detected` event SHALL be recorded

#### Scenario: A replay with a wrong verifier revokes nothing
- **WHEN** a spent code is presented with a PKCE verifier that does not match its challenge
- **THEN** the request SHALL be rejected with `invalid_grant`
- **AND** no token of the issued family SHALL be revoked
- **AND** no replay event SHALL be recorded

#### Scenario: A replay with a wrong redirect URI or client revokes nothing
- **WHEN** a spent code is presented with the correct verifier but a different redirect URI, a different client identifier, or a failing client secret for a confidential client
- **THEN** the request SHALL be refused and no token of the issued family SHALL be revoked

#### Scenario: A replay after the code's expiry still revokes
- **WHEN** a spent code is replayed with valid client, redirect URI and verifier after the code's own expiry has passed
- **THEN** the issued family SHALL be revoked

#### Scenario: A concurrent double exchange leaves no live family
- **WHEN** two exchanges of one code with the same valid client, redirect URI and verifier are submitted concurrently
- **THEN** exactly one SHALL succeed and the other SHALL be rejected with `invalid_grant`
- **AND** after both complete, no token of the family the successful exchange issued SHALL be live

#### Scenario: The replay response does not disclose the detection
- **WHEN** a valid replay is rejected and, separately, a code that names no row is presented
- **THEN** both responses SHALL be identical in HTTP status, headers and body

#### Scenario: A spent code without lineage revokes nothing
- **WHEN** a spent code that carries no recorded `grant_id` is replayed with valid client, redirect URI and verifier
- **THEN** the request SHALL be rejected with `invalid_grant`, nothing SHALL be revoked and nothing committed

#### Scenario: A replay against an already-revoked family records no alarm
- **WHEN** a valid replay names a family with no live token left
- **THEN** the request SHALL be rejected with `invalid_grant` and no `oauth_code_replay_detected` event SHALL be recorded

### Requirement: Authorization codes SHALL be retained, spent or not, until seven days past their expiry
The maintenance pass SHALL delete an authorization code row only when its `expires_at` is more than seven days in the past, and MUST NOT delete a code because it is used. Since a code can only be spent before it expires, every spent code and its lineage SHALL remain available to replay detection for at least seven days after it was spent.

#### Scenario: A spent code survives the next maintenance pass
- **WHEN** the maintenance pass runs shortly after a code was exchanged
- **THEN** the spent code row SHALL NOT be deleted

#### Scenario: A spent code is deleted after the window
- **WHEN** the maintenance pass runs and a spent code's `expires_at` is more than seven days in the past
- **THEN** that code row SHALL be deleted

#### Scenario: Replay detection ends with the window
- **WHEN** a code is replayed after its row has been deleted by the maintenance pass
- **THEN** the request SHALL be refused as an unknown code and nothing SHALL be revoked

### Requirement: Every OAuth grant family SHALL carry an issuance time that rotation inherits and never resets
Every `oauth_tokens` row SHALL carry a non-null grant issuance time. Both tokens minted by an authorization-code exchange MUST carry the time of that exchange, and every token pair produced by a rotation MUST carry, unchanged, the issuance time of the refresh token it rotated, read under the grant-family lock. Every construction of a token row in application code MUST set the issuance time explicitly rather than relying on a database default.

#### Scenario: One exchange, one issuance time
- **WHEN** an authorization code is exchanged
- **THEN** the access and refresh tokens SHALL carry the same issuance time, equal to the exchange time

#### Scenario: Rotation does not restart the clock
- **WHEN** a refresh token is rotated several times
- **THEN** every token minted by those rotations SHALL carry the issuance time of the original exchange

#### Scenario: No mint site relies on the default
- **WHEN** the test suite inspects every token-row construction in `src/`
- **THEN** each SHALL pass the issuance time explicitly

### Requirement: Grant families SHALL expire absolutely at their issuance time plus the configured lifetime
A grant family's absolute deadline SHALL be its issuance time plus `OAUTH_GRANT_ABSOLUTE_LIFETIME_DAYS`, evaluated against the current setting. Every access and refresh token minted for a family MUST expire no later than that deadline, and the `expires_in` returned with an access token MUST state its clamped lifetime. A refresh presented at or after the deadline, or with less than one second remaining, SHALL be refused with `invalid_grant` and a description asking the client to re-authorize, SHALL mint nothing and SHALL revoke nothing. The MCP authentication middleware SHALL refuse an access token whose family is at or past its deadline. Single-use rotation, refresh-token reuse detection and family revocation SHALL be unchanged: the reuse decision is taken before the deadline check, so a rotated-away refresh token presented after the deadline still revokes its family.

#### Scenario: Far from the deadline nothing is clamped
- **WHEN** a refresh is performed when more than 30 days remain before the family's deadline
- **THEN** the new access token SHALL expire one hour after the refresh, the new refresh token 30 days after it, and `expires_in` SHALL be 3600

#### Scenario: Near the deadline both tokens are clamped
- **WHEN** a refresh is performed two days before the family's deadline
- **THEN** the new refresh token SHALL expire exactly at the deadline
- **AND** the new access token SHALL expire one hour after the refresh

#### Scenario: Within the last hour the access token is clamped too
- **WHEN** a refresh is performed 30 minutes before the family's deadline
- **THEN** both new tokens SHALL expire at the deadline and `expires_in` SHALL equal the seconds remaining, rounded down

#### Scenario: At the deadline the client must re-authorize
- **WHEN** a live refresh token is presented at or after its family's deadline
- **THEN** the request SHALL be rejected with `invalid_grant`
- **AND** no token SHALL be minted and no token SHALL be revoked

#### Scenario: A shortened policy takes effect at the next request
- **WHEN** the configured lifetime is lowered so that a family is past its new deadline while it still holds an unexpired access token
- **THEN** that access token SHALL be refused by the MCP middleware with `invalid_token`
- **AND** a refresh of that family SHALL be rejected with `invalid_grant`

#### Scenario: Reuse detection survives the deadline
- **WHEN** a refresh token that was rotated away is presented after its family's deadline
- **THEN** every live token in the family SHALL be revoked and the request SHALL be rejected with `invalid_grant`

#### Scenario: Concurrent refreshes near the deadline
- **WHEN** the same live refresh token is presented by two concurrent requests one day before the deadline
- **THEN** exactly one SHALL rotate, minting tokens that expire no later than the deadline
- **AND** the other SHALL be treated as reuse and leave no live token in the family

### Requirement: The absolute grant lifetime SHALL be configurable within bounds and MUST NOT be disable-able
`OAUTH_GRANT_ABSOLUTE_LIFETIME_DAYS` SHALL default to 90 and SHALL accept whole days from 1 to 365. Startup SHALL refuse zero, a value above 365, and every empty or "off" spelling (empty, `null`, `none`), because an absolute lifetime that one blank setting removes is the defect this requirement closes.

#### Scenario: The default applies
- **WHEN** the setting is not configured
- **THEN** the absolute lifetime SHALL be 90 days

#### Scenario: Out-of-range and off values are refused
- **WHEN** the setting is `0`, `366`, empty, `null` or `none`
- **THEN** settings construction SHALL fail naming the setting

#### Scenario: The bounds are accepted
- **WHEN** the setting is `1` or `365`
- **THEN** settings construction SHALL succeed with that value

### Requirement: The consent screen SHALL disclose the effective credential lifetimes
The `/authorize` consent page SHALL state, on every render, the access-token lifetime, that it is renewed by a refresh token valid for a stated period from its last renewal, the absolute period after approval at which renewal stops and the user must approve again, and that access can be revoked from the control panel. Each stated period SHALL be derived from the configured policy and SHALL NOT exceed it: the access lifetime is the lesser of one hour and the absolute lifetime, the refresh lifetime the lesser of 30 days and the absolute lifetime. The disclosure SHALL comply with the panel content security policy — no inline style attribute and no inline event handler.

#### Scenario: Default policy is disclosed
- **WHEN** the consent page renders under the default setting
- **THEN** it SHALL state a 1 hour access lifetime, a 30 day refresh lifetime and a 90 day absolute lifetime

#### Scenario: A short policy is not overstated
- **WHEN** the absolute lifetime is configured to 7 days
- **THEN** the page SHALL state a 7 day refresh lifetime and a 7 day absolute lifetime, and no period longer than 7 days

#### Scenario: The disclosure is CSP-clean
- **WHEN** the rendered consent page is inspected
- **THEN** it SHALL contain no `style=` attribute and no `on*=` event-handler attribute

### Requirement: Migration 029 SHALL start the absolute clock of every pre-existing grant family at migration time
Migration 029 SHALL set the issuance time of every `oauth_tokens` row that exists when it runs to the migration transaction's timestamp, one value for all rows, so that no pre-existing grant is past its deadline when the migration completes and every pre-existing family reaches its deadline the configured lifetime after the migration.

#### Scenario: Existing connectors keep working after the deploy
- **WHEN** a family that existed before the migration refreshes immediately after it
- **THEN** the refresh SHALL succeed and the new tokens SHALL carry the migration's timestamp as their issuance time

#### Scenario: Old families are not treated as expired
- **WHEN** a family whose first token was created more than 90 days before the migration is inspected after it
- **THEN** its issuance time SHALL be the migration's timestamp, not its original creation time

## MODIFIED Requirements

### Requirement: The OAuth panel SHALL show the status the middleware enforces
The OAuth page SHALL derive each grant's status from revocation, expiry, the grant family's absolute deadline and the owning user's `is_active`, using the same scope-membership helper the authentication middleware uses, and SHALL list revoked and expired tokens rather than omitting them. The page MUST present one revocation control and one scope control per grant, never one per token row.

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

#### Scenario: A grant past its absolute deadline is not shown as active
- **WHEN** a grant family is at or past its absolute deadline while one of its tokens is unrevoked and before its own `expires_at`
- **THEN** that token SHALL be shown as expired and the grant SHALL NOT be badged "Active"

### Requirement: Unused dynamically registered OAuth clients expire
The maintenance job SHALL delete a dynamically registered OAuth client when it has never been used, holds no authorization code and no token of any state, and was registered longer ago than a configured age. `/register` is unauthenticated dynamic client registration, so nothing else bounds the table; today the cleanup job deletes codes and tokens and never touches a client row, and registrations accumulate without limit.

"Never been used" SHALL be read from a durable per-client marker stamped whenever an authorization code or a token is issued for that client, and MUST NOT be inferred from the absence of child rows alone: authorization codes and tokens are deleted seven days after they expire, so a client that was genuinely used, whose rows have aged out, becomes indistinguishable from one that never was. It MUST NOT be inferred from the client having no owner either, because a client authorized in single-user mode is never bound to a user.

The sweep SHALL act only on registrations whose entire history the marker covers. Every client that existed before the marker was introduced is stamped by the migration that introduced it, so an absent marker can only mean a registration made after that point and never used. A client whose evidence of use was purged before the marker existed SHALL therefore never be a candidate — it cannot be distinguished from a hand-configured confidential client, which dynamic registration does not re-provision, because registering again mints a different identifier and secret.

The age SHALL be configurable with a sane default, SHALL refuse a zero-day window, and SHALL be disable-able outright. Each pass SHALL log the number of clients it deleted, because a job that silently removes credentials cannot be audited.

#### Scenario: A never-used client older than the age is deleted
- **WHEN** the maintenance pass runs and finds a client with no recorded use, no code row, no token row, and a registration older than the configured age
- **THEN** that client SHALL be deleted

#### Scenario: A never-used client younger than the age is kept
- **WHEN** the same client is younger than the configured age
- **THEN** it SHALL NOT be deleted

#### Scenario: A client that has ever been used is kept
- **WHEN** a client carries a recorded use, however old, and holds no live token
- **THEN** it SHALL NOT be deleted, whatever its age

#### Scenario: A client holding a live token is kept
- **WHEN** a client has an unexpired, unrevoked access or refresh token
- **THEN** it SHALL NOT be deleted

#### Scenario: A client holding a revoked or expired token is kept while that row survives
- **WHEN** a client's only token rows are revoked or expired but have not yet been purged
- **THEN** the client SHALL NOT be deleted, so a deletion can never cascade away a token row the operator can still see

#### Scenario: A client with a pending authorization code is kept
- **WHEN** a client has an unused, unexpired authorization code
- **THEN** it SHALL NOT be deleted

#### Scenario: A registration predating the marker is never swept
- **WHEN** the sweep runs against a client that was registered before the use marker existed and holds no surviving code or token row
- **THEN** it SHALL NOT be deleted, whatever its age, because the migration stamped its marker rather than leaving it absent

#### Scenario: A client bound to a user is kept
- **WHEN** a client has been claimed by an authorizing user
- **THEN** it SHALL NOT be deleted, because a claimed client has by definition completed an authorization

#### Scenario: Expiry can be switched off
- **WHEN** the configured age is set to the disabled value
- **THEN** no client SHALL be deleted by this pass

#### Scenario: A zero-day window is refused
- **WHEN** the configured age is set to zero days
- **THEN** startup SHALL refuse the configuration rather than delete registrations as they are made

#### Scenario: The deletion is counted
- **WHEN** a pass deletes one or more clients
- **THEN** it SHALL log the number deleted

#### Scenario: Usage attribution survives the expiry
- **WHEN** a client is deleted by this pass and `usage_logs` rows reference tokens it owned
- **THEN** those rows SHALL keep their recorded actor kind, label and reference, exactly as they do when an operator deletes a client from the panel
