"""#193 — a tool body that raises leaves a record and a row, and nothing else.

`_tracked`'s wrapper used to be a `try`/`finally` with **no `except`**: an
exception from a tool body skipped `_log_usage` entirely (probe: zero usage
rows), and the SDK turned it into an error result with no logger call at all. A
write tool that failed halfway left no audit row, no log line, and nothing for
the health page's ERROR ring buffer — for the one class of call an operator
most needs to see.

What is pinned here is the *shape* of the fix, because every plausible way of
getting it wrong is worse than the defect (design D5, D11):

* the handler guards **only** `await fn(...)` — not the three admission gates
  before it, and not the parameter and logging work after it, so a completed
  write can never be reported as failed;
* it catches `Exception`, never `BaseException`, so a cancellation propagates
  without being recorded as a tool failure and without writing a row;
* the audit row is best effort and **never masks the tool's exception** — not
  even when a cancellation arrives during the audit `await`, which is
  deliberately superseded;
* the phase-timing holder is still cleared in `finally`.
"""
import asyncio
import logging

import pytest

import src.mcp_server.tools as tools
from src.logging_setup import build_payload
from src.services import security_events, timing


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record):
        self.records.append(record)


class Boom(RuntimeError):
    """The tool body's own failure, distinguishable from anyone else's."""


@pytest.fixture
def sink():
    """Records that reach the security-event logger, suppressor out of the way.

    One emission attempt has to mean one record here: what these cases count is
    the *handler's* behaviour, and the allowance is pinned separately in
    `tests/test_issue_190_suppressor.py` and
    `tests/test_issue_192_write_refusal_marker.py`.
    """
    handler = _Capture()
    logger = security_events.logger
    logger.addHandler(handler)
    propagate, level = logger.propagate, logger.level
    logger.propagate = False
    logger.setLevel(logging.DEBUG)
    security_events.reset_state()
    try:
        with security_events.suppression_disabled():
            with security_events.strict_fields():
                yield handler.records
    finally:
        logger.removeHandler(handler)
        logger.propagate = propagate
        logger.setLevel(level)
        security_events.reset_state()


@pytest.fixture
def rows(monkeypatch):
    """Every `usage_logs` row `_tracked` asks for, and no database.

    The vault gate is stubbed rather than satisfied: what is under test is the
    decorator's exception path, and a real root would only add a filesystem to
    the surface. The quota gate is left real — one of these cases needs it to
    fail *before* the body.
    """
    written: list[dict] = []

    async def capture(tool, params, duration_ms, response_size):
        written.append(
            {
                "tool": tool,
                "params": params,
                "duration_ms": duration_ms,
                "response_size": response_size,
            }
        )
        return True

    monkeypatch.setattr(tools, "_vault_admission_error", lambda: None)
    monkeypatch.setattr(tools, "_log_usage", capture)
    return written


def _events(records, name):
    return [r for r in records if r.getMessage() == name]


@tools._tracked("probe_raises", ["value"], resource_class="other")
async def probe_raises(value: str = "x", exc: BaseException | None = None) -> str:
    raise exc if exc is not None else Boom("the body failed")


@tools._tracked("probe_ok", ["value"], resource_class="other")
async def probe_ok(value: str = "x") -> str:
    return f"ran:{value}"


@tools._tracked("probe_marks_then_raises", [], resource_class="other")
async def probe_marks_then_raises() -> str:
    timing.record("error", tools._RELATED_SOURCE_NOT_FOUND_MARKER)
    raise Boom("recorded a marker, then failed")


# ══════════════════════════════════════════════════════════════════════════
# 1. The record and the row
# ══════════════════════════════════════════════════════════════════════════


async def test_a_raising_body_writes_one_record_and_one_row(sink, rows):
    with pytest.raises(Boom):
        await probe_raises("hello")

    records = _events(sink, "tool_exception")
    assert len(records) == 1
    record = records[0]
    assert record.levelno == logging.ERROR, "the health page reads ERROR only"

    payload = build_payload(record)
    assert payload["tool"] == "probe_raises"
    assert payload["error_type"] == "Boom"
    assert "Traceback" in payload["stack"], "the traceback is the point of exc_info"
    assert payload["duration_ms"] >= 0

    assert len(rows) == 1
    row = rows[0]
    assert row["tool"] == "probe_raises"
    assert row["params"]["error"] == tools._TOOL_EXCEPTION_MARKER
    assert row["params"]["error_type"] == "Boom"
    # The row carries the call's own arguments, exactly as a successful one
    # does: a failure an operator cannot tell the shape of is half a record.
    assert row["params"]["value"] == "hello"
    assert row["duration_ms"] >= 0
    assert row["response_size"] == 0, "there was no response to size"


async def test_the_original_exception_instance_propagates(sink, rows):
    mine = Boom("this exact object")
    with pytest.raises(Boom) as caught:
        await probe_raises("x", exc=mine)
    assert caught.value is mine


async def test_the_exception_wins_over_a_marker_the_body_recorded(sink, rows):
    """A body that recorded `related_source_not_found` and *then* raised is
    logged as `tool_exception`: the exception is the outcome, and the reader
    that has to choose between two markers on one row would otherwise be
    choosing between "found nothing" and "failed"."""
    with pytest.raises(Boom):
        await probe_marks_then_raises()

    assert rows[0]["params"]["error"] == tools._TOOL_EXCEPTION_MARKER
    assert rows[0]["params"]["error"] != tools._RELATED_SOURCE_NOT_FOUND_MARKER


async def test_the_timing_holder_is_still_cleared(sink, rows):
    with pytest.raises(Boom):
        await probe_marks_then_raises()
    assert timing.current() is None, (
        "the holder must be reset in `finally`, or the next call in this task "
        "inherits this one's phases"
    )


# ══════════════════════════════════════════════════════════════════════════
# 2. What the handler must NOT claim
# ══════════════════════════════════════════════════════════════════════════


async def test_a_completed_body_is_never_reported_as_failed(sink, monkeypatch):
    """The telemetry tail is outside the guard on purpose (design D5).

    Reporting a completed `edit_note` as `tool_exception` because `_log_usage`
    failed *after* the bytes reached the disk is precisely the silently wrong
    record this change exists to prevent — and it is the failure the first draft
    of the handler had, because it wrapped the whole wrapper body.

    Being outside the classifier stopped the failure being *reported* as a tool
    failure. It did not stop it **being** one — the exception still escaped, so
    the caller saw an error for a write that stood, which is the same wrong
    answer from the other side (design D21). The tail is now failure-isolated:
    the completed result comes back, and the bookkeeping failure is recorded as
    itself.
    """
    monkeypatch.setattr(tools, "_vault_admission_error", lambda: None)

    async def exploding_log(*_args, **_kwargs):
        raise RuntimeError("the audit failed after the body completed")

    monkeypatch.setattr(tools, "_log_usage", exploding_log)

    result = await probe_ok("done")

    assert result == "ran:done", "the body completed; its result is the answer"
    assert _events(sink, "tool_exception") == []
    failures = _events(sink, "tool_telemetry_failed")
    assert len(failures) == 1
    payload = build_payload(failures[0])
    assert payload["tool"] == "probe_ok"
    assert payload["error_type"] == "RuntimeError"
    # Class only: a `transforms` or serialisation failure quotes the arguments
    # or the result, which are note content and vault paths (design D2).
    assert "after the body completed" not in repr(payload)


async def test_a_failing_transform_in_the_tail_still_returns_the_result(
    sink, monkeypatch
):
    """The other two ways the tail can raise, and the more dangerous pair.

    `named_params()` runs each tool's own `transforms` and `_response_size`
    serialises the result — both touch caller data, both run *after* a write
    has landed, and neither has any business failing the call.
    """
    monkeypatch.setattr(tools, "_vault_admission_error", lambda: None)

    def exploding_size(_result):
        raise ValueError("the result would not serialise")

    monkeypatch.setattr(tools, "_response_size", exploding_size)

    result = await probe_ok("done")

    assert result == "ran:done"
    assert _events(sink, "tool_exception") == []
    (failure,) = _events(sink, "tool_telemetry_failed")
    assert build_payload(failure)["error_type"] == "ValueError"


async def test_a_cancelled_tail_still_unwinds(sink, monkeypatch):
    """`BaseException` is deliberately not caught in the tail.

    A cancellation there is a client that went away or a shutdown; swallowing
    it into a returned result would defeat the cancellation, which is a
    different bug from the one D21 fixes.
    """
    monkeypatch.setattr(tools, "_vault_admission_error", lambda: None)

    async def cancelled_log(*_args, **_kwargs):
        raise asyncio.CancelledError()

    monkeypatch.setattr(tools, "_log_usage", cancelled_log)

    with pytest.raises(asyncio.CancelledError):
        await probe_ok("done")

    assert _events(sink, "tool_exception") == []
    assert _events(sink, "tool_telemetry_failed") == []


async def test_a_gate_failure_before_the_body_is_not_a_tool_exception(
    sink, monkeypatch, rows
):
    """A database fault inside the quota gate is not a tool failure.

    `quotas.admit` already logs `quota_admission_failed` and re-raises
    deliberately; classifying it as a *tool* exception would tell an operator
    the tool is broken when the counter is.
    """

    async def exploding_gate():
        raise RuntimeError("the quota counter is unreachable")

    monkeypatch.setattr(tools, "_quota_admission_error", exploding_gate)

    with pytest.raises(RuntimeError, match="quota counter"):
        await probe_ok("never runs")

    assert _events(sink, "tool_exception") == []
    assert rows == [], "the body never ran, so there is nothing to audit"


async def test_a_cancelled_body_writes_nothing(sink, rows):
    """`CancelledError` is a `BaseException` in 3.8+, and the handler catches
    `Exception`. A client disconnect or a shutdown is not a tool failure."""
    with pytest.raises(asyncio.CancelledError):
        await probe_raises("x", exc=asyncio.CancelledError())

    assert _events(sink, "tool_exception") == []
    assert rows == []


# ══════════════════════════════════════════════════════════════════════════
# 3. The audit write is best effort, and never masks the failure (D11)
# ══════════════════════════════════════════════════════════════════════════


async def test_an_audit_that_gave_up_is_reported_and_changes_nothing(
    sink, monkeypatch
):
    monkeypatch.setattr(tools, "_vault_admission_error", lambda: None)

    async def gives_up(*_args, **_kwargs):
        return False

    monkeypatch.setattr(tools, "_log_usage", gives_up)

    with pytest.raises(Boom):
        await probe_raises("x")

    assert len(_events(sink, "tool_exception")) == 1
    failures = _events(sink, "tool_usage_log_failed")
    assert len(failures) == 1
    assert build_payload(failures[0])["tool"] == "probe_raises"


async def test_an_audit_that_raises_does_not_mask_the_tool_exception(
    sink, monkeypatch
):
    monkeypatch.setattr(tools, "_vault_admission_error", lambda: None)

    async def explodes(*_args, **_kwargs):
        raise OSError("the pool is exhausted")

    monkeypatch.setattr(tools, "_log_usage", explodes)

    with pytest.raises(Boom):
        await probe_raises("x")

    failures = _events(sink, "tool_usage_log_failed")
    assert len(failures) == 1
    assert build_payload(failures[0])["error_type"] == "OSError"


async def test_a_cancellation_during_the_audit_is_superseded(sink, monkeypatch):
    """The one place in the codebase that catches `BaseException`, deliberately.

    If the task is cancelled while the audit row is being written, the
    `CancelledError` is recorded and **superseded**: the caller receives the
    tool's original exception. The coroutine still unwinds immediately, so
    cancellation achieves its purpose of stopping the work; what changes is only
    the exception type the awaiter observes on a call that had *already* failed.
    The alternative — letting the cancellation win — loses the tool exception
    entirely, which is the record this whole change exists to produce.
    """
    monkeypatch.setattr(tools, "_vault_admission_error", lambda: None)
    reached_the_end = []

    async def cancelled_audit(*_args, **_kwargs):
        raise asyncio.CancelledError()

    monkeypatch.setattr(tools, "_log_usage", cancelled_audit)

    mine = Boom("the tool's own failure")
    with pytest.raises(Boom) as caught:
        await probe_raises("x", exc=mine)
        reached_the_end.append(True)  # pragma: no cover - unwinding is the point

    assert caught.value is mine, "the cancellation must not replace it"
    assert reached_the_end == [], "the coroutine unwound rather than continuing"

    failures = _events(sink, "tool_usage_log_failed")
    assert len(failures) == 1
    assert build_payload(failures[0])["error_type"] == "CancelledError"


# ══════════════════════════════════════════════════════════════════════════
# 4. The row is not a refusal
# ══════════════════════════════════════════════════════════════════════════


def test_the_marker_stays_out_of_the_pre_body_refusal_predicate():
    """A tool that raises after eight seconds of I/O is the slowest path there
    is, and `/admin/performance` is the one view built to find slow paths."""
    from src.services import usage_stats

    assert tools._TOOL_EXCEPTION_MARKER not in (
        usage_stats.PRE_BODY_REFUSAL_ERROR_MARKERS
    )
    assert tools._TOOL_EXCEPTION_MARKER not in usage_stats.PRE_BODY_REFUSAL_BINDS.values()
