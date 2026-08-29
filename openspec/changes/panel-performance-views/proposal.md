## Why

Every MCP call already records `duration_ms`, `response_size`, and per-phase timings (`embed_ms`, `db_ms` inside `params`) into `usage_logs`, but the panel shows only a per-request log line — no aggregates, no percentiles, no pass-level timings. The operator cannot answer "how slow is semantic search lately" or "how long does an embed pass take" without SQL.

## What Changes

- New "Performance" panel page: per-tool p50/p95/p99 `duration_ms` and request counts over a selectable window (24h / 7d / 30d), slowest-requests table, `response_size` aggregates, and an embed_ms/db_ms phase breakdown for search tools (extracted from `usage_logs.params`).
- New `indexer_runs` table (migration **019**) recording each index/embed pass: started/finished, trigger (scheduled/manual/backfill), notes scanned/indexed/embedded, error; written by the indexer at pass end, pruned to the most recent 500 rows.
- Dashboard indexer card links to pass history (full history display lands with `panel-ops-health`).
- Read-only aggregation otherwise; no change to what tools log.

## Capabilities

### New Capabilities

- `panel-performance-views`: the performance page's aggregates, windows, phase breakdown, and the indexer run record.

### Modified Capabilities

(none — `usage-attribution` requirements are untouched; this only reads what they mandate)

## Impact

- `src/control_panel/` (new page + route), `src/services/indexer.py` (pass recording), `src/models/db.py` + migration 019, `docs/architecture/indexing-and-embeddings.md` and `usage-attribution.md` pointers.
- Sequenced after `panel-light-mode` (new template must use the token partial). Schema gate (`make test-schema`) mandatory. Issue: #160.
