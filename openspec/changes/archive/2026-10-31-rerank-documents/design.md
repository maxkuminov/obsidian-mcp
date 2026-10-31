## Context

### What the code does today (verified on c491326)

- `rerank.build_document(title, text)` returns `title + "\n\n" + text` (the title alone when there is no text), cut to `RERANK_DOC_CHARS` = 2,048 code points. `bounded_text` pre-cuts the text so the title fits.
- **semantic_search(rerank=true).** `semantic_candidates` → `_semantic_overfetch` → `semantic_arm_rows(overfetch = max(5 × limit, 50))`. So the overfetch is exactly 50 chunk rows only when `limit ≤ 10`, and up to 250 at `limit = 50`. The rows are re-sorted by `(distance, file_path, chunk_index)`. **Dedup happens in `semantic_arm_rows`**, in the `seen`/`deduped` loop right after that re-sort (`src/services/embeddings.py`, after `rows = sorted(rows, key=lambda r: (r.distance, r.file_path, r.chunk_index))`). It keeps the **first** (cosine-best) row per `note_id` and discards every other fetched chunk of that note. Only the kept row gets `rerank_text = bounded_text(title, chunk_text)` (`None` when stale). `semantic_candidates` returns the first `RERANK_MAX_CANDIDATES` (50) deduped notes. The projection names `NoteEmbedding.heading_path` and `section_ordinal`, never `embed_prefix`.
- **hybrid_search(rerank=true).** The semantic arm is `semantic_arm_rows(overfetch = HYBRID_ARM_DEPTH = 50)`, with the same dedup. The page is `fused[:max(limit, 50)]`. A fresh semantic-arm row carries its chunk's `rerank_text`. Every other row gets `left(body_excerpt, 2048)` through `_fill_excerpts`, one statement in the hybrid snapshot.
- **The tools** (`_semantic_search_reranked`, `_hybrid_search_reranked`) send one document per candidate through `rerank_candidates`, order by `(-score, unreranked position)`, and take the first `limit` rows. Every other field (preview, `section`, `similarity`) keeps its unreranked value.
- **The embedding input** (#355, #390) is `embedding_input(prefix, chunk) = prefix + "\n\n" + EMBEDDED_TEXT_FORMATTERS[v](chunk)`. The prefix, `build_embed_prefix(title, heading_path, tags)`, is at most `EMBED_PREFIX_MAX_CHARS` = 300 characters, with each component cut at 80. It is stored per row as `note_embeddings.embed_prefix`, NULL on a pre-034 row. The note's stamp is `notes_metadata.embedded_chunking_version`, and all of a note's rows are written under one version.
- **Concurrency.** `semantic_search` and `hybrid_search` are class `embedding`, and `MCP_CONCURRENCY_EMBEDDING` defaults to 1. The tool-stage wait `MCP_CONCURRENCY_WAIT_SECONDS` defaults to 5. A `rerank=true` call holds its slot through the exchange (#367 D11). The mode defaults to `shadow`.
- `RERANK_TIMEOUT_MS` is `Field(1500, ge=100, le=10000)`.

### The measurement (#402, last comment)

These are the 12 stored chunks of *Virtue by Circumstance*, replayed against the live bge-reranker-v2-m3:

| document | `title + chunk` (today) | `embed_prefix + chunk` |
| --- | --- | --- |
| chunk 10 (conclusion) | −0.62 | **+0.33** |
| chunk 7 (False Status & Division) | −3.98 | −1.79 |
| chunk 6, the cosine-best, §11 "Victimhood Olympics" | **−8.87** | 1–2 points higher (not separately reported) |

Today the note is scored −8.87, below real prose from weaker notes, so it falls out of the top 6. Under this change it is scored on the maximum over its **allocated** documents. That reaches +0.33 **only if chunk 10 is allocated**: it must be among the call's fetched rows, and among the note's first 4 rows by distance or within the secondary budget. **Whether that holds for the live queries is not known.** The issue replayed all 12 chunks, not the call's fetched and allocated set, so the #402 case is a measurement task (task 7.4), not a claim of this design. Timing on the P4000, uncontended, 50 documents: about 410 characters each takes 1.6 s, and about 2,000 characters each takes 7.3 s, roughly 75 µs per character. The two live `timeout` pages were 50 long chunks at the 10 s deadline, while the GPU was shared with the indexer's embedding batch.

#### Real-data replay and eval gate (tasks 7.1–7.4, 2026-10-31)

Numbers only. These come from a read-only copy of the production database, snapshot 2026-10-31. The scope is one user (`USER_ID=1`, `attachable: true`, `provenance.users: 1`): 3,835 notes, all at chunking version 2, and 32,395 chunks with `chunk_lengths.under_min` 0. There are 199 scored queries (66 identifier, 133 prose: 197 mined plus the 2 named judgments of task 7.3) and 198 judged paths. Every report has verdict `OK` and `corpus_loss` 0. The replay used bge-reranker-v2-m3 with `RERANK_TIMEOUT_MS=20000` (#367 L11) and `RERANK_MAX_DOCUMENTS=100`. Each report runs the incumbent arm (`BASELINE`) against the candidate arm, gated with `--slices overall,class:identifier --min-ci-lower -0.01` on the D10 metrics.

| tool | depth | verdict | worst cell (mean Δ, CI95 lower bound) |
| --- | --- | --- | --- |
| semantic | 10 | PASS | recall@10 overall (+0.033, +0.007) |
| semantic | 20 | PASS | recall@20 overall (+0.018, −0.004) |
| semantic | 50 | PASS | recall@50 (+0.000, +0.000); lowest non-zero cell: recall@20 overall (+0.047, +0.018) |
| hybrid | 10 | PASS | ndcg@10 class:identifier (+0.070, +0.019) |
| hybrid | 20 | **FAIL** (1 of 6) | recall@20 class:identifier (+0.051, **−0.0114**) |
| hybrid | 50 | **FAIL** (1 of 8) | recall@20 class:identifier (+0.051, **−0.0114**) |

Both FAILs are the same cell (n = 66). Every mean Δ is ≥ 0. The release decision belongs to the owner.

**Allocation replay** (`scripts/rerank_allocation_report.py --score`, limits 10 and 20, both tools):

- **Documents per call:** semantic 41 at limit 10 (24 candidates), 77–83 at limit 20 (42–46 candidates); hybrid 67–68 at both limits (50 candidates). No call exceeds the budget of 100.
- **Victimhood note.** 12 chunks are fetched. The per-note cap of 4 is filled by the chunks at fetched ranks 1, 2, 3 and 6. The best-scoring chunk (+0.12, fetched rank 7) is **not allocated** at either limit on either tool. This is the L1 case. The note is still ranked #1, on the chunk at fetched rank 6, with a max of −1.49 (−1.46 at limit 20) against −4.47 for the cosine-best chunk.
- **Free will, RRF #1 note.** Its winning chunk is allocated, with a max of +2.94.
- **Free will, the card #402 describes.** It has a single chunk, allocated, which scores +3.85.

**Named-judgment ranks** (for information only). "—" means the note is not in the top `depth`.

| "Victimhood as a status game" | depth 10 | depth 20 | depth 50 |
| --- | --- | --- | --- |
| semantic incumbent → candidate | — → 1 | — → 1 | 33 → 1 |
| hybrid incumbent → candidate | — → 1 | — → 1 | 36 → 1 |

| "Free will and determinism" (judged: the unreranked RRF #1) | depth 10 | depth 20 | depth 50 |
| --- | --- | --- | --- |
| semantic incumbent → candidate | 6 → 3 | 8 → 3 | 8 → 3 |
| hybrid incumbent → candidate | 6 → 3 | 6 → 3 | 6 → 3 |

The harness records no per-call `rerank_ms`, so task 7.3's latency distribution was **not measured**. The only per-call measure is the document counts above.

Context (**different query sets, not a gate cell**). An earlier real-data run at 16:43Z the same day measured unreranked `hybrid_search` overall recall@10 at 0.662 on 197 queries. The reranked hybrid candidate here scores 0.637 on 199 queries.

### Constraints

- No second retrieval, no extra statement, no extra round trip (#367 D4, owner decision 2).
- All or nothing (#367 D5/D7). The reason codes, probe, re-probe and `/health` are unchanged (owner decision 6).
- `rerank=false` pages and statements are byte-identical (owner decision 5).
- Superseded text never leaves the server (#200): a stale note sends no stored chunk.
- **No candidate the incumbent scores is left unscored** (Codex round 1).

## Goals / Non-Goals

**Goals:** the cross-encoder scores the same text the embedder scored; a note is judged on its best **allocated** chunk; the incumbent candidate set is preserved exactly; the page's preview and locator agree with the score; the per-call document count has a proven bound; latency and the slot hold are documented from measurement.

**Non-Goals:** changing fusion, the fill offset or snippet order (`hybrid-any-fill`); fusing the rerank score with RRF (issue follow-up 2); a weak-score skip (D9); fetching more chunks than the search already does; changing the probe, the wire format or the client; re-deriving prefixes from the vault.

## Decisions

### D1. The document is the embedding input, rebuilt from stored columns

For a chunk row of a note that is **not stale**, whose `embed_prefix` is not NULL, and whose note's `embedded_chunking_version` **is a key of `EMBEDDED_TEXT_FORMATTERS`**:

```
doc = (embed_prefix + "\n\n" + EMBEDDED_TEXT_FORMATTERS[stamp](chunk_text))[:RERANK_DOC_CHARS]
```

This is `embedding_input` with the **stamped** formatter, which is exactly the input that row's vector was built from: the same reconstruction `_lookup_reusable_vectors` relies on, and the same rule that only a known stamp reconstructs. The prefix is at most 300 characters, so the whole prefix and the separator always survive the cut, and at least 1,746 characters of chunk follow. The title is no longer prepended separately: it is the prefix's first component (whitespace-collapsed and cut at 80, L4).

Every other case. For `hybrid_search` the cases are an explicit partition, with no catch-all: (a) a fresh semantic candidate with a known stamp gets the embedding-input documents above; (b) a fresh semantic candidate with a NULL or unknown stamp gets the title only; (c) a stale semantic candidate or a keyword-only candidate gets the title plus the excerpt (Codex round 2).

| case | the note's documents |
| --- | --- |
| semantic, stale note | one document: `title` |
| hybrid (c): stale semantic candidate, or keyword-only candidate | one document: `title + "\n\n" + left(body_excerpt, …)` (NULL excerpt: `title`) |
| semantic **and** hybrid (b): fresh semantic candidate, non-NULL prefix, stamp NULL or **not a key** (a later version's rows read by this build, e.g. after a rollback) | one document: `title`, **never the excerpt** on either tool. The input that built the vector cannot be reconstructed, so no stored text is sent. |
| a fresh chunk row with a NULL `embed_prefix` (pre-034) | `title + "\n\n" + chunk_text` (today's document) for each such row |

*Alternatives.* Rebuilding the prefix from the row's current `title`/`tags` would need no new projection, but it is not the text the embedder scored, and owner decision 7 rules it out. Treating an unknown stamp as version 1 (the first draft) silently sends text that differs from the vector's input (Codex round 1, MAJOR). Re-reading the vault at query time would put a filesystem read on the hot path (#200 refused that).

### D2. Every candidate's primary first, then secondaries in distance order: the allocation and its proof

`semantic_arm_rows(with_rerank_text=True)` no longer discards a deduped note's other rows. Each deduped row carries `rerank_chunks`: the note's fetched rows in the re-sort order `(distance, file_path, chunk_index)`, cosine-best first. A note whose documents are a single title or excerpt (D1 table) carries none. The dedup itself, the row order, the `rows` list, `chunk_rows` and every legacy field are unchanged. The **candidate notes** `c_1 … c_m` (`m ≤ 50`) are exactly today's: for semantic, the first ≤ 50 deduped notes in vector order; for hybrid, `fused[:50]` in fused order. Each `c_i` has:

- a **primary** document `p_i`: the cosine-best chunk under D1 for a fresh semantic row, otherwise the D1 table's document;
- **secondary** documents: the D1 documents of its other fetched rows (none in the single-document cases).

`rerank.allocate_documents(candidates)` is pure and deterministic. It uses `B = settings.rerank_max_documents` (`RERANK_MAX_DOCUMENTS`, default 100, range 50–200) and `K = RERANK_MAX_DOCUMENTS_PER_NOTE = 4` (a constant: the primary plus at most 3):

```
pass 1 (every candidate):  for i in 1..m:  give c_i its primary          # n = m
pass 2 (depth):            S = every secondary of every candidate, sorted by
                               (distance, file_path, chunk_index)
                           for r in S:
                               if n == B: stop
                               if count[r's note] == K: skip
                               give r to its note;  n += 1
```

**Determinism.** `S` is totally ordered by `(distance, file_path, chunk_index)`, the arm's own re-sort key (#280 D6). Pass 1 walks a fixed order. The allocation is a function of the fetched rows and `B`, and it is independent of `limit`. Equidistant rows break ties by path, then by chunk index.

**Bounds (proof).** `m ≤ 50 ≤ B`, so pass 1 always completes, with `n = m` and one document per note. Every addition in pass 2 is preceded by the checks `n < B` and `count < K`. So `n ≤ B`, `count_i ≤ 4`, and therefore **`documents ≤ min(B, 4·m)`**. The per-note maximum is taken over at most 4 documents. With the default, hybrid's 50 candidates get 50 primaries and at most 50 secondaries. Those secondaries are a subset of the arm's at most 50 fetched rows, minus the primaries, so a hybrid call sends at most 100 documents. ∎

**No extra retrieval (proof).** Every chunk document is a distinct row already returned by the call's one overfetch statement: the primary is the dedup's representative, and the secondaries are the same note's other rows in that result. Every excerpt document is read by the existing `_fill_excerpts` statement for a row it already serves. So for `semantic_search`, documents ≤ min(B, 4·m, `chunk_rows`); for `hybrid_search`, documents ≤ min(B, 4·m, m + semantic `chunk_rows`).

**Corollaries.** (a) **The candidate set and the number of candidates reranked are exactly the incumbent's.** Every candidate holds ≥ 1 document, and no note is unscored. `candidates: N` is unchanged. (b) At `B = 50` with `m = 50` there is no secondary budget, and the call degenerates to the incumbent's documents in the new form. Depth needs `B > m`. (c) For `semantic_search` with `limit ≤ 10` (50 fetched rows), every fetched fresh row is a document except a note's rows past its fourth.

*Alternatives.* (i) Primaries for the page only, then secondaries, then the rest (the previous draft). This can leave existing candidates unscored, a silent recall loss (Codex round 1, BLOCKER), so it is reversed. (ii) A per-note cap of 3 within a fixed 50 (the previous draft). With every primary first, a 50-candidate call would have no room for depth (Codex round 1), hence the 100 default and the cap of 4. (iii) Send every fetched chunk: up to 250 documents at `limit = 50`, an unbounded latency. (iv) Secondaries in note order: they would be spent on the top notes rather than on the chunks nearest the query.

### D3. The per-note maximum and the order

`rerank_candidates(query, groups)` takes one non-empty document list (at most 4) per candidate and sends the flattened list (note order, then document order) in **one** exchange. It validates over the flattened count with the D7 order unchanged, so `count_mismatch` and `bad_index` are judged against documents. It maps scores back by index. `score_i = max(group_i)` over ≤ 4 scores. `best_i` is the **first** document in allocation order that reaches the max, so the primary wins a tie and a tie never moves the preview. The order is `(−score_i, i)`, and the page is its first `limit` rows. `RerankOutcome` gains `best: list[int]` and `documents: int`.

### D4. Preview and locator parity

On a **successfully reranked** row whose winning document is a **chunk**, the row's preview and locator come from that chunk. On `semantic_search` that applies when the winner is a chunk other than the primary, because a primary winner is already the incumbent's preview and locator. On `hybrid_search` it applies to **every** chunk winner, **the primary included**, so a row whose incumbent selection showed the keyword snippet shows the winning chunk instead. The `rerank_score`, the preview and the locator then all describe the same prose (Codex round 2):

- the preview is that chunk, uncut, cut by the renderer to the one-line preview;
- the locator is `section_of(winner_row, stale=False)`. When the winning row has **no recorded section metadata** (pre-034: NULL `heading_path`/`section_ordinal`), the row carries **no `§` locator**. The cosine-best chunk's locator is **never** reused for it.

`similarity`, `stale`, `embedding_truncated`, path, title and tags keep their values, and so do the hybrid keyword fields other than the preview selection (`match`, `matched_in`). When the winner is a title or excerpt document (or, on `semantic_search`, the primary chunk, which is the incumbent's preview anyway), the row keeps the incumbent's preview selection and locator. A **fallback page** keeps the incumbent's preview selection everywhere: the identity order, the cosine-best chunk or keyword snippet as today, no `rerank_score`. That is the #367 baseline.

**What `read_note(section="#N")` parity means.** It holds when the winning chunk has a recorded section. Under #390 a chunk's locator is the deepest `#N` whose `read_note` body contains the chunk, so the shown `§ … (#N)` opens a section that contains, verbatim, the `chunk_text` behind `rerank_score`. The reranker saw that text plus the prefix, which is the title, the heading path the locator names, and the tags. An agent that follows the locator reads the passage that earned the score.

### D5. The oracle

1. `rerank=false`, both tools: output byte-identical, and the issued statements identical (captured).
2. Fallback pages, every code: byte-identical to the pre-change fallback page for the same inputs.
3. Reranked pages: let `P_ref` be the pre-change renderer applied to **the same final note order and the same per-note scores**, with the incumbent preview selection and locator. `P_new` SHALL differ from `P_ref` only on rows whose winner is a chunk that is not the incumbent's preview: a non-primary chunk on either tool, or, on `hybrid_search`, any chunk winner of a row whose incumbent preview was the keyword snippet. There it differs only in `preview_kind`, the `preview` literal and the `§` locator field (present, changed or omitted). Those rows' rendered length and abbreviation change with them, under the unchanged cap rules. `candidates: N` is identical.
4. One fetched chunk per note and a fake scorer that ignores the prefix. `semantic_search`: `P_new` is byte-identical to the pre-change reranked page. `hybrid_search`: byte-identical except on both-arms rows whose incumbent preview was the keyword snippet, where `preview_kind` and `preview` become the (primary) chunk's. This is the one-chunk both-arms test.

### D6. The statement under rerank=true

Under `with_rerank_text` only, `semantic_arm_rows` projects two more columns, `NoteEmbedding.embed_prefix` and `NoteMetadata.embedded_chunking_version`, through its existing join, and nothing else (never the vector). This needs a carve-out in `search-quality`'s section-locator requirement, which says the vector statements SHALL NOT project `embed_prefix`. The carve-out applies to this flagged statement only, and `rerank=false` and `find_related` keep the prohibition (Codex round 2). Predicates, `ORDER BY`, `LIMIT`, the `SET LOCAL`s, the exact re-run and the statement count are unchanged. The `rerank=false` statement is unchanged. The prefix stays internal: it is never rendered, logged or put on a legacy row.

### D7. hybrid_search

`HybridRow` gains an internal `rerank_chunks` (the semantic-arm row's list, set only when `rerank_candidates`). `_fill_excerpts` is unchanged. The tool builds the candidates from `result.rows` (fused order, ≤ 50), allocates them, reranks, and applies D3 and D4. The fused order is whatever the hybrid service returns, so `hybrid-any-fill`'s offset composes without a seam. A keyword-only call (semantic unavailable) has no chunks: one excerpt document per note, which is today's behaviour.

### D8. Latency, the budget, and the timeout

**The budget and the timeout must be read together.** At the measured ~75 µs per character (P4000, uncontended):

| documents × size | time |
| --- | --- |
| 50 × ~410 | 1.6 s (measured) |
| 50 × ~2,000 | 7.3 s (measured) |
| 100 × ~410 | ≈ 3.2 s (scaled) |
| 100 × ~2,000 | ≈ 15 s (scaled) |

On a GPU shared with the embedder, the time roughly doubles (the live timeouts). **The code default `RERANK_TIMEOUT_MS=1500` stays** (owner decision 3). It is below even the 50-document case, so it already falls back on most calls on this hardware (L8). The range becomes **100–30000** (owner decision), because `le=10000` would refuse the reference deployment's 20 s. `RERANK_MAX_DOCUMENTS` (default 100, range 50–200) is the latency lever: 50 restores the incumbent's document count. DEPLOYMENT.md gains the table, a sizing rule (`timeout ≥ B × average characters × per-character cost`, ×2 on a shared GPU), and this pairing.

**The embedding slot.** A `rerank=true` call holds its `embedding`-class slot through the exchange, which is up to `RERANK_TIMEOUT_MS`. With `MCP_CONCURRENCY_EMBEDDING=1`:

- **`off` / `shadow`** (the default): nothing waits or is refused. A 20 s timeout is allowed. The overruns show up in the shadow evidence (#367 L7).
- **`queue`**: a concurrent `semantic_search`/`hybrid_search` waits up to `MCP_CONCURRENCY_WAIT_SECONDS` (5) and is then admitted with an `overrun` mark. It is delayed, not refused.
- **`enforce`**: the same caller waits up to 5 s and is then **refused with `slot_timeout`**, although its embedding provider is healthy.

So DEPLOYMENT.md requires, under `queue` or `enforce`, `RERANK_TIMEOUT_MS ≤ MCP_CONCURRENCY_WAIT_SECONDS × 1000`, unless `make concurrency-report TARGET=<mode>` run over evidence that includes `rerank=true` traffic PASSes. The reference deployment's 20 s is contingent on its mode being `shadow` (it is today). Promoting the mode re-opens this check. The consequence is accepted limitation L10. The probe's fixed 10 s deadline is unchanged. The re-probe uses `RERANK_TIMEOUT_MS` and is unaffected (two short documents, no slot).

### D9. Declined: the weak-score skip (follow-up)

Issue follow-up 3 (`rerank: skipped (weak scores)` when every score is below a floor) is declined. A floor is a threshold on an uncalibrated cross-encoder logit, with no judged evidence. It is filed as a follow-up issue naming the evidence it would need: a harness report on the gate's judged set in which the queries whose best rerank score falls below a candidate floor show a reranked-minus-unreranked delta with a CI95 entirely below 0, while the skip arm passes the gate against both the reranked and the unreranked arm. Issue follow-up 2 (blending the rerank score with the RRF rank) is likewise out of scope: it changes the order contract and needs its own gate.

### D10. Harness: the incumbent arms and the gate depths

`--arm-opt` is one run-global dict, so a builder option would switch every arm. `hybrid.make_arm_alt` is the unreranked hybrid arm keyed by `hybrid_alt_k` and cannot hold a builder. The incumbent therefore gets two factories in `scripts/search_eval_arms/rerank.py`: `make_semantic_arm_incumbent` (membership `semantic`) and `make_hybrid_arm_incumbent` (`keyword_or_semantic`). Both call the same candidate services, then send **one document per candidate note** (all of today's ≤ 50), built by a frozen copy of the pre-change `build_document`/`bounded_text` in the arm module: the cosine-best `chunk`, uncut, for a fresh semantic row, otherwise the excerpt or nothing, exactly as today. They go through the same `rerank_candidates` with one document per group, and follow the same probe, `empty_candidates` and `UnrerankedAnswer` rules. These two frozen factories are **the baseline arms**.

**Gate depths (Codex round 1, BLOCKER).** The arms call with `limit = min(depth, 50)`, and the allocation is independent of `limit`. Even so, the overfetch is `max(5 × limit, 50)` for `semantic_search`, and what an agent sees is the top `limit` rows. **A depth-50 report alone cannot see R1 or R2 at the limits agents use.** So the gate runs each pair (semantic, hybrid) at three call limits, each a separate report with the incumbent as `BASELINE`:

| run label | `--depth` (= call limit) | gate metrics (non-null under `staged_depth`) |
| --- | --- | --- |
| `rerank-l10` | 10 | `recall@10`, `ndcg@10` |
| `rerank-l20` | 20 | `recall@10`, `recall@20`, `ndcg@10` |
| depth 50 | 50 | `recall@10`, `recall@20`, `recall@50`, `ndcg@10` |

Each runs over the slices `overall` and `class:identifier`, with CI95 lower bound ≥ −0.01. Recall@k above the run's depth is null by `staged_depth`, so it is not requested as a cell (the gate fails a null cell). The production allocation is therefore what is measured at the limits 10 and 20. No new arm option is needed, because the call limit is the depth.

The issue's two queries go into `.search-eval/manual.jsonl` (tenant data, git-ignored; paths never in the repo) as named judgments. For victimhood, relevant = *Virtue by Circumstance*, expected in the top 3 with rerank. For free will, relevant = the RRF #1 note, expected in the top 3. Their ranks under all four arms at all three depths are reported **for information only, not as a release bar** (owner decision, as in #403).

**Sequencing.** This change merges **after `hybrid-any-fill`** (#403). It uses that change's `gate` step and follows its incumbent-arm pattern.

## Risks / Trade-offs

- **R1 Depth crowding.** A note with many fetched rows near the query could take most of the secondary budget. → The cap of 4 per note, and secondaries in global distance order. The candidate set is never reduced (D2a). `rerank_documents` is on every usage row. The gate at limits 10, 20 and 50 measures it.
- **R2 Max bias.** The maximum over k noisy scores rises with k. → k ≤ 4 by construction. The gate at limits 10 and 20 measures what bias remains (L2).
- **R3 Twice the documents, more latency.** → D8: the measured table, the budget as the lever, and fallback on timeout as today.
- **R4 The slot hold under `enforce`.** → D8, L10. The deployment rule is checked against the concurrency report.
- **R5 Stored-prefix staleness.** → L3. The window is one pass, and only for rows the index still calls fresh.
- **R6 Similarity and preview from different chunks.** → L5. `similarity` is a note-level retrieval score.

## Migration Plan

No migration and no reindex: every column already exists. One new setting, `RERANK_MAX_DOCUMENTS` (default 100). Deploy is merge → image → Flux. After this image is live, the infra PR sets `RERANK_TIMEOUT_MS=20000` on the reference deployment, valid while its concurrency mode is `shadow` (D8). Rollback is the previous image. A leftover `RERANK_MAX_DOCUMENTS` is harmless on rollback: pydantic-settings never applies `extra` checks to process environment variables, and the dotenv source is filtered to known fields (`src/config.py`). `rerank=false` is untouched.

## Owner decisions

From the brief (2026-10-31):

1. The document is `embed_prefix + "\n\n" + chunk_text` (stamped formatter), cut to `RERANK_DOC_CHARS`. Stale rows and keyword-only candidates keep today's document.
2. Per-chunk documents from the already-fetched rows, per-note max. The preview and locator follow the max: **yes**.
3. Latency is documented from the P4000 measurement. Infra raises `RERANK_TIMEOUT_MS` to 20 s, and the code default stays.
4. The weak-score skip is declined, as a follow-up with its evidence bar (D9).
5. `rerank=false` is byte-identical, and `rerank=true` changes only as the D5 oracle allows.
6. All-or-nothing, the codes, the probe and `/health rerank` are unchanged.
7. The prefix is reconstructed from stored columns, never from the vault (L3).

On the open questions (2026-10-31), as revised after Codex round 1:

8. `RERANK_TIMEOUT_MS` range 100–30000; default unchanged at 1,500.
9. Every candidate gets its primary before any secondary (the incumbent candidate set is preserved exactly). The document budget is a setting, `RERANK_MAX_DOCUMENTS` (default 100, range 50–200). The per-note cap is 4 (the primary plus 3). Secondaries go in distance order, ties broken by `(file_path, chunk_index)`. *Superseded:* the cap of 3 within a fixed 50, and pass 2 before the remaining primaries.
10. The two named judgments are information only, as in #403.
11. This change merges after `hybrid-any-fill`, and the two frozen incumbent factories are the baseline arms.
12. On a reranked row with a chunk winner, both the preview and the `§` locator follow that chunk (D4). An unknown stamp sends the title-only document (D1). The 20 s reference setting is contingent on the concurrency mode (D8).

## Accepted limitations

- **L1** Depth comes only from rows the overfetch returned, and only from rows allocated under the cap and the budget. A note's best-for-reranker chunk outside them is never scored. There is no extra retrieval, by decision.
- **L2** The max rule favours a note with 4 documents over a note with 1 (R2). This is bounded by the cap and not normalised.
- **L3** The stored prefix is what the vector was built from. A title or tag edit makes the row stale at the next pass, which then sends no stored text. Until that pass, the stale prefix is sent. This is the same "what the index knows" bound as #200's chunk text.
- **L4** The 2,048 cut can drop up to 302 characters of a full-window chunk (`W = CHUNK_SIZE × 4` = 2,048 by default). A title longer than 80 characters reaches the reranker cut, as a prefix component.
- **L5** `similarity` keeps the cosine-best chunk's value while the preview and locator may show the winning chunk.
- **L6** On a reranked hybrid row with any chunk winner (the primary included), the keyword snippet the incumbent would have shown is replaced by the winning chunk's preview. `match` and `matched_in` still describe the keyword arm.
- **L7** With `B = 50`, or with `m` close to `B`, there is no secondary budget, and the call behaves as the incumbent in the new document form.
- **L8** The 1,500 ms default times out most rerank calls on P4000-class hardware, and more so at the 100-document default. Operators size `RERANK_TIMEOUT_MS` and `RERANK_MAX_DOCUMENTS` together from DEPLOYMENT.md.
- **L9** This retires #367 L2 ("a note is judged on its best-distance chunk only"). #367 L4 (a stale candidate is scored on its title or excerpt) stands.
- **L10** Under `MCP_CONCURRENCY_MODE=enforce` with `MCP_CONCURRENCY_EMBEDDING=1`, one `rerank=true` call holds the sole embedding slot for up to `RERANK_TIMEOUT_MS`. A concurrent `semantic_search` or `hybrid_search` waits `MCP_CONCURRENCY_WAIT_SECONDS` and is refused with `slot_timeout`. Under `queue` it is delayed and admitted with `overrun`. The DEPLOYMENT rule in D8 bounds the timeout to the wait budget in those modes unless the concurrency report PASSes.
- **L11** An unknown or NULL stamp on a fresh, prefixed row is scored on its title alone (D1) until this build knows that version.

## Open Questions

None.

## Codex spec review — round 1 (2026-10-31)

Verdict, verbatim (`codex exec … -m gpt-daybreak-blue-latest`, read-only, on the commit before this fold):

````
The proposal is structurally valid but control-invalid. Its core allocation can omit candidates the incumbent scored, its claimed #402 repair is not guaranteed under the three-document cap, and the release gate runs at a depth that disables the new multi-chunk behavior. With no supporting evidence, pass 3 should precede secondary allocation: that is the only ordering that preserves incumbent candidate recall. Hybrid may then have no room for extras, but that is safer than silently removing a candidate an agent needed.

The timeout change also turns one slow opt-in call into up to 20–30 seconds of occupancy in the sole embedding slot; under `enforce`, ordinary semantic/hybrid callers exhaust the five-second wait and are refused. Documentation alone is not an availability control.

```json
{"verdict":"FAIL","findings":[{"severity":"BLOCKER","file":"openspec/changes/rerank-documents/design.md","line":22,"claim":"The proposal says the affected note will receive the +0.33 score provided chunks 7 and 10 were fetched, and repeatedly describes the result as the best fetched chunk. The allocator actually scores only the three cosine-nearest allocated chunks, so fetched is insufficient and the central #402 repair is not guaranteed.","failing_input":"A note has fetched chunks ordered by distance [6, 2, 3, 10, ...]; chunks 6, 2 and 3 score below -2 while fetched chunk 10 scores +0.33. The three-document cap excludes chunk 10, contradicting the scenario in specs/reranker/spec.md:54-56 and the expected fixture in tasks.md:30.","fix":"Replace every best-fetched claim with best-allocated and make the #402 acceptance case prove that chunk 10 is among the three allocated rows using its real fetched distance rank, or revise the allocation/cap so the claimed winning chunk is guaranteed to be scored before implementation."},{"severity":"BLOCKER","file":"openspec/changes/rerank-documents/specs/reranker/spec.md","line":28,"claim":"Pass 2 precedes pass 3, so secondary semantic documents may exhaust the budget before some existing candidate notes receive any document. Those notes are removed from the reranked candidate set even though today's rule scores every candidate, creating an admitted silent recall loss.","failing_input":"There are 50 fused candidates, limit=10, fused rank 11 is keyword-only, and the semantic candidates supply at least 40 pass-2 rows. Pass 1 uses 10 documents and pass 2 uses the remaining 40; rank 11 receives zero documents and cannot be promoted even if its excerpt would have the highest rerank score.","fix":"Put the pass that gives every candidate its primary before allocation of any secondary document. This preserves the incumbent candidate set; absent valid evidence, it is the defensible ordering even though a 50-candidate hybrid call then has no secondary budget."},{"severity":"BLOCKER","file":"openspec/changes/rerank-documents/tasks.md","line":49,"claim":"The mandatory quality gate cannot evaluate secondary-chunk maxima, max-of-three sampling bias, or pass-2 candidate starvation. The harness receives the run depth as limit, and the prescribed/default depth is 50; pass 1 therefore consumes all 50 documents on primaries before pass 2 runs.","failing_input":"Run task 7.1 with the harness default depth 50. Both candidate arms call allocate_documents(..., limit=50); with 50 candidates every request contains exactly one primary per note. The gate can measure the prefix change but none of R1, R1b, R2, locator swapping, or the behavior agents see at typical limits 10/15/20.","fix":"Make the release gate replay and compare the actual production allocation at representative call limits, including 10, 15 or 20, and gate those results against the incumbent. Do not claim the eight-cell depth-50 report measures max bias or candidate loss."},{"severity":"MAJOR","file":"openspec/changes/rerank-documents/design.md","line":110,"claim":"The proposal promises that the locator and preview follow the document producing rerank_score, but then explicitly keeps a hybrid keyword preview while moving only the locator. tasks.md:24 also mandates unchanged preview selection, contradicting proposal.md:16 and the reranker requirement's parity rule.","failing_input":"A both-arms row has a keyword snippet selected by hybrid preview rules, while a secondary semantic chunk wins the per-note maximum. The output displays the keyword snippet, a rerank_score earned by different prose, and a locator into that different prose.","fix":"On a successfully reranked row whose winning document is a chunk, make both preview and locator use that chunk. Keep incumbent preview selection only on fallback pages or title/excerpt winners, and update D4, L6, tasks and oracle tests consistently."},{"severity":"MAJOR","file":"openspec/changes/rerank-documents/design.md","line":48,"claim":"An unknown embedded_chunking_version is treated as version-1 identity while the design claims this exactly reconstructs the certified provider input. Current reuse code deliberately accepts only stamps present in EMBEDDED_TEXT_FORMATTERS; an unknown version's formatter is unknowable.","failing_input":"A database contains a fresh version-3 row with non-NULL embed_prefix after a later-version deployment or rollback. Version 3 collapsed or otherwise formatted text differently. This build applies identity, sends text different from the input that produced the vector, and silently ranks it as if reconstruction were exact.","fix":"Use the stored formatter only when the stamp is a known key. Specify a conservative legacy/title-only disposition for unknown stamps and add a transition/rollback scenario; do not equate unknown with version 1."},{"severity":"MAJOR","file":"openspec/changes/rerank-documents/design.md","line":131,"claim":"The timeout ceiling is raised to 30 seconds and the reference deployment will use 20 seconds while rerank=true continues to hold the sole embedding-class slot. No acceptance gate evaluates the resulting enforced read-path refusals.","failing_input":"With MCP_CONCURRENCY_MODE=enforce, MCP_CONCURRENCY_EMBEDDING=1 and the five-second tool wait, one rerank=true call spends 20 seconds in the reranker. Concurrent semantic_search and hybrid_search calls wait five seconds and return slot_timeout even though their embedding provider is healthy.","fix":"Make the planned 20-second deployment contingent on an explicit concurrency availability gate under the deployed mode and record the refusal consequence as an accepted limitation. The spec must not treat a documentation note as sufficient evidence that the live read path remains usable."},{"severity":"MINOR","file":"openspec/changes/rerank-documents/specs/reranker/spec.md","line":54,"claim":"The locator scenario unconditionally requires the winning secondary's § locator, but a fresh pre-034 chunk may have NULL section_ordinal and heading_path. section_of explicitly returns no locator for such a row.","failing_input":"A content-current pre-034 row with NULL embed_prefix and NULL section fields wins as a secondary chunk. Its preview can move to the winner, but no valid § locator exists to render.","fix":"Condition locator parity on the winning chunk having recorded section metadata, explicitly require omission for pre-034/NULL locators, and add a test so an implementer cannot reuse the cosine-best locator."}]}
```
````

What changed, per finding (decisions by the owner, 2026-10-31):

1. **BLOCKER, the #402 claim is not guaranteed under the cap.** Accepted. Every "best fetched" claim is now "best allocated". The Context measurement paragraph states that the live case is unknown. The #402 acceptance case is now a **measurement** (task 7.4): a read-only replay script lists the note's fetched distance ranks and which chunks are allocated, with numbers only. The spec scenarios ("A note is scored on its best allocated chunk", hybrid "keeps its place on its best allocated chunk") and fixture 4.1 now use synthetic fixtures where the winner is allocated by construction.
2. **BLOCKER, pass 2 before pass 3 drops candidates.** Accepted, and the earlier owner decision is reversed. Every candidate gets its primary before any secondary (D2), so the candidate set and `candidates: N` are exactly the incumbent's. R1b is dropped. To keep depth possible under that ordering, the budget became the setting `RERANK_MAX_DOCUMENTS` (default 100, range 50–200; ADDED requirement) and the per-note cap became 4. The proof is `documents ≤ min(B, 4·m)`, which for hybrid is 50 primaries plus ≤ 50 secondaries. The latency consequence is in D8 (100 × ~410 ≈ 3.2 s, 100 × 2 KB ≈ 15 s uncontended), and the timeout is to be read together with the budget.
3. **BLOCKER, the depth-50 gate cannot see the new behaviour.** Accepted. The gate also runs both pairs at call limits 10 and 20 (`rerank-l10`, `rerank-l20`, as `--depth 10`/`20`). Because the arms call with `limit = min(depth, 50)`, the call limit is the depth, and no new arm option is needed. The cells there are recall@10, @20 and ndcg@10; recall@50 is null by `staged_depth`. D10 and task 7.1 say plainly that the depth-50 report alone cannot see R1 or R2.
4. **MAJOR, the hybrid preview contradicts parity.** Accepted. On a successfully reranked row with a non-primary chunk winner, both the preview (even over the keyword snippet) and the locator follow that chunk. Title or excerpt winners and fallback pages keep the incumbent selection. D4, D5 (oracle 3 now lists `preview_kind`), L6, the hybrid requirement, and tasks 3.2, 4.1 and 4.2 are updated.
5. **MAJOR, an unknown stamp is treated as version 1.** Accepted. The stored formatter applies only to a known key. A NULL or unknown stamp on a fresh, prefixed note sends the single title document (D1, L11). The rollback scenario is added.
6. **MAJOR, the 20 s timeout against the sole embedding slot.** Accepted. The 20 s reference setting is contingent on the mode (D8). It is allowed in `off`/`shadow`. Under `queue`/`enforce`, DEPLOYMENT requires `RERANK_TIMEOUT_MS ≤ MCP_CONCURRENCY_WAIT_SECONDS` unless `make concurrency-report` with rerank traffic passes. That requirement is in the settings requirement. The mechanism is recorded as L10: under `enforce`, `slot_timeout` refusals; under `queue`, delayed `overrun` admissions.
7. **MINOR, a locator for a pre-034 winner.** Accepted. Locator parity holds only when the winner has recorded section metadata. A NULL winner renders no `§` locator, and the primary's locator is never reused (spec scenario and task 4.2).

Declined: none.

## Codex spec review — round 2 (2026-10-31)

Verdict, verbatim:

````
1. #402 best-fetched claim — VERIFIED. The proposal now says best allocated, makes the live result explicitly unknown, and tests only synthetic allocated winners.
2. Primary-before-secondary allocation — VERIFIED. Every incumbent candidate receives a document before secondary allocation, with a testable bound.
3. Representative-depth quality gate — VERIFIED. Six reports exercise production allocation at limits 10, 20, and 50 against frozen incumbent arms.
4. Preview/locator parity — NOT_FIXED. Hybrid still keeps the incumbent keyword preview when the winning chunk is the primary chunk.
5. Unknown chunking version — REGRESSED. The general rule requires title-only, but the hybrid requirement’s “any other candidate” clause requires an excerpt.
6. Concurrency availability control — VERIFIED. The 20-second deployment is mode-contingent, with the refusal consequence recorded as L10 and an explicit readiness exception.
7. Missing pre-034 locator — VERIFIED. The revised scenario requires no locator and tests that the primary locator is not reused.

The fresh pass found one additional MAJOR contradiction: the new reranker requirement mandates projecting `embed_prefix`, while the existing search-quality specification still prohibits that projection. Overall verdict: FAIL.

```json
{"verdict":"FAIL","round1":[{"claim":"The #402 repair was claimed for the best fetched chunk even when the cap might not allocate it.","status":"VERIFIED","note":"The live outcome is now explicitly unknown, best-fetched claims became best-allocated, and task 7.4 measures allocation without making it a release claim."},{"claim":"Secondary allocation could exhaust the budget before every incumbent candidate received a primary document.","status":"VERIFIED","note":"D2 allocates every primary first; the minimum budget covers all 50 candidates, and the property test pins candidate preservation and the document bounds."},{"claim":"A depth-50-only quality gate could not exercise the new multi-chunk behavior at production limits.","status":"VERIFIED","note":"Tasks 7.1–7.2 require semantic and hybrid reports at call limits 10, 20, and 50 against the frozen incumbent arms."},{"claim":"A hybrid row could show a keyword preview while its rerank score and locator came from a semantic chunk.","status":"NOT_FIXED","note":"Secondary winners are repaired, but specs/reranker/spec.md:95 explicitly keeps incumbent preview selection when the winning semantic chunk is the primary; D5's one-chunk byte-identity oracle preserves the same mismatch."},{"claim":"An unknown embedded_chunking_version was treated as version-1 identity although its formatter is unknowable.","status":"REGRESSED","note":"The general rule now requires title-only, but the hybrid requirement says every candidate other than a fresh known-stamp semantic candidate receives title plus body_excerpt, creating two incompatible dispositions for a fresh unknown-stamp semantic row."},{"claim":"A 20-second rerank hold could cause ordinary embedding-class calls to be refused under enforce without an availability gate.","status":"VERIFIED","note":"D8 makes the reference setting contingent on shadow mode and requires the timeout to fit the wait duration under queue/enforce unless readiness evidence including rerank traffic passes; L10 records the slot_timeout consequence."},{"claim":"The locator scenario required a locator even when a winning pre-034 chunk had no section metadata.","status":"VERIFIED","note":"The revised requirement and task 4.2 require the winner preview with no locator and explicitly prohibit reuse of the primary locator."}],"findings":[{"severity":"MAJOR","file":"openspec/changes/rerank-documents/specs/reranker/spec.md","line":95,"claim":"Hybrid preview parity remains false when the winning document is the primary semantic chunk: the requirement keeps the incumbent preview, which may be a keyword snippet describing different prose.","failing_input":"A both-arms hybrid candidate has a keyword snippet unrelated to its cosine-best semantic chunk. That primary chunk receives the note's maximum rerank score. The result keeps the keyword preview while the score and section locator describe the semantic chunk.","fix":"For every successfully reranked hybrid row whose winning document is a chunk, including the primary, use that chunk for both preview and locator. Keep incumbent selection only for title/excerpt winners and fallback pages; update D4, D5 oracle 4, and add a one-chunk both-arms test."},{"severity":"MAJOR","file":"openspec/changes/rerank-documents/specs/reranker/spec.md","line":95,"claim":"The hybrid rule contradicts the title-only rollback disposition: its catch-all 'Any other candidate' clause requires an excerpt for a fresh semantic candidate with an unknown stamp.","failing_input":"After rollback, a fresh hybrid semantic row has non-NULL embed_prefix, embedded_chunking_version=3, known formatter keys {1,2}, and a non-NULL body_excerpt. Line 28 requires exactly its title, while line 95 requires title plus excerpt, permitting a confidently different rerank order.","fix":"Partition the hybrid cases explicitly: fresh unknown/NULL-stamp prefixed semantic candidates receive one title-only document; stale semantic and keyword-only candidates receive the excerpt form. Add the rollback request-shape test for hybrid specifically."},{"severity":"MAJOR","file":"openspec/specs/search-quality/spec.md","line":535,"claim":"The existing section-locator contract says the semantic-search statement SHALL NOT project embed_prefix, while the new reranker contract requires the rerank=true overfetch statement to project it; both specifications cannot be satisfied.","failing_input":"semantic_search(rerank=true) returns a fresh known-version prefixed chunk. Obeying the existing prohibition leaves no stored prefix with which to construct the required embedding-input document, so the implementation must violate one contract or silently score different text.","fix":"Add a search-quality delta carving out rerank=true: allow only embed_prefix and embedded_chunking_version in that flagged projection, while retaining the prohibition for rerank=false and find_related. Keep the statement-capture tests that pin predicates, ordering, limits, settings, and statement count."}]}
```
````

What changed, per finding (owner decisions, 2026-10-31):

1. **MAJOR, preview parity not fixed for a primary winner (hybrid).** Accepted. On every successfully reranked hybrid row whose winning document is a chunk, the primary included, both the preview and the `§` locator come from that chunk. The incumbent selection is kept only for title or excerpt winners and on fallback pages. D4, D5 oracles 3 and 4, L6, the hybrid requirement (new scenario "A primary chunk winner replaces the keyword snippet") and tasks 3.2 and 4.5 (the one-chunk both-arms test) are updated.
2. **MAJOR, the unknown-stamp rule regressed in hybrid.** Accepted. The hybrid requirement now partitions the cases explicitly: (a) fresh with a known stamp → embedding input; (b) fresh with a NULL or unknown stamp → title only, never the excerpt; (c) stale or keyword-only → title plus excerpt. The catch-all is removed. The D1 table, the "Hybrid rollback stamp" scenario and the hybrid rollback request-shape test (task 4.5) are added.
3. **MAJOR, the `embed_prefix` projection clashes with search-quality.** Accepted. The new `search-quality` delta MODIFIES the section-locator requirement: the `rerank=true` overfetch statement may project exactly `embed_prefix` and `embedded_chunking_version` and nothing else, while `rerank=false` and `find_related` keep the prohibition. A scenario covers it, and task 2.2 adds the projection assertion beside the existing statement-capture checks (predicates, ordering, limits, settings, statement count). D6 cross-references it.

Declined: none. Round 3 is verification-only of these three.

## Codex spec review — round 3 (2026-10-31, verification only)

Verdict, verbatim:

````
Two contradictions remain, so the verdict is FAIL. The hybrid document partition is fixed, and previously verified controls have not regressed. However, the proposal’s top-level oracle still excludes hybrid primary-chunk preview replacement, and the projection scenario ambiguously mandates the stored vector instead of the chunking-version column. Structural validation passes.

```json
{"verdict":"FAIL","round1":[{"claim":"The #402 repair was claimed for the best fetched chunk even when the cap might not allocate it.","status":"VERIFIED"},{"claim":"Secondary allocation could exhaust the budget before every incumbent candidate received a primary document.","status":"VERIFIED"},{"claim":"A depth-50-only quality gate could not exercise the new multi-chunk behavior at production limits.","status":"VERIFIED"},{"claim":"A hybrid row could show a keyword preview while its rerank score and locator came from a semantic chunk.","status":"NOT_FIXED"},{"claim":"An unknown embedded_chunking_version was treated as version-1 identity although its formatter is unknowable.","status":"VERIFIED"},{"claim":"A 20-second rerank hold could cause ordinary embedding-class calls to be refused under enforce without an availability gate.","status":"VERIFIED"},{"claim":"The locator scenario required a locator even when a winning pre-034 chunk had no section metadata.","status":"VERIFIED"}],"round2":[{"claim":"Any hybrid chunk winner, including the primary, replaces the keyword preview and supplies the locator; title/excerpt winners and fallback pages retain the incumbent preview.","status":"NOT_FIXED"},{"claim":"Hybrid documents are partitioned into fresh known-stamp embedding input, fresh unknown/NULL-stamp title-only, and stale or keyword-only title-plus-excerpt cases.","status":"VERIFIED"},{"claim":"Only the rerank=true overfetch may add exactly embed_prefix and embedded_chunking_version; rerank=false and find_related retain the projection prohibition.","status":"NOT_FIXED"}],"findings":[{"severity":"MAJOR","file":"openspec/changes/rerank-documents/proposal.md","line":17,"claim":"The top-level oracle still says preview and locator may change only when the winning chunk is non-primary, contradicting the requirement, D4/D5 and tests that replace a hybrid keyword snippet when the primary chunk wins.","failing_input":"A both-arms hybrid row displays a keyword snippet, has one semantic chunk, and that primary chunk wins. Line 16 and the reranker requirement require the chunk preview and locator, while line 17 prohibits that preview/locator change because the winner is primary.","fix":"Revise the oracle to permit preview and locator changes whenever the winning chunk differs from the incumbent preview, explicitly including a hybrid primary chunk replacing a keyword snippet."},{"severity":"MAJOR","file":"openspec/changes/rerank-documents/specs/search-quality/spec.md","line":54,"claim":"The projection scenario says the rerank=true statement projects exactly 'those two columns' after naming embed_prefix and the vector, contradicting the requirement and task 2.2, which require embed_prefix and embedded_chunking_version and prohibit the vector.","failing_input":"In the statement-capture test, adding embed_prefix and embedded_chunking_version satisfies the requirement and task 2.2 but violates the scenario's literal antecedent; adding embed_prefix and the stored vector satisfies that scenario wording but violates the requirement and leaves no chunking-version stamp for document reconstruction.","fix":"Name the columns explicitly: the rerank=true overfetch projects exactly note_embeddings.embed_prefix and notes_metadata.embedded_chunking_version in addition to the rerank=false projection, and never the stored vector."}]}
```
````

What changed: (1) the proposal's oracle now allows the preview and locator to change whenever the winning chunk differs from the incumbent preview, explicitly including a hybrid primary chunk that replaces a keyword snippet; (2) the search-quality projection scenario names the two added columns, `note_embeddings.embed_prefix` and `notes_metadata.embedded_chunking_version`, and excludes the stored vector. Both were prose residuals, and the requirements already said this. By the review budget there is no round 4.

## Implementation notes (10fa0b6 and after)

These are shifts from the design's wording. None of them changes the contract.

- **`rerank.embedding_document(prefix, chunk_text, stamp)`** builds the D1 document. It returns `None` for a NULL or unknown stamp, and `candidate_documents` then sends the title alone. `candidate_documents(title, chunks=…, stamp=…, fallback_text=…)` implements the D1 table and the hybrid partition (a)/(b)/(c). `allocate_documents(candidates, budget)` implements D2.
- **`rerank_groups(query, groups)` is the core** (D3: state precedence, one exchange over the flattened documents, the per-note max, `best`, `documents`). **`rerank_candidates(query, [(title, text)])` remains** as the one-document-per-candidate wrapper over it, which keeps its #367 callers and tests valid.
- **`semantic_document_groups(rows)` / `hybrid_document_groups(rows)`** are the allocation entry points. The tools and the two production harness arms both call them, so an arm's request is the tool's request.
- **The frozen incumbent builder lives in the arm module** (`scripts/search_eval_arms/rerank.py`: `_incumbent_document`, `_incumbent_bounded_text`, marked never-edit). `src/` keeps no legacy builder beyond `build_document`, which the D1 table still uses.
- **`--depth` is the call limit.** The arms call with `limit = min(depth, 50)`, so the `rerank-l10` / `rerank-l20` runs are `--depth 10` / `--depth 20`, with no arm option. A test pins `limit=10` and the null recall@20/@50 at depth 10.
- `rerank_text` stays on candidate rows. The hybrid incumbent arm reads the excerpt from it, and nothing renders it.
- `scripts/rerank_allocation_report.py` (task 7.4) buffers every line until the whole run succeeds. On any failure it prints one fixed line with the exception class only (Codex impl round 1).
- **The free-will judgment is ambiguous (task 7.3).** The spec judges the free-will query against "the RRF #1 note". On the 2026-10-31 snapshot, the unreranked RRF #1 is a different note from the card #402 describes (the one whose version-1 matched chunk was a two-wikilink section). The judgment follows the spec and uses the current RRF #1. The card is reported alongside: rank 1 under both candidate arms, a single allocated chunk at +3.85.

## Verifier — implementation (10fa0b6)

`openspec-verifier`: 0 blocking, 0 major, 8 minor. All are folded:

1. Task 8.1: a pointer from search.md "Hybrid search (#354)" to the rerank documents. **Done.**
2. Task 4.2: a test calls the section extraction behind `read_note(section="#N")` with the rendered ordinal and asserts the winning chunk's `chunk_text` is inside that section (`test_the_rendered_ordinal_opens_the_section_holding_the_winning_chunk`). **Done.**
3. Tasks 4.1/4.4(3): a hybrid oracle with a non-empty keyword arm, where a secondary chunk winner replaces the keyword snippet (`test_hybrid_secondary_winner_replaces_the_snippet`). **Done.**
4. Task 4.4(2)/(4): every fallback code, `not_configured` and `timeout` included, plus the prefix-blind one-chunk reranked page, compared byte for byte against frozen renders of c491326 in `tests/fixtures/rerank_documents_golden.json`. These were written by `tests/test_rerank_documents_golden.py` run on an exported c491326 tree, 18 pages. **Done.**
5. Task 4.5: the unreadable-vault condition is dropped with a note, because no vault read exists on the path. **Done.**
6. Task 6.2: corpus loss for `make_hybrid_arm_incumbent`, and recall@20/@50 null at `--depth 10`. **Done.**
7. Task 4.6: the two unchanged files are removed from the list. **Done.**
8. These implementation notes. **Done.**

## Adversarial Codex — implementation round 1 (10fa0b6)

Verdict, verbatim:

````
The ranking/allocation implementation itself appears consistent: candidate ownership, document bounds, tie handling, score mapping, and winner metadata propagation hold. The focused rerank suite passed 270/270 tests. However, the operator report has a plausible confidentiality failure, so the control review fails.

```json
{"verdict":"FAIL","findings":[{"severity":"BLOCKER","file":"scripts/rerank_allocation_report.py","line":158,"claim":"The numbers-only report has no sanitized top-level failure path and prints results incrementally. A provider or SQL exception can therefore leave a plausible but incomplete report and emit tenant query text or SQL parameters through the uncaught traceback.","failing_input":"Run the report with two private queries and capture combined stdout/stderr for an issue attachment. Let the first query succeed, then have an OpenAI-compatible embedding provider reject the second with ProviderInputTooLarge whose message echoes that query, or let the hybrid SQL statement fail while displaying bound parameters. main() propagates the exception, so the captured file contains partial measurements plus the private query in the traceback.","fix":"Buffer all report lines until every operation succeeds; wrap parsing and main_async in a top-level exception handler that prints only a fixed code and exception class, never str(exc), and exits nonzero. Add tests with secret-bearing provider and SQL exceptions asserting neither partial output nor the secret appears on either stream."},{"severity":"MINOR","file":"docs/architecture/search.md","line":462,"claim":"The architecture note still describes the pre-#402 implementation in present tense immediately before the new rules: it says rerank=true uses the same projection as rerank=false and sends one best-distance title-plus-chunk document per semantic note.","failing_input":"An operator or maintainer reads the 'Candidates and the scored unit' section to determine the current SQL projection and scored unit; lines 466–478 contradict lines 488–505 and the implementation.","fix":"Remove the obsolete paragraph or explicitly label it as pre-#402 historical behavior, leaving the stored embedding-input and multi-document allocation rules as the sole current contract."}]}
```
````

What changed:

- **BLOCKER (the report script):** `scripts/rerank_allocation_report.py` now buffers every line in memory and writes them at once only after every query on both tools has succeeded. A single top-level handler covers argument parsing (a parser subclass refuses with the fixed code `bad_arguments` instead of echoing the argument) and `main_async`. Any failure prints one line with the exception **class** only, never `str(exc)`, writes no partial report, and exits 1 (64 for a refusal). New tests: a `ProviderInputTooLarge` whose message quotes a secret query, raised on the second query; a `ProgrammingError` carrying the secret in its statement and parameters; a secret-bearing bad argument; a successful run that emits every line at once. Each asserts empty stdout on failure and no secret on stdout or stderr.
- **MINOR (search.md):** the "Candidates and the scored unit" paragraph no longer describes the pre-#402 scored unit in the present tense. It is now "Candidates (D4, D6)", it states the rerank=true projection, and it labels the old one-document unit "*Before #402*", leading into "The documents since #402".

## Adversarial Codex — implementation round 2 (c370ac2)

Verdict, verbatim:

````
The round-one fixes are verified with no regression. The focused rerank suite passed 296 tests. Independently regenerating all 18 golden pages from an exported c491326 tree produced a byte-identical fixture. Failure, refusal, parser, `BaseException`/interrupt, logging, and stderr paths do not expose exception messages or partial query results.

```json
{"verdict":"PASS","round1":[{"claim":"The allocation report could emit partial results and leak tenant query or SQL data through uncaught exceptions.","status":"VERIFIED"},{"claim":"The architecture note described pre-#402 projection and document behavior as current.","status":"VERIFIED"}],"findings":[]}
```
````

PASS. Both round-1 items are VERIFIED, and there are no new findings.

