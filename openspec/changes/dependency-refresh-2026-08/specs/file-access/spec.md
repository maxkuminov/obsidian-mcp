## ADDED Requirements

### Requirement: Transport body limit never undercuts the write cap

The MCP streamable-HTTP transport SHALL accept request bodies at least as large as a `write_file` call whose decoded content equals `MAX_FILE_WRITE_BYTES`. The transport's maximum request body size SHALL be derived from `MAX_FILE_WRITE_BYTES` (accounting for base64 inflation of the content plus envelope headroom) rather than taken from the MCP SDK's default, so that the write cap is the only size limit a `write_file` caller can encounter and every rejection for size is reported as a tool-level error rather than a bare HTTP status.

#### Scenario: Maximum-size write passes the transport

- **WHEN** a JSON-RPC `tools/call` for `write_file` is posted to the MCP endpoint with base64 content that decodes to exactly `MAX_FILE_WRITE_BYTES` bytes
- **THEN** the transport SHALL NOT reject the request with HTTP 413
- **AND** the request SHALL reach the tool, which decides on the write cap alone

#### Scenario: Oversized body is still bounded

- **WHEN** a request body larger than the derived transport limit is posted to the MCP endpoint
- **THEN** the transport SHALL reject it with HTTP 413

#### Scenario: Raising the write cap raises the transport limit

- **WHEN** `MAX_FILE_WRITE_BYTES` is raised via the environment
- **THEN** the transport limit SHALL rise with it without any further configuration
