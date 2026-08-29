## Context

`usage_logs` (indexed on `created_at`) holds duration_ms/response_size per call and JSONB `params` with `embed_ms`/`db_ms` for search tools (recorded via `src/services/timing.py`). Indexer passes run under `index_pass_lock` (`_index_pass_once`) with no persistent record. Panel is Jinja2 + vendored Chart.js; wave sequencing puts this after the light-mode token partial exists.

## Goals / Non-Goals

**Goals:** percentile views per tool; phase breakdown; persistent pass history; bounded queries.
**Non-Goals:** no APM/tracing, no per-request flame graphs, no new logging from tools, no realtime streaming (page loads compute on request), no display of pass history beyond the summary (that page is `panel-ops-health`).

## Decisions

1. **Aggregates computed with `percentile_cont` over `usage_logs`** at request time, window-bounded (24h/7d/30d) and grouped by tool; the `created_at` index bounds the scan. No materialized rollups until proven slow — self-hosted volumes (hundreds of requests/day) don't justify them.
2. **Phase breakdown reads `params->>'embed_ms'` / `params->>'db_ms'`** filtered to rows where the keys exist; averages + p95 per phase. Missing keys (non-search tools, older rows) simply don't contribute.
3. **`indexer_runs` table** (migration 019): `id, started_at, finished_at, trigger ('scheduled'|'manual'|'backfill'), notes_scanned, notes_indexed, notes_embedded, error (nullable text)`. Written once per pass in a `finally` block inside the pass (so a failing pass records its error); pruned to newest 500 rows in the same transaction. Alternative (log-parsing) rejected: logs rotate with containers.
4. **Chart.js reuse** with theme-token colors per the `panel-theming` contract.
5. **Multi-tenant passes** record one row per pass (with the pass's `user_id` when per-user), matching how the indexer loops today.

## Risks / Trade-offs

- [percentile_cont over a large window scans many rows] → windows capped at 30d and the created_at index bounds it; revisit with rollups only if page latency is felt.
- [JSONB extraction on params lacks an index] → same bounding; the filter runs on already-window-limited rows.
- [Migration collides with a parallel change] → number 019 assigned here; 020/021 reserved for the other wave-2 changes.

## Migration Plan

`make test-schema` before deploy; `make deploy` runs migration 019. Rollback: alembic downgrade drops `indexer_runs` (usage aggregation is read-only and rolls back with the image).

## Open Questions

(none blocking)
