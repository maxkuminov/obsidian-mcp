## Why

`/mcp` is the only surface on this server with no rate control of any kind. `app.mount("/mcp", APIKeyMiddleware(mcp_handler))` (`src/main.py:437`) sits outside slowapi, the container carries no Traefik `ratelimit` middleware, and the one per-credential control that does exist — `api_keys.daily_request_limit` (#162) — is opt-in, is set on **0 of 5** active production keys, counts per UTC *day* (so it bounds nothing instantaneous), and is exempt for OAuth by construction. Roughly half of production tool calls in the last 30 days ran on that exempt channel (`actor_kind` oauth 951, api_key 683).

The consumer of this server is an agent, and a retry-storming or prompt-injected agent is an ordinary input for this product. One credential can currently issue unbounded concurrent tool calls against a **single uvicorn worker** (`Dockerfile:31`, `--workers 1`) sharing a **15-connection pool** (5 + 10) with every other tenant, a 60 s `statement_timeout`, embedding calls of up to 180 s, and a *single sequential* multi-user indexer that every write amplifies into. That is a cross-tenant availability failure of exactly the shape `CLAUDE.md` ranks as expensive, and it is the only thing that would bound the blast radius of an agent bulk-deleting or bulk-exfiltrating a tenant vault.

Both original cross-family audit notes are folded in, and so are two rounds of Codex review of this proposal. In particular: a limiter that begins *after* authentication leaves the pool exhaustible by a valid credential and an identity-blind ceiling is not fair; concurrency slots must be taken before the durable quota is consumed; a refusal an agent cannot parse is not an actionable refusal; and a character cap on a search query cannot promise the provider will accept it.

## What Changes

Nine layers, outermost first. L0a–L0c run **before any SQL**.

- **Three pre-authentication admission gates.** A **per-credential in-flight cap** (`MCP_MAX_INFLIGHT_PER_CREDENTIAL`, 8) keyed on the SHA-256 the middleware already computes for its lookup — used only as an in-memory table index, never logged, never persisted — so one credential cannot occupy the process. An **identity-blind ceiling** (`MCP_MAX_INFLIGHT_REQUESTS`, 32), so at least four distinct credentials are always admissible. And an **authentication concurrency bound** (`MCP_MAX_CONCURRENT_AUTHENTICATIONS`, 3) held only across the middleware's own database session — the gate that makes "cannot drain the pool" provable rather than asserted. All three answer **503 + `Retry-After`** with no session, no query and no usage row. The arithmetic is explicit and boot-checked: 3 authentications + 9 tool bodies = 12 of 15 connections, leaving 3 for the indexer, the panel and `/health`.
- **A per-IP budget on failed `/mcp` authentication**, checked before the credential lookup: 60 failures per 300 s, **429 + `Retry-After`**, incremented on *every* 401 branch, address taken from the proxied client and never from a raw header, address-less requests charged to a shared slot rather than exempted. State lives in a fixed-size per-process-salted table, so memory is bounded by construction.
- **A principal identity** bound from the row already loaded: `("api_key", key_id)` or `("oauth", grant_id)`. The OAuth key is the **grant** (`oauth_tokens.grant_id`, NOT NULL since migration 014), so a refresh does not reset an allowance and two independently revocable grants do not share one.
- **Two per-principal token buckets** in `_tracked`: a general **velocity** bucket (120/min, burst 30) and a **write** bucket (60/min, burst 15) that the eight vault-mutating tools must also pass. Pure in-process arithmetic — no `await`, no statement, no lock.
- **Bounded concurrency slots** under one shared deadline of approximately 5 s, acquired per-principal-class → per-principal → **per-tenant-class** → class → global. **Every tool now has a class** — `embedding`, `vector`, `write`, and `other` for everything else — and the global ceiling is at least the sum of all four, which is what guarantees a call holding a class slot can never queue behind the global one. **Every** expensive class has a per-tenant cap strictly below its ceiling, so one user cannot occupy a class through an API key *and* an OAuth grant.
- **Slots are taken BEFORE the daily quota** and released if the quota then refuses: nothing durable is consumed by a call that does not run.
- **One caller-visible refusal shape.** Every pre-body refusal ends with a machine-readable final line — `MCP-REFUSAL {"code":…,"scope":…,"limit":…,"limit_unit":…,"retry_after_seconds":…}` — appended to the existing human prose and carried unchanged into a structured tool's declared error field. `retry_after_seconds` is absent where retrying is futile. The three existing pre-body refusals adopt it additively.
- **Bounded refusal recording.** `rate_limited` and `slot_timeout` rows are coalesced on `(principal, tool, marker, scope)` per 10 s; refusals inside an open window issue **no statement at all**, and the pending count is flushed as `suppressed` by the next row, the next usage-log write, the indexer tick, or shutdown. `1 + suppressed` totals exactly.
- **A query length cap before any embedding call** (`MAX_SEARCH_QUERY_CHARS`, 8,192) via a declarative `arg_char_caps` screen in `_tracked` — *and*, because a character cap cannot promise a token limit, the provider's own input-limit rejection is translated in the body into the same caller-facing `argument_too_long` code with the provider's reason. Its usage marker is deliberately different (`provider_input_rejected`, **post-body**), so a real network round trip is never dropped out of the latency percentiles.
- **Bounded limiter state.** Caller-mintable keys (fingerprints, addresses) use fixed-size salted tables; principal state uses a capped registry with TTL eviction that never evicts a depleted or busy entry, overflowing into one shared bucket.
- **A default daily quota for new keys** (`DEFAULT_DAILY_REQUEST_LIMIT`, 5,000), applied in application code so **existing keys are untouched**. On the JSON API an omitted field means the default and explicit `null` means unlimited; in the panel the create form is pre-filled and a blank submitted field means unlimited with no server-side substitution.
- **One representation for "off"** — empty, `null` or `none` — resolved centrally and tested through a real env file. Zero is rejected everywhere.

Limiter state is in-process and deliberately not persisted; `--workers 1` becomes part of the contract. No new dependencies. **No migration.**

**Accepted limitations, recorded rather than closed:** OAuth grants and pre-existing NULL-limit keys have **velocity bounds only** — no durable daily ceiling (owner-approved); a hard kill loses at most one coalescing interval of refusal counts per key; the shared overflow bucket can let one overflowing principal refuse another, past 10,000 tracked principals; and a `slot_timeout`'s `retry_after_seconds` is a hint, since an embedding slot can be held for a full provider call.

## Capabilities

### New Capabilities

- `mcp-rate-limits`: the three pre-authentication admission gates and the pool arithmetic, the failed-authentication address budget, principal identity, the two per-principal buckets, the four-class concurrency slots with per-tenant caps, the refusal shape, bounded coalesced refusal recording, the argument length cap and the provider-rejection translation, bounded limiter state, and the in-process/single-worker constraint.

### Modified Capabilities

- `usage-quotas`: slots are acquired before quota admission; the OAuth exemption is re-scoped to the daily quota alone and the velocity-only residual is stated; the retry interval comes from the admission's own clock read; new keys receive a configurable default while existing keys are grandfathered.
- `mcp-request-routing`: vault resolution is required before every tool *body*, qualified so that a call refused by an earlier gate in the same decorator need not resolve the root — with scenarios pinning that such a refusal reveals nothing about the vault.
- `panel-performance-views`: the pre-body predicate enumerates the three new pre-body markers and not the post-body provider marker; the refusal count sums `1 + suppressed` through a guarded cast.
- `panel-usage-slicing`: the key create form is pre-filled with the default and a blank field means unlimited.

## Impact

- `src/services/refusals.py` (new), `src/services/rate_limits.py` (new), `src/auth/session.py`, `src/mcp_server/auth.py`, `src/mcp_server/tools.py`, `src/config.py`, `Dockerfile`.
- `src/services/usage_stats.py`, `src/services/quotas.py`, `src/services/embeddings.py`, `src/api/routes.py`, `src/control_panel/routes.py`, `src/control_panel/templates/keys.html`.
- Docs: new `docs/architecture/rate-limits-and-concurrency.md`, plus `docs/architecture/usage-attribution.md`, `docs/architecture/search.md`, `CLAUDE.md`, `README.md`, `.env.example`, `docker-compose.yml` (recorded decision).
- Adversarial Codex pass **mandatory**: this change edits the tool execution path and the admission middleware. No migration; `make db-check` still runs after deploy.

Closes #194
Closes #188
