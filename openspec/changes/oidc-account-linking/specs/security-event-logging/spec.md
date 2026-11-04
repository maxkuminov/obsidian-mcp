## ADDED Requirements

### Requirement: OIDC linking SHALL be recorded by catalogued events that carry no provider identity
The catalogue SHALL declare `panel_oidc_linked` (`user_id`, `username`, `client_ip`, `route`) emitted after a first-login bind commits, `panel_oidc_link_set` (`actor_user_id`, `user_id`, `username`, `mode`, `client_ip`, `route`, with `mode` `expect` or `bind`) and `panel_oidc_link_removed` (`actor_user_id`, `user_id`, `username`, `count`, `client_ip`, `route`) emitted after the admin action commits, and SHALL document each in the event table. No OIDC event SHALL carry a subject, issuer, email, group, code, token, ID token, state, nonce, verifier, client secret or exception text.

#### Scenario: First-login bind
- **WHEN** a pending user binds at first sign-in
- **THEN** exactly one `panel_oidc_linked` record is emitted, followed by `panel_login_succeeded`, and neither contains the subject or the email

#### Scenario: Admin sets an expectation
- **WHEN** an admin sets an expected email
- **THEN** one `panel_oidc_link_set` record with `mode` `expect` is emitted and it does not contain the email

### Requirement: OIDC refusal reasons SHALL be a closed set that never names the resolved user
`panel_oidc_refused` SHALL carry a `reason` from exactly `capacity`, `state`, `provider`, `token`, `entitlement`, `unlinked`, `email_unverified`, `link_conflict`, `account` and `session_mint`, and SHALL NOT carry `user_id` or `username` for any reason.

#### Scenario: Unverified email refusal
- **WHEN** a pending bind is refused because `email_verified` is not true
- **THEN** the record's reason is `email_unverified` and it has no `user_id`

#### Scenario: Retired reason
- **WHEN** the catalogue's documented reasons are compared with the emitting code
- **THEN** `mapping` appears in neither
