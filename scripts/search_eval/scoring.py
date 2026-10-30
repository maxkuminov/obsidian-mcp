"""Drift drop, selection and scoring (#356, design D4, D6, D8, D10).

**The drift drop** is computed from the pre-candidate snapshot only. A judged
path absent from the snapshot (keyed by owner, NULL-safe) is vault drift: it
leaves the qrels (`dropped_paths`), and a query left with no judged path is
dropped (`dropped_queries`). The replay corpus never shrinks the qrels: a path
the snapshot has but a candidate's replay database lost stays relevant, counts
as a miss, and is that arm's `corpus_loss`.

**Selection** is the drift-surviving qids in sorted order, the first N under
`--limit-queries`. `selected_qids_hash` is the sha256 of those qids joined by
newlines; `run` records it and `score` recomputes it.

**`score` reads run data only through `runs/manifest.json`** and never falls
back to an earlier generation. It refuses (exit 64) unless the manifest is
`complete`, its selected-qid hash equals the selection made now under its
recorded selection options, the qrels and snapshot hashes match, every
referenced file has its recorded sha256, and the requested arms are covered.
A `runfile:` arm's missing queries are the one tolerated omission, scored as
empty and reported as `missing_queries`.

**The report** (`report.json`) holds metrics, counts, arm labels, options and
memberships, hashes, the generation, `provenance`, `attachable` and
`commit_sha`, and never query text or a note path; `assert_no_leak` checks
that on the serialized report before it is written. Only a report whose
provenance is single-user is `attachable` (D10); a non-attachable report's
table begins with `NOT ATTACHABLE (multi-user provenance)`.

**The stable projection.** `stable_projection(report)` returns the report with
every key in `UNSTABLE_KEYS` removed at any depth. `commit_sha` changes with
every commit, `generated_at` with every run and `generation` with every
invocation; everything else is a function of the inputs and the seed, so the
committed golden file (`tests/fixtures/search_eval/expected-report.json`) is a
stable projection.

Exit codes: 0 `OK`; 2 `CORPUS_LOSS` (any arm with loss) or
`INSUFFICIENT_DATA` (fewer than `min_queries` selected queries); 64 a usage
error or an incomplete or mismatched run. No metric threshold (E2).
"""
from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from scripts.search_eval import EXIT_INSUFFICIENT, EXIT_OK, UsageError
from scripts.search_eval._io import (
    atomic_write,
    pretty_json,
    read_jsonl,
    sha256_bytes,
    sha256_file,
)
from scripts.search_eval.classify import CLASSES
from scripts.search_eval.corpus import MEMBERSHIPS, Corpus
from scripts.search_eval.metrics import (
    bootstrap_ci,
    mean,
    ndcg_at,
    novel_at,
    recall_at,
    reciprocal_rank,
)

QUERIES_FILE = "queries.jsonl"
QRELS_FILE = "qrels.json"
MINE_REPORT_FILE = "mine-report.json"
SNAPSHOT_FILE = "corpus-snapshot.json"
SNAPSHOT_META_FILE = "snapshot-meta.json"
RUNS_DIR = "runs"
MANIFEST_FILE = "manifest.json"
REPORT_FILE = "report.json"

UNSTABLE_KEYS = frozenset({"commit_sha", "generated_at", "generation"})

VERDICT_OK = "OK"
VERDICT_INSUFFICIENT = "INSUFFICIENT_DATA"
VERDICT_LOSS = "CORPUS_LOSS"

METRICS = ("recall@5", "recall@10", "mrr", "ndcg@10")
SLICES = ("overall", *(f"class:{c}" for c in CLASSES), "source:mined", "source:manual")
NOT_ATTACHABLE_LINE = "NOT ATTACHABLE (multi-user provenance)"

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


# --------------------------------------------------------------------------
# Inputs.
# --------------------------------------------------------------------------


@dataclass
class DataDir:
    root: Path
    queries: dict[str, dict]
    qrels: dict[str, dict[str, int]]
    snapshot: Corpus
    qrels_sha256: str
    snapshot_sha256: str
    mine_report: dict

    @classmethod
    def load(cls, root: Path) -> "DataDir":
        root = Path(root)
        for name in (QUERIES_FILE, QRELS_FILE, MINE_REPORT_FILE):
            if not (root / name).is_file():
                raise UsageError(f"{root / name} is missing; run `mine` first")
        snap = root / SNAPSHOT_FILE
        if not snap.is_file():
            raise UsageError(
                f"{snap} is missing; run `make search-eval-snapshot` on the restored "
                "copy before applying any candidate"
            )
        queries = {q["qid"]: q for q in read_jsonl(root / QUERIES_FILE)}
        qrels_bytes = (root / QRELS_FILE).read_bytes()
        snap_bytes = snap.read_bytes()
        return cls(
            root=root,
            queries=queries,
            qrels=json.loads(qrels_bytes),
            snapshot=Corpus(json.loads(snap_bytes)),
            qrels_sha256=sha256_bytes(qrels_bytes),
            snapshot_sha256=sha256_bytes(snap_bytes),
            mine_report=json.loads((root / MINE_REPORT_FILE).read_text(encoding="utf-8")),
        )


@dataclass
class Drift:
    judged: dict[str, set[str]]           # qid -> judged paths present in the snapshot
    dropped_paths: int = 0
    dropped_queries: int = 0
    ownerless_queries: int = 0


def drift_drop(queries: dict[str, dict], qrels: dict[str, dict], snapshot: Corpus) -> Drift:
    """Remove judged paths absent from the snapshot, and queries left empty (D6)."""
    judged: dict[str, set[str]] = {}
    dropped_paths = dropped_queries = 0
    for qid in sorted(qrels):
        q = queries.get(qid)
        if q is None:
            raise UsageError("qrels.json and queries.jsonl disagree; re-run `mine`")
        keep = {p for p in qrels[qid] if snapshot.has(q["user_id"], p)}
        dropped_paths += len(qrels[qid]) - len(keep)
        if keep:
            judged[qid] = keep
        else:
            dropped_queries += 1
    ownerless = sum(1 for qid in judged if queries[qid]["user_id"] is None)
    return Drift(judged, dropped_paths, dropped_queries, ownerless)


def select_qids(drift: Drift, limit_queries: int | None) -> list[str]:
    qids = sorted(drift.judged)
    return qids[:limit_queries] if limit_queries is not None else qids


def selected_hash(qids: list[str]) -> str:
    return sha256_bytes("\n".join(sorted(qids)).encode("utf-8"))


def read_manifest(root: Path) -> dict | None:
    path = Path(root) / RUNS_DIR / MANIFEST_FILE
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError as e:
        raise UsageError("runs/manifest.json is unreadable; re-run `run`") from e


# --------------------------------------------------------------------------
# Scoring.
# --------------------------------------------------------------------------


def _slice_names(q: dict) -> list[str]:
    names = ["overall", f"class:{q['class']}"]
    names += [f"source:{s}" for s in q["sources"]]
    return names


def _per_query(relevant: set[str], pool: set[str], run: list[str]) -> dict[str, float]:
    return {
        "recall@5": recall_at(relevant, run, 5),
        "recall@10": recall_at(relevant, run, 10),
        "mrr": reciprocal_rank(relevant, run),
        "ndcg@10": ndcg_at(relevant, run, 10),
        "novel@10": novel_at(pool, run, 10),
    }


def _summarise(values: list[dict[str, float]]) -> dict:
    out: dict[str, Any] = {"n_queries": len(values)}
    for m in (*METRICS, "novel@10"):
        out[m] = mean([v[m] for v in values])
    return out


def commit_sha() -> str:
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, capture_output=True,
                             text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    sha = out.stdout.strip()
    return sha if out.returncode == 0 and re.fullmatch(r"[0-9a-f]{40}", sha) else "unknown"


def stable_projection(report: Any) -> Any:
    """The report minus `UNSTABLE_KEYS`, at any depth (see the module docstring)."""
    if isinstance(report, dict):
        return {k: stable_projection(v) for k, v in report.items() if k not in UNSTABLE_KEYS}
    if isinstance(report, list):
        return [stable_projection(v) for v in report]
    return report


# Constant strings the report is built from. They come from this code, not from
# the data, so they cannot leak anything even if a query happens to equal one.
_VOCABULARY = frozenset({
    *METRICS, "novel@10", *SLICES, *MEMBERSHIPS, *CLASSES, "mined", "manual",
    VERDICT_OK, VERDICT_INSUFFICIENT, VERDICT_LOSS, "multi", "unknown",
    "empty", "user_filter", "inconsistent_user_filter",
    "single_user_deployment", "multi_user", "manual_other_owner",
    "keyword", "semantic", "fixture-lexical", "fixture-title",
})
_HEX = re.compile(r"[0-9a-f]{16,64}")


#: Dict keys under these report keys are data-derived (arm labels, option
#: names); every other key is a fixed field name and is not checked.
_DATA_KEYED = frozenset({"deltas", "options"})

#: Values under these keys are produced by this code or supplied by the
#: operator (the generation id, which `run` validates; arm labels; option
#: keys and values). They are ordinary words and stamps, so a substring test
#: against query text would refuse real data sets (a query `2026` inside a
#: UTC-stamp generation, a query `hybrid` inside an arm label); they are
#: checked for exact equality only.
_EXACT_ONLY = frozenset({"generation", "arm", "baseline"})


def _strings(value: Any, parent: str | None = None, exact: bool = False):
    """Yield `(string, exact_only)` for every string the report carries."""
    if isinstance(value, dict):
        for k, v in value.items():
            if parent in _DATA_KEYED:
                yield k, True
            yield from _strings(v, k, exact or parent == "options" or k in _EXACT_ONLY)
    elif isinstance(value, list):
        for v in value:
            yield from _strings(v, parent, exact)
    elif isinstance(value, str):
        yield value, exact


class ReportLeak(RuntimeError):
    """The report would carry query text or a note path (exit 1)."""


def assert_no_leak(serialized: str, texts: set[str], paths: set[str]) -> None:
    """No string in the report equals, or contains, a query text or a note path.

    Fixed vocabulary and hex digests are skipped: they are produced by this
    code. The generation, arm labels and option keys and values must not
    equal any text or path. Every other string must not equal any text or
    path, and must not contain any path or any text of four or more
    characters.
    """
    report = json.loads(serialized)
    long_texts = {t for t in texts if len(t) >= 4}
    for s, exact_only in _strings(report):
        if s in _VOCABULARY or _HEX.fullmatch(s):
            continue
        if s in texts or s in paths:
            raise ReportLeak("the report would contain a query text or a note path")
        if exact_only:
            continue
        if any(p in s for p in paths) or any(t in s for t in long_texts):
            raise ReportLeak("the report would contain a query text or a note path")


@dataclass
class ScoreResult:
    exit_code: int
    report: dict
    table: str
    lines: list[str] = field(default_factory=list)


def _fmt(v: float | None) -> str:
    return "   -  " if v is None else f"{v:6.3f}"


def render_table(report: dict) -> str:
    lines: list[str] = []
    if not report["attachable"]:
        lines.append(NOT_ATTACHABLE_LINE)
    c = report["counts"]
    lines.append(
        f"search-eval: verdict {report['verdict']}, {c['n_queries']} queries "
        f"(identifier {c['per_class']['identifier']}, prose {c['per_class']['prose']}; "
        f"mined {c['per_source']['mined']}, manual {c['per_source']['manual']}, "
        f"both {c['n_both_sources']}); generation {report['generation']}"
    )
    lines.append(
        f"drift: {c['dropped_paths']} judged path(s) and {c['dropped_queries']} query(ies) "
        f"absent from the snapshot; {c['ownerless_queries']} ownerless"
    )
    header = f"{'arm':<28} {'slice':<16} {'n':>4} {'R@5':>6} {'R@10':>6} {'MRR':>6} {'nDCG@10':>7} {'novel@10':>8}"
    lines.append(header)
    for arm in report["arms"]:
        for sl in SLICES:
            m = arm["metrics"][sl]
            lines.append(
                f"{arm['arm'][:28]:<28} {sl:<16} {m['n_queries']:>4} {_fmt(m['recall@5'])} "
                f"{_fmt(m['recall@10'])} {_fmt(m['mrr'])} {_fmt(m['ndcg@10']):>7} "
                f"{_fmt(m['novel@10']):>8}"
            )
        lines.append(
            f"{'':<28} membership {arm['membership']}, corpus_loss {arm['corpus_loss']}, "
            f"unsearchable_before {arm['unsearchable_before']}, missing {arm['missing_queries']}"
        )
    if report["baseline"] is not None:
        lines.append(f"deltas vs baseline {report['baseline']} (overall; 95% bootstrap CI, "
                     f"seed {report['seed']}, reported only):")
        for label, by_slice in report["deltas"].items():
            for metric in METRICS:
                d = by_slice["overall"][metric]
                ci = d["ci95"]
                ci_s = "-" if ci is None else f"[{ci[0]:+.3f}, {ci[1]:+.3f}]"
                mean_s = "-" if d["mean"] is None else f"{d['mean']:+.3f}"
                lines.append(f"  {label[:28]:<28} {metric:<10} {mean_s:>7} {ci_s}")
    return "\n".join(lines) + "\n"


def score(data_dir: Path, arms: list[str] | None = None, *, baseline: str | None = None,
          min_queries: int = 50, seed: int = 0, write: bool = True) -> ScoreResult:
    data_dir = Path(data_dir)
    data = DataDir.load(data_dir)
    manifest = read_manifest(data_dir)
    if manifest is None:
        raise UsageError("no runs/manifest.json; run `run` first")
    gen = manifest.get("generation")
    status = manifest.get("status")
    if status != "complete":
        detail = f" ({manifest.get('error')})" if status == "failed" else ""
        raise UsageError(f"generation {gen} is {status!r}{detail}; re-run `run` "
                         "(score never falls back to an earlier generation)")
    selection = manifest.get("selection") or {}
    drift = drift_drop(data.queries, data.qrels, data.snapshot)
    selected = select_qids(drift, selection.get("limit_queries"))
    if manifest.get("selected_qids_hash") != selected_hash(selected):
        raise UsageError(f"generation {gen} was run on a different query selection")
    if manifest.get("qrels_sha256") != data.qrels_sha256:
        raise UsageError(f"generation {gen} was run on different qrels")
    if manifest.get("snapshot_sha256") != data.snapshot_sha256:
        raise UsageError(f"generation {gen} was run on a different snapshot")
    manifest_arms = list(manifest.get("arms") or [])
    arms = list(arms) if arms else manifest_arms
    missing_arms = [a for a in arms if a not in manifest_arms]
    if missing_arms:
        raise UsageError(f"generation {gen} did not run arm(s) {missing_arms}")
    if baseline is not None and baseline not in arms:
        raise UsageError(f"baseline {baseline!r} is not among the scored arms")
    files = manifest.get("files") or {}
    runs_dir = data_dir / RUNS_DIR

    # Every arm the complete manifest references is validated, not only the
    # requested subset: a generation with a missing or altered file is not
    # complete, whichever arms are scored from it.
    if set(files) != set(manifest_arms):
        raise UsageError(f"generation {gen} does not list exactly one file per arm")
    staged: dict[str, dict] = {}
    for spec in manifest_arms:
        entry = files.get(spec)
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            raise UsageError(f"generation {gen} lists no file for arm {spec!r}")
        path = runs_dir / entry["path"]
        if not path.is_file() or sha256_file(path) != entry.get("sha256"):
            raise UsageError(f"the run file for arm {spec!r} is missing or changed since {gen} completed")
        content = json.loads(path.read_text(encoding="utf-8"))
        meta = content.get("meta") or {}
        for key, want in (("generation", gen), ("selected_qids_hash", manifest["selected_qids_hash"]),
                          ("qrels_sha256", data.qrels_sha256),
                          ("snapshot_sha256", data.snapshot_sha256)):
            if meta.get(key) != want:
                raise UsageError(f"the run file for arm {spec!r} disagrees with the manifest on {key}")
        staged[spec] = content

    # Per-arm, per-query metrics.
    per_arm: dict[str, dict[str, dict[str, float]]] = {}
    arm_reports = []
    any_loss = False
    for spec in arms:
        content = staged[spec]
        meta, runs = content["meta"], content["runs"]
        tolerated = set(meta.get("missing_queries") or [])
        values: dict[str, dict[str, float]] = {}
        for qid in selected:
            run = runs.get(qid)
            if run is None:
                if meta.get("source") == "runfile" and qid in tolerated:
                    run = []
                else:
                    raise UsageError(f"the run file for arm {spec!r} has no entry for a selected query")
            q = data.queries[qid]
            values[qid] = _per_query(drift.judged[qid], set(q["pool"]), run)
        per_arm[spec] = values
        loss = sum(len(v) for k, v in (meta.get("corpus_loss") or {}).items() if k in selected)
        before = sum(len(v) for k, v in (meta.get("unsearchable_before") or {}).items() if k in selected)
        any_loss = any_loss or loss > 0
        metrics = {}
        for sl in SLICES:
            members = [values[qid] for qid in selected if sl in _slice_names(data.queries[qid])]
            metrics[sl] = _summarise(members)
        arm_reports.append({
            "arm": meta.get("label", spec),
            "membership": meta.get("membership"),
            "options": meta.get("options") or {},
            "corpus_loss": loss,
            "unsearchable_before": before,
            "missing_queries": len([q for q in tolerated if q in selected]),
            "metrics": metrics,
            # #355 (D8): semantic arms only; fixture arms gain no key.
            **{k: meta.get(k) for k in ("retrieval_fingerprint", "legacy_chunked_notes")
               if k in meta},
        })

    label_of = {spec: rep["arm"] for spec, rep in zip(arms, arm_reports)}
    deltas: dict[str, dict] = {}
    if baseline is not None:
        base = per_arm[baseline]
        for spec in arms:
            if spec == baseline:
                continue
            by_slice = {}
            for sl in SLICES:
                qids = [qid for qid in selected if sl in _slice_names(data.queries[qid])]
                by_metric = {}
                for metric in METRICS:
                    ds = [per_arm[spec][qid][metric] - base[qid][metric] for qid in qids]
                    ci = bootstrap_ci(ds, seed=seed)
                    by_metric[metric] = {
                        "n": len(ds),
                        "mean": mean(ds),
                        "ci95": None if ci is None else [ci[0], ci[1]],
                        "deltas": sorted(ds),
                    }
                by_slice[sl] = by_metric
            deltas[label_of[spec]] = by_slice

    n_queries = len(selected)
    if any_loss:
        verdict = VERDICT_LOSS
    elif n_queries < min_queries:
        verdict = VERDICT_INSUFFICIENT
    else:
        verdict = VERDICT_OK

    selected_queries = [data.queries[qid] for qid in selected]
    stale = sum(
        1 for qid in selected for p in drift.judged[qid]
        if (data.snapshot.entry(data.queries[qid]["user_id"], p) or {}).get("stale_vector")
    )
    mr = data.mine_report
    provenance = mr.get("provenance") or {"users": "multi", "basis": "empty"}
    report = {
        "schema_version": 1,
        "verdict": verdict,
        "generation": gen,
        "commit_sha": commit_sha(),
        "qrels_sha256": data.qrels_sha256,
        "snapshot_sha256": data.snapshot_sha256,
        "selected_qids_hash": manifest["selected_qids_hash"],
        "window_seconds": mr.get("window_seconds"),
        "seed": seed,
        "min_queries": min_queries,
        "depth": manifest.get("depth"),
        "selection": {"limit_queries": selection.get("limit_queries")},
        "options": manifest.get("options") or {},
        "provenance": provenance,
        "attachable": provenance.get("users") == 1,
        "counts": {
            "n_queries": n_queries,
            "n_both_sources": sum(1 for q in selected_queries if set(q["sources"]) == {"mined", "manual"}),
            "per_class": {c: sum(1 for q in selected_queries if q["class"] == c) for c in CLASSES},
            "per_source": {s: sum(1 for q in selected_queries if s in q["sources"])
                           for s in ("mined", "manual")},
            "judged_paths": sum(len(drift.judged[qid]) for qid in selected),
            "dropped_paths": drift.dropped_paths,
            "dropped_queries": drift.dropped_queries,
            "ownerless_queries": sum(1 for q in selected_queries if q["user_id"] is None),
            "stale_vector_judged": stale,
        },
        "mine": {k: mr.get(k) for k in (
            "rows_total", "malformed_lines", "usable_searches", "usable_reads", "excluded",
            "credited_reads", "judgments", "unattributed_reads", "queries_without_judgment",
            "manual_lines", "manual_excluded_other_owner",
        )},
        "arms": arm_reports,
        "baseline": label_of[baseline] if baseline is not None else None,
        "deltas": deltas,
    }

    serialized = pretty_json(report)
    texts = {q["text"] for q in data.queries.values()}
    paths: set[str] = set()
    for qid, q in data.queries.items():
        paths.update(q.get("pool") or [])
        paths.update(data.qrels.get(qid, {}))
    for owner in data.snapshot.data.values():
        paths.update(owner)
    assert_no_leak(serialized, texts, paths)
    if write:
        atomic_write(data_dir / REPORT_FILE, serialized)
    table = render_table(report)
    code = EXIT_OK if verdict == VERDICT_OK else EXIT_INSUFFICIENT
    return ScoreResult(exit_code=code, report=report, table=table)


__all__ = [
    "UNSTABLE_KEYS", "DataDir", "Drift", "drift_drop", "select_qids", "selected_hash",
    "score", "stable_projection", "render_table", "assert_no_leak",
]
