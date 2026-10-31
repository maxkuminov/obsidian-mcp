## ADDED Requirements

### Requirement: Each client registration path SHALL be recorded as a catalogued event naming its kind
The server SHALL declare an `oauth_client_registration_path` event at INFO with the fields `registration_kind`, `client_id`, `cimd_host`, `outcome`, `user_id` and `client_ip`, and SHALL emit it exactly once, after the commit, for every successful Dynamic Client Registration (`registration_kind` `dcr`, `outcome` `created`) and for every consent approval that created, refreshed or reused a Client ID Metadata Document client row (`registration_kind` `cimd`, `outcome` `created`, `refreshed` or `unchanged`, `cimd_host` the document URL's host). Its subject SHALL be the trusted client address for `dcr` and the approving user for `cimd`. It SHALL carry no secret, no URL path or query, and no document content.

#### Scenario: A DCR registration names its path
- **WHEN** `/register` succeeds
- **THEN** exactly one `oauth_client_registration_path` record SHALL be emitted with `registration_kind` `dcr`

#### Scenario: A CIMD approval names its path
- **WHEN** a user approves `client_id=https://claude.ai/oauth/claude-code-client-metadata` for the first time
- **THEN** exactly one record SHALL be emitted with `registration_kind` `cimd`, `outcome` `created` and `cimd_host` `claude.ai`

#### Scenario: Catalogue entry
- **WHEN** the event catalogue and the documented event table are compared
- **THEN** both SHALL contain `oauth_client_registration_path` with exactly its declared fields

### Requirement: A CIMD refusal SHALL be recorded once as an authorization refusal with a closed client_metadata reason
Every refusal at `/authorize` arising from Client ID Metadata Document handling SHALL emit exactly one `oauth_authorize_refused` record whose `reason` is `client_metadata.` followed by one of `url_invalid`, `url_not_canonical`, `fetch_cooldown`, `fetch_budget_exhausted`, `fetch_busy`, `ssrf_refused`, `fetch_failed`, `fetch_timeout`, `http_status`, `too_large`, `content_encoded`, `not_json`, `client_id_mismatch`, `document_invalid`, `redirect_uris_invalid`, `auth_method_unsupported`, `secret_present`, `grant_types_invalid`, `scope_invalid` or `metadata_changed`, with the submitted URL only in the 64-character-bounded `client_id_submitted` field and the canonical host, when the URL passed its syntax checks, in a new `cimd_host` field. The record SHALL NOT carry a resolved address, SSRF error text, a response status line or any document content.

#### Scenario: An SSRF refusal does not leak the address
- **WHEN** a CIMD URL resolving to `10.0.0.5` is refused
- **THEN** one `oauth_authorize_refused` record SHALL carry `reason` `client_metadata.ssrf_refused`
- **AND** the serialised record SHALL NOT contain `10.0.0.5`

#### Scenario: The subject is the address, never the host
- **WHEN** CIMD refusals naming many distinct hosts arrive from one address
- **THEN** they SHALL share one suppression subject, the trusted client address

### Requirement: An unexpected RFC 8707 resource SHALL be recorded with its host only
The server SHALL declare an `oauth_resource_unexpected` event at INFO with the fields `resource_host`, `route`, `client_id_submitted` and `client_ip`, emitted at most once per request at `/authorize` or `/token` when a `resource` value names neither this server's protected-resource URL nor its base URL. `resource_host` SHALL be the value's host in ASCII form, or the constant `<unparseable>`; the record SHALL NOT carry the value's scheme, path, query or fragment, and its subject SHALL be the trusted client address.

#### Scenario: Only the host is recorded
- **WHEN** `resource=https://other.example/x?token=abc` is sent
- **THEN** the record SHALL carry `resource_host` `other.example` and SHALL NOT contain `token=abc`

### Requirement: The new fields SHALL be allow-listed with declared types and bounds
`ALLOWED_FIELDS` SHALL declare `registration_kind` as a string of at most 8 characters and `cimd_host` and `resource_host` as strings of at most 253 characters; `cimd_host` and `resource_host` SHALL be documented as a server-derived host taken from a parsed URL, distinct from both a database-read identifier and a `_submitted` value, and SHALL never be a suppression subject. The existing bound on `client_id` and `client_id_submitted` SHALL be unchanged.

#### Scenario: The allow-list sweep covers the new fields
- **WHEN** the field allow-list AST sweep runs over `src/`
- **THEN** every emission of `registration_kind`, `cimd_host` and `resource_host` SHALL be in an event that declares it
