## Context

Facts established by reading the tree and the pinned driver sources (`asyncpg==0.31.0`, `sqlalchemy==2.0.52`, `httpx==0.28.1`), not assumed:

- **SQLAlchemy passes every `DATABASE_URL` query key to `asyncpg.connect()` as a keyword, and `connect_args` then overrides it.** `AsyncAdapt_asyncpg_dialect.create_connect_args` does `opts.update(url.query)`; `engine/create.py` then builds `cparams = …union(connect_args)`. So `?ssl=disable` in the URL plus `connect_args={"ssl": ctx}` silently becomes `ctx`, and the reverse ordering bug is one refactor away. `?sslmode=…` is not an `asyncpg.connect` keyword at all and fails at first connect with a `TypeError`.
- **asyncpg's resolution order when `ssl` is not passed:** the DSN's `sslmode`, then `$PGSSLMODE`, then `'prefer'` for TCP hosts. For a *string* mode of `require` or above it also consults `sslrootcert`, `$PGSSLROOTCERT` and `~/.postgresql/root.crt` — and, libpq-style, `require` **silently upgrades to verification** when such a file exists. For `prefer` it builds a `CERT_NONE` context and marks the attempt *advisory*: a server answering `N` to the SSLRequest gets a second, plaintext attempt with no signal to the caller. That is the #184 downgrade, exactly.
- **When `ssl` is an `ssl.SSLContext`, asyncpg sets `sslmode=disable` internally and still wraps the connection in TLS with that context, non-advisory** (`__connect_addr`: `elif params.ssl: _create_ssl_connection(..., ssl_is_advisory=params.sslmode == SSLMode.prefer)` → `False`). There is no plaintext retry. The context's own `verify_mode` / `check_hostname` are the whole verification policy, and no environment variable or home-directory file is consulted. This is what makes an explicit context the deterministic spelling of the strict modes.
- **A Unix-socket host is never TLS.** asyncpg connects a socket path with `create_unix_connection` regardless of `ssl`. A strict mode against a socket therefore connects in plaintext — which the startup assertion (D4) catches, because `pg_stat_ssl.ssl` is false for it.
- **`pg_stat_ssl` is readable by an unprivileged role for its own backend.** `SELECT ssl, version, cipher FROM pg_stat_ssl WHERE pid = pg_backend_pid()` needs no `pg_read_all_stats`.
- **Every production connection creator in the tree** (grep for `create_async_engine`, `create_engine`, `asyncpg.connect`, `asyncpg.create_pool`, `psql`, `pg_dump`):

  | Creator | Reached by | Covered how |
  | --- | --- | --- |
  | `src/database.py` `engine` | the FastAPI app (lifespan, tools, panel, OAuth, indexer), `src/mcp_stdio.py`, `scripts/reset_embeddings.py`, `scripts/rebuild_tsvectors.py` (both import `engine`/`async_session` from `src.database`) | `connect_args` gains `**database_ssl_connect_args()` |
  | `alembic/env.py` `run_async_migrations` | `make db-migrate`, the deploy's `alembic upgrade head`, `make db-check` | the same helper, applied to the URL alembic actually resolves (`get_url()`), with the same query-key refusal |
  | `alembic/env.py` offline mode | `alembic upgrade --sql` | opens no connection — out of scope |
  | `docker/db-init.sh`, `make db-backup` / `db-restore`, `docker/record-backup.sh` | operator | `docker exec <postgres> psql/pg_dump` **inside the database container**, over its local socket; never crosses a network — unaffected, recorded |
  | every `create_async_engine` / `asyncpg.connect` under `tests/integration/` and `tests/test_fts_integration.py` | the test suite | target the throwaway `pgvector` container on loopback; **not** production creators and deliberately untouched |

- **Every HTTP client that talks to an embedding endpoint** (grep for `httpx.`, `AsyncClient`, `ollama_url`, `openai_base_url`, `urllib`, `aiohttp`):

  | Site | URL | Today |
  | --- | --- | --- |
  | `OllamaProvider.embed_one` (`src/services/embeddings.py`) | `{OLLAMA_URL}/api/embed` | `httpx.AsyncClient(timeout=30.0)` |
  | `OpenAIProvider._post` (same file) | `{OPENAI_BASE_URL}/embeddings` | `httpx.AsyncClient(timeout=60.0)` |
  | panel settings page provider ping (`src/control_panel/routes.py`) | `{OLLAMA_URL}/api/tags` | `httpx.AsyncClient(timeout=5.0)` |

  `_warm_embedding_model` and every indexer/search path reach the network only through `get_provider()`, i.e. the first two rows. `src/services/transfer.py`'s `PinnedTransport` client is `import_from_url` — a different hop with its own policy (`IMPORT_ALLOW_HTTP`, SSRF guard) — and is out of scope. `/health` makes no outbound call. `src/services/index_state.py` deliberately excludes endpoint identity from the fingerprint and is unaffected.
- **httpx 0.28 does not follow redirects by default**, and `verify=` takes an `ssl.SSLContext` (a string path is deprecated in 0.28). `ssl.create_default_context(cafile=X)` loads **only** `X` and does not load the system store.
- **The default `OLLAMA_URL` is `http://ollama:11434`** — a plaintext, non-loopback URL — and `tests/conftest.py` builds the settings singleton from model defaults. Any refuse-by-default policy therefore has to touch the test harness (task 5.1).
- **`src/config.py` already owns a fail-closed loopback test** (`_is_loopback_host`: `localhost`, 127/8, `::1`, bracket-stripping, `*` never loopback) used by the sandbox guard, and a narrower inline one in `_validate_public_transport`. The scheme policy reuses the former rather than adding a third.
- **Startup records normally stay on the bare logger** (`docs/architecture/security-event-logging.md`, "What stays on the bare logger"). This change adds one *catalogued* event anyway — see D5.

## Goals / Non-Goals

**Goals:**
- No hop from this application to its dependencies is plaintext **without the operator being told**, at every start, in a form Loki can query.
- A one-line `.env` change makes each hop strict — encrypted **and** server-authenticated — with the process refusing to run if the promise is not kept.
- Deployable today against a database with `ssl=off` and an `http://` Ollama, without breaking it (given the one-line override in D6).
- Exactly one place states the database TLS policy and exactly one factory builds embedding HTTP clients, so no call site can drift.

**Non-Goals:**
- **Server-side TLS on PostgreSQL.** `ssl=on`, certificates, `hostssl` / `hostnossl reject` rows in `pg_hba.conf` and `ssl_min_protocol_version` are properties of a PostgreSQL instance that other stacks share; changing them is host infrastructure and is the operator follow-up below.
- **TLS on Ollama**, mTLS to it, or restricting who may call it. Same reason.
- **Traefik → uvicorn and Traefik → oauth2-proxy.** No application code is involved: uvicorn's listener, the Traefik `serversTransport`, the `forwardAuth` middleware's `tls` block and oauth2-proxy's certificate all live in host configuration. Recorded as the operator follow-up; nothing in this change touches `Dockerfile`, the compose labels or the healthcheck.
- **A startup probe in `alembic/env.py` or `src/mcp_stdio.py`.** Both get the same `connect_args`, so a strict mode is enforced by the driver on every connection they open; only the *warning* under `prefer` is lifespan-only (accepted limitation 4).
- **Surfacing transport state in the control panel.** The startup log and the security event are the operator surface; a panel card is a later nicety.
- **Re-embedding or fingerprinting on an endpoint change.** Out of scope, as `index_state.py` already documents.

## Decisions

**D1 — `DATABASE_SSL_MODE` with libpq's vocabulary, default `prefer`.**
`disable | prefer | require | verify-ca | verify-full`, stripped and case-folded at boot. `allow` is deliberately not offered: it tries plaintext first and is strictly weaker than the default. The default is `prefer` because it is **exactly today's behaviour** — the production server has `ssl=off`, so any stricter default is an outage on deploy — and because the silent part is removed by D4, not by the default. The mode is libpq's so an operator reading PostgreSQL documentation gets what the word means there, with one deliberate difference (D2).

**D2 — The strict modes are an explicit `ssl.SSLContext`; the lax modes are asyncpg's own strings.**

| Mode | `connect_args["ssl"]` | Semantics |
| --- | --- | --- |
| `disable` | `False` | plaintext |
| `prefer` | `"prefer"` | asyncpg's advisory TLS, plaintext fallback (today) |
| `require` | `SSLContext(PROTOCOL_TLS_CLIENT)`, `check_hostname=False`, `verify_mode=CERT_NONE`, `minimum_version=TLSv1_2` | encrypted, **unauthenticated**, no fallback |
| `verify-ca` | same, `verify_mode=CERT_REQUIRED`, `load_verify_locations(DATABASE_SSL_CA_FILE)` | encrypted, chain verified, hostname not checked |
| `verify-full` | same as `verify-ca` with `check_hostname=True` | encrypted, chain and hostname verified |

Client cert/key, when both are set, are loaded with `load_cert_chain` into the strict contexts. The explicit context is chosen over passing the string `"verify-full"` because asyncpg's string path consults `$PGSSLROOTCERT`, `~/.postgresql/root.crt`, `$PGSSLCRL` and `$PGSSLMINPROTOCOLVERSION`, and makes `require` **silently verify** if a stray root file exists — behaviour that depends on the container's home directory is exactly the kind of implicit configuration this change removes. A context is also the only spelling asyncpg treats as non-advisory by construction (Context, third bullet).

**The one deliberate libpq difference:** a `DATABASE_SSL_CA_FILE` with `require` is **refused**, not treated as `verify-ca`. libpq's upgrade is a silent change of meaning; here the operator is told to write the mode they mean.

**D2a — `verify-*` requires `DATABASE_SSL_CA_FILE`; there is no system-store fallback.** An internal CA is the case this change exists for, and a verifying mode whose trust anchor is "whatever the image happens to ship" is not a decision anyone made. An operator whose database carries a publicly-trusted certificate points the setting at the image's system bundle explicitly (documented in `.env.example`). The file is checked for existence, regular-file-ness and readability in the settings validator, and **parsed** when the context is built at `src.database` import — both before the first request.

**D3 — One source of TLS truth: conflicting inputs are refused at boot, never reconciled.**
- **A TLS key in `DATABASE_URL`'s query** — any of `ssl`, `sslmode`, `sslrootcert`, `sslcert`, `sslkey`, `sslcrl`, `sslpassword`, `sslnegotiation`, `direct_tls`, matched case-insensitively on the parsed `make_url(...).query` — is refused with a message naming `DATABASE_SSL_MODE`. The alternative, honouring the URL when the setting is unset, means a precedence rule that has to be learned and a merge that SQLAlchemy already gets backwards (Context, first bullet). The refusal applies to the URL alembic resolves as well as to `settings.database_url`, since `alembic/env.py` can fall back to `alembic.ini`.
- **Any `PGSSL*` environment variable** is refused, for the same reason: `$PGSSLMODE` is honoured today and would be silently overridden by the explicit `connect_args`, and the others only work on the string path this change stops using for strict modes. One rule ("TLS for the database is configured only through `DATABASE_SSL_*`") is easier to state and test than a list of which variables still leak through.
- **Half a client pair**, a cert/key with `disable`/`prefer`, a CA file with a non-verifying mode, a missing or unreadable file — all refused at settings construction.

Refusal is cheap here: production's URL has an empty query string (verified during #184's assessment), and task 7.1 confirms the deploy-dir `.env` carries no `PGSSL*` variable before deploying, so none of these should fire on deploy.

**D4 — The startup assertion reads the server's view of the session, once, first.**
`check_database_transport()` in `src/services/transport_security.py`, awaited in the lifespan **before** `_check_embedding_dim`, so a transport failure is reported as itself rather than as whatever the first query happened to be:

```
try:
    async with async_session() as s:
        row = (await s.execute(text(
            "SELECT ssl, version, cipher FROM pg_stat_ssl "
            "WHERE pid = pg_backend_pid()"))).first()
except <connect/OS/driver error> as exc:
    if strict:  log CRITICAL naming mode + error class + "server may not offer TLS"; sys.exit(1)
    raise                      # lax: the existing checks would fail the same way
encrypted = bool(row and row.ssl)
if strict and not encrypted:   log CRITICAL (mode, "session is not encrypted"); sys.exit(1)
if not encrypted:              emit internal_transport_plaintext(reason="database", outcome=mode)
log INFO once: "Database transport: mode=%s encrypted=%s tls_version=%s server_verified=%s"
```

`strict` = `require | verify-ca | verify-full`. `server_verified` is derived from the mode (`verify-*` only), never claimed from the session. A missing row is treated as *not encrypted* — fail closed under strict, warn under lax. Same shape as `_check_embedding_dim`: CRITICAL then `sys.exit(1)`, below the sandbox short-circuit. The probe runs **once**, on whichever pooled connection it gets; see accepted limitation 3 for what that does not prove under `prefer`.

**D5 — One catalogued event, `internal_transport_plaintext`, although startup records normally stay on the bare logger.**
The bare-logger rule exists to keep *flood* channels bounded; this record is at most one per hop per process start, so the suppressor is irrelevant to it (subject `-`). It is catalogued because it is a standing **security fact** that the operator will query for — "which hops of which deployment are still cleartext" — and a catalogued event is the only log shape with a stable name and an allow-listed field set. Fields: `reason` (the hop: `database` or `embedding`) and `outcome` (what admitted it: the database mode, `prefer` or `disable`, or `override` for `EMBEDDING_ALLOW_PLAINTEXT`), both already in `ALLOWED_FIELDS` with bounds 64 and 16. Level WARNING. **No host, port, URL or DSN** in any field — the startup INFO line carries scheme/host/port for the operator, and a record in a shared sink carries only the closed vocabulary. The `msg` is a constant.

**D6 — Embedding endpoint policy: refuse plaintext to a non-loopback host unless overridden, and the override defaults to `false`.**

Policy, applied in a `Settings` model validator to the **active** provider's URL only (`OLLAMA_URL` when `EMBEDDING_PROVIDER=ollama`, `OPENAI_BASE_URL` when `openai`; an inactive provider's URL is never dialled, so validating it would refuse deployments for a setting they do not use):

1. Parse with `urllib.parse.urlsplit`. Scheme not in `{http, https}`, no hostname, userinfo present (`user@` or `user:pass@`), or an unparseable port → **refuse** (message names the setting, never echoes userinfo).
2. `https` → accept.
3. `http` and `_is_loopback_host(hostname)` → accept. `hostname` is `urlsplit`'s, so the host is what the client will actually dial: `http://127.0.0.1@evil.example` is refused by rule 1 and would have yielded `evil.example` anyway; `http://127.0.0.1.evil.example` is a DNS name, not an address, and is non-loopback; `http://[::1]:11434` and `http://localhost` are loopback. No DNS resolution — a name that happens to resolve to loopback is non-loopback, failing closed, as the sandbox guard already does.
4. `http` otherwise → accept **only if `EMBEDDING_ALLOW_PLAINTEXT=true`**, else refuse with a message naming both remedies (use `https` + `EMBEDDING_CA_FILE`, or set the override to acknowledge a plaintext hop).
5. Skipped entirely under `MCP_SANDBOX_MODE`, which never calls a provider.

`EMBEDDING_CA_FILE` (optional) is validated like `DATABASE_SSL_CA_FILE` and **refused with an `http` URL** — a trust anchor on a hop that has no TLS verifies nothing and signals a misunderstanding.

*Why refuse by default rather than warn (the weighed alternative).* A warn-by-default would mirror `DATABASE_SSL_MODE=prefer` and break nothing. It was rejected because the two hops are not symmetric: `prefer` at least **attempts** TLS and becomes encrypted the day the server enables it, whereas an `http://` URL never will — it stays cleartext until someone edits the setting, and a warning in a log nobody reads is how #185 went unnoticed. An override makes the plaintext hop a **written decision in the operator's own `.env`**, which is ASVS V12.3.3's intent, costs one line, and is removed the day the hop gains TLS. The costs are real and accepted: (a) the default `OLLAMA_URL` no longer boots without the line, so `.env.example` ships `EMBEDDING_ALLOW_PLAINTEXT=true` directly beside the `http://ollama:11434` default with a comment explaining it; (b) an upgrading self-hoster with an `http://` sibling-container Ollama gets a boot refusal whose message states the one-line fix; (c) the test harness must set the override (task 5.1). This is an **owner question** — see the end of this document.

*Upgrade note (goes in DEPLOYMENT.md and the PR description):* before deploying, add `EMBEDDING_ALLOW_PLAINTEXT=true` to the deploy-dir `.env` if `OLLAMA_URL` (or, with the OpenAI provider, `OPENAI_BASE_URL`) is `http://` to anything but loopback. Remove it once the endpoint is `https`.

**D7 — One client factory for every embedding HTTP client.**
`embedding_http_client(timeout: float) -> httpx.AsyncClient` in `src/services/transport_security.py` returns `httpx.AsyncClient(timeout=timeout, verify=<ctx or True>, follow_redirects=False)`. The `SSLContext` from `EMBEDDING_CA_FILE` is built once, lazily, and cached. `follow_redirects=False` restates httpx's default **explicitly**, because a redirect is the one way a validated `https` endpoint could hand the request to an `http://` one after boot, and a default nobody wrote down is a default somebody changes. The three sites in Context use it; a test asserts no other `httpx.AsyncClient(` construction exists in `src/services/embeddings.py` or reaches `ollama_url` / `openai_base_url` in `src/control_panel/routes.py`, so a fourth site cannot bypass it silently.

**D8 — The startup transport report.**
After D4, the lifespan logs one INFO line per hop — the database line from D4, and `Embedding transport: provider=%s scheme=%s host=%s port=%s verify=%s plaintext_override=%s` where `verify` is `system`, `ca-file` or `n/a` (http). Host and port only; the URL's path, query and userinfo never reach the line. When the active embedding URL is plaintext and non-loopback (i.e. it was admitted by the override), the lifespan also emits `internal_transport_plaintext(reason="embedding", outcome="override")`. A plaintext **loopback** hop logs its line but emits no event — traffic that never leaves the network namespace is not what #185 is about. Under `MCP_SANDBOX_MODE` the report is skipped with the rest of the external checks.

**D9 — The compose healthcheck is unaffected, and why.** `docker-compose.yml`'s healthcheck runs `curl -f http://localhost:8000/health` **inside** the application container. It never leaves the container's network namespace, it does not traverse Traefik (and so is outside #196's plaintext-refusal router too), and this change does not alter uvicorn's listener. The Codex note on #185 is correct that it must change **when** uvicorn itself moves to TLS — that is part of the operator follow-up, not this change.

**D10 — Module placement.** `src/services/transport_security.py` holds `database_ssl_connect_args(settings)`, `refuse_url_tls_keys(url)`, `check_database_transport()`, `log_transport_report()`, `embedding_http_client()` and the pure `classify_embedding_url(url) -> (scheme, host, port, is_loopback)`. `src/config.py` holds only the settings and the validators, which call pure helpers that do not import `src.database` (no cycle: `src.database` imports `src.config`). `alembic/env.py` already imports `src.config` transitively through `src.models.db`, so importing the helper there adds no new failure mode to the schema gate's subprocess.

## Accepted limitations

1. **`prefer` still downgrades.** The default is a *reported* downgrade, not a prevented one. Prevention is `require` or above, which needs server TLS.
2. **`require` is encrypted but unauthenticated.** An active interposer can terminate it with any certificate. It is offered because it defeats passive capture and is the only strict mode available before an internal CA exists; the docs say plainly that `verify-full` is the target.
3. **The startup probe inspects one connection.** Under `prefer`, a later pooled connection could land differently (e.g. the server's TLS setting changes while the app runs); under strict modes the driver enforces every connection, so the probe is belt-and-braces there.
4. **No probe outside the FastAPI lifespan.** `alembic`, `src/mcp_stdio.py` and the maintenance scripts enforce strict modes through the driver but do not *warn* under `prefer`.
5. **The scheme policy is name-based.** A DNS name resolving to loopback is refused as plaintext-non-loopback (fail closed); `localhost` is trusted to mean loopback, as `BASE_URL`'s policy already does.
6. **`verify-full` checks the name in `DATABASE_URL`'s host.** A Docker service name must therefore appear in the server certificate's SAN. That is an issuance requirement for the follow-up, not a code problem.
7. **The embedding override is all-or-nothing per process.** It admits whichever plaintext URL the active provider uses; there is no per-host allow-list. One provider is active at a time, so one flag is one decision.
8. **Encrypted is not authorised.** Neither hop gains client authentication from this change (a client cert for Postgres is optional and unused in production; nothing for Ollama). Who may *call* Ollama is the network-isolation follow-up.

## Operator follow-up (text for the infrastructure issue the supervisor files)

> **Title:** Internal TLS, server side: PostgreSQL, Ollama, Traefik → app, Traefik → oauth2-proxy (#184, #185 infra half)
>
> The application half shipped in `internal-transport-tls`: the app can now demand and verify TLS to PostgreSQL (`DATABASE_SSL_MODE`, `DATABASE_SSL_CA_FILE`) and to the embedding endpoint (`https` + `EMBEDDING_CA_FILE`), logs every hop's transport at startup, and emits `internal_transport_plaintext` for each hop still in cleartext. Production runs `DATABASE_SSL_MODE=prefer` (server has `ssl=off`) and `EMBEDDING_ALLOW_PLAINTEXT=true` (Ollama is `http://`). Each item below ends in a one-line `.env` change.
>
> 1. **Internal CA.** Create a private CA (or reuse one) whose root the app container mounts read-only. Certificates below are issued from it; SANs must include the Docker service names the app dials.
> 2. **PostgreSQL (shared instance).** Server cert/key in PGDATA with correct ownership and mode; `ssl = on`, `ssl_min_protocol_version = 'TLSv1.2'`; in `pg_hba.conf` a `hostssl obsidian_mcp obsidian_mcp … scram-sha-256` row **above** a `hostnossl obsidian_mcp obsidian_mcp all reject` row, scoped to this database/role so other stacks keep their current behaviour; reload, and check each other stack still connects. Then set `DATABASE_SSL_MODE=verify-full` and `DATABASE_SSL_CA_FILE=<mounted root>`, deploy, and confirm the startup line reads `encrypted=True server_verified=True`.
> 3. **Ollama.** Put a TLS-terminating proxy in front of it on the shared bridge (Traefik internal entrypoint or a sidecar) with a cert from the CA; restrict callers (mTLS or a dedicated network) — encryption alone leaves it unauthenticated; stop publishing `11434` on all host interfaces (bind to loopback or unpublish). Then set `OLLAMA_URL=https://…`, `EMBEDDING_CA_FILE=<mounted root>`, remove `EMBEDDING_ALLOW_PLAINTEXT`.
> 4. **Traefik → uvicorn.** Run uvicorn with `--ssl-keyfile/--ssl-certfile` (Dockerfile and every compose `command:` override), set the service's `loadbalancer.server.scheme=https` and a `serversTransport` with the CA in `rootCAs` and the right `serverName` — **not** `insecureSkipVerify`. Update the container healthcheck, which today runs `curl -f http://localhost:8000/health` inside the container, to `https` with `--cacert` (or keep a loopback-only plaintext listener for it).
> 5. **Traefik → oauth2-proxy.** Enable oauth2-proxy's `--tls-cert-file/--tls-key-file`; switch the `forwardAuth` address to `https://` and configure the CA on the **forwardAuth middleware's own `tls` block** — a service `serversTransport` does not apply to forwardAuth.
> 6. **Network isolation** (overlaps the #189 follow-up): move obsidian-mcp, its Postgres access and Ollama onto a dedicated backend network so sibling containers on the shared proxy bridge cannot reach these hops at all.

## Owner questions

1. **D6's default.** Refuse-by-default (`EMBEDDING_ALLOW_PLAINTEXT=false`, one `.env` line on deploy, and a boot refusal for upgrading self-hosters with an `http://` Ollama) is recommended. The alternative is warn-by-default (symmetric with `prefer`, zero-break, but the plaintext hop stays an unwritten decision). Confirm before implementation.
2. **Issue disposition.** Close #184 and #185 when this ships (pointing to the follow-up), or keep them open until the infrastructure half lands?
