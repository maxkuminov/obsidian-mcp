## ADDED Requirements

### Requirement: Agent-facing guidance names only registered tools
Every tool name that `read_note`'s responses offer to the caller as a next step SHALL be the name a tool is registered under on the MCP server. A response MUST NOT instruct the caller to call a name no client is offered, and this applies to the truncation notice, the heading outline's omission summary, and any guidance added later. The names logged to `usage_logs` are governed separately and are not affected by this requirement; a historical spelling retained for reading rows written before it was corrected is not agent-facing guidance.

#### Scenario: Truncated whole-note read offers a callable tool

- **WHEN** a whole-note read is truncated and the response suggests narrowing the request by search instead of reading the whole note
- **THEN** the suggested tool name SHALL be `keyword_search`
- **AND** SHALL NOT be `search_notes`

#### Scenario: Truncated outline offers a callable tool

- **WHEN** a heading outline is itself truncated and its omission summary suggests narrowing the request
- **THEN** the suggested tool name SHALL be `keyword_search`
- **AND** SHALL NOT be `search_notes`

#### Scenario: Every offered name is registered

- **WHEN** the tool names offered by agent-facing guidance in the tool module are enumerated and compared with the tools registered on the MCP server
- **THEN** every offered name SHALL appear in the registered set

#### Scenario: A future notice naming an unregistered tool is rejected

- **WHEN** guidance is added that names a tool which is not registered
- **THEN** the enumeration check SHALL fail, rather than the defect reaching a caller
