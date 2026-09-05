## ADDED Requirements

### Requirement: A capability SHALL NOT be redeemed while its owner is named by the quarantine snapshot
The transfer redemption gate SHALL refuse a capability whose owning user the published quarantine snapshot names, for either reason, and SHALL also refuse while no snapshot has been published in this process. It SHALL refuse in the same place and the same manner as it already refuses a capability whose owner has become inactive or whose vault root no longer matches the one pinned at mint.

A capability is a delayed write — or a delayed read — into a root, authorised at mint and redeemed later on the public `/transfer/*` routes, which carry no OAuth chain and never call the vault-root admission gate. A token minted before the condition appeared pins a `vault_root` that is still, byte for byte, the owner's current assignment, so every existing check agrees and the redemption proceeds into a directory the server has just determined is shared with another tenant, or into one whose status it could not establish. Refusing every MCP tool while leaving an outstanding capability redeemable would leave the cross-tenant write reachable through the one path designed to outlive the session that created it.

The gate is where this belongs because it already re-reads the owner row and already fails closed on what it finds there; the check is one more condition at a point whose refusal semantics, error surface and locking are already established.

#### Scenario: An upload capability minted before the quarantine is refused

- **WHEN** a capability was minted while its owner's root was unambiguous, the owner is subsequently named by the snapshot for an overlap, and the capability is presented for upload
- **THEN** the redemption SHALL be refused
- **AND** no byte SHALL be published into the vault

#### Scenario: A download capability is refused on the same condition

- **WHEN** the same owner's outstanding download capability is presented
- **THEN** the redemption SHALL be refused
- **AND** no vault content SHALL be streamed

#### Scenario: An unexaminable root refuses redemption too

- **WHEN** the owner is named by the snapshot because their root could not be examined
- **THEN** the redemption SHALL be refused, for the same reason it is refused for an overlap: the root's status could not be established

#### Scenario: An unpublished snapshot refuses redemption

- **WHEN** a capability is presented in a process where no snapshot has been published
- **THEN** the redemption SHALL be refused rather than proceeding against roots nothing has checked

#### Scenario: Minting is already refused

- **WHEN** a user the snapshot names calls `request_upload`, `request_download` or `import_from_url`
- **THEN** the call SHALL be refused by the tool admission gate, so no new capability is minted

#### Scenario: An unrelated owner's capability is unaffected

- **WHEN** a capability owned by an active user the snapshot does not name is presented
- **THEN** the redemption SHALL proceed exactly as before

#### Scenario: A corrected condition restores redemption

- **WHEN** the condition is corrected, a later snapshot no longer names the owner, and an unexpired capability whose pinned root still matches the owner's assignment is presented
- **THEN** this requirement SHALL NOT refuse it

#### Scenario: Single-user mode is unaffected

- **WHEN** the server runs in single-user mode, where a capability's owner is the ownerless single-user shape and the root comes from settings
- **THEN** no quarantine test SHALL apply and redemption SHALL behave exactly as it does today
