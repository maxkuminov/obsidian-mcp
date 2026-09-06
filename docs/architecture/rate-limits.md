# Rate limits: principals, buckets, and the shape of a refusal

> Deep rationale extracted from `CLAUDE.md`. Read before touching
> `src/services/rate_limits.py`, `src/services/refusals.py`, the failed-auth
> budget in `src/mcp_server/auth.py`, the gate order in `_tracked`
> (`src/mcp_server/tools.py`), or any `MCP_RATE_LIMIT_*` /
> `MCP_AUTH_FAILURE_*` / `DEFAULT_DAILY_REQUEST_LIMIT` setting. The
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
| 1a | Request occupancy (#261) | `APIKeyMiddleware`, before auth session through ASGI completion | global / presented-bearer fingerprint | 32 / 4; default **shadow** | Enforcement: transport 429; shadow: observation only | bounded transport event; authenticated observations can accompany tool rows |
| 1b | Auth-session occupancy (#261) | around the middleware's own DB session only | global | 2; default **shadow** | Enforcement: transport 429 before opening a session | same transport observation channel |
| 2 | General velocity bucket | `_tracked`, first gate | principal | 120/min, burst 30 (`MCP_RATE_LIMIT_PER_MINUTE`, `MCP_RATE_LIMIT_BURST`) | in-band, sentinel line | `rate_limited`, scope `principal` — **coalesced** |
| 3 | Write velocity bucket | `_tracked` (write tools) **and** `PUT /transfer/upload` | principal | 60/min, burst 15 (`MCP_WRITE_RATE_LIMIT_PER_MINUTE`, `MCP_WRITE_RATE_LIMIT_BURST`) | in-band on a tool; **429** on the transfer route | `rate_limited`, scope `principal_write` — **coalesced** |
| 4 | Vault admission (#66) | `_tracked` | user | — | in-band | `no_vault_assigned` (and the three vault-root quarantine markers, #199) |
| 5a | Unencodable-argument screen (#149) | `_tracked` | argument | — | in-band | `argument_not_encodable` |
| 5b | Query length cap | `_tracked`, beside 5a | argument | 8,192 (`MAX_SEARCH_QUERY_CHARS`) | in-band | `argument_too_long` — **own row** |
| 5c | Atomic tool slots (#261) | `_tracked`, after argument screens and before quota | class / principal / tenant / global | one per class; principal 2 / tenant 3 / global 4; default **shadow** | Enforcement: in-band sentinel; shadow: call runs | `slot_timeout`, coalesced only for actual enforcement refusals |
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
  neither partial permits nor a DB connection.

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
  app and therefore wraps the mount, and is honoured only for peers inside the
  trusted private ranges (`--forwarded-allow-ips`). A budget keyed on a
  spoofable header is **worse than none**; `request-trust`'s restricted
  proxy-header requirement is the dependency.
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

## Limiter state is in-process, and `--workers 1` is part of the contract

All bucket, coalescing and failed-authentication state lives in the worker
process and is **not** persisted, replicated or shared. A restart begins with
every bucket full and every counter zero.

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

The original rate settings validate the following rules. #261 adds separate
class hierarchy/sum and pool-budget validation, described under concurrency
below; pool sizes remain 5 + 10, now shared constants consumed by the engine
and validation.

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
| `MCP_RATE_LIMIT_PER_MINUTE` | `120` | Sustained tool calls per minute per principal. Null (with the burst) disables the general bucket. |
| `MCP_RATE_LIMIT_BURST` | `30` | Capacity of the general bucket. |
| `MCP_WRITE_RATE_LIMIT_PER_MINUTE` | `60` | Sustained vault-mutating calls per minute per principal (the eight write tools plus `PUT /transfer/upload`). |
| `MCP_WRITE_RATE_LIMIT_BURST` | `15` | Capacity of the write bucket. |
| `MCP_LIMITER_MAX_TRACKED_PRINCIPALS` | `10000` | Principals holding their own limiter entry before the shared overflow entry. |
| `MCP_REFUSAL_LOG_INTERVAL_SECONDS` | `10` | How long one coalescing window stays open. |
| `DEFAULT_DAILY_REQUEST_LIMIT` | `5000` | Daily quota a **newly created** key receives when the caller does not say otherwise. Null creates unlimited keys. |
| `MAX_SEARCH_QUERY_CHARS` | `8192` | Module constant, not a setting: the longest `query` `keyword_search` / `semantic_search` accept. |

Every nullable one accepts an **empty value**, `null` or `none` as "off". Zero
is refused at boot.

## Concurrency: shadow first (#261)

`src/services/concurrency.py` owns one process controller. `off` bypasses only
concurrency controls; `shadow` is the default and observes real concurrent work
without adding waits, refusals or dropped usage rows. `enforce` is an explicit
operator configuration change, never an automatic promotion. Keep one worker.
The earlier rate-limit change deferred these controls because authentication,
rotating credentials and refusal logging each exposed holes in partial designs.

Shadow requires `MCP_CONCURRENCY_WAIT_SECONDS=0`. Its namespaced
`concurrency_shadow` metadata reports the zero-wait predicate against **observed
occupancy**, not a counterfactual replay: earlier calls which enforcement might
have rejected actually ran and influence later observations. Existing errors,
body outcomes, quota flags and request counts remain actual outcomes. Pressure
before a tool exists emits a bounded transport event, not an ownerless usage row.

The request envelope admits global+bearer-fingerprint dimensions together before
DB lookup. Its lease lasts until the whole downstream ASGI request finishes,
including GET/SSE streams, initialization and notifications. SHA-256 fingerprints
stay in bounded memory only and are erased when their final references drain.
The separate authentication permit encloses only the middleware session, including
cache warming and teardown. Even invalid-auth responses are sent **after** that
session and permit have closed. Auth/request enforcement misses return transport
429 with a backoff hint; they neither query credentials nor INSERT usage rows.

Every tool has one explicit class: embedding (`semantic_search`), vector
(`find_related`), write (the existing eight write-class tools), or other (the
remaining fifteen). Unknown registrations cannot silently become other. Tenant
ceilings share authenticated user IDs, including one stable NULL-owner identity;
principals use key IDs or OAuth grant IDs, so refresh cannot reset them. A tool
acquires class, tenant, principal and global counters in one non-awaiting
transition. A queued call holds none of those active permits. Eligible FIFO wakes
the oldest call that can acquire **all** dimensions, so a saturated class cannot
park global capacity. There is no per-tenant embedding reservation or starvation
SLA: one embedding slot cannot reserve work for N tenants.

Positive enforcement waiting has one monotonic deadline (maximum five seconds)
and bounded global/tenant/principal waiter counts. Zero wait means immediate
admission or refusal, not disabled control. Grants transfer ownership before
waking a waiter; cancellation before or after grant returns every captured lease.
Registry overflow is sticky while any overflow lease **or waiter** exists, even
if a dedicated entry becomes free. Unknown identities share that overflow entry
until it drains, preventing an active identity from obtaining fresh capacity.
Dedicated entries have a bounded reverse index, so arbitrary release order does
not cause quadratic registry scans. No active entry is evicted or migrated.

Default ceilings: requests 32/fingerprint 4, auth 2, tools 4/tenant 3/principal 2,
one per class; pending tools 32/tenant 4/principal 2; keyed registry 1024; writers
1 with 64 waiters and a maximum .25-second enforcement wait. Writer admission is
independent of authenticated principal so background/coalesced refusal flushes
cannot bypass it. Shadow writers neither wait nor drop rows because of this
control. Writer leases encompass the entire usage write including FK retry; the
logging integration releases its initial DB session before opening a retry.

`src/services/pool_budget.py` is the single source for pool size 5, overflow 10,
conservative maximum two simultaneous connections per tool and four headroom
connections. Settings validate `auth + 2*tools + writers + 4 <= 15`, the class sum
and hierarchy. This bounds the **configured MCP contribution in enforce mode**,
not universal pool availability: indexer/panel/OAuth/transfer consumers share the
pool and can consume that headroom. It is not reserved capacity. Future tool paths
with more than two overlapping connections require budget/review updates.

Lifespan explicitly installs a controller; requests never replace live state.
Shutdown refuses/wakes pending tool work, allows the final coalescer writer flush,
then closes writers before engine disposal. Captured leases release their own
controller even across explicit lifecycle/test resets. No asyncio primitives are
created at module import. Production saturation tests are prohibited; calibrate
shadow observations first and test enforcement only in isolated environments.

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
- **Refunding a token on a refusal.** #162's reasoning: a tool that always
  fails would be free.
- **Enforcing the query cap inside the search bodies.** It would be a
  post-body marker polluting the percentiles.
- **Fail-open or fail-closed on principal-registry overflow.** Above.

## Standing residuals

- Default shadow mode does not enforce concurrency ceilings. Explicit enforcement
  bounds MCP contributions only, not every shared pool consumer.
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
