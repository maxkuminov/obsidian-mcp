## ADDED Requirements

### Requirement: The authorization server metadata SHALL advertise CIMD support while it is enabled
`GET /.well-known/oauth-authorization-server` SHALL include `"client_id_metadata_document_supported": true` when `OAUTH_CIMD_ENABLED` is true and SHALL omit it when false. It SHALL always include `"none"` in `token_endpoint_auth_methods_supported`, `"authorization_response_iss_parameter_supported": true`, and the existing `registration_endpoint`, because Dynamic Client Registration remains the fallback for clients that send no URL.

#### Scenario: Both values Claude requires are present
- **WHEN** the metadata is fetched with CIMD enabled
- **THEN** it SHALL contain `client_id_metadata_document_supported: true` and `none` in `token_endpoint_auth_methods_supported`

#### Scenario: Disabled CIMD is not advertised
- **WHEN** the metadata is fetched with `OAUTH_CIMD_ENABLED=false`
- **THEN** it SHALL NOT contain `client_id_metadata_document_supported`
- **AND** it SHALL still contain `registration_endpoint`

### Requirement: A client_id beginning with https:// SHALL be treated only as a Client ID Metadata Document URL
At `GET` and `POST /authorize`, a `client_id` whose first bytes are `https://` SHALL be handled as a CIMD client identifier and SHALL NOT be looked up as a registered client identifier. While CIMD is disabled such a value SHALL be refused as an unknown client. The URL SHALL be refused, before any DNS resolution or connection, unless it is at most 2,048 printable ASCII characters without whitespace, parses, carries no userinfo and no fragment, has a path other than empty or `/`, contains no `.` or `..` path segment (literal or percent-encoded), uses port 443 or 8443, and is byte-equal to the canonical form the hardened fetch would request.

#### Scenario: A non-canonical spelling is refused without a fetch
- **WHEN** `/authorize` receives `client_id=https://Claude.AI/oauth/x`
- **THEN** the response SHALL be `invalid_client`
- **AND** no DNS resolution or connection SHALL be attempted

#### Scenario: A bare origin is refused
- **WHEN** `client_id` is `https://example.com/` or `https://example.com`
- **THEN** the response SHALL be `invalid_client` and nothing SHALL be fetched

#### Scenario: Userinfo, fragments and dot segments are refused
- **WHEN** `client_id` is `https://a@example.com/c`, `https://example.com/c#f` or `https://example.com/a/../c`
- **THEN** each SHALL be refused `invalid_client` before any connection

#### Scenario: A DCR identifier is unaffected
- **WHEN** `client_id` is a 32-character hex identifier issued by `/register`
- **THEN** it SHALL be resolved exactly as before this change

### Requirement: The metadata document SHALL be fetched only through the hardened transfer fetch with its SSRF policy and no redirects
The server SHALL fetch a CIMD URL only through the transfer subsystem's guarded fetch, with `trust_env` disabled, no redirects followed, a 10-second deadline over the whole fetch, a 65,536-byte body cap, identity content encoding only, and the transfer SSRF policy applied to every resolved address with the connection pinned to the validated address. A refused address, any 3xx response, any status other than 200, a timeout, an oversized or content-encoded body, or a body that is not a UTF-8 JSON object SHALL abort the authorization with `invalid_client`.

#### Scenario: A private or metadata address is never connected to
- **WHEN** the URL's host is a literal, or resolves to an address, in any range the transfer deny list refuses (including `127.0.0.1`, `::1`, `10.0.0.0/8`, `169.254.169.254`, `fc00::/7`, `100.64.0.0/10`, IPv4-mapped and NAT64 forms)
- **THEN** no connection SHALL be attempted and the response SHALL be `invalid_client`

#### Scenario: Mixed public and private answers are refused
- **WHEN** the host resolves to one public and one private address
- **THEN** the fetch SHALL be refused

#### Scenario: Redirects are not followed
- **WHEN** the document URL answers 301 or 302
- **THEN** the Location SHALL NOT be fetched and the response SHALL be `invalid_client`

#### Scenario: The size cap holds without a Content-Length
- **WHEN** the server streams 65,537 bytes with no `Content-Length`
- **THEN** the fetch SHALL stop at the cap and the response SHALL be `invalid_client`

#### Scenario: Proxy environment is ignored
- **WHEN** `HTTPS_PROXY` is set in the environment
- **THEN** the fetch SHALL connect directly to the validated address

### Requirement: Document fetches MUST be bounded per address, globally and per URL
A fetch SHALL be attempted only on a cache miss. Before it, the server SHALL refuse with `invalid_client` and without fetching when the URL's previous fetch failed less than 30 seconds ago, when the trusted client address has spent `OAUTH_CIMD_FETCHES_PER_ADDRESS_PER_MINUTE` fetches in the current minute, or when 4 fetches are already in flight for other URLs. Concurrent requests for the same URL SHALL share one fetch. The per-address state SHALL be bounded and an evicted entry SHALL NOT be treated as a refilled budget.

#### Scenario: The per-address budget refuses the eleventh fetch
- **WHEN** one address triggers 11 cache-missing fetches of distinct URLs within a minute under the default budget
- **THEN** the eleventh SHALL be refused without a connection

#### Scenario: A failing URL is fetched at most once per cooldown
- **WHEN** a URL's fetch fails and 50 requests from different addresses name it within the next 30 seconds
- **THEN** the origin SHALL receive no further request in that window

#### Scenario: Cache hits spend no budget
- **WHEN** an address has exhausted its budget and names a URL whose valid document is cached
- **THEN** the request SHALL proceed without a fetch

### Requirement: The document SHALL be validated and only its validated projection SHALL be used
The server SHALL require a JSON object whose `client_id` is a string byte-equal to the URL, whose `client_name` is a non-empty string of at most 255 characters, and whose `redirect_uris` is a list of 1 to 20 distinct strings of at most 2,048 characters each, every one either an `https` URI with a resolvable ASCII host and no fragment (the registration rule) or an `http` URI whose host is exactly `localhost`, `127.0.0.1` or `[::1]` with no userinfo and no fragment. `token_endpoint_auth_method` SHALL be absent or `none`; `client_secret` and `client_secret_expires_at` SHALL be absent; `grant_types`, when present, SHALL include `authorization_code`; `response_types`, when present, SHALL include `code`; `scope`, when present, SHALL be valid and SHALL name `read` or `readwrite`, and when absent SHALL default to the DCR default scope. Any failure SHALL be refused `invalid_client`.

#### Scenario: The Claude Code document validates
- **WHEN** the document is `{"client_id":"https://claude.ai/oauth/claude-code-client-metadata","client_name":"Claude Code","redirect_uris":["http://localhost/callback","http://127.0.0.1/callback"],"grant_types":["authorization_code","refresh_token"],"response_types":["code"],"token_endpoint_auth_method":"none"}` served at that URL
- **THEN** validation SHALL succeed

#### Scenario: A mismatched client_id is refused
- **WHEN** the document's `client_id` differs from the URL by case, a trailing slash or a query
- **THEN** the response SHALL be `invalid_client`

#### Scenario: Shared-secret clients are refused
- **WHEN** the document declares `token_endpoint_auth_method: client_secret_post` or carries a `client_secret` member
- **THEN** the response SHALL be `invalid_client`

#### Scenario: A remote plaintext redirect is refused
- **WHEN** `redirect_uris` contains `http://example.com/cb`
- **THEN** the response SHALL be `invalid_client`

### Requirement: Valid documents SHALL be cached per Cache-Control under a ceiling, and failures SHALL NOT be cached as documents
A validated projection SHALL be cached in process, keyed by the exact URL, for the response's `max-age` clamped to `OAUTH_CIMD_CACHE_MAX_SECONDS` (default 86,400, range 0–86,400), for 300 seconds when no `max-age` is present, and not at all under `no-store`, `no-cache` or a ceiling of 0. The cache SHALL be bounded and least-recently-used. A failed fetch or an invalid document SHALL never be stored or served as a document; it SHALL only start the 30-second cooldown.

#### Scenario: max-age above the ceiling is clamped
- **WHEN** a document is served with `max-age=604800`
- **THEN** it SHALL be refetched after at most 86,400 seconds

#### Scenario: no-store is honoured
- **WHEN** a document is served with `Cache-Control: no-store`
- **THEN** the next authorization naming it SHALL fetch it again

#### Scenario: A failure does not displace a document
- **WHEN** a cached document expires and its refetch fails
- **THEN** the request SHALL be refused, and the expired projection SHALL NOT be used

### Requirement: Redirect URIs SHALL match exactly except for port-agnostic loopback on CIMD clients
`GET` and `POST /authorize` SHALL use one matching function. For a registered client it SHALL be exact string membership, unchanged. For a CIMD client a requested `redirect_uri` SHALL be accepted when it equals a document entry exactly, or when the entry is `http` with host `localhost`, `127.0.0.1` or `[::1]` and the request is `http` with the same host literal, no userinfo, no fragment, any numeric port from 1 to 65535 or none, and a path and query byte-equal to the entry's. The authorization code SHALL store the exact requested URI, so the token endpoint's exact comparison is unchanged.

#### Scenario: Claude Code's ephemeral port is accepted
- **WHEN** the document lists `http://localhost/callback` and the request names `http://localhost:3118/callback`
- **THEN** it SHALL be accepted and the code SHALL record `http://localhost:3118/callback`

#### Scenario: Loopback literals do not cross-match
- **WHEN** the document lists only `http://127.0.0.1/callback` and the request names `http://localhost:3118/callback`
- **THEN** it SHALL be refused `invalid_redirect_uri`

#### Scenario: Path and query are exact on loopback
- **WHEN** the request names `http://localhost:3118/callback/x` or `http://localhost:3118/callback?a=1`
- **THEN** it SHALL be refused

#### Scenario: https entries stay exact
- **WHEN** the document lists `https://claude.ai/api/mcp/auth_callback` and the request names `https://claude.ai:8443/api/mcp/auth_callback`
- **THEN** it SHALL be refused

#### Scenario: The exchange compares the stored URI exactly
- **WHEN** a code minted for `http://localhost:3118/callback` is exchanged with `redirect_uri=http://localhost:4000/callback`
- **THEN** the token endpoint SHALL refuse it as a redirect mismatch

### Requirement: The consent approval SHALL be pinned to the document the consent page rendered
The consent page SHALL bind the SHA-256 of the validated projection it rendered into the signed consent state, and the approval SHALL re-resolve the document and refuse with `invalid_client`, minting nothing, when the projection's hash differs. The token endpoint and the refresh path MUST NOT fetch the document; the code row's client, exact redirect URI and clamped scope SHALL be all that the exchange depends on.

#### Scenario: A document changed between render and approve is refused
- **WHEN** the document's `redirect_uris` change after the consent page renders and before the user approves
- **THEN** the approval SHALL be refused and no code SHALL be minted

#### Scenario: The token endpoint never fetches
- **WHEN** a code minted for a CIMD client is exchanged, or its refresh token rotated
- **THEN** no request SHALL be made to the document URL

### Requirement: A CIMD client SHALL be persisted as one synthetic client row per URL and user, written only on approval
On an approval the server SHALL insert or refresh, in the transaction that inserts the authorization code, an `oauth_clients` row whose `client_id` is `cimd_` followed by the first 59 hex characters of SHA-256 over the URL, a newline and the owning user's id (or `-` when there is none), with `client_id_metadata_url` set to the URL, `user_id` set to the approving user, `token_endpoint_auth_method` `none`, no secret hash, and the projection's name, redirect URIs and scope, stamping `last_used_at` in the same statement. A consent page render and a denial SHALL NOT write a client row. The database SHALL enforce that a row carries a metadata URL exactly when its id begins `cimd_`, and that such a row has auth method `none` and no secret hash.

#### Scenario: Two users connecting the same client get separate rows
- **WHEN** two users in multi-user mode each approve the same CIMD URL
- **THEN** two client rows SHALL exist, each bound to its own user, and neither approval SHALL be refused as cross-user

#### Scenario: A repeat approval reuses the row
- **WHEN** one user approves the same CIMD URL twice
- **THEN** one client row SHALL exist and its name, redirect URIs and scope SHALL reflect the second approval's document

#### Scenario: A denial writes nothing
- **WHEN** a user denies a CIMD client that has no row
- **THEN** no client row SHALL be created

#### Scenario: Grant machinery is unchanged
- **WHEN** a CIMD client's code is exchanged and its refresh token rotated or replayed
- **THEN** grant families, code lineage, the absolute grant lifetime and refresh-reuse revocation SHALL behave exactly as for a registered client

### Requirement: The token and revocation endpoints SHALL accept a CIMD URL as the caller's client identifier
Wherever `/token` or `/revoke` compares a caller-supplied `client_id` against a client row, a value byte-equal to that row's `client_id_metadata_url` SHALL match exactly as the row's own `client_id` does, through one shared definition. All other refusal rules, lookups and constant responses of those endpoints SHALL be unchanged.

#### Scenario: Claude Code exchanges with its URL
- **WHEN** a code issued to a CIMD row is exchanged with `client_id` equal to the document URL
- **THEN** the exchange SHALL succeed

#### Scenario: Another URL does not match
- **WHEN** the same code is exchanged with a different `https://` client_id
- **THEN** it SHALL be refused as a client mismatch and nothing SHALL be revoked

#### Scenario: Revocation with the URL
- **WHEN** `/revoke` presents a CIMD client's token with `client_id` equal to its URL
- **THEN** the grant family SHALL be revoked

### Requirement: CIMD client rows SHALL expire when nothing references them, and MUST NOT be deleted while any code or token row does
The maintenance pass SHALL delete a client row that carries a metadata URL when it holds no authorization code and no token row of any state and its `last_used_at` is older than `OAUTH_CLIENT_UNUSED_EXPIRY_DAYS`, regardless of its owner, under the same skip-locked selection and in-lock re-read the registered-client sweep uses. Setting that age to its disabled value SHALL disable this branch too. The registered-client sweep SHALL be unchanged and SHALL NOT select a CIMD row.

#### Scenario: A row with a live grant is kept
- **WHEN** a CIMD row's marker is older than the age but a token row of any state references it
- **THEN** it SHALL NOT be deleted

#### Scenario: An unreferenced old row is deleted and re-derived later
- **WHEN** a CIMD row has no code or token row and its marker is older than the age
- **THEN** it SHALL be deleted, and the user's next approval SHALL re-create it from the document

#### Scenario: The kill switch covers CIMD rows
- **WHEN** `OAUTH_CLIENT_UNUSED_EXPIRY_DAYS` is null
- **THEN** no CIMD row SHALL be deleted by the pass

### Requirement: Authorization responses SHALL carry the RFC 9207 issuer
Every redirect issued by `POST /authorize`, approval and denial alike, SHALL carry an `iss` query parameter equal to the `issuer` value of the authorization server metadata, for every client kind.

#### Scenario: Approval carries iss
- **WHEN** a user approves
- **THEN** the redirect SHALL carry `code`, `state` and `iss` equal to the metadata issuer

#### Scenario: Denial carries iss
- **WHEN** a user denies
- **THEN** the redirect SHALL carry `error=access_denied` and `iss`

### Requirement: An RFC 8707 resource parameter SHALL be checked and an unexpected value SHALL be recorded without refusing the request
At `GET` and `POST /authorize` and on both `/token` grant types, every `resource` value present SHALL be compared against this server's protected-resource URL and its base URL, each with or without one trailing slash. A value outside that set SHALL cause exactly one `oauth_resource_unexpected` record per request carrying only the value's host, and the request SHALL proceed exactly as if the parameter were absent. This change MUST NOT refuse a request because of its `resource` value, and an absent `resource` SHALL be accepted.

#### Scenario: The advertised resource is accepted silently
- **WHEN** `resource` equals the `resource` field of the protected-resource metadata
- **THEN** the request SHALL proceed and no `oauth_resource_unexpected` record SHALL be emitted

#### Scenario: A foreign resource is logged and the request proceeds
- **WHEN** `resource=https://other.example/mcp?k=secret` is sent at `/authorize` or `/token`
- **THEN** the request SHALL proceed as if `resource` were absent
- **AND** one `oauth_resource_unexpected` record SHALL carry `resource_host` `other.example`
- **AND** the serialised record SHALL NOT contain `/mcp`, `k=` or `secret`

### Requirement: Every CIMD refusal SHALL answer a constant invalid_client and record its reason only in the log
Every refusal arising from CIMD detection, fetching, budgeting or validation at `/authorize` SHALL answer HTTP 400 with the body `{"error": "invalid_client"}` and no description, before any redirect, and SHALL record the specific reason in the security log.

#### Scenario: Two different failures answer identically
- **WHEN** one request fails on a 404 document and another on a `client_id` mismatch
- **THEN** both responses SHALL be byte-identical in status and body

### Requirement: The OAuth panel SHALL identify CIMD clients by their metadata URL
The OAuth panel SHALL mark a client row that carries a metadata URL as a Client ID Metadata Document client, SHALL show its URL and the URL's host, and SHALL label `client_name` as the application's self-description, rendered as text, without inline handlers or styles.

#### Scenario: A CIMD row is labelled
- **WHEN** the panel lists a CIMD client
- **THEN** it SHALL show the metadata URL, its host, and the name labelled as self-described

### Requirement: CIMD settings SHALL be validated at startup and documented
`OAUTH_CIMD_ENABLED` SHALL default to true; `OAUTH_CIMD_CACHE_MAX_SECONDS` SHALL default to 86,400 and refuse values outside 0–86,400; `OAUTH_CIMD_FETCHES_PER_ADDRESS_PER_MINUTE` SHALL default to 10 and refuse values outside 1–120. Each SHALL be documented in the README settings table and `.env.example`.

#### Scenario: An out-of-range ceiling is refused
- **WHEN** `OAUTH_CIMD_CACHE_MAX_SECONDS=90000`
- **THEN** startup SHALL refuse the configuration

#### Scenario: Disabling falls back to DCR
- **WHEN** `OAUTH_CIMD_ENABLED=false` and a client names an `https://` client_id
- **THEN** `/authorize` SHALL answer `invalid_client` and an existing CIMD grant SHALL still refresh
