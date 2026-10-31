# search-evaluation Specification

## Purpose
Measure retrieval quality offline, against implicit relevance judgments mined
from the operator's own `usage_logs` (a search followed by a successful read
of one of its logged results by the same credential principal), so that any
change to ranking, chunking, the embedding model or provider, query parsing,
fusion or reranking is compared with the incumbent on the same judgments,
without writing to production and without publishing another tenant's data.
## Requirements
### Requirement: Relevance judgments SHALL be mined only from a search followed by a successful read of one of its logged results by the same credential principal
The miner SHALL credit a judgment `(query, path)` only under one condition. A usable `read_note` row for `path` must have, by the same principal, a usable `keyword_search`, `semantic_search` or `hybrid_search` row that is strictly earlier, falls within the configured window (default 120 seconds), and has logged `result_paths` containing `path` by exact string equality. When several searches qualify, only the latest SHALL be credited.

The principal SHALL be the credential principal: `(user_id, "api_key", key_id)` when `key_id` is set, otherwise `(user_id, "oauth", grant_id)` when `oauth_token_id` resolves to a token row carrying a `grant_id`. A row whose principal cannot be resolved SHALL be excluded and counted as `unresolved_principal`, and SHALL NOT be merged under `actor_ref`.

A usable row carries no `error` marker and no true `over_quota`. A usable search also carries a `result_paths` array of strings and a non-empty query that the 200-character param cap did not truncate. A usable read carries a `path` that was not truncated. Every excluded row, and every read that credits nothing, SHALL be counted by reason in `mine-report.json`.

#### Scenario: A read of a shown path within the window is credited
- **WHEN** principal P runs `keyword_search("deploy runbook")` whose `result_paths` contains `Ops/Runbook.md`, and 40 seconds later P's `read_note("Ops/Runbook.md")` succeeds
- **THEN** the qrels contain `Ops/Runbook.md` with grade 1 for that query

#### Scenario: A read outside the window or of an unshown path is not credited
- **WHEN** P successfully reads a path from the search's `result_paths` 300 seconds after the search, or reads a path the search did not return
- **THEN** no judgment is produced and the read is counted as unattributed

#### Scenario: Failed reads and refused searches contribute nothing
- **WHEN** the read row carries `error: "not_found"`, or the search row carries `over_quota: true` or `error: "rate_limited"`
- **THEN** that row is excluded and counted under its exclusion reason

#### Scenario: Truncated and pre-telemetry rows are excluded
- **WHEN** a search's logged query is 201 characters ending in `…`, or a search row has no `result_paths`
- **THEN** the row is excluded and counted as `query_truncated` or `no_telemetry` respectively

#### Scenario: Only the latest qualifying search is credited
- **WHEN** P runs search A and then search B, both return `X.md`, and P then reads `X.md` within the window of both
- **THEN** only B's query receives the judgment

#### Scenario: Two grants of the same OAuth client are two principals
- **WHEN** user 7 holds grants G1 and G2 for the same OAuth client (same `actor_ref`); G1 searches `"taxes"` and gets `X.md`, then G2 searches `"receipts"` and also gets `X.md`, then G1 reads `X.md` within the window of both
- **THEN** `"taxes"` receives the judgment and `"receipts"` receives none

#### Scenario: An unresolvable principal is excluded, not merged
- **WHEN** a search or read row has `key_id` NULL and an `oauth_token_id` that is NULL or resolves to no token row
- **THEN** the row is excluded, counted as `unresolved_principal`, and never paired with rows that share its `actor_ref`

#### Scenario: A hybrid_search row credits like the other search tools
- **WHEN** principal P runs `hybrid_search("deploy runbook")` whose `result_paths` contains `Ops/Runbook.md`, and 30 seconds later P's `read_note("Ops/Runbook.md")` succeeds
- **THEN** the qrels contain `Ops/Runbook.md` with grade 1 for the query identified by P's owner, the normalized text and the filters, the same query a `keyword_search` with that text and those filters folds into

#### Scenario: A keyword-only hybrid row is usable
- **WHEN** the `hybrid_search` row carries `arms: "keyword_only"` and no `error` marker
- **THEN** it SHALL be treated as a usable search row

### Requirement: Mined queries SHALL be identified by owner, normalized text and filters, carry a set of sources, and be classified as identifier or prose
Each judged query SHALL be identified by its `user_id`, its whitespace-normalized text (case preserved) and the canonical JSON of its `folder`, `tags` and `frontmatter` filters. Searches that share that identity SHALL fold into one query, whose relevant set is the union of their credited paths and whose pool is the union of their logged `result_paths`.

Each query SHALL carry `sources`, a subset of `{mined, manual}`. A manual judgment whose identity matches a mined query SHALL merge into it: the relevant set becomes the union, and `sources` becomes both values. That query SHALL be counted and scored in both the `mined` and the `manual` slice, and the report SHALL give the count of such queries.

Each query SHALL carry a class, `identifier` or `prose`, from a pure, table-tested classifier. An explicit class in a manual judgment SHALL override the classifier.

#### Scenario: The same query from two users stays two queries
- **WHEN** user 1 and user 2 each search `"weekly review"` and each read `Weekly.md`
- **THEN** the qrels hold two distinct query ids, each with its own owner's judgment

#### Scenario: Identifier and prose queries are classified
- **WHEN** the queries `snake_case_name`, `#356`, `src/main.py` and `how do I rotate the backup keys` are classified
- **THEN** the first three are `identifier` and the last is `prose`

#### Scenario: A manual judgment colliding with a mined query
- **WHEN** a mined query has relevant `A.md`, and a manual line with the same owner, text and filters lists `B.md`
- **THEN** that query has relevant {A.md, B.md} and sources {mined, manual}, it appears in both source slices, and the report counts it once in `n_both_sources`

### Requirement: The usage export SHALL be a single read-only transaction containing only a SELECT, published atomically
`make search-eval-export` SHALL read `usage_logs` only by running `scripts/search_eval_export.sql` with `psql` through `DB_EXEC`. That file SHALL consist of exactly `BEGIN TRANSACTION READ ONLY`, one `SELECT` (which may join `oauth_tokens` to resolve `grant_id`), and `COMMIT`.

The export SHALL emit one JSON object per line, limited to the fields the miner consumes, over the `keyword_search`, `semantic_search`, `hybrid_search` and `read_note` rows of a bounded window. It may optionally be restricted to one `user_id`. It SHALL write to a temporary file, rename the file into place only when `psql` succeeds, and remove the temporary file on failure.

#### Scenario: The export file's shape is pinned
- **WHEN** a statement other than the read-only BEGIN, the one SELECT and COMMIT is added to the export SQL
- **THEN** the offline test that parses the file fails

#### Scenario: The Make target itself is exercised against a real server
- **WHEN** the integration gate runs `make search-eval-export` with `DB_EXEC` set to the local test shim and real `psql`, against a database seeded with search and read rows
- **THEN** the output file is `0600` in a `0700` directory, every line parses as JSON, the miner produces the expected judgments, and the seeded rows are unchanged

#### Scenario: A failed export leaves nothing behind
- **WHEN** the same target runs against a database name that does not exist
- **THEN** it exits non-zero, no `.tmp` file remains, and any earlier `usage-export.jsonl` is unchanged

### Requirement: Drift SHALL be measured against an immutable pre-candidate snapshot of per-arm searchable membership, and arm-specific corpus loss SHALL invalidate the run
A `snapshot` step SHALL run on the restored copy before any candidate derivation. For every `notes_metadata` row it SHALL record, per `user_id` (matched NULL-safe), the path and its searchable membership:
- `metadata`: the row exists;
- `keyword`: `notes_metadata.content_tsvector IS NOT NULL`;
- `semantic`: at least one `note_embeddings` row with `note_id = notes_metadata.id`.

`notes_metadata.embedded_content_hash` SHALL NOT affect membership, because stale vectors stay searchable. The snapshot is written to `corpus-snapshot.json` together with its `sha256`.

At score time, a judged path absent from the snapshot SHALL be dropped as vault drift and counted, and a query left without judged paths SHALL be dropped and counted.

Every arm SHALL have a membership. The built-in `keyword` and `semantic` arms have their own. An imported DB-backed factory SHALL declare one of `metadata`, `keyword`, `semantic`, `keyword_and_semantic` or `keyword_or_semantic`, or the run exits 64. `keyword_and_semantic` means `keyword AND semantic`: both constituents are required. `keyword_or_semantic` (the membership of a union-retrieving arm such as `hybrid_search`, #354) means a path is searchable before when the snapshot marks it `keyword` OR `semantic`. For such a path, corpus loss is **per constituent**: the path counts as that arm's `corpus_loss` when any constituent the snapshot marks present (`keyword`, `semantic` or both) is absent from the replay database, even if the other constituent still reaches it. A path the snapshot marks neither `keyword` nor `semantic` is `unsearchable_before` for that arm.

A judged path that the snapshot marks searchable under an arm's membership, but that the replay database does not, SHALL remain relevant and count as a miss for that arm. It SHALL be reported as that arm's `corpus_loss`. Any non-zero `corpus_loss` SHALL mark the verdict `CORPUS_LOSS` and make `score` exit 2.

The replay corpus SHALL never be used to shrink the qrels.

#### Scenario: A candidate that loses an indexed note is not rewarded
- **WHEN** query Q has relevant {A.md, B.md} in the snapshot, the incumbent returns both, and a candidate's derivation removes B.md from `notes_metadata` on the copy, so that the candidate returns only A.md
- **THEN** Q keeps {A.md, B.md}, the candidate's recall@10 for Q is 0.5, the report shows `corpus_loss: 1` for the candidate, the verdict is `CORPUS_LOSS`, and the exit code is 2

#### Scenario: Losing only the embeddings is loss for the semantic arm
- **WHEN** B.md is `keyword` and `semantic` searchable in the snapshot, and a candidate re-embedding pass deletes B.md's `note_embeddings` rows, then fails, leaving its `notes_metadata` row and `content_tsvector` intact
- **THEN** the `semantic` arm, and any arm whose membership is `semantic`, `keyword_and_semantic` or `keyword_or_semantic`, reports `corpus_loss: 1` and verdict `CORPUS_LOSS` with exit 2; the `keyword` arm reports no loss for B.md

#### Scenario: A path an AND arm could not reach before is not its loss
- **WHEN** C.md had a `content_tsvector` but no `note_embeddings` rows in the snapshot
- **THEN** C.md is `unsearchable_before` for a `keyword_and_semantic` arm and never counts as that arm's corpus loss

#### Scenario: A keyword-only note reachable by the hybrid is the hybrid's loss
- **WHEN** C.md had a `content_tsvector` but no `note_embeddings` rows in the snapshot, and a candidate derivation clears C.md's `content_tsvector` on the copy
- **THEN** C.md counts as `corpus_loss: 1` for a `keyword_or_semantic` arm and for the `keyword` arm, the verdict is `CORPUS_LOSS`, and the exit code is 2

#### Scenario: A semantic-only note reachable by the hybrid is the hybrid's loss
- **WHEN** D.md had `note_embeddings` rows but a NULL `content_tsvector` in the snapshot, and a candidate re-embedding pass deletes D.md's `note_embeddings` rows
- **THEN** D.md counts as `corpus_loss: 1` for a `keyword_or_semantic` arm, the verdict is `CORPUS_LOSS`, and the exit code is 2

#### Scenario: Losing one constituent of a two-arm note is still loss for the union arm
- **WHEN** B.md is `keyword` and `semantic` searchable in the snapshot and the candidate loses only its `note_embeddings`, so the hybrid's keyword arm still reaches it
- **THEN** B.md counts as `corpus_loss: 1` for a `keyword_or_semantic` arm, because the candidate changed what the semantic constituent can see

#### Scenario: A path neither arm could reach is not the union arm's loss
- **WHEN** E.md had neither a `content_tsvector` nor `note_embeddings` rows in the snapshot
- **THEN** E.md is `unsearchable_before` for a `keyword_or_semantic` arm

#### Scenario: A renamed note is drift, not loss
- **WHEN** a judged path is absent from the snapshot
- **THEN** it is removed from that query's relevant set and counted as drift, and a query left empty is dropped and counted

#### Scenario: No snapshot, no real score
- **WHEN** `make search-eval` is run on a real data directory without `corpus-snapshot.json`
- **THEN** the recipe exits 64 (make reports Error 64) and names the snapshot step

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

### Requirement: Retrieval arms SHALL be pluggable callables scored against the same judgments, and a run SHALL be published only when complete
An arm SHALL be an async callable that takes an evaluation query (text, owner, filters, class, sources) and a depth, and returns ranked vault paths, best first. The harness SHALL provide built-in `keyword` and `semantic` arms that call the current `full_text_search` and `semantic_search` services with the query's owner and filters. It SHALL load `<module>:<factory>` arms by import, and `runfile:<path>` arms from a run file written by its own `run` step on another database.

Every `run` invocation SHALL have a generation id. The id is supplied by the caller or defaulted once by the CLI, and an id that already exists is refused.

Before any arm or factory executes, `run` SHALL atomically write `runs/manifest.json` with `status: "running"`, the generation, the hash of the exact selected qids, the qrels and snapshot hashes, the arms, the options and the depth. Arm files SHALL be staged under that generation's directory.

Any exception raised by an arm or its factory, and any output that is not a list of strings, SHALL abort the run with exit 1, and the manifest SHALL be rewritten atomically with `status: "failed"` and the error. Only after every arm has answered every selected query SHALL the manifest be rewritten atomically with `status: "complete"` and each staged file's sha256.

`score` SHALL read run data only through the manifest, and SHALL NOT fall back to an earlier generation. It SHALL exit 64 unless all of the following hold:
- `status` is `complete`;
- the manifest's selected-qid hash equals the hash of the qids `score` selects now under the manifest's selection options;
- the qrels and snapshot hashes match the data directory;
- every referenced file matches its recorded sha256.

The only tolerated omission is a `runfile:` arm's missing queries, which SHALL be reported by count and treated as empty results.

#### Scenario: A candidate arm from a follow-up change
- **WHEN** `make search-eval ARMS="keyword semantic mypkg.hybrid:make_arm"` is run
- **THEN** all three arms are scored against the same qrels in one report

#### Scenario: A failed attempt cannot be scored from yesterday's run
- **WHEN** yesterday's generation G1 completed for the same qrels, snapshot and selection, and today's generation G2's candidate answers 60 queries and raises on query 61
- **THEN** `run` exits 1, the manifest names G2 with `status: "failed"`, G1's files are left on disk but unreferenced, and a subsequent `score` exits 64 instead of scoring G1

#### Scenario: A changed selection is refused
- **WHEN** a complete manifest was produced with `--limit-queries 40` and `score` now selects 50 qids
- **THEN** the selected-qid hashes differ and `score` exits 64

#### Scenario: A run produced against another database
- **WHEN** an arm is given as `runfile:/path/run.json`, written by `run` on a re-embedded copy with the same qrels and snapshot hashes
- **THEN** it is scored against those qrels, its own recorded corpus loss is reported, and selected queries absent from the file are reported as missing and scored as empty

#### Scenario: Arm order is preserved
- **WHEN** an arm returns a duplicate path, or more paths than the depth
- **THEN** the first occurrence keeps its rank, the list is truncated to the depth, and scoring ranks exactly in the returned order

### Requirement: Scoring SHALL report recall@5, recall@10, MRR and nDCG@10 per arm, per query class and per source, with judgment-coverage counts
The `score` step SHALL compute recall@5, recall@10, recall@20, recall@50, MRR and nDCG@10 for each arm with the pure-Python functions in `scripts/search_eval/metrics.py`, using binary gains and only the first occurrence of a duplicate path. It SHALL report them overall, per class (`identifier`, `prose`) and per source (`mined`, `manual`). recall@20 and recall@50 measure recall at investigation depth: an agent reads beyond the first ten candidates, and 50 is both the largest `limit` the search tools accept and `hybrid_search`'s arm depth, so recall@50 is the ceiling of what one page can reach. The `run` step's default `--depth` SHALL be 50. Each arm SHALL carry a `staged_depth` in its staged metadata and its `report.json` entry: the generation's depth for an arm run in that generation, and for a `runfile:` arm the minimum of the generation's depth and the source file's own recorded `depth`, which the runner SHALL preserve and validate (a missing, non-integer or non-positive source depth, or a `staged_depth` that is present and not a positive integer, SHALL fail the run with exit 64; the source's effective depth is the smaller of its `depth` and its `staged_depth`). A recall@k whose k exceeds the arm's `staged_depth` SHALL be reported as null, never computed on a shorter run, and every delta and interval involving it SHALL be null. The report's `schema_version` SHALL be 2. Alongside each it SHALL report the query count and `novel@10`, the mean share of the arm's top ten that is outside the query's judged pool.

With `--baseline`, it SHALL report, per metric (recall@20 and recall@50 included) and per slice, the per-query deltas against the baseline, their mean, and a bootstrap 95 % confidence interval on the mean. The interval SHALL be computed from 1,000 resamples drawn with a seeded pseudo-random generator, so the same input and seed always give the same interval, and it SHALL NOT affect the exit code.

`score` SHALL write a `report.json` that contains no query text and no note path. Its exit codes SHALL be:
- 0 when scored;
- 2 when fewer than `--min-queries` judged queries remain (`INSUFFICIENT_DATA`) or any arm has corpus loss (`CORPUS_LOSS`);
- 64 on a usage error or an incomplete or mismatched run;
- 1 on any other failure.

No metric threshold applies, and the verdict text SHALL be unchanged by the added metrics.

#### Scenario: Metric edge cases
- **WHEN** a query has relevant set {a, b}, and is scored against an empty run, against the run [x, a, a] with k = 10, and against the run [a] with k = 5
- **THEN** the empty run scores 0 on every metric; [x, a, a] scores recall@10 0.5, reciprocal rank 0.5 and nDCG@10 (1/log2 3)/(1 + 1/log2 3); and [a] scores recall@5 0.5; a relevant item at deduplicated rank 30 scores recall@20 0 and recall@50 1 / |relevant|

#### Scenario: The baseline comparison is reproducible and does not gate
- **WHEN** `score --baseline keyword` runs twice on the same data with the same seed
- **THEN** both reports carry identical deltas and confidence intervals, and the exit code is 0 even when every delta is negative

#### Scenario: Too little data is not a verdict
- **WHEN** 12 judged queries survive and `--min-queries` is 50
- **THEN** the metrics are printed, the report is marked `INSUFFICIENT_DATA`, and the exit code is 2

#### Scenario: The report is safe to attach to a PR
- **WHEN** `report.json` is written for a real data set
- **THEN** it holds metrics, counts, arm names, options and memberships, the window, the seed, the qrels and snapshot hashes, the generation, `provenance`, `attachable` and `commit_sha`, and no query text or note path

#### Scenario: A shallow runfile baseline is not scored at depth, end to end
- **WHEN** a `runfile:` baseline whose source metadata records `depth: 20` is imported by `run` into a fresh generation at the default depth 50 and scored beside a database-backed candidate
- **THEN** the baseline's staged metadata and report entry SHALL carry `staged_depth: 20`, its recall@50 SHALL be null, the recall@50 delta and interval SHALL be null, its recall@20 and every shallower metric SHALL be scored as before, and the candidate SHALL carry `staged_depth: 50`

#### Scenario: A runfile without a usable depth
- **WHEN** a `runfile:` source's metadata has no `depth`, or `depth: 0`, or `depth: "20"`
- **THEN** `run` SHALL fail with exit 64 and publish nothing

#### Scenario: The golden file carries the new metrics
- **WHEN** `make search-eval` runs on the committed synthetic fixture
- **THEN** its stable projection SHALL equal the committed golden file, regenerated once by this change, which carries `recall@20`, `recall@50` and `staged_depth` for every arm, the default depth 50 and `schema_version: 2`

### Requirement: Only a report with single-user provenance SHALL be marked attachable
`mine` SHALL derive the provenance of the evidence from the export, and `score` SHALL write it into `report.json`. `provenance.users` SHALL be `1` in two cases: the export was restricted with `USER_ID=`, or the deployment has at most one user and the export holds at most one distinct `user_id`. Otherwise it SHALL be `"multi"`, and an empty export SHALL also be `"multi"`.

`attachable` SHALL be true exactly when `provenance.users` is `1`. A non-attachable report SHALL still be produced for the operator's private use, and its table SHALL begin with a `NOT ATTACHABLE` line.

#### Scenario: An unfiltered multi-user export is not attachable
- **WHEN** the export covers two users and was run without `USER_ID=`
- **THEN** the report has `provenance.users = "multi"` and `attachable: false`, the table's first line says it is not attachable, and the exit code is unaffected

#### Scenario: A single-user export is attachable
- **WHEN** the export was run with `USER_ID=7` on a multi-user deployment
- **THEN** the report has `provenance.users = 1` and `attachable: true`

### Requirement: Real evaluation data SHALL live only in a git-ignored, owner-only directory
The export, mined queries, snapshot, qrels and runs for real data SHALL be written only under `SEARCH_EVAL_DIR`. It defaults to `.search-eval/`, which is listed in `.gitignore`, and SHALL be created with mode `0700` and its files with `0600`. No real data SHALL be committed, uploaded as a CI artifact, or required by any test.

#### Scenario: The default directory is ignored
- **WHEN** `make search-eval-export` has written `.search-eval/usage-export.jsonl`
- **THEN** `git status` shows no new file, the directory is `0700` and the file is `0600`

### Requirement: make search-eval SHALL pass offline on a committed synthetic fixture, and the fixture test SHALL run unconditionally in CI against a stable projection of the report
`make search-eval` with no data argument SHALL run mine, run and score on `tests/fixtures/search_eval/` with fixture-only arms. It SHALL need no database, embedding provider or real data, and SHALL write only to a temporary directory.

A pytest module with no skip condition SHALL run the same pipeline. It SHALL assert that the report's documented stable projection, which excludes `commit_sha` and any timestamp, equals the committed expected report. It SHALL separately assert that `commit_sha` is a 40-character lowercase hex string equal to `git rev-parse HEAD`.

#### Scenario: The fixture pipeline in CI
- **WHEN** CI's tests job runs
- **THEN** the search-eval fixture test executes (not skipped), its stable projection equals `expected-report.json`, its `commit_sha` equals the checked-out HEAD, and it asserts `attachable: false` for the two-user fixture and `attachable: true` for the same fixture filtered to one user

#### Scenario: A new commit does not break the golden file
- **WHEN** a commit changes only unrelated files
- **THEN** the fixture comparison still passes, because `commit_sha` is outside the projection

### Requirement: The harness SHALL add no third-party dependency
The harness SHALL use only the Python standard library and modules already in `requirements.txt`, and SHALL change no `requirements*.txt` file. The built-in database arms import `src.services` lazily, so export, mining, scoring and the fixture pipeline run without database settings.

#### Scenario: Dependency files are unchanged
- **WHEN** the change's diff is inspected
- **THEN** no `requirements*.txt` file or Dockerfile is modified, and the fixture pipeline runs with no `DATABASE_URL` set

### Requirement: A search-path change SHALL attach a real-data search-eval report comparing baseline and candidate arms
Any change to retrieval ranking, chunking, embedding model or provider, query parsing, fusion or reranking SHALL attach to its PR the `report.json` of `make search-eval`, run on real data against a restored copy. That report SHALL have `attachable: true`. The incumbent and the candidate SHALL be arms on the same qrels and snapshot, with the incumbent as `--baseline`. CLAUDE.md's local-gates table SHALL list this gate, and so SHALL say that a non-attachable report may not be attached.

#### Scenario: The gate is documented
- **WHEN** CLAUDE.md's local-gates table is read after this change
- **THEN** it has a `make search-eval` row that names the gate as required for search-path changes and gives the real-data invocation

### Requirement: The harness SHALL ship a replayable hybrid arm whose fusion constant is an arm option
`scripts/search_eval_arms/hybrid.py` SHALL expose two database-backed factories, `make_arm` and `make_arm_alt`, each declaring `membership = "keyword_or_semantic"`. Each SHALL answer a query by calling the production `src.services.hybrid.hybrid_search` service directly, never through `_tracked`, with the query's text, owner and filters, `limit` equal to the smaller of the depth and 50, and one fresh session per query from the read-only session factory. `make_arm` SHALL take its fusion constant from the arm option `hybrid_k`, defaulting to the `HYBRID_RRF_K` setting. `make_arm_alt` SHALL require the option `hybrid_alt_k`, and its factory SHALL raise when the option is absent, so that one report can compare two values of k on the same qrels and snapshot. An arm SHALL raise, and so fail the run, when the service answers keyword-only. The hybrid arms SHALL write nothing to the replay database.

#### Scenario: Replaying the production fusion
- **WHEN** `make search-eval` runs on a restored copy with `ARMS="keyword semantic scripts.search_eval_arms.hybrid:make_arm"`
- **THEN** the report SHALL contain a hybrid arm with membership `keyword_or_semantic` and the arm options it ran with, and its answers SHALL equal the fused paths `hybrid_search` returns for the same query, owner, filters and k

#### Scenario: A/B of k in one report
- **WHEN** the run names both factories with `--arm-opt hybrid_k=60 --arm-opt hybrid_alt_k=20`
- **THEN** the report SHALL score both arms on the same qrels, snapshot and selection hashes

#### Scenario: A degraded arm is not evidence
- **WHEN** the embedding provider fails during replay
- **THEN** the run SHALL fail with exit 1 and a `failed` manifest, and no hybrid score SHALL be written

#### Scenario: The arm writes nothing
- **WHEN** the integration test replays the hybrid arm and reads `pg_stat_user_tables` before and after
- **THEN** no insert, update or delete counter SHALL have moved

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

### Requirement: A semantic arm SHALL record the retrieval fingerprint and the replay copy's legacy-chunked note count
For every database-backed arm whose membership includes `semantic`, the harness's `run` step SHALL record three values, in the arm's staged metadata and in its `report.json` arm entry:

- **`retrieval_fingerprint()`**, as the producing process computed it.
- **`legacy_chunked_notes`**: the number of the replay copy's notes, **within the arm's provenance owner**, that hold vector rows and whose `embedded_chunking_version` is not the current `CHUNKING_VERSION`.
- **`chunk_lengths`**: `{chunks, p10, p50, p90, whole_short, under_min, raw_over_window}` over the provenance owner's stored chunks, or null when the owner has no chunks or the copy predates migration 034.
  - `clen` SHALL be the index-integrity collapsed length, computed in SQL as `length(btrim(regexp_replace(chunk_text, '[\t\n\v\f\r ]+', ' ', 'g')))`. That expression and the chunker's Python function SHALL be kept as one pair and tested for agreement.
  - `m` SHALL be the running effective minimum and `W` the running window in characters.
  - The percentiles SHALL be discrete percentiles of `clen`.
  - `whole_short` SHALL count chunks with `clen < m` whose note holds exactly one chunk.
  - `under_min` SHALL count chunks with `clen < m` whose note holds more than one chunk, with no other exception, and SHALL be 0 for a conforming chunker at any `CHUNK_SIZE` above 100 (below it, index-integrity's small-window residue may count).
  - `raw_over_window` SHALL count chunks whose raw `length(chunk_text)` exceeds `W`.

**Provenance owner.** It is the `user_id` the export was filtered to (`USER_ID=` or `mine --user-id`), read from the `user_id` of the report's queries. The miner guarantees one owner for a single-user report. It SHALL be compared NULL-safe, so the ownerless scope is covered. A single-user report with no queries records null for both counts. Only when the report's provenance is `"multi"`, and the report is therefore not attachable, SHALL the counts cover all owners.

**Computation.**
- Each count SHALL be computed by one read-only `SELECT` carrying that owner predicate.
- `legacy_chunked_notes` SHALL be null when the column does not exist.
- A `runfile:` arm SHALL carry the values its staged metadata holds, or null when it holds none.
- Arms of `metadata` membership SHALL NOT gain any of these keys, so the committed fixture report is unchanged.
- No value SHALL contain query text, a note path, chunk text or the instruction text.

#### Scenario: A candidate run on a fully re-embedded copy
- **WHEN** the built-in `semantic` arm runs on a copy whose every embedded note carries the current chunking version
- **THEN** its report entry SHALL carry the retrieval fingerprint, `legacy_chunked_notes: 0`, and a `chunk_lengths` object with `under_min: 0`

#### Scenario: A baseline from an older harness
- **WHEN** a `runfile:` arm staged by a harness without these fields is scored
- **THEN** its report entry SHALL carry null for each missing field, and scoring SHALL proceed

#### Scenario: Fixture report unchanged
- **WHEN** `make search-eval` runs on the committed synthetic fixture
- **THEN** its stable projection SHALL equal the committed golden file

#### Scenario: Another tenant's rows cannot change the counts
- **WHEN** a restored copy holds users 7 and 8, the export used `USER_ID=7`, user 7 has no legacy-chunked notes and user 8 has 37, and user 8 holds chunks shorter than the minimum
- **THEN** the staged metadata and the attachable report SHALL carry `legacy_chunked_notes: 0`, and a `chunk_lengths` computed from user 7's chunks alone
- **AND** changing, adding or deleting user 8's rows SHALL NOT change any byte of the staged metadata or of the report

### Requirement: The harness SHALL tabulate confidence calibration evidence per tool from the usage export, read-only, with the mine provenance and attachability
`mine` SHALL write `confidence-rows.jsonl` under the data directory, one line per search usage row of `semantic_search` and `hybrid_search` that is not a refusal, has a resolved credential principal and a well-typed `result_paths` (a refusal or an unresolved principal is not evidence and cannot inflate a PASS; a query or folder cut by the 200-character telemetry cap does NOT exclude a row, its logged text serving only for the row's class), carrying exactly `tool`, `class`, `confidence` (the usage row's recorded key, absent when the row predates this change), `created_at` (UTC, from the usage row), `top_similarity`, `keyword_all` and `clicked` (the miner credited a judgment to that search row), and no query text or note path. `python -m scripts.search_eval confidence --data <dir> --threshold <T>` SHALL read that artifact and connect to no database; each tool's span in days SHALL be computed from the `created_at` of its post-change rows. A row is **post-change** exactly when it carries the recorded `confidence` key; a row without it was logged before this change and SHALL be counted as excluded and not tabulated. Post-change rows whose `top_similarity` is null (a keyword-only `hybrid_search` page, or an empty candidate set) SHALL be included: the would-be value at `T` SHALL be computed by the shared rule from `keyword_all` (0 for `semantic_search`) and `top_similarity` as recorded, so such a row with `keyword_all = 0` reads `low` at every `T > 0`; a post-change `hybrid_search` row whose `keyword_all` is null SHALL count as 0 (the stricter reading). `clicked` SHALL be attributed over these rows and the replay-usable searches together, one read crediting at most one search. `mine` SHALL publish `confidence-rows.jsonl` before `mine-report.json` and record its SHA-256 and row count in the report, and the tabulation SHALL refuse (exit 64) unless both match, so rows of another `mine` run never tabulate under this run's provenance. The 14-day span SHALL be judged on the unrounded span.

For each tool (`semantic_search`, `hybrid_search`) separately, and within it per query class (`identifier`, `prose`), it SHALL report the post-change row count, the observed span in days, the deciles of the non-null `top_similarity`, the share that would read `low` at `T`, and the **false-low rate**: the share of post-change clicked-through pages (a search row the miner credited with a judgment: one of its shown notes was read) that would read `low` at `T`, a keyword-only page included. It SHALL print one acceptance line **per tool**: `<tool>: false-low <rate> (<n>/<m> clicked-through pages, <d> days) — PASS|FAIL|INSUFFICIENT_DATA at < 1/50`. A tool's line SHALL be `INSUFFICIENT_DATA` (never PASS) when its post-change rows span fewer than 14 days or it has fewer than 200 clicked-through pages, including zero; otherwise PASS exactly when the rate is below 1/50. There SHALL be no aggregate PASS across tools. Because one setting enables `low` on both tools, the activation rule (the `search-quality` setting requirement) SHALL require PASS on both lines.

The output (the printed table and a JSON file of counts and rates under the data directory, with no query text and no note path) SHALL inherit the `mine` run's provenance: it SHALL carry `provenance` and `attachable`, `attachable` true exactly when `provenance.users` is 1, and for any other provenance the table SHALL begin with the existing `NOT ATTACHABLE` line. Only attachable output MAY leave the private data directory, and the documentation SHALL say so.

#### Scenario: The acceptance lines are per tool
- **WHEN** a single-user fixture export's post-change search rows span 20 days by `created_at`, with 200 clicked-through `hybrid_search` pages, none of which would read `low` at `--threshold 0.50`, and 20 clicked-through `semantic_search` pages, one of which would
- **THEN** every `confidence-rows.jsonl` line SHALL carry the usage row's `confidence` and `created_at` unchanged, the hybrid line SHALL report 20 days and read PASS, the semantic line SHALL read `INSUFFICIENT_DATA`, and no line SHALL combine the two tools

#### Scenario: A keyword-only clicked-through low counts as a false low
- **WHEN** a post-change clicked-through `hybrid_search` row has `confidence` recorded, `top_similarity` null and `keyword_all: 0`
- **THEN** it SHALL be tabulated and SHALL count as a false low at every `T > 0`; a usage row with no `confidence` key SHALL be written to `confidence-rows.jsonl` without the key, with its `created_at`, and SHALL be counted as excluded and contribute neither to a rate nor to the span

#### Scenario: Too short or too few is not a pass
- **WHEN** a tool has 250 clicked-through pages over 10 days, or 0 clicked-through pages over 30 days
- **THEN** its line SHALL read `INSUFFICIENT_DATA`

#### Scenario: Provenance and attachability
- **WHEN** the tabulation runs on the two-user fixture mined without a user filter, and again on the same fixture mined with `USER_ID=` one user
- **THEN** the first output SHALL carry `attachable: false` and begin with the `NOT ATTACHABLE` line, and the second SHALL carry `provenance.users: 1` and `attachable: true`

#### Scenario: No leak
- **WHEN** the tabulation JSON is written for a real export
- **THEN** it SHALL pass `assert_no_leak` against every mined query text and path

### Requirement: The harness SHALL provide a gate step that prints PASS or FAIL per metric and slice from a scored report
`python -m scripts.search_eval gate --report <report.json> --arm <label> --metrics <m,…> --slices <s,…> --min-ci-lower <x> --out <file>` SHALL read the `deltas` of a scored report for one candidate arm against the report's `baseline`, and SHALL print one line per (metric × slice) cell with the delta mean, the bootstrap CI95 and `PASS` or `FAIL`. A cell SHALL be `FAIL` when its CI95 lower bound is below `--min-ci-lower`, or when its CI95 is null (no delta could be computed). The step SHALL write the same table to `--out` as Markdown, ready to paste into a PR body. **The gate SHALL respect the tenant boundary.** It SHALL require `attachable: true` and `provenance.users == 1` on the candidate's report and on the baseline's report: the one `--report` when it holds both arms, and, when the baseline arm is a `runfile:` arm, also the report of the generation that staged it, named by `--baseline-report` (required in that case). That report SHALL be bound to the runfile: its `generation` SHALL equal the generation in the runfile label `runfile:<label>@<generation>`, its `qrels_sha256`, `snapshot_sha256` and `selected_qids_hash` SHALL be present and equal the candidate report's, and the arm `<label>` SHALL be among its arms; its file's SHA-256 SHALL equal the `source_report_sha256` recorded on the candidate report's runfile arm entry, whose `source_generation` SHALL equal its generation; any mismatch, or a missing digest, SHALL be refused like a non-attachable report. Before any runfile source is examined, the candidate report's `baseline` SHALL equal its `deltas_baseline` and SHALL occur exactly once among its `arms`; a report without `deltas_baseline` SHALL be refused. The scored report SHALL carry `deltas_baseline` (the arm every delta was computed against) and `schema_version` 3, and `run` SHALL record on a staged `runfile:` arm its `source_generation` and `source_report_sha256`: the SHA-256 of the source data directory's `report.json` when that report scores the source generation, else null. When any of these checks fails, the gate SHALL exit 64, print no cell and no `PASS` or `FAIL`, and SHALL neither create nor replace `--out`. These checks SHALL run before any cell is computed. It SHALL exit 0 when every cell passes, 1 when any cell fails, and 64 on a usage error (an unknown arm, metric or slice, a report without a baseline, a report whose verdict is not `OK`, or a report that is not attachable single-user evidence). It SHALL read only the reports and write only `--out`.

#### Scenario: A paired-delta release gate
- **WHEN** the gate runs with `--arm scripts.search_eval_arms.hybrid:make_arm --metrics recall@10,recall@20,recall@50,ndcg@10 --slices overall,class:identifier --min-ci-lower -0.01` on a report whose baseline is the alt arm
- **THEN** it SHALL print eight cells, and SHALL exit 1 if any cell's CI95 lower bound is below −0.01

#### Scenario: A report that is not evidence
- **WHEN** the report's verdict is `CORPUS_LOSS` or `INSUFFICIENT`
- **THEN** the gate SHALL exit 64 and print no PASS

#### Scenario: A multi-user report is refused
- **WHEN** the gate runs on an unfiltered two-tenant report with verdict `OK`, a baseline and all eight requested delta cells, which the harness marked `attachable: false` (`provenance.users` is `"multi"`)
- **THEN** the gate SHALL exit 64, print no cell and no `PASS` or `FAIL`, and SHALL NOT create the `--out` file; and when `--out` already exists, it SHALL be left unchanged

#### Scenario: An unrelated safe report cannot vouch for a runfile baseline
- **WHEN** the baseline is `runfile:<label>@G` and `--baseline-report` is an attachable single-user `OK` report whose generation is not G, whose qrels, snapshot or selection hash differs from the candidate report's, or which has no arm `<label>`
- **THEN** the gate SHALL exit 64, print no cell, and neither create nor replace `--out`

#### Scenario: A relabeled baseline is refused
- **WHEN** the report's deltas were computed against one runfile arm but its `baseline` names another label that is not among its arms (or occurs twice, or differs from `deltas_baseline`)
- **THEN** the gate SHALL exit 64 with no cell printed and `--out` neither created nor replaced

#### Scenario: A forged-but-consistent source report is refused
- **WHEN** `--baseline-report` has the runfile's generation, the matching hashes and the source arm, but its SHA-256 differs from the digest recorded on the runfile arm, or no digest was recorded
- **THEN** the gate SHALL exit 64 with no cell printed and `--out` neither created nor replaced

#### Scenario: A runfile baseline from a multi-user generation is refused
- **WHEN** `--report` is attachable and single-user but the baseline is a `runfile:` arm whose `--baseline-report` is `attachable: false`, or `--baseline-report` is missing
- **THEN** the gate SHALL exit 64 with no cell printed and `--out` neither created nor replaced

