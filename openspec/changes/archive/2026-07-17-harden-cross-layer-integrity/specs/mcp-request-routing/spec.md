## ADDED Requirements

### Requirement: Root MCP fallback preserves application middleware
An MCP request accepted through the bearer-authenticated root-path fallback SHALL traverse the same trust, proxy, CORS, session, security-header, and compression middleware boundary as the canonical MCP path. Routing MUST NOT recursively invoke the root fallback.

#### Scenario: Forged host on root fallback
- **WHEN** a bearer-authenticated MCP request targets `/` with a Host value rejected by TrustedHost middleware
- **THEN** the root fallback request SHALL be rejected under the same policy as `/mcp/`

#### Scenario: Root fallback CORS preflight
- **WHEN** a root fallback request requires CORS handling
- **THEN** the response SHALL contain the same CORS policy applied to canonical application routes

