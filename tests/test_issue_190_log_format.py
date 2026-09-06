"""#190 — the log configuration applies, and one record is one line.

The defect: `src/main.py`'s `logging.basicConfig` ran after the MCP SDK had
already put a `RichHandler` on the root logger, so it was a documented no-op.
Every structured field this server passed was dropped, timestamps were local,
and tracebacks were boxed across many physical lines.

These are the format-and-plumbing halves. The field policy is
`test_issue_190_field_allowlist.py`; the allowance is
`test_issue_190_suppressor.py`; the real-process assertion is
`tests/integration/test_issue_190_log_config_process.py`.
"""
import datetime
import json
import logging
import sys

import pytest

from src.logging_setup import (
    STACK_HEAD_BYTES,
    STACK_TAIL_BYTES,
    StructuredFormatter,
    configure_logging,
    installed_stream_handler,
)
from src.services import error_log


@pytest.fixture
def clean_logging():
    """Restore the root logger (and the ring buffer) after each test."""
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    error_log.detach()
    error_log.reset()
    try:
        yield root
    finally:
        error_log.detach()
        error_log.reset()
        for handler in list(root.handlers):
            root.removeHandler(handler)
        for handler in saved_handlers:
            root.addHandler(handler)
        root.setLevel(saved_level)


def _record(msg="an_event", level=logging.WARNING, exc_info=None, **extra):
    record = logging.LogRecord("probe", level, __file__, 1, msg, None, exc_info)
    for name, value in extra.items():
        setattr(record, name, value)
    return record


def _rendered(**kwargs):
    return json.loads(StructuredFormatter().format(_record(**kwargs)))


# A genuinely deep traceback. Distinct frames, deliberately: CPython collapses
# a *recursive* one into "[Previous line repeated N more times]", which is short
# and would not exercise the budget at all.
_DEEP_FRAMES = 300
_deep_ns: dict = {}
exec(
    compile(
        "\n".join(
            [f"def f{i}():\n    f{i + 1}()" for i in range(_DEEP_FRAMES)]
            + [f"def f{_DEEP_FRAMES}():\n    raise RuntimeError('boom')"]
        ),
        "<deep>",
        "exec",
    ),
    _deep_ns,
)


def _deep(depth):
    if depth <= 1:
        raise RuntimeError("boom")
    _deep_ns["f0"]()


def _exc_info(depth=_DEEP_FRAMES, message=None):
    try:
        if message is not None:
            raise RuntimeError(message)
        _deep(depth)
    except RuntimeError:
        return sys.exc_info()
    raise AssertionError("expected a RuntimeError")  # pragma: no cover


# ── One line, whatever the record carries ───────────────────────────────────


def test_a_record_is_exactly_one_physical_line():
    line = StructuredFormatter().format(_record(reason="invalid_key"))
    assert "\n" not in line
    assert json.loads(line)["reason"] == "invalid_key"


def test_a_record_carrying_a_traceback_is_still_one_line():
    line = StructuredFormatter().format(_record(exc_info=_exc_info()))
    assert "\n" not in line
    payload = json.loads(line)
    assert "Traceback" in payload["stack"]
    assert payload["error_type"] == "RuntimeError"


def test_a_multi_line_message_is_still_one_line():
    line = StructuredFormatter().format(_record(msg="first\nsecond"))
    assert "\n" not in line
    assert json.loads(line)["msg"] == "first\nsecond"


# ── The traceback budget ────────────────────────────────────────────────────


def test_a_short_traceback_is_not_elided():
    payload = _rendered(exc_info=_exc_info(depth=1))
    assert "elided" not in payload["stack"]
    assert payload["stack"].endswith("RuntimeError: boom\n")


def test_a_long_traceback_keeps_head_tail_type_line_and_final_line():
    payload = _rendered(exc_info=_exc_info())
    stack = payload["stack"]
    assert "bytes elided" in stack
    # Head: the outermost frame. Tail and type line: what actually failed.
    assert "Traceback (most recent call last)" in stack
    assert "RuntimeError: boom" in stack
    assert len(stack.encode()) < STACK_HEAD_BYTES + STACK_TAIL_BYTES + 4096


def test_the_identifying_lines_survive_a_message_longer_than_the_tail():
    """The guarantee is not free: when the *final line itself* is longer than
    the tail budget, elision would have removed it whole."""
    payload = _rendered(exc_info=_exc_info(message="z" * 20_000))
    stack = payload["stack"]
    assert "bytes elided" in stack
    assert "RuntimeError: " in stack
    # ...and the guarantee may not unbound the record it is guaranteeing.
    assert len(stack.encode()) < STACK_HEAD_BYTES + STACK_TAIL_BYTES + 4096


def test_the_elision_marker_names_the_dropped_byte_count():
    payload = _rendered(exc_info=_exc_info())
    marker = [
        part for part in payload["stack"].splitlines() if "bytes elided" in part
    ][0]
    dropped = int(marker.split("[")[1].split(" bytes")[0])
    assert dropped > 0


# ── The timestamp ───────────────────────────────────────────────────────────


def test_the_timestamp_is_utc_iso_8601_with_a_z():
    payload = _rendered()
    assert payload["ts"].endswith("Z")
    parsed = datetime.datetime.fromisoformat(payload["ts"].replace("Z", "+00:00"))
    assert parsed.tzinfo is not None
    assert parsed.utcoffset() == datetime.timedelta(0)


# ── The text rendering ──────────────────────────────────────────────────────


def test_text_renders_the_same_object_on_one_line():
    record = _record(reason="invalid_key", user_id=7, token_tag="sha:abcdef12")
    line = StructuredFormatter("text").format(record)
    payload = json.loads(StructuredFormatter("json").format(record))
    assert "\n" not in line
    assert line.startswith(payload["ts"])
    for key in ("reason", "user_id", "token_tag"):
        assert f"{key}={payload[key]}" in line
    assert "WARNING probe an_event" in line


def test_text_escapes_a_newline_rather_than_emitting_it():
    line = StructuredFormatter("text").format(_record(msg="first\nsecond"))
    assert "\n" not in line
    assert "first\\nsecond" in line


# ── Reconfiguration ─────────────────────────────────────────────────────────


def test_configure_logging_installs_exactly_one_stream_handler(clean_logging):
    root = clean_logging
    root.addHandler(logging.StreamHandler(sys.stderr))
    configure_logging(level=logging.INFO)
    assert len(root.handlers) == 1
    assert isinstance(root.handlers[0].formatter, StructuredFormatter)
    assert installed_stream_handler() is root.handlers[0]


def test_configure_logging_is_idempotent(clean_logging, capsys):
    root = clean_logging
    configure_logging(level=logging.INFO)
    configure_logging(level=logging.INFO)
    assert len(root.handlers) == 1
    logging.getLogger("probe").warning("an_event", extra={"reason": "twice"})
    lines = [
        line
        for line in capsys.readouterr().err.splitlines()
        if "an_event" in line
    ]
    assert len(lines) == 1
    assert json.loads(lines[0])["reason"] == "twice"


def test_the_ring_buffer_survives_when_it_was_attached_first(clean_logging):
    root = clean_logging
    handler = error_log.attach()
    window = error_log.observing_since()
    configure_logging(level=logging.INFO)
    assert handler in root.handlers
    assert error_log.observing_since() == window
    logging.getLogger("probe").error("kaboom")
    assert error_log.error_count() == 1


def test_the_ring_buffer_survives_when_it_is_attached_afterwards(clean_logging):
    """The order the running server actually uses: `configure_logging()` at
    import, `error_log.attach()` in the lifespan."""
    root = clean_logging
    configure_logging(level=logging.INFO)
    handler = error_log.attach()
    assert handler in root.handlers
    logging.getLogger("probe").error("kaboom")
    logging.getLogger("uvicorn.error").error("Exception in ASGI application")
    assert error_log.error_count() == 2


def test_configure_logging_closes_the_handler_it_displaces(clean_logging):
    closed = []

    class _Noisy(logging.StreamHandler):
        def close(self):
            closed.append(True)
            super().close()

    clean_logging.addHandler(_Noisy(sys.stderr))
    configure_logging(level=logging.INFO)
    assert closed == [True]


def test_configure_logging_never_raises(clean_logging, monkeypatch):
    monkeypatch.setattr(
        error_log, "installed_handler", lambda: (_ for _ in ()).throw(RuntimeError())
    )
    assert configure_logging(level=logging.INFO) is None


def test_the_httpx_floors_survive_reconfiguration(clean_logging):
    logging.getLogger("httpx").setLevel(logging.DEBUG)
    configure_logging(level=logging.DEBUG)
    assert logging.getLogger("httpx").level == logging.WARNING
    assert logging.getLogger("httpcore").level == logging.WARNING
