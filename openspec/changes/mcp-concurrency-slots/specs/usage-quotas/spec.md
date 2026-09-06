## ADDED Requirements

### Requirement: Concurrency refusal consumes no daily quota
The tool slot gate SHALL run before the durable quota gate, and every denied or
cancelled admission SHALL leave daily quota counters unchanged.

#### Scenario: A tool has no capacity
- **WHEN** enforcement refuses a tool for slot timeout or waiter overflow
- **THEN** its body SHALL not run and it SHALL consume no daily quota

#### Scenario: Quota refuses after slots were acquired
- **WHEN** a tool obtains slots and the daily quota gate refuses
- **THEN** its slot lease SHALL be released and its existing quota refusal SHALL retain precedence
