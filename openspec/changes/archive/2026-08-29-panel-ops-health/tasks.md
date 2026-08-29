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
- [x] 4.2 End-to-end (live, post-deploy): 021 at head, alembic check clean; deploy's backup printed the spec'd bootstrap warning; subsequent make db-backup recorded its row (filename+121644582 bytes matching the dump); startup passes recorded per user. Authenticated page/strip render not reachable without operator credentials — covered by the verifier's real-template route-context tests; operator eyeball recommended on next panel visit
