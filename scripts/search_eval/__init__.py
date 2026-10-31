"""The search-eval harness (#356): mine judgments, replay arms, score them.

    python -m scripts.search_eval mine     --export FILE --out DIR [--manual FILE]
    python -m scripts.search_eval snapshot --data DIR [--database-url URL]
    python -m scripts.search_eval run      --data DIR --arm NAME... [--generation ID]
    python -m scripts.search_eval score    --data DIR [--arm NAME...] [--baseline ARM]
    python -m scripts.search_eval confidence --data DIR --threshold T   (#389)

See `docs/architecture/search.md`, "Search evaluation harness (#356)".
Nothing in this package imports `src.*` at module scope: the built-in database
arms import the search services lazily, inside their factories, so mining,
scoring and the fixture pipeline run with no database settings at all.

Exit codes follow `scripts/concurrency_report.py`: 0 done, 2 insufficient data
or corpus loss, 64 usage error or an incomplete/mismatched run, 1 anything
else (including an arm that raised).
"""

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_INSUFFICIENT = 2
EXIT_USAGE = 64


class UsageError(Exception):
    """A usage error: bad arguments, a missing file, a mismatched run (exit 64)."""
