## Why

The 2026-09-04 OWASP ASVS 5.0 assessment left issue #182 (V10.4.5, medium): `_handle_refresh` **rotates** correctly — a new pair is minted and the presented refresh token is marked revoked in the same commit — but it does nothing when that already-rotated token comes back. The unfiltered lookup resolves the `grant_id`, the family lock is taken, the `revoked == False` select returns nothing, and the handler answers `invalid_grant` without calling `revoke_grant_family`.

Rotation without reuse detection is half the requirement, and the missing half is the half that detects theft. After a rotation exactly one party can hold the current refresh token; a second presentation of the rotated-away one means two parties hold the same credential. The production clients (claude.ai, ChatGPT) register `token_endpoint_auth_method: "none"`, so possession *is* the credential — the thief who redeems first keeps an identically-scoped pair (up to `readwrite` over the whole vault) and can keep refreshing it for the 30-day sliding window, while the legitimate client sees `invalid_grant` and quietly re-authorizes. Nobody is told, and the signal RFC 6819 §5.2.1.1 / OAuth 2.1 define for exactly this is discarded.

No test covered refresh-token replay: the two existing "a revoked grant cannot be refreshed" tests start from a panel revocation that has *already* emptied the family, so they cannot observe the missing revocation. `openspec/specs/oauth-authorization-integrity/spec.md` had no reuse requirement either — the behaviour was unspecified, not merely unimplemented.

## What Changes

- **`_handle_refresh` reads the revocation flag under the lock and treats a set flag as reuse.** The refresh row is resolved by token hash and type, the family lock is taken, the row is re-read under it, and a `revoked` row means replay: `revoke_grant_family(session, grant_id)` and a commit kill every remaining live access and refresh token in the family, including the pair the first redemption rotated into.
- **The lookup no longer carries the caller-supplied `client_id`.** Folding the caller's claim into the query made a replay presented under any other (or a garbage) `client_id` look *unknown*, so the live family survived the very replay that proved the token leaked. Identity is now checked against the row afterwards, on the rotation path: a mismatch against a **live** token stays the refusal it has always been and revokes nothing.
- **The external response is unchanged and constant in status, headers and body.** Byte-identical to the unknown-token refusal, so the caller cannot distinguish "hit a live family" from "named nothing", nor detect that reuse detection exists. Every database call on the path — revocation, commit, and both rollbacks — is guarded, so no failure after detection can answer 500 where the not-found path answers 400. Timing is explicitly *not* claimed: the detection path does strictly more work. Accepted residual.
- **Race-safety is inherited, not added.** The grant lock is already held before the authoritative re-read, so no rotation can insert a pair into the family between that read and the UPDATE; `revoke_grant_family` re-takes the same transaction-scoped advisory lock, which is re-entrant. No new lock, no new ordering, no cycle.
- **Explicit non-triggers.** An *expired but never rotated* refresh token is not reuse (its row is live, so the handler reaches the existing expiry check and revokes nothing) — but a token that was rotated away and has since expired still is, because the flag is read before the expiry check. An unknown token hash names no family; a row deleted by cleanup while we waited for the lock names none either; a family already fully revoked is a no-op that commits nothing.
- **One WARNING** (`oauth.refresh_reuse_detected`) carrying `client_id`, `grant_id`, `user_id` and the number of tokens revoked **in the message text as well as in `extra`** — the deployed formatter is `%(message)s`, so `extra` alone never reaches an operator. No token or hash material, ever. Emitted only where live tokens were actually revoked, so the not-found refusals and a repeat replay against a dead family cannot drown the real alarm. A failure while revoking is recorded with the exception's **class name only**: SQLAlchemy renders the failing statement and its bound parameters — one of which is the token hash — into the error text, and the engine does not set `hide_parameters`.

No migration, no new dependency, no configuration.

## Accepted residuals

- **Timing.** The detection path takes a lock, reads twice and writes; a replayed token is therefore measurably slower to refuse than an unknown one. Equalizing it would mean doing that work for every unknown token — a write path any unauthenticated caller could drive. Status, headers and body are what the requirement pins.
- **The record is written after the commit.** A crash in between keeps the revocation and loses the alarm. That is the right half to lose, and an outbox for one WARNING would add a larger failure mode than it closes.
- **Structured log fields.** `extra` is carried for a future structured formatter; today only the message text is emitted, which is why the identifiers are in it.

## Capabilities

### New Capabilities

(none)

### Modified Capabilities

- `oauth-authorization-integrity`: adds the reuse-detection half of refresh-token rotation — a presented refresh token whose row exists but is revoked revokes its whole grant family, under the lock already held, behind a response indistinguishable from the unknown-token refusal.

## Impact

- `src/oauth/routes.py` — `_handle_refresh`'s row lookup and the reuse branch; a module logger is added. The lookup is restructured (one query, hash + type only, re-read under the lock) and the caller-supplied `client_id` check moves to an explicit comparison on the live-token path. No other handler changes.
- `docs/architecture/oauth-and-grants.md` — the rotation section gains the reuse rule: what fires it, why the response stays constant, why the held lock makes it race-safe, and the three non-triggers.
- Tests: `tests/test_issue_182_refresh_reuse.py` (new, fake-session) and two pg-backed cases in `tests/integration/test_oauth_grants_pg.py`.
- Operators gain a WARNING that means "a refresh token of this grant leaked"; the affected user must re-authorize, which is the intended cost of the detection.
- Closes #182.
