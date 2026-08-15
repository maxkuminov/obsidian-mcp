## ADDED Requirements

### Requirement: Transport body limit is derived from the write cap

The MCP streamable-HTTP transport SHALL enforce a maximum request body size equal to `2 × MAX_FILE_WRITE_BYTES + 1 MiB`, derived from configuration rather than taken from the MCP SDK default. Because a base64 encoding of n bytes is at most 2n bytes, a base64-mode `write_file` whose decoded content equals `MAX_FILE_WRITE_BYTES` SHALL always reach the tool, and the tool alone SHALL decide on the write cap. The limit SHALL apply to every MCP request body on both the canonical `/mcp/` path and the bearer-authenticated root fallback, whether or not the request carries `Content-Length`. Text-mode payloads whose JSON encoding exceeds the limit (control-character- or astral-character-dominated content) are an accepted limitation and SHALL be documented in the `write_file` docstring with base64 mode named as the always-safe alternative.

#### Scenario: Maximum-size base64 write succeeds end to end

- **WHEN** a readwrite caller posts a JSON-RPC `tools/call` for `write_file` with base64 content that decodes to exactly `MAX_FILE_WRITE_BYTES` bytes
- **THEN** the transport SHALL NOT reject the request
- **AND** the tool SHALL write the file and return a success result
- **AND** the bytes on disk SHALL equal the decoded content

#### Scenario: One byte over the write cap is a tool-level error

- **WHEN** a readwrite caller posts a `write_file` whose decoded content is `MAX_FILE_WRITE_BYTES + 1` bytes
- **THEN** the transport SHALL NOT reject the request
- **AND** the tool SHALL return an error naming the write cap
- **AND** no file SHALL be written

#### Scenario: Oversized body is bounded on both routes

- **WHEN** a request body larger than `2 × MAX_FILE_WRITE_BYTES + 1 MiB` is posted to `/mcp/` or to the bearer-authenticated root path
- **THEN** the transport SHALL reject it with HTTP 413

#### Scenario: Oversized chunked body is bounded

- **WHEN** a request body larger than the derived limit is delivered as multiple ASGI body chunks without a `Content-Length` header
- **THEN** the transport SHALL reject it with HTTP 413 once the accumulated size exceeds the limit

#### Scenario: Raising the write cap raises the transport limit

- **WHEN** `MAX_FILE_WRITE_BYTES` is raised via the environment
- **THEN** the derived transport limit SHALL equal `2 × MAX_FILE_WRITE_BYTES + 1 MiB` for the new value without any further configuration
