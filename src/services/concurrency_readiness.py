"""Concurrency readiness: window statistics and the flip verdict (#188, D6/D8).

One evaluator backs both the `/admin/performance` verdict and `make
concurrency-report`, so the two can never disagree:

- `window_stats(session, start, end, …)` is a read-only aggregation over
  `usage_logs` rows carrying `params.concurrency.v = 2` plus the durable
  `concurrency_counters`, with coverage from `concurrency_counters.coverage()`
  over `concurrency_runs`.
- `evaluate(stats, target)` is **pure**: PASS / FAIL / INSUFFICIENT_DATA per
  criterion (Q1–Q3 for target `queue`, E1–E6 for target `enforce`), each with
  the numbers behind it.
- `readiness(session, target, …)` resolves the window (watermark, whole-minute
  rounding, the last 72 h) and runs both.

Rules the numbers obey (design D8, "Coverage and the watermark"):

- **Legacy rows are excluded unconditionally.** Only rows whose
  `params->'concurrency'->>'v'` is `'2'` are read; a pre-change row with
  `queue_ms: 0` is invisible to every count and denominator.
- **One source per numerator.** Tool-stage figures (Q1, E1, E4, E5-tool) come
  only from usage rows; transport, writer and pool figures (Q2, Q3, E2, E3,
  E5-transport, E6) only from counters. The counters already count each request
  once, by its worst outcome, so nothing is double-counted.
- **Every boundary is a whole minute.** The start is rounded up and the end
  rounded down — a clean-shutdown end included (SR3-1): a minute bucket is keyed
  by `(minute, epoch, mode)`, not by run, so an exact end could pull a later
  run's incident in. Counter buckets are therefore either wholly inside a
  window or wholly outside it.
- **The end is the durable watermark.** The default end is the latest
  `completed_through` of the evaluated epoch and mode, rounded down; an
  explicitly requested end past it is INSUFFICIENT_DATA, so an unflushed tail
  is never certified.
- **A window qualifies only when covered, single-mode and single-epoch.** Any
  uncovered interval (a gap after an unclean run end, however short; a lossy
  run; a run of another configuration) or any v2 row or counter of another
  mode or epoch makes **every** criterion INSUFFICIENT_DATA, and the latest
  qualifying sub-window start is reported.

The epoch evaluated is the **configured** one (`Controller.epoch`): evidence is
only evidence for the settings about to run, and a mode change keeps the epoch.
"""
from __future__ import annotations

import datetime as _dt
import json
from dataclasses import dataclass, field
from fractions import Fraction

from sqlalchemy import text

from src.services import concurrency
from src.services.concurrency import TOOL_CLASSES, floor_minute
from src.services.concurrency_counters import coverage as _coverage
from src.services.usage_stats import (
    PRE_BODY_REFUSAL_BINDS,
    executed_sql,
    refusal_weight_sql,
)

UTC = _dt.timezone.utc
MINUTE = _dt.timedelta(minutes=1)

PASS = "PASS"
FAIL = "FAIL"
INSUFFICIENT = "INSUFFICIENT_DATA"

TARGETS = ("queue", "enforce")
#: The mode whose evidence qualifies each target (design §Rollout).
SOURCE_MODE = {"queue": "shadow", "enforce": "queue"}
#: The mode the panel evaluates next, given the running mode.
NEXT_TARGET = {"shadow": "queue", "queue": "enforce"}

# ── thresholds (design §Rollout, Step 1 and Step 2) ───────────────────────
#: Step 1 minimum window: ≥ 3 days and ≥ 300 executed calls.
QUEUE_MIN_DAYS = 3
QUEUE_MIN_EXECUTED = 300
#: Q1 — tool-pressured executed calls / executed calls ≤ 10 %.
Q1_MAX_RATIO = Fraction(10, 100)
#: Q2 — `transport_pressured` / `requests` ≤ 5 %.
Q2_MAX_RATIO = Fraction(5, 100)
#: Step 2 minimum window: ≥ 7 days and ≥ 1,000 executed calls.
ENFORCE_MIN_DAYS = 7
ENFORCE_MIN_EXECUTED = 1000
#: Step 2 criteria must also hold over the window's last 72 hours.
RECENT_HOURS = 72
#: E1 — tool-overrun calls ≤ max(1, 0.1 % of executed calls).
E1_FLOOR = 1
E1_MAX_RATIO = Fraction(1, 1000)
#: E4 — tool `queue_ms` p99 ≤ 500 ms.
E4_MAX_P99_MS = 500
#: E5 — max tool `queue_ms` and max `transport_wait_max_ms` ≤ 50 % of their deadline.
E5_MAX_FRACTION = Fraction(1, 2)
#: E6 — maximum `pool_high_water` ≤ 13 (of the 15-connection pool).
E6_MAX_HIGH_WATER = 13

MIN_DAYS = {"queue": QUEUE_MIN_DAYS, "enforce": ENFORCE_MIN_DAYS}
MIN_EXECUTED = {"queue": QUEUE_MIN_EXECUTED, "enforce": ENFORCE_MIN_EXECUTED}
CRITERIA = {"queue": ("Q1", "Q2", "Q3"),
            "enforce": ("E1", "E2", "E3", "E4", "E5", "E6")}
DESCRIPTIONS = {
    "Q1": "Tool-pressured executed calls ≤ 10 % of executed calls (rows)",
    "Q2": "transport_pressured ≤ 5 % of requests (counters)",
    "Q3": "No pool checkout timeout (counters)",
    "E1": "Tool-overrun calls ≤ max(1, 0.1 % of executed calls) (rows)",
    "E2": "No transport_overrun (counters)",
    "E3": "No writer_overrun (counters)",
    "E4": "Tool queue_ms p99 ≤ 500 ms (rows)",
    "E5": "Max tool queue_ms and max transport wait ≤ 50 % of their deadlines",
    "E6": "No pool checkout timeout; pool high-water ≤ 13 (counters)",
}


# ── time helpers ──────────────────────────────────────────────────────────


def _utc(at: _dt.datetime) -> _dt.datetime:
    if at.tzinfo is None:
        return at.replace(tzinfo=UTC)
    return at.astimezone(UTC)


def ceil_minute(at: _dt.datetime) -> _dt.datetime:
    floored = floor_minute(at)
    return floored if floored == _utc(at) else floored + MINUTE


def _iso(at):
    return None if at is None else _utc(at).isoformat()


# ── SQL fragments ─────────────────────────────────────────────────────────
#
# Every fragment is NULL-safe (`COALESCE(…, false)`) and cast-guarded: `params`
# is JSONB this module does not control, and an unguarded cast on a non-number
# aborts the statement rather than returning NULL.

_V2 = "(ul.params->'concurrency'->>'v') = '2'"
_ROW_MODE = "(ul.params->'concurrency'->>'mode')"
_ROW_EPOCH = "(ul.params->'concurrency'->>'epoch')"
# Tool pressure is read from the observations, never from `code`: a shadow row
# whose tool observation is not the first one still counts (spec, "Tool
# pressure is counted from observations").
_TOOL_PRESSURED = (
    "COALESCE(ul.params->'concurrency_shadow'->'observations'"
    " @> CAST('[{\"stage\": \"tool\"}]' AS jsonb), false)"
)
_TOOL_OVERRUN = (
    "COALESCE(ul.params->'concurrency_queue'->'observations'"
    " @> CAST('[{\"stage\": \"tool\", \"overrun\": true}]' AS jsonb), false)"
)
_QUEUE_MS = (
    "CASE WHEN jsonb_typeof(ul.params->'queue_ms') = 'number'"
    " THEN (ul.params->>'queue_ms')::double precision END"
)
_SLOT_TIMEOUT = "COALESCE(ul.params->>'error' = :slot_timeout_marker, false)"
_WINDOW = "ul.created_at >= :start AND ul.created_at < :end"


def _class_map() -> str:
    return json.dumps(TOOL_CLASSES, sort_keys=True)


# ── the statistics ────────────────────────────────────────────────────────


@dataclass
class WindowStats:
    """Everything `evaluate` reads about one window. Built by `window_stats`
    or directly by tests (it is plain data)."""

    start: _dt.datetime
    end: _dt.datetime
    executed: int = 0
    tool_pressured: int = 0
    tool_overruns: int = 0
    queue_p99_ms: float | None = None
    queue_max_ms: float | None = None
    #: metric → summed count for the evaluated epoch and mode.
    counters: dict = field(default_factory=dict)
    #: gauge metric → bucket maximum over the window (None: no sample).
    gauges: dict = field(default_factory=dict)
    #: per-tool, per-class and total rows for display.
    tools: list = field(default_factory=list)
    classes: list = field(default_factory=list)
    watermark: _dt.datetime | None = None
    uncovered: list = field(default_factory=list)
    #: `{"mode", "epoch", "rows", "counters", "latest"}` for every v2 row or
    #: counter set in the window whose configuration is not the evaluated one.
    foreign: list = field(default_factory=list)
    modes_present: list = field(default_factory=list)
    epochs_present: list = field(default_factory=list)
    qualifying_since: _dt.datetime | None = None

    def count(self, metric: str) -> int:
        return int(self.counters.get(metric, 0) or 0)

    def as_dict(self) -> dict:
        return {
            "start": _iso(self.start), "end": _iso(self.end),
            "executed": self.executed, "tool_pressured": self.tool_pressured,
            "tool_overruns": self.tool_overruns,
            "queue_p99_ms": self.queue_p99_ms, "queue_max_ms": self.queue_max_ms,
            "counters": dict(self.counters), "gauges": dict(self.gauges),
            "watermark": _iso(self.watermark),
            "uncovered": [[_iso(a), _iso(b)] for a, b in self.uncovered],
            "foreign": [dict(f, latest=_iso(f.get("latest"))) for f in self.foreign],
            "modes_present": list(self.modes_present),
            "epochs_present": list(self.epochs_present),
            "qualifying_since": _iso(self.qualifying_since),
        }


def _num(value):
    return None if value is None else float(value)


def _tool_row(r) -> dict:
    return {
        "tool": r.tool,
        "cls": r.cls or "unclassified",
        "executed": int(r.executed or 0),
        "tool_pressured": int(r.tool_pressured or 0),
        "overruns": int(r.overruns or 0),
        "slot_timeouts": int(r.slot_timeouts or 0),
        "p50": _num(r.p50), "p95": _num(r.p95), "p99": _num(r.p99),
        "max": _num(r.qmax),
    }


async def tool_table(session, start, end, *, user_id=None, epoch=None, mode=None) -> dict:
    """Per-tool, per-class and whole-window aggregates over v2 rows in
    `[start, end)`.

    `user_id` scopes to one user's rows (the panel's non-admin view); `epoch`
    and `mode` restrict to one configuration (the evaluator). Executed and
    pre-body classification is the existing `executed_sql`; `slot_timeout`
    refusals are weighted `1 + suppressed` through the existing guarded cast.
    `queue_ms` percentiles are over executed calls only; the **maximum** is
    over every row carrying a numeric `queue_ms`, whatever its later outcome
    (E5, impl-R2-1): a call that waited at the tool stage and was then refused
    by quota still waited that long, and enforce would have made it wait too.
    Tool overruns count every row carrying one for the same reason: a call
    that overran and was then refused by quota was still a call enforce would
    have refused.
    """
    executed = executed_sql()
    weight = refusal_weight_sql()
    where = [_V2, _WINDOW]
    params: dict = {
        "start": _utc(start), "end": _utc(end), "class_map": _class_map(),
        "slot_timeout_marker": "slot_timeout", **PRE_BODY_REFUSAL_BINDS,
    }
    if user_id is not None:
        where.append("ul.user_id = :uid")
        params["uid"] = user_id
    if epoch is not None:
        where.append(f"{_ROW_EPOCH} = :epoch")
        params["epoch"] = epoch
    if mode is not None:
        where.append(f"{_ROW_MODE} = :mode")
        params["mode"] = mode
    sql = f"""
        WITH r AS (
            SELECT
                ul.tool AS tool,
                CAST(CAST(:class_map AS text) AS jsonb) ->> ul.tool AS cls,
                {executed} AS executed,
                {_TOOL_PRESSURED} AS tool_pressured,
                {_TOOL_OVERRUN} AS tool_overrun,
                {_SLOT_TIMEOUT} AS slot_timeout,
                {weight} AS weight,
                {_QUEUE_MS} AS queue_ms
            FROM usage_logs ul
            WHERE {' AND '.join(where)}
        )
        SELECT
            r.cls AS cls, r.tool AS tool,
            GROUPING(r.cls) AS g_cls, GROUPING(r.tool) AS g_tool,
            count(*) FILTER (WHERE r.executed) AS executed,
            count(*) FILTER (WHERE r.executed AND r.tool_pressured) AS tool_pressured,
            count(*) FILTER (WHERE r.tool_overrun) AS overruns,
            COALESCE(sum(r.weight) FILTER (WHERE r.slot_timeout), 0) AS slot_timeouts,
            percentile_cont(0.5) WITHIN GROUP (ORDER BY r.queue_ms)
                FILTER (WHERE r.executed AND r.queue_ms IS NOT NULL) AS p50,
            percentile_cont(0.95) WITHIN GROUP (ORDER BY r.queue_ms)
                FILTER (WHERE r.executed AND r.queue_ms IS NOT NULL) AS p95,
            percentile_cont(0.99) WITHIN GROUP (ORDER BY r.queue_ms)
                FILTER (WHERE r.executed AND r.queue_ms IS NOT NULL) AS p99,
            max(r.queue_ms) AS qmax
        FROM r
        GROUP BY GROUPING SETS ((r.cls, r.tool), (r.cls), ())
    """
    rows = (await session.execute(text(sql), params)).fetchall()
    tools, classes, total = [], [], None
    for r in rows:
        if r.g_cls and r.g_tool:
            # The grand total. An empty window still yields it (zero counts).
            total = _tool_row(r)
        elif r.g_tool:
            classes.append(_tool_row(r))
        else:
            tools.append(_tool_row(r))
    tools.sort(key=lambda t: (-t["executed"], t["tool"] or ""))
    classes.sort(key=lambda c: c["cls"])
    if total is None or not (tools or classes):
        total = None
    return {"tools": tools, "classes": classes, "total": total}


async def counter_totals(session, start, end, *, epoch=None, mode=None) -> tuple[dict, dict]:
    """`({count metric: sum}, {gauge metric: max})` over buckets in `[start, end)`.

    Boundaries are whole minutes, so a bucket is wholly in or wholly out.
    """
    where = ["bucket_start >= :start", "bucket_start < :end"]
    params: dict = {"start": _utc(start), "end": _utc(end)}
    if epoch is not None:
        where.append("epoch = :epoch")
        params["epoch"] = epoch
    if mode is not None:
        where.append("mode = :mode")
        params["mode"] = mode
    rows = (await session.execute(text(f"""
        SELECT metric, sum(count) AS n, max(max_value) AS mx
        FROM concurrency_counters WHERE {' AND '.join(where)}
        GROUP BY metric
    """), params)).fetchall()
    counts, gauges = {}, {}
    for r in rows:
        if r.metric in concurrency.GAUGE_METRICS:
            gauges[r.metric] = None if r.mx is None else int(r.mx)
        else:
            counts[r.metric] = int(r.n or 0)
    return counts, gauges


async def configurations_present(session, start, end) -> list[dict]:
    """Every `(mode, epoch)` among v2 rows and counters in `[start, end)`,
    with how many of each and the latest instant it touches. Global: whether a
    window qualifies is never a per-user question."""
    params = {"start": _utc(start), "end": _utc(end)}
    found: dict = {}
    rows = (await session.execute(text(f"""
        SELECT {_ROW_MODE} AS mode, {_ROW_EPOCH} AS epoch,
               count(*) AS n, max(ul.created_at) AS latest
        FROM usage_logs ul
        WHERE {_V2} AND {_WINDOW}
        GROUP BY 1, 2
    """), params)).fetchall()
    for r in rows:
        entry = found.setdefault((r.mode, r.epoch), {"mode": r.mode, "epoch": r.epoch,
                                                     "rows": 0, "counters": 0, "latest": None})
        entry["rows"] += int(r.n or 0)
        entry["latest"] = _later(entry["latest"], r.latest)
    buckets = (await session.execute(text("""
        SELECT mode, epoch, count(*) AS n, max(bucket_start) AS latest
        FROM concurrency_counters
        WHERE bucket_start >= :start AND bucket_start < :end
        GROUP BY 1, 2
    """), params)).fetchall()
    for r in buckets:
        entry = found.setdefault((r.mode, r.epoch), {"mode": r.mode, "epoch": r.epoch,
                                                     "rows": 0, "counters": 0, "latest": None})
        entry["counters"] += int(r.n or 0)
        # A bucket speaks for its whole minute.
        entry["latest"] = _later(entry["latest"],
                                 None if r.latest is None else _utc(r.latest) + MINUTE)
    return sorted(found.values(), key=lambda e: (str(e["mode"]), str(e["epoch"])))


def _later(a, b):
    if a is None:
        return None if b is None else _utc(b)
    if b is None:
        return a
    return max(_utc(a), _utc(b))


async def window_stats(session, start, end, *, epoch: str, mode: str,
                       user_id=None) -> WindowStats:
    """The statistics for `[start, end)` under one `(epoch, mode)`.

    `start` is rounded up and `end` down to whole minutes. Rows and counters
    of any other configuration do not enter a figure; they are reported in
    `foreign` and push `qualifying_since` past themselves.
    """
    start, end = ceil_minute(start), floor_minute(end)
    stats = WindowStats(start=start, end=end)
    if end <= start:
        cov = await _coverage(session, start, start, epoch, mode)
        stats.watermark = cov.watermark
        return stats
    table = await tool_table(session, start, end, user_id=user_id, epoch=epoch, mode=mode)
    stats.tools, stats.classes = table["tools"], table["classes"]
    total = table["total"]
    if total is not None:
        stats.executed = total["executed"]
        stats.tool_pressured = total["tool_pressured"]
        stats.tool_overruns = total["overruns"]
        stats.queue_p99_ms = total["p99"]
        stats.queue_max_ms = total["max"]
    stats.counters, stats.gauges = await counter_totals(
        session, start, end, epoch=epoch, mode=mode)
    cov = await _coverage(session, start, end, epoch, mode)
    stats.watermark = cov.watermark
    stats.uncovered = [(_utc(a), _utc(b)) for a, b in cov.uncovered]
    present = await configurations_present(session, start, end)
    stats.modes_present = sorted({str(p["mode"]) for p in present})
    stats.epochs_present = sorted({str(p["epoch"]) for p in present})
    stats.foreign = [p for p in present if (p["mode"], p["epoch"]) != (mode, epoch)]
    barrier = None
    for p in stats.foreign:
        barrier = _later(barrier, p["latest"])
    for _, hi in stats.uncovered:
        barrier = _later(barrier, hi)
    stats.qualifying_since = start if barrier is None else min(end, max(start, ceil_minute(barrier)))
    return stats


# ── the evaluator ─────────────────────────────────────────────────────────


@dataclass
class ReadinessStats:
    """The evaluator's whole input. `full` is the evaluated window; `recent`
    its last 72 h (target `enforce` only). `problem` names a reason no window
    could be evaluated at all (no run for this configuration, an end past the
    watermark, an unknown target)."""

    target: str
    source_mode: str
    epoch: str
    tool_wait_ms: int
    transport_wait_ms: int
    full: WindowStats | None = None
    recent: WindowStats | None = None
    problem: str | None = None
    requested_days: float | None = None


def _criterion(cid, verdict, reason, **inputs):
    return {"id": cid, "description": DESCRIPTIONS[cid], "verdict": verdict,
            "reason": reason, "inputs": inputs}


def _pct(fr: Fraction) -> str:
    return f"{float(fr) * 100:.2f} %"


def _check_window(w: WindowStats, target: str, stats: ReadinessStats) -> dict:
    """Per-criterion `(verdict, reason, inputs)` for one qualifying window."""
    out = {}
    if target == "queue":
        if w.executed == 0:
            out["Q1"] = (INSUFFICIENT, "no executed calls", {})
        else:
            ratio = Fraction(w.tool_pressured, w.executed)
            out["Q1"] = (PASS if ratio <= Q1_MAX_RATIO else FAIL,
                         f"{w.tool_pressured} of {w.executed} executed calls tool-pressured"
                         f" ({_pct(ratio)}; limit 10 %)",
                         {"tool_pressured": w.tool_pressured, "executed": w.executed})
        requests, pressured = w.count("requests"), w.count("transport_pressured")
        if requests == 0:
            out["Q2"] = (INSUFFICIENT, "no requests counted", {"requests": 0})
        else:
            ratio = Fraction(pressured, requests)
            out["Q2"] = (PASS if ratio <= Q2_MAX_RATIO else FAIL,
                         f"{pressured} of {requests} requests transport-pressured"
                         f" ({_pct(ratio)}; limit 5 %)",
                         {"transport_pressured": pressured, "requests": requests})
        timeouts = w.count("pool_checkout_timeout")
        out["Q3"] = (PASS if timeouts == 0 else FAIL,
                     f"{timeouts} pool checkout timeout(s)",
                     {"pool_checkout_timeout": timeouts})
        return out

    allowed = max(Fraction(E1_FLOOR), E1_MAX_RATIO * w.executed)
    out["E1"] = (PASS if w.tool_overruns <= allowed else FAIL,
                 f"{w.tool_overruns} tool-overrun call(s) of {w.executed} executed"
                 f" (allowed {float(allowed):g})",
                 {"tool_overruns": w.tool_overruns, "executed": w.executed,
                  "allowed": float(allowed)})
    for cid, metric in (("E2", "transport_overrun"), ("E3", "writer_overrun")):
        n = w.count(metric)
        out[cid] = (PASS if n == 0 else FAIL, f"{n} {metric}", {metric: n})
    if w.queue_p99_ms is None:
        # A covered, quiet sub-window is evidence of no queueing, not a gap.
        out["E4"] = (PASS, "no executed call recorded queue_ms", {"queue_p99_ms": None})
    else:
        out["E4"] = (PASS if w.queue_p99_ms <= E4_MAX_P99_MS else FAIL,
                     f"tool queue_ms p99 {w.queue_p99_ms:.0f} ms (limit {E4_MAX_P99_MS} ms)",
                     {"queue_p99_ms": w.queue_p99_ms})
    tool_max = w.queue_max_ms or 0.0
    transport_max = w.gauges.get("transport_wait_max_ms") or 0
    ok = True
    parts = []
    for label, value, deadline in (("tool", tool_max, stats.tool_wait_ms),
                                   ("transport", transport_max, stats.transport_wait_ms)):
        # Exact: `Fraction(float)` is the float's exact value.
        fits = Fraction(value) <= E5_MAX_FRACTION * deadline
        parts.append(f"{label} max {value:.0f} ms of {deadline} ms deadline")
        ok = ok and fits
    out["E5"] = (PASS if ok else FAIL, "; ".join(parts) + " (limit 50 % each)",
                 {"tool_queue_max_ms": tool_max, "tool_wait_ms": stats.tool_wait_ms,
                  "transport_wait_max_ms": transport_max,
                  "transport_wait_ms": stats.transport_wait_ms})
    timeouts = w.count("pool_checkout_timeout")
    high = w.gauges.get("pool_high_water")
    ok = timeouts == 0 and (high is None or high <= E6_MAX_HIGH_WATER)
    out["E6"] = (PASS if ok else FAIL,
                 f"{timeouts} pool checkout timeout(s); high-water "
                 f"{'—' if high is None else high} (limit {E6_MAX_HIGH_WATER})",
                 {"pool_checkout_timeout": timeouts, "pool_high_water": high})
    return out


def _disqualified(w: WindowStats) -> str | None:
    if w.uncovered:
        spans = ", ".join(f"{_iso(a)} for {_duration(b - a)}" for a, b in w.uncovered[:3])
        more = "" if len(w.uncovered) <= 3 else f" and {len(w.uncovered) - 3} more"
        return f"window not covered: uncovered {spans}{more}"
    if w.foreign:
        names = ", ".join(f"{f['mode']}/{f['epoch']}" for f in w.foreign[:3])
        return f"window mixes configurations ({names}); qualifying since {_iso(w.qualifying_since)}"
    return None


def _duration(delta: _dt.timedelta) -> str:
    seconds = int(delta.total_seconds())
    if seconds < 120:
        return f"{seconds} s"
    if seconds < 7200:
        return f"{seconds // 60} min"
    return f"{seconds / 3600:.1f} h"


def evaluate(stats: ReadinessStats, target: str) -> dict:
    """PASS / FAIL / INSUFFICIENT_DATA per criterion, with inputs. Pure.

    INSUFFICIENT_DATA for **every** criterion when the window cannot be
    evaluated, is uncovered, mixes configurations, or is thinner than the
    target's minimum (days or executed calls). For `enforce`, each criterion
    must hold over the whole window **and** its last 72 h: FAIL in either is
    FAIL.
    """
    if target not in TARGETS:
        raise ValueError(f"unknown target {target!r}; expected one of {TARGETS}")
    ids = CRITERIA[target]
    w = stats.full
    blocker = stats.problem
    if blocker is None and w is None:
        blocker = "no window"
    if blocker is None:
        blocker = _disqualified(w)
    if blocker is None and target == "enforce":
        blocker = ("no last-72 h window" if stats.recent is None
                   else _disqualified(stats.recent))
    if blocker is None:
        days = (w.end - w.start).total_seconds() / 86400
        if days + 1e-9 < MIN_DAYS[target]:
            blocker = f"window is {days:.2f} days; {target} needs ≥ {MIN_DAYS[target]}"
        elif w.executed < MIN_EXECUTED[target]:
            blocker = (f"window holds {w.executed} executed calls; {target} needs"
                       f" ≥ {MIN_EXECUTED[target]}")

    if blocker is not None:
        criteria = [_criterion(cid, INSUFFICIENT, blocker) for cid in ids]
    else:
        full = _check_window(w, target, stats)
        recent = _check_window(stats.recent, target, stats) if target == "enforce" else None
        criteria = []
        for cid in ids:
            verdict, reason, inputs = full[cid]
            if recent is None:
                criteria.append(_criterion(cid, verdict, reason, **inputs))
                continue
            r_verdict, r_reason, r_inputs = recent[cid]
            if FAIL in (verdict, r_verdict):
                combined = FAIL
            elif INSUFFICIENT in (verdict, r_verdict):
                combined = INSUFFICIENT
            else:
                combined = PASS
            criteria.append(_criterion(
                cid, combined, f"window: {reason}; last {RECENT_HOURS} h: {r_reason}",
                window=inputs, last_72h=r_inputs))

    verdicts = {c["verdict"] for c in criteria}
    overall = (PASS if verdicts == {PASS} else FAIL if FAIL in verdicts else INSUFFICIENT)
    return {
        "target": target,
        "source_mode": stats.source_mode,
        "epoch": stats.epoch,
        "overall": overall,
        "window": None if w is None else {
            **w.as_dict(),
            "days": round((w.end - w.start).total_seconds() / 86400, 4),
        },
        "last_72h": None if stats.recent is None else stats.recent.as_dict(),
        "criteria": criteria,
    }


# ── resolving a window and running both ───────────────────────────────────


async def readiness(session, target: str, *, days: float | None = None,
                    end: _dt.datetime | None = None, controller=None) -> dict:
    """Resolve the evaluation window for `target` and evaluate it.

    - The configuration is the running one: `controller.epoch`, and the
      source mode for `target` (shadow for queue, queue for enforce).
    - The default end is the durable watermark rounded down; an explicit
      `end` is rounded down too, and past the rounded watermark it gives
      INSUFFICIENT_DATA (an unflushed tail is never certified).
    - `days` defaults to the target's minimum window.
    """
    if target not in TARGETS:
        raise ValueError(f"unknown target {target!r}; expected one of {TARGETS}")
    controller = controller or concurrency.get_controller()
    waits = controller.configured_wait_ms()
    source = SOURCE_MODE[target]
    epoch = controller.epoch
    days = float(days if days is not None else MIN_DAYS[target])
    stats = ReadinessStats(target=target, source_mode=source, epoch=epoch,
                           tool_wait_ms=int(waits["tool"]),
                           transport_wait_ms=int(waits["transport"]),
                           requested_days=days)
    probe = floor_minute(_dt.datetime.now(UTC))
    watermark = (await _coverage(session, probe, probe, epoch, source)).watermark
    if watermark is None:
        stats.problem = f"no {source}-mode run recorded for epoch {epoch}"
        return evaluate(stats, target)
    limit = floor_minute(watermark)
    if end is None:
        window_end = limit
    else:
        window_end = floor_minute(end)
        if window_end > limit:
            stats.problem = (f"requested end {_iso(window_end)} is past the durable"
                             f" watermark {_iso(limit)}")
            stats.full = WindowStats(start=window_end, end=window_end, watermark=watermark)
            return evaluate(stats, target)
    window_start = ceil_minute(window_end - _dt.timedelta(days=days))
    stats.full = await window_stats(session, window_start, window_end, epoch=epoch, mode=source)
    if target == "enforce":
        recent_start = max(window_start, window_end - _dt.timedelta(hours=RECENT_HOURS))
        stats.recent = await window_stats(session, recent_start, window_end,
                                          epoch=epoch, mode=source)
    return evaluate(stats, target)


# ── the panel section ─────────────────────────────────────────────────────


async def panel_section(session, window_seconds: int, *, user_id, is_admin: bool,
                        now: _dt.datetime | None = None, controller=None) -> dict:
    """The `/admin/performance` concurrency section.

    Everyone: per-tool and per-class aggregates over v2 rows in the page's
    window, scoped as the page scopes (`user_id` None = every row). Admins
    also get the live snapshot, the window's durable counters and coverage
    gaps under the running configuration, and the verdict for the next mode —
    evaluated over max(page window, the target's minimum) ending at the
    watermark, so a gap in the page window is inside the verdict window too.
    Nothing admin-only is even computed for a non-admin.
    """
    now = _utc(now or _dt.datetime.now(UTC))
    start = now - _dt.timedelta(seconds=window_seconds)
    table = await tool_table(session, start, now, user_id=user_id)
    section: dict = {
        "tools": table["tools"],
        "classes": table["classes"],
        "has_data": bool(table["tools"]),
        "admin": None,
    }
    if not is_admin:
        return section
    controller = controller or concurrency.get_controller()
    snap = controller.snapshot()
    mode, epoch = snap["mode"], snap["epoch"]
    counts, gauges = await counter_totals(session, floor_minute(start), now,
                                          epoch=epoch, mode=mode)
    cov = await _coverage(session, start, start, epoch, mode)
    gaps: list = []
    if cov.watermark is not None:
        through = floor_minute(cov.watermark)
        if through > start:
            gaps = (await _coverage(session, start, through, epoch, mode)).uncovered
    target = NEXT_TARGET.get(mode)
    verdict = None
    if target is not None:
        days = max(window_seconds / 86400, MIN_DAYS[target])
        verdict = await readiness(session, target, days=days, controller=controller)
    section["admin"] = {
        "snapshot": snap,
        "counters": [{"metric": m, "count": counts.get(m, 0)}
                     for m in concurrency.COUNT_METRICS],
        "gauges": [{"metric": m, "max": gauges.get(m)} for m in concurrency.GAUGE_METRICS],
        "watermark": _iso(cov.watermark),
        "gaps": [{"start": _iso(a), "length": _duration(_utc(b) - _utc(a))}
                 for a, b in gaps],
        "target": target,
        "verdict": verdict,
    }
    return section
