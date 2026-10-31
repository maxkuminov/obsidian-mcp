## 0. Pre-implementation

- [ ] 0.1 Base check: `git log --oneline -1` on the branch base shows the proposal commit on top of c491326 (or a later main that has `alembic/versions/035_keyword_tags.py`). Confirm no other change has taken migration 036.
- [ ] 0.2 Codex spec review of this proposal (read-only, `gpt-daybreak-blue-latest`, high). Fold the findings into proposal/design/specs before any code, and record the round in design.md.

## 1. Settings and schema

- [ ] 1.1 `src/config.py`: `oauth_cimd_enabled` (bool, true), `oauth_cimd_cache_max_seconds` (int, 86400, 0–86400), `oauth_cimd_fetches_per_address_per_minute` (int, 10, 1–120). Out-of-range values refuse boot.
- [ ] 1.2 `src/models/db.py`: `OAuthClient.client_id_metadata_url: String(2048) | None` and `CheckConstraint` `ck_oauth_clients_cimd_shape` (design D6).
- [ ] 1.3 `alembic/versions/036_oauth_cimd.py` (`revision "036"`, `down_revision "035"`): add the column and the CHECK. The downgrade refuses while any CIMD row exists, naming the count. Module docstring states why the column exists and why `client_id` is not widened.
- [ ] 1.4 `make test-schema` and `make db-check` clean ("No new upgrade operations detected"). `test_nonsuperuser_migrations_pg.py` still passes.

## 2. AS metadata

- [ ] 2.1 `oauth_metadata`: add `client_id_metadata_document_supported: true` only while enabled, and `authorization_response_iss_parameter_supported: true`. Keep `none` and `registration_endpoint`.
- [ ] 2.2 Test: both of Claude's required values are present when enabled; the flag is absent when disabled; `issuer` equals the `iss` value D11 appends.

## 3. URL detection and canonical form (`src/oauth/cimd.py`)

- [ ] 3.1 `is_cimd_client_id(value)` (exact `https://` prefix) and `validate_client_id_url(value)`, implementing every D1 rule, with `canonicalise(...).url == value` byte-equality, before any DNS.
- [ ] 3.2 `cimd_row_id(url, user_id)`, which returns `"cimd_" + sha256(url + "\n" + owner)[:59]`, with `owner = str(user_id)` or `-`.
- [ ] 3.3 `/authorize` GET and POST branch on detection after the session, response-type, PKCE and scope checks. A disabled CIMD feature means an unknown client.

## 4. Hardened fetch, budgets, validation, cache (`src/oauth/cimd.py`)

- [ ] 4.1 Fetch via `fetch_url_guarded(url, allow_http=False, max_bytes=65536, deadline=10.0, max_redirects=0)`. Read the body under the cap and require a UTF-8 JSON object. Map every exception to the closed D2 sub-reasons. No second HTTP client anywhere.
- [ ] 4.2 Budgets (D3), in order: cache hit → cooldown (30 s, LRU 1,024) → per-address window (setting, ≤ 512 tracked addresses, eviction never refills) → global 4 in flight → single-flight per URL. In-process, no lock held across `await` except the single-flight future. A `reset_state_for_tests()` hook.
- [ ] 4.3 `validate_document(url, obj)` returns the projection `{client_id, client_name, redirect_uris, scope}`, applying every D4 rule. https redirect URIs go through `_normalized_redirect_uri`, and loopback http follows D5's host set.
- [ ] 4.4 Cache (D4/D13): bounded LRU of 256, keyed by exact URL, storing projection + hash + expiry. TTL = `max-age` clamped to the setting; 300 s when absent; 0 under `no-store`/`no-cache` or a ceiling of 0. An expired entry is never served after a failed refetch.
- [ ] 4.5 `document_hash(projection)` (SHA-256 over sorted-key compact JSON).

## 5. Redirect matching

- [ ] 5.1 `redirect_uri_allowed(registered, requested, *, cimd)` per D5. DCR keeps exact membership byte for byte. The GET and the POST both call it; no other membership test of `redirect_uris` remains in `routes.py`.
- [ ] 5.2 The code row stores the exact requested URI. `/token`'s exact comparison is untouched.

## 6. Synthetic client row, client-id matching, sweep

- [ ] 6.1 Approve path for CIMD: re-resolve the document, compare its hash with the signed state (D7, `metadata_changed`), then in the code's transaction upsert the row (`INSERT … ON CONFLICT (client_id) DO UPDATE … last_used_at = now() RETURNING`), owner = session user, skipping the first-authorizer claim and the cross-user branch. Deny writes no row.
- [ ] 6.2 `oauth_state` cookie payload `{"s": server_state, "m": hash}` for CIMD. Legacy string payloads are still accepted for DCR.
- [ ] 6.3 `client_id_matches(row, submitted)` (D8), used by the code-exchange mismatch check, the refresh mismatch check and `/revoke`. Grep shows no other `client_id !=` comparison against a row.
- [ ] 6.4 `cleanup_expired_tokens` (`src/services/indexer.py`): a CIMD branch per D9 with the same skip-locked batch, in-lock re-read and delete protocol. One INFO count line per pass covers both kinds. The `null` age disables it.
- [ ] 6.5 `/token` and refresh make no outbound request for a CIMD client (asserted by a test that fails on any fetch).

## 7. Consent page host display and CSP

- [ ] 7.1 `authorize_get` passes `client_kind`, `client_host` (`to_ascii_host` of the URL), `client_id_url`, `first_approved_at`, and `loopback_only` for CIMD. It does not pass `client_name` for CIMD.
- [ ] 7.2 `authorize.html`: the CIMD identity block, the CIMD notice, and the loopback-only warning (D10). Classes in the nonce'd style block, no `style=`, no handlers, every value escaped. The DCR rendering is unchanged.
- [ ] 7.3 `panel_csp.build_policy`: consent `form-action 'self' https: http://localhost:* http://127.0.0.1:*`, still a constant. Update the CSP tests that pin the exact directive.
- [ ] 7.4 OAuth panel page: the CIMD marker, metadata URL, host, and `client_name` labelled self-described.

## 8. `iss` and `resource`

- [ ] 8.1 `_append_query(..., iss=<metadata issuer>)` on both `POST /authorize` redirects, for every client kind.
- [ ] 8.2 `resource` check (D11), log-only, at `GET`/`POST /authorize` and on both `/token` grants, against every value present. An unexpected value emits one `oauth_resource_unexpected` (host only) and the request proceeds. No code path refuses on `resource`.

## 9. Security events

- [ ] 9.1 `src/logging_setup.py`: `registration_kind` (str 8), `cimd_host` (str 253) and `resource_host` (str 253) in `ALLOWED_FIELDS`.
- [ ] 9.2 `src/services/security_events.py`: `EVENT_FIELDS["oauth_client_registration_path"]` and `EVENT_FIELDS["oauth_resource_unexpected"]`; add `cimd_host` to `oauth_authorize_refused`. Emit after the commit from `/register` and the CIMD approve.
- [ ] 9.3 Every CIMD refusal answers the constant `{"error":"invalid_client"}` 400 and emits one `oauth_authorize_refused` with `client_metadata.<sub>` (design D12). No address, error text or document bytes in any record.
- [ ] 9.4 After deploy: watch `oauth_resource_unexpected` and the access logs for `/authorize` and `/token` requests carrying `resource`, and record the values seen (hosts only). This is the evidence for 9.5.
- [ ] 9.5 File the follow-up issue "OAuth: refuse foreign RFC 8707 `resource` with `invalid_target`". Its gate is 14 consecutive days of live logs showing only the four accepted spellings, and it links L9 and this change.

## 10. Tests

- [ ] 10.1 Fake document server fixture (design D14), driven through the real `fetch_url_guarded` with an injected resolver and transport. Every failure mode listed there gets one test asserting the constant response and the logged sub-reason.
- [ ] 10.2 SSRF range test parameterised over the full transfer deny list, for a literal-IP URL and a resolved name, plus mixed public/private answers. Each asserts no connection attempt.
- [ ] 10.3 Budgets: per-address eleventh refusal, cooldown (50 callers → one origin request), single-flight, global cap, cache hit spends nothing, eviction does not refill.
- [ ] 10.4 Cache: max-age clamp, no-store, no-cache, absent header, ceiling 0, failure after expiry.
- [ ] 10.5 Redirect matching table: every D5 accept/refuse case. DCR membership is unchanged.
- [ ] 10.6 Pinning: a document changed between GET and POST is refused; `/token` and refresh never fetch.
- [ ] 10.7 Row: per-(URL, user) ids in multi-user mode with two users and no cross-user refusal; a repeat approve refreshes the row; deny writes nothing; the CHECK rejects a malformed row (integration, real Postgres).
- [ ] 10.8 Token/revoke matching with the URL as `client_id`; a different URL is a mismatch that revokes nothing. Grant family, #325 replay and #326 lifetime behave the same for a CIMD client (integration).
- [ ] 10.9 Sweep: a CIMD row with a token of any state is kept; an unreferenced old row is deleted; the `null` age disables it; the race with a concurrent approve leaves a consistent row (integration).
- [ ] 10.10 Consent rendering: host shown, hostile `client_name` absent, loopback warning, CIMD notice, CSP header exact, no inline style or handler (existing CSP template scanners pass).
- [ ] 10.11 `iss` on approve and deny; `resource`: the accepted set emits nothing; a foreign value (with a query) emits one host-only record and the request still succeeds at `/authorize` and `/token`.
- [ ] 10.12 Event catalogue parity test and the allow-list AST sweep cover the new event and fields. The canary test confirms no secret in any new record.
- [ ] 10.13 `pytest tests` plus `make test-integration` green, with the integration suite actually executed.

## 11. Docs (same PR)

- [ ] 11.1 `docs/architecture/oauth-and-grants.md`: new section "Client ID Metadata Documents (#357)" covering D1–D11, the threat model and L1–L8. Cross-reference the #194 section for the CIMD sweep branch and #183 for consent.
- [ ] 11.2 `docs/architecture/file-transfer.md`: note that the guarded fetch has a second caller (CIMD, `max_redirects=0`) and that its SSRF policy changes now affect OAuth.
- [ ] 11.3 `docs/architecture/control-panel.md`: consent `form-action` loopback sources and the CIMD identity block.
- [ ] 11.4 `docs/architecture/security-event-logging.md`: catalogue row for `oauth_client_registration_path`, the `oauth_resource_unexpected` row, the `client_metadata.*` reasons on `oauth_authorize_refused`, and the `cimd_host`/`resource_host` provenance class.
- [ ] 11.5 `docs/architecture/schema-and-migrations.md`: migration 036.
- [ ] 11.6 README: the three settings in the settings table; connector docs (Claude, Claude Code and ChatGPT use CIMD; DCR remains); an upgrade note for `iss` and the log-only `resource` check.
- [ ] 11.7 `.env.example`: the three settings, commented, with no host paths.
- [ ] 11.8 CLAUDE.md: a key-decisions bullet (CIMD rows per (URL, user), no fetch at `/token`, constant `invalid_client`, sweep rule, consent shows the host) and migration 036 in the model list if referenced.
- [ ] 11.9 Grep check: every new setting appears in README and `.env.example`; no host path or hostname in tracked files.

## 12. Verifier

- [ ] 12.1 `openspec-verifier` subagent (not an implementer) audits the implementation against every requirement and scenario here and runs `openspec validate oauth-cimd --strict`, `pytest tests`, `make test-integration`, `make test-schema` and `make db-check`. Iterate to zero blocking gaps.

## 13. Adversarial Codex (mandatory: auth surface)

- [ ] 13.1 Commit, then run Codex read-only (`gpt-daybreak-blue-latest`, high) as a defensive PASS/FAIL control review of `src/oauth/`, `cimd.py`, the sweep and the template. Framing: SSRF, cache poisoning, consent→code→token pinning, open redirect via matching, consent confusion, cross-user grants, DCR exhaustion, and a regression on any #182/#194/#325/#326 property. Machine-readable verdict block.
- [ ] 13.2 Triage per the workflow (plausible input, real impact, cheaper than documenting). Fix BLOCKER/MAJOR, add declined items to the accepted limitations, and run round two. Run round three (verification only) only if round two finds a new class.

## 14. Archive (same PR)

- [ ] 14.1 `openspec archive oauth-cimd -y` as the last commits of the feature branch, after both gates. One PR, `Closes #357`.

## 15. Live end-to-end (after deploy)

- [ ] 15.1 `curl` the AS metadata on the live host: both CIMD values present, `iss` support advertised.
- [ ] 15.2 Claude (claude.ai connector): remove and re-add the connector, complete consent, call `read_note` and `keyword_search`. The log shows `oauth_client_registration_path registration_kind=cimd` with the connector's host, and no `/register` call for that connection.
- [ ] 15.3 Claude Code: `claude mcp add` the server, authenticate through the loopback callback on an ephemeral port, call `list_notes`. The log shows `registration_kind=cimd cimd_host=claude.ai`.
- [ ] 15.4 DCR still works: register a client with `/register` (`curl`), authorize and exchange. The log shows `registration_kind=dcr`.
- [ ] 15.5 A refresh-token rotation for each CIMD client succeeds after the access token expires, and no document fetch is logged on that path.
- [ ] 15.6 Report which MCP tools were actually called, per client.

## 16. Max's browser pass (consent template + CSP)

- [ ] 16.1 Max walks the consent page for a CIMD client (Claude Code: loopback warning, host identity, CIMD notice) and a DCR client with devtools open. Approve and deny each. **Zero CSP violations**, and the loopback redirect completes.
- [ ] 16.2 Max checks the OAuth panel page lists the CIMD rows with URL, host and the self-described name label. Zero CSP violations.
