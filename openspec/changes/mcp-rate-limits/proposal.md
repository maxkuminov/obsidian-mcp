## Why

`/mcp` is the only surface on this server with no rate control of any kind. `app.mount("/mcp", APIKeyMiddleware(mcp_handler))` (`src/main.py:437`) sits outside slowapi, the container carries no Traefik `ratelimit` middleware, and the one per-credential control that does exist — `api_keys.daily_request_limit` (#162) — is opt-in, is set on **0 of 5** active production keys, counts per UTC *day* (so it bounds nothing instantaneous), and is exempt for OAuth by construction. Roughly half of production tool calls in the last 30 days ran on that exempt channel (`actor_kind` oauth 951, api_key 683).

The consumer of this server is an agent, and a retry-storming or prompt-injected agent is an ordinary input for this product. One credential can currently issue unbounded concurrent tool calls against a **single uvicorn worker** (`Dockerfile:31`, `--workers 1`) sharing a 5 + 10 connection pool with every other tenant, a 60 s `statement_timeout`, a 30 s embedding round trip, and a *single sequential* multi-user indexer that every write amplifies into. That is a cross-tenant availability failure of exactly the shape `CLAUDE.md` ranks as expensive, and it is the only thing that would bound the blast radius of an agent bulk-deleting or bulk-exfiltrating a tenant vault.

Both cross-family audit notes are folded in: per-credential *daily* limits alone leave bursts unbounded; a daily default can regress existing clients; OAuth principals must be covered, not only API keys; and every control needs a **bounded** wait rather than an unbounded queue. So is the pre-code Codex review of this proposal — in particular that a limiter which begins *after* authentication leaves the pool exhaustible by a valid credential, that slots must be taken before the durable quota is consumed, and that a refusal an agent cannot parse is not an actionable refusal.

## What Changes

Six layers, outermost first. Everything below the middleware is keyed on a **principal**.

- **A bounded admission gate in front of all authentication work.** `APIKeyMiddleware` keeps a process-wide in-flight counter checked *before any SQL*: over `MCP_MAX_INFLIGHT_REQUESTS` (64) it answers **503 + `Retry-After`** immediately. Without this, a valid credential sending 10,000 concurrent calls exhausts the pool on the credential lookup alone, before any per-principal control can see them.
- **A per-IP budget on failed `/mcp` authentication**, checked before the credential lookup so a refused probe costs no session and no query: `MCP_AUTH_FAILURE_LIMIT` (60) failures per `MCP_AUTH_FAILURE_WINDOW_SECONDS` (300), refused with **429 + `Retry-After`**. State lives in a fixed-size salted hash table, so its memory is bounded by construction rather than by eviction.
- **A principal identity, bound once at admission.** `APIKeyMiddleware` binds `current_principal` from the credential row it already holds — `("api_key", key_id)` or `("oauth", grant_id)`. The OAuth key is the **grant** (`oauth_tokens.grant_id`, NOT NULL since migration 014), so refreshing an access token does not reset an allowance and two independently revocable grants for the same client and user do not share one.
- **Two per-principal token buckets** in `_tracked`, as the first gates: a general velocity bucket (`MCP_RATE_LIMIT_PER_MINUTE` 120, burst 30) on every call, and a **write bucket** (`MCP_WRITE_RATE_LIMIT_PER_MINUTE` 60, burst 15) that the eight vault-mutating tools must pass in addition. Pure in-process arithmetic behind one dictionary lookup — no `await`, no statement, no lock.
- **Bounded concurrency slots around the tool body**, acquired in one fixed order under one shared deadline of approximately `MCP_SLOT_WAIT_SECONDS` (5 s): per-principal-class → per-principal → **per-tenant-class** → class → global. Classes are `embedding` (`semantic_search`), `vector` (`find_related`) and `write` (the eight mutating tools). The per-tenant level is what stops one user holding both embedding slots through an API key *and* an OAuth grant. A wait that ends without a slot is a refusal, never a queue.
- **Slots are taken BEFORE the daily quota is consumed**, and released if the quota then refuses. A durable quota slot must never be spent on a call that a slot timeout was about to refuse.

Cross-cutting:

- **One caller-visible refusal shape.** Every pre-body refusal ends with a machine-readable final line — `MCP-REFUSAL {"code":…,"scope":…,"limit":…,"limit_unit":…,"retry_after_seconds":…}` — appended to the existing human prose and carried unchanged into a structured tool's declared error field through the existing `refusal_result` mapping. An agent gets the same parseable fields whether the tool returns `str` (`keyword_search`) or a model (`read_note`). The three existing pre-body refusals adopt it too, additively, so the family is uniform.
- **Refusal recording is bounded.** Rate and slot refusals are coalesced per `(principal, tool, marker)` per `MCP_REFUSAL_LOG_INTERVAL_SECONDS` (10 s): the representative row carries `suppressed: <n>`, and the performance view's refusal count sums `1 + suppressed`. Nothing is hidden and an agent in a hot loop cannot turn a limiter into an INSERT amplifier.
- **Markers**: `rate_limited` (either bucket), `slot_timeout` (a slot wait), `argument_too_long` (the query cap) — three distinct values, all **pre-body**, all enumerated by `pre_body_refusal_sql()`, all distinct from the `permission_denied` / `tool_exception` pair the parallel `security-event-logging` change adds.
- **A query length cap before any embedding call.** `MAX_SEARCH_QUERY_CHARS` (8,192) enforced by a declarative `arg_char_caps` screen in `_tracked`, beside the existing unencodable-argument screen — pre-body, before the provider call, before the `tsquery` parse, before the query reaches a server-authored string. Precedents: `MAX_LIST_PATTERN_CHARS`, `MAX_NOTE_BYTES`.
- **Bounded limiter state.** Every registry has a hard cardinality cap, TTL eviction that never evicts an entry with in-flight holders or waiters, and a declared overflow behaviour (a shared overflow bucket for principals; a fixed hashed table for addresses).
- **A default daily quota for new keys.** `DEFAULT_DAILY_REQUEST_LIMIT` (5,000) is applied by the key-creation paths, never as a column default: **existing keys are untouched**. On the JSON API an omitted field means the default and an explicit `null` still means unlimited. In the panel the create form is **pre-filled** with the default and a blank submitted field means unlimited — there is no POST-side substitution.
- **One representation for a null setting**, defined centrally: an empty value or the literal `null` / `none` in `.env` means "off" for every nullable limiter setting. Zero is rejected everywhere.

Limiter state is in-process and deliberately not persisted; `--workers 1` becomes part of the contract. No new dependencies. **No migration.**

## Capabilities

### New Capabilities

- `mcp-rate-limits`: the in-flight admission gate, the failed-authentication IP budget, principal identity, the two per-principal buckets, the bounded concurrency slots, the refusal shape and its recording, the argument length cap, bounded limiter state, and the in-process/single-worker constraint.

### Modified Capabilities

- `usage-quotas`: slots are acquired before quota admission; the OAuth exemption is re-scoped to the daily quota alone; "NULL-limit keys behave as today" is scoped to quota accounting; new keys receive a configurable default while existing keys are grandfathered.
- `panel-performance-views`: the shared pre-body-refusal predicate enumerates the three new markers and the refusal count sums coalesced rows.
- `panel-usage-slicing`: the key create form is pre-filled with the default and a blank field means unlimited.

## Impact

- `src/services/refusals.py` (new — the shared refusal shape), `src/services/rate_limits.py` (new), `src/auth/session.py` (`current_principal`), `src/mcp_server/auth.py` (in-flight gate, failed-auth budget, principal binding), `src/mcp_server/tools.py` (gate order, `slot=`, `arg_char_caps=`, coalesced refusal logging), `src/config.py` (settings, the null representation, the coherence validator, `MAX_SEARCH_QUERY_CHARS`), `Dockerfile`.
- `src/services/usage_stats.py`, `src/services/quotas.py`, `src/api/routes.py`, `src/control_panel/routes.py`, `src/control_panel/templates/keys.html`.
- Docs: new `docs/architecture/rate-limits-and-concurrency.md`, plus `docs/architecture/usage-attribution.md`, `docs/architecture/search.md`, `CLAUDE.md`, `README.md`, `.env.example`, `docker-compose.yml` (recorded decision).
- Adversarial Codex pass **mandatory**: this change edits the tool execution path and the admission middleware. No migration; `make db-check` still runs after deploy.

Closes #194
Closes #188
