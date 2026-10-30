## MODIFIED Requirements

### Requirement: Replay SHALL connect only to the named URL, the built-in arms SHALL write nothing, and the read-only session default SHALL be asserted as a backstop
The `run` step SHALL connect only to the URL named by `SEARCH_EVAL_DATABASE_URL` or `--database-url`, never `DATABASE_URL`. The documented replay target, and the only one the docs and Makefile describe, is a disposable database restored from a `BACKUP_FULL=1` dump.

The step SHALL set `default_transaction_read_only=on` on every connection it creates, and SHALL assert `transaction_read_only = on` before the first arm call, aborting otherwise. The built-in `keyword` and `semantic` arms SHALL issue no write. Replay SHALL NOT pass through `_tracked`.

The shipped rerank arms (`scripts/search_eval_arms/rerank.py`) SHALL make the same embedding-provider connection the arm they wrap makes (the built-in `semantic` arm and the hybrid arm embed each query through the configured provider), plus `RERANK_URL` through `transport_security.rerank_http_client`; they SHALL NOT dial any other host. An imported `module:factory` arm runs with whatever privileges the replay URL grants. The harness SHALL document that the restored copy, not the session default, is what protects data from such an arm.

#### Scenario: The built-in arms write nothing
- **WHEN** the `keyword` arm replays seeded queries against a seeded copy
- **THEN** no table's insert, update or delete counters change

#### Scenario: An accidental write through the supplied session is caught
- **WHEN** code using the harness-supplied session issues an INSERT without changing the transaction mode
- **THEN** it fails with PostgreSQL's read-only transaction error, and `run` exits 1 without publishing run files

#### Scenario: No URL, no replay
- **WHEN** `run` is invoked with a database-backed arm and neither `SEARCH_EVAL_DATABASE_URL` nor `--database-url` is set
- **THEN** it exits with the usage-error code without connecting anywhere

#### Scenario: A rerank arm dials no new host beyond the reranker and writes nothing
- **WHEN** the integration test replays `scripts.search_eval_arms.rerank:make_semantic_arm` against a seeded copy with the fake reranker on a loopback port, reading `pg_stat_user_tables` before and after
- **THEN** no insert, update or delete counter SHALL have moved, and the fake reranker SHALL have received every rerank request the arm made

## ADDED Requirements

### Requirement: The harness SHALL ship replayable rerank arms that fail the run rather than score an unreranked answer
`scripts/search_eval_arms/rerank.py` SHALL expose `make_semantic_arm`, declaring `membership = "semantic"` and `requires_db = True`, and, once the `hybrid_search` service of the `hybrid-search` capability exists, `make_hybrid_arm`, declaring `membership = "keyword_or_semantic"` with that capability's per-constituent corpus-loss rule (a judged path counts as the arm's `corpus_loss` when any constituent the snapshot marks present is absent from the replay database). Each arm SHALL fetch its candidates by calling the same production candidate-fetch service the tool uses (`embeddings.semantic_candidates` for the semantic arm, the hybrid service's untruncated fused candidates for the hybrid arm) directly, never through `_tracked`, with the query's text, owner and filters, `limit` equal to the smaller of the depth and 50, and one fresh read-only session per query, so that its candidates are the same up-to-50 notes production scores, whatever the depth. It SHALL then rerank through the same rerank function the tools use, and return the reranked paths. `make_hybrid_arm` SHALL take its fusion constant from the arm option `hybrid_k`, defaulting to `HYBRID_RRF_K`. Before its first answer, each arm SHALL run the startup probe and SHALL raise a usage error if the probe fails. An arm SHALL raise, and so fail the run, on any answer that is `reranked: false` (for any reason), and `make_hybrid_arm` also on any keyword-only answer. Without the hybrid service, `make_hybrid_arm` SHALL raise a usage error naming it. The built-in `semantic` arm SHALL stay unreranked, so that one report can score the incumbent and the reranked candidate on the same qrels and snapshot.

#### Scenario: A/B against the incumbent
- **WHEN** `make search-eval` runs on a restored copy with `ARMS="semantic scripts.search_eval_arms.rerank:make_semantic_arm"` and `BASELINE=semantic`
- **THEN** the report SHALL score both arms on the same qrels, snapshot and selection hashes, and the rerank arm's answers SHALL equal the paths `semantic_search(rerank=true)` renders for the same query, owner, filters and limit

#### Scenario: A candidate deeper than the evaluation depth is promoted
- **WHEN** the depth is 20 and the fake reranker gives the highest score to the note at vector rank 23
- **THEN** `make_semantic_arm` SHALL return that note first, as `semantic_search(limit=20, rerank=true)` renders it

#### Scenario: The hybrid rerank arm keeps union membership
- **WHEN** a judged note is keyword-only in the snapshot and its keyword constituent is absent from the replay database
- **THEN** it SHALL count as `corpus_loss: 1` for `make_hybrid_arm`, and the verdict SHALL be `CORPUS_LOSS`

#### Scenario: A timeout during replay is not evidence
- **WHEN** the reranker exceeds `RERANK_TIMEOUT_MS` on one query during replay
- **THEN** the run SHALL exit 1 with a `failed` manifest, and no rerank score SHALL be written

#### Scenario: A failing probe stops the arm before it answers
- **WHEN** the fake reranker inverts the probe pair
- **THEN** the run SHALL exit 64 before any query is answered
