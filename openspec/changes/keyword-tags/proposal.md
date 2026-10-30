## Why

GitHub #391, observed live 2026-10-30 after `hybrid_search` (#354)
deployed: `hybrid_search("HOA siding complaint")` ranked the HOA note
second, carried by its semantic arm alone, because the word `HOA` appears
only in that note's tags and the keyword arm cannot see tags at all. The
keyword vector is `A(title) || A(aliases) || D(body)` (#344); a note's
frontmatter `tags` and inline `#tags` are stored in `notes_metadata.tags`
for filtering and display but are in no `tsvector`. A tag is often the
most deliberate label a note carries, and the keyword arm is supposed to
be the exact one, so this is the "silently wrong search result" class
CLAUDE.md names as this product's second most expensive failure: an agent
searching for a word its own notes are tagged with gets nothing from
`keyword_search` and acts on that.

## What Changes

- **Tags enter the keyword vector at weight `B`.** The vector becomes
  `A(title) || A(aliases) || B(tags) || D(body)` per configured FTS
  config, built by the one builder in `src/services/fts.py` for every
  writer. Under the unchanged default `RANK_WEIGHTS` `{0.02, 0.2, 0.4,
  1.0}` (`{D, C, B, A}`; `B` is the third slot and already `0.4`) one tag
  cover outranks a body cover and ranks below a title cover. No setting
  changes and no new setting is added.
- **The tags text is derived by one shared rule** from the note's
  extracted tags (frontmatter `tags` plus inline `#tags`, exactly the
  list `notes_metadata.tags` stores). A nested tag contributes its full
  form *and* each `/`-segment (`ctx/phone` → `ctx/phone`, `ctx`,
  `phone`), because PostgreSQL's parser reads `ctx/phone` as one `file`
  token and a query for `phone` would otherwise never match it. The text
  is bounded (2,048 characters, whole tags), like the aliases text.
- **`matched_in` gains `tags`.** It reports each part that alone matches
  — `title` (title or aliases, `{a}`), `tags` (`{b}`), `body` (`{d}`) —
  comma-joined in that order, and when no part alone matches, the first
  minimal split in a fixed order: `title+tags`, `title+body`,
  `tags+body`, else `title+tags+body`. A tags-only match renders `head:`
  with `matched_in: tags` (tags are not in the excerpt); on a note with
  an empty body the row has no preview line at all. The issue's
  query renders `matched_in: tags+body` and `head:`.
- **Migration 035 re-heads every tagged vector in SQL** under the index
  generation lock with the stored fingerprint's configs, from the stored
  `title`, `frontmatter` and `tags` columns, no file access — 031's
  shape. It skips a row that already carries a weight-`B` lexeme and a
  row whose tags produce no lexeme (nothing to add; an untagged row is
  already byte-equal to what the new builder writes). Downgrade strips
  every `B` lexeme. No schema object changes; `alembic check` stays clean.
  035 is all-or-nothing: any row error, the 1 MiB tsvector limit (54000)
  included, fails it with nothing committed and names the row id; the
  upgrade note gives the remedy.
- **Both move paths and the full rebuild bind the tags** through the same
  rule, so the move re-head keeps the tags part instead of dropping it.
  The AST guard that only the builders compose `to_tsvector(` stays, and
  a new guard pins that every builder call in `src/` passes the tags bind.
- **`hybrid_search`'s keyword arm inherits everything** through
  `keyword_arm_rows`/`keyword_snippets`; the only visible difference is
  the new `matched_in` values in its provenance.
- Docstrings (`keyword_search`, `render_keyword_results`), README (`RANK_WEIGHTS` row, keyword section), `search.md`,
  `indexing-and-embeddings.md`, CLAUDE.md's keyword-vector key decision
  and a `DEPLOYMENT.md` upgrade note (035 rewrites vectors in place; stop
  the app before migrating, which `make deploy` does by itself).

Not **BREAKING** for the tool contract: `matched_in` is a list of
documented values and gains new ones; ranks change value as they did in
#344 (ranks were never comparable across versions).

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `search-quality`: the keyword vector's shape and writer contract gain
  `B(tags)`; `matched_in` gains `tags` and the split combinations; the
  docstring states that tags are indexed.
- `index-integrity`: both move paths re-head with the tags part; a new
  requirement fixes the shared tags-text rule.
- `schema-integrity`: migration 035 (the in-SQL re-head, its skip
  predicates, its downgrade).

- `search-quality` (also): the result-identity oracle exempts rows with a
  tag contribution and keeps its guarantee over every other row.
- `hybrid-search`: the response-bound proof uses `title+tags+body` as the
  longest `matched_in` (abbreviated structure 345, floor sum 990).
- `reranker`: `hybrid_search(rerank=true)`'s pinned structure becomes 367;
  its worst case falls through to the omitted form (≤ 984).

`search-evaluation` is not modified: the harness is unchanged, and this
change is subject to its existing gate as a search-path change, run with
the runfile workflow because 035 rewrites the incumbent's vectors (design
D7).

## Impact

- Code: `src/services/fts.py` (builders, `tags_text`), `src/services/search.py`
  (`_snippets` flags and `matched_in` derivation), `src/services/indexer.py`
  (incremental vector write, move-pair re-head, `_rebuild_tsvectors_pinned`),
  `src/mcp_server/tools.py` (`move_note` re-head, `render_keyword_results`
  docstring), `src/mcp_server/server.py` (`keyword_search` docstring),
  `alembic/versions/035_keyword_tags.py` (new, `down_revision "034"`),
  `alembic/versions/031_keyword_head_and_excerpt.py` (one call site passes
  `tags_bind=None` explicitly; behaviour and bytes unchanged, pinned).
- Data: every `notes_metadata` row with a tagged vector is rewritten once by
  035 (new tuple versions; 4,265 notes on the reference deployment).
- Tests: unit (`tags_text`, builder guard, render), PostgreSQL integration
  (ranking between title and body, the issue's query, `matched_in`, move
  paths, rebuild, writer equality), schema gate (035 round trip).
- Gates: search-path change → mandatory adversarial Codex pass and the
  search-eval gate on a restored copy; migration → `make test-schema` case
  and `alembic check` after deploy.
- No new setting, no fingerprint change, no re-embed, no extraction-version
  bump.
