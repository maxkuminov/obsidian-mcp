"""Call-scoped holder for per-phase search timings and result telemetry.

`semantic_search` and `find_related` measure their own phases (`embed_ms`,
`db_ms`) and whether they had to fall back to an exact scan, but the thing that
*writes* a usage-log row is the `_tracked` decorator in `src/mcp_server/tools.py`.
Rather than change the services' return types (existing callers, including the
panel and the integration tests, consume plain lists), the values travel on a
`ContextVar` holder.

**`_tracked` owns the holder's lifecycle**: it calls `begin()` at the start of a
tool call and `clear()` in `finally`, so a value can never be attributed to a
different call and a direct service call made outside a tracked tool simply
finds no holder and records nothing.

The ContextVar lives here, not in `tools.py`, only to avoid an import cycle:
`tools` imports `semantic_search` from `src.services.embeddings`, so the
services cannot import from `tools` at module scope. Ownership is still
`_tracked`'s — nothing else calls `begin()`/`clear()`.

A `ContextVar` is per-task, and each MCP tool call runs in its own task, so
concurrent calls cannot see each other's phases.

## The result-telemetry contract (#161)

The same holder carries what a search *returned* — `result_count`,
`result_paths`, and `find_related`'s `source_path` — because
`/admin/search-analytics` needs to know which queries came back empty and which
notes retrieval never surfaces, and nothing else in the log records it.

**The size bound is enforced here, at the record site, and it has to be.**
`_tracked` builds its logged params as `_truncate_params(named args)` and *then*
`update()`s the holder over the top, so anything recorded here reaches
`usage_logs.params` untouched by the generic 200-character truncation. There is
no backstop downstream: a 500-result search whose paths were merged verbatim
would write a params blob orders of magnitude larger than every other row in the
table. Hence `MAX_RESULT_PATHS` and `MAX_RESULT_PATHS_BYTES` below, applied by
`record_results` itself.

The keys are **reserved and typed**, for the same reason `embed_ms` and
`db_ms` are (see `docs/architecture/usage-attribution.md`): the analytics page
casts and unnests them. `result_count` is an int, `result_paths` a list of
strings, `source_path` a string. A writer that wants to record something else
about a result set uses a different key. The page guards its casts anyway —
a bad row must not be able to take a whole window's page down — but the
contract is what keeps the numbers meaningful.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from contextvars import ContextVar, Token

_phase_timing: ContextVar[dict | None] = ContextVar("_phase_timing", default=None)


def begin() -> Token:
    """Install a fresh holder for this call. Returns the token for `clear()`."""
    return _phase_timing.set({})


def clear(token: Token) -> None:
    """Restore the previous holder (normally `None`). Must run in `finally`."""
    _phase_timing.reset(token)


def current() -> dict | None:
    """The active holder, or None when not inside a tracked tool call."""
    return _phase_timing.get()


def record(key: str, value) -> None:
    """Set `key` on the active holder, if any. No-op outside a tracked call."""
    holder = _phase_timing.get()
    if holder is not None:
        holder[key] = value


def add_ms(key: str, elapsed_seconds: float) -> None:
    """Accumulate a millisecond duration under `key` on the active holder.

    Accumulating (rather than setting) lets a caller measure several disjoint
    database phases — `find_related` fetches the source note's chunks before it
    runs the vector query — and report one `db_ms`.
    """
    holder = _phase_timing.get()
    if holder is None:
        return
    holder[key] = int(holder.get(key, 0)) + max(0, int(elapsed_seconds * 1000))


# --- Result telemetry (#161) ----------------------------------------------

#: How many of a call's returned paths are logged. The ranking on
#: `/admin/search-analytics` is named after this number ("top-logged
#: retrievals") because it is a bound on what the log can see, not a fact about
#: what the tool returned.
MAX_RESULT_PATHS = 10

#: The paths value's total budget, in bytes of its UTF-8 JSON encoding. Paths
#: are dropped **from the end** — the head of a ranked result list is the part
#: worth keeping, and dropping from the end keeps `result_paths` a prefix of
#: what the tool returned rather than an arbitrary subset.
MAX_RESULT_PATHS_BYTES = 2048

#: Above this many bytes, `find_related`'s source is recorded as a digest.
#: A path this long is pathological, but the grouping key on the analytics page
#: must stay bounded *and* non-colliding: the tool's named `path` param is
#: truncated at 200 characters, so two distinct long paths collapse onto one
#: row there. That is exactly the mistake `source_path` exists to avoid.
MAX_SOURCE_PATH_BYTES = 1024


def _json_bytes(value) -> int:
    """The size of `value` as it will be stored, in bytes.

    Measured on the JSON encoding rather than on the paths' own bytes, so the
    budget covers the quoting and separators too: whichever way "the paths
    value" is read, the recorded list is within 2048 bytes. `ensure_ascii` is
    off so a non-ASCII path is measured in UTF-8, the encoding the column
    actually stores, and not in `\\uXXXX` escapes.
    """
    return len(json.dumps(value, ensure_ascii=False).encode("utf-8"))


def fit_result_paths(paths: Iterable[str]) -> list[str]:
    """The logged prefix of `paths` under both bounds. Pure, for testing."""
    kept: list[str] = []
    for path in list(paths)[:MAX_RESULT_PATHS]:
        candidate = kept + [str(path)]
        if _json_bytes(candidate) > MAX_RESULT_PATHS_BYTES:
            # Dropping from the end: everything after the first path that does
            # not fit goes too, so the kept list stays a prefix.
            break
        kept = candidate
    return kept


def record_results(paths: Iterable[str]) -> None:
    """Record a search's `result_count` and its bounded `result_paths`.

    `result_count` is the **full** count of what the tool returned, not the
    number of paths that fit the budget: it is the value the zero-result view
    reads, and a count clipped to the logging cap would report a search that
    found 40 notes as having found 10.

    No-op outside a tracked call, like every other writer here.
    """
    holder = _phase_timing.get()
    if holder is None:
        return
    paths = list(paths)
    holder["result_count"] = len(paths)
    holder["result_paths"] = fit_result_paths(paths)


def source_path_value(path: str) -> str:
    """`find_related`'s grouping key: the path, or its digest when too long."""
    # `surrogatepass` rather than a bare encode: an unpaired surrogate is
    # already refused at admission (`argument_not_encodable`), so this is only
    # about staying total — a telemetry helper must not be the thing that
    # raises inside a tool that has already done its work.
    encoded = path.encode("utf-8", "surrogatepass")
    if len(encoded) <= MAX_SOURCE_PATH_BYTES:
        return path
    return hashlib.sha256(encoded).hexdigest()


def record_source_path(path: str) -> None:
    """Record `find_related`'s source under the telemetry key `source_path`."""
    holder = _phase_timing.get()
    if holder is None:
        return
    holder["source_path"] = source_path_value(path)
