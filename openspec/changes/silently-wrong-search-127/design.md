## Context

Five verified defects (#127) in the index/search layer. Shared background: the embed pass selects rows on `embedded_content_hash IS NULL OR != content_hash` and certifies through `certify_embedded(id, content_hash, file_path)` (a conditional row-locking UPDATE); the keyword index is one tsvector column written per changed note inside the pass transaction; every index pass runs under `index_pass_lock`; PostgreSQL rejects a tsvector larger than 1 MiB, and an uncaught error aborts the whole pass transaction so nothing commits and the same batch retries every tick (the #126 freeze class).

## Goals / Non-Goals

**Goals:**
- An ownerless read never crosses a tenant boundary: `user_id=None` reads exactly the `user_id IS NULL` rows, matching the write path.
- Editing `EMBEDDING_EXCLUDE_PATTERNS` converges: within one embed pass, vectors exist iff the current config includes the note.
- A move (either path) leaves `title` consistent with what a fresh index of the file would produce.
- Keyword indexing is bounded such that no note can abort a pass, while notes of ordinary size are indexed in full.
- A note with many chunks eventually embeds completely and certifies only after complete coverage; a hung provider still times out.

**Non-Goals:**
- No persistent-failure quarantine for notes whose embedding fails for non-timeout reasons (pre-existing behavior, unchanged).
- No re-chunking or provider protocol changes; no schema migration.
- No change to `read_note` (derives its title from the filesystem, already correct).

## Decisions

**D1 — read scoping: `None` maps to `IS NULL` in `apply_note_filters`, and read sites keep their explicit branches only where they don't use the helper.** The helper's `user_id` parameter changes semantics from "None = no predicate" to "None = `user_id IS NULL`" — the write path's `_note_owner_predicate` mapping. Single-user deployments see no behavior change (all rows are NULL-owned). Sites that scope inline (`semantic_search`, `find_related`, graph tools, `get_recent`) are swept to the same total mapping. The alternative — refusing `None` reads outright when multi-user rows exist — was rejected: `MULTI_USER_MODE` off with legacy multi-user rows is a legitimate operator state, and the NULL slice is exactly what a single-user credential owns.

**D2 — exclusion reconciliation is a per-pass sweep over *up-to-date* rows, reusing the certified stamp-then-delete discipline.** After the normal backlog, the pass selects rows where `embedded_content_hash IS NOT DISTINCT FROM content_hash` (plus owner predicate) with `EXISTS(note_embeddings)` as `has_vectors`, and evaluates the current patterns in Python (~2.5k rows; fnmatch is Python-side by design):
- *excluded and has_vectors* → run the existing exclusion branch verbatim: `certify_embedded(id, content_hash, file_path)` then `DELETE` vectors, per-note commit. Never delete-by-id without the certified predicate — a concurrent move must make the stamp miss and roll the note back (the review-caught trap).
- *included and no vectors* → the stamp may be a stale exclusion stamp or a legitimate zero-chunk note. Read the bytes beneath the pass's pinned root, refuse on hash mismatch (skip; the ordinary backlog will handle it), clean + chunk; if chunks are empty, write nothing (genuinely empty note — avoiding a per-pass DB write for every empty note); else embed through the existing `embed_note` certified path.
The cost of the "included, no vectors" probe is one file read per genuinely-empty note per pass — bounded and cheap; noted as accepted.

**D3 — move title: preserve an explicit frontmatter title, else the new stem.** The indexer's move path already holds the parsed entry for the new path (`entry_by_path[new]`), so its UPDATE simply also binds `title = :title` from that entry. `move_note` does not parse the file; its metadata UPDATE recomputes in SQL: `title = CASE WHEN COALESCE(frontmatter->>'title', '') <> '' THEN title ELSE :new_title END` with `:new_title` = the destination stem bounded to 512. Accepted edge: a frontmatter title that is falsy-but-non-empty as JSON text (`0`, `false`) keeps the old stem-derived title until the next content change; exact parity would re-read the file on every move for an edge nobody writes.

**D4 — keyword index: full content first, savepoint-guarded, bounded retreat on failure.** One shared helper performs the tsvector UPDATE for both the incremental pass and `rebuild_tsvectors`: attempt the full body inside `session.begin_nested()`; on failure, halve the content and retry (still savepoint-guarded) down to a 100,000-char floor, then log and skip the note's tsvector (leaving the previous vector, and the skip recorded in the pass's skip list so a re-derive does not certify over it). The savepoint is what keeps a poisoned statement from aborting the pass transaction — the #126 freeze class. The alternative — computing a "safe" static cap — was rejected: the 1 MiB limit binds on *unique-lexeme volume*, not characters, so any static cap either truncates ordinary prose needlessly or still admits a pathological note. Positions above ~16 K are already clamped by PostgreSQL; ranking degradation for matches deep in a 10 MB note is accepted and pre-existing.

**D5 — embed deadline proportional to work; certification requires full coverage.** `OllamaProvider.embed_batch`'s fixed 300 s whole-batch deadline becomes `max(batch_timeout, per_chunk_budget × len(texts))` with the existing 30 s per-call `wait_for` retained (a hung provider still fails in 30 s; a merely *large* note no longer can't finish by construction). `embed_note` already refuses to certify unless `len(embeddings) == len(chunks)` — that invariant is kept and asserted in the spec: no sub-batching shortcut may stamp a note while dropping tail chunks. The pause flag is still only checked between notes; a single giant note occupies the pass until it completes, once — accepted against the alternative (a permanent 300 s/tick retry that never completes).

## Risks / Trade-offs

- **D1** changes what an ownerless read returns on a mixed database from "everything" to "the NULL slice" — that is the fix, but any operator who relied on the leak to browse all tenants' rows with an ownerless key loses it (they have admin credentials for that).
- **D2** adds one bounded metadata query + Python fnmatch per embed pass; the "included, no vectors" probe re-reads genuinely-empty notes each pass.
- **D4**'s halving retreat can run the tsvector statement up to ~7 times for a pathological note; each attempt is savepoint-isolated. In exchange, ordinary notes are indexed in full for the first time.
- **D5** allows a giant note to hold `index_pass_lock` for however long its chunk count takes (bounded by 30 s × chunks worst case). The panel pause takes effect at the next note boundary, as today.
