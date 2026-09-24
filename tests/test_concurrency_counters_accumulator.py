"""The in-process durable-counter accumulator (#188 design D8)."""
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.services import concurrency
from src.services.concurrency import (
    COUNT_METRICS, Counters, GAUGE_METRICS, MAX_UNFLUSHED_MINUTES, METRICS, counters,
)

ROOT = Path(__file__).resolve().parent.parent
T = datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc)


def at(minute=0, second=0):
    return T + timedelta(minutes=minute, seconds=second)


def test_metric_set_is_closed():
    assert METRICS == {
        "requests", "transport_pressured", "transport_waited", "transport_overrun",
        "transport_refused", "writer_overrun", "writer_refused", "pool_checkout_timeout",
        "pool_high_water", "transport_wait_max_ms",
    }
    c = Counters()
    with pytest.raises(ValueError):
        c.record("slot_timeout")
    with pytest.raises(ValueError):
        c.record("pool_high_water")      # a gauge, not a count
    with pytest.raises(ValueError):
        c.gauge("requests", 3)           # a count, not a gauge
    with pytest.raises(ValueError):
        c.record_request("shadow")
    with pytest.raises(ValueError):
        c.record("requests", -1)
    assert c.drain() == {}


def test_a_request_pressured_at_two_stages_counts_once():
    c = Counters()
    c.record_request("pressured", at(0, 5))  # request + auth pressure: one worst outcome
    c.record_request("none", at(0, 6))
    c.record_request("overrun", at(0, 7))
    assert c.drain() == {
        (T, "requests"): (3, None),
        (T, "transport_pressured"): (1, None),
        (T, "transport_overrun"): (1, None),
    }


@pytest.mark.parametrize("worst", ["pressured", "waited", "overrun", "refused"])
def test_each_outcome_has_its_metric(worst):
    c = Counters()
    c.record_request(worst, at())
    assert c.drain() == {(T, "requests"): (1, None), (T, "transport_" + worst): (1, None)}


def test_event_time_minute_not_drain_time():
    c = Counters()
    c.record("pool_checkout_timeout", at=at(0, 50))  # 12:00:50
    c.record("pool_checkout_timeout", at=at(1, 5))   # 12:01:05, before a 12:01:10 drain
    drained = c.drain()
    assert drained == {(T, "pool_checkout_timeout"): (1, None),
                       (at(1), "pool_checkout_timeout"): (1, None)}
    assert c.drain() == {}


def test_gauges_keep_the_bucket_maximum():
    c = Counters()
    for value in (3, 9, 4):
        c.gauge("pool_high_water", value, at(0, value))
    c.gauge("transport_wait_max_ms", 120.4, at(0, 1))
    drained = c.drain()
    assert drained[(T, "pool_high_water")][1] == 9
    assert drained[(T, "transport_wait_max_ms")][1] == 120


def test_default_time_is_now_and_naive_means_utc():
    c = Counters()
    before = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    c.record("writer_overrun")
    c.record("writer_refused", at=datetime(2026, 9, 23, 12, 0, 59))
    keys = set(c.drain())
    assert (T, "writer_refused") in keys
    (minute,) = [k[0] for k in keys if k[1] == "writer_overrun"]
    assert minute.tzinfo is not None and before <= minute <= before + timedelta(minutes=1)


def test_merge_back_preserves_keys():
    c = Counters()
    c.record("pool_checkout_timeout", at=at(0, 50))
    c.gauge("pool_high_water", 7, at(0, 50))
    drained = c.drain()                       # the flush then fails...
    c.record("pool_checkout_timeout", at=at(2, 1))
    c.gauge("pool_high_water", 5, at(0, 55))  # a late same-minute sample
    c.merge_back(drained)
    again = c.drain()
    assert again[(T, "pool_checkout_timeout")] == (1, None)
    assert again[(at(2), "pool_checkout_timeout")] == (1, None)
    assert again[(T, "pool_high_water")] == (2, 7)


def test_lossy_at_the_unflushed_minute_cap():
    c = Counters()
    for m in range(MAX_UNFLUSHED_MINUTES):
        c.record("requests", at=at(m))
    assert not c.lossy and c.minutes() == MAX_UNFLUSHED_MINUTES
    c.record("requests", at=at(MAX_UNFLUSHED_MINUTES))
    assert c.lossy and c.minutes() == MAX_UNFLUSHED_MINUTES
    drained = c.drain()
    assert (T, "requests") not in drained            # the oldest minute was dropped
    assert (at(MAX_UNFLUSHED_MINUTES), "requests") in drained
    assert c.lossy                                   # sticky for the run
    # A merge-back older than everything at the cap is dropped, not re-keyed.
    c2 = Counters()
    for m in range(1, MAX_UNFLUSHED_MINUTES + 1):
        c2.record("requests", at=at(m))
    c2.merge_back({(T, "requests"): (5, None)})
    assert c2.lossy and (T, "requests") not in c2.drain()


def test_process_wide_singleton():
    concurrency.reset_counters()
    assert counters() is counters()
    counters().record_request("waited", at())
    assert counters().drain()[(T, "transport_waited")] == (1, None)
    assert set(COUNT_METRICS) | set(GAUGE_METRICS) == METRICS


def test_import_creates_no_asyncio_primitive():
    code = (
        "import asyncio\n"
        "def boom(*a, **k): raise AssertionError('asyncio primitive at import')\n"
        "for name in ('Event','Lock','Semaphore','BoundedSemaphore','Condition','Queue','Future'):\n"
        "    setattr(asyncio, name, boom)\n"
        "import src.services.concurrency as c\n"
        "assert c._counters is None and c._controller is None and c._replay_budget is None\n"
        "c.counters().record('requests')\n"
    )
    subprocess.run([sys.executable, "-c", code], cwd=ROOT, check=True)
