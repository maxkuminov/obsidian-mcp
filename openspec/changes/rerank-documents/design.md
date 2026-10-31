## Context

### What the code does today (verified on c491326)

- `rerank.build_document(title, text)` returns `title + "\n\n" + text` (the title alone when there is no text), cut to `RERANK_DOC_CHARS` = 2,048 code points. `bounded_text` pre-cuts the text so the title fits.
- **semantic_search(rerank=true).** `semantic_candidates` → `_semantic_overfetch` → `semantic_arm_rows(overfetch = max(5 × limit, 50))`. So the overfetch is exactly 50 chunk rows only when `limit ≤ 10`, and up to 250 rows at `limit = 50`. The rows are re-sorted by `(distance, file_path, chunk_index)`. **Dedup happens in `semantic_arm_rows`**, in the `seen`/`deduped` loop right after that re-sort (`src/services/embeddings.py`, after the line `rows = sorted(rows, key=lambda r: (r.distance, r.file_path, r.chunk_index))`). It keeps the **first** (cosine-best) row per `note_id` and discards every other fetched chunk of that note. Only the kept row gets `rerank_text = bounded_text(title, chunk_text)`, or `None` when it is stale. `semantic_candidates` returns the first `RERANK_MAX_CANDIDATES` (50) deduped notes. The projection names `NoteEmbedding.heading_path` and `section_ordinal`, and never `embed_prefix`.
- **hybrid_search(rerank=true).** The semantic arm is `semantic_arm_rows(overfetch = HYBRID_ARM_DEPTH = 50)` with the same dedup. The page is `fused[:max(limit, 50)]`. A semantic-arm row that is not stale carries the chunk's `rerank_text`. Every other row gets `left(body_excerpt, 2048)` through `_fill_excerpts`, one statement in the hybrid snapshot.
- **The tools** (`_semantic_search_reranked`, `_hybrid_search_reranked`) send one document per candidate through `rerank_candidates`, order by `(-score, unreranked position)` and take the first `limit` rows. Every other field (chunk preview, `section`, `similarity`) keeps its unreranked value.
- **The embedding input** (#355, #390) is `embedding_input(prefix, chunk) = prefix + "\n\n" + EMBEDDED_TEXT_FORMATTERS[v](chunk)`. `prefix = build_embed_prefix(title, heading_path, tags)` is at most `EMBED_PREFIX_MAX_CHARS` = 300 characters, each component cut at 80, and is stored per row as `note_embeddings.embed_prefix` (NULL on a pre-034 row). The note's stamp is `notes_metadata.embedded_chunking_version`. All of a note's rows are written under one version.
- `RERANK_TIMEOUT_MS` is `Field(1500, ge=100, le=10000)`.

### The measurement (#402, last comment)

These are the 12 stored chunks of *Virtue by Circumstance*, replayed against the live bge-reranker-v2-m3:

| document | `title + chunk` (today) | `embed_prefix + chunk` |
| --- | --- | --- |
| chunk 10 (conclusion) | −0.62 | **+0.33** |
| chunk 7 (False Status & Division) | −3.98 | −1.79 |
| chunk 6, the cosine-best, §11 "Victimhood Olympics" | **−8.87** | 1–2 points higher (not separately reported) |

Today the note is scored −8.87, which is below real prose from weaker notes, so it falls out of the top 6. Under this change it is scored max(+0.33, −1.79, …) = +0.33, which keeps it on top, **provided chunks 10 and 7 are among the call's fetched rows**. The measurement replayed all 12 chunks; the live re-run (task 8.4) checks the fetched case. Timing on the P4000, uncontended, 50 documents: about 410 characters each takes 1.6 s, and about 2,000 characters each takes 7.3 s, roughly 75 µs per character. The two live `timeout` pages were 50 long chunks at the 10 s deadline, while the GPU was shared with the indexer's embedding batch.

### Constraints

- No second retrieval, no extra statement, no extra round trip (#367 D4, owner decision 2).
- All or nothing (#367 D5/D7). The reason codes, probe, re-probe and `/health` are unchanged (owner decision 6).
- `rerank=false` pages and statements are byte-identical (owner decision 5).
- Superseded text never leaves the server (#200): a stale note sends no stored chunk.

## Goals / Non-Goals

**Goals:** the cross-encoder scores the same text the embedder scored; a note is judged on its best fetched chunk; the page's locator agrees with the score; the per-call document count is provably ≤ 50; the latency budget is documented from measurement.

**Non-Goals:** changing fusion, the fill offset or snippet order (`hybrid-any-fill`); fusing the rerank score with RRF (issue follow-up 2); a weak-score skip (D9); fetching more chunks than the search already does; changing the probe, the wire format or the client; re-deriving prefixes from the vault.

## Decisions

### D1. The document is the embedding input, rebuilt from stored columns

For a chunk row of a note that is **not stale** and whose `embed_prefix` is not NULL:

```
doc = (embed_prefix + "\n\n" + fmt(chunk_text))[:RERANK_DOC_CHARS]
fmt = EMBEDDED_TEXT_FORMATTERS.get(embedded_chunking_version, identity)
```

This is `embedding_input` with the **stamped** formatter, which is exactly the input that row's vector was built from (the same reconstruction `_lookup_reusable_vectors` relies on). It is not the current version's formatter. An unknown or NULL stamp on a fresh row uses the chunk as stored (the version-1 identity). The prefix is at most 300 characters, so the whole prefix and the separator always survive the cut, and at least 1,746 characters of chunk follow. The title is no longer prepended separately: it is the prefix's first component (whitespace-collapsed and cut at 80, L4).

Every other case keeps today's `build_document(title, text)`:

| case | document |
| --- | --- |
| semantic, stale note | `title` |
| hybrid, stale semantic row or keyword-only row | `title + "\n\n" + left(body_excerpt, …)` (NULL excerpt: `title`) |
| a fresh chunk row with a NULL `embed_prefix` (pre-034) | `title + "\n\n" + chunk_text` |

*Alternatives.* Rebuilding the prefix from the row's current `title`/`tags` with `build_embed_prefix` would need no new projection, but it is not the text the embedder scored, and owner decision 7 rules it out. Re-reading the vault at query time would put a filesystem read on the hot path (#200 refused that).

### D2. Per-chunk documents under a 50-document budget: the allocation and its proof

`semantic_arm_rows(with_rerank_text=True)` no longer discards a deduped note's other rows. Each deduped row carries `rerank_chunks`: the note's fetched rows in the re-sort order `(distance, file_path, chunk_index)`, the cosine-best first. A stale note carries none. The dedup itself, the row order, the `rows` list, `chunk_rows` and every legacy field are unchanged. The **candidate notes** `c_1 … c_m` stay what they are today: semantic, the first ≤ 50 deduped notes in vector order; hybrid, `fused[:50]` in fused order. Each `c_i` has:

- a **primary** document `p_i`: the cosine-best chunk under D1 for a fresh semantic row, otherwise today's document (the D1 table);
- **secondary** documents `s_i,1 … s_i,k`: the D1 documents of its other fetched rows, in re-sort order (none for a stale or keyword-only note).

`rerank.allocate_documents(candidates, limit)` is pure. With `B = RERANK_MAX_DOCUMENTS = 50` and `L = min(limit, m)`:

```
n = 0
pass 1: for i in 1..L:  give c_i its primary;  n += 1
pass 2: for i in 1..m:
            if n == B: stop
            if i > L:  give c_i its primary;  n += 1
            for each secondary of c_i, in order:
                if n == B: stop
                give it to c_i;  n += 1
admitted = the notes that hold at least one document
```

**Bound (proof).** Both tools clamp `limit` to `[1, 50]`, so pass 1 adds `L ≤ limit ≤ 50 = B` documents. Every addition in pass 2 is preceded by the check `n < B`. So after the call `n ≤ B = 50`. ∎

**No extra retrieval (proof).** Every chunk document is a distinct row already returned by the call's one overfetch statement: the primary is the dedup's representative, and the secondaries are the same note's other rows in that result. Every excerpt document is read by the existing `_fill_excerpts` statement for a row it already serves. So, for `semantic_search`, documents ≤ min(50, `chunk_rows`): a stale note contributes 1 document against ≥ 1 rows, and a fresh note contributes at most its rows. For `hybrid_search`, documents ≤ min(50, semantic `chunk_rows` (≤ 50) + the keyword-only or stale candidates). The document count therefore cannot exceed the overfetch's row count plus the excerpt rows the call already reads, and it never exceeds 50.

**Corollaries.** (a) Every note of the unreranked page (`c_1 … c_L`) is admitted, which keeps #367's "the unreranked page is contained in the candidates". (b) The admitted notes are a **prefix** `c_1 … c_j` of the unreranked order, with `j ≥ L`: a note is dropped only if every note above it was admitted. (c) For `semantic_search` with `limit ≤ 10` the overfetch is exactly 50 rows. So documents ≤ rows ≤ 50, the budget never binds, and **every fetched non-stale row is a document**, which is the owner's model exactly. For `10 < limit ≤ 50` (overfetch 55–250 rows), and for `hybrid_search`, the budget binds and breadth gives way to depth (R1).

*Alternatives.* (i) Keep 50 notes with one document each: that is today's behaviour, so no depth. (ii) Send every fetched chunk of all 50 candidates: up to 250 documents, which breaks the latency bound. (iii) Fill pass 2 in global distance order instead of note order: this is equivalent when `limit ≤ 10`, but it has no meaning for keyword-only hybrid rows. (iv) A per-note cap of k = 3 (issue follow-up 1): left as a question, because owner decision 2 says "every chunk".

### D3. The per-note maximum and the order

`rerank_candidates(query, groups)` takes one non-empty document list per admitted note and sends the flattened list (note order, then document order) in **one** exchange. It validates over the flattened count with the D7 order unchanged, so `count_mismatch` and `bad_index` are judged against documents. It maps scores back by index. `score_i = max(group_i)`, and `best_i` is the **first** document in allocation order that reaches the max (the primary wins a tie, so a tie never moves the preview). The order is `(−score_i, i)`, and the page is its first `limit` rows. `RerankOutcome` gains `best: list[int]` and `documents: int`. `candidates` stays the admitted note count, which is the `N` of `reranked: true (candidates: N)`.

On the issue's numbers, the note is scored +0.33 instead of −8.87. The free-will card, whose version-1 matched chunk was a two-wikilink "Related to:" section, is scored on its best fetched prose chunk instead.

### D4. Locator and preview parity

On a reranked row whose `best_i` is a secondary chunk, the row's `chunk` (uncut; the renderer cuts it to the one-line preview as today), `chunk_index` and `section` are replaced by **that chunk row's** values (`section_of(row, stale=False)`). `similarity`, `stale`, `embedding_truncated`, path, title, tags and every hybrid keyword field keep their values. When `best_i` is the primary, or an excerpt or title document, the row is unchanged.

**What `read_note(section="#N")` parity means.** Under #390 a chunk's locator is the deepest `#N` whose `read_note` body contains the chunk. So the shown `§ … (#N)` opens a section that contains, verbatim, the `chunk_text` behind `rerank_score`. The reranker saw that text plus the prefix, and the prefix is the heading path the locator itself names, the title and the tags. An agent that follows the locator therefore reads the passage that earned the score, not the cosine-best passage the score says nothing about. On `hybrid_search` a row whose preview is a keyword snippet keeps the snippet (preview selection is unchanged), while its locator follows the scored chunk (L6).

A **fallback page** is unchanged: the identity order, the cosine-best preview and locator, no `rerank_score`. It is the #367 baseline.

### D5. The oracle

1. `rerank=false`, both tools: output byte-identical, and the issued statements identical (captured).
2. Fallback pages, every code: byte-identical to the pre-change fallback page for the same inputs.
3. Reranked pages: let `P_ref` be the pre-change renderer applied to **the same final note order and the same per-note scores**, with each row's cosine-best chunk and section. `P_new` SHALL differ from `P_ref` only (a) in the `candidates: N` value, and (b) on the rows where `best_i` is a secondary, in the `preview` literal and the `§` locator field (and so in those rows' rendered length and abbreviation, under the unchanged cap rules).
4. One fetched chunk per note, `m ≤ 50`, and a fake scorer that ignores the prefix: `P_new` is byte-identical to the pre-change reranked page.

### D6. The statement under rerank=true

Under `with_rerank_text` only, `semantic_arm_rows` projects two more columns, `NoteEmbedding.embed_prefix` and `NoteMetadata.embedded_chunking_version`, through the join it already has. Predicates, `ORDER BY`, `LIMIT`, the `SET LOCAL`s, the exact re-run and the statement count are unchanged. The `rerank=false` statement is unchanged. The #367 requirement "the statements … SHALL be the same as for `rerank=false`" is amended to "the same except these two projected columns". The prefix stays internal: it is never rendered, logged or put on a legacy row.

### D7. hybrid_search

`HybridRow` gains internal `rerank_chunks` (the semantic arm row's list, set only when `rerank_candidates`). `_fill_excerpts` is unchanged and still covers the keyword-only and stale rows. The tool builds the candidates from `result.rows` (fused order, ≤ 50), allocates with the call's `limit`, reranks and applies D3 and D4. The fused order is whatever the hybrid service returns, so `hybrid-any-fill`'s offset composes without a seam. A keyword-only call (semantic unavailable) has no chunks: one excerpt document per note, which is today's behaviour.

### D8. Latency and the timeout ceiling

The worst-case volume is unchanged: today's code could already send 50 × 2,048 characters. The typical volume rises: the prefix adds up to 302 characters, and secondaries are full-length prose where today's single chunk was often short. Measured at about 75 µs per character on the P4000, 50 full-length documents take about 7.3 s uncontended, and more than 10 s when the GPU is shared. **The code default stays at 1,500 ms** (owner decision 3). The `le` bound rises to **30,000**, because the current bound of 10,000 would refuse the reference deployment's 20 s, which infra sets. DEPLOYMENT.md gains the measured table, a sizing rule (budget ≈ documents × average characters × per-character cost, ×2 for a GPU shared with the embedder), and a note that the `embedding`-class slot is held through the exchange, up to the timeout (rate-limits.md, #367 L7). The probe's fixed 10 s deadline is unchanged; the re-probe uses `RERANK_TIMEOUT_MS`, which is still fine for two short documents.

### D9. Declined: the weak-score skip (follow-up)

Issue follow-up 3 (`rerank: skipped (weak scores)` when every score is below a floor) is declined. A floor is a threshold on an uncalibrated cross-encoder logit, with no judged evidence behind it. It will be filed as a follow-up issue that names the evidence it would need: a harness report on the gate's judged set in which the queries whose best rerank score falls below a candidate floor show a reranked-minus-unreranked delta with a CI95 entirely below 0, while the skip arm passes the eight-cell gate against both the reranked arm and the unreranked arm. Issue follow-up 2 (blending the rerank score with the RRF rank) is likewise out of scope. It changes the order contract and needs its own gate.

### D10. Harness: the incumbent arms

`--arm-opt` is one run-global dict, so a builder option would switch every arm. `hybrid.make_arm_alt` is the unreranked hybrid arm keyed by `hybrid_alt_k` and cannot hold a builder. The incumbent therefore gets two factories in `scripts/search_eval_arms/rerank.py`: `make_semantic_arm_incumbent` (membership `semantic`) and `make_hybrid_arm_incumbent` (`keyword_or_semantic`). Both call the same candidate services, then send **one document per candidate note** (all of today's ≤ 50), built by a frozen copy of the pre-change `build_document`/`bounded_text` that lives in the arm module. The text is the cosine-best `chunk`, uncut, for a fresh semantic row, otherwise the excerpt or nothing, exactly as today. They go through the same `rerank_candidates`, with one document per group, and follow the same probe, `empty_candidates` and `UnrerankedAnswer` rules. The gate then runs twice, each in its own report because a report has one `BASELINE`: `ARMS="…:make_semantic_arm_incumbent …:make_semantic_arm" BASELINE=…:make_semantic_arm_incumbent`, and likewise for hybrid. Each run applies the eight-cell gate (`recall@10/@20/@50`, `ndcg@10` × `overall`, `class:identifier`, CI95 lower bound ≥ −0.01). A `runfile:` baseline staged from the pre-change tree is the fallback if a frozen copy proves impractical.

The issue's two queries go into `.search-eval/manual.jsonl` (tenant data, git-ignored; paths never in the repo) as named judgments. For victimhood, relevant = *Virtue by Circumstance*, top 3 with rerank. For free will, relevant = the RRF #1 note, top 3. Their ranks under all four arms are reported for information in the PR.

## Risks / Trade-offs

- **R1 Crowding.** A note with many fetched chunks near the top of the order spends the budget on its secondaries, and the admitted prefix shrinks, so fewer deep notes can be promoted. → The page notes are always admitted (D2a). `rerank_candidates` and `rerank_documents` are on every usage row, so the shrinkage is measurable. The gate decides. A per-note cap is the ready remedy (open question 2).
- **R2 Max bias.** The maximum over k noisy scores rises with k, which favours notes with many fetched chunks. → The budget bounds k. The gate measures whether the bias costs recall. Normalisation is a follow-up only if the gate shows it.
- **R3 Longer documents, more latency.** → D8: the worst case is unchanged, the latency is measured, and the deployment timeout is raised in infra. A timeout falls back honestly, as today.
- **R4 Stored-prefix staleness.** → L3. The window is one pass, and only for rows the index still calls fresh.
- **R5 Similarity and preview from different chunks.** → L5. It is documented. `similarity` is a note-level retrieval score in the encoding.

## Migration Plan

No migration and no reindex: every column already exists. Deploy is merge → image → Flux. The infra PR sets `RERANK_TIMEOUT_MS=20000` on the reference deployment after this image is live (the ceiling must ship first). Rollback is the previous image. `rerank=false` is untouched, so turning off `rerank` is a caller-side rollback.

## Owner decisions (2026-10-31, from the brief)

1. The document is `embed_prefix + "\n\n" + chunk_text` (stamped formatter), cut to `RERANK_DOC_CHARS`. Stale rows and keyword-only candidates keep today's document.
2. Per-chunk candidates from the already-fetched rows, ≤ 50 documents, per-note max. The locator and preview follow the max: **yes**.
3. Latency is documented from the P4000 measurement. Infra raises `RERANK_TIMEOUT_MS` to 20 s, and the code default stays.
4. The weak-score skip is declined, as a follow-up with its evidence bar (D9).
5. `rerank=false` is byte-identical, and `rerank=true` changes only as the D5 oracle allows.
6. All-or-nothing, the codes, the probe and `/health rerank` are unchanged.
7. The prefix is reconstructed from stored columns, never from the vault (L3).

## Accepted limitations

- **L1** Depth comes only from rows the overfetch returned. A note's best-for-reranker chunk outside the window is never scored. There is no extra retrieval, by decision.
- **L2** The max rule is biased toward notes with many fetched chunks (R2).
- **L3** The stored prefix is what the vector was built from. A title or tag edit makes the row stale at the next pass, which then sends no stored text. Until that pass, the stale prefix is sent. This is the same "what the index knows" bound as #200's chunk text.
- **L4** The 2,048 cut can drop up to 302 characters of a full-window chunk (`W = CHUNK_SIZE × 4` = 2,048 by default). A title longer than 80 characters reaches the reranker cut, as a prefix component.
- **L5** `similarity` keeps the cosine-best chunk's value while the preview and locator may show the max chunk.
- **L6** A hybrid row that renders a keyword snippet keeps the snippet. Only its locator follows the scored chunk.
- **L7** `candidates: N` can be smaller than today's for `semantic_search` with `limit > 10` and for `hybrid_search` (the admitted prefix, R1).
- **L8** The 1,500 ms default times out most 50-document calls on P4000-class hardware. Operators size it from DEPLOYMENT.md.
- **L9** This retires #367 L2 ("a note is judged on its best-distance chunk only"). #367 L4 (a stale candidate is scored on its title or excerpt) stands.

## Open Questions

1. The timeout ceiling: 30,000 (proposed, headroom over the 20 s infra value) or exactly 20,000?
2. A per-note document cap (issue follow-up 1, k = 3) against R1 and R2, or "every fetched chunk" as decided, with the gate as arbiter?
3. Pass 2 in note order (proposed: one rule for both tools, and the prefix property) or in global distance order for `semantic_search`?
4. Are the two named judgments information only (as briefed), or a release bar ("both top 3 or no merge")?
