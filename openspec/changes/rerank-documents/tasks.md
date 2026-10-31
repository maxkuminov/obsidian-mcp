Refs #402. Read `proposal.md`, `design.md` (D1–D10, the owner decisions, L1–L11, the Codex round-1 record) and `specs/` before starting. **Base check:** `git log` contains c491326, `src/services/rerank.py` defines `build_document` and `bounded_text`, and `EMBEDDED_TEXT_FORMATTERS` in `src/services/embeddings.py` has keys `{1, 2}`. **Sequencing:** this change merges **after `hybrid-any-fill`** (#403). The eval gate (group 7) needs that change's `python -m scripts.search_eval gate`. Rebase onto it once it has merged, and do not restate or touch its fusion offset or snippet order. **Migration: none.** This is a search-path change, so groups 7 and 8 are gates, not optional.

## 0. Pre-implementation

- [x] 0.1 Codex spec review round 1 (FAIL: 3 BLOCKER, 3 MAJOR, 1 MINOR), folded and recorded in `design.md`. `openspec validate rerank-documents --strict` passes.
- [x] 0.2 Codex spec review rounds 2 and 3 (verification of the round-1 fixes only), folded; no round 4 by the review budget.
- [ ] 0.3 Confirm the rebase onto `hybrid-any-fill` before group 7.

## 1. Builder and allocation (`src/services/rerank.py`)

- [x] 1.1 `RERANK_MAX_DOCUMENTS_PER_NOTE = 4` (a constant). Add `embedding_document(prefix, chunk_text, stamp)`, which returns `(prefix + "\n\n" + EMBEDDED_TEXT_FORMATTERS[stamp](chunk_text))[:RERANK_DOC_CHARS]` **only when `stamp` is a key**. A NULL or unknown stamp gives the note the single title document (D1; import from `embeddings`, never copy a formatter). `build_document` stays for the legacy cases in the D1 table.
- [x] 1.2 `allocate_documents(candidates, budget) -> list[list[Doc]]`: pure and deterministic, exactly the D2 two passes. Pass 1 gives every candidate its primary, in unreranked order. Pass 2 takes the secondaries in `(distance, file_path, chunk_index)` order while fewer than `budget` documents are allocated, skipping a note at 4. `Doc` carries the text plus an opaque back-reference to its chunk row (or `None` for a title or excerpt document) for D4.
- [x] 1.3 `rerank_candidates(query, groups)`: the flattened list in one `score` call. Validation is unchanged, judged over the document count. Per-note `max`; `best` = the first maximal document in allocation order; order `(−max, position)`. `RerankOutcome` gains `best` and `documents`, and `candidates` = groups (= the incumbent's candidate count). The state precedence and `no_candidates` are unchanged (zero groups means no request).
- [x] 1.4 Unit tests, **the allocation bound proof test**: a property/table test over `budget ∈ {50, 60, 100, 200}` × chunk distributions (one note with 60 rows, 50 single-row notes, 250 rows over 50 notes, stale, unknown-stamp and keyword-only mixes). It asserts:
  - every candidate holds ≥ 1 document, with its primary first;
  - `documents ≤ min(budget, 4·m)`, and `≤ 4` per note;
  - no document is a row outside the fetched list;
  - secondaries are in distance order (the spec's A/B scenario), and equidistant ties fall to `(file_path, chunk_index)`;
  - the same input twice gives the same allocation;
  - `budget = 50` with 50 candidates gives exactly the 50 primaries.

  The builder tests: the prefix is always whole at 300 + 2,000; a stamp-1 vs stamp-2 chunk with a 9-space run; stamp 3 (unknown) gives the title-only document; a NULL prefix gives the legacy document; the code-point cut on an astral character at the boundary.

## 2. Candidate services

- [x] 2.1 `semantic_arm_rows(with_rerank_text=True)`: project `NoteEmbedding.embed_prefix` and `NoteMetadata.embedded_chunking_version` **only** under the flag (D6). Before the dedup discards rows, attach `rerank_chunks` to each deduped row: the note's fetched rows in re-sort order, cosine-best first, and `[]` for a single-title-document note (stale, unknown stamp). Keep `rerank_text` only if the incumbent arm needs it (D10), and never put it on a `rerank=false` row. `rows`, `chunk_rows`, `exact_fallback` and every legacy field are unchanged.
- [x] 2.2 Statement-capture test: the `rerank=false` overfetch SQL is byte-identical to c491326's, and the `rerank=true` SQL differs only by the two projected columns (same `WHERE`, `ORDER BY`, `LIMIT`, `SET LOCAL`s and statement count), for both `semantic_search` and `hybrid_search`. **Projection assertion:** the `rerank=true` overfetch projects exactly `embed_prefix` and `embedded_chunking_version` in addition and never the vector, while `rerank=false` and `find_related` project neither (the `search-quality` carve-out).
- [x] 2.3 `hybrid.py`: `HybridRow.rerank_chunks`, set from the semantic-arm row when `rerank_candidates` (D7). `_fill_excerpts` is untouched. No new statement.

## 3. Tools: per-chunk candidates, per-note max, preview and locator parity

- [x] 3.1 `_semantic_search_reranked`: build the primary and secondary documents per candidate (D1/D2), call `allocate_documents(…, settings.rerank_max_documents)`, then `rerank_candidates`. On success, for each page row whose `best` is a non-primary chunk, set the preview `chunk` and `chunk_index` from that row, and `section = section_of(winner, stale=False)`. That is `None` for a winner without section metadata, and **never** the primary's section. Leave every other field alone. On fallback, render the unchanged baseline.
- [x] 3.2 `_hybrid_search_reranked`: the same over `result.rows` in fused order, with `rerank_scores` keyed by path from the per-note max. On **any** chunk winner, the primary included, the row's preview becomes that chunk's preview **even where the incumbent selection would show the keyword snippet**, and its locator is that chunk's section (absent if none). A title or excerpt winner, and every fallback row, keeps the incumbent preview selection (D4). Build the candidate documents by the explicit partition (a) known stamp → embedding input, (b) NULL or unknown stamp → title only, (c) stale or keyword-only → title + excerpt, with no catch-all.
- [x] 3.3 `_record_rerank`: add `rerank_documents`.
- [x] 3.4 Grep for production callers of `allocate_documents`, `embedding_document` and `rerank_chunks` in both tools and both harness arms (own the seams).

## 4. Tests

- [x] 4.1 **Synthetic winner fixture** (synthetic text only, never tenant text). The winning chunk is allocated **by construction**: note A ("Virtue Note") has 3 fetched chunks, all within the cap and the budget, with its cosine-best chunk under the heading "Status Olympics". Notes B and C have one prose chunk each. A fake reranker scores by a fixed table keyed on document text: A-best −8.87 without the prefix and −7.0 with it, A-second −3.98 / −1.79, A-third −0.62 / +0.33, B −2.0, C −3.0. Assert:
  - the incumbent builder ranks A last (a pinned regression of the old behaviour through the incumbent arm's builder);
  - the new path ranks A first with `rerank_score: 0.33`, and A's preview **and** `§` locator come from A-third.

  Run it for both tools. For hybrid, A is first in both arms and has a keyword snippet, and the reranked row shows A-third's chunk preview instead.
- [x] 4.2 **Locator parity**: for the fixture's A row, `read_note(section="#N")` with the rendered `#N` returns a body that contains A-third's `chunk_text` verbatim. The fallback page shows A-best's locator and the incumbent preview (the keyword snippet on hybrid). **Pre-034 winner**: a variant where A-third has NULL `heading_path`/`section_ordinal` renders A-third's preview and **no** `§` locator, and asserts that A-best's locator string is absent from the row.
- [x] 4.3 **`rerank=false` byte-identity**: both tools, against the render of c491326 (the existing golden harness in `tests/test_semantic_rerank.py` / `tests/test_hybrid_rerank.py`), including the `confidence` token page.
- [x] 4.4 **The `rerank=true` oracle (D5)**:
  - (2) every fallback code is byte-identical to c491326's fallback page;
  - (3) `P_new` vs `P_ref` built from the same final order and scores with the incumbent preview selection and locator: the diff is only in `preview_kind`, the preview literal and the locator of rows whose `best` is a non-primary chunk, and `candidates: N` is equal;
  - (4) the one-chunk-per-note fixture with a prefix-blind scorer is byte-identical to c491326's reranked page.
- [x] 4.5 Request-shape tests on the fake reranker:
  - the documents begin with the stored prefix (the "Stored prefix, not the current title" scenario). *The "vault directory unreadable" condition was dropped*: the rerank path has no vault read at all (only the session's rows), so no unreadable-vault fixture can exercise anything. The test serves the call from a session stub with no vault configured;
  - a stale note sends exactly its title;
  - an unknown-stamp note (the rollback scenario) sends exactly its title, with no stored text in the body;
  - a keyword-only hybrid row sends its excerpt;
  - `count_mismatch` when the fake answers one result per note;
  - the hybrid "Every fused candidate is scored" case sends `candidates: 50` with ≤ 80 documents;
  - **hybrid rollback**: a fresh both-arms candidate with stamp 3 and a non-NULL excerpt sends exactly its title (no excerpt, no stored chunk or prefix);
  - **one-chunk both-arms**: a both-arms row whose incumbent preview is the keyword snippet and whose single (primary) chunk wins shows that chunk as `preview_kind: chunk` with its locator, and the fallback page shows the snippet (D5 oracle 4);
  - the security scenarios (a forged title, tag or chunk) still pass with the swapped preview.
- [x] 4.6 Update the existing tests whose one-document-per-note assumption changes (`tests/test_hybrid_rerank.py`, `tests/test_semantic_rerank.py`, `tests/test_rerank_eval.py`, `tests/test_rerank_probe.py`, plus the statement classifier in `tests/test_hybrid_search_offline.py` and the env-key list in `tests/conftest.py`). Justify each change against a spec scenario and weaken no assertion. Settings tests: `RERANK_TIMEOUT_MS=20000` accepted and `30001` refused; `RERANK_MAX_DOCUMENTS` defaults to 100, and 49 and 201 are refused.
- [x] 4.7 `pytest tests` and `make test-integration` (real Postgres; the statement-capture and snapshot tests must actually run).

## 5. Settings

- [x] 5.1 `src/config.py`: `rerank_timeout_ms: int = Field(1500, ge=100, le=30000)`, and `rerank_max_documents: int = Field(100, ge=50, le=200)`. Both get comments pointing to DEPLOYMENT.md's latency table and the concurrency rule (D8).

## 6. Harness

- [x] 6.1 `scripts/search_eval_arms/rerank.py`: `make_semantic_arm` and `make_hybrid_arm` go through `allocate_documents` with the configured budget. Add `make_semantic_arm_incumbent` and `make_hybrid_arm_incumbent` (the baseline arms) with a frozen copy of the pre-change `build_document`/`bounded_text` (module-private, with a "frozen, never edited" comment) and one document per candidate note (D10). They follow the same membership, `requires_db`, `preflight` and `empty_candidates`/`UnrerankedAnswer` rules.
- [x] 6.2 Offline tests: the incumbent request holds one title-led document per candidate and no prefix; the candidate request holds every primary and ≤ `RERANK_MAX_DOCUMENTS`; at `--depth 10` both arms call with `limit=10` and recall@20/@50 are null; corpus loss for both hybrid arms; the probe failure exits 64.

## 7. Eval gate (search-path change) and the #402 measurement

**Deferred:** 7.1–7.4 run after both this change and `hybrid-any-fill` have merged (the `gate` step ships there). The 7.4 script is implemented (`scripts/rerank_allocation_report.py`, read-only, numbers only, unit-tested), but it has not been run: the coordinator runs it against the restored copy.

- [ ] 7.1 `make search-eval-snapshot`, then on a restored `BACKUP_FULL=1` copy, with `RERANK_URL` set to the P4000 reranker and `RERANK_TIMEOUT_MS=20000` for replay (#367 L11; say so in the report), run **six reports**: {semantic, hybrid} × {`rerank-l10` (`--depth 10`), `rerank-l20` (`--depth 20`), depth 50}. In each, `ARMS="…rerank:make_<tool>_arm_incumbent …rerank:make_<tool>_arm"` and `BASELINE=…:make_<tool>_arm_incumbent`. On a multi-user deployment, run `make search-eval-export USER_ID=…` first. **The depth-50 reports alone cannot see R1 or R2** (D10).
- [ ] 7.2 Release bar, per report: `python -m scripts.search_eval gate --report … --arm …:make_<tool>_arm --slices overall,class:identifier --min-ci-lower -0.01 --out .search-eval/rerank-documents-<tool>-<label>-gate.md` must exit 0. The `--metrics` are `recall@10,ndcg@10` at `rerank-l10`, `recall@10,recall@20,ndcg@10` at `rerank-l20`, and `recall@10,recall@20,recall@50,ndcg@10` at depth 50 (recall@k above the depth is null by `staged_depth` and is not a cell). The PR body carries only tables from successful gate runs on attachable single-user reports. A `CORPUS_LOSS` verdict invalidates.
- [ ] 7.3 The two named judgments go into `.search-eval/manual.jsonl` (tenant data, git-ignored; no path in the repo): victimhood → *Virtue by Circumstance* (top 3 expected with rerank), free will → the RRF #1 note (top 3 expected). Report their ranks under all four arms at the three depths **for information only** (no release bar; owner decision, as in #403). Report the per-call `rerank_documents` and `rerank_ms` distribution from the replay (the input to R1, R3 and the latency doc).
- [ ] 7.4 **The #402 measurement** (Codex round 1, BLOCKER 1). Write a read-only replay script (implementer task; read-only session; no tenant text in its output or in the repo). For the two named queries, at `limit` 10 and 20 and on both tools, it lists the affected note's fetched chunks as `(chunk_index, fetched distance rank, allocated: yes/no)`, and the note's per-document scores when a reranker is configured. Put **numbers only** (indices, ranks, scores, yes/no) into `design.md` under "The measurement". If the winning chunk is not allocated, say so plainly there and in the PR. It is a known limitation (L1), not a reason to widen the fetch silently.

## 8. Docs, verification, archive, live check

- [x] 8.1 Docs, in the feature branch:
  - `docs/architecture/search.md` "Reranking (#367)": the scored unit is the embedding input; every primary first, then secondaries in distance order; the bound `min(B, 4·m)` and its proof; the per-note max over allocated documents; preview and locator parity; `rerank_documents`; #367 L2 retired; this change's L1–L11. Plus a pointer from "Hybrid search (#354)".
  - `docs/architecture/indexing-and-embeddings.md`: `embed_prefix` and the stamped formatter now also feed the reranker, so a change to either is a rerank-input change.
  - `docs/architecture/rate-limits.md`: the slot hold up to `RERANK_TIMEOUT_MS` and the `queue`/`enforce` rule.
  - The CLAUDE.md reranker bullet (#367): one clause on documents, the budget and the max.
  - DEPLOYMENT.md "Reranker (optional)": the measured and scaled table; the sizing rule pairing `RERANK_TIMEOUT_MS` with `RERANK_MAX_DOCUMENTS`; that the reference deployment sets 20 s in infra while in `shadow`; why the default stays 1,500; the `queue`/`enforce` requirement (`RERANK_TIMEOUT_MS ≤ MCP_CONCURRENCY_WAIT_SECONDS × 1000` unless `make concurrency-report` with rerank traffic passes).
  - README: one sentence on what the reranker is sent; settings table rows for the range 100–30000 and for `RERANK_MAX_DOCUMENTS`.
  - `.env.example`: both settings.
  - `docs/architecture/usage-attribution.md`: `rerank_documents`.

  Grep that every changed setting is named in README and `.env.example`.
- [ ] 8.2 `openspec-verifier` subagent (not the implementer). Iterate to zero blocking gaps.
- [ ] 8.3 **Adversarial Codex** (mandatory: a search path an agent acts on). Give it the specs and changed files, and frame it as a defensive PASS/FAIL review. What counts as wrong here:
  - a silently wrong order;
  - a candidate the incumbent scored left unscored;
  - a preview or locator that disagrees with the score;
  - superseded or cross-tenant text leaving the server;
  - a page that mixes reranked and unreranked rows;
  - more than `RERANK_MAX_DOCUMENTS` documents;
  - an extra statement or retrieval;
  - a `rerank=false` byte change.

  Two rounds by default, triaged per the workflow, with declined findings recorded as limitations.
- [ ] 8.4 `/openspec-archive-change` (`openspec archive rerank-documents -y`) as the last commits, with `Closes #402` in the PR. After deploy, the infra PR sets `RERANK_TIMEOUT_MS=20000` (the reference deployment is in `shadow`; recheck D8 before any mode promotion). Then **live re-run** the two #402 queries with `hybrid_search(rerank=true)` and `semantic_search(rerank=true)` against the live server. Report the ranks, the `reranked:` token, `rerank_score`, preview and locator of the two notes, and which tools were called. File the D9 follow-up issue (weak-score skip, with its evidence bar).
