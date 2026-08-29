## 1. Schema

- [x] 1.1 `indexer_runs` ORM model (incl. trigger enum with startup, nullable user_id ON DELETE SET NULL) + migration 019; `make test-schema` green; `alembic check` clean after migrate

## 2. Instrumentation

- [x] 2.1 Record a run row per pass (finally-block write; trigger threaded from startup / scheduled loop / panel reindex / backfill; user_id for per-user passes); prune to 500 in the same transaction

## 3. Panel

- [x] 3.1 Performance page: per-tool percentiles + executed counts + refusal counts + response_size aggregates with 24h/7d/30d window selector (percentile_cont; created_at-bounded; refusal rows excluded from latency/size math)
- [x] 3.2 Phase breakdown card (embed_ms/db_ms mean+p95 where keys exist)
- [x] 3.3 Slowest-requests table (≤50, actor attribution)
- [x] 3.4 Nav entry + dashboard indexer card link; templates consume the theme token partial
- [x] 3.5 Recent-passes card: newest 20 `indexer_runs` rows (started, duration, trigger, owner, scanned/indexed/embedded, error), owner joined live from `users` with NULL and missing owners rendered explicitly; not window-bounded. Full filterable history stays `panel-ops-health` (#163)

## 4. Docs and verification

- [x] 4.1 Update docs/architecture/indexing-and-embeddings.md (run recording) and usage-attribution.md (read-only consumer note)
- [x] 4.2 End-to-end (live, post-deploy): DB at 019, alembic check clean; /admin/performance returns 200; indexer_runs recording real startup rows per user (user 1: 3445 scanned, user 2: 427); windows exercised via the rendered page
