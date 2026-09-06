## ADDED Requirements

### Requirement: Body outcomes retain executed-work attribution
The usage record SHALL retain body refusals and partial completions as executed
work in latency and request accounting, distinct from pre-body refusals.

#### Scenario: Refused body performed work
- **WHEN** a body completes with a closed refusal marker
- **THEN** the record SHALL remain eligible for existing latency statistics
- **AND** the new body marker SHALL not enter the pre-body refusal predicate

#### Scenario: Shadow concurrency and real failure coexist
- **WHEN** an executed call has both a concurrency shadow observation and a real body refusal
- **THEN** its real error marker and disposition SHALL be retained
- **AND** shadow data SHALL not turn it into a pre-body refusal or a second actual request
