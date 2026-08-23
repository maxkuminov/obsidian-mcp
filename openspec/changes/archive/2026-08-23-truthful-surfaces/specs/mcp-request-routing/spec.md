## ADDED Requirements

### Requirement: The users list MUST NOT report a note count the tools will not serve
The control panel's user list SHALL NOT render a note count for an account that holds no vault assignment. It SHALL render an explicit not-served state instead, stating that every MCP tool is refused for that account and that the index is kept for reassignment, so the operator reads the same fact the admission gate enforces.

A number rendered beside `(unassigned)` reads as capacity the account has, when in fact every tool call from that account is refused before its body runs. This is the same over-reporting of liveness as the revoked-key count the panel used to present as an unqualified total, and the same class as a control offered for a credential the middleware already rejects.

#### Scenario: Unassigned account

- **WHEN** the user list renders an account whose vault assignment is empty
- **THEN** the note column SHALL show a not-served state rather than a number
- **AND** SHALL state that the tools are refused and the index is retained for reassignment

#### Scenario: Assigned account

- **WHEN** the user list renders an account that holds a vault assignment
- **THEN** the note column SHALL show that account's note count as before

#### Scenario: The retained rows are not deleted to make the display true

- **WHEN** the display changes for an unassigned account
- **THEN** the account's `notes_metadata`, `note_embeddings` and `note_links` rows SHALL remain in the database, so a reassignment to the same directory still resumes without a full re-index
