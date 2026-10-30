## MODIFIED Requirements

### Requirement: Scoring SHALL report recall@5, recall@10, MRR and nDCG@10 per arm, per query class and per source, with judgment-coverage counts
The `score` step SHALL compute recall@5, recall@10, recall@20, recall@50, MRR and nDCG@10 for each arm with the pure-Python functions in `scripts/search_eval/metrics.py`, using binary gains and only the first occurrence of a duplicate path. It SHALL report them overall, per class (`identifier`, `prose`) and per source (`mined`, `manual`). recall@20 and recall@50 measure recall at investigation depth: an agent reads beyond the first ten candidates, and 50 is both the largest `limit` the search tools accept and `hybrid_search`'s arm depth, so recall@50 is the ceiling of what one page can reach. The `run` step's default `--depth` SHALL be 50. A recall@k whose k exceeds the depth an arm's run was staged at SHALL be reported as null, never computed on a shorter run, and its delta and interval SHALL be null. The report's `schema_version` SHALL be 2. Alongside each it SHALL report the query count and `novel@10`, the mean share of the arm's top ten that is outside the query's judged pool.

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

#### Scenario: A shallow baseline is not scored at depth
- **WHEN** a `runfile:` baseline staged at depth 20 is scored beside a candidate run at depth 50
- **THEN** the baseline's recall@50 SHALL be null, the recall@50 delta and interval SHALL be null, and every other metric SHALL be scored as before

#### Scenario: The golden file carries the new metrics
- **WHEN** `make search-eval` runs on the committed synthetic fixture
- **THEN** its stable projection SHALL equal the committed golden file, regenerated once by this change, which carries `recall@20` and `recall@50` for every arm and slice, the default depth 50 and `schema_version: 2`
