## 1. Read-path owner scoping (D1, D1a)

- [ ] 1.1 `apply_note_filters` in `src/services/filters.py`: `user_id=None` appends `NoteMetadata.user_id.is_(None)`; docstring states the total mapping.
- [ ] 1.2 Sweep every index-backed tool to the total mapping: `keyword_search`, `semantic_search`, `list_notes`, `get_recent`, **`get_tags`**, `get_backlinks`, `get_links`, `get_neighborhood`, `find_related`, `find_orphans` (grep `src/mcp_server/tools.py` and `src/services/embeddings.py` for every `if user_id is not None` / `if uid is not None` owner branch).
- [ ] 1.3 Graph closure: every join resolving `note_links.source_note_id`/`target_note_id` to `notes_metadata` carries the owner predicate in the JOIN condition (dangling-link rows preserved); neighborhood BFS and orphan calculus cannot be influenced by a cross-owner edge.
- [ ] 1.4 Exact-fallback eligibility in `semantic_search`/`find_related`: the owner predicate counts as a filter — any zero-row approximate result re-runs exact, ownerless included.
- [ ] 1.5 Tests: mixed-ownership fixtures — ownerless read returns only NULL rows for each enumerated tool incl. get_tags; adversarial cross-owner link row never surfaces the other owner nor influences neighborhood/orphans; named-user and all-NULL behavior unchanged; mixed-owner zero-row semantic query returns the NULL-owned match via exact fallback (integration, or compiled-SQL assertions plus a harness case).

## 2. Exclusion reconciliation (D2, D2a)

- [ ] 2.1 Reconciliation sweep in `src/services/indexer.py` after the backlog: select owner-scoped certification-current rows (`embedded_content_hash IS NOT DISTINCT FROM content_hash`) with `EXISTS(note_embeddings)`; evaluate current patterns in Python.
- [ ] 2.2 Excluded-with-vectors → existing certified exclusion branch (certify_embedded then DELETE, per-note commit, StaleCertification rollback).
- [ ] 2.3 Included-without-vectors → read bytes beneath the pinned root; hash mismatch → skip; clean+chunk; zero chunks → write nothing; else `embed_note` certified path.
- [ ] 2.4 `_is_paused()` between reconciliation notes; a fresh sweep next pass is idempotent over repaired rows.
- [ ] 2.5 Tests: add-pattern removes vectors next pass; remove-pattern re-embeds; empty note untouched (no DB write); hash-mismatch skipped; stale certification rolls back; pause stops between notes and next sweep completes.

## 3. Title recompute on move (D3)

- [ ] 3.1 Indexer move detection: bind `title = :title` from the parsed new-path entry into `move_upd_sql`.
- [ ] 3.2 `move_note`: after `_verify_the_moved_inode`, read the moved note via the destination target, `parse_frontmatter`, `_note_title(fm, dest_name)`; on read/parse failure fall back to `_note_title(row.frontmatter or {}, dest_name)` from the row's JSONB; bind into the metadata UPDATE.
- [ ] 3.3 Tests: rename with no fm title updates title (both paths); explicit fm title survives; falsy titles (`false`, `0`, `[]`, `{}`, `""`) yield the new stem; fm title changed on disk after last index decides from the file, not the row.

## 4. Bounded keyword indexing (D4)

- [ ] 4.1 Shared helper in `src/services/indexer.py`: attempts full content; `try` OUTSIDE `async with session.begin_nested()` so the error unwinds the savepoint via the context manager; halve per attempt down to the exact 100,000-char floor; floor failure re-raises.
- [ ] 4.2 Use it at both call sites (incremental pass ~line 1244, `rebuild_tsvectors` ~line 1938); log every retreat with the prefix length.
- [ ] 4.2b Make `rebuild_tsvectors` atomic: remove its every-500-notes intermediate commits so a floor failure rolls the whole rebuild back and surfaces to the operator; incremental-pass semantics unchanged (nothing commits on abort).
- [ ] 4.3 Real-Postgres integration test (in `tests/integration/`, using the `PGVECTOR_TEST_ADMIN_URL` harness): induce a genuine statement failure (e.g. an over-limit tsvector or a forced error), bounded retry succeeds in the same outer transaction, a later update commits, both rows verified — covering BOTH call sites, including a rebuild failure past the former 500-note commit boundary rolling everything back. Offline unit tests may cover the halving arithmetic only.

## 5. No aggregate embed deadline (D5)

- [ ] 5.1 `OllamaProvider.embed_batch`: remove the whole-batch deadline; keep the 30 s per-call `asyncio.wait_for`. Check all callers of `embed_batch`/`get_embeddings_batch` for a `batch_timeout` argument to retire.
- [ ] 5.2 Confirm `embed_note`'s `len(embeddings) != len(chunks)` refusal is intact and covered by a test (extend if not).
- [ ] 5.3 Tests: a batch larger than the old deadline's capacity completes (mock provider); a hung chunk raises at the per-call timeout; partial coverage is not certified.

## 6. Gates

- [ ] 6.1 `.venv/bin/python -m pytest -q tests/` green offline; integration modules against a throwaway pgvector server green (`PGVECTOR_TEST_ADMIN_URL`).
- [ ] 6.2 `openspec validate silently-wrong-search-127 --strict` clean.
- [ ] 6.3 CLAUDE.md: update the "Filtered vector search" fallback wording (owner predicate counts as a filter) and the indexer sections touched.
