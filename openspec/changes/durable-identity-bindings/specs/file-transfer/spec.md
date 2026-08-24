## ADDED Requirements

### Requirement: A transfer capability records the actor that minted it
Minting a transfer capability SHALL record the denormalised actor of the minting request — its kind (`api_key` or `oauth`), its label (the key's name or the OAuth client's name) and its reference (the key's `omcp_` prefix or the `client_id`) — on the token row, and the redemption route SHALL copy those three values onto the `usage_logs` row it writes. The label SHALL be taken from the request-scoped actor the authentication middleware already bound, inside the mint's own transaction, so no path gains a database query for it. It SHALL be read through the **same single reader** the MCP tool-call log uses, so the two writers cannot drift in shape or truncation.

A redemption request carries a capability, not a credential, so the route has no request-scoped actor to read and attributed its usage rows by join alone — through `transfer_tokens.key_id` or through `transfer_tokens.oauth_token_id` → `oauth_clients`. Both joins go null on the operator's most urgent path: deleting an OAuth client cascades its tokens, and the panel nulls a key's `usage_logs.key_id` before deleting the key. The rows this destroys are the ones where bytes entered or left the vault, which are exactly the rows an operator reviewing a suspect credential opens the page to read.

The recorded actor is a **snapshot**, never re-derived: it names what the credential was called when the capability was minted. It is display and audit only and SHALL NOT be read for any authorization decision; the credential re-validation, the root check and the publish gate are unchanged by its presence.

#### Scenario: An upload's usage row survives deletion of its OAuth client

- **WHEN** a capability minted by an OAuth-authenticated request is redeemed, and the OAuth client is deleted afterwards
- **THEN** the `usage_logs` row for that redemption SHALL still render the client's name and `client_id`
- **AND** SHALL NOT render as an unknown actor

#### Scenario: A download's usage row survives deletion of its API key

- **WHEN** a capability minted by an API key is redeemed, and the panel then nulls that row's `key_id` and deletes the key
- **THEN** the `usage_logs` row SHALL still render the key's name and `omcp_` prefix

#### Scenario: The label costs no additional query

- **WHEN** `request_upload` or `request_download` mints a capability
- **THEN** the number of database statements issued SHALL be unchanged from before this requirement

#### Scenario: The label is a snapshot, not a lookup

- **WHEN** the minting credential is renamed between the mint and the redemption
- **THEN** the usage row SHALL carry the name the credential had at mint time

#### Scenario: One reader, so mint and log cannot disagree

- **WHEN** the mint records an actor and a tool call in the same request logs one
- **THEN** both SHALL produce the same kind, label and reference, including the same truncation to the stored widths

#### Scenario: A mint with no request-scoped actor records none

- **WHEN** a capability is minted on a path that carries no request-scoped actor
- **THEN** the three values SHALL be left unset rather than inferred from any other row
- **AND** the redemption's usage row SHALL keep the row shape it had before this requirement

#### Scenario: A pre-migration transfer usage row is not relabelled

- **WHEN** a transfer-route `usage_logs` row written before this scheme is rendered
- **THEN** it SHALL be attributed by the existing credential joins, and rendered as an unattributable row when those joins resolve to nothing
- **AND** nothing SHALL write an actor onto it after the fact

#### Scenario: The recorded actor is never consulted for authorization

- **WHEN** a capability whose recorded actor names a credential that has since been deleted is redeemed
- **THEN** the redemption decision SHALL be made by the credential re-validation and the root check exactly as before, and the recorded actor SHALL affect only what is written to the usage log

### Requirement: `delete_file` confirms the caller's vault assignment before it deletes
`delete_file` SHALL re-read the caller's vault assignment from the database immediately before it soft-deletes or unlinks, and SHALL refuse when the assignment no longer equals the root the request bound at admission — when it differs, when it has been cleared, when the user row is gone, or when the user is no longer active. On refusal nothing SHALL be deleted and no trash entry SHALL be created.

`delete_file` does not publish through the shared mutation target the note tools use: it resolves its root separately and walks from an independently opened root descriptor. The structural refusal that covers the note tools therefore does not reach it, and its confirmation is stated here rather than left as an unremarked gap — a destructive operation in a vault the caller has been reassigned away from is the same defect as a write into one.

Single-user mode has no user row to re-read and SHALL be unaffected.

#### Scenario: Reassignment between admission and deletion

- **WHEN** an administrator reassigns the caller to a different vault root after the request was admitted and before `delete_file` reaches its delete
- **THEN** the call SHALL be refused with a tool error
- **AND** the file in the former vault SHALL be unchanged, and no `.trash` entry SHALL exist for it

#### Scenario: Unassignment between admission and deletion

- **WHEN** the caller's vault assignment is cleared in that same window
- **THEN** the call SHALL be refused and nothing SHALL be deleted

#### Scenario: An unchanged assignment deletes as before

- **WHEN** the assignment is unchanged
- **THEN** `delete_file` SHALL behave exactly as it does today, soft-deleting by default and unlinking on `permanent=True`

#### Scenario: Single-user mode is unaffected

- **WHEN** `delete_file` runs in single-user mode
- **THEN** it SHALL issue no assignment re-read and SHALL behave exactly as before

### Requirement: The transfer publish gate is not weakened to the optimistic form
The transfer routes and `import_from_url` SHALL continue to hold their credential and user rows `SELECT … FOR UPDATE` across the filesystem publish, and SHALL continue to compare the database's current root against the root captured at mint. The optimistic re-read adopted for the note mutation tools SHALL NOT replace either gate.

Those paths hold a token row and an already-open session, and their publish is a bounded byte stream, so the locked gate costs them little and buys linearizability. The note tools have none of those properties, which is why they take the weaker guarantee — a difference in what each path can afford, not a difference of opinion about what is correct.

#### Scenario: The upload route still publishes under held locks

- **WHEN** `PUT /transfer/upload` publishes
- **THEN** the token, credential and user rows SHALL be held `FOR UPDATE` from before the publish until the completion and the usage row commit

#### Scenario: `import_from_url` still locks its own identity

- **WHEN** `import_from_url` publishes fetched bytes
- **THEN** it SHALL hold its credential and user rows `FOR UPDATE` across the publish and SHALL re-check the database's current root against the root captured when the tool started
