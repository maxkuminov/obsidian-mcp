# Native browser SSO (OIDC)

Optional OpenID Connect login for the control panel. A person signs in at your
identity provider, and the server maps that verified identity to an **existing**
local account. The same panel session then covers the MCP consent page
(`/authorize`), so connecting Claude, ChatGPT or another MCP client can go
through SSO as well.

What it is not:

- **Not a replacement for this server's OAuth.** The server stays the
  authorization server for MCP clients. API keys, OAuth client registrations
  (DCR and CIMD), grants, access and refresh tokens, and token rotation are
  unchanged. Nothing the provider issues is ever accepted at `/mcp`.
- **No provisioning.** A provider identity that is not linked to a local user
  is refused. Signing in never creates a user, changes a role or the admin
  flag, or assigns a vault. Groups at the provider are never mapped onto
  `is_admin`.
- **No provider tokens kept.** The ID token is validated during the callback
  and then discarded. Nothing from the provider enters the cookie, the session
  registry or the database except the issuer and subject of a linked account,
  stored when an admin binds it or when the first sign-in binds an expected
  email.

SSO is disabled by default (`OIDC_ENABLED=false`). With it off, every page,
the login form and `/authorize` behave exactly as before; the link routes
answer 404.

## Contents

- [How linking works](#how-linking-works)
- [Provider requirements](#provider-requirements)
- [Settings](#settings)
- [Setting it up](#setting-it-up)
- [Keycloak walkthrough](#keycloak-walkthrough)
- [Turning password login off](#turning-password-login-off)
- [Recovery (break-glass)](#recovery-break-glass)
- [Upgrading from OIDC_SUBJECT_USER_IDS](#upgrading-from-oidc_subject_user_ids)
- [Troubleshooting](#troubleshooting)
- [Offboarding, logout and limits](#offboarding-logout-and-limits)

## How linking works

An administrator links accounts in the panel: **Users → (user) → Edit →
Single sign-on**. The card offers three actions, each its own form:

| Action | What it does | Allowed when |
| --- | --- | --- |
| **Expect email** | Records an address. The first sign-in whose ID token carries that address *verified by the provider*, under the configured issuer, within **7 days**, links the account to that identity. | Not linked, pending or expired. Replaces a pending address. |
| **Bind subject** | Links the account immediately to the provider subject (`sub`) you paste. No email involved. | Not linked, pending or expired. |
| **Unlink** | Clears the link (or the pending address) and signs that user out of every browser. | Linked or pending. |

Once an account is linked, **only the pair (issuer, subject) is consulted**.
The email, username and every other claim are ignored for that user from then
on. A rename or email change at the provider does nothing; an address that
moves to a different provider account does not carry the login with it. To
point a linked account at a different identity, unlink it first, then link
again. A linked account is never silently re-pointed.

The card shows one of:

- *Not linked*
- *Waiting for first sign-in from `<email>`* (with its expiry), or *Expired*
  (set it again to re-arm)
- *Linked (since `<date>`)*, with the first 8 characters of the subject
- *Linked under a previous issuer — will not sign in*: the account was bound
  while `OIDC_ISSUER` had another value. Unlink, then link again.

The users list gains an **SSO** column (`linked`, `pending` or `—`), and each
user's own account page shows a read-only "Single sign-on: Linked / Not
linked" line. There is no self-service linking; it is an admin act.

### The email rule

**Expect email** is convenient, because the admin does not need to look up a
subject. It is also the one place a provider claim other than `sub` matters,
so it is narrow:

- The token's `email_verified` must be the JSON boolean `true`. The string
  `"true"`, which some misconfigured mappers send, does not count. A match
  without it is refused (`email_unverified`) and nothing is written.
- The match is exact after one normalization, applied to both the address the
  admin typed and the claim: surrounding ASCII whitespace is stripped, ASCII
  letters are lower-cased, and nothing else changes. Non-ASCII characters are
  compared as entered; there is no Unicode or IDN folding. The address must be
  3–254 characters with exactly one `@`, a non-empty local part and domain,
  and no whitespace or control characters.
- **Single use.** The first match links the account and clears the
  expectation.
- **Expires after 7 days**, counted from when the admin set it. An expired
  expectation simply stops matching. It is not deleted; set it again to re-arm.
- **Scoped to the issuer** configured when it was set.
- Two accounts cannot wait for the same address ("That value is already used
  by another account").

The residual risk is the window itself: if someone else can get a provider
account holding a *verified* copy of the expected address before the intended
person signs in, that account is the one linked. Keep the window short, check
the users list afterwards, or use **Bind subject**, which has no window at all.

Use **Bind subject** when the provider puts `email` only in the UserInfo
response and not in the ID token, when an account has no verified email, or
when you already know the subject. The field takes 1–255 printable ASCII
characters with no spaces; surrounding whitespace from a paste is dropped.

### What happens at sign-in

After the ID token has been fully validated, the callback resolves it:

1. A user bound to the token's `(iss, sub)` → that user. The email is not
   read, so a token with no email, or a malformed one, still signs in.
2. Otherwise, if the email normalizes and matches a live expectation under the
   same issuer: with `email_verified: true` the account is linked and signed
   in; without it the login is refused.
3. Otherwise the login is refused.

The link and the first session are written in one transaction, under the same
lock every admin action and every session mint takes, so nothing can change
the account between the bind and the mint: a password reset or an admin edit
either commits before the sign-in (which then sees it) or waits for it. If the
session still cannot be minted, nothing is written and the account stays
pending; that refusal (`session_mint`) is defensive and should not occur in
practice. An inactive account is refused before anything is written. A successful SSO
login stamps the user's "last login", as a password login does.

Every refusal renders the same login page with the same message, "Single
sign-on could not be completed. Please try again.", and status 401 (503 with
`Retry-After: 60` when the login store is full). The page never says whether
the identity matched a local user. The reason goes to the security log only
(see [Troubleshooting](#troubleshooting)).

SSO refusals do not count against the per-account failed-password budget, and
the budget does not block SSO: someone guessing a user's password cannot lock
that user out of SSO.

### The last reachable administrator

While password login is disabled (`LOCAL_LOGIN_ENABLED=false`), an admin who is
not linked under the configured issuer cannot sign in at all. So, in that
state, the panel refuses any change that would leave **no active admin linked
under the configured issuer**. The changes it guards:

- unlinking a user;
- removing a user's admin flag or deactivating them on the edit form;
- soft-deleting or permanently deleting a user.

The refusal reads "Refused: password sign-in is disabled, and this would
leave no active admin who can sign in with single sign-on. Link another admin
first, or re-enable password sign-in." Nothing is written. You also cannot
unlink your own account while password login is disabled. With password login
enabled, none of this applies: the ordinary "last active admin" rule is the
whole rule, because the password form reaches every active admin.

The panel cannot see changes made outside it: a new `OIDC_ISSUER`, an account
disabled at the provider, or `LOCAL_LOGIN_ENABLED=false` set with nobody
linked. For those, the server logs one CRITICAL line at startup when OIDC is
on, password login is off and no active admin is linked under the configured
issuer:

```
LOCAL_LOGIN_ENABLED=false and no active admin is linked to single sign-on under OIDC_ISSUER: nobody can sign in to the panel as an administrator. Recovery: set LOCAL_LOGIN_ENABLED=true, restart, sign in with a local admin password and link an admin (user edit page, Single sign-on). The MCP endpoint is unaffected.
```

It is a warning, not a refusal: the server starts and `/mcp` keeps serving.

## Provider requirements

Any OpenID Connect provider works if it meets all of these. Keycloak,
Authentik, Authelia, Zitadel, Kanidm, Okta, Auth0 and Microsoft Entra ID
generally do.

- **One HTTPS issuer.** `OIDC_ISSUER` is an `https://` URL with no user info,
  query or fragment. Discovery is fetched from
  `<OIDC_ISSUER>/.well-known/openid-configuration`, and its `issuer` field must
  equal `OIDC_ISSUER` **byte for byte**. Watch the trailing slash.
- **Endpoints on the issuer's host.** The `authorization_endpoint`,
  `token_endpoint` and `jwks_uri` must be `https` on the same host and port as
  the issuer. Providers that spread them across hosts are not supported; that
  includes Google, whose endpoints span `accounts.google.com`,
  `oauth2.googleapis.com` and `www.googleapis.com`.
- **No redirects, small uncompressed responses.** The server does not follow
  redirects on these requests, asks for identity encoding and refuses a
  compressed response, caps each response at 128 KiB, and ignores
  `HTTP(S)_PROXY`. Discovery and JWKS are fetched fresh on every login, so key
  rotation needs nothing from you.
- **Authorization code flow with PKCE `S256`**, `response_mode=query`.
- **A confidential client** authenticating with `client_secret_basic`
  (default) or `client_secret_post`. If discovery lists
  `token_endpoint_auth_methods_supported`, it must include the one you
  configure.
- **ID tokens signed with RS256, PS256 or ES256.** The server accepts only the
  algorithms in that set that discovery lists in
  `id_token_signing_alg_values_supported` (RS256 when the field is absent).
  `none`, HS256 and every other algorithm are refused whatever discovery says.
  The token's `kid` must be in the JWKS.
- **Standard ID token claims:** `iss` equal to the issuer; `aud` equal to the
  client ID (a string, or an array holding only the client ID: an extra
  audience is refused); `azp`, if present, equal to the client ID; `sub` of at
  most 255 characters; integer `exp` and `iat`; the `nonce` echoed.
- **RFC 9207.** If discovery advertises
  `authorization_response_iss_parameter_supported: true`, the provider must
  return `iss` on the callback. A present `iss` must always equal the issuer.
- **For Expect email:** `email` and a boolean `email_verified` in the **ID
  token**. The server requests the scopes `openid email profile`, plus
  `OIDC_EXTRA_SCOPES`. Otherwise use Bind subject.
- **For `OIDC_REQUIRED_GROUP`:** a `groups` claim in the ID token that is an
  array of strings (at most 256 entries of at most 512 characters each).
- **A correct clock.** `exp` and `iat` are checked with zero leeway. Run NTP on
  both the provider and this server; a few seconds of skew is enough to fail
  every SSO login while password login keeps working.

The callback URL to register at the provider is
`<BASE_URL>/admin/auth/oidc/callback`, for example
`https://notes.example.com/admin/auth/oidc/callback`. Register it exactly; no
wildcards. One issuer per deployment.

## Settings

All `OIDC_*` settings are read and validated only when `OIDC_ENABLED=true`,
except `OIDC_SUBJECT_USER_IDS`, which is refused either way. Validation never
contacts the provider, so an unreachable provider cannot stop the server from
starting. An invalid value refuses startup with a message naming the setting.

**Every process that loads the settings validates them, not only the server.**
That includes the migration step (`alembic upgrade head`, the Kubernetes
`migrate` initContainer), `alembic check`, and one-off Jobs or `make` targets
run from the image. With `OIDC_ENABLED=true`, each of them refuses to start
unless it sees the complete OIDC configuration, `OIDC_CLIENT_SECRET`
included (`OIDC requires OIDC_CLIENT_ID and OIDC_CLIENT_SECRET`). Give all of
them the same environment. The simplest way is to keep `OIDC_CLIENT_SECRET`
in the one Secret (or `.env`) that every container already loads. If you
keep it in a separate Secret and inject it with `secretKeyRef`, add that
reference to the migration initContainer and to every Job too. Otherwise
the first rollout after you set `OIDC_ENABLED=true` crash-loops in the
migration step. With a `Recreate` strategy the old pod is already gone at
that point, so the server is down until you fix it. Roll back with
`OIDC_ENABLED=false`.

| Setting | Default | Validation | Purpose |
| --- | --- | --- | --- |
| `OIDC_ENABLED` | `false` | Requires `MULTI_USER_MODE=true`; refused with `MCP_SANDBOX_MODE=true`. Requires `BASE_URL` (or `MCP_HOSTNAME`) to be an HTTPS origin with no path. | Turns SSO on. |
| `OIDC_ISSUER` | empty | Required. `https://`, no user info, query, fragment, whitespace or backslash; at most 2,048 characters. | The issuer URL, exactly as the provider's discovery document states it. |
| `OIDC_CLIENT_ID` | empty | Required. | The client ID registered at the provider. |
| `OIDC_CLIENT_SECRET` | empty | Required. Keep it in your secret store, never in a committed file. | The client secret. |
| `OIDC_TOKEN_AUTH_METHOD` | `client_secret_basic` | `client_secret_basic` or `client_secret_post`. At login, refused (`provider`) when discovery lists auth methods without this one. | How the client secret is sent to the token endpoint: the `Authorization` header, or the form body. Never both. |
| `OIDC_REQUIRED_GROUP` | empty | Optional. At most 512 characters; not whitespace only. | When set, the ID token's `groups` array must contain this exact, case-sensitive string, or the login is refused (`entitlement`). Empty means no group check. Use it as the provider-side switch for who may sign in to this app at all. |
| `OIDC_EXTRA_SCOPES` | empty | Space-separated RFC 6749 scope tokens, at most 8, each at most 64 characters. **`offline_access` is refused.** `openid`, `email`, `profile` and duplicates are dropped. | Extra scopes for providers that put claims (such as `groups`) behind a scope. |
| `OIDC_DISPLAY_NAME` | `SSO` | 1–40 printable characters, not blank. | The login button reads "Sign in with {name}". |
| `OIDC_CA_FILE` | unset | Empty or `off` means unset. A readable PEM file holding at least one CA certificate. | Trust anchor for the provider requests only, for an issuer behind a private CA. When set, **only** this file is trusted for them; unset uses the certifi bundle. Separate from `EMBEDDING_CA_FILE`. |
| `LOCAL_LOGIN_ENABLED` | `true` | In multi-user mode at least one of `LOCAL_LOGIN_ENABLED` and `OIDC_ENABLED` must be true. | The password form. `false` hides it and refuses its POST. Setting it back to `true` and restarting is the [recovery path](#recovery-break-glass) when SSO is unavailable. |
| `OIDC_SUBJECT_USER_IDS` | — | **Retired.** Any non-empty value refuses startup, OIDC on or off. | Replaced by linking in the panel. See [Upgrading from OIDC_SUBJECT_USER_IDS](#upgrading-from-oidc_subject_user_ids). |

`BASE_URL` (or `MCP_HOSTNAME`, which derives it) is the source of the callback
URL. Keep `--workers 1`, as the server already requires: pending logins live in
that worker's memory.

```dotenv
MULTI_USER_MODE=true
MCP_HOSTNAME=notes.example.com
OIDC_ENABLED=true
OIDC_ISSUER=https://keycloak.example.com/realms/<realm>
OIDC_CLIENT_ID=obsidian-mcp
# OIDC_CLIENT_SECRET comes from your secret mechanism.
OIDC_TOKEN_AUTH_METHOD=client_secret_basic
OIDC_REQUIRED_GROUP=/apps/obsidian-mcp
OIDC_DISPLAY_NAME=Keycloak
LOCAL_LOGIN_ENABLED=true
```

## Setting it up

Order matters, because there is no SSO bootstrap: while `OIDC_ENABLED=true`,
`/admin/register` answers 404 even on an empty database. The first identity
to sign in never becomes an admin.

1. **Start with OIDC off** in multi-user mode. Register the first admin
   through the bootstrap form (see the README's
   [Multi-user mode](../README.md#multi-user-mode)) and create the other users
   with their vaults. Every account created in the panel has a password, so a
   recovery password exists for each.
2. **Confirm a local admin password works** and keep it somewhere safe. It is
   your way back in if the provider is down.
3. **Back up the database.**
4. **Register the client at the provider** (the [Keycloak
   walkthrough](#keycloak-walkthrough) below, or your provider's equivalent of
   it), with the exact redirect URI.
5. **Allow egress** from the server to the issuer over HTTPS. On Kubernetes
   that usually means a NetworkPolicy rule. An in-cluster provider at a private
   address is fine; put its CA in `OIDC_CA_FILE` if it is not publicly trusted.
6. **Set the `OIDC_*` settings**, keep `LOCAL_LOGIN_ENABLED=true`, and restart.
   The login page now shows "Sign in with {name}" above the password form.
7. **Link each user** on their edit page: Expect email, or Bind subject.
8. **Have each user sign in once with SSO.** Check the users list shows
   `linked` for each.
9. **Check one MCP client's consent round trip**: connect a client, sign in
   with SSO at the consent page, approve, and confirm the client works.
   Confirm existing API keys and connected clients still work.
10. **Check refusals:** an identity that is not linked, and (if you set
    `OIDC_REQUIRED_GROUP`) one outside the group, are both refused.
11. Optionally [turn password login off](#turning-password-login-off).

**Logging.** Suppress query strings on `/admin/auth/oidc/callback` in your
reverse proxy's or ingress's access logs and in any tracing: the query carries
the authorization code and state. The server filters that line from its own
Uvicorn access log, and its security events carry no subject, email, group,
code, token or provider error text. Do not enable HTTP debug logging while
SSO is configured.

**If an external login gate sits in front of the panel** (for example a
forward-auth proxy), users may have to sign in twice while it remains. Remove
it only once SSO works end to end. The panel paths `/admin/auth/login`,
`/admin/auth/oidc/start` and `/admin/auth/oidc/callback` must be reachable
without it. Before removing it, confirm that an anonymous request to `/admin/`
redirects to the login page and an anonymous `/api/` request is refused. Do
not switch `MULTI_USER_MODE` off to remove a gate: that removes the vault
boundaries between users.

## Keycloak walkthrough

Written against the current Keycloak admin console (version 26). Labels move
between releases; if one is not where this says, search the client's tabs for
it. Replace `<realm>` and `notes.example.com` with your own values.

### 1. The client

1. Open the admin console and pick your realm in the realm selector (top
   left). Your issuer is `https://keycloak.example.com/realms/<realm>`.
   **Realm settings → General → Endpoints → OpenID Endpoint Configuration**
   opens the discovery document; its `issuer` value is what goes in
   `OIDC_ISSUER`, character for character.
2. **Clients → Create client.**
3. **General settings:** Client type **OpenID Connect**; Client ID
   `obsidian-mcp` (this is `OIDC_CLIENT_ID`); a Name if you like. **Next.**
4. **Capability config:**
   - **Client authentication: On** (a confidential client).
   - **Authorization: Off.**
   - Authentication flow: tick **Standard flow** only. Untick **Direct access
     grants**, **Implicit flow**, **Service accounts roles**, **OAuth 2.0
     Device Authorization Grant** and **OIDC CIBA Grant**.
   - If this page shows **PKCE Method**, choose **S256**.
   - **Next.**
5. **Login settings:**
   - **Valid redirect URIs:**
     `https://notes.example.com/admin/auth/oidc/callback`. Exactly that, with
     no `*`.
   - Leave **Valid post logout redirect URIs** and **Web origins** empty. The
     server does no provider logout and no browser-side token calls.
   - **Save.**
6. **Advanced** tab → **Advanced settings** → **Proof Key for Code Exchange
   Code Challenge Method: S256** → **Save** (skip if you set PKCE Method in
   step 4). Leave **ID token signature algorithm** empty (the realm default,
   RS256) or pick RS256, PS256 or ES256.
7. **Credentials** tab: Client Authenticator **Client Id and Secret**. Copy the
   **Client Secret** into your secret store as `OIDC_CLIENT_SECRET`. Keycloak
   accepts both `client_secret_basic` and `client_secret_post` here, so keep
   the default `OIDC_TOKEN_AUTH_METHOD`.

### 2. Scopes

**Client scopes** tab of the client: **email** and **profile** must be listed
with Assigned type **Default**. They are in a new realm. If one is missing,
**Add client scope**, pick it, and add it as **Default**. `email` puts `email`
and `email_verified` in the ID token; `profile` is requested but never used
for matching.

Do not add an audience mapper that puts a second audience into the ID token;
the server refuses an `aud` holding anything but the client ID.

### 3. Realm email settings (for Expect email)

**Realm settings → Login** tab:

- **Verify email: On.** Users must prove they control their address, which is
  what sets `email_verified: true`. Keycloak needs working SMTP for this
  (**Realm settings → Email**). An admin can also mark an address verified
  by hand: **Users → (user) → Details → Email verified: On**.
- **Duplicate emails: Off.** Two Keycloak accounts must not share one address.

Keycloak sets `email_verified` back to false when a user changes their address
in the account console, so a user who types in somebody else's address is not
verified and cannot match.

### 4. Optional: restrict who may sign in with a group

1. **Groups → Create group**, for example `apps`, then a child group
   `obsidian-mcp` inside it. Its full path is `/apps/obsidian-mcp`.
2. Add each person who may use this app: **Users → (user) → Groups → Join
   Group**.
3. Back on the client: **Client scopes** tab → **obsidian-mcp-dedicated** →
   **Add mapper → By configuration → Group Membership**:
   - Name: `groups`
   - Token Claim Name: `groups`
   - **Full group path: On**
   - **Add to ID token: On** (the access token and userinfo toggles do not
     matter to this server)
   - **Save.**
4. Set `OIDC_REQUIRED_GROUP=/apps/obsidian-mcp`. With Full group path on, the
   value starts with `/`. The comparison is exact and case-sensitive.

Removing someone from the group stops new SSO logins. It does not end the
sessions or MCP credentials they already have; see
[Offboarding](#offboarding-logout-and-limits).

### 5. Finding a user's subject (for Bind subject)

**Users → (user) → Details.** The **ID** field (a UUID such as
`3f0c2a9e-…`) is the `sub` Keycloak puts in the ID token. Copy it into **Bind
subject** on that person's edit page in the panel. The panel then shows its
first 8 characters, which you can compare with the start of the Keycloak ID.

### 6. Clocks

Keycloak and this server must both keep accurate time (NTP). The server
allows no skew on `exp` and `iat`.

## Turning password login off

Optional, once SSO works for every admin. Set `LOCAL_LOGIN_ENABLED=false` and
restart. Then check the startup log: if it shows the CRITICAL line quoted in
[The last reachable administrator](#the-last-reachable-administrator), no
admin can sign in. Set it back to `true` and link an admin before trying
again.

From then on, the password form is hidden and its POST refused, the panel
refuses changes that would leave no linked admin, and you cannot unlink
yourself. Keep each admin's local password anyway: it is the recovery path.

## Recovery (break-glass)

When the provider is down, or nobody can sign in with SSO:

- **Password login on** (the default): nothing to do. The password form is on
  the same login page.
- **Password login off:** set `LOCAL_LOGIN_ENABLED=true` in the deployment
  configuration and restart, then sign in with a local admin password.
  - Kubernetes: edit the ConfigMap (or Secret) that holds the setting, then
    `kubectl -n <namespace> rollout restart deployment/<name>`.
  - Docker Compose: edit `.env`, then
    `docker compose up -d --force-recreate`.

There is deliberately no bypass URL: recovery needs access to the
deployment's configuration. While you are in, you can also turn OIDC off; but
multi-user mode with both login methods off refuses to start, and multi-user
mode must stay on to keep each user's vault separate.

An admin whose local password is forgotten can be reset by another admin
(**Users → (user) → Edit → Reset password**). If no admin can sign in at all, re-enable
password login as above first.

## Upgrading from OIDC_SUBJECT_USER_IDS

The first SSO release mapped provider subjects to users through an environment
variable, `OIDC_SUBJECT_USER_IDS={"<sub>":<user id>}`. That setting is retired:
links now live on the user row and are managed in the panel. A deployment that
still sets it refuses to start with:

```
OIDC_SUBJECT_USER_IDS is retired (#416). Upgrade: (1) confirm a local admin password works; (2) set LOCAL_LOGIN_ENABLED=true; (3) remove OIDC_SUBJECT_USER_IDS; (4) start; (5) bind each former subject on its user's edit page (Single sign-on → Bind subject); (6) verify SSO and /authorize; (7) optionally set LOCAL_LOGIN_ENABLED=false. See docs/native-oidc.md, "Upgrading from OIDC_SUBJECT_USER_IDS".
```

Do it in this order. During the upgrade, password login is the only way into
the panel: the environment bindings are gone and the panel bindings do not
exist yet.

1. **Before upgrading, confirm a local admin password works.** If no admin
   knows one, reset it while still on the old release.
2. **Set `LOCAL_LOGIN_ENABLED=true`.**
3. **Copy the map's subject-to-user pairs somewhere safe, then remove
   `OIDC_SUBJECT_USER_IDS`** from the configuration. You need the pairs in
   step 5. Delete the whole line; do not leave it blank. An empty
   `OIDC_SUBJECT_USER_IDS=` fails to parse at startup before the retirement
   message is reached.
4. **Deploy and start.** Migration 037 runs (it adds the link columns to
   `users`; nothing is backfilled).
5. **Sign in with the password and bind each former subject** on its user's
   edit page: **Single sign-on → Bind subject**. That links immediately, so
   nothing is re-linked by email.
6. **Verify** an SSO sign-in for each user and one MCP client's `/authorize`
   consent round trip.
7. **Optionally set `LOCAL_LOGIN_ENABLED=false`** again, and check the startup
   log has no CRITICAL lockout line.

Also: `OIDC_REQUIRED_GROUP` is now optional. If you relied on it, keep it set;
the check is unchanged. The server now requests `openid email profile` (it
used to request `openid profile`).

## Troubleshooting

**The server, or its migration step, does not start after `OIDC_ENABLED=true`**
with `OIDC requires OIDC_CLIENT_ID and OIDC_CLIENT_SECRET`: that process did
not receive the secret. The usual cause is a `secretKeyRef` on the app
container only, with nothing on the migration initContainer. See
[Settings](#settings). `OIDC_ENABLED=false` restores service while you fix it.

The login page says only "Single sign-on could not be completed." The reason is
in the security log, as the `panel_oidc_refused` event's `reason` field:

| `reason` | Usual cause |
| --- | --- |
| `capacity` | Too many logins in progress (1,024) or started this minute across the server (60). Retry shortly. |
| `state` | The login took more than 5 minutes, the server restarted, the user started a second login in the same browser, or the callback was opened in another browser. Start again. |
| `provider` | Discovery, token or JWKS request failed; `issuer` in discovery does not equal `OIDC_ISSUER`; an endpoint on another host; the provider returned an error; missing or wrong `iss` on the callback; a compressed or oversized response; the configured auth method not offered; a TLS failure (check `OIDC_CA_FILE` and egress). |
| `token` | The ID token failed validation: signature, algorithm not allowed or not advertised, `aud`/`azp`, expired or issued in the future (check NTP), nonce, or a `sub` over 255 characters. |
| `entitlement` | `OIDC_REQUIRED_GROUP` is set and the token's `groups` does not contain it exactly (check Full group path and the ID-token toggle on the mapper). |
| `unlinked` | No account is linked to this identity and no live expectation matches its email: not linked yet, the expectation expired, set under a previous issuer, or the ID token carries no email. |
| `email_unverified` | An expectation matches the email, but `email_verified` is not `true`. Verify the address at the provider. |
| `link_conflict` | Another account was linked to the same identity at the same moment. |
| `account` | The linked account is deactivated or was deleted. |
| `session_mint` | The session could not be minted after the account was resolved. The bind and the mint run in one guarded transaction, so a password reset or an admin edit cannot cause it; it is a defensive refusal that should not occur in practice. Retry, and check the server log if it repeats. |

Other events: `panel_oidc_linked` when a first sign-in links an account,
`panel_oidc_link_set` and `panel_oidc_link_removed` for the admin actions, and
the usual `panel_login_succeeded` for every successful SSO login. None of them
carries a subject, an issuer or an email.

Rate limits: 5 starts per minute per address, 10 callbacks per minute per
address, 60 starts per minute across the server.

## Offboarding, logout and limits

- **Removing someone at the provider does not sign them out here.** Disabling
  the account, removing it from the group or resetting its password stops new
  SSO logins only. Existing panel sessions, API keys and OAuth grants keep
  working until they expire or you revoke them. To offboard, **deactivate the
  local account** (which ends its sessions) and revoke its keys and grants.
  Unlinking also ends the user's panel sessions, but not their API keys or
  grants. `SESSION_MAX_AGE` bounds how long a session lives.
- **Logout is local.** It ends the panel session. A live provider session
  signs straight back in on the next "Sign in with …". There is no provider
  logout, back-channel logout or token refresh.
- **Pending logins are in memory.** They expire after 5 minutes, are used
  once, and disappear on restart; the user just starts again. One pending
  login per browser: starting a new one cancels the previous one.
- **Changing `OIDC_ISSUER`** (a realm rename, a new hostname) orphans every
  link and expectation. The panel shows a binding as "linked under a previous
  issuer" and a pending expectation as "Expired"; unlink (or set a new
  expectation) and link each user again.
- **One issuer per deployment.**
- **Return to consent.** A login started from the consent page returns to it,
  unless the encoded `/authorize` URL is longer than 4,096 characters, in
  which case it lands on `/admin/` (real clients are well under that).
- **Downgrading migration 037 drops every link.** Rolling back is otherwise
  just `OIDC_ENABLED=false`: the columns stay and are ignored.

Protocol libraries: [Authlib HTTP clients](https://docs.authlib.org/en/stable/oauth2/client/http/index.html),
[joserfc JWT verification](https://jose.authlib.org/en/guide/jwt/).
