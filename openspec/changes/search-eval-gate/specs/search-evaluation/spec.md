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
