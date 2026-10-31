"""CLI for the search-eval harness (#356). See the package docstring."""
from __future__ import annotations

import argparse
import asyncio
import datetime
import sys
from pathlib import Path

from scripts.search_eval import EXIT_FAILURE, EXIT_OK, EXIT_USAGE, UsageError


class _Parser(argparse.ArgumentParser):
    def error(self, message):  # argparse's own exit code is 2, which means INSUFFICIENT_DATA here
        self.print_usage(sys.stderr)
        print(f"{self.prog}: error: {message}", file=sys.stderr)
        sys.exit(EXIT_USAGE)


def _positive_float(value: str) -> float:
    try:
        v = float(value)
    except ValueError as e:
        raise argparse.ArgumentTypeError(f"not a number: {value!r}") from e
    if not v > 0:
        raise argparse.ArgumentTypeError("must be > 0")
    return v


def _cmd_mine(args) -> int:
    from scripts.search_eval.commands import write_mine

    write_mine(
        export=Path(args.export),
        out=Path(args.out),
        window_seconds=args.window_seconds,
        manual=Path(args.manual) if args.manual else None,
        user_id=args.user_id,
    )
    return EXIT_OK


def _cmd_snapshot(args) -> int:
    from scripts.search_eval.commands import write_snapshot

    meta = asyncio.run(write_snapshot(data=Path(args.data), database_url=args.database_url,
                                      force=args.force))
    print(f"search-eval: snapshot of {meta['rows']} indexed path(s) written, sha256 {meta['sha256']}")
    return EXIT_OK


def _default_generation() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def _cmd_run(args) -> int:
    from scripts.search_eval.arms import parse_arm_options
    from scripts.search_eval.runner import run_generation

    outcome = asyncio.run(run_generation(
        Path(args.data),
        args.arm,
        generation=args.generation or _default_generation(),
        depth=args.depth,
        limit_queries=args.limit_queries,
        database_url=args.database_url,
        fixture_corpus=Path(args.fixture_corpus) if args.fixture_corpus else None,
        arm_options=parse_arm_options(args.arm_opt),
    ))
    m = outcome.manifest
    if outcome.exit_code == EXIT_OK:
        print(f"search-eval: generation {m['generation']} complete ({len(m['arms'])} arm(s))")
    else:
        print(f"search-eval: generation {m['generation']} FAILED ({m.get('error')})",
              file=sys.stderr)
    return outcome.exit_code


def _cmd_score(args) -> int:
    from scripts.search_eval.scoring import score

    result = score(Path(args.data), args.arm or None, baseline=args.baseline,
                   min_queries=args.min_queries, seed=args.seed)
    sys.stdout.write(result.table)
    return result.exit_code


def _cmd_confidence(args) -> int:
    from scripts.search_eval.confidence import run_confidence

    result = run_confidence(Path(args.data), args.threshold)
    sys.stdout.write(result.table)
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="python -m scripts.search_eval",
                     description="Search evaluation harness (#356).")
    sub = parser.add_subparsers(dest="command", required=True, parser_class=_Parser)

    p = sub.add_parser("mine", help="mine judgments from a usage export")
    p.add_argument("--export", required=True, help="usage-export.jsonl")
    p.add_argument("--out", required=True, help="output directory (created 0700)")
    p.add_argument("--window-seconds", type=_positive_float, default=120.0)
    p.add_argument("--manual", default=None, help="operator-written judgments (JSONL)")
    p.add_argument("--user-id", type=int, default=None,
                   help="keep only this user's rows (counts as a user filter for provenance)")
    p.set_defaults(func=_cmd_mine)

    p = sub.add_parser("snapshot", help="record per-arm searchable membership (pre-candidate)")
    p.add_argument("--data", "--out", dest="data", required=True, help="the data directory")
    p.add_argument("--database-url", default=None,
                   help="the restored copy (default: $SEARCH_EVAL_DATABASE_URL)")
    p.add_argument("--force", action="store_true", help="replace an existing snapshot")
    p.set_defaults(func=_cmd_snapshot)

    p = sub.add_parser("run", help="replay arms over the selected queries as one generation")
    p.add_argument("--data", required=True)
    p.add_argument("--arm", action="extend", nargs="+", required=True)
    p.add_argument("--generation", default=None, help="generation id (default: a UTC stamp)")
    p.add_argument("--depth", type=int, default=50)
    p.add_argument("--limit-queries", type=int, default=None)
    p.add_argument("--database-url", default=None,
                   help="the restored copy (default: $SEARCH_EVAL_DATABASE_URL)")
    p.add_argument("--fixture-corpus", default=None,
                   help="a fixture corpus: the snapshot and the replay corpus together")
    p.add_argument("--arm-opt", action="append", default=[], metavar="KEY=VALUE")
    p.set_defaults(func=_cmd_run)

    p = sub.add_parser("score", help="score the published generation")
    p.add_argument("--data", required=True)
    p.add_argument("--arm", action="extend", nargs="+", default=[])
    p.add_argument("--baseline", default=None)
    p.add_argument("--min-queries", type=int, default=50)
    p.add_argument("--seed", type=int, default=0)
    p.set_defaults(func=_cmd_score)

    p = sub.add_parser("confidence",
                       help="tabulate SEARCH_LOW_CONFIDENCE_SIMILARITY calibration evidence (#389)")
    p.add_argument("--data", required=True, help="the data directory `mine` wrote")
    p.add_argument("--threshold", type=_positive_float, required=True,
                   help="the candidate threshold T, in (0, 1]")
    p.set_defaults(func=_cmd_confidence)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except UsageError as e:
        print(f"search-eval: usage error: {e}", file=sys.stderr)
        return EXIT_USAGE
    except Exception as e:  # noqa: BLE001 - the exit code is the contract
        print(f"search-eval: failed: {type(e).__name__}: {e}", file=sys.stderr)
        return EXIT_FAILURE


if __name__ == "__main__":
    sys.exit(main())
