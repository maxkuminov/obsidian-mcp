## ADDED Requirements

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
