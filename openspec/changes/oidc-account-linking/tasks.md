# Tasks: OIDC account linking (#416)

Slices A and B have disjoint file scopes and can be fanned out in parallel
worktrees. Slice C depends on both (it imports A's service and B's
`VerifiedIdentity`), so it runs after A and B are merged. Slice D (docs) runs
last, on the merged branch. Migration number **037** belongs to slice A alone.

## 0. Spec gate

- [ ] 0.1 Codex reviews this proposal (read-only, `gpt-daybreak-blue-latest`), framed per CLAUDE.md: a wrong binding is a cross-tenant takeover of a vault; fold the findings into design.md and the deltas before any code
- [ ] 0.2 `openspec validate oidc-account-linking --strict` clean after the fold

## 1. Slice A: schema and link service

Files: `src/models/db.py` (User columns + constraints only),
`alembic/versions/037_oidc_account_links.py`, new `src/services/oidc_links.py`,
new `tests/test_oidc_links.py`, new
`tests/integration/test_oidc_links_pg.py`. Do NOT touch `src/auth/`,
`src/config.py` or templates.

- [ ] 1.1 Add the six nullable columns and the five named constraints (design D1) to `User`, with a comment block in the file's style explaining each
- [ ] 1.2 Migration 037, `down_revision` = 036's revision id: add the columns and constraints, no backfill; downgrade drops them (docstring states that the links are lost)
- [ ] 1.3 `oidc_links.normalize_email(str) -> str | None` per D2 (strip ASCII whitespace, 3–254 chars, one `@`, no whitespace or control characters, ASCII-only lower-case)
- [ ] 1.4 `oidc_links.resolve(session, iss, sub, email, email_verified) -> Resolution` (bound / pending-match / unverified / none), unlocked SELECTs only, with `PENDING_DAYS = 7`
- [ ] 1.5 `oidc_links.prepare_login(session, resolution) -> PrepareResult`: `lock_account_guard` (from `src/oauth/grants.py`), `FOR UPDATE` re-read with `populate_existing=True`, re-check every D4 step-3 condition for the bound or pending branch, write the bind (pending only) and stamp `last_login_at`, flush, **no commit** (the caller's `start_session` commits); a unique violation or failed re-check → rollback + a closed reason (`unlinked` / `account` / `link_conflict`); returns the re-read `session_version`
- [ ] 1.6 Integration test that `pg_advisory_xact_lock` re-entry by `start_session` in the same transaction does not block
- [ ] 1.7 Admin operations with no commit (the caller commits, as with `revoke_user_sessions`): `set_expectation`, `bind_subject`, `unlink` (clears the six columns and returns the `revoke_user_sessions` count); each returns a closed result (`ok` / `bound_already` / `conflict` / `invalid`)
- [ ] 1.8 Unit tests: normalization table (case, whitespace, non-ASCII kept, two `@`, control characters, length bounds)
- [ ] 1.9 Integration tests (real Postgres): each constraint rejects its bad row; bind happy path; expired; wrong issuer; bound-and-pending rejected; concurrent double bind of one `(iss, sub)` → exactly one wins, the other `link_conflict`; unlink revokes sessions; a permanent user delete leaves no link residue
- [ ] 1.10 `make test-schema` and `make db-check` clean

## 2. Slice B: protocol profile and settings

Files: `src/auth/oidc.py`, `src/config.py` (OIDC fields + `_validate_oidc`
only), `src/services/transport_security.py` (a new `load_oidc_ca_context`
only), `tests/test_native_oidc.py`, new `tests/test_oidc_settings.py`. Do NOT
touch routes, templates, models or the migration.

- [ ] 2.1 Settings per D7: `OIDC_TOKEN_AUTH_METHOD`, `OIDC_EXTRA_SCOPES`, `OIDC_DISPLAY_NAME`, `OIDC_CA_FILE`; `OIDC_REQUIRED_GROUP` optional; issuer ≤ 2,048; `OIDC_SUBJECT_USER_IDS` non-empty refused with the D3 message (whether OIDC is on or off); no network at validation
- [ ] 2.2 `load_oidc_ca_context` (same shape as `load_embedding_ca_context`), parsed once; `BoundedTransport` passes `verify=` that context, else certifi (confirm the `verify` parameter against pinned httpx2 2.16.0)
- [ ] 2.3 `exchange()` returns `VerifiedIdentity(iss, sub, email, email_verified, groups_ok)`, not a user id; remove the `oidc_subject_user_ids` lookup; `sub` ≤ 255; `email` a string ≤ 254 or absent; `email_verified` kept only as a real bool
- [ ] 2.4 Algorithms: `{RS256, PS256, ES256}` ∩ discovery `id_token_signing_alg_values_supported` (RS256 if absent), passed to `jwt.decode(algorithms=…)`; never `none`/HS*
- [ ] 2.5 Token endpoint auth method from settings; refused `provider` at start when discovery lists methods and omits it
- [ ] 2.6 Scopes `openid email profile` + extra; group check only when configured
- [ ] 2.7 Export a `metadata()` result that carries `iss_parameter_required` (discovery `authorization_response_iss_parameter_supported is True`) for slice C's callback ordering
- [ ] 2.8 Tests with a fake provider (real RS256/PS256/ES256 keys via joserfc, a mocked transport): each alg accepted when advertised, refused when not; HS256/`none` refused; basic vs post (the token request carries credentials in the header vs the body, never both); CA file accepted / refused (no CA, unreadable); every new setting's validation table; the retired setting refused; group optional

## 3. Slice C: callback, panel routes, templates, lifespan warning, events

Starts after A and B are merged. Files: `src/auth/routes.py`,
`src/control_panel/users.py`, `src/control_panel/routes.py` (account page
context only), templates `login.html`, `user_edit.html`, `users.html`,
`account.html`, `_utilities.html` (only if new utility classes are needed,
regenerated by its generator), `src/control_panel/static/panel.js` (only if the
existing `data-confirm` listener does not already cover the unlink form),
`src/main.py` (lifespan warning), `src/services/security_events.py`,
new `tests/test_oidc_callback_linking.py`, new `tests/test_oidc_panel.py`, new
`tests/integration/test_oidc_callback_pg.py`.

- [ ] 3.1 Callback order: consume state → discovery → `iss` check (present-and-different refused; absent refused when required) → token + JWKS → validated identity → only then DB: `resolve` → `prepare_login` (or refuse) → `start_session(expected_session_version=<prepare's version>)`, rolling back on a refused mint → `warm_user_vault_cache` → `panel_oidc_linked` (bind only) and `panel_login_succeeded`
- [ ] 3.2 Refusal reasons per D14 (`mapping` → `unlinked`; add `email_unverified`, `link_conflict`); a single response builder keeps every refusal byte-identical apart from the CSRF token and the nonce
- [ ] 3.3 The callback never calls `rate_limits.record_login_failure` and never consults the per-account budget
- [ ] 3.4 Panel POST routes `/admin/users/{id}/oidc/{expect,bind,unlink}`: `require_admin`, CSRF, 404 when OIDC is off, `_lock_admin_guard`, the slice-A operation, commit, flash, event; self-unlink refused while `LOCAL_LOGIN_ENABLED=false`
- [ ] 3.5 Templates per D11 under the nonce CSP (no `on*=`, no `style=`, unlink as `type="button"` + `data-confirm`); every new block wrapped in `{% if oidc_enabled %}`; login button label from `OIDC_DISPLAY_NAME`
- [ ] 3.6 Lifespan: after the DB is reachable, with OIDC on and password login off, count active admins bound under the configured issuer; zero → one CRITICAL line; query failure → a WARNING with the class name; never exit
- [ ] 3.7 Catalogue `panel_oidc_linked`, `panel_oidc_link_set`, `panel_oidc_link_removed`; the reason set on `panel_oidc_refused`; the existing field-allow-list tests pass
- [ ] 3.8 Tests: bound login ignores the email; pending bind with a verified email; `email_verified` false / absent / `"true"` refused; expired; rename (new email, same sub) still signs in; reassigned email (new sub) refused; A-bound + B-pending → A; inactive refused before any write; a refused mint after a pending match leaves no binding and no `last_login_at`; an unlink committed between resolve and mint refuses the bound login; `last_login_at` stamped; budget untouched; `iss` required / optional / mismatched; `/authorize` round trip returns to the exact encoded authorize URL; refusal bodies identical across reasons; no subject or email in any captured log record
- [ ] 3.9 Panel tests: each action's success, conflict, re-point refusal, CSRF missing → refused, non-admin → 403, OIDC off → 404; unlink revokes sessions; self-unlink refused with password login off; CSP scan of rendered pages (no `style=`, no `on*=`)
- [ ] 3.10 OIDC-off snapshot test: login, users list, user edit and account pages render byte-identically to the pre-change templates (normalize the CSRF token and the nonce), in single-user and multi-user mode

## 4. Slice D: docs (last commits of the feature branch)

- [ ] 4.1 README: the feature section rewritten for linking, a settings table row for every new setting, and an upgrade note for the retired `OIDC_SUBJECT_USER_IDS`; grep-check that every `OIDC_*` name in `src/config.py` appears in README and `.env.example`
- [ ] 4.2 `.env.example`: the new settings, commented, with defaults; the retired one removed
- [ ] 4.3 `docs/native-oidc.md`: generic provider requirements (same-origin HTTPS endpoints, the algorithms, the auth methods, `email` + `email_verified` in the ID token, optional group), the linking workflow (expect email or bind subject, first sign-in, unlink), bootstrap order, break-glass, and a **Keycloak walkthrough**: realm → client `obsidian-mcp`, Client authentication ON, Standard flow only, PKCE S256, valid redirect URI `https://<MCP_HOSTNAME>/admin/auth/oidc/callback`, no wildcards, post-logout redirect none; client scopes `email` + `profile` default; realm "Verify email" on and "Duplicate emails" off; optional Group Membership mapper (`groups`, ID token, full path) + `OIDC_REQUIRED_GROUP`; where to find a user's `sub` for direct binding; NTP note (L6)
- [ ] 4.4 DEPLOYMENT.md: a pointer section updated; k3s break-glass (edit the config, rollout restart)
- [ ] 4.5 `docs/architecture/control-panel.md` OIDC section: linking, matching rule, bind/mint ordering, the self-unlink rule
- [ ] 4.6 `docs/architecture/security-event-logging.md`: the three events, the reason set
- [ ] 4.7 `docs/architecture/schema-and-migrations.md`: migration 037 and why it uses columns with no FK
- [ ] 4.8 CLAUDE.md: replace the #414 paragraph with a Key-decisions bullet (#416: admin-linked, verified-email first bind, `(iss, sub)` only afterwards, no provisioning, the retired env map, no plaintext issuer, local logout); add a `docs/native-oidc.md` row to the architecture-notes table

## 5. Verification gates

- [ ] 5.1 `openspec-verifier` against these deltas (non-author)
- [ ] 5.2 Adversarial Codex (mandatory: an auth and tenancy boundary): attack the binding (window, rename, reassignment, issuer change, concurrency), refusal uniformity, the callback ordering and the OIDC-off byte identity; two rounds per the review budget
- [ ] 5.3 `make audit`, `make test-schema`, `make db-check`, `make test-integration`, `pytest tests`
- [ ] 5.4 Archive (`openspec archive oidc-account-linking -y`) in the feature PR, `Closes #416`

## 6. Post-deploy (owner)

- [ ] 6.1 Max's browser pass with devtools open: SSO button, each card action, unlink confirm, zero CSP violations
- [ ] 6.2 Live: set expectations for max and oksana, each signs in once, confirm binding and vault; complete one MCP client's `/authorize` consent via SSO; confirm API keys and refresh still work
- [ ] 6.3 Optional: `LOCAL_LOGIN_ENABLED=false` once the boot log shows no CRITICAL lockout line
