## Context

Three edge findings, three mechanisms, no shared file. What binds them is the layer: each is a place where the boundary between the network and the application either cannot express what it trusts or quietly repairs a client's mistake instead of reporting it.

Facts established by reading the live host (read-only `docker inspect`, `docker ps`), not assumed:

- **Traefik is v3** (the rule syntax and the available middlewares are what matter here, and both are v3-wide). Entrypoints are `http` (`:80`), `https` (`:443`) and a separate internal port for the API. The API is enabled (`--api=true`, `--api.dashboard=true`) but **not** `--api.insecure`, so that internal port serves nothing — the dashboard is reached over `https` behind the SSO middleware chain. Providers are `docker` (`exposedByDefault=false`, scoped to the shared proxy network) and `file`.
- **The catch-all is on Traefik's own container labels**, not in `/rules` and not in this repo: `traefik.http.routers.http-catchall.rule=HostRegexp(`.+`)`, `entrypoints=http`, `middlewares=redirect-to-https` (a `redirectscheme` to `https`). It declares **no explicit priority**, so Traefik's default applies: priority = the rule's length = `len("HostRegexp(`.+`)")` = **16**. Any rule we write is far longer, so we would outrank it by default — but a default that depends on a string length is not a contract, so the new router sets `priority=200` explicitly.
- **`forwardedHeaders.trustedIPs` is set on the `https` entrypoint only**, covering the shared proxy network. The `http` entrypoint trusts no forwarded headers, so an `ipAllowList` on `http` sees the true TCP peer with no strategy configuration needed.
- **An ACME HTTP-01 certificate resolver answers on the `http` entrypoint.** This deployment's own certificates come from a DNS challenge, but a router of ours matching `/.well-known` on `:80` sits in the same entrypoint as every other host's ACME challenge. Traefik's internal ACME router carries a maximal priority and should win, but "should" is not a design; the rule carves the prefix out explicitly.
- **`obsidian-mcp` sits on the shared proxy network — a private /24 — with dozens of other containers**, several of which execute user-supplied code as their purpose. Traefik holds an address on that same network. Nothing publishes obsidian-mcp's `:8000` to the host, but every peer on that network can reach it directly.
- **`OAuthClient` is constructed in exactly one place** — `src/oauth/routes.py:501`, inside `POST /register`. There is no pre-provisioned or static client anywhere in the tree, and no panel path that creates one.

Constraints the change must respect:

- `docker-compose.yml` is **public** and the deploy-directory copy must stay byte-identical (`CLAUDE.md`, "Public repo — host paths live outside the tree"). No hostname, no subnet, no path may be written into it; `${MCP_HOSTNAME}` is the only spelling of the host.
- The host's Traefik **static** configuration lives outside this repo. A control expressed there is not reproducible from the tree — the same reasoning already recorded in the `docker-compose.yml` comment that rejects a Traefik `ratelimit` middleware.
- `--workers 1` is load-bearing. Every existing rate control is in-process worker memory (`docs/architecture/rate-limits.md`, "Limiter state is in-process"). A new one inherits that contract and no other.
- Security-event fields are allow-listed **per event**, caller-supplied values are **never** suppressor subjects, and every record is bounded (`docs/architecture/security-event-logging.md`).
- Deleting an `OAuthClient` cascades `oauth_codes` and `oauth_tokens` through `ON DELETE CASCADE`, and `usage_logs.oauth_token_id` is `ON DELETE SET NULL`. An expiry sweep therefore has real destructive reach and must be proven not to touch a live grant.

## Goals / Non-Goals

**Goals:**
- A client that speaks plaintext to a machine endpoint **fails**, visibly, with no `Location` header and without the request reaching the application.
- Exactly one place in the tree states which peers may set `X-Forwarded-*`, validated at boot, with today's behaviour as its default so a deploy without an `.env` change cannot break client-IP resolution.
- A per-account bound on online password guessing that an attacker cannot buy their way around by rotating addresses, and that cannot be turned into a permanent lockout of the owner.
- An `oauth_clients` table that does not grow without bound, with a deletion predicate that provably cannot reach a client holding a live credential or a pending authorization.
- Every requirement testable by something named in `tasks.md`.

**Non-Goals:**
- **Giving Traefik a static IP on a dedicated backend network.** This is the actual fix for #189's threat model and it is host infrastructure, not repository content: it means editing the host's own compose project, creating a network, and moving dozens of containers' expectations. Filed as a follow-up issue for the owner. `TRUSTED_PROXY_IPS` is what makes that follow-up a one-line `.env` change instead of a code change.
- **Changing the host's `http-catchall` router.** Excluding this host there would be simpler and is rejected for the reason the tree already records about Traefik-side rate limits: it is not reproducible from the repository, so a rebuild of the host loses it silently.
- **A semantically ideal `426 Upgrade Required`.** No Traefik middleware can synthesise an arbitrary status; the only way is to forward to an application route, which is exactly what this change exists to prevent. 403 it is.
- **Recovering the credential that was already sent.** By the time Traefik answers, the client has put its bearer token on the wire in cleartext. Nothing at this layer can undo that. The goal is that it goes no further and that the client is told, loudly, on the first request rather than never.
- **Rate limiting `/register` further, or authenticating it.** RFC 7591 dynamic registration is unauthenticated by design and the 3/min limit stands. This change bounds the *residue*, not the arrival rate.
- **Deleting OAuth clients that are in use but idle.** A client with a live token, a pending code, or a recorded use is never a candidate, however old.
- **A general per-account lockout, admin unlock flow, or CAPTCHA.** Out of scope and, in the case of lockout, actively rejected — see D6.

## Decisions

**D1 — The plaintext refusal is an `ipAllowList` middleware with an impossible source range, on a `priority=200` router on the `http` entrypoint.**
Traefik v3 has no "return this status" middleware. `ipAllowList` is the one built-in that **terminates the request itself**: a source outside the range gets `403 Forbidden` and the service is never contacted. The range is `192.0.2.0/32` — RFC 5737 TEST-NET-1, reserved for documentation and never routable — so the allow-list is empty in practice while remaining a syntactically valid CIDR. No `ipStrategy` is configured, so the check uses the direct TCP peer; that is correct here because the `http` entrypoint trusts no forwarded headers (Context), so a forged `X-Forwarded-For` cannot fabricate an allowed source.

```
obsidian-mcp-plaintext-rtr:
  entrypoints = http
  priority    = 200
  rule = Host(`${MCP_HOSTNAME}`)
         && !PathPrefix(`/.well-known/acme-challenge/`)
         && ( PathPrefix(`/mcp`) || PathPrefix(`/transfer`) || Path(`/health`)
           || PathPrefix(`/.well-known`) || Path(`/register`)
           || Path(`/token`) || Path(`/revoke`) )
  middlewares = obsidian-mcp-no-plaintext
  service     = obsidian-mcp-svc      # never reached
obsidian-mcp-no-plaintext:
  ipallowlist.sourcerange = 192.0.2.0/32
```

The router still needs a `service`; Traefik requires one and the middleware short-circuits before it. Pointing it at the existing `obsidian-mcp-svc` avoids inventing a second service definition, and the test matrix proves the app never sees the request.

*Alternatives considered.* The **`errors` middleware** only fires on a status the *backend* returned, so it forwards first — it cannot refuse. A **tiny application route answering 4xx** does reach the goal of "no redirect", but carries the credential into the app over plaintext across the Docker network, spends app resources on unauthenticated traffic, and would require the application to learn which entrypoint it was reached on (it cannot: `X-Forwarded-Proto` on `:80` is untrusted there) — rejected. **Not routing the host on `http` at all** is impossible: `HostRegexp(`.+`)` catches everything, which is the finding. **`redirectregex` to an error page** is still a redirect. A **Traefik plugin** returning a fixed status needs an entry in the host's static configuration and a plugin download — outside the tree, same rejection as editing the catch-all.

**D2 — `/health` is in the refusal set; `/admin` and `/authorize` are not.**
`/health` is machine-facing, has no browser use, and the container's own healthcheck calls `http://localhost:8000/health` **inside the container**, bypassing Traefik entirely — so refusing plaintext at the edge cannot affect container health. The asymmetry with `/admin` is the whole ASVS point: a person who typed a hostname benefits from a redirect and leaks nothing; a machine holding a long-lived bearer token benefits from a failure. The one real risk is an external uptime monitor configured with `http://…/health`; that is a *pre-deploy check* in `tasks.md`, not a reason to weaken the rule, and if one exists the fix is to correct the monitor.

**D3 — The ACME challenge prefix is carved out with a negated matcher.**
Traefik v3 supports `!` in rules. `!PathPrefix(`/.well-known/acme-challenge/`)` is placed **before** the path group so the exemption cannot be lost by someone editing the group. The ACME HTTP-01 resolver's challenge lives on this entrypoint; Traefik's internal ACME router carries a maximal priority and should win regardless, but a security control that depends on an undocumented internal priority is a control that breaks on a Traefik upgrade. Obsidian-mcp's own certificates come from a DNS challenge, so this protects *other* hosts on the same proxy from our rule — which is precisely the kind of blast radius a repo-local change should not have.

**D4 — `Caddyfile.example` gets the same refusal, expressed natively.**
The repository publishes two reference deployments. Caddy has `respond "…" 403`, so the published stack can express the rule directly with an `http://{$MCP_HOSTNAME}` site block that refuses the machine paths and redirects everything else. Leaving it out would publish a weaker reference than the one we run, which is the opposite of what a reference is for. `docker-compose.proxy.yml` (bring-your-own-proxy) gets a documented rule in `DEPLOYMENT.md` and nothing else — there is no proxy there to configure.

**D5 — One proxy-trust setting, and uvicorn's layer is switched off rather than aligned.**
`TRUSTED_PROXY_IPS: Annotated[list[str], NoDecode]`, defaulting to `["127.0.0.1", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"]` — today's *effective* behaviour, so a deploy with an untouched `.env` changes nothing. `NoDecode` for the reason `fts_configs` and `oauth_known_redirect_hosts` already carry it: a bare `list[str]` is JSON-decoded by pydantic-settings, so the comma-separated form an operator naturally writes would abort startup. Every entry is parsed with `ipaddress.ip_network(entry, strict=False)` (a bare address is a valid `/32`), and a malformed entry **refuses startup naming the entry** rather than silently dropping it — a trust list that quietly loses a range is worse than one that fails. The house `_OFF_SPELLINGS` convention applies: an empty value, `null` or `none` means the middleware is not installed at all, which is the correct configuration for a directly-exposed deployment.

The Dockerfile's `--proxy-headers --forwarded-allow-ips 172.16.0.0/12,10.0.0.0/8` becomes `--no-proxy-headers`. Merely *aligning* the two lists was rejected: uvicorn's `proxy_headers` defaults to `True` and its `forwarded_allow_ips` reads `$FORWARDED_ALLOW_IPS`, so "two controls that happen to agree" is a state an operator can break from the environment without touching either file. `--no-proxy-headers` makes the app-level middleware the only one that exists, and it is the one every architecture note and every test already names. The effective list is logged once at startup at INFO, because a trust boundary nobody can read from the logs is a trust boundary nobody audits.

**D6 — The username budget is a pre-verification failure counter, and the lockout-DoS answer is four properties, not one.**
slowapi cannot express this. `src/limiter.py` documents why in its `session_user_key` docstring: `key_func` is synchronous, runs before the handler, and reading the request body there consumes the stream the handler needs. The submitted username is in the form body. So the budget lives in `src/services/rate_limits.py` beside `check_auth_failures`/`record_auth_failure` and reuses their machinery verbatim — a fixed-size slot table indexed by a per-process-salted hash of the key, so an attacker submitting random usernames cannot grow memory (`MCP_AUTH_FAILURE_TABLE_SIZE`'s reason, applied to a second key space). New settings: `PANEL_LOGIN_FAILURE_LIMIT` (10, `NullableLimit`), `PANEL_LOGIN_FAILURE_WINDOW_SECONDS` (900), `PANEL_LOGIN_FAILURE_TABLE_SIZE` (1024).

The check runs **before** the password comparison, because a budget consulted after `verify_password` bounds nothing — the guess has already been answered. That is what makes a lockout possible in principle, so the DoS is closed by construction rather than by hoping:

1. **Only failures are counted and the window is short.** Nothing is durable, there is no admin unlock, and the counter self-heals 15 minutes after the last failure. An attacker must sustain a flood *continuously* to keep the owner out, and the cost of doing so is bounded by the existing 5/min IP limiter and the fact that each forged address buys 5 attempts per minute, not unlimited ones.
2. **An authenticated session is unaffected.** The owner who is already signed in never touches `POST /admin/auth/login` — `login_form` short-circuits a valid session to the panel. A flood therefore cannot evict anyone; it can only delay a *fresh* password sign-in.
3. **The threshold is modest, not tight.** Ten failures in fifteen minutes is far above any human typo rate and far below an online guessing budget worth having.
4. **The refusal is byte-identical to an ordinary failed login.** Same 401, same rendered page, same "Invalid credentials" string — not a 429 and not a distinct message. A distinguishable throttled response is a username oracle twice over: it confirms the account exists, and it tells an attacker which names are under attack by someone else. The existing rule that the three `panel_login_failed` reasons are byte-identical in the response and distinguished only in the log is extended, not broken. The asymmetry with the IP limiter's visible 429 is deliberate and stated: that limiter keys on something the caller already knows about themselves.

**What remains, stated rather than hidden:** a sustained flood can deny *password* sign-in for one named account for the duration of the flood plus fifteen minutes. That is a real availability cost, accepted, and strictly preferable to unbounded per-account guessing against credentials that guard other tenants' vaults. Recovery without waiting is a container restart, which clears in-process state; that is documented, not a feature.

**D7 — The new event is `panel_login_account_throttled`, subject = client address, and `username_submitted` is bounded.**
Fields: `client_ip`, `route`, `username_submitted`, `limit_count`, `window_seconds` — added to the per-event allow-list and to the catalogue table in `docs/architecture/security-event-logging.md`. The suppressor subject is the **client address**, never the submitted username: the doc's rule is categorical that caller-supplied values are never subjects, because rotating them mints fresh logging allowances. The password never appears, in any field or message, and the existing canary test that captures a minted secret and asserts its absence from every record is extended to the submitted password.

Logging an attacker-chosen username is safe and already precedented — `panel_login_failed` carries `username_submitted` today — but only once it is **bounded**. It is not bounded today: `login_submit` takes `username: str = Form(...)`, normalises it, and hands it to `security_events.emit` with no length check, so an attacker can write an arbitrary number of bytes into the security log through a field the log already trusts. It is truncated to 255 characters — the `users.username` column width, so no value that could ever match a real account is altered — before it enters a record *or* a budget key. This is a pre-existing defect found while designing the slice; it is fixed here because it is the same field in the same function, and it is called out rather than folded in silently.

**D8 — Slice C needs migration 025, and the no-migration designs were each rejected on correctness.**
The predicate wants "this client has never been used". Three candidate signals exist and none is sound:

- **`user_id IS NULL`** looks perfect — `/authorize` binds a client to its first authorizing user and never rebinds. But `src/oauth/routes.py:819` says plainly that in **single-user mode** `session_user_id` stays `None` and `client_row.user_id` stays NULL forever. Every client in a single-user deployment — the one `DEPLOYMENT.md` walks a new operator through — would look unused. Rejected.
- **Absence of child rows.** `cleanup_expired_tokens` deletes a **used** `OAuthCode` immediately (no age gate at all) and deletes tokens seven days after `expires_at`. So a client that was genuinely used, whose grant was revoked and whose rows aged out, is indistinguishable from one that never was. For a confidential client holding a `client_secret` the user configured once, deletion is a hard break, not a transparent re-registration. Rejected as the *sole* signal; retained as a mandatory additional guard.
- **`usage_logs.actor_ref`**, which holds `client_id` and survives credential deletion by design. Tempting, and it needs no migration — but it records *tool calls*, not token issuance, so a client that authorized and never called a tool has no row; and it silently couples OAuth retention to an analytics table whose retention policy may change later, which is exactly the class of invisible semantic drift this project's architecture notes exist to prevent. Rejected.

So: **migration 025 adds `oauth_clients.last_used_at TIMESTAMPTZ NULL`**, stamped in the same transaction that issues an authorization code and again when a token is minted. It follows the house pattern for an added column — a single marked unit with an ownership marker mirrored between the migration and `src/models/db.py`, refusing a pre-existing column of another shape, exactly as migrations 016 and 017 do. The backfill sets `last_used_at` to the newest `created_at` among that client's surviving `oauth_tokens` and `oauth_codes` rows, and leaves it NULL where there is none: that infers use only from evidence that still exists, and never guesses. A client whose evidence already aged out before the migration is protected instead by its `created_at` only if it is younger than the cutoff — an accepted limitation, listed below.

**D9 — The sweep locks its candidates and re-checks inside the lock; the loser of the race gets a clean OAuth error.**
The naive single-statement `DELETE … WHERE NOT EXISTS (…)` has a real race, not a theoretical one. Inserting an `oauth_codes` row takes a `FOR KEY SHARE` lock on the parent `oauth_clients` row (the FK); a concurrent `DELETE` takes `FOR UPDATE`. They conflict, so they serialise — but under READ COMMITTED, when the `DELETE` unblocks, PostgreSQL re-evaluates only the *target row's* predicate, **not** the `NOT EXISTS` subquery. The delete proceeds and `ON DELETE CASCADE` removes the code that was just issued. The sweep therefore:

1. `SELECT client_id FROM oauth_clients WHERE last_used_at IS NULL AND created_at < :cutoff … FOR UPDATE SKIP LOCKED` (bounded batch);
2. re-reads `last_used_at` and both child tables **inside the same transaction, under the lock**;
3. deletes only what still qualifies.

Because `/authorize` stamps `last_used_at` with an `UPDATE` on the same parent row, the two paths take conflicting locks on one row and one of them wins cleanly. If the sweep wins, `/authorize`'s conditional `UPDATE … RETURNING` matches zero rows and the code insert would violate the FK; that branch returns `invalid_client` — an ordinary OAuth error the client recovers from by re-registering, which is what RFC 7591 clients do — and **never a 500 and never a half-written grant**. `SKIP LOCKED` means a contended row is simply left for the next pass, five minutes later.

**D10 — Expiry is configurable, defaulted on, and disable-able.**
`OAUTH_CLIENT_UNUSED_EXPIRY_DAYS: NullableLimit = 30`, `ge=1` like every other bound in `config.py` (a zero-day window would delete a registration the moment it was made), `null` disables the sweep entirely. Thirty days is chosen against the actual use: a DCR client registers and authorizes within seconds, so any gap over a few hours is already anomalous; thirty days is two orders of magnitude of slack for an operator who registered a connector and got distracted, while still bounding the table at roughly a month of the 3/min limiter's output rather than forever. The sweep runs inside the existing `cleanup_expired_tokens` — it is the same job, on the same five-minute tick, with the same "delete what is provably dead" mandate — and logs one INFO line with the count when it deletes anything, because a job that silently deletes credentials is a job nobody can audit.

## Risks / Trade-offs

- [The 403 breaks a client that legitimately spoke http and relied on the redirect] → that is the finding, not a regression: the redirect was repairing a credential leak. The `curl` matrix in `tasks.md` enumerates every affected path before deploy, and rollback is reverting two label blocks with no state to unwind.
- [An external uptime monitor polls `http://…/health`] → explicit pre-deploy check (task 1.1); if one exists, the monitor is corrected, not the rule.
- [The `ipAllowList` trick is a Traefik idiom, not a documented "deny" middleware, and could change behaviour on upgrade] → pinned by the post-deploy `curl` check, which is a behavioural assertion and not a config assertion; a Traefik upgrade that changed it would fail that check. Recorded in `DEPLOYMENT.md` so the next operator knows *why* the range is `192.0.2.0/32` and does not "fix" it.
- [Our `/.well-known` rule shadows another host's ACME challenge] → the `Host()` matcher already scopes it to one host, and D3's negated prefix is a second, independent guard. Verified live by requesting `http://<host>/.well-known/acme-challenge/probe` and asserting it is **not** 403.
- [`--no-proxy-headers` plus a mis-set `TRUSTED_PROXY_IPS` loses client-IP resolution entirely, blinding every limiter] → the default is the current effective list, so no `.env` change is needed to preserve today's behaviour; a malformed value refuses startup rather than degrading; the effective list is logged at boot; and a regression test asserts the default trusts a private-network peer (`192.168.0.10`) and rewrites from `X-Forwarded-For`.
- [The username budget denies a fresh sign-in during a flood] → D6, accepted and bounded to the window; no durable state, no lockout, sessions unaffected, container restart clears it.
- [The throttle becomes a username oracle] → D6.4, byte-identical response; a test asserts the throttled and non-throttled 401s are identical in status, headers and body.
- [Migration 025 on a deploy] → additive, nullable, no default, no backfill of data the database does not already hold; `make test-schema` gates it and `alembic check` must be clean afterwards.
- [The sweep deletes a client somebody still has configured] → three independent guards (`last_used_at IS NULL`, no child row of either kind, `created_at` older than 30 days) all inside one `FOR UPDATE` transaction, plus an integration test per guard. A DCR client that is deleted re-registers transparently; a confidential client cannot reach the predicate once it has been used, because issuing its token stamps `last_used_at`.

## Migration Plan

Migration **025**, additive: `oauth_clients.last_used_at TIMESTAMPTZ NULL`, no server default, carrying the ownership marker in both the migration and `src/models/db.py` (byte-identical, as 019's marker requires). Backfill in the same migration from `MAX(created_at)` over that client's surviving `oauth_tokens` and `oauth_codes` rows; NULL where there is none. Downgrade drops the column only if it carries the marker. `make test-schema` before the deploy that carries it; `make db-check` after. The column is nullable and unread by any pre-025 code path, so the deploy is forward-compatible in both directions and needs no window. Slices A and B carry no schema change and can deploy independently of C if the owner prefers to stage them.

## Accepted limitations

Recorded here so they are not re-fixed when a reviewer re-reports them. Each is stated, not disguised.

1. **The credential has already crossed the wire.** Slice A stops the request at Traefik; it cannot un-send the bearer token the client put on `:80`. The value delivered is that the leak is *reported* on the first request instead of never, and that nothing downstream accepts it.
2. **`docker-compose.proxy.yml` deployments are undefended.** That file exists precisely because the proxy is somebody else's; `DEPLOYMENT.md` gains the rule in prose and that is all this repo can do.
3. **A co-tenant on the shared proxy network can still forge `X-Forwarded-For` under the default `TRUSTED_PROXY_IPS`.** This change makes the trust list narrowable in one `.env` line; it does not narrow it, because the correct narrow value requires Traefik to have a fixed address on a dedicated network, which is host infrastructure. Filed as a follow-up. The username-keyed budget in the same slice is the mitigation that does not depend on that follow-up landing.
4. **A sustained flood can deny fresh password sign-in for one account for the window's duration.** D6. Not a lockout: nothing durable, sessions unaffected, self-healing in 15 minutes.
5. **The username budget is per-worker.** It inherits the `--workers 1` contract like every other in-process control; a second worker would multiply the effective threshold by the worker count. Stated in `rate-limits.md` beside the existing warning rather than solved.
6. **A client used before migration 025 whose codes and tokens had already aged out, and which is older than the cutoff, is deleted on the first sweep.** The backfill can only infer use from rows that still exist. The window is narrow — a client in that state holds no live credential and has had none for at least seven days past its last token's expiry — and a DCR client re-registers transparently. Not worth a second signal.
7. **A client can be deleted between the moment it registers and the moment it first authorizes**, if that gap exceeds the configured age. D9 makes the *concurrent* race safe; it does not make a 30-day-delayed first authorization safe. The response is a clean `invalid_client`, not a 500.
8. **`/register` still admits ~4,320 registrations per day per address.** This change bounds the residue, not the arrival rate. A burst can still put 4,320 rows in the table for up to 30 days.
9. **Slice A is verified behaviourally, not by a unit test.** There is no way to unit-test another process's proxy configuration. The repository test asserts the *labels* are present and host-value-free; the named `curl` matrix is the gate that asserts the behaviour, and it runs against the live server after deploy.

## Open Questions

- **Does anything poll `http://<host>/health`?** Answerable only on the host, where any external uptime monitor's configuration lives. Task 1.1 checks it before the deploy; if something does, the monitor is corrected. Not blocking the proposal.
- **Owner call: does #194 close?** The residual this change closes is the last *code* item. What remains on #194 is the operator chore of setting `daily_request_limit` on five grandfathered keys, which is a panel action, not a commit. The change comments the residual closed and leaves the issue's state to the owner.
- **Owner call: 30 days, or shorter?** D10 argues 30. A DCR client that has not authorized within an hour is already anomalous, so 7 would also be defensible and bounds the table harder. Proposing 30 as the safer default; one `.env` value either way.
