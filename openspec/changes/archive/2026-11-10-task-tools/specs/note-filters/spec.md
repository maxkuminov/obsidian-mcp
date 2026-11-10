## MODIFIED Requirements

### Requirement: Read-path owner scoping is total

The shared filter helper SHALL treat `user_id=None` as a scoping value, not as the absence of one: it SHALL append `notes_metadata.user_id IS NULL` when `user_id` is None, and `notes_metadata.user_id = :uid` when it is an integer.

Every index-backed read SHALL scope its owner predicate by this same total mapping, so a caller with no user identifier can never read rows owned by a named user. That covers these tools and lookups:

- `keyword_search`, `semantic_search`, `list_notes`, `get_recent` and `get_tags`;
- `get_backlinks`, `get_links`, `get_neighborhood`, `find_related` and `find_orphans`;
- `list_tasks` and `task_agenda`;
- `get_changes` (over `note_events.user_id`);
- the index lookups of the task write tools: `add_task`'s duplicate and similarity scans, and the write-time task refresh's row lookup.

**Joins.** For the graph tools the mapping SHALL be closed over joins: any query that resolves `note_links.source_note_id` or `target_note_id` to a `notes_metadata` row SHALL require that row to be inside the caller's owned set as part of the join condition, not as a post-filter that would drop dangling-link rows. `list_tasks`, `task_agenda` and the task write tools' index lookups SHALL resolve `note_tasks.note_id` to its `notes_metadata` row under the same mapping, as part of the join.

**Graph membership.** Neighborhood and orphan computations SHALL NOT let an edge whose other endpoint is outside the owned set influence membership, distance or orphan status.

#### Scenario: Ownerless read on a mixed database returns only NULL-owned rows

- **WHEN** the database holds rows with `user_id IS NULL` and rows owned by named users, and any of the enumerated read tools runs with no user identifier (`current_user_id` unset)
- **THEN** the query SHALL apply `user_id IS NULL` and return only NULL-owned rows
- **AND** no named user's paths, titles, tags, frontmatter, or excerpts SHALL appear in the result

#### Scenario: get_tags does not count another owner's tags

- **WHEN** a named user's notes carry a tag that no NULL-owned note carries, and `get_tags` runs with no user identifier
- **THEN** that tag SHALL NOT appear in the result

#### Scenario: A cross-owner link never surfaces the other owner's row

- **WHEN** a NULL-owned note's link row resolves (`target_note_id`) to a note owned by a named user, and `get_links`, `get_neighborhood`, or `find_orphans` runs with no user identifier
- **THEN** the named user's title and path SHALL NOT appear in any result, the cross-owner edge SHALL NOT contribute to neighborhood membership or distance, and SHALL NOT change any note's orphan status
- **AND** dangling links (no resolved target) SHALL still be reported where the tool reports them today

#### Scenario: Single-user deployment behavior is unchanged

- **WHEN** every row in the database has `user_id IS NULL` (a single-user deployment) and a read tool runs with no user identifier
- **THEN** the result set SHALL be identical to the pre-change behavior

#### Scenario: Named-user scoping is unchanged

- **WHEN** a read tool runs with `user_id=7`
- **THEN** the query SHALL apply `user_id = 7` exactly as before

#### Scenario: list_tasks does not list another owner's tasks

- **WHEN** a named user's notes hold open tasks and `list_tasks` runs with no user identifier
- **THEN** none of those tasks, paths or headings SHALL appear in the result, and neither SHALL they be counted in its header

#### Scenario: task_agenda and add_task stay in their owner scope

- **WHEN** user 8's notes hold overdue tasks and a task identical to one user 7 adds, and user 7 calls `task_agenda` and `add_task`
- **THEN** no user-8 task SHALL appear in the agenda, in its counts, in a `duplicate_task` refusal or in the similar-task list

#### Scenario: get_changes on a mixed database

- **WHEN** `note_events` holds NULL-owned events and events owned by named users, and `get_changes` runs with no user identifier
- **THEN** only NULL-owned events SHALL be returned

#### Scenario: Cursor bound to its owner
- **WHEN** user 7 passes a cursor that user 9's identical `list_notes(order_by="content_changed")` call returned
- **THEN** the call SHALL be refused with `invalid_argument`
- **AND** neither user's id SHALL appear in the cursor's decoded payload or in any response
