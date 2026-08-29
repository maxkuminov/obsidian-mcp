## Context

`_tracked` merges `timing.current()` into logged params (`src/mcp_server/tools.py`), and search services already record `embed_ms`/`db_ms` there. Queries are logged today (named params); result counts and returned paths are not. `_truncate_params` bounds param size. Analytics = JSONB aggregation over `usage_logs` windows.

## Goals / Non-Goals

**Goals:** query visibility, zero-result detection, retrieval coverage, per-tool split, window-bounded queries.
**Non-Goals:** no relevance scoring or query rewriting, no per-agent behavioral profiling beyond existing actor attribution, no retention changes, no new logging for non-search tools, no exposure outside the admin panel.

## Decisions

1. **Telemetry via `timing.record`**: `result_count` (int) always; `result_paths` as the first 10 returned note paths, additionally bounded to 2048 bytes total (paths dropped from the end to fit). The budget is enforced at the record site because `_tracked` merges `timing.current()` AFTER `_truncate_params` runs — the generic truncation is not a backstop for telemetry. Recorded in the service layer where results are final, so the tool wrapper stays generic.
2. **Zero-result = `result_count == 0`**, distinguished from errors (error-marked rows excluded).
3. **Coverage metrics named honestly.** The ranking is "top-logged retrievals" (appearances within each call's first 10 results) — not a claim about all retrievals — and never-retrieved is an upper bound; the cap caveat is displayed beside both. find_related, which logs a source `path` rather than a `query`, gets its own per-source-path tables and is excluded from query-frequency; its source is recorded via timing.record under the `source_path` key — full path up to 1024 bytes, sha256 hex beyond that, non-colliding either way (the named param is truncated at 200 chars — distinct long paths would collapse), and every aggregation keys on (user_id, path) NULL-safely.
4. **Aggregation in SQL** (jsonb_array_elements over window-bounded rows), same no-rollup stance as `panel-performance-views`.
5. **Privacy stance:** queries can contain personal text; the page is behind the same admin OAuth as everything else and adds no new exposure. Screenshots of this page for the README must use demo data (per the `panel-light-mode` screenshot checklist).

## Risks / Trade-offs

- [params bloat from result_paths] → hard cap 10 paths and 2048 bytes per call at the record site (the generic truncation does not see merged telemetry).
- [JSONB aggregation cost] → window-bounded via created_at index; small self-hosted volumes.
- [Misreading "never-retrieved" as exact] → explicit caveat copy in the page.

## Migration Plan

No migration. Deploy = container rebuild; rollback = previous image (new param keys in old rows are inert).

## Open Questions

(none blocking)
