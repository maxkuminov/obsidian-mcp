# change-feed Specification

## Purpose
The durable, owner-scoped record of what changed in a vault (#351): the `note_events` log and its two writers (the MCP write tools, attributed at call time, and the index pass, unattributed `human/sync`), the per-scope sequence assigned under an advisory lock held to commit that makes an `ev:<seq>` cursor exact and private, suppression of a pass's duplicate of a tool's own event, the baseline `reset`, overflow and durable `gap` lines, retention with its watermark, the purges on provenance discard and permanent user deletion, and the `get_changes` tool's arguments, `by` semantics, rendering and disclosure rules.
## Requirements
### Requirement: Note changes SHALL be recorded in one durable, owner-scoped event table without foreign keys
The system SHALL record note changes as rows of `note_events`, one row per event, each carrying the owner scope as a plain `user_id` column (NULL for single-user), a per-scope sequence number `seq`, `occurred_at`, a `kind` from `created`, `modified`, `moved`, `deleted` (note kinds) or `gap`, `reset` (meta kinds), the vault-relative `path` (the destination for a move, NULL for meta kinds), `from_path` for a move, the canonical `content_hash` the event leaves at `path` when known, and a `source` of `mcp` (written by a tool, attributed) or `index` (discovered by the index pass, unattributed), and a `reason` (`removed`, `not_indexable` or `quarantined`) on a `deleted` event and on a content event about bytes that cannot be indexed. `note_events`, `note_event_scopes` and `note_event_gaps` SHALL have no foreign key to `users` or to `notes_metadata`. An FK check on `users` taken after the pass's `notes_metadata` row locks would close the documented deadlock with permanent user deletion, and events must outlive their rows. An event SHALL be written only into the scope whose vault root its paths are relative to. The record of a deletion SHALL NOT depend on the deleted file's presence in `.trash/` or on the deleted note's `notes_metadata` row.

#### Scenario: A deletion outlives the row
- **WHEN** a note is deleted outside the server (not into `.trash/`) and the next index pass prunes its row
- **THEN** a `deleted` event for its path SHALL exist in `note_events` after the row is gone

#### Scenario: A tool event is scoped to the caller's vault
- **WHEN** user 7 calls `edit_note` on `Projects/A.md`
- **THEN** the event SHALL carry `user_id = 7` and `path = 'Projects/A.md'`, relative to user 7's root

#### Scenario: No foreign keys
- **WHEN** the schema gate inspects `pg_constraint` for the three feed tables
- **THEN** none SHALL carry a foreign-key constraint

### Requirement: Every append SHALL assign a per-scope sequence under a lock held to commit, so that a cursor is exact and private
Every insert into `note_events` SHALL go through one helper that takes a transaction-scoped advisory lock keyed on the feed's namespace and the scope (`user_id`, or 0 for single-user), holds it until commit, and takes no lock after it on any row another transaction can hold. Under that lock the helper SHALL assign each event `seq = note_event_scopes.last_seq + 1, …` and store the new `last_seq`. `(user_id, seq)` SHALL be unique, and `last_seq` SHALL never decrease. Consequently, for any reader snapshot, every event of a scope not visible to it SHALL have a seq greater than every visible event of that scope. The cursor SHALL be the seq. The surrogate primary key SHALL never be rendered or accepted.

#### Scenario: A late commit cannot fall behind a returned cursor
- **WHEN** two transactions append events to the same scope concurrently and a `get_changes` call runs between their commits
- **THEN** the event of the transaction committing second SHALL have a seq greater than the `next_since` that call returned
- **AND** a following `get_changes(since=<that next_since>)` SHALL return it exactly once

#### Scenario: Interleaved tenants see gapless sequences
- **WHEN** users 7 and 9 alternate writes, ten each
- **THEN** user 7's `get_changes(since="ev:0")` SHALL show seqs 1 to 10 with no gap, and so SHALL user 9's

#### Scenario: No direct insert bypasses the helper
- **WHEN** the source tree is searched for inserts into `note_events`
- **THEN** the only insert site SHALL be the append helper in `src/services/change_feed.py`

### Requirement: A tool's event SHALL be appended only while the caller is still bound to the root the tool ran under
A tool SHALL pass the append helper a binding of the vault root `_tracked` resolved for the call: a digest of its canonical root path. Under the feed lock, the helper SHALL re-read the caller's current assignment and SHALL append only when its binding is equal. Otherwise it SHALL write nothing for that call and SHALL log one security event, `change_feed_root_changed`, naming the user and no path, root or digest. Neither the root nor its digest SHALL be rendered by any tool. Single-user mode SHALL skip the check.

#### Scenario: Reassignment between publication and append
- **WHEN** a tool publishes `Secret.md` under user 7's root A, an administrator reassigns user 7 to root B, B's first pass discards the scope and records `reset`, and only then does the tool's append take the feed lock
- **THEN** no event naming `Secret.md` from root A SHALL enter user 7's feed
- **AND** one `change_feed_root_changed` security event SHALL be logged

#### Scenario: Append before the discard
- **WHEN** the tool's append commits before the reassignment's discard
- **THEN** the discard SHALL purge it, and user 7's feed after the discard SHALL hold only the `reset` and later events

### Requirement: Permanent user deletion SHALL remove the scope's feed rows without a lock cycle and without orphans
The permanent-deletion path SHALL delete the scope's `note_events`, `note_event_scopes` and `note_event_gaps` rows in its own transaction, after its cascade statement and after taking the scope's feed lock as its last lock. The append helper SHALL, under the feed lock, write nothing for a `user_id` that no longer has a `users` row. Retention SHALL delete feed rows whose `user_id` has no `users` row as a backstop.

#### Scenario: Deletion interleaved with a pass's append
- **WHEN** a pass for user 7 holds its `notes_metadata` row locks and reaches its append while an administrator permanently deletes user 7
- **THEN** neither transaction SHALL be aborted as a deadlock victim
- **AND** after both complete, no `note_events`, `note_event_scopes` or `note_event_gaps` row with `user_id = 7` SHALL remain

#### Scenario: Append after deletion
- **WHEN** a tool's append for user 7 takes the feed lock after user 7's permanent deletion has committed
- **THEN** it SHALL write no row

### Requirement: An event write SHALL never lose the change it describes
`move_note`'s `moved` event SHALL be inserted inside the same transaction as its `notes_metadata` path update, all or nothing, so that a failed event also leaves the row at the old path for the next pass to pair and record. Every other tool publication is file-only. Its event SHALL be appended in its own transaction after the publication, and a failure there SHALL be logged and SHALL NOT raise: the next pass observes the change and records it as an `index` event. The consequence is misattribution, never loss. The index pass's append, and every file-only tool's, SHALL run in a savepoint with `lock_timeout = CHANGE_FEED_LOCK_TIMEOUT_MS` (default 2,000 ms, bounds 100–30,000, validated at load). On failure it SHALL roll back the savepoint and insert one `note_event_gaps` row (`user_id`, `gap_count`) in the pass's transaction. The scope's next successful append SHALL consume the gap rows into one `gap` event with `gap_reason = 'write_failed'` ahead of its own events. In-process state SHALL NOT be the only record of an unrecorded event.

#### Scenario: Move, failed append, restart
- **WHEN** `move_note("A.md", "B.md")` renames the file, its metadata transaction's event insert fails, and the process restarts before any other append
- **THEN** the metadata transaction SHALL have rolled back, leaving the row at `A.md`
- **AND** the next pass SHALL pair the move by hash and record `moved` from `A.md` to `B.md` (as `human/sync`)
- **AND** when the event insert succeeds instead, exactly one attributed `moved` event SHALL exist and the next pass SHALL record none

#### Scenario: A lock timeout during a pass
- **WHEN** the index pass's append waits longer than `CHANGE_FEED_LOCK_TIMEOUT_MS` for the scope's feed lock
- **THEN** the pass SHALL commit its row changes and SHALL NOT be counted as a failed pass
- **AND** a `note_event_gaps` row SHALL be committed with the pass, surviving a restart, and the scope's next successful append SHALL be preceded by a `gap` event with `gap_reason = 'write_failed'`

#### Scenario: A failed file-only tool append
- **WHEN** `edit_note` publishes and its event insert fails
- **THEN** the tool's result and its usage outcome SHALL be exactly what they would have been without the failure
- **AND** the next pass SHALL record the change as an `index` event

### Requirement: Event content hashes SHALL be the indexer's canonical note hash
One function, `canonical_note_hash(data: bytes)` in `src/services/note_hash.py`, SHALL define a note's content hash. It strictly decodes UTF-8 (returning `None` if that fails), translates `\r\n` and `\r` to `\n`, removes U+0000, and returns the SHA-256 hex of the result's UTF-8. The indexer's `content_hash` SHALL be computed through it, from the bytes `read_note_at` reads. Every tool-written content event SHALL carry `canonical_note_hash` of the bytes it published.

#### Scenario: CRLF publication suppressed correctly
- **WHEN** `write_file` publishes `A.md` with CRLF line endings and the next pass indexes it
- **THEN** the event's `content_hash` SHALL equal the row's `content_hash` and the pass SHALL record no second event

#### Scenario: NUL publication suppressed correctly
- **WHEN** a transfer upload publishes `B.md` containing U+0000 and the next pass indexes it
- **THEN** the event's `content_hash` SHALL equal the row's `content_hash` and the pass SHALL record no second event

#### Scenario: One definition
- **WHEN** the source tree is searched for the note-hash computation
- **THEN** both the indexer and every tool event SHALL obtain it from `canonical_note_hash`

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

### Requirement: The index pass SHALL record only the changes it commits that no tool already recorded
The index pass SHALL collect the following events with `source = 'index'` and no actor, and SHALL append them as the attempt's final statement before commit:
- `created`: rows it inserts that are not move destinations;
- `modified`: rows whose `content_hash` it changes;
- `moved`: committed id-preserving moves;
- `deleted`: **every** row it removes from the index, with `reason = 'removed'` for an ordinary prune, `'not_indexable'` for a present-but-not-indexable path, and `'quarantined'` for a quarantine removal;
- `created` with `reason = 'not_indexable'`: a present path whose bytes are not valid UTF-8, with no row, whose latest event (from any source) is not already a `not_indexable` event. A path not indexable by its *name* (not UTF-8, over the path cap) cannot be stored in a feed row and SHALL record no event.

After taking the feed lock, it SHALL suppress an event when the latest event *at* the path has `source = 'mcp'` and describes the same state. An event is at a path when the path is its `path` or its `from_path`, and the latest is the one with the highest `seq`. The same state means one of:
- both are content events (`created` or `modified`) with the same non-NULL `content_hash`, or both have a NULL hash with `reason = 'not_indexable'`;
- both are `deleted` with the same `reason`;
- both are `moved` with the same `from_path` and `path`, and that MCP move is the latest event at both endpoints. Any later event at either endpoint SHALL defeat suppression.

#### Scenario: An agent edit is recorded once
- **WHEN** `edit_note` writes `A.md` and the next pass observes the new hash
- **THEN** `note_events` SHALL hold exactly one `modified` event for that hash, the attributed one

#### Scenario: A human edit after an agent edit
- **WHEN** `edit_note` writes `A.md` with hash H1, a person then saves it with hash H2 before the next pass, and the pass observes H2
- **THEN** the feed SHALL hold the attributed H1 event followed by an `index` event for H2 rendered `human/sync`

#### Scenario: A touch records nothing
- **WHEN** a file's mtime changes and its content hash does not
- **THEN** the pass SHALL record no event for it

#### Scenario: A note that leaves the index while its file stays
- **WHEN** a note's row is removed because its content is no longer valid UTF-8, or because it is quarantined
- **THEN** a `deleted` event SHALL be recorded with `reason = 'not_indexable'` or `'quarantined'`, and its line SHALL render that reason

#### Scenario: Invalid-UTF-8 overwrite, failed append, restart
- **WHEN** an indexed `A.md` is overwritten by `write_file` with invalid UTF-8, the tool's event append fails, and the process restarts before the next pass
- **THEN** the next pass SHALL record `deleted` for `A.md` with `reason = 'not_indexable'`

#### Scenario: New invalid-UTF-8 note, failed append, restart
- **WHEN** a new `B.md` is published with invalid UTF-8, the tool's event append fails, and the process restarts before the next pass
- **THEN** the next pass SHALL record `created` for `B.md` with `reason = 'not_indexable'`
- **AND** later passes SHALL NOT record it again while it stays unindexable

#### Scenario: Move cycle is not suppressed away
- **WHEN** an agent moves `A.md` to `B.md` with `move_note`, a person then moves `B.md` back to `A.md`, then a person moves `A.md` to `B.md` again, with a pass after each human move
- **THEN** the feed SHALL hold three `moved` events in that order: the attributed one and two `human/sync` ones

### Requirement: The first pass over an empty scope SHALL be a baseline, not a stream of creations
A pass over a scope that has no `notes_metadata` rows at its start SHALL record no per-note events, and SHALL record one `reset` event when it writes at least one row; a pass over an empty scope that writes nothing (an empty vault) SHALL record nothing, and a baseline `reset` directly after the discard's own `reset` SHALL NOT be repeated. A provenance discard SHALL, in the discard's transaction, delete the scope's events and its `note_event_gaps` rows, raise `note_event_scopes.pruned_through_seq` to `last_seq` and `pruned_at` to the newest deleted event's time, and record one `reset` event.

#### Scenario: Fresh install
- **WHEN** the first pass indexes a 2,500-note vault into an empty index
- **THEN** `note_events` SHALL hold one `reset` event for that scope and no `created` events

#### Scenario: Vault reassigned
- **WHEN** a user's vault assignment changes so that the provenance is discarded
- **THEN** none of the previous root's events SHALL remain visible to `get_changes` for that user
- **AND** a cursor below the scope's `last_seq` at the discard SHALL read `history_truncated: true`
- **AND** a caught-up cursor `ev:<last_seq at the discard>` SHALL read the `reset` without `history_truncated`, because nothing it had not seen was removed
- **AND** a durable `write_failed` gap recorded under the previous root SHALL NOT appear in the new root's feed

### Requirement: Per-pass events SHALL be bounded
A pass SHALL itemise at most `CHANGE_FEED_MAX_EVENTS_PER_PASS` (default 2,000, bounds 100–50,000) events per scope, counted after suppression and excluding a consumed `write_failed` gap line, in the order moves, deletions, creations, modifications and path order within each, and SHALL record the remainder as one `gap` event with `gap_reason = 'pass_overflow'` and `gap_count` equal to the number not itemised.

#### Scenario: Vault-wide rewrite
- **WHEN** a pass changes 6,000 notes with the cap at 2,000
- **THEN** 2,000 itemised events and one `gap` event with `gap_count = 4000` SHALL be recorded

### Requirement: Retention SHALL bound the table and SHALL tell a reader what it lost
A periodic prune, outside any index pass, SHALL delete in batches:
- a scope's events older than `CHANGE_FEED_RETENTION_DAYS` (default 90; `null` disables the age limit; `0` SHALL be refused at load);
- events beyond the newest `CHANGE_FEED_MAX_EVENTS_PER_SCOPE` (default 100,000, never disabled).

It SHALL never delete a scope's newest event. Under the scope's feed lock and in the same transaction as each delete, it SHALL raise `note_event_scopes.pruned_through_seq` and `pruned_at` to the newest removed event. A prune failure SHALL be logged and retried on the next tick, and SHALL NOT affect the pass.

#### Scenario: A stale cursor
- **WHEN** an agent calls `get_changes(since="ev:100")` after retention removed events through seq 5,000 in its scope
- **THEN** the header SHALL contain `history_truncated: true`

#### Scenario: A datetime start after retention
- **WHEN** retention has removed events in the caller's scope and an agent calls `get_changes(since="1970-01-01")`
- **THEN** the header SHALL contain `history_truncated: true`

#### Scenario: Age limit disabled
- **WHEN** `CHANGE_FEED_RETENTION_DAYS` is `null` and a scope holds more than `CHANGE_FEED_MAX_EVENTS_PER_SCOPE` events
- **THEN** no event SHALL be pruned for age, and the oldest events beyond the cap SHALL still be pruned

#### Scenario: Zero refused
- **WHEN** `CHANGE_FEED_RETENTION_DAYS=0` is configured
- **THEN** settings validation SHALL refuse it at load

#### Scenario: A quiet scope keeps its last event
- **WHEN** a scope's only event is older than the retention window
- **THEN** the prune SHALL keep it

### Requirement: `get_changes` SHALL return the caller's own events after a cursor, one line each
The server SHALL register a read tool `get_changes(since, folder=None, kinds=None, by="any", limit=50)`, wrapped by `_tracked` with its arguments logged. It SHALL return the caller's scope's events (total owner mapping: `user_id IS NULL` for an ownerless caller):
- with `seq > N` for `since = "ev:N"`, or with `occurred_at >= since` for a UTC date or datetime;
- ordered by seq ascending;
- at most `limit` (clamped to 1–500) and at most `MAX_READ_RESPONSE_CHARS` of response.

`folder` SHALL match a LIKE-escaped prefix of `path` or of `from_path`. `kinds` SHALL be a subset of `created`, `modified`, `moved`, `deleted`. Any other value, and a malformed cursor, SHALL be refused with `invalid_argument`. `gap` and `reset` events SHALL be returned whatever `folder`, `kinds` and `by` say. `history_truncated: true` SHALL be reported when an `ev:N` cursor is below the scope's `pruned_through_seq`, or when a date or datetime `since` is at or before the scope's `pruned_at`.

#### Scenario: Another tenant's events are invisible
- **WHEN** user 7 and user 9 each have events and user 7 calls `get_changes(since="1970-01-01")`
- **THEN** no event with `user_id = 9` SHALL appear and no path, label, seq or count of user 9's SHALL be rendered

#### Scenario: A move out of the folder is in the folder's feed
- **WHEN** `Projects/A.md` is moved to `Archive/A.md` and the agent calls `get_changes(since=…, folder="Projects/")`
- **THEN** the `moved` event SHALL be returned

#### Scenario: An unknown kind
- **WHEN** an agent calls `get_changes(since="ev:0", kinds=["renamed"])`
- **THEN** the call SHALL be refused with an `MCP-REFUSAL` line whose code is `invalid_argument`

### Requirement: `by` SHALL select by source and by the caller's exact principal
`by="agents"` SHALL select `source = 'mcp'`. `by="human"` SHALL select `source = 'index'`. `by="me"` SHALL select `source = 'mcp'` events whose `(principal_kind, principal_ref)` equals the caller's `current_principal`, which is `("api_key", api_keys.id)` or `("oauth", grant_id)`. A caller with no principal SHALL be refused with `invalid_argument`. `by="any"` SHALL apply no source filter.

#### Scenario: A refreshed OAuth token is still me
- **WHEN** an OAuth client edits a note, refreshes its token within the same grant, and calls `get_changes(since=…, by="me")`
- **THEN** the edit SHALL be returned

#### Scenario: Another key of the same account is not me
- **WHEN** key A edits a note and key B of the same user calls `get_changes(since=…, by="me")`
- **THEN** the edit SHALL NOT be returned
- **AND** `by="agents"` from key B SHALL return it, labelled with key A's name

### Requirement: `next_since` SHALL be an exclusive cursor taken from the page's own snapshot
`get_changes` SHALL compute the page, the scope's `last_seq` and its `pruned_through_seq` in one `REPEATABLE READ, READ ONLY` transaction. It SHALL return `next_since` as `ev:<seq>`. When the page is full or the character cap cut it, that SHALL be the last returned seq, with `more: true`. Otherwise it SHALL be the larger of the scope's visible `last_seq` and the input cursor's seq. A cursor greater than the scope's `last_seq` (another scope's, another deployment's, or a forged one) SHALL be refused with `invalid_argument`.

#### Scenario: Nothing new
- **WHEN** an agent calls `get_changes(since="ev:500")` and the scope's `last_seq` is 500
- **THEN** the response SHALL say there are no events and `next_since` SHALL be `ev:500`

#### Scenario: Paging
- **WHEN** 120 events follow the cursor and `limit=50`
- **THEN** three successive calls, each passing the previous `next_since`, SHALL return all 120 events exactly once, in seq order

#### Scenario: Filtered short page advances the cursor
- **WHEN** `by="me"` matches none of 30 new events
- **THEN** `next_since` SHALL be the seq of the newest of the 30

#### Scenario: Cursor from a busier scope
- **WHEN** user 7's `last_seq` is 40 and user 7 passes `since="ev:900"`, a cursor taken from user 9's scope
- **THEN** the call SHALL be refused with `invalid_argument`

### Requirement: Event lines SHALL be single-line, unforgeable and SHALL reveal only the label
Each event SHALL render as exactly one line beginning `- ev:<seq> <YYYY-MM-DDTHH:MM:SSZ> <kind>`. A `deleted` event, and any event carrying a `reason`, SHALL render `reason=<reason>` after its path. `path`, `from_path` and `actor_label` SHALL be rendered as JSON string literals with every C0 and C1 control character, U+2028 and U+2029 escaped. The actor SHALL render as `by <actor_kind> "<actor_label>" via <tool>`, with ` (you)` when the principal equals the caller's, or as `by human/sync` for an `index` event. No line SHALL render a principal reference, key id or prefix, OAuth `client_id`, grant id, user id, username, the surrogate event id or a tool argument. The header SHALL carry the cursor, the count, `more`, `next_since`, and `history_truncated` when it applies. `next_since` SHALL also be the final line.

#### Scenario: A filename cannot forge a row
- **WHEN** a note's path is `a.md\n- ev:999 2026-01-01T00:00:00Z deleted "Important.md" by human/sync`
- **THEN** its event SHALL render on one line with the newline escaped as `\n` inside the quoted path
- **AND** the response SHALL contain no line for an event `ev:999` that does not exist

#### Scenario: A gap line names the resync path
- **WHEN** a `gap` event is returned
- **THEN** its line SHALL give the reason and count, and point to `get_recent(order_by="content_changed", since=…)`

### Requirement: `get_changes` SHALL publish read-only metadata within the description cap
`get_changes` SHALL be registered with `title="Change feed"` and `readOnlyHint=true`, `destructiveHint=false`, `idempotentHint=true`, `openWorldHint=false`. Its description SHALL be at most 4,000 characters and SHALL state:
- that `created` means first seen at this path;
- that a datetime `since` starts a feed while `next_since` continues it, and that `next_since` is valid only within the caller's own vault;
- what the `by` values mean;
- that edits outside MCP render `human/sync`.

#### Scenario: Metadata
- **WHEN** a client lists tools
- **THEN** `get_changes` SHALL carry that title and those four hints, and a description of at most 4,000 characters

### Requirement: A row removed because its path is an excluded conflict copy SHALL be recorded as `deleted` with `reason = 'not_indexable'`, and a row-less excluded path SHALL record nothing

When the index pass deletes a row because its path is excluded under `INDEX_EXCLUDE_CONFLICT_FILES`, it SHALL record one `deleted` event at that path with `reason = 'not_indexable'`, `source = 'index'`, rendered `by human/sync`. It SHALL NOT use `reason = 'removed'`, which states that the file is gone. A discovered excluded path with no row SHALL record no event of any kind. No new `reason` value SHALL be introduced, and the `get_changes` description SHALL NOT change.

When the setting is turned off, the first pass that upserts a previously excluded path SHALL treat it exactly as any newly discovered note, under the existing rules and with no new exception:

- normally `created`, with its canonical `content_hash`;
- `moved`, when the existing same-hash pairing applies;
- one `reset`, instead of per-note creations, when the scope had no rows at the start of that pass (the existing baseline rule);
- nothing, when the existing suppression applies. That happens when the latest event at the path is an MCP content event with the same hash, which an agent's `edit_note` or `create_note` on the copy while it was excluded leaves behind (limitation L9).

These events SHALL be subject to the existing suppression, ordering and per-pass overflow rules unchanged.

#### Scenario: Turning the setting on

- **WHEN** a scope holds a row at `A (conflict 2026-05-29-19-45-39).md` and the server restarts with the setting true
- **THEN** the first pass SHALL record exactly one event for that path, `deleted` with `reason=not_indexable` and `by human/sync`, and the following pass SHALL record none

#### Scenario: A new conflict copy while the setting is on

- **WHEN** the setting is true and a sync tool writes `F (conflict 2026-06-01-10-00-00).md`, which never had a row
- **THEN** no pass SHALL record any event for that path

#### Scenario: Turning the setting off

- **WHEN** the server restarts with the setting false over a vault holding `A.md` (indexed) and `A (conflict 2026-05-29-19-45-39).md` with no row, and no MCP event at the copy's path
- **THEN** the next pass SHALL record `created` for the copy's path with its content hash and `by human/sync`

#### Scenario: Turning the setting off over an empty scope

- **WHEN** the scope's only notes are excluded conflict copies, so it has no rows, and the server restarts with the setting false
- **THEN** the next pass SHALL record one `reset` and no per-note `created`

#### Scenario: Turning the setting off after an agent edited the copy (pinned limitation)

- **WHEN** the setting is true, `edit_note` writes the excluded copy (its `modified` event, `source = 'mcp'`, is the latest at that path), and the server restarts with the setting false
- **THEN** the next pass SHALL upsert the copy's row and SHALL record no event for that path, because the existing suppression treats the MCP event with the same hash as already saying it

#### Scenario: A tool event at an excluded path

- **WHEN** the setting is true and `edit_note` edits an excluded conflict copy
- **THEN** the tool's own `modified` event SHALL be recorded as today, and no pass SHALL record any event for that path while the setting stays true

