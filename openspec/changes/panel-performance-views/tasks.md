## 1. Schema

- [ ] 1.1 `indexer_runs` ORM model (incl. trigger enum with startup, nullable user_id ON DELETE SET NULL) + migration 019; `make test-schema` green; `alembic check` clean after migrate

## 2. Instrumentation

- [ ] 2.1 Record a run row per pass (finally-block write; trigger threaded from startup / scheduled loop / panel reindex / backfill; user_id for per-user passes); prune to 500 in the same transaction

## 3. Panel

- [ ] 3.1 Performance page: per-tool percentiles + executed counts + refusal counts + response_size aggregates with 24h/7d/30d window selector (percentile_cont; created_at-bounded; refusal rows excluded from latency/size math)
- [ ] 3.2 Phase breakdown card (embed_ms/db_ms mean+p95 where keys exist)
- [ ] 3.3 Slowest-requests table (≤50, actor attribution)
- [ ] 3.4 Nav entry + dashboard indexer card link; templates consume the theme token partial

## 4. Docs and verification

- [ ] 4.1 Update docs/architecture/indexing-and-embeddings.md (run recording) and usage-attribution.md (read-only consumer note)
- [ ] 4.2 End-to-end: trigger manual pass, verify run row + panel display; exercise all three windows against live data
