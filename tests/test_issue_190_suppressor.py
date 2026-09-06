"""#190 — the allowance: charged once, bounded at every level, never lost.

The contract these pin (design D7):

* one `acquire` charges one unit, and consuming the permit checks nothing;
* both caps apply, and a caller-derived value is never a subject;
* every level is bounded, INFO included, and every withheld record is counted;
* a count survives a closed window, an eviction and a shutdown;
* an internal failure fails **open**.
"""
import logging

import pytest

from src.services import security_events


class _Clock:
    """Replaces the module's `time`, so windows can be closed on demand."""

    def __init__(self, start: float = 1_000.0):
        self.now = start

    def monotonic(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record):
        self.records.append(record)


@pytest.fixture
def clock(monkeypatch):
    fake = _Clock()
    monkeypatch.setattr(security_events, "time", fake)
    return fake


@pytest.fixture
def sink():
    handler = _Capture()
    logger = security_events.logger
    logger.addHandler(handler)
    propagate = logger.propagate
    level = logger.level
    logger.propagate = False
    # INFO is part of the contract, and the root's default WARNING would filter
    # it before any handler saw it.
    logger.setLevel(logging.DEBUG)
    security_events.reset_state()
    try:
        yield handler.records
    finally:
        logger.removeHandler(handler)
        logger.propagate = propagate
        logger.setLevel(level)
        security_events.reset_state()


def _events(records, name):
    return [r for r in records if r.getMessage() == name]


def _summaries(records):
    return [r for r in records if r.getMessage() == security_events.SUMMARY_EVENT]


# ── The two caps ────────────────────────────────────────────────────────────


def test_the_per_event_subject_cap_bounds_a_flood(clock, sink):
    for _ in range(25):
        security_events.emit("auth_failure", subject="ip:198.51.100.7", reason="invalid_key")
    assert len(_events(sink, "auth_failure")) == security_events.MAX_EVENTS_PER_WINDOW


def test_one_subject_cannot_multiply_its_allowance_across_events(clock, sink):
    events = [
        "auth_failure",
        "csrf_refused",
        "panel_forbidden",
        "transfer_refused",
        "tool_write_refused",
        "panel_login_failed",
        "oauth_revoke_refused",
    ]
    permits = 0
    for event in events:
        for _ in range(20):
            if security_events.acquire(event, "ip:198.51.100.7") is not None:
                permits += 1
    assert permits == security_events.MAX_EVENTS_PER_SUBJECT_PER_WINDOW


def test_a_second_subject_is_unaffected(clock, sink):
    for _ in range(25):
        security_events.emit("auth_failure", subject="ip:198.51.100.7", reason="invalid_key")
    security_events.emit("auth_failure", subject="ip:203.0.113.5", reason="invalid_key")
    tags = [getattr(r, "client_ip", None) for r in _events(sink, "auth_failure")]
    assert len(_events(sink, "auth_failure")) == security_events.MAX_EVENTS_PER_WINDOW + 1
    assert tags  # the last record is the second subject's


def test_rotating_token_tags_share_one_address_allowance(clock, sink):
    """The whole point of the subject rule: a caller-derived value is not a
    subject, so a fresh bogus token per request cannot mint a fresh allowance."""
    for i in range(40):
        security_events.emit(
            "auth_failure",
            subject="ip:198.51.100.7",
            reason="invalid_key",
            token_tag=security_events.redacted_token_tag(f"token-{i}"),
        )
    assert len(_events(sink, "auth_failure")) == security_events.MAX_EVENTS_PER_WINDOW


def test_an_informational_flood_is_bounded_and_counted(clock, sink):
    """INFO is suppressed too (owner decision): a replayed consent or a logout
    is unbounded on routes no rate limit covers."""
    for _ in range(30):
        security_events.emit(
            "panel_logout", level=logging.INFO, subject="ip:198.51.100.7"
        )
    assert len(_events(sink, "panel_logout")) == security_events.MAX_EVENTS_PER_WINDOW

    clock.advance(security_events.WINDOW_SECONDS + 1)
    security_events.emit("panel_logout", level=logging.INFO, subject="ip:198.51.100.7")
    summary = _summaries(sink)[-1]
    assert summary.reason == "panel_logout"
    assert summary.count == 20
    assert summary.levelno == logging.INFO


# ── Charging exactly once ───────────────────────────────────────────────────


def test_acquiring_then_emitting_charges_the_allowance_once(clock, sink):
    permits = [
        security_events.acquire("transfer_refused", "ip:198.51.100.7")
        for _ in range(security_events.MAX_EVENTS_PER_WINDOW)
    ]
    assert all(p is not None for p in permits)
    for permit in permits:
        security_events.emit(permit, reason="unknown_token")
    assert len(_events(sink, "transfer_refused")) == security_events.MAX_EVENTS_PER_WINDOW
    # The eleventh is refused, which it would not be if consuming had charged.
    assert security_events.acquire("transfer_refused", "ip:198.51.100.7") is None


def test_an_acquired_permit_that_is_never_spent_is_still_charged(clock, sink):
    for _ in range(security_events.MAX_EVENTS_PER_WINDOW):
        assert security_events.acquire("transfer_refused", "ip:198.51.100.7") is not None
    assert security_events.acquire("transfer_refused", "ip:198.51.100.7") is None
    assert _events(sink, "transfer_refused") == []


def test_emitting_a_permit_performs_no_second_check(clock, sink):
    permit = security_events.acquire("transfer_refused", "ip:198.51.100.7")
    for _ in range(security_events.MAX_EVENTS_PER_WINDOW * 3):
        security_events.acquire("transfer_refused", "ip:198.51.100.7")
    security_events.emit(permit, reason="unknown_token")
    assert len(_events(sink, "transfer_refused")) == 1


# ── Summaries ───────────────────────────────────────────────────────────────


def test_the_summary_is_emitted_lazily_before_the_next_event(clock, sink):
    for _ in range(15):
        security_events.emit("auth_failure", subject="ip:198.51.100.7", reason="invalid_key")
    assert _summaries(sink) == []
    clock.advance(security_events.WINDOW_SECONDS + 1)
    security_events.emit("auth_failure", subject="ip:198.51.100.7", reason="invalid_key")
    summary = _summaries(sink)[0]
    assert summary.reason == "auth_failure"
    assert summary.count == 5
    assert summary.window_seconds == security_events.WINDOW_SECONDS
    # The summary precedes the record that triggered it.
    assert sink.index(summary) < len(sink) - 1


def test_a_summary_is_never_itself_suppressed_or_counted(clock, sink):
    for window in range(4):
        for _ in range(15):
            security_events.emit(
                "auth_failure", subject="ip:198.51.100.7", reason="invalid_key"
            )
        clock.advance(security_events.WINDOW_SECONDS + 1)
    security_events.emit("auth_failure", subject="ip:198.51.100.7", reason="invalid_key")
    # One summary per closed window that withheld something, none of them lost
    # to the very cap they are reporting on.
    assert len(_summaries(sink)) == 4


def test_an_outstanding_count_is_flushed_at_shutdown(clock, sink):
    for _ in range(13):
        security_events.emit("auth_failure", subject="ip:198.51.100.7", reason="invalid_key")
    assert _summaries(sink) == []
    security_events.flush_suppression_summaries()
    summary = _summaries(sink)[0]
    assert summary.reason == "auth_failure"
    assert summary.count == 3
    # Flushing twice does not double-report.
    security_events.flush_suppression_summaries()
    assert len(_summaries(sink)) == 1


def test_an_entry_holding_a_count_emits_its_summary_before_eviction(clock, sink):
    """Round 2's MAJOR: evicting a key with an outstanding count loses it, and
    the log then silently under-reports — worse than the flood it bounded."""
    victim = "ip:192.0.2.1"
    for _ in range(security_events.MAX_EVENTS_PER_WINDOW + 3):
        security_events.emit("auth_failure", subject=victim, reason="invalid_key")
    assert _summaries(sink) == []

    # Push the victim, the oldest entry, out of a full map.
    for i in range(security_events.MAX_TRACKED_KEYS + 1):
        security_events.acquire("csrf_refused", f"ip:203.0.113.{i}")

    summary = _summaries(sink)[0]
    assert summary.reason == "auth_failure"
    assert summary.count == 3


# ── Failing open ────────────────────────────────────────────────────────────


def test_an_internal_failure_returns_a_permit_and_emits_the_record(clock, sink, monkeypatch):
    class _Broken(dict):
        def get(self, *args, **kwargs):
            raise RuntimeError("suppressor is broken")

    monkeypatch.setattr(security_events, "_event_windows", _Broken())
    assert security_events.acquire("auth_failure", "ip:198.51.100.7") is not None
    security_events.emit("auth_failure", subject="ip:198.51.100.7", reason="invalid_key")
    assert len(_events(sink, "auth_failure")) == 1


def test_emit_never_raises_into_a_request_path(clock, sink, monkeypatch):
    monkeypatch.setattr(
        security_events,
        "_log",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("sink is broken")),
    )
    security_events.emit("auth_failure", subject="-", reason="invalid_key")


def test_the_suppressor_can_be_disabled_for_tests_that_count_attempts(clock, sink):
    with security_events.suppression_disabled():
        for _ in range(30):
            security_events.emit(
                "auth_failure", subject="ip:198.51.100.7", reason="invalid_key"
            )
    assert len(_events(sink, "auth_failure")) == 30


def test_emitting_a_denied_permit_is_a_no_op(clock, sink):
    """A caller that passes `acquire`'s `None` straight through must not turn a
    withheld record into an emitted one — that would be the second check."""
    for _ in range(security_events.MAX_EVENTS_PER_WINDOW):
        security_events.acquire("transfer_refused", "ip:198.51.100.7")
    denied = security_events.acquire("transfer_refused", "ip:198.51.100.7")
    assert denied is None
    security_events.emit(denied, reason="unknown_token")
    assert _events(sink, "transfer_refused") == []
