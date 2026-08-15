## ADDED Requirements

### Requirement: write_file refuses a symlinked final component

`write_file` SHALL apply the same rule as the mutating note tools: it acts on the named path and refuses (naming the link target, no write) when the final component is a symbolic link, including a dangling one; symlinked directory components resolving inside the vault remain permitted. `read_file` is unchanged.

#### Scenario: write_file on an alias

- **WHEN** `alias.png` is a symlink to `real.png` and `write_file("alias.png", …, overwrite=True)` is invoked
- **THEN** the tool SHALL return an error naming `real.png` and `real.png` SHALL be unchanged
