## Why

The 2026-09-04 OWASP ASVS 5.0 assessment left issue #182 (V10.4.5, medium): `_handle_refresh` **rotates** correctly — a new pair is minted and the presented refresh token is marked revoked in the same commit — but it does nothing when that already-rotated token comes back. The unfiltered lookup resolves the `grant_id`, the family lock is taken, the `revoked == False` select returns nothing, and the handler answers `invalid_grant` without calling `revoke_grant_family`.

Rotation without reuse detection is half the requirement, and the missing half is the half that detects theft. After a rotation exactly one party can hold the current refresh token; a second presentation of the rotated-away one means two parties hold the same credential. The production clients (claude.ai, ChatGPT) register `token_endpoint_auth_method: "none"`, so possession *is* the credential — the thief who redeems first keeps an identically-scoped pair (up to `readwrite` over the whole vault) and can keep refreshing it for the 30-day sliding window, while the legitimate client sees `invalid_grant` and quietly re-authorizes. Nobody is told, and the signal RFC 6819 §5.2.1.1 / OAuth 2.1 define for exactly this is discarded.

No test covered refresh-token replay: the two existing "a revoked grant cannot be refreshed" tests start from a panel revocation that has *already* emptied the family, so they cannot observe the missing revocation. `openspec/specs/oauth-authorization-integrity/spec.md` had no reuse requirement either — the behaviour was unspecified, not merely unimplemented.

## What Changes

- **`_handle_refresh` treats a revoked-row hit as reuse and revokes the family.** At the one point where the family is identified and the locking select finds no live row — which can only mean "the row exists and is revoked" — the handler calls `revoke_grant_family(session, grant_id)` and commits, killing every remaining live access and refresh token in the family, including the pair the first redemption rotated into.
- **The external response is unchanged and constant.** Byte-identical `invalid_grant` body and status to the unknown-token path, so the caller cannot distinguish "hit a live family" from "named nothing", and cannot detect that reuse detection exists. The revocation is wrapped so that a write failure also cannot turn this path into a 500 while the not-found path stays a 400.
- **Race-safety is inherited, not added.** The grant lock is already held before the select, so no rotation can insert a pair into the family between the select and the UPDATE; `revoke_grant_family` re-takes the same transaction-scoped advisory lock, which is re-entrant. No new lock, no new ordering, no cycle.
- **Explicit non-triggers.** An *expired but never rotated* refresh token is not reuse (its row is live, so the handler reaches the existing expiry check and revokes nothing); an unknown token hash names no family and revokes nothing; a family already fully revoked is a no-op that commits nothing.
- **One structured WARNING** (`event="oauth.refresh_reuse_detected"`) carrying `client_id`, `grant_id`, `user_id` and the number of tokens revoked, and no token or hash material. Emitted only where live tokens were actually revoked, so the not-found path and a repeat replay against a dead family cannot drown the real alarm.

No migration, no new dependency, no configuration.

## Capabilities

### New Capabilities

(none)

### Modified Capabilities

- `oauth-authorization-integrity`: adds the reuse-detection half of refresh-token rotation — a presented refresh token whose row exists but is revoked revokes its whole grant family, under the lock already held, behind a response indistinguishable from the unknown-token refusal.

## Impact

- `src/oauth/routes.py` — `_handle_refresh`'s `if not old_token` branch only; a module logger is added.
- `docs/architecture/oauth-and-grants.md` — the rotation section gains the reuse rule: what fires it, why the response stays constant, why the held lock makes it race-safe, and the three non-triggers.
- Tests: `tests/test_issue_182_refresh_reuse.py` (new, fake-session) and two pg-backed cases in `tests/integration/test_oauth_grants_pg.py`.
- Operators gain a WARNING that means "a refresh token of this grant leaked"; the affected user must re-authorize, which is the intended cost of the detection.
- Closes #182.
