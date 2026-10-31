Refs #402. Builds on the `reranker` capability (#367, archived 2026-10-30) and on heading-aware chunks (#355) with the chunk minimum (#390, `CHUNKING_VERSION` 2). Scope: **which documents the reranker is sent, and which candidates they come from**. The fusion offset and snippet order in `src/services/hybrid.py` belong to `hybrid-any-fill` (#403) and are not restated here; this change only adds the per-chunk candidate text to the hybrid rerank path.

## Why

With `rerank=true` the reranker demotes notes that both arms ranked first. In #402, *Virtue by Circumstance* dropped from rank 1 in both arms to outside the top 6, and the RRF #1 for "free will and determinism" fell to #4. The endpoint works: bge-reranker-v2-m3 scores a sanity pair +7.8 against −11. The fault is in the text the server sends, and it was measured live after the version-2 drain (issue #402, last comment):

1. **The reranker never sees the heading.** #355 keeps heading lines out of `chunk_text` and puts the heading path and tags in `embed_prefix`. The embedder sees that prefix. The reranker does not, because `build_document` sends `title + "\n\n" + chunk_text`. The chunk under "Victimhood Olympics" never shows the word *victimhood* to the cross-encoder. Adding the prefix raises every replayed score by 1–2 points.
2. **One chunk per note, and it is the wrong one.** The reranker scores only the chunk the semantic arm chose by cosine distance, after dedup. For this note that chunk (6, §11) is the cross-encoder's weakest at −8.87. Its best is chunk 10 at −0.62 (+0.33 with the prefix). Taking the per-note maximum keeps the note on top.

This has to land before anyone is told to turn `rerank=true` on. As things stand, the opt-in is worse than leaving it off for exactly the queries where both arms already agree.

## What Changes

- **The rerank document is the embedding input** (owner decision 1): `embed_prefix + "\n\n" + fmt[embedded_chunking_version](chunk_text)`, cut to `RERANK_DOC_CHARS` (2,048) at a code-point boundary. This is the text the embedder scored, so the cross-encoder sees the heading path and tags. The input is rebuilt from **stored columns** (`embed_prefix`, `chunk_text`, the note's stamp) and never re-derived from the vault at query time (owner decision 7). Three cases keep today's document, `title + "\n\n" + text`: a stale note (semantic: its title alone; hybrid: its title and `body_excerpt`), a keyword-only hybrid candidate (title and excerpt), and a pre-034 chunk row with a NULL prefix (title and chunk).
- **Candidates are reranked per chunk, scores are taken per note** (owner decision 2). Every non-stale chunk row of a candidate note that the call's overfetch already returned becomes a document, under a fixed budget of **at most 50 documents per call** (`RERANK_MAX_DOCUMENTS`, the same number as today's `RERANK_MAX_CANDIDATES`, so a call never sends more documents than before). The allocation is a pure function: one primary document for each note on the unreranked page, then the remaining budget walked in unreranked note order (design D2, with the proof). There is no extra retrieval, no extra statement and no extra round trip. A note's `rerank_score` is the **maximum** over its documents, and notes are ordered by that maximum, then by unreranked position.
- **The locator and preview follow the score.** On a reranked row whose maximum came from a chunk other than the cosine-best one, the `§` locator and the `chunk` preview come from that chunk, so `read_note(section="#N")` opens the section the reranker scored (design D4). The `similarity` field keeps its value.
- **Oracle** (owner decision 5). `rerank=false` pages stay byte-identical. A `rerank=true` page changes only in row order (and with it which candidates fill the page), in the `candidates: N` count and `rerank_score` values, and in the locator and preview of rows whose maximum chunk differs from the cosine-best chunk. Fallback pages equal today's baseline.
- **Unchanged** (owner decision 6): the all-or-nothing contract, the closed reason codes (now judged over the flattened document list), the probe, the re-probe, `/health rerank`, the tool schemas and the header and row grammar. This is not a tool-contract change.
- **Latency is documented from measurement** (owner decision 3). On a P4000, uncontended, 50 documents of about 410 characters take 1.6 s, and 50 of about 2,000 characters take 7.3 s. The code default `RERANK_TIMEOUT_MS=1500` stays. The setting's **ceiling rises from 10,000 to 30,000** ms, because today's `le=10000` would refuse the 20 s that the reference deployment will set in infra (not in this repo). DEPLOYMENT.md explains why.
- **Telemetry**: a `rerank=true` usage row adds `rerank_documents`, the number of documents sent.
- **Harness**: `make_semantic_arm_incumbent` and `make_hybrid_arm_incumbent` are frozen copies of today's builder and candidate set. They are the `BASELINE` for the eval gate.
- **Declined** (owner decision 4): a weak-score skip (`rerank: skipped (weak scores)`). It is a heuristic with no evidence behind it. It is recorded as a follow-up, together with the evidence that would justify it (design D9).
- Docs in the same PR: `docs/architecture/search.md` ("Reranking (#367)": the scored unit, the budget, the max rule, the locator, L2 retired), `docs/architecture/indexing-and-embeddings.md` (the embedding input now has a second reader), the CLAUDE.md reranker bullet, DEPLOYMENT.md (the latency table and the infra note), README (one sentence, plus the timeout range in the settings table), and `.env.example` (the range).

No migration. No new setting. No change to the probe, the wire format or the client.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `reranker`: MODIFIED "The reranker SHALL be configured by four optional settings…" (the `RERANK_TIMEOUT_MS` range becomes 100–30000), "A rerank=true call SHALL score only the candidates the search already fetched…" (the document is the embedding input, documents are per chunk under the 50-document budget, the score is the per-note max, the locator and preview follow it, and the projection gains two columns under `rerank=true` only), "rerank on hybrid_search SHALL rerank the first 50 fused rows…" (the same allocation over fused order), "Any rerank failure SHALL return the unreranked page…" (validation over the document list; the fallback keeps the cosine-best preview) and "The usage row SHALL record whether a call was reranked…" (`rerank_documents`).
- `search-quality`: MODIFIED "Search calls record per-phase timing" (it lists the rerank usage keys and gains `rerank_documents`).
- `search-evaluation`: MODIFIED "The harness SHALL ship replayable rerank arms…" (the two incumbent arms, and the arms rerank through the shared allocation).

## Impact

- `src/services/rerank.py`: `build_document` gains the embedding-input form, `allocate_documents` is new, `RERANK_MAX_DOCUMENTS` is new, and `rerank_candidates` takes per-note document groups and returns the per-note max plus the winning document. The exchange, validation, probe and suppressor are unchanged.
- `src/services/embeddings.py` (`semantic_arm_rows` with `with_rerank_text`, `semantic_candidates`): under `rerank=true` only, the projection adds `NoteEmbedding.embed_prefix` and `NoteMetadata.embedded_chunking_version`, and each deduped row carries its note's other fetched chunks. With `rerank=false` the statement is unchanged.
- `src/services/hybrid.py`: the semantic-arm chunk groups are carried onto `HybridRow` when `rerank_candidates`. `_fill_excerpts` is unchanged.
- `src/mcp_server/tools.py`: `_semantic_search_reranked` and `_hybrid_search_reranked` allocate, rerank, and swap a row's chunk and section to the winning chunk. `_record_rerank` adds `rerank_documents`.
- `src/config.py`: the `rerank_timeout_ms` ceiling.
- `scripts/search_eval_arms/rerank.py`: the incumbent factories.
- Tests: a replay fixture built from synthetic chunks that reproduces the #402 ordering, the ≤ 50 proof test, byte-identity of `rerank=false`, the `rerank=true` oracle, locator parity, and updates to the existing rerank tests whose one-document-per-note assumptions change.
- Gates: the eval gate (the `gate` step from `hybrid-any-fill`, 8 cells, CI95 lower bound ≥ −0.01, incumbent arm as `BASELINE`), `openspec-verifier`, and an adversarial Codex pass, which is mandatory because an agent acts on this search path. **Sequencing:** the gate step ships with `hybrid-any-fill`, so this change merges after it.
