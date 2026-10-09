# Design: oauth-grant-lifetime (#325, #326)

Read `docs/architecture/oauth-and-grants.md` first. Everything here extends the grant-family model it describes (#64, #182) and changes none of its invariants: one `grant_id` ⇒ one `(client_id, user_id)`; `lock_grant` before any family row; revocation kills in-flight access tokens, rotation does not; the replay response is constant.

## Context

| Today | Where |
| --- | --- |
| Code lookup: `code_hash == h AND used == False [AND client_id == submitted]`, `FOR UPDATE` | `_handle_auth_code` |
| Check order: client exists → client auth → **code expiry** → redirect_uri → PKCE → ownerless → cross-user → scope → mark used → mint | same |
| `grant_id` minted at exchange, not recorded on the code | same |
| Access `now + 1h`, refresh `now + 30d`, `expires_in: 3600` literal, at both mint sites | `_handle_auth_code`, `_handle_refresh` |
| Cleanup: `DELETE oauth_codes WHERE expires_at < now-7d OR used` | `cleanup_expired_tokens` |
| No grants table; the family is `oauth_tokens.grant_id` | `src/oauth/grants.py` |

Every exchange already takes `lock_user_bootstrap` (one global advisory key) first, so **code exchanges are fully serialized against each other** today. That matters for the concurrency analysis below.

## Decisions

### D1. Family issuance time lives on `oauth_tokens`, copied like `grant_id` — not in a new `oauth_grants` table

A grants table is the "proper" shape but changes much more than the issue needs: a backfill of one row per distinct `grant_id`, an FK from `oauth_tokens`, its own cleanup schedule, and a new child table the #194 client sweep must reason about. The family already exists as "rows sharing `grant_id`", and the codebase already has exactly one inheritance rule for family attributes: set at the code exchange, copied verbatim by `_handle_refresh` from the locked refresh row. `grant_issued_at` follows the same rule, so it is uniform across a family by construction (both mint sites write one value to both rows; the backfill writes one value to every row).

The refresh path reads it off `old_token` — the row re-read under the grant lock — never off anything read before the lock.

### D2. Store issuance, derive the deadline from the current setting

`deadline = grant_issued_at + OAUTH_GRANT_ABSOLUTE_LIFETIME_DAYS`, computed at use, by one helper in `src/oauth/grants.py` (`grant_deadline`). Storing the deadline instead would freeze each family to the policy in force when it was minted, so *shortening* the policy (the direction an operator reaches for after an incident) would not touch existing families. Storing issuance makes both directions immediate. The cost — raising the setting extends grants beyond what their consent page showed — is accepted (L1); the consent page states the current policy, which is what V10.7.2 asks.

It also expresses the owner decision directly: "the clock starts at migration time" is `grant_issued_at = <migration timestamp>`.

### D3. Clamp at mint, refuse at the deadline, and check it at the middleware too

At both mint sites, with one `now` captured once:

- `deadline = grant_deadline(grant_issued_at)`;
- if `deadline - now < 1 s`, refuse (refresh only — at the code exchange `grant_issued_at = now`, so the remaining time is the full setting, ≥ 1 day);
- `access.expires_at = min(now + 1h, deadline)`, `refresh.expires_at = min(now + 30d, deadline)`;
- `expires_in = floor((access.expires_at - now).total_seconds())`, which is ≥ 1 by the refusal rule.

In `_handle_refresh` the deadline check sits **after** the reuse branch and the client checks and **before** the per-token expiry check, with reason `invalid_grant.grant_lifetime_exceeded` and `error_description: "grant lifetime exceeded; re-authorize"`. After the reuse branch, because a rotated-away token presented after the deadline is still reuse (the family-kill is harmless there, and a patient thief must gain nothing by waiting). Before per-token expiry, because once tokens are clamped the two coincide and the operator should see the cause that actually ended the grant. Nothing is revoked on this refusal — it is a grant reaching its end, the same reasoning #182 applies to an expired never-rotated token.

**Precedence, stated once so the two rules cannot be read as contradicting each other (Codex spec review).** The reuse decision comes first and wins: a presented refresh token whose locked row is `revoked` takes #182's branch — family revoked, the byte-identical generic `{"error": "invalid_grant"}` 400, no `error_description` — whether or not the family is past its deadline. The deadline refusal (`grant_lifetime_exceeded`, re-authorize description, nothing revoked) applies **only to a live, unrevoked refresh token**. A rotated-away token therefore never learns from the response that the grant has also aged out.

`APIKeyMiddleware` gets the same check on the OAuth path, immediately after its `expires_at` check: `now >= grant_deadline(...)` ⇒ 401 `invalid_token`, auth-failure reason `grant_lifetime_exceeded`. Clamping already guarantees this for every token minted after the change; the check exists for tokens minted *before a shortening* of the setting, so a shortened policy takes effect at the next request instead of up to an hour later. It reads a column of the row the middleware already loaded — no new query.

The panel's `_token_status` treats `now >= deadline` as `expired` (the #76 rule: the panel must never show live what the middleware refuses).

**The transfer subsystem is a fourth enforcement point (Codex spec review, MAJOR).** `src/services/transfer.py` re-validates the minting credential on its own, without going through the middleware: `plan_mint_window` clamps a capability to `credential_expires_at(cred)`, every redemption runs `_credential_ok`, and the publish gate re-runs `_credential_ok` against the credential row it holds `FOR UPDATE`. All three read `OAuthToken.expires_at` alone, so a pending upload capability minted before a shortening of the setting would still overwrite the vault after the grant was supposed to be dead. Both helpers therefore treat an `OAuthToken`'s effective expiry as `min(expires_at, grant_deadline(grant_issued_at))` — one change in `credential_expires_at`, which `_credential_ok` then uses for its OAuth expiry comparison, so the mint clamp, the redemption check and the pre-publication re-check stay literally the same predicate. API keys are untouched.

Boundary: the grant is dead **at** its deadline (`now >= deadline`). The middleware's existing per-token check treats the `expires_at` instant itself as live; a clamped access token whose `expires_at == deadline` is therefore refused by the deadline check at that instant. Tests pin both sides of the instant with an injected clock.

### D4. The setting: default 90, range 1–365, **no disabled spelling**

`oauth_grant_absolute_lifetime_days: int = Field(90, ge=1, le=365)`, env `OAUTH_GRANT_ABSOLUTE_LIFETIME_DAYS`. It is *not* a `NullableLimit`: empty, `null`, `none` and `0` all fail at boot.

The house precedent (`OAUTH_CLIENT_UNUSED_EXPIRY_DAYS`) refuses `0` and allows `null` as a kill switch. Half of that applies: `0` would make every grant dead on arrival, which reads as an outage, not a setting. The other half does not. The client sweep's kill switch exists because the sweep *deletes* things and an operator needs a rollback that stops deletion; nothing here deletes anything, and the worst outcome of a wrong value is a connector re-authorizing — recoverable by one consent click. Meanwhile a disabled spelling *is* #326: an ASVS L2 control that one blank env line silently removes, with nothing on `/health` or the consent page to show it. The rollback lever is raising the value, which takes effect immediately for every family (D2).

The ceiling of 365 keeps "absolute" meaning something — a value large enough to be indistinguishable from disabled would be the same hole spelled differently — while leaving an operator a full year. Validation errors name the setting and the allowed range.

### D5. Spent-code lineage: `oauth_codes.grant_id`, written with `used = True`

Nullable `varchar(64)`, no default, no index (codes are only ever looked up by the unique `code_hash`), no FK (there is no grants table). Written in the same transaction, from the same `new_grant_id()` value the tokens receive. NULL means "unspent", or "spent before 029 / by a pre-029 process during rollout" (L3).

### D6. Retention: seven days past the code's expiry — and why that window

The cleanup predicate becomes `expires_at < now - 7d` alone; the `OR used` disjunct goes. A code can only be spent before it expires (`spent_at ≤ expires_at`), so this keeps every spent code — and its lineage — for **at least seven days after it was spent**, the same argument and the same window `cleanup_expired_tokens` already uses for token rows.

Why seven days is enough, and longer buys nothing:

- **Who replays a code matters.** A thief who won the first exchange already holds the family; replaying the code would only kill it, so the thief never does. The replay that this detection exists for is the *legitimate* client's own exchange — the one that lost the race — or its automatic retry. Codes live ten minutes and connectors exchange within seconds of the redirect, so that replay lands in seconds to minutes. Seven days is three orders of magnitude of slack over the realistic case and still covers a manual, next-day retry.
- **It matches token retention**, so a family's history and the evidence that could revoke it age out on one schedule, and the panel's "revoked" rows from a replay stay visible for the same window.
- **The rows are cheap and secret-free:** one per consent; the code is stored only as a SHA-256; the PKCE challenge is a hash of a verifier that only the client holds.

The #194 client sweep is unaffected in practice: a client holding a spent code has issued a credential, so its `last_used_at` is stamped and it is never a candidate. Retained codes only make the existing no-child-row guard more conservative.

### D7. Replay check order: revalidate everything a first exchange validates, then decide

New `_handle_auth_code` order (bootstrap lock first, unchanged):

1. Look up by `code_hash` **alone**, `FOR UPDATE`, `populate_existing=True`. No `used` predicate (the flag is read, never inferred from emptiness — #182's lesson), no caller `client_id` predicate (a caller claim must not make a row look unknown).
2. No row → `invalid_grant.unknown_code` (unchanged).
3. Caller supplied a `client_id` and it differs from the row's → refuse `invalid_grant.client_id_mismatch`, revoke nothing. Today this case is reported as `unknown_code`; the response stays the same constant body.
4. Client exists, client authenticates (secret for confidential clients) — unchanged reasons.
5. `redirect_uri` equals the code's — unchanged reason.
6. PKCE verifier well-formed and matches — unchanged reasons.
7. **`used` is true → the replay branch (D8).** No other check follows on this branch.
8. Code expiry — **moved here** from before step 5. A spent code is necessarily past or near its ten-minute expiry by the time a late replay arrives; checking expiry first would turn every replay older than ten minutes into an ordinary "code expired" refusal and let the family survive. The visible consequence is that an *unspent*, expired code presented with a wrong verifier now reports the PKCE failure rather than the expiry — both are `invalid_grant` 400; only `error_description` differs. Accepted.
9. Ownerless / cross-user / scope / mark used + write `grant_id` / mint — unchanged, plus D3's clamp.

Revalidating before revoking is the constraint that makes this safe to ship: everything up to step 6 is what a party must prove to *redeem* a code, so a replay that triggers revocation is exactly a second party able to redeem it — the two-holder evidence RFC 6749 §4.1.2 acts on. A caller holding only the code hash, or the code without the verifier, gets the unknown/PKCE refusal and changes nothing.

### D8. The replay branch

Modelled line for line on #182's reuse branch:

- `grant_id` NULL (no lineage) → refuse with `invalid_grant.code_reused`, nothing revoked, nothing committed.
- Otherwise `lock_grant(grant_id)`, then `revoke_grant_family(session, grant_id)` (which re-takes the same re-entrant key). Commit if it flipped rows, roll back if zero.
- **Lock order:** bootstrap key → code row lock → grant key → token rows. The refresh path takes bootstrap → grant → token rows and never a code row; the panel takes grant → token rows and never a code row or the bootstrap key; the client sweep locks client rows `SKIP LOCKED`. No path takes a code row lock while holding a grant key, so there is no cycle.
- Response: byte-identical status, headers and body to `unknown_code` (`400 {"error": "invalid_grant"}`), and every DB call on the branch — revoke, commit, both rollbacks — guarded, so a failure still answers that same 400.
- Records, after the commit: `oauth_code_replay_detected` (WARNING; `client_id`, `grant_id`, `user_id`, `revoked_tokens`, `client_ip`) only when live tokens were revoked; `oauth_token_refused` with `invalid_grant.code_reused` when nothing live remained or no lineage exists; `oauth_code_replay_revocation_failed` (ERROR; class name only, no `exc_info`, no `str(exc)`) on failure. All through `security_events.emit`, so the suppressor bounds them. No code, verifier, challenge or hash in any field.

### D9. Concurrent double exchange

Because both exchanges take the global bootstrap key first, they are serialized: the second waits for the first to commit, then its locked read returns the row with `used = True` and the freshly written `grant_id` (READ COMMITTED `FOR UPDATE` re-reads the latest committed version). If the second presents the same verifier, it is a valid replay and revokes the family the first just minted. The outcome is order-independent: exactly one 200, one 400, and zero live tokens. That is RFC 6749 §4.1.2's behaviour and the intended cost (L4). The real-Postgres test runs both exchanges concurrently on separate connections and asserts exactly that.

### D10. Consent disclosure

`authorize_get` passes three strings from one helper in `src/oauth/grants.py` (`consent_lifetimes()`): access lifetime (`min(1 hour, cap)`), refresh lifetime (`min(30 days, cap)`) and the cap, humanized ("1 hour", "30 days", "90 days"). The template renders a block, after the request box and before the scope radios:

> **How long this access lasts.** The application receives an access token valid for 1 hour, which it renews using a refresh token valid for 30 days from its last renewal. Renewal stops 90 days after the application first receives its tokens (moments after you approve); after that the application must ask you again. You can revoke this access at any time from the control panel.

**Anchor (Codex spec review, MINOR).** The absolute period runs from `grant_issued_at`, which is the code exchange, not the approval click. The two are normally seconds apart and at most the code's ten-minute life; the disclosure says "after the application first receives its tokens" rather than "after you approve" so it never promises a deadline the server does not enforce. A test advances the clock between approval and exchange and asserts the deadline is anchored to the exchange.

Values come only from the helper, so the page cannot promise more than policy. Markup uses existing classes or new rules in the template's nonce'd `<style>` block; no `style=` attribute, no `on*=` handler. Owner browser pass with devtools open; zero CSP violations.

### D11. Migration 029

One unit, 025's pattern (marker comment mirrored in `src/models/db.py`; reconcile-or-refuse; `lock_timeout` / `statement_timeout` set and `RESET`; `search_path` pinned to `public` and `RESET`; downgrade drops only marked columns).

- `oauth_codes.grant_id varchar(64) NULL`, no default, marker comment. No backfill (no lineage exists for spent codes).
- `oauth_tokens.grant_issued_at timestamptz NOT NULL DEFAULT now()`, marker comment. Every pre-existing row is set to the migration transaction's timestamp — one value for all rows, so every family stays uniform — and a value already present is never overwritten (stamp-back idempotence).

**Why a server default** when 025 refused one: during a rolling deploy the previous image serves on the migrated schema and inserts tokens without the column. Without a default that is a NOT NULL violation — a 500 on every token exchange for the rollout. With it, an old-image row gets its insert time, which for a rotation during the rollout restarts that family's clock at the rollout (bounded, L9). The default's risk is the opposite one — a future mint site that forgets to copy the value silently resets the clock, i.e. reintroduces #326. That is closed by an AST test that requires every `OAuthToken(` construction in `src/` to pass `grant_issued_at=` explicitly, the same device that pins the async-commit allow-list.

The ORM declares the default (`server_default=func.now()`) and NOT NULL so `alembic check` is clean; the gate also verifies the default through the catalogue, since autogenerate does not compare server defaults.

Order inside the migration: `oauth_codes` then `oauth_tokens`, matching the app's own direction (code row, then token inserts), so a concurrent exchange queues behind it rather than closing a wait cycle.

## Owner decisions (recorded)

- Default **90 days**, range **1–365**, **not disable-able** (D4).
- Every pre-existing family's clock **starts at migration time** (D11, L2).
- **Raising** the setting extends existing grants (L1, accepted).
- A client that **retries a code exchange** loses that grant (RFC 6749 §4.1.2, L4, accepted).
- Codex spec-review findings (transfer enforcement, reuse-vs-deadline precedence, consent anchor) were accepted and folded into D3, D10 and the `file-transfer` delta.

## Accepted limitations

Recorded so they are not re-fixed when re-reported.

- **L1 Raising the setting extends existing grants.** The deadline is derived from issuance and the *current* setting (D2), so a raised value applies to families whose consent page showed the old one. Owner's policy lever; lowering is the direction that matters and takes effect at the next request.
- **L2 Pre-029 families get up to 90 days from the deploy, whatever their real age.** Owner decision: nobody is logged out by the deploy.
- **L3 Codes spent before 029, or by a pre-029 process during the rollout, carry no lineage**; their replay refuses and revokes nothing. Codes live ten minutes, so the window closes within minutes of the rollout.
- **L4 A legitimate client that retries a code exchange after losing the response loses that grant** and must re-authorize. RFC 6749 §4.1.2 behaviour; indistinguishable from theft at the server.
- **L5 Replay detection lasts seven days past the code's expiry** (D6). A later replay finds no row and is an ordinary unknown-code refusal.
- **L6 Timing.** The replay branch locks, reads and writes, so it is slower than an unknown-code refusal; status, headers and body are what is constant (#182's residual, same reasoning).
- **L7 The alarm is written after the commit**; a crash in between keeps the revocation and loses the record (#182's residual).
- **L8 A request already in flight at the deadline completes** — the middleware resolves the token once per request, as for revocation.
- **L9 Rolling-deploy gap.** While old pods serve: they mint unclamped tokens (bounded by the old 30-day refresh life, then clamped at their next rotation by a new pod), rotations they perform restart that family's clock at the insert time, and their cleanup still deletes used codes. Bounded to the rollout.
- **L10 Clock source.** Mint-time issuance and all comparisons use the application clock; the backfill and the server default use the database clock. Skew between the two is seconds against a 90-day window.
- **L11 Code-only theft cannot trigger revocation.** By design: the PKCE verifier is required, so a party holding only the code cannot end anyone's grant (D7). The detection covers the case that matters — two parties both able to redeem.

## Non-goals

- A per-grant "re-authorization due" date in the panel (status is enough for this change; a follow-up issue if wanted).
- A grants table, or moving `scope`/`user_id` to the family level.
- Changing the 1-hour or 30-day per-token lifetimes.
- Any change to `/revoke`, the panel's revoke/scope controls, or #182's reuse branch beyond the deadline check placed after it.
