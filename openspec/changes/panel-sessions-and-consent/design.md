## Context

Three ASVS findings on one surface. The constraints that shape every decision below:

- **The panel has no CSP and will not get one** (`docs/architecture/control-panel.md`). Every control runs from an inline `onclick` or an htmx attribute, so the only policy they survive carries `unsafe-inline` for scripts — which permits exactly the injection a CSP is bought to stop. That is why "an XSS on the panel steals the session cookie" is a live path in #198 rather than a theoretical one, and why the fix has to be server-side rather than a cookie flag.
- **Every colour the panel renders comes from a token in `_theme.html`**, dark canonical, light written out **twice** (`:root[data-theme="light"]` and the `prefers-color-scheme: light` block, asserted identical by `checks/token_coverage.py`). `colorscan` globs the templates directory and fails on a colour literal outside a token definition. A new consent warning that hard-codes an amber breaks the sweep.
- **`SessionMiddleware` is mounted in both modes** because `verify_csrf` reads `request.session` on all `/admin` routes regardless of `multi_user_mode` (`src/main.py:318-326`). The registry therefore cannot replace the cookie; it sits behind it.
- **Single-user mode never reaches any of this.** `get_current_user` returns `_SINGLE_USER_SENTINEL` before the session is consulted, and `src/auth/routes.py` is not even mounted (`src/main.py:387-390`). No `user_sessions` row is ever written in that mode.
- **`_lock_admin_guard` is a check-then-act critical section and nothing may commit inside it** (`docs/architecture/control-panel.md`, "The last-admin guard"). Any revocation added to `reset_password` / `edit_user_submit` / `delete_user` has to ride the handler's existing transaction, not open one of its own.
- **A `pg_dump` of this database is protected data** (`docs/architecture/schema-and-migrations.md`): it already holds every password, API-key, OAuth and transfer-token *hash*, and deliberately no plaintext credential. A session table storing raw identifiers would be the first exception.

## Decisions

### D1 — The cookie carries a 256-bit CSPRNG identifier; the table stores only its SHA-256

`secrets.token_urlsafe(32)` goes into the signed session cookie under `sid`. The row's primary key is `sha256(sid).hexdigest()`, a `String(64)` — byte for byte the shape `api_keys` and `transfer_tokens` already use for their credentials.

*Why:* the identifier **is** a bearer credential for seven days. Storing it verbatim would make a database dump — which the deploy takes before every migration and keeps for thirty days — a file full of live panel sessions, contradicting the invariant that dumps hold hashes and nothing more. The lookup is an equality on a hashed primary key, so it costs the same as an equality on the raw one.

*Rejected — store the raw id:* simpler to debug, and one leaked backup is a live session for every user. *Rejected — HMAC with `SECRET_KEY` instead of a bare SHA-256:* an unkeyed digest of 256 bits of CSPRNG output has nothing to brute-force, and keying it makes the whole table unreadable after a `SECRET_KEY` rotation, which is a recovery step the operator may need.

### D2 — `session_version` stays, as a second and account-wide switch

The registry is the per-session control. `users.session_version` remains in the cookie and remains checked, after the registry checks.

*Why:* it is already load-bearing for a shipped requirement (`oauth-authorization-integrity`: "Password reset invalidates consent session"), removing it would rewrite that spec for no gain, and it is the one invalidator that still works if a registry write is somehow lost. Two independent gates that both have to pass is the cheap direction of defense in depth.

*Rejected — replace `session_version` with the registry:* it deletes a working control to make a diff smaller. *Rejected — `users.sessions_invalid_before` as the whole fix:* the cross-family audit note on #198 rejects it explicitly, and it is right to. It terminates every device at once, needs a precise issued-at field and a comparison rule the cookie does not carry today, and retains same-timestamp cookies if the comparison is written as `<` where it should be `<=`. It answers "log everyone out" when the requirement is "log *this* session out".

### D3 — Absolute expiry, no sliding renewal

`expires_at = created_at + settings.session_max_age` (7 days), written once at mint and never extended.

*Why:* Starlette re-signs the cookie on any response that modifies the session, so the cookie's own itsdangerous age effectively slides while the row does not. Making the row the *tighter* of the two bounds means the pair can never disagree in the dangerous direction — the row expires first, the cookie is refused, and no configuration produces a cookie that outlives its row. A session that is used daily and therefore never expires is precisely the durable access #198 is about.

*Rejected — extend `expires_at` on each touch:* a compromised session held by an attacker who polls the dashboard lives forever.

### D4 — Cookies minted before the deploy are refused, not grandfathered

`get_active_session_user` requires **both** `user_id` and `sid`. A cookie carrying only `user_id` is a pre-deploy cookie and is refused, cleared, and redirected to login.

*Why:* grandfathering would keep the #198 replay window open for a further seven days after the fix ships — the exact window the change exists to close. The cost is that every live panel session is signed out once, at deploy. Production has two users.

### D5 — `user_agent_hash` is recorded and never enforced

`sha256(request.headers.get("user-agent", ""))`, nullable, written at mint. Nothing reads it in an authorization decision.

*Why:* it is worth having when an operator is reconstructing what happened. As a *binding* it is a bad control: it is trivially replayed by anyone who has the cookie (the same theft yields the header), and it signs users out on every Chrome auto-update, which trains them to re-authenticate after an unexplained logout — the habit phishing depends on.

### D6 — `last_seen_at` is throttled to 60 s and written on its own transaction

A validated request updates `last_seen_at` only when the stored value is older than `settings.session_touch_interval_seconds` (default 60). The `UPDATE` runs in a **separate** `async_session()` and commits itself; a failure is logged at WARNING and swallowed.

*Why the throttle:* `require_user_panel` runs on every panel route, every `/api/*` route and the reset-embeddings progress endpoint the panel polls. One write per request is write amplification on a shared PostgreSQL instance for a field nothing authorizes on.

*Why a separate session:* `get_active_session_user` is called from a FastAPI dependency, before the handler's own work. Writing on the request's session would put the touch inside the handler's transaction — lost on any rollback, and worse, sharing a transaction with `_lock_admin_guard`'s critical section on the user-admin routes. Committing the request's session from a dependency to avoid that would commit a handler's partial work. A separate session is what `cleanup_expired_tokens` already does for the same reason.

*Why swallow the failure:* `last_seen_at` is telemetry. A dead write must not 500 a page load.

### D7 — Revocation rides the caller's transaction; the caller commits

`revoke_user_sessions(session, user_id)` issues `UPDATE user_sessions SET revoked_at = now() WHERE user_id = :u AND revoked_at IS NULL` on the **AsyncSession it is handed** and does not commit. Every panel caller already has an open transaction and its own commit.

*Why:* `reset_password`, `edit_user_submit` and `delete_user` hold `_lock_admin_guard`, a transaction-scoped advisory lock, and the documented rule is that nothing may commit between taking it and writing the flags — a commit there is what makes the check-then-act non-atomic. A helper that committed would silently break the last-admin guard from the inside. The same shape keeps the hash write and the revocation in `change_password` atomic: either the password changed and the sessions died, or neither.

`revoke_session(session, sid_hash)` is the single-row form, used by logout.

### D8 — Logout that cannot revoke still logs you out

If the `UPDATE` or its commit raises, `logout` rolls back, records an ERROR, clears the cookie and redirects to login anyway.

*Why:* the alternative is a 500 on the sign-out button, which leaves the user signed in *and* the cookie alive — strictly worse than the state we are trying to leave. Clearing the cookie removes it from this browser, which is the common case (a person leaving a shared machine); the replay window survives only for a copy already taken, which is the rarer case and is what the ERROR is for.

*Rejected — fail closed with a 500:* it converts a database blip into "the sign-out button is broken", and users respond by closing the tab, which is the same outcome with no record.

### D9 — Purge follows the OAuth retention rule exactly

`cleanup_expired_tokens` gains `DELETE FROM user_sessions WHERE expires_at < now() - interval '7 days'`. Revoked-but-unexpired rows are **not** deleted early.

*Why:* this is the #64 lesson transposed. Deleting revoked rows on sight makes "row absent" the ordinary case, which destroys the only record an operator has that a session was ended deliberately, and reproduces the blank space that made a no-op revoke read as success. Because a session can only be revoked while it exists (`revoked_at <= expires_at` for every revocation this code performs), a window measured from `expires_at` guarantees a revoked row stays visible for at least seven days after it was revoked. As in the OAuth case that makes the revoked branch a strict subset of the expiry branch, so the predicate is a single comparison rather than an `or_`.

The function keeps its name. "Tokens" there already means "dead credential rows on one schedule"; renaming it churns a 3,400-line module and its call site for a word.

### D10 — Password policy: one constant, 12 characters, no composition rules

`MIN_PASSWORD_LENGTH = 12` lives in `src/auth/passwords.py` beside the hasher, and `validate_new_password(new, confirm) -> str | None` returns a user-facing message or `None`. It checks: length, the confirmation match, and the NUL byte. All three password entry points use it — the new self-service handler, `register_submit` (8 today) and `reset_password` (8 today).

*Why raise the other two:* leaving them at 8 means an administrator can set a password its owner is then forbidden from setting again, and it leaves the bootstrap admin — the most privileged account on the server — under the weakest rule. *Why no composition rules:* they demonstrably reduce entropy by pushing users to `Password1!`; length is the control that works. *Why no forced rotation:* nothing re-checks the policy at login, so existing accounts keep working. That is a deliberate limitation, recorded below.

*Why the NUL check has to be in the validator:* `hash_password` **raises `ValueError`** on an embedded NUL (`src/auth/passwords.py`, preserving passlib's policy — a documented rule that must not be "fixed"). Today a NUL in the bootstrap or admin-reset form would reach it and produce a 500. Routing all three through the validator turns that into a form error.

The 72-byte truncation is untouched and undocumented-in-the-UI on purpose: telling users "only your first 72 bytes count" invites shortening. It is documented in the module and in the spec, where it already is.

### D11 — The change-password throttle is keyed on (client IP, session user id)

`src/limiter.py` gains `ip_and_session_user(request)`, returning `f"{get_remote_address(request)}|{request.session.get('user_id')}"`, used as `key_func` on `@limiter.limit("5/minute")` for `POST /admin/account/password`.

*Why this key:* the threat is someone at a borrowed or hijacked browser guessing the current password in order to take the account over. That attacker is pinned by the account, not only by the address, and an account-inclusive key survives an IP rotation. `request.session` is already parsed by `SessionMiddleware` before the route runs, so a *synchronous* slowapi `key_func` can read it.

**Deviation from the brief, stated plainly:** the brief asked for the login limiter to be keyed on username as well as IP. That is not implementable with slowapi as wired here — the login username is a form field, the limiter's `key_func` is synchronous and receives only the `Request`, and the body has not been read when the key is computed. Reading it there would consume the stream the handler then needs. So `login_submit`'s `5/minute` stays IP-keyed and unchanged; the *new* route gets the account into its key, which is the part of the intent this change can actually deliver. Re-keying login belongs to a change that revisits the limiter's storage and middleware, and is a non-goal here.

### D12 — A self-service change revokes every session and re-mints the current one

In one transaction: verify `current_password`; refuse if the new password equals the current one; write `password_hash`; `session_version += 1`; `revoke_user_sessions(session, user.id)` — **all** of them, this one included; commit. Then mint a fresh session (new identifier, new row) and let `SessionMiddleware` write the new cookie on the redirect.

*Why revoke-all-then-remint rather than revoke-others:* the pre-change cookie should not survive a password change either. It is a credential that was live while the old password was, and rotating the session identifier alongside the password is standard fixation hygiene. The user-visible behaviour is the one asked for — other devices signed out, this browser still signed in.

*Why `session_version` too:* it makes the account-wide switch fire as well, so a cookie that somehow escapes the registry check still fails the version check.

*Failure ordering:* if the mint fails after the commit, the user is signed out and signs in with the new password. The password change is the durable half and the session is the recoverable half, so that is the right direction to fail.

### D13 — Consent renders the redirect **host**, from `hostname`, never Unicode-decoded

`urlparse(redirect_uri).hostname`, lower-cased.

*Why `hostname` and not `netloc`:* `urlparse("https://claude.ai@evil.example/cb").netloc` is `"claude.ai@evil.example"`, whose left edge reads as the brand. `.hostname` strips userinfo and the port and answers `"evil.example"` — the host the code would actually be sent to, which is the only thing the user needs.

*Why never Unicode-decoded:* a homograph host (`сlaude.ai` with a Cyrillic с) rendered in Unicode defeats the control entirely. The value is displayed exactly as the ASCII/punycode form it arrives in; a host that is not ASCII is displayed in its `xn--` form. Jinja's autoescape does the rest — the existing requirement "Consent renders client-supplied text as text" already covers `client_name` and extends to these fields.

The card also shows the `client_id` (server-generated hex, not client text) and the client's `created_at`.

### D14 — The allow-list matches exact hosts only

`OAUTH_KNOWN_REDIRECT_HOSTS`, default `claude.ai,chatgpt.com`, parsed by the `NoDecode` + CSV/JSON validator `fts_configs` already uses. `src/oauth/trust.py` holds one function: lower-case the URL's `hostname` and compare for **equality** against the lower-cased configured entries.

*Why exact:* a suffix test is the bug. `endswith("claude.ai")` matches `evilclaude.ai`; `"claude.ai" in host` matches `claude.ai.evil.example`; even `endswith(".claude.ai")` hands the badge to any subdomain an attacker can get, and neither of the two real connectors needs one. The config validator **rejects** an entry containing `*`, `/`, `@` or whitespace, so an operator who writes `*.claude.ai` is told it is not supported instead of quietly getting a host that matches nothing.

*Why the default is these two and no more:* they are the connector hosts this deployment actually serves. The list is deliberately short and additive — an operator adding `chat.openai.com` writes one env value. An unlisted host degrades to the warning, never the reverse, so a stale list is safe and a wrong one is loud.

*An empty list means everything is unverified.* An operator who clears the setting gets warnings on every consent screen, which is correct: nothing has been declared known.

### D15 — The self-registration notice is unconditional; the badge is about the destination only

Every consent render — allow-listed or not — carries the standing notice that the application registered itself with this server, that anyone can register one, and that the name shown was chosen by the application.

*Why unconditional:* `/register` is open, so an attacker can register a client *named* "Claude" whose redirect happens to be an allow-listed host only if they control that host, which they do not — but they can register any name at all, and the name is the thing a user reads first. The badge is worded as a statement about the redirect destination ("this server's operator lists this destination as a known connector"), never as "this application is verified", because nothing verifies it.

Non-allow-listed clients additionally get a warning block that **names the host** and says the authorization code will be sent there, with the advice to deny unless the user started this from that application.

### D16 — Event names come from the sibling catalogue; no credential material, ever

`panel_logout` already exists in `security-event-logging`'s catalogue. This change adds `panel_session_replay_refused` (WARNING; `reason` ∈ `no_session_id` | `unknown_session` | `revoked_session` | `expired_session`, `user_id` when the cookie named one, `token_tag`, `route`, `client_ip`), `panel_sessions_revoked` (INFO; `reason` ∈ `logout` | `password_change` | `admin_password_reset` | `user_deactivated` | `user_deleted`, `user_id`, `count`), `panel_password_changed` (INFO; `user_id`, `username`, `client_ip`, `route`) and `panel_password_change_refused` (WARNING; `reason` ∈ `wrong_current_password` | `too_short` | `mismatch` | `same_as_current` | `nul_byte`, `user_id`, `client_ip`, `route`).

**The session identifier is never a field.** Where a record has to name a session it uses `token_tag` — the catalogue's `"sha:" + sha256(value).hexdigest()[:8]` — the same treatment a presented bearer token gets. A password, a password hash and a session identifier may reach a log in no form at all. Success records are emitted **after** the commit that makes them true, per that change's D17.

If #216 has not landed, the same names go through `logging.getLogger(__name__)` with the identifiers **in the message text**, because the deployed formatter is `%(message)s` and anything left in `extra` reaches nobody.

## Session lifecycle

| Phase | Trigger | What happens | Where |
| --- | --- | --- | --- |
| **Mint** | successful login; bootstrap admin registration; re-issue after a self-service password change | `sid = secrets.token_urlsafe(32)` written to the signed cookie beside `user_id` / `session_version` / `is_admin` / `username`; row inserted with `id = sha256(sid)`, `user_id`, `created_at = now`, `last_seen_at = now`, `expires_at = now + session_max_age`, `revoked_at = NULL`, `user_agent_hash` | `start_session()` in `src/auth/session.py`; called from `login_submit`, `register_submit`, `change_password` — the only three mint sites |
| **Validate** | every request that resolves a browser identity | cookie must carry **both** `user_id` and `sid`; row read by `sha256(sid)`; refused when the row is absent, `revoked_at` is set, `expires_at <= now`, the user is missing or inactive, or `session_version` disagrees. Every refusal clears the cookie and records `panel_session_replay_refused` | `get_active_session_user()`; reached from `require_user_panel` (the panel router, `/api`, `/admin/users`), `login_form`, `authorize_get`, `authorize_post` — the only four entry points |
| **Touch** | a validated request whose row is more than `session_touch_interval_seconds` (60) stale | `UPDATE user_sessions SET last_seen_at = now() WHERE id = :h AND revoked_at IS NULL`, on its own session, committed there, failure logged and swallowed | `touch_session()`, called from `get_active_session_user` |
| **Revoke — one** | logout | `revoked_at = now()` for this row, commit, cookie cleared, `panel_logout` + `panel_sessions_revoked(reason=logout)` | `logout` in `src/auth/routes.py` |
| **Revoke — all of a user** | self-service password change; admin password reset; deactivation through the user edit form; soft delete | `UPDATE user_sessions SET revoked_at = now() WHERE user_id = :u AND revoked_at IS NULL`, **in the caller's transaction**, inside `_lock_admin_guard` where one is held, with no commit between the lock and the write | `change_password`; `reset_password`, `edit_user_submit`, `delete_user` in `src/control_panel/users.py` |
| **Revoke — implicit** | permanent user delete | rows removed by `ON DELETE CASCADE` on `user_sessions.user_id`; no handler code | `delete_user` |
| **Purge** | every indexer tick | `DELETE FROM user_sessions WHERE expires_at < now() - session_purge_retain_days` (7); revoked rows on the same schedule, never earlier | `cleanup_expired_tokens()` in `src/services/indexer.py` |

Two properties the table is meant to make checkable: **mint has three sites and one implementation**, and **validate has four entry points and one implementation**. A fifth entry point that reads `request.session["user_id"]` directly is the defect this change removes from `login_form`, and is the shape of its regression.

## The password-change flow

```
POST /admin/account/password   (panel router: require_user_panel + verify_csrf)
  └─ throttle 5/minute on (client_ip, session user_id)                    [D11]
  └─ multi_user_mode? no  → 404 (the sentinel has no account)
  └─ validate_new_password(new, confirm)  → message? → flash + 303 back   [D10]
  └─ verify_password(current, user.password_hash)  false? → flash + 303   ← constant message, no
  └─ verify_password(new, user.password_hash)      true?  → flash + 303     hint whether the
  ├─ ONE TRANSACTION:                                                       account exists
  │    password_hash = hash_password(new)
  │    session_version += 1
  │    revoke_user_sessions(session, user.id)     ← all, including this one [D12]
  │    commit
  ├─ panel_password_changed  (after the commit)
  └─ start_session(request, user)  → new sid, new row, new cookie
     303 → /admin/account  with a flash
```

Every refusal returns the same shape — flash on the session, 303 back to `/admin/account`, never a query string (`#138`). The wrong-current-password refusal is throttled by the same limiter as the successes, so the throttle cannot be drained by guessing.

## The consent card

Rendered above the existing request box, on every `/authorize` GET:

| Element | Source | Notes |
| --- | --- | --- |
| Application name | `client.client_name` | client-supplied text, escaped; already the case |
| **Redirect destination** | `urlparse(redirect_uri).hostname`, lower-cased ASCII | host only — no scheme, path, port or userinfo (D13) |
| **Client identifier** | `client.client_id` | server-generated hex |
| **Registered** | `client.created_at` | date only |
| **Standing notice** | static | "This application registered itself with this server. Anyone can register one; the name above was chosen by the application and is not verified here." — unconditional (D15) |
| **Known-client badge** | `redirect_host ∈ OAUTH_KNOWN_REDIRECT_HOSTS` | worded about the *destination*, not the application |
| **Unverified warning** | otherwise | names the host, states the authorization code will be sent there, advises Deny unless the user started this from that application |

Colours come from existing `--consent-*` tokens (`--consent-warn`, `--consent-neutral-surface`, `--consent-text-2/3`, `--consent-surface-border`). If a token is added it goes into **all three** blocks of `_theme.html`.

`authorize_post` is unchanged: it already re-validates `redirect_uri` against the client's registered list before minting anything, so the destination shown on the GET is the destination the code goes to.

## Risks

| Risk | Mitigation |
| --- | --- |
| A defect in `get_active_session_user` locks every user out of the panel, including the admin who would fix it | The browser pass before archive exercises login → panel → logout → login. Recovery is a rollback of the image; the migration is additive and does not need to be reversed for the previous image to run. |
| `alembic check` goes dirty because the model and 024 disagree on an index or a server default | `make test-schema` on a throwaway pgvector container, with the 024 cases asserting the catalog directly, is the gate — `alembic check` alone does not compare CHECK predicates or server defaults (`docs/architecture/schema-and-migrations.md`). |
| 024 merges ahead of the sibling's 023 and the chain breaks | `down_revision = "023"`; the task list makes the ordering a merge precondition and names the rebase if 023 is abandoned. |
| A colour literal in the new consent markup silently passes review and fails the sweep | The sweep is in the task list, and the literal rule is restated in the slice contract. |
| One extra indexed primary-key read per authenticated panel request | Single-row PK lookup on a table with at most a handful of rows; the touch is throttled to once a minute per session. |
| The allow-list is edited to include something broad ("`ai`", a wildcard) | The validator rejects `*`, `/`, `@` and whitespace, and matching is equality, so a broad-looking entry simply matches nothing. |
| A future handler mints a session without a row, or validates a cookie without the registry | Both are single-implementation by construction (D-table above), and the spec states it as a requirement rather than as a convention. |

## Non-goals

- Email-based or token-based password recovery. The admin reset stays the recovery path.
- An "active sessions / sign out everywhere" panel page. The registry makes it buildable; it is not built here.
- Replacing the cookie transport (JWTs, server-side session storage of the whole payload). `SessionMiddleware` stays.
- Re-keying the **login** throttle (D11).
- A real client-verification scheme for OAuth — first-party registration, publisher attestation, or a registry. The badge is an operator's allow-list of destinations and says so.
- Multi-factor authentication.
- Changing anything about `omcp_` API keys, OAuth tokens, or transfer capabilities. They have their own lifecycles and are untouched.

## Accepted limitations

| Limitation | Why it is accepted |
| --- | --- |
| The deploy signs every live panel session out once | Grandfathering pre-deploy cookies keeps the #198 window open for another seven days. Two users, two logins. (D4) |
| `last_seen_at` may be up to 60 s stale | It is telemetry; nothing authorizes on it, and per-request writes are amplification on a shared database. (D6) |
| Revocation takes effect at the *next* request; one already in flight completes | Identical to the posture documented for OAuth revocation in `oauth-and-grants.md`. Closing it would mean holding a lock across arbitrary handler work. |
| No user-agent or IP binding | Both are replayed by whoever holds the cookie and both cause spurious logouts. (D5) |
| The login throttle stays IP-keyed | slowapi's `key_func` is synchronous and cannot read the form body. (D11) |
| The throttle is per-process, in slowapi's default in-memory storage, and resets on restart | One uvicorn worker (`Dockerfile`, `--workers 1`), so per-process is per-server today. A restart-resets window is a smaller gap than adding a shared store for this route alone. |
| Existing accounts are not forced up to the 12-character minimum | Nothing re-checks policy at login; forcing rotation on two users to satisfy a floor they may already meet is disruption without a finding behind it. (D10) |
| The known-client badge attests the redirect **host**, not the application, its name, or its intent | Nothing on this server verifies an application. Wording it any more strongly would be the vulnerability with extra steps. (D15) |
| An allow-listed host still shows the self-registration notice, which may read as noisy to a user who connects daily | The notice is the finding. A badge that suppressed it would restore exactly the page #183 describes. |
| Consent shows no "you already granted this client" history | Useful, and a different change; the panel's OAuth page already lists live grants. |
| A stolen cookie still works until logout, expiry, or an account event | This change makes those three effective; it does not detect theft. Session-theft *detection* (impossible-travel, concurrent-use) is out of scope. |
| `session_version` and the registry can disagree if a row write is lost | They are checked with AND, so disagreement always resolves to a refusal — the safe direction. |

## Owner decisions

These were open questions; each is decided here rather than left for the implementer.

1. **Cookie carries a CSPRNG identifier; the table stores its SHA-256** — dumps hold hashes, never live credentials. (D1)
2. **`session_version` is kept**, checked after the registry, as an account-wide second switch. (D2)
3. **Absolute 7-day expiry** equal to `session_max_age`; no sliding renewal. (D3)
4. **Pre-deploy cookies are refused**, not grandfathered; one forced logout at deploy. (D4)
5. **`user_agent_hash` is forensic only** and never enforced. (D5)
6. **`last_seen_at` is throttled to 60 s**, written on its own transaction, failures swallowed. (D6)
7. **Revocation rides the caller's transaction**; the helper never commits, so `_lock_admin_guard` stays atomic. (D7)
8. **A failing logout still clears the cookie and redirects**, with an ERROR recorded. (D8)
9. **Purge is `expires_at < now - 7 days`**, revoked rows included and never earlier; `cleanup_expired_tokens` keeps its name. (D9)
10. **Password minimum is 12**, in one constant, applied to all three entry points; no composition rules; no forced rotation; the NUL check moves into the shared validator so the existing handlers stop 500-ing on it. (D10)
11. **The change-password throttle is `(client IP, session user id)` at 5/minute**; the login throttle is not re-keyed, and that deviation from the brief is recorded rather than faked. (D11)
12. **A password change revokes every session and re-mints the current one**, and bumps `session_version`. (D12)
13. **The consent card shows `urlparse().hostname`**, lower-cased, ASCII/punycode, never `netloc`, never Unicode-decoded. (D13)
14. **The allow-list is exact-host equality**, default `claude.ai,chatgpt.com`, wildcard-ish entries rejected by the validator, empty list = everything unverified. (D14)
15. **The self-registration notice is unconditional**; the badge is worded about the destination only. (D15)
16. **Event names align with the sibling catalogue**, with a plain-logger fallback under the same names; no session identifier, password or hash is ever a field. (D16)
17. **New panel page is `/admin/account`**, gated by `require_user_panel` (not admin), 404 in single-user mode, linked from the sidebar's Access section.
18. **Migration 024 chains from 023** and must not merge ahead of it; the schema gate's `HEAD_REVISION` moves to `"024"` when both are in.
