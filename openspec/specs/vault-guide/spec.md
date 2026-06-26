# vault-guide Specification

## Purpose
TBD - created by archiving change vault-guide-with-primer. Update Purpose after archive.
## Requirements
### Requirement: Tool name and registration

The MCP server SHALL expose a tool named `get_vault_guide` that returns onboarding instructions for an agent operating against the Obsidian vault. The tool SHALL accept no arguments and SHALL return a single string.

The MCP server SHALL NOT expose a tool named `get_vault_context`. The previous name is removed by this change.

#### Scenario: Tool is registered under the new name

- **WHEN** an MCP client lists available tools on the server
- **THEN** the listing SHALL contain `get_vault_guide`
- **AND** the listing SHALL NOT contain `get_vault_context`

#### Scenario: Tool accepts no arguments

- **WHEN** an MCP client invokes `get_vault_guide` with an empty arguments object
- **THEN** the tool SHALL return successfully without raising an argument-validation error

### Requirement: Response includes a hardcoded Obsidian primer

The tool SHALL include a static Obsidian primer section in every response. The primer SHALL cover, at minimum: wikilinks (`[[Note Name]]` and `[[Note Name|alias]]`), embeds (`![[Note]]`), block references (`[[Note#^block-id]]`), heading references (`[[Note#Heading]]`), backlinks, tags including nested tags (`#tag/nested`), YAML frontmatter conventions, and a note that Dataview/Templater syntax appearing inside notes SHOULD be treated as literal text rather than executed.

The primer content SHALL be loaded from a source that ships with the server (a Markdown file in the repository or a Python constant), not from the database or from the vault filesystem.

#### Scenario: Primer is present in response

- **WHEN** the tool is invoked
- **THEN** the response SHALL contain a section header that introduces the Obsidian primer
- **AND** the response SHALL contain at least one example of wikilink syntax (e.g., `[[Note Name]]`)
- **AND** the response SHALL contain at least one example of embed syntax (e.g., `![[Note]]`)

#### Scenario: Primer is the same regardless of vault state

- **WHEN** the tool is invoked against a vault with no `CLAUDE.md`
- **AND** the tool is invoked against a vault containing a `CLAUDE.md`
- **THEN** the primer section of both responses SHALL be byte-identical

### Requirement: Response includes vault-specific guide from `CLAUDE.md`

When `CLAUDE.md` exists at the vault root, the tool SHALL include the file's contents as a vault-specific section, concatenated after the primer with a clear section header separating the two.

When `CLAUDE.md` does not exist at the vault root, the tool SHALL include a short onboarding message in place of the vault-specific section that explains how to create a `CLAUDE.md` file and what to put in it (folder structure, naming conventions, tag taxonomy). The response SHALL NOT raise an error in this case.

#### Scenario: `CLAUDE.md` is present

- **WHEN** the tool is invoked against a vault where `CLAUDE.md` exists at the root
- **THEN** the response SHALL contain the contents of `CLAUDE.md` verbatim
- **AND** the `CLAUDE.md` contents SHALL appear after the primer section, separated by a section header

#### Scenario: `CLAUDE.md` is absent

- **WHEN** the tool is invoked against a vault where `CLAUDE.md` does not exist at the root
- **THEN** the response SHALL still return successfully (no error)
- **AND** the response SHALL include text instructing the user how to create a `CLAUDE.md` file
- **AND** the response SHALL include the primer section as usual

### Requirement: Other tools reference the guide neutrally

MCP tool docstrings on peer tools (e.g., `create_note`, `edit_note`) SHALL NOT instruct the agent to call `get_vault_guide` first using compelling language such as "MUST", "IMPORTANT: Call …first", or equivalent. References to `get_vault_guide` in peer-tool docstrings SHALL use neutral framing such as "see `get_vault_guide` for vault conventions".

#### Scenario: Peer-tool docstrings use neutral framing

- **WHEN** the docstring of a peer write tool (e.g., `create_note`) is inspected
- **THEN** the docstring SHALL NOT contain the phrase "MUST call" or "IMPORTANT: Call … first" in reference to `get_vault_guide`
- **AND** if the docstring mentions `get_vault_guide`, it SHALL frame the reference as informational ("see", "for context", "describes")

### Requirement: Usage logging records the new tool name

The usage-logging system SHALL record invocations of `get_vault_guide` with `tool='get_vault_guide'` in the `usage_logs.tool` column. Pre-existing rows with `tool='get_vault_context'` SHALL be left unchanged for historical accuracy.

#### Scenario: New invocations are logged under the new name

- **WHEN** a client invokes `get_vault_guide`
- **THEN** a row SHALL be appended to `usage_logs` with `tool='get_vault_guide'`

#### Scenario: Historical rows are not rewritten

- **WHEN** the change is deployed
- **THEN** rows previously written with `tool='get_vault_context'` SHALL remain in `usage_logs` with their original tool name unchanged

### Requirement: Guide references the file-access tools

The guide returned by `get_vault_guide` SHALL reference the raw file-access tools (`read_file`, `write_file`, `list_files`) so that an agent discovers it can read, write, and browse non-markdown files (PDFs, images, skill assets) in the vault. The reference SHALL use neutral framing consistent with how other peer tools are described.

#### Scenario: Guide mentions the file tools

- **WHEN** `get_vault_guide` is invoked
- **THEN** the response SHALL mention the `read_file`, `write_file`, and `list_files` tools
- **AND** the response SHALL indicate they operate on arbitrary (including non-markdown) files
