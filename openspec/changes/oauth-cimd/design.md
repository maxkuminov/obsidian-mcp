## Context

### What the code does today (verified on c491326)

- `GET /.well-known/oauth-authorization-server` (`src/oauth/routes.py` `oauth_metadata`) already lists `["none", "client_secret_post"]` in `token_endpoint_auth_methods_supported`. It does not list `client_id_metadata_document_supported` or `authorization_response_iss_parameter_supported`.
- `/authorize` GET and POST resolve the client by `OAuthClient.client_id == client_id`. Redirect validation is `redirect_uri in client.redirect_uris`, an exact match. `_normalized_redirect_uri` admits only `https`, so a loopback redirect cannot be registered at all.
- `/token` resolves the client **from the code row** (`oauth_code.client_id`) or the refresh token row. A caller `client_id` that differs from the row's is a refusal that revokes nothing (#182, #325). `/revoke` requires the caller's `client_id` to be present and exactly equal to the token's.
- `oauth_clients.client_id`, `oauth_codes.client_id`, `oauth_tokens.client_id`, `usage_logs.actor_ref` and `transfer_tokens.actor_ref` are all `VARCHAR(64)`. The log allow-list bounds `client_id` and `client_id_submitted` to 64 characters (`src/logging_setup.py`).
- The consent page's CSP is `form-action 'self' https:` (`src/services/panel_csp.py` `build_policy`). Browsers apply `form-action` to every redirect hop of a form submission, so a 302 from `POST /authorize` to `http://localhost:PORT/callback` **is blocked today**. CIMD alone would not make Claude Code work.
- A client binds to its first authorizing user (`UPDATE … WHERE user_id IS NULL`), and a second user is refused (#68). One shared row per CIMD URL would make the *second* user who connects Claude in multi-user mode an `access_denied`.
- `fetch_url_guarded` (`src/services/transfer.py`) is the hardened outbound fetch. It canonicalises the host first, applies the explicit deny list and then `is_global`, pins the connection to the validated address with Host/SNI kept, uses a new client per hop with `trust_env=False`, `http2=False`, `follow_redirects=False` and manual redirects (`max_redirects`), takes one deadline over the whole fetch, and accepts identity encoding only.
- The live Claude Code document (fetched 2026-10-31, `Cache-Control: public, max-age=300`): `{"client_id":"https://claude.ai/oauth/claude-code-client-metadata","client_name":"Claude Code","client_uri":"https://claude.ai","redirect_uris":["http://localhost/callback","http://127.0.0.1/callback"],"grant_types":["authorization_code","refresh_token"],"response_types":["code"],"token_endpoint_auth_method":"none"}`.

### Sources (quoted)

- MCP 2026-07-28, client registration: AS "**SHOULD** fetch metadata documents when encountering URL-formatted client_ids", "**MUST** validate that the fetched document's `client_id` matches the URL exactly", "**SHOULD** cache metadata respecting HTTP cache headers", "**MUST** validate redirect URIs presented in an authorization request against those in the metadata document", "**MUST** validate the document structure is valid JSON and contains required fields". Client: "The `client_id` URL **MUST** use the "https" scheme and contain a path component"; the document "**MUST** include at least … `client_id`, `client_name`, `redirect_uris`". Advertised by `"client_id_metadata_document_supported": true`.
- MCP security considerations: "**SHOULD** display additional warnings for `localhost`-only redirect URIs", "**MUST** clearly display the redirect URI hostname during authorization"; "Authorization servers **MUST** validate exact redirect URIs against pre-registered values".
- CIMD draft-00: "`token_endpoint_auth_method` property MUST NOT include `client_secret_post`", "the `client_secret` and `client_secret_expires_at` properties MUST NOT be used", "If fetching the metadata document fails, the authorization server SHOULD abort the authorization request", "The authorization server MUST NOT cache error responses", "The recommended maximum response size … is 5 kilobytes", "SHOULD display the hostname of the `client_id` on the authorization interface", "SHOULD avoid fetching any URLs using private or loopback addresses".
- MCP changelog 2026-07-28: "Authorization servers **SHOULD** include the `iss` parameter in authorization responses per RFC 9207"; DCR "deprecated … in favor of Client ID Metadata Documents"; a "minimum twelve-month deprecation window".
- Anthropic: CIMD only with both metadata values; "Claude Code declares `http://localhost/callback` and `http://127.0.0.1/callback` … so match both with the port component ignored"; "display the redirect URI's hostname clearly"; endpoints get 10 s.

## Goals / Non-Goals

**Goals:** Claude, Claude Code and ChatGPT connect via CIMD with no `/register` call, and DCR keeps working unchanged for clients that send no URL. Every existing invariant keeps its meaning: grant families, #325 lineage, the #326 absolute lifetime, #194 marker semantics, #68 cross-user refusal, the panel's per-user listing and Delete. The outbound fetch an unauthenticated caller can trigger is bounded in rate, size, time and destination.

**Non-Goals:** `private_key_jwt` / `jwks_uri` client authentication (refused: auth method must be `none`). Fetching or displaying `logo_uri`. Domain trust policies or an allow-list of CIMD hosts (see Open Questions). Re-validating the document on refresh. Removing DCR. Audience-binding tokens beyond validating `resource` (one resource exists).

## Decisions

### D1. Detection and the canonical URL

A `client_id` that begins with the exact bytes `https://` is CIMD, and only CIMD. It is validated or refused and never looked up as a DCR id. DCR ids are `secrets.token_hex(16)` and cannot collide with that prefix. Under `OAUTH_CIMD_ENABLED=false` such a value is an unknown client (`invalid_client`), so a client falls back to DCR as the metadata no longer advertises CIMD.

The URL is refused (`client_metadata.url_invalid`) unless all of the following hold:
- it is ≤ 2,048 characters of printable ASCII with no whitespace;
- `urlsplit` succeeds;
- it contains no `?` and no `#` anywhere (no query, no fragment, not even an empty one);
- there is no userinfo and no `%`-encoded or literal `.`/`..` path segment;
- the path is non-empty and not `/`;
- the port is 443 or 8443, transfer's `SCHEME_PORTS`.

It is refused `client_metadata.url_not_canonical` unless `canonicalise(url, allow_http=False).url == url` **byte for byte**. That means a lower-case ASCII host with no explicit default port. Both checks run before any DNS. The canonical requirement is what makes "the document's `client_id` equals the URL" and "the URL we fetched is the URL the client named" the same statement. Without it, `https://Claude.AI/x` and `https://claude.ai/x` would be two clients sharing one document. Query strings are **prohibited** (Codex spec round 1, finding 2). Draft-00 only discourages them, but a query is where a URL can carry a secret, and the client identifier is logged and stored. No known client uses one.

*Alternative rejected:* normalising the caller's URL and comparing normalised forms. That gives two spellings one identity, and the draft requires exact equality.

### D2. The fetch: reuse `fetch_url_guarded`, never a second client

`src/oauth/cimd.py` calls `fetch_url_guarded(url, allow_http=False, max_bytes=65536, deadline=10.0, max_redirects=0)`. Any 3xx therefore raises, giving **no redirects**. Every SSRF rule, the address pin, `trust_env=False`, the identity-encoding rule and the declared-length check are inherited, not re-implemented. The body is read under the 64 KiB cap; a body that exceeds it is refused `too_large` without reading further. It must decode as UTF-8 JSON (any `Content-Type`; the draft does not mandate one) and parse to an object, otherwise `not_json`. Errors map to closed sub-reasons: `ssrf_refused`, `fetch_failed`, `fetch_timeout`, `http_status`, `too_large`, `content_encoded`, `not_json`. `IMPORT_ALLOW_HTTP` has no effect here.

**`FetchResult.cache_control`** (Codex round 1, finding 8). `FetchResult` today exposes `final_url` and `content_type` only, so the transfer module gains one narrowly bounded field: `cache_control: CacheDirective | None`, where `CacheDirective` holds `max_age: int | None` (a parsed non-negative `max-age`, clamped to 2³¹−1) and `no_store: bool` (true for `no-store` or `no-cache`). It is parsed inside `transfer.py` from the `Cache-Control` header only. The raw header string and every other response header are never exposed. `import_from_url` ignores the field, and every `FetchResult` construction site and fixture is updated.

64 KiB is the brief's figure. The draft recommends 5 KB, and real documents are about 300 bytes. 64 KiB costs nothing that matters and leaves room for a document with a long `redirect_uris` list.

### D3. Fetch admission: the fetch is an outbound hop an unauthenticated caller can drive

In the reference deployment `/authorize` sits behind `chain-oauth@file`, so a fetch needs a signed-in user. The app does not assume that, and in single-user mode the consent page has no session requirement. Admission is evaluated in this order. A step that admits charges nothing further up the list, and a refusal spends nothing.

1. **Cache hit** → no fetch, nothing charged.
2. **Join an existing flight:** if a fetch of the exact URL is already in progress, the request awaits that fetch's result. It is charged **nothing**: no per-address budget, no per-address in-flight slot, no global slot. Joining therefore always comes before any charge, so a request for a URL already being fetched cannot be refused by the caps.
3. **Cooldown:** a URL whose last fetch failed within 30 s is refused `client_metadata.fetch_cooldown` without fetching. This is the negative cache: a bounded table of 1,024 URL entries, oldest-first replacement. A replaced entry only lets one fetch through early, at most one per URL per replacement, which is the safe direction for an origin-protection control. It never stores or serves an error *as a document*, which is the draft's "MUST NOT cache error responses". A refusal during the cooldown still repeats a failure without re-asking, so the deviation is recorded (L4).
4. **Per-address budget**, `OAUTH_CIMD_FETCHES_PER_ADDRESS_PER_MINUTE` (10, 1–120). This is **the same algorithm as the `/mcp` failed-authentication budget** (`src/services/rate_limits.py`, `_slot_index` / `record_auth_failure` / `check_auth_failures`), applied to its own table with its own salt. It is not an LRU and nothing is ever evicted:
   - a **fixed-size table** of `CIMD_ADDRESS_TABLE_SIZE` = 4,096 slots (a module constant, overridable only by tests), allocated once;
   - **slot 0 is reserved** for a request with no resolvable client address, so such requests share one budget rather than being exempt;
   - every other address maps to `1 + blake2b(address, key=salt, digest_size=8) mod (size − 1)`, with a **per-process random 16-byte salt**;
   - each slot holds `window_start`, `count` and `inflight`. A **fixed 60 s window** starts at the first charge into an empty or expired slot, matching `record_auth_failure`'s "new window when `now − window_start ≥ window`";
   - admission refuses `client_metadata.fetch_budget_exhausted` when `count ≥ limit` inside the window, and a refused request does not increment;
   - an admitted fetch increments `count` when it starts.

   **Collisions and overflow:** two addresses that hash to one slot share one budget and one in-flight allowance. That only makes the control stricter, and the salt means nobody can choose whom to collide with. Memory is O(4,096) whatever the address churn. Because nothing is evicted, an address that spent its budget cannot get it back by flooding other addresses; it gets it back only when its slot's window expires. The cost is that a legitimate address colliding with a hostile one (probability about 1 in 4,095 per pair) shares the hostile address's budget for that window (L10).
5. **Per-address in flight:** at most **1** fetch in flight per slot. A second distinct URL from the same slot while one is in progress is refused `client_metadata.fetch_busy`.
6. **Global cap:** at most **8** fetches in flight process-wide. Beyond that, the request is refused `client_metadata.fetch_busy` immediately, without waiting.

So one address holds at most one of the eight global slots, and seven remain for everyone else. The acceptance case (Codex round 1) is: four hostile, slow, valid-looking CIMD URLs from one address. The first is admitted and the other three are refused per-address. A Claude authorization from another address naming an uncached valid URL then succeeds. Exhausting the global cap needs eight distinct addresses (or slots) each holding a 10 s fetch (L11). A URL that fails costs its origin at most one fetch per 30 s from all callers combined. No single fetch exceeds 10 s or 64 KiB.
### D4. Validation: the projection

The validated **projection** is the only thing the server keeps:
- `client_id` must be a string byte-equal to the URL (`client_id_mismatch`).
- `client_name` must be a non-empty string ≤ 255 after strip (`document_invalid`).
- `redirect_uris` must be a list of 1–20 distinct strings, each ≤ 2,048, each either `https` passing `_normalized_redirect_uri` (the #183 rule, stored normalised) or loopback `http` per D5 (`redirect_uris_invalid`). Two entries that normalise to one are refused, as DCR does.
- `token_endpoint_auth_method` absent or `none` (`auth_method_unsupported`). Absent is accepted because nothing else is supported and Claude's documents send `none`.
- Neither `client_secret` nor `client_secret_expires_at` present (`secret_present`).
- `grant_types` absent or containing `authorization_code`, and `response_types` absent or containing `code` (`grant_types_invalid`).
- `scope`: absent means `DEFAULT_CLIENT_SCOPE`; a string must pass `_validate_scope` and `has_vault_scope` (`scope_invalid`).

Unknown members are ignored. The stored projection is `{client_id, client_name, redirect_uris, scope}`.

The **authorization projection** is only what decides where a code may go and what it grants: `{client_id, redirect_uris: sorted list of the normalised URIs, scope: space-joined sorted scope tokens}`. Its SHA-256 over canonical JSON (sorted keys, no whitespace) is the **authorization hash** that D7 pins (Codex round 2, finding R2-2). `client_name` and every other self-described field are **outside** the hash, so a name-only edit or a redirect-order-only edit never blocks an authorization. A changed redirect set or scope does.

### D5. Redirect matching

One function, `redirect_uri_allowed(registered, requested, *, cimd)`, is used by the GET and the POST, so they cannot diverge. For DCR rows it is `requested in registered`, today's rule, byte for byte. For CIMD rows:
- An exact string match is accepted.
- Otherwise the match is loopback port-agnostic: the registered entry is `http` with host exactly `localhost`, `127.0.0.1` or `[::1]`, and the requested URI is `http` with the **same host literal**, no userinfo, no fragment, a numeric port 1–65535 or none, and a path and query byte-equal to the registered entry's.
- `localhost` never matches `127.0.0.1`, and an `https` entry never matches port-agnostically.

A CIMD redirect mismatch is answered with the constant `invalid_client` 400 of D12, not `invalid_redirect_uri`; the specific reason (`client_metadata.redirect_uri_mismatch`) is only in the log. DCR keeps its existing `invalid_redirect_uri` response. The code row stores the exact requested URI (with its port), so `/token`'s `oauth_code.redirect_uri != redirect_uri` comparison is unchanged and exact. This is the only relaxation, and it applies only to hosts that resolve to the user's own machine. It cannot become an open redirect to a remote host.

### D6. The synthetic row: one per (URL, user)

Migration 036 adds `oauth_clients.client_id_metadata_url VARCHAR(2048) NULL` and `CHECK ck_oauth_clients_cimd_shape`:

`(client_id LIKE 'cimd\_%') = (client_id_metadata_url IS NOT NULL) AND (client_id_metadata_url IS NULL OR (token_endpoint_auth_method = 'none' AND client_secret_hash IS NULL))`.

The row's `client_id` is `"cimd_" + HMAC-SHA256(key=K, msg=url + "\n" + owner).hexdigest()[:59]`, which is 64 characters and fits every existing `VARCHAR(64)`. Here `owner = str(user_id)`, or `-` with no user, and `K = HMAC-SHA256(SECRET_KEY, "oauth-cimd-client-id/v1")`, a per-deployment secret derived from `SECRET_KEY` (Codex round 2, finding R2-1). The id is therefore **not computable** by anyone without the secret, so it cannot be enumerated from a public URL and integer user ids. No foreign key, `actor_ref` or log bound changes. Rotating `SECRET_KEY` changes the id that new approvals derive. The next approval then creates a new row, and the old row keeps its grants until they age out and D9 sweeps it (L13).

**The internal id is never an input** (R2-1, normative):
- A submitted `client_id` beginning `cimd_` is refused **before any lookup**, with the endpoint's constant `invalid_client` and log reason `client_metadata.internal_id_submitted`. This applies at `GET` and `POST /authorize`, at `/token` (both grants) and at `/revoke`. The refusal depends only on the prefix, so it reveals nothing about which rows exist.
- Every lookup by a caller-submitted **non-URL** identifier adds `client_id_metadata_url IS NULL`, so a CIMD row is unreachable by any non-URL path even if the prefix check were removed.
- A CIMD row is reached only by computing its id from (the submitted, validated URL, the **authenticated** session user). "Prior approval" on the consent page is therefore that user's own row or nothing.
- `/token` resolves the client from the code or refresh-token row, as today, which is not a caller-submitted lookup.

User B submitting user A's id answers exactly like a nonexistent client and renders nothing.

*Why not `client_id = URL`:* that would widen five columns and two log bounds. Worse, it would make one row per URL shared across users, which turns #68's first-authorizer binding into a refusal for the second Claude user. The fix would mean unowned shared rows, and the panel's cascading Delete would then reach into other users' grants. That is exactly the defect #68 removed. Per-(URL, user) rows keep the invariant "a client row belongs to at most one user" with no change to any ownership predicate.

*Why not reuse columns only (no migration):* the URL must be stored to match `/token`'s and `/revoke`'s caller `client_id` (D8) and to show the panel what the row is. No existing column can hold it without overloading `client_name` (self-asserted text) or `redirect_uris`.

The row is written **only by an approve**, in the code's transaction:

`INSERT … ON CONFLICT (client_id) DO UPDATE SET client_name, redirect_uris, scope, last_used_at = now() RETURNING client_id`

with `user_id` = the session user (NULL in single-user mode). Because the id includes the owner, the conflict target can only be this user's own row. The cross-user branch and the `WHERE user_id IS NULL` claim are skipped for CIMD rows, since there is nothing to claim. A deny writes no row, and neither does a GET. This is what removes DCR exhaustion: rows ≤ users × URLs they approved. The upsert also does the #194 stamp in the same statement, so there is no "vanished" branch at approve: a row the sweep just removed is simply re-created.

`_stamp_client_use` at the code exchange and on refresh is unchanged. A CIMD row cannot be swept while a code or token row exists (D9), so its `False` branch remains the sweep-race guard it is today.

### D7. Pinning across consent, code and token

- **GET → POST:** the GET renders from the validated projection and puts its **authorization hash** (D4) into the signed `oauth_state` cookie payload (`{"s": server_state, "m": hash}`). A legacy plain-string payload is still accepted for DCR during the 10-minute rollout. The POST re-resolves the document from cache, or by fetch under D3, recomputes the authorization hash and refuses `client_metadata.metadata_changed` (`invalid_client`) when it differs. **The invariant is that the approval binds the authorization projection that was shown**: the redirect URIs and scope written are exactly those the user saw, or nothing is written. A refreshed `client_name` from the re-resolved document may be stored on the row; it is not shown on consent and is outside the hash. It is not a promise to detect every change at the origin. If the cached projection is still fresh at the POST, the POST compares against the cache, so an origin edit inside the freshness window is invisible until the entry expires. That is accepted by design (L12), and the approval still writes what was shown. Only a POST whose re-resolution is uncached or expired, and which fetches a *different* projection, is refused.
- **Code → token:** the code row already pins `client_id` (the row id), `redirect_uri` (exact) and `scope` (clamped). `/token` never consults the document, and auth is always `none` with PKCE. A document edited after consent therefore cannot change what the code redeems for or where tokens go. No per-code hash column is needed: everything the hash would protect is already a column on the code.
- **Refresh:** no fetch. A withdrawn or changed document does not end an existing grant. Revocation and the 90-day absolute lifetime (#326) do (L2).

### D8. Client-id matching at `/token` and `/revoke`

`client_id_matches(row, submitted)` returns true when `submitted == row.client_id` or (`row.client_id_metadata_url is not None` and `submitted == row.client_id_metadata_url`). It is the single definition used by the code exchange's mismatch check, the refresh mismatch check and `/revoke`'s "present and exactly equal". Every other line of those paths is unchanged: lookups by hash alone, spent-code fork, reuse detection and constant responses. The internal `cimd_…` id **never** matches as a caller value. It is refused by prefix before any lookup (D6, R2-1), so for a CIMD row only its URL matches. `client_id_matches` therefore compares `submitted == row.client_id` only for rows with `client_id_metadata_url IS NULL`.

### D9. The sweep rule for CIMD rows

`cleanup_expired_tokens` gains a second candidate set using the same protocol (`FOR UPDATE SKIP LOCKED` batch, in-lock re-read in a separate statement, delete what still qualifies). A row qualifies when:
- `client_id_metadata_url IS NOT NULL`, and
- no `oauth_codes` row and no `oauth_tokens` row of any state exists, and
- `last_used_at < now − OAUTH_CLIENT_UNUSED_EXPIRY_DAYS`.

The owner is ignored, because a CIMD row is a cache of a public document, holds no secret and is re-derived at the next consent. The #194 argument for never deleting claimed or pre-025 rows (an unrecoverable hand-configured secret) does not apply. `null` disables both branches. One INFO count line per pass covers both kinds. Tokens are retained 7 days past expiry, so a CIMD row lives at least 37 days past its last refresh-token mint and is **never** swept while any grant row references it. The #194 DCR rule is untouched, and it never selects a CIMD row, which always carries a marker.

### D10. Consent page

For a CIMD client the identity block is:
- **Application:** the `client_id` URL's host via `trust.to_ascii_host`, in its own labelled element.
- **Client ID:** the full URL, escaped and monospace.
- **Destination:** the redirect host, unchanged.
- **First approved:** the row's `created_at`, or "first time" when this user has no row.

The self-asserted `client_name` is **not rendered on the consent page**. A name chosen to read as "Claude" is the phishing vector, and the host is the identity the server can actually vouch for (it fetched the document from it). The request line ("… is requesting") names the host.

The standing notice for CIMD reads: "This application identified itself with a document published at `<host>`. This server fetched that document; it does not verify who operates `<host>`." When **every** registered redirect URI is loopback, a further warning says the code goes to a program on this computer and that any local program can claim that address. The known-destination badge rule is unchanged: exact host equality on the destination. CSP-clean throughout: classes in the nonce'd style block, no `style=`, no handlers.

`form-action` on the consent page becomes `'self' https: http://localhost:* http://127.0.0.1:*`. It is still a constant with no request-derived part. CSP host-source grammar cannot express an IPv6 literal, so a `[::1]` callback is matched by the server but its final hop may be blocked by the browser (L3). No known client declares `[::1]`.

### D11. AS metadata, `iss`, `resource`

- Metadata adds `client_id_metadata_document_supported: true` (only while enabled) and `authorization_response_iss_parameter_supported: true`. A test pins that `none` stays in `token_endpoint_auth_methods_supported`, because Claude needs both values.
- **`iss`:** every redirect `POST /authorize` issues (approve with `code`, deny with `error`) appends `iss=<metadata issuer>` through `_append_query`. The value is the exact `issuer` string the metadata serves. Errors answered as JSON (pre-redirect-validation) are not redirects and carry none.
- **`resource` (RFC 8707), log-only in this change (owner decision, 2026-10-31):** checked at `GET`/`POST /authorize` and on both `/token` grants, and the request **always proceeds unchanged**: nothing is refused and nothing is stored. Every request gets one low-cardinality classification `resource_class` (Codex round 1, finding 7):
  - `absent`: no `resource` parameter;
  - `accepted_1` = `{base}/mcp`, `accepted_2` = `{base}/mcp/`, `accepted_3` = `{base}`, `accepted_4` = `{base}/`, when every value present equals that one spelling;
  - `unexpected`: anything else, including several values that do not all equal one accepted spelling.

  Only for `unexpected`, `resource_host` is the first unexpected value's host via `trust.to_ascii_host`, or the constant `<unparseable>`. The full value, path or query is never recorded.

  Terminal paths and what they carry (finding R2-3). Every security record emitted while handling an `/authorize` or `/token` request carries `resource_class`, and `resource_host` when `unexpected`. That covers:
  - `/authorize`: `oauth_authorize_refused`, `oauth_consent_granted`, `oauth_consent_denied` and `oauth_cross_user_client_refused`;
  - `/token`: `oauth_token_issued`, `oauth_token_refreshed`, `oauth_token_refused`, `oauth_code_replay_detected`, `oauth_code_replay_revocation_failed`, `oauth_refresh_reuse_detected`, `oauth_refresh_reuse_revocation_failed` and `oauth_token_rotation_failed`.

  A request whose handler emits **none** of these emits exactly one new **classification-only** record, `oauth_resource_classified` (INFO). It carries `resource_class`, `route`, and `resource_host` only when `unexpected`, and nothing else; its subject is the trusted address. Such requests are:
  - a successful `GET /authorize` render, and its redirect to the login page in multi-user mode;
  - the client-vanished `invalid_client` at the POST;
  - an unhandled exception.

  This is guaranteed by construction, not by auditing each return. Each handler classifies once at entry into `request.state`. Emission helpers set a flag when they attach the class, and the handler's `finally` emits `oauth_resource_classified` iff the flag is unset.

  The `/token` slowapi 10/min refusal (a 429 answered before the handler) is classified too. The rate-limit exception handler, for `/token` only, reads the form under the existing request-body limit and emits the classification-only record. `/token` form bodies are covered because the value is classified in the handler, not read from access logs.

  **The 14-day gate is defined on exactly this evidence and nothing weaker:** the follow-up issue (L9, task 9.5) may switch to `invalid_target` once 14 consecutive days of every record carrying `resource_class` from `/authorize` and `/token` show no `unexpected`. That is every outcome and alternate event above, plus `oauth_resource_classified`. The later enforcement change **may rely on this evidence and on nothing weaker**. In particular it may not rely on a subset of events, on access logs or on sampling. Since there is one resource, every token is for it whatever the client sent.

### D12. Security events

- **No new event for the registration path** (Codex round 1, finding 3: one emission per outcome). `registration_kind` (`dcr`|`cimd`) is folded into the existing single-outcome events:
  - `oauth_client_registered` (DCR `/register`) carries `registration_kind: dcr`;
  - `oauth_consent_granted` carries `registration_kind` for every approval. For a CIMD approval it also carries `cimd_host`, `cimd_ref` and `outcome` (`created`|`refreshed`|`unchanged`, from the row upsert).

  There is still exactly one emission per DCR registration and one per consent approval. Live verification greps `oauth_consent_granted … registration_kind=cimd`.
- **No URL in any CIMD record** (finding 2). Wherever a submitted `client_id` begins `https://`, at `/authorize`, `/token` (both grants) and `/revoke`, the record carries **`cimd_host`** (the canonical host when the URL parses, else absent) and **`cimd_ref`** (the first 16 hex characters of SHA-256 over the full submitted string, a non-reversible correlation value) and **never `client_id_submitted`**. The OAuth helpers (`_authorize_refused`, `_token_refused`, `_revoke_noop`, the consent-denied emission) apply the rule in one place, `cimd.submitted_fields(value)`. A DCR `client_id_submitted` is unchanged. The resolved row's `client_id` (the `cimd_…` id) is a database value and is logged as today.
- CIMD refusals at `/authorize` emit the existing `oauth_authorize_refused` with `reason = client_metadata.<sub>`, where `<sub>` is one of `url_invalid`, `url_not_canonical`, `fetch_cooldown`, `fetch_budget_exhausted`, `fetch_busy`, `ssrf_refused`, `fetch_failed`, `fetch_timeout`, `http_status`, `too_large`, `content_encoded`, `not_json`, `client_id_mismatch`, `document_invalid`, `redirect_uris_invalid`, `auth_method_unsupported`, `secret_present`, `grant_types_invalid`, `scope_invalid`, `redirect_uri_mismatch`, `metadata_changed` or `internal_id_submitted`. At `/token` and `/revoke`, `internal_id_submitted` is recorded on `oauth_token_refused` (`invalid_client.internal_id_submitted`) and `oauth_revoke_refused`.
- `resource_class` and, for `unexpected`, `resource_host` are on every `/authorize` and `/token` record of D11. There is one new classification-only event, `oauth_resource_classified` (INFO: `resource_class`, `resource_host`, `route`), emitted only on a terminal path that emits no other record.
- New allow-listed fields: `registration_kind` (str 8), `cimd_host` (str 253), `cimd_ref` (str 16), `resource_class` (str 16) and `resource_host` (str 253). Provenance: `cimd_host`, `cimd_ref`, `resource_class` and `resource_host` are **server-derived** from a parsed value. They are neither a database read nor a raw `_submitted` value, so they are declared as their own class in the field-policy table. None is ever a suppression subject; the subject stays the address, or the user for a consent. SSRF error text, resolved addresses and document bytes are never logged.
- **The response to every CIMD refusal is `{"error": "invalid_client"}` 400**, constant, with no description that says which check failed. This includes a redirect mismatch (finding 4). A metadata publisher debugs against the log of an operator who can read it, not the response.
### D13. Settings

| Setting | Default | Range | Note |
| --- | --- | --- | --- |
| `OAUTH_CIMD_ENABLED` | `true` | bool | `false`: no metadata flag; URL client ids are unknown clients; existing CIMD grants keep refreshing |
| `OAUTH_CIMD_CACHE_MAX_SECONDS` | `86400` | 0–86400 | ceiling on `max-age`; 0 = never cache |
| `OAUTH_CIMD_FETCHES_PER_ADDRESS_PER_MINUTE` | `10` | 1–120 | the D3 per-address budget |

Fixed constants (in `cimd.py`, not settings): 10 s deadline, 64 KiB cap, 300 s default TTL, 30 s cooldown, cache 256 positive / 1,024 cooldown entries, a 4,096-slot salted address table, 1 fetch in flight per slot and 8 globally, 20 redirect URIs. Every setting goes in README and `.env.example` (public repo, no host paths).

### D14. Test fixture: a fake document server

A fixture serves documents through the real `fetch_url_guarded`, with an injected `resolver` mapping test hosts to a public-looking test address and a transport stub. It covers:
- valid documents, with `max-age`, `no-store`, `no-cache`, no header, and `max-age` above the ceiling;
- 301/302 redirects;
- 404 and 500 responses;
- a slow body past 10 s;
- a 64 KiB + 1 body, and an over-long `Content-Length`;
- `Content-Encoding: gzip`;
- non-JSON, a JSON array, and invalid UTF-8;
- `client_id` differing by case or by a trailing slash; a client identifier with `?` or `#` (refused before any fetch);
- four hostile slow URLs from one address while a Claude flow from another address authorizes an uncached valid URL (the first hostile one admitted, three refused `fetch_busy`, the Claude flow succeeds); a request joining an in-progress flight while the per-address and global caps are full (admitted, nothing charged); the per-address table at `size + 1` distinct addresses (a forced collision shares a budget, nothing is evicted, and a spent slot is not refilled by churn);
- missing `client_name` or `redirect_uris`; 21 redirect URIs; an `http` non-loopback URI; a fragment; userinfo;
- `client_secret_post`; a `client_secret` member; `grant_types` without `authorization_code`; an `offline_access`-only scope;
- a document that changes between GET and POST.

The SSRF test is parameterised over the deny list (loopback v4/v6, RFC 1918, ULA, link-local incl. `169.254.169.254`, CGNAT, NAT64, 6to4, Teredo, IPv4-mapped) for both a literal-IP URL and a name resolving to each. It also covers a name resolving to one public and one private address. Each case asserts that no connection was attempted.

## Threat model

| Threat | Control |
| --- | --- |
| SSRF via the document URL (metadata service, cluster services, Postgres) | D1 canonical https-only on 443/8443, before DNS; D2 transfer deny list on **every** resolved address, connection pinned (no rebinding window), no redirects, `trust_env=False`; D3 budgets; refusal text never echoed |
| Cache poisoning | Cache keyed by exact URL and filled only by this server's own fetch of that URL, after validation including `client_id == URL`; no shared or upstream cache; no redirects, so a document cannot be served from another URL; a refetch replaces the entry wholesale |
| Document changes between `/authorize` and `/token` | D7: GET→POST hash pin; the code pins the redirect URI and scope; `/token` never fetches |
| Open redirect via matching | D5 exact match; port-agnostic only for loopback http with the same host literal, path and query; the code stores the exact URI; the POST re-validates with the same function |
| Hostile `client_name` on consent | D10: the host is the identity; the name is not rendered on consent and is labelled self-described in the panel |
| Document at `evil.example` listing `https://claude.ai/...` as redirect | The code goes to claude.ai, which the attacker does not control. Consent shows "Application evil.example", so nothing is gained |
| Look-alike host (`claude.ai.evil.example`, IDN homograph) | The host is shown in ASCII/punycode (#183 rules); no badge for the application host |
| DCR exhaustion | CIMD rows only on approve, one per (URL, user); swept by D9; `/register` unchanged at 3/min |
| Fetch as a DoS amplifier against an origin | D3: 10/min per address slot, 1 in flight per slot, 8 globally, single-flight joined before any charge, 30 s cooldown on failure, 24 h cache |
| One caller monopolising the fetch pool to block other clients' CIMD | D3: one in-flight fetch per address slot; joining an existing flight costs nothing; seven of eight global slots stay available against one address |
| A secret in the client identifier reaching logs | D1 prohibits `?` and `#`; D12 logs only `cimd_host` and a 16-hex digest for every https client_id |
| A local program claiming a loopback callback | Inherent to RFC 8252. Warned on consent (D10); PKCE binds the exchange to the initiating client |
| Cross-user grant via a shared CIMD id | D6: per-(URL, user) rows; #68 rule untouched |
| Cross-tenant probe with another user's synthetic id | D6: ids are HMAC'd under a `SECRET_KEY`-derived key (not computable); a `cimd_` client_id is refused by prefix at every endpoint before any lookup; non-URL lookups exclude CIMD rows; prior approval resolves only from (URL, authenticated user) |
| A harmless metadata edit blocking authorization | D4/D7: the pin covers only client_id, the sorted redirect set and scope |

## Risks / Trade-offs

- **[A future `resource` refusal breaks a live connector]** → This change only classifies and logs (D11). The refusal ships separately, after 14 days with no `resource_class: unexpected`.
- **[`iss` confuses a client that validates it against a differently-spelled issuer]** → `iss` is the exact metadata `issuer`; RFC 9207 clients compare against that.
- **[The form-action relaxation]** → It applies only on the consent page, as constants. Where a code may go is still decided server-side (D5).
- **[In-process cache and budgets]** → Per worker, per the `--workers 1` contract. A restart empties the cache, which costs one fetch per URL.
- **[Bootstrap single→multi-user]** → A `-`-owner row claimed by the admin keeps its id. The admin's next consent derives a new id and a new row, and the old one keeps its grants until they age out and D9 sweeps it (L5).

## Migration Plan

036 adds a nullable column and a CHECK that every existing row satisfies, with no backfill and no rewrite. It is metadata-only plus one validation scan of `oauth_clients`, which is small. The downgrade refuses while any row has `client_id_metadata_url IS NOT NULL`, naming the count, and says to delete those clients in the panel first. That follows the 014 house style: never silently cascade grants away. Rollback without downgrade: `OAUTH_CIMD_ENABLED=false` and recreate. Clients fall back to DCR, and existing CIMD grants keep refreshing. Gates: `make test-schema`, `make db-check`.

## Owner decisions taken by default

1. CIMD on by default (`OAUTH_CIMD_ENABLED=true`). It is the point of the change and Claude prefers it.
2. One synthetic row per (URL, user) with a hashed `cimd_` id (D6), not `client_id = URL`.
3. Migration 036 adds `client_id_metadata_url` and a shape CHECK. Kind is derived from the column; there is no `registration_kind` column.
4. Rows are written only on approve, never on GET or deny.
5. `/token` and refresh never fetch. A withdrawn document does not end live grants (L2).
6. Consent shows the client host and the full URL. `client_name` is panel-only (Max, 2026-10-31).
7. Absent `token_endpoint_auth_method` is accepted as `none`.
8. Port-agnostic loopback matching applies to `localhost`, `127.0.0.1` and `[::1]`, same literal only, CIMD clients only. DCR still requires https.
9. Cache: `max-age` clamped to 24 h, 300 s when absent, `no-store`/`no-cache` uncached. 30 s failure cooldown.
10. Admission: 10 fetches/min per salted address slot (the failed-auth algorithm), 1 in flight per slot, 8 globally, single-flight joined first (Codex round 1).
11. `resource` is checked log-only against the four-spelling set and the request proceeds; the `invalid_target` refusal is a follow-up (Max, 2026-10-31). `iss` on every POST redirect.
12. Every CIMD refusal, including a redirect mismatch, answers a constant `invalid_client` 400; the reason goes only to the log.
16. CIMD client identifiers with a query or fragment are refused; CIMD records carry `cimd_host` and `cimd_ref`, never the URL (Codex round 1).
17. No registration-path event; `registration_kind` rides `oauth_client_registered` and `oauth_consent_granted` (Codex round 1).
18. Synthetic ids are HMAC-derived from `SECRET_KEY`; a submitted `cimd_` id is refused everywhere; CIMD rows are reachable only from (URL, authenticated user) (Codex round 2).
19. The consent pin covers the authorization projection only (client_id, sorted redirect URIs, scope) (Codex round 2).
20. `resource_class` is on every `/authorize` and `/token` record, plus one classification-only record where a terminal path emits nothing; the enforcement gate relies on exactly that evidence (Codex round 2).
13. CIMD sweep: no child rows + marker older than the #194 age, owner ignored, same kill switch.
14. 64 KiB cap (brief) over the draft's 5 KB recommendation.
15. No trust policy or host allow-list for CIMD URLs (Max, 2026-10-31): the fetch is hardened and `/register` is open anyway.

## Open Questions

None open. Max answered all three on 2026-10-31: no CIMD host allow-list (decision 15); `resource` ships log-only (D11, L9); `client_name` stays panel-only (decision 6).

## Accepted limitations

- **L1** CIMD URLs on ports other than 443/8443 are refused (transfer's port policy).
- **L2** Changing or withdrawing a document does not affect existing grants. They end by revocation or the 90-day absolute lifetime.
- **L3** A `[::1]` loopback callback is matched by the server but may be blocked by the browser's `form-action` (CSP cannot name an IPv6 literal).
- **L4** The 30 s failure cooldown is a bounded negative cache, a deliberate deviation from the draft's "MUST NOT cache error responses" for origin protection. No error is ever served as a document.
- **L5** After the single→multi-user bootstrap, a pre-bootstrap CIMD row and a new per-admin row can coexist for one URL.
- **L6** The cache, cooldown and budgets are per process and reset on restart.
- **L7** Timing distinguishes a cache hit from a fetch, and one refusal sub-reason from another. The response body does not.
- **L8** A local program can claim a loopback callback. This is inherent to RFC 8252. The page warns, and PKCE binds the exchange.
- **L9** The RFC 8707 `resource` parameter is checked but not enforced. A token requested for a foreign resource is still issued for this server, and the mismatch is only logged (`resource_class: unexpected` on the outcome event). Every token is for the one resource this server has, so the residual is a client misconfiguration going unrefused, not a token usable elsewhere. The `invalid_target` refusal is a follow-up issue (task 9.5), gated on 14 consecutive days of emitted `/authorize` and `/token` outcome records with no `resource_class: unexpected`. The outcome events pass the ordinary suppressor, so a burst from one address may be partly suppressed. Its first record per window is still emitted, which is all the gate needs to see that the class occurred.
- **L13** Rotating `SECRET_KEY` changes the derived synthetic ids. The next approval of each (URL, user) creates a new row, and the old row keeps its grants until they expire, after which D9 sweeps it. (Rotating `SECRET_KEY` already invalidates sessions and consent state.)
- **L10** Per-address budget collisions: two addresses sharing a salted slot (about 1 in 4,095 per pair, not choosable) share one budget and one in-flight allowance for that window.
- **L11** Eight distinct addresses (or slots), each holding a 10 s fetch, can fill the global cap and make other uncached CIMD authorizations answer `fetch_busy` for up to 10 s. Cached URLs, joined flights and DCR are unaffected. In the reference deployment `/authorize` is behind SSO.
- **L12** An origin edit inside the cached projection's freshness window is not seen by the consent POST. The approval writes the projection that was shown, which is the invariant.

## Codex spec review — round 1

Verdict FAIL: 7 MAJOR, 1 MINOR. Verbatim:

```json
{"verdict":"FAIL","findings":[{"severity":"MAJOR","file":"openspec/changes/oauth-cimd/specs/oauth-client-metadata-documents/spec.md","line":59,"claim":"The unconditional four-fetch global cap permits an unauthenticated caller to take down the only CIMD authorization path, while tasks.md also checks the global cap before single-flight despite the requirement saying an existing fetch must be shared.","failing_input":"One address opens four valid CIMD URLs whose public origins stream slowly for 10 seconds; during that interval Claude authorizes with an uncached valid URL and is refused invalid_client. Separately, with four fetches active, a request for a URL already among them may be refused before joining its single-flight.","fix":"Specify admission semantics that prevent one address from occupying every global slot, test four hostile slow fetches followed by a valid Claude flow, and require joining an existing per-URL flight before charging per-address or global admission."},{"severity":"MAJOR","file":"openspec/changes/oauth-cimd/specs/security-event-logging/spec.md","line":19,"claim":"The draft requires the submitted CIMD URL in client_id_submitted even though queries are allowed, contradicting the existing security-event rule that no structured field may contain a query string; /token and /revoke mismatch paths have the same unmentioned exposure.","failing_input":"client_id=https://a.example/x?token=secret is refused at /authorize, /token, or /revoke; the first 64 characters, including token=secret, can enter client_id_submitted.","fix":"Either prohibit queries in CIMD client identifiers or normatively define query-free logging for every authorize, token, and revoke event, using only the parsed host and a non-reversible correlation value if needed."},{"severity":"MAJOR","file":"openspec/changes/oauth-cimd/specs/security-event-logging/spec.md","line":4,"claim":"Adding oauth_client_registration_path beside oauth_client_registered and oauth_consent_granted violates the existing requirement and regression tests that each DCR registration and consent grant causes exactly one security-event emission.","failing_input":"A successful /register emits oauth_client_registered plus oauth_client_registration_path; a CIMD approval emits oauth_consent_granted plus oauth_client_registration_path.","fix":"Reconcile the modified security-event specification explicitly: fold registration_kind into the existing single outcome event or replace the old success event, and pin exactly one emission per DCR registration and consent approval."},{"severity":"MAJOR","file":"openspec/changes/oauth-cimd/specs/oauth-client-metadata-documents/spec.md","line":114,"claim":"Redirect-mismatch behavior contradicts the proposal's constant-refusal contract: this scenario requires invalid_redirect_uri, while proposal.md and the later requirement say every CIMD refusal is the same invalid_client response with detail only in logs.","failing_input":"The validated document lists http://127.0.0.1/callback but the authorization request uses http://localhost:3118/callback.","fix":"Choose one externally visible contract. To match the stated owner decision, require the constant invalid_client body for CIMD redirect mismatches and retain the specific mismatch only in the bounded log."},{"severity":"MAJOR","file":"openspec/changes/oauth-cimd/specs/oauth-client-metadata-documents/spec.md","line":133,"claim":"The document-change scenario is impossible under the cache requirement as written: an immediate approval is allowed to re-resolve from a fresh cached projection and therefore cannot observe an origin change.","failing_input":"GET fetches a valid document with Cache-Control: max-age=300; the origin changes redirect_uris immediately; POST approval within 300 seconds receives the cached projection and succeeds instead of satisfying the scenario's mandatory refusal.","fix":"Qualify the scenario to an uncached or expired re-resolution that returns a different projection, and state that the security invariant is approval of the projection shown—not detection of every origin mutation during freshness."},{"severity":"MAJOR","file":"openspec/changes/oauth-cimd/design.md","line":57,"claim":"The claimed bounded per-address design is neither fully specified nor accurately based on current code: the existing failed-auth control uses fixed salted slots, not a 512-entry exact LRU, and a bounded LRU cannot remember every evicted depleted address without defined overflow semantics.","failing_input":"An attacker spends the budget for address A, inserts 512 other addresses to evict A, then returns as A; a normal LRU refills it, while refusing all untracked addresses can deny a legitimate shared-egress client.","fix":"Specify the exact bounded algorithm and churn behavior, including overflow/collision semantics, and add a 513-address test. Do not describe the current failed-auth table as an LRU."},{"severity":"MAJOR","file":"openspec/changes/oauth-cimd/tasks.md","line":62,"claim":"The 14-day resource-enforcement gate cannot be verified from the telemetry specified: accepted values emit nothing, unexpected events retain only the host, and access logs cannot observe resource values posted in /token form bodies.","failing_input":"Clients send https://server.example/mcp, https://server.example/wrong, or no resource to /token; the retained records cannot distinguish these cases or prove that only the four accepted spellings occurred.","fix":"Define privacy-preserving, verifiable classification telemetry for absent, each accepted form, and unexpected values, or weaken the follow-up gate to exactly what the host-only events can prove."},{"severity":"MINOR","file":"openspec/changes/oauth-cimd/tasks.md","line":26,"claim":"The task breakdown omits the required transfer-client API change: FetchResult currently exposes final_url and content_type but no Cache-Control header, so task 4.4 cannot be implemented solely through the stated call.","failing_input":"A document responds Cache-Control: no-store or max-age=604800; the CIMD caller cannot observe that header through the current fetch_url_guarded result.","fix":"Add an explicit task and impact entry to expose a narrowly bounded Cache-Control value from fetch_url_guarded, update all FetchResult fixtures/call sites, and test that no unrestricted response headers propagate."}]}
```

What changed, per finding (decisions by the supervisor, 2026-10-31):

1. **Fetch admission DoS (MAJOR).** D3 rewritten. An existing per-URL flight is joined before any charge. There is at most 1 fetch in flight per address slot and a global cap of 8 (was 4). There is a scenario and a test for four hostile slow fetches from one address while a Claude flow succeeds (spec "Document fetches MUST be bounded…", tasks 4.2, 10.3). Residual L11.
2. **Query strings in logs (MAJOR).** `?` and `#` are prohibited in a CIMD client identifier (D1, spec). Every CIMD record at `/authorize`, `/token` and `/revoke` carries only `cimd_host` and `cimd_ref` (16 hex of SHA-256), never `client_id_submitted` (D12, security-event spec, task 9.3).
3. **Event count (MAJOR).** `oauth_client_registration_path` was removed. `registration_kind` is folded into `oauth_client_registered` (DCR) and `oauth_consent_granted` (every approval), with exactly one emission each (D12, security-event spec).
4. **Redirect mismatch contract (MAJOR).** A CIMD mismatch now answers the constant `invalid_client`; the reason `client_metadata.redirect_uri_mismatch` is log-only. The contradicting scenario is fixed (D5, spec).
5. **Document-change scenario (MAJOR).** The invariant is restated as "the approval binds the projection that was shown". The scenario is qualified to an uncached or expired re-resolution that returns a different projection. Fresh-cache acceptance is limitation L12 (D7, spec).
6. **Per-address budget (MAJOR).** It is now specified as the failed-auth algorithm exactly: a fixed 4,096-slot table, reserved slot 0, keyed blake2b with a per-process salt, a fixed 60 s window, collisions merge, nothing is evicted. All LRU wording is deleted. There is a test at slot-count + 1 addresses (D3, spec, tasks 4.2, 10.3). Residual L10.
7. **Resource telemetry (MAJOR).** `resource_class: absent | accepted_1..accepted_4 | unexpected` is on the one outcome event of every `/authorize` and `/token` request, with the host only for `unexpected`. `oauth_resource_unexpected` is removed. The 14-day gate is defined on these classes (D11, L9, spec, tasks 9.4/9.5).
8. **`FetchResult` Cache-Control (MINOR).** `FetchResult.cache_control` is a bounded `CacheDirective` (`max_age`, `no_store`) with no raw header. There is a task and an impact entry, plus fixtures and a no-header-propagation test (D2, task 4.6, proposal Impact).

## Codex spec review — round 2

Verdict FAIL: round-1 fixes 1–6 and 8 VERIFIED, 7 NOT_FIXED, and two new MAJOR findings. Verbatim:

```json
{"verdict":"FAIL","round1":[{"claim":"Fetch admission prevents one address monopolizing the global pool and joins an existing flight before admission checks.","status":"VERIFIED","note":"The fixed per-slot in-flight limit, eight-slot global cap, admission order, and hostile-flow tests close both failing inputs."},{"claim":"CIMD client identifiers cannot expose URL queries through structured OAuth records.","status":"VERIFIED","note":"Authorization rejects queries and fragments, while token and revoke mismatch records use only cimd_host and a bounded SHA-256 correlation reference."},{"claim":"Registration-path telemetry preserves exactly one event per registration and approval.","status":"VERIFIED","note":"The extra event was removed and registration_kind is carried on oauth_client_registered or oauth_consent_granted."},{"claim":"Every CIMD redirect mismatch uses the constant invalid_client response.","status":"VERIFIED","note":"The redirect requirement, scenarios, design, and task now agree on invalid_client with the detailed reason confined to logging."},{"claim":"Consent pinning distinguishes an expired or uncached re-resolution from an origin edit hidden by a fresh cache entry.","status":"VERIFIED","note":"Both cases now have explicit, mutually consistent scenarios and the stated invariant is approval of the projection shown."},{"claim":"The per-address fetch budget has bounded, non-evicting churn semantics.","status":"VERIFIED","note":"The proposal now specifies the current salted fixed-slot algorithm, collision behavior, reserved slot, expiry rule, and a size-plus-one test."},{"claim":"The 14-day resource-enforcement gate is supported by complete privacy-preserving request classification telemetry.","status":"NOT_FIXED","note":"design.md states that a successful GET /authorize emits nothing, and existing cross-user, replay, rotation-failure, and rate-limit outcomes use events outside the six events given resource_class. Thus actual unexpected requests can remain absent while the gate passes."},{"claim":"The guarded fetch exposes bounded Cache-Control information needed by the CIMD cache.","status":"VERIFIED","note":"FetchResult.cache_control, its bounded parsed shape, fixture/call-site updates, and parser/no-header-propagation tests are all explicit."}],"findings":[{"severity":"MAJOR","file":"openspec/changes/oauth-cimd/design.md","line":102,"claim":"The deterministic per-user synthetic id is accepted by the existing non-URL /authorize lookup, permitting an authenticated tenant to probe another tenant's CIMD row and render its consent metadata.","failing_input":"User B knows a public CIMD URL and enumerates integer user ids, computing cimd_ + sha256(url + newline + user_id)[:59]. GET /authorize with User A's existing synthetic id does not enter URL-form CIMD handling; the current registered-client lookup distinguishes the row from an unknown id and can render its URL, redirect host, and approval history before the POST-only cross-user check.","fix":"Normatively refuse internal cimd_ identifiers at both authorization methods, or exclude rows carrying client_id_metadata_url from non-URL authorization lookup. Resolve prior approval only from the submitted URL plus the authenticated user, and test that another tenant's computed id is indistinguishable from a nonexistent client before rendering."},{"severity":"MAJOR","file":"openspec/changes/oauth-cimd/specs/oauth-client-metadata-documents/spec.md","line":151,"claim":"Consent pinning hashes client_name even though that value is deliberately absent from the consent screen and irrelevant to code exchange, so a harmless metadata edit can falsely take down authorization.","failing_input":"A valid no-store document is rendered, then changes only client_name before approval while client_id, redirect_uris, and scope remain identical. POST re-fetches it, the full projection hash changes, and the required metadata_changed refusal blocks an otherwise unchanged authorization.","fix":"Bind a canonical authorization projection containing only client_id, normalized redirect semantics, and scope; keep self-described client_name outside the consent-security hash. Test that name-only and redirect-order-only changes proceed, while redirect or scope changes still refuse."},{"severity":"MAJOR","file":"openspec/changes/oauth-cimd/design.md","line":160,"claim":"The resource telemetry contract contradicts the claim that every request is classified on its outcome event, leaving the enforcement gate blind to real unexpected requests.","failing_input":"A client sends an unexpected resource on a successful GET /authorize and the user closes the page; no outcome event is emitted. Likewise, cross-user authorization refusals and successful code/refresh reuse detections terminate through events outside the six events given resource_class. Fourteen days can therefore contain unexpected requests while every retained classified record is accepted or absent.","fix":"Either add bounded classification to every actual terminal path, including successful authorization GETs and alternate outcome events, or explicitly narrow the gate to observed terminal flows and prevent a later enforcement change from treating that weaker evidence as proof about unobserved GET traffic."}]}
```

What changed, per finding (decisions by the supervisor, 2026-10-31):

1. **R2-1, cross-tenant probe via the synthetic id (MAJOR).** A submitted `client_id` with the `cimd_` prefix is refused before any lookup at both `/authorize` methods, at `/token` and at `/revoke`. The response is the endpoint's constant `invalid_client`, with log reason `client_metadata.internal_id_submitted`. Every non-URL lookup excludes rows with `client_id_metadata_url`. Prior approval resolves only from (URL, authenticated user). The id is HMAC-SHA256 under a `SECRET_KEY`-derived key, so it is not computable. There is a scenario and a test where user B submits user A's id and gets the constant refusal with nothing rendered. Rotation residual: L13 (D6, D8, D12, spec, tasks 3.2, 6.3, 10.15).
2. **R2-2, pin hash over self-described fields (MAJOR).** The pin is now the authorization projection only: client_id, the sorted normalised redirect URIs and the sorted scope. `client_name` is outside it. Tests cover a name-only change and a redirect-order-only change proceeding, and a redirect or scope change refused `metadata_changed` (D4, D7, spec, tasks 4.5, 10.6).
3. **Round-1 #7 NOT_FIXED, resource telemetry.** `resource_class` is now on every record of every terminal `/authorize` and `/token` path, including the cross-user refusal, code and refresh replay detection, their revocation failures and rotation failure. A terminal path with no event (successful GET render, login redirect, client-vanished, unhandled exception, the `/token` slowapi 429) emits one classification-only `oauth_resource_classified`. This is guaranteed by a per-request flag and a handler `finally`. The 14-day gate is defined on exactly this evidence, and the enforcement change may rely on nothing weaker (D11, D12, specs, tasks 8.2, 9.2, 9.4, 9.5, 10.11).
