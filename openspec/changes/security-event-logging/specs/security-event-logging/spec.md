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

### Requirement: Structured log fields SHALL be allow-listed, typed, bounded, and declared by provenance

The log formatter SHALL emit only a fixed set of field names declared in one module, each with a declared type, a maximum length and a declared provenance class, and SHALL drop any field a call site passes that is not in that set. No **structured field** SHALL carry a password, a client secret, an authorization code, an access or refresh token, a PKCE verifier, a session cookie, a CSRF token, a request body, a query string, or a filesystem path. A value presented by a caller as a credential SHALL reach the log in exactly one form — a stable, non-reversible `token_tag` computed as a truncated SHA-256 digest by exactly one function in the codebase — and SHALL be **absent** rather than empty when no credential was presented. Fields of the **resolved** class SHALL carry only values read from a database row the server loaded, and `key_id` SHALL name an API key row while `oauth_token_id` SHALL name an OAuth token row; fields of the **submitted** class SHALL be the only place a caller-supplied identifier appears. The record's message and traceback are not structured fields and are governed by the next requirement.

#### Scenario: An unknown field is dropped

- **WHEN** a call site logs with a field name outside the allow-list
- **THEN** the emitted record SHALL NOT contain that field and SHALL still contain the allow-listed fields and the message

#### Scenario: A presented token appears only as a tag

- **WHEN** an unknown bearer token is presented to the MCP endpoint and to a transfer route
- **THEN** each refusal record SHALL carry a `token_tag` of the form `sha:` followed by eight hexadecimal characters, the tag SHALL be identical for identical tokens, and no record SHALL contain any substring of the presented token twelve characters or longer

#### Scenario: No tag is invented when nothing was presented

- **WHEN** a request carrying no credential is refused
- **THEN** the record SHALL have no `token_tag` field at all

#### Scenario: An unresolved identifier is still recorded, in a submitted field

- **WHEN** a login names a user that does not exist and a token request names a client that does not exist
- **THEN** each record SHALL carry the submitted identifier in its declared submitted-class field, and no resolved-class field SHALL be populated for it

#### Scenario: An OAuth credential is not recorded as an API key

- **WHEN** an authentication failure concerns an OAuth token row
- **THEN** the record SHALL carry `oauth_token_id` and SHALL NOT carry that value in `key_id`

#### Scenario: The allow-list cannot drift from the call sites

- **WHEN** the source tree is parsed and every structured logging call site is collected — both `extra=` on a logger call and the keyword arguments of the event emitter
- **THEN** every field name passed by any call site SHALL be present in the allow-list, and a call site that expands a field set dynamically instead of naming its fields SHALL fail the check

### Requirement: Messages and tracebacks MUST NOT carry credential material

A record's message SHALL be a developer-authored constant or format string, and it MAY interpolate operational context such as a vault-relative path exactly as existing messages do, but it MUST NOT interpolate a password, a client secret, an authorization code, an access or refresh token, a PKCE verifier, a session cookie or a CSRF token. The same prohibition SHALL apply to any value a call site places in an exception it logs. Compliance SHALL be verified with unique high-entropy canary values submitted through the real request paths, not by inspection.

#### Scenario: No secret material anywhere in the record

- **WHEN** the panel login, the OAuth token endpoint, the OAuth registration endpoint, the revocation endpoint and a transfer redemption are each exercised with a distinct 32-character random canary in the credential position, and the whole log output is captured
- **THEN** no emitted record SHALL contain any canary value, nor any substring of one twelve characters or longer, in any field, message or traceback

#### Scenario: Operational context is still allowed

- **WHEN** an existing warning interpolates a vault-relative path into its message
- **THEN** the record SHALL be emitted unchanged and the check SHALL NOT fail on it

### Requirement: Every authentication outcome SHALL cause exactly one emission, with a reason code

The server SHALL call the event emitter exactly once — never zero times and never twice for one decision — for each of: panel login success, panel login failure, panel logout, first-administrator bootstrap and its refusals, an administrator's password reset of another account, OAuth authorization-code issuance, OAuth refresh-token rotation, every token-endpoint refusal, every `/authorize` refusal, consent granted, consent denied, dynamic client registration and its refusals, token revocation together with the number of tokens revoked, each revocation no-op the RFC requires the response to conceal, and every rate-limit rejection. A failure record SHALL carry a reason code distinguishing the cause. Informational outcomes SHALL always reach the log sink; warning and error outcomes SHALL reach it subject to the suppression requirement below, which accounts for what it withholds. The externally visible response SHALL be unchanged by the presence of logging — in particular the panel login failure SHALL remain a single indistinguishable response across all of its causes, and the revocation endpoint SHALL remain non-disclosing. A revocation record SHALL be emitted by the request handler after its own transaction has committed, never by the shared helper that performs the revocation, so that a rolled-back transaction leaves no record and every record carries the acting identity and request context.

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

#### Scenario: Rate-limit rejections are recorded centrally

- **WHEN** any rate-limited endpoint rejects a request with HTTP 429
- **THEN** exactly one emission SHALL occur carrying the route, the method, the client identity, the limit count and the window length, from the shared handler rather than from the route

#### Scenario: The client identity is the trusted one

- **WHEN** a request arrives from an untrusted peer carrying a forged `X-Forwarded-For` header
- **THEN** the record's client identity SHALL be the peer's own address and SHALL NOT be the forged header value

### Requirement: Every authorization refusal SHALL be recorded, and write-tool refusals SHALL also be marked in the usage log

The server SHALL emit one record for each authorization refusal: a write tool refused for a read-only credential, a transfer capability-token refusal, a panel permission refusal (including the duplicate ownership check on the REST key-revocation route), a CSRF validation failure, and the OAuth cross-user client refusal. The write-tool refusal SHALL be recorded at the single shared permission check so that every calling tool inherits it, and it SHALL additionally write the marker `permission_denied` into the call's `usage_logs` row. A transfer refusal SHALL carry a reason code, the redacted token tag where a token was presented, and the trusted client identity, and the response SHALL remain byte-identical across every refusal cause; the reason SHALL be derived after the refusal decision has been taken, by a read-only diagnosis that SHALL NOT alter which tokens are accepted and SHALL NOT be performed for a request that is not refused. A panel or CSRF refusal SHALL carry the route, the method and the acting user id where a session resolves one.

#### Scenario: A refused write is distinguishable from a successful one

- **WHEN** a read-only credential calls a write tool
- **THEN** a warning record SHALL be emitted naming the tool and the actor, the call's `usage_logs` row SHALL carry the `permission_denied` marker, and the tool SHALL return its existing refusal message unchanged

#### Scenario: Every write tool inherits the marker

- **WHEN** each tool that requires write permission is called with a read-only credential
- **THEN** every one of those calls SHALL produce a `usage_logs` row carrying the `permission_denied` marker, with no per-tool code required to add it

#### Scenario: The transfer refusal stays uniform while the reasons separate

- **WHEN** a transfer endpoint is exercised with no credential, an unknown token, an expired token, a completed upload token, a token whose credential was revoked, and a token whose vault root was reassigned
- **THEN** every response SHALL be identical in status, headers and body, and six records SHALL be emitted carrying six distinct reason codes, of which the first SHALL carry no token tag

#### Scenario: Diagnosis costs nothing on the accepted path

- **WHEN** a transfer redemption succeeds
- **THEN** no diagnosis read SHALL be issued for it

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

### Requirement: Warning and error events SHALL be rate-limited on a subject a caller cannot mint

The server SHALL bound the number of warning and error records that reach the log sink, per event and subject within a time window and per subject across all events, SHALL count what it withholds, and SHALL emit one summary record naming the event and the suppressed count. The subject SHALL be the authenticated user id when one resolved and otherwise the trusted client address; a caller-supplied or caller-derived value — a token tag, a submitted username, a submitted client id — SHALL NOT be used as a subject, so that rotating credentials cannot mint fresh allowances. A summary SHALL be emitted when the next event for that key arrives after its window closed and any outstanding counts SHALL be flushed at shutdown; a summary SHALL NOT itself be suppressed. Suppression state SHALL be bounded in size, SHALL fail open, SHALL never raise into a request path, and SHALL apply to log records only: a `usage_logs` row for a refused or failed tool call SHALL always be written. Informational events SHALL NOT be suppressed.

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

#### Scenario: The audit row is never suppressed

- **WHEN** a read-only credential is refused a write tool more times than the per-window log limit
- **THEN** every one of those calls SHALL still write its `usage_logs` row carrying the `permission_denied` marker

#### Scenario: Suppression cannot break a request

- **WHEN** the suppressor's internal state raises for any reason
- **THEN** the record SHALL still be emitted and the request SHALL complete normally
