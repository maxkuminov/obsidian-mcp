## Why

Issue #127 (found by the #122 rigor campaign, adversarially verified) collects five defects that all end the same way: an agent gets silently wrong search results — the failure this product ranks above everything except destructive writes. Three of the five have *dangerous naive fixes* (delete-by-id vector sweeps, an unbounded tsvector, a stamp-while-dropping-chunks), so the change goes through the spec + adversarial gate rather than as a patch batch.

## What Changes

- **Read-path owner scoping**: `apply_note_filters(user_id=None)` currently appends *no* owner predicate, while every write path maps `None` → `user_id IS NULL`. With multi-user rows present and `MULTI_USER_MODE` off, an ownerless credential reads every tenant's rows. The read path adopts the write path's mapping: `None` → `IS NULL`. Every read site that scopes with an `if user_id is not None` branch (search, list, graph, recent) is swept to the same rule.
- **Exclusion-pattern reconciliation**: the embed pass selects only hash-mismatched rows, so `EMBEDDING_EXCLUDE_PATTERNS` edits never apply to already-stamped notes (adding a pattern strands vectors; removing one strands a zero-vector stamp). Each embed pass now reconciles up-to-date rows whose exclusion status disagrees with their stored vectors, using the existing certified (`id + content_hash + file_path`) stamp-then-delete discipline — never delete-by-id.
- **Title recompute on move**: neither the indexer's id-preserving move detection nor `move_note` recomputes the stem-derived `title`, so `Alpha.md → Beta.md` shows `Alpha` in index-backed tools forever. Both move paths recompute the title, preserving an explicit frontmatter title.
- **Bounded keyword indexing**: the tsvector build slices content at 100,000 chars, making terms past that point invisible to `keyword_search`; naively removing the slice can exceed PostgreSQL's 1 MiB tsvector limit and abort the whole pass. The build attempts the full content under a per-note savepoint and degrades to a bounded prefix on failure, so ordinary size-limit failures retreat per note while normal-size notes are fully indexed for the first time; a failure at the 100,000-char floor propagates and aborts exactly as the pre-change implementation did (incremental pass: nothing commits, retried next tick; the full rebuild becomes atomic and surfaces the error to the operator).
- **Giant-note embed completion**: `embed_batch`'s fixed 300 s whole-batch deadline makes a many-chunk note time out, never certify, and retry forever under `index_pass_lock` — a permanent 300 s/tick burn. The deadline becomes proportional to chunk count (per-chunk timeout still bounds a hung provider), and a note is only ever certified after *all* of its chunks embedded.

## Capabilities

### New Capabilities

(none)

### Modified Capabilities

- `note-filters`: the shared filter helper's `user_id=None` semantics change from "no owner predicate" to "`user_id IS NULL`", and every index-backed read path must scope through it (or an equivalent predicate).
- `index-integrity`: exclusion-pattern changes reconcile on the next pass; moves recompute stem-derived titles; the keyword index is bounded without aborting a pass; a many-chunk note completes and certifies only after full coverage.

## Impact

- `src/services/filters.py` (owner predicate), read sites in `src/mcp_server/tools.py` and `src/services/embeddings.py`
- `src/services/indexer.py` (move detection title, tsvector build + rebuild, exclusion reconciliation pass)
- `src/services/embeddings.py` (batch deadline, `embed_note` certification unchanged in shape)
- `src/mcp_server/tools.py` `move_note` metadata update (title recompute)
- No schema migration. No API surface change. Behavior changes are index/search-side only.
