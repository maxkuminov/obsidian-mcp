## ADDED Requirements

### Requirement: Search result telemetry
`keyword_search`, `semantic_search`, and `find_related` SHALL record into their logged params the final `result_count` and the returned note paths under an explicit telemetry contract — at most the first 10 paths and at most 2048 bytes total for the paths value, dropping paths from the end to fit, applied at the record site after any merge into logged params (the generic param truncation does not cover merged telemetry) — without altering tool responses or existing param keys.

#### Scenario: Count and paths logged
- **WHEN** a semantic_search returns 4 notes
- **THEN** its usage_logs row's params include result_count 4 and those 4 paths

#### Scenario: Zero-result call
- **WHEN** a search executes successfully and returns nothing
- **THEN** result_count 0 is logged and the row carries no error marker

#### Scenario: Operational failures are not zero results
- **WHEN** find_related is called on a missing or not-yet-embedded source note
- **THEN** the row carries an error marker (added to those return paths, which today return plain strings unmarked) and is excluded from zero-result analytics, while a valid source with no neighbors remains a true zero-result row

### Requirement: Search analytics views
The panel SHALL provide an admin search-analytics page over a selectable window (24 hours, 7 days, 30 days) showing: for `keyword_search` and `semantic_search`, the most frequent queries with call counts and mean result count, and the zero-result queries with counts; for `find_related`, the same tables grouped and labeled by source note path (it has no query), using a full source path recorded through the telemetry contract (the tool's named `path` param is truncated in logs and unsuitable for grouping). All groupings and coverage joins SHALL scope identity to (usage_logs.user_id, path) with NULL-safe owner matching, since identical paths can exist for different users. Error-marked and refusal-marked rows are excluded.

#### Scenario: Top and zero-result queries
- **WHEN** the operator selects a window containing search calls
- **THEN** top queries and zero-result queries for that window are listed per query tool, and find_related activity appears grouped by source path

#### Scenario: find_related analytics
- **WHEN** find_related was called on the same source note five times, once with zero results
- **THEN** the find_related table shows that path with five calls and its zero-result occurrence is counted in the zero-result view

### Requirement: Retrieval coverage
The analytics page SHALL present retrieval coverage as top-logged retrievals — explicitly labeled as counting appearances within each call's first 10 logged results — plus the notes never appearing in any logged result, and SHALL display the logging-cap caveat beside BOTH the ranking and the never-retrieved list.

#### Scenario: Coverage lists
- **WHEN** the coverage section renders for a window
- **THEN** the ranking is labeled as top-10-logged appearances with the caveat shown, and the never-retrieved list contains only indexed notes absent from every logged result of the window, also carrying the caveat
