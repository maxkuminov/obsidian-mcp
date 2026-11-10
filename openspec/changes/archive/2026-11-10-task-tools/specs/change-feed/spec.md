## MODIFIED Requirements

### Requirement: MCP write tools SHALL record attributed events for every markdown publication
After a confirmed publication, the tool that performed it SHALL append an event carrying:

- `source = 'mcp'`;
- the server-authored `tool` name;
- the caller's `actor_kind` and `actor_label`, as written to `usage_logs`;
- the caller's principal as `principal_kind` and `principal_ref` (`api_key` and the key's id, or `oauth` and the grant id).

The events SHALL be:

| Tool | Event |
|---|---|
| `create_note` | `created` |
| `edit_note`, `set_frontmatter` | `modified` |
| `complete_task`, `update_task`, `add_task` | `modified` (recorded after the write-time task refresh, whatever its result) |
| `write_file`, `import_from_url`, `PUT /transfer/upload` on a `.md` path | `created` without an incumbent, `modified` with one |
| `move_note` | `moved`, inside its metadata transaction, plus one `modified` per backlink source whose rewrite it published |
| `delete_note` | `deleted` with `reason = 'removed'` |

`delete_file` refuses every `.md` path, and so never records an event.

A transfer upload SHALL be attributed to the actor and principal that minted the capability. That is the same principal the write-bucket charge at redemption already uses (`transfer.minting_principal`), so this reuses that rule rather than defining a second one.

A publication the tool reports as `publication_uncertain` SHALL record no event. A dry run, a refusal and a call that changed no byte SHALL record no event.

#### Scenario: An edit is attributed to the calling key
- **WHEN** an API key named `laptop` (id 12) edits `A.md`
- **THEN** an event SHALL be recorded with the `canonical_note_hash` of the bytes written and these fields:
  - `modified`;
  - `path = 'A.md'`;
  - `source = 'mcp'`;
  - `tool = 'edit_note'`;
  - `actor_kind = 'api_key'`;
  - `actor_label = 'laptop'`;
  - `principal_kind = 'api_key'`;
  - `principal_ref = '12'`.

#### Scenario: A task completion is attributed to the caller
- **WHEN** an OAuth grant completes a task in `Projects/Alpha.md`
- **THEN** one `modified` event SHALL be recorded with `tool = 'complete_task'`, the grant as its principal and the canonical hash of the bytes written, after the write-time task refresh

#### Scenario: A transfer upload is attributed to the minting principal
- **WHEN** an OAuth grant mints an upload link for `Inbox/x.md` and a person redeems it
- **THEN** the event SHALL carry the minting grant as its principal and the minting client's label, with `tool = 'transfer_upload'`, exactly the principal the redemption's write-bucket charge used

#### Scenario: A move that rewrites backlinks
- **WHEN** `move_note("A.md", "B.md", rewrite_links=True)` moves the note and rewrites two backlink sources
- **THEN** one `moved` event (`from_path = 'A.md'`, `path = 'B.md'`) and two `modified` events SHALL be recorded, all attributed to the caller

#### Scenario: A non-markdown write records nothing
- **WHEN** `write_file` writes `attachments/x.pdf`
- **THEN** no event SHALL be recorded

#### Scenario: A refused or dry-run task write records nothing
- **WHEN** `update_task` is refused with `task_changed`, or `add_task` runs with `dry_run=true`
- **THEN** no event SHALL be recorded
