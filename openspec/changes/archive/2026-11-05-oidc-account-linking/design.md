# Design: OIDC account linking (#416)

## Context

#415 shipped `src/auth/oidc.py`: Authlib's `AsyncOAuth2Client` with PKCE S256,
joserfc signature verification, Authlib `CodeIDToken` claim validation, a
`BoundedTransport` (no environment proxies, no redirects, identity encoding,
128 KiB bodies), and an in-memory single-use transaction store bound to the
browser session (5 min TTL, 1,024 slots, 60 starts/min/process). The callback
does all network I/O before it touches the database, then mints through
`start_session`. This change keeps all of that. It changes **who the verified
identity resolves to** and **how many providers pass the profile**.

Reading order for implementers: `docs/native-oidc.md`, the archived
`2026-11-02-native-oidc-login` change, `src/auth/oidc.py`,
`src/auth/routes.py::oidc_callback`, `src/auth/session.py::start_session`, and
`src/control_panel/users.py` (the admin critical section and
`revoke_user_sessions`).

## Goals / Non-goals

Goals: admin-managed linking in the panel; first-login binding pinned to
`(issuer, sub)`; standards-compliant providers beyond Keycloak; byte-identical
behaviour with OIDC off.

Non-goals: provisioning; role or group mapping onto `is_admin`; provider
logout; storing provider tokens; using the provider as the MCP authorization
server; multiple simultaneous issuers.

## Decisions

### D1. Link state lives on `users`, not in a new table

Migration **037** (`037_oidc_account_links.py`; 036 is the current head) adds
six nullable columns to `users`:

| column | type | meaning |
| --- | --- | --- |
| `oidc_issuer` | `VARCHAR(2048)` | issuer the subject is bound under (exact `iss`) |
| `oidc_subject` | `VARCHAR(255)` | the bound `sub` |
| `oidc_linked_at` | `TIMESTAMPTZ` | when the binding was made |
| `oidc_expected_issuer` | `VARCHAR(2048)` | issuer the pending expectation was set under |
| `oidc_expected_email` | `VARCHAR(254)` | normalized expected email |
| `oidc_expected_set_at` | `TIMESTAMPTZ` | when the admin set it (drives expiry) |

Constraints, all named, all declared on the ORM model so that `alembic check`
stays clean:

- `uq_users_oidc_identity UNIQUE (oidc_issuer, oidc_subject)`. One identity
  maps to at most one user. PostgreSQL treats NULLs as distinct, so unlinked
  rows don't collide.
- `uq_users_oidc_expected UNIQUE (oidc_expected_issuer, oidc_expected_email)`.
  Two users cannot wait for the same address, so a match is never ambiguous.
- `ck_users_oidc_bound_complete`: the three bound columns are all NULL or all
  non-NULL.
- `ck_users_oidc_expected_complete`: the same for the three expected columns.
- `ck_users_oidc_not_both`: a row is never bound and pending at once.

Why columns rather than a `user_oidc_identities` table: one identity per user
is the whole requirement, and a table needs a foreign key to `users`. CLAUDE.md
records that an FK to `users` reopened the permanent-deletion deadlock for the
feed tables. With columns, deleting a user removes its link with no extra
purge step, and the user-row lock that every admin action already takes covers
the link too. The email is stored already normalized (D2), so no functional
index is needed. Plain UNIQUE constraints autogenerate cleanly where a
`lower()` expression index would not. The btree row bound: 2,048 + 255 bytes
plus overhead is under PostgreSQL's 2,704-byte limit. `OIDC_ISSUER` is
validated to ≤ 2,048 characters and `sub` to ≤ 255.

Downgrade drops the columns and constraints. That loses the links, which is
stated in the migration docstring. No data migration is needed: #415 kept
mappings in the environment.

**Provenance: marker-owned, reconcile or refuse**, in the shape of 029 and
030 in `docs/architecture/schema-and-migrations.md`:

- **Column markers.** Each of the six columns carries 037's column COMMENT
  marker, `OIDC account link (037_oidc_account_links)`. It is mirrored
  byte-identically in `src/models/db.py` as a module constant
  `_OIDC_LINK_COLUMN_MARKER`, passed as `comment=` on each `mapped_column`, so
  `alembic check` compares it. A drift shows up as a pending
  `alter_column(comment=…)`. The migration's `COLUMN_MARKER` and the model
  constant must stay byte-identical.
- **Constraint markers.** Each of the five constraints carries 037's
  constraint-comment marker, `OIDC account link constraint
  (037_oidc_account_links)`. Autogenerate does not compare constraint
  comments, so the gate checks them through the catalogue.
- **Adopt only an exact match, else refuse. All six columns and all five
  constraints are checked before anything is touched.** Re-running over a
  same-named column adopts it only if it has the exact type
  (`character varying(2048)` / `(255)` / `(254)` / `timestamp with time
  zone`), is nullable, has no default and carries the column marker. A
  same-named constraint is adopted only if its `pg_get_constraintdef` is the
  canonical definition, `convalidated` is true and it carries the constraint
  marker. Anything else is refused by name with the disagreement, and nothing
  changes. That includes an object with the right shape but no marker, an
  `oidc_subject varchar(1024)`, a NOT NULL or defaulted column, or a `NOT
  VALID` CHECK. An unmarked object of the right shape has unknown provenance:
  adopting it would let `downgrade()` drop something 037 did not create.
- **`downgrade()` decides each object on its own marker.** It drops a
  constraint only if it carries the constraint marker, and a column only if it
  carries the column marker. Each unmarked object is skipped with a printed
  line. Constraints go before columns.
- `search_path` is pinned to `public` and asserted, then `RESET`.
  `lock_timeout` / `statement_timeout` are set, then `RESET`. Nothing needs
  superuser (`test_nonsuperuser_migrations_pg.py` runs it as the owning role).

`HEAD_REVISION` in `tests/integration/test_schema_check.py` moves `036 -> 037`
as part of the migration, not beside it. The gate's 037 cases:

- a fresh upgrade;
- the chain from 036 with existing users untouched;
- for each column, the catalogue type, nullability, absent default and
  `col_description` equal to the marker;
- for each CHECK and UNIQUE, `pg_get_constraintdef`, `convalidated` and an
  `obj_description` equal to the constraint marker;
- every invalid row shape rejected;
- re-run stamp-back over the exact marked shape changing nothing;
- impostor refusals, atomic across all eleven objects: a wrong type, a
  NOT NULL column, a defaulted column, a wrong constraint definition, a `NOT
  VALID` CHECK, and **the correct shape with no marker** (once for a column,
  once for a constraint);
- downgrade of marked objects, downgrade skipping unmarked ones, and
  downgrade to 036 followed by a clean re-upgrade with `alembic check`
  clean.

### D2. The matching rule: verified email, single use, 7-day window

Owner decision 2 lets the expected identity be `preferred_username` or email.
**We use email, and require `email_verified: true`.**

- OIDC Core §5.7 says of `preferred_username` that the RP "MUST NOT rely upon
  this value being unique". It is a display hint. On Authentik, Zitadel and
  Entra its meaning varies (a username, a login name, a UPN).
- `email` with `email_verified` is the one standard claim pair that carries
  the provider's assertion of control. Keycloak resets `emailVerified` when a
  user changes their email through the account console, so a user who retypes
  their address to someone else's gets `email_verified: false` and is refused.
  Open WebUI on the same Keycloak already keys on email.
- The residual risk is a takeover *during the window*: someone who can
  get a provider account with a verified copy of the expected address before
  the intended person signs in. Three bounds keep it small. The expectation is
  **single use**: the first match binds and clears it. It **expires 7 days**
  after the admin set it (`OIDC_LINK_PENDING_DAYS`, a module constant, not a
  setting). It is **scoped to the issuer** configured when it was set. Once
  bound, a rename, an email change or a reassigned address at the provider
  changes nothing: only `(iss, sub)` is read.
- Normalization: strip surrounding ASCII whitespace from admin input, then
  require 3–254 characters, exactly one `@`, a non-empty local part and domain,
  and no whitespace or control characters. Lower-case **ASCII letters only**;
  non-ASCII stays as entered. Store that form. The claim is compared under the
  same normalization. This is an exact match. No IDN or Unicode folding, which
  would add a homograph surface.
- **Email is never a reason to refuse a bound user.** `exchange()` validates
  the token (signature, `iss`, `aud`, `azp`, `exp`/`iat`, `nonce`, `sub`,
  group) but does **not** judge `email`. It carries the claim as
  `email: str | None`, where anything that is not a string of ≤ 254 characters
  (absent, a number, a list, oversized) becomes `None`. `None` is a
  non-matching sentinel: it equals no stored expectation. `email_verified` is
  carried as `True` only for the JSON boolean `true`, else `False`. The
  string `"true"`, seen from some misconfigured mappers, becomes `False`.
  Normalization, and the `email_verified` check, run **only on the pending
  branch** of resolution, after `(iss, sub)` has failed to match. A bound user
  whose token has no email, or a malformed one, signs in as usual.
- **Direct subject binding** is the escape hatch. An admin who has the `sub`
  (from the Keycloak console) binds it immediately with no email involved. This
  covers providers that put `email` only in UserInfo, accounts with no
  verified email, and operators moving off `OIDC_SUBJECT_USER_IDS`.

Resolution order at the callback, after the ID token is fully validated:

1. A user with `(oidc_issuer, oidc_subject) = (iss, sub)` → that user. Email is
   not read. An active pending expectation elsewhere for the same email does
   not matter.
2. Otherwise, if `email` is not `None` and normalizes, a user whose pending
   `(oidc_expected_issuer, oidc_expected_email)` equals `(iss,
   normalize(email))` and is not expired. If `email_verified` is `True`, bind
   (D4); otherwise refuse `email_unverified`.
3. Otherwise refuse `unlinked`. That covers a `None` or unnormalizable email,
   no matching expectation, and an expired one. The response is the same
   generic page as every other OIDC refusal.

An expired expectation is not deleted on the read path. It simply doesn't
match, and the panel shows it as "expired". Re-arming means setting it again.

### D3. Retiring `OIDC_SUBJECT_USER_IDS`

Two sources of truth for one binding ("the env says sub X is user 1, the row
says user 2") would mean a precedence rule nobody remembers under pressure. The
field stays in `Settings` only so it can be refused. A non-empty value fails
validation, with OIDC on or off, with:

> *OIDC_SUBJECT_USER_IDS is retired (#416). Upgrade: (1) confirm a local admin
> password works; (2) set LOCAL_LOGIN_ENABLED=true; (3) remove
> OIDC_SUBJECT_USER_IDS; (4) start; (5) bind each former subject on its user's
> edit page (Single sign-on → Bind subject); (6) verify SSO and /authorize;
> (7) optionally set LOCAL_LOGIN_ENABLED=false. See docs/native-oidc.md,
> "Upgrading from OIDC_SUBJECT_USER_IDS".*

**The upgrade sequence for an existing #415 deployment**, which may run
`LOCAL_LOGIN_ENABLED=false` with the env map as its only way in:

1. Before upgrading, confirm a local admin password works. If no admin knows
   one, reset it while still on the old release, or out of band.
2. Set `LOCAL_LOGIN_ENABLED=true`. During the upgrade, password login is the
   only way into the panel, because the env bindings are gone and the panel
   bindings don't exist yet.
3. Remove `OIDC_SUBJECT_USER_IDS`. Copy its subjects somewhere first; they are
   needed in step 5.
4. Deploy and start. Migration 037 runs. With password login enabled, the
   lockout CRITICAL (D13) does not apply.
5. Sign in with the password and bind each former subject on its user's edit
   page. Direct subject binding binds immediately, so nothing is re-linked by
   email.
6. Verify an SSO sign-in for each user and one `/authorize` consent round trip.
7. Optionally set `LOCAL_LOGIN_ENABLED=false` again. The boot check (D13) and
   the last-reachable-admin rule (D15) then guard it.

Startup refuses until step 3 is done, so no path runs the new code with
stale env bindings in force. The owner confirmed (2026-11-04) that no
production deployment has #415's OIDC enabled. The sequence is still the
public upgrade contract.

### D4. Binding and minting: one guarded transaction

The callback ends its network phase holding validated claims
(`VerifiedIdentity(iss, sub, email, email_verified)`), not a user id. Then, on
the request's one `AsyncSession`:

1. **Resolve**: unlocked `SELECT` per D2. It identifies the candidate user id
   and which branch matched.
2. **Guard**: `lock_account_guard` (`src/oauth/grants.py`). This is the one
   definition that `_lock_admin_guard` delegates to and `start_session` takes,
   so a bind serializes with link, unlink, deactivation, delete, password
   reset and every mint. The lock is transaction-scoped and re-entrant within
   the session, so `start_session` re-taking it in step 4 is a no-op.
3. **Re-read and write, no commit**: re-read the user `FOR UPDATE` with
   `populate_existing=True`. Re-check that it is active. For the pending
   branch, also re-check that it is still pending with the same
   `(issuer, email)`, not expired, not bound, and that no other row holds
   `(iss, sub)`. Then write `oidc_issuer`, `oidc_subject`,
   `oidc_linked_at = now()` and clear the three expected columns. For the
   bound branch, re-check that the row is still bound to `(iss, sub)`, since
   an unlink may have committed since step 1. Both branches stamp
   `last_login_at`. Any failed re-check rolls back and refuses (`unlinked`,
   `account` or `link_conflict`). A unique violation on
   `uq_users_oidc_identity` at flush rolls back and refuses `link_conflict`.
4. **Mint**: `start_session(request, session, user_id,
   expected_session_version=<version from the step-3 re-read>)`. It commits
   the bind, `last_login_at` and the session row **together**. On a refused
   mint (`session_mint`) the route rolls back explicitly before rendering the
   refusal, so **a refused mint leaves no binding** and no `last_login_at`.
   The implementer confirms whether `start_session` already rolls back on its
   refusal path, and adds the rollback in the route if it does not.

The bind is atomic with the mint, which makes the admin's intent and the
first session one fact: either both exist or neither does.

**No DB connection is held during provider I/O.** This is unchanged from #415.
`get_session` checks out lazily, and every statement comes after `exchange()`.

### D5. Provider profile: standards-compliant, same trust model

| Aspect | #415 | This change |
| --- | --- | --- |
| Endpoint origin | HTTPS, same origin as issuer | **unchanged** |
| Signing alg | RS256 | `{RS256, PS256, ES256}` ∩ discovery `id_token_signing_alg_values_supported` (RS256 if absent, per OIDC Discovery default); never `none`/HS*/others |
| Client auth | `client_secret_basic` | `OIDC_TOKEN_AUTH_METHOD` ∈ {`client_secret_basic` (default), `client_secret_post`}; at login, refused as `provider` when discovery's `token_endpoint_auth_methods_supported` is present and omits it (never checked at boot, D8) |
| Scopes | `openid profile` | `openid email profile` + `OIDC_EXTRA_SCOPES` |
| Group | required | `OIDC_REQUIRED_GROUP` optional; same exact array check when set |
| RFC 9207 `iss` | checked if present | checked if present; **required** if discovery advertises `authorization_response_iss_parameter_supported: true` |
| JWKS/discovery cache | none, fetched per login | **unchanged** (see below) |

The endpoint rule stays same-origin. It covers Keycloak, Authentik, Authelia,
Zitadel, Kanidm, Okta, Auth0 and Entra ID. Google's split hosts
(`accounts.google.com` / `oauth2.googleapis.com` / `www.googleapis.com`) stay
unsupported (L4). A second origin allow-list is a setting nobody would audit.

**No JWKS cache, on purpose.** Discovery and JWKS are fetched on every
callback, which already happens. Login volume is bounded by 5/min/address and
60 starts/min/process. Per-login fetch makes key rotation a non-event (there is
no stale set and no "unknown `kid` → refetch" logic to get wrong), and a
poisoned cache can't outlive a request. joserfc selects the key by `kid` from
the freshly fetched set. A token whose `kid` is absent fails as `token`.

The callback checks `iss` **after** the discovery fetch and **before** the
token request. #415 checked it before any fetch. The move is needed to know
whether `iss` is mandatory. State is still consumed first, so a replay cannot
use the discovery fetch to burn provider requests.

Requested `profile` exists only so Keycloak's default mappers include
`preferred_username`. It is never used for matching, and the rest is
discarded.

### D6. Transport policy for the back-channel

The issuer is **operator configuration**, an internal hop like `OLLAMA_URL`,
not a user-supplied URL. So `fetch_url_guarded` and its SSRF deny list do not
apply. An in-cluster Keycloak at a private address is expected. The policy is
the #415 transport plus one setting:

- `OIDC_ISSUER` must be `https`. **There is no plaintext flag**, unlike
  `EMBEDDING_ALLOW_PLAINTEXT`. The issuer is browser-facing (the authorization
  endpoint shares its origin), and it is compared byte-for-byte with the
  token's `iss`, so the back-channel cannot use a different `http://svc:8080`
  URL anyway. Plaintext would also expose the client secret and the
  authorization code.
- `OIDC_CA_FILE` (optional, `off`/empty = unset) is parsed once at settings
  construction by the same helper shape as `load_embedding_ca_context`
  (`check_certificate_file` + `ssl.create_default_context(cafile=…)`,
  refusing a file with no CA). It becomes `verify=` on the
  `httpx2.AsyncHTTPTransport` inside `BoundedTransport`. Unset means certifi's
  bundle, as `certifi_ssl_context()` gives the embedding clients. A separate
  setting, not `EMBEDDING_CA_FILE`, because the two hops have different
  operators and trust roots.
- `trust_env=False`, `follow_redirects=False`, identity encoding, 128 KiB
  bodies, 5 s per operation, and a 20 s phase deadline are all unchanged.
- No DNS pinning. The operator controls the name, as with the embedding hop.

### D7. Settings

All are read only when `OIDC_ENABLED=true`, except `LOCAL_LOGIN_ENABLED` and
the retired field. They are validated in the existing
`Settings._validate_oidc` model validator. Half-configuration refuses startup.

| Setting | Default | Validation |
| --- | --- | --- |
| `OIDC_ENABLED` | `false` | unchanged: requires `MULTI_USER_MODE`, forbids `MCP_SANDBOX_MODE` |
| `OIDC_ISSUER` | `""` | unchanged rules + length ≤ 2,048 |
| `OIDC_CLIENT_ID` / `OIDC_CLIENT_SECRET` | `""` | unchanged (both required) |
| `OIDC_TOKEN_AUTH_METHOD` | `client_secret_basic` | one of `client_secret_basic`, `client_secret_post` |
| `OIDC_REQUIRED_GROUP` | `""` | **now optional**; ≤ 512 chars when set |
| `OIDC_EXTRA_SCOPES` | `""` | space-separated; each token matches RFC 6749 `scope-token` (`%x21 / %x23-5B / %x5D-7E`), ≤ 8 tokens, ≤ 64 chars each; refuses `offline_access`; duplicates of `openid`/`email`/`profile` dropped |
| `OIDC_DISPLAY_NAME` | `SSO` | 1–40 printable characters, no control characters; rendered autoescaped as "Sign in with {name}" |
| `OIDC_CA_FILE` | unset | D6 |
| `LOCAL_LOGIN_ENABLED` | `true` | unchanged; multi-user still needs ≥ 1 login method |
| `OIDC_SUBJECT_USER_IDS` | — | **retired**: non-empty refuses startup (D3) |

`BASE_URL` remains the source of the callback URL
(`<BASE_URL>/admin/auth/oidc/callback`). It is unchanged and must be an HTTPS
origin.

With `OIDC_ENABLED=false`, no new setting is validated beyond its type, the
retired-field refusal still applies, and no route, template branch or column
read changes the rendered output. The new columns exist but are read only by
OIDC code and by the panel card, which renders only when OIDC is enabled. Each
rendered page must be byte-identical with OIDC off (golden tests captured on the base commit, task 0.3).

### D8. Failure modes

- **Provider down at boot:** nothing at boot contacts the provider. Startup,
  `/health` and password login are unaffected. That is unchanged.
- **Discovery/JWKS/token errors at login:** refusal `provider`, the generic
  page, password login still offered. The transaction is consumed, so the
  user restarts the flow.
- **Unsupported auth method or alg** (discovery lists neither our method nor
  any allowed alg): refusal `provider` at start (method) or `token` (alg). The
  checks run at login, not boot, so a provider outage cannot block startup.
- **Clock skew:** leeway stays **0**, as in #415. `exp` and `iat` are integers.
  The token is checked seconds after the provider minted it, so a skew large
  enough to matter is a broken NTP setup that should fail loudly. The docs say
  so. (Accepted: L6.)
- **`email_verified` not the boolean `true`**, with the email matching a live
  expectation: refusal `email_unverified`. **Email absent or malformed:**
  irrelevant on the bound branch; on the pending branch it matches nothing,
  so the refusal is `unlinked`.
- **Inactive user:** `account`, before any bind write. That is unchanged.
- **Unknown identity:** `unlinked` (renamed from #415's `mapping`, which no
  longer describes a map).

### D9. Login CSRF, session fixation, return-to

All unchanged from #415, restated as contract:

- `state` is 32 random bytes, bound to the browser through the signed session
  cookie (`oidc_login_state`), single use, consumed before any network I/O,
  and compared in constant time. A callback from another browser, a replayed
  state or an expired one mints nothing. This is the login-CSRF defence: an
  attacker cannot make a victim's browser complete the attacker's login,
  because the victim's session holds no matching state.
- `nonce` and the PKCE verifier stay server-side in the transaction and never
  reach a cookie.
- Session fixation: `start_session` writes a fresh server-side session id, and
  a pre-login session id grants nothing because it has no registry row.
- Return-to: `next` passes through `_safe_next` at start (≤ 4,096 characters,
  else `/admin/`), is stored in the transaction rather than taken from the
  callback, and passes `_safe_next` again at redirect. A login started from
  `/authorize` returns to the exact percent-encoded authorize URL, because the
  panel's login redirect already encodes it. No open redirect: `_safe_next`
  admits only same-origin paths.

### D10. Rate limits and the per-account budget

- The per-account failed-login budget (10 / 15 min) is about **password
  guessing**. OIDC refusals do not charge it. Before verification there is no
  account to charge, and after verification the provider has authenticated
  the person. The budget does not gate OIDC either: an attacker spraying
  passwords at `max` must not lock Max out of SSO. A successful OIDC login
  does not reset it.
- The address limits stay as #415 set them: start 5/min, callback 10/min,
  global 60 starts/min, separate from the 5/min password POST limit.
- The admin link actions run behind `require_admin` and CSRF. They need no new
  limiter: they are admin writes, as the other user-edit POSTs are.

### D11. Panel UI (nonce CSP rules)

- **Login page:** the existing `<a class="btn-submit">` link to
  `/admin/auth/oidc/start?next=…` gets the label "Sign in with {{
  oidc_display_name }}". It is a GET link, not a form, so `form-action 'self'`
  is unaffected. No inline handlers and no `style=`.
- **User edit page** (`user_edit.html`): a "Single sign-on" card, rendered only
  when `oidc_enabled`, showing one of: *Not linked*; *Waiting for first sign-in
  from `<email>` (expires `<date>`)* or *expired*; *Linked (since `<date>`)*.
  The subject is shown truncated to its first 8 characters, and the issuer is
  shown only if it differs from the configured one ("linked under a previous
  issuer — will not sign in"). Forms, each `method=POST` with `csrf_token`:
  - `POST /admin/users/{id}/oidc/expect`, field `email`. Allowed on a
    not-linked or pending row, and replaces a pending one.
  - `POST /admin/users/{id}/oidc/bind`, field `subject` (1–255 printable ASCII,
    no whitespace). Allowed on a not-linked or pending row. Binds under the
    configured issuer and clears pending.
  - `POST /admin/users/{id}/oidc/unlink`: clears all six columns and calls
    `revoke_user_sessions` in the same transaction (the existing pattern:
    `_lock_admin_guard`, no commit before the writes). It uses the existing
    `type="button"` + `data-confirm` delegated listener in `panel.js`, which
    fails closed. It is refused for `is_self` when `LOCAL_LOGIN_ENABLED=false`
    (flash `err`), and by the D15 rule when the target is the last reachable
    admin.

  **Every one of the three handlers** enters `_lock_admin_guard` and then
  immediately re-runs `_actor_still_privileged(session, user)`, **before
  reading the target row**. On failure it takes `actor_snapshot(user)`, rolls
  back, calls `_log_actor_revoked`, and answers with the established
  `_ACTOR_REVOKED_MSG` refusal: the same sequence `edit_user_submit` and
  `delete_user` use. Only then does it read the target, apply the D15 check
  (unlink, on that pre-write state), lock the target `FOR UPDATE`, write, and
  commit.

  To change a bound user's identity, unlink first. That two-step act is the
  point: a bound user is never silently re-pointed. Conflicts (email already
  pending elsewhere, subject bound elsewhere) are flashed as "already used by
  another account". The admin is entitled to see that, since they can see
  every user.
- **Users list:** a "SSO" column (`linked` / `pending` / `—`) when
  `oidc_enabled`.
- **Account page:** read-only "Single sign-on: linked / not linked" when
  `oidc_enabled`. There is no self-service linking, because the binding is an
  admin act.
- Every new style goes in a nonce'd `<style>` block or a `_utilities.html`
  class, with no `style=`. Every POST passes the existing CSRF middleware.

### D12. Logout

Logout stays local (revoke the registry row, clear the cookie). **We do not
add RP-initiated logout.** It needs an `id_token_hint` to skip the provider's
confirmation page, and keeping the ID token would break the #415 invariant that
provider credentials never enter the session. Without the hint, Keycloak shows
an interstitial, and for a two-person household on a shared Keycloak (Open
WebUI uses the same session) a panel logout that also signs the user out of
Open WebUI is the surprising behaviour. The docs say a local logout
followed by "Sign in with SSO" signs straight back in while the Keycloak
session lives (L3).

### D13. Bootstrap and break-glass

- **Bootstrap.** Unchanged from #415. While `OIDC_ENABLED=true`,
  `/admin/register` is 404 even on an empty table, and the panel never
  redirects there. The setup order is documented: start with OIDC off,
  register the first admin through the (proxy-gated) bootstrap, create the
  other users, then enable OIDC, set each user's expected email, and sign in.
  An empty table with OIDC on is an operator error. Every SSO attempt is
  `unlinked`, and the docs name the fix (turn OIDC off for the bootstrap).
  OIDC bootstrap (first verified identity becomes admin) is rejected. On an
  internet-reachable login it is a race for the admin account.
- **Break-glass when OIDC is down.** With password login enabled (the default),
  nothing is needed: the form is on the same page. With
  `LOCAL_LOGIN_ENABLED=false`, the operator sets it to `true` in the
  deployment config (k3s: the ConfigMap/Secret, then a rollout restart) and
  signs in with a local admin password. That needs cluster access, which is
  the right bar. A URL-reachable bypass would be a permanent second login path.
  Admin-created users keep a password hash (the create form still requires
  one), so recovery passwords exist.
- **Lockout warning.** At lifespan start, with OIDC enabled and
  `LOCAL_LOGIN_ENABLED=false`, one query counts active admins bound under the
  configured issuer. Zero logs one CRITICAL line naming the recovery step.
  Startup continues: an outage of one login method must not become an outage
  of `/mcp`. A failure of the query itself logs a WARNING (class name only) and
  continues.
- The self-unlink refusal (D11) and the last-reachable-admin rule (D15)
  close the foot-gun of the panel removing its own last way in.

### D14. Security events

The field allow-list is in `EVENT_FIELDS`. Every name below goes in both the
catalogue and `docs/architecture/security-event-logging.md`.

| Event | Level | Fields | When |
| --- | --- | --- | --- |
| `panel_oidc_refused` | WARNING | `reason`, `client_ip`, `route` (unchanged) | reasons now `capacity`, `state`, `provider`, `token`, `entitlement`, `unlinked`, `email_unverified`, `link_conflict`, `account`, `session_mint` (`mapping` removed) |
| `panel_oidc_linked` | INFO | `user_id`, `username`, `client_ip`, `route` | first-login bind, after its commit |
| `panel_oidc_link_set` | INFO | `actor_user_id`, `user_id`, `username`, `mode` (`expect`/`bind`), `client_ip`, `route` | admin action, after commit |
| `panel_oidc_link_removed` | INFO | `actor_user_id`, `user_id`, `username`, `count`, `client_ip`, `route` | admin unlink, after commit; `count` = sessions revoked |
| `panel_login_succeeded` | INFO | unchanged | OIDC success, after mint |

**Never logged:** `sub`, `iss` claim values beyond the configured issuer
(nothing logs the issuer at all), email (expected or claimed), groups, code,
tokens, the raw `id_token`, state, nonce, verifier, client secret, and
exception text. `panel_oidc_refused` carries no `user_id` on `unlinked`,
`email_unverified` or `link_conflict`. On `account` it carries none either:
the record would confirm that the identity resolves to a local user, and it
is subject-keyed by address like `panel_login_account_throttled`.

### D15. The last reachable administrator (password login disabled)

With `LOCAL_LOGIN_ENABLED=false`, an admin who is not bound under the
configured issuer cannot sign in at all. The existing "last active admin"
guard counts admins who *exist*, not admins who can *reach* the panel. So
while password login is disabled, **every panel mutation that can reduce the
set of reachable admins refuses a change that would take the count from one
to zero**: one whose target is a reachable admin now and whose resulting
state has no other. A reachable admin is a row with `is_admin AND is_active
AND oidc_issuer = <configured issuer> AND oidc_subject IS NOT NULL`. A
change whose target is not reachable now (an ordinary user, an inactive
account, an unbound admin, an admin bound under a previous issuer) cannot
reduce the count and is never refused by D15, even when the count is already
zero; refusing it would only obstruct cleanup.

- **The helper:** `oidc_links.reachable_admins_after(session, issuer, *,
  target_id, target_after)` counts reachable admins as if the target row had
  the post-mutation state `target_after` (`unlinked`, `demoted`,
  `deactivated` or `deleted`). The panel's `_leaves_no_reachable_admin(session,
  target, target_after)` refuses exactly when password login is disabled, the
  target's pre-write state is a reachable admin, and that count is zero. It is
  evaluated **inside `_lock_admin_guard`, after `_actor_still_privileged`,
  before any write**, in the same transaction as the write, so the check and
  the act are atomic against every other guarded mutation. For the unlink,
  the target's pre-write state is the row read under the guard, and the
  check runs before `oidc_links.unlink`.
- **Call sites, each refusing with a flash `err` and nothing written:**
  1. `POST /admin/users/{id}/oidc/unlink` (new).
  2. `edit_user_submit` when the edit clears `is_admin` or `is_active`, next
     to the existing last-active-admin check.
  3. `delete_user`, soft delete (clears `is_active`).
  4. `delete_user`, permanent delete.

  Nothing else in the panel reduces the set. `oidc/expect` and `oidc/bind`
  are refused on bound rows. The first-login bind and the admin create only
  add to it. Password reset doesn't touch it, and an admin's self-service
  password change doesn't either. A test asserts that the four sites are the
  only `src/` writers of `users.is_admin`, `users.is_active` or
  `users.oidc_subject`, and the only deleters of a `users` row, apart from
  the first-login bind, bootstrap and create. So a fifth mutation site can't
  silently skip the rule.
- **Not applied** when `LOCAL_LOGIN_ENABLED=true`. There, the password form
  reaches every active admin, and the existing guard is the whole rule.
- **Outside the panel:** changing `OIDC_ISSUER`, disabling the account at the
  provider, or setting `LOCAL_LOGIN_ENABLED=false` with no bound admin is
  operator config. The boot CRITICAL (D13) reports it; the panel can't
  prevent it.
- **Concurrency:** two admins A and B, both bound, password login disabled.
  Requests "A unlinks B" and "B deactivates A" race. The guard serializes
  them. The second to take the lock re-runs `_actor_still_privileged`, so if
  its actor was just deactivated or demoted it gets the actor-revoked
  refusal; otherwise the D15 count sees the first one's committed result.
  Exactly one succeeds and one reachable admin always remains. The tests force
  that order deterministically: the first request holds the guard on a
  barrier while the second is issued.

### D16. The OIDC-off regression contract

"Byte-for-byte unchanged" is enforced by golden tests captured on the base
commit (bd9b386) **before** any slice starts, committed with the spec and
green on that base. After the merge, they must pass unchanged. Normalized: the
CSRF token, the CSP nonce, `Set-Cookie` values, dates, generated ids and codes.
Each golden runs in a matrix of OIDC disabled (single-user and multi-user) and
OIDC enabled (multi-user, fake provider). The goldens:

1. Unauthenticated `GET /authorize?…` (a DCR client and a CIMD client): the
   302 to `/admin/auth/login?next=<encoded>`, byte-for-byte.
2. Authenticated consent render for a DCR client and for a CIMD client.
3. Consent approval: the redirect, the code's user and the `grant_id` lineage
   are preserved, and the code exchanges for a token owned by the same user.
4. `/admin/register` GET and POST: OIDC disabled → the existing empty-table
   bootstrap and the closed-once-users-exist redirect, unchanged; OIDC enabled
   → 404 for both, as #415 shipped.
5. The login, users list, user edit and account pages.

In the OIDC-enabled column, items 1–3 must equal the disabled column. OIDC
changes only *how* the session is minted, never what `/authorize` does with
it. Item 5 must differ only by the OIDC blocks.

## Accepted limitations

- **L1.** Window takeover: before the first sign-in, a different provider
  account holding a *verified* copy of the expected email binds instead. This
  is bounded by single use, the 7-day expiry and issuer scope. Direct subject
  binding avoids it entirely.
- **L2.** Provider-side offboarding (disable, group removal) does not end
  existing app sessions or MCP credentials. Unchanged from #415. Unlink or
  deactivate locally.
- **L3.** Logout is local. A live provider session signs straight back in.
- **L4.** Providers whose discovery endpoints span origins (Google) are
  unsupported.
- **L5.** Providers that put `email` only in UserInfo cannot use email linking.
  They use direct subject binding.
- **L6.** Zero clock-skew leeway. A host with broken NTP fails OIDC login,
  while password login keeps working.
- **L7.** Changing `OIDC_ISSUER` (realm rename, new host) orphans every binding
  and pending expectation. The panel shows them as linked under a previous
  issuer, and the admin re-links each user.
- **L8.** One issuer per deployment.
- **L9.** An `/authorize` URL longer than 4,096 characters, after encoding,
  returns to `/admin/` instead of the consent page. This is unchanged from
  #415. Real client requests are well under that.
- **L10.** The in-memory transaction store is lost on restart (one worker,
  #415). The user retries.
- **L11.** D15 refuses only a change to a target that is a reachable admin
  now. With `LOCAL_LOGIN_ENABLED=false` and zero reachable admins already (the
  boot CRITICAL state), D15 refuses nothing: every edit, unlink or delete
  proceeds, since none can reduce a count that is already zero (the existing
  last-active-admin rule still applies), so a session that is still live can
  clean up. No new sign-in reaches the panel in that state; the recovery is
  the documented break-glass (`LOCAL_LOGIN_ENABLED=true`, restart).

## Risks / Trade-offs

- Retiring `OIDC_SUBJECT_USER_IDS` with a hard refusal breaks a deployment that
  set it. That is mitigated by an explicit message, the README upgrade note,
  and #415 being days old and off by default. Silent dual sources are worse.
- Making `OIDC_REQUIRED_GROUP` optional loosens a #415 default. Per-user
  admin binding is now the primary admission control, so the group is defense
  in depth. The Keycloak walkthrough keeps setting it, because it is the
  provider-side kill switch for new logins.

## Migration plan

Two paths. Which one applies depends on whether `OIDC_SUBJECT_USER_IDS` is
set anywhere in the deployment's configuration.

**(a) Fresh activation.** OIDC was never configured, or
`OIDC_SUBJECT_USER_IDS` was never set.

1. Deploy with OIDC off. Migration 037 runs, and every page is unchanged
   (D16).
2. Confirm a local admin password works. Keep `LOCAL_LOGIN_ENABLED=true`.
3. Configure the provider client, set the `OIDC_*` settings, enable OIDC and
   restart.
4. For each user, set an expected email, or bind a known subject, on their
   edit page. Each user signs in once with SSO.
5. Verify an SSO sign-in per user and one `/authorize` consent round trip.
6. Optionally set `LOCAL_LOGIN_ENABLED=false`, only after the boot log shows
   no lockout CRITICAL (D13). D15 guards it from then on.

**(b) Upgrade from `OIDC_SUBJECT_USER_IDS`.** This is D3, in order:

1. **On the old release**, confirm a local admin password works. Reset one if
   needed.
2. Set `LOCAL_LOGIN_ENABLED=true`.
3. Record the map's subject-to-user pairs, then remove
   `OIDC_SUBJECT_USER_IDS`.
4. Deploy. Migration 037 runs. Startup refuses while the map is still set.
5. Sign in with the password and bind each former subject on its user's edit
   page (Bind subject).
6. Verify an SSO sign-in per user and one `/authorize` consent round trip.
7. Optionally set `LOCAL_LOGIN_ENABLED=false` again, under the same boot-log
   condition as (a) step 6.

Rollback: unset `OIDC_ENABLED`. The columns are inert. The migration downgrade
drops the links.
