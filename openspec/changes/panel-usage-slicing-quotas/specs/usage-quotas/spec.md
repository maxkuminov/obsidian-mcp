## ADDED Requirements

### Requirement: Per-key daily quota enforcement
A key with a non-null `daily_request_limit` SHALL be refused further tool calls once its count of quota-consuming logged calls since UTC midnight reaches the limit: the refusal is a structured tool error naming the limit and the UTC reset, is itself logged with an over-quota marker, does not consume quota, and never affects other keys or OAuth traffic. Keys with a null limit SHALL behave exactly as today.

#### Scenario: Limit reached
- **WHEN** a key with limit 100 makes its 101st call of the UTC day
- **THEN** the call is refused before the tool body runs, the refusal is logged with the over-quota marker, and a different key's calls proceed

#### Scenario: Day rollover
- **WHEN** the same key calls after the next UTC midnight
- **THEN** the call executes normally

#### Scenario: Refusals do not consume quota
- **WHEN** an over-limit key is refused 50 times
- **THEN** its consumed count for the day remains at the limit, and at rollover exactly the limit-many new calls are admitted

#### Scenario: Unlimited default
- **WHEN** a key has no limit set
- **THEN** no quota query alters its behavior
