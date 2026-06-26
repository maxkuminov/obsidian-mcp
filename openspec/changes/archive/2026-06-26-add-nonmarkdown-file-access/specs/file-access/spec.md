## ADDED Requirements

### Requirement: read_file tool

The MCP server SHALL expose a tool named `read_file` that returns the contents of an arbitrary file in the vault. The tool SHALL accept `path` (string, required) and `encoding` (string, optional, default `"auto"`, one of `"auto"`, `"text"`, `"base64"`).

The tool SHALL resolve the encoding as follows:
- `"auto"`: text-like files (by MIME type, e.g. `text/*`, `application/json`, `application/javascript`, `*+xml`) SHALL be returned as a text payload; image files (e.g. `image/png`, `image/jpeg`, `image/gif`, `image/webp`) SHALL be returned as an inline MCP image content block; all other files SHALL be returned as a base64-encoded string.
- `"text"`: the file SHALL be decoded as UTF-8 and returned as text; if it is not valid UTF-8 the tool SHALL return an error rather than corrupt output.
- `"base64"`: the file's raw bytes SHALL be returned as a base64-encoded string regardless of type.

File type SHALL be detected using the standard-library `mimetypes` mapping, with a magic-byte sniff used to confirm image types.

#### Scenario: Tool is registered

- **WHEN** an MCP client lists available tools on the server
- **THEN** the listing SHALL contain `read_file`

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

The `read_file` tool SHALL refuse to read files larger than a configurable limit `MAX_FILE_READ_BYTES` (default 10 MB). When a file exceeds the limit, the tool SHALL return an error that reports the file's size and path rather than returning partial or truncated content.

#### Scenario: File within the cap

- **WHEN** `read_file` is invoked on a file whose size is at or below `MAX_FILE_READ_BYTES`
- **THEN** the file's contents SHALL be returned

#### Scenario: File exceeds the cap

- **WHEN** `read_file` is invoked on a file whose size exceeds `MAX_FILE_READ_BYTES`
- **THEN** the tool SHALL return an error that states the file's actual size and path
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

The `write_file` tool SHALL NOT overwrite an existing file unless `overwrite=true` is passed. When the target exists and `overwrite` is `false`, the tool SHALL return an error and leave the existing file unchanged.

#### Scenario: Existing file without overwrite

- **WHEN** `write_file` targets an existing file with `overwrite=false`
- **THEN** the tool SHALL return an error indicating the file already exists
- **AND** the existing file's contents SHALL be unchanged

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

The `read_file`, `write_file`, and `list_files` tools SHALL reject any path that resolves outside the vault root, reusing the existing vault path-traversal guard. In addition, these tools SHALL reject any path that contains a directory component beginning with a dot (e.g. `.obsidian`, `.git`, `.trash`, `.smart-env`), consistent with the indexer's existing visibility rule. `list_files` SHALL omit dot-directories and their contents from results.

#### Scenario: Path traversal is rejected

- **WHEN** any of the three tools is invoked with a `path` that escapes the vault root (e.g. via `../`)
- **THEN** the tool SHALL return an error
- **AND** no file SHALL be read or written

#### Scenario: Dot-dir path is rejected

- **WHEN** `read_file` or `write_file` is invoked with a `path` inside a dot-directory (e.g. `.obsidian/config.json`)
- **THEN** the tool SHALL return an error
- **AND** no file SHALL be read or written

#### Scenario: list_files hides dot-dirs

- **WHEN** `list_files` is invoked on a folder that contains a dot-directory
- **THEN** the dot-directory SHALL NOT appear in the results
- **AND** invoking `list_files` with `folder` set to a dot-directory SHALL return an error

### Requirement: Configurable file size limits

The server configuration SHALL expose `MAX_FILE_READ_BYTES` (default 10 MB) and `MAX_FILE_WRITE_BYTES` (default 25 MB) as settings loadable from the environment, governing `read_file` and `write_file` respectively.

#### Scenario: Defaults apply when unset

- **WHEN** neither environment variable is set
- **THEN** `MAX_FILE_READ_BYTES` SHALL default to 10 MB and `MAX_FILE_WRITE_BYTES` SHALL default to 25 MB

#### Scenario: Overrides are honored

- **WHEN** `MAX_FILE_READ_BYTES` or `MAX_FILE_WRITE_BYTES` is set in the environment
- **THEN** the corresponding tool SHALL enforce the configured value
