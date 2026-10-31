## MODIFIED Requirements

### Requirement: The harness SHALL ship replayable rerank arms that fail the run rather than score an unreranked answer
`scripts/search_eval_arms/rerank.py` SHALL expose `make_semantic_arm`, declaring `membership = "semantic"` and `requires_db = True`, and, once the `hybrid_search` service of the `hybrid-search` capability exists, `make_hybrid_arm`, declaring `membership = "keyword_or_semantic"` with that capability's per-constituent corpus-loss rule (a judged path counts as the arm's `corpus_loss` when any constituent the snapshot marks present is absent from the replay database). Each arm SHALL fetch its candidates by calling the same production candidate-fetch service the tool uses (`embeddings.semantic_candidates` for the semantic arm, the hybrid service's untruncated fused candidates for the hybrid arm) directly, never through `_tracked`, with the query's text, owner and filters, `limit` equal to the smaller of the depth and 50, and one fresh read-only session per query, so that its candidates are the same up-to-50 notes production scores, whatever the depth. It SHALL then allocate documents through the same allocation function and rerank through the same rerank function the tools use, and return the reranked paths. The module SHALL also expose the incumbent arms `make_semantic_arm_incumbent` (membership `semantic`) and `make_hybrid_arm_incumbent` (membership `keyword_or_semantic`), which fetch candidates exactly as the corresponding arm does and send one document per candidate note, all of the up-to-50 candidate notes, built by a frozen copy of the builder before the `rerank-documents` change: the note's title, two newlines and its cosine-best chunk (a fresh semantic row) or its excerpt (any other hybrid row), or the title alone, at most 2,048 characters. They SHALL rerank through the same rerank function and follow every rule of this requirement. `make_hybrid_arm` and `make_hybrid_arm_incumbent` SHALL take their fusion constant from the arm option `hybrid_k`, defaulting to `HYBRID_RRF_K`. Before its first answer, each arm SHALL run the startup probe and SHALL raise a usage error if the probe fails. A query whose search returns no candidates (`no_candidates`) SHALL be an empty answer: the query SHALL be scored as returning no results, and the arm SHALL count it, recorded per arm in the generation manifest and the arm's run file as `empty_candidates`. An arm SHALL raise `UnrerankedAnswer`, and so fail the run, on every other answer that is `reranked: false`, and the hybrid arms also on any keyword-only answer. Without the hybrid service, the hybrid arms SHALL raise a usage error naming it. The built-in `semantic` arm SHALL stay unreranked, so that one report can score the incumbent and the reranked candidate on the same qrels and snapshot.

#### Scenario: A/B against the incumbent
- **WHEN** `make search-eval` runs on a restored copy with `ARMS="semantic scripts.search_eval_arms.rerank:make_semantic_arm"` and `BASELINE=semantic`
- **THEN** the report SHALL score both arms on the same qrels, snapshot and selection hashes, and the rerank arm's answers SHALL equal the paths `semantic_search(rerank=true)` renders for the same query, owner, filters and limit

#### Scenario: A/B against the incumbent document builder
- **WHEN** `make search-eval` runs with `ARMS="scripts.search_eval_arms.rerank:make_hybrid_arm_incumbent scripts.search_eval_arms.rerank:make_hybrid_arm"` and the incumbent as `BASELINE`, against a fake reranker that records every request
- **THEN** each incumbent request SHALL hold exactly one document per fused candidate (up to 50), each beginning with the note's title and none with an `embed_prefix`, and each candidate-arm request SHALL hold every candidate's primary and at most `RERANK_MAX_DOCUMENTS` documents

#### Scenario: A shallow depth measures the production allocation at that limit
- **WHEN** a run uses `--depth 10` with `make_semantic_arm_incumbent` as `BASELINE` and `make_semantic_arm`
- **THEN** both arms SHALL call the candidate service with `limit=10`, the candidate arm's requests SHALL equal those `semantic_search(limit=10, rerank=true)` sends for the same query, and recall@20 and recall@50 SHALL be null under `staged_depth`

#### Scenario: A candidate deeper than the evaluation depth is promoted
- **WHEN** the depth is 20, each note has one fetched chunk, and the fake reranker gives the highest score to the note at vector rank 23
- **THEN** `make_semantic_arm` SHALL return that note first, as `semantic_search(limit=20, rerank=true)` renders it

#### Scenario: The hybrid rerank arm keeps union membership
- **WHEN** a judged note is keyword-only in the snapshot and its keyword constituent is absent from the replay database
- **THEN** it SHALL count as `corpus_loss: 1` for `make_hybrid_arm` and for `make_hybrid_arm_incumbent`, and the verdict SHALL be `CORPUS_LOSS`

#### Scenario: A timeout during replay is not evidence
- **WHEN** the reranker exceeds `RERANK_TIMEOUT_MS` on one query during replay
- **THEN** the run SHALL exit 1 with a `failed` manifest, and no rerank score SHALL be written

#### Scenario: A query with no candidates is an empty answer
- **WHEN** a replayed query's search returns no candidates for either rerank arm
- **THEN** the run SHALL complete, that query SHALL score as returning nothing, and the manifest SHALL record `empty_candidates: 1` for that arm

#### Scenario: A failing probe stops the arm before it answers
- **WHEN** the fake reranker inverts the probe pair
- **THEN** the run SHALL exit 64 before any query is answered
