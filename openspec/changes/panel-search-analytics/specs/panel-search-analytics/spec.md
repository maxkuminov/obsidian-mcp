## ADDED Requirements

### Requirement: Search result telemetry
`keyword_search`, `semantic_search`, and `find_related` SHALL record into their logged params the final `result_count` and the returned note paths capped at 10 per call, without altering tool responses or existing param keys.

#### Scenario: Count and paths logged
- **WHEN** a semantic_search returns 4 notes
- **THEN** its usage_logs row's params include result_count 4 and those 4 paths

#### Scenario: Zero-result call
- **WHEN** a search returns nothing
- **THEN** result_count 0 is logged and the row carries no error marker

### Requirement: Search analytics views
The panel SHALL provide an admin search-analytics page over a selectable window (24 hours, 7 days, 30 days) showing per search tool: the most frequent queries with call counts and mean result count, and the zero-result queries with counts.

#### Scenario: Top and zero-result queries
- **WHEN** the operator selects a window containing search calls
- **THEN** top queries and zero-result queries for that window are listed per tool, excluding error-marked rows

### Requirement: Retrieval coverage
The analytics page SHALL show the most-retrieved notes over the window and the notes never appearing in logged results, and SHALL state in the page copy that the per-call path cap makes never-retrieved an upper bound.

#### Scenario: Coverage lists
- **WHEN** the coverage section renders for a window
- **THEN** most-retrieved notes are ranked by appearance count and the never-retrieved list contains only indexed notes absent from every logged result of the window, with the caveat displayed
