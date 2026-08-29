## Context

`usage_logs` carries `key_id`, `oauth_token_id`, `user_id`, denormalized actor labels (issue #77), and a `created_at` index. `_tracked` resolves the caller's credentials/vault root before tool bodies run — the natural enforcement point. Panel key CRUD lives in `src/api`/`src/control_panel`; api_keys rows carry the display prefix and hash.

## Goals / Non-Goals

**Goals:** filterable usage, per-actor totals, opt-in per-key daily ceiling that fails closed per-call but never blocks other actors.
**Non-Goals:** no rate-limiting (req/s), no OAuth quotas in v1, no billing semantics, no retention changes, no per-tool quotas.

## Decisions

1. **Enforcement in `_tracked`, after auth, before the tool body, via an atomic counter row.** A plain COUNT-then-execute is raceable (two concurrent calls both see one remaining slot). Instead migration 020 adds `quota_counters(key_id, day, count)` (PK `(key_id, day)`, `day` = UTC date, `key_id` FK to `api_keys.id` ON DELETE CASCADE so deleting a key removes its counters) and admission is one atomic statement: `INSERT INTO quota_counters (key_id, day, count) VALUES (:k, :d, 1) ON CONFLICT (key_id, day) DO UPDATE SET count = quota_counters.count + 1 WHERE quota_counters.count < :limit RETURNING count` — a returned row admits the call; no row means the cap is reached and the call is refused before the tool body. The admission statement runs after every existing pre-body gate (so no-vault or unencodable-argument refusals never consume a slot) and commits in its own short transaction before the body starts (so an admitted body that later raises has still consumed its slot — never returned). Refusals never increment (the guarded UPDATE declines), so exactly `limit` calls are admitted per UTC day regardless of concurrency; a concurrent boundary test proves it. Rows older than two days are pruned opportunistically on insert. The refusal is still logged to `usage_logs` with the over-quota marker so pressure is visible. Alternatives rejected: COUNT-then-act (raceable), in-memory counters (lost on restart, wrong across workers), advisory locks around a COUNT (serializes all of a key's calls through the lock).
2. **UTC day boundary,** documented in the key form copy. `daily_request_limit` is a nullable integer with a DB CHECK constraint: NULL = unlimited (default, no behavior change), otherwise 1..1000000; zero and negatives are rejected by both server-side validation (flash error) and the constraint — disabling a key is what revoke is for.
2b. **Over-quota marker shape (single definition):** the refusal row's params carry `"over_quota": true` (JSON boolean), written and read through one shared constant/helper; the NULL-safe exclusion predicate for consumers (perf aggregates, displays) is `COALESCE((params->>'over_quota')::boolean, false)`. Quota *display* reads the counter row, not a COUNT over usage_logs.
3. **Quota display on the keys page** (today's count / limit) reads the key's `quota_counters` row for the current UTC day (0 when absent — consumption is defined as admissions since the limit was enabled, because unlimited keys perform no quota accounting; the UI labels it so); usage page totals reuse the filter query.
4. **Filters are WHERE-clause composition** on the existing usage query, validated against known ids; the denormalized actor columns keep deleted-key history visible under "all".

## Risks / Trade-offs

- [an extra statement per call adds latency] → one indexed upsert on a two-column PK, only for keys with a limit set; measured against the perf views of #160.
- [Two clients on one key starve each other] → inherent to per-key quotas; document that agents deserving isolation deserve their own keys.
- [Quota refusal loops an agent] → the refusal message states the reset time (next UTC midnight) so a competent agent backs off.
- [Migration collision] → 020 assigned here (019 perf views, 021 ops health).

## Migration Plan

`make test-schema`; deploy runs migration 020 (nullable CHECK-constrained limit column, the `quota_counters` table with its FK, and the composite usage_logs index; no backfill). Rollback: downgrade drops all three; enforcement code no-ops on missing limits.

## Open Questions

(none blocking)
