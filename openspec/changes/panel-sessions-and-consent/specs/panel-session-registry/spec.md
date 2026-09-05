## ADDED Requirements

### Requirement: A browser session SHALL be a server-side row, not a signed cookie alone

Every authenticated browser session in multi-user mode SHALL be represented by a row in a server-side session registry, and a signed session cookie SHALL NOT by itself be sufficient to authenticate a request. The cookie SHALL carry a session identifier generated from a cryptographically secure random source of at least 256 bits; the registry SHALL store only a one-way hash of that identifier, never the identifier itself, so that a database dump contains no usable session credential.

Each row SHALL record the owning user, the time it was created, the time it was last seen, the time it expires, and whether and when it was revoked. It MAY record a hash of the creating request's user-agent for forensic purposes only; a user-agent value SHALL NOT be used as an authorization input, because it is replayed by any party holding the cookie and changes on ordinary browser updates.

Sessions SHALL be minted at exactly three points — a successful login, bootstrap administrator registration, and the re-issue that follows a self-service password change — and by a single implementation shared by all three, so that no future handler can create a cookie with no row behind it.

The ORM relationship from the owning user to its sessions SHALL declare passive deletes and a delete cascade, so that deleting a user relies on the database's own `ON DELETE CASCADE` rather than on the ORM loading and deleting each row, and so the schema's cascade is the mechanism that actually fires.

Single-user mode SHALL neither create nor validate session rows, because identity there is resolved from a sentinel before any session is consulted. This exemption covers the authentication surface only: the periodic maintenance purge operates on whatever rows exist in either mode, so that rows left behind by a mode flip are not stranded.

#### Scenario: Login creates a row

- **WHEN** a user signs in successfully in multi-user mode
- **THEN** a session row SHALL exist for that user with a future expiry and no revocation time
- **AND** the session cookie SHALL carry an identifier that resolves to that row

#### Scenario: The identifier is not stored

- **WHEN** the session registry is read directly
- **THEN** no column SHALL contain the identifier carried by the cookie
- **AND** the stored value SHALL be a one-way hash of it

#### Scenario: Bootstrap registration creates a row

- **WHEN** the first administrator is created through bootstrap registration and signed in
- **THEN** a session row SHALL exist for that administrator

#### Scenario: Single-user mode creates no rows

- **WHEN** the panel is used with multi-user mode disabled
- **THEN** no session row SHALL be created or required to authenticate a request

#### Scenario: Maintenance still runs in single-user mode

- **WHEN** the periodic cleanup runs with multi-user mode disabled and purgeable session rows exist
- **THEN** those rows SHALL still be purged

#### Scenario: Deleting a user relies on the database cascade

- **WHEN** a user with session rows is deleted through the administrative delete handler
- **THEN** the session rows SHALL be removed by the database's cascade
- **AND** the ORM SHALL NOT issue individual deletes or null the owning reference

### Requirement: A minted session row SHALL be durable before its cookie is handed to the browser

The mint implementation SHALL commit the session row itself and SHALL return only once that row is durable, so that no response can carry a cookie whose row was never committed. The request-scoped database session neither commits nor rolls back on its own, so an insert left to a caller's discretion is an insert that may never happen, and the resulting cookie would authenticate nothing.

Each of the three mint sites SHALL call it at a point where committing is safe: after the login handler's own commit, **after** the bootstrap transaction has committed rather than inside it — that transaction holds the bootstrap advisory lock and MUST NOT be lengthened — and in a second transaction after a password change has committed.

#### Scenario: The cookie works on the very next request

- **WHEN** a user signs in and immediately makes another request with the cookie the login response set
- **THEN** that request SHALL be authenticated
- **AND** the session row SHALL be present in a separately opened database session

#### Scenario: Bootstrap mints outside the bootstrap transaction

- **WHEN** the first administrator is registered
- **THEN** the session row SHALL be committed in a transaction separate from the one that holds the bootstrap advisory lock

#### Scenario: A failed mint does not leave a usable cookie

- **WHEN** the mint's commit fails
- **THEN** the response SHALL NOT present a cookie that authenticates a subsequent request

### Requirement: Session validation SHALL consult the registry on every request that resolves a browser identity

Every request that resolves a browser identity SHALL require the cookie to carry both a user reference and a session identifier, and SHALL resolve that identifier against the registry. The request SHALL be refused when the row is absent, when it carries a revocation time, when its expiry has passed, when the row's owning user is not exactly the user the cookie names, when the referenced user does not exist or is not active, or when the account-wide session version does not match the cookie. All of these checks SHALL be applied together; passing any subset SHALL NOT authenticate the request.

A cookie that carries a user reference but no session identifier SHALL be refused rather than accepted for compatibility, so that cookies issued before the registry existed stop being credentials at the moment the registry ships.

Every refusal SHALL clear the session cookie, so a rejected cookie cannot be replayed against a different route.

There SHALL be exactly one implementation of this validation, and every entry point that resolves a browser identity SHALL use it — including the login page's own "already signed in" short-circuit, which SHALL NOT read the cookie's user reference directly. A revoked cookie reaching a raw read there sends the visitor into a redirect loop between the login page and the panel.

#### Scenario: A replayed cookie after logout is refused

- **WHEN** a session cookie captured before logout is replayed against a panel route after that session has been logged out
- **THEN** the request SHALL be refused with a redirect to the login page
- **AND** no user identity SHALL be resolved from it

#### Scenario: A pre-registry cookie is refused

- **WHEN** a correctly signed session cookie carrying a user reference but no session identifier is presented
- **THEN** the request SHALL be refused and the cookie SHALL be cleared

#### Scenario: A row belonging to another user is refused

- **WHEN** a cookie is presented whose session row's owning user differs from the user the cookie names
- **THEN** the request SHALL be refused and the cookie SHALL be cleared
- **AND** neither the cookie's user nor the row's user SHALL be authenticated

#### Scenario: An expired row is refused while the cookie signature is still valid

- **WHEN** a cookie whose signature has not yet aged out is presented and its session row has passed its expiry
- **THEN** the request SHALL be refused

#### Scenario: A revoked row is refused

- **WHEN** a cookie is presented whose session row carries a revocation time
- **THEN** the request SHALL be refused

#### Scenario: An inactive user is refused even with a live row

- **WHEN** a cookie is presented whose session row is live but whose user has been deactivated
- **THEN** the request SHALL be refused

#### Scenario: The login page uses the same validation

- **WHEN** a revoked or expired cookie is presented to the login page
- **THEN** the login form SHALL be rendered
- **AND** the visitor SHALL NOT be redirected to the panel

#### Scenario: A valid session still works

- **WHEN** a cookie whose row is live, unrevoked and unexpired is presented by an active user whose session version matches and whose identity matches the row
- **THEN** the request SHALL be authenticated as that user

### Requirement: Logout SHALL revoke the session server-side

The panel's logout handler SHALL record a revocation time on the session row named by the presenting cookie before clearing the cookie, so that the cookie ceases to be a credential for every subsequent request rather than merely disappearing from the browser that submitted the logout.

If the revocation cannot be written, the handler SHALL still clear the cookie and redirect to the login page, and SHALL record an error-level event. A failure of the rollback that follows SHALL likewise not escape, so that no database fault can turn the sign-out into a server error — which would leave the user signed in and the cookie live, the state the handler exists to leave. The record SHALL carry the failing exception's class name only: not its message, its rendered statement, its bound parameters, or its traceback, any of which can carry the stored session hash.

Logout SHALL revoke only the presenting session.

#### Scenario: Logout revokes the presenting session

- **WHEN** a signed-in user submits the logout form
- **THEN** that session's row SHALL carry a revocation time
- **AND** a subsequent request replaying the same cookie SHALL be refused

#### Scenario: Other sessions survive a logout

- **WHEN** a user with two live sessions logs out of one of them
- **THEN** the other session SHALL remain usable

#### Scenario: A failing revocation still signs the browser out

- **WHEN** the revocation write fails during logout
- **THEN** the cookie SHALL still be cleared and the response SHALL still redirect to the login page
- **AND** an error-level event SHALL be recorded

#### Scenario: A failing rollback still signs the browser out

- **WHEN** the revocation write fails and the rollback that follows also fails
- **THEN** the cookie SHALL still be cleared and the response SHALL still redirect to the login page

#### Scenario: The failure record carries no statement text

- **WHEN** the revocation fails with an exception whose message contains the stored session hash
- **THEN** the record SHALL contain the exception's class name and SHALL NOT contain that hash or any twelve-character substring of it

### Requirement: Session revocation SHALL be written in the caller's transaction, never committed by the helper

The helpers that revoke one session or all of a user's sessions SHALL issue their write on the database session they are given and SHALL NOT commit; the calling handler SHALL commit as part of its own transaction. Handlers that hold the account-guard advisory lock must not commit between taking it and writing the flags it protects, so a helper that committed would silently break the last-administrator guard from inside.

Revoking all of a user's sessions SHALL affect only rows that are not already revoked, so a re-revocation does not rewrite historical revocation times.

#### Scenario: The revocation and the write it accompanies are atomic

- **WHEN** an account-level write that revokes sessions fails and its transaction is rolled back
- **THEN** no session SHALL have been revoked

#### Scenario: The advisory lock is not broken

- **WHEN** a handler holding the account-guard advisory lock revokes a target's sessions
- **THEN** no commit SHALL occur between the lock being taken and the protected flags being written

#### Scenario: Already-revoked rows keep their original revocation time

- **WHEN** a user's sessions are revoked twice
- **THEN** the rows revoked by the first call SHALL retain their original revocation time

### Requirement: Last-seen tracking SHALL take no second database connection and SHALL never fail a request

The last-seen update SHALL be issued on the **request's own** database session and committed there before the request handler runs, and SHALL NOT open a second database session. A request that holds two connection-pool leases halves the pool's effective capacity, and the pool's exhaustion behaviour is a timeout followed by a server error for every other caller in the process — a telemetry field must not be able to cause that.

The update SHALL be attempted only on safe request methods and only when the stored value is older than a configured interval. On any other method, or when the update or its commit fails, the touch SHALL be skipped, the request's transaction returned to a clean state, a warning logged, and the request served normally. Restricting the write to safe methods is what makes committing inside the dependency safe — no handler work is pending — and is also what keeps the row lock it takes from being held while a mutating handler waits on the account-guard lock, which would be a deadlock between the two.

#### Scenario: A fresh session is not written on every request

- **WHEN** several authenticated safe requests are made within the throttle interval
- **THEN** at most one last-seen update SHALL be issued

#### Scenario: A stale session is touched on a safe request

- **WHEN** an authenticated `GET` is made after the throttle interval has passed
- **THEN** the session's last-seen time SHALL be updated and committed

#### Scenario: An unsafe method does not touch

- **WHEN** an authenticated `POST` is made after the throttle interval has passed
- **THEN** no last-seen update SHALL be issued

#### Scenario: No second connection is taken

- **WHEN** an authenticated request performs a last-seen update
- **THEN** it SHALL use the same database session the request already holds

#### Scenario: Concurrency beyond the pool's capacity does not exhaust it

- **WHEN** more concurrent authenticated requests with stale sessions are served than the connection pool's total capacity
- **THEN** none SHALL fail with a connection-pool timeout

#### Scenario: A failing touch does not fail the page

- **WHEN** the last-seen update raises
- **THEN** the request SHALL still be served
- **AND** the failure SHALL be logged

### Requirement: Expired session rows SHALL be purged only after the later of their expiry and their revocation

The periodic cleanup job SHALL delete a session row only when both its expiry and, if present, its revocation time are more than the configured retention window (seven days) in the past.

Measuring from the expiry alone is not sufficient here: an administrative reset or a deactivation revokes every unrevoked row of that user, **including rows that had already expired**, and such a row is immediately past its expiry. Purging on the next tick would delete the record of a revocation an operator performed minutes earlier — the same disappearance that once made a no-op revoke read as success. Taking the later of the two timestamps makes the guarantee unconditional: a revoked row remains readable for the full window after the revocation, whenever it happened.

#### Scenario: A long-expired, never-revoked row is removed

- **WHEN** the cleanup job runs and a session row expired more than the retention window ago and was never revoked
- **THEN** that row SHALL be deleted

#### Scenario: An already-expired row revoked moments ago is retained

- **WHEN** a session row that had already passed its expiry is revoked, and the cleanup job then runs
- **THEN** that row SHALL still exist

#### Scenario: A recently revoked unexpired row is retained

- **WHEN** the cleanup job runs and a session was revoked moments ago and has not passed its expiry
- **THEN** that row SHALL still exist

#### Scenario: A live session is never purged

- **WHEN** the cleanup job runs while a session is live
- **THEN** that session's row SHALL be untouched

### Requirement: No session identifier, its stored hash, no password and no password hash SHALL appear in any log record

Records emitted by the session and password paths SHALL NOT contain a session identifier, the one-way hash of a session identifier that the registry stores, a password, or a password hash — nor any substring of any of them twelve characters or longer — in any field, message or traceback. The stored hash is included deliberately: it is the registry's key, so a record carrying it names a specific live session.

Where a record must identify a session it SHALL use the server's short truncated-digest form, the same one already used for presented bearer credentials.

The event names SHALL be those declared in the server's security-event catalogue, and the new events SHALL be registered there rather than emitted past it. Where the shared emitter is unavailable, the same names SHALL be emitted through a module logger with the identifying fields rendered into the message text, because the deployed log format emits the message alone. Records asserting a successful outcome SHALL be emitted after the transaction that makes them true has committed.

#### Scenario: A refused replay records no credential

- **WHEN** a revoked cookie is replayed and the refusal is recorded
- **THEN** the record SHALL NOT contain the session identifier, its stored hash, or any twelve-character substring of either

#### Scenario: A password change records no password

- **WHEN** a password change succeeds or is refused and the outcome is recorded
- **THEN** no record SHALL contain the submitted current password, the new password, or any password hash

#### Scenario: The events are declared in the catalogue

- **WHEN** the security-event catalogue is inspected
- **THEN** every event name emitted by the session and password paths SHALL have an entry declaring its permitted fields
