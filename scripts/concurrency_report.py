"""Concurrency readiness report (#188, design D6): is the next mode safe?

    make concurrency-report TARGET=queue            # shadow -> queue
    make concurrency-report TARGET=enforce DAYS=10  # queue -> enforce
    python -m scripts.concurrency_report --target enforce [--days N] [--end ISO]

Runs the same `window_stats` + `evaluate` that back the `/admin/performance`
verdict, from the database alone (usage rows carrying
`params.concurrency.v = 2`, `concurrency_counters`, `concurrency_runs`), for
the **configured** epoch — the settings this container runs with — and the
target's source mode (shadow for `queue`, queue for `enforce`).

- `--days` defaults to the target's minimum window (3 for queue, 7 for
  enforce). The end defaults to the durable watermark rounded down to a whole
  minute; an explicit `--end` past it is INSUFFICIENT_DATA.
- Output: a table, then exactly one JSON line (the whole verdict) to post on
  #188.
- Exit code: 0 only when every criterion is PASS; 1 when any criterion FAILs;
  2 when the verdict is INSUFFICIENT_DATA; 64 on a usage error.
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as _dt
import json
import sys

EXIT = {"PASS": 0, "FAIL": 1, "INSUFFICIENT_DATA": 2}
EXIT_USAGE = 64


def _parse_end(value: str) -> _dt.datetime:
    try:
        at = _dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as e:
        raise argparse.ArgumentTypeError(f"not an ISO-8601 instant: {value!r}") from e
    return at if at.tzinfo else at.replace(tzinfo=_dt.timezone.utc)


def _days(value: str) -> float:
    try:
        days = float(value)
    except ValueError as e:
        raise argparse.ArgumentTypeError(f"not a number of days: {value!r}") from e
    if not 0 < days <= 35:
        # Counters and runs are pruned after 35 days.
        raise argparse.ArgumentTypeError("--days must be in (0, 35]")
    return days


def parse_args(argv):
    parser = argparse.ArgumentParser(
        prog="concurrency_report",
        description="Evaluate the MCP concurrency readiness criteria (#188).")
    parser.add_argument("--target", required=True, choices=("queue", "enforce"))
    parser.add_argument("--days", type=_days, default=None,
                        help="window length in days (default: the target's minimum)")
    parser.add_argument("--end", type=_parse_end, default=None,
                        help="window end, ISO-8601 (default: the durable watermark)")
    return parser.parse_args(argv)


def render(report: dict) -> str:
    """The human table. The JSON line is printed separately."""
    lines = []
    window = report.get("window") or {}
    lines.append(f"Target: {report['target']}  (evidence: {report['source_mode']} mode,"
                 f" epoch {report['epoch']})")
    if window:
        lines.append(f"Window: {window.get('start')} -> {window.get('end')}"
                     f"  ({window.get('days')} days; watermark {window.get('watermark')})")
        lines.append(f"Executed calls: {window.get('executed')}   requests:"
                     f" {(window.get('counters') or {}).get('requests', 0)}")
        if window.get("uncovered"):
            for lo, hi in window["uncovered"]:
                lines.append(f"Uncovered: {lo} -> {hi}")
        if window.get("foreign"):
            lines.append("Other configurations in window: " + ", ".join(
                f"{f['mode']}/{f['epoch']}" for f in window["foreign"])
                + f"; qualifying since {window.get('qualifying_since')}")
    lines.append("")
    width = max(len(c["id"]) for c in report["criteria"])
    lines.append(f"{'ID':<{width}}  {'VERDICT':<17}  REASON")
    for c in report["criteria"]:
        lines.append(f"{c['id']:<{width}}  {c['verdict']:<17}  {c['reason']}")
    lines.append("")
    lines.append(f"Overall: {report['overall']}")
    return "\n".join(lines)


async def run(args) -> dict:
    from src.database import async_session, engine
    from src.services.concurrency_readiness import readiness

    try:
        async with async_session() as session:
            return await readiness(session, args.target, days=args.days, end=args.end)
    finally:
        await engine.dispose()


def main(argv=None) -> int:
    try:
        args = parse_args(sys.argv[1:] if argv is None else argv)
    except SystemExit as e:
        return EXIT_USAGE if e.code else 0
    report = asyncio.run(run(args))
    print(render(report))
    print(json.dumps(report, sort_keys=True, default=str))
    return EXIT[report["overall"]]


if __name__ == "__main__":
    sys.exit(main())
