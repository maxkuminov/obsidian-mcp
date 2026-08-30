## Why

Operational state lives in three places the panel cannot show: indexer pass outcomes (container logs, lost on rotation), application errors (same), and backup freshness (host filesystem the container cannot see). "Is the system healthy, when did the last backup run, what failed recently" currently requires SSH.

## What Changes

- New "Health" panel page: indexer run history (from `indexer_runs`, introduced by `panel-performance-views`), recent application errors, and latest-backup age with a staleness warning.
- Errors: an in-process ring buffer logging handler (most recent 100 ERROR+ records) surfaced on the page — process-lifetime only, no schema.
- Backups: `backups_log` table (migration **021**); `make db-backup` inserts a row (filename, size) after a successful dump, so backup age is DB-visible without mounting host paths into the container.
- Dashboard gains a compact health strip (last pass ok/failed, last backup age, recent error count).

## Capabilities

### New Capabilities

- `panel-ops-health`: the health page, error surface, and backup recency record.

### Modified Capabilities

(none)

## Impact

- `src/control_panel/` (page + dashboard strip), logging setup in `src/main.py`, `src/models/db.py` + migration 021, `Makefile` db-backup target, `DEPLOYMENT.md`, `docs/architecture/control-panel.md`.
- Depends on `panel-performance-views` (indexer_runs) and `panel-light-mode` (tokens); implement last in wave 2. Schema gate mandatory. Issue: #163.
