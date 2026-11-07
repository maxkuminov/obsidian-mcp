Refs #420 (the feature PR closes it with `Closes #420`).

## Why

ChatGPT's connector now identifies itself with a Client ID Metadata Document, `client_id=https://chatgpt.com/oauth/client.json`. That document declares `token_endpoint_auth_method: private_key_jwt`, `token_endpoint_auth_signing_alg: RS256` and a same-origin `jwks_uri`. #357 admitted only public (`none`) CIMD clients, so `cimd.validate_document` refuses the document (`client_metadata.auth_method_unsupported`), and every ChatGPT connection fails at `/authorize` with `invalid_client`. The document also lists `none` in `token_endpoint_auth_methods_supported`, but the declared method is the client's choice. Downgrading it would accept, unauthenticated, a client that asked to be authenticated by key.

## What Changes

- **Documents.** CIMD documents may declare `private_key_jwt` (RFC 7523 §2.2 / OIDC Core §9). Such a document must carry a `jwks_uri` that passes the client-id URL rules and is same origin (scheme, host, port) as the `client_id` URL. It must not carry an inline `jwks`. Its optional `token_endpoint_auth_signing_alg` must be one of RS256/PS256/ES256 and then pins the algorithm. Any other method is still refused. The declared method is honoured, never downgraded.
- **The JWKS at `/authorize`.** After the document validates, its JWKS is fetched with the same guarded fetcher and admission chain as the document. It has its own bounded cache and costs **a second per-address charge** on a miss. The JWKS is validated:
  - at most 16 keys;
  - only public RSA (≥ 2,048, ≤ 8,192 bits) and EC P-256 signing keys are used;
  - any private-key member refuses the set;
  - `use: enc` keys are ignored;
  - at least one usable key is required.
- **Pinning.** The consent hash for a `private_key_jwt` document also covers the method, `jwks_uri` and signing alg, but not the keys. A `none` document's hash stays byte-identical. At approve, the synthetic row stores the method in the existing `token_endpoint_auth_method` column, together with the `jwks_uri`, the alg and a normalised JWKS snapshot. A later approval refreshes them. A `private_key_jwt` row is never downgraded to `none`.
- **Client authentication at `/token` (both grants) and `/revoke`.** For a `private_key_jwt` row, the client authenticates with a `client_assertion` verified against the row's stored snapshot:
  - the `client_assertion_type` must be exact;
  - the alg must be in the allow-list and equal the pin;
  - `crit`, `jku`, `jwk`, `x5u` and `x5c` are refused;
  - `iss` and `sub` must equal the row's URL;
  - `aud` must be the issuer or the endpoint URL, as a single value;
  - `exp` is required with zero leeway, lifetime is bounded to 300 s, and `iat`/`nbf` get 30 s of skew;
  - `jti` is single-use until `exp` in a bounded in-process cache that refuses when full.

  The request is refused `invalid_client` when the assertion is absent, when a `client_secret` is presented, or when a `none` CIMD row presents an assertion. Authentication sits exactly where `_client_authenticated` sits today. A spent code that fails it answers the unknown-code response. A failed or replayed assertion never revokes anything. #182's reuse branch is unchanged (owner-accepted L7). A `private_key_jwt` row is never downgraded to `none` at approval (owner-accepted L10). At `/revoke` a `private_key_jwt` client may omit `client_id`.
- **JWKS refetch: a narrow revision of "/token never fetches".** A pre-phase may refetch the row's pinned `jwks_uri` before the handler opens its main session, on either of two triggers:
  - the assertion's `kid` is absent from the snapshot;
  - the snapshot is older than 24 h (a constant equal to the JWKS cache ceiling).

  It runs only for a request that carries a credential resolving to that `private_key_jwt` row whose URL equals the assertion's `iss`. It fetches at most once per `jwks_uri` per 60 s process-wide, under the in-flight caps, and **with no session, transaction or row lock held across the hop**. It decides no response.

  A successful refetch replaces the snapshot, so an assertion whose `kid` the client has removed is refused. When a stale refetch fails, the stored snapshot is used and `jwks_fetched_at` is not bumped, so an outage at the client's origin never breaks refreshes.

  The document itself is still never fetched at `/token` or `/revoke`, and a withdrawn document still ends nothing. The refetch also works while `OAUTH_CIMD_ENABLED=false`.
- **AS metadata** (only while `OAUTH_CIMD_ENABLED`): `private_key_jwt` is added to `token_endpoint_auth_methods_supported`, plus `token_endpoint_auth_signing_alg_values_supported: ["RS256","PS256","ES256"]`. DCR still cannot register `private_key_jwt`.
- **Events.** New closed reason values, and one new `oauth_client_jwks_refetched` event. No assertion, `jti`, key or JWKS byte is ever logged.
- **No new settings.** All bounds are constants.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `oauth-client-metadata-documents`:
  - MODIFIED: the metadata requirement, document validation, the consent pin (including the revised "token endpoint never fetches"), the synthetic row, and the `/token`/`/revoke` client-identifier requirement.
  - ADDED: JWKS resolution and validation at `/authorize`, assertion authentication, its placement in the check order, the replay cache, and the JWKS refetch (kid miss or stale snapshot).
- `security-event-logging`: ADDED: the new refusal reasons, and the `oauth_client_jwks_refetched` event with its fields.

`oauth-authorization-integrity` is deliberately not modified. Its spent-code requirement already says "the client exists and authenticates by its registered method", and its refresh-reuse requirement already orders reuse before client checks. This change satisfies both as written.

## Impact

- **Code:**
  - `src/oauth/cimd.py`: document rules, `resolve_jwks`, `validate_jwks`, the JWKS cache, the projection and hash;
  - new `src/oauth/client_assertion.py`: verification, replay cache, pre-phase refetch;
  - `src/oauth/routes.py`: metadata, the `/authorize` GET/POST wiring and upsert, the `/token` and `/revoke` pre-phase, the `_client_authenticated` dispatcher;
  - `src/models/db.py`;
  - `src/services/security_events.py` and `src/logging_setup.py`, for the catalogue.
- **Schema:** migration `038_oauth_cimd_private_key_jwt.py` (`down_revision "037"`). It adds four nullable columns and replaces or adds CHECKs, with no backfill. The downgrade refuses while `private_key_jwt` rows exist.
- **Dependencies:** none new. `joserfc` is already a hard dependency, used by native OIDC.
- **Gates:**
  - auth surface, so the adversarial Codex pass is mandatory;
  - `make test-schema` and `make db-check`;
  - `make test-integration`;
  - no browser pass, since no template changes.
- **Live:** ChatGPT connects to the deployed server and calls a read tool. Claude (CIMD `none`), Claude Code and a DCR client still connect and refresh.
