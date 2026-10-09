## MODIFIED Requirements

### Requirement: New API keys receive a configurable default daily request limit

Key creation SHALL apply `DEFAULT_DAILY_REQUEST_LIMIT` (default 5,000) as the `daily_request_limit` of a newly created key whenever the creator did not choose a value. It SHALL do so in application code rather than as a database column default, so that keys created before this requirement keep whatever limit they carry, including NULL, with no migration and no backfill.

"Did not choose a value" SHALL mean an **omitted** field on the JSON API, distinguished from an explicit null by whether the field was set on the request and not by the value's truthiness. On the control panel it SHALL mean a **blank** submitted limit field. The panel create handler SHALL substitute the default for a blank field, and the create form SHALL continue to pre-fill the field with it.

A key SHALL be created unlimited only by an administrator's explicit request: an explicit JSON `null`, or the panel's `unlimited` control. A non-admin's explicit request SHALL be refused as the api-key-issuance capability specifies.

When `DEFAULT_DAILY_REQUEST_LIMIT` is null, a create request that chose no value SHALL be refused as missing a required limit (400 on the JSON API, a flashed error on the panel) instead of creating an unlimited key. An administrator's explicit unlimited request SHALL still succeed.

The configured default SHALL be subject to the same 1..1,000,000 domain as any other limit and SHALL be rejected at startup if outside it.

#### Scenario: Existing keys keep their current quota
- **WHEN** the change is deployed to a database whose active keys all carry `daily_request_limit = NULL`
- **THEN** every one of those keys SHALL still be unlimited, no counter row SHALL be created for them, and their quota accounting SHALL be byte-for-byte what it was before the deploy

#### Scenario: A new key gets the default from the pre-filled form
- **WHEN** an operator creates a key through the control panel without altering the pre-filled limit field
- **THEN** the created key SHALL carry `daily_request_limit = DEFAULT_DAILY_REQUEST_LIMIT` and the keys page SHALL show it

#### Scenario: A blank panel field receives the default
- **WHEN** any account, administrator or not, clears the pre-filled limit field and submits without the `unlimited` control
- **THEN** the created key SHALL carry `DEFAULT_DAILY_REQUEST_LIMIT` and SHALL NOT be unlimited

#### Scenario: Omitted and explicit null differ on the JSON API
- **WHEN** an administrator sends one create request omitting `daily_request_limit` and another sending `{"daily_request_limit": null}`
- **THEN** the first key SHALL carry the configured default and the second SHALL be unlimited

#### Scenario: A non-admin explicit null is refused
- **WHEN** a non-admin sends a create request with `{"daily_request_limit": null}`
- **THEN** the response SHALL be 403 and no key SHALL be created

#### Scenario: An explicit value still wins
- **WHEN** a create request sends `{"daily_request_limit": 250}`
- **THEN** the created key SHALL carry 250 regardless of the configured default

#### Scenario: A null default makes the limit required
- **WHEN** `DEFAULT_DAILY_REQUEST_LIMIT` is null and a create request omits the field (JSON) or submits it blank without the `unlimited` control (panel)
- **THEN** the request SHALL be refused as missing a required limit and no key SHALL be created
- **AND** an administrator's explicit unlimited request SHALL still create an unlimited key
