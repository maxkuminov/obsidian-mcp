## ADDED Requirements

### Requirement: The application's log configuration SHALL take effect regardless of import order

The application SHALL configure the root logger from a single entry point (`configure_logging()`) that runs after the MCP SDK's `FastMCP` construction has installed its own handler, and that reconfiguration SHALL remove and close every root handler it did not install, install exactly one stream handler writing to **stderr**, and set the root level from configuration. It SHALL be idempotent, it SHALL be called from every process entry point (the FastAPI application and the stdio entry point), and it SHALL NOT remove, close or detach a handler owned by the in-process error buffer, in either call order. Each emitted record SHALL occupy exactly one physical line, including records carrying a traceback, and SHALL carry a UTC timestamp in ISO-8601 form with an explicit `Z` designator.

#### Scenario: Format applies in a real process

- **WHEN** a process imports the application module and emits a record carrying structured fields
- **THEN** the root logger SHALL have exactly one handler besides any error-buffer handler, the emitted line SHALL parse as a single structured record, its timestamp SHALL parse as UTC and end in `Z`, and the structured fields SHALL be present in it

#### Scenario: A traceback is one line, bounded, and still identifies the fault

- **WHEN** a record is emitted with exception information whose formatted traceback exceeds the configured budget
- **THEN** the record SHALL occupy one physical line, its traceback field SHALL contain a bounded head and tail separated by a marker naming the number of elided bytes, and the exception type line and the traceback's final line SHALL both be present

#### Scenario: A short traceback is not elided

- **WHEN** a record is emitted whose formatted traceback fits within the budget
- **THEN** the traceback field SHALL contain it in full with no elision marker

#### Scenario: The error buffer survives reconfiguration

- **WHEN** the error buffer's handler is attached and `configure_logging()` is then called
- **THEN** the handler SHALL still be attached to the root logger and a subsequently logged ERROR SHALL still appear in the buffer

#### Scenario: Reconfiguration is idempotent

- **WHEN** `configure_logging()` is called twice
- **THEN** the root logger SHALL still have exactly one stream handler and records SHALL be emitted once, not twice

#### Scenario: The stdio entry point never writes to stdout

- **WHEN** the stdio entry point configures logging and a record is emitted
- **THEN** the record SHALL be written to stderr and stdout SHALL carry only MCP protocol traffic

### Requirement: Structured log fields SHALL be allow-listed, typed, bounded, optional, and declared per event

The log formatter SHALL emit only a fixed set of field names declared in one module, each with a declared type and — for strings — a maximum length, and SHALL drop any field a call site passes that is not in that set or that the emitting event does not declare. Every field SHALL be optional: a record SHALL omit a field whose value the emitting path does not have, and absence SHALL be meaningful rather than an error. A value whose type does not match its declaration SHALL be **dropped**, never converted to another type, and the emitting call SHALL NOT raise; truncating an over-long string SHALL NOT count as a conversion. No **structured field** SHALL carry a password, a client secret, an authorization code, an access or refresh token, a PKCE verifier, a session cookie, a CSRF token, a request body, a query string, or a filesystem path. A value presented by a caller as a credential SHALL reach the log in exactly one form — a stable, non-reversible `token_tag` computed as a truncated SHA-256 digest by exactly one function in the codebase — and SHALL be **absent** rather than empty when no credential was presented. Provenance SHALL be carried by the field name and SHALL be a property of the event-field pair: an unsuffixed identifier field SHALL hold only a value read from a database row, a `_submitted`-suffixed field SHALL be the only place a caller-supplied identifier that did not resolve appears, a `_session`-suffixed field SHALL be the only place a value copied from the session cookie without a database read appears, and `key_id` SHALL name an API key row while `oauth_token_id` SHALL name an OAuth token row. Where one account can act on another's resource, the record SHALL distinguish **who acted** from **whom the record is about**: `actor_user_id` SHALL name the authenticated principal that caused the record and SHALL be present on every such surface even when the two are the same, while `user_id` SHALL name the account the record concerns. The names a call site may pass SHALL be disjoint from the names the formatter produces (the timestamp, level, logger, message and traceback) and from the emitter's own control keywords; a call site that passes a formatter-owned name SHALL have it dropped, and control keywords SHALL NOT be rendered as data. The record's message and traceback are not structured fields and are governed by the next requirement.

#### Scenario: An unknown field is dropped

- **WHEN** a call site logs with a field name outside the allow-list
- **THEN** the emitted record SHALL NOT contain that field and SHALL still contain the allow-listed fields and the message

#### Scenario: A field the event does not declare is dropped

- **WHEN** a call site passes an allow-listed field that the emitted event's declared set does not include
- **THEN** the record SHALL omit that field, and under the test suite's strict mode the call SHALL fail loudly instead

#### Scenario: A mistyped value is dropped, not converted

- **WHEN** a call site passes a non-integer value for an integer field
- **THEN** the record SHALL omit that field, SHALL NOT contain it as a string or any other type, SHALL still contain the event's other fields, and the call SHALL NOT raise

#### Scenario: A presented token appears only as a tag

- **WHEN** an unknown bearer token is presented to the MCP endpoint and to a transfer route
- **THEN** each refusal record SHALL carry a `token_tag` of the form `sha:` followed by eight hexadecimal characters, the tag SHALL be identical for identical tokens, and no record SHALL contain any substring of the presented token twelve characters or longer

#### Scenario: No tag is invented when nothing was presented

- **WHEN** a request carrying no credential is refused
- **THEN** the record SHALL have no `token_tag` field at all

#### Scenario: An unresolved identifier is still recorded, in a submitted field

- **WHEN** a login names a user that does not exist and a token request names a client that does not exist
- **THEN** each record SHALL carry the submitted identifier in the correspondingly suffixed field, and the unsuffixed field SHALL be absent from that record

#### Scenario: The same field name is resolved on the success path

- **WHEN** a login succeeds and a token exchange succeeds
- **THEN** each record SHALL carry the identifier in the unsuffixed field, read from the row the server loaded, and SHALL NOT carry the suffixed one

#### Scenario: A field the emitting path lacks is simply absent

- **WHEN** a refresh-token request is refused before any grant or client row resolves
- **THEN** the record SHALL carry its reason and the submitted client id, SHALL omit the grant and user fields, and SHALL NOT carry placeholder or empty values for them

#### Scenario: An administrator acting on another account is distinguishable

- **WHEN** an administrator revokes another user's OAuth grant from the panel
- **THEN** the record SHALL carry the administrator as the acting principal and the grant's owner as the account the record is about, as two separate fields; and **WHEN** the same family is revoked through the client-authenticated revocation endpoint, the record SHALL carry the owner and no acting principal

#### Scenario: A formatter-owned name cannot be forged by a call site

- **WHEN** a call site passes a timestamp, level, logger, message or traceback as a field
- **THEN** the emitted record SHALL carry the formatter's own value for it, not the call site's

#### Scenario: An OAuth credential is not recorded as an API key

- **WHEN** an authentication failure concerns an OAuth token row
- **THEN** the record SHALL carry `oauth_token_id` and SHALL NOT carry that value in `key_id`

#### Scenario: The allow-list cannot drift from the call sites

- **WHEN** the source tree is parsed and every structured logging call site is collected — both `extra=` on a logger call and the keyword arguments of the event emitter
- **THEN** every field name passed by any call site SHALL be present in the allow-list, and a call site that expands a field set dynamically instead of naming its fields SHALL fail the check

### Requirement: Messages and tracebacks MUST NOT carry credential material

A record's message SHALL be a developer-authored constant or format string, and it MAY interpolate operational context such as a vault-relative path exactly as existing messages do, but it MUST NOT interpolate a password, a client secret, an authorization code, an access or refresh token, a PKCE verifier, a session cookie or a CSRF token. The same prohibition SHALL apply to any value a call site places in an exception it logs. Compliance SHALL be verified against real request paths with two kinds of high-entropy value, not by inspection: **submitted** canaries planted in every credential position a caller controls, and **captured** secrets that the server itself generated and therefore cannot be planted.

#### Scenario: No submitted secret anywhere in the record

- **WHEN** the panel login, the OAuth token endpoint, the OAuth revocation endpoint, an MCP tool call and a transfer redemption are each exercised with a distinct 32-character random canary in every caller-controlled credential position — password, client secret, authorization code, refresh token, PKCE verifier, session cookie, CSRF token, MCP bearer token and transfer bearer token — and the whole log output is captured
- **THEN** no emitted record SHALL contain any canary value, nor any substring of one twelve characters or longer, in any field, message or traceback

#### Scenario: No server-generated secret anywhere in the record

- **WHEN** dynamic client registration, an authorization-code grant, a refresh rotation, API key creation and a transfer mint are each performed and the secrets **the server generated** are captured from their responses — the client secret, the authorization code, the access and refresh tokens, the `omcp_` key and the transfer token
- **THEN** no emitted record SHALL contain any of those values, nor any substring of one twelve characters or longer, in any field, message or traceback

#### Scenario: Operational context is still allowed

- **WHEN** an existing warning interpolates a vault-relative path into its message
- **THEN** the record SHALL be emitted unchanged and the check SHALL NOT fail on it

### Requirement: Every authentication outcome SHALL cause exactly one emission, with a reason code

The server SHALL attempt exactly one emission — never zero, never twice for one decision — for each of: panel login success, panel login failure, panel logout, first-administrator bootstrap and its refusals, an administrator's password reset of another account, OAuth authorization-code issuance, OAuth refresh-token rotation, every token-endpoint refusal, every `/authorize` refusal, consent granted, consent denied, dynamic client registration and its refusals, token revocation together with the number of tokens revoked, each revocation no-op the RFC requires the response to conceal, and every rate-limit rejection. A failure record SHALL carry a reason code distinguishing the cause. Whether a given attempt reaches the log sink is governed by the suppression requirement below, which applies to every level and accounts for what it withholds. **A record asserting that something succeeded SHALL be emitted only after the transaction that made it true has committed**, so that a failed commit leaves no record claiming otherwise; a revocation record SHALL further be emitted by the request handler itself, never by the shared helper that performs the revocation, so that every record carries the acting identity and request context. The externally visible response SHALL be unchanged by the presence of logging — in particular the panel login failure SHALL remain a single indistinguishable response across all of its causes, and the revocation endpoint SHALL remain non-disclosing.

#### Scenario: Login failure reasons are distinguished in the log only

- **WHEN** login is attempted with an unknown username, with an inactive user's correct password, and with a known user's wrong password
- **THEN** three emissions SHALL occur carrying distinct reason codes with the submitted username, and the three HTTP responses SHALL be identical in status and body

#### Scenario: Token issuance and refusal are attributable

- **WHEN** an authorization-code exchange succeeds and a second exchange fails PKCE verification
- **THEN** the first SHALL emit an issuance record carrying the client id, the user id, the grant id and the granted scope, and the second SHALL emit a refusal record carrying the client id and a reason naming the PKCE failure, with neither record carrying the code or the verifier

#### Scenario: A revocation no-op is recorded although the response conceals it

- **WHEN** a revocation request presents an unknown token value, and another presents a valid token with a mismatched client id
- **THEN** both responses SHALL remain HTTP 200 with an empty body, and each SHALL emit a record naming its no-op reason

#### Scenario: A rolled-back revocation leaves no record

- **WHEN** a revocation's transaction fails to commit after the family was updated in memory
- **THEN** no revocation record SHALL be emitted

#### Scenario: A failed commit leaves no success record

- **WHEN** the first-administrator bootstrap inserts its user and the commit then raises, and separately when a successful login's `last_login_at` commit raises
- **THEN** no success record SHALL be emitted for either, and the failure SHALL surface as it does today

#### Scenario: Rate-limit rejections are recorded centrally

- **WHEN** any rate-limited endpoint rejects a request with HTTP 429
- **THEN** exactly one emission SHALL occur carrying the route, the method, the client identity, the limit count and the window length, from the shared handler rather than from the route

#### Scenario: The client identity is the trusted one

- **WHEN** a request arrives from an untrusted peer carrying a forged `X-Forwarded-For` header
- **THEN** the record's client identity SHALL be the peer's own address and SHALL NOT be the forged header value

### Requirement: Every authorization refusal SHALL be recorded, and write-tool refusals SHALL also be marked in the usage log

The server SHALL emit one record for each authorization refusal: a write tool refused for a read-only credential, a transfer capability-token refusal, a panel permission refusal (including the duplicate ownership check on the REST key-revocation route), a CSRF validation failure, and the OAuth cross-user client refusal. The write-tool refusal SHALL be recorded at the single shared permission check so that every calling tool inherits it, and it SHALL additionally write the marker `permission_denied` into the call's `usage_logs` row. A transfer refusal SHALL carry a reason code, the redacted token tag where a token was presented, and the trusted client identity, and the response SHALL remain byte-identical across every refusal cause; the reason SHALL be derived after the refusal decision has been taken, by a read-only diagnosis that SHALL NOT alter which tokens are accepted, SHALL NOT be performed for a request that is not refused, and SHALL be performed only once a suppression permit for that record has been acquired — the permit's subject being the trusted client address, which is known before the diagnosis, rather than an identity the diagnosis itself would have to resolve. A panel or CSRF refusal SHALL carry the route, the method and the acting user id where a session resolves one.

#### Scenario: A refused write is distinguishable from a successful one

- **WHEN** a read-only credential calls a write tool
- **THEN** a warning record SHALL be emitted naming the tool and the actor, the call's `usage_logs` row SHALL carry the `permission_denied` marker, and the tool SHALL return its existing refusal message unchanged

#### Scenario: Every write tool inherits the marker

- **WHEN** each tool that requires write permission is called with a read-only credential
- **THEN** every one of those calls SHALL produce a `usage_logs` row carrying the `permission_denied` marker, with no per-tool code required to add it

#### Scenario: The transfer refusal stays uniform while the reasons separate

- **WHEN** a transfer endpoint is exercised with no credential, an unknown token, an expired token, a completed upload token, a token whose credential was revoked, and a token whose vault root was reassigned
- **THEN** every response SHALL be identical in status, headers and body, and six records SHALL be emitted carrying six distinct reason codes, of which the first SHALL carry no token tag

#### Scenario: Diagnosis costs nothing on the accepted or suppressed path

- **WHEN** a transfer redemption succeeds, and separately when a refusal's permit is denied because the source is already at its allowance
- **THEN** no diagnosis read SHALL be issued in either case, and the refused request SHALL still return its uniform response

#### Scenario: An unresolved token invents no identity

- **WHEN** a transfer refusal occurs on a branch where no token row resolved
- **THEN** the record SHALL carry the reason and, where a token was presented, the tag, and SHALL NOT carry a user id, a key id or an OAuth token id

### Requirement: An exception in a tool body SHALL be logged, recorded, and re-raised, and only a body failure SHALL be reported as one

The tool tracking decorator SHALL guard **only the invocation of the tool body** with `except Exception` — it SHALL NOT catch `BaseException`, and it SHALL NOT place a failure of the admission gates that precede the body, or of the parameter and usage-logging work that follows a completed body, in the same handler. On a body failure it SHALL log at ERROR with exception information, the tool name, the actor and the user id; SHALL write a best-effort `usage_logs` row carrying the marker `tool_exception`, the exception's class name and the measured duration; and SHALL then re-raise the original exception unchanged. The audit write SHALL report whether a row was written, a failure of it SHALL be logged and discarded, and a cancellation arriving during it SHALL NOT replace the tool's exception. A cancellation of the body SHALL pass through untouched.

#### Scenario: A raising tool leaves a record and a row

- **WHEN** a tracked tool body raises `RuntimeError`
- **THEN** exactly one ERROR record SHALL be emitted carrying the tool name, the exception class and a traceback; exactly one `usage_logs` row SHALL be written carrying `error` = `tool_exception`, the exception class name and a non-negative duration; and the caller SHALL receive the same error result it receives today

#### Scenario: A completed body is never reported as failed

- **WHEN** a tool body returns normally and the parameter-building or usage-logging work that follows it then raises
- **THEN** no `tool_exception` record SHALL be emitted and no row SHALL carry the `tool_exception` marker

#### Scenario: A pre-body failure is not a tool exception

- **WHEN** the quota admission gate raises before the tool body is invoked
- **THEN** the existing admission-failure record SHALL be emitted and re-raised, and no `tool_exception` record or marker SHALL be produced

#### Scenario: A failing audit write does not mask the tool exception

- **WHEN** a tracked tool body raises and the `usage_logs` write then fails
- **THEN** the original exception SHALL propagate unchanged to the caller and a warning record SHALL name the failed audit write

#### Scenario: Cancellation during the audit write preserves the original exception

- **WHEN** a tracked tool body raises and the enclosing task is cancelled while the audit row is being written
- **THEN** the caller SHALL receive the tool body's original exception, and a warning record SHALL name the interrupted audit write

#### Scenario: Cancellation of the body is not a tool exception

- **WHEN** a tracked tool call is cancelled, so that the body raises `asyncio.CancelledError`
- **THEN** no `tool_exception` record SHALL be emitted, no `usage_logs` row SHALL be written for that call, and the cancellation SHALL propagate unchanged

#### Scenario: The body's own markers do not survive an exception

- **WHEN** a tool body records a post-body marker and then raises
- **THEN** the written row's `error` value SHALL be `tool_exception` and the exception's class name SHALL be present

### Requirement: Every event SHALL pass exactly one allowance check, on a subject a caller cannot mint

The server SHALL bound the number of records that reach the log sink at **every level, informational records included**, per event and subject within a time window and per subject across all events, SHALL count what it withholds, and SHALL emit one summary record naming the event and the suppressed count. The allowance SHALL be checked **exactly once per emission attempt**: a caller SHALL acquire a permit for an event and subject, which charges the allowance, and the emitting call SHALL consume that permit **without performing a second check**, so that a caller that must do work to build its fields cannot be charged twice or escape the bound. The subject SHALL be computable before any work the permit gates, and SHALL be the authenticated user id when the request already resolved one and otherwise the trusted client address; a caller-supplied or caller-derived value — a token tag, a submitted username, a submitted client id — SHALL NOT be used as a subject, so that rotating credentials cannot mint fresh allowances. A summary SHALL be emitted when the next attempt for that key arrives after its window closed, **before any entry carrying a nonzero withheld count is evicted**, and for any outstanding count at shutdown; a summary SHALL carry the suppressed event's own level and SHALL NOT itself be suppressed or counted. Suppression state SHALL be bounded in size, SHALL fail open, SHALL never raise into a request path, and SHALL apply to log records only: a `usage_logs` row for a refused or failed tool call SHALL always be written. **Every security, refusal or credential-outcome record that a caller can trigger repeatedly through a request SHALL be emitted through this mechanism**, including the events that exist today and are written directly to a logger; background and once-per-pass work — indexing, embedding, filesystem housekeeping and startup — SHALL remain outside it, so that suppression can never hide the operational errors the health page exists to show.

#### Scenario: An existing refusal event is bounded too

- **WHEN** a caller drives an existing caller-triggerable refusal — a tool call with no vault assignment, an over-quota tool call, an authentication failure, or a transfer publication refusal — far more times than the per-window limit
- **THEN** those records SHALL be bounded and accounted exactly as the new events are, and SHALL NOT reach the sink unbounded by way of a direct logger call

#### Scenario: Background work is not suppressed

- **WHEN** the indexer, the embed pass or filesystem housekeeping logs a warning or an error
- **THEN** that record SHALL reach the sink and the ring buffer without passing through suppression

#### Scenario: A refusal flood is bounded and accounted

- **WHEN** one subject triggers the same refusal event far more times than the per-window limit
- **THEN** at most the configured number of records SHALL reach the sink in that window, and one summary record SHALL name the event and the number withheld

#### Scenario: Rotating credentials do not mint allowances

- **WHEN** one client address presents a different unknown bearer token on every request, far more times than the per-window limit
- **THEN** the records SHALL be suppressed on that address's allowance, and the differing token tags SHALL NOT create a new allowance per token

#### Scenario: One subject cannot multiply its allowance across events

- **WHEN** one subject triggers many different refusal events in one window
- **THEN** the total number of its records reaching the sink SHALL be bounded by the per-subject cap

#### Scenario: Suppression does not hide other subjects

- **WHEN** one subject is being suppressed and a different subject triggers the same event
- **THEN** the second subject's record SHALL be emitted

#### Scenario: Outstanding counts are not lost at shutdown

- **WHEN** a window holds a nonzero suppressed count and the process shuts down before the next event for that key
- **THEN** a summary record SHALL be emitted during shutdown

#### Scenario: Outstanding counts are not lost to eviction

- **WHEN** the suppression state is at its size bound and an entry holding a nonzero withheld count is the one chosen for eviction
- **THEN** that entry's summary SHALL be emitted before it is evicted, so the withheld count is never lost

#### Scenario: An informational flood is bounded and accounted

- **WHEN** one subject drives an informational outcome — such as a replayed consent or a logout — far more times than the per-window limit on a route no rate limit covers
- **THEN** at most the configured number of informational records SHALL reach the sink in that window and one summary SHALL name the exact number withheld

#### Scenario: The allowance is charged once, not twice

- **WHEN** a call site acquires a permit and then emits with it
- **THEN** exactly one unit of that subject's allowance SHALL be consumed for that outcome, and the emitting call SHALL NOT re-evaluate the limit

#### Scenario: An acquired permit that is never spent is still charged

- **WHEN** a call site acquires a permit and then fails to emit
- **THEN** the allowance SHALL remain charged, so the failure direction is a quieter log rather than an unbounded one

#### Scenario: The audit row is never suppressed

- **WHEN** a read-only credential is refused a write tool more times than the per-window log limit
- **THEN** every one of those calls SHALL still write its `usage_logs` row carrying the `permission_denied` marker

#### Scenario: Suppression cannot break a request

- **WHEN** the suppressor's internal state raises for any reason
- **THEN** the record SHALL still be emitted and the request SHALL complete normally
