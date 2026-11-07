## Context

### What the code does today (verified on 7e8c992)

- `cimd.validate_document` (`src/oauth/cimd.py`) refuses any `token_endpoint_auth_method` other than absent or `none` with `client_metadata.auth_method_unsupported`. Every `/authorize` refusal answers the constant `{"error":"invalid_client"}` 400.
- ChatGPT's connector now presents `client_id=https://chatgpt.com/oauth/client.json`. Its document declares `"token_endpoint_auth_method":"private_key_jwt"`, `"token_endpoint_auth_signing_alg":"RS256"` and `"jwks_uri":"https://chatgpt.com/oauth/jwks.json"`, and lists `["none","private_key_jwt"]` in `token_endpoint_auth_methods_supported`. Every ChatGPT connection fails at `GET /authorize` (#420).
- `oauth_clients.token_endpoint_auth_method` (`String(32)`, NOT NULL) **already exists** and already holds `none` on every CIMD row. Two CHECKs pin it: `ck_oauth_clients_auth_method_secret` (`none` ⇔ no secret, `client_secret_post` ⇔ secret) and 036's `ck_oauth_clients_cimd_shape` (a CIMD row is `none` with no secret).
- `_client_authenticated(client, client_secret)` in `src/oauth/routes.py` is the one client-authentication definition for `/token` (both grants) and `/revoke`. Any method other than `client_secret_post` and `none` returns False, so a `private_key_jwt` row would fail closed today.
- `/token` takes `lock_user_bootstrap`, then the code row `FOR UPDATE` (or the grant lock and the refresh row `FOR UPDATE`), and authenticates the client **inside** that transaction. `/revoke` authenticates inside its session. Nothing in either handler performs network I/O today. `/token` is slowapi-limited to 10/min per address, `/revoke` to 20/min.
- `joserfc==1.7.7` (with Authlib) is a hard dependency in `requirements.txt` and is imported unconditionally (`src/auth/oidc.py`, via `src/auth/routes.py`). Native OIDC already verifies RS256/PS256/ES256 with `joserfc.jwt.decode` and `joserfc.jwk.KeySet`. The `requirements.txt` comment calls it "Optional browser OIDC". That comment becomes wrong in this change.
- The AS metadata advertises `token_endpoint_auth_methods_supported: ["none","client_secret_post"]` and has no revocation-endpoint auth fields. `tests/test_oauth_cimd.py` asserts `none` is present. No golden fixture pins the whole metadata document.

### Sources

- RFC 7523 §2.2 (client authentication with a JWT bearer assertion: `client_assertion_type=urn:ietf:params:oauth:client-assertion-type:jwt-bearer`, `client_assertion`), §3 (`iss` and `sub` are the `client_id`; `aud` identifies the AS, and "the token endpoint URL of the authorization server MAY be used"; `exp` is required; the AS MAY reject a JWT with an `exp` unreasonably far in the future; `jti` MAY be used to prevent replay).
- OpenID Connect Core §9 (`private_key_jwt`: `iss`, `sub`, `aud`, `jti`, `exp` required, `iat` optional; "the Audience SHOULD be the URL of the Authorization Server's Token Endpoint"; the AS must not accept a `jti` it has already seen while the JWT is valid).
- draft-ietf-oauth-rfc7523bis. It followed the 2025 audience-injection disclosure and makes the AS **issuer identifier**, as a single string, the audience a client should use. It moves away from multi-valued audiences and endpoint URLs.
- RFC 6749 §2.3: a client MUST NOT use more than one authentication method in a request.
- RFC 7591 §2 (`jwks_uri` and `jwks` are mutually exclusive) and RFC 8414 §2 (`token_endpoint_auth_signing_alg_values_supported`).
- CIMD draft-00 permits `private_key_jwt`; its only auth-method restriction is shared secrets.

## Goals / Non-Goals

**Goals:** ChatGPT connects through CIMD. It authenticates with `private_key_jwt` at `/token`, for both grants, and at `/revoke`. Nothing that works today changes behaviour: DCR clients (Claude.ai's fallback), `none` CIMD clients (Claude, Claude Code), and existing refresh families. Every place this codebase makes two refusals identical stays identical. Every outbound fetch an unauthenticated caller can cause stays bounded.

**Non-Goals:**
- `private_key_jwt` for DCR registrations.
- `client_secret_jwt`, `tls_client_auth`, EdDSA, and inline `jwks` documents.
- A consent-page or panel UI change. The consent card already identifies a CIMD client by host. The panel needs no new control.
- A periodic background JWKS refresh. Refetches happen only in a request's pre-phase (D7). See L4.
- Revocation-endpoint metadata fields, which the metadata does not carry today.

## Decisions

### D1. Which documents are admitted

`token_endpoint_auth_method` is admitted when it is absent, `none` or `private_key_jwt`. Every other value is still refused `auth_method_unsupported`. The **declared** value is honoured: a `private_key_jwt` document is never downgraded to `none`, even though `token_endpoint_auth_methods_supported` lists `none`. Honouring the declared value is the only reading under which the document's author controls how its client authenticates.

A `private_key_jwt` document SHALL also meet all of these:
- `jwks_uri` is a string that passes `validate_client_id_url`'s rules unchanged: ≤ 2,048 printable ASCII, no `?`/`#`, no userinfo, no dot segments, a non-root path, port 443/8443, byte-equal to its canonical form. Failing any rule gives `jwks_uri_invalid`.
- `jwks_uri` is **same origin** as the `client_id` URL, comparing scheme, canonical host and effective port. A different origin gives `jwks_uri_cross_origin`.
- There is no `jwks` member. One key source only (RFC 7591 §2); a document with `jwks` gives `jwks_inline_refused`.
- `token_endpoint_auth_signing_alg`, when present, is one of `RS256`, `PS256`, `ES256` and pins the algorithm. Any other value gives `signing_alg_unsupported`. When absent, any of the three is accepted.
- The existing rules still apply: no `client_secret`, `grant_types`/`response_types`, redirect URIs, scope.

A `none` document carrying `jwks_uri` or `jwks` is still admitted. Those members are ignored for a `none` client, as today. The projection grows by `auth_method`, `jwks_uri` and `signing_alg`. The last two are `None` for a `none` client.

### D2. The JWKS at /authorize: second charge, own cache

After the document validates, a `private_key_jwt` projection's JWKS is resolved by `cimd.resolve_jwks(projection, address)`. GET calls it so the user is never shown a consent that cannot complete, and POST calls it because POST writes the snapshot. It runs the same machinery as `resolve`, keyed by the `jwks_uri`:

> JWKS cache hit → join an in-progress fetch of the same `jwks_uri` (charged nothing) → the 30 s failure cooldown (same table, keyed by URL) → **one more charge to the same per-address budget** → 1 in flight per slot → 8 in flight per process → `fetch_url_guarded` (no redirects, 10 s, 64 KiB, deny list, pinned).

**A second charge, not a shared one.** A document cache hit charges nothing. If the JWKS rode the document's charge, every cached-document `GET /authorize` would be a free outbound request to the client's origin, an amplifier any caller could drive. Each outbound request therefore costs one charge. The document's flight has already released its slot when the JWKS fetch starts, so the per-slot cap of 1 is not self-blocking. A ChatGPT consent spends at most 2 of the address's 10 per minute, and nothing once both are cached.

The JWKS cache is a second bounded LRU of 256 entries with the document cache's TTL rules: `max-age` clamped to `OAUTH_CIMD_CACHE_MAX_SECONDS`, 300 s when the header is absent, and nothing stored under `no-store`/`no-cache`. It stores only a **validated** snapshot (D3). Every `JwksSnapshot` carries an immutable `fetched_at`, the moment its bytes were received from the origin. A cache hit returns that original time, never the time of the hit.

**`jwks_fetched_at` is always that origin fetch time** (Codex spec R1-2), at approval and at a refetch alike, and never the approval time. Otherwise an approval served from a 23 h-old cache entry would restart the 24 h staleness clock, and a removed key could survive about 48 h.

JWKS refusals are `client_metadata.jwks_<sub>`, behind the same constant `invalid_client`. The subs are `jwks_fetch_failed`, `jwks_fetch_timeout`, `jwks_http_status`, `jwks_too_large`, `jwks_content_encoded`, `jwks_ssrf_refused`, `jwks_not_json`, `jwks_invalid`, `jwks_private_key` and `jwks_no_usable_key`. Admission refusals reuse `fetch_cooldown`, `fetch_budget_exhausted` and `fetch_busy`. A JWKS failure does not arm the *document's* cooldown.

### D3. JWKS validation and the snapshot

`cimd.validate_jwks(obj)` returns a normalised snapshot or raises. The checks, in order:
1. The value is a JSON object whose `keys` is a list of 1–16 objects. Anything else is `jwks_invalid`.
2. Any key carrying a private or symmetric member (`d`, `p`, `q`, `dp`, `dq`, `qi`, `oth`, `k`) refuses the **whole set** with `jwks_private_key`. A publisher leaking private keys is misconfigured, and none of its keys should be trusted.
3. Keys that are **ignored**, not refused:
   - `use` present and not `sig`;
   - `key_ops` present without `verify`;
   - `kty` other than `RSA` or `EC`;
   - EC with `crv` other than `P-256`;
   - `alg` present and outside `RS256`/`PS256`/`ES256`;
   - an RSA modulus under 2,048 or over 8,192 bits;
   - an RSA exponent that is not an odd integer in 3…2³²;
   - a key that does not import as a public key in `joserfc`.
4. Two usable keys sharing a `kid` refuse the set with `jwks_invalid`, because ambiguous selection is not resolved by order. Zero usable keys gives `jwks_no_usable_key`.

The snapshot is `{"keys": [...]}` holding only the usable keys, each reduced to its public members: `kty`, `kid`, `alg`, and `n`/`e` or `crv`/`x`/`y`. It is a few KiB at most. The same validator runs on the `/token` refetch (D7).

### D4. The authorization hash

For a `private_key_jwt` projection the hashed object gains `token_endpoint_auth_method`, `jwks_uri` and `token_endpoint_auth_signing_alg` (or JSON `null`). Key material is excluded because it rotates. For a `none` projection the hashed object is **byte-identical to today's**: the three members are omitted, not set to `none`/`null`. A consent in flight across the deploy, and the existing hash tests, are unaffected. The method's presence in the hash is what makes a `none` ⇄ `private_key_jwt` flip between GET and POST a `metadata_changed` refusal.

### D5. The synthetic row and migration 038

**The pinned method is the existing `token_endpoint_auth_method` column.** No new method column is added. Existing CIMD rows already read `none`, so they need no backfill and behave exactly as before.

Migration `038_oauth_cimd_private_key_jwt.py` (`down_revision "037"`) adds four nullable columns, each comment-marked so a re-run reconciles by 036's rule:
- `jwks_uri VARCHAR(2048)`;
- `token_endpoint_auth_signing_alg VARCHAR(16)`;
- `jwks_snapshot JSONB`;
- `jwks_fetched_at TIMESTAMPTZ`.

It replaces two CHECKs and adds one, each reconciled against a probe definition as 036 does:
- `ck_oauth_clients_auth_method_secret`: a `private_key_jwt` row has no secret hash. The `none` and `client_secret_post` clauses are unchanged.
- `ck_oauth_clients_cimd_shape`: a metadata URL exists exactly when the id starts `cimd_`, and such a row has no secret and a method in (`none`, `private_key_jwt`).
- New `ck_oauth_clients_private_key_jwt_shape`:
  - `private_key_jwt` ⇒ a CIMD row with `jwks_uri`, `jwks_snapshot` and `jwks_fetched_at` all NOT NULL;
  - any other method ⇒ all four new columns NULL;
  - the alg is NULL or one of the three.

DCR rows can therefore never be `private_key_jwt`, and the database says so. The **downgrade** refuses while any `private_key_jwt` row exists, naming the count (dropping the columns would strand those grants). Otherwise it drops the new constraint and columns and restores 036's two CHECK definitions. The migration needs only table ownership, so it passes `test_nonsuperuser_migrations_pg.py`.

**The approve upsert** writes `token_endpoint_auth_method`, `jwks_uri`, the alg, the snapshot and `jwks_fetched_at = <the snapshot's origin fetch time>`, and refreshes them `ON CONFLICT`. A later approval therefore refreshes the snapshot. An approval keeps a stored snapshot of the same `jwks_uri` when that snapshot is newer than the approval's own (the D7 compare-and-set rule, evaluated in the conflict update against the row as it stands), so a snapshot never moves backwards.

**Downgrade at approve is refused.** An existing row is already read `FOR UPDATE` in the approve transaction. If that row is `private_key_jwt` and the re-resolved document now says `none`, the approve refuses `client_metadata.auth_method_downgrade` and writes nothing. Two first approvals racing for an absent row both see nothing to lock, so the `ON CONFLICT DO UPDATE` carries the same rule as its `WHERE` (never `private_key_jwt` → `none`); an update it skips gives the same refusal and rolls back. A refresh token issued to a key-holding client must never become redeemable without the key. The way out is the panel's Delete, which cascades that user's grants, followed by a fresh consent. Upgrading `none` → `private_key_jwt` is allowed. The existing `none` families of that row must then present assertions; that is the declared method taking effect.

### D6. Assertion verification (`src/oauth/client_assertion.py`, new)

One function, `authenticate(form, row, endpoint, now)`, returns or raises `AssertionRefusal(sub)`. It runs where `_client_authenticated` runs today (D8). `_client_authenticated` becomes a dispatcher over the row's method:
- **`private_key_jwt` row:**
  - `client_assertion` absent → `assertion_missing`;
  - `client_secret` present, with or without an assertion → `secret_presented` (RFC 6749 §2.3);
  - otherwise `authenticate(...)`.
- **CIMD `none` row:** any `client_assertion` or `client_assertion_type` present → `assertion_unexpected`. This is the one behaviour change for `none` CIMD rows, and no known client sends these parameters. Otherwise the row authenticates trivially, as before.
- **DCR rows:** unchanged byte for byte. Assertion parameters are ignored, as they are today (L6).

`authenticate` checks, in this order, each with its own sub-reason:
1. `client_assertion_type` must equal exactly `urn:ietf:params:oauth:client-assertion-type:jwt-bearer` (`assertion_type_invalid`).
2. The assertion is ≤ 8,192 bytes of ASCII, a compact JWS of exactly three segments, so JWE and JSON serialisation are out. Its header decodes to an object (`assertion_malformed`).
3. The header checks (`assertion_header_invalid` / `assertion_alg_invalid`):
   - `crit` present → refused;
   - any of `jku`, `jwk`, `x5u`, `x5c` present → refused, not ignored, because a key-bearing header is never a key source here and its presence is either a confused client or an attack;
   - `alg` ∈ {`RS256`,`PS256`,`ES256`} and equal to the row's pinned alg when there is one (`none` and `HS*` are therefore impossible);
   - `typ` is not checked.
4. Key selection, from the row's `jwks_snapshot` only:
   - with a `kid` header: the usable key with that `kid` (`assertion_kid_unknown` when absent);
   - with no `kid` header: the single usable key compatible with `alg`, or `assertion_key_ambiguous` when there are zero or several;
   - the key's `kty`/`crv` must fit the alg (RS*/PS* → RSA, ES256 → EC P-256), and its `alg` member, when set, must equal the header's (`assertion_alg_invalid`).
5. `joserfc.jwt.decode(assertion, that_one_key, algorithms=[alg])`. Exactly one key and one algorithm, so the library makes no selection of its own. Failure is `assertion_signature_invalid`.
6. The claims (`assertion_claims_invalid`):
   - `iss` and `sub` are strings, each byte-equal to the **row's** `client_id_metadata_url`;
   - a `client_id` form parameter, when present, must equal it too (already enforced by `_caller_matches` before this point; re-checked here as defence in depth).
7. `aud` (`assertion_audience_invalid`) is either a string or an array of **exactly one** string, and that value must be in the endpoint's accepted set:
   - at `/token`: {issuer, `{issuer}/token`};
   - at `/revoke`: {issuer, `{issuer}/token`, `{issuer}/revoke`};
   - issuer is `settings.base_url`, the metadata's `issuer`, compared byte-exactly.

   The issuer is what rfc7523bis recommends. The token endpoint URL is what RFC 7523 §3 permits and OIDC Core §9 recommends, and what most `private_key_jwt` client libraries send, ChatGPT's included as far as is known. At `/revoke`, the token-endpoint form is accepted because clients commonly reuse one audience. Multi-valued arrays are refused, following rfc7523bis's audience-injection hardening.
8. Time, using constants `MAX_LIFETIME = 300` and `SKEW = 30`:
   - `exp` is required and is a number with `exp > now`, with **zero leeway** (`assertion_expired`);
   - `exp − now ≤ MAX_LIFETIME + SKEW` (`assertion_lifetime_exceeded`);
   - when `iat` is present: `iat ≤ now + SKEW` (`assertion_not_yet_valid`) and `exp − iat ≤ MAX_LIFETIME` (`assertion_lifetime_exceeded`);
   - when `nbf` is present: `nbf ≤ now + SKEW` (`assertion_not_yet_valid`);
   - non-numeric time claims are `assertion_claims_invalid`.
9. `jti` is a required string of 1–256 characters (`assertion_claims_invalid`), and it is then **consumed** in the replay cache. A seen value is `assertion_replayed`. A full cache is `assertion_replay_cache_full`.

**The replay cache** is in-process (the `--workers 1` contract, [rate limits](../../../docs/architecture/rate-limits.md)):
- It holds 16,384 entries, keyed by SHA-256(`iss` + "\n" + `jti`) and held until that `exp`. The key is per client URL, **not per row**, so an assertion spent at one user's row cannot be spent again at another user's row for the same client.
- Expired entries are purged before an insert.
- **When the cache is full of unexpired entries the assertion is refused, never evict-and-accept.** Evicting a live entry would make that `jti` replayable.
- Only an assertion that passed steps 1–8 reaches the cache. Unauthenticated garbage cannot fill it; filling it takes valid signatures. An entry lives at most 330 s, so filling the cache takes about 50 accepted assertions per second, against a `/token` limit of 10/min per address.

### D7. The JWKS refetch (kid miss or stale snapshot): the one narrow revision of "/token never fetches"

The endpoint may refetch the row's **pinned** `jwks_uri` on either of two **triggers**:
- **kid miss:** the assertion's `kid` is not in the row's snapshot;
- **stale:** the row's `jwks_fetched_at` is older than `JWKS_MAX_AGE_SECONDS = 86400` (24 h). That equals the JWKS cache ceiling, the maximum of `OAUTH_CIMD_CACHE_MAX_SECONDS`, so no snapshot is trusted for longer than any cache entry could be. The trigger fires whether or not the `kid` is present. It is what bounds how long a key the client has removed stays usable (owner decision, 2026-11-07).

Both triggers take one path, a **pre-phase** that runs before the handler's main session is opened, with no session, transaction or row lock held across the network hop:

1. **Gate.** All of these must hold, or the pre-phase does nothing:
   - `client_assertion` is present, parses (steps 2–3 of D6, without verifying the signature), and has an allowed `alg`. A `kid` is required for the kid-miss trigger only.
   - The request carries a credential: a `code`, a `refresh_token`, or at `/revoke` a `token`.
2. **Read and eligibility.** A short read-only session resolves the credential by hash and reads the client row. It reads that row's `token_endpoint_auth_method`, `client_id_metadata_url`, `jwks_uri`, snapshot kids and `jwks_fetched_at`, without `FOR UPDATE`, then **closes the session**.

   Egress is gated on **credential eligibility**: everything the main phase would require of the credential that can be checked without network I/O (Codex spec R1-1). The pre-phase continues only when all of these hold:
   - **For a code:**
     - the code is unspent and unexpired;
     - the caller's `client_id`, when present, matches the row (`_caller_matches`; never a `cimd_` id);
     - `redirect_uri` equals the code's;
     - the PKCE verifier is well-formed and its S256 matches the code's challenge, using the same constant-time comparison.
   - **For a refresh token:**
     - the row is unrevoked and unexpired;
     - it is inside the grant's absolute lifetime (`grant_deadline(grant_issued_at)`);
     - the caller's `client_id`, when present, matches.

     A revoked (rotated-away) token is ineligible, so **the #182 reuse branch never triggers a fetch**; the assertion is never verified, recorded or allowed to affect the reuse decision (the pre-phase may have parsed its unverified header and `iss`, nothing more).
   - **For `/revoke`:**
     - the token row is unrevoked and unexpired;
     - it is inside its grant's absolute lifetime, so revocation can still act on something live;
     - the caller's `client_id`, when present, matches the row.
   - **In every case:**
     - the row is `private_key_jwt`;
     - the unverified `iss` equals the row's URL;
     - at least one trigger holds.

   These pre-phase checks are **advisory, never decisive**. They run on an unlocked read and produce no response, record or state change. A request that fails them simply skips the fetch, and the main transaction re-runs every check under its locks in D8's order. All existing responses, including #325's spent-code concealment and #326's lifetime refusal, are therefore exactly as they would be without the pre-phase. The pre-phase does not consume the code, does not mark anything, and never consults the replay cache.
3. **Fetch.** First the process JWKS cache (D2) is consulted for that `jwks_uri`. A hit is used instead of fetching only if both of these hold:
   - for the kid-miss trigger, it contains the `kid`;
   - for the stale trigger, its `fetched_at` is within `JWKS_MAX_AGE_SECONDS` and newer than the row's `jwks_fetched_at`.

   Otherwise:
   - a per-`jwks_uri` refetch cooldown applies: `JWKS_REFETCH_COOLDOWN_SECONDS = 60` since the last refetch attempt, success or failure, in a 1,024-entry table with the oldest replaced;
   - an in-progress refetch of the same `jwks_uri` is joined;
   - the existing global in-flight cap of 8 applies;
   - the fetch is `fetch_url_guarded` with D2's parameters, and its result goes through `validate_jwks`;
   - **no per-address charge** (justified below).

   One refetch serves both triggers. A request that is both stale and kid-missing does not fetch twice.
4. **Write.** On a validated result (fetched, or a qualifying cache hit), a short transaction runs:

   `UPDATE oauth_clients SET jwks_snapshot = :s, jwks_fetched_at = :t WHERE client_id = :row AND jwks_uri = :uri_read AND jwks_fetched_at < :t`

   Here `:t` is the snapshot's `fetched_at`: the origin fetch time, carried through a cache hit. This is a compare-and-set on the URI, so an approval that re-pinned a different `jwks_uri` in between is never overwritten. The `jwks_fetched_at < :t` clause means a snapshot never moves backwards. A fetch also updates the process JWKS cache, so other rows of the same client do not refetch.
5. **Failure.** A network error, a fetch refusal, an invalid JWKS, a cooldown or a busy cap writes nothing. **`jwks_fetched_at` is not bumped**, so the next request after the cooldown tries again. The cooldown is armed by every attempt. What the main phase sees then depends on the trigger:
   - **stale only** (the `kid` is in the stored snapshot): the main phase verifies against the **stored snapshot**. An outage at the client's origin never breaks an exchange or a refresh.
   - **kid miss:** the `kid` is still not in the stored snapshot, so the main phase refuses `assertion_kid_unknown`.

   The refetch outcome appears only on the record (D10).

The main phase then re-reads the row under its usual locks and verifies against **that** snapshot (D6). The pre-phase never decides a response.

**A successful refetch that no longer contains the assertion's `kid` refuses.** The new snapshot replaced the old one in step 4, so the main phase cannot find the `kid` and refuses `assertion_kid_unknown`. Removing a compromised key from the JWKS is exactly the action the stale trigger exists to honour. This holds for both triggers. A kid-miss refetch whose result still lacks the `kid` also writes the fresh snapshot (outcome `kid_still_absent`).

Why no per-address charge: the refetch is reachable only by a caller presenting a credential that resolves to a `private_key_jwt` row whose URL equals the assertion's `iss`. It is bounded to one request per `jwks_uri` per minute process-wide, whoever asks. ChatGPT's token calls arrive from a shared egress pool, so an address budget would be either irrelevant or a way for one user's traffic to block another's refresh. In steady state, the stale trigger adds at most one fetch per `jwks_uri` per 24 h, plus one per minute while the origin is failing.

**Why it works while `OAUTH_CIMD_ENABLED=false`.** The switch stops the server *accepting new CIMD clients* and *fetching documents named by unauthenticated callers*. Existing CIMD grants keep refreshing under it (the #357 rule). The refetch fetches only a URL pinned on an already-approved row, for a credential holder. Disabling it would strand every `private_key_jwt` grant at the client's next key rotation, and would stop honouring key removal, which contradicts the existing promise. While disabled, the metadata stops advertising `private_key_jwt` (D9).

**The invariant, restated** (oauth-and-grants.md §CIMD replaces "`/token` never fetches"):
- `/token` and `/revoke` **never fetch the document**.
- They fetch the row's pinned `jwks_uri` **only** through the pre-phase, never while holding a lock, and only on one of two triggers: the assertion's `kid` is missing from the snapshot, or the snapshot is older than 24 h.
- A failed stale refetch falls back to the stored snapshot. A successful refetch is authoritative, including when it removes the assertion's key.
- A withdrawn or changed document still ends nothing. Revocation and the 90-day absolute lifetime do (L2 of #357).
- The `jwks_uri` changes only at an approval.

### D8. Where client authentication sits: the order of checks per endpoint

Assertion verification sits **exactly** where `_client_authenticated` sits today. Nothing moves:

| Endpoint / credential | Order (new step in **bold**) | Assertion failure answers | Revokes? |
| --- | --- | --- | --- |
| `/token` / `/revoke` pre-phase | **D7, only for an eligible credential (live code with matching redirect URI and PKCE; live, in-lifetime refresh or revoke token; matching `client_id` if given) and only on a kid miss or a snapshot older than 24 h; no lock; decides no response, re-checked below** | — | never |
| `/token` code, **live** | bootstrap lock → code by hash `FOR UPDATE` → `cimd_` id refused → caller `client_id` vs row → client row exists → **client authentication (D6)** → code expiry → `redirect_uri` → PKCE → ownerless / cross-user / scope → mint | `invalid_client` 401, the existing `authentication_failed` body byte for byte; `invalid_client.assertion_<sub>` | no |
| `/token` code, **spent** | same up to the client row → **client authentication (D6)** → `redirect_uri` → PKCE → replay branch (revokes) | the unknown-code `invalid_grant` 400 byte for byte; `invalid_grant.spent_code_assertion_<sub>` | no. Only a fully revalidated replay, with a valid fresh assertion, reaches the replay branch |
| `/token` refresh, **rotated away** | bootstrap lock → grant id by hash → grant lock → row `FOR UPDATE` → **#182 reuse branch, unchanged: the assertion is not verified, recorded or consumed** | — (constant reuse `invalid_grant`) | yes, as today, for every client (L7) |
| `/token` refresh, **live** | … → `cimd_` id refused → caller `client_id` vs row → client row exists → **client authentication (D6)** → #326 grant deadline → token expiry → rotation | `invalid_client` 401, the existing body; `invalid_client.assertion_<sub>` | no |
| `/revoke` | `cimd_` prefix (constant 200) → missing token (200) → **pre-phase** → token by hash → unknown token (200) → client row → unknown client (200) → `client_id` present: mismatch → constant 200 (`client_mismatch`), match → **client authentication (D6)**; `client_id` **absent**: `private_key_jwt` row → **client authentication (D6) directly against the credential-resolved row** (no identity comparison; the verified `iss`/`sub` bind it), any other row → constant 200 (`client_mismatch`, as today) → revoke family | `invalid_client` 401, the existing "right client, failed auth" case; `oauth_revoke_refused reason=assertion_<sub>` | no |

Consequences, stated so they are not "fixed" later:
- **A replayed, invalid or missing assertion never revokes anything.** Every assertion failure exits before any revocation write. The two revoking branches are untouched:
  - the spent-code replay branch is reached only *after* a successful assertion;
  - the #182 reuse branch never verifies, records or acts on the assertion.
- **Concealment is preserved.**
  - A spent code fails assertion checks with the unknown-code response.
  - A live credential gets the same `invalid_client` 401 for every assertion sub-reason.
  - `/revoke`'s `cimd_` and mismatch paths stay the constant 200.
  - `/revoke` without `client_id` was a hole for public clients and remains a no-op for them. A `private_key_jwt` caller may omit it, because the assertion, not the parameter, identifies and authenticates it (Codex spec R1-3). With a live `private_key_jwt` token and no `client_id`, the exact outcomes are:

    | Assertion | Response | Record |
    | --- | --- | --- |
    | none | `invalid_client` 401 | `oauth_revoke_refused reason=assertion_missing` |
    | malformed | `invalid_client` 401 | `reason=assertion_malformed` |
    | wrong `iss` or `sub` | `invalid_client` 401 | `reason=assertion_claims_invalid` |
    | valid | `{}` 200, family revoked | `oauth_grant_revoked` |

    Every other assertion failure is the same 401 with its own sub.
- **Cross-tenant.**
  - The assertion is always verified against the row the *credential* resolved, never a row looked up by URL.
  - Its `iss` must equal that row's URL.
  - Its `jti` is spent globally per client URL.

  So an assertion proves "this is ChatGPT", and the code with its PKCE verifier, or the refresh token, proves "this is user B's grant". No combination lets user A's credential reach B's grant.

### D9. AS metadata

While `OAUTH_CIMD_ENABLED` is true:
- `token_endpoint_auth_methods_supported` becomes `["none","client_secret_post","private_key_jwt"]`;
- `token_endpoint_auth_signing_alg_values_supported: ["RS256","PS256","ES256"]` is added.

While it is false, both are exactly as today. No revocation-endpoint auth fields are added, because the metadata carries none today. `/register` still refuses `private_key_jwt` (`TOKEN_ENDPOINT_AUTH_METHODS` unchanged), so the metadata's listing is true only for CIMD clients. RFC 8414 has no per-registration-path field to say that; this is recorded as L6.

### D10. Security events

No assertion, `kid`, `jti`, key, JWKS byte or unverified `iss` is ever logged. Records carry the row's `client_id` and `cimd_host` (of the row's URL) as today. New closed reason values:
- `oauth_authorize_refused`: `client_metadata.` + `jwks_uri_invalid`, `jwks_uri_cross_origin`, `jwks_inline_refused`, `signing_alg_unsupported`, `auth_method_downgrade`, and D2's `jwks_*` subs.
- `oauth_token_refused`: `invalid_client.<sub>` and `invalid_grant.spent_code_<sub>`, where `<sub>` is one of the assertion subs: `assertion_missing`, `secret_presented`, `assertion_unexpected`, `assertion_type_invalid`, `assertion_malformed`, `assertion_header_invalid`, `assertion_alg_invalid`, `assertion_kid_unknown`, `assertion_key_ambiguous`, `assertion_signature_invalid`, `assertion_claims_invalid`, `assertion_audience_invalid`, `assertion_expired`, `assertion_lifetime_exceeded`, `assertion_not_yet_valid`, `assertion_replayed`, `assertion_replay_cache_full`.
- `oauth_revoke_refused`: `reason` the same subs (`client_auth_failed` stays for the secret case).
- **One new event**, `oauth_client_jwks_refetched` (INFO; WARNING on `failed`). It is emitted once per *actual* refetch attempt in the pre-phase, never for a cooldown skip or a cache hit. Fields: `client_id` (the row), `cimd_host`, `trigger` (`kid_miss` | `stale`; `kid_miss` when both hold), `outcome` (`updated` | `kid_still_absent` | `failed`), `reason` (a `jwks_*` sub on `failed`), `route`. A stale refetch that fails is recorded `failed`; the request then proceeds on the stored snapshot.

### D11. No new settings

Every bound is a module constant:
- in `cimd.py`: `JWKS_MAX_KEYS = 16`, `RSA_MIN_BITS = 2048`, `RSA_MAX_BITS = 8192`, `JWKS_CACHE_ENTRIES = 256`;
- in `client_assertion.py`: `ASSERTION_MAX_BYTES = 8192`, `MAX_LIFETIME = 300`, `SKEW = 30`, `JTI_MAX_CHARS = 256`, `REPLAY_CACHE_ENTRIES = 16384`, `JWKS_REFETCH_COOLDOWN_SECONDS = 60`.

The existing `OAUTH_CIMD_*` settings govern the JWKS fetches unchanged. README and `.env.example` need no new entry.

### D12. Test fixture

The #357 fake document server (archived design D14) gains:
- a JWKS route;
- a test key factory: RSA 2048/1024/9000, EC P-256/P-384, OKP, and keys with private members;
- an assertion builder with every claim and header knob.

The pre-phase's no-lock property is tested against real Postgres. While the fake JWKS fetch is suspended, `pg_stat_activity` shows no `idle in transaction` backend and no lock held by the app's connection.

## Threats considered

- **Assertion replay.** `jti` is single-use per client URL until `exp`. A full cache refuses rather than evicting. Lifetime is bounded to 330 s. An assertion alone redeems nothing without the code and verifier, or the refresh token. A restart forgets the cache (L1).
- **Refetch amplification (kid miss or stale) by unauthenticated POSTs naming a known client_id, or by dead credentials.** The refetch is keyed on an **eligible credential**, not on a `client_id`: a live code with its redirect URI and PKCE verifier, or a live, in-lifetime refresh or revoke token. A spent or expired code, a failed PKCE verifier, and a revoked, rotated-away, expired or past-lifetime token never cause a fetch. A POST carrying only `client_id=https://chatgpt.com/...` and a random-`kid` assertion resolves no row and fetches nothing. With a credential, the limits are one fetch per `jwks_uri` per 60 s process-wide, joined flights, the global cap of 8, and slowapi on the endpoint. The stale trigger adds no new reach: it has the same precondition and the same cooldown.
- **Key substitution via `jku`/`jwk`/`x5u`/`x5c`.** These headers are refused outright. Keys come only from the row's snapshot, which came from the pinned same-origin `jwks_uri`.
- **Alg confusion.** The alg allow-list is RS256/PS256/ES256, intersected with the pin. Key type must match the alg family. The key's `alg` must match when present. One key and one alg are passed to the verifier. `none`, HS* and symmetric `oct` keys cannot be reached; an `oct` key's `k` refuses the whole set.
- **Downgrade to unauthenticated.**
  - The declared method is honoured.
  - The row's method is authoritative at `/token`.
  - A `private_key_jwt` row never becomes `none` (D5).
  - A `private_key_jwt` row without an assertion, or with a secret, is refused.
  - The hash pin catches a method flip between GET and POST.
  - The CHECK makes a `private_key_jwt` row without keys unrepresentable.
- **Cross-tenant.** Rows are per (URL, user). Verification uses the credential-resolved row. The replay cache is per URL. The pre-phase writes only the credential-resolved row, under a CAS on `jwks_uri`. See D8.
- **SSRF via `jwks_uri`.** Same URL rules as `client_id`, same origin, and the same guarded fetcher with deny list, pinning, no redirects and `trust_env=False`, both at `/authorize` and at the refetch. DNS is re-validated each time.
- **Oversized or hostile JWKS.** 64 KiB fetch cap; at most 16 keys; RSA ≤ 8,192 bits, which bounds verification cost; JSON recursion caught; normalised snapshot stored.
- **Clock skew.** Zero leeway on `exp`, 30 s on `iat`/`nbf`, bounded lifetime. A client more than 30 s fast is refused with a precise log reason.
- **Lock held across a fetch.** Structurally impossible: the fetch happens only in the pre-phase, before the main session exists. Pinned by the integration test in D12.
- **A key the client has removed.** The stale trigger refetches a snapshot older than 24 h before verification. A successful refetch is authoritative and refuses a removed `kid`. Only a failed refetch falls back, and it does not reset the 24 h clock (L4).
- **Breaking existing clients.**
  - DCR is byte-identical.
  - `none` CIMD rows change only when they present assertion parameters, which no known client does.
  - `none` hashes are byte-identical.
  - The metadata only gains values.
  - Existing families are untouched by the migration.

## Accepted limitations

- L1 The `jti` replay cache is per process and is lost on restart. An assertion captured before a restart can be replayed within its ≤ 330 s life, but only together with a valid code+verifier or refresh token.
- L2 A full replay cache refuses valid assertions until entries expire. This is fail-closed by design.
- L3 An assertion without `kid` is refused when the snapshot has more than one key compatible with its alg.
- L4 A key the client removes from its JWKS stays usable through the stored snapshot for up to about 24 h after the last successful fetch (`JWKS_MAX_AGE_SECONDS`), plus up to the 60 s refetch cooldown. It stays usable for longer while the client's origin is unreachable or serves an invalid JWKS, because a failed stale refetch falls back to the stored snapshot (owner decision, 2026-11-07).
- L5 Timing distinguishes a request that triggered a refetch.
- L6 DCR clients cannot use `private_key_jwt` although the metadata lists it, and DCR clients that send assertion parameters have them ignored.
- L7 A rotated-away refresh token of a `private_key_jwt` client revokes its family without client authentication (#182 unchanged). A holder of an old refresh token can end the grant but not use it. **Owner-accepted 2026-11-07.**
- L8 The `jwks_uri` is pinned per row and changes only at an approval. A withdrawn document ends nothing.
- L9 Only RS256, PS256 and ES256 are supported. No EdDSA, no `client_secret_jwt`, no mTLS.
- L10 A `private_key_jwt` → `none` change in a client's document is refused at approval until the user deletes that client in the panel. **Owner-accepted 2026-11-07.**
- L11 The kid-miss and stale refetches run while `OAUTH_CIMD_ENABLED=false`.
- L12 The token-endpoint URL is accepted as `aud` besides the issuer, which keeps the residual audience-injection exposure of clients that build `aud` from a malicious AS's metadata.
- L13 The metadata advertises no revocation-endpoint auth methods, as today.
- L14 A compromise of the client's origin is a compromise of the client: the JWKS is trusted on the same basis as the document.
- L15 At `/revoke`, a holder of a live `private_key_jwt` client's token who omits `client_id` and the assertion gets `invalid_client` 401 rather than the concealed 200. That tells them the token is live, which a bearer token's holder can learn at `/mcp` anyway.
- L16 The pre-phase's eligibility checks run on an unlocked read. A credential that is spent, rotated or revoked concurrently can still cause one bounded refetch, which the cooldown caps. The main transaction decides the response.

## Migration Plan

Deploy is merge → CI → image → Flux. The image's init container runs 038 before the app starts. 038 is additive with no backfill, so the previous image runs against the 038 schema: it writes only `none` rows, which satisfy the new CHECKs. The new image runs against 037 only during a rollout race in which no `private_key_jwt` row can be written. Rollback while no `private_key_jwt` row exists is a plain downgrade; with one, delete those clients first (the downgrade says so).

## Open Questions

None. Owner decisions 2026-11-07:
- the stale-snapshot refetch (D7, L4) is in scope;
- L7 (#182 unchanged) is accepted;
- L10 (no `private_key_jwt` → `none` downgrade) is accepted.

## Review record

- **Codex spec review, round 1 (2026-11-07):** FAIL, 2 MAJOR and 2 MINOR, all accepted and folded in (commit 9577a8a):
  - refetch egress gated on credential eligibility;
  - `jwks_fetched_at` = the origin fetch time through cache hits;
  - `/revoke` without `client_id` authenticates against the resolved row;
  - Slice S owns `test_schema_check.py`.
- **Round 2 (verification only):** PASS. One MINOR, accepted: the spec said "never reads the assertion" on the #182 path, where the advisory pre-phase does parse the unverified header and `iss`. The wording was narrowed to "never verified, recorded, or allowed to affect the reuse decision" (commit 0961386).
- **Declined:** none.
- **Adversarial Codex, round 1 (implementation):** FAIL, 1 MAJOR and 1 MINOR, both accepted and fixed:
  - MAJOR: concurrent first approvals could downgrade a `private_key_jwt` row to `none` (both read the absent row before either upserted). The conflict update is now guarded by the downgrade rule, and the newer-snapshot rule moved into it too; pinned by a two-session real-Postgres test.
  - MINOR: non-finite NumericDates (`NaN`, `±inf`, integers too large for a float) passed the type checks; each of `exp`, `iat`, `nbf` must now be finite, refused `assertion_claims_invalid`.
- **openspec-verifier, round 1:** one blocking finding, fixed: a `/token` client-authentication refusal lacked `cimd_host` when the caller omitted `client_id`; it now falls back to the row's metadata URL, as `/revoke` does.
- **Adversarial Codex, round 2 (verification only, 2026-11-07):** PASS. Fixed: the downgrade race, the finite NumericDates, and the verifier's `cimd_host` finding.
  - **Declined NIT:** an integer NumericDate too large for a float (such as `nbf = -10**400`) is refused as `assertion_claims_invalid` rather than judged temporally. This is stricter, only the holder of the client's key can produce it, and it fails all three triage questions.
  - No round 3: round 2 produced no new class of finding.
