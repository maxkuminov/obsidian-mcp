## ADDED Requirements

### Requirement: read_file response size cap

Text results from `read_file` SHALL be bounded by `MAX_READ_RESPONSE_CHARS`, independently of the `MAX_FILE_READ_BYTES` on-disk cap. `MAX_FILE_READ_BYTES` governs how much the server reads into memory; `MAX_READ_RESPONSE_CHARS` governs how much is returned to the caller, whose context the result consumes.

The tool SHALL accept `offset` (integer, optional, default 0) and `limit` (integer, optional) to window a text result. When a text result is truncated, the response SHALL state the character range shown, the total size, and the `offset` that continues the read. A `limit` above the configured cap SHALL NOT raise it.

Base64 results and inline image results SHALL NOT be windowed, since a partial encoding or a partial image is not usable.

#### Scenario: Text file within the response cap

- **WHEN** `read_file` returns a text result at or below `MAX_READ_RESPONSE_CHARS`
- **THEN** the full text SHALL be returned with no truncation notice

#### Scenario: Text file exceeds the response cap

- **WHEN** `read_file` returns a text result larger than `MAX_READ_RESPONSE_CHARS`
- **THEN** the response SHALL contain at most that many characters
- **AND** SHALL state the range shown, the total size, and the continuing `offset`

#### Scenario: Forced text encoding is capped

- **WHEN** `read_file` is invoked with `encoding="text"` on a file larger than the response cap
- **THEN** the result SHALL be truncated with a continuation offset

#### Scenario: Continuing a truncated text read

- **WHEN** `read_file` is reissued with the reported `offset`
- **THEN** the returned window SHALL begin exactly where the previous window ended

#### Scenario: Invalid offset or limit

- **WHEN** `read_file` is invoked with a negative `offset` or a `limit` below 1
- **THEN** the tool SHALL return an error naming the offending value
- **AND** SHALL NOT return file content

#### Scenario: Binary results are not windowed

- **WHEN** `read_file` returns a base64 payload or an inline image block
- **THEN** the result SHALL NOT be truncated by the response cap

## MODIFIED Requirements

### Requirement: read_file tool

The MCP server SHALL expose a tool named `read_file` that returns the contents of an arbitrary file in the vault. The tool SHALL accept `path` (string, required), `encoding` (string, optional, default `"auto"`, one of `"auto"`, `"text"`, `"base64"`), `offset` (integer, optional, default 0), and `limit` (integer, optional).

The tool SHALL resolve the encoding as follows:
- `"auto"`: text-like files (by MIME type, e.g. `text/*`, `application/json`, `application/javascript`, `*+xml`) SHALL be returned as a text payload; image files (e.g. `image/png`, `image/jpeg`, `image/gif`, `image/webp`) SHALL be returned as an inline MCP image content block; all other files SHALL be returned as a base64-encoded string.
- `"text"`: the file SHALL be decoded as UTF-8 and returned as text; if it is not valid UTF-8 the tool SHALL return an error rather than corrupt output.
- `"base64"`: the file's raw bytes SHALL be returned as a base64-encoded string regardless of type.

File type SHALL be detected using the standard-library `mimetypes` mapping, with a magic-byte sniff used to confirm image types.

`offset` and `limit` apply only to text results and window the decoded text; they SHALL have no effect on base64 or image results.

#### Scenario: Tool is registered

- **WHEN** an MCP client lists available tools on the server
- **THEN** the listing SHALL contain `read_file`
- **AND** its input schema SHALL accept `path`, `encoding`, `offset`, and `limit`

#### Scenario: Auto encoding returns text for a text-like file

- **WHEN** `read_file` is invoked on an existing `.html` or `.txt` file with `encoding="auto"`
- **THEN** the response SHALL contain the file's contents as readable text
- **AND** the response SHALL NOT be base64-encoded

#### Scenario: Auto encoding returns an inline image block for an image

- **WHEN** `read_file` is invoked on an existing `.png` or `.jpg` file with `encoding="auto"`
- **THEN** the response SHALL include an MCP image content block carrying the image data and its MIME type

#### Scenario: Auto encoding returns base64 for other binaries

- **WHEN** `read_file` is invoked on an existing `.pdf` file with `encoding="auto"`
- **THEN** the response SHALL contain a base64-encoded string of the file's bytes
- **AND** the response SHALL indicate the encoding is base64

#### Scenario: Forced base64 on a text file

- **WHEN** `read_file` is invoked on a `.txt` file with `encoding="base64"`
- **THEN** the response SHALL contain the base64 encoding of the file's raw bytes

#### Scenario: Missing file

- **WHEN** `read_file` is invoked on a path that does not exist
- **THEN** the tool SHALL return an error message identifying the path
- **AND** the tool SHALL NOT raise an unhandled exception

### Requirement: Configurable file size limits

The server configuration SHALL expose `MAX_FILE_READ_BYTES` (default 10 MB), `MAX_FILE_WRITE_BYTES` (default 25 MB), and `MAX_READ_RESPONSE_CHARS` (default 40,000) as settings loadable from the environment.

`MAX_FILE_READ_BYTES` and `MAX_FILE_WRITE_BYTES` bound what the server reads from and writes to disk. `MAX_READ_RESPONSE_CHARS` bounds what a read tool returns to the caller. These are distinct limits: satisfying the byte caps does not bound the response, and a file well within `MAX_FILE_READ_BYTES` can still be far too large to return.

#### Scenario: Defaults apply when unset

- **WHEN** none of the environment variables is set
- **THEN** `MAX_FILE_READ_BYTES` SHALL default to 10 MB, `MAX_FILE_WRITE_BYTES` to 25 MB, and `MAX_READ_RESPONSE_CHARS` to 40,000

#### Scenario: Overrides are honored

- **WHEN** any of the three variables is set in the environment
- **THEN** the corresponding tool SHALL enforce the configured value

#### Scenario: Byte cap does not imply a bounded response

- **WHEN** a file is within `MAX_FILE_READ_BYTES` but its text far exceeds `MAX_READ_RESPONSE_CHARS`
- **THEN** the read SHALL succeed
- **AND** the returned text SHALL still be bounded by `MAX_READ_RESPONSE_CHARS`
