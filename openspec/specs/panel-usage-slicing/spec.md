# panel-usage-slicing Specification

## Purpose
TBD - created by archiving change panel-usage-slicing-quotas. Update Purpose after archive.
## Requirements
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

### Requirement: Quota administration
The keys UI SHALL allow setting, changing, and clearing a key's `daily_request_limit` at create and edit time, SHALL display each limited key's consumed count for the current UTC day alongside its limit, and SHALL pre-fill the create form's limit field with the configured `DEFAULT_DAILY_REQUEST_LIMIT` — the keys page handler passing that value to the template, leaving the field empty when no default is configured — with help text stating that the value is a default the operator may change or clear and that an empty field means unlimited. A **blank submitted field SHALL create an unlimited key**, and the create handler SHALL NOT substitute the configured default for a blank submission: the pre-filled form is the only place the default is applied, so the operator's last view of the field is what the key receives. The edit path SHALL be unchanged and SHALL NOT apply the default to an existing key.

#### Scenario: Set and observe
- **WHEN** the operator sets limit 500 on a key that has made 12 calls earlier today, before the limit existed
- **THEN** the keys page shows 0/500 (consumption counts admissions since the limit was enabled — unlimited keys perform no quota accounting) with copy making the "since limit set" basis explicit, subsequent calls count up from there, and clearing the limit returns the key to unlimited

#### Scenario: The create form offers the default
- **WHEN** the operator opens the create-key form with `DEFAULT_DAILY_REQUEST_LIMIT` configured
- **THEN** the limit field is pre-filled with that value, the help text says it is a default and that an empty field means unlimited, and submitting the form unchanged creates a key carrying that limit

#### Scenario: Clearing the pre-filled default creates an unlimited key
- **WHEN** the operator clears the pre-filled limit field and submits
- **THEN** the created key is unlimited and no default is substituted on the server side

#### Scenario: No default configured leaves the field empty
- **WHEN** `DEFAULT_DAILY_REQUEST_LIMIT` is null and the operator opens the create form
- **THEN** the limit field is empty and submitting it unchanged creates an unlimited key

#### Scenario: Editing an existing key never applies the default
- **WHEN** the operator opens the limit editor for an existing unlimited key and cancels, or clears the field and saves
- **THEN** the key remains unlimited and no default is substituted

