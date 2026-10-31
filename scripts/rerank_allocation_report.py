"""Read-only measurement for rerank-documents task 7.4 (#402).

For each named query and one target note, at the given call limits and on
both tools, report which of the note's fetched chunks the production
allocation sends to the reranker:

    q<k> <tool> limit=<L>: candidates=<m> documents=<n> note_rank=<r>
      chunk <chunk_index>: fetched_rank=<i> allocated=<yes|no> [score=<s>]

`fetched_rank` is the chunk's 1-based position among the fetched chunk rows
of the call's candidate notes (the full-precision re-sort order). `score` is
printed only with `--score` and a configured `RERANK_URL` (probed first); it
scores every fetched chunk of the note, allocated or not, in one extra
request.

**Numbers only.** The query text and the note path come from the command
line (or a JSON file outside the repo), and are never printed: queries are
labelled `q1, q2, …` and the note only by its rank. The database session is
read-only (`default_transaction_read_only`, asserted on open), so the copy
cannot be written.

    python -m scripts.rerank_allocation_report --database-url URL \\
        --query "…" --note "Folder/Note.md" [--query … --note …] \\
        [--user-id N] [--limits 10,20] [--score]

Never point it at a database you would not hand a read-only reader.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys


class ReportFailure(Exception):
    """A refusal with a fixed, text-free code (exit 64)."""


class _Parser(argparse.ArgumentParser):
    """argparse's errors echo the offending argument, which may be tenant
    query text: refuse with a fixed code instead."""

    def error(self, message):  # noqa: D401 - argparse hook
        raise ReportFailure("bad_arguments")


def _parse(argv):
    p = _Parser(prog="rerank_allocation_report", description=__doc__.split("\n")[0])
    p.add_argument("--database-url", required=True)
    p.add_argument("--query", action="append", default=[])
    p.add_argument("--note", action="append", default=[])
    p.add_argument("--pairs-file", help="JSON list of {query, note} (kept outside the repo)")
    p.add_argument("--user-id", type=int, default=None)
    p.add_argument("--limits", default="10,20")
    p.add_argument("--score", action="store_true")
    args = p.parse_args(argv)
    pairs = list(zip(args.query, args.note))
    if len(args.query) != len(args.note):
        p.error("--query and --note must be given in pairs")
    if args.pairs_file:
        with open(args.pairs_file, encoding="utf-8") as fh:
            pairs += [(d["query"], d["note"]) for d in json.load(fh)]
    if not pairs:
        p.error("no query/note pair")
    args.pairs = pairs
    args.limit_list = [int(x) for x in args.limits.split(",") if x.strip()]
    return args


def _fetched_ranks(chunk_lists) -> dict[tuple[str, int], int]:
    rows = sorted(
        (c for chunks in chunk_lists for c in chunks),
        key=lambda c: (c["distance"], c["file_path"], c["chunk_index"]),
    )
    return {(c["file_path"], c["chunk_index"]): i + 1 for i, c in enumerate(rows)}


def report_lines(label: str, tool: str, limit: int, rows, note: str, *, path_of,
                 chunks_of, groups, scores: dict[int, float] | None = None) -> list[str]:
    """The pure part: given the candidate rows and their allocated groups."""
    ranks = _fetched_ranks(chunks_of(r) for r in rows)
    n = sum(len(g) for g in groups)
    idx = next((i for i, r in enumerate(rows) if path_of(r) == note), None)
    head = f"{label} {tool} limit={limit}: candidates={len(rows)} documents={n}"
    if idx is None:
        return [head + " note_rank=absent"]
    out = [head + f" note_rank={idx + 1}"]
    allocated = {d.chunk["chunk_index"] for d in groups[idx] if d.chunk is not None}
    for c in chunks_of(rows[idx]):
        line = (f"  chunk {c['chunk_index']}: fetched_rank={ranks[(c['file_path'], c['chunk_index'])]}"
                f" allocated={'yes' if c['chunk_index'] in allocated else 'no'}")
        if scores is not None and c["chunk_index"] in scores:
            line += f" score={scores[c['chunk_index']]:.2f}"
        out.append(line)
    if not chunks_of(rows[idx]):
        out.append("  (no fetched chunk: stale, unknown stamp or keyword-only)")
    return out


async def _note_scores(query, title, chunks, stamp):
    from src.services import rerank

    cand = rerank.candidate_documents(title, chunks=chunks, stamp=stamp, fallback_text=None)
    docs = [cand.primary, *(d for _k, d in cand.secondaries)]
    if any(d.chunk is None for d in docs):
        return None
    result = await rerank.score(query, [d.text for d in docs], deadline_s=60)
    if isinstance(result, rerank.RerankFailure):
        return None
    return {d.chunk["chunk_index"]: s for d, s in zip(docs, result)}


async def main_async(args, out: list[str]) -> int:
    """Append every report line to `out`; print nothing. The caller emits
    the lines only once every operation has succeeded."""
    from scripts.search_eval.db import open_readonly
    from src.config import settings
    from src.services import rerank
    from src.services.embeddings import semantic_candidates
    from src.services.hybrid import hybrid_search

    if args.score:
        if not settings.rerank_url:
            raise ReportFailure("score_needs_rerank_url")
        code = await rerank.probe_attempt(rerank.RERANK_PROBE_TIMEOUT_S)
        if code is not None:
            raise ReportFailure(f"probe_failed:{code}")
        rerank._set_enabled()
    engine, maker = await open_readonly(args.database_url)
    try:
        for k, (query, note) in enumerate(args.pairs, 1):
            label = f"q{k}"
            for limit in args.limit_list:
                async with maker() as session:
                    found = await semantic_candidates(session, query, limit=limit,
                                                      user_id=args.user_id)
                rows = found.rows
                groups = rerank.semantic_document_groups(rows)
                scores = None
                target = next((r for r in rows if r["path"] == note), None)
                if args.score and target is not None:
                    scores = await _note_scores(query, target["title"],
                                                target.get("rerank_chunks"),
                                                target.get("rerank_stamp"))
                for line in report_lines(label, "semantic", limit, rows, note,
                                         path_of=lambda r: r["path"],
                                         chunks_of=lambda r: r.get("rerank_chunks") or [],
                                         groups=groups, scores=scores):
                    out.append(line)
                async with maker() as session:
                    result = await hybrid_search(session, query, limit=limit,
                                                 user_id=args.user_id, rerank_candidates=True)
                hrows = result.rows
                hgroups = rerank.hybrid_document_groups(hrows)
                for line in report_lines(
                        label, "hybrid", limit, hrows, note, path_of=lambda r: r.path,
                        chunks_of=lambda r: (r.rerank_chunks
                                             if r.semantic_rank is not None and not r.stale
                                             else []),
                        groups=hgroups, scores=scores):
                    out.append(line)
    finally:
        await engine.dispose()
        await rerank.close_rerank_client()
    return 0


def main(argv=None) -> int:
    """Buffered and sanitised (Codex impl round 1): nothing is printed until
    every query has been measured, and any failure prints one fixed line with
    the exception **class** only — never its message, which can quote the
    query text or SQL parameters — and no partial report."""
    out: list[str] = []
    try:
        args = _parse(argv if argv is not None else sys.argv[1:])
        code = asyncio.run(main_async(args, out))
    except SystemExit as exc:  # `--help` only: argparse errors are ReportFailure
        return int(exc.code or 0)
    except ReportFailure as exc:
        print(f"rerank_allocation_report: refused ({exc.args[0]})", file=sys.stderr)
        return 64
    except BaseException as exc:  # noqa: BLE001 - sanitised, never re-raised
        print(f"rerank_allocation_report: failed ({type(exc).__name__}); no report written",
              file=sys.stderr)
        return 1
    sys.stdout.write("".join(line + "\n" for line in out))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
