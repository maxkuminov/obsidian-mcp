# native-oidc-login Specification

## Purpose
Provide optional native OIDC browser sign-in for explicitly linked existing users while preserving local vault ownership, session safeguards and MCP credentials.
## Requirements
### Requirement: Optional existing-account OIDC
The server SHALL keep OIDC disabled by default and, when enabled, require an HTTPS issuer and callback and confidential client credentials, and SHALL resolve a verified identity only to an existing local user through a database-held link, never by provisioning a user, changing a role or assigning a vault. When `OIDC_REQUIRED_GROUP` is non-empty a valid ID token lacking that exact group SHALL be refused; when it is empty no group check SHALL be applied.

#### Scenario: Unlinked identity
- **WHEN** a valid ID token's `(iss, sub)` is bound to no user and matches no live pending expectation
- **THEN** login is denied with the generic OIDC refusal page and no user, role or vault is created or changed

#### Scenario: Configured group missing
- **WHEN** `OIDC_REQUIRED_GROUP` is set and a valid ID token for a bound user lacks the exact group
- **THEN** login is denied with reason `entitlement`

#### Scenario: No group configured
- **WHEN** `OIDC_REQUIRED_GROUP` is empty and a valid ID token for a bound user carries no `groups` claim
- **THEN** login proceeds

### Requirement: Browser login transaction
The server SHALL bind a short-lived single-use state, nonce and PKCE S256 transaction to the initiating browser and validate signed ID-token issuer, audience, expiry, nonce and subject before starting the existing revocable app session.
#### Scenario: Replayed or cross-browser callback
- **WHEN** callback state is expired, consumed or bound to another browser
- **THEN** login is denied and no app session is created
#### Scenario: Existing account disabled concurrently
- **WHEN** the account is disabled or its session version changes during login
- **THEN** the existing session mint guard refuses login

### Requirement: Preserve MCP identity and safe rollout
The server SHALL preserve user IDs, vault ownership, local MCP issuer, grants, tokens and API keys, disable bootstrap while OIDC is enabled, and enforce an explicit local password-login switch.
#### Scenario: MCP authorization after SSO
- **WHEN** an authorized existing user signs in from the local authorize flow
- **THEN** the safe local next path resumes that same flow as the same user
#### Scenario: Password login disabled
- **WHEN** local login is disabled
- **THEN** its form is hidden and password POST is refused

### Requirement: Safe activation and provider profile
The server SHALL reject startup with OIDC enabled unless multi-user mode is enabled and sandbox mode is disabled, SHALL reject multi-user configuration with both login methods disabled, and SHALL reject any non-empty `OIDC_SUBJECT_USER_IDS` with a message naming the panel action that replaces it. The supported provider profile SHALL be any OpenID Connect provider whose discovery document, authorization, token and JWKS endpoints are HTTPS on the issuer's origin; providers whose endpoints span origins SHALL remain unsupported.

#### Scenario: Single-user exposure
- **WHEN** OIDC is enabled in single-user mode
- **THEN** startup fails before routes serve

#### Scenario: Retired mapping setting
- **WHEN** `OIDC_SUBJECT_USER_IDS` is set to a non-empty object, with OIDC enabled or not
- **THEN** startup fails with a message that lists the upgrade sequence (confirm a local admin password, set `LOCAL_LOGIN_ENABLED=true`, remove the setting, start, bind each former subject on its user's edit page, verify SSO and `/authorize`, optionally disable password login) and names the `docs/native-oidc.md` section that describes it

#### Scenario: Cross-origin endpoint
- **WHEN** discovery advertises a token or JWKS endpoint on a host other than the issuer's
- **THEN** login is refused with reason `provider`

### Requirement: Strict protocol and entitlement validation
The server SHALL require exact discovery issuer, a signature validated against the issuer's freshly fetched JWKS using an algorithm in `{RS256, PS256, ES256}` intersected with the discovery document's `id_token_signing_alg_values_supported` (RS256 when that field is absent), nonempty string `iss`/`sub`/`nonce` with `sub` at most 255 characters, integer `exp`/`iat`, `aud` equal to the client ID or its singleton array, `azp` equal to the client ID if present, and zero clock skew. Discovery endpoints SHALL use HTTPS on the issuer origin without credentials or fragment; HTTP requests SHALL disable redirects and environment proxies and use bounded time and response size. When a group is required, the groups claim SHALL be an array of at most 256 strings of at most 512 characters and admission SHALL require exact case-sensitive element equality.

#### Scenario: Wrong audience or malformed groups
- **WHEN** a signed token includes an extra audience, a foreign azp, scalar groups or a case-variant group while a group is required
- **THEN** login is denied

#### Scenario: ES256 provider
- **WHEN** discovery advertises `ES256` and the ID token is ES256-signed by a key in the JWKS
- **THEN** signature validation succeeds

#### Scenario: Symmetric or unsigned token
- **WHEN** the ID token header names `HS256` or `none`, whatever discovery advertises
- **THEN** login is denied with reason `token`

#### Scenario: Algorithm not advertised
- **WHEN** discovery advertises only `RS256` and the token is PS256-signed
- **THEN** login is denied with reason `token`

### Requirement: Ephemeral provider credentials
The server SHALL request the scopes `openid email profile` plus any configured `OIDC_EXTRA_SCOPES`, never `offline_access`, and keep provider codes/tokens only transiently during callback. They SHALL NOT enter cookies, app sessions, databases, caches, events, exceptions or application access logs. Callback tokens SHALL be discarded before the database phase of the callback; deployments SHALL suppress callback query logging at their external proxy.

#### Scenario: Callback audit
- **WHEN** a callback succeeds or fails
- **THEN** application logs contain no callback query, provider credential, subject or email

#### Scenario: offline_access cannot be requested
- **WHEN** `OIDC_EXTRA_SCOPES` contains `offline_access`
- **THEN** startup fails

### Requirement: Bounded transactions and audit
The server SHALL atomically consume transactions once, expire them after five minutes, clean expired entries before admission and refuse new starts with Retry-After when all 1024 slots are live. Per-address start/callback limits and a process-global start budget SHALL bound public requests. Catalogued bounded events SHALL record start/success/refusal, with success only after app-session commit and closed refusal reasons, without state/nonce/verifier/client-secret/token/subject/raw-groups or exception strings.
#### Scenario: Full store and repeated callback
- **WHEN** capacity is full or state was already consumed
- **THEN** new admission fails without evicting a live transaction and callback cannot mint a session

### Requirement: OIDC links SHALL be stored on the user row with uniqueness enforced by the database
Migration 037 SHALL add to `users` the nullable columns `oidc_issuer`, `oidc_subject`, `oidc_linked_at`, `oidc_expected_issuer`, `oidc_expected_email` and `oidc_expected_set_at`, a UNIQUE constraint on `(oidc_issuer, oidc_subject)`, a UNIQUE constraint on `(oidc_expected_issuer, oidc_expected_email)`, and CHECK constraints making each triple all-NULL or all-non-NULL and forbidding a row that is both bound and pending. The ORM model SHALL declare the same constraints so that `alembic check` reports no operations, and the migration SHALL follow the schema gate's conventions: each of the six columns SHALL carry 037's column comment marker, mirrored on the model; each constraint SHALL carry 037's constraint comment marker; a same-named existing column SHALL be adopted only with the exact type, nullability, absent default and marker, and a same-named constraint only with the canonical definition, validated and marked, otherwise the migration SHALL refuse by name having changed nothing; `downgrade()` SHALL drop each object only if it carries its marker; the `search_path` SHALL be pinned; lock and statement timeouts SHALL be bounded; and no superuser SHALL be required.

#### Scenario: One identity, one user
- **WHEN** a write would bind an `(issuer, subject)` pair already bound to another user
- **THEN** the database SHALL reject it and the write SHALL be reported as a conflict

#### Scenario: Bound and pending at once is unrepresentable
- **WHEN** a write sets the expected columns on a bound row
- **THEN** the database SHALL reject it

#### Scenario: Schema gate
- **WHEN** migrations are applied to head
- **THEN** `alembic check` SHALL report "No new upgrade operations detected"
- **AND** the gate's asserted head revision SHALL be `037`

#### Scenario: Catalogue shape
- **WHEN** the catalogue is read after a fresh upgrade and after an upgrade from 036 with existing users
- **THEN** the six columns SHALL have the specified types, be nullable, have no default and carry the column marker, each CHECK and UNIQUE SHALL be validated, match its canonical `pg_get_constraintdef` and carry the constraint marker, and existing user rows SHALL be unchanged with all six columns NULL

#### Scenario: Correct shape without a marker is refused
- **WHEN** 037 runs over a database whose `users.oidc_subject` already exists as `character varying(255)` NULL with no comment, or whose `uq_users_oidc_identity` exists with the canonical definition but no marker
- **THEN** the migration SHALL refuse, naming the object, and SHALL change nothing

#### Scenario: Downgrade and re-upgrade
- **WHEN** the database is downgraded to 036 and upgraded again
- **THEN** the columns and constraints SHALL be removed and then recreated identically, and `alembic check` SHALL be clean

#### Scenario: Non-superuser migration
- **WHEN** 037 runs as the database-owning, non-superuser role
- **THEN** it SHALL complete

### Requirement: A pending expectation SHALL bind only on a verified, exactly matching email within seven days, once
When no user is bound to a validated token's `(iss, sub)`, and only then, the server SHALL examine the token's `email` and `email_verified`, and SHALL bind it to the user whose pending expectation has `oidc_expected_issuer` equal to `iss`, `oidc_expected_email` equal to the token's `email` after the same normalization (surrounding ASCII whitespace stripped, ASCII letters lower-cased, nothing else folded), `oidc_expected_set_at` no more than seven days ago, and only if the token's `email_verified` claim is the JSON boolean `true`. A bind SHALL write the issuer, subject and link time, clear the three expected columns and stamp `last_login_at` under the account guard, after re-checking every condition on a `FOR UPDATE` re-read, in the same transaction that `start_session` commits, so that a refused mint leaves no binding.

#### Scenario: First sign-in binds
- **WHEN** an active user is pending for `max@example.com` under the configured issuer, set yesterday, and a valid token carries `email` `Max@Example.com` with `email_verified: true`
- **THEN** the user SHALL become bound to the token's `(iss, sub)`, the expectation SHALL be cleared, and a session SHALL be minted for that user

#### Scenario: Absent or malformed email on the pending branch
- **WHEN** no user is bound to the token's `(iss, sub)` and its `email` claim is absent, not a string, longer than 254 characters or fails normalization
- **THEN** nothing SHALL be written and login SHALL be refused with reason `unlinked`

#### Scenario: Unverified email does not bind
- **WHEN** the token's email matches but `email_verified` is false, absent or the string `"true"`
- **THEN** nothing SHALL be written and login SHALL be refused with reason `email_unverified`

#### Scenario: Expired expectation does not bind
- **WHEN** the matching expectation was set eight days ago
- **THEN** nothing SHALL be written and login SHALL be refused with reason `unlinked`

#### Scenario: Expectation set under another issuer
- **WHEN** the expectation's issuer differs from the token's `iss`
- **THEN** it SHALL NOT match

#### Scenario: Refused mint leaves no binding
- **WHEN** a pending match passes every check but the session mint is refused because the account's session version changed
- **THEN** the user SHALL remain pending and unbound

#### Scenario: Concurrent bind of the same identity
- **WHEN** a second user's pending bind would write an `(iss, sub)` committed to another user in the meantime
- **THEN** the second bind SHALL roll back and login SHALL be refused with reason `link_conflict`

### Requirement: A bound user SHALL be resolved by issuer and subject alone
Once a user is bound, the callback SHALL resolve that user only by equality of `(oidc_issuer, oidc_subject)` with the validated token's `(iss, sub)`, before any examination of the email claim, and SHALL NOT refuse it for an absent or malformed email nor consult the token's `email`, `preferred_username` or any other claim to resolve, re-bind or refuse that user, except the group check when a group is required.

#### Scenario: Bound login with no email claim
- **WHEN** a bound user's valid token carries no `email` claim
- **THEN** that user SHALL sign in

#### Scenario: Bound login with a malformed email claim
- **WHEN** a bound user's valid token carries an `email` that is a number, a list, a 300-character string or a string with two `@`, and `email_verified` is the string `"true"`
- **THEN** that user SHALL sign in

#### Scenario: Provider account renamed
- **WHEN** a bound user's provider account changes its username and email
- **THEN** that user still signs in by `(iss, sub)`

#### Scenario: Address reassigned to another provider account
- **WHEN** a different provider account, with a new `sub`, now holds the bound user's former verified email
- **THEN** it SHALL NOT sign in as the bound user and SHALL be refused `unlinked`

#### Scenario: Bound identity matching another pending expectation
- **WHEN** a token's `(iss, sub)` is bound to user A and its email matches user B's pending expectation
- **THEN** the session SHALL be minted for A and B's expectation SHALL be left unchanged

### Requirement: OIDC refusals SHALL NOT reveal which local users exist
Every OIDC callback refusal SHALL render the same login page, the same message and the same status (401, or 503 with `Retry-After` for `capacity`), whatever the reason, including an unlinked identity, an unverified email, an expired expectation, a link conflict and an inactive account.

#### Scenario: Unlinked versus inactive
- **WHEN** one callback is refused `unlinked` and another `account`
- **THEN** the two response bodies SHALL be identical apart from the CSRF token and the nonce

### Requirement: The callback SHALL require the RFC 9207 iss parameter when the provider advertises it
After consuming the state and fetching discovery, and before the token request, the callback SHALL refuse a response whose `iss` parameter is present and differs from the configured issuer, and SHALL refuse one that omits `iss` when discovery advertises `authorization_response_iss_parameter_supported: true`.

#### Scenario: Missing iss from an advertising provider
- **WHEN** discovery advertises the parameter and the callback carries no `iss`
- **THEN** login is refused with reason `provider` and no token request is made

#### Scenario: Provider that does not advertise
- **WHEN** discovery does not advertise the parameter and the callback carries no `iss`
- **THEN** the flow continues

### Requirement: The OIDC back-channel SHALL use a dedicated transport policy with an optional private trust anchor
Every discovery, token and JWKS request SHALL be made over HTTPS with environment proxies disabled, redirects not followed, identity content encoding and bounded response bodies, verifying the server certificate against `OIDC_CA_FILE` when set and otherwise against the certifi bundle. `OIDC_CA_FILE` SHALL be parsed once at settings construction and refused when unreadable or containing no CA certificate. There SHALL be no setting that permits plaintext HTTP for the issuer, and the SSRF deny list of user-supplied fetches SHALL NOT apply, the issuer being operator configuration.

#### Scenario: Private-CA issuer
- **WHEN** the issuer's certificate chains to the CA in `OIDC_CA_FILE`
- **THEN** discovery succeeds

#### Scenario: Plaintext issuer
- **WHEN** `OIDC_ISSUER` is an `http://` URL
- **THEN** startup fails

### Requirement: OIDC settings SHALL be validated at boot and contact no provider
`OIDC_TOKEN_AUTH_METHOD` SHALL be `client_secret_basic` (default) or `client_secret_post`; `OIDC_EXTRA_SCOPES` SHALL be at most eight space-separated RFC 6749 scope tokens of at most 64 characters each; `OIDC_DISPLAY_NAME` SHALL be 1 to 40 printable characters (default `SSO`); `OIDC_ISSUER` SHALL be at most 2,048 characters. Validation SHALL NOT make any network request, so an unreachable provider SHALL NOT prevent startup, `/health` or password login.

#### Scenario: Provider down at boot
- **WHEN** the issuer is unreachable while the process starts with OIDC enabled
- **THEN** startup completes, `/health` answers as before, and password login works

#### Scenario: Auth method not offered by the provider
- **WHEN** `OIDC_TOKEN_AUTH_METHOD=client_secret_post` and discovery's `token_endpoint_auth_methods_supported` omits it
- **THEN** login is refused with reason `provider` at the start route

### Requirement: An OIDC login SHALL stamp last_login_at and stay independent of the password-failure budget
A successful OIDC callback SHALL set the user's `last_login_at` in the transaction that commits the session mint. OIDC refusals SHALL NOT charge the per-account failed-login budget, the budget SHALL NOT refuse an OIDC login, and an OIDC success SHALL NOT reset it.

#### Scenario: Last login recorded
- **WHEN** a bound user signs in through OIDC
- **THEN** that user's `last_login_at` SHALL be updated

#### Scenario: Exhausted password budget
- **WHEN** a user's per-account failed-login budget is exhausted and that user signs in through OIDC
- **THEN** the OIDC login SHALL succeed and the budget SHALL remain exhausted

### Requirement: Disabled password login SHALL warn when no administrator can sign in through OIDC
At lifespan start, with OIDC enabled and `LOCAL_LOGIN_ENABLED=false`, the server SHALL count active administrators bound under the configured issuer and SHALL log one CRITICAL line naming the recovery step when the count is zero, and SHALL continue starting in every case, including when the count query fails.

#### Scenario: No bound administrator
- **WHEN** password login is disabled and no active admin is bound under the configured issuer
- **THEN** one CRITICAL line is logged and startup continues

### Requirement: Deployments without OIDC SHALL be byte-for-byte unchanged
With `OIDC_ENABLED=false`, in single-user or multi-user mode, every panel, login, account, user list, user edit and consent page SHALL render identically to the release before this change, and no route added by this change SHALL be reachable. This SHALL be enforced by golden tests captured on the pre-change commit, covering the unauthenticated `/authorize` redirect, authenticated consent rendering for a DCR and a CIMD client, consent approval preserving the user and grant lineage, and `/admin/register` GET and POST. The `/authorize` goldens SHALL also hold with OIDC enabled.

#### Scenario: Panel snapshot with OIDC off
- **WHEN** the login, users list, user edit and account pages are rendered with OIDC disabled
- **THEN** each equals the pre-change rendering apart from the CSRF token and the nonce

#### Scenario: Unauthenticated authorize redirect
- **WHEN** an unauthenticated browser requests `/authorize` for a DCR client or a CIMD client, with OIDC disabled or enabled
- **THEN** the redirect SHALL equal the golden captured before this change

#### Scenario: Consent render and approval
- **WHEN** a signed-in user opens and approves consent for a DCR client and for a CIMD client, with OIDC disabled or enabled
- **THEN** the consent page SHALL equal its golden apart from the CSRF token and the nonce, and the issued code SHALL belong to the same user and grant lineage as before this change

#### Scenario: Bootstrap matrix
- **WHEN** `/admin/register` is requested by GET and POST on an empty and on a populated users table
- **THEN** with OIDC disabled the responses SHALL equal the pre-change goldens, and with OIDC enabled both SHALL be 404

#### Scenario: Link routes absent
- **WHEN** OIDC is disabled and an admin POSTs to `/admin/users/{id}/oidc/expect`
- **THEN** the response is 404 and nothing is written

