## ADDED Requirements

### Requirement: The application's log configuration SHALL take effect regardless of import order

The application SHALL configure the root logger from a single entry point (`configure_logging()`) that runs after the MCP SDK's `FastMCP` construction has installed its own handler, and that reconfiguration SHALL remove and close every root handler it did not install, install exactly one stream handler writing to **stderr**, and set the root level from configuration. It SHALL be idempotent, it SHALL be called from every process entry point (the FastAPI application and the stdio entry point), and it SHALL NOT remove, close or detach a handler owned by the in-process error buffer, in either call order. Each emitted record SHALL occupy exactly one physical line, including records carrying a traceback, and SHALL carry a UTC timestamp in ISO-8601 form with an explicit `Z` designator.

#### Scenario: Format applies in a real process

- **WHEN** a process imports the application module and emits a record carrying structured fields
- **THEN** the root logger SHALL have exactly one handler besides any error-buffer handler, the emitted line SHALL parse as a single structured record, its timestamp SHALL parse as UTC and end in `Z`, and the structured fields SHALL be present in it

#### Scenario: A traceback is one line

- **WHEN** a record is emitted with exception information attached
- **THEN** the whole traceback SHALL appear inside one physical line of output, bounded in length, and SHALL NOT be split across lines

#### Scenario: The error buffer survives reconfiguration

- **WHEN** the error buffer's handler is attached and `configure_logging()` is then called
- **THEN** the handler SHALL still be attached to the root logger and a subsequently logged ERROR SHALL still appear in the buffer

#### Scenario: Reconfiguration is idempotent

- **WHEN** `configure_logging()` is called twice
- **THEN** the root logger SHALL still have exactly one stream handler and records SHALL be emitted once, not twice

#### Scenario: The stdio entry point never writes to stdout

- **WHEN** the stdio entry point configures logging and a record is emitted
- **THEN** the record SHALL be written to stderr and stdout SHALL carry only MCP protocol traffic

### Requirement: Structured log fields SHALL be allow-listed, typed, bounded, and free of presented credential material

The log formatter SHALL emit only a fixed set of field names declared in one module, each with a declared type and a maximum length, and SHALL drop any other field passed by a call site rather than serialising it. No log record SHALL contain a password, a client secret, an authorization code, an access or refresh token, a PKCE verifier, a session cookie, a CSRF token, a request body, a query string, or a vault-relative note path taken from a request. A value derived from a credential presented by a caller SHALL appear only as a stable, non-reversible tag (`token_tag`) computed as a truncated SHA-256 digest, produced by exactly one function in the codebase; every other identifier SHALL come from a database row the server already resolved. A free-text exception string SHALL NOT be an allow-listed field; an exception SHALL be identified by its class name and, where a traceback is warranted, by the record's bounded traceback field.

#### Scenario: An unknown field is dropped

- **WHEN** a call site logs with a field name outside the allow-list
- **THEN** the emitted record SHALL NOT contain that field and SHALL still contain the allow-listed fields and the message

#### Scenario: No secret material in any record

- **WHEN** the panel login, the OAuth token endpoint, the OAuth registration endpoint and the revocation endpoint are exercised with valid and invalid credentials, and the whole log output is captured
- **THEN** no emitted record SHALL contain the submitted password, the client secret, the authorization code, the access token, the refresh token, or the PKCE verifier, in whole or in any substring longer than eight characters

#### Scenario: A presented token appears only as a tag

- **WHEN** an unknown bearer token is presented to the MCP endpoint and to a transfer route
- **THEN** each refusal record SHALL carry a `token_tag` of the form `sha:` followed by eight hexadecimal characters, the tag SHALL be identical for identical tokens, and no record SHALL contain any substring of the presented token

#### Scenario: The allow-list cannot drift from the call sites

- **WHEN** the source tree is swept for structured logging call sites
- **THEN** every field name passed by any call site SHALL be present in the allow-list

### Requirement: Every authentication outcome SHALL be recorded once, with a reason code

The server SHALL emit exactly one structured record for each of: panel login success, panel login failure, panel logout, first-administrator bootstrap and its refusals, an administrator's password reset of another account, OAuth authorization-code issuance, OAuth refresh-token rotation, every token-endpoint refusal, every `/authorize` refusal, consent granted, consent denied, dynamic client registration and its refusals, token revocation together with the number of tokens revoked, each revocation no-op that the RFC requires the response to conceal, and every rate-limit rejection. A failure record SHALL carry a reason code distinguishing the cause, and the externally visible response SHALL be unchanged by the presence of logging — in particular the panel login failure SHALL remain a single indistinguishable response across all of its causes, and the revocation endpoint SHALL remain non-disclosing.

#### Scenario: Login failure reasons are distinguished in the log only

- **WHEN** login is attempted with an unknown username, with an inactive user's correct password, and with a known user's wrong password
- **THEN** three records SHALL be emitted carrying distinct reason codes with the submitted username, and the three HTTP responses SHALL be identical in status and body

#### Scenario: Token issuance and refusal are attributable

- **WHEN** an authorization-code exchange succeeds and a second exchange fails PKCE verification
- **THEN** the first SHALL emit an issuance record carrying the client id, the user id, the grant id and the granted scope, and the second SHALL emit a refusal record carrying the client id and a reason naming the PKCE failure, with neither record carrying the code or the verifier

#### Scenario: A revocation no-op is recorded although the response conceals it

- **WHEN** a revocation request presents an unknown token value, and another presents a valid token with a mismatched client id
- **THEN** both responses SHALL remain HTTP 200 with an empty body, and each SHALL emit a record naming its no-op reason

#### Scenario: Rate-limit rejections are recorded centrally

- **WHEN** any rate-limited endpoint rejects a request with HTTP 429
- **THEN** exactly one record SHALL be emitted carrying the route, the method and the client identity, from the shared handler rather than from the route

#### Scenario: The client identity is the trusted one

- **WHEN** a request arrives from an untrusted peer carrying a forged `X-Forwarded-For` header
- **THEN** the record's client identity SHALL be the peer's own address and SHALL NOT be the forged header value

### Requirement: Every authorization refusal SHALL be recorded, and tool-call refusals SHALL also be marked in the usage log

The server SHALL emit one structured record for each authorization refusal: a write tool refused for a read-only credential, a transfer capability-token refusal, a panel permission refusal, a CSRF validation failure, and the OAuth cross-user client refusal. The write-tool refusal SHALL be recorded at the single shared permission check so that every calling tool inherits it, and it SHALL additionally write the marker `permission_denied` into the call's `usage_logs` row. A transfer refusal SHALL carry the redacted token tag, the trusted client identity, and the reason the response deliberately withholds, and the response SHALL remain byte-identical across every refusal cause. A panel or CSRF refusal SHALL carry the route, the method and the acting user id where a session resolves one.

#### Scenario: A refused write is distinguishable from a successful one

- **WHEN** a read-only credential calls a write tool
- **THEN** a warning record SHALL be emitted naming the tool and the actor, the call's `usage_logs` row SHALL carry the `permission_denied` marker, and the tool SHALL return its existing refusal message unchanged

#### Scenario: Every write tool inherits the marker

- **WHEN** each tool that requires write permission is called with a read-only credential
- **THEN** every one of those calls SHALL produce a `usage_logs` row carrying the `permission_denied` marker, with no per-tool code required to add it

#### Scenario: The transfer refusal stays uniform

- **WHEN** a transfer endpoint is exercised with a missing token, an unknown token, an expired token, a consumed token, a token whose credential was revoked, and a token whose vault root was reassigned
- **THEN** every response SHALL be HTTP 404 with an identical body, and each SHALL emit a record with a distinct reason code and a redacted token tag

#### Scenario: An unresolved token invents no identity

- **WHEN** a transfer refusal occurs on a branch where no token row resolved
- **THEN** the record SHALL carry the reason and the token tag and SHALL NOT carry a user id, a key id or an OAuth token id

### Requirement: An exception in a tool body SHALL be logged, recorded, and re-raised

The tool tracking decorator SHALL catch `Exception` — and SHALL NOT catch `BaseException` — around the tool body, SHALL log at ERROR with exception information and the tool name, the actor and the user id, SHALL write a best-effort `usage_logs` row carrying the marker `tool_exception` and the exception's class name together with the measured duration, and SHALL then re-raise the original exception unchanged so the MCP layer still produces its error result. A failure of that audit write SHALL be caught, logged, and discarded; it SHALL NOT replace, wrap or suppress the original exception. A cancellation SHALL pass through untouched.

#### Scenario: A raising tool leaves a record and a row

- **WHEN** a tracked tool body raises `RuntimeError`
- **THEN** exactly one ERROR record SHALL be emitted carrying the tool name, the exception class and a traceback; exactly one `usage_logs` row SHALL be written carrying `error` = `tool_exception`, the exception class name and a non-negative duration; and the caller SHALL receive the same error result it receives today

#### Scenario: A failing audit write does not mask the tool exception

- **WHEN** a tracked tool body raises and the `usage_logs` write itself then raises
- **THEN** the original exception SHALL propagate unchanged to the caller, a warning record SHALL name the failed audit write, and no exception from the audit path SHALL appear in the propagated traceback's active exception

#### Scenario: Cancellation is not a tool exception

- **WHEN** a tracked tool call is cancelled, so that the body raises `asyncio.CancelledError`
- **THEN** no `tool_exception` record SHALL be emitted, no `usage_logs` row SHALL be written for that call, and the cancellation SHALL propagate unchanged

#### Scenario: The body's own markers do not survive an exception

- **WHEN** a tool body records a post-body marker and then raises
- **THEN** the written row's `error` value SHALL be `tool_exception` and the exception's class name SHALL be present

### Requirement: Denial and failure logging SHALL be rate-limited without suppressing the audit row

The server SHALL bound the number of warning and error security records emitted for a given event and subject within a time window, SHALL account for the records it suppressed, and SHALL emit one summary record naming the event and the suppressed count when the window closes. The subject SHALL be the most specific identity available on the record. Suppression state SHALL be bounded in size, SHALL never raise into a request path, and SHALL apply to log records only: a `usage_logs` row for a refused or failed tool call SHALL always be written.

#### Scenario: A refusal flood is bounded

- **WHEN** one subject triggers the same refusal event far more times than the per-window limit
- **THEN** at most the configured number of records SHALL be emitted in that window and one summary record SHALL name the event and the number suppressed

#### Scenario: Suppression does not hide other subjects

- **WHEN** one subject is being suppressed and a different subject triggers the same event
- **THEN** the second subject's record SHALL be emitted

#### Scenario: The audit row is never suppressed

- **WHEN** a read-only credential is refused a write tool more times than the per-window log limit
- **THEN** every one of those calls SHALL still write its `usage_logs` row carrying the `permission_denied` marker

#### Scenario: Suppression cannot break a request

- **WHEN** the suppressor's internal state raises for any reason
- **THEN** the record SHALL still be emitted and the request SHALL complete normally
