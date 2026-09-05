## MODIFIED Requirements

### Requirement: Filtered usage views

The usage page SHALL support combined filtering by user, API key, tool, and window (24 hours, 7 days, 30 days), applying the filters to both the chart and the request log, SHALL show per-actor request totals for the filtered window including actors whose credential was since deleted (via the denormalized attribution columns), and SHALL show, for each row of the request log, the outcome recorded in that row's `params` — a refusal (with its reason code) for a row carrying an error marker or the over-quota marker, a failure (with the exception class name) for a row carrying the tool-exception marker, and nothing at all for an ordinary call. The outcome SHALL be read as text, never through an unguarded SQL cast, and an unrecognised marker SHALL render as its own value rather than as a blank or an error.

#### Scenario: Filter by key
- **WHEN** the operator filters to one API key over 7 days
- **THEN** chart, log, and totals reflect only that key's rows

#### Scenario: Deleted actor history
- **WHEN** no filter is set and rows exist from a deleted key
- **THEN** those rows appear attributed by their denormalized labels

#### Scenario: A refused write is visible on the page
- **WHEN** a read-only credential has called a write tool and the operator opens the usage page
- **THEN** that row SHALL be rendered as a refusal naming `permission_denied`, distinguishable at a glance from a successful call to the same tool by the same actor

#### Scenario: A malformed marker does not take the page down
- **WHEN** a row carries an unexpected value under the over-quota or error key
- **THEN** the page SHALL render for the whole window without error
