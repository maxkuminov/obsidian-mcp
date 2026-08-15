## 1. Migration
- [ ] 1.1 `alembic/versions/013_schema_reconciliation.py` per design D1–D3 (backfill + SET NOT NULL per column; CHECK verify→create-if-absent; downgrade drops CHECK only if created here). Comment block explains the drift's origin (#53).
## 2. Tests & docs
- [ ] 2.1 `tests/integration/test_schema_check.py` (opt-in `PGVECTOR_TEST_ADMIN_URL`, reuse the throwaway-DB harness): (a) empty→head then `alembic check` clean (run alembic's check via its Python API or subprocess with the test DB URL); (b) drift simulation: upgrade to 012, drop the CHECK, `ALTER … DROP NOT NULL` on the nine columns, insert one NULL row per column where a table allows it, run 013 → clean + values backfilled; (c) violating `oauth_clients` row → 013 raises naming the client_id, schema unchanged; (d) 013 twice → ok.
- [ ] 2.2 `CLAUDE.md`: "`alembic check` must be clean" line + how to run in the container; `Makefile`: `db-check` target.
## 3. Verify & ship
- [ ] 3.1 `openspec validate schema-reconciliation --strict`; offline suite; integration module run once against a throwaway pgvector container.
- [ ] 3.2 `openspec-verifier`; adversarial Codex (live-DB migration). Iterate to no BLOCKER/MAJOR.
- [ ] 3.3 `make deploy`; post-deploy `docker exec obsidian-mcp alembic check` clean; `pg_constraint` shows the CHECK.
- [ ] 3.4 Archive, PR closing #53, merge.
