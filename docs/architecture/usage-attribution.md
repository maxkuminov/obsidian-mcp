# Usage attribution

> Deep rationale extracted from `CLAUDE.md`. Read before touching `usage_logs`, `_log_usage`, or the actor columns on `transfer_tokens`.

## Usage attribution is denormalised, because the credential can be deleted

`usage_logs` carries `actor_kind` (`api_key` | `oauth`), `actor_label` (the
key's name or the OAuth `client_name`) and `actor_ref` (the key's `omcp_`
prefix or the `client_id`) — migration 015, all nullable, all written at call
time. `/admin/usage` renders them and keeps its LEFT JOINs only as the
fallback for rows written before 015.

The join alone was the bug (#77). Both FK columns are allowed to lose their
target while the log row stays, and both do so on the operator's most urgent
path: `usage_logs.oauth_token_id` is `ON DELETE SET NULL` and
`oauth_tokens.client_id` is `ON DELETE CASCADE`, so deleting an OAuth client
unattributed every line it had produced; `usage_logs.key_id` has **no
`ON DELETE` at all**, so the panel `UPDATE usage_logs SET key_id = NULL` before
deleting a key, with the same effect. An operator who stops a suspect
credential and then opens the Usage page to see what it did was shown
"unknown" for exactly the rows they came to read.

- **The label is bound by `APIKeyMiddleware`, not looked up by `_log_usage`.**
  `current_actor` (a ContextVar beside `current_user_id` / `current_vault_root`
  in `src/auth/session.py`) is set from the credential row the middleware has
  already loaded, and it is read *before* the tool runs rather than seconds
  later when the credential may be gone. The API-key branch has the `APIKey`;
  the OAuth branch gets `client_name` from **the token lookup itself**, which
  `outerjoin`s `oauth_clients` and returns `(token, client_owner,
  client_name)` — one statement feeding the cross-user check and the label, so
  an ownerless OAuth request still issues exactly one query, as it did before.
  Do not add a second `oauth_clients` select; that is a round trip on the
  hottest path in the server. `_actor_columns()` returns `{}` when the
  ContextVar is unset, so a writer outside a request keeps the pre-015 row
  shape.
- **A dangling FK must not take the row down with it.** A tool call can outlive
  its own credential — the operator deletes the key or the client while a slow
  call is running — and the insert then names a row that is gone. `_log_usage`
  catches **only** `foreign_key_violation` (SQLSTATE 23503), rolls back and
  retries **once** with `key_id`/`oauth_token_id` cleared and `actor_*` kept;
  that is the same end state the panel's own key delete produces, so the reader
  already handles it. `user_id` is dropped only when it is the constraint that
  failed, because the panel scopes a non-admin's page by `user_id`. Since #193
  it also **returns whether the row landed** — `True` inserted, `False` gave
  up. Swallowing every failure is still right for the success path (a call that
  has already written to disk must not be failed by its own bookkeeping), but
  the one caller that has to *report* the audit could not otherwise tell:
  `_tracked`'s exception handler emits `tool_usage_log_failed` on `False`, so
  the log distinguishes "the tool failed and here is its row" from "the tool
  failed and the row is missing". Every other caller ignores the value. The
  error arrives wrapped twice and the layers carry different things: SQLAlchemy's
  `.orig` is the asyncpg *dialect's* error (SQLSTATE, no constraint name),
  whose `__cause__` is asyncpg's own (constraint name). `_error_chain` walks
  both and falls back to the message text — reading `orig.constraint_name`
  alone finds nothing and silently degrades every recovery to "assume it was
  `user_id`". An unresolvable name deliberately clears all three: losing the
  scoping beats losing the row. The broad `except` stays last: usage logging
  must never fail a call that already did its work.
- **It is a snapshot, not a view.** 015's backfill is guarded on
  `actor_kind IS NULL` and so is any re-run. Re-deriving the label from the
  credential's present state would rewrite history on every rename. A row
  carrying a label beside a NULL `actor_kind` is therefore an *error*, not
  something to fix up: the guard would relabel it from whatever credential it
  points at now, overwriting an attribution 015 did not write.
- **The three columns are one owned unit, and the COMMENT marker is what owns
  them.** 015 creates all three and stamps each with
  `denormalised actor, written at call time (015_usage_log_actor)`; on a
  re-run it completes only a set that is all present, exactly typed, nullable,
  default-free **and marked**, and refuses anything else (a partial set, a
  `NOT NULL` column, a foreign one) naming what it found. `downgrade()` drops
  only marked columns, all-or-nothing. Type and width are a coincidence anyone
  could reproduce; the marker is the only evidence that *this* scheme wrote the
  values, which is the whole basis for showing them to an operator as an audit
  trail. The same string is declared on the model columns
  (`UsageLog._ACTOR_COLUMN_MARKER`) so `alembic check` compares it — keep the
  two byte-identical or the check goes dirty.
- **Nothing is invented.** 015 labels a row from the credential its own FK
  points at, or leaves it NULL — no guess-by-`user_id`, because two of a
  user's keys are different actors. A NULL row renders
  "unknown (credential deleted)", which is a gap in the audit trail rather
  than a gap in the data, and says so.
- **The OAuth Delete was not weakened to protect the log.** Replacing it with
  a per-token revoke is a *worse* stop — per #64 a client whose row survives
  refreshes its way back — so the delete keeps all four cascades
  (`oauth_tokens`, `oauth_codes`, `transfer_tokens`, and the `SET NULL` on
  `usage_logs.oauth_token_id`) and the confirm text changed instead: it now
  states that the tokens are deleted, that transfer links minted under them
  stop working, and that the usage history stays attributed. **Do not
  interpolate `client_name` into that `confirm()`** — Jinja escapes an
  apostrophe to `&#39;`, the HTML parser restores it before the JS string is
  parsed, and the `onclick` throws, which submits the form *unconfirmed*.
- **`usage_logs.key_id` still has no `ON DELETE`, deliberately.** That is
  unchanged here and the panel still NULLs it by hand; the whole point is that
  the label survives it, which
  `tests/integration/test_schema_check.py::test_the_label_survives_the_panel_deleting_an_api_key`
  runs as the real two-statement sequence.
- **Transfer rows carry the actor from mint** (migration 017, the 015 register:
  marker-owned nullable columns on `transfer_tokens`, snapshot never re-derived,
  orphan-label refusal before any backfill). `mint_token` splices in the actor
  `APIKeyMiddleware` already bound — one shared reader,
  `src.auth.session.actor_columns`, so mint and `_log_usage` cannot drift in
  truncation — and `_log_row` copies it at redemption. The backfill labels
  `transfer_tokens` only, from the row's own FK; it writes nothing to
  `usage_logs`, because no usage row references the token that produced it.
  The honest gap is rows written between 015 and 017: they keep join-only
  attribution and render as unattributable when the joins miss. The label
  authorises nothing — redemption still resolves the credential row.
- **A transfer token names at most one minting credential**, and since 017 the
  database says so: `ck_transfer_tokens_one_credential`
  (`key_id IS NULL OR oauth_token_id IS NULL`), created and marked by 017 and
  resolved through `pg_constraint` — a same-named `CHECK (true)` would satisfy
  a lookup by name. Both NULL stays legal; that is the single-user and sandbox
  shape. It exists because nothing in a two-credential row records *which* of
  them minted it, so the API-key backfill would have labelled such a row purely
  by running first and the OAuth statement's `actor_kind IS NULL` guard would
  then have skipped the row it had just mislabelled — an invented attribution,
  rendered to an operator as an audit trail. 017 refuses such rows by id before
  either backfill, and `transfer.Identity.__post_init__` refuses the same state
  in the app. Unreachable today (`APIKeyMiddleware` clears both ContextVars and
  fills one branch), which is why it is asserted rather than assumed.
- **The deploy window is declared, not closed.** The deploy migrates and *then*
  recreates, so a mint served by the old code between 017's backfill and the
  recreate inserts a row with all three actor columns NULL. That is the tail of
  the same 015→017 gap: it renders through the join fallback like every pre-017
  row and degrades only if the credential is later deleted. Symmetric with
  016's treatment (NULL record ⇒ re-derive; NULL label ⇒ join fallback). No
  barrier and no quiesce: the window is seconds long and the fallback works.


## The read-only consumer (#160)

`/admin/performance` aggregates `usage_logs` and writes nothing. The SQL lives
in `src/services/usage_stats.py`; two things about it are load-bearing.

- **One predicate for "the body did not run", enumerated, never broad.**
  `_tracked` refuses some calls *before* the tool body executes and logs the
  refusal like any other call — same tool, same actor, a `duration_ms` of
  roughly zero, a `response_size` of the refusal string. Those rows are real
  history and stay in the log, but folding them into a latency percentile is how
  a tool that is refusing five thousand times an hour comes out looking fast. So
  the aggregates filter on `pre_body_refusal_sql()` and the per-tool refusal
  count uses **the same expression**: one helper, two consumers, no drift. Two
  hand-written predicates agree until somebody adds a third marker to one of
  them.

  It matches exactly three things and nothing else: `over_quota: true` (the
  quota gate, #162, declared here ahead of the gate that writes it so the two
  land as one contract), `error = 'no_vault_assigned'`, and
  `error = 'argument_not_encodable'`. A broad match — `params ? 'error'`, or
  `params->>'error' IS NOT NULL` — is wrong in a way that is invisible on the
  page: `_VAULT_REASSIGNED_MARKER`, `_CONFIRMATION_UNAVAILABLE_MARKER` and
  `_ANCHOR_LOST_AT_PUBLISH_MARKER` are written by tools whose bodies *ran*,
  resolved a vault, did the work and then refused to publish. Excluding those
  hides the slowest write path in the server from the one view built to find
  slow paths. **Every marker added to `_tracked` has to be classified
  deliberately** — pre-body or not — and only the pre-body ones belong in the
  predicate.

  **The classification rule, stated once: a marker belongs to exactly one side
  of the body/no-body line, and two branches on opposite sides may never share
  a value.** `vault_anchor_lost_at_publish` exists because that rule was broken
  once. `_confirmed_publication`'s `VaultAnchorUnavailable` branch logged
  `no_vault_assigned` — reasonable-looking, since both mean "no usable vault
  root" — but the admission gate writes that value *before* any body runs while
  this branch is reached from inside a mutating tool that has already resolved
  a root, read the note and computed the write. Filed under the pre-body value
  (reachable through the #88 race) the most expensive refusal the server logs
  was silently absent from every percentile. When a new refusal reads like an
  existing marker, check which side of the line it is on before reusing the
  string; a new value costs nothing and a shared one mis-measures in a
  direction nobody can see from the page.

  **The register, as of #192/#193.** Pre-body: `no_vault_assigned`,
  `argument_not_encodable`, and the boolean `over_quota` (#162). Post-body:
  `vault_assignment_changed`, `vault_confirmation_unavailable`,
  `vault_anchor_lost_at_publish`, `find_related`'s two operational
  failures, `related_source_not_found` and `related_source_not_embedded`, and
  the two the security-event change added — `permission_denied` (#192) and
  `tool_exception` (#193).
  `related_source_not_found` and `related_source_not_embedded` were classified
  by the rule above and land on the post-body side: the body ran, resolved the vault and queried the database before
  either branch could be reached, so enumerating them as refusals would drop a
  real database round trip out of the percentiles. They are two values rather
  than one because they are different facts with different fixes — a caller
  naming a note that does not exist, and the embed pass not having reached a
  note that does. They exist at all because those branches used to log
  nothing: a `find_related` that never got as far as looking was
  indistinguishable in the log from one that looked and found nothing, and
  `/admin/search-analytics` reads the second as the signature fact about what
  a vault does not hold. The marker is what keeps the first out of that count.

  **`permission_denied` and `tool_exception` are post-body, and that is a
  decision, not an oversight.** Both are written by `_tracked`'s own code
  rather than by a tool body, which is exactly the shape that reads like a
  pre-body refusal, so the reasoning is recorded here rather than re-derived:

  * `permission_denied` is recorded at `_require_write`'s single definition, so
    all nine gated call sites inherit it — and `_require_write` is called from
    *inside* a tool body that has already passed the vault gate, the argument
    screen and the quota gate, and has already **spent its quota slot**. A
    refusal that consumed a slot and ran three gates did not have "no body".
    The accepted cost, stated because it is visible on the page: a read-only
    credential probing `create_note` five thousand times contributes five
    thousand near-zero rows to that tool's percentiles (residual R5). The
    honest fix is to move the write gate up into `_tracked`, which changes
    quota accounting and refusal ordering for nine tools; instead the refusal
    was made *visible* on `/admin/usage`, where an operator can see a
    read-only credential apparently writing for what it is.
  * `tool_exception` is by definition a body that ran — it is written by the
    handler that guards `await fn(...)` and nothing else. A tool that raises
    after eight seconds of I/O is the slowest path there is, and the one view
    built to find slow paths must see it. It also **wins over any post-body
    marker the body recorded before raising**: a `find_related` that recorded
    `related_source_not_found` and then raised is logged as `tool_exception`,
    because the exception is the outcome.

  Neither is in `PRE_BODY_REFUSAL_ERROR_MARKERS`, and adding either is not a
  tuning knob: it would drop those rows out of every percentile and move them
  into the refusal count, silently.

  **`error_type` is a reserved `params` key of type string** — the exception's
  class name, written only beside `error = 'tool_exception'`. It is reserved
  for the same reason `embed_ms`, `db_ms` and `over_quota` are: a reader reads
  it. Unlike those three, **no reader casts it** — `/admin/usage` selects it as
  text and renders it, so a row carrying nonsense there degrades to a nonsense
  label rather than a 500. It is still a class name and nothing else: a writer
  with something else to say about a failure uses a different key. Note that
  `error` (the marker) and `error_type` (the class) are two keys, never one —
  the marker is a closed vocabulary this register enumerates, and the class
  name is not.

  **In-band refusals outside this register stay unmarked** (residual R1).
  `create_note` on an existing path, a path-validation refusal, a size refusal
  and a write conflict each return a message and write an ordinary row, exactly
  as they did before #192. That is deliberate: a typed outcome for every tool
  return is a change of its own, and half-marking the set would leave the
  register looking complete when it is not.

  Both halves are `COALESCE(..., false)`: `params` is nullable and `->>` on a
  missing key yields NULL, so without them the negation used by the executed
  filter would be NULL and PostgreSQL would drop every ordinary row on the
  floor. The marker strings are mirrored from `tools.py` rather than imported,
  because #162's quota gate imports `usage_stats` from `tools.py` and an
  import the other way closes the cycle. `over_quota` therefore travels in the
  *other* direction — imported by the writer from `usage_stats` — and is the
  one marker in the register with a single definition rather than a pinned
  pair; `tests/test_issue_160_refusal_predicate.py` pins the two copies equal, and
  `tests/integration/test_issue_160_performance_pg.py` seeds a row carrying
  some *other* `params.error` value and asserts it stays in the aggregates.

- **Phase timings are read where they exist, never zeroed.** `embed_ms` and
  `db_ms` come from `src/services/timing.py` and only search tools record them.
  The breakdown filters on `(params->>'embed_ms') IS NOT NULL` rather than
  `COALESCE(..., 0)`: treating a missing key as zero would drag every mean
  toward the number of tools that do not measure, which is not a fact about
  anything.

  **The casts are unguarded, and that is a constraint on future writers.** The
  page evaluates `(params->>'embed_ms')::double precision` and
  `(params->>'over_quota')::boolean` on every row of the window that carries the
  key. PostgreSQL raises on a value it cannot cast and the raise is not caught,
  so a single row whose `params` carries `embed_ms: "fast"` or
  `over_quota: "yes"` takes `/admin/performance` down with a 500 for the whole
  window that row falls in — for every user, until it ages out. Hence the rule:
  **`params` keys named `embed_ms`, `db_ms` and `over_quota` are reserved, and
  anything logging them must log a number, a number, and a real boolean.** A
  writer that wants to record something else about a phase gives it a different
  key. (`src/services/timing.py` is the only writer today and it records floats;
  the quota gate #162 must record a JSON boolean, not the string `"true"`.)

The page's one non-`usage_logs` source is the "Recent passes" card, which reads
`indexer_runs` (written by the indexer, see
[indexing and embeddings](indexing-and-embeddings.md)). Its owner column is
**joined live** from `users`, which is the opposite of the rule below and
deliberately so: an actor is a historical fact about who made a call, so it is
denormalised and survives the credential; an owner is a live fact about who a
row belongs to, the FK is `ON DELETE SET NULL`, and a stored label would go on
asserting whose vault a pass indexed after that user no longer exists.

Actor attribution on the slowest-requests table is the denormalised
`actor_*` columns first and the LEFT JOINs as the fallback — the same
`_usage_actor` reader `/admin/usage` uses, for the same reason (#77): resolving
by join alone renders "unknown" for precisely the credential an operator has
just deleted and come to investigate.


## The per-key daily quota (#162)

`api_keys.daily_request_limit` is a nullable integer: NULL is unlimited and is
what every key carries until an operator sets one. Enforcement lives in
`_tracked`, the same decorator the vault gate and the unencodable-argument
screen live in, and the code is `src/services/quotas.py`.

- **The counter row is the lock, and that is the whole design.** Migration 020
  adds `quota_counters(key_id, day, count)` with the composite PK `(key_id,
  day)` and an `ON DELETE CASCADE` FK to `api_keys`. Admission is one
  statement — `INSERT … VALUES (:k, :d, 1) ON CONFLICT (key_id, day) DO UPDATE
  SET count = quota_counters.count + 1 WHERE quota_counters.count < :limit
  RETURNING count`. A returned row admits; no row refuses. PostgreSQL evaluates
  that `WHERE` while holding the conflicting row's lock, so exactly `limit`
  calls are admitted per UTC day however many arrive at once. A
  `COUNT`-then-decide over `usage_logs` passes a sequential boundary test and
  fails a concurrent one — two calls both read "99 used, limit 100" and both
  run — which is why
  `tests/integration/test_issue_162_quotas_pg.py::test_exactly_n_tool_bodies_run_under_more_than_n_concurrent_calls`
  releases twenty tasks from one barrier onto a pool wide enough to hold them
  and counts *tool body executions*, not admissions. The composite PK is
  load-bearing rather than tidy: it is the arbiter `ON CONFLICT` names, and a
  single-column PK of the same name is a quota that never resets.
- **The gate is the last pre-body gate.** It runs after credential/vault
  resolution and after the argument screen, so a call refused for having no
  vault or for carrying an unpaired surrogate consumes nothing — a slot spent
  on a call that was never going to run is a slot an operator cannot account
  for. It commits in its **own** transaction, on its own pooled connection,
  released before the tool body starts: holding a connection across the body
  would turn a five-connection pool into a five-concurrent-call server.
- **Refusals never consume; admitted failures do.** The guarded UPDATE declines
  at the ceiling, so an agent looping on the refusal cannot push the number
  past it, and the next UTC day admits exactly `limit` again. An admitted call
  whose body then raises has already spent its slot and nothing gives it back.
  Both directions are deliberate: incrementing on completion would admit
  unboundedly many concurrent calls, and refunding a failure makes a tool that
  always fails free.
- **The marker is imported, not mirrored.** `over_quota` is the one `params`
  key that is a JSON **boolean**, and `tools.py` takes it from
  `src.services.usage_stats.OVER_QUOTA_PARAM` rather than declaring a second
  copy. That is the direction the import can run — `usage_stats` mirrors *its*
  two string markers from `tools.py` because the reverse would close a cycle —
  and it means the writer and the pre-body refusal predicate cannot disagree
  about the key's name at all. `usage_stats` enumerated it ahead of this gate
  shipping so the two would land as one contract. It must stay a real boolean:
  the performance page's cast is unguarded (see the reserved-keys rule above),
  and a row carrying the string `"true"` takes `/admin/performance` down with a
  500 for every user until it ages out.
- **The day is UTC, computed in Python and bound as a parameter** — never
  `now()::date`, which is the server's timezone. A limit that resets at an hour
  nobody administering it can name is not a limit anybody can reason about, and
  the refusal quotes the next UTC midnight verbatim so an agent can back off
  rather than spin.
- **Consumption is admissions since the limit was enabled, not requests since
  midnight.** The NULL-to-limited transition deletes the key's current-day
  counter row in the same transaction as the limit write; a limited-to-limited
  change keeps it. So a key that made forty calls this morning with no limit set
  reads 0/100 the moment a limit of 100 is set, and the keys page says so. The
  alternative charges an operator for traffic that was explicitly unlimited when
  it happened, which is the surprise that gets a limit turned off again. Raising
  a limit keeping the count is the same rule from the other side: those calls
  *were* admitted under a quota, and forgiving them would make "lower the limit"
  a way to grant more calls.
- **A key with no limit issues no quota statement at all.** The ceiling rides on
  the request as `current_daily_request_limit`, bound by `APIKeyMiddleware` from
  the `APIKey` row it has already loaded — the same "the row is in hand, do not
  add a round trip" rule the actor label follows. So the common case is two
  ContextVar reads and no SQL, and there is no query to regress on rather than a
  query whose result happens to be permissive.
  `tests/test_issue_162_quota_gate.py` counts statements, because nothing on any
  page would reveal that property going wrong.
- **OAuth is exempt by construction, not by a list.** The OAuth branch never
  sets the limit, so the default None stands; any future credential kind is
  exempt the same way until somebody deliberately binds a ceiling for it. v1
  exempts OAuth because panel OAuth is the operator, and an operator locked out
  by a ceiling they set on themselves cannot raise it.
- **A database failure is not silently "unlimited", and not silently anything
  else either.** If the admission statement raises, the exception is logged
  (`quota_admission_failed`, with the key id, the accounting day and the
  exception type) and re-raised, so the call does not run. Swallowing it would
  mean a database blip quietly disables every configured ceiling, which is the
  one failure mode nobody would notice; keys with no limit never reach the code,
  so nothing that was working before can start failing because of it. The log
  line matters because the raise happens *before* `_log_usage` — an enforcement
  outage would otherwise leave no record anywhere naming the key it happened to.
- **The refusal's reset instant comes from the admission, never from a second
  clock read.** `admit()` reads the clock once to pick the accounting day and
  returns it on the `Admission`; `quota_refusal_message` is handed
  `Admission.reset_at` and recomputes nothing. A refusal that straddles UTC
  midnight otherwise decides against day D and then describes day D+1's
  midnight as though it were D+2's, telling an obedient agent to back off for
  nearly two days when its quota was milliseconds from resetting — a
  self-inflicted outage produced entirely by the one non-deterministic thing on
  the path. `reset_instant` therefore takes a **date**, not a clock reading, so
  the mistake cannot be reintroduced by passing `now`.
- **Only tool calls consume quota; `/transfer/*` redemptions do not.** The
  public transfer routes are served by `src/transfer/`, not by an MCP tool, so
  they never pass through `_tracked` and never touch the counter. What consumes
  the slot is the **mint** — `request_upload`, `request_download` and
  `import_from_url` are ordinary `_tracked` tools — so a key that reaches its
  ceiling can still complete uploads and downloads for capabilities it minted
  earlier, and cannot mint new ones. That is the intended shape: a capability is
  a promise already made, and revoking it belongs to the transfer token's own
  expiry and to key revocation, not to a daily request ceiling. It does mean the
  quota bounds requests to the *server*, not bytes over the wire.
- **Zero is refused at both layers.** `daily_request_limit` is constrained to
  NULL or 1..1,000,000 by `ck_api_keys_daily_request_limit` and by the panel's
  own validation. Zero would make the guarded UPDATE decline every call forever,
  and a key that refuses everything reads to its operator as an outage rather
  than as a setting — revoking the key is the way to stop it. Both layers,
  because a constraint is what makes the invariant true of the data and a
  message is what makes it fixable.
- **Counter rows are pruned opportunistically**, by the admission whose
  `RETURNING count` is 1 — the INSERT branch, i.e. the first call of a new UTC
  day for that key. The **trigger is per-key; the scope is global**: that one
  call deletes *every* key's rows older than the cutoff, not just its own,
  which is the only reason the table stays bounded — what accumulates is rows
  belonging to keys nobody is calling any more, and a per-key prune is by
  construction never run for those keys again. At most once per key per day,
  never on the contended path, and its failure is swallowed: a call that has
  already been admitted must not be turned into an error by housekeeping. The
  accepted cost is that the request which happens to be a key's first of the
  day pays for the housekeeping, before `admit()` returns — one indexed DELETE
  over a table holding at most one row per key per retained day, rather than a
  background scheduler this server does not otherwise need.


## The filtered usage views (#162)

`/admin/usage` reads through `src/services/usage_filters.py`, a peer of
`usage_stats.py` rather than part of it: that module answers the performance
page's question and has its own load-bearing rule about which rows count as
executed. They share the window vocabulary by import, because two lists of
selectable windows is how two pages start disagreeing about what "7d" means.

- **The owner scope is not a filter.** `user_id` appears twice and the two are
  different things. The scope is the viewer's tenancy — NULL for an admin, their
  id otherwise — and it is applied unconditionally, after everything the client
  sent; the `user=` filter is only an admin's choice of whose rows to read and is
  not offered to a scoped viewer at all. A non-admin cannot widen their view by
  editing the query string, which
  `tests/test_issue_77_usage_attribution.py` and the PG module both pin.
- **Unknown filter values clamp to "no filter"**, never to a 422 and never to a
  value passed through to SQL — the same treatment `normalize_window` gives an
  unknown window, for the same reason: the selectors are links and a stale
  bookmark should render. The fallback is always the *less* specific view, and
  the scope clause is applied on top of it either way.
- **Deleted actors keep their line.** The per-actor totals group by the
  denormalised `actor_*` columns and fall back to the LEFT JOINs, which is
  `_usage_actor`'s rule verbatim: a credential the operator has just deleted and
  come to investigate must not render as "unknown" (#77). The key selector can
  only offer keys that still exist, so the unfiltered view is where that history
  lives, and that is stated in the module rather than left to be discovered.
- **`ix_usage_logs_key_id_created_at`** (migration 020) is what makes the
  per-key filter a range scan instead of a window scan that discards most of
  what it reads.


## Search result telemetry (#161)

The search tools record three more `params` keys — `result_count`,
`result_paths` and (`find_related` only) `source_path` — through the same
`timing` holder the phase timings ride on. The contract, the byte budget and
why the budget is enforced at the record site rather than downstream are
written down in [search](search.md); what belongs here is what they mean for
this table.

- **They are reserved keys with declared types**, for the reason `embed_ms`,
  `db_ms` and `over_quota` are: a reader casts and unnests them.
  `result_count` is an int, `result_paths` a JSON array of strings,
  `source_path` a string. A writer with something else to say about a result
  set uses a different key.
- **The reader guards its casts anyway.** The performance page's casts are
  unguarded and that is a standing constraint on future writers; the analytics
  page took the other half of the deal as well — it tests
  `params->>'result_count'` against an integer pattern and
  `jsonb_typeof(params->'result_paths') = 'array'` before reading either. Both
  belts, because the failure mode is a 500 on the whole window for every user
  until the bad row ages out, and the search keys are written on the hottest
  read path in the server.
- **`_truncate_params` does not see them.** `_tracked` truncates the *named*
  arguments and then merges the holder over the top. That is why the paths
  value carries its own 10-path / 2048-byte bound, and why `find_related`'s
  grouping key is the separate `source_path` and not the named `path`, which
  is cut at 200 characters and would collapse distinct long paths onto one row.

**The identity rule for anything reading these keys: `(usage_logs.user_id,
path)`, matched NULL-safely.** A path is unique only within an owner —
`uq_notes_metadata_user_id_file_path` says so in the schema, with
`NULLS NOT DISTINCT` — so `Daily/2026-08-29.md` names a different note in each
user's vault. A grouping or coverage join on the path alone would merge two
tenants' analytics into one row and tell each of them things about the other's
vault. `IS NOT DISTINCT FROM` rather than `=` because both sides are nullable
and both are NULL in exactly the shape that matters most: single-user mode
writes NULL on every log row and every note, and `usage_logs.user_id` is
`ON DELETE SET NULL`, so a deleted user's rows join the ownerless slice rather
than vanishing. That is the honest reading — those rows *are* now unattributed
— and it is the same treatment the ownerless indexer passes get.
