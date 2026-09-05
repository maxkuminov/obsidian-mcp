## ADDED Requirements

### Requirement: A capability SHALL NOT be redeemed while its owner's vault root overlaps another tenant's
The transfer redemption gate SHALL refuse a capability whose owning user is in the published overlap set, on both the upload and the download direction, and SHALL refuse it in the same place and the same manner as it already refuses a capability whose owner has become inactive or whose vault root no longer matches the one pinned at mint.

A capability is a delayed write — or a delayed read — into a root, authorised at mint and redeemed later on the public `/transfer/*` routes, which carry no OAuth chain and never call the vault-root admission gate. A token minted before the overlap appeared pins a `vault_root` that is still, byte for byte, the owner's current assignment, so every existing check agrees and the redemption proceeds into a directory the server has just determined is shared with another tenant. Refusing every MCP tool while leaving an outstanding capability redeemable would leave the cross-tenant write reachable through the one path that was designed to outlive the session that created it.

The gate is where this belongs because it already re-reads the owner row and already fails closed on what it finds there; the check is one more condition at a point whose refusal semantics, error surface and locking are already established.

#### Scenario: An upload capability minted before the overlap is refused

- **WHEN** a capability was minted while its owner's root was unambiguous, the owner's root is subsequently detected as overlapping another active user's, and the capability is presented for upload
- **THEN** the redemption SHALL be refused
- **AND** no byte SHALL be published into the vault

#### Scenario: A download capability is refused on the same condition

- **WHEN** the same owner's outstanding download capability is presented
- **THEN** the redemption SHALL be refused
- **AND** no vault content SHALL be streamed

#### Scenario: Minting is already refused

- **WHEN** a user in the overlap set calls `request_upload`, `request_download` or `import_from_url`
- **THEN** the call SHALL be refused by the tool admission gate, so no new capability is minted

#### Scenario: An unrelated owner's capability is unaffected

- **WHEN** a capability owned by an active user who is not in the overlap set is presented
- **THEN** the redemption SHALL proceed exactly as before

#### Scenario: A corrected overlap restores redemption

- **WHEN** the overlap is corrected, the owner leaves the overlap set, and an unexpired capability whose pinned root still matches the owner's assignment is presented
- **THEN** this requirement SHALL NOT refuse it

#### Scenario: Single-user mode is unaffected

- **WHEN** the server runs in single-user mode, where a capability's owner is the ownerless single-user shape and the root comes from settings
- **THEN** no overlap test SHALL apply and redemption SHALL behave exactly as it does today
