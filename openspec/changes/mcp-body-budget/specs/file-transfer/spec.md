## ADDED Requirements

### Requirement: `import_from_url` SHALL refuse a `url` longer than `MAX_IMPORT_URL_CHARS` before the tool body and without parsing it
`import_from_url` SHALL refuse a `url` argument longer than `MAX_IMPORT_URL_CHARS` (8,192 characters) through the shared decorator's declarative argument-length screen. The refusal SHALL be the existing pre-body `argument_too_long` refusal: it carries the `MCP-REFUSAL` line, names the argument, its length and the limit, does not echo the value, writes a `usage_logs` row with the `argument_too_long` marker, and is issued before any DNS resolution, connection, vault access or quota statement.

The `url` logging transform SHALL return the fixed placeholder `<over-long>` for a value longer than `MAX_IMPORT_URL_CHARS`, without converting or parsing that value, so that no code path, including the refusal's usage row, runs URL parsing on an over-long argument.

This cap SHALL NOT be the bound on request-body memory. That bound is the process-wide body budget in `mcp-request-routing`.

#### Scenario: An over-long URL is refused before any fetch
- **WHEN** `import_from_url` is called with a `url` of 8,193 characters beginning `https://example.com/`
- **THEN** the result SHALL end with an `MCP-REFUSAL` line whose `code` is `argument_too_long`
- **AND** no resolver or HTTP client SHALL have been invoked, and no file SHALL be written

#### Scenario: A URL at the limit is processed normally
- **WHEN** `import_from_url` is called with a well-formed `https` URL of exactly 8,192 characters
- **THEN** the call SHALL pass the length screen and proceed to the existing URL policy checks

#### Scenario: The usage row never parses the over-long value
- **WHEN** an over-long `url` is refused
- **THEN** the `usage_logs` row's `url` parameter SHALL be `<over-long>`
- **AND** `urllib.parse.urlsplit` SHALL NOT have been called with that value (asserted by patching it)
