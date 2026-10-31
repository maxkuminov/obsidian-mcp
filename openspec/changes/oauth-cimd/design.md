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
- there is no userinfo, no fragment and no `%`-encoded or literal `.`/`..` path segment;
- the path is non-empty and not `/`;
- the port is 443 or 8443, transfer's `SCHEME_PORTS`.

It is refused `client_metadata.url_not_canonical` unless `canonicalise(url, allow_http=False).url == url` **byte for byte**. That means a lower-case ASCII host with no explicit default port. Both checks run before any DNS. The canonical requirement is what makes "the document's `client_id` equals the URL" and "the URL we fetched is the URL the client named" the same statement. Without it, `https://Claude.AI/x` and `https://claude.ai/x` would be two clients sharing one document. Query strings are allowed (draft-00 only discourages them) and are compared byte-exactly.

*Alternative rejected:* normalising the caller's URL and comparing normalised forms. That gives two spellings one identity, and the draft requires exact equality.

### D2. The fetch: reuse `fetch_url_guarded`, never a second client

`src/oauth/cimd.py` calls `fetch_url_guarded(url, allow_http=False, max_bytes=65536, deadline=10.0, max_redirects=0)`. Any 3xx therefore raises, giving **no redirects**. Every SSRF rule, the address pin, `trust_env=False`, the identity-encoding rule and the declared-length check are inherited, not re-implemented. The body is read under the 64 KiB cap; a body that exceeds it is refused `too_large` without reading further. It must decode as UTF-8 JSON (any `Content-Type`; the draft does not mandate one) and parse to an object, otherwise `not_json`. Errors map to closed sub-reasons: `ssrf_refused`, `fetch_failed`, `fetch_timeout`, `http_status`, `too_large`, `content_encoded`, `not_json`. `IMPORT_ALLOW_HTTP` has no effect here.

64 KiB is the brief's figure. The draft recommends 5 KB, and real documents are about 300 bytes. 64 KiB costs nothing that matters and leaves room for a document with a long `redirect_uris` list.

### D3. Budgets: the fetch is an outbound hop an unauthenticated caller can drive

In the reference deployment `/authorize` sits behind `chain-oauth@file`, so a fetch needs a signed-in user. The app does not assume that, and in single-user mode the consent page has no session requirement. Four bounds apply, checked in this order before any fetch:

1. **Cache hit** → no fetch, no budget spent.
2. **Cooldown:** a URL whose last fetch failed within 30 s is refused `client_metadata.fetch_cooldown` without fetching. This is the negative cache: bounded LRU, 1,024 entries, keyed by exact URL. It never stores or serves an error *as a document*, which is the draft's "MUST NOT cache error responses". Strictly, a refusal during the cooldown still repeats a failure without re-asking, so the deviation is recorded here (L4).
3. **Per-address budget:** `OAUTH_CIMD_FETCHES_PER_ADDRESS_PER_MINUTE` (10, 1–120), an exact in-process window per trusted client address. It follows the failed-auth budget's shape and bounds (≤ 512 tracked addresses, LRU, an evicted entry refused rather than refilled). Over budget: `client_metadata.fetch_budget_exhausted`.
4. **Global cap:** at most 4 fetches in flight. Concurrent requests for one URL share one fetch (single-flight), and a fifth distinct fetch is refused `client_metadata.fetch_busy` immediately, without waiting.

So a hostile caller can drive at most 10 fetches/min from one address. A URL that fails costs its origin at most one fetch per 30 s from all callers combined. No single fetch exceeds 10 s or 64 KiB.

### D4. Validation: the projection

The validated **projection** is the only thing the server keeps:
- `client_id` must be a string byte-equal to the URL (`client_id_mismatch`).
- `client_name` must be a non-empty string ≤ 255 after strip (`document_invalid`).
- `redirect_uris` must be a list of 1–20 distinct strings, each ≤ 2,048, each either `https` passing `_normalized_redirect_uri` (the #183 rule, stored normalised) or loopback `http` per D5 (`redirect_uris_invalid`). Two entries that normalise to one are refused, as DCR does.
- `token_endpoint_auth_method` absent or `none` (`auth_method_unsupported`). Absent is accepted because nothing else is supported and Claude's documents send `none`.
- Neither `client_secret` nor `client_secret_expires_at` present (`secret_present`).
- `grant_types` absent or containing `authorization_code`, and `response_types` absent or containing `code` (`grant_types_invalid`).
- `scope`: absent means `DEFAULT_CLIENT_SCOPE`; a string must pass `_validate_scope` and `has_vault_scope` (`scope_invalid`).

Unknown members are ignored. The projection is `{client_id, client_name, redirect_uris, scope}`, and its SHA-256 over canonical JSON (sorted keys, no whitespace) is the **document hash** used in D7.

### D5. Redirect matching

One function, `redirect_uri_allowed(registered, requested, *, cimd)`, is used by the GET and the POST, so they cannot diverge. For DCR rows it is `requested in registered`, today's rule, byte for byte. For CIMD rows:
- An exact string match is accepted.
- Otherwise the match is loopback port-agnostic: the registered entry is `http` with host exactly `localhost`, `127.0.0.1` or `[::1]`, and the requested URI is `http` with the **same host literal**, no userinfo, no fragment, a numeric port 1–65535 or none, and a path and query byte-equal to the registered entry's.
- `localhost` never matches `127.0.0.1`, and an `https` entry never matches port-agnostically.

The code row stores the exact requested URI (with its port), so `/token`'s `oauth_code.redirect_uri != redirect_uri` comparison is unchanged and exact. This is the only relaxation, and it applies only to hosts that resolve to the user's own machine. It cannot become an open redirect to a remote host.

### D6. The synthetic row: one per (URL, user)

Migration 036 adds `oauth_clients.client_id_metadata_url VARCHAR(2048) NULL` and `CHECK ck_oauth_clients_cimd_shape`:

`(client_id LIKE 'cimd\_%') = (client_id_metadata_url IS NOT NULL) AND (client_id_metadata_url IS NULL OR (token_endpoint_auth_method = 'none' AND client_secret_hash IS NULL))`.

The row's `client_id` is `"cimd_" + sha256(url + "\n" + (str(user_id) if user_id else "-"))[:59]`, which is 64 characters and fits every existing `VARCHAR(64)`. No foreign key, `actor_ref` or log bound changes.

*Why not `client_id = URL`:* that would widen five columns and two log bounds. Worse, it would make one row per URL shared across users, which turns #68's first-authorizer binding into a refusal for the second Claude user. The fix would mean unowned shared rows, and the panel's cascading Delete would then reach into other users' grants. That is exactly the defect #68 removed. Per-(URL, user) rows keep the invariant "a client row belongs to at most one user" with no change to any ownership predicate.

*Why not reuse columns only (no migration):* the URL must be stored to match `/token`'s and `/revoke`'s caller `client_id` (D8) and to show the panel what the row is. No existing column can hold it without overloading `client_name` (self-asserted text) or `redirect_uris`.

The row is written **only by an approve**, in the code's transaction:

`INSERT … ON CONFLICT (client_id) DO UPDATE SET client_name, redirect_uris, scope, last_used_at = now() RETURNING client_id`

with `user_id` = the session user (NULL in single-user mode). Because the id includes the owner, the conflict target can only be this user's own row. The cross-user branch and the `WHERE user_id IS NULL` claim are skipped for CIMD rows, since there is nothing to claim. A deny writes no row, and neither does a GET. This is what removes DCR exhaustion: rows ≤ users × URLs they approved. The upsert also does the #194 stamp in the same statement, so there is no "vanished" branch at approve: a row the sweep just removed is simply re-created.

`_stamp_client_use` at the code exchange and on refresh is unchanged. A CIMD row cannot be swept while a code or token row exists (D9), so its `False` branch remains the sweep-race guard it is today.

### D7. Pinning across consent, code and token

- **GET → POST:** the GET renders from the validated projection and puts its document hash into the signed `oauth_state` cookie payload (`{"s": server_state, "m": hash}`). A legacy plain-string payload is still accepted for DCR during the 10-minute rollout. The POST re-resolves the document from cache, or by fetch under D3, recomputes the hash and refuses `client_metadata.metadata_changed` (`invalid_client`) when it differs. The user approved one set of redirect URIs and scope, and that set is what is written.
- **Code → token:** the code row already pins `client_id` (the row id), `redirect_uri` (exact) and `scope` (clamped). `/token` never consults the document, and auth is always `none` with PKCE. A document edited after consent therefore cannot change what the code redeems for or where tokens go. No per-code hash column is needed: everything the hash would protect is already a column on the code.
- **Refresh:** no fetch. A withdrawn or changed document does not end an existing grant. Revocation and the 90-day absolute lifetime (#326) do (L2).

### D8. Client-id matching at `/token` and `/revoke`

`client_id_matches(row, submitted)` returns true when `submitted == row.client_id` or (`row.client_id_metadata_url is not None` and `submitted == row.client_id_metadata_url`). It is the single definition used by the code exchange's mismatch check, the refresh mismatch check and `/revoke`'s "present and exactly equal". Every other line of those paths is unchanged: lookups by hash alone, spent-code fork, reuse detection and constant responses. A caller that sends the internal `cimd_…` id also matches, which is harmless because that id is not secret.

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
- **`resource` (RFC 8707):** accepted at `GET`/`POST /authorize` and on both `/token` grants. Every `resource` value present must equal one of `{base}/mcp`, `{base}/mcp/`, `{base}`, `{base}/`. Otherwise the request is refused `invalid_target`: a JSON 400 at `/authorize` (the redirect is not yet validated on GET, and the POST re-checks) and an RFC 6749 JSON 400 at `/token`. Absent is accepted. Nothing is stored, since there is one resource and every token is for it. The consent form carries `resource` through as a hidden field so the POST re-validates.

### D12. Security events

- New `oauth_client_registration_path` (INFO), fields `registration_kind` (`dcr`|`cimd`), `client_id` (row), `cimd_host`, `outcome` (`created`|`refreshed`|`unchanged`; DCR always `created`), `user_id`, `client_ip`. It is emitted after the commit: by `/register` beside `oauth_client_registered`, and by the CIMD approve. Live verification greps for this.
- CIMD refusals at `/authorize` emit the existing `oauth_authorize_refused` with `reason = client_metadata.<sub>`, where `<sub>` is one of `url_invalid`, `url_not_canonical`, `fetch_cooldown`, `fetch_budget_exhausted`, `fetch_busy`, `ssrf_refused`, `fetch_failed`, `fetch_timeout`, `http_status`, `too_large`, `content_encoded`, `not_json`, `client_id_mismatch`, `document_invalid`, `redirect_uris_invalid`, `auth_method_unsupported`, `secret_present`, `grant_types_invalid`, `scope_invalid` or `metadata_changed`. The field set adds `cimd_host`, the canonical host of a URL that passed D1, or absent. `invalid_redirect_uri` stays its own reason. `invalid_target` refusals use `reason = invalid_target` there and `invalid_target.resource_mismatch` in `oauth_token_refused`.
- New allow-listed fields: `registration_kind` (str, 8) and `cimd_host` (str, 253). Provenance: `cimd_host` is derived by the server from a validated URL. It is not a database read, so it is declared as its own class in the field-policy table, and it is never a suppression subject (the subject stays the address). The URL itself appears only as `client_id_submitted` (64-character bound) on refusals. SSRF error text, resolved addresses and document bytes are never logged.
- **The response to every CIMD refusal is `{"error": "invalid_client"}` 400**, constant, with no description that says which check failed. A metadata publisher debugs against the log of an operator who can read it, not the response.

### D13. Settings

| Setting | Default | Range | Note |
| --- | --- | --- | --- |
| `OAUTH_CIMD_ENABLED` | `true` | bool | `false`: no metadata flag; URL client ids are unknown clients; existing CIMD grants keep refreshing |
| `OAUTH_CIMD_CACHE_MAX_SECONDS` | `86400` | 0–86400 | ceiling on `max-age`; 0 = never cache |
| `OAUTH_CIMD_FETCHES_PER_ADDRESS_PER_MINUTE` | `10` | 1–120 | the D3 per-address budget |

Fixed constants (in `cimd.py`, not settings): 10 s deadline, 64 KiB cap, 300 s default TTL, 30 s cooldown, cache 256 positive / 1,024 negative entries, 4 in flight, 20 redirect URIs. Every setting goes in README and `.env.example` (public repo, no host paths).

### D14. Test fixture: a fake document server

A fixture serves documents through the real `fetch_url_guarded`, with an injected `resolver` mapping test hosts to a public-looking test address and a transport stub. It covers:
- valid documents, with `max-age`, `no-store`, `no-cache`, no header, and `max-age` above the ceiling;
- 301/302 redirects;
- 404 and 500 responses;
- a slow body past 10 s;
- a 64 KiB + 1 body, and an over-long `Content-Length`;
- `Content-Encoding: gzip`;
- non-JSON, a JSON array, and invalid UTF-8;
- `client_id` differing by case, by a trailing slash, or by a query;
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
| Fetch as a DoS amplifier against an origin | D3: 10/min per address, 4 in flight, single-flight, 30 s cooldown on failure, 24 h cache |
| A local program claiming a loopback callback | Inherent to RFC 8252. Warned on consent (D10); PKCE binds the exchange to the initiating client |
| Cross-user grant via a shared CIMD id | D6: per-(URL, user) rows; #68 rule untouched |

## Risks / Trade-offs

- **[RFC 8707 enforcement breaks a live DCR connector that sends a foreign `resource`]** → The accepted set covers every spelling of this server's resource. Before merge, task 9.4 checks the live access log for the `resource` values connectors actually send. If one is outside the set, stop and ask Max (Open Question 2).
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
6. Consent shows the client host and the full URL, and does not render `client_name`.
7. Absent `token_endpoint_auth_method` is accepted as `none`.
8. Port-agnostic loopback matching applies to `localhost`, `127.0.0.1` and `[::1]`, same literal only, CIMD clients only. DCR still requires https.
9. Cache: `max-age` clamped to 24 h, 300 s when absent, `no-store`/`no-cache` uncached. 30 s failure cooldown.
10. Budgets: 10 fetches/min/address, 4 in flight.
11. `resource` enforced with the four-spelling accepted set; `iss` on every POST redirect.
12. Every CIMD refusal answers a constant `invalid_client` 400; the reason goes only to the log.
13. CIMD sweep: no child rows + marker older than the #194 age, owner ignored, same kill switch.
14. 64 KiB cap (brief) over the draft's 5 KB recommendation.
15. No trust policy or host allow-list for CIMD URLs; any public https host may publish a document, as anyone may `/register` today.

## Open Questions (only Max can answer)

1. Should CIMD be restricted to an operator allow-list of `client_id` hosts (e.g. `claude.ai, chatgpt.com, openai.com`)? This proposal says no (decision 15). An allow-list would defeat the point for other clients while DCR stays open anyway.
2. `resource` enforcement: is `invalid_target` for a foreign `resource` right at deploy, or should the first release log only (shadow) and enforce later?
3. Should `client_name` appear on consent at all, as a subordinate "calls itself …" line, or stay panel-only as proposed?

## Accepted limitations

- **L1** CIMD URLs on ports other than 443/8443 are refused (transfer's port policy).
- **L2** Changing or withdrawing a document does not affect existing grants. They end by revocation or the 90-day absolute lifetime.
- **L3** A `[::1]` loopback callback is matched by the server but may be blocked by the browser's `form-action` (CSP cannot name an IPv6 literal).
- **L4** The 30 s failure cooldown is a bounded negative cache, a deliberate deviation from the draft's "MUST NOT cache error responses" for origin protection. No error is ever served as a document.
- **L5** After the single→multi-user bootstrap, a pre-bootstrap CIMD row and a new per-admin row can coexist for one URL.
- **L6** The cache, cooldown and budgets are per process and reset on restart.
- **L7** Timing distinguishes a cache hit from a fetch, and one refusal sub-reason from another. The response body does not.
- **L8** A local program can claim a loopback callback. This is inherent to RFC 8252. The page warns, and PKCE binds the exchange.
