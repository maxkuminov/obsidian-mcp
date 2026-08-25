## 1. Read-path owner scoping (D1)

- [ ] 1.1 Change `apply_note_filters` in `src/services/filters.py`: `user_id=None` appends `NoteMetadata.user_id.is_(None)`; update the docstring to state the total mapping.
- [ ] 1.2 Sweep every index-backed read site that scopes inline with `if user_id is not None` (grep `src/mcp_server/tools.py`, `src/services/embeddings.py` for owner branches: semantic_search, find_related, get_recent, list_notes, graph tools, keyword_search) to the same total mapping (use `apply_note_filters` or the existing `_note_owner_predicate` idiom).
- [ ] 1.3 Tests: ownerless read on a mixed DB returns only NULL-owned rows; named-user scoping unchanged; single-user unchanged. (Offline: assert compiled SQL contains the `IS NULL` predicate; reuse existing filter-test idioms in `tests/`.)

## 2. Exclusion reconciliation (D2)

- [ ] 2.1 Add a reconciliation step to the embed pass in `src/services/indexer.py` after the hash-mismatch backlog: select owner-scoped rows with `embedded_content_hash IS NOT DISTINCT FROM content_hash` plus `EXISTS(note_embeddings)`; evaluate current patterns in Python.
- [ ] 2.2 Excluded-with-vectors → existing certified exclusion branch (certify_embedded then DELETE, per-note commit, StaleCertification rollback).
- [ ] 2.3 Included-without-vectors → read bytes beneath the pinned root, verify hash (skip on mismatch), clean+chunk; zero chunks → write nothing; else embed via `embed_note` certified path.
- [ ] 2.4 Respect `_is_paused()` between reconciliation notes.
- [ ] 2.5 Tests: add-pattern removes vectors next pass; remove-pattern re-embeds next pass; empty note writes nothing; stale certification rolls back (fakes/monkeypatch idioms already used in tests/test_issue_11*).

## 3. Title recompute on move (D3)

- [ ] 3.1 Indexer move detection: bind `title = :title` from the parsed new-path entry into `move_upd_sql`.
- [ ] 3.2 `move_note` metadata UPDATE in `src/mcp_server/tools.py`: `title = CASE WHEN COALESCE(frontmatter->>'title','') <> '' THEN title ELSE :new_title END`, `:new_title` = destination stem bounded to 512.
- [ ] 3.3 Tests: rename with no fm title updates title (both paths); explicit fm title survives.

## 4. Bounded keyword indexing (D4)

- [ ] 4.1 Shared helper in `src/services/indexer.py` that performs one note's tsvector UPDATE: full content inside `session.begin_nested()`; on failure halve and retry (savepoint each attempt) down to a 100,000-char floor; final failure → log + append to the pass's `skips` list.
- [ ] 4.2 Use it at both call sites (incremental pass ~line 1244, `rebuild_tsvectors` ~line 1938; the rebuild has no skip list — log there).
- [ ] 4.3 Tests: a statement that raises once at full size succeeds at half size without poisoning the transaction; total failure lands in skips (monkeypatched session/execute).

## 5. Proportional embed deadline (D5)

- [ ] 5.1 `OllamaProvider.embed_batch`: deadline becomes `max(batch_timeout, per_chunk_budget * len(texts))` (pick per_chunk_budget = 30.0 to match the per-call bound); keep the 30 s per-call `wait_for`.
- [ ] 5.2 Confirm `embed_note`'s `len(embeddings) != len(chunks)` refusal is intact and covered by a test (extend if not).
- [ ] 5.3 Tests: a batch of N slow-but-under-30s chunks completes past 300 s total (mock provider, fake clock or small-scale equivalent); a hung chunk still raises at the per-call timeout.

## 6. Gates

- [ ] 6.1 `.venv/bin/python -m pytest -q tests/` green (integration modules self-skip).
- [ ] 6.2 `openspec validate silently-wrong-search-127 --strict` clean.
