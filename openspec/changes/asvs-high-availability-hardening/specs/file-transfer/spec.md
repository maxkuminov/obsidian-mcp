## ADDED Requirements

### Requirement: The upload endpoint holds no database connection while waiting or streaming

`PUT /transfer/upload` SHALL commit and close the session used for the claim, the identity/root/path re-validation and the publication-support probe before it waits for an upload slot or reads any body byte, and SHALL hold no database connection while the body streams. Every subsequent database action on that request — releasing or consuming the claim, and the publish gate — SHALL open its own short-lived session. The claimed token row is used detached after phase 1; only its plain columns are read.

#### Scenario: Queued uploads do not pin the pool

- **WHEN** `TRANSFER_MAX_CONCURRENT_UPLOADS` uploads are streaming and further uploads are waiting for a slot
- **THEN** the engine's checked-out connection count SHALL be zero while no other request is active, and an unrelated session SHALL obtain a connection without waiting on the upload traffic (integration test against the real engine, with the per-loop upload semaphore reset between cases)

#### Scenario: The route reads only what a detached row carries

- **WHEN** the phase-1 session is closed and the route proceeds to stream, release, consume or publish
- **THEN** every token attribute it reads SHALL be a plain column already loaded, and no lazy load SHALL be attempted

### Requirement: The slot wait is bounded, deadline-aware, and distinguishes an overrun from a full queue

`stream_to_vault` SHALL accept a slot timeout and SHALL acquire its upload slot in slices of at most one second, recomputing the remaining stream deadline against the wall clock on every slice so the deadline is never converted to a monotonic value. When the wait ends without a slot, the deadline SHALL be re-checked first: an overrun SHALL be handled exactly as an overrun during the body (HTTP 408, token `consumed`, nothing written, no staged bytes); only a wait cut short by the slot timeout (30 seconds) with deadline remaining SHALL raise a queue-timeout error, which `PUT /transfer/upload` SHALL map to HTTP 503 with `Retry-After: 5` and SHALL release the claim to `pending`, because the capability's window is still open and nothing was staged. A 503 SHALL only ever be produced after the claim and re-validation have succeeded, so the uniform-404 rule for non-usable tokens is unaffected. `import_from_url` SHALL keep the same slot bound through the same parameter.

#### Scenario: Queue wait times out with deadline remaining

- **WHEN** an upload waits for a slot for the full slot timeout while its deadline is still in the future
- **THEN** the response SHALL be HTTP 503 with `Retry-After: 5`, the token SHALL be `pending` again, and no staged bytes SHALL exist

#### Scenario: Deadline elapses during the wait

- **WHEN** an upload's remaining deadline is shorter than the slot timeout and elapses while it waits for a slot
- **THEN** the response SHALL be HTTP 408 and the token SHALL become `consumed`, without acquiring a slot

#### Scenario: Deadline already overrun before the wait

- **WHEN** the deadline computed from the claim has already passed when the route is about to wait for a slot (produced in tests by advancing `transfer.now_utc`)
- **THEN** the response SHALL be HTTP 408 and the token SHALL become `consumed`, without acquiring a slot

### Requirement: The note size cap binds markdown transfers

A transfer whose bound path ends in `.md` (case-insensitive) SHALL be capped at the smaller of `MAX_NOTE_BYTES` and `MAX_FILE_WRITE_BYTES` — on `PUT /transfer/upload` (413 at cap+1, staged bytes discarded, claim released, exactly as an oversized file) and on `import_from_url` — so no transport path can place a markdown file the note tools would refuse.

#### Scenario: Oversized markdown upload

- **WHEN** an upload bound to a `.md` path streams more than the applicable cap
- **THEN** the response SHALL be HTTP 413, no staged bytes SHALL remain, and the token SHALL be `pending`

#### Scenario: Oversized markdown import

- **WHEN** `import_from_url` targets a `.md` path and the fetched body exceeds the applicable cap
- **THEN** the tool SHALL refuse naming the limit and nothing SHALL be published

### Requirement: The engine's pool timeout is explicit

The database engine SHALL set `pool_timeout` explicitly (30 seconds, the library default), so the bound a pool-exhaustion failure is measured against is written in the engine configuration rather than inherited silently.

#### Scenario: Pool exhausted

- **WHEN** every pooled connection is checked out and a further session requests one
- **THEN** the request SHALL fail after the configured `pool_timeout` with the library's timeout error
