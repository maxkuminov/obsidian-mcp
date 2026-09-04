## ADDED Requirements

### Requirement: The upload endpoint holds no database connection while waiting or streaming

`PUT /transfer/upload` SHALL commit and close the session used for the claim and the identity/root/path re-validation before it waits for an upload slot or reads any body byte, and SHALL hold no database connection while the body streams. Every subsequent database action on that request — releasing or consuming the claim, and the publish gate — SHALL open its own short-lived session. Before waiting for a slot the route SHALL check the stream deadline; a deadline already overrun SHALL be handled exactly as an overrun during the body (HTTP 408, token `consumed`, nothing written). The wait for a slot SHALL be bounded by the smaller of 30 seconds and the remaining deadline; a wait that times out SHALL return HTTP 503 with a `Retry-After` header and SHALL release the claim to `pending`, because no body byte was read.

#### Scenario: Queued uploads do not pin the pool

- **WHEN** `TRANSFER_MAX_CONCURRENT_UPLOADS` uploads are streaming and further uploads are waiting for a slot
- **THEN** the number of pool connections checked out on behalf of upload requests SHALL be zero for every streaming and every waiting request, and an unrelated request on the same process SHALL obtain a connection without waiting on the upload traffic

#### Scenario: Queue wait times out

- **WHEN** an upload waits for a slot longer than the bounded wait
- **THEN** the response SHALL be HTTP 503 with `Retry-After`, the token SHALL be `pending` again, and no staged bytes SHALL exist

#### Scenario: Deadline already overrun before the wait

- **WHEN** the deadline computed from the claim has already passed when the route is about to wait for a slot
- **THEN** the response SHALL be HTTP 408 and the token SHALL become `consumed`, without acquiring a slot

### Requirement: The engine fails loudly on a held-idle connection

The database engine SHALL set `pool_timeout` explicitly and SHALL set `idle_in_transaction_session_timeout` for its connections, so that code which holds a transaction open across non-database I/O fails with a visible error rather than silently pinning a connection.

#### Scenario: A transaction is left idle past the timeout

- **WHEN** a session holds an open transaction with no statement for longer than the configured idle timeout
- **THEN** the server SHALL terminate that backend and the next statement on the session SHALL raise, which the caller sees as an error rather than a hang
