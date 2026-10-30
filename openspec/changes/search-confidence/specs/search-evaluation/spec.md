## MODIFIED Requirements

### Requirement: Scoring SHALL report recall@5, recall@10, MRR and nDCG@10 per arm, per query class and per source, with judgment-coverage counts
The `score` step SHALL compute recall@5, recall@10, recall@20, recall@50, MRR and nDCG@10 for each arm with the pure-Python functions in `scripts/search_eval/metrics.py`, using binary gains and only the first occurrence of a duplicate path. It SHALL report them overall, per class (`identifier`, `prose`) and per source (`mined`, `manual`). recall@20 and recall@50 measure recall at investigation depth: an agent reads beyond the first ten candidates, and 50 is both the largest `limit` the search tools accept and `hybrid_search`'s arm depth, so recall@50 is the ceiling of what one page can reach. The `run` step's default `--depth` SHALL be 50. Each arm SHALL carry a `staged_depth` in its staged metadata and its `report.json` entry: the generation's depth for an arm run in that generation, and for a `runfile:` arm the minimum of the generation's depth and the source file's own recorded `depth`, which the runner SHALL preserve and validate (a missing, non-integer or non-positive source depth SHALL fail the run with exit 64). A recall@k whose k exceeds the arm's `staged_depth` SHALL be reported as null, never computed on a shorter run, and every delta and interval involving it SHALL be null. The report's `schema_version` SHALL be 2. Alongside each it SHALL report the query count and `novel@10`, the mean share of the arm's top ten that is outside the query's judged pool.

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

## ADDED Requirements

### Requirement: The harness SHALL tabulate confidence calibration evidence per tool from the usage export, read-only, with the mine provenance and attachability
`python -m scripts.search_eval confidence --data <dir> --threshold <T>` SHALL read the per-search-row artifact a `mine` run wrote and connect to no database. A row is **post-change** exactly when it carries the recorded `confidence` key; a row without it was logged before this change and SHALL be counted as excluded and not tabulated. Post-change rows whose `top_similarity` is null (a keyword-only `hybrid_search` page, or an empty candidate set) SHALL be included: the would-be value at `T` SHALL be computed by the shared rule from `keyword_all` (0 for `semantic_search`) and `top_similarity` as recorded, so such a row with `keyword_all = 0` reads `low` at every `T > 0`.

For each tool (`semantic_search`, `hybrid_search`) separately, and within it per query class (`identifier`, `prose`), it SHALL report the post-change row count, the observed span in days, the deciles of the non-null `top_similarity`, the share that would read `low` at `T`, and the **false-low rate**: the share of post-change clicked-through pages (a search row the miner credited with a judgment: one of its shown notes was read) that would read `low` at `T`, a keyword-only page included. It SHALL print one acceptance line **per tool**: `<tool>: false-low <rate> (<n>/<m> clicked-through pages, <d> days) — PASS|FAIL|INSUFFICIENT_DATA at < 1/50`. A tool's line SHALL be `INSUFFICIENT_DATA` (never PASS) when its post-change rows span fewer than 14 days or it has fewer than 200 clicked-through pages, including zero; otherwise PASS exactly when the rate is below 1/50. There SHALL be no aggregate PASS across tools. Because one setting enables `low` on both tools, the activation rule (the `search-quality` setting requirement) SHALL require PASS on both lines.

The output (the printed table and a JSON file of counts and rates under the data directory, with no query text and no note path) SHALL inherit the `mine` run's provenance: it SHALL carry `provenance` and `attachable`, `attachable` true exactly when `provenance.users` is 1, and for any other provenance the table SHALL begin with the existing `NOT ATTACHABLE` line. Only attachable output MAY leave the private data directory, and the documentation SHALL say so.

#### Scenario: The acceptance lines are per tool
- **WHEN** a single-user fixture spans 20 days with 200 clicked-through `hybrid_search` pages, none would read `low` at `--threshold 0.50`, and 20 clicked-through `semantic_search` pages, one of which would
- **THEN** the hybrid line SHALL read PASS, the semantic line SHALL read `INSUFFICIENT_DATA`, and no line SHALL combine the two tools

#### Scenario: A keyword-only clicked-through low counts as a false low
- **WHEN** a post-change clicked-through `hybrid_search` row has `confidence` recorded, `top_similarity` null and `keyword_all: 0`
- **THEN** it SHALL be tabulated and SHALL count as a false low at every `T > 0`; a row with no `confidence` key SHALL be counted as excluded

#### Scenario: Too short or too few is not a pass
- **WHEN** a tool has 250 clicked-through pages over 10 days, or 0 clicked-through pages over 30 days
- **THEN** its line SHALL read `INSUFFICIENT_DATA`

#### Scenario: Provenance and attachability
- **WHEN** the tabulation runs on the two-user fixture mined without a user filter, and again on the same fixture mined with `USER_ID=` one user
- **THEN** the first output SHALL carry `attachable: false` and begin with the `NOT ATTACHABLE` line, and the second SHALL carry `provenance.users: 1` and `attachable: true`

#### Scenario: No leak
- **WHEN** the tabulation JSON is written for a real export
- **THEN** it SHALL pass `assert_no_leak` against every mined query text and path
