## Context

Backups are written by `make db-backup` (host-side, into `$(DATA_DIR)/backups`) — the container deliberately cannot see that directory, and the public repo must not encode host paths. `indexer_runs` arrives with `panel-performance-views` (019). Application logs go to stdout/container logs only.

## Goals / Non-Goals

**Goals:** one page answering "healthy?": pass history, recent errors, backup age.
**Non-Goals:** no alerting/notifications, no log shipping or persistence of errors beyond process lifetime, no backup content verification (age only), no host-path mounts.

## Decisions

1. **Backup recency via DB record, not filesystem:** `backups_log(id, created_at, filename, size_bytes)` (migration 021); the `db-backup` target inserts through the same `docker exec` psql channel it already uses for `pg_dump`. Survives container recreation; keeps host paths out of the repo. Staleness threshold: warn at > 8 days (backups ride `make deploy`; cadence is deploy-driven). Alternative (mount backups read-only) rejected: host-specific compose in a public repo, and `Makefile.local` divergence.
2. **Error surface = ring buffer handler** (`collections.deque(maxlen=100)`) attached to the root logger at ERROR level, rendered with timestamp, logger name, message (no tracebacks beyond first line — the page is a pointer, `make logs` is the tool). Process-lifetime is acceptable and stated on the page; persisting errors is observability scope creep.
3. **Health strip on the dashboard** reuses the page's queries: last pass status from `indexer_runs`, last backup age, error count since process start.
4. **Failure posture:** a missing `backups_log` row set renders "no backup recorded yet" (fresh installs), never an error; the page must render fully even when every section is empty.

## Risks / Trade-offs

- [Makefile insert fails silently] → the insert is `&&`-chained after a successful dump and its failure fails the target loudly; deploy aborts (backup is the migration safety net — CLAUDE.md already treats it as such).
- [Ring buffer misses multi-worker errors] → single-process uvicorn today; noted in docs if that changes.
- [Panel shows stale "healthy" after container restart clears errors] → page states the observation window ("since process start HH:MM").

## Migration Plan

`make test-schema`; deploy runs 021. The first backup row appears on the deploy that ships this change (its own `db-backup` step). Rollback: downgrade drops the table; page renders empty states.

## Open Questions

(none blocking)
