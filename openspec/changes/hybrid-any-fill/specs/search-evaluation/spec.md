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
