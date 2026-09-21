## Context

Three edge findings, three mechanisms, no shared file. What binds them is the layer: each is a place where the boundary between the network and the application either cannot express what it trusts or quietly repairs a client's mistake instead of reporting it.

Facts established by reading the live host (read-only `docker inspect`, `docker ps`) and the Traefik v3 documentation, not assumed:

- **Traefik is v3** (the rule syntax and the available middlewares are what matter, and both are v3-wide). Entrypoints are `http` (`:80`), `https` (`:443`) and a separate internal port for the API. The API is enabled (`--api=true`, `--api.dashboard=true`) but **not** `--api.insecure`, so that internal port serves nothing — the dashboard is reached over `https` behind the SSO middleware chain. Providers are `docker` (`exposedByDefault=false`, scoped to the shared proxy network) and `file`.
- **The catch-all is on Traefik's own container labels**, not in the file provider and not in this repo: `traefik.http.routers.http-catchall.rule=HostRegexp(`.+`)`, `entrypoints=http`, `middlewares=redirect-to-https` (a `redirectscheme` to `https`). It declares **no explicit priority**, so Traefik's default applies: priority = the rule's length = `len("HostRegexp(`.+`)")` = **16**. Any rule we write is far longer, so we would outrank it by default — but a default that depends on a string length is not a contract, so the new router sets `priority=200` explicitly.
- **`forwardedHeaders.trustedIPs` is set on the `https` entrypoint only**, covering the shared proxy network. The `http` entrypoint trusts no forwarded headers, so an `ipAllowList` on `http` sees the true TCP peer with no strategy configuration needed.
- **An ACME HTTP-01 certificate resolver answers on the `http` entrypoint.** This deployment's own certificates come from a DNS challenge, but a router of ours matching `/.well-known` on `:80` sits in the same entrypoint as every other host's ACME challenge. Traefik's internal ACME router carries a maximal priority and should win, but "should" is not a design; the rule carves the prefix out explicitly.
- **`obsidian-mcp` sits on the shared proxy network — a private /24 — with dozens of other containers**, several of which execute user-supplied code as their purpose. Traefik holds an address on that same network. Nothing publishes obsidian-mcp's `:8000` to the host, but every peer on that network can reach it directly.
- **`ipAllowList` refuses with `403` by default and accepts an explicit `rejectStatusCode`** (label spelling `…ipallowlist.rejectstatuscode`). It rejects before calling the service.
- **`HeaderRegexp(`key`, `regexp`)` is a v3 matcher, `!` negates any matcher, and regexps are Go-flavoured, so `(?i)` works.** This repo already relies on `HeaderRegexp(`Authorization`, `^Bearer `)` on the `https` root router, so the matcher is confirmed both by the documentation and by a rule running in production.
- **There is an existing Bearer-qualified root MCP entry point.** `obsidian-mcp-root-rtr` serves `Path(`/`) && HeaderRegexp(`Authorization`, `^Bearer `)` on `https`, and `RootMCPProxyMiddleware` rewrites exactly `/` → `/mcp/` for clients that strip the prefix. `Caddyfile.example` has the same `@rootMcp` matcher. Any plaintext refusal that omits it leaves the original leak open on a path both reference deployments support.
- **`OAuthClient` is constructed in exactly one place** — `src/oauth/routes.py`, inside `POST /register`. There is no pre-provisioned or static client anywhere in the tree, and no panel path that creates one.
- **`src/logging_setup.py` already truncates `username_submitted` to 64 characters** (`ALLOWED_FIELDS` declares `_S(str, 64)`, applied by `coerce_field`). The security log is therefore already bounded against a long submitted username.
- **`login_submit` runs `verify_password` only for an active row.** An unknown username and an inactive account both skip bcrypt today. That pre-existing timing difference is load-bearing context for D6.
- **`make test-schema` runs exactly three modules**, one of which is `tests/integration/test_schema_check.py`, whose `HEAD_REVISION` literal is `"024"`. A new migration that does not advance that literal fails the gate's head assertion.

Constraints the change must respect:

- `docker-compose.yml` is **public** and the deploy-directory copy must stay byte-identical (`CLAUDE.md`, "Public repo — host paths live outside the tree"). No hostname, no subnet, no host path may be written into it; `${MCP_HOSTNAME}` is the only spelling of the host.
- The host's Traefik **static** configuration lives outside this repo. A control expressed there is not reproducible from the tree — the same reasoning already recorded in the `docker-compose.yml` comment that rejects a Traefik `ratelimit` middleware.
- `--workers 1` is load-bearing. Every existing rate control is in-process worker memory (`docs/architecture/rate-limits.md`). A new one inherits that contract and no other.
- Security-event fields are allow-listed **per event**, caller-supplied values are **never** suppressor subjects, and every record is bounded by the formatter (`docs/architecture/security-event-logging.md`).
- Deleting an `OAuthClient` cascades `oauth_codes` and `oauth_tokens` through `ON DELETE CASCADE`, and `usage_logs.oauth_token_id` is `ON DELETE SET NULL`. An expiry sweep therefore has real destructive reach and must be proven not to touch a live grant.
- **A sibling PR (#273) rewrites the schema gate's head requirement.** It is not yet merged. See D11.

## Goals / Non-Goals

**Goals:**
- A client that speaks plaintext to a machine endpoint **fails**, visibly, with no `Location` header and without the request reaching the application — including on the Bearer-qualified root path.
- Exactly one place in the tree states which peers may set `X-Forwarded-*`, validated *and canonicalised* at boot, with today's behaviour as its default so a deploy without an `.env` change cannot break client-IP resolution.
- A per-account bound on online password guessing that an attacker cannot buy their way around by rotating addresses, that cannot be turned into a permanent lockout, and that **cannot affect an account other than the one being attacked**.
- An `oauth_clients` table that does not grow without bound, with a deletion predicate that provably cannot reach a client holding a live credential, a pending authorization, or any registration that predates the marker.
- Every requirement testable by something named in `tasks.md`.

**Non-Goals:**
- **Giving Traefik a static IP on a dedicated backend network.** This is the actual fix for #189's threat model and it is host infrastructure, not repository content: it means editing the host's own compose project, creating a network, and moving dozens of containers' expectations. Filed as a follow-up issue for the owner. `TRUSTED_PROXY_IPS` is what makes that follow-up a one-line `.env` change instead of a code change.
- **Changing the host's `http-catchall` router.** Excluding this host there would be simpler and is rejected for the reason the tree already records about Traefik-side rate limits: it is not reproducible from the repository, so a rebuild of the host loses it silently.
- **Recovering the credential that was already sent.** By the time Traefik answers, the client has put its bearer token on the wire in cleartext. Nothing at this layer can undo that. The goal is that it goes no further and that the client is told, loudly, on the first request rather than never.
- **Rate limiting `/register` further, or authenticating it.** RFC 7591 dynamic registration is unauthenticated by design and the 3/min limit stands. This change bounds the *residue*, not the arrival rate.
- **A general per-account lockout, admin unlock flow, or CAPTCHA.** Out of scope and, in the case of lockout, actively rejected — see D6.
- **Equalising login response *timing*.** `verify_password` already runs only for an active row, so unknown and inactive accounts already answer faster than a wrong password does. This change does not widen that difference for any account that has a budget, and does not attempt to close a pre-existing one. D6 defines equivalence over response *content*, explicitly excluding timing.

## Decisions

**D1 — The plaintext refusal is an `ipAllowList` middleware with an impossible source range, on a `priority=200` router on the `http` entrypoint.**
`ipAllowList` **terminates the request itself**: a source outside the range is refused and the service is never contacted. The range is `192.0.2.0/32` — RFC 5737 TEST-NET-1, reserved for documentation and never routable — so the allow-list is empty in practice while remaining a syntactically valid CIDR. No `ipStrategy` is configured, so the check uses the direct TCP peer; that is correct here because the `http` entrypoint trusts no forwarded headers (Context), so a forged `X-Forwarded-For` cannot fabricate an allowed source.

**403 is the chosen status, not a limitation.** `ipAllowList` refuses with 403 by default and also accepts `rejectStatusCode`, so another 4xx was available and 403 was selected: it is the accurate meaning ("this source may not use this endpoint"), and a status the middleware returns natively needs no extra configuration to drift.

```
obsidian-mcp-plaintext-rtr:
  entrypoints = http
  priority    = 200
  rule = Host(`${MCP_HOSTNAME}`)
         && !PathPrefix(`/.well-known/acme-challenge/`)
         && ( PathPrefix(`/mcp`) || PathPrefix(`/transfer`) || Path(`/health`)
           || PathPrefix(`/.well-known`) || Path(`/register`)
           || Path(`/token`) || Path(`/revoke`)
           || ( Path(`/`) && HeaderRegexp(`Authorization`, `(?i)^Bearer `) ) )
  middlewares = obsidian-mcp-no-plaintext
  service     = obsidian-mcp-svc      # never reached
obsidian-mcp-no-plaintext:
  ipallowlist.sourcerange = 192.0.2.0/32
```

The router still needs a `service`; Traefik requires one and the middleware short-circuits before it. Pointing it at the existing `obsidian-mcp-svc` avoids inventing a second service definition, and the test matrix proves the app never sees the request.

*Alternatives considered.* The **`errors` middleware** only fires on a status the *backend* returned, so it forwards first — it cannot refuse. A **tiny application route answering 4xx** does reach the goal of "no redirect", but carries the credential into the app over plaintext across the internal network, spends app resources on unauthenticated traffic, and would require the application to learn which entrypoint it was reached on (it cannot: `X-Forwarded-Proto` on `:80` is untrusted there) — rejected. **Not routing the host on `http` at all** is impossible: `HostRegexp(`.+`)` catches everything, which is the finding. **`redirectregex` to an error page** is still a redirect. A **Traefik plugin** needs an entry in the host's static configuration and a plugin download — outside the tree, same rejection as editing the catch-all.

**D1a — The Bearer-qualified root path is in the refusal set.**
`Path(`/`) && HeaderRegexp(`Authorization`, `(?i)^Bearer `)` is appended to the path group. Without it the exact leak this change exists to close stays open on a route **both** reference deployments already support: `POST http://<host>/` with a bearer credential takes the catch-all redirect, replays over HTTPS into `obsidian-mcp-root-rtr`, and `RootMCPProxyMiddleware` rewrites it to `/mcp/` — a silent success. The header qualifier keeps ordinary browser behaviour intact: a plain `GET http://<host>/` carries no `Authorization`, matches nothing in this rule, and still redirects.

The refusal spells the matcher `(?i)^Bearer ` where the existing `https` root router spells it `^Bearer `. The asymmetry is deliberate and safe in one direction only: a lowercase `authorization: bearer …` is refused on `:80` but would not have matched the root router on `:443` anyway, so the refusal is strictly broader than the route it protects. Being broader on a *refusal* costs nothing; being narrower would reopen the hole.

**D2 — `/health` is in the refusal set; `/admin` and `/authorize` are not.**
`/health` is machine-facing, has no browser use, and the container's own healthcheck calls `http://localhost:8000/health` **inside the container**, bypassing Traefik entirely — so refusing plaintext at the edge cannot affect container health. The asymmetry with `/admin` is the whole ASVS point: a person who typed a hostname benefits from a redirect and leaks nothing; a machine holding a long-lived bearer token benefits from a failure. The one real risk is an external uptime monitor configured with `http://…/health`; that is a *pre-deploy check* in `tasks.md`, not a reason to weaken the rule.

**D3 — The ACME challenge prefix is carved out with a negated matcher.**
Traefik v3 supports `!` in rules. `!PathPrefix(`/.well-known/acme-challenge/`)` is placed **before** the path group so the exemption cannot be lost by someone editing the group. The ACME HTTP-01 resolver's challenge lives on this entrypoint; Traefik's internal ACME router carries a maximal priority and should win regardless, but a security control that depends on an undocumented internal priority is a control that breaks on a Traefik upgrade. Obsidian-mcp's own certificates come from a DNS challenge, so this protects *other* hosts on the same proxy from our rule — which is precisely the kind of blast radius a repo-local change should not have.

**D4 — `Caddyfile.example` gets the same refusal, expressed natively, including the root path.**
The repository publishes two reference deployments. Caddy has `respond "…" 403`, so the published stack can express the rule directly with an `http://{$MCP_HOSTNAME}` site block that refuses the machine paths **and the `@rootMcp` bearer matcher it already defines**, exempts the ACME prefix, and redirects everything else. Leaving it out would publish a weaker reference than the one we run. `docker-compose.proxy.yml` (bring-your-own-proxy) gets a documented rule in `DEPLOYMENT.md` and nothing else — there is no proxy there to configure.

**D5 — One proxy-trust setting, canonicalised, and uvicorn's layer switched off rather than aligned.**
`TRUSTED_PROXY_IPS: Annotated[list[str], NoDecode]`, defaulting to `["127.0.0.1", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"]` — today's *effective* behaviour, so a deploy with an untouched `.env` changes nothing. `NoDecode` for the reason `fts_configs` and `oauth_known_redirect_hosts` already carry it: a bare `list[str]` is JSON-decoded by pydantic-settings, so the comma-separated form an operator naturally writes would abort startup.

**Validation is not enough; the stored value must be canonical.** `ipaddress.ip_network(entry, strict=False)` *accepts* `192.168.0.10/24` and returns `192.168.0.0/24`, but if the original string is what reaches the middleware, uvicorn's own stricter parse fails on the host bits and keeps it as a literal that matches no peer. The failure is silent and its consequence is the opposite of the intent: **every** proxied request then retains the proxy's address, so all callers share one limiter bucket. The validator therefore stores `str(ip_network(entry, strict=False))` — a bare address stays bare — and that canonical list is what reaches both the middleware and the startup log. `192.168.0.10/24` → `192.168.0.0/24` is an explicit regression test **against the real uvicorn middleware**, not against our own parser.

A malformed entry **refuses startup naming the entry** rather than being silently dropped — a trust list that quietly loses a range is worse than one that fails. The house `_OFF_SPELLINGS` convention applies: an empty value, `null` or `none` means the middleware is not installed at all, which is the correct configuration for a directly-exposed deployment.

The Dockerfile's `--proxy-headers --forwarded-allow-ips …` becomes `--no-proxy-headers`. Merely *aligning* the two lists was rejected: uvicorn's `proxy_headers` defaults to `True` and its `forwarded_allow_ips` reads `$FORWARDED_ALLOW_IPS`, so "two controls that happen to agree" is a state an operator can break from the environment without touching either file. The effective list is logged once at startup at INFO, because a trust boundary nobody can read from the logs is a trust boundary nobody audits.

**D6 — The username budget is keyed exactly, by user id, and only for usernames that resolve to an account.**
slowapi cannot express this at all: `src/limiter.py` documents that `key_func` is synchronous, runs before the handler, and reading the request body there consumes the stream the handler needs. The submitted username is in the form body. So the budget lives in `src/services/rate_limits.py`.

**It does not reuse the salted hash-slot table.** That machinery merges colliding keys into one counter, which is correct for the `/mcp` failed-auth budget (where the key space is unbounded client addresses and a merged bucket is a *bound*, never a bypass) and wrong here: ten failures against a colliding name would refuse a different account's **correct password**, and an attacker submitting random non-existent usernames could saturate every slot without knowing any victim's name. That is a cross-account denial, far beyond the accepted "one named account during a flood".

Instead: an **exact-keyed in-process map from `users.id` to a fixed-window counter**, and a failure is counted **only when the submitted username resolves to a `users` row**. Three properties follow directly:

- **Bounded without collisions.** The key space is the set of accounts, which is small and administrator-controlled; entries expire with the window, so the map holds at most one entry per account that has failed recently. No slot table, no salt, no capacity-exhaustion policy to get wrong.
- **An unknown username gets no budget at all.** There is nothing to brute-force behind a name that matches no account, so there is nothing to bound. Such attempts remain under the existing 5/min address limit, which is the control appropriate to them. This is what makes "a different username is unaffected" literally true rather than probabilistically true.
- **Keying covers inactive accounts too**, by row rather than by active flag. An inactive account cannot be signed into regardless, so the budget is moot for it — but branching on the flag would make budget behaviour a side channel for account state, and there is no reason to introduce one.

The check runs **after** the user lookup (it needs the id) and **before** the password comparison, because a budget consulted after `verify_password` bounds nothing — the guess has already been answered. That is what makes a lockout possible in principle, so the DoS is closed by construction:

1. **Only failures are counted and the window is short.** Nothing is durable, there is no admin unlock, and the counter self-heals 15 minutes after the last failure. An attacker must sustain a flood *continuously*, and the cost is bounded by the existing 5/min address limiter.
2. **An authenticated session is unaffected.** `login_form` short-circuits a valid session to the panel, so a flood cannot evict anyone; it can only delay a *fresh* password sign-in.
3. **The threshold is modest, not tight.** Ten failures in fifteen minutes is far above any human typo rate and far below an online guessing budget worth having.
4. **The refusal is equivalent in content to an ordinary failed login.** Defined precisely in D6a.

New settings: `PANEL_LOGIN_FAILURE_LIMIT` (10, `NullableLimit`), `PANEL_LOGIN_FAILURE_WINDOW_SECONDS` (900).

**What remains, stated rather than hidden:** a sustained flood can deny *password* sign-in for one named account for the duration of the flood plus fifteen minutes. Accepted, and strictly preferable to unbounded per-account guessing against credentials that guard other tenants' vaults. Recovery without waiting is a container restart, which clears in-process state.

**D6a — "Indistinguishable" means response content under identical inputs, and excludes timing, CSRF and cookies.**
The claim that only the log distinguishes a throttled attempt needs a precise scope, because two things genuinely differ and pretending otherwise would be the same class of overstatement this review caught.

Equivalence is defined as: **the same HTTP status, the same rendered template, and the same user-visible message**, for the same submitted inputs. Explicitly **excluded**:

- **Timing.** A throttled attempt skips bcrypt and so answers faster than a wrong password against an active account. Part of this is pre-existing — an unknown username and an inactive account already skip it (`verify_password` runs only for an active row) — but part is **new and should not be dressed up as pre-existing**: for a single active account, attempts 1–10 run bcrypt and attempt 11 does not, which is a fresh signal distinguishing an exhausted budget from an unexhausted one. It follows unavoidably from consulting the budget before the comparison, which is what makes the budget a bound rather than an accounting exercise. It discloses budget state for a name the observer already knows exists, not whether the name exists and not whether a password is right. Accepted, recorded as limitation 5, and out of scope to equalise.
- **Per-response nondeterminism.** A freshly rendered login page carries a CSRF token and may carry a session cookie; these differ between any two responses, throttled or not, and are not part of the comparison.

This keeps the property that matters — an attacker cannot read the *content* of a response to learn that a name is under attack or that it exists — while stating exactly what it does not cover. The scenario "normal login is not affected" is correspondingly qualified: it holds **while both budgets are unexhausted**.

**D7 — The new event is `panel_login_account_throttled`, subject = client address, and the existing 64-character bound already covers it.**
Fields: `client_ip`, `route`, `username_submitted`, `limit_count`, `window_seconds`, added to the per-event allow-list and to the catalogue table. The suppressor subject is the **client address**, never the submitted username: the doc's rule is categorical that caller-supplied values are never subjects, because rotating them mints fresh logging allowances. The password never appears in any field or message, and the existing canary test is extended to assert that.

**The earlier claim that the submitted username is unbounded in the log was wrong.** `src/logging_setup.py` declares `username_submitted` as `_S(str, 64)` and `coerce_field` applies the truncation, so a 300-character submission already renders as 64 characters. There is no defect to fix and no 255-character requirement to add — a 255 bound would have *contradicted* the formatter and could not have held without editing a file this change never listed. What the spec keeps is the narrow, true statement: the new event's `username_submitted` is covered by that existing bound, so adding the event cannot introduce an unbounded log field. The budget key is an integer row id, so it is bounded by construction and needs no truncation at all.

**D8 — Slice C needs migration 025, and the backfill covers every pre-existing row.**
The predicate wants "this client has never been used". Three candidate signals exist and none is sound:

- **`user_id IS NULL`** looks perfect — `/authorize` binds a client to its first authorizing user and never rebinds. But in **single-user mode** the session user is `None` and the client's `user_id` stays NULL forever. Every client in a single-user deployment — the one `DEPLOYMENT.md` walks a new operator through — would look unused. Rejected.
- **Absence of child rows.** A **used** `OAuthCode` is deleted immediately (no age gate at all) and tokens are deleted seven days after `expires_at`. So a client that was genuinely used, whose grant was revoked and whose rows aged out, is indistinguishable from one that never was. Rejected as the *sole* signal; retained as a mandatory additional guard.
- **`usage_logs.actor_ref`**, which holds `client_id` and survives credential deletion by design. Needs no migration — but it records *tool calls*, not token issuance, so a client that authorized and never called a tool has no row; and it silently couples OAuth retention to an analytics table whose retention policy may change later. Rejected.

So: **migration 025 adds `oauth_clients.last_used_at TIMESTAMPTZ NULL`**, stamped in the same transaction that issues an authorization code and again at each token issuance path (consent approval, code exchange, refresh). It follows the house pattern for an added column — a single marked unit with an ownership marker mirrored between the migration and `src/models/db.py`, refusing a pre-existing column of another shape, exactly as migrations 016 and 017 do.

**The backfill sets a non-NULL marker on every row that exists when it runs.** Where surviving `oauth_tokens`/`oauth_codes` rows exist, the marker is the newest of their `created_at`. Where none exist — the ambiguous case — it is **the migration's own timestamp**, not NULL.

This is the correction the spec review forced, and the reasoning is worth keeping. The earlier design left those rows NULL and justified the resulting first-sweep deletion with "a DCR client re-registers transparently". That claim is false in a reachable case: RFC 7591 permits credentials to be packaged into client software by a developer, and a confidential client configured by hand months ago, whose token rows have long since been purged, would have its registration **and its secret hash** deleted on the first sweep, with no automatic recovery — re-registering mints a *different* `client_id` and secret that the configured client does not have. Stamping the migration's timestamp means such a row is never NULL, so it can never reach the predicate.

The consequence is a clean invariant instead of a probabilistic one: **after 025, `last_used_at IS NULL` means "registered after 025 and never used" and nothing else.** The sweep acts only on rows whose entire history is visible to it. The cost — genuinely unused registrations that predate 025 are never collected — is small, bounded by whatever is in the table today, and removable by the operator in the panel. Recorded as an accepted limitation.

**D9 — The sweep locks its candidates and re-checks inside the lock; the loser of the race gets a clean OAuth error.**
The naive single-statement `DELETE … WHERE NOT EXISTS (…)` has a real race. Inserting an `oauth_codes` row takes a `FOR KEY SHARE` lock on the parent `oauth_clients` row (the FK); a concurrent `DELETE` takes `FOR UPDATE`. They conflict, so they serialise — but under READ COMMITTED, when the `DELETE` unblocks, PostgreSQL re-evaluates only the *target row's* predicate, **not** the `NOT EXISTS` subquery. The delete proceeds and `ON DELETE CASCADE` removes the code that was just issued. The sweep therefore:

1. `SELECT client_id FROM oauth_clients WHERE last_used_at IS NULL AND created_at < :cutoff … FOR UPDATE SKIP LOCKED` (bounded batch);
2. re-reads `last_used_at` and both child tables **inside the same transaction, under the lock** — a separate statement gets a fresh READ COMMITTED snapshot, which is the point;
3. deletes only what still qualifies.

Because `/authorize` stamps `last_used_at` with an `UPDATE` on the same parent row, the two paths take conflicting locks on one row and one of them wins cleanly. If the sweep wins, `/authorize`'s conditional `UPDATE … RETURNING` matches zero rows and the code insert would violate the FK; that branch returns `invalid_client` — an ordinary OAuth error — and **never a 500 and never a half-written grant**. `SKIP LOCKED` leaves a contended row for the next pass.

**D10 — Expiry is configurable, defaulted on, and disable-able.**
`OAUTH_CLIENT_UNUSED_EXPIRY_DAYS: NullableLimit = 30`, `ge=1` like every other bound in `config.py` (a zero-day window would delete a registration the moment it was made), `null` disables the sweep entirely. Thirty days is chosen against the actual use: a DCR client registers and authorizes within seconds, so any gap over a few hours is already anomalous; thirty days is two orders of magnitude of slack while still bounding the table at roughly a month of the 3/min limiter's output. The sweep runs inside the existing `cleanup_expired_tokens` — same job, same five-minute tick, same "delete what is provably dead" mandate — and logs one INFO line with the count when it deletes anything.

**D11 — Migration 025 is integrated into the existing schema gate, and this change must be rebased after PR #273.**
A new migration is not covered by adding a new test file: `make test-schema` runs exactly three modules, and `tests/integration/test_schema_check.py` carries a single `HEAD_REVISION` literal (`"024"`) asserted in many places. Adding 025 without advancing it fails the gate — by design, since that literal exists precisely so a migration cannot silently widen what "head" means. Slice C therefore **owns that file**: it advances the literal to `025` and puts 025's marker, drift, downgrade and stamp-back-and-re-upgrade cases there, where the gate actually runs them. `tests/integration/test_asvs_client_expiry_pg.py` keeps only the behavioural expiry and race tests, which belong to `make test-integration`.

The published requirement governing that literal is `schema-integrity`'s **"The schema gate covers both migrations of this wave before deploy"**. Adding a second, parallel head requirement — as the first draft did — would leave two requirements naming different heads, which is exactly the contradiction the literal exists to prevent. So it is a **MODIFIED** delta, restating the requirement with head `025` and keeping every earlier scenario.

**The complication:** PR #273 is open and not merged, and it archives changes that rewrite this same requirement — moving the head literal to `024` with `023` in the chain, and adding a "the earlier waves' cases still run" scenario. A MODIFIED delta must restate the requirement as it will read *after* the change lands, so this delta is written against #273's version (read from `origin/archive-sweep-changes`), not against the current `openspec/specs/`. **This proposal must be rebased after #273 merges**, and the delta re-diffed against the then-current requirement before implementation starts. If #273 is abandoned, the delta reverts to restating the pre-#273 text with head `025`. This is a sequencing dependency on the change, not an optional tidy-up.

## Spec review history

| # | Severity | Finding | Resolution |
| --- | --- | --- | --- |
| 1 | MAJOR | Legacy-client deletion rested on a false "re-registers transparently" recovery guarantee; a hand-configured confidential client would lose its secret. | D8 rewritten: the 025 backfill stamps **every** pre-existing row (child-row max, else the migration's timestamp), so NULL now means "registered after 025". Claim removed; uncollected pre-025 junk recorded as accepted limitation 6. |
| 2 | MAJOR | The reused salted slot table merges colliding keys, so one name's failures could block an unrelated account, and random names could exhaust all slots. | D6 rewritten: exact map keyed by `users.id`, counted only for usernames that resolve to a row; unknown names get no budget. "A different username is unaffected" is now literally true. |
| 3 | MAJOR | `ip_network(strict=False)` accepts host bits but the un-canonicalised string is ignored by uvicorn, silently merging every caller into one limiter bucket. | D5: the validator stores `str(ip_network(...))`; the canonical list drives the middleware and the log; `192.168.0.10/24` → `192.168.0.0/24` tested against the real middleware. |
| 4 | MAJOR | The refusal omitted `/` carrying `Authorization: Bearer`, an entry point both reference deployments support, leaving the original leak open. | D1a: added to the rule, to the Caddy block (D4) and to the acceptance matrix; plain browser `GET /` still redirects. |
| 5 | MAJOR | 025 was not wired into the gate `make test-schema` runs; `HEAD_REVISION` stayed `024` and a second head requirement contradicted the published one. | D11: Slice C owns `test_schema_check.py`, advances the literal to `025`, and the delta MODIFIES the existing head requirement instead of adding one. |
| 6 | MINOR | "Only the log distinguishes a throttle" overstated: bcrypt is skipped, and CSRF/cookies differ per response. | D6a defines equivalence over status, template and message; excludes timing, CSRF and cookies; qualifies "normal login unaffected" with both budgets unexhausted. |
| 7 | MINOR | The "unbounded username log" claim was false — the formatter already truncates to 64 — and the proposed 255 bound could not hold. | D7: claim withdrawn, 255 requirement dropped; the spec now only asserts the existing 64-character bound covers the new event. |
| 8 | MINOR | The live check read a client IP from `usage_logs`, which records none; eleven rapid logins hit the 5/min limiter first. | Tasks 7.3: observe `auth_failure` (which carries `client_ip`) and `panel_login_succeeded`; the login exercise is paced under the address limit. |
| 9 | MINOR | A blanket ban on literal addresses contradicted the required `192.0.2.0/32` and the unchanged reference examples. | The requirement now bans **deployment-specific** values and explicitly exempts documentation constants and pre-existing reference examples. |
| 10 | MINOR | "No Traefik middleware can synthesise another status" is false — `rejectStatusCode` exists. | D1: 403 is described as the selected behaviour and `ipAllowList`'s default; the impossibility claim is gone. |

Round 2 confirmed r1 findings 2, 3, 7, 8 and 10 resolved, and raised six more:

| # | Severity | Finding | Resolution |
| --- | --- | --- | --- |
| r2-1 | MAJOR | The backfill invariant does not survive `make deploy`'s migrate-then-recreate gap: the old image can register and authorize a client without stamping it. | **DECLINED** as a code or process change — no drain, no rollback reconciliation, no integration case. Recorded as accepted limitation 10: real use restamps within the one-hour access-token life, and the child-row guard covers the client for ~37 days regardless, so the only reachable case is a registration abandoned for over a month. The "needs no window" claim in the Migration Plan is withdrawn. |
| r2-2 | MINOR | The Caddy task reused `@rootMcp`, whose `method POST GET DELETE` lets `HEAD /` with a bearer fall through to the redirect; the live matrix had no root probes. | Task 1.3 specifies a **method-independent** plaintext root matcher; 1.5 and 7.2 gain POST, GET and HEAD bearer-root probes and the unauthenticated-root redirect check. |
| r2-3 | MINOR | The MODIFIED schema delta dropped #273's "The ordering against the sibling migration holds" (024 → 023) and "Head at 024". | Both restored; the ordering scenario now carries 024 → 023 *and* 025 → 024. Re-diffed against the sibling branch — all five of its scenarios are accounted for. |
| r2-4 | MINOR | "However many unknown-name attempts" contradicts the retained 5/min address limit. | Scenario and task 2.8 qualified with an unexhausted address budget; the assertion is now that unknown names consume no **account** allowance. |
| r2-5 | MINOR | The timing limitation wrongly called the throttle signal pre-existing. | D6a and limitation 5 now name the **new** exhausted-vs-unexhausted signal explicitly; the security-event delta says "no response content distinguishes" rather than "only the log". |
| r2-6 | MINOR | Task 1.2's blanket literal-address ban contradicted the `192.0.2.0/32` it mandates in the same task. | Tasks 1.2 and 1.3 carry the deployment-specific qualification and the RFC 5737 exemption already in 1.5 and the delta. |

## Implementation review history

Round 1, 2026-09-21. Adversarial Codex against the merged implementation: **FAIL**, 1 MAJOR + 2 MINOR. `openspec-verifier`: 0 blocking gaps, 11 non-blocking. All three Codex findings were fixed; the verifier's non-blocking items were taken as far as they were coverage or hygiene rather than restatement.

| # | Severity | Finding | Resolution |
| --- | --- | --- | --- |
| i1 | MAJOR | `docker-compose.proxy.yml` and `docker-compose.simple.yml` override the image CMD, so both reference stacks launched uvicorn **without** `--no-proxy-headers`. With `FORWARDED_ALLOW_IPS` set, uvicorn rewrites the client address before the application middleware runs and `TRUSTED_PROXY_IPS` cannot undo it — D5's "exactly one control" held only for the Dockerfile. | Both overrides carry `--no-proxy-headers` and `--workers 1` (the CMD's other load-bearing flag, absent for no stated reason). The comments that said no such flag was needed now say why it is. The startup-command test no longer names the Dockerfile: it **globs** every tracked file that launches the server, so a compose file added later is covered without anyone remembering. |
| i2 | MINOR | A registration the sweep deleted mid-consent landed in the cross-user branch: 403 `access_denied` naming an owner that does not exist, plus an `oauth_cross_user_client_refused` record about a user who did nothing. | The owner re-read distinguishes a missing row from a stranger's: NULL there can only mean the row is gone (the conditional claim matched nothing, and an owner is never cleared), and it returns the same `invalid_client` the stamp's own loss returns, through one shared helper. The genuine conflict is byte-for-byte unchanged. |
| i3 | MINOR | The PostgreSQL race cases used `asyncio.sleep(0.5)` as synchronisation. A slow container lets the two transactions run serially — the positive case then passes having proved nothing about concurrency, and the naive-delete control can fail spuriously. | Sleeps replaced by synchronisation on real lock progress: `_wait_until_lock_blocked` polls `pg_stat_activity` for `wait_event_type = 'Lock'` under a bounded deadline that **fails** the test if the contention never happens. Both orders are forced — the sweep skipping a row the authorization holds (awaited to completion *inside* the open transaction, so no polling is needed and a blocking sweep would time out), and the **real consent handler** observably blocked on the sweep's `FOR UPDATE` until the sweep commits, then answering `invalid_client` with nothing written. |

Verifier items folded in: `OAUTH_CLIENT_UNUSED_EXPIRY_DAYS` documented in `.env.example`; a case proving the denormalised `usage_logs` actor columns survive a client delete (built on the only state that can reach the sweep — a row whose token was purged earlier); the two login-budget scenarios that had never been asserted (an authenticated session during a flood, and the retained 5/min address limit's 429); `172.18.0.2` added as a second parametrised peer for the default-trust case; and `DEPLOYMENT.md`'s `TRUSTED_PROXY_IPS` example replaced with a placeholder so no literal reads as a real address.

## Risks / Trade-offs

- [The 403 breaks a client that legitimately spoke http and relied on the redirect] → that is the finding, not a regression: the redirect was repairing a credential leak. The `curl` matrix enumerates every affected path before deploy, and rollback is reverting two label blocks with no state to unwind.
- [An external uptime monitor polls `http://…/health`] → explicit pre-deploy check (task 1.1); if one exists, the monitor is corrected, not the rule.
- [Adding the Bearer root matcher accidentally refuses ordinary browser traffic] → the matcher is header-qualified, so a plain `GET /` carries no `Authorization` and is untouched; the acceptance matrix asserts both directions.
- [The `ipAllowList` idiom could change behaviour on a Traefik upgrade] → pinned by the post-deploy `curl` check, which is a behavioural assertion and not a config assertion. Recorded in `DEPLOYMENT.md` so the next operator knows *why* the range is `192.0.2.0/32` and does not "fix" it.
- [Our `/.well-known` rule shadows another host's ACME challenge] → the `Host()` matcher already scopes it to one host, and D3's negated prefix is a second, independent guard, verified live.
- [`--no-proxy-headers` plus a mis-set or non-canonical `TRUSTED_PROXY_IPS` loses client-IP resolution, blinding every limiter] → the default is the current effective list; canonicalisation removes the silent-literal failure mode (D5); a malformed value refuses startup; the effective list is logged at boot; and a regression test asserts the default trusts a private-network peer (`192.168.0.10`) and rewrites from `X-Forwarded-For`.
- [The username budget denies a fresh sign-in during a flood] → D6, accepted and bounded to the window; no durable state, no lockout, sessions unaffected. It can no longer reach an account other than the one attacked.
- [The throttle becomes a username oracle] → D6a; content equivalence is tested, timing is explicitly out of scope and unchanged from today.
- [Migration 025 on a deploy] → additive, nullable, no server default; `make test-schema` gates it at the advanced head and `alembic check` must be clean afterwards.
- [The sweep deletes a client somebody still has configured] → four independent guards now (`last_used_at IS NULL`, which after D8's backfill can only be true for post-025 registrations; no child row of either kind; `created_at` older than 30 days) all inside one `FOR UPDATE` transaction, with an integration test per guard.
- [#273 merges after this and rewrites the same requirement] → D11; this change is rebased and the delta re-diffed before implementation.

## Migration Plan

Migration **025**, additive: `oauth_clients.last_used_at TIMESTAMPTZ NULL`, no server default, carrying the ownership marker in both the migration and `src/models/db.py` (byte-identical, as 019's marker requires). Backfill in the same migration: `MAX(created_at)` over each client's surviving `oauth_tokens` and `oauth_codes` rows where any exist, otherwise the migration's own transaction timestamp — every pre-existing row ends non-NULL. Downgrade drops the column only if it carries the marker.

`tests/integration/test_schema_check.py` advances `HEAD_REVISION` to `025` in the same slice. `make test-schema` before the deploy that carries it; `make db-check` after. The column is nullable and unread by any pre-025 code path, so the deploy is forward-compatible in both directions and **no drain or barrier is added**. It is not, however, a window-free deploy in the strict sense: `make deploy` runs `alembic upgrade head` and then `up -d --force-recreate`, so for the seconds between them the previous image serves against the new column and does not stamp it. Accepted limitation 11 states what that costs and why nothing is done about it. Slices A and B carry no schema change and can deploy independently of C.

## Accepted limitations

Recorded here so they are not re-fixed when a reviewer re-reports them.

1. **The credential has already crossed the wire.** Slice A stops the request at Traefik; it cannot un-send the bearer token the client put on `:80`. The value is that the leak is *reported* on the first request instead of never.
2. **`docker-compose.proxy.yml` deployments are undefended.** That file exists because the proxy is somebody else's; `DEPLOYMENT.md` gains the rule in prose and that is all this repo can do.
3. **A co-tenant on the shared proxy network can still forge `X-Forwarded-For` under the default `TRUSTED_PROXY_IPS`.** This change makes the list narrowable in one `.env` line; it does not narrow it, because the correct value requires Traefik to have a fixed address on a dedicated network. Filed as a follow-up. The username budget is the mitigation that does not depend on it landing.
4. **A sustained flood can deny fresh password sign-in for one account for the window's duration.** D6. Not a lockout, and it cannot reach any other account.
5. **Login response *timing* distinguishes cases, and this change adds one new distinction.** An unknown username and an inactive account already skip bcrypt today, so a timing gap between "no account" and "wrong password" predates this change. What is **new** is a throttle-state signal: for one existing active account, attempts 1–10 run bcrypt and attempt 11 does not, so an observer who can time responses can tell an exhausted budget from an unexhausted one for a name they already know exists. It reveals budget state, not credential validity, and it is inherent to checking a budget before doing the expensive comparison — which is the property that makes the budget bound anything at all. Equalising login timing stays out of scope; the guarantee this change makes is over response *content* (D6a), and it is stated that way everywhere rather than as an unqualified "only the log knows".
6. **Never-used registrations created before migration 025 are never collected.** The backfill stamps them rather than risk deleting a hand-configured confidential client whose evidence was purged (D8). They can be removed by an operator in the panel. The table is bounded going forward, not retroactively.
7. **A client can be deleted between the moment it registers and the moment it first authorizes**, if that gap exceeds the configured age. D9 makes the *concurrent* race safe; it does not make a 30-day-delayed first authorization safe. The response is a clean `invalid_client`.
8. **`/register` still admits roughly 4,300 registrations per day per address.** This change bounds the residue, not the arrival rate.
9. **Slice A is verified behaviourally, not by a unit test.** There is no way to unit-test another process's proxy configuration. The repository test asserts the *labels*; the `curl` matrix asserts the behaviour after deploy.
10. **A client registered *and* authorized in the deploy's migrate-to-recreate gap can end up NULL-marked.** `make deploy` runs `alembic upgrade head` and then `up -d --force-recreate`; for the seconds between, the previous image serves and does not know the column. Declined as a code or process change — no drain step, no rollback reconciliation, no integration case — because it fails both the plausible-input and the real-impact tests, and the reasoning is checkable rather than asserted. A client that is *actually in use* re-stamps itself at its next code exchange or refresh, and an access token lives **one hour** (`expires_in: 3600`), so real use restamps within the hour. Before that, the sweep cannot reach the client anyway: its refresh token row survives **30 days** and is purged seven days after it expires, and the no-child-row guard holds for that whole period — about 37 days, beyond the 30-day expiry age. So the only registration that can still be NULL when the sweep can finally see it is one that was created in a seconds-long window and then went completely unused for over a month, which is precisely the population the sweep exists to remove. If it nevertheless happens, the cost is one re-registration. The same reasoning covers an extended rollback to the pre-025 image: on re-upgrade **nothing needs to be done**, because any client still in use restamps itself within an hour and the child-row guard covers the rest. The kill switch below is the lever if an operator wants certainty during such a period.
11. **The sweep's kill switch is `null`, not `0`.** `OAUTH_CLIENT_UNUSED_EXPIRY_DAYS` is a `NullableLimit`: an empty value, `null` or `none` disables the sweep entirely, while `0` is *rejected* at boot by `ge=1`. That follows the project's single representation for "off" — a control configured to refuse or delete on every pass reads to an operator as an outage rather than as a setting. An operator who wants the sweep off during a rollback sets the value to `null` and redeploys.
12. **The budget is per-worker.** It inherits the `--workers 1` contract like every other in-process control; a second worker would multiply the effective threshold by the worker count.

## Open Questions

- **Does anything poll `http://<host>/health`?** Answerable only on the host, where any external uptime monitor's configuration lives. Task 1.1 checks it before the deploy. Not blocking.
- **Owner call: does #194 close?** The residual this change closes is the last *code* item. What remains is the operator chore of setting `daily_request_limit` on five grandfathered keys, which is a panel action, not a commit.
- **Owner call: 30 days, or shorter?** D10 argues 30. A DCR client that has not authorized within an hour is already anomalous, so 7 would also be defensible. One `.env` value either way.
- **Sequencing: when does #273 merge?** D11 makes this change depend on it. If #273 is going to sit, the schema-integrity delta needs re-diffing against whatever is current at implementation time.
