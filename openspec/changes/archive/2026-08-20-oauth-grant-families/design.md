## Context

`OAuthToken` carried `client_id` and `user_id` and nothing else that could
group rows. Every "act on the grant" behaviour the panel appeared to offer was
therefore unimplementable, and the two controls it did offer were misleading
rather than merely incomplete.

## Goals / Non-Goals

**Goals.** One durable identifier per consent event; one code path that
resolves a family; revocation and downgrade that survive a rotation; a
registered scope that caps every path; a panel that shows the same liveness the
middleware enforces.

**Non-Goals.** Per-session grants for pre-014 tokens (impossible to recover —
the information was never stored). A UI for browsing full token history. Any
change to how API keys are displayed (#76's keys/users half is separate work).

## Decisions

### D1 — `grant_id` is NOT NULL, with a backfill, not nullable with a fallback

The alternative was leaving `grant_id` NULL for pre-014 rows and resolving
those families by `(client_id, user_id)` at read time. Rejected in the issue's
decision comment, and correctly: two code paths for "find the family" is how
this bug returns. The backfill assigns one id per distinct
`(client_id, user_id)` over *all* rows, including revoked ones, so exactly one
resolution rule exists from the first minute after the migration.

The approximation is stated honestly: where a client genuinely backed two
concurrent sessions for one user, those collapse into one family and are
revoked together. That errs toward over-revoking, and it converges — every
grant issued after the migration is exact.

### D2 — Families are resolved by `grant_id` alone, never by `grant_id AND user_id`

A `user_id` predicate on the family write looks like defence in depth and is
not: under a broken invariant it converts a *complete* revocation into a
partial one, which is the failure mode this whole change exists to remove. The
invariant (one `grant_id` ⇒ one `(client_id, user_id)`) is instead established
at every write site and by the backfill's group key, and asserted directly in
`tests/integration/test_schema_check.py`.

Authorization is unaffected: `_assert_oauth_token_owner` still guards the token
the operator names, and because the family cannot span users that check covers
everything the write touches.

### D3 — A transaction-scoped advisory lock on the family, taken before any family row

Row locks cannot make revocation linearizable here, because the rows that would
escape it *do not exist yet*. Under READ COMMITTED an
`UPDATE … WHERE grant_id = :g` takes its snapshot at statement start; a
concurrent `_handle_refresh` that commits two new rows afterwards leaves them
untouched, and the operator is told the grant is revoked while the client keeps
the pair it just rotated into.

`pg_advisory_xact_lock(key)` on a key derived from the `grant_id` closes it.
Both sides take that one lock **before** touching any family row, so the
acquisition order is total and there is no cycle to deadlock on; the lock
releases on COMMIT or ROLLBACK. The subsequent statement takes a fresh
snapshot, which is precisely what makes the concurrently-inserted rows visible.

`_handle_refresh` needs the id before it can lock, so it does one unlocked
lookup of `grant_id` by token hash, takes the lock, and only then runs the
authoritative `FOR UPDATE` select. The unlocked lookup deliberately does not
filter on `revoked` — it is finding a family, not authorizing a refresh.

The key is derived in Python (SHA-256 of a namespace prefix plus the id, folded
to a signed 64-bit int) rather than with Postgres' `hashtext()`, which is
undocumented and has changed algorithm between majors.

### D4 — Revocation kills in-flight access tokens; rotation does not

`_handle_refresh` still lets the access token it replaces run to its natural
expiry. That is correct for rotation — the client may have requests in flight.
It is wrong for revocation, so the family revoke covers access rows too. An
hour of surviving write access after the operator clicked Revoke is exactly the
failure being fixed.

### D5 — The RFC 7009 endpoint is family-scoped too

RFC 7009 §2.1 explicitly permits revoking the associated refresh token, and
anything narrower reproduces the same near no-op for any client that presents
its access token. Nothing is leaked by the widening: the caller already holds a
credential from this family, and the RFC requires HTTP 200 either way.

### D6 — The panel refuses an over-registration scope rather than clamping it silently

`update_oauth_token_scope` rejects `readwrite` for a client not registered for
it (redirect, no write) *and* clamps the value it does write. The refusal is
the honest signal — an operator who deliberately selected `readwrite` should
not be told nothing happened when the select snaps back — and the clamp is what
guarantees no path can write a scope above the registration even if a future
caller skips the refusal.

### D7 — #68 is fixed at the source (option b), not by unioning the panel listing

Unioning the client list means gating every client-level action, and the
obvious version of that renders the owner's "Delete this client and revoke all
its tokens?" button to the other user. Refusing the reuse in `authorize_post`
also closes the converse hazard, where the owner's Delete silently kills
another user's live grant. Single-user mode is untouched: there is no session
identity, so the check cannot fire.

### D8 — Revocation history is bounded, not unbounded

Revoked and expired rows are now listed, because a row that silently vanishes
is what made a no-op Revoke read as success. But a client refreshing hourly
leaves hundreds of rotated-away tokens in a 30-day window. Every *live* token
is always rendered; dead ones are capped at five per grant with the remainder
counted in a "+ N earlier" line, and the per-client scan is bounded so one
chatty client cannot make the page unbounded.

## Risks / Trade-offs

- **Deploy ordering.** 014 makes `grant_id` NOT NULL, so a container running
  pre-014 code cannot insert tokens after the migration. `make deploy` migrates
  then recreates, so the window is the recreate itself; a token exchange landing
  inside it fails with a 500 and the client retries. Acceptable, and the same
  shape as every other NOT NULL this repo has added.
- **Lock contention.** The advisory lock serializes concurrent operations on
  one grant. Grants are per-connector-per-user and refreshes are hourly, so
  contention is effectively nil; when it happens the loser waits, it does not
  fail.
- **Re-clamping on refresh changes returned scope strings.** A client whose
  registration was narrowed after the grant now sees a narrower `scope` in the
  refresh response. That is the point, and RFC 6749 §5.1 requires the response
  to state the granted scope when it differs from what was requested.
- **Revocation takes effect at the next authenticated request; an in-flight
  request completes.** `APIKeyMiddleware` resolves the token once, at the start
  of the request, so a tool call authenticated microseconds before a revoke or
  downgrade commits still runs with the permission it was granted. Closing that
  would mean holding the grant lock across tool execution — arbitrary vault I/O,
  embedding calls, network fetches — which trades a bounded, sub-second
  staleness for unbounded lock contention on every request. Accepted as a
  documented limitation: it is the same optimistic level as `edit_note(expected=…)`
  and the transfer fingerprint check, declared rather than implied.
