# Rate limits: principals, buckets, and the shape of a refusal

> Deep rationale extracted from `CLAUDE.md`. Read before touching
> `src/services/rate_limits.py`, `src/services/refusals.py`, the failed-auth
> budget in `src/mcp_server/auth.py`, the gate order in `_tracked`
> (`src/mcp_server/tools.py`), or any `MCP_RATE_LIMIT_*` /
> `MCP_AUTH_FAILURE_*` / `DEFAULT_DAILY_REQUEST_LIMIT` setting — and, for the
> concurrency half, `src/services/concurrency.py`, `pool_budget.py`,
> `concurrency_counters.py`, `concurrency_readiness.py`, the pool subclass in
> `src/database.py` and every `MCP_CONCURRENCY_*` setting. The
> operator-facing marker register lives in
> [usage attribution](usage-attribution.md); the daily quota's own history is
> in there too.

Before this, `/mcp` was the only surface on this server with no rate control of
any kind. `app.mount("/mcp", APIKeyMiddleware(mcp_handler))` sits outside the
slowapi limiter that decorates every other public route, the container carries
no Traefik `ratelimit` middleware, and the one per-credential control that
existed — `api_keys.daily_request_limit` (#162) — was opt-in, was set on **0 of
5** active production keys, counted per UTC *day* (so it bounded nothing
instantaneous), and was exempt for OAuth by construction, which was roughly
half of production tool calls.

The consumer of this server is an **agent**, and a retry-storming or
prompt-injected agent is an ordinary input for this product. A single
credential could loop on `delete_note` or `read_note` at wire speed with
nothing to slow it and nothing to tell it to stop.

## The control table

| L | Control | Where | Scope key | Default (setting) | Refusal | Marker |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | Failed-auth budget | `APIKeyMiddleware`, before the credential lookup | address slot (salted fixed table) | 60 / 300 s (`MCP_AUTH_FAILURE_LIMIT`, `MCP_AUTH_FAILURE_WINDOW_SECONDS`; null ⇒ off) | HTTP **429** + `Retry-After` — **transport, not a tool result** | none; one WARNING per slot per window |
| 1a | Request occupancy (#261, #188) | `APIKeyMiddleware`, before auth session through ASGI completion | global / presented-bearer fingerprint | 64 / 20, waiters 64 / 16, sharing one 2 s transport deadline with 1b; mode default **shadow** | enforce: transport 429 after the deadline or on waiter overflow; queue: admitted with an overrun; shadow: observation only. A disconnected waiter is released at once | `mcp_concurrency_pressure` event; request-level `concurrency_counters`; authenticated observations can accompany tool rows |
| 1b | Auth-session occupancy (#261, #188) | around the middleware's own DB session only | global | 2, waiters 32, same deadline as 1a | enforce: transport 429 before opening a session; queue/shadow as 1a | same transport event and counters |
| 2 | General velocity bucket | `_tracked`, first gate | principal | 120/min, burst 30 (`MCP_RATE_LIMIT_PER_MINUTE`, `MCP_RATE_LIMIT_BURST`) | in-band, sentinel line | `rate_limited`, scope `principal` — **coalesced** |
| 3 | Write velocity bucket | `_tracked` (write tools) **and** `PUT /transfer/upload` | principal | 60/min, burst 15 (`MCP_WRITE_RATE_LIMIT_PER_MINUTE`, `MCP_WRITE_RATE_LIMIT_BURST`) | in-band on a tool; **429** on the transfer route | `rate_limited`, scope `principal_write` — **coalesced** |
| 4 | Vault admission (#66) | `_tracked` | user | — | in-band | `no_vault_assigned` (and the three vault-root quarantine markers, #199) |
| 5a | Unencodable-argument screen (#149) | `_tracked` | argument | — | in-band | `argument_not_encodable` |
| 5b | Query length cap | `_tracked`, beside 5a | argument | 8,192 (`MAX_SEARCH_QUERY_CHARS`) | in-band | `argument_too_long` — **own row** |
| 5c | Atomic tool slots (#261, #188) | `_tracked`, after argument screens and before quota | class / principal / tenant / global | classes embedding 1 / vector 1 / write 1 / scan 2 / light 4; principal 3 / tenant 4 / global 6; 5 s wait; mode default **shadow** | enforce: in-band sentinel after the wait; queue: admitted with an overrun, call runs; shadow: call runs | `slot_timeout`, coalesced, only for actual enforcement refusals; `concurrency_shadow` / `concurrency_queue` annotate otherwise |
| 6 | Daily quota (#162) | `_tracked`, last pre-body gate | api key | 5,000 for **new** keys (`DEFAULT_DAILY_REQUEST_LIMIT`) | in-band | `over_quota` |
| 7 | Provider input rejection | inside the body, on the provider's answer | argument | the provider's own limit | in-band, `argument_too_long` **code** | `provider_input_rejected` — **post-body** |

The eight write-class tools (L3) are `create_note`, `edit_note`, `move_note`,
`delete_note`, `set_frontmatter`, `write_file`, `delete_file` and
`import_from_url` — every `_tracked` tool that changes vault bytes and
therefore amplifies into the next indexer pass — **plus `PUT /transfer/upload`**,
which changes vault bytes without being a tool call at all.

## The principal is the grant, not the token, and not (client, user)

Every authenticated `/mcp` request binds `current_principal` (a ContextVar in
`src/auth/session.py`, beside `current_actor` and `current_vault_root`) from
the credential row `APIKeyMiddleware` has **already loaded**, under the house
rule *the row is in hand, do not add a round trip*. It is `("api_key",
api_keys.id)` or `("oauth", oauth_tokens.grant_id)`, and it is `reset()` in the
same `finally` as the other request-scoped auth variables.

**The OAuth half is the grant.** `oauth_tokens.grant_id` is NOT NULL and
indexed since migration 014 (#64), shared by every rotation of one `/authorize`
approval and already on the loaded row, so keying on it costs no query.

- Keying on `oauth_tokens.id` would hand a refreshing agent a **fresh
  allowance hourly** — the limiter would be defeated by the ordinary operation
  of the protocol.
- Keying on `(client_id, user_id)` would merge two grants that #64 made
  independently revocable. Revoking one would not free the other's allowance,
  and the operator's stop would look like it had not worked.

**No principal ⇒ no per-principal control, deliberately.** Sandbox mode
short-circuits the middleware and a direct in-process caller never passes it;
both read `None` and are exempt from both buckets, the same shape as
`_quota_admission_error`'s "a limit with no key is exempt rather than a crash".
Nothing untrusted reaches that path — untrusted traffic arrives through the
middleware, which binds a principal or answers 401/429. The failed-auth budget
(L1) is not keyed on the principal and applies regardless.

## Gate order, and the invariant it preserves

L2 → L3 → L4 → L5a/b → L5c (slots) → L6 (quota) → body → telemetry.

- **A token is not a quota slot.** A rate token refills, so consuming one on a
  call a later gate refuses is correct — the refusal itself costs work. The
  general bucket's token is spent even when the write bucket then refuses. The
  buckets come first because they are the only gate that is *pure arithmetic*:
  one dictionary lookup and some floats, before anything touches a cache, an
  argument tree or the database. The flood we most want to shed is shed
  cheapest.
- **Nothing durable is consumed by a call that does not run.** The daily quota
  keeps its #162 position as the **last** pre-body gate, so a call refused for
  having no vault, for an unencodable argument, for an over-long query — or now
  for exceeding its rate — consumes no daily slot. Its `quota_counters` row is
  untouched. The atomic slot gate (#261) also precedes quota, and an admitted
  lease stays held through the response-neutral telemetry tail, releasing in
  `finally` on success, quota refusal, exception or cancellation. Waiting holds
  neither partial permits nor a DB connection. "Durable" means the daily
  quota: a call refused for concurrency has already spent its rate tokens, and
  they stay spent (#188 D9, below).

**The order is unchanged by the request-path commits (performance-2026-09,
#279), but the quota now commits asynchronously (L1, an owner decision taken
by default).** `quotas.admit` issues `SET LOCAL synchronous_commit = off` before
`ADMISSION_SQL`, and again before the prune in its own next transaction. An
asynchronously committed increment is visible to every other session at commit,
so the conditional increment under the row lock still admits exactly `limit`
calls per key per UTC day under any concurrency
(`tests/integration/test_perf_async_commit_pg.py` repeats the N-of-more-than-N
case). What changes is durability across a PostgreSQL server or host crash:
increments committed in the preceding ~600 ms can be lost. That undercounts the
key, in the caller's favour. It can never overcount, and it can never refuse a
call the synchronous form would have admitted. The admission is still fail
closed and still emits `quota_admission_failed`, and a rate-refused call still
issues no quota statement. Writes that grant or revoke (OAuth code exchange,
refresh rotation, revocation, transfer tokens, users) stay synchronous; the
rule is that only bookkeeping whose loss undercounts may commit asynchronously.

Because L2/L3 sit *above* the vault gate, a call can be refused before its
vault root is resolved, which the `mcp-request-routing` requirement did not
originally contemplate. That requirement now reads "before its body, unless the
call was already refused by an earlier gate". The substance is unchanged — no
tool body ever runs without a resolved root, the gate lives in the shared
decorator, there are no exemptions — and a rate-refused call runs no body and
reveals nothing about the vault: its content depends only on the caller's own
request rate.

## Two buckets, because velocity and destruction are different questions

The general bucket is a **velocity** bound: it stops a hot loop and bounds the
rate at which any work is created. It is *not* a blast-radius bound — 120
deletes a minute empties a 2,577-note vault in about twenty minutes. The write
bucket halves that. **Neither bucket bounds totality**; only the daily quota
does, and only for the credentials it reaches.

> **Accepted limitation, owner-approved.** OAuth principals and pre-existing
> NULL-limit API keys have **velocity bounds only** — no durable ceiling on
> total destructive work in a day. Closing it would mean either a quota for
> OAuth (rejected in #162: panel OAuth is the operator, and an operator locked
> out by their own ceiling cannot raise it) or a backfill onto existing keys
> (rejected here: grandfathering is the whole point of the new-key default
> below). The operator lever is to set a limit on the live keys; the mitigations
> that remain are `permanent=False` trash recovery and the write bucket.

Rate limiting also never prevents a *single* destructive write, which is what
the [vault tools](vault-tools.md) disciplines are for.

## The write bucket follows the bytes, not the decorator

`PUT /transfer/upload` publishes into the vault by redeeming a capability and
**never passes through `_tracked`**. Bounding only the eight tools would
therefore leave the write rate escapable: mint capabilities at the general
rate, then redeem them without limit.

Redemption consumes the write bucket of the principal **that minted the
token**, which costs nothing to obtain — the `transfer_tokens` row already
carries `key_id` / `oauth_token_id` (migration 017, with
`ck_transfer_tokens_one_credential` guaranteeing at most one), and the identity
resolution already loads the minting `OAuthToken` row, whose `grant_id` is NOT
NULL. So `("api_key", key_id)` or `("oauth", grant_id)` is derivable at
redemption with **no additional query and no schema change**. A row naming
neither credential (single-user, sandbox) resolves to no principal and is
exempt, matching `_tracked`'s rule.

The token is taken inside the phase-one session window #208 established:
**after** the re-validation ladder — an unusable token is a 404 and must not
also cost its minter a token — and **before** any request body is read or any
byte is staged.

**The refusal is a 429 with `Retry-After` and it releases the claim rather than
consuming it**, deliberately mirroring the 503 queue-timeout path #208
established and for the same reason: *a capability the server declined to serve
right now is a promise still outstanding*, so it must remain redeemable once
the bucket refills. It gets its own body rather than the uniform 404 — the
token is fine and stays claimable, and telling a legitimate redeemer their link
had died would make them mint another.

**The 429 is conditional on the release actually landing.** `claim_upload`
commits before this gate, so a `release_claim` that fails leaves the token
**claimed until its TTL** — and `Retry-After` would then be a lie the caller
acts on: it says "this link works again in N seconds" about a link that will
answer 404 for the next several minutes, so an obedient agent retries on
schedule, is refused, and has no way to tell the refusal it was given from the
one it now gets. When the claim cannot be restored the route falls back to this
route's ordinary non-retryable answer — which is what a retry would in fact
receive — and `transfer_claim_release_failed` records the cause class-only. A
retryable refusal is a promise, and only a confirmed release can support it.

The full rule, beside the #208 phase-one session discipline it lives inside,
is in [file transfer](file-transfer.md).

Minting is **not** charged: `request_upload`, `request_download` and
`check_upload` touch capability rows only, and billing both ends would count
one write twice. `import_from_url` keeps consuming the bucket at its tool call,
as an ordinary write tool, and is not charged again at redemption. Like L1's
429, the redemption refusal is a transport refusal outside the in-band refusal
contract below — there is no tool call to answer.

## One caller-visible refusal shape — for refusals raised inside `_tracked`

`usage_logs.params["error"]` is an **operator's** field, invisible to the
caller, so "typed, actionable refusal" has to mean something the agent on the
other end can parse. `src/services/refusals.py` — importing nothing from the
application, so `tools.py`, `quotas.py`, `embeddings.py` and `rate_limits.py`
can all use it without a cycle — defines a `Refusal` carrying `code`, `scope`,
`limit`, `limit_unit` and `retry_after_seconds`, the closed `code` set, and one
renderer appending a single final line to the existing prose.

What an agent actually receives when the general bucket refuses it:

```
Error: this credential exceeded its general rate limit of 120 calls per minute, so the call was refused before it ran. Nothing was read, written, or counted against the daily quota. Retry in 3 seconds, or slow the calling loop down.
MCP-REFUSAL {"code":"rate_limited","scope":"principal","limit":120,"limit_unit":"calls_per_minute","retry_after_seconds":3}
```

- The sentinel `MCP-REFUSAL` is **line-initial** and the JSON is **one line**,
  so the pair survives being quoted into a transcript.
- A `str` tool gets prose plus that line; a **structured** tool gets the
  identical complete text in its declared error field through `refusal_result`,
  so both kinds expose the same fields and no output-schema validation can
  fail.
- `retry_after_seconds` is a number ≥ 1 wherever retrying can help, and is
  **absent** for `no_vault_assigned` and `argument_not_encodable`, where
  quoting a number would tell an obedient agent to sleep and retry forever.
  `Refusal` rejects one on those codes at construction, so a bad refusal fails
  where it is built rather than reaching an agent as a line it cannot act on.
- The three pre-existing pre-body refusals adopt the line **additively** —
  prose byte-unchanged, so every `in` / `startswith` assertion still holds.
- `render` is **idempotent**. A refusal message is built at one altitude and
  rendered at another (`quotas.quota_refusal_message` composes the over-quota
  prose; the decorator decides the interval), and the two must not be able to
  stack two sentinel lines by both doing their job.

> **Accepted limitation.** The contract covers refusals raised **inside
> `_tracked`**, where a tool call exists to answer. L1's 429 is a *transport*
> refusal to a request that never authenticated: there is no tool call, no
> principal and no `usage_logs` row, so it carries `Retry-After` and
> `WWW-Authenticate` headers instead of a sentinel line. The transfer
> redemption 429 is outside it for the same reason. An agent that only parses
> tool results sees an HTTP error there; that is the honest shape of an
> unauthenticated rejection, and pretending otherwise would mean answering an
> unauthenticated request with a fabricated tool result.

## The query cap is pre-body; the provider's own limit is translated in the body

A character cap is necessary and **not sufficient**: 8,192 characters of a
densely-tokenizing script can still exceed a provider's token limit, so the cap
alone cannot promise the provider will accept the input. Two halves, on
opposite sides of the body/no-body line.

**L5b, pre-body.** `arg_char_caps={"query": MAX_SEARCH_QUERY_CHARS}` declared
on `_tracked` beside the existing `_first_unencodable_argument` screen — a
generic argument screen already lived there, so this generalises to any future
argument. It refuses before the provider call, before the `tsquery` parse,
before any search or quota statement, and before the value is interpolated into
a server-authored string. The refusal names the argument, its length, the limit
and the setting, and **never echoes the argument**: quoting it back would break
the #149 discipline in the very screen that enforces it, and 8 KB quoted into a
tool result is 8 KB of the caller's context spent repeating what it just sent.

**L7, post-body.** When the provider answers with its own input-limit error,
the providers raise `refusals.ProviderInputTooLarge` — declared in
`refusals.py`, not in `embeddings.py`, so the code that raises it and the code
that handles it share a dependency-free contract — and `semantic_search`
translates it into the same caller-facing `argument_too_long` code carrying the
provider's stated reason. The agent sees **one** actionable failure mode for
"the query was too large", whichever limit actually applied.

**Its sentinel line carries `limit: null` and `limit_unit: null`.** The limit
that fired is the provider's, it is a *token* limit, and this server does not
know its value — the provider states it in prose or not at all. Quoting
`MAX_SEARCH_QUERY_CHARS` there (the first shape) told a parsing agent that an
8,192-**character** bound had been exceeded by a query that was under it: a
machine-readable falsehood, and one an obedient agent acts on by trimming to
that number, retrying, and being refused identically forever. The prose still
explains why the character cap did not catch it; the payload asserts nothing it
cannot support.

**The usage marker is different on purpose.** `provider_input_rejected` is
classified **post-body** and is deliberately *not* in
`pre_body_refusal_sql()`, because the body ran, resolved a vault and made a
network call. Enumerating it would drop the most expensive class of call in the
server out of the latency percentiles. This is the classification rule applied
exactly: the caller-facing **code** and the operator-facing **marker** answer
different questions and are permitted to differ.

Note what the cap is *not* for. #194's own verification **withdrew** the
cost-amplification claim: Ollama truncates to the model context and OpenAI
rejects over its token limit, so an over-long query was never a way to spend
the operator's money. The reasons are an unbounded argument interpolated into a
server-authored string (the #149 discipline), `tsquery` parsing on the single
event loop (the #204 class), and — before L7 — an OpenAI deployment turning an
over-long query into a raw provider error where the contract promises a typed
refusal.

## Refusal recording is bounded by coalescing, and the coalescer owns a complete row

A refusal is cheap to produce, so before this the cheapest thing an agent could
do was **generate database writes** — and unlike an admitted call, a
`rate_limited` refusal occurs at the caller's *arrival* rate, which is
precisely the rate nothing bounds. So `rate_limited` rows are coalesced on
`(principal, tool, marker, scope)`, at most one row per key per
`MCP_REFUSAL_LOG_INTERVAL_SECONDS` (10 s).

`pending` counts the refusals since the last written row that **no row yet
represents**, and every reader takes a row to stand for `1 + suppressed`
refusals. The two flush paths are **not symmetric**, and getting that wrong
double-counts:

- **Window opening.** The first refusal for a key writes its own row with
  `suppressed = 0` and sets `pending = 0`; that row represents exactly itself.
- **Inside an open window.** `pending += 1`, and **no INSERT and no UPDATE** —
  no statement of any kind. The row template is not even built.
- **Rollover, triggered by a new refusal** after the window closed: write one
  row with `suppressed = pending`, **the arriving refusal being the row's
  base**, then reset the window and `pending = 0`.
- **Standalone flush**, driven by the indexer's periodic tick or by lifespan
  shutdown **before `engine.dispose()`**: there is no new refusal to serve as a
  base, so the row must stand for one of the pending refusals itself and
  carries `suppressed = pending − 1`. A closed window with `pending == 0`
  writes **nothing** — the refusal that opened it already has a row.

Σ `(1 + suppressed)` over a key's rows therefore equals the refusals observed
for it, **exactly**, on any interleaving of rollovers and flushes.
`_due_rows()` pops every closed window *synchronously, before the first
`await`*, which is what makes a flush safe against a refusal arriving
mid-flush: the window is either already retired (and the arriving refusal opens
a fresh one, writing its own row) or untouched. Deciding and then awaiting
before mutating would let one refusal be counted on both sides.

### A planned row is acknowledged, and a failed one is requeued

Advancing the window when a row is *planned* is the only workable order — the
alternative is holding a lock across a database write on the hottest path in
the server — but it means the count that row carries is in flight, owned by
the registered entry, until the write is confirmed. So every row the coalescer decides to
write is a **`PlannedRow`** carrying its own `weight` (`1 + suppressed`), and
`write_planned_row` either sees `write_usage_row` return `True` or **requeues
the whole weight** into the window's `pending`.

That is not defensive coding; it is the arithmetic. Without it a write that
answered `False` — the credential deleted mid-call, the pool exhausted, the
insert rejected — left the window already advanced and the row never written,
so `1 + suppressed` observed refusals vanished with no trace on any surface and
Σ `(1 + suppressed)` silently undercounted, which is the one thing this
arithmetic exists to make exact. A requeue after a *flush* failure restores the
window with its **original start**, so the row is due again on the very next
tick rather than after another whole interval; a requeue after an *immediate*
failure adds to whatever has accumulated since. Exceptions count as failures:
an exception is not evidence the row landed.

**In-flight rows pin their entry.** Planning increments an entry-local count;
acknowledgement or requeue releases it. The idle sweep refuses an entry while
that count is nonzero, even after a flush retired all its windows. Otherwise a
new principal could evict it during the database await and a failed write would
restore counts into an object no future flush can reach. Pinned entries still
count against the registry cap; new principals use the existing shared overflow
entry, so retaining ownership does not expand that cap.

**Cancellation retains counts and still propagates.** The active writer puts
its unconfirmed weight back before re-raising cancellation; the batch puts back
every later row it retired but has not attempted. The lifespan cancels the
periodic indexer before its final flush, so losing these rows on cancellation
would lose refusals on a graceful restart, not just on a hard kill.

The immediate row is written from the captured template too, through the same
`write_planned_row`. One code path builds every `rate_limited` row, so the
deferred one is not the only path anybody has exercised end to end.

### Two flushes, and why shutdown needs the other one

`flush_expired()` retires only **closed** windows: inside its interval more
refusals may still arrive and coalescing them is the entire point. `flush_all()`
retires **every** window, open ones included, and is what the lifespan calls
before `engine.dispose()` — at shutdown there is no next tick and no next
refusal, so an open window's pending count is simply lost unless it is retired
now. Using the periodic flush at shutdown dropped the current interval on every
clean restart.

The tick's flush sits in the loop's **`finally`**, reached by the paused branch
and the failure branch as well as the healthy one. It used to sit after
`cleanup_expired_tokens()`, which only a healthy tick reaches: a paused
deployment (`continue`) and a failing one (the exception handler) both jumped
past it, so exactly the two states an operator investigates with
`/admin/performance` open were the two that never wrote their counts.

**The entry stores the complete, immutable attribution of the row it will
write** — owner `user_id`, `key_id` / `oauth_token_id`, the denormalised
`actor_*` triple, tool, marker, scope and the bounded params — captured at the
moment the window opened. A deferred flush therefore reads **no ContextVar**
and depends on **no live credential**: by flush time the request is long gone
and the key may have been deleted, and `write_usage_row`'s existing 23503
recovery (clear the FK ids, keep `actor_*`) is exactly the path that makes such
a row land anyway. That recovery exists because #77 needed the label to survive
the credential; here it does double duty. Building the template is itself
guarded — a `transforms` entry that raises on the value it was given must not
turn a *refusal* into an exception.

`scope` is part of the key because `principal_write` and `principal` are
different facts about the same tool, and merging them would attribute a
write-bucket refusal to the general one.

**`argument_too_long` is deliberately NOT coalesced.** It sits *below* the
general bucket, so a principal can produce at most
`MCP_RATE_LIMIT_PER_MINUTE` of them per minute — the same bound as any admitted
call's row, and therefore already bounded without a mechanism. A second code
path would buy nothing.

Cardinality is bounded by the same registry cap as the buckets: past
`MCP_LIMITER_MAX_TRACKED_PRINCIPALS`, further keys fold into shared overflow
entries keyed on **`(tool, marker, scope)`** — the principal is the only
component dropped, so an overflowed row still names the tool, the marker and
the control that fired.

**A row written from the overflow entry is explicitly UNATTRIBUTED**: its
`user_id`, `key_id`, `oauth_token_id` and `actor_*` columns are NULL. The entry
is shared, so its row stands for traffic from several credentials at once, and
stamping it with whichever member happened to open the window would attribute
an aggregate to one specific credential — a false fact about a named key on the
surface an operator uses to decide whose key to revoke, which is worse than the
missing attribution the overflow already accepts. The count survives; the name
does not.

> **Accepted limitations.** (a) An abrupt process termination (SIGKILL, OOM)
> loses the pending counts of open windows — **at most one interval's worth per
> active key**; the alternative is a durable write per refusal, which is the
> amplification this exists to stop. (b) Past the registry cap, coalesced rows
> lose per-principal attribution but **not** their count.

**Rejected:** "the gate refusal writes no row." A limiter invisible in the log
is one nobody can diagnose or size, and `/admin/performance` is where an
operator looks.

## Nothing blocks while holding the loop, and nothing adds I/O

A bucket update is a **synchronous** function — read `(tokens, updated)`,
compute, write back — with **no `await` between the read and the write**, so on
a single-threaded event loop it is atomic *by construction* and needs no lock.
The L1 address table is the same shape. The admitted common path is a couple of
dict/table lookups and some floats: no statement, no session checkout. That is
pinned by a statement-counting test, the way `tests/test_issue_162_quota_gate.py`
pins the quota gate — the only way this could regress invisibly.

The clock is `time.monotonic()`, never the wall clock: a clock adjustment must
not hand out free capacity or refuse a caller for an hour. `take()` returns
`(admitted, retry_after_seconds)` where the interval is a whole number of at
least one second — a refusal quoting "retry in 0 seconds" invites the tightest
possible loop.

## The default daily limit is applied in application code, never as a column default

`DEFAULT_DAILY_REQUEST_LIMIT` (5,000) is applied by the key-creation paths.
A `server_default` would be a schema change, would apply to every future insert
path, and still could not express "grandfather the rows that exist".
**Existing keys are untouched.**

- **JSON API.** Omitted vs. explicit-`null` is distinguished by
  `model_fields_set`, not by truthiness: an omitted field means the default, an
  explicit `null` still means **unlimited**, and an explicit value wins. The
  setting is read per request, never captured at import.
- **Panel.** The default is materialised only as the create form's pre-filled
  value. A blank submitted field is an explicit unlimited with **no POST-side
  substitution**, so the operator's last view of the field is what the key
  receives. `keys_page` passes the default to the template; the create handler
  never reads the setting. The edit path substitutes nothing, on either
  surface.

**Why 5,000.** ~1,600 tool calls per 30 days across all credentials, so
5,000/day is two orders of magnitude of headroom and cannot interrupt a real
session, while a runaway stops the same day. At 120/min it takes ≥ 42 minutes
to spend. It binds only keys created after this shipped.

## The quota's retry interval comes from the admission's own clock read

`Admission` carries `day` and `count`, and `reset_at` is derived from `day`.
Computing a retry interval downstream would need a second `datetime.now()` —
precisely the double-clock-read bug the class exists to prevent (#162). So
`admit()` records the instant it *already read* as `decided_at` on the
`Admission`, and `retry_after_seconds = max(1, ceil(reset_at − decided_at))`,
with nothing downstream re-reading the clock.

The failure this prevents is not hypothetical: a decision bound to day *D*
whose message is rendered after midnight would otherwise quote an interval to
the *next* day's reset — about 48 hours — for a ceiling that had already
reset. A test drives the clock across UTC midnight between the decision and the
message and asserts the interval is the small one; the decorator's own clock
*raises* if read at all, so "no clock read between the statement and the
rendered refusal" is enforced rather than asserted about a value.

## The failed-auth budget lives in the app; Traefik is the wrong instrument

A proxy cannot condition on the **response status**, so a `ratelimit`
middleware on `obsidian-mcp-api-rtr` would have to throttle *authenticated*
agents in order to bound *unauthenticated* probing — and `/transfer/*`,
`/health` and `/.well-known` share that router. On top of that the host's
Traefik static configuration is outside this repo (`CLAUDE.md`, "Public repo —
host paths live outside the tree"), so a control expressed there would not be
reproducible from the tree. The `docker-compose.yml` labels are unchanged, with
a comment there recording the decision.

Three details make the in-app control correct.

- **The address comes from `ProxyHeadersMiddleware`**, which is added on the
  app and therefore wraps the mount, and is honoured only for the peers
  `TRUSTED_PROXY_IPS` names. A budget keyed on a spoofable header is **worse
  than none**; `request-trust`'s restricted proxy-header requirement is the
  dependency.
- **Every 401 branch increments** — missing bearer, unknown credential,
  ownerless, inactive user, expired, cross-user grant, missing vault scope —
  because a prober picks the cheapest one, and a budget covering six of seven
  bounds nothing. The bearer-less branch is charged directly rather than
  through `_emit_auth_failure`, because that event's `reason` is a closed
  vocabulary about a credential that was *read*, and this branch read none.
- **A request with no client address is charged to one reserved slot** shared
  by all such requests, rather than exempted: exempting is a bypass that anyone
  able to strip the header gets for free.

**The threshold and the `Retry-After` arithmetic live in one helper** so they
cannot drift. Refuse when the count already recorded in the window is `≥
MCP_AUTH_FAILURE_LIMIT` — with 60, the 61st failure is the first refused — and
a refused request does **not** increment, because it never reached
authentication. `Retry-After` is the whole seconds remaining in the current
window, minimum 1. One WARNING per slot per window (`auth_failure_rate_limited`
in the security-event catalogue): every later refusal in the same window is the
same fact and would be an unbounded channel opened by the control that exists
to close one.

What it bounds is the **database work an unauthenticated caller can force** —
one session checkout and one indexed SELECT per probe. It is *not* a defence
against credential guessing; #194's own verification withdrew that (256-bit
`secrets.token_hex` keys).

> **Declared risk — shared egress.** claude.ai egresses from shared addresses,
> and the hashed table can merge two addresses into one slot. Both make the
> control **stricter, never weaker**. Mitigated by a generous default (60
> failures / 5 minutes — no working client fails 60 times in 5 minutes), by
> `MCP_AUTH_FAILURE_LIMIT=null`, and by the one WARNING naming the address the
> first time a slot engages.

## The panel login budget is keyed by account, exactly, and is not a lockout

`POST /admin/auth/login` keeps its 5/min per-address slowapi limit and gains a
per-**account** failed-attempt budget beside it (#189). The two are additive
and neither subsumes the other: an address-keyed limit hands an attacker a
fresh allowance for every address they can rotate through, and an
account-keyed limit alone lets one address walk many accounts.

**slowapi cannot express it.** `src/limiter.py` says why in as many words: its
`key_func` is synchronous and runs before the handler, and the submitted
username is in the form body — reading it there would consume the stream the
handler needs. So the budget lives in `src/services/rate_limits.py`, next to
the other in-process controls.

**It does not reuse the salted slot table, and that is the load-bearing
decision.** That table merges colliding keys, which is a *bound* for the `/mcp`
failed-auth budget — addresses are unbounded and free to mint, so a shared slot
only makes the control stricter and nobody can choose whom to collide with. For
this key space it would be a **cross-account denial**: ten failures against a
colliding name would refuse a different account's *correct* password, and an
attacker submitting random non-existent usernames could saturate every slot
without knowing any victim's name. The budget is therefore a plain
`dict[users.id, window]` — exact keys, no salt, no capacity policy to get
wrong — swept on access so it holds at most one counter per account that has
failed recently. The key space is the users table, which is small and
administrator-controlled; that is the whole bound.

A failure is counted **only when the submitted username resolves to a row**.
There is nothing to brute-force behind a name that matches no account, so there
is nothing to bound, and those attempts stay under the address limit alone —
which is what makes "a different username is unaffected" literally true rather
than probabilistically true. Keying is by row and not by the `is_active` flag,
so budget behaviour never becomes a side channel for account state.

The check runs **after** the user lookup, because it needs the id, and
**before** `verify_password`, because a budget consulted after the comparison
bounds nothing — the guess has already been answered.

**Four properties make it a bound rather than a lockout.** Only failures are
counted and the window is short (ten in fifteen minutes, self-healing, no
durable state, no administrative unlock). An authenticated session is
unaffected — `login_form` short-circuits a valid session to the panel, so a
flood can delay a *fresh* password sign-in and nothing else. The threshold sits
far above a human typo rate and far below an online guessing budget worth
having. And the refusal is equivalent in content to an ordinary failed login.

**"Equivalent in content" has an exact scope, and it excludes two things.**
Equivalence means the same HTTP status, the same rendered template and the same
user-visible message for the same submitted inputs — a 401 login page, never a
429. Excluded: **timing**, because a throttled attempt skips bcrypt, and part
of that signal is new (for one active account, attempts 1–10 run the comparison
and attempt 11 does not, so an observer who can time responses can tell an
exhausted budget from an unexhausted one for a name they already know exists);
and **per-response nondeterminism** — a freshly rendered page carries a CSRF
token and may carry a session cookie, which differ between any two responses.
The guarantee is over response content, and it is stated that way rather than
as an unqualified "only the log knows". The only place the distinction is
recorded is `panel_login_account_throttled` (see
[security event logging](security-event-logging.md)), whose subject is the
client address and never the submitted username.

What remains, stated rather than hidden: a sustained flood can deny *password*
sign-in for one named account for the flood's duration plus the window.
Accepted, strictly preferable to unbounded per-account guessing against
credentials that guard other tenants' vaults, and recoverable without waiting
by restarting the container — which clears in-process state, like every other
control in this file.

## Proxy trust is one setting, canonicalised, and uvicorn's layer is off

Every control in this file keys on the address `ProxyHeadersMiddleware`
resolves, so the list of peers allowed to set `X-Forwarded-*` is part of the
rate-limit design and not an unrelated deployment detail. It was expressed
twice and inconsistently until #189: a hard-coded literal in `src/main.py` and
a `--forwarded-allow-ips` in the `Dockerfile` that excluded the real proxy
subnet and was therefore inert.

`TRUSTED_PROXY_IPS` (pydantic, CSV or JSON) is now the only statement of it.
Its default is exactly the previously effective list, so a deploy that changes
no environment value changes no client-IP resolution. Narrowing it to the
proxy's own address is an operator decision that must not need a code change —
the deployment's Docker network is shared with containers that run user-
supplied code by design, and any of them can forge the header.

**uvicorn's layer is switched off (`--no-proxy-headers`), not aligned.** Merely
agreeing is a state an operator can break from the environment: uvicorn's
`proxy_headers` defaults to *enabled* and its `forwarded_allow_ips` reads
`$FORWARDED_ALLOW_IPS`. One control means one control.

**Entries are stored canonicalised, and that is not cosmetic.**
`ip_network("192.168.0.10/24", strict=False)` accepts host bits and answers
`192.168.0.0/24`, but the un-canonicalised string is what uvicorn's stricter
parse rejects, keeping it as a literal that matches no peer. The failure is
silent and inverts the intent: every proxied request then retains the *proxy's*
address, so all callers collapse into one limiter bucket. `Settings` therefore
stores `str(ip_network(...))` — a bare address stays bare — and the regression
test drives that case through the **real** installed middleware rather than
through our own parser. A malformed entry refuses startup naming it; a trust
list that quietly loses a range is worse than one that fails. The effective
canonical list is logged once at startup, because a trust boundary nobody can
read from the logs is a trust boundary nobody audits.

## Limiter state is bounded by construction, and each registry says how

- **Addresses** are unauthenticated and unbounded in cardinality — a caller
  mints a new one per request for free — so eviction is a losing game. The
  failed-auth budget uses a **fixed-size table** of
  `MCP_AUTH_FAILURE_TABLE_SIZE` (4,096) counters indexed by a **per-process
  randomly salted** blake2b hash. Memory is O(size), there is nothing to evict,
  collisions only make the control stricter, and the random per-process salt
  means nobody can *choose* to collide with a victim. Slot 0 is reserved for
  the address-less requests.
- **Principals** are authenticated, so cardinality is bounded by the
  credentials that exist. A dict with a hard cap
  (`MCP_LIMITER_MAX_TRACKED_PRINCIPALS`, 10,000) and TTL eviction swept
  amortised on insert — bounded work per admission, deliberately **not** a
  background task, which would be a second thing to start, stop and reason
  about at shutdown for a dictionary.

  The sweep walks a **rotating cursor**: a snapshot of the registry's keys,
  consumed `SWEEP_SCAN` at a time and rebuilt when it empties, so one O(n)
  rebuild per full rotation keeps the per-sweep work bounded *and* every entry
  eventually examined. A fixed insertion-order prefix — the first shape —
  never reached the tail of a registry holding more than one scan's worth of
  entries, so those entries could only be reclaimed by a restart and the
  registry ratcheted towards its cap, and into the shared overflow entry,
  permanently.

**An entry is evictable only when it is full and idle.** A depleted bucket must
not be evicted (a fresh entry starts full, so eviction would grant free
capacity — idling through the sweep would be a way to reset a spent bucket),
and an entry with a pending coalescer count must not be evicted (that count is
the only record of refusals no row represents yet). The fullness test asks
whether the bucket *would* be at capacity if it refilled now, because the
refill is lazy and `tokens` alone says only what the bucket held when it was
last used.

> **Accepted limitation.** Past the cap, further principals share **one
> overflow entry**, so an overflowing principal's traffic can cause an
> unrelated overflowing principal to be refused. It fires only beyond 10,000
> tracked principals — a state requiring more than ten thousand live
> credentials. Fail-open was rejected (it lets the flood succeed) and
> fail-closed was rejected (it turns a bookkeeping cap into an outage for a
> legitimate credential).

**Accounts** are the third key space and the only **exactly** keyed one. The
panel login budget is a plain dict keyed on `users.id`, swept on access, so it
holds at most one counter per account that has failed recently and cannot grow
with the number of usernames an attacker submits. No table, no salt, no cap:
merging two keys here would refuse an unrelated account's correct password,
which is why the salted address table is deliberately not reused for it.

## Limiter state is in-process, and `--workers 1` is part of the contract

All bucket, coalescing, failed-authentication and panel-login-budget state
lives in the worker process and is **not** persisted, replicated or shared. A
restart begins with every bucket full and every counter zero — which is also
the login budget's recovery path if an operator does not want to wait out a
flood's window.

That is sound because there is exactly **one** uvicorn worker; because
instantaneous pressure is meaningless to persist across a process that no
longer exists; because a restart is an operator action or a crash, not
something a caller can induce; and because the durable ceiling already exists
as `quota_counters`.

**`--workers N` multiplies every in-process rate by N.** A second worker does
not split the configured rates between them — it gives each worker a full set —
and it splits the coalescer, so the same key writes a row per worker. The
`Dockerfile`'s `CMD` carries a comment saying so at the definition; this
paragraph is the prose half of the same statement. Raising the worker count
means revisiting this note first.

## One representation for "off", and what the boot validator checks

`.env` has no JSON `null`, so one central `BeforeValidator` (`NullableLimit` in
`src/config.py`) maps an **empty value**, `null` or `none` — stripped,
case-insensitive — to `None` for **every** nullable limiter setting. One
validator, not per-field variants, so no two controls can end up disabled
differently. It is tested through a real written env file, not only by
constructing the settings object with a Python `None`.

**Null is the only disable. Zero is rejected everywhere** (`ge=1`), for #162's
reason: a control that refuses every call reads to an operator as an outage
rather than as a setting.

**Every limiter setting also has a ceiling**, and for the mirror-image reason.
"No upper bound" is not "no limit": pydantic accepts an arbitrarily long
integer literal from the environment, so a burst set to a 401-digit number
booted cleanly and handed one principal a bucket no real traffic could ever
exhaust — a control that is *configured* and does nothing, which is the failure
an operator cannot see from any surface. `LIMITER_COUNT_MAX` (1,000,000) bounds
the rates, bursts and registry caps; `LIMITER_WINDOW_SECONDS_MAX` (one day) the
failure window; `REFUSAL_INTERVAL_SECONDS_MAX` (one hour) the coalescing
interval; `AUTH_FAILURE_TABLE_SIZE_MAX` (1,048,576) the address table, which is
allocated in full and is therefore a direct memory bound.
`DEFAULT_DAILY_REQUEST_LIMIT` keeps its domain in the model validator instead,
so the boot failure names `ck_api_keys_daily_request_limit`'s 1..1,000,000
rather than a bare field bound — the domain is what an operator has to satisfy.

The original rate settings validate the following rules. The concurrency
settings add hierarchy, coherence and per-class pool-budget validation,
described under concurrency below; pool sizes remain 5 + 10, shared constants
consumed by the engine and validation.

- each bucket's rate and burst are **both set or both null** — a rate with no
  burst is not "a bucket with a default burst", it is a control an operator
  believes is on and that admits everything, so the error names *both*
  settings;
- `DEFAULT_DAILY_REQUEST_LIMIT` lies within 1..1,000,000, the same domain
  `ck_api_keys_daily_request_limit` enforces — otherwise the boot looks healthy
  and the panel fails on the operator's next action.

## Three markers, split by what an operator asks

`rate_limited` (either bucket; `rate_limit_scope` distinguishes) and
`argument_too_long` are both **pre-body** and both enumerated by
`pre_body_refusal_sql()`. `provider_input_rejected` is **post-body** and
deliberately outside it. "Is one agent too fast?", "did a caller send something
too big?" and "did the provider refuse what we sent?" are answerable from the
marker alone.

`rate_limit_scope` is a JSON **string** that no reader casts. `suppressed` is a
JSON **integer** read with a *guarded, length-bounded* cast, and
`/admin/performance` sums `1 + suppressed` rather than counting rows — a reader
that counted rows would undercount by exactly the traffic an operator opened
the page to see. The full reading rules are in
[usage attribution](usage-attribution.md).

## Settings

| Setting | Default | What it bounds |
| --- | --- | --- |
| `MCP_AUTH_FAILURE_LIMIT` | `60` | Failed `/mcp` authentications per address per window before a 429. Null disables. |
| `MCP_AUTH_FAILURE_WINDOW_SECONDS` | `300` | The window that limit is counted over. |
| `MCP_AUTH_FAILURE_TABLE_SIZE` | `4096` | Counter slots in the salted fixed-size address table. Memory is O(size). |
| `TRUSTED_PROXY_IPS` | `127.0.0.1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16` | The peers whose `X-Forwarded-*` headers are honoured — the address every control here keys on. CSV or JSON, stored canonicalised, empty trusts nobody. The only such control; uvicorn's is off. |
| `PANEL_LOGIN_FAILURE_LIMIT` | `10` | Failed panel sign-ins one **account** may accrue per window before further attempts are refused without comparing the password. Null disables. |
| `PANEL_LOGIN_FAILURE_WINDOW_SECONDS` | `900` | The window that budget is counted over. |
| `MCP_RATE_LIMIT_PER_MINUTE` | `120` | Sustained tool calls per minute per principal. Null (with the burst) disables the general bucket. |
| `MCP_RATE_LIMIT_BURST` | `30` | Capacity of the general bucket. |
| `MCP_WRITE_RATE_LIMIT_PER_MINUTE` | `60` | Sustained vault-mutating calls per minute per principal (the eight write tools plus `PUT /transfer/upload`). |
| `MCP_WRITE_RATE_LIMIT_BURST` | `15` | Capacity of the write bucket. |
| `MCP_LIMITER_MAX_TRACKED_PRINCIPALS` | `10000` | Principals holding their own limiter entry before the shared overflow entry. |
| `MCP_REFUSAL_LOG_INTERVAL_SECONDS` | `10` | How long one coalescing window stays open. |
| `DEFAULT_DAILY_REQUEST_LIMIT` | `5000` | Daily quota a **newly created** key receives when the caller does not say otherwise. Null creates unlimited keys. |
| `MAX_SEARCH_QUERY_CHARS` | `8192` | Module constant, not a setting: the longest `query` `keyword_search` / `semantic_search` accept. |

Every nullable one accepts an **empty value**, `null` or `none` as "off". Zero
is refused at boot. The `MCP_CONCURRENCY_*` settings are not nullable (`off` is
a mode); their defaults are under concurrency below, and `.env.example` carries
the full block.

## Concurrency: four modes, earned one step at a time (#261, #188)

`src/services/concurrency.py` owns one process controller with four admission
points: the **request envelope** and the **auth permit** in
`APIKeyMiddleware` (the transport stages), the **tool lattice** in `_tracked`,
and the **writer permit** around every `write_usage_row`. Keep one worker. The
earlier rate-limit change deferred these controls because authentication,
rotating credentials and refusal logging each exposed holes in partial designs;
#261 built them in shadow, and #188 (`concurrency-enforce-ready`) made
enforcement reachable.

| Mode | Waits | Where `enforce` would refuse for capacity | Row annotation |
| --- | --- | --- | --- |
| `off` | never | no accounting at all | none, not even provenance |
| `shadow` (**default**) | never; the configured waits are only *reported* | the call runs; the miss is recorded as pressure | `concurrency_shadow` |
| `queue` | exactly as `enforce`: deadlines, waiter bounds, eligible FIFO | the lease is **granted** with an `overrun` mark | `concurrency_queue` |
| `enforce` | bounded | transport 429, in-band `slot_timeout`, or a dropped audit row at the writer | the refusal row |

**Promotion is an operator `.env` edit and a recreate, never automatic**, and
each step is earned with durable numbers (the rollout below). **Rollback is one
line**: `MCP_CONCURRENCY_MODE=<previous>`. That is why every other setting
validates identically in all four modes — the old rule "shadow requires
`MCP_CONCURRENCY_WAIT_SECONDS=0`" is gone, and shadow reports the waits it did
not apply as `configured_wait_ms` instead — and why the **epoch** (below)
excludes the mode: evidence collected in shadow stays attributable to the
configuration queue then runs.

**Why shadow alone could not be flipped.** The shadow window of 2026-09-13 to
09-21 (8.2 days, 1,073 calls, two tenants, all defaults) said enforcement as
built would have refused about **9.6 % of legitimate tool calls** as
`slot_timeout` (`read_note` 71, `keyword_search` 16, `get_vault_guide` 14) and
answered up to ~153 requests with a transport 429. Five blockers followed: the
transport stages could not wait; the pool budget was exactly saturated so only
one configuration validated, pinning `other = 1` for 15 of the 25 tools; the
shadow `code` was the *last* observation, under-reporting tool pressure ~11×;
nothing surfaced the data; and a writer wait extends every slot hold with no
evidence of what that costs. PGDATA then moved to NVMe (2026-09-23, commits
~40–100 ms → ~2.5 ms), so **no pre-change row is evidence for any flip** — the
evaluator excludes them unconditionally — but they remain evidence about the
shape: zero-wait ceilings of 1–2 refuse an agent's ordinary parallel reads. A
false refusal of a legitimate agent call breaks the live path, so the design
**waits within bounded time before refusing**.

### The transport stages wait, within one deadline, and notice a disconnect

The request envelope admits global + bearer-fingerprint dimensions together,
before any DB lookup, and its lease lasts until the whole downstream ASGI
request finishes — GET/SSE streams, initialization and notifications included.
SHA-256 fingerprints stay in bounded memory and are erased when their last
reference drains. The separate **auth permit** encloses only the middleware's
own session, cache warming and teardown included; even an invalid-auth
response is sent after that session and permit have closed. The auth permit is
**kept** rather than left to the pool: the pool's own queue is a 30 s timeout
ending in a 500, shared with the panel and `/token`.

- **One deadline for both stages**, `MCP_CONCURRENCY_TRANSPORT_WAIT_SECONDS`
  (default 2, max 5), started when the middleware first asks for the request
  lease. Waiters are bounded: request 64 (global) and fingerprint 16, auth 32.
  A request waiting for the envelope holds a registry reference only; one
  waiting for the auth permit holds its request lease; **neither holds a DB
  connection**. Queueing is cheap now and refusing is not: at auth 2 and ~5 ms
  per session, a 2 s deadline drains ~800 queued authentications.
- **In `enforce`, deadline expiry or waiter overflow** returns the existing
  transport 429 (`code`, `scope`, `limit`, `Retry-After: 1`), outside the
  in-band refusal contract. It queries no credential and writes no usage row.
- **A disconnect is not a cancellation.** uvicorn reports a client leaving
  through ASGI `receive`; it never cancels the middleware's coroutine, so a
  waiter nobody reads for would learn only at its deadline — and could then
  authenticate for nobody. `ReceiveWatch` (`src/mcp_server/auth.py`) is
  therefore the **only caller of `receive`** from the first wait until
  transport admission ends. It keeps reading **past `more_body: false`** —
  after the last body message the next `receive` blocks until the client
  leaves, which is exactly what it is waiting for (a watcher that stopped at
  the complete body missed that disconnect, SR2-1). `http.disconnect` releases
  the waiter, the references and any request lease at once; no credential
  query, no response. The middleware checks the flag again after the auth
  grant and before opening its session.
- **Every consumed message is replayed, intact.** Messages are appended as
  received — never dropped, split, merged or reordered. The handoff cancels
  and awaits the watcher (a completed `receive` is already in the list because
  the append precedes the next await; a cancelled pending one consumed
  nothing), and the app's `receive` yields the list and then delegates.
- **Memory is a process-wide replay budget, not a per-request cap.**
  `MCP_CONCURRENCY_REPLAY_BUDGET_BYTES` (32 MiB, range 1–256 MiB). When it is
  exhausted the watcher **stops calling `receive`** and keeps what it holds, so
  nothing is ever lost; that request is then deadline-bounded (≤ 5 s) instead
  of disconnect-aware (L8). The per-request alternatives failed review: a
  64 KiB cap that drops the crossing message is lossy, because a message is
  sized only after `receive` consumed it (SR2-2), and "body limit × waiters" is
  61 MiB (`mcp_max_request_body_bytes`; the SDK's 4 MiB default does not apply
  here) × 96 ≈ 5.7 GiB. The honest bound is **budget + 96 waiters × one uvicorn
  message**, and one message is at most `HIGH_WATER_LIMIT` (64 KiB, the point
  at which uvicorn pauses reading and hands back everything accumulated) plus
  one socket read (256 KiB) ≈ 320 KiB — **≈ 62 MiB** (L11, SR3-2). A guard test
  pins `HIGH_WATER_LIMIT == 65536`; a uvicorn upgrade that changes it, or the
  loop's read size, must re-derive the figure.

### Five tool classes, each with an independent ceiling

| Class | Tools | Ceiling | Connections |
| --- | --- | --- | --- |
| `embedding` | `semantic_search` | 1 | 1 |
| `vector` | `find_related` | 1 | 1 |
| `write` | the eight write-class tools (L3) | 1 | 2 |
| `scan` | `keyword_search`, `list_notes`, `get_tags`, `get_neighborhood`, `find_orphans`, `list_files` | 2 | 1 |
| `light` | `read_note`, `read_file`, `get_recent`, `get_vault_guide`, `get_backlinks`, `get_links`, `request_upload`, `check_upload`, `request_download` | 4 | 1 |

#261's `other` held 15 of the 25 tools and was pinned to 1. Raising it alone
would let one tenant's scans hold every cheap-read slot, so it was split by
cost. `MCP_CONCURRENCY_OTHER=1` — the old `.env.example` value, which expressed
no intent — is ignored with one WARNING; any other value is refused at boot,
naming `MCP_CONCURRENCY_LIGHT` and `MCP_CONCURRENCY_SCAN`. Unknown
registrations cannot silently land in a class. `CLASS_MAPPING_VERSION` is part
of the epoch, so rows from another classification never mix into one window.

A tool acquires class, tenant, principal and global counters in **one
non-awaiting transition**, after the argument screens and before quota. A
queued call holds none of those permits. Eligible FIFO wakes the oldest call
that can acquire **all** dimensions, so a saturated class cannot park global
capacity. Tenants are authenticated user IDs (with one stable NULL-owner
identity); principals are key IDs or OAuth grant IDs, so a refresh cannot reset
them. There is no per-tenant embedding reservation or starvation SLA: one
embedding slot cannot reserve work for N tenants.

The tool wait is one monotonic deadline (`MCP_CONCURRENCY_WAIT_SECONDS`,
default 5, max 10). Zero means immediate admission or refusal, not disabled
control. Grants transfer ownership before waking a waiter, a grant racing the
timeout grants exactly once, and cancellation before or after a grant returns
every captured lease. Registry overflow is sticky while any overflow lease **or
waiter** exists; unknown identities share the overflow entry until it drains,
so an active identity cannot obtain fresh capacity. Dedicated entries have a
bounded reverse index; no active entry is evicted or migrated.

**Defaults:** requests 64 / fingerprint 20; auth 2; tools 6 / tenant 4 /
principal 3; class ceilings as tabled; tool waiters 64 global / 32 tenant / 16
principal; keyed registry 1024; writers 1 with 64 waiters and a maximum
0.25 s wait. Writer admission is independent of the authenticated principal, so
background and coalesced refusal flushes cannot bypass it. Writer leases cover
the whole usage write including the FK retry; the failed session closes before
the retry opens.

### The pool budget: per-class multipliers, no class sum

`src/services/pool_budget.py` is the single source: pool 5 + overflow 10, four
connections of headroom, and `CLASS_CONNECTIONS` (write 2, every other class
1). `tool_demand(tools, caps)` fills the `tools` slots highest multiplier
first, which is the exact maximum of `Σ m·n` per class under `n ≤ cap` and
`Σ n ≤ tools`. Settings refuse `auth + tool_demand + writers + 4 > 15` and name
every term. At the defaults: **2 + 7 + 1 + 4 = 14**.

- **The multiplier is measured, not assumed.** The non-write paths are
  sequential: quota admission commits and releases before the body, the auth
  session closes before the tool runs, and the usage write happens after the
  body under the writer permit (budgeted in `writers`). A real-PG test drives
  **every registered tool** through `_tracked` and asserts its per-task
  checkout peak ≤ `CLASS_CONNECTIONS[class]`. A tool that measures higher
  raises its class's multiplier here; it is not waved through. `write` stays
  at 2 because its paths depend on input size (L7). #261's flat ×2, which
  priced a `read_note` like a `move_note`, is gone.
- **The class-sum rule is gone, and the bound did not weaken.** The global
  `tools` counter already bounds total admitted tools and admission is atomic;
  independent class ceilings (each ≤ `tools`) bound class *shares* — e.g.
  `embedding` ≤ 1 protects the provider. Under the sum rule exactly one
  configuration validated.
- **This bounds MCP's configured contribution, not pool availability.** The
  indexer, panel, OAuth and transfer share the pool and can consume the
  headroom; it is not reserved (L4). Raising the pool was rejected: the
  Postgres instance is shared, and the pool was never the measured bottleneck.

### Coherence, and a log line because validation is not proof

Besides the pool budget, the validator refuses a child above its parent
(fingerprint ≤ requests, principal ≤ tenant ≤ tools, each class ≤ tools,
principal ≤ tenant ≤ global tool waiters, fingerprint ≤ request waiters) and
requires **`fingerprint ≥ principal + principal_waiters`**: one principal's
admitted and waiting tools each hold a request lease, and the transport
envelope must not refuse what the tool stage would have queued.

A fully pinned legacy `.env` can still validate (`FINGERPRINT=4 ≥ PRINCIPAL 2 +
PRINCIPAL_WAITERS 2`), so passing validation does not prove the intended
settings are live. Startup therefore logs **one INFO line** — `MCP concurrency
effective settings: mode=… epoch=… pool_demand=… limits=…` — and the deploy
compares it with the intended block.

### `queue`: a rehearsal that measures the real waits

Every stage (request, auth, tool, writer) runs enforcement's admission. Where
enforcement would refuse **for capacity** — deadline expiry, waiter overflow, a
zero-wait miss — queue grants and marks the admission `overrun`. Only the
concurrency outcome changes:

- The call proceeds through the remaining gates, and they keep their
  authority. An overrun followed by a quota refusal is an ordinary `over_quota`
  pre-body refusal: no body, no quota consumed, classified exactly as before.
  `concurrency_queue` only annotates; it never changes classification.
- An overrun writes no `slot_timeout` row and returns no refusal. A writer
  overrun still writes its row (and counts `writer_overrun`).
- Shutdown refusal stays a refusal in every mode.
- Queue bounds added latency, **not occupancy**: during an overrun occupancy
  may exceed the ceilings, as in shadow (L1).

A shadow counterfactual estimator was rejected: its estimate resolves after the
row is written, and it is biased by calls enforcement would not have run. Queue
measures the same quantity exactly.

### The lease stays held through telemetry

An admitted tool lease covers quota, the body and the response-neutral
telemetry tail, released in `finally` on success, quota refusal, exception or
cancellation. #188 proposed releasing it before telemetry, to stop a writer wait
from extending the slot hold, and spec review rejected that (SR1-1):
`write_usage_row` returns `False` after its 0.25 s writer wait and an ordinary
completed-call row is not requeued, so a lease held through its own usage write
is the backpressure that limits how fast further completed writes queue behind
a slow writer. Early release reproduced a **lost audit row for a completed
write** that the current lifetime saves — worse than a few milliseconds of slot
hold. The coupling is bounded instead (≤ 0.25 s, and ~ms writer holds since
NVMe) and **measured**: queue-mode `queue_ms` includes any writer-extended hold,
so E1/E4 see it before enforcement is possible (L10). Worst-case added latency
for one call is transport + tool + writer wait, **7.25 s** at the defaults
(L3).

### A concurrency refusal spends rate tokens, and no quota

Gate order is buckets → vault → argument screens → tool slots → quota. A call
refused at the slot gate has already spent a general-bucket token — and a
write token for a write-class tool — as every later pre-body refusal has since
#162 ("a tool that always fails would be free"). **The guarantee "nothing
durable is consumed by a call that does not run" covers the durable daily
quota (`quota_counters`) only.** Rate tokens are in-memory velocity state; they
refill and are not refunded (L9). Refunding would need reservation machinery
on the hottest path. Queue mode never refuses for capacity, so it never
produces the case.

### Durable evidence: provenance, event-time counters and a run watermark

The flip question has to be answered from numbers that survive restarts and
can be windowed, which neither in-process since-boot counters nor the rotating
security-event log can give.

- **Row provenance.** Whenever the mode is not `off`, every row `_tracked` and
  its refusal, coalescer and failure paths write carries `params.concurrency =
  {v: 2, mode, epoch}` — unpressured rows included, so a v2 row is
  distinguishable from a legacy one. The **epoch** is the first 12 hex digits
  of the SHA-256 of every `mcp_concurrency_*` setting except `mode`, plus the
  class mapping version. Shapes: [usage attribution](usage-attribution.md).
- **Transport, writer and pool outcomes** go to `concurrency_counters` in
  event-time minute buckets (migration 028; the tables are in
  [schema and migrations](schema-and-migrations.md)). Each `/mcp` request that
  reaches admission counts once in `requests` and at most once more, by its
  **worst** outcome (`transport_pressured` | `_waited` | `_overrun` |
  `_refused`); `writer_overrun`, `writer_refused` and `pool_checkout_timeout`
  are counts; `pool_high_water` and `transport_wait_max_ms` are bucket maxima.
  A shutdown refusal is not a capacity outcome and is not counted as
  `transport_refused` — otherwise every deploy would trip the enforce
  rollback. An ownerless usage row per pressured request stays forbidden (#261:
  it turns an unauthenticated flood into writes); the counters cost one
  statement a minute.
- **Pool timeouts are counted at the shared checkout boundary.**
  `CountingQueuePool` (`src/database.py`) overrides `_do_get`, counts
  `sqlalchemy.exc.TimeoutError` — the pool's own, not a provider's
  `TimeoutError` — and re-raises the same object, for **every** engine
  consumer: MCP auth, quota, tool bodies, usage writers, panel, `/token`,
  transfer, indexer. Counting `tool_exception` rows would miss most of those
  and confuse unrelated timeouts.
- **One transaction a minute** drains the accumulator at `t`, upserts the
  drained buckets under their original keys, and sets the run's
  `completed_through = floor_minute(t)` — the **completed-interval watermark**.
  A failed flush merges back under the original keys and does not advance it.
  The shutdown flush (after the coalescer's `flush_all()`, before
  `engine.dispose()`) sets it to exactly `t` with `clean_shutdown = true`. The
  commit is synchronous; it does not join #279's allow-list. More than 60
  unflushed minutes drops the oldest and marks the run `lossy`.
- **Coverage.** A run covers `[started_at, completed_through]` unless lossy. A
  gap between runs is covered **only after a clean shutdown**; after a hard
  kill, OOM or crash it is uncovered however short the restart, because the
  killed run's unflushed tail may have held incidents (L2; heartbeat spacing
  could not tell a recreate from a kill, SR2-3). Bucket time is event time,
  never flush time (SR2-4). Every evaluation boundary is a whole minute — start
  rounded up, end rounded down, **clean-shutdown ends included**, because a
  bucket is keyed by minute, epoch and mode rather than run, and an exact end
  would pull a same-configuration successor's incident from the shared minute
  into the earlier window (SR3-1).
- **One source per numerator.** Tool-stage figures come only from usage rows;
  transport, writer and pool figures only from counters. Nothing is counted
  twice.

### Readiness and the rollout

`src/services/concurrency_readiness.py` holds one read-only `window_stats` and
one pure `evaluate(stats, target)`; the `/admin/performance` verdict and `make
concurrency-report` both call them, so they cannot disagree. A window
qualifies only if it is **covered**, holds v2 evidence of **exactly one mode
and one epoch** (the target's source mode and the configured epoch), meets the
minimum length and call count, and ends at or before the durable watermark.
Anything else gives INSUFFICIENT_DATA for **every** criterion, with the latest
qualifying sub-window start — a quiet covered window is evidence, an uncovered
one is not.

**Step 0 — deploy in shadow.** Reconcile the deploy-dir `.env` concurrency
block to `.env.example` (the old block pins #261's values and can still
validate) and dry-run the settings against it with the new image. After the
recreate, compare the startup INFO line with the intended block; `make
db-check` must be clean after 028. Exercise one tool per class live, and
confirm rows carry `params.concurrency`, the new run has a `concurrency_runs`
row, and its `completed_through` advances each minute.

**Step 1 — shadow → queue.** A covered window of **≥ 3 days** and **≥ 300
executed calls**, all shadow, one epoch:

| ID | Criterion (source) | Threshold |
| --- | --- | --- |
| Q1 | Tool-pressured executed calls / executed calls (rows) | ≤ 10 % |
| Q2 | `transport_pressured` / `requests` (counters) | ≤ 5 % |
| Q3 | `pool_checkout_timeout` (counters) | 0 |

Tool pressure is read from the **observations**, never from `code`, so a row
whose tool observation is not the first still counts.

**Step 2 — queue → enforce.** A covered window of **≥ 7 days** and **≥ 1,000
executed calls**, all queue, one epoch; each criterion must hold over the whole
window **and** over its last 72 h:

| ID | Criterion (source) | Threshold |
| --- | --- | --- |
| E1 | Distinct calls with a tool-stage overrun (rows) | ≤ max(1, 0.1 % of executed calls) |
| E2 | `transport_overrun` requests (counters) | 0 |
| E3 | `writer_overrun` (counters) | 0 |
| E4 | Tool `queue_ms` p99 across executed calls (rows) | ≤ 500 ms |
| E5 | Max tool `queue_ms` over every v2 row carrying one, including calls then refused pre-body (e.g. `over_quota`) / tool wait (rows); `transport_wait_max_ms` / transport wait (counters) | ≤ 0.5 each |
| E6 | `pool_checkout_timeout` (counters); max `pool_high_water` | 0; ≤ 13 |

**Rollback triggers**, checked daily for the first 7 days of each new mode:

- **queue → shadow:** any 24 h with tool `queue_ms` p95 > 1,000 ms, or an
  agent-side timeout attributable to queueing;
- **enforce → queue:** any `transport_refused` > 0; weighted `slot_timeout` >
  max(2, 0.2 % of calls) in any 24 h; or any `pool_checkout_timeout`.

**`make concurrency-report TARGET=queue|enforce [DAYS=n] [END=iso]`** runs the
evaluator inside the container from the database alone, for the configured
epoch and the target's source mode (shadow for `queue`, queue for `enforce`).
`DAYS` defaults to the target's minimum and must be in (0, 35] — the prune
horizon; `END` defaults to the watermark, and an explicit end past it is
INSUFFICIENT_DATA, so an unflushed tail is never certified. It prints a table
and **one JSON line**, which is what gets posted on #188. Exit codes: **0**
every criterion PASS; **1** any FAIL (FAIL outranks INSUFFICIENT_DATA);
**2** INSUFFICIENT_DATA; **64** a usage error. The thresholds are module
constants calibrated to ~130 calls a day across two tenants (L6): re-run the
report after a tenant is added.

### Lifecycle

The lifespan installs the controller explicitly and logs the effective-settings
line; requests never replace live state. It registers the run and starts the
60 s counter flush (sandbox mode skips both). Shutdown refuses and wakes
pending tool work, runs the coalescer's final `flush_all()`, then the clean
counter flush, then closes writers before `engine.dispose()`. Captured leases
release their own controller even across explicit lifecycle and test resets.
No asyncio primitive is created at module import. Production saturation tests
are prohibited: calibrate from shadow and queue evidence, and test enforcement
only in isolated environments.

### Accepted limitations (#188)

- **L1** Queue bounds latency, not occupancy.
- **L2** A hard kill loses up to ~60 s of counter increments; windows spanning
  its gap read INSUFFICIENT_DATA, however quick the restart.
- **L3** Worst-case added latency is transport + tool + writer wait (7.25 s).
- **L4** The budget bounds MCP's configured contribution; headroom is not
  reserved.
- **L5** A shared credential is a shared fingerprint and principal.
- **L6** Thresholds are calibrated to current traffic.
- **L7** Per-class multipliers are measured on fixtures; `write` keeps 2.
- **L8** While the replay budget is exhausted a waiting request is
  deadline-bounded, not disconnect-aware, and one whose client left may
  authenticate once. It never loses bytes.
- **L9** Rate tokens spent before a concurrency refusal are not refunded.
- **L10** Writer-extended slot holds remain; queue measures them.
- **L11** Worst-case replay memory ≈ 62 MiB, derived from uvicorn's
  `HIGH_WATER_LIMIT` and the loop's read size.

## Alternatives rejected

- **slowapi on the `/mcp` mount.** Keyed on the remote address — wrong scope:
  shared egress merges tenants, and one tenant with two agents is one bucket.
  It is decorator-shaped for routes, not an ASGI mount, and it produces an HTTP
  429 where the contract needs a parseable in-band tool result.
- **A Traefik `ratelimit` middleware as the primary control.** Above.
- **Making the daily quota the burst control by lowering it.** A per-day
  counter cannot bound a burst at all, and a low daily number to approximate a
  rate would exhaust a legitimate agent's day in one healthy burst. #188's own
  recommendation is necessary and insufficient.
- **Persisting bucket state in PostgreSQL** (the `quota_counters` shape). A
  statement on the hottest path in the server, to bound something meaningless
  once the process is gone, with the durable ceiling already one layer up.
- **Refunding a token on a refusal** — a concurrency refusal included (#188
  D9). #162's reasoning: a tool that always fails would be free.
- **Going straight from shadow to enforce with long waits**, and **per-class
  tool waits** (#188). Queue exists so the waits are measured before any
  capacity refusal is possible; one tool deadline keeps the arithmetic
  readable.
- **Early release of the tool lease before telemetry** (#188, SR1-1). Above:
  it lost audit rows for completed writes.
- **Transport evidence from security-event logs or since-boot counters**, and
  **counting pool timeouts from `tool_exception` rows** (#188 D8, D10). Above.
- **Enforcing the query cap inside the search bodies.** It would be a
  post-body marker polluting the percentiles.
- **Fail-open or fail-closed on principal-registry overflow.** Above.

## Standing residuals

- Default shadow mode does not enforce concurrency ceilings, and queue bounds
  latency, not occupancy; only `enforce` bounds, and it bounds MCP's
  contribution only, not every shared pool consumer. The #188 accepted
  limitations L1–L11 are listed under concurrency above.
- OAuth grants and grandfathered NULL-limit keys have **velocity bounds only**.
- The transport 429s — L1 and the transfer redemption — sit outside the in-band
  refusal contract.
- A hard kill loses at most one coalescing interval of refusal counts per key.
- Past 10,000 principals the shared overflow entries lose per-principal
  attribution but not their counts.
- **The defaults are guesses against a small sample** (~1,600 calls per 30
  days). Every one of them is a setting; read `/admin/performance` for a week
  before treating any as settled.
- A shared key is a shared bucket. Two agents on one credential contend,
  exactly as they do for the daily quota — an agent that deserves isolation
  deserves its own credential.
