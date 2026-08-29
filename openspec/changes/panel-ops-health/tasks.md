## 1. Schema

- [x] 1.1 `backups_log` model + migration 021; test-schema green; alembic check clean

## 2. Recording

- [x] 2.1 Makefile db-backup inserts the row after a successful dump, guarded by a to_regclass existence check (absent → loud warning, target still succeeds; present → insert failure fails the target); DEPLOYMENT.md updated incl. the bootstrap-deploy note
- [x] 2.2 Ring buffer ERROR handler (maxlen 100) attached in main.py lifespan

## 3. Panel

- [x] 3.1 Health page: run history (≤50), error list with observation window, backup age + 8-day warning; empty states for all three
- [x] 3.2 Dashboard health strip linking to the page; theme token partial throughout

## 4. Docs and verification

- [x] 4.1 docs/architecture/control-panel.md health section
- [ ] 4.2 End-to-end: trigger a pass, force one logged error, run db-backup, verify page + strip against live server
