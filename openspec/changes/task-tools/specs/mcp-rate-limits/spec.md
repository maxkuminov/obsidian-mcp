## MODIFIED Requirements

### Requirement: Two per-principal token buckets admit every tool call

Every `_tracked` tool call SHALL pass a per-principal general token bucket before any other gate in the decorator, refilling at `MCP_RATE_LIMIT_PER_MINUTE` tokens per minute with capacity `MCP_RATE_LIMIT_BURST`.

Every call to a vault-mutating tool SHALL additionally pass a second per-principal bucket, refilling at `MCP_WRITE_RATE_LIMIT_PER_MINUTE` with capacity `MCP_WRITE_RATE_LIMIT_BURST`, checked immediately after the general bucket. The vault-mutating tools are `create_note`, `edit_note`, `move_note`, `delete_note`, `set_frontmatter`, `write_file`, `delete_file`, `import_from_url`, `complete_task`, `update_task` and `add_task`.

Each bucket:

- SHALL be updated synchronously, with no `await` between reading and writing its state;
- SHALL issue no database statement;
- SHALL hold no lock.

A token SHALL be consumed by every call that reaches a bucket, including calls a later gate then refuses: a token refills, and a durable quota slot does not.

Both buckets SHALL be **velocity** bounds. They SHALL NOT be claimed to bound the total work a credential can do in a day, which is bounded only by the daily quota and only for the credentials that quota reaches.

Either bucket's rate and burst SHALL be set together or be null together. Null SHALL disable that bucket, and zero SHALL be rejected as a configuration value.

**The write bucket SHALL also be consumed where vault bytes are written outside a tool call.** `PUT /transfer/upload` publishes into the vault by redeeming a capability and never passes through the tool decorator, so bounding only the vault-mutating tools would leave the write rate escapable by minting capabilities and redeeming them. Redemption SHALL therefore consume the write bucket of the principal **that minted the token**:

- `("api_key", key_id)` when the transfer row names a key;
- `("oauth", grant_id)`, taken from the minting token row that identity resolution already loads.

No additional query is required and no schema change is needed.

`request_upload`, `request_download` and `check_upload` only mint or read capability rows, and SHALL NOT consume the write bucket. `import_from_url` SHALL continue to consume it at the tool call, as an ordinary write tool, and SHALL NOT be charged again.

#### Scenario: A capability redemption consumes the minting principal's write bucket

- **WHEN** a principal mints upload capabilities and redeems them through `PUT /transfer/upload` faster than `MCP_WRITE_RATE_LIMIT_PER_MINUTE`
- **THEN** the excess redemptions SHALL be refused, and the write rate SHALL NOT be escapable by moving writes from the tools onto the transfer route

#### Scenario: Minting is not double-charged

- **WHEN** a principal calls `request_upload` and later redeems the capability
- **THEN** the write bucket SHALL be consumed once, at redemption, and `import_from_url` SHALL continue to consume it once, at its tool call

#### Scenario: A burst from one principal does not delay another principal's call

- **WHEN** principal A exhausts its burst capacity and continues calling, while principal B issues a single ordinary tool call
- **THEN** B's call SHALL be admitted and executed without waiting on A, and A's excess calls SHALL be refused rather than queued

#### Scenario: Write tools pass both buckets

- **WHEN** a principal calls `delete_note`, or `complete_task`, at a rate under `MCP_RATE_LIMIT_PER_MINUTE` but above `MCP_WRITE_RATE_LIMIT_PER_MINUTE`
- **THEN** the excess calls SHALL be refused with a scope naming the write bucket, while that principal's read tools, `list_tasks` and `task_agenda` included, SHALL continue to be admitted

#### Scenario: Sustained rate is admitted

- **WHEN** a principal calls at or below the configured sustained rate, having started with a full bucket
- **THEN** every call SHALL be admitted

#### Scenario: The admitted path performs no additional I/O

- **WHEN** a call is admitted by both buckets
- **THEN** the gates SHALL have issued no SQL statement and no session checkout, proven by a statement-counting test
