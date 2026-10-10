## 1. Schema (D10)

- [x] 1.1 `src/models/db.py`: `NoteMetadata.derived_under: Mapped[str | None] = mapped_column(String(64), nullable=True, comment=_DERIVED_UNDER_COLUMN_MARKER)` with a comment block naming D2/D3 (what the digest binds, who writes it, why NULL means "not derived")
- [x] 1.2 `alembic/versions/030_note_derived_under.py` (revises `029`): pinned and asserted `search_path`, `lock_timeout`/`statement_timeout` set and `RESET`; add the column with no default and no backfill; on a re-run reconcile a marked `varchar(64)` nullable no-default column and refuse by name any other shape (type, NOT NULL, default, missing marker); `downgrade()` drops only a marked column and prints a skip otherwise
- [x] 1.3 `docker exec obsidian-mcp alembic check` equivalent on the throwaway database is clean ("No new upgrade operations detected")

## 2. Digest and per-row force (D2, D4)

- [x] 2.1 `src/services/indexer.py`: `derived_under_digest(facts, file_path, content_hash, extraction_version) -> str` exactly as design D2 (`["derived-under-v1", …]`, `ensure_ascii=True`, compact separators, SHA-256 hex); unit-tested for totality over surrogate-escaped strings and for exact handle comparison (NULL ≠ any value)
- [x] 2.2 Owner-scoped snapshot and locked selects carry `derived_under`; `SnapshotRow` includes it, so C4 re-processes a row whose marker changed between the snapshot and the lock
- [x] 2.3 `_scan_vault` / `_run_scan`: replace the pass-wide `force_read` for re-derive with a per-row rule — shortcut iff shortcut on, not a full-hash pass, extraction marker current, stat equal, and (not re-derive or row current); `_needs_body` retains the body iff row absent, hash differs, marker stale, or (re-derive and row not current)
- [x] 2.4 `_index_vault_attempt`: the "no change" branch applies iff extraction marker current, locked hash equal, and (not re-derive or locked row current) — decided against the locked row

## 3. Writes, withholding and the stamp (D3, D5, D6, D7)

- [x] 3.1 Upsert `ON CONFLICT … SET derived_under = NULL` (insert carries NULL); id-preserving move UPDATE sets `derived_under = NULL`
- [x] 3.2 Re-derive only: after the link rebuild, one chunked owner-scoped UPDATE marks the fully-derived set (`to_upsert ∪ moved_new_paths` minus keyword-vector and link-rebuild skip paths), predicated on `file_path`, `content_hash`, `extraction_version` equal to the digest's inputs; never in keep mode or single-user mode
- [x] 3.3 Withholding per design D5: read / C4 re-read / raw-body / parse skips and C5 deferrals withhold iff the locked row exists and is not current; a non-root walk-failure prefix withholds iff a locked row at or beneath it is not current; keyword-vector and link-rebuild skips always withhold (unchanged)
- [x] 3.4 Completion re-resolution (D7): on a re-derive about to stamp (withholding empty), rebuild `vault_index` from the scope's final rows and re-resolve every link row (`resolve_target(target_path, source path, index)`), updating `target_note_id` only where it differs; skip `target_path` rows at 1,024 characters (L3); no file read
- [x] 3.5 Invariant re-read (D6): after 3.4 and before the NOWAIT stamp, re-read the scope's rows in the transaction and count rows whose `derived_under` ≠ their expected digest; stamp iff withholding empty and count zero, else `REDERIVE_INCOMPLETE`; log the count and (when withholding is empty) the first `SKIP_REPORT_LIMIT` such paths
- [x] 3.6 `IndexPassResult.rederive_pending: int` (0 when recorded or not a re-derive); the incomplete WARNING includes it
- [x] 3.7 `_reconcile_provenance` / `_index_vault_pinned`: the re-derive branch already returns `facts`; thread them to the scan and attempt. Move `clear_sweep_state(user_id)` to after a committed re-derive that upserted, moved or deleted at least one row (D8)
- [x] 3.8 Clearing writers (design D3 table): `move_note`'s metadata transaction NULLs the moved row, every `source_note_id` whose `note_links` row its `target_path` UPDATE changes, and every planned backlink-rewrite source; the pass's `move_tp_sql` rewrite NULLs every source whose rows it changes; the link backfill NULLs every note whose links it writes
- [x] 3.9 Grep-check every `notes_metadata` / `note_links` write outside the attempt's upsert against the D3 table; a test pins that only the pass's tail writes a non-NULL `derived_under`

## 4. Unit tests (offline)

- [x] 4.1 Digest: facts, path, hash and version each change it; handle NULL vs value differ; surrogate input is total
- [x] 4.2 Scan eligibility table: re-derive + current row + equal stat → no read; re-derive + not-current row → read; full-hash pass → read
- [x] 4.3 Withholding table (D5): the walk-failure narrowing both ways offline (`tests/test_issue_91_indexed_root.py`); the read-skip, deferral and invariant rows against real rows in 5.1, 5.9, 5.10 — the offline fakes execute no SQL, so they cannot model which rows are current
- [x] 4.4 Sweep record kept by a re-derive that changed no row — in the integration suite (5.x), for the same reason

## 5. Integration test on real Postgres (`tests/integration/test_issue_311_rederive_progress_pg.py`)

Run with `make test-integration SCHEMA_TEST_CONTAINER=omcp-schema-w2b SCHEMA_TEST_PORT=55443`.

- [x] 5.1 The #311 reproduction: a scope in re-derive (no record) with N notes and one row-backed unreadable file — the read failure injected through the established `read_note_at` monkeypatch seam, not `chmod 000` (root ignores it); pass 1 commits incomplete and marks N−1 rows; pass 2 upserts zero rows and rewrites no keyword vector and no link rows — asserted by every resolved `notes_metadata` row's `xmin` (and every `note_links` row's `xmin`) being unchanged across pass 2, not by `indexed_at` — and is still incomplete; `rederive_incomplete` reaches the threshold and `/health` reports `degraded`
- [x] 5.2 File becomes readable → recorded, keep next tick, counter reset; file deleted instead → pruned and recorded
- [x] 5.3 Restart: clear the in-process backstop/quarantine state between passes; the next pass reads every file and upserts only unresolved rows
- [x] 5.4 A→B→A by `users.vault_path` edits that produce re-derive verdicts at each step (one fact changed): rows rewritten under B are forced under A; untouched A-marked rows are not
- [x] 5.5 Handle-mismatch re-derive marks nothing current from the previous handle
- [x] 5.6 `move_note` on a current row → not current → re-marked next pass; external rename → move-paired and marked in the same pass, its backlink sources NULLed
- [x] 5.6a Codex r1 failing input: unresolved scope, S marked under A; reassign B (re-derive verdict); `move_note(T.md, U.md)` before B's pass rewrites S; reassign A; assert S is not current, and that when A records, S's `note_links.target_path` is `T…` as A's bytes name it
- [x] 5.6b Link backfill on a marked row NULLs it
- [x] 5.7 Link/keyword skip leaves the row NULL and withholds; a forced `PoisonNote` restart leaves no marker from the rolled-back attempt
- [x] 5.8 Completion re-resolution: the bare-name link to a protected-then-pruned row resolves to the remaining candidate after recording (spec scenario)
- [x] 5.9 Walk-failure narrowing: unlistable dir over current rows records; over a NULL row withholds
- [x] 5.10 Invariant guard: a row inserted with NULL by a concurrent writer before the tail makes the pass incomplete with no named skip
- [x] 5.11 Existing re-derive, #308 and #309 integration suites still pass

## 6. Schema gate

- [x] 6.1 `tests/integration/test_schema_check.py`: head literal `030`; cases for the fresh shape, the chain from 029, no backfill, stamp-back idempotence with row data preserved, impostor-column refusals, and both downgrade directions
- [x] 6.2 `make test-schema SCHEMA_TEST_CONTAINER=omcp-schema-w2b SCHEMA_TEST_PORT=55443` (never concurrently with 5.x on the same container/port)

## 7. Docs (last commits of the feature branch)

- [x] 7.1 `docs/architecture/indexing-and-embeddings.md`: new section "Re-derive progress (#311)" — the marker, the digest and its binding, the writer rule, per-row force, the narrowed withholding table, the invariant re-read, completion re-resolution, the sweep rule, what a stuck scope costs per tick, L1–L5; update D8's paragraph ("a re-derive forces every file into `to_upsert`"), the stat-shortcut eligibility bullet, C5's deferral sentence and the D12 sentence on re-derive
- [x] 7.2 `docs/architecture/schema-and-migrations.md`: "030: `notes_metadata.derived_under`" — no backfill and why (016's rule; a recorded digest can never match a re-derive), reconcile-or-refuse, marked downgrade, gate cases
- [x] 7.3 CLAUDE.md, Key decisions: one bullet — re-derive progress is a per-row `derived_under` digest bound to the root facts and the row's path/hash/extraction version; only a re-derive pass writes it non-NULL; the stamp records only when every surviving row matches; a repeated re-derive rewrites only unresolved rows (#311)
- [x] 7.4 Confirm no new setting (README / `.env.example` unchanged); grep-check per the public-docs rule

## 8. Gates

- [x] 8.1 `openspec validate rederive-progress --strict`
- [ ] 8.2 `openspec-verifier` subagent against this change
- [ ] 8.3 Adversarial Codex pass (mandatory: indexer re-derive / provenance path) framed on foreign rows certified as current and on silently wrong search/graph results; two rounds by default
- [ ] 8.4 After deploy: `docker exec`-equivalent `alembic check` clean; end-to-end exercise of `keyword_search`, `semantic_search`, `get_links`, `get_backlinks` and `move_note` against the live server, named in the report
