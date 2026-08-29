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
  failed, because the panel scopes a non-admin's page by `user_id`. The error
  arrives wrapped twice and the layers carry different things: SQLAlchemy's
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

  Both halves are `COALESCE(..., false)`: `params` is nullable and `->>` on a
  missing key yields NULL, so without them the negation used by the executed
  filter would be NULL and PostgreSQL would drop every ordinary row on the
  floor. The marker strings are mirrored from `tools.py` rather than imported,
  because #162's quota gate will import `usage_stats` from `tools.py` and an
  import the other way closes the cycle;
  `tests/test_issue_160_refusal_predicate.py` pins the two copies equal, and
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

Actor attribution on the slowest-requests table is the denormalised
`actor_*` columns first and the LEFT JOINs as the fallback — the same
`_usage_actor` reader `/admin/usage` uses, for the same reason (#77): resolving
by join alone renders "unknown" for precisely the credential an operator has
just deleted and come to investigate.
