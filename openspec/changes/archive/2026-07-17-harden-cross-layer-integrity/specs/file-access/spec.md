## MODIFIED Requirements

### Requirement: read_file size cap
The `read_file` tool SHALL refuse to read files larger than a configurable limit `MAX_FILE_READ_BYTES` (default 10 MB). It SHALL enforce the cap while reading through one open file descriptor so a pathname replacement or file growth cannot cause an unbounded result after a separate size check. When a file exceeds the limit, the tool SHALL return an error rather than returning partial or truncated content.

#### Scenario: File within the cap
- **WHEN** `read_file` is invoked on a file whose size is at or below `MAX_FILE_READ_BYTES`
- **THEN** the file's contents SHALL be returned

#### Scenario: File exceeds the cap
- **WHEN** `read_file` is invoked on a file whose size exceeds `MAX_FILE_READ_BYTES`, including growth during the read
- **THEN** the tool SHALL return an error that states the configured limit and path
- **AND** the tool SHALL NOT return file contents

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
