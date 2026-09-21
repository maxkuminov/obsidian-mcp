## ADDED Requirements

### Requirement: A throttled login is recorded, and its subject is the client address
The server SHALL emit a bounded `panel_login_account_throttled` event whenever the per-account failed-login budget refuses an attempt, carrying only `client_ip`, `route`, `username_submitted`, `limit_count` and `window_seconds`. Its suppression subject SHALL be the trusted client address and never the submitted username, because a caller-supplied subject lets an attacker mint a fresh logging allowance for every value they rotate through.

The event is the only place the throttle is distinguishable from an ordinary failed login. The response is equivalent in content by design, so without this record an operator has no way to tell a password-guessing campaign from a user who forgot their password.

#### Scenario: The throttle is recorded
- **WHEN** a login attempt is refused by the per-account budget
- **THEN** one `panel_login_account_throttled` record SHALL be emitted at warning level, naming the submitted username, the limit and the window

#### Scenario: The subject is the address
- **WHEN** attempts for many different submitted usernames arrive from one address and are throttled
- **THEN** every record SHALL share the address as its suppression subject, so the existing per-subject caps bound the total volume

#### Scenario: No credential material is written
- **WHEN** the record is emitted
- **THEN** it SHALL contain no password, no session identifier and no value outside its declared field list

### Requirement: The new event's submitted username is covered by the existing field bound
The `username_submitted` field of `panel_login_account_throttled` SHALL be declared in the formatter's field allow-list with the same 64-character bound the existing login events already carry, so that adding the event introduces no unbounded log field. This change SHALL NOT widen that bound.

The bound already exists and is applied centrally by the formatter, so a caller-supplied username of any length already renders truncated. The requirement records that the new event inherits it rather than bypassing it — a new event declared without a bound would be the one way to reintroduce the problem.

#### Scenario: The new event declares the bounded field
- **WHEN** the field allow-list is inspected for `username_submitted`
- **THEN** it SHALL declare the same 64-character string bound that the existing login events rely on

#### Scenario: An over-long submitted username is already truncated
- **WHEN** a login attempt for a username longer than that bound is throttled and recorded
- **THEN** the emitted `username_submitted` value SHALL be truncated to the declared bound

#### Scenario: The budget key needs no truncation
- **WHEN** the per-account budget records a failure
- **THEN** its key SHALL be the account's row identifier rather than the submitted text, so it is bounded by construction
