"""The file-level steps behind the CLI: read inputs, call the pure code, write outputs."""
from __future__ import annotations

from pathlib import Path

from scripts.search_eval import UsageError
from scripts.search_eval._io import atomic_write, canonical_json, ensure_private_dir, pretty_json
from scripts.search_eval.mining import DEFAULT_WINDOW_SECONDS, MineResult, load_export, load_manual, mine

QUERIES_FILE = "queries.jsonl"
QRELS_FILE = "qrels.json"
MINE_REPORT_FILE = "mine-report.json"
#: #389: one line per usable semantic/hybrid search row, for `confidence`.
CONFIDENCE_ROWS_FILE = "confidence-rows.jsonl"


def write_mine(*, export: Path, out: Path, window_seconds: float = DEFAULT_WINDOW_SECONDS,
               manual: Path | None = None, user_id: int | None = None) -> MineResult:
    """`mine`: export → queries.jsonl, qrels.json, mine-report.json and
    confidence-rows.jsonl (#389), all 0600."""
    if not export.is_file():
        raise UsageError(f"no export file at {export}")
    if manual is not None and not manual.is_file():
        raise UsageError(f"no manual judgments file at {manual}")
    loaded = load_export(export)
    manual_rows = load_manual(manual) if manual is not None else None
    result = mine(loaded.rows, window_seconds=window_seconds, manual=manual_rows,
                  malformed=loaded.malformed, user_filter=user_id)
    ensure_private_dir(out)
    atomic_write(out / QUERIES_FILE, "".join(canonical_json(q) + "\n" for q in result.queries))
    atomic_write(out / QRELS_FILE, canonical_json(result.qrels))
    atomic_write(out / MINE_REPORT_FILE, pretty_json(result.report))
    atomic_write(out / CONFIDENCE_ROWS_FILE,
                 "".join(canonical_json(r) + "\n" for r in result.confidence_rows))
    return result


async def write_snapshot(*, data: Path, database_url: str | None, force: bool = False) -> dict:
    """`snapshot`: per-arm searchable membership of the restored copy, pre-candidate (D6)."""
    from scripts.search_eval.db import describe_url, fetch_membership, open_readonly, resolve_url
    from scripts.search_eval.scoring import SNAPSHOT_FILE, SNAPSHOT_META_FILE

    url = resolve_url(database_url)
    target = data / SNAPSHOT_FILE
    if target.exists() and not force:
        raise UsageError(f"{target} exists; pass --force to replace the pre-candidate snapshot")
    engine, maker = await open_readonly(url)
    try:
        async with maker() as session:
            membership = await fetch_membership(session)
    finally:
        await engine.dispose()
    ensure_private_dir(data)
    digest = atomic_write(target, canonical_json(membership))
    meta = {
        "sha256": digest,
        **describe_url(url),
        "rows": sum(len(v) for v in membership.values()),
        "stale_vector_rows": sum(1 for v in membership.values() for e in v.values()
                                 if e["stale_vector"]),
    }
    atomic_write(data / SNAPSHOT_META_FILE, pretty_json(meta))
    return meta
