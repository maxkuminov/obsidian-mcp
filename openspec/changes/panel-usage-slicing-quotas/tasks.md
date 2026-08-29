## 1. Schema

- [ ] 1.1 `daily_request_limit` nullable column + composite index (key_id, created_at); migration 020; test-schema green; alembic check clean

## 2. Enforcement

- [ ] 2.1 Quota check in `_tracked` after credential resolution, before tool body; structured refusal naming limit + UTC reset; over-quota marker logged; refusals excluded from the consuming count
- [ ] 2.2 Tests: limit boundary, rollover, refusal non-consumption, null-limit no-op, cross-key isolation

## 3. Panel

- [ ] 3.1 Usage filters (user/key/tool/window) on chart + log + per-actor totals; denormalized labels for deleted actors
- [ ] 3.2 Key create/edit limit field + today's count display

## 4. Docs, audit, verification

- [ ] 4.1 docs/architecture/usage-attribution.md: quota semantics, marker, UTC boundary
- [ ] 4.2 Adversarial Codex pass on the enforcement path (tool execution surface)
- [ ] 4.3 End-to-end with a low-limit throwaway key against the live server
