## 1. Record the actor at call time

- [x] 1.1 Add `current_actor` to `src/auth/session.py`, beside `current_user_id` / `current_vault_root`, holding `(kind, label, ref)` for the request
- [x] 1.2 Bind it in `APIKeyMiddleware`'s API-key branch from the `APIKey` row already loaded (`name`, `key_prefix`); reset it with the other ContextVars in `finally`
- [x] 1.3 Widen the OAuth branch's existing `oauth_clients` lookup from `user_id` to `(user_id, client_name)` so one row feeds both the cross-user check and the label; keep the check itself guarded on an owned token
- [x] 1.4 Add `_actor_columns()` in `src/mcp_server/tools.py` and write it onto the `UsageLog` in `_log_usage`, truncating to the column widths so an over-long value cannot silently lose the whole row

## 2. Schema

- [x] 2.1 Add `UsageLog.actor_kind` / `actor_label` / `actor_ref` (nullable `String(20)` / `String(255)` / `String(64)`) to `src/models/db.py`
- [x] 2.2 Write `alembic/versions/015_usage_log_actor.py`: `SET LOCAL` the timeouts, add the three columns (verifying rather than adopting a pre-existing one), backfill from `api_keys` and from `oauth_tokens` → `oauth_clients` guarded on `actor_kind IS NULL`, `RESET` the timeouts
- [x] 2.3 Downgrade drops the three columns

## 3. Panel

- [x] 3.1 Select the actor columns in both `/admin/usage` queries
- [x] 3.2 Add `_usage_actor(row)`: recorded label first, join as the pre-015 fallback, `(None, None)` when neither resolves
- [x] 3.3 `usage.html` renders "unknown (credential deleted)" for an unattributable row
- [x] 3.4 `oauth.html`'s Delete confirm states the real blast radius and no longer promises a revocation; document the same in `delete_oauth_client`

## 4. Tests

- [x] 4.1 `tests/test_issue_77_usage_attribution.py`: `_log_usage` writes and truncates the columns; the middleware binds and resets the label on both branches; `_usage_actor` precedence including the NULLed-`key_id` case; rendered copy for both templates
- [x] 4.2 `tests/integration/test_schema_check.py`: nullable columns on a fresh database, backfill of resolvable credentials, already-orphaned rows left NULL, the label surviving the panel's key-delete sequence and the client-delete cascade, re-run after `stamp 014` changing no label, a wrong-shaped pre-existing column refused, and downgrade
- [x] 4.3 `make test-schema` green with the new head

## 5. Documentation

- [x] 5.1 CLAUDE.md records that usage attribution is denormalised and why the delete was not weakened
