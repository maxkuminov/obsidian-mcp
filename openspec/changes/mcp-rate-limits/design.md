## Context

- `/mcp` is mounted at `src/main.py:437` as `APIKeyMiddleware(mcp_handler)`, outside the slowapi limiter that decorates every other public route.
- `APIKeyMiddleware` computes `hash_key(token)` before any database work, then opens **one** `async_session()` for the credential lookup, the `last_used_at` update and the vault warm; that session closes **before** `await self.app(...)`. It binds seven request-scoped ContextVars from the row it loaded, under the house rule *the row is in hand, do not add a round trip*.
- `oauth_tokens.grant_id` is **NOT NULL** and indexed (migration 014, #64): every row minted from one `/authorize` approval shares it and every rotation inherits it.
- `_tracked` (`src/mcp_server/tools.py:626`) is the decorator all 25 tools inherit, running three pre-body gates — vault admission (#66), the unencodable-argument screen (#149), the daily quota (#162) — each mapped through `refusal_result` so a structured tool refuses in its declared shape.
- `_log_usage` already survives a credential deleted mid-call: it catches SQLSTATE 23503, retries once with `key_id`/`oauth_token_id` cleared and the denormalised `actor_*` columns kept (#77). That recovery is what makes a deferred flush safe.
- `Admission` (`src/services/quotas.py:169`) carries `day` and `count` and derives `reset_at` from `day`. It carries **no decision instant**.
- **One worker.** `Dockerfile:31` — `uvicorn … --workers 1`.
- Embedding calls: Ollama 30 s per call; OpenAI up to 60 s per attempt, up to 3 attempts.
- `delete_note` and `delete_file` default to `permanent=False` (trash).
- Precedents for an argument cap: `MAX_LIST_PATTERN_CHARS` (1,024, #204), `MAX_NOTE_BYTES`.

## Deferred to `mcp-concurrency-slots`

Everything that bounds **concurrency** is removed from this change: the pre-authentication in-flight ceilings (identity-blind, per-credential fingerprint, and the authentication-concurrency semaphore), the per-class / per-tenant / per-principal / global tool slots, the `slot_timeout` refusal, and the boot validator that checked those numbers against the connection pool.

**Why.** Three successive cross-family review rounds each produced a *new class* of finding in exactly that area — first the pool being exhaustible on the authentication path itself, then tenant-level admission reachable through rotated tokens, then the refusal rows' own pool usage. Each round's fix was locally correct and each opened a fresh interaction between admission ceilings, the 5 + 10 pool and the slot lattice. That pattern is the engineering workflow's non-convergence signal: it means the design is a heuristic being tuned, not a rule being stated, and the right response is to stop adding rounds and narrow the change. Rate limiting needs no arithmetic against the pool and no lattice; concurrency limiting needs both, and it deserves its own change with its own review budget.

**How it will ship.** `mcp-concurrency-slots` ships in **shadow mode first**: the slots are computed and the would-be refusal is recorded in `usage_logs` — the same markers, a `shadow: true` flag — while every call still runs. Only after a period of real traffic shows what the ceilings would actually have refused does enforcement turn on. That is the correct order for a control whose failure mode is refusing a legitimate agent, and it is only possible because the observability half of this change lands first.

**What still holds meanwhile.** The one *proven* pool-exhaustion path — `PUT /transfer/upload` re-checking out a connection and holding it across a semaphore wait and the whole body stream — was closed by **#208**, already merged. Concurrency on the tool surface remains unbounded until the follow-up ships; that is stated in the accepted limitations rather than implied.

A GitHub issue is filed for it at archive time (task 6.9).

## Goals / Non-Goals

**Goals.** Bound the *rate* at which one principal — API key or OAuth grant — can create work; bound the database work an unauthenticated caller can force; make every refusal machine-parseable by the agent that receives it; make the pressure visible without letting the refusals become the load; add no I/O to the common path and no unbounded state.

**Non-Goals.**
- **Concurrency.** Above.
- **Indexer fairness (#202).** Round-robin over `_active_user_ids()`, a per-tenant chunk budget and a max-chunks-per-note cap belong to that issue.
- **A new durable ceiling for OAuth grants or grandfathered keys.** Owner decision: velocity bounds only. Accepted limitation below.
- **Expiring unused DCR clients.** OAuth housekeeping, not a rate control. Residual on #194.
- Distributed or persisted limiter state, per-tenant billing, a Traefik `ratelimit` label, or changing the daily quota's UTC-day semantics.

## The control table

| L | Control | Where | Scope key | Default (setting) | Refusal | Marker |
|---|---|---|---|---|---|---|
| 1 | Failed-auth budget | `APIKeyMiddleware`, before the credential lookup | address slot (salted fixed table) | 60 / 300 s (`MCP_AUTH_FAILURE_LIMIT`, `MCP_AUTH_FAILURE_WINDOW_SECONDS`; null ⇒ off) | HTTP **429** + `Retry-After`, no query — **transport, not a tool result** | none; one WARNING per slot per window |
| 2 | General velocity bucket | `_tracked`, first gate | principal | 120/min, burst 30 (`MCP_RATE_LIMIT_PER_MINUTE`, `MCP_RATE_LIMIT_BURST`) | in-band, sentinel line | `rate_limited`, scope `principal` — **coalesced** |
| 3 | Write velocity bucket | `_tracked`, write tools only | principal | 60/min, burst 15 (`MCP_WRITE_RATE_LIMIT_PER_MINUTE`, `MCP_WRITE_RATE_LIMIT_BURST`) | in-band | `rate_limited`, scope `principal_write` — **coalesced** |
| 4 | Vault admission (existing #66) | `_tracked` | user | — | in-band | `no_vault_assigned` |
| 5a | Unencodable-argument screen (#149) | `_tracked` | argument | — | in-band | `argument_not_encodable` |
| 5b | Query length cap | `_tracked`, beside 5a | argument | 8,192 (`MAX_SEARCH_QUERY_CHARS`) | in-band | `argument_too_long` — **own row** |
| 6 | Daily quota (existing #162) | `_tracked`, last pre-body gate | api key | 5,000 for new keys (`DEFAULT_DAILY_REQUEST_LIMIT`) | in-band | `over_quota` |
| 7 | Provider input rejection | inside the body, on the provider's answer | argument | provider's own limit | in-band, `argument_too_long` **code** | `provider_input_rejected` — **post-body** |

Write tools (L3): `create_note`, `edit_note`, `move_note`, `delete_note`, `set_frontmatter`, `write_file`, `delete_file`, `import_from_url` — every `_tracked` tool that changes vault bytes and therefore amplifies into the next indexer pass.

## Decisions

**D1. The principal is the grant, not the token, and not (client, user).** `oauth_tokens.grant_id` is NOT NULL, indexed, shared by every rotation of one `/authorize` approval, and already on the loaded row — so `("oauth", grant_id)` costs no query. Keying on `oauth_tokens.id` would hand a refreshing agent a fresh allowance hourly. Keying on `(client_id, user_id)` merges two grants that #64 made independently revocable, so revoking one would not free the other's allowance and the operator's stop would look like it had not worked. `("api_key", api_keys.id)` for the other branch. `current_principal` is set and `reset()` in the same `finally` as the other ContextVars.

**D2. No principal ⇒ no per-principal control, deliberately.** Sandbox mode short-circuits the middleware; a direct in-process caller never passes it. Both read `None` and are exempt, the same shape as `_quota_admission_error`'s "a limit with no key is exempt rather than a crash". Nothing untrusted reaches that path — untrusted traffic arrives through the middleware, which binds a principal or returns 401/429. L1 is not keyed on the principal and applies regardless.

**D3. Gate order, and the invariant it preserves.** L2 → L3 → L4 → L5 → L6 (quota) → body.
- *A token is not a quota slot.* A rate token refills, so consuming one on a call a later gate refuses is correct — the refusal itself costs work. The buckets come first so the flood we most want to shed is shed by arithmetic before anything touches a cache, an argument tree or the database.
- *Nothing durable is consumed by a call that does not run.* The quota keeps its #162 position as the **last** pre-body gate, so a call refused for having no vault, for an unencodable argument, for an over-long query — or now for exceeding its rate — consumes no daily slot. Removing the concurrency slots restores this to exactly the shape #162 designed, with one more gate above it.

Because L2/L3 sit *above* the vault gate, a call can be refused before its vault root is resolved, which the `mcp-request-routing` requirement did not contemplate. That requirement is **modified in this change** to say "before its body, unless the call was already refused by an earlier gate" — the substance is unchanged (no tool body ever runs without a resolved root; the gate lives in the shared decorator; no exemptions), and a rate-refused call runs no body and reveals nothing about the vault: its content depends only on the caller's own request rate.

**D4. Two buckets, because velocity and destruction are different questions.** The general bucket is a **velocity** bound: it stops a hot loop and bounds the rate at which any work is created. It is *not* a blast-radius bound — 120 deletes/minute empties a 2,577-note vault in about twenty minutes. The write bucket halves that. Neither bounds totality; the daily quota does.

> **Accepted limitation, owner-approved.** OAuth principals and pre-existing NULL-limit API keys have **velocity bounds only** — no durable ceiling on total destructive work in a day. Closing it would mean a quota for OAuth (rejected in #162: panel OAuth is the operator, and an operator locked out by their own ceiling cannot raise it) or a backfill onto existing keys (rejected here: grandfathering is the whole point of D9). The operator lever is to set a limit on the five live keys after deploy (task 6.8); the mitigations that remain are `permanent=False` trash recovery and the write bucket.

Rate limiting also never prevents a *single* destructive write, which is what the `vault-tools` disciplines are for. The 60/15 numbers are an owner decision (Open Questions).

**D5. One caller-visible refusal shape — for refusals raised inside `_tracked`.** `params.error` is an operator's field, invisible to the caller, so "typed, actionable refusal" has to mean something the agent can parse. A new `src/services/refusals.py` — importing nothing from the app, so `tools.py`, `quotas.py`, `embeddings.py` and `rate_limits.py` can all use it without a cycle — defines a `Refusal` carrying `code`, `scope`, `limit`, `limit_unit`, `retry_after_seconds`, the closed `code` set, **and the provider input-limit exception type** (see D6), and one renderer appending a final line to the existing prose:

```
MCP-REFUSAL {"code":"rate_limited","scope":"principal","limit":120,"limit_unit":"calls_per_minute","retry_after_seconds":3}
```

`str` tools get prose + that line; structured tools get the identical complete text in their declared error field through `refusal_result`, so no output-schema validation can fail and both carry the same fields. The sentinel is line-initial and the JSON one line, so it survives being quoted into a transcript. `retry_after_seconds` is a number ≥ 1 wherever retrying can help and **absent** for `no_vault_assigned` and `argument_not_encodable`, where a number would invite a loop that cannot end. The three existing pre-body refusals adopt the line **additively** — prose unchanged, so every `in`/`startswith` assertion still holds.

> **Accepted limitation.** The contract covers refusals raised **inside `_tracked`**, where a tool call exists to answer. L1's 429 is a *transport* refusal to a request that never authenticated: there is no tool call, no principal and no `usage_logs` row, so it carries `Retry-After` and `WWW-Authenticate` headers instead of a sentinel line. An agent that only parses tool results will see an HTTP error there; that is the honest shape of an unauthenticated rejection, and pretending otherwise would mean answering an unauthenticated request with a fabricated tool result.

**D6. The query cap is pre-body; the provider's own limit is translated in the body.** A character cap is necessary and **not sufficient**: 8,192 characters of densely-tokenizing script can still exceed a provider's token limit, so the cap alone cannot promise the provider will accept the input. Two halves, on opposite sides of the body/no-body line:

- **L5b, pre-body.** `arg_char_caps={"query": MAX_SEARCH_QUERY_CHARS}` beside the existing `_first_unencodable_argument` screen — a generic argument screen already lives there, so this generalises to any future argument. It refuses before the provider call, the `tsquery` parse and any search or quota statement, and it joins `pre_body_refusal_sql()`.
- **L7, post-body.** When the provider answers with its own input-limit error, the tool translates it into the same caller-facing `code` — `argument_too_long`, with the provider's reason — so the agent sees one actionable failure mode instead of a raw provider error. **The usage marker is different**: `provider_input_rejected`, classified **post-body** and deliberately *not* in the pre-body predicate, because the body ran, resolved a vault and made a network call, and enumerating it would drop a real round trip out of the percentiles. The classification rule applied exactly: the caller-facing code and the operator-facing marker answer different questions and may differ.

The exception type raised by the providers is declared in `refusals.py`, not in `embeddings.py`, so the slice that handles it (A, in `tools.py`) and the slice that raises it (D, in `embeddings.py`) share a dependency-free contract and A's tests can stub a provider without waiting for D.

Note what the cap is *not* for: #194's verification withdrew the cost-amplification claim (Ollama truncates, OpenAI rejects over 8,192 tokens). The reasons are an unbounded argument interpolated into `f"No results for '{query}'"` (the #149 discipline), `tsquery` parsing on the single event loop (the #204 class), and — before L7 — an OpenAI deployment turning an over-long query into a raw provider error where the contract promises a typed refusal.

**D7. Refusal recording is bounded by coalescing, and the coalescer owns a complete row.** A refusal is cheap to produce, so before this the cheapest thing an agent could do was generate database writes — and unlike an admitted call, a `rate_limited` refusal occurs at the caller's *arrival* rate, which is precisely the rate nothing bounds. So `rate_limited` rows are coalesced on `(principal, tool, marker, scope)`:

- First refusal for a key: write a row now with `suppressed = 0` and open a window.
- Refusal inside an open window: `pending += 1`, **no INSERT and no UPDATE** — no statement of any kind.
- Refusal after the window closed: write one row carrying `suppressed = pending`, reset.
- **Flush**: a key whose window has closed with `pending > 0` is written by the next row for that key, by the indexer's periodic tick, or at lifespan shutdown **before `engine.dispose()`** — whichever comes first.

**The entry stores the complete, immutable attribution of the row it will write** — owner `user_id`, `key_id` / `oauth_token_id`, the denormalised `actor_*` triple, tool, marker, scope and the bounded params — captured at the moment of the first refusal. A deferred flush therefore reads **no ContextVar** and depends on **no live credential**: by flush time the request is long gone and the key may have been deleted, and `_log_usage`'s existing 23503 recovery (clear the FK ids, keep `actor_*`) is exactly the path that makes such a row land anyway. That recovery exists because #77 needed the label to survive the credential; here it does double duty.

`scope` is in the key because `principal_write` and `principal` are different facts about the same tool, and merging them would attribute a write-bucket refusal to the general one.

**`argument_too_long` is deliberately NOT coalesced.** It sits *below* the general bucket, so a principal can produce at most `MCP_RATE_LIMIT_PER_MINUTE` of them per minute — the same bound as any admitted call's row, and therefore already bounded without a mechanism. Adding coalescing there would buy nothing and cost a second code path. Spec and design say the same thing.

Cardinality is bounded by the same registry cap as the buckets: past `MCP_LIMITER_MAX_TRACKED_PRINCIPALS` entries, further keys fold into a **single shared overflow entry per marker**, whose flushed row carries scope `overflow` and no per-principal attribution.

> **Accepted limitations.** (a) An abrupt process termination (SIGKILL, OOM) loses the pending counts of open windows — at most one interval's worth per active key; the alternative is a durable write per refusal, which is the amplification this exists to stop. (b) Past the registry cap, refusal rows lose per-principal attribution but **not** their count.

**Rejected:** "the gate refusal writes no row." A limiter invisible in the log is one nobody can diagnose or size, and `/admin/performance` is where an operator looks.

**D8. Nothing blocks while holding the loop, and nothing adds I/O.** A bucket update is a synchronous function — read `(tokens, last_refill)`, compute, write back — with no `await` between read and write, so on a single-threaded event loop it is atomic by construction and needs no lock. The L1 table is the same shape. The admitted common path is a couple of dict/table lookups and some floats: no statement, no session checkout. Pinned by a statement-counting test, as `tests/test_issue_162_quota_gate.py` pins the quota gate — the only way this regresses invisibly.

**D9. The default daily limit is applied in application code, never as a column default.** A `server_default` would be a schema change, would apply to every future insert path, and still could not express "grandfather the rows that exist". The JSON API distinguishes omitted from explicit-null by `model_fields_set` — `null` is documented as unlimited and must stay so. The **panel** materialises the default only as the create form's pre-filled value: a blank submitted field is an explicit unlimited with **no POST-side substitution**, so the operator's last view of the field is what the key receives, and `keys_page` passes the default to the template.

**Why 5,000.** ~1,600 tool calls per 30 days across all credentials, so 5,000/day is two orders of magnitude of headroom and cannot interrupt a real session, while a runaway stops the same day. At 120/min it takes ≥ 42 minutes to spend. It binds only keys created after this ships.

**D10. Quota `retry_after_seconds` comes from the admission's own clock read.** `Admission` carries `day` and `count`, and `reset_at` is derived from `day`; computing a retry interval would need a second `datetime.now()` — precisely the double-clock-read bug the class exists to prevent (#162). So `admit()` records the instant it already read as `decided_at` on the `Admission` and derives `retry_after_seconds = ceil(reset_at − decided_at)`, minimum 1, with nothing downstream re-reading the clock. A test crosses UTC midnight between the decision and the message and asserts the interval is small, not ~48 hours.

**D11. The failed-auth budget lives in the app; Traefik is the wrong instrument.** A proxy cannot condition on the response status, so a `ratelimit` label on `obsidian-mcp-api-rtr` would throttle authenticated agents to bound unauthenticated probing — and `/transfer/*`, `/health` and `/.well-known` share that router. The host's Traefik static configuration is outside this repo (`CLAUDE.md`, "Public repo — host paths live outside the tree"), so a control expressed there would not be reproducible from the tree. `docker-compose.yml` labels unchanged, with a comment recording the decision.

Three details make it correct. The address comes from `ProxyHeadersMiddleware`, which is added on the app and therefore wraps the mount — a budget keyed on a spoofable header is worse than none, and `request-trust`'s restricted-proxy-header requirement is the dependency. **Every** 401 branch increments — missing bearer, unknown credential, ownerless, inactive user, expired, cross-user grant, missing vault scope — because a prober picks the cheapest one, and a budget covering six of seven bounds nothing. A request with no client address is charged to one reserved slot shared by all such requests, rather than exempted: exempting is a bypass anyone who can strip the header gets for free.

What it bounds is the **database work an unauthenticated caller can force** — one session checkout and one indexed SELECT per probe. It is *not* a defence against credential guessing; #194's own verification withdrew that (256-bit `secrets.token_hex` keys).

**D12. Threshold and `Retry-After` arithmetic, in one helper.** Refuse when the count already recorded in the window is `≥ MCP_AUTH_FAILURE_LIMIT`, so with 60 the 61st failure is the first refused, and a refused request does not increment (it never reached authentication). `Retry-After` is the whole seconds remaining in the current window, minimum 1. One helper, one test parameterised over every 401 branch.

**D13. Limiter state is bounded by construction, and each registry says how.**
- **Addresses** are unauthenticated and unbounded in cardinality — a caller mints a new one per request for free — so eviction is a losing game. The failed-auth budget uses a **fixed-size table** of `MCP_AUTH_FAILURE_TABLE_SIZE` (4,096) counters indexed by a **per-process randomly salted** hash. Memory is O(size), nothing to evict, collisions only make the control stricter, and the random per-process salt means nobody can choose to collide with a victim.
- **Principals** are authenticated, so cardinality is bounded by the credentials that exist. A dict with a hard cap (`MCP_LIMITER_MAX_TRACKED_PRINCIPALS`, 10,000) and TTL eviction swept amortised on insert (bounded work per admission; no background task). An entry is evictable only when **full and idle**: a depleted bucket must not be evicted (a fresh entry starts full, so eviction would grant free capacity) and an entry with a pending coalescer count must not be evicted (its count is unflushed). Past the cap, further principals share **one overflow bucket**.

> **Accepted limitation.** The overflow bucket is shared, so past the cap an overflowing principal's traffic can cause an unrelated overflowing principal to be refused. It fires only beyond 10,000 tracked principals — a state requiring more than ten thousand live credentials. Fail-open was rejected (it lets the flood succeed) and fail-closed was rejected (it turns a bookkeeping cap into an outage for a legitimate credential).

**D14. Limiter state is in-process and not persisted.** A restart begins with every bucket full and every counter zero. Sound because there is exactly **one** uvicorn worker; because instantaneous pressure is meaningless to persist across a process that no longer exists; because a restart is an operator action or a crash, not something a caller can induce; and because the durable ceiling already exists as `quota_counters`. **The worker count is part of the contract** — `--workers N` multiplies every in-process rate by N. A comment goes at the `CMD` and the architecture note says it in prose.

**D15. Boot validation, and one representation for "off".** With the concurrency lattice gone there is no pool equation and no class-sum rule left to check, and `src/database.py` is untouched: settings validation now enforces only that each bucket's rate and burst are both set or both null, that `default_daily_request_limit` lies within 1..1,000,000, and that **zero is rejected everywhere** (#162's reason: a control that refuses every call reads as an outage, not a setting). Null is the only disable, following `daily_request_limit`'s NULL-is-unlimited idiom — and because `.env` has no JSON null, one central `BeforeValidator` maps an empty value, `null` or `none` (stripped, case-insensitive) to `None` for every nullable limiter setting, tested **through a real env file**.

**D16. Three markers, split by what an operator asks.** `rate_limited` (either bucket, `scope` distinguishes) and `argument_too_long` — both **pre-body**, both in `pre_body_refusal_sql()` — and `provider_input_rejected`, **post-body** and deliberately outside it. "Is one agent too fast?", "did a caller send something too big?" and "did the provider refuse what we sent?" are answerable from the marker alone. `rate_limit_scope` is a string no reader casts; `suppressed` is an integer read with a guarded cast.

## Rejected findings

- **Round 1 — "…or the gate refusal writes no row."** Coalescing with a flush lifecycle instead (D7). A limiter invisible in the log cannot be diagnosed or sized.
- **Round 1 — a per-tenant *reservation* for the embedding class.** Moot here (concurrency deferred); carried to `mcp-concurrency-slots` with the reason: with two slots and N tenants there is nothing to reserve.
- **Round 1 — the write bucket as a blast-radius fix.** Accepted as a control, rejected as a solution: D4 states that no bucket bounds totality and records the OAuth/grandfathered residual as owner-accepted.
- **Round 2 — "acquire global FIRST, then narrow inward."** Moot here; carried to the follow-up with the reason: global-first makes a task hold the global slot while waiting for a class slot, so one saturated class parks tasks that block every tenant.
- **Round 3 — the remaining admission/pool findings.** Not rejected on their merits: the controls they attach to are **deferred wholesale** to `mcp-concurrency-slots`, where they are the starting material rather than a third round of patches on a design that had stopped converging.

## Alternatives rejected

- **slowapi on the `/mcp` mount.** Keyed on the remote address (wrong scope: shared egress merges tenants, one tenant with two agents is one bucket) and decorator-shaped for routes, not an ASGI mount; it produces an HTTP 429 where the contract needs a parseable in-band tool result.
- **A Traefik `ratelimit` middleware as the primary control.** D11.
- **Making the daily quota the burst control by lowering it.** A per-day counter cannot bound a burst at all, and a low daily number to approximate a rate would exhaust a legitimate agent's day in one healthy burst. #188's own recommendation is necessary and insufficient.
- **Persisting bucket state in PostgreSQL** (the `quota_counters` shape). A statement on the hottest path in the server to bound something meaningless once the process is gone, with the durable ceiling already one layer up.
- **Refunding a token on a refusal.** #162's reasoning: a tool that always fails would be free.
- **Enforcing the query cap inside the search bodies.** D6 — it would be a post-body marker polluting the percentiles.
- **Fail-open or fail-closed on principal-registry overflow.** D13.

## Risks / Trade-offs

- **Concurrency stays unbounded until the follow-up.** The deliberate cost of narrowing. Mitigated by the rate buckets (a bounded arrival rate bounds how fast concurrency can build), by #208 having closed the one proven pool-exhaustion path, and by the follow-up's shadow mode measuring before it enforces. **Declared.**
- **Shared egress IPs and slot collisions.** claude.ai egresses from shared addresses, and the hashed table can merge two addresses into one slot. Both make the control stricter, never weaker. Mitigated by a generous default (60 failures / 5 min — no working client fails 60 times in 5 minutes), by `MCP_AUTH_FAILURE_LIMIT` = null, and by one WARNING naming the address the first time a slot engages. **Declared.**
- **A shared key is a shared bucket.** Two agents on one credential contend, exactly as they do for the daily quota. Same answer as #162: an agent that deserves isolation deserves its own credential.
- **Coalescing means the log is a summary.** The row count is no longer the refusal count; `1 + suppressed` is, and it is exact only after a flush. Every reader moves in one slice.
- **The transport 429 is outside the refusal contract.** D5. An agent that only parses tool results sees an HTTP error there.
- **The defaults are guesses against a small sample** (~1,600 calls/30 days). Every one is a setting; read `/admin/performance` for a week before treating any as settled.
- **A new gate on the hottest path in the server.** Mitigated by D8 and pinned by a statement-counting test.
- **`--workers 1` becomes load-bearing.** D14.

## Migration Plan

None. No schema change, no backfill, no new dependency, and `src/database.py` untouched. `make db-check` still runs after deploy. Rollback is a redeploy of the previous image — settings are read at boot and nothing is persisted. Every control can be disabled in `.env`: null the nullable settings and null `DEFAULT_DAILY_REQUEST_LIMIT`.

## Open Questions

1. **General bucket — 120/min, burst 30?** Recommended: far above observed usage, stops a hot loop within a second, burst covers a post-search fan-out.
2. **Write bucket — 60/min, burst 15?** Owner decision recorded. 60/min still reaches every note here in ~45 minutes, so it is a velocity bound (D4); 20/min would make bulk destruction obvious well before it completed, at the cost of slowing a legitimate bulk import.
3. **`DEFAULT_DAILY_REQUEST_LIMIT` — 5,000?** Recommended (D9). Alternative: 2,000, still ~40× observed usage, halving a runaway's reach.
4. **`MCP_AUTH_FAILURE_LIMIT` — 60 per 5 minutes?** Recommended, with the shared-egress risk as the reason it is not lower.
5. **Should existing production keys be given a limit at deploy time?** #194 recommends it, and D4's accepted limitation is why it matters: those five keys have velocity bounds only until an operator sets one. Post-deploy task 6.8.
6. **Is shadow-mode-first the right shape for `mcp-concurrency-slots`?** Recommended. It is the only way to learn what a ceiling would have refused before it refuses anything, and this change's observability half is what makes it measurable.
