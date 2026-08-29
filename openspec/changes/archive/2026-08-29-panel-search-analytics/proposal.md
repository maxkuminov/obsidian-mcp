## Why

Agents are ~80% of vault traffic, but the operator has no view of what they actually search for, which queries come back empty (memory the vault is asked for but doesn't hold), or which notes retrieval never surfaces. For a product repositioning as agent memory (#158), "what do my agents think this vault knows" is the signature observability question, and `usage_logs.params` already stores the queries.

## What Changes

- Search tools (`keyword_search`, `semantic_search`, `find_related`) additionally record `result_count` and the returned note paths (capped) into their logged params via the existing `timing` holder — no schema change.
- New "Search analytics" panel page over a selectable window: top queries with frequency and mean result count; zero-result queries; top-logged retrievals (first-10-per-call basis, labeled); never-retrieved notes (upper bound, labeled); query tables per query tool, find_related grouped by source path.
- Read-only otherwise; admin-only like the rest of the panel.

## Capabilities

### New Capabilities

- `panel-search-analytics`: result telemetry on search calls and the analytics views over it.

### Modified Capabilities

(none — `usage-attribution` and `search-quality` requirements unchanged; params gain keys under their own byte-budgeted telemetry contract (the generic truncation does not govern merged telemetry))

## Impact

- `src/services/search.py` (+`timing.record` calls), `src/control_panel/` (new page/route), `docs/architecture/search.md` + `usage-attribution.md` notes.
- Sequenced after `panel-light-mode`. No migration. Issue: #161.
