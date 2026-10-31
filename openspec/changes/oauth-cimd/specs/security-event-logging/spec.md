## ADDED Requirements

### Requirement: The registration path SHALL be carried on the existing single-outcome events, with exactly one emission per registration and per approval
The server SHALL NOT add an event for the client registration path. `oauth_client_registered` SHALL carry `registration_kind` `dcr` on every successful Dynamic Client Registration, and `oauth_consent_granted` SHALL carry `registration_kind` (`dcr` or `cimd`) on every consent approval, plus, for a Client ID Metadata Document approval, `cimd_host`, `cimd_ref` and `outcome` (`created`, `refreshed` or `unchanged`). Each successful `/register` SHALL cause exactly one emission and each approval exactly one emission.

#### Scenario: A DCR registration emits once and names its path
- **WHEN** `/register` succeeds
- **THEN** exactly one security record SHALL be emitted for it, `oauth_client_registered`, carrying `registration_kind` `dcr`

#### Scenario: A CIMD approval emits once and names its path
- **WHEN** a user approves `client_id=https://claude.ai/oauth/claude-code-client-metadata` for the first time
- **THEN** exactly one security record SHALL be emitted for the approval, `oauth_consent_granted`, carrying `registration_kind` `cimd`, `outcome` `created` and `cimd_host` `claude.ai`

#### Scenario: Catalogue entries
- **WHEN** the event catalogue and the documented event table are compared
- **THEN** `oauth_client_registered` and `oauth_consent_granted` SHALL declare the added fields in both, and no `oauth_client_registration_path` event SHALL exist

### Requirement: A CIMD client identifier MUST NOT appear in any security record
Wherever a submitted `client_id` begins with `https://`, at `/authorize`, at `/token` for both grant types and at `/revoke`, the record SHALL carry `cimd_host` (the canonical host when the value parses, otherwise absent) and `cimd_ref` (the first 16 hexadecimal characters of SHA-256 over the full submitted value) and SHALL NOT carry `client_id_submitted`. The rule SHALL be applied by one shared helper used by every OAuth emission site that records a submitted client identifier.

#### Scenario: A token-endpoint mismatch does not log the URL
- **WHEN** a code is exchanged with `client_id=https://a.example/x` that does not match the code's client
- **THEN** the `oauth_token_refused` record SHALL carry `cimd_host` `a.example` and a 16-hex `cimd_ref`
- **AND** it SHALL NOT carry `client_id_submitted` or the string `https://a.example/x`

#### Scenario: Revocation follows the same rule
- **WHEN** `/revoke` names an `https://` client_id that does not match the token
- **THEN** the `oauth_revoke_noop` record SHALL carry `cimd_host` and `cimd_ref` and no `client_id_submitted`

### Requirement: A CIMD refusal SHALL be recorded once as an authorization refusal with a closed client_metadata reason
Every refusal at `/authorize` arising from Client ID Metadata Document handling SHALL emit exactly one `oauth_authorize_refused` record whose `reason` is `client_metadata.` followed by one of `internal_id_submitted`, `url_invalid`, `url_not_canonical`, `fetch_cooldown`, `fetch_budget_exhausted`, `fetch_busy`, `ssrf_refused`, `fetch_failed`, `fetch_timeout`, `http_status`, `too_large`, `content_encoded`, `not_json`, `client_id_mismatch`, `document_invalid`, `redirect_uris_invalid`, `auth_method_unsupported`, `secret_present`, `grant_types_invalid`, `scope_invalid`, `redirect_uri_mismatch` or `metadata_changed`. The record SHALL NOT carry a resolved address, SSRF error text, a response status line or any document content.

#### Scenario: An SSRF refusal does not leak the address
- **WHEN** a CIMD URL resolving to `10.0.0.5` is refused
- **THEN** one `oauth_authorize_refused` record SHALL carry `reason` `client_metadata.ssrf_refused`
- **AND** the serialised record SHALL NOT contain `10.0.0.5`

#### Scenario: The subject is the address, never the host
- **WHEN** CIMD refusals naming many distinct hosts arrive from one address
- **THEN** they SHALL share one suppression subject, the trusted client address

### Requirement: Every /authorize and /token terminal path SHALL carry the RFC 8707 resource classification
Every security record emitted while handling a `GET` or `POST /authorize` or a `/token` request SHALL carry `resource_class`, one of `absent`, `accepted_1`, `accepted_2`, `accepted_3`, `accepted_4`, `unexpected` or `unclassified_rate_limited`, and SHALL carry `resource_host` only when the class is `unexpected`. This SHALL include at least `oauth_authorize_refused`, `oauth_consent_granted`, `oauth_consent_denied`, `oauth_cross_user_client_refused`, `oauth_token_issued`, `oauth_token_refreshed`, `oauth_token_refused`, `oauth_code_replay_detected`, `oauth_code_replay_revocation_failed`, `oauth_refresh_reuse_detected`, `oauth_refresh_reuse_revocation_failed` and `oauth_token_rotation_failed`. A request whose terminal path emits none of them — a successful consent render, the multi-user login redirect, the client-vanished refusal, an unhandled exception, or the `/token` rate-limiter refusal — SHALL emit exactly one `oauth_resource_classified` record at INFO carrying only `resource_class`, `route` and, when `unexpected`, `resource_host`, with the trusted address as its subject. `resource_host` SHALL be the value's host in ASCII form or the constant `<unparseable>`, and no record SHALL carry the value's scheme, path, query or fragment. The `/token` rate-limiter refusal MUST NOT read or parse the request body; its record SHALL carry `resource_class` `unclassified_rate_limited` and no `resource_host`. The later change that enforces `resource` MAY rely on exactly this evidence — no `unexpected` on any of these records for 14 consecutive days, with `unclassified_rate_limited` excluded because a rate-limited client's value is observed on its next admitted request — and on nothing weaker.

#### Scenario: Only the host is recorded
- **WHEN** `resource=https://other.example/x?token=abc` is sent to `/token`
- **THEN** the outcome record SHALL carry `resource_class` `unexpected` and `resource_host` `other.example` and SHALL NOT contain `token=abc`

#### Scenario: A replay detection is classified
- **WHEN** a replayed authorization code with `resource=https://other.example/` revokes its family
- **THEN** the `oauth_code_replay_detected` record SHALL carry `resource_class` `unexpected`

#### Scenario: A throttled token request is classified
- **WHEN** `/token` refuses a request by its rate limiter
- **THEN** exactly one `oauth_resource_classified` record SHALL be emitted for it, carrying `resource_class` `unclassified_rate_limited` and no `resource_host`
- **AND** the request body SHALL NOT be read

#### Scenario: The follow-up gate is computable from the records
- **WHEN** the emitted `/authorize` and `/token` records of a period are counted by `resource_class`
- **THEN** every request handled in the period SHALL be represented by at least one record, and every record SHALL fall in exactly one of the seven classes

### Requirement: The new fields SHALL be allow-listed with declared types and bounds
`ALLOWED_FIELDS` SHALL declare `registration_kind` as a string of at most 8 characters, `cimd_ref` of at most 16, `resource_class` of at most 32, and `cimd_host` and `resource_host` of at most 253. `cimd_host`, `cimd_ref`, `resource_class` and `resource_host` SHALL be documented as server-derived values computed from a parsed request value, distinct from both a database-read identifier and a `_submitted` value, and none SHALL ever be a suppression subject. The existing bound on `client_id` and `client_id_submitted` SHALL be unchanged.

#### Scenario: The allow-list sweep covers the new fields
- **WHEN** the field allow-list AST sweep runs over `src/`
- **THEN** every emission of the new fields SHALL be in an event that declares it
