## 1. Schema

- [x] 1.1 Migration 020: `daily_request_limit` nullable column with CHECK (NULL or 1..1000000), `quota_counters(key_id, day, count)` with composite PK and key_id FK ON DELETE CASCADE (key-deletion-with-counter covered by a test), plus composite index (key_id, created_at) on usage_logs; test-schema green; alembic check clean

## 2. Enforcement

- [x] 2.1 Atomic conditional-increment admission in `_tracked` after credential resolution, before tool body; structured refusal naming limit + UTC reset; over-quota marker `"over_quota": true` via a shared constant; refusals never increment; opportunistic pruning of counter rows older than 2 days
- [x] 2.2 Tests: limit boundary, CONCURRENT boundary (exactly N admitted under >N concurrent calls), rollover, refusal non-consumption, null-limit no-op, cross-key isolation, invalid limit values rejected

## 3. Panel

- [x] 3.1 Usage filters (user/key/tool/window) on chart + log + per-actor totals; denormalized labels for deleted actors
- [x] 3.2 Key create/edit limit field + today's count display

## 4. Docs, audit, verification

- [x] 4.1 docs/architecture/usage-attribution.md: quota semantics, marker, UTC boundary
- [ ] 4.2 Adversarial Codex pass on the enforcement path (tool execution surface)
- [ ] 4.3 End-to-end with a low-limit throwaway key against the live server
