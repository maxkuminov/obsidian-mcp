## ADDED Requirements

### Requirement: Filtered usage views
The usage page SHALL support combined filtering by user, API key, tool, and window (24 hours, 7 days, 30 days), applying the filters to both the chart and the request log, and SHALL show per-actor request totals for the filtered window including actors whose credential was since deleted (via the denormalized attribution columns).

#### Scenario: Filter by key
- **WHEN** the operator filters to one API key over 7 days
- **THEN** chart, log, and totals reflect only that key's rows

#### Scenario: Deleted actor history
- **WHEN** no filter is set and rows exist from a deleted key
- **THEN** those rows appear attributed by their denormalized labels

### Requirement: Quota administration
The keys UI SHALL allow setting, changing, and clearing a key's `daily_request_limit` at create and edit time, and SHALL display each limited key's consumed count for the current UTC day alongside its limit.

#### Scenario: Set and observe
- **WHEN** the operator sets limit 500 on a key that has made 12 calls today
- **THEN** the keys page shows 12/500 for it, and clearing the limit returns the key to unlimited
