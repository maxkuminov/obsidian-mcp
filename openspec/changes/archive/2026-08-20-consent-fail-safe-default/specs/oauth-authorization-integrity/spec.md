## ADDED Requirements

### Requirement: Consent preselects the least privilege
The OAuth consent screen SHALL render the read-only access option as the preselected (`checked`) option on every request, and SHALL NOT render any write-capable option preselected. The preselection MUST NOT depend on the `scope` query parameter, on the client's registered scope, or on any other client-supplied input. Granting write access MUST therefore require an affirmative selection by the user in addition to approving the request. The screen MUST also prevent the browser from restoring a previously selected access level in place of the markup default, so that the preselection holds on a repeat visit to the same authorization URL.

#### Scenario: Write-capable client requests readwrite
- **WHEN** a client registered for `readwrite` starts an authorization request with `scope=readwrite`
- **THEN** the consent screen renders the read-only option preselected
- **AND** the read + write option is rendered but not preselected
- **AND** approving without changing the selection submits the read-only scope

#### Scenario: Read request
- **WHEN** an authorization request asks for `scope=read`
- **THEN** the consent screen renders the read-only option preselected

#### Scenario: Scope parameter omitted
- **WHEN** an authorization request omits the `scope` query parameter entirely
- **THEN** the request is treated as a read request
- **AND** the consent screen renders the read-only option preselected

#### Scenario: Browser state restore cannot revive an earlier write selection
- **WHEN** the consent screen is rendered
- **THEN** the form and every access-level control opt out of browser autofill/state restoration
- **AND** revisiting the same authorization URL after previously selecting read + write renders the read-only option preselected

#### Scenario: No option other than read-only is preselected
- **WHEN** the consent screen is rendered for any valid authorization request
- **THEN** exactly one access-level option carries `checked`
- **AND** that option is the read-only option

### Requirement: Consent discloses the requested access level
The OAuth consent screen SHALL name the access level the client requested, so that a user whose grant will be narrower than the request sees the difference rather than receiving a silent downgrade. When the client requested write access it is not registered to hold, the screen SHALL state that write access is not available to that client. When the client is registered for write access, the screen SHALL state that read-only is preselected and that write is granted only if the user selects it.

#### Scenario: Readwrite request from a write-capable client is named
- **WHEN** a client registered for `readwrite` requests `scope=readwrite`
- **THEN** the consent screen states that the client is requesting read + write access
- **AND** it states that read only is preselected and read + write is granted only if selected

#### Scenario: The preselect explanation is only shown where there is a choice
- **WHEN** the client is not registered for `readwrite`, so no read + write option is offered
- **THEN** the consent screen does not state that read + write is granted only if selected

#### Scenario: Read request is named
- **WHEN** a client requests `scope=read`
- **THEN** the consent screen states that the client is requesting read only access

#### Scenario: Read-only client requesting write is told write is unavailable
- **WHEN** a client registered only for `read` requests `scope=readwrite`
- **THEN** the consent screen states that the client is requesting read + write access
- **AND** it states that read + write is not available to that client
- **AND** no read + write option is offered

#### Scenario: Write-capable client is not told write is unavailable
- **WHEN** a client registered for `readwrite` requests `scope=readwrite`
- **THEN** the consent screen does not state that write access is unavailable

### Requirement: Consent renders client-supplied text as text
The OAuth consent screen SHALL render every client-supplied value it displays — the client name in particular — as escaped text, never as markup. Client registration is unauthenticated, so a client-supplied string that reached the page as markup could contribute a form control, including a preselected one, that an unchanged approval would submit. A `scope` query parameter that is not a known scope token SHALL be rejected with `invalid_scope` before any consent screen is rendered.

#### Scenario: Hostile client name is escaped
- **WHEN** a registered client's name contains markup, quotes, or a complete `<input ... checked>` element
- **THEN** the consent screen renders that name as text
- **AND** the page still carries exactly one checked control, the read-only option
- **AND** no additional access-level control appears

#### Scenario: Malformed scope is rejected before rendering
- **WHEN** an authorization request carries a `scope` value containing a token outside the known scopes
- **THEN** the server responds `invalid_scope` with HTTP 400
- **AND** no consent screen, form, or access-level control is produced

### Requirement: Consent marks the selected access level visibly
The OAuth consent screen SHALL visually distinguish the currently selected access level beyond the native radio indicator. The styling that provides this MUST degrade gracefully where the browser does not support it, leaving the native radio indicator as the fallback signal, and MUST NOT be grouped with styling the screen depends on for legibility.

#### Scenario: Selected option is highlighted
- **WHEN** an access-level option is selected on the consent screen
- **THEN** that option's container is styled distinctly from the unselected options

#### Scenario: Unsupported browser keeps a working screen
- **WHEN** the browser cannot parse the selected-option selector
- **THEN** only the highlight rules are dropped
- **AND** every other consent-screen rule, including the native radio indicator styling, still applies
