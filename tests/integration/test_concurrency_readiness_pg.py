"""Real-Postgres gate for the concurrency readiness evaluator (#188, D6/D8).

What only a real server shows: that the JSONB predicates select exactly the
v2 rows (legacy rows invisible), read tool pressure from the observations
rather than the code, weigh coalesced `slot_timeout` rows `1 + suppressed`,
keep a queue overrun refused by quota a pre-body refusal; that the
`GROUPING SETS` statement produces tool, class and total rows; and that
whole-minute windows, the watermark clamp and run coverage behave at the
boundaries the design names — driven through the real `register_run` /
`flush` / `coverage` path.

Skipped unless `PGVECTOR_TEST_ADMIN_URL` names a throwaway Postgres server; run
it with `make test-integration`.
"""
from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import delete, insert, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import _harness
from src.models.db import UsageLog, User
from src.services import concurrency
from src.services import concurrency_counters as cc
from src.services import concurrency_readiness as cr

pytestmark = [_harness.requires_pgvector, pytest.mark.asyncio(loop_scope="module")]

EPOCH = "0123456789ab"
OTHER_EPOCH = "ba9876543210"
UTC = dt.timezone.utc


def at(day, hh, mm, ss=0):
    return dt.datetime(2026, 9, day, hh, mm, ss, tzinfo=UTC)


@pytest.fixture(scope="module")
def migrated_url():
    yield from _harness.throwaway_database("concurrency_readiness", 64)


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def maker(migrated_url):
    engine = create_async_engine(migrated_url, pool_size=2, max_overflow=0)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest_asyncio.fixture(loop_scope="module")
async def env(maker):
    async def wipe():
        async with maker() as session:
            await session.execute(text("TRUNCATE concurrency_counters, concurrency_runs"))
            await session.execute(delete(UsageLog))
            await session.execute(delete(User))
            await session.commit()

    await wipe()
    cc.reset_for_tests()
    concurrency.reset_counters()

    def controller(mode="queue", epoch=EPOCH):
        return SimpleNamespace(mode=mode, epoch=epoch,
                               configured_wait_ms=lambda: {"tool": 5000, "transport": 2000})

    async def register(now, mode="queue", epoch=EPOCH):
        return await cc.register_run(controller=controller(mode, epoch), now=now,
                                     session_factory=maker)

    async def flush(now, clean=False):
        assert await cc.flush(clean, now=now, session_factory=maker)

    def kill():
        cc.reset_for_tests()
        concurrency.reset_counters()

    async def rows(items):
        async with maker() as session:
            await session.execute(insert(UsageLog), items)
            await session.commit()

    async def readiness(target, mode, **kw):
        async with maker() as session:
            return await cr.readiness(session, target, controller=controller(mode), **kw)

    async def stats(start, end, mode="queue", epoch=EPOCH, user_id=None):
        async with maker() as session:
            return await cr.window_stats(session, start, end, epoch=epoch, mode=mode,
                                         user_id=user_id)

    yield SimpleNamespace(maker=maker, register=register, flush=flush, kill=kill,
                          rows=rows, readiness=readiness, stats=stats)
    await wipe()
    cc.reset_for_tests()
    concurrency.reset_counters()


def v2(mode="queue", epoch=EPOCH):
    return {"v": 2, "mode": mode, "epoch": epoch}


def row(created_at, tool="read_note", *, mode="queue", epoch=EPOCH, user_id=None, **params):
    params.setdefault("concurrency", v2(mode, epoch))
    if params["concurrency"] is None:
        del params["concurrency"]
    return {"tool": tool, "params": params, "duration_ms": 5, "response_size": 10,
            "user_id": user_id, "created_at": created_at}


def spread(start, n, **kw):
    """`n` rows evenly spread over the day after `start`."""
    step = dt.timedelta(seconds=86000 / max(n, 1))
    return [row(start + i * step, **kw) for i in range(n)]


def verdicts(report):
    return {c["id"]: c["verdict"] for c in report["criteria"]}


def tool_obs(overrun):
    return {"stage": "tool", "scope": "class", "limit": 4, "waited_ms": 30, "overrun": overrun}


# ── the row predicates ──────────────────────────────────────────────────


async def test_rows_legacy_excluded_pressure_from_observations_weighted_refusals(env):
    t = at(20, 12, 0, 30)
    user = User(username="regular", password_hash="x", is_admin=False, is_active=True,
                vault_path="/tmp/vault-7")
    async with env.maker() as session:
        session.add(user)
        await session.commit()
        uid = user.id
    shadow_meta = {
        "shadow": True, "schema": 2, "code": "request_concurrency_limited",
        # The tool observation is *not* the first one: still tool-pressured.
        "observations": [{"stage": "request", "scope": "global", "limit": 64},
                         {"stage": "tool", "scope": "class", "limit": 4}],
    }
    await env.rows([
        # Legacy rows, pressured and not: invisible to every figure.
        row(t, concurrency=None, queue_ms=0),
        row(t, concurrency=None, queue_ms=900, concurrency_shadow=shadow_meta),
        # v2 shadow rows: one tool-pressured via a non-first observation.
        row(t, mode="shadow", queue_ms=0, concurrency_shadow=shadow_meta),
        row(t, mode="shadow", queue_ms=0),
        # A coalesced enforced slot_timeout row standing for 1 + 4 refusals.
        row(t, "keyword_search", mode="enforce", error="slot_timeout", suppressed=4,
            queue_ms=5000),
        # Queue: an overrun that executed, and one then refused by quota.
        row(t, "keyword_search", queue_ms=5100, user_id=uid,
            concurrency_queue={"schema": 2, "overrun": True, "code": "slot_timeout",
                               "observations": [tool_obs(True)]}),
        row(t, "keyword_search", queue_ms=5200, over_quota=True,
            concurrency_queue={"schema": 2, "overrun": True, "code": "slot_timeout",
                               "observations": [tool_obs(True)]}),
        # A malformed queue_ms must not abort the statement.
        row(t, "keyword_search", queue_ms="abc"),
    ])
    start, end = at(20, 12, 0), at(20, 12, 1)
    async with env.maker() as session:
        table = await cr.tool_table(session, start, end)
    tools = {r["tool"]: r for r in table["tools"]}
    assert tools["read_note"]["executed"] == 2          # legacy rows invisible
    assert tools["read_note"]["tool_pressured"] == 1    # read from observations
    ks = tools["keyword_search"]
    assert ks["slot_timeouts"] == 5                     # 1 + suppressed
    assert ks["overruns"] == 2                          # both overrun rows
    assert ks["executed"] == 2                          # over_quota + slot_timeout are pre-body
    # The maximum is over every row carrying queue_ms, the quota-refused
    # overrun and the enforced slot_timeout included (impl-R2-1).
    assert ks["max"] == 5200.0
    classes = {r["cls"]: r for r in table["classes"]}
    assert classes["scan"]["overruns"] == 2 and classes["light"]["executed"] == 2
    assert table["total"]["executed"] == 4

    async with env.maker() as session:
        mine = await cr.tool_table(session, start, end, user_id=uid)
    assert [(r["tool"], r["executed"]) for r in mine["tools"]] == [("keyword_search", 1)]

    # Restricted to one configuration: queue rows only.
    q = await env.stats(start, end)
    # The overrun that executed and the malformed-queue_ms row are executed;
    # the over_quota overrun is pre-body but still an overrun, and its wait is
    # still the maximum (impl-R2-1).
    assert (q.executed, q.tool_overruns, q.queue_max_ms) == (2, 2, 5200.0)
    assert {(f["mode"], f["rows"]) for f in q.foreign} == {("shadow", 2), ("enforce", 1)}


# ── windows, coverage and the watermark ─────────────────────────────────


async def _covered_shadow_days(env, start, days, per_day=110):
    """A shadow run covering `days` from `start`, with v2 rows and requests."""
    await env.register(start, mode="shadow")
    for d in range(days):
        day = start + dt.timedelta(days=d)
        await env.rows(spread(day + dt.timedelta(minutes=5), per_day, mode="shadow",
                              queue_ms=0))
        concurrency.counters().record("requests", per_day, at=day + dt.timedelta(hours=1))
        await env.flush(day + dt.timedelta(days=1))


async def test_two_runs_in_one_minute_do_not_leak_into_the_earlier_window(env):
    """Run A shuts down cleanly at 12:00:20; run B (same configuration)
    starts at 12:00:40 and has a pool timeout at 12:00:50. A window ending at
    A's shutdown rounds down to 12:00 and excludes it; one ending at 12:01
    includes it and fails Q3."""
    base = at(10, 12, 0)
    await _covered_shadow_days(env, base, 3)
    end_a = base + dt.timedelta(days=3, seconds=20)
    await env.flush(end_a, clean=True)
    env.kill()
    await env.register(base + dt.timedelta(days=3, seconds=40), mode="shadow")
    concurrency.counters().record("pool_checkout_timeout",
                                  at=base + dt.timedelta(days=3, seconds=50))
    await env.flush(base + dt.timedelta(days=3, minutes=1, seconds=5))

    early = await env.readiness("queue", "shadow", end=end_a)
    assert early["window"]["end"] == (base + dt.timedelta(days=3)).isoformat()
    assert verdicts(early) == {"Q1": "PASS", "Q2": "PASS", "Q3": "PASS"}

    late = await env.readiness("queue", "shadow", end=base + dt.timedelta(days=3, minutes=1))
    assert verdicts(late)["Q3"] == "FAIL"
    # The default end is the watermark rounded down: 12:01 on day 3.
    default = await env.readiness("queue", "shadow")
    assert default["window"]["end"] == (base + dt.timedelta(days=3, minutes=1)).isoformat()
    assert verdicts(default)["Q3"] == "FAIL"


async def test_incidents_at_both_window_boundaries(env):
    """Pool timeouts at 11:59:50 (just before a window starting 12:00) and at
    12:59:50 (inside a window ending 13:00 whose watermark is 13:00)."""
    start = at(10, 12, 0)
    await env.register(start - dt.timedelta(minutes=5), mode="shadow")
    concurrency.counters().record("pool_checkout_timeout", at=at(10, 11, 59, 50))
    concurrency.counters().record("pool_checkout_timeout", at=at(10, 12, 59, 50))
    await env.flush(at(10, 13, 0, 30))
    w = await env.stats(start, at(10, 13, 0), mode="shadow")
    assert w.count("pool_checkout_timeout") == 1
    assert w.uncovered == []
    before = await env.stats(at(10, 11, 0), start, mode="shadow")
    assert before.count("pool_checkout_timeout") == 1
    # Through the evaluator: a qualifying 3-day window ending at 13:00.
    await env.rows(spread(at(8, 13, 0), 320, mode="shadow", queue_ms=0))
    async with env.maker() as session:
        await session.execute(text("UPDATE concurrency_runs SET started_at = :s"),
                              {"s": at(7, 12, 0)})
        await session.commit()
    concurrency.counters().record("requests", 320, at=at(8, 14, 0))
    await env.flush(at(10, 13, 0, 40))
    # [day 7 12:00, day 10 12:00]: the 11:59:50 timeout is inside, the
    # 12:59:50 one outside.
    report = await env.readiness("queue", "shadow", end=at(10, 12, 0))
    assert report["window"]["uncovered"] == []
    assert verdicts(report)["Q3"] == "FAIL"
    q3 = next(c for c in report["criteria"] if c["id"] == "Q3")
    assert q3["inputs"]["pool_checkout_timeout"] == 1


async def test_a_quick_restart_after_a_hard_kill_is_not_a_pass(env):
    """Flush at 12:00, transport overrun at 12:00:20, killed at 12:00:40 with
    no shutdown flush, a same-settings run at 12:01: a window containing
    12:00–12:01 is INSUFFICIENT_DATA for every enforce criterion."""
    base = at(1, 12, 0)
    await env.register(base, mode="queue")
    for d in range(7):
        day = base + dt.timedelta(days=d)
        await env.rows(spread(day + dt.timedelta(minutes=5), 150, queue_ms=3))
        concurrency.counters().record("requests", 150, at=day + dt.timedelta(hours=1))
        await env.flush(day + dt.timedelta(days=1))
    # The last flush was at `end` (the watermark). The overrun lands 20 s
    # later, the process is killed at +40 s with no shutdown flush, and a
    # same-settings run starts at +60 s: a gap far under 180 s.
    end = base + dt.timedelta(days=7)
    concurrency.counters().record("transport_overrun", at=end + dt.timedelta(seconds=20))
    env.kill()
    await env.register(end + dt.timedelta(minutes=1), mode="queue")
    await env.flush(end + dt.timedelta(minutes=10, seconds=5))

    report = await env.readiness("enforce", "queue")  # [base+10 min, end+10 min]
    assert set(verdicts(report).values()) == {"INSUFFICIENT_DATA"}
    assert report["window"]["uncovered"], report["window"]
    lo, hi = report["window"]["uncovered"][0]
    assert lo == end.isoformat() and hi == (end + dt.timedelta(minutes=1)).isoformat()

    # A window that ends before the kill is covered, and passes.
    before = await env.readiness("enforce", "queue", end=end)
    assert before["window"]["uncovered"] == []
    assert before["overall"] == "PASS", before["criteria"]


async def test_e5_counts_the_wait_of_a_call_refused_by_quota(env):
    """impl-R2-1: a covered single-epoch queue window, 1,000+ executed calls
    with queue_ms 0 in the last 72 h and one tool-overrun call that waited
    5,100 ms and was then refused by the daily quota. The wait is over half the
    5,000 ms tool deadline, so E5 FAILs although the call never executed —
    and E4 (executed-only p99) and every other criterion stay PASS."""
    base = at(1, 12, 0)
    await env.register(base, mode="queue")
    for d in range(7):
        day = base + dt.timedelta(days=d)
        if d >= 4:  # the last 72 h hold every executed call
            await env.rows(spread(day + dt.timedelta(minutes=5), 334, queue_ms=0))
            concurrency.counters().record("requests", 334, at=day + dt.timedelta(hours=1))
        await env.flush(day + dt.timedelta(days=1))
    await env.rows([row(base + dt.timedelta(days=6, hours=6), "keyword_search",
                        queue_ms=5100, over_quota=True,
                        concurrency_queue={"schema": 2, "overrun": True,
                                           "code": "slot_timeout",
                                           "observations": [tool_obs(True)]})])
    report = await env.readiness("enforce", "queue", end=base + dt.timedelta(days=7))
    assert report["window"]["uncovered"] == [] and report["window"]["foreign"] == []
    assert report["window"]["executed"] == 1002
    assert report["window"]["queue_max_ms"] == 5100.0
    assert report["last_72h"]["queue_max_ms"] == 5100.0
    v = verdicts(report)
    assert v["E5"] == "FAIL", report["criteria"]
    assert {k: x for k, x in v.items() if k != "E5"} == {
        "E1": "PASS", "E2": "PASS", "E3": "PASS", "E4": "PASS", "E6": "PASS"}
    assert report["overall"] == "FAIL"


async def test_a_clean_recreate_stays_covered(env):
    base = at(1, 12, 0)
    await env.register(base, mode="queue")
    await env.flush(base + dt.timedelta(hours=1, seconds=10), clean=True)
    env.kill()
    await env.register(base + dt.timedelta(hours=1, seconds=50), mode="queue")
    await env.flush(base + dt.timedelta(hours=2, seconds=5))
    w = await env.stats(base, base + dt.timedelta(hours=2))
    assert w.uncovered == []


async def test_an_unflushed_tail_is_not_certified(env):
    base = at(1, 12, 0)
    await env.register(base, mode="shadow")
    await env.flush(base + dt.timedelta(days=3, seconds=30))  # watermark day3 12:00
    # A pool timeout after the watermark, recorded but never flushed.
    concurrency.counters().record("pool_checkout_timeout",
                                  at=base + dt.timedelta(days=3, seconds=40))
    past = await env.readiness("queue", "shadow",
                               end=base + dt.timedelta(days=3, minutes=1))
    assert set(verdicts(past).values()) == {"INSUFFICIENT_DATA"}
    assert "watermark" in past["criteria"][0]["reason"]
    default = await env.readiness("queue", "shadow")
    assert default["window"]["end"] == (base + dt.timedelta(days=3)).isoformat()
    assert default["window"]["counters"].get("pool_checkout_timeout", 0) == 0


async def test_a_mixed_epoch_window_is_insufficient_with_the_qualifying_start(env):
    base = at(1, 12, 0)
    await env.register(base, mode="queue")
    for d in range(7):
        day = base + dt.timedelta(days=d)
        await env.rows(spread(day + dt.timedelta(minutes=5), 150, queue_ms=3))
        concurrency.counters().record("requests", 150, at=day + dt.timedelta(hours=1))
        await env.flush(day + dt.timedelta(days=1))
    # Day 2: rows from another epoch (and shadow mode) inside the window.
    stray = base + dt.timedelta(days=1, hours=3, seconds=10)
    await env.rows([row(stray, epoch=OTHER_EPOCH, queue_ms=0),
                    row(stray, mode="shadow", queue_ms=0)])
    report = await env.readiness("enforce", "queue")
    assert set(verdicts(report).values()) == {"INSUFFICIENT_DATA"}
    assert report["window"]["qualifying_since"] == (
        stray + dt.timedelta(seconds=50)).isoformat()
    assert sorted(report["window"]["epochs_present"]) == sorted([EPOCH, OTHER_EPOCH])
    assert set(report["window"]["modes_present"]) == {"queue", "shadow"}


async def test_the_panel_section_runs_against_real_postgres(env):
    now = dt.datetime.now(UTC)
    await env.register(now - dt.timedelta(hours=2), mode="shadow")
    await env.rows([row(now - dt.timedelta(minutes=30), mode="shadow", queue_ms=0)])
    await env.flush(now - dt.timedelta(minutes=1))

    class Ctl:
        mode, epoch = "shadow", EPOCH

        def snapshot(self):
            return {"mode": "shadow", "epoch": EPOCH}

        def configured_wait_ms(self):
            return {"tool": 5000, "transport": 2000}

    async with env.maker() as session:
        section = await cr.panel_section(session, 86400, user_id=None, is_admin=True,
                                         controller=Ctl(), now=now)
    assert section["has_data"] and section["tools"][0]["tool"] == "read_note"
    admin = section["admin"]
    assert admin["target"] == "queue" and admin["watermark"] is not None
    assert admin["verdict"]["overall"] == "INSUFFICIENT_DATA"  # < 3 days of evidence
