# note-filters Specification

## Purpose
How the index-backed read tools select and order notes: the one shared `folder` / `tags` / `frontmatter` filter helper, the filters each search and listing tool accepts, the total owner-scoping rule that keeps an ownerless caller from reading a named user's rows, and (#351) the `since` / `until` / `order_by` / `cursor` window on `get_recent` and `list_notes` with its UTC bound parser and owner-bound keyset cursor.
## Requirements
### Requirement: Shared filter helper

The system SHALL provide a single helper function that applies optional `folder`, `tags`, and `frontmatter` filters to a SQLAlchemy `select` over `NoteMetadata`.

#### Scenario: Folder prefix filter with LIKE escape

- **WHEN** the helper is invoked with `folder="Projects/100%/"`
- **THEN** the helper SHALL escape LIKE wildcards (`%`, `_`, `\`) in the folder string and apply a `file_path LIKE 'Projects/100\%/%' ESCAPE '\'` predicate
- **AND** the helper SHALL not apply a folder predicate when `folder` is None or empty string

#### Scenario: Tag filter uses ARRAY containment

- **WHEN** the helper is invoked with `tags=["project", "active"]`
- **THEN** the helper SHALL apply a predicate equivalent to `notes_metadata.tags @> ARRAY['project','active']::text[]`
- **AND** the helper SHALL not apply a tag predicate when `tags` is None or an empty list

#### Scenario: Frontmatter filter uses JSONB containment

- **WHEN** the helper is invoked with `frontmatter={"status": "draft", "type": "card"}`
- **THEN** the helper SHALL apply a predicate equivalent to `notes_metadata.frontmatter @> '{"status":"draft","type":"card"}'::jsonb`
- **AND** the helper SHALL not apply a frontmatter predicate when `frontmatter` is None or an empty dict

#### Scenario: Multiple filters AND-combined

- **WHEN** the helper is invoked with non-empty `folder`, `tags`, and `frontmatter`
- **THEN** all three predicates SHALL be applied with AND semantics

### Requirement: `keyword_search` accepts tag and frontmatter filters

The `keyword_search` MCP tool SHALL accept optional `tags: list[str]` and `frontmatter: dict[str, str | int | float | bool]` parameters in addition to the existing `query`, `folder`, and `limit`.

#### Scenario: Existing call without new parameters

- **WHEN** an existing client calls `keyword_search(query="docker", folder="Projects/", limit=10)` without supplying `tags` or `frontmatter`
- **THEN** behavior SHALL be identical to the pre-change implementation, modulo the underlying query refactor
- **AND** on a database containing rows owned by named users, the result SHALL additionally be scoped by the total owner mapping (an ownerless call reads only `user_id IS NULL` rows); "identical" is promised only for a database whose rows are all NULL-owned

#### Scenario: Tag filter applied

- **WHEN** an agent calls `keyword_search(query="status update", tags=["active"])`
- **THEN** the system SHALL only return notes whose `tags` array contains `active` AND match the tsvector query
- **AND** results SHALL still be ordered by `ts_rank_cd` descending

#### Scenario: Frontmatter filter applied

- **WHEN** an agent calls `keyword_search(query="quarterly", frontmatter={"status": "draft"})`
- **THEN** the system SHALL only return notes whose JSONB frontmatter contains `status: "draft"` AND match the tsvector query

### Requirement: `semantic_search` accepts tag and frontmatter filters

The `semantic_search` MCP tool SHALL accept optional `tags: list[str]` and `frontmatter: dict[str, str | int | float | bool]` parameters in addition to the existing `query`, `folder`, and `limit`.

#### Scenario: Existing call without new parameters

- **WHEN** an existing client calls `semantic_search(query="x", folder="Projects/")` without supplying `tags` or `frontmatter`
- **THEN** behavior SHALL be identical to the pre-change implementation
- **AND** on a database containing rows owned by named users, the result SHALL additionally be scoped by the total owner mapping (an ownerless call reads only `user_id IS NULL` rows); "identical" is promised only for a database whose rows are all NULL-owned

#### Scenario: Combined filters

- **WHEN** an agent calls `semantic_search(query="ideas about onboarding", tags=["product"], frontmatter={"status": "active"})`
- **THEN** the system SHALL apply all filters at the SQL level before ordering by cosine distance and applying the `limit`

### Requirement: `list_notes` accepts tag and frontmatter filters and reads from the index

The `list_notes` MCP tool SHALL read note metadata from `notes_metadata` (rather than walking the filesystem) and SHALL accept optional `tags` and `frontmatter` parameters in addition to the existing `folder` and `limit`.

#### Scenario: Listing without filters

- **WHEN** an agent calls `list_notes()` with no arguments
- **THEN** the system SHALL return up to 50 notes ordered by `modified_at` descending, each with `path`, `size`, and `modified` fields preserving the existing response shape

#### Scenario: Listing with folder filter

- **WHEN** an agent calls `list_notes(folder="Cards/")`
- **THEN** the system SHALL return only notes whose `file_path` starts with `Cards/`

#### Scenario: Listing with tag and frontmatter filters

- **WHEN** an agent calls `list_notes(folder="Cards/", tags=["idea"], frontmatter={"status": "active"})`
- **THEN** the system SHALL apply all three filters and return matching notes ordered by `modified_at` descending

#### Scenario: Newly created on-disk note not yet indexed

- **WHEN** a note exists on disk but has not yet been picked up by the indexer
- **THEN** `list_notes` SHALL not return that note until the next index pass completes

### Requirement: `get_recent` accepts tag and frontmatter filters

The `get_recent` MCP tool SHALL accept optional `tags` and `frontmatter` parameters in addition to the existing `limit` and `folder`, and route folder/tag/frontmatter filtering through the shared filter helper.

#### Scenario: Existing call without new parameters

- **WHEN** an existing client calls `get_recent(limit=20, folder="Projects/")`
- **THEN** behavior SHALL be identical to the pre-change implementation
- **AND** on a database containing rows owned by named users, the result SHALL additionally be scoped by the total owner mapping (an ownerless call reads only `user_id IS NULL` rows); "identical" is promised only for a database whose rows are all NULL-owned

#### Scenario: Recent notes filtered by tag

- **WHEN** an agent calls `get_recent(limit=10, tags=["meeting"])`
- **THEN** the system SHALL return up to 10 notes tagged `meeting`, ordered by `modified_at` descending

### Requirement: Symmetric, non-ranking docstrings on the two search tools

The system SHALL document `keyword_search` and `semantic_search` so that neither is described as the "primary" tool. Each docstring SHALL state the tool's intended use case and direct the agent to the other tool when its use case fits better.

#### Scenario: `keyword_search` docstring

- **WHEN** an MCP client lists tools
- **THEN** the `keyword_search` description SHALL identify it as the right choice for exact identifiers, proper nouns, code symbols, and known phrases
- **AND** SHALL NOT describe itself as the primary or default search tool
- **AND** SHALL mention `semantic_search` as the alternative for conceptual queries

#### Scenario: `semantic_search` docstring

- **WHEN** an MCP client lists tools
- **THEN** the `semantic_search` description SHALL identify it as the right choice for conceptual or paraphrased queries
- **AND** SHALL NOT describe itself as the primary or default search tool
- **AND** SHALL mention `keyword_search` as the alternative for exact-match queries

### Requirement: Usage logs capture new parameters

Calls that include the new `tags` or `frontmatter` parameters SHALL record them in `usage_logs.params` (subject to the existing string-truncation behavior of `_tracked`).

#### Scenario: Tag filter logged

- **WHEN** an agent calls `semantic_search(query="x", tags=["a","b"])`
- **THEN** the corresponding `usage_logs` row SHALL have `params` containing both `query` and the `tags` list

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

### Requirement: `get_recent` and `list_notes` SHALL accept `since`, `until`, `order_by` and `cursor`
`get_recent` and `list_notes` SHALL accept the optional parameters `since: str`, `until: str`, `order_by: str` (`"mtime"`, the default, or `"content_changed"`) and `cursor: str`.
- `order_by` SHALL select both the ordering column and the column the bounds apply to: `modified_at` for `"mtime"`, `content_changed_at` for `"content_changed"`.
- Ordering SHALL be that column descending, with `file_path` ascending as the tie-break.
- `since` SHALL be inclusive and `until` exclusive. A row whose selected column is NULL SHALL be excluded whenever any of `since`, `until`, `order_by` and `cursor` is given — `order_by` alone included — so that a first page and the pages its cursor continues select the same rows.
- Any other `order_by` value SHALL be refused with `invalid_argument`.
- `get_recent` SHALL label the time it shows `modified` or `changed` according to `order_by`.
- The published descriptions of both tools SHALL state that notes whose content changed are found with `order_by="content_changed"` and `since`.

A call that supplies none of `since`, `until`, `order_by` and `cursor` SHALL issue the same query as before this change and return byte-identical output, with no footer.

#### Scenario: A touch does not reorder content_changed
- **WHEN** 17 notes are touched (mtime changed, content hash unchanged) after note X's content last changed, and an agent calls `get_recent(limit=5, order_by="content_changed")`
- **THEN** none of the 17 touched notes SHALL rank above X on account of the touch

#### Scenario: Window over content changes
- **WHEN** an agent calls `list_notes(folder="Projects/", order_by="content_changed", since="2026-10-20", until="2026-10-25")`
- **THEN** only notes under `Projects/` whose `content_changed_at` is at or after 2026-10-20T00:00Z and before 2026-10-26T00:00Z SHALL be returned

#### Scenario: The docstrings point to the content-change call
- **WHEN** the `get_recent` and `list_notes` descriptions are inspected
- **THEN** each SHALL contain `order_by="content_changed"` and `since`

#### Scenario: Existing call byte-identical
- **WHEN** an existing client calls `get_recent(limit=20, folder="Projects/")` over 21 matching notes
- **THEN** the response SHALL be byte-identical to the pre-change implementation's, with no paging footer

### Requirement: UTC bounds SHALL be parsed by one helper that refuses ambiguity
One parser SHALL serve `since` and `until` on `get_recent`, `list_notes` and `get_changes`.
- It SHALL accept `YYYY-MM-DD`, meaning the UTC day: `since` is at or after 00:00Z that day, and `until` is before 00:00Z the next day.
- It SHALL accept an ISO 8601 datetime with an explicit `Z` or `±HH:MM` offset, normalised to UTC.
- It SHALL refuse with `invalid_argument` a datetime without an offset, an unparseable value, and a `since` that is not earlier than `until`.
- A `get_changes` cursor (`ev:N`) SHALL be accepted only by `get_changes`. The other tools SHALL refuse it and name `get_changes`.

#### Scenario: Naive datetime refused
- **WHEN** an agent calls `get_recent(since="2026-10-25T09:00:00")`
- **THEN** the call SHALL be refused with an `MCP-REFUSAL` line whose code is `invalid_argument`, saying that an offset (`Z` or `±HH:MM`) is required

#### Scenario: Offset normalised
- **WHEN** an agent passes `since="2026-10-25T09:00:00+02:00"`
- **THEN** the bound SHALL be 2026-10-25T07:00:00Z

#### Scenario: Date-only until includes the day
- **WHEN** an agent passes `until="2026-10-25"`
- **THEN** a row at 2026-10-25T23:59:59Z SHALL be included and a row at 2026-10-26T00:00:00Z SHALL NOT

### Requirement: Paging of `get_recent` and `list_notes` SHALL use a keyset cursor that reaches every row
When a call uses any of `since`, `until`, `order_by` or `cursor`, returns `limit` rows, and more rows match, the response SHALL end with a footer `more: cursor=<opaque>`.
- The cursor SHALL share `list_tasks`' cursor envelope (versioned, unpadded base64url JSON) and its owner discriminator, so that there is one codec, not two.
- It SHALL encode a version, the `order_by`, the last row's `(selected time with microseconds, file_path)`, a digest of the call's `folder`, `tags`, `frontmatter`, `since` and `until`, and the owner discriminator `list_tasks` computes: the first 16 hex of SHA-256 over `owner:<user_id>` (`owner:null` for the NULL scope). No owner id SHALL be rendered.
- The next page SHALL select rows with `column < t OR (column = t AND file_path > p)`, in the same order.
- A cursor whose version, `order_by`, filter digest or owner discriminator does not match the call, or that does not decode, SHALL be refused with `invalid_argument`.
- Calls that use none of the four parameters SHALL show no footer.

#### Scenario: A tie group larger than a page
- **WHEN** 501 notes have the same `content_changed_at` and an agent pages `list_notes(order_by="content_changed", limit=500)` by following the footer's cursor
- **THEN** the two pages together SHALL return all 501 notes exactly once, in `file_path` order

#### Scenario: Cursor bound to its filters
- **WHEN** an agent passes a cursor from a `folder="Projects/"` listing to a call with `folder="Cards/"`
- **THEN** the call SHALL be refused with `invalid_argument`

