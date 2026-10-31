## Context

This change was built and reviewed inside the `hybrid-any-fill` change
(#403), which ended in the owner declining the fusion offset after the
first real-data gate run. That branch stays as history. This change extracts
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
