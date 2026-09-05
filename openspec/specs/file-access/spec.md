# file-access Specification

## Purpose
Raw read, write, and browse access to arbitrary (non-markdown and markdown) files in the vault via MCP tools, including binary transport (base64 / inline image blocks), size caps, dot-dir exclusion, and path-traversal safety. Distinct peers to the markdown-only note tools; pure byte transport with no server-side extraction, embedding, or indexing of non-markdown files.
## Requirements
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

### Requirement: read_file size cap
The `read_file` tool SHALL refuse to read files larger than a configurable limit `MAX_FILE_READ_BYTES` (default 10 MB). It SHALL enforce the cap while reading through one open file descriptor so a pathname replacement or file growth cannot cause an unbounded result after a separate size check. When a file exceeds the limit, the tool SHALL return an error rather than returning partial or truncated content.

#### Scenario: File within the cap
- **WHEN** `read_file` is invoked on a file whose size is at or below `MAX_FILE_READ_BYTES`
- **THEN** the file's contents SHALL be returned

#### Scenario: File exceeds the cap
- **WHEN** `read_file` is invoked on a file whose size exceeds `MAX_FILE_READ_BYTES`, including growth during the read
- **THEN** the tool SHALL return an error that states the configured limit and path
- **AND** the tool SHALL NOT return file contents

### Requirement: write_file tool

The MCP server SHALL expose a tool named `write_file` that writes a file into the vault. The tool SHALL accept `path` (string, required), `content` (string, required), `encoding` (string, optional, default `"base64"`, one of `"base64"`, `"text"`), and `overwrite` (boolean, optional, default `false`).

When `encoding="base64"`, the tool SHALL base64-decode `content` and write the resulting raw bytes. When `encoding="text"`, the tool SHALL write `content` as UTF-8 text. The write SHALL be atomic, reusing the existing same-directory temp-file-plus-`os.replace` mechanism so a crash mid-write cannot truncate the destination.

The tool SHALL create any missing parent directories of `path` before writing.

#### Scenario: Tool is registered

- **WHEN** an MCP client lists available tools on the server
- **THEN** the listing SHALL contain `write_file`

#### Scenario: Write a new binary file via base64

- **WHEN** `write_file` is invoked with a non-existent `path`, base64 `content`, and `encoding="base64"`
- **THEN** the decoded bytes SHALL be written to that path
- **AND** the tool SHALL report success

#### Scenario: Write text content

- **WHEN** `write_file` is invoked with `encoding="text"` and a string body
- **THEN** the body SHALL be written verbatim as UTF-8

#### Scenario: Parent directories are created

- **WHEN** `write_file` is invoked with a `path` whose parent folder does not yet exist
- **THEN** the missing parent folders SHALL be created
- **AND** the file SHALL be written

#### Scenario: Invalid base64 content

- **WHEN** `write_file` is invoked with `encoding="base64"` and `content` that is not valid base64
- **THEN** the tool SHALL return an error
- **AND** no file SHALL be written

### Requirement: write_file no-clobber default
The `write_file` tool SHALL NOT overwrite an existing file unless `overwrite=true` is passed. With `overwrite=false`, destination creation and commit SHALL be one race-safe no-clobber operation; a destination created by another actor during the call MUST remain unchanged.

#### Scenario: Existing file without overwrite
- **WHEN** `write_file` targets an existing file with `overwrite=false`
- **THEN** the tool SHALL return an error indicating the file already exists
- **AND** the existing file's contents SHALL be unchanged

#### Scenario: Destination appears concurrently
- **WHEN** another actor creates the destination while `write_file(overwrite=false)` is in progress
- **THEN** `write_file` SHALL fail without replacing that destination

#### Scenario: Existing file with overwrite
- **WHEN** `write_file` targets an existing file with `overwrite=true`
- **THEN** the file SHALL be replaced atomically with the new content

### Requirement: write_file size cap

The `write_file` tool SHALL refuse to write content larger than a configurable limit `MAX_FILE_WRITE_BYTES` (default 25 MB), measured by the decoded byte length. When the content exceeds the limit, the tool SHALL return an error and SHALL NOT write any file.

#### Scenario: Content exceeds the cap

- **WHEN** `write_file` is invoked with decoded content larger than `MAX_FILE_WRITE_BYTES`
- **THEN** the tool SHALL return an error reporting the size limit
- **AND** no file SHALL be written

### Requirement: list_files tool

The MCP server SHALL expose a tool named `list_files` that browses the vault filesystem. The tool SHALL accept `folder` (string, optional, default `"."` meaning the vault root), `pattern` (string glob, optional, default `"*"`), `recursive` (boolean, optional, default `false`), and `limit` (integer, optional, default 200).

By default (`recursive=false`) the tool SHALL list the immediate children of `folder` only: both subdirectories and files, including markdown files. Each file entry SHALL include its vault-relative path, size in bytes, and modification time; directory entries SHALL be distinguishable from file entries. The `pattern` glob SHALL filter file entries. When the number of matching entries exceeds `limit`, the tool SHALL return at most `limit` entries and SHALL indicate that the result was truncated.

#### Scenario: Tool is registered

- **WHEN** an MCP client lists available tools on the server
- **THEN** the listing SHALL contain `list_files`

#### Scenario: Non-recursive listing of a folder

- **WHEN** `list_files` is invoked on a folder that contains both files and subfolders, with `recursive=false`
- **THEN** the response SHALL list the folder's immediate files (with size and mtime) and its immediate subdirectories
- **AND** the response SHALL NOT include entries from nested subfolders

#### Scenario: Glob pattern filter

- **WHEN** `list_files` is invoked with `pattern="*.pdf"`
- **THEN** the response SHALL include only files whose names match `*.pdf`

#### Scenario: Recursive listing

- **WHEN** `list_files` is invoked with `recursive=true`
- **THEN** the response SHALL include matching files from nested subfolders beneath `folder`

#### Scenario: Result is capped

- **WHEN** the number of matching entries exceeds `limit`
- **THEN** the response SHALL contain at most `limit` entries
- **AND** the response SHALL indicate that the listing was truncated

### Requirement: Dot-dir exclusion and path-traversal safety

The `read_file`, `write_file`, `list_files`, and `delete_file` tools, the transfer mint tools (`request_upload`, `request_download`, `import_from_url`), and the `/transfer/*` HTTP routes SHALL reject any path that resolves outside the vault root, reusing the existing vault path-traversal guard. In addition, they SHALL reject any path that contains a directory component beginning with a dot (e.g. `.obsidian`, `.git`, `.trash`, `.smart-env`), consistent with the indexer's existing visibility rule. `list_files` SHALL omit dot-directories and their contents from results. Transfer routes SHALL apply the guard to the token's bound path at use time, not only at mint time.

#### Scenario: Path traversal is rejected

- **WHEN** any of these tools is invoked with a `path` that escapes the vault root (e.g. via `../`)
- **THEN** the tool SHALL return an error
- **AND** no file SHALL be read or written

#### Scenario: Dot-dir path is rejected

- **WHEN** `read_file`, `write_file`, `delete_file`, `request_upload`, `request_download`, or `import_from_url` is invoked with a `path` inside a dot-directory (e.g. `.obsidian/config.json`)
- **THEN** the tool SHALL return an error
- **AND** no file SHALL be read, written, or deleted

#### Scenario: list_files hides dot-dirs

- **WHEN** `list_files` is invoked on a folder that contains a dot-directory
- **THEN** the dot-directory SHALL NOT appear in the results
- **AND** invoking `list_files` with `folder` set to a dot-directory SHALL return an error

#### Scenario: Transfer route re-validates at use time

- **WHEN** an upload token's bound path has become invalid or hidden between mint and use
- **THEN** the upload SHALL be refused and no file SHALL be written

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

### Requirement: write_file refuses a symlinked final component

`write_file` SHALL apply the same rule as the mutating note tools: it acts on the named path and refuses (naming the link target, no write) when the final component is a symbolic link, including a dangling one; symlinked directory components resolving inside the vault remain permitted. `read_file` is unchanged.

#### Scenario: write_file on an alias

- **WHEN** `alias.png` is a symlink to `real.png` and `write_file("alias.png", …, overwrite=True)` is invoked
- **THEN** the tool SHALL return an error naming `real.png` and `real.png` SHALL be unchanged

### Requirement: list_files refuses over-long patterns

`list_files` SHALL refuse a `pattern` longer than `MAX_LIST_PATTERN_CHARS` (1,024 characters) before the pattern is compiled, before the folder path is validated, and before any directory is read, returning a tool-level error that names the limit. The check SHALL live in the shared `list_dir` service entry and SHALL raise `ValueError`, which the tool already maps to an in-band refusal.

#### Scenario: Over-long pattern

- **WHEN** `list_files` is invoked with a `pattern` of 1,025 or more characters, on any folder including a non-existent one
- **THEN** the tool SHALL return an error naming `MAX_LIST_PATTERN_CHARS`, SHALL NOT compile the pattern, and SHALL NOT read or validate the folder

#### Scenario: Pattern at the limit

- **WHEN** `list_files` is invoked with a `pattern` of exactly 1,024 characters
- **THEN** the listing SHALL proceed normally

### Requirement: write_file applies the note size cap to markdown destinations

When the destination path of `write_file` ends in `.md` (case-insensitive), the tool SHALL refuse decoded content larger than the smaller of `MAX_NOTE_BYTES` (10 MiB) and `MAX_FILE_WRITE_BYTES`, returning an error that names the limit that applied, so that no tool can place a markdown file the note tools would refuse and the indexer would then read. Non-markdown destinations keep the `MAX_FILE_WRITE_BYTES` cap.

#### Scenario: Oversized markdown via write_file

- **WHEN** `write_file` is invoked with a `.md` path and decoded content of `MAX_NOTE_BYTES + 1` bytes, with `MAX_FILE_WRITE_BYTES` at its default
- **THEN** the tool SHALL return an error naming `MAX_NOTE_BYTES` and nothing SHALL be written

#### Scenario: Large non-markdown file is unaffected

- **WHEN** `write_file` is invoked with a `.pdf` path and content between `MAX_NOTE_BYTES` and `MAX_FILE_WRITE_BYTES`, with `MAX_FILE_WRITE_BYTES` at its default
- **THEN** the write SHALL succeed

#### Scenario: Operator lowers the file cap below the note cap

- **WHEN** `MAX_FILE_WRITE_BYTES` is configured below `MAX_NOTE_BYTES` and `write_file` targets a `.md` path with content between the two
- **THEN** the tool SHALL refuse, naming `MAX_FILE_WRITE_BYTES`

