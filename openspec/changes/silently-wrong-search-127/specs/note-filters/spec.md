## ADDED Requirements

### Requirement: Read-path owner scoping is total

The shared filter helper SHALL treat `user_id=None` as a scoping value, not as the absence of one: it SHALL append `notes_metadata.user_id IS NULL` when `user_id` is None and `notes_metadata.user_id = :uid` when it is an integer. Every index-backed read tool — `keyword_search`, `semantic_search`, `list_notes`, `get_recent`, `get_tags`, `get_backlinks`, `get_links`, `get_neighborhood`, `find_related`, and `find_orphans` — SHALL scope its owner predicate by this same total mapping, so a caller with no user identifier can never read rows owned by a named user.

For the graph tools the mapping SHALL be closed over joins: any query that resolves `note_links.source_note_id` or `target_note_id` to a `notes_metadata` row SHALL require that row to be inside the caller's owned set as part of the join condition (not as a post-filter that would drop dangling-link rows), and neighborhood and orphan computations SHALL NOT let an edge whose other endpoint is outside the owned set influence membership, distance, or orphan status.

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

## MODIFIED Requirements

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

### Requirement: `get_recent` accepts tag and frontmatter filters

The `get_recent` MCP tool SHALL accept optional `tags` and `frontmatter` parameters in addition to the existing `limit` and `folder`, and route folder/tag/frontmatter filtering through the shared filter helper.

#### Scenario: Existing call without new parameters

- **WHEN** an existing client calls `get_recent(limit=20, folder="Projects/")`
- **THEN** behavior SHALL be identical to the pre-change implementation
- **AND** on a database containing rows owned by named users, the result SHALL additionally be scoped by the total owner mapping (an ownerless call reads only `user_id IS NULL` rows); "identical" is promised only for a database whose rows are all NULL-owned

#### Scenario: Recent notes filtered by tag

- **WHEN** an agent calls `get_recent(limit=10, tags=["meeting"])`
- **THEN** the system SHALL return up to 10 notes tagged `meeting`, ordered by `modified_at` descending

