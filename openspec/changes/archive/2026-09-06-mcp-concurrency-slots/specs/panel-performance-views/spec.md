## ADDED Requirements

### Requirement: Actual slot refusals and shadow observations remain distinct
Usage statistics SHALL classify enforced slot_timeout as pre-body and SHALL keep
shadow observations separate from real outcomes and executed-work statistics.

#### Scenario: Enforced refusal is coalesced
- **WHEN** slot_timeout refusals are coalesced
- **THEN** existing weighted refusal counts SHALL read their full represented count
- **AND** their rows SHALL not enter body latency percentiles

#### Scenario: Shadow pressure accompanies a body error
- **WHEN** a call executes under shadow pressure and returns a typed #263 body refusal
- **THEN** the row SHALL preserve that post-body error and remain one executed request
- **AND** the shadow object SHALL not be interpreted as an actual slot refusal
