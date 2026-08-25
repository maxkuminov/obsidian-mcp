# file-transfer — delta for nested-mount-honest-refusals

## ADDED Requirements

### Requirement: The fallback's staging discard distinguishes a published name from a disappeared one

The named-staging fallback's discard SHALL be told whether the publication landed, and SHALL treat an absent staging name after a successful publish as the ordinary consumed case — silent, because the overwrite publish is a rename that consumes the name — reserving the "staging name disappeared before its write was published" warning for a name that vanished while the write had genuinely not published. Every discard call site on the transfer path SHALL pass the publication's actual outcome, including the outer cleanup reached when a failure *after* publication (a post-publication directory flush failing, correctly classified as a post-publish failure with the claim stranded) unwinds the stream: hardcoding "not published" there makes the warning false in exactly the doubly-degraded corner where an operator most needs to trust it, and a false disappearance warning trains an operator to ignore the true one, which is the substitution signal. The inode-guarded unlink direction is unchanged: a present name still referring to the staged inode is removed quietly, a substituted or unidentifiable name is left in place and logged, published or not.

#### Scenario: A fallback upload publishes and a post-publication flush fails

- **WHEN** a named-fallback upload's overwrite publish lands and a subsequent post-publication directory flush raises
- **THEN** the failure SHALL remain classified post-publish and the claim SHALL strand exactly as specified elsewhere
- **AND** the cleanup's discard SHALL log no warning about the staging name having disappeared

#### Scenario: A pre-publication disappearance still warns

- **WHEN** a named-fallback upload fails before publication and its staging name is found absent at discard
- **THEN** the discard SHALL log the disappearance warning exactly as it does today

#### Scenario: A no-clobber fallback publish still cleans its leftover source

- **WHEN** a named-fallback no-clobber publish links the staged file into the destination and the staging name therefore survives
- **THEN** the discard SHALL remove the staging name quietly while it still refers to the staged inode
- **AND** a name referring to a different inode SHALL be left in place and logged, exactly as the substitution guard specifies
