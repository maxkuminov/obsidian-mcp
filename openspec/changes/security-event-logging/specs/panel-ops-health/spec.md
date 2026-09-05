## ADDED Requirements

### Requirement: The error buffer MUST survive logging reconfiguration

Reconfiguring the application's logging SHALL NOT remove, close or detach the error ring buffer's handler from any logger it was attached to, in either call order, so that the health page keeps reporting errors after the root logger has been taken back from the MCP SDK. Attaching the buffer SHALL remain idempotent across a reconfiguration, and the observation window the page states SHALL NOT be reset by one.

#### Scenario: Reconfiguration after attachment

- **WHEN** the buffer is attached and the logging configuration is then applied
- **THEN** an ERROR logged afterwards SHALL appear in the buffer and the page's observation window SHALL be unchanged

#### Scenario: Attachment after reconfiguration

- **WHEN** the logging configuration is applied first and the buffer is attached afterwards, as the running server does
- **THEN** an ERROR logged afterwards SHALL appear in the buffer exactly once, including one logged on `uvicorn.error`

#### Scenario: Tool failures reach the page

- **WHEN** a tracked tool body raises
- **THEN** the resulting ERROR record SHALL appear in the buffer and therefore on the health page
