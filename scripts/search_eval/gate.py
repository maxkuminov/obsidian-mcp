"""`python -m scripts.search_eval gate`: PASS or FAIL per (metric ×
slice) from a scored `report.json`.

It reads the report's `deltas` for one candidate arm against the report's
`baseline` (the paired candidate-minus-baseline bootstrap CI95 that `score`
already computed) and prints one line per cell: the delta mean, the CI95 and
`PASS` or `FAIL`. A cell FAILs when its CI95 lower bound is below
`--min-ci-lower`, or when its CI95 is null (no delta could be computed). The
same table goes to `--out` as Markdown, ready for a PR body.

**The tenant boundary comes first.** The candidate's report, and the
baseline's (the same report when it holds both arms; `--baseline-report`,
required, when the baseline is a `runfile:` arm), must each be `attachable:
true` with `provenance.users == 1` and verdict `OK`. The report's `baseline`
must equal its `deltas_baseline` (schema 3) and occur exactly once in its
`arms`. A `runfile:` baseline's report must be that generation's report:
the same generation and qrels / snapshot / selection hashes, the source arm
present, and its file sha256 equal to the `source_report_sha256` the harness
recorded on the runfile arm when it staged it. Otherwise the gate exits 64
having printed no cell and no PASS/FAIL, and `--out` is neither created nor
replaced. Every check runs before any cell is computed.

Exit 0 when every cell passes, 1 when any fails, 64 on a usage error. It
reads only the reports and writes only `--out`.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path

from scripts.search_eval import EXIT_FAILURE, EXIT_OK, UsageError
from scripts.search_eval._io import atomic_write, sha256_file
from scripts.search_eval.scoring import METRICS, SCHEMA_VERSION, SLICES, VERDICT_OK

RUNFILE_PREFIX = "runfile:"


@dataclass
class Cell:
    metric: str
    slice: str
    n: int | None
    mean: float | None
    ci95: tuple[float, float] | None
    passed: bool


@dataclass
class GateResult:
    exit_code: int
    cells: list[Cell]
    table: str
    markdown: str


def _load(path: Path, what: str) -> dict:
    try:
        report = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError as e:
        raise UsageError(f"{what} {path} does not exist") from e
    except (OSError, ValueError) as e:
        raise UsageError(f"{what} {path} is not a readable JSON report") from e
    if not isinstance(report, dict):
        raise UsageError(f"{what} {path} is not a report object")
    return report


def _require_evidence(report: dict, what: str) -> None:
    """The tenant boundary and the verdict, before anything is computed."""
    if report.get("verdict") != VERDICT_OK:
        raise UsageError(f"{what} verdict is {report.get('verdict')!r}, not {VERDICT_OK!r}: "
                         "not evidence")
    provenance = report.get("provenance")
    users = provenance.get("users") if isinstance(provenance, dict) else None
    if report.get("attachable") is not True or users != 1:
        raise UsageError(
            f"{what} is not attachable single-user evidence (attachable="
            f"{report.get('attachable')!r}, provenance.users={users!r}); re-export with "
            "USER_ID= and re-score")


#: The hashes a runfile arm is bound to: `resolve_arm` refuses a runfile
#: whose qrels, snapshot or selection differ from the scoring run's, so the
#: staging generation's report must carry the same three.
_BOUND_HASHES = ("qrels_sha256", "snapshot_sha256", "selected_qids_hash")


def _require_source_of(source: dict, baseline: str, report: dict) -> None:
    """`--baseline-report` must be the report of the generation that staged
    the `runfile:<label>@<generation>` baseline: the same generation, the
    same qrels / snapshot / selection hashes as the candidate's report, and
    the source arm present in it."""
    body = baseline[len(RUNFILE_PREFIX):]
    label, sep, generation = body.rpartition("@")
    if not sep or not label or not generation:
        raise UsageError(f"the runfile baseline {baseline!r} names no source generation")
    if source.get("generation") != generation:
        raise UsageError(f"--baseline-report is generation {source.get('generation')!r}, "
                         f"not the runfile's source generation {generation!r}")
    for key in _BOUND_HASHES:
        if not source.get(key) or source.get(key) != report.get(key):
            raise UsageError(f"--baseline-report's {key} does not match the report's")
    source_arms = [a.get("arm") for a in source.get("arms") or [] if isinstance(a, dict)]
    if label not in source_arms:
        raise UsageError(f"--baseline-report has no arm {label!r}")


def _split(value: str, allowed: tuple[str, ...], what: str) -> list[str]:
    items = [v.strip() for v in value.split(",") if v.strip()]
    if not items:
        raise UsageError(f"--{what} is empty")
    unknown = [v for v in items if v not in allowed]
    if unknown:
        raise UsageError(f"unknown {what}: {', '.join(unknown)} (known: {', '.join(allowed)})")
    return list(dict.fromkeys(items))


def _fmt(x: float | None) -> str:
    return "null" if x is None else f"{x:+.4f}"


def run_gate(*, report_path: Path, arm: str, metrics: str, slices: str,
             min_ci_lower: float, out: Path,
             baseline_report_path: Path | None = None) -> GateResult:
    if not math.isfinite(min_ci_lower):
        raise UsageError("--min-ci-lower must be a finite number")
    metric_list = _split(metrics, METRICS, "metrics")
    slice_list = _split(slices, SLICES, "slices")

    report = _load(report_path, "report")
    # Exactly the schema this gate reads (Codex review): a schema-2 report
    # hand-carrying `deltas_baseline` is not schema-3 evidence.
    if report.get("schema_version") != SCHEMA_VERSION:
        raise UsageError(f"the report's schema_version is {report.get('schema_version')!r}, "
                         f"not {SCHEMA_VERSION}: re-score it")
    _require_evidence(report, "the report")
    baseline = report.get("baseline")
    if not baseline or not isinstance(baseline, str):
        raise UsageError("the report has no baseline arm; score it with --baseline")
    # Codex implementation round 2: the baseline label must be the arm the
    # deltas were computed against, and an arm of this report exactly once,
    # before any runfile source is examined.
    if report.get("deltas_baseline") != baseline:
        raise UsageError("the report's deltas_baseline does not equal its baseline "
                         "(a report with schema_version < 3 has none: re-score it)")
    entries = [a for a in report.get("arms") or [] if isinstance(a, dict)]
    labels = [a.get("arm") for a in entries]
    if labels.count(baseline) != 1:
        raise UsageError(f"the baseline {baseline!r} occurs {labels.count(baseline)} times "
                         "among the report's arms, not once")
    if labels.count(arm) != 1:
        raise UsageError(f"arm {arm!r} is not in the report exactly once "
                         f"(arms: {', '.join(map(str, labels))})")
    if arm == baseline:
        raise UsageError(f"arm {arm!r} is the report's baseline")
    if baseline.startswith(RUNFILE_PREFIX):
        # The baseline's evidence came from another generation: its own report
        # must clear the same boundary, and must BE that generation's report
        # (Codex implementation rounds 1 and 2: an unrelated safe report must
        # not vouch for a multi-user runfile). The harness recorded that
        # report's digest on the runfile arm when it staged it.
        if baseline_report_path is None:
            raise UsageError(f"the baseline {baseline!r} is a runfile arm; pass "
                             "--baseline-report with the report of the generation that staged it")
        source = _load(baseline_report_path, "baseline report")
        _require_evidence(source, "the baseline report")
        _require_source_of(source, baseline, report)
        base_entry = entries[labels.index(baseline)]
        recorded = base_entry.get("source_report_sha256")
        if not isinstance(recorded, str) or len(recorded) != 64:
            raise UsageError(f"the runfile arm {baseline!r} records no source report digest; "
                             "score the source generation, then re-stage the runfile")
        if base_entry.get("source_generation") != source.get("generation"):
            raise UsageError("the runfile arm's source_generation does not match "
                             "--baseline-report's generation")
        if sha256_file(Path(baseline_report_path)) != recorded:
            raise UsageError("--baseline-report's sha256 does not match the digest the "
                             "runfile arm recorded")
    elif baseline_report_path is not None:
        _require_evidence(_load(baseline_report_path, "baseline report"), "the baseline report")
    by_slice = (report.get("deltas") or {}).get(arm)
    if not isinstance(by_slice, dict):
        raise UsageError(f"the report has no deltas for {arm!r}")

    cells: list[Cell] = []
    for metric in metric_list:
        for sl in slice_list:
            entry = (by_slice.get(sl) or {}).get(metric) or {}
            ci = entry.get("ci95")
            ci_t = (float(ci[0]), float(ci[1])) if isinstance(ci, list) and len(ci) == 2 else None
            passed = ci_t is not None and ci_t[0] >= min_ci_lower
            cells.append(Cell(metric, sl, entry.get("n"), entry.get("mean"), ci_t, passed))

    fails = sum(1 for c in cells if not c.passed)
    head = (f"gate: {arm} vs baseline {baseline}; FAIL when the CI95 lower bound of the "
            f"paired delta < {min_ci_lower:g}")
    lines = [head, f"  {'metric':<10} {'slice':<18} {'n':>5} {'mean':>8}  {'ci95':<19} result"]
    md = [f"**Search-eval gate** — `{arm}` vs baseline `{baseline}`; FAIL when the CI95 "
          f"lower bound of the paired delta < {min_ci_lower:g}.", "",
          "| metric | slice | n | mean Δ | CI95 | result |", "| --- | --- | --- | --- | --- | --- |"]
    for c in cells:
        ci_s = "null" if c.ci95 is None else f"[{_fmt(c.ci95[0])}, {_fmt(c.ci95[1])}]"
        res = "PASS" if c.passed else "FAIL"
        n_s = "-" if c.n is None else str(c.n)
        lines.append(f"  {c.metric:<10} {c.slice:<18} {n_s:>5} {_fmt(c.mean):>8}  {ci_s:<19} {res}")
        md.append(f"| {c.metric} | {c.slice} | {n_s} | {_fmt(c.mean)} | {ci_s} | {res} |")
    verdict = "PASS" if fails == 0 else f"FAIL ({fails} of {len(cells)} cells)"
    lines.append(f"gate: {verdict}")
    md += ["", f"**{verdict}**"]
    table = "\n".join(lines) + "\n"
    markdown = "\n".join(md) + "\n"
    atomic_write(Path(out), markdown)
    return GateResult(EXIT_OK if fails == 0 else EXIT_FAILURE, cells, table, markdown)
