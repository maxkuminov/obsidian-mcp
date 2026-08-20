## ADDED Requirements

### Requirement: Deleting an OAuth client preserves its usage attribution
Deleting an OAuth client from the control panel SHALL leave every `usage_logs` row that client produced attributed to it by name. The delete MUST remain a delete — the cascades that remove the client's tokens, authorization codes and any transfer capabilities minted under those tokens are the point of it and MUST be preserved — and MUST NOT be replaced by marking tokens revoked, which a surviving client row can defeat by refresh.

#### Scenario: History stays readable after the client is deleted
- **WHEN** an operator deletes an OAuth client and then opens the usage view
- **THEN** every row that client produced SHALL still name the client
- **AND** no such row SHALL render as an unknown actor

#### Scenario: The stop is still a real stop
- **WHEN** an OAuth client is deleted
- **THEN** its access and refresh tokens SHALL be removed
- **AND** any outstanding transfer capabilities minted under those tokens SHALL be removed
- **AND** the client SHALL NOT be able to obtain new tokens

### Requirement: The delete confirmation states what the delete actually does
The control panel's OAuth client Delete control SHALL describe the operation it performs — that the client's tokens are deleted, that outstanding transfer links minted under them stop working, and that usage history keeps its attribution — and SHALL NOT describe it as revoking the client's tokens.

#### Scenario: The confirmation no longer promises a revocation
- **WHEN** the OAuth clients page is rendered
- **THEN** the Delete control's confirmation SHALL NOT describe the action as revoking the client's tokens

#### Scenario: The confirmation names the consequences
- **WHEN** the OAuth clients page is rendered
- **THEN** the Delete control's confirmation SHALL state that the tokens are deleted, that transfer links minted under them stop working, and that usage history remains attributed to the client
