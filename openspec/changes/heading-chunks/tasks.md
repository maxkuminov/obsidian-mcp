## 0. Prerequisites

- [x] 0.1 Check the base: `git log --oneline` contains ed7d334 (change feed), `alembic/versions/033_content_changed_at.py` exists, and no `034_*` exists on `origin/main`. If a different 034 has landed, stop and renumber with the supervisor.
- [x] 0.2 Read `design.md` D1–D8, `docs/architecture/indexing-and-embeddings.md` (chunk-vector reuse D16, the chunk cap, the budget, the fingerprints, the generation lock), `vault-tools.md` "Section addressing" and "The fence grammar every consumer shares", `search.md` (#200 staleness, #280 projection, the harness), and `schema-and-migrations.md` (026/030/032 reconciliation shape).

## 1. Schema: migration 034 and models

- [x] 1.0 Migration 034 also adds `notes_metadata.embedded_input_digest varchar(64)` (nullable, marked, no default, no backfill) under the same reconcile-or-refuse rules. Include it in every 1.x item below.
- [x] 1.1 `src/models/db.py`: `NoteEmbedding.heading_path` (`ARRAY(Text)`, nullable), `section_ordinal` (`Integer`, nullable) and `embed_prefix` (`Text`, nullable), each with its 034 marker comment; `ck_note_embeddings_heading_meta` in `__table_args__`. `NoteMetadata.embedded_chunking_version` (`SmallInteger`, nullable, marker) with `ck_notes_metadata_embedded_chunking_version`. Add a comment block pointing at design D4/D5.
- [x] 1.2 `alembic/versions/034_heading_chunks.py` (`revision="034"`, `down_revision="033"`), in the 030/032 shape.
  - Pin and assert `search_path`; set and `RESET` `lock_timeout` / `statement_timeout`.
  - Verify every pre-existing same-named column and constraint (type, nullability, no default, marker, exact CHECK definition), and refuse by name before changing anything.
  - Add the columns and comments. Add both CHECKs `NOT VALID`, then `VALIDATE`.
  - No backfill and no index.
  - `downgrade()`, in one transaction: `pg_advisory_xact_lock(8029183045093649969)` written literally; `UPDATE notes_metadata SET embedded_content_hash = NULL WHERE id IN (SELECT note_id FROM note_embeddings WHERE embed_prefix IS NOT NULL)`; `DELETE FROM note_embeddings WHERE embed_prefix IS NOT NULL`; then drop only the marked constraints and columns.
- [x] 1.3 Schema gate (`tests/integration/test_schema_check.py` and `make test-schema`).
  - Set `HEAD_REVISION = "034"`.
  - Cases: the fresh shape; the chain from 033 with existing rows, which stay all NULL with vectors untouched; the stamp-back re-run; an impostor of each column (wrong type, unmarked, defaulted) and of each CHECK; both CHECK definitions read through `pg_constraint` and validated; the downgrade's uncertify-and-delete, with mixed pre-034 and prefixed rows; and the downgrade→upgrade round trip with `alembic check` clean after both.
- [x] 1.4 Confirm `tests/integration/test_nonsuperuser_migrations_pg.py` passes unchanged, and that `tests/test_backup_derived_tables.py` needs no change (both tables are already derived).
- [x] 1.5 Run `make test-schema` (never concurrently with `make test-integration`), then `make db-check`: "No new upgrade operations detected".

## 2. Chunker (offline, `src/services/embeddings.py`)

- [x] 2.1 Make the scanner shareable: expose `_scan_headings` and the one-terminator body-start step from `src/services/vault.py` under public names, or import them as they are. Do not copy them. The heading-path chain uses `_path_chain_match`'s depth rule.
- [x] 2.2 `ChunkSpec` (frozen: `text`, `heading_path: tuple[str, ...]`, `section_ordinal: int`) and `chunk_note_bounded(body, *, chunk_size, overlap, max_chunks) -> tuple[list[ChunkSpec], bool]`, per design D1:
  - one `scan_fences(body, context=BODY)` call;
  - own-text spans up to the next heading of any depth;
  - section 0 for the preamble;
  - whole-body fence spans removed per section;
  - `chunk_text_bounded` per section, with one cap counter across sections (stop one past the cap);
  - heading lines excluded;
  - components cut at 200 characters with `…`;
  - the zero-chunk fallback to `chunk_text_bounded(clean_for_embedding(body))` as section 0.
  - It runs under `asyncio.to_thread` at its call sites, as today.
- [x] 2.3 `CHUNKING_VERSION = 1`, with a comment: NULL means pre-034, and any change to sectioning, the prefix or `embedding_input` bumps it (embedding-providers requirement).
- [x] 2.4 Point the exclusion sweep's zero-chunk probe (`indexer.py`, around the `chunk_text_bounded` call near the sweep) at `chunk_note_bounded`, so "no chunks" means the same thing in both places.
- [x] 2.5 Tests (`tests/test_heading_chunks.py`), one per spec scenario:
  - a `#` line inside backtick, tilde, longer-closer, indented and unterminated column-zero fences;
  - inline code holding a heading;
  - LF, CRLF and lone-CR bodies;
  - valid, whitespace-only and defective frontmatter;
  - preamble only, headings only (the fallback), a parent with only subsections, empty sections, and a section of exactly one window, one window plus one character and three windows;
  - no headings: byte-equal to `chunk_text_bounded(clean_for_embedding(body))` on a corpus of fixtures;
  - duplicate sibling headings (distinct ordinals), a heading text containing `/`, a 300-character heading;
  - the cap across sections, and exactly at the cap.
- [x] 2.6 Parity property test: for generated and fixture notes, every chunk with ordinal N ≥ 1 satisfies `extract_section_parts(body, f"#{N}")` succeeding and the chunk text being a substring of that section body with the whole-body fence spans removed. Every chunk with ordinal 0 is a substring of the preamble or of the fallback body. The concatenation of section cleaned texts equals `clean_for_embedding(body)` minus heading lines.

## 3. Prefix and reuse

- [x] 3.1 `EMBED_PREFIX_MAX_CHARS = 300`; `build_embed_prefix(title, heading_path, tags) -> str` per design D2 (80-character component cut, outer-heading `…` replacement, all-or-nothing tags line, whitespace collapsed); `embedding_input(prefix, chunk) -> str` as the single formatter.
- [x] 3.2 `embed_note`:
  - build the specs and prefixes;
  - send `embedding_input(...)` texts with `kind="document"`;
  - insert rows with `chunk_text`, `heading_path`, `section_ordinal` and `embed_prefix`;
  - read title and tags from the loaded `NoteMetadata`.
- [x] 3.3 `_lookup_reusable_vectors`:
  - also read `embed_prefix` and the note's `embedded_chunking_version`;
  - return nothing unless the fingerprint is `MATCH` and the version equals `CHUNKING_VERSION`;
  - key on `(embed_prefix, chunk_text)`, skipping NULL prefixes.
- [x] 3.4 `_generation_matches`: `reused_rows` maps row id to `(chunk_text, embed_prefix)`, and the under-lock recheck compares both.
- [x] 3.5 `certify_embedded`: the conditional UPDATE also sets `embedded_chunking_version = CHUNKING_VERSION` and `embedded_input_digest = <sha256 of the canonical JSON of the ordered provider inputs>` (`[]` for empty or excluded notes, so every caller passes the inputs or the empty list). `expire_on` expires all three attributes. The legacy uncertified path (tests only) sets them too.
- [x] 3.5a Invalidation on prefix inputs (design D3a): every index-pass statement that writes `title` or `tags` with an unchanged `content_hash` (extraction-version re-upsert, re-derive, the id-preserving move branch) also sets `embedded_content_hash = NULL` in the same statement when either is `IS DISTINCT FROM` the new value. Add the full-hash backstop: for each certified note read under the current chunking version, recompute the input digest and clear certification in the pass transaction on a mismatch or NULL. Tests: a tag-grammar bump with identical cleaned text invalidates; unchanged title and tags do not; the backstop catches a hand-corrupted digest; no provider call is made by the backstop.
- [x] 3.6 Tests:
  - prefix shape, caps, tags omitted when long, title-only for section 0;
  - reuse hits on an append;
  - an inserted heading reuses vectors and re-inserts rows with new ordinals;
  - a tag change and a rename-title change miss;
  - a NULL-prefix row and an old version never reuse;
  - the under-lock recheck fails when a reused row's prefix changed;
  - the existing #281 reuse tests updated to the new key, not deleted.

## 4. Provider `kind` and the query instruction

- [x] 4.1 `src/config.py`: `embedding_query_instruction: str = ""` with validation (at most 1,000 characters; reject NUL and C0 controls other than `\n` and `\t`). Settings tests for the default, a valid multi-line value, too long, and NUL/CR.
- [x] 4.2 `EmbeddingKind = Literal["query", "document"]`. Protocol `embed_one(text, *, kind)` and `embed_batch(texts, *, kind)`. `get_embedding(text, *, kind)` and `get_embeddings_batch(texts, *, kind)` are required keywords that apply the instruction for `query` only, in this one place. `OllamaProvider` and `OpenAIProvider` accept and ignore `kind`.
- [x] 4.3 Call sites:
  - `semantic_search` → `query`;
  - `prewarm_search_caches` (`indexer.py`) → `query`;
  - `src/main.py` warm-up: route it through `get_embedding(..., kind="query")`;
  - `embed_note` → `document`.
  - Grep `src/` and `scripts/` for any other call.
- [x] 4.4 AST sweep test: every call to `get_embedding`, `get_embeddings_batch`, `embed_one` or `embed_batch` under `src/` and `scripts/` passes `kind=`.
- [x] 4.5 `src/services/index_state.py`: `retrieval_fingerprint()`, canonical JSON of the embedding fingerprint fields plus `chunking_version` and `query_instruction_sha256` (16 hex digits, or null). Assert in a test that `embedding_fingerprint()` is byte-identical with and without an instruction set.
- [x] 4.6 Startup logs `Embedding retrieval: <retrieval fingerprint>` once, after the embedding transport line, and never the instruction text. Test that the log carries the digest and not the text.
- [x] 4.7 Tests:
  - the default input equals the query;
  - an instructed query input is instruction + query;
  - documents never carry it (a mocked provider records inputs from one `semantic_search` and one `embed_note`);
  - changing only the instruction leaves the stored-fingerprint comparison `MATCH`.

## 5. Indexer: backlog, ordering, budget

- [x] 5.1 One predicate helper for "vectors not current", `embedded_content_hash IS DISTINCT FROM content_hash OR embedded_chunking_version IS DISTINCT FROM :cv`, and one for "not re-chunked" (`embedded_chunking_version IS DISTINCT FROM :cv`, used by the response marker and the health state). A test greps `src/` for any `embedded_chunking_version <` or `>` comparison. Both are shared by `_embed_vault_pinned`'s two SQL branches, `_vectors_not_current` in `src/control_panel/routes.py`, and anything else that counts pending. The helper is defined once (SQL text plus SQLAlchemy expression, with a test that they agree on a fixture table).
- [x] 5.2 Backlog `ORDER BY`: content-stale first, then version-only, each `modified_at DESC`. The selected row carries a `version_only` flag.
- [x] 5.3 `EmbedBudget`: applied to version-only notes whatever `_active_scope_count`. Multi-scope passes keep applying it to both segments. At-least-one-note and note-boundary rules unchanged. The stop logs one WARNING per user per pass and is not a failure.
- [x] 5.4 Confirm the excluded branch and the `CERTIFIED_EMPTY` branch stamp the version through `certify_embedded`, and that the exclusion sweep's "certification-current" selection is unchanged (design D4: version-only rows are owned by the backlog).
- [x] 5.5 Confirm nothing in `indexer_health` keys on backlog size or pass duration, and add a test: a 10-pass simulated transition with a healthy provider leaves `/health` `ok`.

## 6. Rendering and docstrings

- [x] 6.1 `semantic_search` (service) and `find_related_stmt` project `NoteEmbedding.heading_path` and `section_ordinal`, never `embed_prefix` or `embedding`. The result dict gains `section`, which is `None` when the row is stale or its path is NULL. Update the projection-identity tests (`test_perf_projection_identity_pg.py`) to the new column list.
- [x] 6.2 One renderer helper, `_section_locator(section) -> str`: ` · § <change_feed.quote(joined)> (#N)`, ` · § (top of note)`, or `""`; the 160-character path cut keeps the inner components behind `…/`. Use it in `semantic_search_impl` and `find_related_impl` on the result line, after `_degradation_suffix`.
- [x] 6.3 The services pass the uncut `chunk_text` (remove `semantic_search`'s `[:500]`). Add one helper, `_preview_line(text, limit=200)`: collapse each maximal `str.isspace()` run to one space, trim, escape the remaining C0/DEL/C1 as `\uXXXX`, then cut. Both renderers use it and keep their own ellipsis. Tests: 600 leading spaces then `answer`; U+001B and U+0085; a forged row.
- [x] 6.3a Transition marker: in both tools, run the vector query and an owner-scoped `SELECT count(*) FROM notes_metadata WHERE <owner> AND embedded_chunking_version IS DISTINCT FROM :cv` (the one predicate of task 5.1; never `<`) in one `REPEATABLE READ, READ ONLY` transaction. While the count is above zero, emit `index_transition: N notes not yet re-chunked; cross-generation ranking is approximate, re-run after the backlog drains` directly after the header, on every query-running path including empty results. Render tests: present, absent at 0, owner-scoped, on the empty path.
- [x] 6.4 Docstrings in `src/mcp_server/server.py` for `semantic_search` and `find_related`: the locator's two forms, its omission on stale and pre-034 rows, and the `read_note(path, section="#N")` follow-up, in peer wording. Check the tools-list fixtures (`tools_list_schema_additions.json` and any description pins) and update only what the description change requires.
- [x] 6.5 Render tests:
  - both locator forms;
  - none for stale or NULL;
  - forgery: a heading containing `"`, `(#2)`, ` ` and control characters stays in one literal, and the only unquoted `(#N)` is the stored one;
  - the long-path cut;
  - a chunk holding `\n- **Forged** …` stays on its `>` line;
  - row count and header unchanged;
  - `tests/test_issue_343_tool_metadata.py` (descriptions at most 4,000 characters, mention the locator).

## 7. PostgreSQL integration tests (`tests/integration/`)

- [x] 7.1 `test_heading_chunks_pg.py`, transition:
  - seed pre-034 rows (NULL locator, NULL version) for several notes;
  - startup proceeds and the fingerprint is unchanged;
  - `semantic_search` returns them unstale with previews and no locator;
  - one pass re-embeds within the budget; mixed results render correctly;
  - after convergence every row has a prefix and every note has version 1;
  - the backlog is empty and the pending count is zero.
- [x] 7.1a Transition membership (Codex r1 B1): with `limit=1`, legacy note A nearer than B before deploy, and a budget that re-embeds B on pass 1 and A on pass 2, assert:
  - after pass 1 the response carries `index_transition:` whichever note it returns, and `/health` reports `indexer.rechunk: "in_progress"` with `status` `ok` and no transition number in the body;
  - after pass 2 the line is absent and `indexer.rechunk` is `"complete"`;
  - rollback (Codex r2 B2): with `CHUNKING_VERSION` patched to 2, re-embed B and C, then patch it back to 1 with no schema downgrade. The marker counts B and C and stays until every note in scope is stamped 1. The backlog re-selects B and C, and no version-2 row is reused;
  - another tenant's legacy notes never add the line to this caller.

  Implement `indexer_health`'s per-scope count in process memory, set by each embed stage under the one predicate. `/health` exposes only `indexer.rechunk: "in_progress" | "complete"`, never the count, never in `status`. Document this privacy contract in `indexing-and-embeddings.md`, beside the existing "counts only, no path/error/user id" rule: the field is non-volumetric because `/health` is unauthenticated.
- [x] 7.2 Ordering and budget: with version-only notes pending and one edited note, the edited note is embedded first; single-user mode stops at the time budget at a note boundary with no run error.
- [x] 7.3 Reuse on PostgreSQL: append, inserted heading, tag change, and a reset racing the provider call (extend `test_perf_chunk_reuse_pg.py`).
- [x] 7.4 End-to-end parity: index a fixture vault, then for every stored row with ordinal N ≥ 1 call `read_note_impl(path, section="#N")` and assert the containment of design D1.
- [x] 7.5 Downgrade on real data: mixed rows → `downgrade 033` → prefixed rows gone, their notes uncertified, legacy rows intact → `upgrade head` → clean.
- [x] 7.6 Run `make test-integration`. A local `pytest tests` alone skips these.

## 8. Search-evaluation harness

- [x] 8.1 For DB-backed arms whose membership includes `semantic`, record `retrieval_fingerprint` and `legacy_chunked_notes` in the staged `meta` and in the report's arm entry. `legacy_chunked_notes` is one read-only `SELECT` with the provenance owner's predicate (the export's `USER_ID`, NULL-safe), covering all owners only for a `"multi"` report. It is null when the column is absent. A `runfile:` arm carries its meta's values or null. Fixture arms gain no key.
- [x] 8.2 Tests: two tenants, `USER_ID=7` export, user 8 with 37 legacy notes. The staged meta and report carry 0, and mutating user 8's rows changes no byte of either (Codex r1 B2). The fixture golden file is unchanged; an integration test on a copy gives `legacy_chunked_notes` 0 after a re-embed and N before; a runfile without the fields scores with nulls; the report-content check (no query text, no paths) still passes.
- [ ] 8.3 Operator run, before the PR is opened for merge (design D8 steps 1–6): baseline generation from the incumbent tag on the pre-034 copy; upgrade and re-embed with the candidate image; candidate generation with `runfile:` baseline. Record in this file the chunk count after re-embed, the per-pass durations, and the total re-embed time (these replace the design's estimate band), and attach `report.json` to the PR if `attachable: true`. `CORPUS_LOSS` or `legacy_chunked_notes > 0` means the run is not evidence.
- [ ] 8.4 Re-run `tests/integration/test_search_recall.py` against the re-embedded copy's chunk count. If filtered recall drops below the SLO, record it and the `hnsw.max_scan_tuples` remedy before shipping (design Risks).

## 9. Documentation (last commits of the feature branch)

- [x] 9.1 README: `EMBEDDING_QUERY_INSTRUCTION` in the settings table, with the Qwen3 example in double quotes and the statement that the default model is unchanged; the `§` locator in the `semantic_search` / `find_related` descriptions; a note that changing the instruction needs no reset, and changing the model still does.
- [x] 9.2 `.env.example`: `EMBEDDING_QUERY_INSTRUCTION=` (empty) with a comment covering queries only, verbatim, no reset needed, the Qwen3 template, and `\n` inside double quotes. Grep that every new setting appears in both README and `.env.example` (feedback: public docs for settings).
- [x] 9.3 DEPLOYMENT.md:
  - an "Upgrading to 034 (heading-aware chunks)" note: what the migration does, that startup does not refuse, the background re-embed and its measured duration, what the dashboard and logs show, `/health` unaffected, Ollama sharing, and rollback via `alembic downgrade 033` with its cost;
  - "Measuring search quality": the `runfile:` before/after recipe for a re-chunk or re-embed (design D8), including the read-only vault mount and the budget override on the one-off container.
- [x] 9.4 `docs/architecture/indexing-and-embeddings.md`: a section "Heading-aware chunks (#355)" with sectioning, the zero-chunk invariant, the prefix, the reuse key (amend D16), the chunking-version backlog and why it is outside the fingerprint, the budget in every mode, the rolling-deploy window, and L1–L10. Amend "Indexing decisions" (no longer "512 token chunks" only) and the fingerprint section with the instruction and retrieval-fingerprint rule.
- [x] 9.5 `docs/architecture/search.md`: the locator and its forgery rule, stale-row withholding, one-line previews, the harness's new arm fields, and the `max_scan_tuples` note updated with the new chunk count.
- [x] 9.6 `docs/architecture/vault-tools.md` "Section addressing": the chunker is a consumer of `_scan_headings`. State the containment guarantee and that a change to the heading scan now also needs a `CHUNKING_VERSION` bump.
- [x] 9.7 `docs/architecture/schema-and-migrations.md`: a "034" section (columns, CHECKs, no backfill, reconcile-or-refuse, the downgrade's delete-and-uncertify).
- [x] 9.8 CLAUDE.md: one key-decisions bullet (heading-aware chunks; prefix; reuse key; per-note `CHUNKING_VERSION` outside the fingerprint; the instruction is query-only and unfingerprinted; the locator). Update "Indexing decisions" wording if it states 512-token windows only.

## 10. Verification gates

- [x] 10.1 `openspec validate heading-chunks --strict`.
- [x] 10.2 `openspec-verifier` subagent (a non-author) audits the implementation against these deltas and runs `make audit`, `make test-schema`, `make test-integration`, `make search-eval` (fixture) and `make db-check`. Iterate to zero blocking gaps. — verifier: 0 blocking, 8 minor, all folded.
- [ ] 10.3 Adversarial Codex (mandatory: search path, chunking and embedding). Commit first, pin `-m gpt-daybreak-blue-latest`, `--sandbox read-only`, stdin from a file, run in the background. Frame wrong as: a silently wrong or misleading search result (wrong ordinal, forged locator, reused vector for a different input, a note dropped from semantic search), a refused or crash-looping deploy, or `/health` degraded by a healthy transition. Attack:
  - ordinal parity on adversarial fences and terminators;
  - the reuse key and under-lock recheck;
  - the backlog predicate parity across its three users;
  - the old-build overlap;
  - the downgrade;
  - locator and preview forgery;
  - the instruction's reach into documents.

  Two rounds by default, triaged per the workflow. Record the rounds in `design.md`, and record declined findings as accepted limitations.
- [ ] 10.4 After fixes, re-run 10.1 and the verifier's command set once on the final tree.

## 11. Archive, merge, deploy, live check

- [ ] 11.1 `openspec archive heading-chunks -y` as the last commit of the feature branch, with the docs above. One PR, `Closes #355`; reference #367 as partially addressed (the reranker remains), not closed.
- [ ] 11.2 Merge. Deploy is the image workflow and Flux. Watch the rollout: startup line `Embedding retrieval:`, no fingerprint refusal, migration 034 applied, `make db-check` clean against production.
- [ ] 11.3 Watch the re-embed: the dashboard pending count falls each pass, `indexer_runs` has no error rows, `/health` stays `ok`. Record the actual duration here.
- [ ] 11.4 Live end-to-end (no user-representative pass; the consumer is an agent): call `semantic_search` on a known-sectioned topic and `find_related` on a sectioned note, then `read_note(path, section="#N")` for a returned locator and confirm the heading matches; call `semantic_search` with a query whose best note is not yet re-embedded (during the transition) and confirm the locator is absent and the preview is present. Report which tools were called.
- [ ] 11.5 Cut the release tag at the merge commit.
