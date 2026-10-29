## Why

GitHub #344, reproduced live 2026-10-28: `keyword_search("<exact two-word
title of an existing note>")` returned ten rows and the note carrying that
title was not among them, while a one-word body search found it a minute
later. The agent had just followed a `[[wikilink]]` to it. That is the
"silently wrong search result" CLAUDE.md names as this product's second
most expensive failure, on the arm that is supposed to be the exact one.

Three causes, all in code:

1. `index_tsvector_sql` builds `content_tsvector` from the body alone — no
   `setweight`, `notes_metadata.title` is not in the vector, frontmatter
   `aliases` are not indexed anywhere. `ts_rank_cd` therefore ranks a long
   note that mentions both words many times above the note whose title *is*
   the query.
2. `websearch_to_tsquery` ANDs bare terms, so a three-word query excludes
   every note missing any one stem, and the docstring never says so.
3. Rows carry path, title, tags and rank only. Triaging fifteen hits costs
   fifteen `read_note` calls, while `semantic_search` returns a preview per
   row — which pushes agents away from the arm that is right for
   identifiers.

## What Changes

- **Ranking weights.** The keyword vector becomes
  `A(title) || A(aliases) || D(body)` per configured FTS config: the
  indexed title (`notes_metadata.title`, the shared `note_title` rule) and
  the frontmatter `aliases` (a new shared `note_aliases` rule, list or
  scalar) at weight `A`, the existing full-content build at weight `D`.
  Ranking uses an explicit weight array, the `RANK_WEIGHTS` setting
  (default `{0.02, 0.2, 0.4, 1.0}`), so one title cover outweighs fifty
  body covers (design D2).
- **Conjunction first, disjunction to fill.** The existing AND query runs
  unchanged. When it returns fewer than `limit` rows and the parsed query is
  a plain conjunction, the same stems OR-ed (derived from PostgreSQL's own
  parse of the websearch query, never from the raw string) run second,
  excluding the AND rows, and fill the remainder. Every row is tagged
  `match: all | any`; each block keeps `rank DESC, file_path ASC`; all
  statements share the `SET LOCAL random_page_cost = 1.1` transaction and
  the same `folder`/`tags`/`frontmatter`/owner predicates.
- **A page-bounded snippet.** Migration **031** adds
  `notes_metadata.body_excerpt`: the first 20,000 characters of exactly the
  text the keyword vector was built from, written in the same UPDATE as the
  vector. Each returned row carries `snippet` from `ts_headline` over that
  excerpt with the tsquery that matched the row, computed by a third
  statement over the page's ids only, plus `matched_in` (title/body) and
  the term count (rendered `terms:`). A row whose excerpt holds no highlight shows the
  excerpt's leading words as `head:` instead; a row with no stored excerpt
  yet (indexed before 031) carries none, and the header counts them.
- **Migration 031 re-heads every stored vector in SQL** — no file access:
  `head(title, aliases) || ts_filter(content_tsvector, '{d}')`, under the
  index generation lock, using the configs the stored keyword fingerprint
  names. Ranking is therefore right at deploy with no operator step. The
  excerpt is populated by the one existing rebuild command
  (`make rebuild-tsvectors`; the k8s Job manifest gains a checked-in copy),
  and a startup WARNING counts the rows still without one. No fingerprint
  change, no startup refusal, no `/health` change.
- **Both move paths re-head the vector**, because the title is derived from
  the path and both paths preserve `content_tsvector` (#127's class, which
  a title in the vector would otherwise reopen).
- **Docstring**: bare terms are ANDed; the `OR`, `"phrase"` and `-term`
  forms are shown; `match`, `snippet`, `head`, `matched_in` and
  `terms` are described. `keyword_search` and `semantic_search`
  remain symmetric peers.

Three new optional settings — `RANK_WEIGHTS`, `KEYWORD_EXCERPT_CHARS`,
`KEYWORD_SNIPPET_ROWS` (design D4) — documented in README, `.env.example`
and `DEPLOYMENT.md`. `FINGERPRINT_VERSION` unchanged. Not in scope: hybrid
fusion (#354), a title-only mode, `ts_rank_cd` normalisation.

## Capabilities

### New Capabilities
<!-- none -->

### Modified Capabilities
- `search-quality`: the keyword planner/ordering requirement (two blocks,
  one transaction, the fallback's construction); the projection requirement
  (`body_excerpt` never shipped; the identity oracle re-scoped to the `all`
  block); new requirements for the weighted vector and rank weights, the
  conjunction-then-disjunction semantics, the page-bounded snippet, and the
  docstring.
- `index-integrity`: the keyword-indexing requirement (head + excerpt in
  every writer, the bounded aliases rule); the move-title requirement (both
  move paths re-head the vector and preserve the excerpt); a new
  requirement for the aliases derivation and the excerpt's provenance.
- `schema-integrity`: migration 031 — the marked excerpt column and the
  in-SQL re-head, idempotent, under the generation lock, refusing an
  unreadable fingerprint.

## Impact

- `src/services/fts.py`: `index_tsvector_sql(content_bind, title_bind,
  aliases_bind)`, `head_tsvector_sql`, `rehead_tsvector_sql`,
  `rank_expression`, `FTS_ALIASES_MAX_CHARS`, `note_aliases`, the
  tsquery-text conjunction parser and the fallback builder.
- `src/config.py`: the `RANK_WEIGHTS`, `KEYWORD_EXCERPT_CHARS` and
  `KEYWORD_SNIPPET_ROWS` settings and their validators.
- `src/services/search.py`: the two ranking statements, the snippet
  statement, the `all`/`any` assembly, telemetry keys.
- `src/services/indexer.py`: the incremental writer, the rebuild writer,
  the id-preserving move UPDATE (`write_tsvector_bounded` binds title,
  aliases and the excerpt per attempt).
- `src/mcp_server/tools.py`: `move_note`'s metadata UPDATE; the
  `keyword_search` renderer. `src/mcp_server/server.py`: the docstring.
- `src/models/db.py`: `NoteMetadata.body_excerpt` (deferred, raiseload,
  marked). `alembic/versions/031_keyword_head_and_excerpt.py`.
- `src/main.py`: the startup excerpt WARNING beside `_check_fts_fingerprint`.
- `scripts/rebuild_tsvectors.py` docstring;
  `deploy/kubernetes/base/rebuild-tsvectors-job.example.yaml`.
- Tests: `tests/test_fts.py`, `tests/test_perf_projection.py`,
  `tests/integration/test_keyword_plan.py`,
  `test_perf_projection_identity_pg.py`, `test_schema_check.py` (head
  `031`), new `tests/integration/test_keyword_ranking_pg.py`, the recall
  suite unchanged and still green.
- Docs: `docs/architecture/search.md`, `indexing-and-embeddings.md`,
  `schema-and-migrations.md`, README (What's in the box, Full-text search,
  Upgrading), `docs/deployment-kubernetes.md`, CLAUDE.md key decisions.
  `.env.example` gains the three settings.
- Not touched: `semantic_search`'s statements, the HNSW path, the
  fingerprints, rate limits, OAuth, transfer, the panel.
