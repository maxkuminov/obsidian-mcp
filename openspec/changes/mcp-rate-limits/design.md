## Context

- `/mcp` is mounted at `src/main.py:437` as `APIKeyMiddleware(mcp_handler)`, outside the slowapi limiter that decorates every other public route.
- `APIKeyMiddleware` computes `hash_key(token)` — a SHA-256 of the presented bearer token — *before* any database work, then opens **one** `async_session()` for the credential lookup, the `last_used_at` update and the vault warm. That session is closed **before** `await self.app(...)` runs, so authentication and the tool body do not hold a connection at the same time. Both facts are load-bearing below.
- The middleware already binds seven request-scoped ContextVars from the row it loaded. The house rule is explicit: *the row is in hand, do not add a round trip.*
- `oauth_tokens.grant_id` is **NOT NULL** and indexed (migration 014, #64): every row minted from one `/authorize` approval shares it and every rotation inherits it.
- `_tracked` (`src/mcp_server/tools.py:626`) is the decorator all 25 tools inherit, running three pre-body gates — vault admission (#66), the unencodable-argument screen (#149), the daily quota (#162) — each mapped through `refusal_result` so a structured tool refuses in its declared shape.
- `Admission` (`src/services/quotas.py:169`) carries `day` and `count` and derives `reset_at` from `day`. It carries **no decision instant**, which matters below.
- `FastMCP(stateless_http=True)`: every tool call is an independent POST.
- **One worker.** `Dockerfile:31` — `uvicorn … --workers 1`.
- **Pool is 5 + 10 = 15**, shared with the panel, the indexer and `/health`; `statement_timeout` 60 s, `pool_timeout` 30 s.
- Embedding calls: Ollama 30 s per call; **OpenAI up to 60 s per attempt with up to 3 attempts** — a worst case of about 180 s inside one tool body.
- `delete_note` and `delete_file` default to `permanent=False` (trash).
- Precedents for an argument cap: `MAX_LIST_PATTERN_CHARS` (1,024, #204), `MAX_NOTE_BYTES`.

## Goals / Non-Goals

**Goals.** Bound what one credential, one principal and one *tenant* can do per unit time and at one instant, on a single worker, with no I/O added to the common path and no unbounded state; make every refusal machine-parseable by the agent that receives it; make the pressure visible without letting the refusals become the load; and make the connection-pool arithmetic explicit rather than assumed.

**Non-Goals.**
- **Indexer fairness (#202).** Round-robin over `_active_user_ids()`, a per-tenant chunk budget and a max-chunks-per-note cap belong to that issue. This change bounds the *rate at which work is created*.
- **A new durable ceiling for OAuth grants or grandfathered keys.** Owner decision, round 2: they get velocity bounds only. Recorded as an accepted limitation below.
- **Expiring unused DCR clients.** `/register` grows `oauth_clients` unbounded. OAuth housekeeping, not a rate control. **Residual on #194.**
- Distributed or persisted limiter state, per-tenant billing, a Traefik `ratelimit` label, or changing the daily quota's UTC-day semantics.

## The layers

Outermost to innermost. L0a–L0c are pre-SQL; only L0c can wait.

| L | Control | Where | Scope key | Default (setting) | Refusal |
|---|---|---|---|---|---|
| 0a | Per-credential in-flight cap | `APIKeyMiddleware`, **pre-SQL** | SHA-256 fingerprint of the bearer token | 8 (`MCP_MAX_INFLIGHT_PER_CREDENTIAL`) | HTTP **503** + `Retry-After`, no query, no row |
| 0b | Identity-blind in-flight ceiling | `APIKeyMiddleware`, **pre-SQL** | process | 32 (`MCP_MAX_INFLIGHT_REQUESTS`) | HTTP **503** + `Retry-After` |
| 0c | Authentication concurrency | around the middleware's auth session | process | 3 (`MCP_MAX_CONCURRENT_AUTHENTICATIONS`), bounded wait | HTTP **503** + `Retry-After` |
| 1 | Failed-auth budget | before the credential lookup | address slot (salted fixed table) | 60 / 300 s (`MCP_AUTH_FAILURE_LIMIT`, `…WINDOW_SECONDS`; null ⇒ off) | HTTP **429** + `Retry-After` |
| 2 | General velocity bucket | `_tracked`, first gate | principal | 120/min, burst 30 | in-band `rate_limited` |
| 3 | Write velocity bucket | `_tracked`, write tools only | principal | 60/min, burst 15 | in-band `rate_limited` (scope `principal_write`) |
| 4 | Vault admission (existing #66) | `_tracked` | user | — | in-band `no_vault_assigned` |
| 5a | Unencodable-argument screen (#149) | `_tracked` | argument | — | in-band `argument_not_encodable` |
| 5b | Argument length cap | `_tracked`, beside 5a | argument | 8,192 (`MAX_SEARCH_QUERY_CHARS`) | in-band `argument_too_long` |
| 6 | Concurrency slots | `_tracked`, around the body | five scopes, below | ~5 s shared deadline | in-band `slot_timeout` |
| 7 | Daily quota (existing #162) | `_tracked`, **after** slots | api key | 5,000 for new keys | in-band `over_quota` |
| 8 | Provider input rejection | inside the body, on the provider's answer | argument | provider's own limit | in-band `argument_too_long` |

Slot levels at L6, acquired narrowest-first and released in reverse:

| Order | Slot | Scope key | Defaults (embedding / vector / write / other) |
|---|---|---|---|
| 1 | per-principal-per-class | `(principal, class)` | 1 / 1 / 1 / 2 |
| 2 | per-principal, all classes | principal | 3 (`MCP_MAX_CONCURRENT_PER_PRINCIPAL`) |
| 3 | per-tenant-per-class | `(user_id, class)` | 1 / 1 / 1 / 2 |
| 4 | per-class | class | 2 / 2 / 2 / 3 |
| 5 | global | process | 9 (`MCP_MAX_CONCURRENT_TOOL_CALLS`) |

Classes: `embedding` = `semantic_search`; `vector` = `find_related`; `write` = `create_note`, `edit_note`, `move_note`, `delete_note`, `set_frontmatter`, `write_file`, `delete_file`, `import_from_url`; **`other` = every remaining tool**. There is no unclassified tool — that is the round-2 fix, and D6 explains why it is structural rather than tidiness.

## Decisions

**D1. Three pre-SQL admission gates, because a limiter that starts after authentication is not one — and an identity-blind one is not fair.** Getting a principal costs a session checkout and an indexed SELECT, so a flood exhausts the pool on the lookups alone. Round 1 answered with one identity-blind ceiling; round 2 correctly showed that is not enough — tenant A can occupy all of it while tenant B gets 503, and the ceiling does not itself bound *concurrent authentication SQL*. So:

- **L0a, per-credential fingerprint.** The key is the SHA-256 the middleware **already computes** for the lookup (`hash_key(token)`), so the gate costs no extra hashing. It is used only as an in-memory table index: never logged (the log line keeps its existing truncated, differently-derived tag) and never persisted by the limiter. Cap 8 in flight per credential. It is deliberately coarser than the principal — the principal is not known yet — and it bounds *concurrency only*, never a rate, so an OAuth token rotating hourly bypasses nothing that matters. Its table is a fixed-size salted counter array (below), so its cardinality is bounded even though a caller can mint fingerprints freely by sending random tokens.
- **L0b, identity-blind ceiling**, 32. With L0a at 8, at least four distinct credentials can always be admitted and no single credential can hold more than a quarter of the ceiling. This is the memory/task bound, not the fairness one.
- **L0c, authentication concurrency**, 3 — a semaphore held *only* across the middleware's `async_session()` block. **This is the gate that makes "cannot drain the pool" true rather than asserted**, and neither L0a nor L0b can do it: 32 admitted requests could each be inside their auth session at the same instant, demanding 32 of 15 connections. Waiting on it happens before any SQL, is bounded, and times out into the same 503.

**The arithmetic, stated.** Pool 15. Authentication holds at most `MCP_MAX_CONCURRENT_AUTHENTICATIONS` = 3 connections, and it releases before the body starts. Tool bodies hold at most one connection each and are capped by the global slot ceiling, 9. Worst case 3 + 9 = **12 of 15**, leaving 3 for the indexer's pass, the control panel and `/health`. Every one of those three numbers is a setting, and boot validation enforces `MCP_MAX_CONCURRENT_AUTHENTICATIONS + MCP_MAX_CONCURRENT_TOOL_CALLS ≤ pool_size + max_overflow − MCP_RESERVED_CONNECTIONS` (3), so the relationship cannot rot when somebody raises one of them. 503 rather than 429 throughout L0: this is server capacity, not the caller's allowance.

**D2. Coalesced refusal rows have a flush lifecycle, and the key includes the scope.** Round 1 bounded the INSERT rate but specified no way for a pending count to reach the table, and per-refusal `UPDATE`s would have recreated the amplification it removed. The state is `(principal, tool, marker, scope) → (window_start, pending)`:

- First refusal for a key: write a row now with `suppressed = 0` and open a window.
- Refusal inside an open window: `pending += 1`, write nothing, issue no statement.
- Refusal after the window closed: write one row carrying `suppressed = pending`, reset the window.
- **Flush**: any key whose window has closed with `pending > 0` is written by the next `_log_usage` on any path, by the indexer's periodic tick, or at lifespan shutdown — whichever comes first.

`scope` is in the key because `principal_write` and `principal` are different facts about the same tool and merging them would attribute a write-bucket refusal to the general one. The visibility guarantee is therefore **"within one coalescing interval under traffic, and in all cases by the next indexer tick (5 minutes) or shutdown"** — not "immediately". Summing `1 + suppressed` over written rows equals the true refusal count once flushed, which is what the performance view reads.

**Accepted limitation:** a hard kill (SIGKILL, OOM) loses the open windows' pending counts — at most one interval's worth per active `(principal, tool, marker, scope)`. The alternative is a durable write per refusal, which is the amplification this exists to stop.

**Rejected:** "the gate refusal writes no row." A limiter invisible in the log is one nobody can diagnose or size, and `/admin/performance` is where an operator looks.

**D3. The principal is the grant, not the token, and not (client, user).** `oauth_tokens.grant_id` is NOT NULL, indexed, shared by every rotation of one `/authorize` approval, and already on the loaded row — so `("oauth", grant_id)` costs no query. Keying on `oauth_tokens.id` would hand a refreshing agent a fresh allowance hourly. Keying on `(client_id, user_id)` merges two grants that #64 made independently revocable, so revoking one would not free the other's allowance and the operator's stop would look like it had not worked. `("api_key", api_keys.id)` for the other branch.

**D4. No principal ⇒ no per-principal control, deliberately.** Sandbox mode short-circuits the middleware; a direct in-process caller never passes it. Both read `None` and are exempt, the same shape as `_quota_admission_error`'s "a limit with no key is exempt rather than a crash". Nothing untrusted reaches that path. Layers 0 and 1 are not keyed on the principal and apply regardless.

**D5. Gate order, and the two rules that fix it.** L2 → L3 → L4 → L5 → **L6 (slots)** → **L7 (quota)** → body.
- *A token is not a slot.* A rate token refills, so consuming one on a call a later gate refuses is correct — the refusal itself costs work. The buckets come first so the flood we most want to shed is shed by arithmetic.
- *Nothing durable is consumed by a call that does not run.* Round 1 broke this: a key that passed quota admission and then timed out on a slot had spent a daily slot unrecoverably. Slots are acquired **before** quota admission and released in the `finally` covering both the quota gate and the body. #162's own reasoning is preserved; the slot wait simply joins the set of things that must resolve before the counter moves.

Because L2/L3 sit *above* the vault gate, a call can now be refused before its vault root is resolved, which the `mcp-request-routing` requirement did not contemplate. That requirement is **modified in this change** to say "before its body, unless the call was already refused by an earlier gate" — the substance (no tool body ever runs without a resolved root; the gate lives in the shared decorator; no exemptions) is unchanged, and a rate-refused call runs no body and reveals nothing about the vault.

**D6. Every tool has a class, and that is what makes the acquisition order safe.** Round 2 found the real hole in round 1's ordering argument: with unclassified tools taking only the global semaphore, a `semantic_search` holding an embedding slot could block on a global ceiling filled by ordinary reads — head-of-line blocking of the scarcest class by the cheapest traffic. Adding an **`other` class** closes it structurally rather than by reordering:

> With `global ≥ embedding + vector + write + other`, at most `Σ` calls can hold a class slot at once, so **a call that already holds its class slot is guaranteed the global slot**. The global semaphore can never be what a classed call queues behind.

That turns the ordering question into a non-question: narrowest-first stays, and its justification is now this invariant rather than a heuristic about who is hurt while waiting. Boot validation enforces the sum over **all four** classes — round 1 enforced it over three, which was exactly the hole. Codex's alternative (acquire global first) was **rejected**: it makes a task hold a global slot while waiting for a class slot, so a saturated class parks tasks that block *every* tenant, which is strictly worse than the problem it fixes.

**D7. Per-tenant caps on all three expensive classes.** One user holding an API key *and* an OAuth grant is two principals, so per-principal caps alone let one user occupy a whole class. Round 1 fixed that for `embedding` only; `vector` and `write` had the same hole. Every class now has a per-tenant cap strictly below its ceiling (1 of 2, 1 of 2, 1 of 2, 2 of 3), so a second tenant always has a slot. Single-user and ownerless traffic (`user_id IS NULL`) is one tenant, consistent with the owner predicate being total (#127). A per-tenant *reservation* remains **rejected** as unimplementable: with two slots and N tenants there is nothing to reserve, and a reservation that cannot guarantee a slot breaks under exactly the load it exists for.

**D8. Velocity is bounded for everyone; totality is bounded only for limited API keys.** The buckets are **velocity** bounds: they stop a hot loop and bound the rate at which work is created. They are not blast-radius bounds — 60 deletes/minute reaches every note in this vault in about three quarters of an hour. Totality is bounded by the daily quota, and **the daily quota does not apply to OAuth grants (exempt by construction) or to keys created before this change (grandfathered NULL)**.

> **Accepted limitation, owner-approved in round 2.** OAuth principals and pre-existing NULL-limit API keys have **velocity bounds only** — no durable ceiling on total destructive work in a day. Closing it would mean either a quota for OAuth (rejected in #162: panel OAuth is the operator, and an operator locked out by their own ceiling cannot raise it) or a backfill onto existing keys (rejected here: grandfathering is the whole point of D19). The operator lever is to set a limit on the five live keys after deploy (task 5.8), and the mitigations that remain are `permanent=False` trash recovery and the write bucket's velocity bound.

Rate limiting also never prevents a *single* destructive write, which is what the `vault-tools` disciplines are for. The 60/15 numbers are an owner decision (Open Questions).

**D9. One caller-visible refusal shape, composed with every tool output type.** `params.error` is an operator's field, invisible to the caller, so round 1's "typed, actionable refusal" was prose in a string for every `str`-returning tool. A new `src/services/refusals.py` (importing nothing from the app, so `tools.py`, `quotas.py`, `embeddings.py` and `rate_limits.py` can all use it without a cycle) defines a `Refusal` carrying `code`, `scope`, `limit`, `limit_unit`, `retry_after_seconds`, and one renderer appending a final line to the existing prose:

```
MCP-REFUSAL {"code":"slot_timeout","scope":"slot:embedding","limit":2,"limit_unit":"concurrent_calls","retry_after_seconds":5}
```

`str` tools get prose + that line; structured tools get the identical complete text in their declared error field through `refusal_result`, so no output-schema validation can fail and both carry the same fields. The sentinel is line-initial and the JSON is one line so it survives being quoted into a transcript. `retry_after_seconds` is a number ≥ 1 wherever retrying can help and **absent** for `no_vault_assigned` and `argument_not_encodable`, where a number would invite a loop that cannot end. The three existing pre-body refusals adopt the line **additively** — prose unchanged, so every `in`/`startswith` assertion still holds.

**D10. Nothing blocks while holding the loop, and nothing adds I/O.** A bucket update is synchronous — read `(tokens, last_refill)`, compute, write back — with no `await` between read and write, so on a single-threaded loop it is atomic by construction and needs no lock. The L0 counters are the same shape. `asyncio.Semaphore` construction does not bind a loop in 3.12, so class, global and authentication semaphores are module-level. The admitted common path is a few table and dict lookups, some floats, and semaphore acquisitions that succeed immediately: no statement, no session checkout. Pinned by a statement-counting test.

**D11. One deadline across all five slot acquisitions, not five timeouts.** `deadline = monotonic() + MCP_SLOT_WAIT_SECONDS`; each `wait_for` gets the remaining budget. Five independent timeouts would make the worst case 25 s. The elapsed wait is *approximately* the budget — the deadline is checked between acquisitions and the loop may schedule late — so the spec says approximately and the test asserts a tolerance.

**Provider worst case, and why the budget is not sized to it.** An `embedding` slot can be held for a full provider call: 30 s (Ollama) or **up to 60 s × 3 attempts ≈ 180 s (OpenAI, with its documented retry policy)**. With the class at 2, a third `semantic_search` therefore waits 5 s and is refused, and its `retry_after_seconds` of 5 **understates** the true recovery time by up to two orders of magnitude in the worst case. That is deliberate: sizing the wait to the provider's worst case *is* the unbounded-queue failure this design rejects, and under a slow provider the honest behaviour is to degrade `semantic_search` into fast typed refusals rather than into 180-second latency for everyone.

> **Accepted limitation.** `retry_after_seconds` on a `slot_timeout` is a floor and a hint, not a promise. The operator levers are raising the class ceiling, lowering the provider timeout, or switching provider; the architecture note records the per-provider worst case beside the numbers so the next person sizing this has the arithmetic.

**D12. Limiter state is bounded by construction, and each registry says how.**
- **Fingerprints and addresses** are *unauthenticated* and unbounded in cardinality — a caller mints a new one per request for free — so eviction is a losing game. Both use a **fixed-size table** of counters (`MCP_INFLIGHT_TABLE_SIZE`, `MCP_AUTH_FAILURE_TABLE_SIZE`, 4,096) indexed by a **per-process randomly salted** hash. Memory is O(size), there is nothing to evict, collisions only make a control stricter, and the random per-process salt means nobody can choose to collide with a victim.
- **Principals and tenants** are authenticated, so cardinality is bounded by the credentials that exist. A dict with a hard cap (`MCP_LIMITER_MAX_TRACKED_PRINCIPALS`, 10,000) and TTL eviction swept amortised on insert (bounded work per admission; no background task). An entry is evictable only when **full and idle**: a depleted bucket must not be evicted (a fresh entry starts full, so eviction would grant free capacity) and an entry with in-flight holders or waiters must not be evicted (its counts are live). Past the cap, further principals share **one overflow bucket**.

> **Accepted limitation.** The overflow bucket is shared, so past the cap an overflowing principal's traffic can cause an unrelated overflowing principal to be refused. It is the bounded-memory trade-off and it fires **only** beyond 10,000 tracked principals — a state that requires more than ten thousand live credentials, which this deployment cannot reach without something already having gone very wrong. Fail-open was rejected (it lets the flood succeed) and fail-closed was rejected (it turns a bookkeeping cap into an outage for a legitimate credential).

**D13. Limiter state is in-process and not persisted.** A restart begins with every bucket full, every slot free, every counter zero. Sound because there is exactly **one** uvicorn worker; because instantaneous pressure is meaningless to persist across a process that no longer exists; because a restart is an operator action or a crash, not something a caller can induce; and because the durable ceiling already exists as `quota_counters`. **The worker count is part of the contract** — `--workers N` multiplies every in-process control by N. A comment goes at the `CMD` and the architecture note says it in prose.

**D14. Boot validation, and one representation for "off".** A `model_validator` enforces: `global ≥ embedding + vector + write + other`; `authentications + global ≤ pool_size + max_overflow − reserved`; `per_principal ≤ global`; for **each** class, `per_principal_class ≤ per_tenant_class ≤ class ceiling`; `per_credential_inflight ≤ inflight_ceiling`; `inflight_ceiling ≥ global`; each bucket's rate and burst both set or both null; `default_daily_request_limit` inside 1..1,000,000; and **zero rejected everywhere** (#162's reason: a control that refuses every call reads as an outage, not a setting). Null is the only disable, following `daily_request_limit`'s NULL-is-unlimited idiom — and because `.env` has no JSON null, one central `BeforeValidator` maps an empty value, `null` or `none` (stripped, case-insensitive) to `None` for every nullable limiter setting, tested **through a real env file**.

**D15. The query cap is a declarative screen in `_tracked`; the provider's own limit is translated in the body.** A character cap is necessary and **not sufficient**: 8,192 characters of high-tokenization Unicode can still exceed a provider's token limit, so the cap alone cannot promise the provider will accept the input. Two halves, on opposite sides of the body/no-body line:

- **L5b, pre-body.** `arg_char_caps={"query": MAX_SEARCH_QUERY_CHARS}` beside the existing `_first_unencodable_argument` screen — a generic argument screen already lives there, so this generalises. It refuses before the provider call, the `tsquery` parse and any search or quota statement. It is a pre-body marker and joins `pre_body_refusal_sql()`.
- **L8, post-body.** When the provider answers with its own input-limit error, the tool translates it into the same caller-facing `code` — `argument_too_long`, with the provider's reason in the scope/prose — so the agent sees one actionable failure mode instead of a raw provider error. **The usage marker is different**: `provider_input_rejected`, classified **post-body** and deliberately *not* in the pre-body predicate, because the body ran, resolved a vault and made a network call, and enumerating it as a refusal would drop a real round trip out of the percentiles. This is the classification rule from `docs/architecture/usage-attribution.md` applied exactly: the caller-facing `code` and the operator-facing marker answer different questions and may differ.

Note what the cap is *not* for: #194's verification withdrew the cost-amplification claim (Ollama truncates, OpenAI rejects over 8,192 tokens). The reasons are an unbounded argument interpolated into `f"No results for '{query}'"` (the #149 discipline), `tsquery` parsing on the single event loop (the #204 class), and — before L8 — an OpenAI deployment turning an over-long query into a raw provider error where the contract promises a typed refusal.

**D16. The failed-auth budget lives in the app; Traefik is the wrong instrument.** A proxy cannot condition on the response status, so a `ratelimit` label on `obsidian-mcp-api-rtr` would throttle authenticated agents to bound unauthenticated probing — and `/transfer/*`, `/health` and `/.well-known` share that router. The host's Traefik static configuration is outside this repo, so a control expressed there would not be reproducible from the tree. `docker-compose.yml` labels unchanged, with a comment recording the decision. The address comes from `ProxyHeadersMiddleware` (which wraps the mount) and never from a header read directly; **every** 401 branch increments, because a prober picks the cheapest one; a request with no client address is charged to one shared reserved slot rather than exempted.

**D17. Threshold and `Retry-After` arithmetic, in one helper.** Refuse when the count already recorded in the window is `≥ MCP_AUTH_FAILURE_LIMIT`, so with 60 the 61st failure is the first refused, and a refused request does not increment (it never reached authentication). `Retry-After` is the whole seconds remaining in the current window, minimum 1. One helper, one test parameterised over every 401 branch.

**D18. 429 for the failure budget, 503 for capacity.** An over-budget response is distinguishable, which tells a prober the limiter exists — a fair trade, since the limiter is documented and a legitimate client with an expired token deserves an answer it can act on.

**D19. The default daily limit is applied in application code, never as a column default.** A `server_default` would be a schema change, would apply to every future insert path, and still could not express "grandfather the rows that exist". The JSON API distinguishes omitted from explicit-null by `model_fields_set` — `null` is documented as unlimited and must stay so. The **panel** materialises the default only as the create form's pre-filled value: a blank submitted field is an explicit unlimited with **no POST-side substitution**, so the operator's last view of the field is what the key receives, and `keys_page` passes the default to the template.

**Why 5,000.** ~1,600 tool calls per 30 days across all credentials, so 5,000/day is two orders of magnitude of headroom and cannot interrupt a real session, while a runaway stops the same day. At 120/min it takes ≥ 42 minutes to spend. It binds only keys created after this ships.

**D20. Quota `retry_after_seconds` comes from the admission's own clock read.** `Admission` today carries `day` and `count`, and `reset_at` is derived from `day`; computing a retry interval would need a second `datetime.now()` — precisely the double-clock-read bug the class exists to prevent (#162). So `admit()` records the instant it already read as `decided_at` on the `Admission` and derives `retry_after_seconds = ceil((reset_at − decided_at).total_seconds())`, minimum 1, with nothing downstream re-reading the clock. A test crosses UTC midnight between the decision and the message and asserts the interval is small, not ~48 hours.

**D21. Four markers, split by what an operator asks.** `rate_limited` (either bucket, `scope` distinguishes), `slot_timeout` (a slot wait), `argument_too_long` (the pre-body cap) — all **pre-body**, all in `pre_body_refusal_sql()` — and `provider_input_rejected` (L8), **post-body** and deliberately outside it. "Is one agent too fast?" versus "is the server at capacity?" versus "did a caller send something the provider refused?" are answerable from the marker alone. `rate_limit_scope` is a string no reader casts; `suppressed` is an integer read with a guarded cast.

## Rejected findings

- **Round 1 — "…or the gate refusal writes no row."** Coalescing with a flush lifecycle instead (D2). A limiter invisible in the log cannot be diagnosed or sized.
- **Round 1 — a per-tenant *reservation* for the embedding class.** Unimplementable at two slots and N tenants (D7); the per-tenant **cap** is folded in, now for all three expensive classes.
- **Round 1 — the write bucket as a blast-radius fix.** Accepted as a control, rejected as a solution: D8 now states plainly that no bucket bounds totality and records the OAuth/grandfathered residual as owner-accepted.
- **Round 2 — "acquire global FIRST, then narrow inward."** Rejected in favour of the `other` class (D6). Global-first makes a task hold the global slot while waiting for a class slot, so one saturated class parks tasks that block every tenant — strictly worse than the head-of-line blocking it fixes. The reservation invariant removes the problem instead of relocating it, and the boot check enforces it.

## Alternatives rejected

- **slowapi on the `/mcp` mount.** Keyed on the remote address (wrong scope) and decorator-shaped for routes, not an ASGI mount; it produces an HTTP 429 where the contract needs a parseable in-band tool result.
- **A Traefik `ratelimit` middleware as the primary control.** D16.
- **Making the daily quota the burst control by lowering it.** A per-day counter cannot bound concurrency, and a low daily number would exhaust a legitimate agent's day in one healthy burst.
- **Persisting bucket state in PostgreSQL.** A statement on the hottest path to bound something meaningless once the process is gone, with the durable ceiling already one layer up.
- **An unbounded queue instead of a bounded wait.** Queuing turns a rate problem into a latency problem: the caller's connection is held, the pool behind it, and the agent gets no signal to back off.
- **Per-tool-name semaphores.** More knobs, no more protection; four classes name the scarce resources exactly.
- **Refunding a slot on a failed body, or a token on a refusal.** #162's reasoning both ways: a tool that always fails would be free.
- **Fail-open or fail-closed on principal-registry overflow.** D12.

## Risks / Trade-offs

- **L0b can refuse a well-behaved tenant** when the process is genuinely saturated; L0a is what stops one credential causing it. 32 in-flight is above any legitimate concurrent load here, and a fast 503 with `Retry-After` is strictly better than a 500 after `pool_timeout`.
- **L0c serialises authentication to 3.** Under a burst, legitimate requests queue briefly before their credential lookup. Bounded, times out into 503, and it is the price of a provable pool bound.
- **Shared egress IPs and slot collisions.** claude.ai egresses from shared addresses, and the hashed tables can merge two addresses (or two credentials) into one slot. Both make a control stricter, never weaker. Mitigated by generous defaults, by `MCP_AUTH_FAILURE_LIMIT` = null, and by one WARNING naming the address the first time a slot engages. **Declared.**
- **Two tenants can still make a third wait**, and under a slow provider that wait ends in a refusal whose `retry_after_seconds` understates recovery (D11). Honest outcome for a serialised resource.
- **A shared key is a shared bucket.** Same answer as #162: an agent that deserves isolation deserves its own credential.
- **Coalescing means the log is a summary.** The row count is no longer the refusal count; `1 + suppressed` is, and it is exact only after a flush. Every reader moves in one slice.
- **Lowering the global ceiling from 12 to 9** (round 2, for the pool arithmetic) reduces peak tool concurrency. With four classes summing to 9 and per-tenant caps below each, this bounds throughput, not fairness — flagged in Open Questions.
- **The defaults are guesses against a small sample** (~1,600 calls/30 days). Every one is a setting; read `/admin/performance` for a week before treating any as settled.
- **`--workers 1` becomes load-bearing.** D13.

## Migration Plan

None. No schema change, no backfill, no new dependency. `make db-check` still runs after deploy. Rollback is a redeploy of the previous image — settings are read at boot and nothing is persisted. Every control can be disabled in `.env`: null the nullable settings, raise the concurrency numbers, null `DEFAULT_DAILY_REQUEST_LIMIT`.

## Open Questions

Every default is a setting; these want the owner's word. None blocks implementation.

1. **General bucket — 120/min, burst 30?** Recommended.
2. **Write bucket — 60/min, burst 15?** Owner decision recorded. 60/min still reaches every note here in ~45 minutes, so it is a velocity bound (D8); 20/min would make bulk destruction obvious well before it completed, at the cost of slowing a legitimate bulk import.
3. **`DEFAULT_DAILY_REQUEST_LIMIT` — 5,000?** Recommended; 2,000 is still ~40× observed usage and halves a runaway's reach.
4. **The pool budget — authentications 3 + global 9, reserving 3 of 15?** Recommended, and it is the constraint that set the global ceiling. Raising either requires raising the pool.
5. **`MCP_MAX_INFLIGHT_REQUESTS` 32 and `MCP_MAX_INFLIGHT_PER_CREDENTIAL` 8?** Recommended: four distinct credentials always admitted, no credential above a quarter of the ceiling.
6. **`MCP_AUTH_FAILURE_LIMIT` — 60 per 5 minutes?** Recommended, with the shared-egress risk as the reason it is not lower.
7. **Should existing production keys be given a limit at deploy time?** #194 recommends it, and D8's accepted limitation is the reason it matters more after round 2: those five keys have velocity bounds only until an operator sets one. Post-deploy task 5.8.
