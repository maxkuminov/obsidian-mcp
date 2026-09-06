## ADDED Requirements

### Requirement: Upload redemption is bounded by the minting principal's write rate

`PUT /transfer/upload` SHALL consume one token of the write rate bucket belonging to the principal that **minted** the capability, before any request body is read and before any bytes are staged, so that vault writes performed by redeeming a capability are bounded by the same rate as vault writes performed through the write tools. The principal SHALL be derived from the transfer token row and the minting credential that identity resolution already loads — `("api_key", key_id)` where the row names a key, and `("oauth", grant_id)` from the minting token row — requiring no additional query and no schema change. A redemption refused by the bucket SHALL return **HTTP 429** with a `Retry-After` header and SHALL **release the claim** rather than consuming it, mirroring the treatment a queue-timeout 503 already receives, so a refused redemption can be retried against the same capability once the bucket refills. The refusal SHALL NOT stage, publish or delete any bytes.

Minting SHALL NOT consume the write bucket: `request_upload`, `request_download` and `check_upload` create or read capability rows only, and charging both mint and redemption would bill one write twice.

#### Scenario: Redemptions above the write rate are refused

- **WHEN** the principal that minted a set of upload capabilities redeems them faster than the write rate allows
- **THEN** the excess redemptions SHALL receive HTTP 429 with `Retry-After`, no bytes SHALL be staged or published for them, and the writes SHALL NOT have escaped the write rate by using the transfer route

#### Scenario: A refused redemption releases its claim

- **WHEN** a redemption is refused by the write bucket
- **THEN** the claim SHALL be released rather than consumed, and the same capability SHALL be redeemable once the bucket has refilled and while the token is still valid

#### Scenario: The bucket belongs to the minter, not the presenter

- **WHEN** a capability minted by one principal is redeemed
- **THEN** the tokens SHALL be drawn from the minting principal's write bucket, so a capability cannot be used to spend another principal's allowance

#### Scenario: Minting alone consumes no write tokens

- **WHEN** a principal calls `request_upload` or `request_download` without redeeming
- **THEN** no write-bucket token SHALL be consumed
