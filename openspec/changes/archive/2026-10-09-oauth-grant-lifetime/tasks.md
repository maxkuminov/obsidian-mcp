Implementation order: slice 1 first (everything depends on the columns, the setting and the helpers). Slices 2 and 3 both edit `src/oauth/routes.py` (different functions: 2 owns `_handle_auth_code`'s lookup/replay and the cleanup; 3 owns both mint sites' clamp, `_handle_refresh`, middleware, panel, consent) — run them sequentially, or have slice 3 rebase on slice 2, never in parallel worktrees against the same base. Migration number **029** is assigned to this change.

## 1. Schema, setting and helpers

- [x] 1.1 `alembic/versions/029_oauth_grant_lifetime.py`: `oauth_codes.grant_id varchar(64) NULL` (no default, no index, marker comment) and `oauth_tokens.grant_issued_at timestamptz NOT NULL DEFAULT now()` (marker comment), following 025's shape: reconcile a marked column of the exact shape, refuse and name any other shape, `lock_timeout`/`statement_timeout` set and `RESET`, `search_path` pinned to `public` and `RESET`, qualified identity asserted, `oauth_codes` before `oauth_tokens`.
- [x] 1.2 Backfill: every pre-existing `oauth_tokens` row gets the migration transaction's timestamp (one value for all rows); a value already present is never overwritten. Downgrade drops only marked columns and fails naming an unmarked one.
- [x] 1.3 `src/models/db.py`: `OAuthCode.grant_id`, `OAuthToken.grant_issued_at` (`server_default=func.now()`, NOT NULL), marker constants mirrored byte-identically; comment on each explaining the inheritance rule and why the default exists (rolling deploy) yet must never be relied on.
- [x] 1.4 `src/config.py`: `oauth_grant_absolute_lifetime_days: int = Field(90, ge=1, le=365)` — a plain int, not `NullableLimit`; empty, `null`, `none` and `0` fail at boot. Comment carries D4's justification.
- [x] 1.5 `src/oauth/grants.py`: `grant_deadline(grant_issued_at)`, a clamp helper returning `(access_expires_at, refresh_expires_at, expires_in)` for a given `now` and deadline (or a refusal signal when under 1 s remains), and `consent_lifetimes()` returning the three humanized strings.
- [x] 1.6 `make test-schema` cases for 029: fresh upgrade (columns, nullability, marker, server default via catalogue), backfill value identical across every pre-existing row and equal to the migration's timestamp, impostor column refused (wrong type / nullable `grant_issued_at` / `grant_id` NOT NULL / missing default), stamp-back re-run changes no value, downgrade of marked and unmarked columns, `alembic check` clean.

## 2. #325 — authorization-code replay

- [x] 2.1 `_handle_auth_code`: look up by `code_hash` alone, `FOR UPDATE`, `populate_existing=True`; drop the `used` and caller-`client_id` predicates. A caller `client_id` that differs from the row's refuses (`invalid_grant.client_id_mismatch`) and revokes nothing.
- [x] 2.2 Reorder per design D7: client auth → `redirect_uri` → PKCE → `used` branch → expiry → existing checks. Update existing tests whose expectations depended on expiry preceding PKCE. (Superseded by 2.7: a live code keeps the pre-#325 order, expiry before `redirect_uri`/PKCE.)
- [x] 2.7 Codex review (MAJOR, accepted): a **spent** code's failed revalidation (client_id mismatch, vanished client, failed client auth, wrong/missing `redirect_uri`, malformed/wrong verifier) answers the unknown-code response byte for byte and records `invalid_grant.spent_code_<check>`; a live code keeps its specific refusals and the pre-#325 order (expiry first). Real-Postgres exact-response comparisons in `test_oauth_grant_lifetime_pg.py`; unit cases in `test_oauth_code_replay.py`.
- [x] 2.8 Verifier notes: 029/ORM marker equality test; `OAUTH_GRANT_ABSOLUTE_LIFETIME_DAYS` driven through the environment; AST guard covers `insert(OAuthToken)` / `OAuthToken.__table__.insert()`; 029 `downgrade()` sets the same `lock_timeout`/`statement_timeout` as `upgrade()`.
- [x] 2.3 Replay branch per D8: NULL lineage → `invalid_grant.code_reused`, nothing committed; else `lock_grant` + `revoke_grant_family`, commit only if rows flipped; every DB call guarded; response byte-identical to `unknown_code`.
- [x] 2.4 First exchange writes `oauth_code.grant_id = grant_id` in the same transaction as `used = True` and the token inserts.
- [x] 2.5 `src/services/security_events.py`: catalogue entries `oauth_code_replay_detected` (`client_id`, `grant_id`, `user_id`, `revoked_tokens`, `client_ip`) and `oauth_code_replay_revocation_failed` (`client_id`, `grant_id`, `user_id`, `client_ip`, `error_type`); emitted after commit; class name only on failure, no `exc_info`.
- [x] 2.6 `cleanup_expired_tokens`: code predicate becomes `expires_at < cutoff` alone; docstring updated (the "used code has no history value" sentence is now false).

## 3. #326 — absolute grant lifetime and disclosure

- [x] 3.1 Code exchange: `grant_issued_at = now` on both rows (one captured `now`), clamp via the helper, `expires_in` from the clamped access expiry.
- [x] 3.2 `_handle_refresh`: copy `grant_issued_at` from the locked `old_token`; deadline check after the reuse branch and client checks, before per-token expiry (`invalid_grant.grant_lifetime_exceeded`, "grant lifetime exceeded; re-authorize", nothing revoked, nothing committed); refuse when under 1 s remains; clamp both new tokens; `expires_in` from the clamped access expiry.
- [x] 3.3 `src/mcp_server/auth.py`: after the OAuth `expires_at` check, `now >= grant_deadline(...)` → 401 `invalid_token`, auth-failure reason `grant_lifetime_exceeded` (add to any reason catalogue the failure emitter checks).
- [x] 3.4 `src/control_panel/routes.py` `_token_status`: `now >= deadline` → `expired`.
- [x] 3.5 `authorize_get` passes `consent_lifetimes()`; `authorize.html` renders the D10 block — no `style=`, no `on*=`, any new CSS in the nonce'd style block, text escaped.
- [x] 3.6 AST test: every `OAuthToken(` construction under `src/` passes `grant_issued_at=` explicitly.
- [x] 3.7 `src/services/transfer.py`: `credential_expires_at` returns `min(expires_at, grant_deadline(grant_issued_at))` for an `OAuthToken`, and `_credential_ok`'s OAuth expiry comparison uses it — so `plan_mint_window`, redemption (`resolve_identity`) and the locked pre-publication re-check (`_identity_publish_ok`, the upload gate) all honour the deadline through the one predicate.

## 4. Tests

Unit tests (fake-session pattern of `tests/test_issue_182_refresh_reuse.py`) for branch logic and response constancy; the cases below on **real Postgres** in `tests/integration/` (skipped without `PGVECTOR_TEST_ADMIN_URL`; run via `make test-integration`).

- [x] 4.1 First exchange: 200; both tokens share `grant_id` and `grant_issued_at`; the code row carries that `grant_id` and `used = true`; `expires_in` = 3600.
- [x] 4.2 Valid replay (same client, `redirect_uri`, verifier): 400 byte-identical to unknown-code; every token of the family revoked, including the live access token; one `oauth_code_replay_detected`; a second replay adds no WARNING.
- [x] 4.3 Invalid replays revoke nothing: wrong verifier, malformed verifier, wrong `redirect_uri`, wrong `client_id`, wrong secret for a confidential client — each leaves the family live.
- [x] 4.4 Replay after the code's ten-minute expiry (clock injected) still revokes.
- [x] 4.5 Concurrent double exchange on two connections: exactly one 200, one 400, zero live tokens afterwards, in either commit order.
- [x] 4.6 Cleanup retention: a spent code survives the cleanup until seven days past its `expires_at` and is deleted after; an unspent expired code follows the same schedule; a replay inside the window revokes, after it is an unknown code.
- [x] 4.7 NULL-lineage spent code (simulating pre-029): replay refused, nothing revoked, nothing committed.
- [x] 4.8 Family revocation under the lock: a replay racing a refresh of the same family leaves no live token (the rotated-in pair included).
- [x] 4.9 Rotation inherits `grant_issued_at` unchanged across several rotations.
- [x] 4.10 Near the deadline (refresh 2 days before, with 90-day cap): refresh token `expires_at == deadline`, access `min(1h, deadline)`, `expires_in` matches; at 30 minutes before: access clamped, `expires_in` ≈ 1800.
- [x] 4.11 At and beyond the deadline: refresh refused with `grant_lifetime_exceeded`, nothing revoked or minted; under 1 s remaining refused.
- [x] 4.12 Concurrent refreshes of one token near the deadline: one rotates (clamped), the other is reuse and revokes the family — #182 unchanged.
- [x] 4.13 Replay of a rotated-away refresh token after the deadline still revokes the family.
- [x] 4.14 Shortened setting: a family past the new deadline is refused at refresh and its unexpired access token 401s at `/mcp` with `grant_lifetime_exceeded`; the panel shows it expired.
- [x] 4.15 Setting validation: `0`, empty, `null`, `none`, `366` refused at settings construction; `1` and `365` accepted.
- [x] 4.16 Consent page: renders the three lifetimes from the setting (default, and a value below 30 days where refresh shows the cap); no `style=` attribute and no `on*=` in the rendered HTML.
- [x] 4.17 Migration: pre-existing families read `grant_issued_at` = migration timestamp and can still refresh immediately after upgrade.
- [x] 4.18 Reuse vs deadline precedence: a rotated-away refresh token after the deadline gets the byte-identical generic replay response (no `error_description`) and revokes the family; a live one gets `grant_lifetime_exceeded` and revokes nothing.
- [x] 4.19 Consent anchor: time advanced between approval (`/authorize` POST) and the code exchange — `grant_issued_at` equals the exchange time, not the approval time.
- [x] 4.20 Transfer (real Postgres): a pending upload and a pending download capability minted by an OAuth access token are refused (uniform 404, nothing written) once the setting is shortened past the family's deadline while token and capability are unexpired; the mint window is clamped to the deadline; no capability is minted from a credential past its deadline; the locked pre-publication re-check refuses.

## 5. Docs

- [x] 5.1 `docs/architecture/oauth-and-grants.md`: new sections for code replay (D5–D9, risk window) and absolute lifetime (D1–D4, D10); correct the #194 section's "a used `oauth_codes` row is deleted the instant it is spent"; accepted limitations L1–L11.
- [x] 5.2 `docs/architecture/schema-and-migrations.md`: 029 — the two columns, why `grant_issued_at` has a server default when 025 refused one, the AST guard, backfill value.
- [x] 5.3 `docs/architecture/security-event-logging.md`: the two new events and the new reason codes.
- [x] 5.4 `CLAUDE.md` key decisions: one bullet — absolute grant lifetime (`OAUTH_GRANT_ABSOLUTE_LIFETIME_DAYS`, 90, not disable-able, issuance inherited like `grant_id`) and code-replay family revocation after full revalidation, spent codes retained 7 days past expiry.
- [x] 5.5 `.env.example`: commented `OAUTH_GRANT_ABSOLUTE_LIFETIME_DAYS=90` with range and why there is no off value.
- [x] 5.6 `README.md` settings table: the new row.
- [x] 5.7 `docs/architecture/file-transfer.md`: the OAuth credential's effective expiry is `min(expires_at, grant deadline)` at mint, redemption and pre-publication.

## 6. Gates

- [x] 6.1 `openspec validate oauth-grant-lifetime --strict` clean.
- [ ] 6.2 `make test-schema`, then `make test-integration` (not concurrently), unit suite, `make audit`; `make db-check` after the migration runs anywhere.
- [ ] 6.3 `openspec-verifier` pass; adversarial Codex (mandatory: token endpoint and auth) — two rounds by default, findings triaged per the workflow budget.
- [ ] 6.4 Owner browser pass on `/authorize` with devtools open: lifetime block legible in light and dark, zero CSP violations.
- [ ] 6.5 After deploy: `alembic check` clean on the live database; an existing connector keeps working without re-authorizing; a fresh consent shows the lifetime block.

## 7. Archive

- [ ] 7.1 `/openspec-archive-change oauth-grant-lifetime` (`openspec archive -y`) as the last commit of the feature branch, with the docs above, in the same PR (`Closes #325`, `Closes #326`).
