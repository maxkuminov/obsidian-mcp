## Why

`/mcp` is the only surface on this server with no rate control of any kind. `app.mount("/mcp", APIKeyMiddleware(mcp_handler))` (`src/main.py:437`) sits outside slowapi, the container carries no Traefik `ratelimit` middleware, and the one per-credential control that does exist — `api_keys.daily_request_limit` (#162) — is opt-in, is set on **0 of 5** active production keys, counts per UTC *day* (so it bounds nothing instantaneous), and is exempt for OAuth by construction. Roughly half of production tool calls in the last 30 days ran on that exempt channel (`actor_kind` oauth 951, api_key 683).

The consumer of this server is an agent, and a retry-storming or prompt-injected agent is an ordinary input for this product. A single credential can loop on `delete_note` or `read_note` at wire speed with nothing to slow it and nothing to tell it to stop.

**This change is deliberately narrower than the first draft.** Three rounds of cross-family review each found a *new class* of interaction between the proposed admission ceilings, the connection pool and the concurrency slots — the non-convergence signal from the engineering workflow. Rather than tune another round of thresholds, every concurrency control is **deferred to a follow-up (`mcp-concurrency-slots`) that will ship in shadow mode first**, and what remains here is the layer that is simple to reason about, bounded by construction, and needs no arithmetic against the pool: **rate**, not concurrency. The one *proven* pool-exhaustion path — the transfer upload route holding a connection across a semaphore wait — was already closed by #208.

## What Changes

Seven gates, outermost first. Only L1 runs before authentication.

- **A per-IP budget on failed `/mcp` authentication** (L1), checked before the credential lookup so a refused probe costs no session and no query: `MCP_AUTH_FAILURE_LIMIT` (60) failures per `MCP_AUTH_FAILURE_WINDOW_SECONDS` (300), refused with **429 + `Retry-After`**, incremented on *every* 401 branch, the address taken from the proxied client and never from a raw header, address-less requests charged to a shared reserved slot rather than exempted. State lives in a fixed-size per-process-salted table, so memory is bounded by construction with nothing to evict.
- **A principal identity**, bound once at admission from the row `APIKeyMiddleware` already holds: `("api_key", key_id)` or `("oauth", grant_id)`. The OAuth key is the **grant** (`oauth_tokens.grant_id`, NOT NULL since migration 014), so refreshing an access token does not reset an allowance and two independently revocable grants do not share one.
- **Two per-principal token buckets** in `_tracked`, as the first gates in the decorator: a general velocity bucket (`MCP_RATE_LIMIT_PER_MINUTE` 120, burst 30) on every call, and a **write bucket** (`MCP_WRITE_RATE_LIMIT_PER_MINUTE` 60, burst 15) that the eight vault-mutating tools must pass in addition. Pure in-process arithmetic behind one dictionary lookup — no `await` between read and write, no statement, no lock.
- **The write bucket follows the bytes, not the decorator.** `PUT /transfer/upload` writes into the vault by redeeming a capability and never passes through `_tracked`, so it consumes the write bucket of the principal **that minted the token** — derivable from the transfer row and the credential identity resolution already loads (`key_id`, or `grant_id` from the minting OAuth token), with no extra query and no schema change. A refusal there is a **429 + `Retry-After`** that **releases the claim** rather than consuming it, mirroring the 503 queue-timeout path #208 established: a capability the server declined to serve is a promise still outstanding. Minting is not charged, and `import_from_url` keeps consuming the bucket at its tool call.
- **Gate order: buckets → vault → argument screens → quota → body.** The buckets go first because they are the only gate that is pure arithmetic. The daily quota keeps its position as the last pre-body gate, so #162's invariant is untouched and strengthened: **nothing durable is consumed by a call that does not run** — a rate-refused call spends no quota slot.
- **A query length cap before any embedding call.** `MAX_SEARCH_QUERY_CHARS` (8,192) enforced by a declarative `arg_char_caps` screen in `_tracked`, beside the existing unencodable-argument screen — pre-body, before the provider call, before the `tsquery` parse, before the query reaches a server-authored string. And because a character cap cannot promise a *token* limit, the provider's own input-limit rejection is translated in the body into the same caller-facing `argument_too_long` code carrying the provider's reason. Its usage marker is deliberately different (`provider_input_rejected`, **post-body**), so a real network round trip is never dropped out of the latency percentiles.
- **One caller-visible refusal shape** for every refusal raised inside `_tracked`: a machine-readable final line — `MCP-REFUSAL {"code":…,"scope":…,"limit":…,"limit_unit":…,"retry_after_seconds":…}` — appended to the existing human prose and carried unchanged into a structured tool's declared error field through `refusal_result`. The three existing pre-body refusals adopt it additively. The L1 429 is explicitly **outside** this contract: it is a transport-level refusal to an unauthenticated request, with no tool call to answer.
- **Bounded refusal recording.** `rate_limited` rows are coalesced per `(principal, tool, marker, scope)` per `MCP_REFUSAL_LOG_INTERVAL_SECONDS` (10 s): inside an open window a refusal issues **no statement of any kind**. A rollover triggered by a new refusal writes `suppressed = pending` with that refusal as the row's base; a standalone tick or shutdown flush, having no such base, writes `suppressed = pending − 1` and writes nothing at all when `pending` is zero — so `Σ (1 + suppressed)` equals the refusals observed, exactly, on any interleaving. The coalescer entry stores the **complete, immutable attribution** of the row it will write — owner, credential ids, denormalised actor, tool, marker, scope, bounded params — so a flush never reads request context and never depends on the credential still existing. `argument_too_long` is **not** coalesced: it is already bounded to the general bucket's rate, because the bucket runs above it.
- **A default daily quota for new keys.** `DEFAULT_DAILY_REQUEST_LIMIT` (5,000) applied by the key-creation paths, never as a column default: **existing keys are untouched**. On the JSON API an omitted field means the default and an explicit `null` still means unlimited; in the panel the create form is pre-filled and a blank submitted field means unlimited with no server-side substitution. The over-quota refusal's retry interval now comes from the admission's own single clock read.
- **One representation for "off"** — an empty value, `null` or `none` — resolved by one shared validator and tested through a real env file. Zero is rejected everywhere.

Limiter state is in-process and deliberately not persisted; `--workers 1` remains part of the contract, because a second worker would double every effective rate. No new dependencies. **No migration.** `src/database.py` is untouched.

**Deferred to `mcp-concurrency-slots`:** every concurrency control — the pre-authentication in-flight ceilings, the per-class/per-tenant/per-principal/global slots, the pool-budget validator, and the `slot_timeout` refusal. Rationale and the shadow-mode plan are in `design.md`; a GitHub issue is filed at archive time.

**Accepted limitations, recorded rather than closed:** OAuth grants and pre-existing NULL-limit keys have **velocity bounds only** — no durable daily ceiling (owner-approved); the L1 429 is a transport refusal outside the in-band refusal contract; a hard kill loses at most one coalescing interval of pending refusal counts per key; past the registry cap, coalesced refusal rows lose per-principal attribution but not their count; and concurrency remains unbounded until the follow-up ships.

## Capabilities

### New Capabilities

- `mcp-rate-limits`: the failed-authentication address budget, principal identity, the two per-principal velocity buckets, the in-band refusal shape, bounded coalesced refusal recording, the argument length cap and the provider-rejection translation, and the bounded in-process limiter state.

### Modified Capabilities

- `usage-quotas`: the OAuth exemption is re-scoped to the daily quota alone and the velocity-only residual is stated; the retry interval comes from the admission's own clock read; new keys receive a configurable default while existing keys are grandfathered.
- `file-transfer`: `PUT /transfer/upload` consumes the minting principal's write rate before any body is read, refusing with 429 + `Retry-After` and releasing the claim.
- `mcp-request-routing`: vault resolution is required before every tool *body*, qualified so a call refused by an earlier gate in the same decorator need not resolve the root — with scenarios pinning that such a refusal reveals nothing about the vault.
- `panel-performance-views`: the pre-body predicate enumerates the two new pre-body markers and not the post-body provider marker; the refusal count sums `1 + suppressed` through a guarded cast.
- `panel-usage-slicing`: the key create form is pre-filled with the default and a blank field means unlimited.

## Impact

- `src/services/refusals.py` (new), `src/services/rate_limits.py` (new), `src/auth/session.py`, `src/mcp_server/auth.py`, `src/mcp_server/tools.py`, `src/config.py`, `src/services/indexer.py` (tick flush), `src/main.py` (shutdown flush), `Dockerfile`.
- `src/services/usage_stats.py`, `src/services/quotas.py`, `src/services/embeddings.py`, `src/transfer/routes.py`, `src/services/transfer.py`, `src/api/routes.py`, `src/control_panel/routes.py`, `src/control_panel/templates/keys.html`.
- Docs: new `docs/architecture/rate-limits.md`, plus `docs/architecture/usage-attribution.md`, `docs/architecture/search.md`, `CLAUDE.md`, `README.md`, `.env.example`, `docker-compose.yml` (recorded decision).
- Adversarial Codex pass **mandatory**: this change edits the tool execution path and the admission middleware. No migration; `make db-check` still runs after deploy.

Closes #194
Closes #188
