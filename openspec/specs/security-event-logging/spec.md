# security-event-logging Specification

## Purpose
TBD - created by archiving change typed-tool-outcomes. Update Purpose after archive.
## Requirements
### Requirement: Body outcome events are bounded and response neutral
The server SHALL emit one bounded generic event per terminal typed body outcome,
using only a closed reason/disposition, tool name and authenticated row identities.

#### Scenario: A body refusal is observed
- **WHEN** a tool returns a typed refusal or partial completion
- **THEN** the generic event SHALL be subject to the existing suppressor
- **AND** it SHALL include no note content, paths, credential material, hashes or exception prose

#### Scenario: Observation fails after publication
- **WHEN** generic outcome logging or usage-row insertion fails after a tool produced its result
- **THEN** the completed result SHALL still be returned without inventing a tool exception
- **AND** cancellation SHALL retain the existing propagation behavior

