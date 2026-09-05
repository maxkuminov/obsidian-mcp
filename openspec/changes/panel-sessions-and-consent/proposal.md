## Why

Three findings from the 2026-09-04 OWASP ASVS 5.0 assessment sit on the same surface — the panel's browser session and the consent screen it authorizes grants from — and each one ends at the same place: an attacker holding a panel cookie can mint `readwrite` `omcp_` API keys and approve OAuth grants against a tenant's vault, which is durable agent write access to Max's source of truth.

**#198 (V7.4.1) — logout does not log you out.** `logout()` calls `request.session.clear()` and nothing else (`src/auth/routes.py:204-206`). Starlette's `SessionMiddleware` answers that with an expiring `Set-Cookie`; the cookie that was already issued stays a valid, correctly-signed credential until its itsdangerous timestamp passes `session_max_age` — seven days. The verification on the issue replayed a pre-logout cookie against the container's installed Starlette 1.6.0 and got `user_id 7` back after the user had signed out. The only server-side invalidator in the tree is `users.session_version` (`src/auth/session.py:151`), and exactly one handler increments it: the admin password reset (`src/control_panel/users.py:610`). No test in `tests/` references logout at all.

**#197 (V6.2.2) — no way to rotate your own password.** Every route that writes `password_hash` outside bootstrap is on the admin router (`src/control_panel/users.py:55`, `:579-613`). In production `multi_user_mode` a non-admin who suspects their password is compromised has to ask an administrator, who then knows the replacement. `README.md` states the gap as a design position ("Password reset is admin-driven only"), which is why it was never treated as a bug.

**#183 (V10.4.7) — the consent screen cannot tell you who is asking.** `/register` is unauthenticated, so every OAuth client on this server is self-registered, and `_valid_redirect_uri` checks only "https, has a host, no fragment" (`src/oauth/routes.py:61-66`). `authorize_get` passes `client_name` — attacker-chosen text — and no redirect host, no client identifier, no registration date and no trust statement (`:396-411`). A client named "Claude" with a redirect to an attacker's host renders a page indistinguishable from the real connector's. `/authorize` sits behind `chain-oauth@file`, so the reachable victims are exactly the Google-authorized panel users, i.e. the population that holds vaults. The read-only preselect (#63) caps the default at read, which for a multi-tenant vault of record is still full exfiltration.

The three are one change because they are one flow: a session that cannot be ended is the credential a phishing consent screen is redeemed with, and self-service rotation is useless if rotating the password leaves the stolen session alive.

## What Changes

- **A server-side session registry.** A new `user_sessions` table (migration **024**) holds one row per live browser session: `id` (the **SHA-256 of** a 256-bit CSPRNG identifier), `user_id`, `created_at`, `last_seen_at`, `expires_at`, `revoked_at`, `user_agent_hash`. The signed cookie carries the identifier alongside the fields it already carries; `get_active_session_user` resolves the row and refuses a cookie whose row is missing, revoked, or expired, on top of the existing user-exists / user-active / `session_version` checks. The cookie transport does not change — Starlette's `SessionMiddleware` stays, because `verify_csrf` depends on it in both modes.
- **Logout revokes the row.** `POST /admin/auth/logout` stamps `revoked_at` for its own session before clearing the cookie, so the replay the issue demonstrated answers 302 to login. A revocation that fails still clears the cookie and redirects, and records an ERROR: refusing the sign-out would leave the user signed in *and* the cookie alive.
- **Every account-level event revokes the user's other sessions**, in the same transaction as the write that makes it true and inside the `_lock_admin_guard` critical section where one is held: admin password reset, deactivation through the user edit form, and the soft delete. A permanent delete needs no code — the rows carry `ON DELETE CASCADE`.
- **`POST /admin/account/password`, self-service.** A new non-admin panel page (`GET /admin/account`) and handler taking `current_password`, `new_password` and `new_password_confirm`, CSRF-protected by the panel router's existing dependency, verifying and hashing through the existing `verify_password` / `hash_password` — **the 72-byte truncation and the NUL rejection are preserved exactly; they are not bugs to fix.** On success it writes the hash, increments `session_version`, revokes **every** session of that user, and immediately mints a fresh one for the browser that made the request. Net user-visible effect: other devices are signed out, this one is not. The admin reset stays as the recovery path.
- **The change-password route is throttled** at the login limiter's rate (`5/minute`) under a key composed of the client IP **and** the authenticated session's user id, so neither an IP rotation nor a shared NAT egress defeats it.
- **One password policy, one constant.** `MIN_PASSWORD_LENGTH = 12` in `src/auth/passwords.py`, applied by the new handler, the bootstrap form and the admin reset — which are at 8 today and would otherwise be able to set a password the owner cannot re-set. No composition rules, no maximum below bcrypt's own truncation, and no forced rotation: existing accounts are never re-checked at login.
- **The consent card identifies the client.** `authorize_get` additionally passes the redirect URI's **host** (from `urlparse(...).hostname` — never `netloc`, so `https://claude.ai@evil.example/cb` correctly reads `evil.example`), the `client_id`, and the client's registration date. A standing notice on **every** render says the application registered itself and is not verified by this server.
- **An operator-configurable allow-list of known connector redirect hosts** (`OAUTH_KNOWN_REDIRECT_HOSTS`, default `claude.ai,chatgpt.com`) drives a "known client" badge; every other host gets the stronger warning naming the host the authorization code would be sent to. Matching is **exact host equality**, lower-cased ASCII: no suffix match, no wildcard, so `evilclaude.ai` and `claude.ai.evil.example` are unverified. An empty list means everything is unverified, which is the safe direction.
- **Expired session rows are purged by the existing cleanup job**, on the OAuth retention rule already established by #64: `expires_at < now - 7 days`, revoked rows included and never deleted earlier, so a revocation stays visible for at least a week.
- **Security events.** Emitted through the sibling `security-event-logging` change's `security_events` module when it has landed, and through a plain module logger using the **same event names** when it has not: `panel_logout` (already in that catalogue), `panel_session_replay_refused`, `panel_sessions_revoked`, `panel_password_changed`, `panel_password_change_refused`. **No credential material is ever recorded** — not the session identifier, not a password, not a hash. Where a session must be identified in a record it is by `token_tag` (the catalogue's `"sha:" + sha256(value)[:8]` form), never by the identifier itself.

Migration 024 creates one table and backfills nothing. No new dependency.

## Dependency on the sibling `security-event-logging` change (PR #216)

That change owns the event catalogue, the `ALLOWED_FIELDS` allow-list and the `security_events.emit` contract. This change's records are named to fit it and are additive to its table. **If #216 lands first**, the call sites here import `src/services/security_events.py` and the new events are registered in `EVENT_FIELDS`; the fields used are `reason`, `user_id`, `username`, `count`, `route`, `method`, `client_ip` and `token_tag`, all of which already exist in that allow-list. **If it does not**, the same names are emitted through `logging.getLogger(__name__)` with the identifiers in the **message text** (the deployed formatter is `%(message)s`, so `extra` alone reaches nobody), and adopting `security_events` later is a mechanical substitution. Whichever order they land in, the event names do not change.

Migration **023** is reserved by a sibling change. 024's `down_revision` is `"023"`, so 024 MUST NOT merge ahead of it; if 023 is abandoned, 024 is rebased onto `"022"` before merge. `tests/integration/test_schema_check.py`'s `HEAD_REVISION` moves to `"024"` only once both are in.

## Capabilities

### New Capabilities

- `panel-session-registry`: the browser session becomes a server-side row with a lifecycle — minted at login, validated on every request, touched, revoked by logout and by every account-level event, and purged on the OAuth retention schedule. A signed cookie is no longer sufficient on its own.
- `panel-account-password`: a signed-in user can change their own password, under CSRF and a per-(IP, account) throttle, with the effect on their other sessions specified.

### Modified Capabilities

- `oauth-authorization-integrity`: consent revalidation now runs through the session registry as well as `session_version`, and the consent screen must identify the requesting client — redirect host, client identifier, registration date, an unconditional self-registration notice, and a badge or a warning decided by the operator's allow-list.
- `panel-user-administration`: the admin actions that end an account's access (password reset, deactivation, both deletes) must end its live sessions too, in the same transaction as the write.
- `schema-integrity`: migration 024 owns `user_sessions`; the schema gate's head assertion moves with it.

## Impact

- `src/models/db.py` — new `UserSession` model; `User` gains the relationship. `alembic/versions/024_user_sessions.py` — new table, three indexes, no backfill.
- `src/config.py` — `session_touch_interval_seconds`, `session_purge_retain_days`, `oauth_known_redirect_hosts` (the last with the `NoDecode` + CSV/JSON validator `fts_configs` uses; a bare `list[str]` is JSON-decoded by pydantic-settings and a comma-separated env value would fail).
- `src/auth/session.py` — `start_session`, `revoke_session`, `revoke_user_sessions`, `touch_session`; `get_active_session_user` gains the registry checks. This module is the single definition of all four.
- `src/auth/routes.py` — `login_submit` and `register_submit` mint through `start_session`; `logout` gains a database session and revokes; **`login_form` stops reading `request.session["user_id"]` raw** and resolves through `get_active_session_user`, or a revoked cookie sends the visitor into a redirect loop between the login page and `/admin/`.
- `src/auth/passwords.py` — `MIN_PASSWORD_LENGTH` and a `validate_new_password` helper returning a user-facing message (including for the NUL case, which today would reach `hash_password` and raise a 500).
- `src/limiter.py` — the composite `(client IP, session user id)` key function.
- `src/control_panel/routes.py` — `GET /admin/account`, `POST /admin/account/password`. `src/control_panel/templates/account.html` (new), `base.html` (nav entry).
- `src/control_panel/users.py` — `reset_password`, `edit_user_submit` and `delete_user` revoke the target's sessions inside their existing critical section, with **no commit between the lock and the write**.
- `src/services/indexer.py` — `cleanup_expired_tokens` also purges `user_sessions`.
- `src/oauth/routes.py` — `authorize_get` derives and passes the host, identifier, registration date and trust verdict. `src/oauth/trust.py` (new) — the allow-list matcher, one definition.
- `src/control_panel/templates/authorize.html` — the identity block, the standing notice, the badge and the warning. `_theme.html` if any token is added — and then in **all three** blocks (`:root`, `:root[data-theme="light"]`, and the `prefers-color-scheme: light` copy), because `checks/token_coverage.py` asserts the two light copies are identical and `colorscan` fails on a colour literal outside a token definition.
- `docs/architecture/control-panel.md` (session lifecycle, the account page, the revocation call sites), `oauth-and-grants.md` (the consent identity block and the allow-list rule), `schema-and-migrations.md` (024).
- `README.md` — the "Password reset is admin-driven only" limitation is replaced; while that section is open, the adjacent "No rate limiting on `/admin/auth/login`" bullet is corrected, since `login_submit` has carried `@limiter.limit("5/minute")` for some time.
- **Operational:** the deploy signs every live panel session out exactly once, because a cookie with no session identifier is refused rather than grandfathered. With two production users that is two logins, and grandfathering would leave the #198 window open for a further seven days.

Closes #198
Closes #197
Closes #183
