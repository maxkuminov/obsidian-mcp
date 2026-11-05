# Tasks: OIDC account linking (#416)

Step 0.3 (goldens) runs first, on the base commit. Slices A and B have
disjoint file scopes and can be fanned out in parallel worktrees. Slice C
depends on both (it imports A's service and B's `VerifiedIdentity`), so it
runs after A and B are merged. Slice D (docs) runs last, on the merged branch.
Migration number **037** belongs to slice A alone.

**Test-file ownership.** Each file has exactly one owner. No other slice
edits it.

| File | Owner |
| --- | --- |
| `tests/golden/oidc_off/*`, `tests/test_oidc_off_regression.py`, `tests/integration/test_oidc_off_consent_pg.py` | 0.3 (later slices may only *run* them) |
| `tests/test_oidc_links.py`, `tests/integration/test_oidc_links_pg.py`, `tests/integration/test_schema_check.py`, `tests/integration/test_nonsuperuser_migrations_pg.py` (only if it needs a change for 037) | A |
| `tests/test_native_oidc.py`, `tests/test_oidc_settings.py` | B |
| `tests/test_oidc_callback_linking.py`, `tests/test_oidc_panel.py`, `tests/integration/test_oidc_callback_pg.py`, `tests/integration/test_oidc_reachable_admin_pg.py`, `tests/test_oidc_mutation_sites.py` | C |

## 0. Spec gate and regression baseline

- [x] 0.1 Codex reviews this proposal (read-only, `gpt-daybreak-blue-latest`); round 1 FAIL folded (reachable-admin invariant, actor re-check, email sentinel, upgrade sequence, schema gate, OIDC-off goldens, docs matrix)
- [x] 0.2 `openspec validate oidc-account-linking --strict` clean after each fold; Codex round 2 FAIL on two remediation gaps (stale migration plan, column markers), folded in fdfb5c0 and checked by the supervisor; no round 3 (no new class)
- [x] 0.3 **Goldens on the base commit, before any slice.** On bd9b386, capture and commit the D16 goldens with their tests, normalizing the CSRF token, nonce, cookies, dates, ids and codes:
  - the unauthenticated `/authorize` redirect (DCR and CIMD clients);
  - authenticated consent render (DCR and CIMD);
  - consent approval, asserting the code's user, the `grant_id` lineage and that the exchanged token's owner is the same user;
  - `/admin/register` GET and POST, empty and populated table;
  - the login, users list, user edit and account pages.

  Matrix: OIDC off in single-user mode, OIDC off in multi-user mode, OIDC on in multi-user mode with a fake provider and #415's settings. The tests must pass on the base. Every later slice must leave them passing **unmodified**, except that slice C may add OIDC-on expectations for item 5's OIDC blocks in a separate file, and only for those blocks.

## 1. Slice A: schema and link service

Files: `src/models/db.py` (User columns + constraints only),
`alembic/versions/037_oidc_account_links.py`, new `src/services/oidc_links.py`,
and the slice-A test files above. Do NOT touch `src/auth/`, `src/config.py`,
`src/control_panel/` or templates.

- [x] 1.1 Add the six nullable columns and the five named constraints (design D1) to `User`, each column with `comment=_OIDC_LINK_COLUMN_MARKER`. The module constant must be byte-identical to the migration's `COLUMN_MARKER`, with a comment block in the file's style (see `_INDEXED_PROVENANCE_MARKER`)
- [x] 1.2 Migration 037, `down_revision` = 036's revision id, following D1's provenance rules:
  - a column marker on each of the six columns and a constraint marker on each of the five constraints;
  - all eleven objects checked before anything is touched; adopt only an exact type / nullability / no-default / marker (or canonical definition / `convalidated` / marker) match, else refuse by name and change nothing;
  - `downgrade()` decides each object on its own marker (constraints first) and prints a skip for an unmarked one; the docstring states that the links are lost;
  - pinned `search_path`, timeouts set and reset, no backfill, no superuser requirement.
- [x] 1.3 `tests/integration/test_schema_check.py`: `HEAD_REVISION = "037"`, keeping every earlier wave's cases. Add 037 cases:
  - a fresh upgrade;
  - the chain from 036 with existing user rows unchanged and all six columns NULL;
  - catalogue column types and nullability;
  - **per column:** type, nullability, absent default, and `col_description` equal to the column marker;
  - **per constraint:** `pg_get_constraintdef`, `convalidated`, and `obj_description` equal to the constraint marker;
  - stamp-back: re-running over the exact marked shape changes nothing;
  - every invalid row shape rejected (half-bound, half-pending, bound-and-pending, a duplicate `(issuer, subject)`, a duplicate `(expected_issuer, expected_email)`);
  - impostor refusals, atomic across all eleven objects: a wrong type, NOT NULL, a default, a wrong constraint definition, a `NOT VALID` CHECK, and **the correct shape but unmarked**, once for a column and once for a constraint;
  - downgrade drops the marked objects and skips the unmarked ones;
  - downgrade to 036, then a clean re-upgrade and `alembic check`.
- [x] 1.4 Confirm `tests/integration/test_nonsuperuser_migrations_pg.py` runs 037 to head as the owning role (edit it only if it pins a head)
- [x] 1.5 `oidc_links.normalize_email(str) -> str | None` per D2; a pure function that never raises
- [x] 1.6 `oidc_links.resolve(session, iss, sub, email: str | None, email_verified: bool) -> Resolution`: unlocked SELECTs; **`(iss, sub)` first, email never examined when it matches**; otherwise `None` or an unnormalizable email → `none`; a matching live expectation → `pending` or `unverified`; `PENDING_DAYS = 7`
- [x] 1.7 `oidc_links.prepare_login(session, resolution) -> PrepareResult`: `lock_account_guard`, `FOR UPDATE` re-read with `populate_existing=True`, re-check every D4 step-3 condition, write the bind (pending only) and stamp `last_login_at`, flush, **no commit**; a unique violation or failed re-check → rollback + a closed reason; returns the re-read `session_version`
- [x] 1.8 `oidc_links.reachable_admins_after(session, issuer, *, target_id, target_after)` per D15
- [x] 1.9 Admin operations with no commit: `set_expectation`, `bind_subject`, `unlink` (clears the six columns, returns the `revoke_user_sessions` count); closed results `ok` / `bound_already` / `conflict` / `invalid`
- [x] 1.10 Unit tests: the normalization table (case, whitespace, non-ASCII kept, two `@`, control characters, length bounds, non-string input)
- [x] 1.11 Integration tests: bind happy path; expired; wrong issuer; a concurrent double bind of one `(iss, sub)` → exactly one wins; `resolve` on a bound user with `email=None`; `reachable_admins_after` for each `target_after`; advisory-lock re-entry by `start_session` in the same transaction does not block; unlink revokes sessions; a permanent user delete leaves no link residue
- [x] 1.12 `make test-schema`, `make db-check`, `make test-integration` (slice-A modules) clean

## 2. Slice B: protocol profile and settings

Files: `src/auth/oidc.py`, `src/config.py` (OIDC fields + `_validate_oidc`
only), `src/services/transport_security.py` (a new `load_oidc_ca_context`
only), and the slice-B test files above. Do NOT touch routes, templates,
models or the migration.

- [x] 2.1 Settings per D7. `OIDC_SUBJECT_USER_IDS` non-empty is refused with the **exact D3 message** (the upgrade sequence plus the docs section name), whether OIDC is on or off; no network at validation
- [x] 2.2 `load_oidc_ca_context`, parsed once; `BoundedTransport` passes `verify=` that context, else certifi (confirm the parameter against pinned httpx2 2.16.0)
- [x] 2.3 `exchange()` returns `VerifiedIdentity(iss, sub, email: str | None, email_verified: bool)`. It **never refuses on email**: non-string / absent / > 254 → `None`; `email_verified` is `True` only for the JSON boolean `true`. It refuses only on signature, `iss`/`aud`/`azp`/`exp`/`iat`/`nonce`/`sub` (≤ 255) and group. Remove the `oidc_subject_user_ids` lookup
- [x] 2.4 Algorithms: `{RS256, PS256, ES256}` ∩ discovery-advertised (RS256 if absent), passed to `jwt.decode(algorithms=…)`
- [x] 2.5 Token endpoint auth method from settings; refused `provider` at start when discovery lists methods and omits it
- [x] 2.6 Scopes `openid email profile` + extra; group check only when configured
- [x] 2.7 `metadata()` exposes `iss_parameter_required` for slice C
- [x] 2.8 Tests with a fake provider (real RS256/PS256/ES256 keys):
  - each algorithm is accepted when advertised and refused when not; HS256 and `none` are refused;
  - basic vs post (header vs body, never both);
  - the CA file is accepted, or refused when it has no CA or is unreadable;
  - every setting's validation table, including the retired setting's message text;
  - the group is optional;
  - `exchange()` returns an identity, not a refusal, for tokens whose email is absent, a number, a list, 300 characters long, or whose `email_verified` is `"true"`.

## 3. Slice C: callback, panel routes, templates, lifespan warning, events

Starts after A and B are merged. Files: `src/auth/routes.py`,
`src/control_panel/users.py`, `src/control_panel/routes.py` (account page
context only), templates `login.html`, `user_edit.html`, `users.html`,
`account.html`, `_utilities.html` (only via its generator),
`src/control_panel/static/panel.js` (only if the existing `data-confirm`
listener does not cover the unlink form), `src/main.py` (lifespan warning),
`src/services/security_events.py`, and the slice-C test files above.

- [x] 3.1 Callback order:
  1. consume the state, then fetch discovery;
  2. check `iss`;
  3. fetch the token and JWKS, then validate the identity;
  4. only then touch the DB: `resolve`, then `prepare_login` (or refuse), then `start_session(expected_session_version=…)`, rolling back on a refused mint;
  5. `warm_user_vault_cache`, then the events.
- [x] 3.2 Refusal reasons per D14, with one response builder that keeps every refusal byte-identical apart from the CSRF token and the nonce
- [x] 3.3 The callback never touches the per-account budget
- [x] 3.4 Panel POST routes `/admin/users/{id}/oidc/{expect,bind,unlink}`:
  - `require_admin`, CSRF, 404 when OIDC is off;
  - `_lock_admin_guard`, then **immediately** `_actor_still_privileged`; on failure `actor_snapshot`, rollback, `_log_actor_revoked`, then `_ACTOR_REVOKED_MSG` (D11);
  - only then the target `FOR UPDATE`, the slice-A operation, the D15 check (unlink), commit, flash and the event;
  - the self-unlink refusal while password login is off.
- [x] 3.5 **D15 at the existing sites:** in `edit_user_submit` (clearing `is_admin` / `is_active`) and `delete_user` (soft and permanent), add `reachable_admins_after(...) == 0 → refuse` when `LOCAL_LOGIN_ENABLED=false`, inside the guard after `_actor_still_privileged`, beside the existing last-active-admin check, and without a commit before the check
- [x] 3.6 Templates per D11 under the nonce CSP; every new block wrapped in `{% if oidc_enabled %}`
- [x] 3.7 The lifespan warning (D13)
- [x] 3.8 Catalogue entries for the three events and the reason set
- [x] 3.9 Callback tests:
  - bound login with the email absent, a number, a list, 300 characters, or two `@` and `email_verified: "true"` → signs in;
  - pending bind with a verified email; `email_verified` false / absent / `"true"` with a matching email → `email_unverified`;
  - a pending branch with the email absent or malformed → `unlinked`; an expired expectation;
  - a rename (new email, same sub) still signs in; a reassigned email (new sub) is refused; A-bound + B-pending → A;
  - an inactive user is refused before any write; a refused mint leaves no binding and no `last_login_at`; an unlink committed between resolve and mint refuses;
  - `last_login_at` is stamped; the budget is untouched;
  - `iss` required, optional and mismatched;
  - the `/authorize` round trip returns to the exact encoded URL;
  - refusal bodies are identical across reasons; no subject or email appears in any log record.
- [x] 3.10 Panel tests:
  - each action's success, conflict and re-point refusal; CSRF missing; a non-admin gets 403; OIDC off gives 404;
  - unlink revokes sessions; self-unlink is refused with password login off;
  - a CSP scan of the rendered pages;
  - **actor re-check, deterministic:** hold A's link request at the guard with a barrier, commit B's demotion of A, release, and expect A's request to be refused with `_ACTOR_REVOKED_MSG` with nothing written (once per route).
- [x] 3.11 `tests/integration/test_oidc_reachable_admin_pg.py`:
  - each D15 site refuses the zero-reachable outcome: unlink, demote, deactivate, soft delete, permanent delete;
  - each proceeds when another reachable admin exists, or when password login is enabled;
  - an unbound active admin does not count;
  - concurrency, deterministic via a barrier: "A unlinks B" vs "B deactivates A", "A demotes B" vs "B deletes A", and two unlinks of each other; exactly one succeeds each time and one reachable admin always remains.
- [x] 3.12 `tests/test_oidc_mutation_sites.py`: an AST or grep guard that every `src/` write of `User.is_admin`, `User.is_active`, `User.oidc_subject`, and every `users` delete, is in the D15 allow-list (the four sites plus first-login bind, bootstrap and create)
- [x] 3.13 The 0.3 goldens pass: every OIDC-off golden and every non-page on_multi golden is byte-identical. Supervisor decision (2026-11-04): the four `page_*_on_multi` goldens are regenerated in b6cdbd4, because the 0.3 harness compares whole pages; their diff is exactly the D11 OIDC blocks plus the stamped last login. The blocks are also asserted on their own in `tests/test_oidc_panel.py`. `tests/integration/test_oidc_off_consent_pg.py`'s OIDC-on `sign_in` step was changed in 28fddcf to bind through `oidc_links.bind_subject`, because the `OIDC_SUBJECT_USER_IDS` env map is retired; the change is harness only, with no golden bytes.

## 4. Slice D: docs (last commits of the feature branch)

- [x] 4.1 **Settings × docs matrix.** Each of the following appears, with default, validation and purpose, in **all four** of README.md (settings table), DEPLOYMENT.md, `.env.example` and `docs/native-oidc.md`:
  - `OIDC_TOKEN_AUTH_METHOD`
  - `OIDC_EXTRA_SCOPES`, including that `offline_access` is refused
  - `OIDC_DISPLAY_NAME`
  - `OIDC_CA_FILE`
  - `OIDC_REQUIRED_GROUP`, now optional
  - `LOCAL_LOGIN_ENABLED`, with its recovery role
  - `OIDC_SUBJECT_USER_IDS`, marked retired with a pointer to the upgrade section (in `.env.example`, a commented "retired" line, not a live example)

  A grep check over the four files for each name is part of the task.
- [x] 4.2 README: the feature section rewritten for linking, plus an **upgrade note** that reproduces the D3 sequence and links to the docs section
- [x] 4.3 `docs/native-oidc.md`:
  - generic provider requirements;
  - the linking workflow;
  - bootstrap order and break-glass;
  - the D15 rule;
  - an **"Upgrading from OIDC_SUBJECT_USER_IDS"** section with the exact D3 sequence (its title must match the startup message);
  - a **Keycloak walkthrough**: realm → client, Client authentication ON, Standard flow only, PKCE S256, redirect URI `https://<MCP_HOSTNAME>/admin/auth/oidc/callback`, no wildcards; client scopes `email` + `profile`; "Verify email" on, "Duplicate emails" off; an optional Group Membership mapper (`groups`, ID token, full path) with `OIDC_REQUIRED_GROUP`; where to find a user's `sub`; an NTP note.
- [x] 4.4 DEPLOYMENT.md: the pointer section, k3s break-glass (edit the config, rollout restart), and the upgrade sequence (or a pointer to it)
- [x] 4.5 `docs/architecture/control-panel.md`: linking, the matching rule, bind/mint ordering, the actor re-check, D15
- [x] 4.6 `docs/architecture/security-event-logging.md`: the three events and the reason set
- [x] 4.7 `docs/architecture/schema-and-migrations.md`: a 037 section (columns with no FK, markers, the gate's head literal `037`, the 037 cases)
- [x] 4.8 CLAUDE.md: a Key-decisions bullet replacing the #414 paragraph (including D15); a `docs/native-oidc.md` row in the architecture table

## 5. Verification gates

- [x] 5.1 `openspec-verifier` against these deltas (non-author): 0 blocking, 8 non-blocking notes, fixed in the verifier-fix commit except the `mapping` strictness one, which the security-event-logging doc fix covers
- [x] 5.2 Adversarial Codex (mandatory: an auth and tenancy boundary): round 1 on d32e70b PASS with zero findings (349 offline OIDC tests run by the reviewer); no fixes, so no re-review round. L11 recorded from the supervisor's own review
- [x] 5.3 `make audit`, `make test-schema` (375), `make test-integration` (1333), `pytest tests`; `make db-check` is left for the deploy
- [x] 5.4 Archive (`openspec archive oidc-account-linking -y`) in the feature PR, `Closes #416`

## 6. Post-deploy (owner)

- [ ] 6.1 Max's browser pass with devtools open: SSO button, each card action, unlink confirm, zero CSP violations
- [ ] 6.2 Live: set expectations for max and oksana, each signs in once; one MCP client's `/authorize` consent via SSO; API keys and refresh still work
- [ ] 6.3 Optional: `LOCAL_LOGIN_ENABLED=false` once the boot log shows no CRITICAL lockout line
