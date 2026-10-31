## 1. Extraction (from 06a76e0, harness only)

- [x] 1.1 `scripts/search_eval/gate.py`, the `gate` subcommand in `__main__.py`, the usage line in `__init__.py`
- [x] 1.2 `runner.py`: `check_options` hook (generic comment), `source_generation` + `source_report_sha256` recorded at staging
- [x] 1.3 `scoring.py`: `deltas_baseline`, `schema_version` 3, the runfile arm entry's source fields
- [x] 1.4 `tests/fixtures/search_eval/expected-report.json` regenerated once for schema 3
- [x] 1.5 `tests/test_search_eval_gate.py` without the `hybrid_alt_any_offset` section; `check_options` tested with a synthetic arm; `tests/test_search_eval_depth_confidence.py` additions
- [x] 1.6 Nothing under `src/`, no tool description, no `scripts/search_eval_arms/` change (verify with `git diff --stat origin/main`)

## 2. Docs

- [x] 2.1 DEPLOYMENT.md eval recipe: the `gate` step, the two report paths, "score the source generation before staging the runfile"
- [x] 2.2 `docs/architecture/search.md`: the gate step section (the two Codex-verified rules, L1)
- [x] 2.3 CLAUDE.md eval-gate table row mentions `gate`

## 3. Gates

- [x] 3.1 `pytest tests/test_search_eval_gate.py tests/test_search_eval_depth_confidence.py tests/test_search_eval*.py tests/test_issue_358_scoped_tools_list.py -q` (182 passed, 1 skipped)
- [x] 3.2 Full offline `pytest tests` (7,874 passed, 1,180 skipped, with `OMCP_ALLOW_SKIP_TRANSFER_INTEGRATION=1`); `make search-eval` on the fixture (exit 0, golden regenerated once for schema 3); `make audit` (clean); `openspec validate --all --strict` (41/41). No integration run: nothing under `src/` changed
- [ ] 3.3 Archive after review (`openspec archive search-eval-gate -y`), one PR
