## Context

Three ASVS findings on one surface. The constraints that shape every decision below:

- **The panel has no CSP and will not get one** (`docs/architecture/control-panel.md`). Every control runs from an inline `onclick` or an htmx attribute, so the only policy they survive carries `unsafe-inline` for scripts — which permits exactly the injection a CSP is bought to stop. That is why "an XSS on the panel steals the session cookie" is a live path in #198 rather than a theoretical one, and why the fix has to be server-side rather than a cookie flag.
- **Every colour the panel renders comes from a token in `_theme.html`**, dark canonical, light written out **twice** (`:root[data-theme="light"]` and the `prefers-color-scheme: light` block, asserted identical by `checks/token_coverage.py`). `colorscan` globs the templates directory and fails on a colour literal outside a token definition.
- **`SessionMiddleware` is mounted in both modes** because `verify_csrf` reads `request.session` on all `/admin` routes regardless of `multi_user_mode` (`src/main.py:318-326`). The registry therefore cannot replace the cookie; it sits behind it.
- **`get_session` neither commits nor rolls back** (`src/database.py`) — it yields a session and closes it. Anything that must be durable commits explicitly, and anything left uncommitted when the request ends is silently discarded. This is the fact behind the mint contract (D7a) and the touch rule (D6).
- **The connection pool is `pool_size=5, max_overflow=10, pool_timeout=30`** (`src/database.py`, with the exhaustion behaviour written down there deliberately). Fifteen concurrent checkouts is the ceiling; a request that holds two leases at once halves it, and the sixteenth caller waits 30 s and then gets a 500. Nothing on a per-request path may take a second lease.
- **Single-user mode never reaches the authentication surface.** `get_current_user` returns `_SINGLE_USER_SENTINEL` before the session is consulted and `src/auth/routes.py` is not mounted (`src/main.py:387-390`), so no session row is ever *created or validated* in that mode. The maintenance purge is not part of that surface and runs in both modes over whatever rows exist — a mode flip must not strand them.
- **`_lock_admin_guard` is a check-then-act critical section and nothing may commit inside it** (`docs/architecture/control-panel.md`, "The last-admin guard"). Any revocation added to `reset_password` / `edit_user_submit` / `delete_user` has to ride the handler's existing transaction, and the *same constant* must be used — "two keys do not exclude each other".
- **A locked re-read must carry `populate_existing=True`** (`docs/architecture/oauth-and-grants.md`). A `SELECT … FOR UPDATE` whose row is already in the session's identity map hands back the *loaded* object with its pre-lock attribute values. That is exactly the shape of the self-service password change, whose `User` arrives pre-loaded from a FastAPI dependency.
- **A `pg_dump` of this database is protected data** (`docs/architecture/schema-and-migrations.md`): it holds every password, API-key, OAuth and transfer-token *hash*, and deliberately no plaintext credential.

## Decisions

### D1 — The cookie carries a 256-bit CSPRNG identifier; the table stores only its SHA-256

`secrets.token_urlsafe(32)` goes into the signed session cookie under `sid`. The row's primary key is `sha256(sid).hexdigest()`, a `String(64)` — byte for byte the shape `api_keys` and `transfer_tokens` already use.

*Why:* the identifier **is** a bearer credential for seven days. Storing it verbatim would make a database dump — taken before every migration and kept thirty days — a file full of live panel sessions, contradicting the invariant that dumps hold hashes and nothing more.

*Rejected — store the raw id:* one leaked backup is a live session for every user. *Rejected — HMAC with `SECRET_KEY`:* an unkeyed digest of 256 bits of CSPRNG output has nothing to brute-force, and keying it makes the table unreadable after a `SECRET_KEY` rotation the operator may need.

### D2 — `session_version` stays, as a second and account-wide switch

The registry is the per-session control; `users.session_version` remains in the cookie and remains checked, after the registry checks.

*Why:* it is already load-bearing for a shipped requirement (`oauth-authorization-integrity`: "Password reset invalidates consent session"), and it is the one invalidator that still works if a registry write is lost. Two gates that both have to pass is the cheap direction of defense in depth.

*Rejected — `users.sessions_invalid_before` as the whole fix:* the cross-family audit note on #198 rejects it explicitly and is right to. It terminates every device at once, needs a precise issued-at field the cookie does not carry, and retains same-timestamp cookies if the comparison is written as `<` where it should be `<=`. It answers "log everyone out" when the requirement is "log *this* session out".

### D3 — Absolute expiry, no sliding renewal

`expires_at = created_at + settings.session_max_age` (7 days), written once and never extended.

*Why:* Starlette re-signs the cookie on any response that modifies the session, so the cookie's itsdangerous age effectively slides while the row does not. Making the row the *tighter* bound means the pair can never disagree in the dangerous direction. A session used daily and therefore never expiring is precisely the durable access #198 is about.

### D4 — Cookies minted before the deploy are refused, not grandfathered

`get_active_session_user` requires **both** `user_id` and `sid`. A cookie carrying only `user_id` is a pre-deploy cookie: refused, cleared, redirected to login.

*Why:* grandfathering keeps the #198 replay window open for a further seven days after the fix ships. The cost is one forced logout at deploy; production has two users.

### D5 — `user_agent_hash` is recorded and never enforced

`sha256(request.headers.get("user-agent", ""))`, nullable, written at mint. Nothing reads it in an authorization decision.

*Why:* useful when reconstructing an incident. As a *binding* it is bad: trivially replayed by anyone who has the cookie (the same theft yields the header), and it signs users out on every browser auto-update, training them to re-authenticate after an unexplained logout — the habit phishing depends on.

### D6 — The last-seen touch runs inside the request's own transaction, on safe methods only, and is skipped otherwise

**Revised after review.** A validated request updates `last_seen_at` only when the stored value is older than `settings.session_touch_interval_seconds` (default 60), **and only on `GET`/`HEAD`**. The `UPDATE` is issued on the **request's own** `AsyncSession` inside `get_active_session_user` and committed there and then, before the handler body runs. On any other method, or if the update or its commit raises, the touch is skipped: the session's own transaction is rolled back to a clean state, a WARNING is logged, and the request proceeds.

*Why not a second `AsyncSession` (the first draft):* it holds **two pool leases for the duration of the request**. With `pool_size=5, max_overflow=10` the pool tops out at fifteen; fifteen concurrent requests whose sessions happen to be stale would take thirty leases, and everything else on the process — MCP tool calls, `/token`, the indexer — waits `pool_timeout=30` and then 500s. A telemetry field must not be able to take the server down.

*Why `GET`/`HEAD` only, and why commit immediately:* the alternative — leaving the `UPDATE` uncommitted for the handler to carry — creates a deadlock class. The touch takes a row lock on the actor's `user_sessions` row; a mutating panel handler then waits on the account-guard advisory lock (D12), which an administrator may be holding while trying to revoke that same actor's sessions. A waits on the advisory lock, B waits on A's row lock. Committing the touch **in the dependency, before the handler starts**, releases the row lock before any lock is requested, and restricting it to safe methods means the commit can never commit partial handler work — on a `GET` the handler has not run and nothing else is pending on that session.

*Why skipping is acceptable:* `last_seen_at` is telemetry throttled to once a minute; nothing authorizes on it. A user who only ever POSTs without a GET in between is not a real session.

### D7 — Revocation rides the caller's transaction; the helper never commits

`revoke_user_sessions(session, user_id)` and `revoke_session(session, sid_hash)` issue `UPDATE user_sessions SET revoked_at = now() WHERE … AND revoked_at IS NULL` on the `AsyncSession` they are handed and **do not commit**. The calling handler commits.

*Why:* `reset_password`, `edit_user_submit` and `delete_user` hold a transaction-scoped advisory lock, and the documented rule is that nothing may commit between taking it and writing the flags it protects. A helper that committed would silently break the last-admin guard from inside. The same shape keeps the hash write and the revocation in the password change atomic.

`WHERE revoked_at IS NULL` means a second revocation does not rewrite a historical revocation time.

### D7a — The mint runs inside the account guard, against a freshly read active account, and commits there

**Added after review round 1, tightened after round 2.** `start_session(request, session, user_id)` is one critical section:

1. take the **account guard** — the same advisory key the administrative handlers and the password change take (D12);
2. re-read the user `SELECT … FOR UPDATE` with **`execution_options(populate_existing=True)`**;
3. refuse unless the row exists and `is_active` is exactly true;
4. insert the session row;
5. **commit**, which releases the guard;
6. only then write `sid` into the cookie.

*Why it commits (round 1):* an insert nobody is required to commit is an insert that silently does not happen. `get_session` neither commits nor rolls back, so a cookie handed to the browser beside an uncommitted row authenticates nothing — a hard logout loop on the very next request. Making the helper own the commit makes "the row exists before the cookie leaves" a property of one function rather than of three call sites' discipline. This is the deliberate asymmetry with D7: the mint owns a transaction, the revoke helpers ride someone else's.

*Why it must be **inside** the guard (round 2):* every mint in the first draft happened after its guard-holding transaction had already committed and released — the login mint held no guard at all, and the post-change mint ran in a second transaction with no re-check. So an administrator's deactivation committing during a paused login, or between a password change's commit and its re-issue, would be followed by an insert creating a **live row for an account that had just been disabled**. Validation refuses that row while `is_active` is false, which hides the defect; the row is still there, and the day the account is **reactivated** it becomes a working credential the administrator never granted and never saw. Taking the guard and re-reading under it makes the mint serialize with the very handlers that deactivate, so the insert either precedes the deactivation (and is revoked by it) or never happens.

The three callers, each placed so that taking the guard and committing is safe:

| Caller | Placement |
| --- | --- |
| `login_submit` | after the existing `last_login_at` commit; the mint's guarded transaction is its own |
| `register_submit` | **after** the bootstrap transaction commits — never inside it, because that transaction holds `USER_BOOTSTRAP_LOCK_KEY` and must not be lengthened. The two keys are taken sequentially, never nested, so no cycle is introduced |
| the password-change handler | in a second guarded transaction after the change has committed (D13) |

Two regressions are stated as requirements: the cookie a mint hands back authenticates the **very next** request, and a deactivation racing a mint leaves **no row at all** — checked again after a subsequent reactivation, which is where a row created in that window would otherwise come to life.

### D8 — Logout that cannot revoke still logs you out, and records only a class name

If the `UPDATE` or its commit raises, `logout` attempts a rollback, records an ERROR, and still clears the cookie and redirects to login. **The rollback itself is guarded**: a failing rollback must not escape and turn the sign-out into a 500. The record carries the exception's **class name only** — never `str(exc)`, never `exc_info`. SQLAlchemy renders the failing statement *and its bound parameters* into the error text and the engine does not set `hide_parameters`; on this path one of those parameters is the session-identifier hash.

*Why not fail closed with a 500:* it leaves the user signed in *and* the cookie alive, which is worse than the state we are leaving. Clearing the cookie removes it from this browser — the common case, a person leaving a shared machine. The replay window survives only for a copy already taken, which is what the ERROR is for.

### D9 — Purge retains until seven days past the *later* of expiry and revocation

**Revised after review.** `DELETE FROM user_sessions WHERE expires_at < cutoff AND (revoked_at IS NULL OR revoked_at < cutoff)`, where `cutoff = now() - session_purge_retain_days`.

*Why not the single `expires_at` comparison of the first draft:* the OAuth argument it copied ("a token can only be revoked while it exists, so `R <= expires_at`") does **not** hold here. A family revocation in OAuth flips already-expired rows, and the same is true of sessions: an administrator resetting a password revokes every unrevoked row, expired ones included. Such a row is already past `expires_at` and would be purged on the next tick — deleting the record of a revocation minutes after an operator performed it, which is the #64 blank space this rule exists to prevent. Taking the later of the two timestamps makes the guarantee unconditional: a revoked row is readable for the full window after the revocation, whenever it happened.

Revoked-but-unexpired rows are still not deleted early, for the same reason.

The function keeps its name, `cleanup_expired_tokens`. "Tokens" there already means "dead credential rows on one schedule"; renaming churns a 3,400-line module and its call site for a word.

### D10 — Password policy: one constant, 12 characters, no composition rules, every setter

`MIN_PASSWORD_LENGTH = 12` lives in `src/auth/passwords.py` beside the hasher, with `validate_new_password(new, confirm=None) -> str | None` returning a user-facing message or `None`. It checks length, the confirmation match, and the NUL byte.

**Four setters, not three** (revised after review): the self-service handler, `register_submit`, the administrator `reset_password`, **and the administrator `create_user`** — which today accepts eight characters and passes its input straight to `hash_password`, so a NUL in the initial-password field is an unhandled `ValueError` and a 500.

*Why raise the existing minimums:* leaving them at 8 means an administrator can set a password its owner is then forbidden from re-setting, and it leaves the bootstrap admin — the most privileged account on the server — under the weakest rule. *Why no composition rules:* they push users to `Password1!`; length is the control that works. *Why no forced rotation:* nothing re-checks policy at login, so existing accounts keep working.

*Why the NUL check belongs in the validator:* `hash_password` **raises `ValueError`** on an embedded NUL, preserving passlib's policy — a documented rule that must not be "fixed". Routing all four setters through the validator turns four latent 500s into form errors. The 72-byte truncation and the NUL rejection themselves are untouched.

### D11 — Two independent limits on the password-change route: one per account, one per address

**Revised after review.** The handler carries **both** `@limiter.limit("5/minute", key_func=session_user_key)` and `@limiter.limit("5/minute", key_func=get_remote_address)`.

*Why two:* the first draft's single composite `(ip, user)` key was claimed to bound guessing "even across IP rotation", and it does not — rotating the address changes the key and hands the attacker a fresh allowance every time. An account-keyed limit bounds guessing against **one account regardless of address**; an address-keyed limit bounds an attacker walking many accounts from one place. Neither subsumes the other, and stacked decorators are how slowapi expresses both.

Successful and refused attempts count against the same limits, so the allowance cannot be drained by guessing.

`request.session` is parsed by `SessionMiddleware` before the route runs, so the synchronous `key_func` can read the account from it; a missing `SessionMiddleware` (test harnesses) degrades to a constant rather than raising.

**Deviation from the brief, stated plainly:** the brief asked for the *login* limiter to be keyed on username as well as address. That is not implementable with slowapi as wired here — the login username is a form field, the `key_func` is synchronous and receives only the `Request`, and reading the body there would consume the stream the handler needs. `login_submit`'s `5/minute` stays address-keyed. Re-keying login belongs to a change that revisits the limiter's storage and middleware, and is a non-goal here.

### D12 — The password change locks and re-reads the account before it verifies or writes

**Added after review; this was the first draft's worst defect.** The handler received its `User` from a FastAPI dependency, verified the submitted current password against *that* object's hash, and wrote to it. Between the dependency's read and the write, an administrator's reset or deactivation can commit — and the self-change would then overwrite a just-reset password with one derived from a stale verification, resurrecting access the administrator had just removed.

The handler therefore:

1. takes the **account guard** — the same transaction-scoped advisory key `_lock_admin_guard` already uses, because two keys do not exclude each other;
2. re-reads the acting user `SELECT … FOR UPDATE` with **`execution_options(populate_existing=True)`** — without it SQLAlchemy returns the dependency-loaded object's pre-lock attribute values and the re-read proves nothing;
3. refuses if the row is gone or `is_active` is false — the actor may have been deactivated while this request queued, which is precisely the window the lock creates;
4. verifies `current_password` against the **freshly read** hash;
5. writes the new hash, increments `session_version`, revokes every session of that user;
6. commits — which releases the lock.

**Where the shared key lives.** `_lock_admin_guard` is in `src/control_panel/users.py`, which imports from `src/control_panel/routes.py`; the handler lives in `routes.py`, so it cannot import back. The key and a `lock_account_guard(session)` helper therefore move to `src/oauth/grants.py`, which is already this codebase's home for advisory-lock primitives (`USER_BOOTSTRAP_LOCK_KEY`, `lock_user_bootstrap`, `lock_grant`) and is already imported by `src/auth/routes.py`. `_lock_admin_guard` delegates to it with the **value unchanged** — it is a wire constant, and changing it would un-serialize the guard during a rolling deploy.

**Lock order is unchanged and acyclic.** The password change takes the account guard and nothing else; the admin handlers take the account guard and nothing else; the bootstrap takes the bootstrap key and the OAuth handlers take bootstrap-then-grant. No path takes two of these in opposing orders.

### D13 — A password change revokes every session and re-mints the current one

In the transaction of D12: new hash, `session_version += 1`, `revoke_user_sessions(session, user.id)` — **all** rows, this one included — then commit. Then, in a second **guarded** transaction (D7a), `start_session(...)` re-takes the account guard, re-reads the account, requires it to still be active, mints a fresh identifier and row and commits; `SessionMiddleware` writes the new cookie on the redirect. The guard is released between the two transactions, so the re-check in the second is not redundant: an administrator can deactivate the account in exactly that gap.

*Why revoke-all-then-remint:* the cookie that was live while the old password was live should not survive the change. Rotating the identifier alongside the password is standard fixation hygiene. The user-visible behaviour is the one asked for — other devices signed out, this browser still signed in.

*Why `session_version` too:* the account-wide switch fires as well, so a cookie that somehow escapes the registry check still fails the version check.

*Failure ordering:* if the mint fails after the change committed, the user is signed out and signs in with the new password. The password change is the durable half and the session the recoverable half.

### D14 — Session validation binds the row to the cookie's user

**Added after review.** Validation refuses, and clears the cookie, when `row.user_id` is not exactly the `user_id` the cookie carries. The two are written together at mint and can only disagree through tampering, a bug, or a row reused across accounts; treating a disagreement as an error rather than preferring one of the two means no path can be talked into authenticating the cookie's claimed user with somebody else's live session.

### D15 — Consent renders the redirect **host**, from `hostname`, never Unicode-decoded — and registration stops accepting hostless or non-ASCII URIs

`urlparse(redirect_uri).hostname`, lower-cased.

*Why `hostname` and not `netloc`:* `urlparse("https://claude.ai@evil.example/cb").netloc` is `"claude.ai@evil.example"`, whose left edge reads as the brand. `.hostname` strips userinfo and the port and answers `"evil.example"` — the host the code is actually delivered to.

*Why never Unicode-decoded:* a homograph host rendered decoded defeats the control entirely. The value is displayed exactly as the ASCII/punycode form; a host that is not ASCII is displayed `xn--`.

**Registration is brought into scope** (added after review). `_valid_redirect_uri` today checks `p.scheme == "https" and bool(p.netloc) and not p.fragment`, so `https://@/cb` registers — its `netloc` is `"@"` and truthy while its `hostname` is empty, which would render a consent card with **no destination at all**. Registration therefore additionally requires a **non-empty `hostname`** and a **successful IDNA-to-ASCII conversion** of it, so a non-ASCII host is normalised to its A-label at registration rather than at display and cannot be stored in two forms that compare differently. The display path still degrades safely — an empty or unresolvable host renders as an explicit "destination could not be determined" and always takes the warning branch, never the badge — because rows registered before this change exist.

The card also shows the `client_id` (server-generated hex, not client text) and the client's `created_at`.

### D16 — The allow-list matches exact hosts only

`OAUTH_KNOWN_REDIRECT_HOSTS`, default `claude.ai,chatgpt.com`, parsed by the `NoDecode` + CSV/JSON validator `fts_configs` already uses. `src/oauth/trust.py` holds one function: lower-case the URL's `hostname` and compare for **equality** against the lower-cased configured entries.

*Why exact:* a suffix test is the bug. `endswith("claude.ai")` matches `evilclaude.ai`; `"claude.ai" in host` matches `claude.ai.evil.example`; even `endswith(".claude.ai")` hands the badge to any subdomain an attacker can get, and neither real connector needs one.

The validator **strips outer whitespace** from each entry (an operator writing `claude.ai, chatgpt.com` means two hosts) and **rejects** an entry containing `*`, `/`, `@` or *internal* whitespace, so a pattern is refused loudly instead of quietly matching nothing.

*Why the default is these two:* they are the connector hosts this deployment serves. The list is additive — an operator adding `chat.openai.com` writes one env value — and an unlisted host degrades to the warning, never the reverse, so a stale list is safe and a wrong one is loud. **An empty list means everything is unverified.**

### D17 — The self-registration notice is unconditional; the badge is about the destination only

Every consent render — allow-listed or not — carries the notice that the application registered itself, that registration is open, and that the displayed name was chosen by the application.

*Why unconditional:* `/register` is open and the name is the first thing a user reads. The badge is worded as a statement about the redirect destination ("this server's operator lists this destination as a known connector"), never "this application is verified", because nothing verifies it.

Non-allow-listed clients additionally get a warning naming the host, stating the authorization code will be sent there, and advising Deny unless the user began the flow from that application.

### D18 — Migration 024 owns its table the way 016/017/022 own theirs

**Revised after review.** A bare `create_table` is not the house shape. 024 carries a module-level `MARKER` string, stamps it as a `COMMENT ON TABLE` in the same transaction as the create, and mirrors it in the model's `__table_args__` so `alembic check` compares it like any other attribute.

On a database where `user_sessions` already exists, 024 **verifies the complete shape it would have created** — every column's type and nullability, the `ON DELETE CASCADE` on the foreign key resolved through `pg_constraint` (`confrelid`, `confdeltype`) rather than by name, and each index resolved through `pg_index` — and **refuses** a foreign or partial shape rather than patching it. A table of that name somebody else created is not this migration's table, and adopting it would leave the registry running against a schema nobody verified. `downgrade()` drops the table **only if it carries 024's marker**.

A stamp-back re-run (the gate does `alembic stamp <prev>` then `upgrade head`) must **preserve existing rows**: the reconciliation path writes nothing, so live sessions survive a re-run and are not silently logged out by a gate exercise.

### D19 — Event names come from the catalogue in `security_events`; no credential material, ever

**Revised after review: the sibling `security-event-logging` change (PR #216) has merged its proposal and its Slice A is being implemented, so `src/services/security_events.py` is assumed to exist.** This change **declares its own events in that module's `EVENT_FIELDS`** rather than emitting past it, and that declaration is owned by one integration slice so two changes cannot edit the registry concurrently.

Added events: `panel_session_replay_refused` (WARNING; `reason` ∈ `no_session_id` | `unknown_session` | `revoked_session` | `expired_session` | `user_mismatch` | `user_missing` | `user_inactive` | `version_mismatch`, `user_id`, `token_tag`, `route`, `client_ip`), `panel_sessions_revoked` (INFO; `reason` ∈ `logout` | `password_change` | `admin_password_reset` | `user_deactivated` | `user_deleted`, `user_id`, `count`), `panel_password_changed` (INFO; `user_id`, `username`, `client_ip`, `route`), `panel_password_change_refused` (WARNING; `reason` ∈ `wrong_current_password` | `too_short` | `mismatch` | `same_as_current` | `nul_byte` | `account_inactive`, `user_id`, `client_ip`, `route`). `panel_logout` is already in the catalogue. Every field used is already in that change's `ALLOWED_FIELDS`.

**The secret-absence rule, aligned with that change's canary test:** no record may contain the session identifier, **its stored SHA-256**, a password, or a password hash — nor **any substring of any of them twelve characters or longer** — in any field, message or traceback. The hash is included deliberately: it is the database key, and a log line carrying it is a log line that names a specific live session. Where a record must identify a session it uses `token_tag`, the catalogue's `"sha:" + sha256(value).hexdigest()[:8]`. Success records are emitted **after** the commit that makes them true.

If the integration slice finds `security_events` absent at merge time, the same names go through a module logger with the identifiers **in the message text** (the deployed formatter is `%(message)s`), and the substitution later is mechanical.

### D20 — Configuration ranges are enforced, not assumed

`session_touch_interval_seconds: int = Field(60, ge=1)` and `session_purge_retain_days: int = Field(7, ge=1)`. A zero or negative touch interval turns a throttled hint into a write on every request; a zero or negative retention window deletes a revocation the moment it is made, which is exactly the #64 failure D9 exists to prevent. Both fail at settings construction, so a bad value stops the container rather than degrading it silently.

## Session lifecycle

| Phase | Trigger | What happens | Where |
| --- | --- | --- | --- |
| **Mint** | successful login; bootstrap admin registration; re-issue after a password change | **under the account guard**: re-read the user `FOR UPDATE` with `populate_existing=True`, require it to exist and be active, then `sid = secrets.token_urlsafe(32)` and a row with `id = sha256(sid)`, `user_id`, `created_at`, `last_seen_at`, `expires_at = now + session_max_age`, `revoked_at = NULL`, `user_agent_hash`; **the helper commits**, releasing the guard, and only then writes `sid` into the signed cookie beside `user_id` / `session_version` / `is_admin` / `username` | `start_session()` in `src/auth/session.py`; three callers, one implementation, each placed where taking the guard and committing is safe (D7a) |
| **Validate** | every request that resolves a browser identity | cookie must carry **both** `user_id` and `sid`; row read by `sha256(sid)`; refused when the row is absent, `revoked_at` is set, `expires_at <= now`, **`row.user_id != cookie user_id`** (D14), the user is missing or inactive, or `session_version` disagrees. Every refusal clears the cookie and records `panel_session_replay_refused` | `get_active_session_user()`; reached from `require_user_panel` (panel, `/api`, `/admin/users`), `login_form`, `authorize_get`, `authorize_post` — the only four entry points |
| **Touch** | a `GET`/`HEAD` request whose row is more than `session_touch_interval_seconds` (60) stale | `UPDATE … SET last_seen_at = now() WHERE id = :h AND revoked_at IS NULL` on the **request's own** session, committed in the dependency before the handler runs; skipped on every other method and on any failure (D6) | `touch_session()`, called from `get_active_session_user` |
| **Revoke — one** | logout | `revoked_at = now()` for this row, commit, cookie cleared, `panel_logout` + `panel_sessions_revoked(reason=logout)`; a failure still clears and redirects, logging the exception class only (D8) | `logout` in `src/auth/routes.py` |
| **Revoke — all of a user** | self-service password change; admin password reset; deactivation through the user edit form; soft delete | `UPDATE … SET revoked_at = now() WHERE user_id = :u AND revoked_at IS NULL`, **in the caller's transaction**, under the account-guard advisory lock, with no commit between the lock and the write (D7, D12) | `change_password`; `reset_password`, `edit_user_submit`, `delete_user` |
| **Revoke — implicit** | permanent user delete | rows removed by the database's `ON DELETE CASCADE`; the ORM relationship declares `passive_deletes=True` so `session.delete(user)` does not load and delete them one by one (D21 below) | `delete_user` |
| **Purge** | every indexer tick, in **both** modes | `DELETE … WHERE expires_at < cutoff AND (revoked_at IS NULL OR revoked_at < cutoff)`, `cutoff = now() - session_purge_retain_days` (D9) | `cleanup_expired_tokens()` in `src/services/indexer.py` |

**D21 — the relationship is `passive_deletes=True` with `cascade="all, delete"`.** Without `passive_deletes`, SQLAlchemy emits a `SELECT` of every child row and individual `DELETE`s (or, worse, `UPDATE … SET user_id = NULL`, which the NOT NULL column rejects) when `session.delete(user)` runs — so the database cascade the migration installs would never be the thing that fires, and a divergence between the ORM's behaviour and the schema's would go unnoticed until the ORM path was removed. The permanent-delete test exercises the **real handler**, not `session.delete` in isolation.

Two properties the table is meant to make checkable: **mint has three sites and one implementation**, and **validate has four entry points and one implementation**. A fifth entry point reading `request.session["user_id"]` directly is the defect this change removes from `login_form`, and is the shape of its regression.

## The password-change flow

```
POST /admin/account/password   (panel router: require_user_panel + verify_csrf)
  ├─ throttle 5/min per account  AND  5/min per client address              [D11]
  ├─ multi_user_mode? no  → 404 (the sentinel has no account)
  ├─ validate_new_password(new, confirm)  → message? → flash + 303 back     [D10]
  ├─ ONE TRANSACTION:                                                       [D12]
  │    lock_account_guard(session)                  ← same key as _lock_admin_guard
  │    re-read acting user FOR UPDATE, populate_existing=True
  │    row gone or not is_active?          → rollback, flash, 303, sign out
  │    verify_password(current, fresh.password_hash)  false? → rollback, flash, 303
  │    verify_password(new,     fresh.password_hash)  true?  → rollback, flash, 303
  │    password_hash = hash_password(new)
  │    session_version += 1
  │    revoke_user_sessions(session, user.id)     ← all, including this one  [D13]
  │    commit                                     ← releases the lock
  ├─ panel_password_changed  (after the commit)
  └─ SECOND GUARDED TRANSACTION: start_session(...)                          [D7a]
       lock_account_guard  →  re-read FOR UPDATE  →  still active?
       no  → no row is minted; the user is simply signed out
       yes → insert + commit → new sid, durable row, new cookie
     303 → /admin/account  with a flash
```

Every refusal returns the same shape — flash on the session, 303 back to `/admin/account`, never a query string (`#138`) — and the credential refusal carries one constant message. Refusals count against the same limits as successes.

**One exception, deliberately.** A request rejected by either rate limit never reaches the handler: slowapi's `RateLimitExceeded` is answered by the application-wide `_rate_limit_exceeded_handler` (`src/main.py:299`) with its own JSON 429. That response is **exempt** from the flash-and-303 rule and stays as it is. Wrapping it would mean either duplicating the limiter's decision inside the handler — where it is no longer a limit but a second, divergent counter — or replacing a process-wide error handler for one route. A 429 is also the one refusal a caller should be able to read programmatically, and it carries no message an attacker chose. The user-visible cost is that the sixth attempt in a minute renders as JSON rather than as the account page with a flash; the account page is one back-navigation away, and the limit is five attempts per minute.

## The consent card

| Element | Source | Notes |
| --- | --- | --- |
| Application name | `client.client_name` | client-supplied text, escaped; already the case |
| **Redirect destination** | `urlparse(redirect_uri).hostname`, lower-cased ASCII | host only — no scheme, path, port or userinfo (D15). Empty or unresolvable → explicit "could not be determined" **and the warning branch** |
| **Client identifier** | `client.client_id` | server-generated hex |
| **Registered** | `client.created_at` | date only |
| **Standing notice** | static | unconditional (D17) |
| **Known-client badge** | `redirect_host ∈ OAUTH_KNOWN_REDIRECT_HOSTS` | worded about the *destination*, not the application |
| **Unverified warning** | otherwise | names the host, states the code will be sent there, advises Deny |

Colours come from existing `--consent-*` tokens. If a token is added it goes into **all three** blocks of `_theme.html`.

`authorize_post` is unchanged: it already re-validates `redirect_uri` against the client's registered list before minting anything, so the destination shown on the GET is the destination the code goes to.

## Risks

| Risk | Mitigation |
| --- | --- |
| A defect in `get_active_session_user` locks every user out of the panel | The browser pass before archive exercises login → panel → logout → login. Recovery is an image rollback; 024 is additive and needs no downgrade for the previous image to run. |
| Pool exhaustion from a per-request second lease | Removed by construction (D6): no path takes two leases. A concurrency test drives more concurrent stale-session requests than `pool_size + max_overflow` and asserts none time out. |
| A stale-read password change resurrects access an administrator just removed | The account guard, the `populate_existing` re-read and the `is_active` re-check (D12), with explicit concurrent-reset and concurrent-deactivation scenarios. |
| A cookie is issued beside a row that was never committed | The mint helper commits (D7a), and the requirement is "accepted on the very next request". |
| `alembic check` goes dirty because the model and 024 disagree | `make test-schema` on a throwaway container, asserting the catalog directly — `alembic check` compares neither CHECK predicates nor server defaults. |
| 024 merges ahead of the sibling's 023 | `down_revision = "023"` (the `index-integrity-hardening` migration); the ordering is a stated merge precondition, with the rebase named if 023 is abandoned. |
| Two changes edit `security_events`' `EVENT_FIELDS` concurrently | One integration slice owns that file here, sequenced after the sibling's Slice A. |
| A colour literal in the new markup fails the sweep | The sweep is in the task list, and must report a **non-zero** template count (#170). |
| The allow-list is edited to something broad | The validator rejects `*`, `/`, `@` and internal whitespace, and matching is equality, so a broad-looking entry matches nothing. |

## Non-goals

- Email- or token-based password recovery. The admin reset stays the recovery path.
- An "active sessions / sign out everywhere" panel page. The registry makes it buildable; it is not built here.
- Replacing the cookie transport. `SessionMiddleware` stays.
- Re-keying the **login** throttle (D11).
- A real client-verification scheme for OAuth. The badge is an operator's allow-list of destinations and says so.
- Multi-factor authentication.
- Changing anything about `omcp_` API keys, OAuth tokens, or transfer capabilities.

## Accepted limitations

| Limitation | Why it is accepted |
| --- | --- |
| The deploy signs every live panel session out once | Grandfathering keeps the #198 window open another seven days. Two users, two logins. (D4) |
| `last_seen_at` may be up to 60 s stale, and is not recorded at all for a session that only ever POSTs | It is telemetry; nothing authorizes on it, and the alternatives are a second pool lease or a deadlock class. (D6) |
| Revocation takes effect at the *next* request; one already in flight completes | Identical to the posture documented for OAuth revocation. Closing it means holding a lock across arbitrary handler work. |
| No user-agent or IP binding | Both are replayed by whoever holds the cookie and both cause spurious logouts. (D5) |
| The login throttle stays address-keyed | slowapi's `key_func` is synchronous and cannot read the form body. (D11) |
| Both throttles are per-process, in slowapi's in-memory storage, and reset on restart | One uvicorn worker, so per-process is per-server today. A shared store for two routes is a larger change than the gap. |
| Existing accounts are not forced up to the 12-character minimum | Nothing re-checks policy at login. (D10) |
| Redirect URIs registered **before** this change may carry a non-ASCII or empty host | Registration is hardened going forward; the display path degrades to "could not be determined" and always warns, so no such row can earn a badge. (D15) |
| The known-client badge attests the redirect **host**, not the application | Nothing on this server verifies an application. (D17) |
| An allow-listed client still shows the self-registration notice | The notice is the finding; suppressing it restores the page #183 describes. |
| A stolen cookie still works until logout, expiry, or an account event | This change makes those three effective; it does not detect theft. |
| The account guard serializes self-service password changes, **every session mint**, and admin user-management writes against each other | One shared key is what makes the check-then-act atomic; two keys do not exclude each other. Every login now waits on it, so the contention set grew — but it is two users deep, the section is one indexed read and one insert, and the alternative is the deactivation race of D7a. |
| The sixth password-change attempt in a minute renders as slowapi's JSON 429 rather than as the account page with a flash | Duplicating the limiter's decision inside the handler makes it a second, divergent counter; replacing the process-wide handler for one route is worse. (D25) |

## Owner decisions

1. Cookie carries a CSPRNG identifier; the table stores its SHA-256. (D1)
2. `session_version` is kept as an account-wide second switch. (D2)
3. Absolute 7-day expiry; no sliding renewal. (D3)
4. Pre-deploy cookies are refused, not grandfathered. (D4)
5. `user_agent_hash` is forensic only. (D5)
6. **The touch runs in the request's own transaction, on `GET`/`HEAD` only, committed in the dependency, and is skipped otherwise** — never a second pool lease. (D6)
7. Revocation helpers never commit. (D7)
8. **The mint runs inside the account guard against a freshly re-read, still-active account, and commits there** — a mint after the guard is released can create a live row for an account an administrator has just disabled, which comes to life on reactivation. (D7a)
9. A failing logout still clears the cookie and redirects; the rollback is guarded and only the exception class is recorded. (D8)
10. **Purge retains until seven days past the later of `expires_at` and `revoked_at`.** (D9)
11. Password minimum is 12, in one constant, applied by **four** setters including `create_user`; no composition rules; no forced rotation. (D10)
12. **Two independent limits** on the change route — per account and per address. (D11)
13. **The password change takes the shared account guard, re-reads `FOR UPDATE` with `populate_existing=True`, and re-checks `is_active`** before verifying or writing; the key moves to `src/oauth/grants.py` with its value unchanged. (D12)
14. A password change revokes every session and re-mints the current one, and bumps `session_version`. (D13)
15. **Validation refuses a row whose `user_id` disagrees with the cookie's.** (D14)
16. Consent shows `urlparse().hostname`, ASCII/punycode, never `netloc`, never decoded; **registration additionally requires a non-empty host and a successful IDNA conversion**. (D15)
17. Allow-list is exact-host equality; entries are stripped of outer whitespace and rejected for `*`, `/`, `@` or internal whitespace; empty list = everything unverified. (D16)
18. The self-registration notice is unconditional; the badge speaks only of the destination. (D17)
19. **024 is marker-owned**: exact reconciliation, refusal of a foreign or partial shape, marker-guarded downgrade, rows preserved across a stamp-back re-run. (D18)
20. Events are **declared in `security_events`' catalogue** by one integration slice; no identifier, stored hash, password or hash — nor any ≥12-character substring — reaches a record. (D19)
21. Config ranges are enforced (`ge=1` on both). (D20)
22. `User.sessions` is `passive_deletes=True` with `cascade="all, delete"`, and the cascade is tested through the real handler. (D21)
23. New panel page is `/admin/account`, `require_user_panel` (not admin), linked from the sidebar's Access section — and **both** the `GET` and the `POST` answer 404 in single-user mode. The first draft rendered the page without its password card there; a page whose only content is a form that cannot exist is not a page, and the sidebar entry is already gated on `multi_user_mode`. One rule for both methods is also one thing to test.
25. **Rate-limit rejections are exempt from the flash-and-303 rule**: slowapi's JSON 429 from the application-wide handler stands, because the alternative is a second, divergent counter inside the handler or a process-wide error handler replaced for one route.
24. 024 chains from **023** (`index-integrity-hardening`) and must not merge ahead of it; the schema gate's head requirement is **modified**, not duplicated, to move `017 → 024`.

## Review round 1 (Codex, pre-code) — findings and where they went

FAIL: 3 BLOCKER, 11 MAJOR, 3 MINOR. All folded; none rejected.

| Finding | Disposition |
| --- | --- |
| BLOCKER — self-change verifies and writes from a dependency-loaded `User` with no locked re-read | D12; new spec requirement and concurrent-reset / concurrent-deactivation scenarios |
| BLOCKER — `start_session` inserts a row nobody must commit | D7a; per-caller placement table; "accepted on the very next request" scenario |
| BLOCKER — `touch_session` on a second `AsyncSession` holds two pool leases | D6 rewritten to the owner's decision; pool-capacity concurrency test |
| MAJOR — `(ip, user)` key does not bound guessing across address rotation | D11: two independent limits |
| MAJOR — `register_client` / `_valid_redirect_uri` accept `https://@/cb` and Unicode hosts | D15; new registration requirement, both cases tested |
| MAJOR — `create_user` outside the password policy | D10: four setters |
| MAJOR — `User.sessions` relationship unspecified | D21: `passive_deletes=True`, tested through the real handler |
| MAJOR — a revocation after expiry is purged immediately | D9: `max(expires_at, revoked_at)`; expired-then-revoked scenario |
| MAJOR — schema-gate head duplicated rather than modified | `schema-integrity` delta now carries a `## MODIFIED Requirements` block moving `017 → 024`, ordering stated |
| MAJOR — 024 not in 022/023's marker-owned shape | D18 |
| MAJOR — no slice owns the event-catalogue additions or the existing tests that must change | New slice 7 (integration), with the affected modules enumerated |
| MAJOR — validation does not bind the row to the cookie's user | D14 |
| MAJOR — logout failure path unguarded; may log bound parameters | D8 |
| MAJOR — secret-absence too narrow | D19: identifier **and its stored hash**, ≥12-character substrings |
| MINOR — config ranges unenforced | D20 |
| MINOR — "single-user mode never reads `user_sessions`" contradicts the purge | Context section and the spec now exempt maintenance cleanup |
| MINOR — allow-list whitespace | D16 |

## Review round 2 (Codex, pre-code) — findings and where they went

FAIL: 1 BLOCKER, 1 MAJOR, 2 MINOR, with round 1's items accepted as resolved. All folded; none rejected. This was the final spec round.

| Finding | Disposition |
| --- | --- |
| BLOCKER — the mint runs after the guard is released and with no fresh active-account check, so a paused login or a post-change re-issue can create a live row for an account an administrator just deactivated, which authenticates once the account is reactivated | D7a rewritten: every mint takes the account guard, re-reads `FOR UPDATE` with `populate_existing=True`, requires the account to exist and be active, and inserts and commits inside that critical section. Both race regressions specified, including the reactivation check |
| MAJOR — slice 7 misses the two test modules the guard-key move and the row-counted `revoke_user_sessions` break, and does not name the shared mint helper | `tests/test_issue_69_self_edit_role_lock.py` and `tests/test_issue_90_self_delete_refused.py` added to slice 7's inventory; the helper is `tests/session_helpers.py`, owned by that slice |
| MINOR — `GET /admin/account` in single-user mode | Owner decision 23: **404 for both methods**; the page has no purpose there. Decision, task and scenario aligned |
| MINOR — rate-limit responses versus the flash-and-303 rule | D25 and a spec exemption: slowapi's JSON 429 stands, with the reasons and a sixth-request test |
