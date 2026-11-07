## 0. Pre-implementation

- [x] 0.1 Base check. The branch contains `alembic/versions/037_oidc_account_links.py`, and no `038_*` exists on `main` or in any open PR. Coordination issue #420 is open and references this change.
- [x] 0.2 Codex spec review of this proposal: read-only, `-m gpt-daybreak-blue-latest`, effort high, framed as a defensive PASS/FAIL review. "Wrong" here means three things: a client authenticates that should not; a working client breaks (DCR, `none` CIMD, existing refresh families); or a deliberately identical refusal becomes distinguishable. Fold the findings in, and record the round and any declined findings in design.md, before any code.

## 1. Slicing for implementation (independent Opus subagents)

Wave 1 runs in parallel, in worktrees. The file scopes are disjoint:

- **Slice S (schema):** `alembic/versions/038_oauth_cimd_private_key_jwt.py`, `src/models/db.py`, `tests/integration/test_schema_check.py`, `tests/integration/test_oauth_cimd_migration_pg.py` and the new `tests/integration/test_oauth_cimd_pkjwt_migration_pg.py`.
- **Slice J (CIMD document + JWKS):** `src/oauth/cimd.py`, `tests/test_oauth_cimd.py` and a new `tests/test_oauth_cimd_jwks.py`.
- **Slice A (assertion module):** the new `src/oauth/client_assertion.py` and a new `tests/test_oauth_client_assertion.py`.

Wave 2 is one agent, after the Wave 1 merge:

- **Slice R (wiring):** `src/oauth/routes.py`, `src/services/security_events.py`, `src/logging_setup.py`, and the route and integration tests.

Test files that pin what this change alters (grep for `HEAD_REVISION`, `ck_oauth_clients_`, `token_endpoint_auth_methods_supported` and `client_id_metadata_document_supported`, done 2026-11-07):
- `tests/integration/test_schema_check.py` pins `HEAD_REVISION = "037"` and the canonical `ck_oauth_clients_auth_method_secret` definition. Owned by **S** (2.4).
- `tests/integration/test_oauth_cimd_migration_pg.py` asserts `ck_oauth_clients_cimd_shape` after `upgrade head`. Owned by **S**: re-check it against 038's replacement definition.
- `tests/test_oauth_cimd.py` pins the AS metadata. Its document and hash tests belong to **J**. Its metadata test belongs to **R** (5.1), which runs after J's merge, so there is no concurrent edit.
- `tests/test_native_oidc.py` pins an *upstream provider's* metadata, not ours, and is not touched.
- `tests/test_oidc_links.py:258` asserts 037's own `revision`/`down_revision`, which 038 leaves true. It is not touched.

S re-runs the grep before finishing and reports any further hit.

Wave 3 is the docs slice (§9). It may run in parallel with the gates once R is merged.

**The seams.** These signatures are fixed in this contract and stubbed by any slice that needs them before merge:
- `cimd.Projection` gains `auth_method: str`, `jwks_uri: str | None` and `signing_alg: str | None`.
- `cimd.resolve_jwks(projection, address) -> JwksSnapshot` and `cimd.validate_jwks(obj) -> JwksSnapshot`. A `JwksSnapshot` carries `.as_json() -> dict` (the normalised `{"keys": [...]}`) and `.kids -> frozenset[str]`.
- `cimd.fetch_jwks_guarded(jwks_uri) -> JwksSnapshot` is the raw guarded fetch and validation with no address charge, and is used by A's refetch.
- `cimd.jwks_cache_get(uri)` and `cimd.jwks_cache_put(uri, snapshot, directive)`.
- `client_assertion.AssertionRefusal(sub)` with the closed `SUBS` frozenset.
- `client_assertion.parse_unverified(form) -> Unverified | None`, returning `alg`, `kid` and `iss`, and never raising.
- `client_assertion.authenticate(form, *, metadata_url, snapshot, pinned_alg, endpoint, now) -> None`.
- `client_assertion.prephase(form, *, credential_kind, credential, route, client_ip) -> None` (async). It owns its short sessions and is the only place that refetches.
- `client_assertion.reset_state_for_tests()`.
- `JwksSnapshot.fetched_at: datetime` (UTC) is the origin fetch time. It is immutable, preserved by the cache, and is the only value ever written to `jwks_fetched_at`.

The model's new attribute names are `jwks_uri`, `token_endpoint_auth_signing_alg`, `jwks_snapshot` and `jwks_fetched_at`.

Post-merge, the orchestrator greps for production callers of every new export. Every one must be called from `routes.py` (design D7/D8), or the task is not done.

## 2. Schema (Slice S)

- [x] 2.1 Write `038_oauth_cimd_private_key_jwt.py` (`revision "038"`, `down_revision "037"`):
  - add four nullable, comment-marked columns (design D5);
  - replace `ck_oauth_clients_auth_method_secret` and `ck_oauth_clients_cimd_shape`, and add `ck_oauth_clients_private_key_jwt_shape`;
  - follow 036's devices throughout: search_path pin and assertion, lock and statement timeouts, reconcile-by-marker and probe-constraint re-run, `RESET` at the end.
  - No backfill.
  - The downgrade refuses while any `private_key_jwt` row exists, naming the count. Otherwise it restores 036's exact CHECK definitions.
  - The docstring says why the existing method column is reused.
- [x] 2.2 `src/models/db.py`: mirror the columns, with the comment markers byte-identical to the migration, and the three CHECKs. Update the `OAuthClient` docstring.
- [x] 2.3 `make test-schema` is clean. `make db-check` reports "No new upgrade operations detected." `tests/integration/test_nonsuperuser_migrations_pg.py` passes.
- [x] 2.4 `tests/integration/test_schema_check.py`:
  - change the head-chain assertion to `HEAD_REVISION = "038"` and its `037 → 038` chain;
  - change the canonical `ck_oauth_clients_auth_method_secret` expectation to 038's definition;
  - **keep** every existing `none` / `client_secret_post` enforcement case (`none` with a secret fails, `client_secret_post` without one fails);
  - add `private_key_jwt` cases: with a secret fails; on a DCR id fails; without a snapshot fails; well-formed on a `cimd_` row passes;
  - add an "absent constraint" case for `ck_oauth_clients_private_key_jwt_shape`, mirroring the #53 test.
- [x] 2.5 New `tests/integration/test_oauth_cimd_pkjwt_migration_pg.py` covers:
  - the upgrade on a database holding DCR and `none` CIMD rows (unchanged, all CHECKs valid);
  - a re-run (reconciles);
  - a foreign column or constraint of the same name (refuses, changes nothing);
  - the CHECK matrix: a `private_key_jwt` DCR row, a `private_key_jwt` row without a snapshot, a `none` row with a `jwks_uri`, and a bad alg each fail;
  - the downgrade refusing with a `private_key_jwt` row present and succeeding without one.

## 3. Document rules, JWKS, hash (Slice J)

- [x] 3.1 `validate_document`: admit `none` and `private_key_jwt`, and apply the `jwks_uri` rules (the URL rules plus same origin), the inline-`jwks` refusal and the signing-alg rule. Add the new closed reasons to `REASONS`.
- [x] 3.2 `Projection` gains the three fields. `authorization_hash` includes them for `private_key_jwt` only. A test pins the `none` hash to its pre-change value for a fixed projection.
- [x] 3.3 `validate_jwks`, implementing every D3 rule, with RSA bit length computed from `n` and the exponent bounded. The snapshot is normalised.
- [x] 3.4 `resolve_jwks`, implementing the D2 admission order: its own LRU of 256, shared cooldown table, second per-address charge, slot and global caps, join. Plus `fetch_jwks_guarded` and the cache accessors for Slice A.
- [x] 3.5 Unit tests on the #357 fake document server extended with a JWKS route:
  - the ChatGPT document;
  - cross-origin, port, query and inline `jwks`;
  - HS256, `none` and EdDSA algs;
  - each private-member key;
  - 1,024- and 9,000-bit RSA, P-384, OKP, `use: enc` and `key_ops` without `verify`;
  - a duplicate `kid`;
  - 17 keys;
  - a 64 KiB + 1 body;
  - redirect, 404, gzip and non-JSON responses;
  - the SSRF deny-list parameterisation for the `jwks_uri`;
  - a cached document plus an uncached JWKS charging one unit;
  - a joined JWKS flight charging nothing;
  - a cache hit returning the original `fetched_at`, never the hit time.

## 4. Assertion verification and replay cache (Slice A)

- [x] 4.1 `authenticate`: D6 steps 1–9, in order, each with its sub-reason. Verify with `joserfc.jwt.decode` against **one** imported key and **one** algorithm. Claims are validated by our own code, not the library's registry defaults.
- [x] 4.2 The replay cache: 16,384 entries, keyed by SHA-256 of `iss` and `jti`, held until `exp`, purged before insert, refusing when full and never evicting a live entry. It is recorded only after steps 1–8 pass.
- [x] 4.3 `parse_unverified`, bounded by the same size limits and never raising. `prephase` implements D7, with both triggers (kid miss, and `jwks_fetched_at` older than `JWKS_MAX_AGE_SECONDS = 86400`) sharing one refetch:
  - a credential-keyed read in a short session that is closed before I/O, then the D7 eligibility checks: code unspent and unexpired with redirect URI and PKCE verified; refresh or revoke token unrevoked, unexpired and in its lifetime; `client_id` matching when present. These are advisory only: no response, record or write;
  - the cache, then the cooldown (60 s per `jwks_uri`, 1,024 entries), then join, then the global cap, then `fetch_jwks_guarded`;
  - a CAS `UPDATE … WHERE client_id = :row AND jwks_uri = :uri AND jwks_fetched_at < :t` in its own short transaction;
  - on failure, nothing is written and `jwks_fetched_at` is not advanced. A stale-only failure falls back to the stored snapshot; a kid-miss failure leads to `assertion_kid_unknown`;
  - the JWKS cache records each entry's fetch time, and a stale-trigger cache hit counts only within 24 h;
  - one `oauth_client_jwks_refetched` emission per actual attempt;
  - it ignores `OAUTH_CIMD_ENABLED`.
- [x] 4.4 Unit tests:
  - every sub-reason;
  - `kid` absent with one key and with two compatible keys;
  - each forbidden header;
  - `alg` pinned versus unpinned;
  - a key `alg` mismatch;
  - RSA key with ES256 and EC key with RS256;
  - `aud` as issuer, as token URL, as revoke URL at `/token` (refused) and at `/revoke` (accepted), as a one-element array, as a two-element array (refused), and with a trailing-slash variant (refused);
  - `exp` = now (refused), exp − now = 331 (refused), `iat` 31 s ahead (refused), exp − iat = 301 (refused);
  - `jti` of 0 and 257 characters;
  - replay across two rows of the same URL;
  - the full cache refusing;
  - bad-signature assertions leaving the cache empty;
  - no log record containing the assertion, `jti`, `kid` or key material (capture every record and grep).

## 5. Wiring (Slice R)

- [x] 5.1 Metadata (D9): the two additions only while enabled. Update `tests/test_oauth_cimd.py`'s metadata test and add the disabled-state exact-list assertion.
- [x] 5.2 `/authorize` GET and POST:
  - call `resolve_jwks` after the document for `private_key_jwt`;
  - the POST's upsert writes and refreshes the method, `jwks_uri`, alg, snapshot and `jwks_fetched_at`;
  - the `auth_method_downgrade` refusal sits under the existing `FOR UPDATE` read;
  - the consent template is unchanged.
- [x] 5.3 `_client_authenticated` becomes the D6 dispatcher, with one definition for all three call sites. It returns a closed result whose sub becomes the reason. The DCR path is byte-identical; a test runs the existing DCR suites unchanged.
- [x] 5.4 `/token`, both grants:
  - `await client_assertion.prephase(...)` before `async with async_session()`;
  - the assertion is verified at the existing `_client_authenticated` position against the row read in the main transaction;
  - spent-code failures go through `_refuse_spent("assertion_<sub>")`;
  - live failures return the existing `invalid_client` 401 body;
  - the #182 reuse branch is untouched.
- [x] 5.5 `/revoke`:
  - the pre-phase after the `cimd_` and missing-token checks;
  - with `client_id` absent and a `private_key_jwt` row, skip the identity comparison and authenticate directly against the credential-resolved row (design D8, the exact response table);
  - assertion failure returns the existing 401 with `oauth_revoke_refused reason=assertion_<sub>`.
- [x] 5.6 Events: add the new reasons and the `oauth_client_jwks_refetched` event to the `security_events` catalogue and the `logging_setup` allow-list. The catalogue/call-site drift test stays green.
- [x] 5.7 Route tests (offline):
  - every D8 table row;
  - spent code + bad assertion is byte-identical (status, headers, body) to an unknown code;
  - live code + bad assertion is byte-identical to today's `authentication_failed` 401;
  - expired live code + bad assertion → 401, not "code expired";
  - rotated-away refresh + no assertion → family revoked;
  - live refresh + replayed assertion → 401 and nothing revoked;
  - `/revoke` with no `client_id`: `private_key_jwt` → revoked, `none` → constant 200;
  - a `none` CIMD row with an assertion → `assertion_unexpected`;
  - a `client_id`-only POST with a random `kid` → zero fetches;
  - 20 kid-miss requests in a minute → ≤ 1 fetch;
  - `OAUTH_CIMD_ENABLED=false` → existing `private_key_jwt` grant refreshes and both kid-miss and stale refetches still run;
  - stale snapshot (25 h) with the `kid` still served → one fetch before verification, success, `jwks_fetched_at` advanced;
  - stale snapshot, served JWKS no longer contains the signing `kid` → snapshot replaced, `assertion_kid_unknown`, nothing minted or revoked; at `/revoke`, 401 and nothing revoked;
  - stale snapshot with the origin failing (timeout, 500, invalid JWKS, private-key JWKS, cooldown) → refresh succeeds on the stored snapshot, and snapshot and `jwks_fetched_at` are unchanged;
  - after a failed stale refetch, a request more than 60 s later fetches again; within 60 s it does not;
  - stale and kid-miss together → one fetch, event `trigger=kid_miss`;
  - **zero-fetch eligibility** (fetch counter = 0, and the response byte-identical to the same request with the pre-phase patched out), each with an unknown-`kid` assertion whose `iss` is the row's URL:
    - a spent code with a failed PKCE verifier;
    - an expired code with a failed PKCE verifier;
    - a live code with a wrong `redirect_uri`;
    - a live code with a malformed verifier;
    - a revoked refresh token;
    - a rotated-away refresh token, which still revokes its family;
    - a refresh token past its grant's absolute lifetime;
    - a mismatched `client_id`;
    - at `/revoke`, a revoked or expired token;
  - an approval served from a JWKS cache entry 23 h 50 min old stores that entry's `fetched_at`. A token request 11 min later meets the stale trigger;
  - `/revoke` with no `client_id` on a `private_key_jwt` row: no assertion → 401 `assertion_missing`; malformed → 401 `assertion_malformed`; wrong `iss` → 401 `assertion_claims_invalid`; valid → 200 and the family revoked. On a `none` row with no `client_id` → the constant 200 `client_mismatch`;
  - a stale-trigger cache hit older than 24 h is not used, and a fresh one is used without a fetch and writes the entry's fetch time;
  - 20 stale-trigger requests within a minute → ≤ 1 fetch;
  - a test that makes `cimd.resolve` and `cimd._fetch` raise still passes for exchange, refresh and revoke (the document is never fetched).

## 6. Integration tests (real Postgres, `tests/integration/`)

- [x] 6.1 `test_oauth_cimd_pkjwt_pg.py`, the full ChatGPT flow on the fake origin:
  - GET → POST approve → row shape;
  - code exchange with an assertion;
  - refresh with an assertion;
  - key rotation through a kid-miss refetch;
  - a stale refetch on real Postgres: the row is aged to 25 h by direct UPDATE; a served key removal is refused; an origin outage refreshes on the stored snapshot, with `jwks_fetched_at` unchanged in the database;
  - revoke without `client_id`;
  - replayed spent code with a valid assertion revokes the family;
  - two users connect ChatGPT and each gets a separate row; user A's assertion `jti` is refused on B's refresh.
- [x] 6.2 No lock across the hop, for both triggers. While the fake JWKS fetch is suspended in the pre-phase, `pg_stat_activity` for the app's connections shows no `idle in transaction` and `pg_locks` shows no lock held by them.
- [x] 6.3 CAS. An approval re-pins `jwks_uri` during a suspended refetch, and the refetch's UPDATE affects 0 rows.
- [x] 6.4 Regression. The existing `test_oauth_cimd_pg.py`, `test_oauth_grants_pg.py` and `test_oauth_grant_lifetime_pg.py` pass unchanged.
- [x] 6.5 `make test-integration` is green (not concurrently with `make test-schema`).

## 7. Gates

- [x] 7.1 Full offline suite plus `make test-integration`, `make test-schema`, `make db-check` and `make audit`, run once on the merged result.
- [x] 7.2 The `openspec-verifier` subagent, which is not an author, until zero blocking gaps.
- [x] 7.3 The adversarial Codex pass, which is mandatory because this is the auth surface. Commit first, then run read-only against the requirements and the changed files, with the "wrong" framing from 0.2. Two rounds by default, triaged per the workflow, with declined findings recorded in design.md's accepted limitations.
- [x] 7.4 `openspec validate cimd-private-key-jwt --strict` is clean.

## 8. Archive (last commits of the feature branch)

- [x] 8.1 `openspec archive cimd-private-key-jwt -y`, then `openspec validate --strict` on the promoted specs.

## 9. Docs (same PR, before merge)

- [x] 9.1 `docs/architecture/oauth-and-grants.md` §CIMD:
  - replace "`/token` never fetches" (the Detection paragraph and the consent-pin paragraph) with the revised invariant, D7's "The invariant, restated";
  - add a `private_key_jwt` subsection covering document rules, the JWKS fetch and charge, validation, the snapshot, the assertion rules, the replay cache, the pre-phase and the downgrade rule;
  - extend "The order of checks per endpoint" with D8's table;
  - append L1–L14 as this change's accepted limitations, marking L7 and L10 as owner-accepted 2026-11-07.
- [x] 9.2 `CLAUDE.md`: in the CIMD key-decision bullet, `private_key_jwt` for CIMD only, the same-origin `jwks_uri`, the snapshot plus refetch (kid miss or older than 24 h; a failed stale refetch falls back to the stored snapshot) with no lock across the hop, and "`/token` never fetches **the document**". Add the 038 migration and the new module to the bullet.
- [x] 9.3 `docs/architecture/security-event-logging.md`: add the new reasons on the three events and the `oauth_client_jwks_refetched` row.
- [x] 9.4 `docs/architecture/schema-and-migrations.md`: a 038 entry.
- [x] 9.5 `README.md`: in the client-compatibility section, ChatGPT connects through CIMD with `private_key_jwt`. Run the grep check for any stale "CIMD public clients only" or "`none` only" wording in README, DEPLOYMENT.md and `.env.example`. No new setting, so `.env.example` is unchanged.
- [x] 9.6 `requirements.txt`: the `joserfc` / Authlib comment no longer says "Optional browser OIDC". It states that `joserfc` is also used for CIMD client assertions.

## 10. Deploy and live end-to-end

- [x] 10.1 Merge to `main` (PR body `Closes #420`, the gate tables) → CI → image → Flux. Watch the rollout, then confirm 038 ran and `alembic check` is clean in the running pod.
- [x] 10.2 Live: connect ChatGPT to the deployed server. Confirm `oauth_consent_granted registration_kind=cimd cimd_host=chatgpt.com` and `oauth_token_issued`, then call a read tool (`keyword_search` or `read_note`) from ChatGPT. Record which tools were called.
- [x] 10.3 Live regression: Claude (CIMD `none`) and Claude Code connect and call a tool, a DCR client still registers, and an existing grant refreshes. If a ChatGPT refresh occurs within the window, confirm `oauth_token_refreshed` for the ChatGPT row.
- [x] 10.4 Tag a release at the merge commit.

### Deploy record (2026-11-07)

- **2.3 / 10.1:** PR #421 was squash-merged as `c700640`. CI, `image` (`main-c700640-418`) and the Flux rollout succeeded. The running pod reports `alembic current` = `038 (head)` and `alembic check` reports "No new upgrade operations detected." `make test-schema` passed in the Slice S worktree (379 passed) and in CI (`schema-gate`).
- **10.2:**
  - The first ChatGPT attempt got through consent (`oauth_consent_granted registration_kind=cimd cimd_host=chatgpt.com outcome=created`). `/token` was then refused with `invalid_client.assertion_missing`, because the pre-existing connector had cached the pre-#420 AS metadata and authenticated as `none`.
  - After the connector was deleted and re-added in ChatGPT, it re-read the metadata. The flow ran `oauth_consent_granted outcome=unchanged`, then `oauth_token_issued reason=authorization_code` for the `private_key_jwt` row (an assertion verified), then `POST /mcp` 200s.
  - The owner confirmed it works from ChatGPT. Which tools ChatGPT called was not read from `usage_logs`, because a direct production DB query was declined in-session.
- **10.3:**
  - **Verified:** an existing claude.ai grant (a CIMD `none` client) refreshed on the new image (`oauth_token_refreshed`) and served a `get_recent` call (200).
  - **Not observed in the window:** a fresh DCR registration, a Claude Code connection, and a ChatGPT refresh. The unchanged DCR paths are covered by the CI suite.
- **10.4:** tagged `v1.1.1` at the merge commit of the follow-up docs PR. That commit is docs-only, so `registry-publish` reuses the `:main` image built from `c700640`.

- **§7 / §8:**
  - **7.1:** on the final tree, CI's `tests` job (offline + integration), `schema-gate` and `audit` passed on PR #421. `alembic check` ran clean against the live database after deploy.
  - **7.2:** verifier round 1 found one blocking gap, fixed in f364e60.
  - **7.3:** adversarial Codex, two rounds, PASS.
  - **7.4 and 8.1:** clean; archived as `2026-11-07-cimd-private-key-jwt`. See design.md's review record.
