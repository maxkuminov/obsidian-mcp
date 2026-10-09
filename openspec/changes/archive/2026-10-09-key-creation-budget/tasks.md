Slices A and B touch disjoint files and can run in parallel. Slice C depends on both. Slice D (docs) can be written alongside C. Tests are written by the slice that owns the code, and the gates in §6 are run by non-authors.

## 1. Slice A — settings, budget and event (D1, D2, D7, D10)

- [x] 1.1 `src/config.py`: add `key_creation_account_limit` (10), `key_creation_address_limit` (20) and `key_max_active_per_account` (25), all as `NullableLimit` with `LIMITER_COUNT_MAX` ceilings, and `key_creation_window_seconds` (3600, `ge=1, le=LIMITER_WINDOW_SECONDS_MAX`). Each gets a comment stating its why. The startup log gets no additions unless the existing limiter settings are logged.
- [x] 1.2 `src/services/rate_limits.py`: add `KeyCreationRefusal(reason, limit, window_seconds, retry_after_seconds)` and `try_charge_key_creation(account_key, address) -> KeyCreationRefusal | None`. It uses two exact dicts swept on access and fixed windows. It is synchronous: check both counters, then charge both, with no `await`. It charges neither on refusal. Add a `_reset_*` test hook alongside the login budget's.
- [x] 1.3 `src/services/security_events.py`: add `key_creation_throttled` to the allow-list with exactly the eight fields named in the spec.
- [x] 1.4 Unit tests: atomic dual charge, exact keying (no cross-account merge), sweep bound, window expiry with a patched monotonic clock, `retry_after_seconds` ≤ the remaining window, null disables each counter independently, and settings through a real env file (zero refused, empty/`null`/`none` accepted, above-ceiling refused).

## 2. Slice B — the limit resolvers and the cap (D3, D4, D5)

- [x] 2.1 New `src/services/api_keys.py`:
  - `resolve_create_limit(user, *, provided, value, unlimited)` and `resolve_edit_limit(user, *, value, unlimited)`, returning a limit or a typed refusal (`required` | `forbidden_unlimited` | `blank_edit`) per the D5 table. Pure functions with no DB access.
  - `assert_active_key_capacity(session, user)`, which takes `SELECT … FOR UPDATE` on the `users` row and counts active keys. It returns a typed refusal or None, and does nothing for admins and the single-user operator.
- [x] 2.2 Unit tests for the full D5 matrix (admin / non-admin × value / blank-or-omitted / explicit unlimited × default set / null) on create and on edit.

## 3. Slice C — wire both routes and the panel (D3, D5, D6)

- [x] 3.1 `src/api/routes.py` `create_key`. Order: validation → `resolve_create_limit` (403 + `_log_panel_forbidden(..., "unlimited_requires_admin", ...)`; 400 required) → `assert_active_key_capacity` (409) → `try_charge_key_creation` (429 + `Retry-After`, emit `key_creation_throttled`) → insert/commit. Keep `@limiter.limit("5/minute")`. Remove `_created_key_limit` and update its docstring rationale in the resolver.
- [x] 3.2 `src/api/routes.py` `set_key_limit`: `_assert_key_owner` first, then `resolve_edit_limit`, where an explicit null from a non-admin is a 403 plus `panel_forbidden`.
- [x] 3.3 `src/control_panel/routes.py` `create_key_form`: add an `unlimited: str = Form("")` field, then the same order as 3.1 using flash + 303 for every refusal. Replace the "No default substitution here" comment block with the new rationale (owner decision, #323).
- [x] 3.4 `src/control_panel/routes.py` `set_key_limit_form`: the `unlimited` field, and a blank field becomes a flashed error.
- [x] 3.5 `keys_page`: pass `is_admin` (or reuse the existing context flag if `_panel_context` provides one) and `quota_limit_default`. Update the create and edit help copy per the spec.
- [x] 3.6 `keys.html`: add the admin-only `<input type="checkbox" name="unlimited" value="1" data-unlimited-toggle data-unlimited-target="…">` to both modals. No inline `style=` or `on*=`; use classes only. Regenerate `_utilities.html` only if a new utility class is used.
- [x] 3.7 `panel.js`: a delegated `change` listener for `[data-unlimited-toggle]` that sets `disabled` on the target input. `editLimit` ticks the box and disables the input when the current limit is `''`, and resets both otherwise. Both must tolerate an absent toggle (a non-admin's modal has none): with no box, the input is always left enabled.
- [x] 3.8 Route tests (`tests/test_issue_323_key_creation_budget.py`), per design §Tests 1–14, including **alternating JSON/form requests against one budget**, address rotation, account rotation, validation failures charging nothing, concurrency ≤ limit, refusal shapes and the event fields, and the template rendering the checkbox for admins only. Update the existing #162/#194 tests that asserted "blank means unlimited" or "explicit null means unlimited for anyone". Each such edit is called out in the PR description; none may be silently deleted.
- [x] 3.8a Regression: in single-user mode, budget refusals from rotating trusted client addresses all charge one fixed suppression subject (`account:single-user`), so with the suppressor live they do not each get a fresh log allowance; and exactly one emitter invocation happens per refusal.
- [x] 3.8b Regression: a non-admin can open the editor of their own grandfathered unlimited key (no Unlimited control rendered) and assign a numeric limit through the form.
- [x] 3.8c `api-auth-hardening` delta: a test pins that a non-admin may create a key through `POST /api/keys` (owner-scoped) and cannot revoke another account's key.
- [x] 3.9 Integration test (`tests/integration/`): the cap is exact under two concurrent creates, revoked keys do not count, and admins are exempt.

## 4. Docs (same PR)

- [x] 4.1 `docs/architecture/rate-limits.md`:
  - a new row in the control table (key-creation budget: where, scope key account+address, defaults, refusal, event);
  - a section "Key creation is one budget across two representations", covering D1–D4 and why slowapi cannot share across routes;
  - the "Accounts" key-space paragraph extended;
  - the in-process paragraph listing this budget;
  - the four new settings in the Settings table;
  - the "default daily limit" section rewritten for the new blank/null semantics.
- [x] 4.2 `docs/architecture/control-panel.md`: the admin-only Unlimited control, the `data-unlimited-toggle` listener, and the blank-field semantics on create and edit.
- [x] 4.3 `docs/architecture/security-event-logging.md`: a catalogue row for `key_creation_throttled`, and `unlimited_requires_admin` added to the `panel_forbidden` reasons.
- [x] 4.4 `CLAUDE.md`, key decisions:
  - Replace "an explicit `null` (or a blank panel field) still means unlimited" with: a blank or omitted limit gets the default, only an admin creates or restores unlimited via an explicit control or null, and a null default makes the limit required.
  - Add one bullet for the shared account+address key-creation budget and the non-admin active-key cap.
  - Add the new settings to the rate-limits row of the architecture table.
- [x] 4.5 `README.md`: the settings table (four new rows), the `DEFAULT_DAILY_REQUEST_LIMIT` row, and the quota paragraph.
- [x] 4.6 `.env.example`: a commented block for the four new settings, and the `DEFAULT_DAILY_REQUEST_LIMIT` comment corrected (blank no longer means unlimited, and null now means "required").

## 5. Owner browser pass (panel template change)

- [ ] 5.1 After deploy, Max walks `/admin/keys` with devtools open.
  - As an admin: create a key with the default; clear the box; tick Unlimited (the number field disables); edit a limited key to Unlimited; edit an unlimited key, untick the box and set a number; submit a blank edit and see the flash.
  - As a non-admin: confirm no Unlimited control appears in either modal.
  - Exhaust the budget from the form and see the flash.
  - **Zero CSP violations** in the console throughout.

## 6. Gates and archive

- [x] 6.1 Offline suite green; `make test-integration` green (the cap test needs it).
- [x] 6.2 `openspec validate key-creation-budget --strict` clean.
- [ ] 6.3 `openspec-verifier` subagent, run by a non-author.
- [ ] 6.4 Adversarial Codex (auth/permissions and quota correctness: mandatory). Two rounds by default; declined findings go under Accepted limitations in `design.md`.
- [ ] 6.5 End-to-end check against the live server after deploy: create keys through `/api/keys` and the form until refused, then confirm an MCP tool call with a new default-limited key is quota-accounted. The report names the routes and tools actually called.
- [ ] 6.6 `openspec archive key-creation-budget -y` as the last commits of the feature branch, before the PR is opened for merge. The PR body says `Closes #323`.
