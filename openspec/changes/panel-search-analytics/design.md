## Context

`_tracked` merges `timing.current()` into logged params (`src/mcp_server/tools.py`), and search services already record `embed_ms`/`db_ms` there. Queries are logged today (named params); result counts and returned paths are not. `_truncate_params` bounds param size. Analytics = JSONB aggregation over `usage_logs` windows.

## Goals / Non-Goals

**Goals:** query visibility, zero-result detection, retrieval coverage, per-tool split, window-bounded queries.
**Non-Goals:** no relevance scoring or query rewriting, no per-agent behavioral profiling beyond existing actor attribution, no retention changes, no new logging for non-search tools, no exposure outside the admin panel.

## Decisions

1. **Telemetry via `timing.record`**: `result_count` (int) always; `result_paths` as the returned note paths capped at 10 per call (enough for coverage aggregation; keeps params bounded alongside `_truncate_params`). Recorded in the service layer where results are final, so the tool wrapper stays generic.
2. **Zero-result = `result_count == 0`**, distinguished from errors (error-marked rows excluded).
3. **Never-retrieved notes** = notes in `notes_metadata` (per the owning user) with no appearance in any logged `result_paths` over the window; presented with the honest caveat in-page that path caps make it an upper bound... the cap makes *most-retrieved* exact only for top-10-visible results, and never-retrieved slightly overestimates retrieval absence. Acceptable for a hygiene view; stated in the UI copy.
4. **Aggregation in SQL** (jsonb_array_elements over window-bounded rows), same no-rollup stance as `panel-performance-views`.
5. **Privacy stance:** queries can contain personal text; the page is behind the same admin OAuth as everything else and adds no new exposure. Screenshots of this page for the README must use demo data (per the `panel-light-mode` screenshot checklist).

## Risks / Trade-offs

- [params bloat from result_paths] → hard cap 10 paths/call, existing truncation as backstop.
- [JSONB aggregation cost] → window-bounded via created_at index; small self-hosted volumes.
- [Misreading "never-retrieved" as exact] → explicit caveat copy in the page.

## Migration Plan

No migration. Deploy = container rebuild; rollback = previous image (new param keys in old rows are inert).

## Open Questions

(none blocking)
