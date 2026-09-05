## MODIFIED Requirements

### Requirement: Quota administration
The keys UI SHALL allow setting, changing, and clearing a key's `daily_request_limit` at create and edit time, SHALL display each limited key's consumed count for the current UTC day alongside its limit, and SHALL pre-fill the create form's limit field with the configured `DEFAULT_DAILY_REQUEST_LIMIT` (leaving the field empty when no default is configured) with help text stating that the value is a default the operator may change or clear and that clearing it means unlimited. The edit path SHALL be unchanged and SHALL NOT apply the default to an existing key.

#### Scenario: Set and observe
- **WHEN** the operator sets limit 500 on a key that has made 12 calls earlier today, before the limit existed
- **THEN** the keys page shows 0/500 (consumption counts admissions since the limit was enabled — unlimited keys perform no quota accounting) with copy making the "since limit set" basis explicit, subsequent calls count up from there, and clearing the limit returns the key to unlimited

#### Scenario: The create form offers the default
- **WHEN** the operator opens the create-key form with `DEFAULT_DAILY_REQUEST_LIMIT` configured
- **THEN** the limit field is pre-filled with that value, the help text says it is a default and that an empty field means unlimited, and submitting the form unchanged creates a key carrying that limit

#### Scenario: Clearing the pre-filled default creates an unlimited key
- **WHEN** the operator clears the pre-filled limit field and submits
- **THEN** the created key is unlimited

#### Scenario: Editing an existing key never applies the default
- **WHEN** the operator opens the limit editor for an existing unlimited key and cancels, or clears the field and saves
- **THEN** the key remains unlimited and no default is substituted
