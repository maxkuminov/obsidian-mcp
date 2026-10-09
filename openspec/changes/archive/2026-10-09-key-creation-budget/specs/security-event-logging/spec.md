## ADDED Requirements

### Requirement: A throttled key creation SHALL be recorded with the authenticated actor as its subject

The server SHALL invoke the security-event emitter exactly once for `key_creation_throttled`, at warning level, for each request the key-creation budget refuses on either creation route; whether that record reaches the log sink SHALL remain subject to the global per-subject suppressor. The event SHALL carry only `actor_user_id`, `actor_username`, `client_ip`, `route`, `method`, `reason` (`account_budget` or `address_budget`), `limit_count` and `window_seconds`. These field names already carry bounds in the formatter's allow-list, and this requirement SHALL NOT add or widen any field bound. The suppression subject SHALL be the same exact account identity the budget's account counter keys on (the `users` row identifier, or one fixed identity for the single-user operator). It SHALL NOT fall back to the client address, which SHALL appear only as the `client_ip` field, and it SHALL never be derived from a request body value. A refusal by the active-key cap SHALL NOT emit this event, and a non-admin unlimited request SHALL be recorded as `panel_forbidden` and not as this event.

#### Scenario: A budget refusal is recorded
- **WHEN** a create request on either route is refused by the key-creation budget
- **THEN** the emitter SHALL be invoked exactly once for `key_creation_throttled`, naming the refusing counter in `reason`, together with the limit and the window

#### Scenario: Rotating addresses do not mint log allowance in single-user mode
- **WHEN** the single-user operator's create requests are refused by the budget from a different trusted client address each time
- **THEN** every refusal SHALL be charged to one fixed suppression subject, so the suppressor bounds the records exactly as it would for one address

#### Scenario: Nothing outside the allow-list is written
- **WHEN** the record is emitted for a form request whose body carries a key name
- **THEN** the record SHALL contain no key name, no key material, no CSRF token and no value outside its declared field list

#### Scenario: A cap refusal is not a security event
- **WHEN** a create request is refused by the active-key cap
- **THEN** no `key_creation_throttled` record SHALL be emitted
