## 1. Record the actor at call time

- [x] 1.1 Add `current_actor` to `src/auth/session.py`, beside `current_user_id` / `current_vault_root`, holding `(kind, label, ref)` for the request
- [x] 1.2 Bind it in `APIKeyMiddleware`'s API-key branch from the `APIKey` row already loaded (`name`, `key_prefix`); reset it with the other ContextVars in `finally`
- [x] 1.3 Fold `client_name` into the OAuth branch's token lookup (`outerjoin oauth_clients`, returning `(token, client_owner, client_name)`) so one statement feeds the cross-user check and the label and no path gains a query; keep the check itself guarded on an owned token
- [x] 1.4 Add `_actor_columns()` in `src/mcp_server/tools.py` and write it onto the `UsageLog` in `_log_usage`, truncating to the column widths so an over-long value cannot silently lose the whole row
- [x] 1.5 Retry the usage write once on `foreign_key_violation` with the credential FKs cleared and the label kept, dropping `user_id` only when its constraint was the one violated; walk the whole `orig`/`__cause__` chain for the SQLSTATE and the constraint name, and keep the broad `except` as the last resort

## 2. Schema

- [x] 2.1 Add `UsageLog.actor_kind` / `actor_label` / `actor_ref` (nullable `String(20)` / `String(255)` / `String(64)`) to `src/models/db.py`, each carrying 015's ownership marker as its column comment so `alembic check` compares it
- [x] 2.2 Write `alembic/versions/015_usage_log_actor.py`: `SET LOCAL` the timeouts, add the three columns and stamp each with the COMMENT marker, backfill from `api_keys` and from `oauth_tokens` → `oauth_clients` guarded on `actor_kind IS NULL`, `RESET` the timeouts
- [x] 2.3 Treat the three as one owned unit: all absent → create and mark; all present, exactly typed, nullable, default-free and marked → accept as a re-run; anything else → raise naming what was found. Refuse a row carrying a label beside a NULL `actor_kind`
- [x] 2.4 Downgrade drops only marked columns, all-or-nothing

## 3. Panel

- [x] 3.1 Select the actor columns in both `/admin/usage` queries
- [x] 3.2 Add `_usage_actor(row)`: gate on `actor_label` (not `actor_kind`, which would suppress a join that could still answer), join as the pre-015 fallback, an unrecognised kind rendered without asserting a credential type, `(None, None)` when neither resolves
- [x] 3.3 `usage.html` renders "unknown (credential deleted)" for an unattributable row
- [x] 3.4 `oauth.html`'s Delete confirm states the real blast radius and no longer promises a revocation; document the same in `delete_oauth_client`

## 4. Tests

- [x] 4.1 `tests/test_issue_77_usage_attribution.py`: `_log_usage` writes and truncates the columns; the FK retry's logic, including the doubly-wrapped exception chain and the message-text fallback; the middleware binds the label on both branches and resets it after a return, an exception and a cancellation; a refused (no-vault) call still records its actor; statement counts prove the label costs no query; `_usage_actor` precedence including the NULLed-`key_id` case and an unrecognised kind; rendered copy for both templates, plus a hostile label escaped; `/admin/usage` still scoped for a non-admin
- [x] 4.2 `tests/integration/test_schema_check.py`: nullable *and marked* columns on a fresh database, backfill of resolvable credentials, already-orphaned rows left NULL, the label surviving the panel's key-delete sequence and the client-delete cascade, re-run after `stamp 014` changing no label, refusals for a wrong type / partial set / `NOT NULL` / server default / unmarked set / orphan label, a marked set accepted, and both downgrade paths
- [x] 4.3 `tests/integration/test_usage_log_fk_recovery.py`: against a real database, a key and an OAuth client deleted between tool start and log still produce a labelled row with NULL FKs, and the driver's actual exception shape is what the recovery matches on
- [x] 4.4 `make test-schema` green with the new head

## 5. Documentation

- [x] 5.1 CLAUDE.md records that usage attribution is denormalised and why the delete was not weakened
