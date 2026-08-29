## Context

`usage_logs` carries `key_id`, `oauth_token_id`, `user_id`, denormalized actor labels (issue #77), and a `created_at` index. `_tracked` resolves the caller's credentials/vault root before tool bodies run — the natural enforcement point. Panel key CRUD lives in `src/api`/`src/control_panel`; api_keys rows carry the display prefix and hash.

## Goals / Non-Goals

**Goals:** filterable usage, per-actor totals, opt-in per-key daily ceiling that fails closed per-call but never blocks other actors.
**Non-Goals:** no rate-limiting (req/s), no OAuth quotas in v1, no billing semantics, no retention changes, no per-tool quotas.

## Decisions

1. **Enforcement in `_tracked`, after auth, before the tool body:** `SELECT count(*) FROM usage_logs WHERE key_id = :k AND created_at >= :utc_midnight` compared to the key's limit; over → structured error (the same error surface tools already use), the refusal itself logged (with an over-quota marker) so the operator sees the pressure, but refusals do not consume quota (the count predicate excludes over-quota-marked rows). One indexed COUNT per call is acceptable at self-hosted volumes; a composite index `(key_id, created_at)` ships in migration 020. Alternatives rejected: in-memory counters (lost on restart, wrong across workers), middleware placement (no key row loaded yet).
2. **UTC day boundary,** documented in the key form copy; a nullable limit means unlimited (default, no behavior change).
3. **Quota display on the keys page** (today's count / limit) via the same COUNT; usage page totals reuse the filter query.
4. **Filters are WHERE-clause composition** on the existing usage query, validated against known ids; the denormalized actor columns keep deleted-key history visible under "all".

## Risks / Trade-offs

- [COUNT per call adds latency] → composite index; measured against the perf views of #160.
- [Two clients on one key starve each other] → inherent to per-key quotas; document that agents deserving isolation deserve their own keys.
- [Quota refusal loops an agent] → the refusal message states the reset time (next UTC midnight) so a competent agent backs off.
- [Migration collision] → 020 assigned here (019 perf views, 021 ops health).

## Migration Plan

`make test-schema`; deploy runs migration 020 (nullable column + composite index; no backfill). Rollback: downgrade drops both; enforcement code no-ops on missing limits.

## Open Questions

(none blocking)
