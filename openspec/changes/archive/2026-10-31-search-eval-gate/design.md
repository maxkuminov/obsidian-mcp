## Context

This step was built and reviewed for the first real-data evaluation run (2026-10-31), which measured a
ranking candidate that the owner then declined. History: the step was
extracted from the declined #403 branch (`hybrid-any-fill`), which stays as
history. This change extracts
**only the harness pieces**, ported from its head 06a76e0:

- `scripts/search_eval/gate.py`;
- the `gate` subcommand wiring;
- the runner's `check_options` hook and staging-time `source_generation` /
  `source_report_sha256`;
- scoring's `deltas_baseline`, schema 3 and the arm entry's source fields;
- the golden report, the tests and the docs.

Left behind: everything about the fusion offset, meaning the hybrid
service, renderer and description, the `hybrid_alt_any_offset` arm option
and its `ContextVar`, the hybrid tests, the `hybrid-search` delta and the
README sentence. The gate tests used the alt arm only in their (removed)
first section, and the `check_options` hook is now tested with a synthetic
arm.

## D1 — The tenant boundary (Codex spec review, round 2)

A gate table is meant for a public PR body, and per-slice deltas are tenant
data (D10 of the harness). The report must therefore be `OK`, `attachable:
true` and `provenance.users == 1`, and so must the baseline's report. Every
check runs before any cell is computed. A refusal prints nothing and leaves
`--out` untouched, whether or not it existed.

## D2 — The baseline binding (Codex implementation, rounds 1–2)

- **Round 1.** An unrelated safe report could vouch for a `runfile:`
  baseline. Fix: `--baseline-report` must carry the runfile label's
  generation, the candidate report's qrels / snapshot / selection hashes
  and the source arm.
- **Round 2.** Relabeling `baseline` bypassed that, and "generation +
  hashes + arm" is not a unique report identity. Fix:
  - `baseline` must equal `deltas_baseline` and occur exactly once in
    `arms`;
  - `run` records the SHA-256 of the source data directory's `report.json`
    when it stages the runfile (only when that report scores the source
    generation, else null);
  - the gate requires `--baseline-report`'s bytes to hash to it.
- **Round 3:** PASS.

## D3 — Schema 3

`deltas_baseline` is new (the arm the deltas were computed against). A
`runfile:` arm entry carries `source_generation` and
`source_report_sha256`. `schema_version` goes from 2 to 3, and the golden
is regenerated once. A schema-2 report has no `deltas_baseline`, so the
gate refuses it until it is re-scored.

**Operator consequence:** with a `runfile:` baseline, score the source
generation before staging the runfile, and do not re-score it afterwards.

## Accepted limitations

- L1: An operator who forges both report files and the digest metadata
  consistently can pass the gate (Codex implementation round 3). This is
  outside the trusted-operator model, and normal harness output cannot do
  it silently.
- L2: `--min-ci-lower` and the metric/slice set are per-change choices,
  not a fixed policy. `score` itself still has no threshold (E2).

## Adversarial Codex — implementation round 1

Verdict, verbatim:

FAIL. The targeted suite passes (182 passed, 1 skipped), but scoring can reject ordinary evaluation data before producing a report. The extraction is otherwise faithful to `06a76e0`: executable differences are comment-only; the disclosed hybrid-offset test section was replaced by the synthetic `check_options` test.

```json
{"verdict":"FAIL","findings":[{"severity":"MAJOR","file":"scripts/search_eval/scoring.py","line":268,"claim":"The new operator-controlled `deltas_baseline` and `source_generation` fields are not marked exact-only, so the leak scanner mistakes harmless substrings for leaked query text and aborts scoring.","failing_input":"A normal query `hybrid` with baseline arm `hybrid_probe:base`: `run` completes, but `score` raises `ReportLeak` because `hybrid` occurs inside `deltas_baseline`, and no report is written. A runfile generation containing `2026` has the same problem for a query `2026`.","fix":"Add `deltas_baseline` and `source_generation` to `_EXACT_ONLY`, and add end-to-end regression tests for overlapping query text."},{"severity":"MINOR","file":"scripts/search_eval/gate.py","line":140,"claim":"The gate never verifies `schema_version == 3`, despite the specification requiring schema 3 evidence.","failing_input":"An attachable `OK` report declaring `schema_version: 2`, but manually carrying `deltas_baseline` and eight passing cells, exits 0 and writes `gate.md`.","fix":"Refuse any candidate report whose schema version is not exactly 3 before examining cells or writing output."},{"severity":"MINOR","file":"scripts/search_eval/runner.py","line":125,"claim":"Source-report discovery walks lexical parents of the supplied runfile path, so an equivalent path containing an internal `..` records a null digest.","failing_input":"A valid arm supplied as `.search-eval/runs/G/../G/semantic.json` beside `.search-eval/report.json` for generation G is accepted as a runfile, but `_source_report_sha256` returns null and the later gate refuses it.","fix":"Resolve the validated runfile path before deriving its source data directory."},{"severity":"MINOR","file":"docs/architecture/search.md","line":1416,"claim":"The architecture note still describes the current report as schema version 2, contradicting the implementation and the following schema-3 paragraph.","failing_input":"An operator following the architecture note attempts to use or construct schema-2 evidence for the new gate.","fix":"State that the current report schema is 3 and describe schema 2 only as historical #389 output."}]}
```

What changed, per finding:

1. MAJOR, the leak scanner: fixed. `deltas_baseline` and `source_generation`
   join `_EXACT_ONLY` (operator-controlled labels and generation ids,
   checked for exact equality only, like `baseline` and `generation`). End
   to end regressions: a query `fixed` inside the baseline arm's label, and
   a query `2026` beside a source generation `R2026`. Both fail without the
   fix and pass with it.
2. MINOR, the schema version: fixed. The gate refuses a candidate report
   whose `schema_version` is not exactly 3, before any other check; tests
   cover 2, absent, `"3"` and 4.
3. MINOR, the runfile path: fixed. The runfile path is resolved before its
   source data directory is derived; test with `runs/G/../G/<file>`.
4. MINOR, `search.md`: fixed. Schema 3 is current, and 2 is historical
   #389 output that `gate` refuses.

## Adversarial Codex — implementation round 2

Verdict, verbatim:

PASS. All four Round-1 findings are fixed, and the focused offline suite passes: 190 passed, 1 skipped.

Against `06a76e0`, every semantic difference is accounted for:

- Gate now requires candidate `schema_version == 3`.
- Runfile paths are resolved before source-report discovery.
- `deltas_baseline` and `source_generation` are exact-only leak fields.
- The removed hybrid-offset test section is replaced by a synthetic `check_options` test.
- Added tests cover schema rejection, candidate-equals-baseline refusal, `..` paths, and both leak-scanner regressions. Other differences are comments or neutral naming.

`--generation` is operator-selectable, with a harness timestamp only as its default. Exact-only matching permits a substring such as query `2026` within `R2026`, but still rejects exact equality. This does not introduce a new weakening: generation, arm, and baseline labels already use the same policy, and the runfile baseline label already contains that source generation. Restoring substring matching would reinstate the demonstrated false-positive failure.

round1:[{"claim":"deltas_baseline and source_generation caused leak-scanner false positives","status":"FIXED"},{"claim":"gate accepted reports whose schema_version was not exactly 3","status":"FIXED"},{"claim":"runfile paths containing .. recorded a null source digest","status":"FIXED"},{"claim":"search.md incorrectly called schema 2 current","status":"FIXED"}]

```json
{"verdict":"PASS","findings":[]}
```

No change.

