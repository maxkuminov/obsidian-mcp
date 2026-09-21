## ADDED Requirements

### Requirement: A username-throttled login is recorded, and its subject is the client address
The server SHALL emit a bounded `panel_login_account_throttled` event whenever the per-username failed-login budget refuses an attempt, carrying only `client_ip`, `route`, `username_submitted`, `limit_count` and `window_seconds`. Its suppression subject SHALL be the trusted client address and never the submitted username, because a caller-supplied subject lets an attacker mint a fresh logging allowance for every value they rotate through.

The event is the only place the throttle is distinguishable from an ordinary failed login. The response is byte-identical by design, so without this record an operator has no way to tell a password-guessing campaign from a user who forgot their password.

#### Scenario: The throttle is recorded
- **WHEN** a login attempt is refused by the per-username budget
- **THEN** one `panel_login_account_throttled` record SHALL be emitted at warning level, naming the submitted username, the limit and the window

#### Scenario: The subject is the address
- **WHEN** attempts for many different submitted usernames arrive from one address and are throttled
- **THEN** every record SHALL share the address as its suppression subject, so the existing per-subject caps bound the total volume

#### Scenario: No credential material is written
- **WHEN** the record is emitted
- **THEN** it SHALL contain no password, no session identifier and no value outside its declared field list

### Requirement: A caller-supplied username entering a record MUST be length-bounded
Every security event field carrying a username the caller submitted SHALL be truncated to the `users.username` column width before the record is emitted. The login form accepts an arbitrarily long value today and writes it straight into the log, so an attacker can drive unbounded bytes into the security log through a field the log already trusts. Truncating at the column width cannot alter any value that could match a real account.

#### Scenario: An over-long submitted username is truncated
- **WHEN** a failed login is recorded for a submitted username longer than the column width
- **THEN** the emitted `username_submitted` value SHALL be truncated to that width

#### Scenario: A real username is unchanged
- **WHEN** a failed login is recorded for a submitted username within the column width
- **THEN** the emitted value SHALL be exactly the normalised username, unchanged
