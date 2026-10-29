## MODIFIED Requirements

### Requirement: Other tools reference the guide neutrally

MCP tool docstrings on peer tools (e.g., `create_note`, `edit_note`) and the server's `instructions` SHALL NOT instruct the agent to call `get_vault_guide` first using compelling language such as "MUST", "IMPORTANT: Call …first", or equivalent. References to `get_vault_guide` in peer-tool docstrings and in the server instructions SHALL use neutral framing such as "see `get_vault_guide` for vault conventions" or "`get_vault_guide` describes this vault's conventions; one call per session is enough".

#### Scenario: Peer-tool docstrings use neutral framing

- **WHEN** the docstring of a peer write tool (e.g., `create_note`) is inspected
- **THEN** the docstring SHALL NOT contain the phrase "MUST call" or "IMPORTANT: Call … first" in reference to `get_vault_guide`
- **AND** if the docstring mentions `get_vault_guide`, it SHALL frame the reference as informational ("see", "for context", "describes")

#### Scenario: The server instructions use neutral framing

- **WHEN** the server's `instructions` are inspected
- **THEN** they SHALL mention `get_vault_guide`
- **AND** SHALL NOT contain "MUST call" or "IMPORTANT" in reference to it
- **AND** SHALL frame the once-per-session advice as sufficiency ("one call per session is enough"), not as an order
