## MODIFIED Requirements

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
