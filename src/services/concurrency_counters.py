"""Durable concurrency counters, run registration and coverage (design D8, #188).

The in-process accumulator (`concurrency.counters()`) is keyed by **event-time
minute**. This module turns it into durable evidence:

- `register_run()` — at lifespan start: mints a `run_id` and inserts the run's
  `concurrency_runs` row with `completed_through = started_at`.
- `flush(clean=False)` — every 60 s (`run_flush_loop`) and once at shutdown.
  One transaction: read `t`, drain the accumulator, one multi-row
  `INSERT … ON CONFLICT DO UPDATE` keyed by the drained event-time buckets
  (count added, `max_value` the greater), and upsert the run row with
  `completed_through = floor_minute(t)` — or exactly `t` plus
  `clean_shutdown = true` when `clean` — and the accumulator's `lossy` flag.
  A failure merges the drained entries back **under their original keys** and
  does not advance the watermark. The commit is synchronous (durable on
  return): this is one statement a minute, and it deliberately does not join
  the asynchronous-commit allow-list of #279.
- `prune()` — counters and runs older than 35 days.
- `coverage(session, start, end, epoch, mode)` — the watermark and the
  uncovered intervals of a window, for the readiness evaluator (S4).

Why `completed_through = floor_minute(t)` is a watermark: every event before
`t` was recorded before the drain, so every bucket that ends by
`floor_minute(t)` is complete and durable once the transaction commits.
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import logging
import uuid
from dataclasses import dataclass
from typing import NamedTuple

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from src.database import async_session
from src.models.db import ConcurrencyCounter, ConcurrencyRun
from src.services import concurrency
from src.services.concurrency import floor_minute

log = logging.getLogger(__name__)

FLUSH_INTERVAL_SECONDS = 60
PRUNE_AFTER = _dt.timedelta(days=35)
# Pruning is housekeeping, not part of the watermark: at most once an hour, in
# its own transaction, so a failed prune never rolls back a flush.
PRUNE_INTERVAL = _dt.timedelta(hours=1)
_INT4_MAX = 2**31 - 1


@dataclass
class Run:
    run_id: uuid.UUID
    epoch: str
    mode: str
    started_at: _dt.datetime


_run: Run | None = None
_last_prune: _dt.datetime | None = None


def _now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def _utc(at: _dt.datetime) -> _dt.datetime:
    if at.tzinfo is None:
        return at.replace(tzinfo=_dt.timezone.utc)
    return at.astimezone(_dt.timezone.utc)


def current_run() -> Run | None:
    return _run


def reset_for_tests() -> None:
    """Test boundary only: forget the process's run (a simulated process exit)."""
    global _run, _last_prune
    _run = None
    _last_prune = None


def _run_upsert(run: Run, completed_through: _dt.datetime, *, clean: bool, lossy: bool):
    """Insert-or-advance the run row. The watermark never moves backwards
    (`GREATEST`), and `clean_shutdown` / `lossy` are sticky."""
    table = ConcurrencyRun.__table__
    stmt = pg_insert(table).values(
        run_id=run.run_id,
        epoch=run.epoch,
        mode=run.mode,
        started_at=run.started_at,
        completed_through=completed_through,
        clean_shutdown=clean,
        lossy=lossy,
    )
    excluded = stmt.excluded
    return stmt.on_conflict_do_update(
        index_elements=[table.c.run_id],
        set_={
            "completed_through": func.greatest(
                table.c.completed_through, excluded.completed_through
            ),
            "clean_shutdown": table.c.clean_shutdown | excluded.clean_shutdown,
            "lossy": table.c.lossy | excluded.lossy,
        },
    )


def _counter_upsert(run: Run, drained: dict):
    """One multi-row upsert for every drained `(minute, metric)` — bounded by
    the accumulator's cap (60 minutes × 10 metrics), never by request volume."""
    table = ConcurrencyCounter.__table__
    rows = [
        {
            "bucket_start": minute,
            "epoch": run.epoch,
            "mode": run.mode,
            "metric": metric,
            "count": int(count),
            # `integer` column: clamp, so one absurd gauge sample can never
            # make every later flush fail on the same drained entry.
            "max_value": (
                None if value is None else max(-_INT4_MAX, min(_INT4_MAX, int(value)))
            ),
        }
        for (minute, metric), (count, value) in sorted(
            drained.items(), key=lambda kv: (kv[0][0], kv[0][1])
        )
    ]
    stmt = pg_insert(table).values(rows)
    excluded = stmt.excluded
    return stmt.on_conflict_do_update(
        index_elements=[table.c.bucket_start, table.c.epoch, table.c.mode, table.c.metric],
        set_={
            "count": table.c["count"] + excluded["count"],
            # PostgreSQL's GREATEST ignores NULLs, so a count metric keeps NULL
            # and a gauge keeps the larger of the two maxima.
            "max_value": func.greatest(table.c.max_value, excluded.max_value),
        },
    )


async def register_run(
    *, controller=None, now: _dt.datetime | None = None, session_factory=None
) -> Run:
    """Mint this process's run and insert its row (`completed_through =
    started_at`). A failed insert is logged, not raised: the run stays minted
    and the next flush upserts the same row, so serving never waits on it."""
    global _run
    controller = controller or concurrency.get_controller()
    started = _utc(now or _now())
    run = Run(uuid.uuid4(), controller.epoch, controller.mode, started)
    _run = run
    factory = session_factory or async_session
    try:
        async with factory() as session:
            await session.execute(_run_upsert(run, started, clean=False, lossy=False))
            await session.commit()
    except Exception as e:  # noqa: BLE001 - counters never block startup
        log.error("Registering the concurrency run failed (%s); the next flush retries",
                  type(e).__name__)
    return run


async def flush(
    clean: bool = False, *, now: _dt.datetime | None = None, session_factory=None
) -> bool:
    """Drain the accumulator into `concurrency_counters` and advance the run's
    watermark, in one transaction. Returns whether it committed."""
    run = _run
    if run is None:
        return False
    t = _utc(now or _now())
    acc = concurrency.counters()
    drained = acc.drain()
    lossy = bool(acc.lossy)
    completed_through = t if clean else floor_minute(t)
    # No await between reading `t` and the drain: every recorder runs on the
    # event loop's thread (the pool hook included, under SQLAlchemy's
    # greenlet), so nothing can be recorded "before t" and miss this drain.
    factory = session_factory or async_session
    committed = False
    try:
        async with factory() as session:
            if drained:
                await session.execute(_counter_upsert(run, drained))
            await session.execute(
                _run_upsert(run, completed_through, clean=clean, lossy=lossy)
            )
            await session.commit()
            committed = True
    except BaseException as e:
        # Cancellation included: whatever was drained goes back under its
        # original event-time keys, and the watermark did not move. Not after
        # a commit that landed (a failure closing the session, a cancel in
        # `__aexit__`): re-merging then would count the entries twice.
        if not committed:
            acc.merge_back(drained)
        elif isinstance(e, Exception):
            log.error("Concurrency counter flush committed; closing its session "
                      "failed (%s)", type(e).__name__)
            return True
        if isinstance(e, Exception):
            log.error("Concurrency counter flush failed (%s); %d entries kept for retry",
                      type(e).__name__, len(drained))
            return False
        raise
    return True


async def prune(*, now: _dt.datetime | None = None, session_factory=None) -> None:
    """Delete counters and runs older than 35 days (never the current run)."""
    global _last_prune
    t = _utc(now or _now())
    cutoff = t - PRUNE_AFTER
    factory = session_factory or async_session
    async with factory() as session:
        await session.execute(
            delete(ConcurrencyCounter).where(ConcurrencyCounter.bucket_start < cutoff)
        )
        stmt = delete(ConcurrencyRun).where(ConcurrencyRun.completed_through < cutoff)
        if _run is not None:
            stmt = stmt.where(ConcurrencyRun.run_id != _run.run_id)
        await session.execute(stmt)
        await session.commit()
    _last_prune = t


async def _maybe_prune(now: _dt.datetime) -> None:
    if _last_prune is not None and now - _last_prune < PRUNE_INTERVAL:
        return
    try:
        await prune(now=now)
    except Exception as e:  # noqa: BLE001 - housekeeping, never fatal
        log.error("Concurrency counter prune failed (%s)", type(e).__name__)


async def run_flush_loop(interval: float = FLUSH_INTERVAL_SECONDS) -> None:
    """The periodic flush: every `interval` seconds whatever the traffic, so
    the watermark advances on an idle server too. Never dies on an error."""
    while True:
        await asyncio.sleep(interval)
        try:
            await flush()
            await _maybe_prune(_now())
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - the loop must survive
            log.error("Concurrency counter flush loop error (%s)", type(e).__name__)


# ── coverage (design D8 "Coverage and the watermark") ────────────────────


class Coverage(NamedTuple):
    """`watermark`: the latest `completed_through` of any run with the asked
    epoch and mode, **exact** (the caller rounds it down to a whole minute
    before using it as an evaluation end), or None when there is no such run.
    `uncovered`: sorted, merged, non-empty `(from, to)` intervals of
    `[start, end]` that are not covered."""

    watermark: _dt.datetime | None
    uncovered: list


def _merge(intervals):
    out = []
    for lo, hi in sorted(i for i in intervals if i[0] < i[1]):
        if out and lo <= out[-1][1]:
            if hi > out[-1][1]:
                out[-1] = (out[-1][0], hi)
        else:
            out.append((lo, hi))
    return out


def _clip(intervals, start, end):
    return [(max(lo, start), min(hi, end)) for lo, hi in intervals
            if min(hi, end) > max(lo, start)]


def _subtract(base, cuts):
    """`base` minus the union of `cuts`; both merged interval lists."""
    out = []
    for lo, hi in base:
        cursor = lo
        for clo, chi in cuts:
            if chi <= cursor or clo >= hi:
                continue
            if clo > cursor:
                out.append((cursor, clo))
            cursor = max(cursor, chi)
            if cursor >= hi:
                break
        if cursor < hi:
            out.append((cursor, hi))
    return out


def compute_coverage(runs, start, end, epoch: str, mode: str) -> Coverage:
    """Pure core of `coverage()`. `runs` are rows with `run_id, epoch, mode,
    started_at, completed_through, clean_shutdown, lossy`.

    - A run with the asked epoch and mode covers `[started_at,
      completed_through]` unless it is lossy. A run with another epoch or mode,
      or a lossy one, is **uncovered** over that span.
    - The gap from a run's `completed_through` to the next run to start at or
      after it is covered only if the run shut down cleanly (the process was
      not serving); after an unclean end it is uncovered **however short**,
      because the killed run's unflushed tail may hold lost incidents. With no
      later run the gap is open-ended.
    - Uncovered wins: an interval poisoned by one run is not rescued by an
      overlapping one.
    """
    start, end = _utc(start), _utc(end)
    far = _dt.datetime.max.replace(tzinfo=_dt.timezone.utc)
    runs = sorted(runs, key=lambda r: (_utc(r.started_at), str(r.run_id)))
    starts = [_utc(r.started_at) for r in runs]

    covered, poison = [], []
    watermark = None
    for run in runs:
        began, through = _utc(run.started_at), _utc(run.completed_through)
        matching = run.epoch == epoch and run.mode == mode
        if matching:
            watermark = through if watermark is None else max(watermark, through)
        if matching and not run.lossy:
            covered.append((began, through))
        else:
            poison.append((began, through))
        later = [s for s, other in zip(starts, runs)
                 if other is not run and s >= through]
        gap_end = min(later) if later else None
        if run.clean_shutdown and gap_end is not None:
            covered.append((through, gap_end))
        elif not run.clean_shutdown:
            poison.append((through, gap_end or far))

    if end <= start:
        return Coverage(watermark, [])
    window = [(start, end)]
    not_covered = _subtract(window, _merge(_clip(covered, start, end)))
    uncovered = _merge(not_covered + _clip(_merge(poison), start, end))
    return Coverage(watermark, uncovered)


async def coverage(session, start, end, epoch: str, mode: str) -> Coverage:
    """`(watermark, uncovered_intervals)` for `[start, end]` — see
    `compute_coverage`. Reads every run row (bounded by the 35-day prune)."""
    rows = (await session.execute(select(
        ConcurrencyRun.run_id, ConcurrencyRun.epoch, ConcurrencyRun.mode,
        ConcurrencyRun.started_at, ConcurrencyRun.completed_through,
        ConcurrencyRun.clean_shutdown, ConcurrencyRun.lossy,
    ))).all()
    return compute_coverage(rows, start, end, epoch, mode)
