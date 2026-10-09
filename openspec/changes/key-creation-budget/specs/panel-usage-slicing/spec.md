## MODIFIED Requirements

### Requirement: Quota administration

The keys UI SHALL allow setting and changing a key's `daily_request_limit` at create and edit time, and SHALL display each limited key's consumed count for the current UTC day alongside its limit. It SHALL pre-fill the create form's limit field with the configured `DEFAULT_DAILY_REQUEST_LIMIT`; the keys page handler passes that value to the template and leaves the field empty when no default is configured.

A **blank submitted create field SHALL receive the configured default**, and SHALL NOT create an unlimited key. When no default is configured, a blank field SHALL be refused as a missing required limit.

The create form and the edit-limit form SHALL each carry an "Unlimited" checkbox (`name="unlimited"`, `value="1"`), rendered **only for administrators**. It is the only panel control that produces or restores an unlimited key. While it is ticked, the numeric field SHALL be disabled by a delegated `data-unlimited-toggle` listener in `panel.js`, under the panel CSP rules: no inline handler, no inline style, no `!important`. The edit form SHALL open with the box ticked when the key is currently unlimited.

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
