## ADDED Requirements

### Requirement: An OAuth-minted transfer capability SHALL honour its grant family's absolute deadline
The transfer subsystem SHALL treat the effective expiry of an OAuth credential as the earlier of the token row's `expires_at` and its grant family's absolute deadline (`grant_issued_at` plus `OAUTH_GRANT_ABSOLUTE_LIFETIME_DAYS`, evaluated against the current setting, as defined in `oauth-authorization-integrity`). That effective expiry SHALL be used everywhere the subsystem re-validates an OAuth credential: the mint-window calculation (so no capability is minted with a deadline beyond the grant's), the redemption check on every transfer route, and the locked pre-publication re-validation inside the publish gate. An OAuth credential at or past its family's deadline SHALL be treated exactly as an expired one: no capability is minted against it and every pending capability it minted is refused with the uniform 404, whatever the capability's own `expires_at` and the token's own `expires_at` say. API-key credentials are unchanged.

#### Scenario: The mint window is clamped to the grant deadline
- **WHEN** an OAuth access token whose family's deadline falls before both the token's own `expires_at` and the requested link lifetime mints a capability
- **THEN** the capability's `expires_at` SHALL NOT be later than the family's deadline

#### Scenario: A pending upload capability dies with its grant
- **WHEN** an upload capability is minted by an OAuth access token and the configured lifetime is then shortened so that the family is past its deadline while the access token and the capability are both unexpired
- **THEN** redeeming the capability SHALL be refused with the uniform 404 and nothing SHALL be written to the vault

#### Scenario: A pending download capability dies with its grant
- **WHEN** a download capability is minted by an OAuth access token and the family then passes its deadline while the access token and the capability are both unexpired
- **THEN** redeeming the capability SHALL be refused with the uniform 404

#### Scenario: The deadline is re-checked before publication
- **WHEN** the family passes its deadline after an upload's entry check has passed but before the bytes are published
- **THEN** the locked pre-publication re-validation SHALL refuse and the destination SHALL be left unchanged

#### Scenario: No capability is minted from a credential past its deadline
- **WHEN** a transfer tool runs on an OAuth access token whose family is at or past its deadline
- **THEN** no capability SHALL be minted
