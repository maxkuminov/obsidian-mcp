"""Call-scoped holder for per-phase search timings.

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
"""
from __future__ import annotations

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
