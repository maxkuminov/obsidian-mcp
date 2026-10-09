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

The keys UI SHALL allow setting and changing a key's `daily_request_limit` at create and edit time, and SHALL display each limited key's consumed count for the current UTC day alongside its limit. It SHALL pre-fill the create form's limit field with the configured `DEFAULT_DAILY_REQUEST_LIMIT`; the keys page handler passes that value to the template and leaves the field empty when no default is configured.

A **blank submitted create field SHALL receive the configured default**, and SHALL NOT create an unlimited key. When no default is configured, a blank field SHALL be refused as a missing required limit.

The create form and the edit-limit form SHALL each carry an "Unlimited" checkbox (`name="unlimited"`, `value="1"`), rendered **only for administrators**. It is the only panel control that produces or restores an unlimited key. While it is ticked, the numeric field SHALL be disabled by a delegated `data-unlimited-toggle` listener in `panel.js`, under the panel CSP rules: no inline handler, no inline style, no `!important`. For an administrator, the edit form SHALL open with the box ticked when the key is currently unlimited. For a non-admin the box is absent, so the edit form SHALL open with an empty, enabled numeric field, and the `panel.js` editor code SHALL tolerate the absent toggle. A non-admin MAY assign a numeric limit to their own currently unlimited key.

A blank edit field without the checkbox SHALL be a validation error that leaves the key unchanged. The edit path SHALL NOT apply the default to an existing key.

The help text SHALL state that a blank create field receives the default, and that only an administrator can make a key unlimited.

#### Scenario: Set and observe
- **WHEN** the operator sets limit 500 on a key that has made 12 calls earlier today, before the limit existed
- **THEN** the keys page shows 0/500 (consumption counts admissions since the limit was enabled — unlimited keys perform no quota accounting) with copy making the "since limit set" basis explicit, subsequent calls count up from there, and an administrator ticking Unlimited in the editor returns the key to unlimited

#### Scenario: The create form offers the default
- **WHEN** an account opens the create-key form with `DEFAULT_DAILY_REQUEST_LIMIT` configured
- **THEN** the limit field is pre-filled with that value, and submitting the form unchanged creates a key carrying that limit

#### Scenario: Clearing the pre-filled default yields the default
- **WHEN** an account clears the pre-filled limit field and submits without ticking Unlimited
- **THEN** the created key carries `DEFAULT_DAILY_REQUEST_LIMIT`

#### Scenario: The Unlimited control is admin-only
- **WHEN** a non-admin account opens the keys page
- **THEN** neither the create form nor the edit-limit form contains the `unlimited` field
- **AND** when an administrator opens it, both forms contain it

#### Scenario: A non-admin limits a grandfathered unlimited key
- **WHEN** a non-admin opens the limit editor for their own existing unlimited key
- **THEN** no Unlimited control is present, the numeric field is empty and enabled
- **AND** saving a number sets that limit on the key

#### Scenario: Ticking Unlimited disables the number field
- **WHEN** an administrator ticks Unlimited in either form
- **THEN** the numeric limit field is disabled until the box is unticked, and no Content-Security-Policy violation is reported in the browser console

#### Scenario: No default configured makes the field required
- **WHEN** `DEFAULT_DAILY_REQUEST_LIMIT` is null and an account submits the create form with the limit field empty and Unlimited not ticked
- **THEN** no key is created and a flashed error says a limit is required

#### Scenario: Editing an existing key never applies the default
- **WHEN** an account opens the limit editor for an existing unlimited key and cancels
- **THEN** the key remains unlimited and no default is substituted
- **AND** when the account instead saves with the field empty and Unlimited not ticked, the key's limit is unchanged and a flashed error is shown

