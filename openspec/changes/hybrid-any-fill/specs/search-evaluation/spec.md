## MODIFIED Requirements

### Requirement: The harness SHALL ship a replayable hybrid arm whose fusion constant is an arm option
`scripts/search_eval_arms/hybrid.py` SHALL expose two database-backed factories, `make_arm` and `make_arm_alt`, each declaring `membership = "keyword_or_semantic"`. Each SHALL answer a query by calling the production `src.services.hybrid.hybrid_search` service directly, never through `_tracked`, with the query's text, owner and filters, `limit` equal to the smaller of the depth and 50, and one fresh session per query from the read-only session factory. `make_arm` SHALL take its fusion constant from the arm option `hybrid_k`, defaulting to the `HYBRID_RRF_K` setting. `make_arm_alt` SHALL require the option `hybrid_alt_k`, and its factory SHALL raise when the option is absent, so that one report can compare two values of k on the same qrels and snapshot. `make_arm_alt` SHALL also accept the optional option `hybrid_alt_any_offset`, `0` or `50` (default `50`; any other value SHALL be a usage error, exit 64). With `0` it SHALL replay the fusion before #403 (no `match: any` offset) by setting the hybrid service's task-local override `ContextVar` around its own service call and resetting it afterwards, so that another arm's queries never see it. No module under `src/` SHALL set that override, and the tool SHALL have no argument for it. An arm SHALL raise, and so fail the run, when the service answers keyword-only. The hybrid arms SHALL write nothing to the replay database.

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

#### Scenario: The incumbent fusion as the baseline
- **WHEN** the run names `make_arm` and `make_arm_alt` with `--arm-opt hybrid_alt_k=60 --arm-opt hybrid_alt_any_offset=0` and `BASELINE` is the alt arm
- **THEN** the alt arm's answers SHALL equal the fused paths the service returned before #403 for the same query, owner, filters and k, `make_arm`'s SHALL equal the current service's, and both SHALL be scored on the same qrels, snapshot and selection hashes

#### Scenario: An invalid offset option
- **WHEN** the run passes `--arm-opt hybrid_alt_any_offset=10`
- **THEN** it SHALL exit 64 before the generation is reserved

#### Scenario: The override never leaves the harness
- **WHEN** the source tree under `src/` is parsed
- **THEN** no statement SHALL set the hybrid service's offset override

## ADDED Requirements

### Requirement: The harness SHALL provide a gate step that prints PASS or FAIL per metric and slice from a scored report
`python -m scripts.search_eval gate --report <report.json> --arm <label> --metrics <m,…> --slices <s,…> --min-ci-lower <x> --out <file>` SHALL read the `deltas` of a scored report for one candidate arm against the report's `baseline`, and SHALL print one line per (metric × slice) cell with the delta mean, the bootstrap CI95 and `PASS` or `FAIL`. A cell SHALL be `FAIL` when its CI95 lower bound is below `--min-ci-lower`, or when its CI95 is null (no delta could be computed). The step SHALL write the same table to `--out` as Markdown, ready to paste into a PR body. **The gate SHALL respect the tenant boundary.** It SHALL require `attachable: true` and `provenance.users == 1` on the candidate's report and on the baseline's report: the one `--report` when it holds both arms, and, when the baseline arm is a `runfile:` arm, also the report of the generation that staged it, named by `--baseline-report` (required in that case). That report SHALL be bound to the runfile: its `generation` SHALL equal the generation in the runfile label `runfile:<label>@<generation>`, its `qrels_sha256`, `snapshot_sha256` and `selected_qids_hash` SHALL be present and equal the candidate report's, and the arm `<label>` SHALL be among its arms; its file's SHA-256 SHALL equal the `source_report_sha256` recorded on the candidate report's runfile arm entry, whose `source_generation` SHALL equal its generation; any mismatch, or a missing digest, SHALL be refused like a non-attachable report. Before any runfile source is examined, the candidate report's `baseline` SHALL equal its `deltas_baseline` and SHALL occur exactly once among its `arms`; a report without `deltas_baseline` SHALL be refused. The scored report SHALL carry `deltas_baseline` (the arm every delta was computed against) and `schema_version` 3, and `run` SHALL record on a staged `runfile:` arm its `source_generation` and `source_report_sha256`: the SHA-256 of the source data directory's `report.json` when that report scores the source generation, else null. When any of these checks fails, the gate SHALL exit 64, print no cell and no `PASS` or `FAIL`, and SHALL neither create nor replace `--out`. These checks SHALL run before any cell is computed. It SHALL exit 0 when every cell passes, 1 when any cell fails, and 64 on a usage error (an unknown arm, metric or slice, a report without a baseline, a report whose verdict is not `OK`, or a report that is not attachable single-user evidence). It SHALL read only the reports and write only `--out`.

#### Scenario: The hybrid-any-fill release gate
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
