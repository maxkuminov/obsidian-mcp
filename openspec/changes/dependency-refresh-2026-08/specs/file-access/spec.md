## ADDED Requirements

### Requirement: Transport body limit is derived from the write caps

The MCP streamable-HTTP transport SHALL enforce a maximum request body size equal to `max(2 × MAX_FILE_WRITE_BYTES, 6 × MAX_NOTE_BYTES) + 1 MiB`, derived from configuration rather than taken from the MCP SDK default. The limit SHALL apply to every MCP request body on both the canonical `/mcp/` path and the bearer-authenticated root fallback, whether or not the request carries `Content-Length`.

For a canonical `tools/call` envelope whose JSON-RPC framing and non-content arguments encode to at most 1 MiB − 2 bytes, the following supported call shapes SHALL always reach the tool, which alone decides on its own cap: a base64-mode `write_file` whose decoded content is at most `MAX_FILE_WRITE_BYTES` (base64 length is `4·⌈n/3⌉ ≤ 2n + 2`); any note write whose content arguments are at most `MAX_NOTE_BYTES` bytes of UTF-8 before JSON escaping (JSON escaping expands a byte at most 6×). Text-mode `write_file` content whose JSON escaping exceeds the limit, envelopes over 1 MiB, and arguments that are large but discarded are unsupported shapes: they SHALL be bounded by the transport (HTTP 413), and the `write_file` docstring SHALL name base64 mode as the always-safe encoding for large or non-prose content.

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

#### Scenario: Worst-case-escaped note write reaches the tool regardless of the file cap

- **WHEN** `MAX_FILE_WRITE_BYTES` is set far below `MAX_NOTE_BYTES` (for example 64 KiB) and a readwrite caller posts a `create_note` whose content is `MAX_NOTE_BYTES` bytes of control characters (six-character JSON escapes each)
- **THEN** the transport SHALL NOT reject the request
- **AND** the tool SHALL accept the note (it is exactly at `MAX_NOTE_BYTES`)

#### Scenario: Oversized body is bounded on both routes

- **WHEN** a request body larger than the derived limit is posted to `/mcp/` or to the bearer-authenticated root path
- **THEN** the transport SHALL reject it with HTTP 413

#### Scenario: Oversized chunked body is bounded

- **WHEN** a request body larger than the derived limit is delivered as multiple ASGI body chunks without a `Content-Length` header
- **THEN** the transport SHALL reject it with HTTP 413 once the accumulated size exceeds the limit

#### Scenario: Raising the write cap raises the transport limit

- **WHEN** `MAX_FILE_WRITE_BYTES` is raised via the environment above `3 × MAX_NOTE_BYTES`
- **THEN** the derived transport limit SHALL equal `2 × MAX_FILE_WRITE_BYTES + 1 MiB` for the new value without any further configuration
