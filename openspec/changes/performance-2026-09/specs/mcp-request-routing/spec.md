## MODIFIED Requirements

### Requirement: Refreshing a user's cached vault root removes a revoked assignment
Refreshing the cached vault root for a single user SHALL write the user's current assignment, or remove the cached entry when the user has no usable assignment, so that a revocation takes effect on the next authenticated tool call in every worker process without depending on an explicit cache-clear call.

The refresh SHALL be a fresh read of `users.is_active` and `users.vault_path`, made on every authenticated request and bound to that request. Two ways of making it satisfy this requirement:
- the dedicated `warm_user_vault_cache` query;
- the same two columns read in the credential lookup statement itself (an outer join from the API key or OAuth token to its user), applied through the same write-or-evict rule.

Folding the read into the credential statement SHALL NOT weaken either property: the value is still read per request, and it is still bound to the request so that it outranks the process-global cache. The inactive-user refusal SHALL keep its reason code and response body, and a missing `users` row SHALL be refused as inactive.

#### Scenario: Mid-session unassignment in a process that did not serve the panel request
- **WHEN** a user's vault path is cleared and a worker process that still holds
  the previous value refreshes that user's cached root while authenticating the
  next tool call
- **THEN** the cached entry SHALL be removed and the tool call SHALL be refused

#### Scenario: Deactivated user
- **WHEN** the refresh finds the user inactive or absent
- **THEN** the cached entry SHALL be removed

#### Scenario: The folded read still revokes on the next request
- **WHEN** an API key's or OAuth token's user is deactivated, unassigned or deleted between two requests, and the second request's credential lookup reads the user's columns in the same statement as the credential
- **THEN** the second request SHALL be refused exactly as it would have been by the separate refresh query, with the same reason code and body

#### Scenario: One statement authenticates an API key
- **WHEN** an API-key request authenticates and the key's `last_used_at` is within the write resolution
- **THEN** authentication SHALL issue exactly one database statement, which returns the key and its user's `is_active` and `vault_path`

## ADDED Requirements

### Requirement: Per-request authentication bookkeeping SHALL be throttled and SHALL NOT wait on a disk flush
`api_keys.last_used_at` SHALL be written at most once per 60 seconds per key: an authenticated request whose loaded `last_used_at` is newer than that SHALL issue no write. When a write is due, it SHALL be a conditional update that re-checks the age in its own predicate, so concurrent requests that both saw a stale value result in at most one effective write. It SHALL be committed with `SET LOCAL synchronous_commit = off` in the same transaction.

A write whose loss after a database crash could re-admit a caller or forget a revocation SHALL remain synchronously committed. That covers credential and OAuth issuance, rotation and revocation, authorization-code exchange, client registration, panel session mint and revoke, user edits, and transfer-token mint, redemption and publication. Only bookkeeping whose loss undercounts SHALL use asynchronous commit.

`SET LOCAL` SHALL be the only form used, so the setting ends with its transaction and SHALL NOT be observable on a later checkout of the same pooled connection.

#### Scenario: A fresh stamp issues no write
- **WHEN** a key whose `last_used_at` is 10 seconds old authenticates
- **THEN** no UPDATE of `api_keys` SHALL be issued

#### Scenario: A stale stamp is written asynchronously, once
- **WHEN** two requests for a key whose `last_used_at` is two minutes old authenticate concurrently
- **THEN** each request that issues the UPDATE SHALL have issued `SET LOCAL synchronous_commit = off` in that transaction first
- **AND** the stored value SHALL advance once, not be re-written by the second request

#### Scenario: The setting does not leak through the pool
- **WHEN** a connection that committed an asynchronous bookkeeping write is checked out again
- **THEN** `SHOW synchronous_commit` on it SHALL report the server default

#### Scenario: Grant and revoke paths stay synchronous
- **WHEN** an OAuth code is exchanged, a refresh token rotated or revoked, or a transfer token minted or redeemed
- **THEN** no `synchronous_commit` setting SHALL be issued in that transaction

### Requirement: The usage row SHALL be committed asynchronously, and its write contract SHALL be otherwise unchanged
`_insert_usage` SHALL issue `SET LOCAL synchronous_commit = off` as the first statement of its transaction, for both the initial insert and the foreign-key-cleared retry. Nothing else about the usage write SHALL change:
- `write_usage_row` SHALL return `True` only after the insert's transaction has committed and is visible to other sessions, and `False` otherwise;
- the writer-concurrency lease, the single FK retry, and the refusal coalescer's requeue of an unconfirmed row (#193) SHALL behave exactly as before.

`True` SHALL be documented as meaning "committed and visible", not "durable across a database server crash". A crash of the PostgreSQL server or of the host may lose rows committed within the preceding ~600 ms. An application crash or restart SHALL lose no committed row.

#### Scenario: A successful write still reports True
- **WHEN** a tool call completes and its usage row commits
- **THEN** `write_usage_row` SHALL return `True` and a second session SHALL be able to read the row immediately

#### Scenario: A failed write still requeues
- **WHEN** the usage insert fails for a reason other than a foreign-key violation
- **THEN** `write_usage_row` SHALL return `False` and a coalesced caller SHALL requeue its unconfirmed weight, as before

#### Scenario: The FK retry is also asynchronous
- **WHEN** the initial insert fails on a dangling credential and is retried with the credential columns cleared
- **THEN** the retry's transaction SHALL also begin with `SET LOCAL synchronous_commit = off`
