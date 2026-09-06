## ADDED Requirements

### Requirement: Move identity witness lifetime

`move_note` SHALL retain the descriptor witnessing the source inode from its identity capture through the forward rename, destination verification and any verification-triggered rollback, and SHALL close it on every success or failure exit. Failure to capture a witness SHALL preserve the existing unverifiable outcome and SHALL NOT authorize guessed rollback.

#### Scenario: Non-file replacement before capture
- **WHEN** a source becomes a symlink or directory before identity capture
- **THEN** verification SHALL identify that witnessed non-file and attempt the existing non-clobbering rollback

#### Scenario: Replacement after capture
- **WHEN** the source is unlinked and replaced after identity capture
- **THEN** the retained descriptor SHALL prevent inode-number reuse for the witnessed source during verification
- **AND** a different destination inode SHALL remain unverifiable and SHALL NOT be rolled back as the witnessed source

#### Scenario: Descriptor cleanup
- **WHEN** rename or verification succeeds or raises
- **THEN** the witness descriptor SHALL be closed after its last verification or rollback use
