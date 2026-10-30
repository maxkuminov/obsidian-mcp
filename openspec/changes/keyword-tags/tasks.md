## 0. Pre-flight

- [ ] 0.1 Worktree off `origin/main` at or after `425bc22`; confirm `alembic/versions/034_heading_chunks.py` exists and `tests/integration/test_schema_check.py` reads `HEAD_REVISION = "034"` before touching it; no other change claims revision 035
- [ ] 0.2 Read `docs/architecture/search.md` (keyword section), `indexing-and-embeddings.md` (tsvector writers, the rebuild's certification, the fingerprint, both move paths), `schema-and-migrations.md`, the archived `2026-10-29-keyword-search-ranking` design (D1, D5, D6, L1–L15) and this change's `design.md`

## 1. Builders and the tags rule (`src/services/fts.py`) — D1, D2

- [ ] 1.1 `head_tsvector_sql`, `index_tsvector_sql`, `rehead_tsvector_sql` gain a required keyword `tags_bind: str | None`; a string adds the tags parts `setweight(to_tsvector(cfg, :<tags_bind>), 'B')` for every config after the aliases parts and before the body; `None` omits them; add `tags_tsvector_sql(tags_bind, configs=None)` (the tags parts alone, for 035's predicate); update the module docstring's "vector's shape" section
- [ ] 1.2 `FTS_TAGS_MAX_CHARS = 2048`; `tags_text(tags: list[str] | None) -> str` per design D2 (one line per tag: the tag, then its non-empty `/`-segments space-separated; `\n`-joined; NUL/U+0002/U+0003 removed; cut at a whole-line boundary)
- [ ] 1.3 `alembic/versions/031_keyword_head_and_excerpt.py`: its one `rehead_tsvector_sql(...)` call passes `tags_bind=None`; nothing else in 031 changes
- [ ] 1.4 Unit tests (`tests/test_fts.py`, `tests/test_fts_ranking.py`): exact fragment strings for one, two and three configs with and without tags; 031's fragment byte-identical to its pre-change text; `tags_text` table (flat, nested, deep `a/b/c`, leading `#`, duplicates, empty, `None`, sentinel removal, 1,000 × 30-character tags → ≤ 2,048 and ends on a whole line)
- [ ] 1.5 AST guard (beside `test_to_tsvector_is_composed_only_by_the_builders_and_031`): every call of the three builders under `src/` passes a non-`None` `tags_bind`; the only `None` is in `alembic/versions/031_*.py`; 035 calls only builders (the `to_tsvector(` guard stays green without widening its allow-list)

## 2. Migration 035 — D5

- [ ] 2.1 `alembic/versions/035_keyword_tags.py` (`revision "035"`, `down_revision "034"`): module docstring on 031's model (what, why in SQL, the two skip predicates and why `rehead` needs them, the configs rule, 54000, downgrade, the deploy order); `lock_timeout 10s`, `statement_timeout 15min`, pinned/asserted `search_path` and qualified table, all `RESET`; generation lock first; stored-fingerprint configs (present / absent → settings / unreadable → refuse naming the key); **size preflight first** (design D5: read-only estimate `S(ts_filter(v,'{d}')) + S(head(title, aliases, tags))` per row the rewrite would change, abort before any UPDATE naming the count and ids when any exceeds 1,048,511); then per-row UPDATE over `id, title, frontmatter, tags` with both predicates in the UPDATE's `WHERE`, no savepoints; **all-or-nothing**: any error, 54000 included, re-raised as a failure naming the row id and the SQLSTATE (no path, no text), nothing committed; summary line (re-headed / already tagged / no tag lexemes) via `_say`
- [ ] 2.2 `downgrade()`: generation lock, `ts_filter(content_tsvector, '{a,d}')` on every row carrying a `B` lexeme, count logged
- [ ] 2.3 Offline literal test (as `tests/test_migration_031_literals.py`): 035's lock-key and fingerprint-key/version literals equal `index_state`'s
- [ ] 2.4 `tests/integration/test_schema_check.py`: `HEAD_REVISION = "035"`; cases per the schema-integrity delta — tagged / untagged / stop-word-tagged rows over a 034 database (tagged gains `B`, others byte-equal), a D-only tagged row re-headed byte-exactly, stamp-back byte-idempotence, fingerprint present / absent / unreadable, a seeded near-limit tagged row makes the preflight abort before any UPDATE (count and id named, `alembic_version` `034`, every vector byte-equal, `alembic check` clean afterwards), an ordinary database passes the preflight, the `DEPLOYMENT.md` operator statement (verbatim from design D5) run against a seeded D-only tagged row that passes `S(v) + S(B)` but exceeds `S(D) + S(A + B)` reports its id, and 035 then aborts naming the same id, a unit test of `S` against PostgreSQL's reported size; with the preflight bypassed, a 54000 row fails the whole migration naming its id with `alembic_version` still `034` and every vector unchanged, any other row error likewise, downgrade leaves no `B` lexeme and keeps every `A`/`D` lexeme (a tag-and-body lexeme keeps its `D` positions), `alembic check` clean after upgrade, downgrade and re-upgrade
- [ ] 2.5 `make test-schema` green (own container/port; never concurrently with `make test-integration`); `test_nonsuperuser_migrations_pg.py` still green (035 needs no privilege beyond ownership)

## 3. Writers — D6

- [ ] 3.1 Incremental pass: `index_tsvector_sql("content", "title", "aliases", tags_bind="tags_text")` (a bind name that cannot collide with the `tags` column binds), bound `tags_text(n["tags"])` from the same `to_upsert` entry; `write_tsvector_bounded` and the poison floor bind it on every attempt (the halving retreat still shrinks only the body)
- [ ] 3.2 Indexer move-pair UPDATE: `rehead_tsvector_sql("head_title", "head_aliases", tags_bind="head_tags")`, bound `tags_text(e["tags"])`
- [ ] 3.3 `move_note`: the destination read derives tags with `extract_tags(body, frontmatter)`; the fallback selects `frontmatter` and `tags` in its one statement and binds `tags_text(stored_tags)`; `head_tags` bind on the re-head expression
- [ ] 3.3a `render_keyword_results` / `_snippets`: distinguish a computed empty headline (empty excerpt: no second line, not counted as missing) from a headline not computed and from a NULL excerpt — no truthiness test decides between them
- [ ] 3.4 `_rebuild_tsvectors_pinned`: bind `tags_text(extract_tags(content, frontmatter))` from the verified bytes
- [ ] 3.5 Confirm by test, not by reading: a frontmatter-only tag edit rewrites the vector in the next pass (index-integrity scenario "A tag edit alone updates the vector"); no `CURRENT_EXTRACTION_VERSION` bump; `embedded_content_hash` behaviour (#355) unchanged

## 4. `matched_in` and the render — D4, D7

- [ ] 4.1 `search._snippets`: project the six flags (`{a}`, `{b}`, `{d}`, `{a,b}`, `{a,d}`, `{b,d}`); derive `matched_in` per design D4 (singles in order `title`, `tags`, `body`; else first pair `title+tags`, `title+body`, `tags+body`; else `title+tags+body`); the snippet-vs-`head:` rule unchanged (headline only when `{d}` matched)
- [ ] 4.2 `keyword_search` docstring (`src/mcp_server/server.py`): tags indexed below title/aliases and above the body; `matched_in` parts `title`, `tags`, `body` and the `+` split form; stays ≤ 4,000 characters (`tests/test_issue_343_tool_metadata.py`); `render_keyword_results` docstring follows; `hybrid_search`'s docstring needs no change (verify)
- [ ] 4.3 Unit tests: the eleven `matched_in` values from flag combinations (a table test over every consistent combination of the six flags), render of `matched_in: tags` with `head:`, docstring contains the tags statement

## 5. PostgreSQL integration tests (`tests/integration/test_keyword_ranking_pg.py`, `test_hybrid_search_pg.py`)

- [ ] 5.1 A note found only by its tag ranks between a title hit and a body hit for `keyword_search("hoa")` (titled `HOA` first, tagged second, three body occurrences third) and renders `head:` with `matched_in: tags`
- [ ] 5.2 The issue's shape: tag `HOA`, body `the siding complaint went in` — `keyword_search("HOA siding complaint")` returns it `match: all`, `matched_in: tags+body`, `head:`; `hybrid_search` on the same corpus carries it in the keyword arm with that `matched_in`
- [ ] 5.3 Nested tag `#ctx/phone` found by `phone`, `ctx` and `ctx/phone`; `matched_in: title,tags` case; an inline tag inside a fence is not indexed
- [ ] 5.4 `test_every_writer_produces_the_same_shape` extended with tags (incremental, rebuild, 035 over a D-only row; `english` and `english,simple`); untagged vector byte-equal to the pre-change shape
- [ ] 5.5 Both move paths keep `hoa` at `B` and the note findable by tag at its new path; the existing rename scenario still passes
- [ ] 5.5a `move_note` stored-tags fallback (index-integrity scenario): multi-user, two tenants holding `Alpha.md` with different tags; force the destination read to fail after the rename; assert user 1's own stored tags were bound (owner-scoped), `hoa` at `B`, the tag search returns `Beta.md`, and both still hold after the next incremental pass that only refreshes the stat
- [ ] 5.5b Empty-body tags-only note: `matched_in: tags`, no `snippet:`/`head:` line, not in the no-excerpt count
- [ ] 5.5c Identity oracle (`search-quality` "Read paths…"): tag-contribution exemption per its scenario (`hoa`, `budget forecast`, `forecast`, `forecast -hoa`); every non-exempt row satisfies (a) and (b)
- [ ] 5.5d Response bounds: `HYBRID_ROW_STRUCTURE_MAX = 345`, `HYBRID_RERANK_ROW_STRUCTURE_MAX = 367`; the fixed-bound tests (`tests/test_hybrid_rerank.py` arithmetic and the hybrid structure test) recomputed with `matched_in: title+tags+body`; minimum-cap tests for `rerank=false` (cut form, 40–45) and `rerank=true` (≤ 1,000, omitted form when the cut form does not fit); `docs/architecture/search.md`'s pinned numbers updated
- [ ] 5.6 A tag edit alone: `alpha` → `beta`, one incremental pass, `beta` found and `alpha` not
- [ ] 5.7 The worst-case snippet latency test gains tag-only rows and stays under 300 ms; record the measurement in `design.md`
- [ ] 5.8 `make test-integration` green; full offline `pytest tests` green

## 6. Docs (last commits of the feature branch)

- [ ] 6.1 `README.md`: keyword-search description (tags indexed, `matched_in: tags`), the `RANK_WEIGHTS` row (`B` = tags), upgrade section entry for 035
- [ ] 6.2 `docs/architecture/search.md`: the vector shape, `matched_in`'s eleven values and six flags, the tags-only `head:` rule, the hybrid inheritance, L1–L10 where they bind
- [ ] 6.3 `docs/architecture/indexing-and-embeddings.md`: the writers list with the tags bind, the move re-head with tags, `tags_text`, why a tag edit rewrites the vector; `docs/architecture/schema-and-migrations.md`: 035
- [ ] 6.4 CLAUDE.md key decision: the keyword vector is `A(title) || A(aliases) || B(tags) || D(body)` (#344, #391, migrations 031/035); 035's re-head and skip predicates in one clause
- [ ] 6.5 `DEPLOYMENT.md` upgrade note "Upgrading across migration 035 (tags in keyword search, #391)": rewrites tagged vectors in place in seconds, under the generation lock; stop the app before migrating (`make deploy` does it when a migration is pending; k8s `Recreate`); no new setting, nothing re-embedded; optional `make rebuild-tsvectors` for byte-exact vectors; 035 is all-or-nothing and starts with a size preflight; the note gives the operator the design's read-only preflight SQL to run against the live database **before merging** (counts and ids only; it flags every D-only candidate, and the note says such a row must be shortened or rewritten through the halving retreat first) and states that an abort under k8s `Recreate` is downtime until the digest is reverted — on a failure naming a note id the database is still at 034: serve the previous image (on k8s revert the digest), shorten that note's body or tags, let it re-index, deploy again; rollback is `alembic downgrade 034` then the previous image
- [ ] 6.6 Public-repo grep: no host paths, hostnames or secrets in the diff; no new setting, so `.env.example` unchanged (verify)

## 7. Gates

- [ ] 7.1 `openspec validate keyword-tags --strict`
- [ ] 7.2 Search-eval gate (search-path change), runfile workflow (design D7): restore a `BACKUP_FULL=1` copy **at 034**, `make search-eval-snapshot`; at the incumbent commit run `keyword`, `semantic` and the hybrid arm over it and **retain** that complete generation; migrate the copy to 035; at the candidate commit run the same arms with the same qrels, snapshot and selection; score with the retained files as `runfile:<path>` arms and the incumbent keyword runfile as `BASELINE` (then hybrid); hashes must match; attach `report.json` only if `attachable: true`; `CORPUS_LOSS` invalidates; expected movement in keyword and hybrid arms only
- [ ] 7.3 `openspec-verifier` subagent (not an implementer) against the deltas; iterate to zero blocking gaps
- [ ] 7.4 Adversarial Codex (mandatory: search path, `move_note`, an in-place vector rewrite), framed per CLAUDE.md (silently wrong search results; destructive writes); two rounds by default, triage per the workflow file, declined findings recorded as accepted limitations in `design.md`
- [ ] 7.5 `openspec archive keyword-tags -y` and the docs above as the last commits; one PR, `Closes #391`

## 8. After merge

- [ ] 8.1 Watch the rollout; confirm the migrate init container logged 035's summary line and `alembic check` is clean
- [ ] 8.2 Live end-to-end against the deployed server (no `user-representative` pass — MCP-only change): `keyword_search` for a word that is only a tag on a known note (`matched_in: tags`, `head:`), `keyword_search("HOA siding complaint")`, a nested-tag segment query, `hybrid_search("HOA siding complaint")` (keyword arm present for the HOA note), and `move_note` of a scratch tagged note followed by a tag search; report which tools were called
