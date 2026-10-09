## ADDED Requirements

### Requirement: A throttled key creation SHALL be recorded with the authenticated actor as its subject

The server SHALL emit one bounded `key_creation_throttled` event, at warning level, whenever the key-creation budget refuses a request on either creation route. The event SHALL carry only `actor_user_id`, `actor_username`, `client_ip`, `route`, `method`, `reason` (`account_budget` or `address_budget`), `limit_count` and `window_seconds`. These field names already carry bounds in the formatter's allow-list, and this requirement SHALL NOT add or widen any field bound. The suppression subject SHALL be derived from the authenticated actor and the trusted client address, never from a request body value. A refusal by the active-key cap SHALL NOT emit this event, and a non-admin unlimited request SHALL be recorded as `panel_forbidden` and not as this event.

#### Scenario: A budget refusal is recorded
- **WHEN** a create request on either route is refused by the key-creation budget
- **THEN** one `key_creation_throttled` record SHALL be emitted, naming the refusing counter in `reason`, together with the limit and the window

#### Scenario: Nothing outside the allow-list is written
- **WHEN** the record is emitted for a form request whose body carries a key name
- **THEN** the record SHALL contain no key name, no key material, no CSRF token and no value outside its declared field list

#### Scenario: A cap refusal is not a security event
- **WHEN** a create request is refused by the active-key cap
- **THEN** no `key_creation_throttled` record SHALL be emitted
