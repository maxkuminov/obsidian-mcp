"""An in-process ring buffer of the most recent ERROR-and-above log records.

The panel's health page needs to answer "what has gone wrong lately" without an
operator opening an SSH session. Everything this server logs goes to stdout and
therefore to the container log, which **rotates with the container**: the errors
from before the last `make deploy` are gone, and the ones an operator most wants
are usually the ones that preceded a restart.

So the errors are kept in memory instead, and that is the whole design:

* **Process lifetime only.** No table, no migration, nothing to prune. Persisting
  them is observability scope creep — the honest boundary is "since this process
  started", and the page says exactly that rather than implying a history it does
  not have. `make logs` remains the tool for anything older or fuller.
* **A bounded `deque`.** `maxlen=100`; the 101st error evicts the 1st. An
  unbounded list is a memory leak with a log line as its trigger, which is the
  one shape of leak a server can be *made* to have by whatever is already going
  wrong.
* **The first line of the message, not the traceback.** The page is a pointer,
  not a log viewer: a hundred multi-thousand-line tracebacks rendered into one
  HTML page is a page nobody can read and a response nobody should send. The
  first line is what identifies the error; `make logs` has the rest.

## Why it cannot raise

This is a `logging.Handler` on the **root** logger, so it runs inside every
`logger.error(...)` in the process, including the ones in exception handlers.
A handler that raises there turns an error into a second error at a point where
almost nothing is prepared for one. `emit` therefore catches everything and
falls back to the un-formatted `msg`; a record it cannot render at all is
dropped rather than escalated.

## Multi-worker

Single-process uvicorn today, so the buffer is the whole server's. Under
multiple workers each process would keep its own and the page would show one
worker's errors — noted here and on the page's copy rather than solved, because
the fix (shipping records somewhere shared) is the persistence this module
deliberately does not do.
"""
from __future__ import annotations

import collections
import datetime
import logging
import threading

#: How many records the buffer holds. The 101st error evicts the 1st.
ERROR_BUFFER_SIZE = 100

#: The level at and above which a record is captured. `logging.ERROR` catches
#: `CRITICAL` too — the startup guards in `src/main.py` log at that level, and
#: those are precisely the records an operator comes looking for.
CAPTURE_LEVEL = logging.ERROR

# The buffer and the moment observation began. `_lock` guards both: logging
# calls can arrive from the event loop and from any thread a library spawns,
# and `deque.append` is atomic while "append, then read the whole thing for a
# render" is not.
_lock = threading.Lock()
_buffer: collections.deque = collections.deque(maxlen=ERROR_BUFFER_SIZE)
_observing_since: datetime.datetime | None = None
_handler: logging.Handler | None = None


def _first_line(record: logging.LogRecord) -> str:
    """The record's message, first line only, never raising.

    `record.getMessage()` interpolates `msg % args` and raises on a format
    mismatch — a real thing that happens in the exception paths that produce
    these records in the first place. The fallback is the raw `msg`, which is
    still the identifying half of the line.
    """
    try:
        text = record.getMessage()
    except Exception:  # noqa: BLE001 - a logging handler may not raise
        try:
            text = str(record.msg)
        except Exception:  # noqa: BLE001
            return "<unrenderable log record>"
    line = text.splitlines()[0] if text.splitlines() else ""
    return line.strip()


class RingBufferHandler(logging.Handler):
    """Append ERROR+ records to the module-level ring buffer.

    Deliberately stores a small dict rather than the `LogRecord`: a record
    holds `exc_info`, which holds a traceback, which holds every frame's locals
    — up to a hundred of them, alive for the life of the process. Keeping four
    strings instead is the difference between a bounded buffer and a bounded
    number of unbounded object graphs.
    """

    def emit(self, record: logging.LogRecord) -> None:
        try:
            entry = {
                "timestamp": datetime.datetime.fromtimestamp(
                    record.created, tz=datetime.timezone.utc
                ),
                "logger": record.name,
                "level": record.levelname,
                "message": _first_line(record),
            }
        except Exception:  # noqa: BLE001 - never raise out of logging
            return
        with _lock:
            _buffer.append(entry)


def attach(root: logging.Logger | None = None) -> logging.Handler:
    """Install the handler on the root logger. Idempotent.

    Called from the lifespan, before the sandbox-mode branch returns, so a
    process that skips every startup guard still records what it fails at.
    Calling it twice (a re-entered lifespan, as in tests) reuses the existing
    handler rather than double-recording every error.
    """
    global _handler, _observing_since

    target = root if root is not None else logging.getLogger()
    with _lock:
        if _handler is not None and _handler in target.handlers:
            return _handler
        if _handler is None:
            _handler = RingBufferHandler(level=CAPTURE_LEVEL)
        if _observing_since is None:
            _observing_since = datetime.datetime.now(datetime.timezone.utc)
        handler = _handler
    target.addHandler(handler)
    return handler


def detach(root: logging.Logger | None = None) -> None:
    """Remove the handler. The buffer's contents are left alone — a shutdown
    that logs an error on its way out should still be readable if anything
    renders afterwards."""
    global _handler
    target = root if root is not None else logging.getLogger()
    with _lock:
        handler = _handler
    if handler is not None:
        target.removeHandler(handler)


def reset() -> None:
    """Forget everything, including the observation window. Tests only —
    nothing in the server clears this, because "since process start" is only
    true if it is never cleared."""
    global _observing_since
    with _lock:
        _buffer.clear()
        _observing_since = None


def observing_since() -> datetime.datetime | None:
    """When the handler was attached, i.e. what "since" means on the page.

    None before the lifespan has run at all (a bare test client, an import-time
    render), which the page states as an unknown window rather than implying it
    has been watching since the epoch.
    """
    with _lock:
        return _observing_since


def error_count() -> int:
    """How many records the buffer currently holds.

    At the cap this is 100 and not "100 or more": the buffer cannot say how many
    it evicted, and the dashboard strip's copy says "most recent" rather than a
    total it cannot know.
    """
    with _lock:
        return len(_buffer)


def is_full() -> bool:
    """True once eviction has begun, so a reader can say "at least"."""
    with _lock:
        return len(_buffer) >= ERROR_BUFFER_SIZE


def recent_errors(limit: int = ERROR_BUFFER_SIZE) -> list[dict]:
    """The buffered records, **newest first**, at most `limit` of them.

    Copies are returned: the entries are plain dicts and a caller that mutated
    one would be editing the buffer.
    """
    limit = max(0, min(int(limit), ERROR_BUFFER_SIZE))
    with _lock:
        entries = list(_buffer)
    entries.reverse()
    return [dict(entry) for entry in entries[:limit]]
