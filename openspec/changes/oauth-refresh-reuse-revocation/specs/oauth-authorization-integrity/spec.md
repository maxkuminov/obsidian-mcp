## ADDED Requirements

### Requirement: A replayed refresh token revokes its grant family

The token endpoint SHALL treat the presentation of a refresh token whose stored row exists but is revoked as evidence that the token leaked, and SHALL revoke every still-live token in that token's grant family — access and refresh alike, including any pair minted by the rotation that revoked the presented token — in the same transaction, before responding.

The detection SHALL fire on exactly that condition: the token hash names an existing refresh-token row, and that row is revoked. A token hash that names no row SHALL revoke nothing, because no family is identified. A refresh token that has **expired but was never rotated or revoked** SHALL NOT be treated as reuse — expiry is a token reaching the end of its life, not a second party holding it — and SHALL leave the family untouched. A family in which no live token remains SHALL be left as it is, with nothing committed.

The revocation SHALL be performed while the transaction already holds the grant-family lock taken before the family was read, so that no rotation can insert a token into the family between the read that detected the reuse and the write that revokes it.

The response to a detected reuse SHALL be identical to the response given for a refresh token that names no row at all — the same error code, the same HTTP status, and the same response body — so that a caller cannot learn whether it named a live grant, whether anything was revoked, or that reuse detection exists. A failure to complete the revocation SHALL NOT change that response either.

The server SHALL record one WARNING-level event identifying the detection (`oauth.refresh_reuse_detected`), the client, the grant and the grant's owner, and SHALL NOT record any token value or token hash. That event SHALL be recorded only where live tokens were revoked, so neither the unknown-token refusal nor a repeated replay against an already-dead family produces one.

#### Scenario: A replayed refresh token kills the family it names

- **WHEN** a refresh token is exchanged successfully and the same original refresh token is then presented again
- **THEN** the second exchange SHALL be rejected with `invalid_grant`
- **AND** no new token SHALL be minted
- **AND** every token in that grant family SHALL be revoked, including the access token the rotation deliberately left live

#### Scenario: The current refresh token of that family is refused afterwards

- **WHEN** the refresh token minted by the rotation is presented after the replay was detected
- **THEN** it SHALL be rejected with `invalid_grant`
- **AND** no live token SHALL remain in the family

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

#### Scenario: The refusal does not disclose the detection

- **WHEN** a replayed refresh token and an unknown refresh token are each presented
- **THEN** the two responses SHALL carry the same error code, HTTP status and body

#### Scenario: The reuse event names the grant and no secret

- **WHEN** a replay revokes live tokens
- **THEN** exactly one WARNING event SHALL be recorded naming the client, the grant and the grant's owner
- **AND** it SHALL NOT contain the presented refresh token, any other token value, or any token hash
