"""`confidence`: tabulate the calibration evidence for
`SEARCH_LOW_CONFIDENCE_SIMILARITY` per tool (#389, design D2, D7).

Reads `confidence-rows.jsonl` and `mine-report.json` from the data directory
`mine` wrote; connects to no database. For a candidate threshold `T` it asks,
per tool and per query class, how many post-change pages would read
`confidence: low`, and — the acceptance number — how many **clicked-through**
pages (a shown note was read, so the page was not noise) would.

- A row is **post-change** exactly when it carries the recorded `confidence`
  key. A row without it predates the token: counted as excluded, never
  tabulated, never part of a span.
- A post-change row whose `top_similarity` is null (a keyword-only
  `hybrid_search` page, or an empty candidate set) is included: the shared
  rule gives `low` at every `T > 0` when its `keyword_all` is 0, so a
  clicked-through keyword-only `low` counts as a false low.
- One acceptance line per tool, never an aggregate: `INSUFFICIENT_DATA` when
  that tool's post-change rows span fewer than 14 days or it has fewer than
  200 clicked-through pages (zero included); otherwise `PASS` exactly when
  the false-low rate is below 1/50. One setting enables both tools, so a
  deployment sets it only when both lines read PASS (L15).

The output (the printed table and `confidence-report.json`, counts and rates
only) inherits the mine run's `provenance` and `attachable`; a non-attachable
table begins with the `NOT ATTACHABLE` line. Only attachable output may leave
the private data directory.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from scripts.search_eval import UsageError
from scripts.search_eval._io import atomic_write, pretty_json, read_jsonl, sha256_bytes
from scripts.search_eval.classify import CLASSES
from scripts.search_eval.metrics import _percentile

CONFIDENCE_ROWS_FILE = "confidence-rows.jsonl"
CONFIDENCE_REPORT_FILE = "confidence-report.json"
MINE_REPORT_FILE = "mine-report.json"
QUERIES_FILE = "queries.jsonl"
QRELS_FILE = "qrels.json"

TOOLS = ("semantic_search", "hybrid_search")
MIN_SPAN_DAYS = 14
MIN_CLICKED = 200
#: The acceptance line: a false-low rate strictly below 1 in 50.
MAX_FALSE_LOW = 1 / 50
PASS, FAIL, INSUFFICIENT = "PASS", "FAIL", "INSUFFICIENT_DATA"
NOT_ATTACHABLE_LINE = "NOT ATTACHABLE (multi-user provenance)"
_US_PER_DAY = 86_400 * 1_000_000
VOCABULARY = frozenset({*TOOLS, PASS, FAIL, INSUFFICIENT})


def would_be_low(row: dict, threshold: float) -> bool:
    """The shared rule (`src/services/search_confidence.py`, D1), restated
    without importing `src` so the harness stays database- and app-free:
    `low` iff `T > 0`, `keyword_all == 0` (always 0 on `semantic_search`)
    and `top_similarity` is null or strictly below `T`."""
    if not threshold > 0:
        return False
    keyword_all = 0 if row.get("tool") == "semantic_search" else (row.get("keyword_all") or 0)
    if keyword_all:
        return False
    top = row.get("top_similarity")
    return top is None or top < threshold


def _deciles(values: list[float]) -> list[float] | None:
    if not values:
        return None
    ordered = sorted(values)
    return [_percentile(ordered, q / 10) for q in range(1, 10)]


def _span_days(rows: list[dict]) -> float:
    stamps = [r["created_at"] for r in rows if isinstance(r.get("created_at"), int)]
    if len(stamps) < 2:
        return 0.0
    return (max(stamps) - min(stamps)) / _US_PER_DAY


def _rate(n: int, m: int) -> float | None:
    return n / m if m else None


def _block(rows: list[dict], threshold: float) -> dict:
    clicked = [r for r in rows if r.get("clicked") is True]
    lows = sum(1 for r in rows if would_be_low(r, threshold))
    false_lows = sum(1 for r in clicked if would_be_low(r, threshold))
    return {
        "rows": len(rows),
        "span_days": round(_span_days(rows), 3),
        "clicked": len(clicked),
        "would_be_low": lows,
        "share_low": _rate(lows, len(rows)),
        "false_low": false_lows,
        "false_low_rate": _rate(false_lows, len(clicked)),
        "null_top_similarity": sum(1 for r in rows if r.get("top_similarity") is None),
        "deciles_clicked": _deciles([r["top_similarity"] for r in clicked
                                     if r.get("top_similarity") is not None]),
        "deciles_not_clicked": _deciles([r["top_similarity"] for r in rows
                                         if r.get("clicked") is not True
                                         and r.get("top_similarity") is not None]),
    }


def verdict(block: dict, span_days: float) -> str:
    """`span_days` is the **unrounded** span (Codex impl r1 m1): 13.9996
    days is short of 14, whatever the rounded display says."""
    if span_days < MIN_SPAN_DAYS or block["clicked"] < MIN_CLICKED:
        return INSUFFICIENT
    return PASS if block["false_low_rate"] < MAX_FALSE_LOW else FAIL


def tabulate(rows: list[dict], threshold: float, mine_report: dict) -> dict:
    """The counts-and-rates report. Pure."""
    provenance = mine_report.get("provenance") or {"users": "multi", "basis": "empty"}
    tools: dict[str, dict] = {}
    for tool in TOOLS:
        mine = [r for r in rows if r.get("tool") == tool]
        post = [r for r in mine if r.get("confidence") in ("ok", "low")]
        block = _block(post, threshold)
        block["excluded_pre_change"] = len(mine) - len(post)
        block["verdict"] = verdict(block, _span_days(post))
        block["per_class"] = {c: _block([r for r in post if r.get("class") == c], threshold)
                              for c in CLASSES}
        tools[tool] = block
    return {
        "threshold": threshold,
        "acceptance": {"max_false_low_rate": MAX_FALSE_LOW, "min_span_days": MIN_SPAN_DAYS,
                       "min_clicked": MIN_CLICKED},
        "provenance": provenance,
        "attachable": provenance.get("users") == 1,
        "tools": tools,
    }


def _days(value: float) -> str:
    return format(round(value, 1), "g")


def acceptance_line(tool: str, block: dict) -> str:
    rate = block["false_low_rate"]
    rate_s = "n/a" if rate is None else f"{rate:.4f}"
    return (f"{tool}: false-low {rate_s} ({block['false_low']}/{block['clicked']} "
            f"clicked-through pages, {_days(block['span_days'])} days) — "
            f"{block['verdict']} at < 1/50")


def render(report: dict) -> str:
    lines: list[str] = []
    if not report["attachable"]:
        lines.append(NOT_ATTACHABLE_LINE)
    lines.append(f"search-eval confidence: threshold {report['threshold']:g}; "
                 "one line per tool, both must PASS before the setting is raised")
    for tool in TOOLS:
        lines.append(acceptance_line(tool, report["tools"][tool]))
    lines.append(f"{'tool':<16} {'class':<11} {'rows':>6} {'days':>6} {'clicked':>7} "
                 f"{'low@T':>6} {'false-low':>9} {'excluded':>8}")
    for tool in TOOLS:
        block = report["tools"][tool]
        for name, sub in (("all", block), *block["per_class"].items()):
            share = "-" if sub["share_low"] is None else f"{sub['share_low']:.3f}"
            fl = "-" if sub["false_low_rate"] is None else f"{sub['false_low_rate']:.4f}"
            excl = block["excluded_pre_change"] if name == "all" else ""
            lines.append(f"{tool:<16} {name:<11} {sub['rows']:>6} {_days(sub['span_days']):>6} "
                         f"{sub['clicked']:>7} {share:>6} {fl:>9} {excl!s:>8}")
        for name in ("deciles_not_clicked", "deciles_clicked"):
            d = block[name]
            shown = "-" if d is None else " ".join(f"{v:.3f}" for v in d)
            lines.append(f"  {tool} top_similarity {name[len('deciles_'):]}: {shown}")
    return "\n".join(lines) + "\n"


@dataclass
class ConfidenceResult:
    report: dict
    table: str


def run_confidence(data_dir: Path, threshold: float, *, write: bool = True) -> ConfidenceResult:
    """Read the mine output, tabulate, check for leaks, write the JSON."""
    from scripts.search_eval.scoring import assert_no_leak

    data_dir = Path(data_dir)
    if not (0 < threshold <= 1):
        raise UsageError("--threshold must be in (0, 1]")
    rows_path = data_dir / CONFIDENCE_ROWS_FILE
    report_path = data_dir / MINE_REPORT_FILE
    for path in (rows_path, report_path):
        if not path.is_file():
            raise UsageError(f"{path} is missing; run `mine` first")
    mine_report = json.loads(report_path.read_text(encoding="utf-8"))
    raw = rows_path.read_bytes()
    want = mine_report.get("confidence_rows_sha256")
    # The rows must be the ones this report's `mine` run wrote (Codex impl
    # r1 B1): otherwise its provenance says nothing about them.
    if not isinstance(want, str) or sha256_bytes(raw) != want:
        raise UsageError(f"{rows_path} does not match {report_path} (missing or different "
                         "confidence_rows_sha256); re-run `mine`")
    rows = read_jsonl(rows_path)
    if mine_report.get("confidence_rows") != len(rows):
        raise UsageError(f"{rows_path} row count does not match {report_path}; re-run `mine`")
    report = tabulate(rows, threshold, mine_report)
    serialized = pretty_json(report)
    texts: set[str] = set()
    paths: set[str] = set()
    if (data_dir / QUERIES_FILE).is_file():
        for q in read_jsonl(data_dir / QUERIES_FILE):
            texts.add(q.get("text") or "")
            paths.update(q.get("pool") or [])
    if (data_dir / QRELS_FILE).is_file():
        for judged in json.loads((data_dir / QRELS_FILE).read_text(encoding="utf-8")).values():
            paths.update(judged)
    texts.discard("")
    assert_no_leak(serialized, texts, paths, vocabulary=VOCABULARY)
    if write:
        atomic_write(data_dir / CONFIDENCE_REPORT_FILE, serialized)
    return ConfidenceResult(report=report, table=render(report))
