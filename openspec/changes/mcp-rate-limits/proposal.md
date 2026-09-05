## Why

`/mcp` is the only surface on this server with no rate control of any kind. `app.mount("/mcp", APIKeyMiddleware(mcp_handler))` (`src/main.py:437`) sits outside slowapi, the container carries no Traefik `ratelimit` middleware, and the one per-credential control that does exist — `api_keys.daily_request_limit` (#162) — is opt-in, is set on **0 of 5** active production keys, counts per UTC *day* (so it bounds nothing instantaneous), and is exempt for OAuth by construction. Roughly half of production tool calls in the last 30 days ran on that exempt channel (`actor_kind` oauth 951, api_key 683).

The consumer of this server is an agent, and a retry-storming or prompt-injected agent is an ordinary input for this product. One credential can currently issue unbounded concurrent tool calls against a **single uvicorn worker** (`Dockerfile:31`, `--workers 1`) sharing a 5 + 10 connection pool with every other tenant, a 60 s `statement_timeout`, a 30 s embedding round trip, and a *single sequential* multi-user indexer that every write amplifies into. That is a cross-tenant availability failure of exactly the shape `CLAUDE.md` ranks as expensive, and it is the only thing that would bound the blast radius of an agent bulk-deleting or bulk-exfiltrating a tenant vault.

Both cross-family audit notes are folded in below: per-credential *daily* limits alone leave bursts unbounded; a daily default can regress existing clients; OAuth principals must be covered, not only API keys; and every control needs a **bounded** wait rather than an unbounded queue.

## What Changes

- **A principal identity, bound once at admission.** `APIKeyMiddleware` binds `current_principal` from the credential row it has already loaded — `("api_key", key_id)` or `("oauth", client_id, user_id)` — so every control below reaches API keys and OAuth grants alike. The OAuth key is the **grant**, not the access token, so an agent that refreshes hourly does not get a fresh allowance.
- **A per-principal token bucket on every tool call**, in `_tracked`, as the *first* pre-body gate: `MCP_RATE_LIMIT_PER_MINUTE` (default 120) sustained with `MCP_RATE_LIMIT_BURST` (default 30). Pure in-process arithmetic behind one dictionary lookup — no `await`, no database statement, no lock.
- **Bounded concurrency slots around the tool body**, acquired in one fixed order under one shared deadline (`MCP_SLOT_WAIT_SECONDS`, default 5 s): per-principal-per-class → per-principal → per-class → global. Classes are `embedding` (`semantic_search`, the only tool that calls the provider at request time), `vector` (`find_related`), and `write` (`create_note`, `edit_note`, `move_note`, `delete_note`, `set_frontmatter`, `write_file`, `delete_file`, `import_from_url` — the tools that amplify into the indexer). A wait that ends without a slot is the same typed refusal, not a queue.
- **Every refusal is in-band, typed and actionable** — the message names the control, the ceiling and a `retry_after_seconds`, and it travels through the existing `refusal_result` branch, so `read_note` refuses in its own structured shape rather than breaking the wire format. A limiter never returns an empty result set.
- **The refusals are recorded**: `params.error = "rate_limited"` with a `rate_limit_scope` string saying which control fired, and `params.error = "argument_too_long"` for the query cap. Both are **pre-body** markers and both join `pre_body_refusal_sql()`, so a server refusing five thousand calls an hour does not read as fast on `/admin/performance`. Marker names are deliberately distinct from the `permission_denied` / `tool_exception` pair the parallel `security-event-logging` change adds.
- **A per-IP budget on failed `/mcp` authentication** inside `APIKeyMiddleware`: `MCP_AUTH_FAILURE_LIMIT` (default 60) failures per `MCP_AUTH_FAILURE_WINDOW_SECONDS` (default 300) per client IP, refused with **429 + `Retry-After`** and, crucially, **no database round trip**. Kept in the app rather than pushed to Traefik: a proxy cannot see that a response was a 401, so bounding probing there means bounding every authenticated agent too, and the host's Traefik static configuration is outside this repo, so a control expressed there would not be reproducible from the tree. `docker-compose.yml` labels are unchanged, and the file records that this was decided rather than overlooked.
- **A query length cap before any embedding call.** `MAX_SEARCH_QUERY_CHARS` (8,192) is enforced by a new declarative `arg_char_caps` screen in `_tracked`, beside the existing unencodable-argument screen — so it is genuinely pre-body, it is refused before the provider call, before `tsquery` parsing and before the query is interpolated into a server-authored result string, and it generalises to any future argument. Precedents: `MAX_LIST_PATTERN_CHARS`, `MAX_NOTE_BYTES`.
- **A default daily quota for new keys.** `DEFAULT_DAILY_REQUEST_LIMIT` (default **5,000**) is applied by the key-creation paths, not by a column default: **existing keys are untouched**, an explicit `null` in the JSON API still means unlimited (distinguished from an omitted field by `model_fields_set`), and the panel create form is pre-filled with the default and says so. Setting the setting to null restores today's behaviour exactly.

Limiter state is **in-process and deliberately not persisted** — see design. No new dependencies. **No migration.**

## Capabilities

### New Capabilities

- `mcp-rate-limits`: principal identity, the per-principal request-rate bucket, the bounded concurrency slots, the failed-authentication IP budget, the argument length cap, the in-band refusal contract, and the in-process/single-worker constraint.

### Modified Capabilities

- `usage-quotas`: the OAuth exemption is re-scoped — it applies to the *daily* quota only, and OAuth principals are covered by the rate and concurrency controls; new keys receive a configurable default limit while existing keys are grandfathered.
- `panel-performance-views`: the shared pre-body-refusal predicate enumerates the two new markers, so rate refusals are counted as refusals rather than folded into latency percentiles.
- `panel-usage-slicing`: the key create form surfaces the default limit.

## Impact

- `src/services/rate_limits.py` (new), `src/auth/session.py` (`current_principal`), `src/mcp_server/auth.py` (principal binding, failed-auth budget), `src/mcp_server/tools.py` (`_tracked` gates, `slot=` and `arg_char_caps=`), `src/config.py` (settings + `MAX_SEARCH_QUERY_CHARS` + a coherence validator), `Dockerfile` (a comment binding `--workers 1` to these controls).
- `src/services/usage_stats.py` (pre-body predicate), `src/services/quotas.py`, `src/api/routes.py`, `src/control_panel/routes.py`, `src/control_panel/templates/keys.html`.
- Docs: new `docs/architecture/rate-limits-and-concurrency.md`, plus `docs/architecture/usage-attribution.md` (marker register), `docs/architecture/search.md` (query cap), `CLAUDE.md` (index row), `README.md`, `.env.example`, `docker-compose.yml` (recorded decision).
- Adversarial Codex pass **mandatory**: this change edits the tool execution path and the admission middleware. Schema gate not applicable (no migration); `make db-check` still runs after deploy.

Closes #194
Closes #188
