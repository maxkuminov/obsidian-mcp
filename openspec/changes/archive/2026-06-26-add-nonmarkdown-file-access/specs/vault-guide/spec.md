## ADDED Requirements

### Requirement: Guide references the file-access tools

The guide returned by `get_vault_guide` SHALL reference the raw file-access tools (`read_file`, `write_file`, `list_files`) so that an agent discovers it can read, write, and browse non-markdown files (PDFs, images, skill assets) in the vault. The reference SHALL use neutral framing consistent with how other peer tools are described.

#### Scenario: Guide mentions the file tools

- **WHEN** `get_vault_guide` is invoked
- **THEN** the response SHALL mention the `read_file`, `write_file`, and `list_files` tools
- **AND** the response SHALL indicate they operate on arbitrary (including non-markdown) files
