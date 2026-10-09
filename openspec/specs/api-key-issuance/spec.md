# api-key-issuance Specification

## Purpose
TBD - created by archiving change key-creation-budget. Update Purpose after archive.
## Requirements
### Requirement: Both key-creation routes SHALL draw on one shared creation budget keyed by account and by client address

`POST /api/keys` and `POST /admin/keys/create` SHALL both charge one in-process key-creation budget through a single shared function, so that a creation through either representation consumes the same allowance. The budget SHALL consist of two counters, both of which MUST admit a creation:
- an account counter keyed **exactly** on the authenticated panel account (the `users` row identifier, or one fixed key for the single-user operator);
- an address counter keyed on the trusted client address that the app's proxy-header handling resolved.

When both counters admit, both SHALL be incremented with no suspension point between the check and the increments. When either refuses, neither SHALL be incremented.

The budget SHALL be charged only for a request that has passed authentication, CSRF, request validation, the unlimited-authorization rule and the active-key cap. A request refused by any of those checks SHALL consume no allowance.

Each counter SHALL use a fixed window that opens at its first charge and lasts `KEY_CREATION_WINDOW_SECONDS` (default 3600). The defaults are `KEY_CREATION_ACCOUNT_LIMIT` 10 and `KEY_CREATION_ADDRESS_LIMIT` 20. Each limit SHALL accept the shared null representation as "off" and SHALL refuse zero at startup. Administrators SHALL be subject to the budget.

A refused JSON request SHALL receive HTTP 429 with an integer `Retry-After` header. A refused form request SHALL receive a flashed error and a 303 redirect to the keys page. In both cases no key SHALL be written. The JSON route's existing per-address slowapi limit SHALL remain in addition to this budget.

#### Scenario: Alternating JSON and form requests share one budget
- **WHEN** one account with an account limit of 3 alternates JSON and form create requests, each otherwise valid
- **THEN** exactly 3 keys SHALL be created in total
- **AND** the fourth request SHALL be refused on whichever route it uses

#### Scenario: Rotating addresses cannot outrun the account counter
- **WHEN** one account submits valid create requests, each from a different trusted client address, beyond the account limit within one window
- **THEN** every request beyond the account limit SHALL be refused and no key SHALL be written for it

#### Scenario: Rotating accounts cannot outrun the address counter
- **WHEN** several accounts, each under its own account limit, submit valid create requests from one client address beyond the address limit within one window
- **THEN** every request beyond the address limit SHALL be refused

#### Scenario: An unrelated account and address are unaffected
- **WHEN** one account's counter and one address's counter are exhausted
- **THEN** a create request from a different account at a different address SHALL succeed

#### Scenario: Refused checks consume no allowance
- **WHEN** an account submits any number of create requests that fail name validation, followed by valid requests
- **THEN** the account SHALL still be able to create its full account limit of keys in that window

#### Scenario: Concurrent creates cannot overshoot
- **WHEN** more create requests than the account limit for one account run concurrently
- **THEN** at most the account limit of keys SHALL be created

#### Scenario: The budget expires without intervention
- **WHEN** the window passes after an exhausted counter's last charge
- **THEN** a valid create request SHALL succeed with no administrative action

#### Scenario: Refusal shapes
- **WHEN** a JSON create request is refused by the budget
- **THEN** the response SHALL be 429 carrying an integer `Retry-After` no greater than the remaining window
- **AND** a form create request refused by the budget SHALL be redirected with 303 to the keys page, which shows the flashed error

#### Scenario: The budget is bounded by construction
- **WHEN** create requests arrive from an unbounded number of client addresses
- **THEN** the budget SHALL hold at most one account entry per account charged in the current window and at most one address entry per admitted creation in the current window

### Requirement: A non-admin account SHALL NOT hold more than the configured number of active keys

A key creation for a non-admin account SHALL be refused when that account already owns `KEY_MAX_ACTIVE_PER_ACCOUNT` (default 25; null disables) or more keys with `is_active = true`. The count SHALL be taken inside the creating transaction while holding a row lock on the owning `users` row, so that concurrent creations for one account cannot exceed the cap. Revoked keys SHALL NOT count. Administrators and the single-user operator SHALL be exempt. A refusal SHALL write no key and SHALL NOT charge the creation budget. It SHALL be a 409 on the JSON route and a flashed error on the form route, and both SHALL name the cap and the remedy of revoking a key. Existing keys SHALL NOT be revoked or altered to satisfy the cap.

#### Scenario: The cap refuses the next key
- **WHEN** a non-admin account owning 25 active keys submits a valid create request
- **THEN** the request SHALL be refused, no key SHALL be written and the creation budget SHALL be unchanged

#### Scenario: Revoking makes room
- **WHEN** that account revokes one key and submits a valid create request
- **THEN** the key SHALL be created

#### Scenario: The cap is exact under concurrency
- **WHEN** a non-admin account owning one fewer than the cap submits two valid create requests concurrently
- **THEN** exactly one key SHALL be created

#### Scenario: Administrators are exempt
- **WHEN** an administrator owning more active keys than the cap submits a valid create request within the budget
- **THEN** the key SHALL be created

### Requirement: Only an administrator SHALL be able to make a key unlimited, and only by explicit request

A key's `daily_request_limit` SHALL become NULL (unlimited), at creation or by an edit, only when the authenticated account is an administrator (the single-user operator counts as one) **and** the request explicitly asks for unlimited. On the panel, the explicit request is the `unlimited` field with value `1`, rendered only for administrators. On the JSON API, it is an explicit `null` for `daily_request_limit`. A blank panel field and an omitted JSON field SHALL never mean unlimited.

A non-admin request that explicitly asks for unlimited SHALL be refused with no key written or altered and no creation budget charged. The JSON route SHALL answer 403, the panel SHALL show a flashed error, and the server SHALL record a `panel_forbidden` security event with reason `unlimited_requires_admin`. It SHALL NOT be silently downgraded to a limited key.

On edit, a blank panel limit field SHALL be a validation error for every account and SHALL leave the stored limit unchanged. The edit path SHALL still never apply the configured default to an existing key. When the explicit unlimited request is present, the numeric field SHALL be ignored.

#### Scenario: A non-admin cannot create an unlimited key through the form
- **WHEN** a non-admin submits the create form with `unlimited=1`
- **THEN** no key SHALL be created, a flashed error SHALL be shown, a `panel_forbidden` event with reason `unlimited_requires_admin` SHALL be recorded, and the creation budget SHALL be unchanged

#### Scenario: A non-admin cannot create an unlimited key through the API
- **WHEN** a non-admin sends `POST /api/keys` with `{"daily_request_limit": null}`
- **THEN** the response SHALL be 403 and no key SHALL be created

#### Scenario: An administrator creates an unlimited key explicitly
- **WHEN** an administrator submits the create form with `unlimited=1`, or sends `POST /api/keys` with `{"daily_request_limit": null}`
- **THEN** the created key SHALL have `daily_request_limit` NULL

#### Scenario: A non-admin cannot clear a limit
- **WHEN** a non-admin edits their own limited key with `unlimited=1` on the panel, or with `{"daily_request_limit": null}` on `PUT /api/keys/{id}/limit`
- **THEN** the key's limit SHALL be unchanged and the request SHALL be refused (flashed error, or 403)

#### Scenario: A blank edit is refused for everyone
- **WHEN** any account submits the edit-limit form with an empty field and no `unlimited` field
- **THEN** the key's limit SHALL be unchanged and a flashed error SHALL be shown

#### Scenario: An administrator clears a limit explicitly
- **WHEN** an administrator submits the edit-limit form with `unlimited=1` for any key
- **THEN** the key SHALL become unlimited

#### Scenario: The unlimited request wins over a stray number
- **WHEN** an administrator's create or edit form carries both `unlimited=1` and a numeric limit
- **THEN** the resulting limit SHALL be NULL

