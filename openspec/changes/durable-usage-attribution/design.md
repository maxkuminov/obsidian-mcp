# Design — durable usage attribution

## The decision: denormalise, do not weaken the delete

Two shapes were available for #77.

**Stop the delete from happening.** Replace `delete_oauth_client` with a
per-token revoke, or gate it behind a two-step revoke-then-delete flow like the
API keys have. Rejected. Per #64 a revoke that leaves the client row alive is
not durable: `_handle_refresh` resolves on `token_hash` + `token_type` +
`revoked`, so the client's ordinary 401-then-refresh cycle mints a fresh,
identically scoped pair. Making the operator's stop button weaker in order to
protect a log is the wrong trade in a product whose expensive failure is an
ineffective revocation. The delete stays exactly as destructive as it is.

**Stop the log from depending on the credential.** Taken. The actor is a fact
about a call that already happened; deriving it from a row that is allowed to
be deleted was the defect. Three nullable columns record it at write time.

This also repairs the API-key half in the same stroke, which no OAuth-only fix
would have.

## Where the label is captured

`APIKeyMiddleware`, not `_log_usage`.

`_log_usage` could have run its own `SELECT` against `api_keys` /
`oauth_tokens` → `oauth_clients` when writing the row. That costs one extra
query per tool call, and it reads the credential *after* the tool body ran —
seconds later, in a window where the credential can already be gone. The
middleware, by contrast, has the credential row in hand: the API-key branch
already loaded `APIKey`, and the OAuth branch already resolves the token.

The first draft got the OAuth half half-right: it widened the *cross-user
check*'s `oauth_clients` select to `(user_id, client_name)` and ran it
unconditionally — which added a round trip to the single-user and ownerless
paths, where that check is skipped and no client query happened at all. The
label now rides on the token lookup itself, which `outerjoin`s `oauth_clients`,
so every path issues exactly the statements it did before. `outerjoin` rather
than `join`: the FK makes a token without a client row impossible, and if that
ever stopped holding, an inner join would silently convert the token into a
401 — a different decision, made by accident.

The value travels in a ContextVar (`current_actor`) beside `current_user_id`
and `current_vault_root`, set and reset in the same `try`/`finally`, so it can
never label another request's log line.

`{}` rather than three explicit NULLs when the ContextVar is unset: a caller
outside a request — the transfer routes, the indexer, sandbox mode, tests —
leaves the columns at their database default and the row keeps exactly the
shape it had before 015.

## Why three columns and not one

`actor_label` alone would have to be `"nightly sync (omcp_a1b2c3)"`. That is a
rendering, not a record: a key named `audit (prod)` is not recoverable from
`audit (prod) (omcp_a1b2c3)`, and any consumer other than the one template has
to parse it back out. `actor_kind` additionally lets the panel render the two
kinds differently without re-deriving which FK was populated — which is the
thing that stops working.

`actor_ref` for an OAuth row is the `client_id` rather than the literal string
"OAuth" the panel used to print. Once the client row is gone the `client_id` is
the only stable handle the operator has for correlating with anything else.

## Denormalising the label is pointless if the row is discarded

The credential can also disappear *during* the call. A revoke-then-delete, or
an OAuth client delete, committing while a slow tool call is in flight leaves
`_log_usage` inserting a row that names a credential that no longer exists;
PostgreSQL raises `foreign_key_violation` and the blanket `except` around the
commit dropped the whole audit line — the call an operator investigating that
credential most wants to see, and the one whose durable attribution these
columns already carry.

So the write is retried **once**, and narrowly:

- **Only SQLSTATE 23503.** Any other failure is not fixed by clearing columns,
  so retrying it is a second chance to fail rather than a recovery.
- **Credential FKs cleared, label kept.** That is the same end state the
  panel's own key delete produces, so the reader already handles it.
- **`user_id` only when it was the violated constraint.** The panel scopes a
  non-admin's page by `user_id`; clearing it needlessly hides the row from the
  one person entitled to see it. When the constraint cannot be identified at
  all, all three go — losing the scoping beats losing the row.
- **The exception is wrapped twice, and the layers carry different things.**
  Measured, not assumed: SQLAlchemy's `.orig` is the asyncpg *dialect's* error
  and carries `sqlstate`; its `__cause__` is asyncpg's own error and is the
  only layer with `constraint_name`. The first draft read `orig.constraint_name`
  alone, found nothing, and would have silently degraded every recovery to the
  fail-safe branch — dropping `user_id` from every recovered row, forever, with
  every test still green. `tests/integration/test_usage_log_fk_recovery.py`
  pins the shape against a real database precisely because no fake would have
  found it.
- **The broad `except` stays last.** Usage logging must never fail a tool call
  that has already done its work.

## What the migration must not do

- **Invent a label.** A row is labelled from the credential its own FK points
  at, or not at all. A guess-by-`user_id` fallback would attribute a call to
  the wrong one of a user's several keys — worse than an admitted gap, because
  the column's whole value is that an operator can trust it while deciding
  whether a connector misbehaved.
- **Re-stamp.** Both `UPDATE`s are guarded on `actor_kind IS NULL`. Beyond
  idempotence under `alembic stamp 014 → upgrade head`, this is what keeps the
  label a snapshot: re-deriving it from the credential's present state would
  silently rewrite history every time a key is renamed.
- **Adopt a column it did not create.** 013's philosophy — verify or refuse,
  never guess — and the first draft applied it too weakly, checking only the
  varchar width, column by column. Three holes followed, and the columns are
  now one **owned unit** stamped with a COMMENT marker:
  - a *partial* set (only `actor_kind`) was silently completed, running the
    backfill against a guard column of unknown meaning — `actor_kind IS NULL`
    is what decides which rows get written;
  - `NOT NULL` and a server default passed, though neither is what 015 creates
    and nullability is the load-bearing half (a call that cannot name its actor
    must still be recorded);
  - matching type and width is a coincidence anyone could reproduce, so a
    hand-made `varchar(255)` of arbitrary text was adopted and rendered to an
    operator as an audit trail. The marker is the only evidence of authorship.

  The marker is declared on the model too, so `alembic check` compares it like
  any other column attribute — it cannot drift from the migration that keys on
  it without the schema gate going dirty.
- **Drop a column it did not create.** `downgrade()` removes only marked
  columns, and removes none of them if any is unmarked, so the decision is made
  for all three before any is touched.
- **Relabel a row somebody else wrote.** A row carrying a label beside a NULL
  `actor_kind` would be re-derived from the credential it currently points at.
  That is an error naming the rows, not a fix-up.
- **Become NOT NULL.** A call that cannot name its actor must still be
  recorded, and rows orphaned before 015 have nothing to backfill from. These
  columns are display and audit only; nothing authorizes against them.

## Locks

One `ADD COLUMN` on `usage_logs` (ACCESS EXCLUSIVE, held to COMMIT) and two
`UPDATE … FROM` statements reading `api_keys` / `oauth_tokens` /
`oauth_clients` (ACCESS SHARE). `usage_logs` is a child of all three, so the
order is child-then-parent — the direction 013 established and the direction
the app itself takes. `lock_timeout` / `statement_timeout` are set and `RESET`,
because alembic runs every pending revision in one transaction.

The backfill scans `usage_logs` once per statement. On a table large enough to
exceed the 60 s budget the migration aborts, the transaction rolls back, and
the deploy stops before the container is recreated — the same fail-fast
behaviour 013 and 014 chose, and re-runnable when quiet.

## Accepted limitations

- **Transfer-route log rows are unlabelled.** `src/transfer/routes.py::_log_row`
  builds its own `UsageLog` attributed to the *minting* identity, with no
  request-scoped actor to read. Those rows keep join-only attribution and go
  "unknown (credential deleted)" if the minting credential is deleted. Out of
  scope here; the fix is to carry the label on `transfer_tokens` at mint.
- **The confirm dialog interpolates nothing.** Jinja escapes an apostrophe to
  `&#39;`, the HTML parser turns it back into `'` before the JS string literal
  is parsed, and a client named `O'Brien` would break `onclick` — so the form
  would submit *unconfirmed*. The text is static for that reason.
