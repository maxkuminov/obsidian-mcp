# OIDC login: admin-managed account linking, any standards-compliant provider (#416)

## Why

Native OIDC browser login shipped in #414/#415 (`src/auth/oidc.py`,
`openspec/specs/native-oidc-login/`). It is disabled by default and deliberately
narrow: a Keycloak-only profile, RS256 only, a mandatory `groups` claim, and the
subject-to-user mapping held in an environment variable
(`OIDC_SUBJECT_USER_IDS={"<sub>":1}`). To link an account the operator has to
find the Keycloak `sub` (a UUID visible only in the Keycloak admin console),
edit the deployment's secret, and restart the pod. Nothing in the panel shows
who is linked.

Issue #416 records the owner's decisions for the general version (2026-11-04):

1. **Federated login only.** The provider authenticates people at the panel
   login and, through the same panel session, at the `/authorize` consent page.
   The app stays the OAuth authorization server for MCP clients. API keys,
   grants, DCR, CIMD and token rotation do not change.
2. **Admins link accounts in the panel, and the link is pinned to the subject.**
   The admin records the identity they expect for a local user. The first
   verified OIDC login that matches it binds the user to `(issuer, sub)`
   permanently. After that only `(issuer, sub)` is consulted. The admin can
   unlink.
3. **No JIT provisioning.** An identity with no linked or linkable local user is
   refused, and the refusal does not reveal which local users exist.
4. **Password login stays** by default. A setting turns it off for OIDC-only
   deployments.
5. Single-user mode, and multi-user deployments with OIDC not configured, behave
   exactly as they do today.

The issue also asks for "any standards-compliant OIDC provider". The shipped
profile rejects common providers on details that don't affect security:
ES256-signing providers such as Kanidm, `client_secret_post`-only clients, and
providers with no `groups` claim.

This change builds on the #415 implementation. It does not replace it. The
transaction store, the bounded transport, the callback log redaction, the
single-mint discipline and the rate limits all stay as they are.

## What Changes

- **Linking moves from the environment to the database (migration 037).** Six
  nullable columns go on `users`: the bound `(oidc_issuer, oidc_subject,
  oidc_linked_at)` and a pending expectation `(oidc_expected_issuer,
  oidc_expected_email, oidc_expected_set_at)`. Both pairs get UNIQUE
  constraints, and CHECK constraints keep each triple all-or-none and stop a
  row from being bound and pending at once. No new table and no foreign key.
- **Matching rule: verified email, once.** A pending expectation matches an ID
  token whose `email` equals the expected address (ASCII case-insensitive) and
  whose `email_verified` is the JSON boolean `true`, under the same issuer, at
  most 7 days after the admin set it. A match binds `(iss, sub)` and clears the
  expectation in one guarded transaction. After that, email and username are
  never consulted for that user. An admin who already knows the subject can
  enter it directly and bind immediately; this also replaces
  `OIDC_SUBJECT_USER_IDS`.
- **Panel:** a "Single sign-on" card on the user edit page with three actions
  (expect an email, bind a subject, unlink), each its own CSRF-protected POST.
  The users list gets a link-status column and `/admin/account` a read-only
  status line. Unlinking revokes that user's panel sessions. An admin cannot
  unlink themselves while password login is disabled.
- **Provider profile widened, within the same trust model:**
  - signing algorithms RS256, PS256 and ES256, intersected with what discovery
    advertises (never `none` or HS*);
  - `OIDC_TOKEN_AUTH_METHOD=client_secret_basic|client_secret_post`;
  - `OIDC_REQUIRED_GROUP` becomes optional (empty means no group check; the
    exact semantics stay when it is set);
  - `OIDC_EXTRA_SCOPES` for providers that gate claims behind a scope
    (`offline_access` is refused);
  - the RFC 9207 `iss` response parameter becomes *required* when discovery
    advertises `authorization_response_iss_parameter_supported`;
  - `OIDC_DISPLAY_NAME` sets the button label;
  - `OIDC_CA_FILE` sets a private trust anchor for the back-channel.

  The requested scopes become `openid email profile` (+ extra).
- **Retired:** `OIDC_SUBJECT_USER_IDS`. A non-empty value refuses startup with a
  message naming the panel action that replaces it (an upgrade note goes in the
  README).
- **`last_login_at`** is stamped on OIDC login. #415 does not stamp it.
- **The last reachable admin:** while `LOCAL_LOGIN_ENABLED=false`, unlink,
  demotion, deactivation, soft delete and permanent delete refuse any result
  that leaves no active admin bound under the configured issuer. The check runs
  under the shared admin guard, after the actor re-check (design D15).
- **Boot warning, never a refusal:** with `LOCAL_LOGIN_ENABLED=false` and no
  active admin bound under the configured issuer, one CRITICAL line says the
  panel can only be recovered by re-enabling password login.
- **Security events:** `panel_oidc_linked` (first-login bind),
  `panel_oidc_link_set` and `panel_oidc_link_removed` (admin actions), and new
  closed refusal reasons on `panel_oidc_refused`.

Unchanged: the shipped routes (`/admin/auth/oidc/start`, `/admin/auth/oidc/callback`); the
same-origin HTTPS endpoint rule; the in-memory single-use transactions; no
provider-token storage; local-only logout; bootstrap closed while OIDC is
enabled; the "at least one login method" invariant; every MCP-facing path.

## Capabilities

### Modified Capabilities

- `native-oidc-login`: database-backed linking, the verified-email bind rule,
  the widened provider profile, the retired mapping setting, and the new
  settings.
- `panel-user-administration`: the link and unlink admin actions, and session
  revocation on unlink.
- `security-event-logging`: three new events and the new refusal reasons.

## Impact

- **Code:** `src/auth/oidc.py` (claims out instead of a user id, algorithm and
  auth-method profile, CA context), `src/auth/routes.py` (callback resolution
  and bind, `last_login_at`), `src/config.py` (settings),
  `src/control_panel/users.py` + `user_edit.html` + `users.html` +
  `account.html` + `login.html`, `src/models/db.py`,
  `alembic/versions/037_oidc_account_links.py`,
  `src/services/security_events.py`, `src/main.py` (boot warning).
- **Dependencies:** none new. Authlib 1.9.2, joserfc 1.7.7 and httpx2 2.16.0
  are already pinned by #415. `make audit` is unaffected.
- **Schema:** migration 037, nullable columns plus constraints, no backfill.
  `make test-schema` and `make db-check` gate it.
- **Upgrade:** a deployment that set `OIDC_SUBJECT_USER_IDS` follows the
  sequence in design D3: confirm a local admin password, enable password login,
  remove the map, start, bind the subjects, verify, and optionally disable
  password login again. The startup refusal repeats that sequence. The owner
  confirmed that no production deployment has #415's OIDC enabled.
- **Docs:** README (feature section, settings, upgrade note), `.env.example`,
  DEPLOYMENT.md, `docs/native-oidc.md` (generic provider + Keycloak
  walkthrough), `docs/architecture/control-panel.md`,
  `docs/architecture/security-event-logging.md`,
  `docs/architecture/schema-and-migrations.md`, CLAUDE.md (Key decisions
  bullet and the architecture-table row).
