## ADDED Requirements

### Requirement: Tool calls with undeclared arguments are refused
Every MCP tool call whose arguments include a name the tool does not declare SHALL fail with a tool error that names each undeclared argument, and no part of the tool body SHALL run. Every tool's published input schema MUST set `additionalProperties` to `false`. The rule MUST be applied to all registered tools in one place rather than per tool, and MUST be disableable only by setting `MCP_REJECT_UNKNOWN_ARGUMENTS=false`, which restores the SDK's ignore behaviour and unmodified schemas.

#### Scenario: Misspelled filter is refused
- **WHEN** a caller invokes `keyword_search` with `{"query": "x", "folders": "Projects/"}`
- **THEN** the call SHALL return a tool error naming `folders`
- **AND** no search SHALL be executed

#### Scenario: Argument the tool lacks is refused
- **WHEN** a caller invokes `semantic_search` with `{"query": "x", "user_id": 2}`
- **THEN** the call SHALL return a tool error naming `user_id`

#### Scenario: Declared arguments still validate
- **WHEN** a caller invokes any tool with only arguments it declares, including optional ones passed as `null`
- **THEN** argument validation SHALL succeed exactly as before this change

#### Scenario: Published schemas forbid extras
- **WHEN** a client lists tools
- **THEN** every tool's `inputSchema` SHALL contain `"additionalProperties": false`

#### Scenario: Rollback flag
- **WHEN** the server starts with `MCP_REJECT_UNKNOWN_ARGUMENTS=false`
- **THEN** undeclared arguments SHALL be ignored and the published schemas SHALL NOT contain `additionalProperties`
