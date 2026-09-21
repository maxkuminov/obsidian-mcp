## MODIFIED Requirements

### Requirement: Filtered usage views

The usage page SHALL support combined filtering by user, API key, tool, and window (24 hours, 7 days, 30 days), applying the filters to both the chart and the request log, SHALL show per-actor request totals for the filtered window including actors whose credential was since deleted (via the denormalized attribution columns), and SHALL show, for each row of the request log, an outcome derived from that row's recorded markers. The query SHALL select the marker values as raw text — the error marker, the exception class name, and the over-quota value — with no SQL cast, and the mapping to a displayed outcome SHALL happen in the route with a declared precedence: a `tool_exception` marker renders as a failure carrying the exception class name; any other error marker renders as a refusal carrying that marker as its reason; an over-quota value equal to `true` renders as a refusal for quota; any other non-empty value renders as a refusal showing the raw value; and a row with none of them renders no outcome at all. No selected value SHALL be discarded between the query and the template.

#### Scenario: Filter by key
- **WHEN** the operator filters to one API key over 7 days
- **THEN** chart, log, and totals reflect only that key's rows

#### Scenario: Deleted actor history
- **WHEN** no filter is set and rows exist from a deleted key
- **THEN** those rows appear attributed by their denormalized labels

#### Scenario: A refused write is visible on the page
- **WHEN** a read-only credential has called a write tool and the operator opens the usage page
- **THEN** that row SHALL be rendered as a refusal naming `permission_denied`, distinguishable at a glance from a successful call to the same tool by the same actor

#### Scenario: A failed call is distinguishable from a refused one
- **WHEN** a row carries the `tool_exception` marker and an exception class name
- **THEN** the page SHALL render it as a failure showing that class name, not as a refusal

#### Scenario: A malformed marker does not take the page down
- **WHEN** a row carries a value under the over-quota key that is neither `true` nor `false`
- **THEN** the page SHALL render for the whole window without error and SHALL show that row's raw value rather than treating the row as ordinary
