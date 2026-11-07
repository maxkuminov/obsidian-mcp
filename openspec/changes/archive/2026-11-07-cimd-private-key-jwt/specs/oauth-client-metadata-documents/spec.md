## MODIFIED Requirements

### Requirement: The authorization server metadata SHALL advertise CIMD support while it is enabled
`GET /.well-known/oauth-authorization-server` SHALL include `"client_id_metadata_document_supported": true` when `OAUTH_CIMD_ENABLED` is true and SHALL omit it when false. It SHALL always include `"none"` in `token_endpoint_auth_methods_supported`, `"authorization_response_iss_parameter_supported": true`, and the existing `registration_endpoint`, because Dynamic Client Registration remains the fallback for clients that send no URL. While `OAUTH_CIMD_ENABLED` is true, `token_endpoint_auth_methods_supported` SHALL also include `"private_key_jwt"` and the metadata SHALL include `"token_endpoint_auth_signing_alg_values_supported": ["RS256","PS256","ES256"]`. While it is false, neither SHALL appear. `/register` SHALL continue to refuse `private_key_jwt`.

#### Scenario: Both values Claude requires are present
- **WHEN** the metadata is fetched with CIMD enabled
- **THEN** it SHALL contain `client_id_metadata_document_supported: true` and `none` in `token_endpoint_auth_methods_supported`

#### Scenario: Disabled CIMD is not advertised
- **WHEN** the metadata is fetched with `OAUTH_CIMD_ENABLED=false`
- **THEN** it SHALL NOT contain `client_id_metadata_document_supported`
- **AND** it SHALL still contain `registration_endpoint`
- **AND** `token_endpoint_auth_methods_supported` SHALL be exactly `["none","client_secret_post"]` and `token_endpoint_auth_signing_alg_values_supported` SHALL be absent

#### Scenario: Key-based client authentication is advertised while CIMD is enabled
- **WHEN** the metadata is fetched with CIMD enabled
- **THEN** `token_endpoint_auth_methods_supported` SHALL contain `private_key_jwt` and `token_endpoint_auth_signing_alg_values_supported` SHALL be `["RS256","PS256","ES256"]`

#### Scenario: Dynamic registration still refuses private_key_jwt
- **WHEN** `/register` is called with `token_endpoint_auth_method: private_key_jwt`
- **THEN** it SHALL be refused as an unsupported auth method, as before

### Requirement: The document SHALL be validated and only its validated projection SHALL be used
The server SHALL require a JSON object meeting all of the following, and any failure SHALL be refused `invalid_client`:
- `client_id` is a string byte-equal to the URL.
- `client_name` is a non-empty string of at most 255 characters.
- `redirect_uris` is a list of 1 to 20 distinct strings of at most 2,048 characters each. Every one is either an `https` URI with a resolvable ASCII host and no fragment (the registration rule) or an `http` URI whose host is exactly `localhost`, `127.0.0.1` or `[::1]`, with no userinfo and no fragment.
- `token_endpoint_auth_method` is absent, `none` or `private_key_jwt`. The declared value SHALL be honoured: a document declaring `private_key_jwt` MUST NOT be treated as `none` because another member lists `none`.
- `client_secret` and `client_secret_expires_at` are absent.
- `grant_types`, when present, includes `authorization_code`.
- `response_types`, when present, includes `code`.
- `scope`, when present, is valid and names `read` or `readwrite`. When absent, it defaults to the DCR default scope.

A `private_key_jwt` document SHALL also meet all of these:
- It carries a `jwks_uri` that meets every rule the client identifier URL must meet.
- That `jwks_uri` is same origin (scheme, canonical host and effective port) as the client identifier URL.
- It carries no `jwks` member.
- When it carries `token_endpoint_auth_signing_alg`, the value is `RS256`, `PS256` or `ES256`.

The projection SHALL carry the auth method and, for a `private_key_jwt` document, the `jwks_uri` and the signing alg, or none when the alg is absent.

#### Scenario: The Claude Code document validates
- **WHEN** the document is `{"client_id":"https://claude.ai/oauth/claude-code-client-metadata","client_name":"Claude Code","redirect_uris":["http://localhost/callback","http://127.0.0.1/callback"],"grant_types":["authorization_code","refresh_token"],"response_types":["code"],"token_endpoint_auth_method":"none"}` served at that URL
- **THEN** validation SHALL succeed

#### Scenario: The ChatGPT document validates as a key-authenticated client
- **WHEN** the document is `{"client_id":"https://chatgpt.com/oauth/client.json","client_uri":"https://chatgpt.com/","redirect_uris":["https://chatgpt.com/connector_platform_oauth_redirect"],"token_endpoint_auth_method":"private_key_jwt","token_endpoint_auth_methods_supported":["none","private_key_jwt"],"grant_types":["authorization_code","refresh_token"],"response_types":["code"],"client_name":"ChatGPT","logo_uri":"https://persistent.oaistatic.com/sonic/misc/openai-logo.png","token_endpoint_auth_signing_alg":"RS256","jwks_uri":"https://chatgpt.com/oauth/jwks.json"}` served at that URL
- **THEN** validation SHALL succeed with auth method `private_key_jwt`, `jwks_uri` `https://chatgpt.com/oauth/jwks.json` and signing alg `RS256`

#### Scenario: A mismatched client_id is refused
- **WHEN** the document's `client_id` differs from the URL by case, a trailing slash or a query
- **THEN** the response SHALL be `invalid_client`

#### Scenario: Shared-secret clients are refused
- **WHEN** the document declares `token_endpoint_auth_method: client_secret_post` or carries a `client_secret` member
- **THEN** the response SHALL be `invalid_client`

#### Scenario: Other key-based methods are refused
- **WHEN** the document declares `client_secret_jwt` or `tls_client_auth`
- **THEN** the response SHALL be `invalid_client` with the logged reason `client_metadata.auth_method_unsupported`

#### Scenario: A cross-origin or inline key source is refused
- **WHEN** a `private_key_jwt` document's `jwks_uri` is on another host, another port or carries a query, or the document carries an inline `jwks`, or no `jwks_uri`
- **THEN** the response SHALL be `invalid_client` and the JWKS SHALL NOT be fetched

#### Scenario: An unsupported signing alg is refused
- **WHEN** a `private_key_jwt` document declares `token_endpoint_auth_signing_alg: HS256`, `none` or `EdDSA`
- **THEN** the response SHALL be `invalid_client`

#### Scenario: A remote plaintext redirect is refused
- **WHEN** `redirect_uris` contains `http://example.com/cb`
- **THEN** the response SHALL be `invalid_client`

### Requirement: The consent approval SHALL bind the projection the consent page showed
The consent page SHALL bind into the signed consent state the SHA-256 of the canonical authorization projection it rendered. That projection consists of the client identifier URL, the sorted list of normalised redirect URIs and the sorted scope tokens. For a `private_key_jwt` document only, it also includes the auth method, the `jwks_uri` and the signing alg (or null), and never the key material. For a `none` document the hashed object SHALL be byte-identical to the one used before key-based authentication existed. `client_name` and every other self-described document field SHALL be outside the hash.

The approval SHALL re-resolve the document, and for a `private_key_jwt` document its JWKS, under the cache and admission rules. It SHALL refuse with `invalid_client`, minting and writing nothing, when the re-resolved authorization projection's hash differs from the bound one. The redirect URIs, scope, auth method and key source an approval writes SHALL be exactly those that were shown. Detecting a change at the origin while the cached projection is still fresh is not required.

The token endpoint, the refresh path and the revocation endpoint MUST NOT fetch the document. Their only outbound fetch SHALL be the refetch of a row's pinned `jwks_uri`, made when an assertion's `kid` is missing from the row's snapshot or the snapshot is older than 24 hours. A changed or withdrawn document SHALL end no existing grant.

#### Scenario: An expired re-resolution returning a different projection is refused
- **WHEN** the consent page rendered a projection, its cache entry has expired or was never cached, and the approval's fetch returns a document whose `redirect_uris` differ
- **THEN** the approval SHALL be refused with `invalid_client` and no code or client row SHALL be written

#### Scenario: A name-only change does not block approval
- **WHEN** an uncached re-resolution at approval returns the rendered document with only `client_name` changed
- **THEN** the approval SHALL succeed

#### Scenario: A redirect-order-only change does not block approval
- **WHEN** an uncached re-resolution at approval returns the same `redirect_uris` in a different order with the same scope
- **THEN** the approval SHALL succeed

#### Scenario: A scope change is refused
- **WHEN** an uncached re-resolution at approval returns a document whose `scope` differs from the rendered one
- **THEN** the approval SHALL be refused with `invalid_client` and nothing SHALL be written

#### Scenario: An auth-method or key-source change is refused
- **WHEN** an uncached re-resolution at approval returns a document whose `token_endpoint_auth_method`, `jwks_uri` or `token_endpoint_auth_signing_alg` differs from the rendered one
- **THEN** the approval SHALL be refused with `invalid_client` and nothing SHALL be written

#### Scenario: A key rotation does not block approval
- **WHEN** the JWKS served at approval contains different keys from the one fetched at the consent render, with the same `jwks_uri`
- **THEN** the approval SHALL succeed and SHALL store the keys fetched at approval, unless the row already holds a newer snapshot of the same `jwks_uri`, which it SHALL keep (a snapshot never moves backwards)

#### Scenario: Existing public-client hashes are unchanged
- **WHEN** the authorization hash of a `none` projection is computed before and after this change
- **THEN** the two values SHALL be equal

#### Scenario: A fresh cached projection is approved as shown
- **WHEN** the origin's document changes while the cached projection the page rendered is still fresh, and the user approves
- **THEN** the approval SHALL succeed and SHALL write the projection that was shown

#### Scenario: The token endpoint never fetches the document
- **WHEN** a code minted for a CIMD client is exchanged, its refresh token rotated, or its token revoked
- **THEN** no request SHALL be made to the document URL

### Requirement: A CIMD client SHALL be persisted as one synthetic client row per URL and user, written only on approval
On an approval the server SHALL insert or refresh an `oauth_clients` row in the transaction that inserts the authorization code. The row SHALL carry:
- a `client_id` of `cimd_` followed by the first 59 hex characters of HMAC-SHA256 over the URL, a newline and the owning user's id (or `-` when there is none), keyed by a key derived from the deployment's `SECRET_KEY`;
- `client_id_metadata_url` set to the URL;
- `user_id` set to the approving user;
- no secret hash;
- `token_endpoint_auth_method` set to the projection's auth method;
- for a `private_key_jwt` projection, its `jwks_uri`, its signing alg, the validated normalised JWKS snapshot resolved for this approval, and `jwks_fetched_at`. `jwks_fetched_at` SHALL be that snapshot's origin fetch time, carried unchanged through any cache hit, and never the approval time;
- the projection's name, redirect URIs and scope.

The same statement SHALL stamp `last_used_at`.

A consent page render and a denial SHALL NOT write a client row. An approval SHALL be refused with `invalid_client`, writing nothing, when the existing row, read under its lock in the approval transaction, is `private_key_jwt` and the re-resolved document is `none`.

The database SHALL enforce all of the following:
- a row carries a metadata URL exactly when its id begins `cimd_`;
- such a row has no secret hash and an auth method of `none` or `private_key_jwt`;
- only such a row may be `private_key_jwt`;
- a `private_key_jwt` row carries a `jwks_uri`, a snapshot and a fetch time;
- every other row carries none of the key-source columns;
- a stored signing alg is one of `RS256`, `PS256`, `ES256`.

Rows existing before this change SHALL keep auth method `none` without a backfill.

#### Scenario: Two users connecting the same client get separate rows
- **WHEN** two users in multi-user mode each approve the same CIMD URL
- **THEN** two client rows SHALL exist, each bound to its own user, and neither approval SHALL be refused as cross-user

#### Scenario: A repeat approval reuses the row
- **WHEN** one user approves the same CIMD URL twice
- **THEN** one client row SHALL exist and its name, redirect URIs, scope and, for a `private_key_jwt` client, key snapshot SHALL reflect the second approval

#### Scenario: A denial writes nothing
- **WHEN** a user denies a CIMD client that has no row
- **THEN** no client row SHALL be created

#### Scenario: A cache hit at approval does not reset the snapshot's age
- **WHEN** a JWKS fetched at T0 is served from the cache to an approval at T0 + 23 h 50 min
- **THEN** the row's `jwks_fetched_at` SHALL be T0, and a token request at T0 + 24 h 1 min SHALL meet the stale trigger

#### Scenario: A key-authenticated row is never downgraded
- **WHEN** a user's row for a URL is `private_key_jwt` and the document now declares `none`, and the user approves
- **THEN** the approval SHALL be refused with `invalid_client`, the row SHALL be unchanged and no code SHALL be written

#### Scenario: Concurrent first approvals do not downgrade
- **WHEN** two approvals for the same URL and user find no row, one declaring `private_key_jwt` and one `none`, and the `private_key_jwt` one commits first
- **THEN** the `none` approval SHALL be refused `client_metadata.auth_method_downgrade` with `invalid_client`, the row SHALL stay `private_key_jwt` and no code SHALL be written for it

#### Scenario: The database refuses a key-authenticated row without keys
- **WHEN** a row with auth method `private_key_jwt` and a NULL `jwks_snapshot`, or a non-CIMD row with `private_key_jwt`, is inserted directly
- **THEN** the insert SHALL fail on a CHECK constraint

#### Scenario: Grant machinery is unchanged
- **WHEN** a CIMD client's code is exchanged and its refresh token rotated or replayed
- **THEN** grant families, code lineage, the absolute grant lifetime and refresh-reuse revocation SHALL behave exactly as for a registered client

### Requirement: The token and revocation endpoints SHALL accept a CIMD URL as the caller's client identifier
Wherever `/token` or `/revoke` compares a caller-supplied `client_id` against a client row, a value byte-equal to that row's `client_id_metadata_url` SHALL match, and for a CIMD row nothing else SHALL match, through one shared definition. For a `private_key_jwt` row the `client_id` parameter SHALL be optional at both endpoints. At `/revoke`, when it is absent and the token's row is `private_key_jwt`, the caller-identity comparison SHALL be skipped and the client SHALL be authenticated directly against that credential-resolved row, whose verified `iss` and `sub` bind the identity. For every other row, `/revoke` SHALL continue to require `client_id` and answer its absence with the constant 200. All other refusal rules, lookups and constant responses of those endpoints SHALL be unchanged.

#### Scenario: Claude Code exchanges with its URL
- **WHEN** a code issued to a CIMD row is exchanged with `client_id` equal to the document URL
- **THEN** the exchange SHALL succeed

#### Scenario: Another URL does not match
- **WHEN** the same code is exchanged with a different `https://` client_id
- **THEN** it SHALL be refused as a client mismatch and nothing SHALL be revoked

#### Scenario: Revocation with the URL
- **WHEN** `/revoke` presents a CIMD client's token with `client_id` equal to its URL
- **THEN** the grant family SHALL be revoked

#### Scenario: A key-authenticated client revokes without client_id
- **WHEN** `/revoke` presents a `private_key_jwt` client's token with a valid assertion and no `client_id`
- **THEN** the grant family SHALL be revoked

#### Scenario: Revocation without client_id and without an assertion is an authentication failure
- **WHEN** `/revoke` presents a live `private_key_jwt` client's token with no `client_id` and no assertion, or with a malformed assertion, or with a validly signed assertion whose `iss` is another URL
- **THEN** the response SHALL be `invalid_client` 401, nothing SHALL be revoked, and `oauth_revoke_refused` SHALL carry `reason` `assertion_missing`, `assertion_malformed` or `assertion_claims_invalid` respectively

#### Scenario: A public client still cannot revoke without client_id
- **WHEN** `/revoke` presents a `none` CIMD client's token with no `client_id`
- **THEN** the response SHALL be the existing constant 200 and nothing SHALL be revoked

## ADDED Requirements

### Requirement: A private_key_jwt client's JWKS SHALL be fetched at authorization under the document's bounds and validated before it is stored
For a `private_key_jwt` projection, `GET` and `POST /authorize` SHALL resolve the `jwks_uri`, after the document validates, only through the hardened transfer fetch with the document's parameters: no redirects, a 10 s deadline, a 64 KiB cap, identity encoding, the SSRF deny list with the connection pinned, and the proxy environment ignored.

Admission SHALL follow the document's order, keyed by the `jwks_uri`:
1. a JWKS cache hit and a join of an in-progress fetch of the same `jwks_uri` SHALL charge nothing;
2. the 30 s failure cooldown;
3. one charge to the same per-address budget the document fetch uses, separate from the document's charge;
4. at most 1 fetch in flight per address slot;
5. at most 8 in flight per process.

Only a validated JWKS SHALL be cached, in a bounded in-process cache under the document cache's TTL rules.

The JWKS SHALL be a JSON object whose `keys` list holds 1 to 16 objects. Any key carrying `d`, `p`, `q`, `dp`, `dq`, `qi`, `oth` or `k` SHALL refuse the whole set. A key SHALL be ignored when any of these holds:
- its `use` is present and not `sig`;
- its `key_ops` is present without `verify`;
- its `kty` is neither `RSA` nor `EC`;
- it is EC with a `crv` other than `P-256`;
- its `alg` is present and outside `RS256`, `PS256`, `ES256`;
- it is RSA with a modulus under 2,048 or over 8,192 bits, or an exponent that is not an odd integer from 3 to 2³²;
- it does not import as a public key.

Two usable keys sharing a `kid` SHALL refuse the set. At least one usable key SHALL be required. The stored snapshot SHALL contain only the usable keys, reduced to their public members.

Every failure SHALL answer the constant `invalid_client` 400 with a closed `client_metadata.jwks_*` reason in the log only. No consent page SHALL be rendered for a client whose JWKS does not validate.

#### Scenario: A JWKS with a private key is refused
- **WHEN** the served JWKS contains an RSA key carrying `d`
- **THEN** the response SHALL be the constant `invalid_client` and the logged reason SHALL be `client_metadata.jwks_private_key`

#### Scenario: Weak and encryption keys are not usable
- **WHEN** the served JWKS contains only a 1,024-bit RSA signing key and a 2,048-bit RSA key with `use: enc`
- **THEN** the response SHALL be the constant `invalid_client` with reason `client_metadata.jwks_no_usable_key`

#### Scenario: A cached document does not make the JWKS fetch free
- **WHEN** the document is a cache hit and the JWKS is not cached
- **THEN** the JWKS fetch SHALL be charged one unit of the caller's per-address budget

#### Scenario: Redirects and private addresses are refused for the JWKS
- **WHEN** the `jwks_uri` answers 302, or its host resolves to a private address
- **THEN** the response SHALL be the constant `invalid_client` and no redirect SHALL be followed and no private address connected to

### Requirement: A private_key_jwt client SHALL authenticate at the token and revocation endpoints with a verified, single-use JWT assertion
When the client row resolved from the credential has auth method `private_key_jwt`, `/token`, for both grants, and `/revoke` SHALL authenticate the client only by a `client_assertion` verified against that row's stored JWKS snapshot. The checks are:
- `client_assertion_type` SHALL equal exactly `urn:ietf:params:oauth:client-assertion-type:jwt-bearer`.
- The assertion SHALL be a compact JWS of at most 8,192 bytes.
- Its header SHALL NOT carry `crit`, `jku`, `jwk`, `x5u` or `x5c`.
- Its `alg` SHALL be `RS256`, `PS256` or `ES256`, and equal to the row's pinned signing alg when one is pinned.
- The verification key SHALL be selected from the snapshot only. With a `kid`, it is the key with that `kid`. Without one, it is the single key compatible with the alg. The key's type and curve SHALL fit the alg, and its `alg` member SHALL equal the header's when present.
- The signature SHALL be verified with exactly that key and that alg.
- `iss` and `sub` SHALL be byte-equal to the row's metadata URL.
- `aud` SHALL be a string or a one-element array whose value is the issuer identifier or `{issuer}/token`, or additionally, at `/revoke`, `{issuer}/revoke`.
- `exp` SHALL be present and later than now with no leeway, and no more than 330 s ahead.
- `iat`, when present, SHALL be no more than 30 s ahead and no more than 300 s before `exp`.
- `nbf`, when present, SHALL be no more than 30 s ahead.
- `jti` SHALL be a string of 1 to 256 characters.

A `private_key_jwt` row presenting no assertion, or presenting a `client_secret` with or without one, SHALL be refused. A CIMD row with auth method `none` presenting `client_assertion` or `client_assertion_type` SHALL be refused. Registered (DCR) clients SHALL be authenticated exactly as before. Every such refusal SHALL be the endpoint's existing client-authentication failure.

#### Scenario: ChatGPT exchanges a code with a valid assertion
- **WHEN** a code issued to ChatGPT's `private_key_jwt` row is exchanged with a correct verifier and redirect URI, and an RS256 assertion signed by a snapshot key with `iss`=`sub`=`https://chatgpt.com/oauth/client.json`, `aud` = the token endpoint URL, a fresh `jti` and `exp` 60 s ahead
- **THEN** the exchange SHALL succeed and SHALL mint one grant family

#### Scenario: A key-bearing header is never a key source
- **WHEN** an assertion carries a `jwk` or `jku` header naming a key the attacker holds, and is signed with that key
- **THEN** the request SHALL be refused `invalid_client` and the header's key SHALL NOT be fetched or used

#### Scenario: Algorithm confusion is refused
- **WHEN** an assertion uses `alg: none`, `alg: HS256` keyed by the RSA public key, or `PS256` against a row pinned to `RS256`
- **THEN** the request SHALL be refused `invalid_client`

#### Scenario: An assertion for another client is refused
- **WHEN** an assertion validly signed by a different CIMD client's key, with `iss`=`sub` = that client's URL, is presented with ChatGPT's refresh token
- **THEN** the request SHALL be refused `invalid_client` and nothing SHALL be minted or revoked

#### Scenario: A multi-valued audience is refused
- **WHEN** `aud` is `["https://mcp.example","https://other.example"]`
- **THEN** the request SHALL be refused `invalid_client`

#### Scenario: Missing authentication is not a downgrade
- **WHEN** a `private_key_jwt` client's refresh token is presented with no assertion, or with a `client_secret`
- **THEN** the request SHALL be refused `invalid_client`

#### Scenario: A public CIMD client presenting an assertion is refused
- **WHEN** a `none` CIMD client's code exchange carries a `client_assertion`
- **THEN** the request SHALL be refused `invalid_client` with reason `invalid_client.assertion_unexpected`

#### Scenario: A registered client is unaffected
- **WHEN** a DCR `none` client exchanges a code with a `client_assertion` parameter present
- **THEN** the exchange SHALL behave exactly as it did before this change

#### Scenario: Expiry has no leeway and lifetime is bounded
- **WHEN** an assertion's `exp` is one second in the past, or 600 s in the future
- **THEN** the request SHALL be refused `invalid_client`

### Requirement: An assertion jti MUST be accepted at most once until its expiry, and a full replay cache MUST refuse
The server SHALL record each accepted assertion's `jti` in a bounded in-process cache. The key SHALL be derived from the assertion's `iss` and `jti`, not from the client row, and the entry SHALL be held until the assertion's `exp`. An assertion whose `jti` is already recorded under the same `iss` SHALL be refused. Only an assertion that passed every other check SHALL be recorded. Expired entries SHALL be purged before an insert. When the cache holds its maximum of unexpired entries, a new assertion SHALL be refused, and no unexpired entry SHALL ever be evicted to admit it. The cache's contents SHALL NOT be logged.

#### Scenario: A replayed assertion is refused
- **WHEN** an assertion that succeeded at `/token` is presented again with another valid credential before its `exp`
- **THEN** the second request SHALL be refused `invalid_client` with reason `invalid_client.assertion_replayed`

#### Scenario: A jti spent on one user's row is spent for every row of that client
- **WHEN** an assertion accepted for user A's ChatGPT grant is presented with user B's ChatGPT refresh token
- **THEN** the request SHALL be refused as replayed

#### Scenario: A full cache refuses rather than evicts
- **WHEN** the cache is full of unexpired entries and a new valid assertion is presented
- **THEN** it SHALL be refused with reason `assertion_replay_cache_full` and every recorded `jti` SHALL remain refused

#### Scenario: Unverified assertions do not occupy the cache
- **WHEN** many assertions with bad signatures and distinct `jti` values are presented
- **THEN** none SHALL be recorded

### Requirement: Assertion authentication SHALL sit at the existing client-authentication step and MUST NOT cause or prevent any revocation
At `/token`, assertion authentication SHALL run at exactly the position the client-secret check occupies:
- **live code:** after the code row is resolved and locked, the synthetic-id refusal, the caller-`client_id` comparison and the client-row lookup, and before code expiry, redirect URI and PKCE;
- **spent code:** at the same position, before redirect URI, PKCE and the replay branch;
- **live refresh token:** after the reuse decision, the synthetic-id refusal, the caller-`client_id` comparison and the client-row lookup, and before the absolute-lifetime and expiry checks.

A failed assertion on a live credential SHALL answer the existing `invalid_client` 401 byte for byte, whatever the sub-reason. On a spent code it SHALL answer the response for a code that names no row, byte for byte. A spent code SHALL reach the replay branch only after a successful assertion, together with the redirect URI and PKCE checks.

The refresh-reuse decision SHALL be unchanged: a rotated-away refresh token SHALL revoke its family without the assertion being read, verified or recorded.

At `/revoke`, the order SHALL be: the synthetic-id check (constant 200) → the token lookup → the client-row lookup → the caller-identity comparison (constant 200 on mismatch), skipped only when `client_id` is absent and the row is `private_key_jwt` → assertion authentication (the existing `invalid_client` 401 on failure) → revocation.

A failed, missing or replayed assertion MUST NOT revoke anything at any endpoint.

#### Scenario: A spent code with a bad assertion looks unknown and revokes nothing
- **WHEN** a spent code is presented with the correct verifier and redirect URI but an assertion with a bad signature
- **THEN** the response SHALL be identical in status, headers and body to the unknown-code response, nothing SHALL be revoked, and the record SHALL carry `invalid_grant.spent_code_assertion_signature_invalid`

#### Scenario: A spent code with a valid fresh assertion revokes its family
- **WHEN** a spent code is presented again with the correct verifier, redirect URI and a valid fresh assertion
- **THEN** the family its first exchange issued SHALL be revoked, as for any client

#### Scenario: A live code's assertion failure precedes the expiry check
- **WHEN** an expired, unspent code is presented with an invalid assertion
- **THEN** the response SHALL be the `invalid_client` 401, not the code-expired refusal

#### Scenario: Refresh reuse is unchanged for key-authenticated clients
- **WHEN** a rotated-away ChatGPT refresh token is presented with no assertion
- **THEN** its family SHALL be revoked and the response SHALL be the constant reuse `invalid_grant`

#### Scenario: Revocation with a failed assertion revokes nothing
- **WHEN** `/revoke` presents a ChatGPT token with `client_id` equal to its URL and an expired assertion
- **THEN** the response SHALL be `invalid_client` 401 and nothing SHALL be revoked

### Requirement: An unknown assertion kid or a stale snapshot SHALL trigger a bounded refetch of the row's pinned jwks_uri, with no database lock held across the fetch
A `/token` or `/revoke` request SHALL trigger a pre-phase, run before the handler's main session is opened, when all of these hold:
- it carries an assertion with an allowed `alg`;
- its credential (a code, a refresh token, or at `/revoke` a token) resolves to a `private_key_jwt` client row;
- the assertion's unverified `iss` equals that row's URL;
- either the assertion's header names a `kid` absent from the row's snapshot (the kid-miss trigger), or the row's `jwks_fetched_at` is older than 24 hours, a constant equal to the JWKS cache ceiling (the stale trigger);
- the credential is eligible by every check the main phase would make that needs no network I/O, as follows:
  - **a code:** unspent and unexpired, the caller's `client_id` matching when present, `redirect_uri` equal to the code's, and a well-formed PKCE verifier matching its challenge;
  - **a refresh token:** unrevoked, unexpired and inside its grant's absolute lifetime, with the caller's `client_id` matching when present;
  - **at `/revoke`:** an unrevoked, unexpired token inside its grant's absolute lifetime, with the caller's `client_id` matching when present.

A revoked refresh token SHALL never trigger a fetch, and the refresh-reuse branch SHALL never verify the assertion, record its `jti`, or let it affect the reuse decision; the advisory pre-phase may parse its unverified header and `iss` only. The eligibility checks SHALL be advisory: they produce no response, record or state change. The main transaction SHALL re-run every check under its locks, so every response, including the spent-code and absolute-lifetime responses, SHALL be identical to the response without the pre-phase.

The pre-phase SHALL behave as follows:
- It SHALL resolve the row in a short read-only session and close that session before any network I/O. It SHALL hold no session, transaction or row lock across the fetch.
- It SHALL first consult the process JWKS cache for that row's pinned `jwks_uri`. For the stale trigger, a cache entry counts only if it was fetched within the last 24 hours.
- It SHALL fetch at most once per `jwks_uri` per 60 s process-wide, success or failure, whichever trigger caused it. It SHALL join an in-progress fetch of the same `jwks_uri` and observe the process-wide in-flight cap. One refetch SHALL serve both triggers.
- The fetch SHALL go only through the hardened transfer fetch, and its result SHALL be validated by the authorization-time JWKS rules.
- A validated result SHALL replace the row's snapshot, and set `jwks_fetched_at` to the snapshot's origin fetch time (carried through a cache hit), in a short transaction. That write SHALL be conditioned on the row's `jwks_uri` being unchanged and its `jwks_fetched_at` being older than the result's.
- A failed refetch (network failure, fetch refusal, invalid JWKS, cooldown or busy cap) SHALL write nothing and MUST NOT advance `jwks_fetched_at`.
- The pre-phase SHALL decide no response.

The main phase SHALL verify against the snapshot it reads under its own locks. That is:
- the refreshed snapshot after a successful refetch, so an assertion whose `kid` the refreshed set no longer contains SHALL be refused;
- the stored snapshot after a failed refetch.

No refetch SHALL occur for a request whose credential resolves to no `private_key_jwt` row, or for any document URL. The refetch SHALL also be available while `OAUTH_CIMD_ENABLED` is false.

#### Scenario: A rotated key is picked up
- **WHEN** ChatGPT signs with a new `kid` that its `jwks_uri` now serves and the row's snapshot lacks
- **THEN** the request SHALL succeed after one refetch and the row's snapshot SHALL contain the new key

#### Scenario: A stale snapshot is refreshed before verification
- **WHEN** a row's `jwks_fetched_at` is 25 hours old and ChatGPT presents a valid assertion whose `kid` is in both the stored snapshot and the served JWKS
- **THEN** exactly one fetch of the `jwks_uri` SHALL be made before verification, the request SHALL succeed, and the row's `jwks_fetched_at` SHALL be advanced

#### Scenario: A key removed by the client is refused after a stale refetch
- **WHEN** a row's snapshot is older than 24 hours and contains key K, the served JWKS no longer contains K, and an assertion signed with K is presented with a valid refresh token
- **THEN** the row's snapshot SHALL be replaced without K, the request SHALL be refused `invalid_client` with reason `invalid_client.assertion_kid_unknown`, and nothing SHALL be minted or revoked

#### Scenario: An origin outage does not break refreshes
- **WHEN** a row's snapshot is older than 24 hours and the `jwks_uri` times out, answers 500, serves an invalid JWKS, or is in its refetch cooldown, and a valid assertion signed with a stored key is presented with a valid refresh token
- **THEN** the refresh SHALL succeed against the stored snapshot
- **AND** the row's snapshot and `jwks_fetched_at` SHALL be unchanged

#### Scenario: A failed stale refetch is retried after the cooldown
- **WHEN** a stale refetch failed and a further request for the same row arrives more than 60 s later
- **THEN** a new fetch SHALL be attempted, because `jwks_fetched_at` was not advanced

#### Scenario: A failed kid-miss refetch refuses
- **WHEN** an assertion names a `kid` absent from the stored snapshot and the refetch fails
- **THEN** the request SHALL be refused `invalid_client` with reason `invalid_client.assertion_kid_unknown`

#### Scenario: A client_id alone triggers nothing
- **WHEN** an unauthenticated caller posts to `/token` with `client_id=https://chatgpt.com/oauth/client.json`, an unknown code value and an assertion naming a random `kid`
- **THEN** no fetch SHALL be made

#### Scenario: A dead or unproven code triggers no fetch
- **WHEN** a request presents a `private_key_jwt` client's spent code, or its expired code, or its live code with a wrong or malformed PKCE verifier or a wrong redirect URI, together with an assertion whose `iss` is the row's URL and whose `kid` is unknown
- **THEN** no fetch SHALL be made
- **AND** the response SHALL be byte-identical to the one the same request receives with the pre-phase disabled

#### Scenario: A revoked or rotated-away refresh token triggers no fetch
- **WHEN** a revoked or rotated-away refresh token of a `private_key_jwt` client, or one past its grant's absolute lifetime, is presented with an unknown-`kid` assertion
- **THEN** no fetch SHALL be made, and a rotated-away token SHALL still revoke its family with the constant reuse response

#### Scenario: Revocation of a dead token triggers no fetch
- **WHEN** `/revoke` presents a revoked or expired `private_key_jwt` token, or a live one under a mismatched `client_id`, with an unknown-`kid` assertion
- **THEN** no fetch SHALL be made

#### Scenario: Refetches are rate-bounded per key source
- **WHEN** 20 requests holding valid credentials for the same client each present an unknown `kid`, or each find the snapshot stale, within one minute
- **THEN** at most one fetch of the `jwks_uri` SHALL be made

#### Scenario: No lock is held during the fetch
- **WHEN** a kid-miss or stale refetch is in progress
- **THEN** the application SHALL hold no open transaction and no row or advisory lock on the database

#### Scenario: A concurrent re-pin is not overwritten
- **WHEN** an approval changes the row's `jwks_uri` while a refetch of the old `jwks_uri` is in flight
- **THEN** the refetch SHALL NOT write its result to the row
