## MODIFIED Requirements

### Requirement: Guide references the file-access tools

The guide returned by `get_vault_guide` SHALL reference the raw file-access tools (`read_file`, `write_file`, `list_files`, `delete_file`) so that an agent discovers it can read, write, browse, and delete non-markdown files (PDFs, images, skill assets) in the vault, and SHALL describe the file-transfer flow for clients that cannot supply bytes: `request_upload` → hand the human the link → `check_upload`, `request_download` for handing files out, and `import_from_url` for links — with `![[path]]` via `edit_note` as the way to embed a stored file in a note. The reference SHALL use neutral framing consistent with how other peer tools are described.

#### Scenario: Guide mentions the file tools

- **WHEN** `get_vault_guide` is invoked
- **THEN** the response SHALL mention the `read_file`, `write_file`, `list_files`, and `delete_file` tools
- **AND** the response SHALL indicate they operate on arbitrary (including non-markdown) files

#### Scenario: Guide explains transfer

- **WHEN** `get_vault_guide` is invoked
- **THEN** the response SHALL mention `request_upload`, `check_upload`, `request_download`, and `import_from_url` and the upload-link flow in one short paragraph
