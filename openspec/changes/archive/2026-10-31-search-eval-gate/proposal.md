## Why

The search-evaluation harness reported deltas and confidence intervals but
had no mechanical bar. The first real-data evaluation run (2026-10-31), for a `hybrid_search`
ranking candidate, needed one: a step that turns a
scored report into PASS or FAIL per (metric × slice) and a table for the
PR body. That run happened and the fusion change was declined: every cell
fell within ±0.004 of zero, and recall@50's overall lower bound was
−0.0152 against a bound of −0.01. The gate step stands on its own.

Its two rules came out of Codex review. First, a gate table is publishable
evidence, so it must respect the harness's tenant boundary: only attachable
single-user reports. Second, a `runfile:` baseline's evidence must be bound
to the exact report that produced it, or an unrelated or relabeled safe
report can vouch for deltas computed from a multi-user runfile.

## What Changes

- **`python -m scripts.search_eval gate`** reads a scored `report.json` for
  one candidate arm against the report's baseline. It prints, per (metric
  × slice), the delta mean, the paired bootstrap CI95 and PASS or FAIL. A
  cell FAILs when its CI95 lower bound is below `--min-ci-lower`, or when
  its CI95 is null. The step writes the same table as Markdown to `--out`
  and exits 0 / 1 / 64.
- **The tenant boundary.** The report must be verdict `OK`, `attachable:
  true` and `provenance.users == 1`, and so must the baseline's report.
  Otherwise the step exits 64, prints no cell, and neither creates nor
  replaces `--out`.
- **The baseline binding.**
  - The report's `baseline` must equal the new `deltas_baseline` and occur
    exactly once in `arms`.
  - A `runfile:` baseline needs `--baseline-report`. That report must have
    the runfile's generation, the same three data hashes and the source
    arm, and its file SHA-256 must equal the digest `run` recorded when it
    staged the runfile.
- **Report schema 3.** `score` writes `deltas_baseline`. `run` records
  `source_generation` and `source_report_sha256` on a staged `runfile:`
  arm, and `score` carries both into the arm entry. The golden
  `expected-report.json` is regenerated once for schema 3.
- **A runner hook.** `factory.check_options(options)` validates arm-option
  values before the generation is reserved, so a bad value exits 64.
- Docs: the DEPLOYMENT.md eval recipe (the gate step, the two report paths,
  "score the source generation before staging the runfile"), the
  `docs/architecture/search.md` gate section, and the CLAUDE.md eval-gate
  row.

Nothing under `src/` changes, and no tool description, schema or output
changes.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `search-evaluation`: ADDED the gate-step requirement (the tenant
  boundary, the runfile binding, `deltas_baseline` and the staged digest).

## Impact

- `scripts/search_eval/gate.py` (new), `scripts/search_eval/__main__.py`,
  `scripts/search_eval/__init__.py`, `scripts/search_eval/runner.py`,
  `scripts/search_eval/scoring.py`.
- `tests/test_search_eval_gate.py` (new),
  `tests/test_search_eval_depth_confidence.py`,
  `tests/fixtures/search_eval/expected-report.json`.
- A report written before this change (schema 2) has no
  `deltas_baseline`, so the gate refuses it until it is re-scored.
