## Why

`/admin/usage` derived a historical fact from a live row, and the row is
allowed to disappear.

Every log line's actor was resolved by LEFT JOIN at read time — through
`api_keys` for a key, through `oauth_tokens` → `oauth_clients` for an OAuth
grant. Both joins go NULL on the operator's own most urgent path:

- **OAuth (#77).** `oauth.html` offered one click labelled *"Delete this client
  and revoke all its tokens?"* — a promise of revocation. `delete_oauth_client`
  does `session.delete(client)`; `oauth_tokens.client_id` is `ON DELETE
  CASCADE` and `usage_logs.oauth_token_id` is `ON DELETE SET NULL`. So an
  operator who suspects a connector misbehaved, clicks Delete to stop it, and
  then opens `/admin/usage` to review what it did is shown "unknown" for every
  row that connector produced. The evidence they opened the page to read was
  destroyed by the button they pressed to stop the client.
- **API keys (pre-existing, same mechanism).** `usage_logs.key_id` has no
  `ON DELETE` at all, so `delete_key_form` and `delete_all_revoked` explicitly
  `UPDATE usage_logs SET key_id = NULL` before deleting the key — otherwise the
  delete raises. Identical outcome. The real asymmetry between the two paths is
  wording and gating (a key must be revoked before it can be deleted; a client
  is one click), not whether history survives.

The blast radius that *is* wanted stays: deleting a client also cascades
`oauth_codes` and, via `transfer_tokens.oauth_token_id`, any outstanding
transfer capabilities minted under those tokens. Replacing the delete with a
per-token revoke would be a **weaker** stop — per #64 a client whose row still
exists can refresh its way back — so the delete stands and the copy is what
changes.

## What Changes

- **`usage_logs` carries the actor** (migration 015): `actor_kind`
  (`api_key` | `oauth`), `actor_label` (the key's name or the OAuth
  `client_name`) and `actor_ref` (the key's `omcp_` prefix or the `client_id`),
  all nullable. Name and identifier stay separate columns: joined into one
  string the row stops being a record.
- **Written at call time.** `APIKeyMiddleware` binds `current_actor` (a
  ContextVar beside `current_user_id` / `current_vault_root` in
  `src/auth/session.py`) from the credential row it has already loaded, so the
  label costs no extra query — the OAuth branch's token lookup `outerjoin`s
  `oauth_clients` and returns `(token, client_owner, client_name)` in one
  statement — and `_log_usage` writes it with the row. A label written at call
  time cannot be taken away by a later delete, and it is a snapshot, so
  renaming a key does not retroactively rename its history.
- **The row survives a credential deleted mid-call.** A slow call can outlive
  the credential it authenticated with, and the insert then raises
  `foreign_key_violation` — which a blanket `except` used to swallow, dropping
  the audit line for exactly the credential under investigation. `_log_usage`
  retries once with the credential FKs cleared and the label kept, dropping
  `user_id` only when that was the constraint that failed.
- **The three columns are one marked, owned unit.** 015 stamps each with a
  COMMENT marker, completes only a set that is all present, exactly typed,
  nullable, default-free and marked, and refuses every other combination
  (partial set, `NOT NULL`, foreign). `downgrade()` drops only marked columns,
  all-or-nothing. The marker is mirrored on the model so `alembic check`
  compares it.
- **Backfilled, never invented.** 015 labels every existing row whose
  credential still resolves, using the same join the panel performs. Rows whose
  credential is already gone stay NULL; there is no guess-by-`user_id`
  fallback, because two of a user's keys are different actors. Both statements
  are guarded on `actor_kind IS NULL`, so a re-run cannot rewrite history.
- **The panel prefers the denormalised label** and keeps the joins as the
  fallback for pre-015 rows. A row with neither renders
  "unknown (credential deleted)" rather than a bare "unknown" — the difference
  between a gap in the data and a gap in the audit trail.
- **The Delete confirm says what the delete does**: tokens deleted, transfer
  links minted under them stop working, usage history stays attributed.

Explicitly **not** changed: `usage_logs.key_id` still has no `ON DELETE`, and
the panel still NULLs it before deleting a key. The denormalised label is
required to survive exactly that, and a test pins it.

## Capabilities

### Modified Capabilities
- `mcp-request-routing`: every authenticated MCP tool call records a durable
  actor label alongside its `usage_logs` row.
- `oauth-authorization-integrity`: deleting an OAuth client preserves its usage
  attribution, and the confirmation states the real blast radius.

## Impact

- `alembic/versions/015_usage_log_actor.py` — new
- `src/models/db.py` — `UsageLog.actor_kind` / `actor_label` / `actor_ref`,
  each carrying 015's ownership marker as its column comment
- `src/auth/session.py` — `current_actor` ContextVar
- `src/mcp_server/auth.py` — bind the label on both auth branches; the OAuth
  token lookup joins `oauth_clients` so no path gains a query
- `src/mcp_server/tools.py` — `_actor_columns()` on the `_log_usage` write, and
  the foreign-key-violation retry around it
- `src/control_panel/routes.py` — `_usage_actor()`, the `/admin/usage` select,
  `delete_oauth_client` docstring
- `src/control_panel/templates/usage.html`, `oauth.html` — rendered copy
- `tests/test_issue_77_usage_attribution.py` and
  `tests/integration/test_usage_log_fk_recovery.py` — new; fourteen cases added
  to `tests/integration/test_schema_check.py`

Carries a migration, so `make test-schema` is a required gate and `make
db-check` must report "No new upgrade operations detected" after deploy.
