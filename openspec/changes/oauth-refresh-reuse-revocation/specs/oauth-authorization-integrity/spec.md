## ADDED Requirements

### Requirement: A replayed refresh token revokes its grant family

The token endpoint SHALL treat the presentation of a refresh token whose stored row exists but is revoked as evidence that the token leaked, and SHALL revoke every still-live token in that token's grant family — access and refresh alike, including any pair minted by the rotation that revoked the presented token — in the same transaction, before responding.

The row SHALL be identified by the presented token's hash and token type alone, and never narrowed by any client identifier the caller supplied: a replayed token presented under another client's identifier, or under one that matches no client, SHALL still revoke its family. The reuse decision SHALL be taken from the revocation flag on the row read under the grant-family lock, not inferred from a query returning no rows, so that no additional predicate can silently disable the detection. The caller-supplied client identifier SHALL be checked against the row *after* the reuse decision: a mismatch against a **live** refresh token SHALL remain a refusal that revokes nothing, so that knowing a client identifier is never a way to end another party's grant.

The detection SHALL fire on exactly one condition: the token hash names an existing refresh-token row and that row is revoked. A token hash that names no row SHALL revoke nothing, because no family is identified. A refresh token that has **expired but was never rotated or revoked** SHALL NOT be treated as reuse — expiry is a token reaching the end of its life, not a second party holding it — and SHALL leave the family untouched; a token that was rotated away and has *since* expired SHALL still be treated as reuse. A family in which no live token remains SHALL be left as it is, with nothing committed.

The revocation SHALL be performed while the transaction already holds the grant-family lock taken before the row was read, so that no rotation can insert a token into the family between the read that detected the reuse and the write that revokes it.

The response to a detected reuse SHALL be identical in error code, HTTP status, headers and body to the response given for a refresh token that names no row at all, so that a caller cannot learn from the response whether it named a live grant, whether anything was revoked, or that reuse detection exists. Every database operation on the detection path — the revocation, its commit, and any rollback — SHALL be guarded so that a failure in any of them still produces that same response rather than a server error. Response *timing* is outside this requirement: the detection path performs strictly more work and is measurably slower, and that residual is accepted and documented rather than equalized.

The server SHALL record one WARNING-level event identifying the detection (`oauth.refresh_reuse_detected`), the client, the grant and the grant's owner, and SHALL NOT record any token value or token hash. Those identifiers SHALL appear in the record's rendered message, not only in structured metadata, because the deployed log format emits the message alone. That event SHALL be recorded only where live tokens were revoked, so neither the unknown-token refusal nor a repeated replay against an already-dead family produces one. A failure while revoking SHALL be recorded with the failing exception's class name only — never its message, rendered statement, bound parameters, or traceback, any of which can carry the token hash.

#### Scenario: A replayed refresh token kills the family it names

- **WHEN** a refresh token is exchanged successfully and the same original refresh token is then presented again
- **THEN** the second exchange SHALL be rejected with `invalid_grant`
- **AND** no new token SHALL be minted
- **AND** every token in that grant family SHALL be revoked, including the access token the rotation deliberately left live

#### Scenario: The current refresh token of that family is refused afterwards

- **WHEN** the refresh token minted by the rotation is presented after the replay was detected
- **THEN** it SHALL be rejected with `invalid_grant`
- **AND** no live token SHALL remain in the family

#### Scenario: A replay under a different client identifier still revokes the family

- **WHEN** a rotated-away refresh token is presented together with a client identifier that is not the one its row carries
- **THEN** the request SHALL be rejected with `invalid_grant`
- **AND** every live token in that grant family SHALL still be revoked
- **AND** the reuse event SHALL still be recorded

#### Scenario: A live refresh token named with the wrong client identifier is refused intact

- **WHEN** a live refresh token is presented together with a client identifier other than the one its row carries
- **THEN** the request SHALL be rejected with `invalid_grant`
- **AND** no token SHALL be revoked and no new token SHALL be minted
- **AND** no reuse event SHALL be recorded

#### Scenario: An unknown refresh token revokes nothing

- **WHEN** a refresh token that matches no stored row is presented
- **THEN** the request SHALL be rejected with `invalid_grant`
- **AND** no token of any grant SHALL be revoked
- **AND** no reuse event SHALL be recorded

#### Scenario: An already-revoked family stays revoked and answers the same

- **WHEN** a refresh token of a family whose tokens are all already revoked is presented
- **THEN** the request SHALL be rejected with `invalid_grant` carrying the same response body as an unknown refresh token
- **AND** the family's tokens SHALL remain revoked with nothing further committed
- **AND** no reuse event SHALL be recorded

#### Scenario: An expired but never rotated refresh token is not reuse

- **WHEN** a refresh token that is past its expiry but has never been rotated or revoked is presented
- **THEN** the request SHALL be rejected without revoking any token in its family
- **AND** no reuse event SHALL be recorded

#### Scenario: An expired token that was rotated away is still reuse

- **WHEN** a refresh token that was rotated away and has since passed its expiry is presented
- **THEN** every live token in its grant family SHALL be revoked
- **AND** the request SHALL be rejected with `invalid_grant`

#### Scenario: The refusal does not disclose the detection

- **WHEN** a replayed refresh token and an unknown refresh token are each presented
- **THEN** the two responses SHALL carry the same error code, HTTP status, headers and body

#### Scenario: A failure while revoking changes neither the answer nor the secrecy of the token

- **WHEN** the revocation, its commit, or the rollback that follows a failed commit raises
- **THEN** the response SHALL still be `invalid_grant` with the same status and body
- **AND** the recorded failure SHALL name only the exception's class, carrying no statement text, bound parameters, traceback, or token hash

#### Scenario: The reuse event names the grant and no secret

- **WHEN** a replay revokes live tokens
- **THEN** exactly one WARNING event SHALL be recorded whose rendered message names the client, the grant, the grant's owner and how many tokens were revoked
- **AND** it SHALL NOT contain the presented refresh token, any other token value, or any token hash
