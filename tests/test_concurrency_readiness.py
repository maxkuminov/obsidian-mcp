"""The readiness evaluator (#188, design D6/D8), offline.

`evaluate` is pure, so every criterion's boundary, the minimum windows, the
coverage and single-configuration rules and the both-windows rule for enforce
are asserted on hand-built `WindowStats`. `readiness` (window resolution:
watermark clamp, whole-minute rounding, the last 72 h) is asserted with its two
database reads replaced. The SQL itself runs against real Postgres in
`tests/integration/test_concurrency_readiness_pg.py`.
"""
from __future__ import annotations

import datetime as dt
import json
from types import SimpleNamespace

import pytest

from src.services import concurrency_readiness as cr
from src.services.concurrency_counters import Coverage
from src.services.concurrency_readiness import (
    FAIL,
    INSUFFICIENT,
    PASS,
    ReadinessStats,
    WindowStats,
    evaluate,
)

UTC = dt.timezone.utc
END = dt.datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
EPOCH = "0123456789ab"


def window(days=7, executed=2000, *, end=END, **kw) -> WindowStats:
    kw.setdefault("counters", {"requests": executed})
    kw.setdefault("gauges", {"pool_high_water": 8, "transport_wait_max_ms": 10})
    kw.setdefault("queue_p99_ms", 20.0)
    kw.setdefault("queue_max_ms", 40.0)
    return WindowStats(start=end - dt.timedelta(days=days), end=end, executed=executed,
                       watermark=end, **kw)


def enforce_stats(full=None, recent=None, **kw) -> ReadinessStats:
    full = full or window()
    if recent is None:
        recent = window(days=3, executed=900)
    return ReadinessStats(target="enforce", source_mode="queue", epoch=EPOCH,
                          tool_wait_ms=5000, transport_wait_ms=2000,
                          full=full, recent=recent, **kw)


def queue_stats(full=None, **kw) -> ReadinessStats:
    full = full or window(days=3, executed=300)
    return ReadinessStats(target="queue", source_mode="shadow", epoch=EPOCH,
                          tool_wait_ms=5000, transport_wait_ms=2000, full=full, **kw)


def verdicts(report) -> dict:
    return {c["id"]: c["verdict"] for c in report["criteria"]}


# ── healthy baselines ───────────────────────────────────────────────────


def test_a_healthy_queue_window_passes_every_criterion():
    report = evaluate(queue_stats(), "queue")
    assert verdicts(report) == {"Q1": PASS, "Q2": PASS, "Q3": PASS}
    assert report["overall"] == PASS
    json.dumps(report, default=str)  # the report's JSON line must serialise


def test_a_healthy_enforce_window_passes_every_criterion():
    report = evaluate(enforce_stats(), "enforce")
    assert set(verdicts(report).values()) == {PASS}
    assert report["overall"] == PASS
    # Both windows' inputs are reported.
    assert "window" in report["criteria"][0]["inputs"]
    assert "last_72h" in report["criteria"][0]["inputs"]


def test_an_unknown_target_is_refused():
    with pytest.raises(ValueError):
        evaluate(queue_stats(), "shadow")


# ── Q1–Q3 boundaries ────────────────────────────────────────────────────


@pytest.mark.parametrize("pressured, verdict", [(30, PASS), (31, FAIL)])
def test_q1_boundary_is_ten_percent(pressured, verdict):
    stats = queue_stats(window(days=3, executed=300, tool_pressured=pressured))
    assert verdicts(evaluate(stats, "queue"))["Q1"] == verdict


@pytest.mark.parametrize("pressured, verdict", [(20, PASS), (21, FAIL)])
def test_q2_boundary_is_five_percent_of_counted_requests(pressured, verdict):
    stats = queue_stats(window(days=3, executed=300,
                               counters={"requests": 400, "transport_pressured": pressured}))
    assert verdicts(evaluate(stats, "queue"))["Q2"] == verdict


def test_q2_reads_counters_only_so_row_pressure_cannot_double_count():
    """300 executed calls of which many carry transport observations in their
    rows: Q2 still reads `transport_pressured / requests` from the counters,
    and 12 auth-pressured requests count once."""
    stats = queue_stats(window(days=3, executed=300, tool_pressured=0,
                               counters={"requests": 300, "transport_pressured": 12}))
    report = evaluate(stats, "queue")
    q2 = next(c for c in report["criteria"] if c["id"] == "Q2")
    assert q2["verdict"] == PASS
    assert q2["inputs"] == {"transport_pressured": 12, "requests": 300}


def test_q2_with_no_counted_requests_is_insufficient():
    stats = queue_stats(window(days=3, executed=300, counters={}))
    assert verdicts(evaluate(stats, "queue"))["Q2"] == INSUFFICIENT


def test_one_pool_timeout_fails_q3_and_e6():
    q = queue_stats(window(days=3, executed=300,
                           counters={"requests": 300, "pool_checkout_timeout": 1}))
    assert verdicts(evaluate(q, "queue"))["Q3"] == FAIL
    e = enforce_stats(window(counters={"requests": 2000, "pool_checkout_timeout": 1}))
    report = evaluate(e, "enforce")
    assert verdicts(report)["E6"] == FAIL
    assert report["overall"] == FAIL


# ── E1–E6 boundaries ────────────────────────────────────────────────────


@pytest.mark.parametrize("overruns, verdict", [(2, PASS), (3, FAIL)])
def test_e1_at_two_and_three_of_two_thousand(overruns, verdict):
    stats = enforce_stats(window(executed=2000, tool_overruns=overruns))
    assert verdicts(evaluate(stats, "enforce"))["E1"] == verdict


@pytest.mark.parametrize("overruns, verdict", [(1, PASS), (2, FAIL)])
def test_e1_floor_is_one_call(overruns, verdict):
    stats = enforce_stats(window(executed=1000, tool_overruns=overruns))
    assert verdicts(evaluate(stats, "enforce"))["E1"] == verdict


@pytest.mark.parametrize("cid, metric", [("E2", "transport_overrun"),
                                         ("E3", "writer_overrun")])
def test_e2_e3_any_overrun_fails(cid, metric):
    ok = enforce_stats(window(counters={"requests": 2000, metric: 0}))
    assert verdicts(evaluate(ok, "enforce"))[cid] == PASS
    bad = enforce_stats(window(counters={"requests": 2000, metric: 1}))
    assert verdicts(evaluate(bad, "enforce"))[cid] == FAIL


@pytest.mark.parametrize("p99, verdict", [(500.0, PASS), (500.5, FAIL)])
def test_e4_boundary_is_500_ms(p99, verdict):
    stats = enforce_stats(window(queue_p99_ms=p99, queue_max_ms=max(p99, 40.0)))
    assert verdicts(evaluate(stats, "enforce"))["E4"] == verdict


def test_e4_regression_in_the_last_72h_fails_although_the_window_passes():
    stats = enforce_stats(window(queue_p99_ms=120.0),
                          recent=window(days=3, executed=900, queue_p99_ms=800.0,
                                        queue_max_ms=900.0))
    report = evaluate(stats, "enforce")
    assert verdicts(report)["E4"] == FAIL
    e4 = next(c for c in report["criteria"] if c["id"] == "E4")
    assert e4["inputs"]["window"]["queue_p99_ms"] == 120.0
    assert e4["inputs"]["last_72h"]["queue_p99_ms"] == 800.0


def test_a_quiet_last_72h_is_evidence_not_a_gap():
    quiet = window(days=3, executed=0, queue_p99_ms=None, queue_max_ms=None,
                   counters={}, gauges={})
    assert set(verdicts(evaluate(enforce_stats(recent=quiet), "enforce")).values()) == {PASS}


@pytest.mark.parametrize("tool_max, transport_max, verdict", [
    (2500.0, 1000, PASS),   # exactly half of 5 s and 2 s
    (2500.5, 1000, FAIL),
    (2500.0, 1001, FAIL),
])
def test_e5_boundary_is_half_of_each_deadline(tool_max, transport_max, verdict):
    stats = enforce_stats(window(queue_max_ms=tool_max,
                                 gauges={"pool_high_water": 8,
                                         "transport_wait_max_ms": transport_max}))
    assert verdicts(evaluate(stats, "enforce"))["E5"] == verdict


@pytest.mark.parametrize("high, verdict", [(13, PASS), (14, FAIL)])
def test_e6_high_water_boundary_is_13(high, verdict):
    stats = enforce_stats(window(gauges={"pool_high_water": high,
                                         "transport_wait_max_ms": 0}))
    assert verdicts(evaluate(stats, "enforce"))["E6"] == verdict


# ── windows that do not qualify ─────────────────────────────────────────


@pytest.mark.parametrize("days, executed", [(5, 5000), (7, 999)])
def test_a_thin_enforce_window_is_insufficient_for_every_criterion(days, executed):
    report = evaluate(enforce_stats(window(days=days, executed=executed)), "enforce")
    assert set(verdicts(report).values()) == {INSUFFICIENT}
    assert report["overall"] == INSUFFICIENT


@pytest.mark.parametrize("days, executed", [(2.9, 5000), (3, 299)])
def test_a_thin_queue_window_is_insufficient(days, executed):
    report = evaluate(queue_stats(window(days=days, executed=executed)), "queue")
    assert set(verdicts(report).values()) == {INSUFFICIENT}


def test_a_thin_window_is_insufficient_even_when_it_would_fail():
    """A failing number on too little evidence is not a FAIL verdict either:
    the operator needs a longer window, not a rollback."""
    stats = queue_stats(window(days=1, executed=300, tool_pressured=300))
    assert set(verdicts(evaluate(stats, "queue")).values()) == {INSUFFICIENT}


def test_a_mixed_mode_or_epoch_window_is_insufficient_and_names_the_qualifying_start():
    since = END - dt.timedelta(days=5)
    full = window(foreign=[{"mode": "shadow", "epoch": EPOCH, "rows": 40, "counters": 3,
                            "latest": since - dt.timedelta(seconds=30)}],
                  qualifying_since=since)
    report = evaluate(enforce_stats(full), "enforce")
    assert set(verdicts(report).values()) == {INSUFFICIENT}
    assert report["window"]["qualifying_since"] == since.isoformat()
    assert "qualifying since" in report["criteria"][0]["reason"]


def test_an_uncovered_interval_makes_every_criterion_insufficient():
    """A hard kill with a restart under 180 s: the gap is uncovered however
    short, and every criterion — counter-based ones included — is
    INSUFFICIENT_DATA rather than PASS."""
    gap = (END - dt.timedelta(days=2, seconds=40), END - dt.timedelta(days=2))
    full = window(uncovered=[gap])
    report = evaluate(enforce_stats(full), "enforce")
    assert set(verdicts(report).values()) == {INSUFFICIENT}
    assert "40 s" in report["criteria"][0]["reason"]


def test_an_uncovered_last_72h_is_insufficient():
    recent = window(days=3, executed=900,
                    uncovered=[(END - dt.timedelta(hours=1), END - dt.timedelta(minutes=58))])
    report = evaluate(enforce_stats(recent=recent), "enforce")
    assert set(verdicts(report).values()) == {INSUFFICIENT}


def test_enforce_without_its_last_72h_window_is_insufficient():
    stats = enforce_stats()
    stats.recent = None
    assert set(verdicts(evaluate(stats, "enforce")).values()) == {INSUFFICIENT}


def test_a_problem_is_insufficient_for_every_criterion():
    stats = queue_stats(problem="requested end is past the durable watermark")
    report = evaluate(stats, "queue")
    assert set(verdicts(report).values()) == {INSUFFICIENT}
    assert "watermark" in report["criteria"][0]["reason"]


# ── window resolution (`readiness`) ─────────────────────────────────────


class _Controller:
    epoch = EPOCH
    mode = "queue"

    def configured_wait_ms(self):
        return {"tool": 5000, "transport": 2000}


@pytest.fixture
def resolved(monkeypatch):
    """`readiness` with its database reads replaced: the watermark comes from
    `calls.watermark`, and every `window_stats` call is recorded."""
    calls = SimpleNamespace(watermark=dt.datetime(2026, 9, 30, 12, 0, 40, tzinfo=UTC),
                            windows=[])

    async def fake_coverage(session, start, end, epoch, mode):
        assert (epoch, mode) == (EPOCH, calls.mode)
        return Coverage(calls.watermark, [])

    async def fake_window_stats(session, start, end, *, epoch, mode, user_id=None):
        calls.windows.append((start, end, epoch, mode))
        return window(days=(end - start).total_seconds() / 86400, executed=2000, end=end)

    monkeypatch.setattr(cr, "_coverage", fake_coverage)
    monkeypatch.setattr(cr, "window_stats", fake_window_stats)
    calls.mode = "queue"
    return calls


async def test_the_default_end_is_the_watermark_rounded_down(resolved):
    report = await cr.readiness(None, "enforce", controller=_Controller())
    start, end, epoch, mode = resolved.windows[0]
    assert end == dt.datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
    assert start == end - dt.timedelta(days=7)
    assert (epoch, mode) == (EPOCH, "queue")
    # The last 72 h ends at the same whole minute.
    assert resolved.windows[1][:2] == (end - dt.timedelta(hours=72), end)
    assert report["overall"] == PASS


async def test_an_explicit_end_past_the_watermark_is_insufficient(resolved):
    """An incident after the watermark is unflushed (or merely unproven):
    asking for an end past it certifies nothing."""
    report = await cr.readiness(None, "enforce", controller=_Controller(),
                                end=dt.datetime(2026, 9, 30, 12, 1, tzinfo=UTC))
    assert set(verdicts(report).values()) == {INSUFFICIENT}
    assert "watermark" in report["criteria"][0]["reason"]
    assert resolved.windows == []


async def test_an_explicit_end_is_rounded_down_to_a_whole_minute(resolved):
    await cr.readiness(None, "enforce", controller=_Controller(),
                       end=dt.datetime(2026, 9, 30, 11, 30, 20, tzinfo=UTC))
    assert resolved.windows[0][1] == dt.datetime(2026, 9, 30, 11, 30, tzinfo=UTC)


async def test_no_run_for_the_configuration_is_insufficient(resolved):
    resolved.watermark = None
    report = await cr.readiness(None, "enforce", controller=_Controller())
    assert set(verdicts(report).values()) == {INSUFFICIENT}
    assert "no queue-mode run" in report["criteria"][0]["reason"]


async def test_the_queue_target_reads_shadow_evidence_over_three_days(resolved):
    resolved.mode = "shadow"
    await cr.readiness(None, "queue", controller=_Controller())
    assert len(resolved.windows) == 1
    start, end, _, mode = resolved.windows[0]
    assert mode == "shadow" and end - start == dt.timedelta(days=3)


def test_minute_rounding_helpers():
    t = dt.datetime(2026, 9, 30, 12, 0, 20, tzinfo=UTC)
    assert cr.ceil_minute(t) == dt.datetime(2026, 9, 30, 12, 1, tzinfo=UTC)
    whole = dt.datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
    assert cr.ceil_minute(whole) == whole


# ── the panel section never computes admin data for a non-admin ─────────


async def test_panel_section_computes_nothing_admin_only_for_a_non_admin(monkeypatch):
    seen = {}

    async def fake_table(session, start, end, *, user_id=None, epoch=None, mode=None):
        seen["user_id"] = user_id
        return {"tools": [], "classes": [], "total": None}

    async def forbidden(*a, **k):
        raise AssertionError("admin-only data computed for a non-admin")

    monkeypatch.setattr(cr, "tool_table", fake_table)
    monkeypatch.setattr(cr, "counter_totals", forbidden)
    monkeypatch.setattr(cr, "_coverage", forbidden)
    monkeypatch.setattr(cr, "readiness", forbidden)
    section = await cr.panel_section(None, 86400, user_id=7, is_admin=False,
                                     controller=_Controller())
    assert seen["user_id"] == 7
    assert section["admin"] is None and section["has_data"] is False


# ── `make concurrency-report` ───────────────────────────────────────────


@pytest.mark.parametrize("overall, code", [(PASS, 0), (FAIL, 1), (INSUFFICIENT, 2)])
def test_the_report_exits_zero_only_on_pass(monkeypatch, capsys, overall, code):
    from scripts import concurrency_report as script

    stats = queue_stats()
    report = evaluate(stats, "queue")
    report["overall"] = overall

    async def fake_run(args):
        assert (args.target, args.days, args.end) == ("queue", 4.0, None)
        return report

    monkeypatch.setattr(script, "run", fake_run)
    assert script.main(["--target", "queue", "--days", "4"]) == code
    out = capsys.readouterr().out.strip().splitlines()
    # A table, then exactly one JSON line: the whole verdict.
    assert out[-1].startswith("{") and json.loads(out[-1])["overall"] == overall
    assert any(line.startswith("Q1") for line in out[:-1])
    assert sum(1 for line in out if line.startswith("{")) == 1


@pytest.mark.parametrize("argv", [[], ["--target", "shadow"], ["--target", "queue", "--days", "0"],
                                  ["--target", "queue", "--end", "yesterday"]])
def test_the_report_refuses_bad_usage(argv, capsys):
    from scripts import concurrency_report as script

    assert script.main(argv) == script.EXIT_USAGE


def test_the_report_parses_an_iso_end_as_utc():
    from scripts import concurrency_report as script

    args = script.parse_args(["--target", "enforce", "--end", "2026-09-30T12:00:30"])
    assert args.end == dt.datetime(2026, 9, 30, 12, 0, 30, tzinfo=UTC)
